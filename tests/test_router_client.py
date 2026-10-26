"""The router client against an in-process fake router; never the real router."""

import logging
import socket
import uuid

import pytest

import router_client
from router_client import (CLASSIFICATION_ROUTE, GUEST_ROUTE, RouterClient, RouterError, RouterResult,
                           estimated_context_tokens)
from tests.router_fake import MODEL, TOKEN, FakeRouter, fake_router  # noqa: F401
from tests.router_schema_validator import estimate_tokens, provider_messages

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["ok"],
          "properties": {"ok": {"type": "boolean"}}}
MESSAGES = [{"role": "system", "content": "Answer as JSON."}, {"role": "user", "content": "Is this synthetic?"}]


def client(router, **options):
    return RouterClient(router.url, token_file=router.token_file, expected_model=MODEL, **options)


def call(router, key="podcast.test:1", **options):
    return client(router, **options).run(CLASSIFICATION_ROUTE, MESSAGES, SCHEMA, idempotency_key=key,
                                         prompt_version="test-prompt/v1", max_output_tokens=64, deadline=30)


def failure(router, **options):
    with pytest.raises(RouterError) as caught:
        call(router, **options)
    return caught.value


def closed_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_success_sends_the_governed_task_and_returns_parsed_output(fake_router):  # noqa: F811
    result = call(fake_router)
    assert result == RouterResult({"ok": True}, MODEL, {"input_tokens": 100, "output_tokens": 20, "latency_ms": 1500},
                                  False)
    method, path, headers, body = fake_router.requests[-1]
    assert (method, path) == ("POST", "/v1/model-tasks")
    assert headers["Authorization"] == f"Bearer {TOKEN}" and headers["Idempotency-Key"] == "podcast.test:1"
    assert headers["Content-Type"] == "application/json"
    assert body == {
        "task_id": str(uuid.uuid5(router_client._TASK_NAMESPACE, "podcast.test:1")),
        "task_type": "structured_generation", "route": "podcast.classification", "principal": "intelligence:podcast",
        "messages": MESSAGES, "prompt_version": "test-prompt/v1",
        "requirements": {"local_only": True, "max_latency_ms": 30_000, "max_cost_usd": 0,
                         "minimum_context_tokens": 2048, "required_modalities": ["text"],
                         "data_classification": "internal"},
        "generation": {"temperature": 0, "max_output_tokens": 64}, "response_schema": SCHEMA}


def test_a_repeated_key_replays_instead_of_generating_twice(fake_router):  # noqa: F811
    first, second = call(fake_router), call(fake_router)
    assert first.parsed == second.parsed and (first.replayed, second.replayed) == (False, True)
    assert fake_router.generations == 1


@pytest.mark.parametrize("status, payload, category, advance", [
    (429, FakeRouter.error("MODEL_RATE_LIMITED"), "router_busy", False),
    (202, {"status": "in_progress", "invocation_id": "inv-1", "trace_id": "t"}, "router_busy", False),
    (503, FakeRouter.error("MODEL_NO_ELIGIBLE_CANDIDATE"), "router_busy", True),
    (504, FakeRouter.error("MODEL_TIMEOUT"), "router_busy", True),
    (500, FakeRouter.error("MODEL_ROUTER_INTERNAL"), "router_busy", True),
    (502, FakeRouter.error("MODEL_PROVIDER_UNAVAILABLE"), "router_busy", True),
    (502, b"", "router_busy", False),
    (503, b"<html>proxy</html>", "router_busy", False),
    (502, FakeRouter.error("MODEL_OUTPUT_INVALID"), "model_invalid_output", True),
    (401, FakeRouter.error("MODEL_UNAUTHORIZED"), "model_auth", True),
    (403, FakeRouter.error("MODEL_ROUTE_FORBIDDEN"), "model_auth", True),
    (400, FakeRouter.error("MODEL_TASK_INVALID"), "model_config", True),
    (404, FakeRouter.error("MODEL_ROUTE_NOT_FOUND"), "model_config", True),
    (409, FakeRouter.error("LAMAD_IDEMPOTENCY_CONFLICT"), "model_config", True),
    (413, FakeRouter.error("MODEL_CONTEXT_TOO_LARGE"), "model_config", True),
])
def test_router_responses_map_to_closed_categories(fake_router, status, payload, category, advance):  # noqa: F811
    fake_router.reply(status, payload)
    error = failure(fake_router)
    retryable = category in {"router_busy", "model_invalid_output"}
    assert (error.category, error.retryable, error.advance_key) == (category, retryable, advance)
    assert str(error) == category and "sanitized" not in repr(error)


def test_a_stored_busy_outcome_replays_until_the_key_advances(fake_router):  # noqa: F811
    fake_router.reply(503, FakeRouter.error("MODEL_NO_ELIGIBLE_CANDIDATE"))
    assert failure(fake_router).advance_key is True
    assert failure(fake_router).category == "router_busy"  # the stored 503, replayed
    assert call(fake_router, key="podcast.test:2").parsed == {"ok": True}


@pytest.mark.parametrize("route, content, max_output_tokens", [
    (CLASSIFICATION_ROUTE, "x" * 130_000, 8192),  # the router's estimate is over the 32,768-token context
    (GUEST_ROUTE, "Is this synthetic?", 1025),     # over the guest route's 1,024-token output budget
])
def test_a_task_too_large_for_the_route_is_refused_before_sending(fake_router, route, content,  # noqa: F811
                                                                   max_output_tokens):
    with pytest.raises(RouterError) as caught:
        client(fake_router).run(route, [{"role": "user", "content": content}], SCHEMA, idempotency_key="k",
                                prompt_version="v", max_output_tokens=max_output_tokens, deadline=30)
    assert (caught.value.category, caught.value.retryable) == ("model_config", False)
    assert not fake_router.requests  # never a stored 503 MODEL_NO_ELIGIBLE_CANDIDATE that loops as busy


def test_a_task_that_exactly_fits_the_route_is_sent(fake_router):  # noqa: F811
    contract = provider_messages([{"role": "user", "content": ""}], SCHEMA)[0]["content"]
    messages = [{"role": "user", "content": "x" * ((32_768 - 8_192 - 8) * 4 - len(contract))}]
    assert estimated_context_tokens(messages, SCHEMA, 8192) == 32_768 == fake_router.context_tokens
    assert client(fake_router).run(CLASSIFICATION_ROUTE, messages, SCHEMA, idempotency_key="k", prompt_version="v",
                                   max_output_tokens=8192, deadline=30).parsed == {"ok": True}


def test_no_eligible_candidate_for_a_fitting_task_is_busy_and_advances(fake_router):  # noqa: F811
    fake_router.context_tokens = 100  # stands in for an open provider circuit or an unavailable model
    error = failure(fake_router)
    assert (error.category, error.advance_key) == ("router_busy", True)
    assert failure(fake_router).category == "router_busy" and fake_router.generations == 0  # stored, replayed


def test_output_that_breaks_the_schema_is_invalid_output(fake_router):  # noqa: F811
    fake_router.output = {"ok": "yes"}
    assert failure(fake_router).category == "model_invalid_output"


@pytest.mark.parametrize("payload", [
    b"not json", b'{"status":"succeeded","status":"succeeded"}',
    {"status": "succeeded", "selected_model": MODEL},
    {"status": "failed", "selected_model": MODEL, "parsed_output": {}},
])
def test_malformed_success_bodies_are_invalid_output(fake_router, payload):  # noqa: F811
    fake_router.reply(200, payload)
    assert failure(fake_router).category == "model_invalid_output"


def test_a_different_model_is_a_permanent_mismatch(fake_router):  # noqa: F811
    fake_router.model = "qwen3:8b"
    error = failure(fake_router)
    assert (error.category, error.retryable) == ("model_mismatch", False)


def test_timeouts_and_refused_connections_are_busy_without_advancing(fake_router):  # noqa: F811
    fake_router.delay = 1.0
    error = failure(fake_router, read_timeout=0.2)
    assert (error.category, error.advance_key) == ("router_busy", False)
    refused = RouterClient(f"http://127.0.0.1:{closed_port()}", token_file=fake_router.token_file,
                           expected_model=MODEL)
    with pytest.raises(RouterError) as caught:
        refused.run(GUEST_ROUTE, MESSAGES, SCHEMA, idempotency_key="k", prompt_version="v",
                    max_output_tokens=64, deadline=5)
    assert (caught.value.category, caught.value.advance_key) == ("router_busy", False)


@pytest.mark.parametrize("content", ["", "short", "bad token with spaces " * 2, "x" * 513])
def test_an_unusable_token_fails_as_auth_before_any_request(fake_router, tmp_path, content):  # noqa: F811
    path = tmp_path / "bad-token"
    path.write_text(content)
    bad = RouterClient(fake_router.url, token_file=str(path), expected_model=MODEL)
    with pytest.raises(RouterError) as caught:
        bad.run(CLASSIFICATION_ROUTE, MESSAGES, SCHEMA, idempotency_key="k", prompt_version="v",
                max_output_tokens=64, deadline=30)
    assert caught.value.category == "model_auth" and not fake_router.requests
    assert bad.token_available() is False


def test_a_missing_token_file_fails_as_auth(fake_router, tmp_path):  # noqa: F811
    missing = RouterClient(fake_router.url, token_file=str(tmp_path / "absent"), expected_model=MODEL)
    with pytest.raises(RouterError) as caught:
        missing.run(CLASSIFICATION_ROUTE, MESSAGES, SCHEMA, idempotency_key="k", prompt_version="v",
                    max_output_tokens=64, deadline=30)
    assert caught.value.category == "model_auth"
    assert client(fake_router).token_available() is True


@pytest.mark.parametrize("change", [
    dict(route="podcast.other"), dict(idempotency_key="has space"), dict(idempotency_key="k" * 201),
    dict(deadline=4), dict(deadline=901), dict(max_output_tokens=8193), dict(max_output_tokens=15),
    dict(prompt_version=""), dict(messages=[]),
    dict(messages=[{"role": "user", "content": "x" * 131_073}]),
])
def test_requests_the_router_would_refuse_are_never_sent(fake_router, change):  # noqa: F811
    arguments = dict(route=CLASSIFICATION_ROUTE, messages=MESSAGES, schema=SCHEMA, idempotency_key="k",
                     prompt_version="v", max_output_tokens=64, deadline=30)
    arguments.update(change)
    route, messages, schema = (arguments.pop(name) for name in ("route", "messages", "schema"))
    with pytest.raises(RouterError) as caught:
        client(fake_router).run(route, messages, schema, **arguments)
    assert caught.value.category == "model_config" and not fake_router.requests


def test_an_oversized_body_is_refused_before_sending(fake_router, monkeypatch):  # noqa: F811
    monkeypatch.setattr(router_client, "MAX_REQUEST_BYTES", 100)
    assert failure(fake_router).category == "model_config" and not fake_router.requests


def test_the_token_never_reaches_errors_or_logs(fake_router, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    fake_router.reply(503, FakeRouter.error("MODEL_NO_ELIGIBLE_CANDIDATE"))
    error = failure(fake_router)
    call(fake_router, key="podcast.test:3")
    assert TOKEN not in repr(error) + repr(vars(error)) + caplog.text


def test_environment_proxies_are_ignored(fake_router, monkeypatch):  # noqa: F811
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{closed_port()}")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    assert call(fake_router).parsed == {"ok": True}


def test_readiness_needs_a_ready_router_and_fails_fast(fake_router):  # noqa: F811
    assert client(fake_router).ready() is True
    assert "Authorization" not in fake_router.requests[-1][2]
    fake_router.ready = False  # e.g. Forge unavailable: /readyz answers 503
    assert client(fake_router).ready() is False
    assert RouterClient(f"http://127.0.0.1:{closed_port()}", token_file="unused",
                        expected_model=MODEL).ready() is False


def test_the_context_estimate_matches_the_pinned_router_formula():
    messages = [{"role": "system", "content": "s" * 1000}, {"role": "user", "content": "u" * 5000}]
    assert estimated_context_tokens(messages, SCHEMA, 8192) == estimate_tokens(
        provider_messages(messages, SCHEMA)) + 8192
    assert estimated_context_tokens(messages[1:], SCHEMA, 64) == estimate_tokens(
        provider_messages(messages[1:], SCHEMA)) + 64
