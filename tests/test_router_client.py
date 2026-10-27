"""The router client against an in-process fake router; never the real router."""

import collections
import gc
import hashlib
import logging
import types
import socket
import ssl
import threading
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


@pytest.mark.parametrize("status, payload, category, reason, advance", [
    (429, FakeRouter.error("MODEL_RATE_LIMITED"), "router_busy", "rate_limited", False),
    (202, {"status": "in_progress", "invocation_id": "inv-1", "trace_id": "t"}, "router_busy", "in_progress", False),
    (503, FakeRouter.error("MODEL_NO_ELIGIBLE_CANDIDATE"), "router_busy", "no_candidate", True),
    (504, FakeRouter.error("MODEL_TIMEOUT"), "router_busy", "router_failure", True),
    (500, FakeRouter.error("MODEL_ROUTER_INTERNAL"), "router_busy", "router_failure", True),
    (502, FakeRouter.error("MODEL_PROVIDER_UNAVAILABLE"), "router_busy", "provider_unavailable", True),
    (502, b"", "router_busy", "proxy", False),
    (503, b"<html>proxy</html>", "router_busy", "proxy", False),
    (502, FakeRouter.error("MODEL_OUTPUT_INVALID"), "model_invalid_output", "output_invalid", True),
    (401, FakeRouter.error("MODEL_UNAUTHORIZED"), "model_auth", "unauthorized", True),
    (403, FakeRouter.error("MODEL_ROUTE_FORBIDDEN"), "model_auth", "forbidden", True),
    (400, FakeRouter.error("MODEL_TASK_INVALID"), "model_config", "rejected", True),
    (404, FakeRouter.error("MODEL_ROUTE_NOT_FOUND"), "model_config", "rejected", True),
    (409, FakeRouter.error("LAMAD_IDEMPOTENCY_CONFLICT"), "model_config", "conflict", True),
    (409, FakeRouter.error("SOME_OTHER_CONFLICT"), "model_config", "rejected", True),
    (409, b"", "model_config", "rejected", True),
    (413, FakeRouter.error("MODEL_CONTEXT_TOO_LARGE"), "model_config", "rejected", True),
])
def test_router_responses_map_to_closed_categories(fake_router, status, payload, category, reason,  # noqa: F811
                                                   advance):
    fake_router.reply(status, payload)
    error = failure(fake_router)
    retryable = category in {"router_busy", "model_invalid_output"}
    assert (error.category, error.reason, error.retryable, error.advance_key) == (category, reason, retryable,
                                                                                  advance)
    assert error.reason in router_client.REASONS
    assert str(error) == category and "sanitized" not in repr(error)


def test_a_key_still_running_on_the_router_replays_in_progress(fake_router):  # noqa: F811
    # A generation the router never finished (it restarted mid-call): its store keeps the key in progress.
    sent = router_client._canonical(fake_router_body("podcast.test:1")).encode("utf-8")
    with fake_router._lock:
        fake_router._stored["podcast.test:1"] = (hashlib.sha256(sent).hexdigest(), None, "inv-stuck")
    error = failure(fake_router)
    assert (error.category, error.reason, error.advance_key) == ("router_busy", "in_progress", False)
    assert fake_router.generations == 0


def fake_router_body(key):
    return {"task_id": str(uuid.uuid5(router_client._TASK_NAMESPACE, key)), "task_type": "structured_generation",
            "route": CLASSIFICATION_ROUTE, "principal": "intelligence:podcast", "messages": MESSAGES,
            "prompt_version": "test-prompt/v1",
            "requirements": {"local_only": True, "max_latency_ms": 30_000, "max_cost_usd": 0,
                             "minimum_context_tokens": 2048, "required_modalities": ["text"],
                             "data_classification": "internal"},
            "generation": {"temperature": 0, "max_output_tokens": 64}, "response_schema": SCHEMA}


def test_reasons_are_closed_and_optional():
    assert RouterError("router_busy").reason is None
    assert RouterError("router_busy", reason="in_progress", advance_key=False).reason == "in_progress"
    assert RouterError("router_busy", reason="free text from a response").reason is None
    assert RouterError("not a category").category == "model_config"


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
    assert (caught.value.category, caught.value.reason, caught.value.retryable) == ("model_config", "too_large",
                                                                                    False)
    assert not fake_router.requests  # never a stored 503 MODEL_NO_ELIGIBLE_CANDIDATE that loops as busy


def test_sixty_four_messages_in_known_roles_are_sent(fake_router):  # noqa: F811
    messages = [{"role": ("system", "user", "assistant")[min(index, 1 + index % 2)], "content": "x"}
                for index in range(64)]
    assert client(fake_router).run(CLASSIFICATION_ROUTE, messages, SCHEMA, idempotency_key="k", prompt_version="v",
                                   max_output_tokens=64, deadline=30).parsed == {"ok": True}


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
    assert (error.category, error.reason, error.retryable) == ("model_mismatch", "mismatch", False)


def test_timeouts_and_refused_connections_are_busy_without_advancing(fake_router):  # noqa: F811
    fake_router.delay = 1.0
    error = failure(fake_router, read_timeout=0.2)
    assert (error.category, error.reason, error.advance_key) == ("router_busy", "timeout", False)
    refused = RouterClient(f"http://127.0.0.1:{closed_port()}", token_file=fake_router.token_file,
                           expected_model=MODEL)
    with pytest.raises(RouterError) as caught:
        refused.run(GUEST_ROUTE, MESSAGES, SCHEMA, idempotency_key="k", prompt_version="v",
                    max_output_tokens=64, deadline=5)
    assert (caught.value.category, caught.value.reason, caught.value.advance_key) == ("router_busy", "transport",
                                                                                      False)


@pytest.mark.parametrize("content", ["", "short", "bad token with spaces " * 2, "x" * 513])
def test_an_unusable_token_fails_as_auth_before_any_request(fake_router, tmp_path, content):  # noqa: F811
    path = tmp_path / "bad-token"
    path.write_text(content)
    bad = RouterClient(fake_router.url, token_file=str(path), expected_model=MODEL)
    with pytest.raises(RouterError) as caught:
        bad.run(CLASSIFICATION_ROUTE, MESSAGES, SCHEMA, idempotency_key="k", prompt_version="v",
                max_output_tokens=64, deadline=30)
    assert (caught.value.category, caught.value.reason) == ("model_auth", "token") and not fake_router.requests
    assert caught.value.__context__ is None and caught.value.__cause__ is None
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
    dict(messages=[{"role": "user", "content": "x"}] * 65),
    dict(messages=[{"role": "tool", "content": "x"}]),
    dict(messages=[{"role": "developer", "content": "x"}]),
])
def test_requests_the_router_would_refuse_are_never_sent(fake_router, change):  # noqa: F811
    arguments = dict(route=CLASSIFICATION_ROUTE, messages=MESSAGES, schema=SCHEMA, idempotency_key="k",
                     prompt_version="v", max_output_tokens=64, deadline=30)
    arguments.update(change)
    route, messages, schema = (arguments.pop(name) for name in ("route", "messages", "schema"))
    with pytest.raises(RouterError) as caught:
        client(fake_router).run(route, messages, schema, **arguments)
    assert (caught.value.category, caught.value.reason) == ("model_config", "refused") and not fake_router.requests


def test_an_oversized_body_is_refused_before_sending(fake_router, monkeypatch):  # noqa: F811
    monkeypatch.setattr(router_client, "MAX_REQUEST_BYTES", 100)
    assert failure(fake_router).category == "model_config" and not fake_router.requests


_OPAQUE = (type, types.ModuleType, types.FunctionType, types.BuiltinFunctionType, types.MethodType,
           types.CodeType, types.FrameType)


def _children(value):
    if isinstance(value, dict):
        return [*value.keys(), *value.values()]
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    own = getattr(value, "__dict__", None)
    children = []
    for referent in gc.get_referents(value):
        children.extend(referent.values() if referent is own and isinstance(own, dict) else [referent])
    return children


_WALK_LIMIT = 200_000  # nodes; reaching it fails the test rather than passing it unexamined
_SKIPPED = (FakeRouter, threading.Thread, threading.Event, threading.Condition, threading.Semaphore,
            type(threading.Lock()), type(threading.RLock()))


def token_reachable(error, limit=_WALK_LIMIT):
    """Whether the token is reachable from any client frame of the traceback, following references.

    The walk has no depth limit but a node cap. Frames in this test module are skipped: they
    legitimately hold the fake router, which keeps the requests it received. FakeRouter instances
    (reachable through the fake's server threads) and threading objects are skipped for the same
    reason; nothing in the client stores the token in either.
    """
    needle, pending, traceback = TOKEN, collections.deque(), error.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_code.co_filename != __file__:
            pending.extend(traceback.tb_frame.f_locals.values())
        traceback = traceback.tb_next
    pending.extend(vars(error).values())
    seen = set()
    while pending:
        value = pending.popleft()
        if id(value) in seen or isinstance(value, _OPAQUE) or isinstance(value, _SKIPPED):
            continue
        seen.add(id(value))
        assert len(seen) <= limit, "reference walk exceeded its node cap"
        if isinstance(value, str):
            if needle in value:
                return True
        elif isinstance(value, (bytes, bytearray)):
            if needle.encode() in value:
                return True
        elif not isinstance(value, (int, float, bool, type(None))):
            pending.extend(_children(value))
    return False


def frame_values(error):
    values, traceback = [], error.__traceback__
    while traceback is not None:
        values.extend(repr(value) for value in traceback.tb_frame.f_locals.values())
        traceback = traceback.tb_next
    return " ".join(values)


def test_the_token_never_reaches_errors_or_logs(fake_router, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    fake_router.reply(503, FakeRouter.error("MODEL_NO_ELIGIBLE_CANDIDATE"))
    error = failure(fake_router)
    call(fake_router, key="podcast.test:3")
    assert TOKEN not in repr(error) + repr(vars(error)) + caplog.text + frame_values(error)
    assert error.__context__ is None and error.__cause__ is None


@pytest.mark.parametrize("scenario", ["timeout", "refused", "bad_body"])
def test_transport_failures_carry_no_exception_chain_or_token(fake_router, scenario):  # noqa: F811
    if scenario == "timeout":  # requests' ReadTimeout holds the prepared request and its Authorization header
        fake_router.delay = 1.0
        error = failure(fake_router, read_timeout=0.2)
    elif scenario == "refused":
        refused = RouterClient(f"http://127.0.0.1:{closed_port()}", token_file=fake_router.token_file,
                               expected_model=MODEL)
        with pytest.raises(RouterError) as caught:
            refused.run(GUEST_ROUTE, MESSAGES, SCHEMA, idempotency_key="k", prompt_version="v",
                        max_output_tokens=64, deadline=5)
        error = caught.value
    else:
        fake_router.reply(200, b"not json")
        error = failure(fake_router)
    assert error.__context__ is None and error.__cause__ is None
    assert TOKEN not in frame_values(error)


@pytest.mark.parametrize("scenario", ["busy_503", "mismatch", "malformed_200", "missing_output", "timeout",
                                      "adapter_type_error", "read_type_error"])
def test_no_reference_from_a_client_frame_reaches_the_token(fake_router, monkeypatch, scenario):  # noqa: F811
    import requests
    options = {}
    if scenario == "busy_503":
        fake_router.reply(503, FakeRouter.error("MODEL_NO_ELIGIBLE_CANDIDATE"))
    elif scenario == "mismatch":
        fake_router.model = "qwen3:8b"
    elif scenario == "malformed_200":
        fake_router.reply(200, b"not json")
    elif scenario == "missing_output":
        fake_router.reply(200, {"status": "succeeded", "selected_model": MODEL})
    elif scenario == "timeout":
        fake_router.delay, options = 1.0, {"read_timeout": 0.2}
    elif scenario == "adapter_type_error":
        def broken_send(self, request, **kwargs):
            raise TypeError("adapter bug")
        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", broken_send)
    else:
        def broken_iter(self, *args, **kwargs):
            raise TypeError("decoder bug")
        monkeypatch.setattr(requests.models.Response, "iter_content", broken_iter)
    error = failure(fake_router, **options)
    expected = {"adapter_type_error": ("model_config", "refused"),
                "read_type_error": ("router_busy", "transport")}.get(scenario)
    if expected:
        assert (error.category, error.reason) == expected
    assert error.__context__ is None and error.__cause__ is None
    assert not token_reachable(error)


def test_the_reference_walk_finds_a_token_held_by_a_response(fake_router):  # noqa: F811
    import requests
    session = requests.Session()
    response = session.post(fake_router.url + "/v1/model-tasks", data=b"{}",
                            headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        raise RouterError("router_busy")
    except RouterError as error:
        held = error
    held.__traceback__ = None
    holder = types.SimpleNamespace(response=response)
    held.holder = holder
    assert token_reachable(held)
    session.close()


@pytest.mark.parametrize("where", ["send", "read"])
def test_a_soft_time_limit_is_a_busy_timeout_that_keeps_the_key(fake_router, monkeypatch, where):  # noqa: F811
    # Celery raises SoftTimeLimitExceeded (an Exception) inside the call; the request may have been
    # sent, so the router may be generating under this key: reuse it.
    import requests
    from celery.exceptions import SoftTimeLimitExceeded

    def soft_limit(*args, **kwargs):
        raise SoftTimeLimitExceeded()
    if where == "send":
        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", soft_limit)
    else:
        monkeypatch.setattr(requests.models.Response, "iter_content", soft_limit)
    error = failure(fake_router)
    assert (error.category, error.reason, error.advance_key) == ("router_busy", "timeout", False)
    assert error.__context__ is None and error.__cause__ is None
    assert not token_reachable(error)


def _verification_failure():
    return ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")


def _alert():
    error = ssl.SSLError(1, "[SSL: TLSV13_ALERT_CERTIFICATE_REQUIRED] tlsv13 alert certificate required")
    error.reason = "TLSV13_ALERT_CERTIFICATE_REQUIRED"
    return error


def _wrapped(inner):
    # requests' SSLError wraps urllib3's MaxRetryError, whose reason wraps urllib3's SSLError around the ssl error.
    import urllib3
    retry = urllib3.exceptions.MaxRetryError(None, "/v1/model-tasks", urllib3.exceptions.SSLError(inner))
    return retry


@pytest.mark.parametrize("inner, expected", [
    (_verification_failure, ("model_auth", "tls", True)),
    (_alert, ("model_auth", "tls", True)),
    (lambda: ssl.SSLEOFError(8, "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol"),
     ("router_busy", "transport", False)),
    (lambda: ConnectionResetError(104, "Connection reset by peer"), ("router_busy", "transport", False)),
    (lambda: ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number"), ("router_busy", "transport", False)),
])
@pytest.mark.parametrize("direct", [True, False])
def test_only_certificate_and_alert_tls_failures_are_auth_errors(fake_router, monkeypatch, inner, expected,  # noqa: F811
                                                                 direct):
    import requests

    def handshake_failure(*args, **kwargs):
        cause = inner()
        raise requests.exceptions.SSLError(cause if direct else _wrapped(cause))
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", handshake_failure)
    error = failure(fake_router)
    assert (error.category, error.reason, error.advance_key) == expected
    assert error.__context__ is None and error.__cause__ is None
    assert not token_reachable(error)


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


def test_the_read_timeout_leaves_room_for_the_router_worst_case():
    assert router_client.READ_MARGIN_SECONDS == 90  # the router answers by about deadline + 65 s


def test_the_context_estimate_matches_the_pinned_router_formula():
    messages = [{"role": "system", "content": "s" * 1000}, {"role": "user", "content": "u" * 5000}]
    assert estimated_context_tokens(messages, SCHEMA, 8192) == estimate_tokens(
        provider_messages(messages, SCHEMA)) + 8192
    assert estimated_context_tokens(messages[1:], SCHEMA, 64) == estimate_tokens(
        provider_messages(messages[1:], SCHEMA)) + 64


def test_from_settings_uses_the_configured_router_and_credential_paths():
    from config import load_settings
    settings = load_settings({"SECRET_KEY": "test-only-key-do-not-use-in-production-" + "s" * 32,
                              "DATABASE_URL": "postgresql+psycopg2://podcasts:test@postgres:5432/podcasts",
                              "REDIS_URL": "redis://redis:6379/0", "AWS_REGION": "us-east-1",
                              "AWS_S3_BUCKET_NAME": "podcast-transcriptions-web-2025",
                              "YOUTUBE_API_KEY": "test-only-youtube-key",
                              "ROUTER_URL": "https://router.internal:8443"})
    built = RouterClient.from_settings(settings)
    assert (built.base_url, built.token_file, built.expected_model, built.verify, built.cert) == (
        "https://router.internal:8443", "/router/token", "bonsai2:27b-pq2_0", "/router/router-ca.crt",
        ("/router/client.crt", "/router/client.key"))
