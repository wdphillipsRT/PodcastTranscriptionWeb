"""The pinned router validator is byte-identical to its source, and the fake router behaves like it."""

from hashlib import sha256
from pathlib import Path

import pytest
import requests

from tests.router_fake import TOKEN, FakeRouter, fake_router  # noqa: F401
from tests.router_schema_validator import RouterError, validate_instance, validate_schema

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


def post(router, key, body=None):
    body = body or {"response_schema": {"type": "object", "additionalProperties": False, "required": ["ok"],
                                        "properties": {"ok": {"type": "boolean"}}}}
    return requests.post(router.url + "/v1/model-tasks", json={**body, "task_id": "t"},
                         headers={"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": key}, timeout=5)


def test_fake_router_replays_stored_failures_but_not_rate_limits(fake_router):  # noqa: F811
    fake_router.reply(429, FakeRouter.error("MODEL_RATE_LIMITED"))
    fake_router.reply(503, FakeRouter.error("MODEL_NO_ELIGIBLE_CANDIDATE"))
    assert post(fake_router, "a").status_code == 429
    assert post(fake_router, "a").status_code == 503
    replay = post(fake_router, "a")
    assert replay.status_code == 503 and replay.headers["Idempotent-Replayed"] == "true"
    assert post(fake_router, "b").status_code == 200 and fake_router.generations == 1
