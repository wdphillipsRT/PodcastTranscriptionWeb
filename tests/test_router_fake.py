"""The pinned router validator is byte-identical to its source, and the fake router behaves like it."""

from hashlib import sha256
import http.client
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

import pytest
import requests

from tests.router_fake import TOKEN, FakeRouter, fake_router  # noqa: F401
from tests.router_schema_validator import (RouterError, estimate_tokens, provider_messages, validate_instance,
                                           validate_schema)

PINNED_SHA256 = "8119a06b7e1363eead77194c69f43ed2411becf97db0b2aa49e6fa856e686c46"


def test_pinned_validator_block_is_byte_identical_to_router_commit_4bcf4ae():
    text = (Path(__file__).parent / "router_schema_validator.py").read_text(encoding="utf-8")
    begin = text.index("\n", text.index("# --- BEGIN PINNED")) + 1
    end = text.index("# --- END PINNED ---")
    assert sha256(text[begin:end].encode("utf-8")).hexdigest() == PINNED_SHA256


@pytest.mark.parametrize("schema", [
    {"type": "string", "pattern": "^seg-"},
    {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
    {"enum": ["editorial"]},
    {"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": True},
])
def test_pinned_validator_rejects_what_the_router_rejects(schema):
    with pytest.raises(RouterError):
        validate_schema(schema)


def test_pinned_validator_accepts_typed_enums_and_checks_instances():
    schema = {"type": "object", "additionalProperties": False, "required": ["label"],
              "properties": {"label": {"type": "string", "enum": ["editorial", "uncertain"]}}}
    validate_schema(schema)
    validate_instance({"label": "editorial"}, schema)
    with pytest.raises(RouterError):
        validate_instance({"label": "advert"}, schema)


SCHEMA = {"type": "object", "additionalProperties": False, "required": ["ok"],
          "properties": {"ok": {"type": "boolean"}}}


def task(content="Is this synthetic?", max_output_tokens=64, **changes):
    return {"task_id": "t", "messages": [{"role": "user", "content": content}],
            "generation": {"temperature": 0, "max_output_tokens": max_output_tokens},
            "response_schema": SCHEMA, **changes}


def post(router, key, body=None, *, path="/v1/model-tasks", **options):
    headers = {"Authorization": f"Bearer {TOKEN}", **options.pop("headers", {})}
    if key is not None:
        headers["Idempotency-Key"] = key
    if "data" not in options and "json" not in options:
        options["json"] = task() if body is None else body
    return requests.post(router.url + path, headers=headers, timeout=5, **options)


def error_code(response):
    return response.json()["error"]["code"]


def test_fake_router_replays_stored_failures_but_not_rate_limits(fake_router):  # noqa: F811
    fake_router.reply(429, FakeRouter.error("MODEL_RATE_LIMITED"))
    fake_router.reply(503, FakeRouter.error("MODEL_NO_ELIGIBLE_CANDIDATE"))
    assert post(fake_router, "a").status_code == 429
    assert post(fake_router, "a").status_code == 503
    replay = post(fake_router, "a")
    assert replay.status_code == 503 and replay.headers["Idempotent-Replayed"] == "true"
    assert post(fake_router, "b").status_code == 200 and fake_router.generations == 1


@pytest.mark.parametrize("key", [None, "", "k" * 201, "has space", "caf\u00e9", "tab\there"],
                         ids=["missing", "empty", "201-chars", "space", "non-ascii", "tab"])
def test_fake_router_requires_a_bounded_printable_key_and_stores_nothing(fake_router, key):  # noqa: F811
    response = post(fake_router, key)
    assert (response.status_code, error_code(response)) == (400, "LAMAD_IDEMPOTENCY_REQUIRED")
    assert fake_router.generations == 0 and post(fake_router, "k" * 200).status_code == 200


def test_fake_router_answers_404_off_the_task_endpoint(fake_router):  # noqa: F811
    response = post(fake_router, "a", path="/v1/chat/completions")
    assert (response.status_code, error_code(response)) == (404, "MODEL_ENDPOINT_NOT_FOUND")
    assert fake_router.generations == 0


def without(name):
    return {key: value for key, value in task().items() if key != name}


@pytest.mark.parametrize("options, status, code", [
    (dict(data=b"not json", headers={"Content-Type": "application/json"}), 400, "MODEL_TASK_INVALID"),
    (dict(json=["not", "an", "object"]), 400, "MODEL_TASK_INVALID"),
    (dict(json=without("messages")), 400, "MODEL_TASK_INVALID"),
    (dict(json=without("generation")), 400, "MODEL_TASK_INVALID"),
    (dict(json=task(messages=[])), 400, "MODEL_TASK_INVALID"),
    (dict(json=task(messages=[{"role": "user"}])), 400, "MODEL_TASK_INVALID"),
    (dict(json=task(max_output_tokens=8193)), 400, "MODEL_TASK_INVALID"),
    (dict(json=task(max_output_tokens=15)), 400, "MODEL_TASK_INVALID"),
    (dict(json=task(max_output_tokens="64")), 400, "MODEL_TASK_INVALID"),
    (dict(json=task(response_schema={"type": "string", "pattern": "^x"})), 400, "MODEL_TASK_INVALID"),
    (dict(json=without("response_schema")), 400, "MODEL_OUTPUT_SCHEMA_REQUIRED"),
    (dict(data=b"{}", headers={"Content-Type": "text/plain"}), 415, "MODEL_TASK_INVALID"),
    (dict(data=b"{", headers={"Content-Type": "application/json"}), 413, "MODEL_TASK_INVALID"),
])
def test_fake_router_refuses_malformed_tasks_and_keeps_serving(fake_router, options, status, code):  # noqa: F811
    response = post(fake_router, "a", **options)
    assert (response.status_code, error_code(response)) == (status, code)
    assert fake_router.generations == 0 and post(fake_router, "a").status_code == 200  # nothing stored


def test_fake_router_needs_a_content_length(fake_router):  # noqa: F811
    connection = http.client.HTTPConnection(urlsplit(fake_router.url).netloc, timeout=5)
    try:
        connection.putrequest("POST", "/v1/model-tasks")
        for name, value in (("Authorization", f"Bearer {TOKEN}"), ("Idempotency-Key", "a"),
                            ("Content-Type", "application/json")):
            connection.putheader(name, value)
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 411 and b"MODEL_TASK_INVALID" in response.read()
    finally:
        connection.close()
    assert post(fake_router, "a").status_code == 200


def test_fake_router_stores_no_eligible_candidate_when_the_context_cannot_fit(fake_router):  # noqa: F811
    body = task(content="x" * 2000)
    needed = estimate_tokens(provider_messages(body["messages"], SCHEMA)) + 64
    fake_router.context_tokens = needed
    assert post(fake_router, "fits", body).status_code == 200
    fake_router.context_tokens = needed - 1
    response = post(fake_router, "too-big", body)
    assert (response.status_code, error_code(response)) == (503, "MODEL_NO_ELIGIBLE_CANDIDATE")
    fake_router.context_tokens = needed
    replay = post(fake_router, "too-big", body)  # stored like the router's _fail, so replayed
    assert replay.status_code == 503 and replay.headers["Idempotent-Replayed"] == "true"
    assert fake_router.generations == 1


def test_fake_router_stores_no_eligible_candidate_over_the_output_budget(fake_router):  # noqa: F811
    fake_router.max_output_tokens = 64
    assert post(fake_router, "a", task(max_output_tokens=64)).status_code == 200
    response = post(fake_router, "b", task(max_output_tokens=65))
    assert (response.status_code, error_code(response)) == (503, "MODEL_NO_ELIGIBLE_CANDIDATE")
    assert fake_router.generations == 1


def test_fake_router_defaults_match_the_classification_route(fake_router):  # noqa: F811
    assert (fake_router.context_tokens, fake_router.max_output_tokens) == (32_768, 8_192)


def test_fake_router_answers_202_while_the_same_key_is_running(fake_router):  # noqa: F811
    fake_router.delay = 1.0
    results = {}
    first = threading.Thread(target=lambda: results.setdefault("first", post(fake_router, "a")))
    first.start()
    deadline = time.monotonic() + 5
    while fake_router.generations == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    second = post(fake_router, "a")
    conflict = post(fake_router, "a", task(content="different"))
    first.join(5)
    assert second.status_code == 202 and second.headers["Idempotent-Replayed"] == "true"
    assert second.json()["status"] == "in_progress" and set(second.json()) == {"status", "invocation_id", "trace_id"}
    assert (conflict.status_code, error_code(conflict)) == (409, "LAMAD_IDEMPOTENCY_CONFLICT")
    assert results["first"].status_code == 200 and fake_router.generations == 1
    fake_router.delay = 0.0
    replay = post(fake_router, "a")
    assert replay.status_code == 200 and replay.headers["Idempotent-Replayed"] == "true"
