"""Owner operational commands expose only aggregate, non-private results."""
import pytest
from extensions import db
import models as m
from tests.test_classification_tasks import worker
from tests.test_classification_service import env, add_item


def invoke(worker, *args):
    return worker.app.test_cli_runner().invoke(args=["classification", *args])


def test_status_only_aggregates_without_storage(worker):
    worker.env.item.title = "PRIVATE TITLE"
    db.session.commit()
    result = invoke(worker, "status")
    assert result.exit_code == 0, result.output
    assert "missing_current_policy=1" in result.output
    assert "PRIVATE" not in result.output and "https:" not in result.output and "items/" not in result.output
    assert not worker.env.storage.reads


@pytest.mark.parametrize("limit", ["0", "101", "private-value"])
def test_backlog_rejects_invalid_limit_without_echoing_input(worker, limit):
    result = invoke(worker, "enqueue-backlog", "--limit", limit)
    assert result.exit_code != 0 and "private-value" not in result.output


def test_backlog_disabled_still_creates_bounded_work(worker):
    worker.app.config["CLASSIFICATION_ENABLED"] = False
    add_item(worker.env, external_id="other")
    result = invoke(worker, "enqueue-backlog", "--limit", "1")
    assert result.exit_code == 0, result.output
    assert "created=1" in result.output and m.ClassificationRevision.query.count() == 1
    assert not worker.sent and not worker.env.classifier.calls


def test_retry_appends_idempotently_and_can_retry_later_failure(worker):
    original = worker.env.service.ensure_revision(worker.env.item.id, "backfill")
    row = db.session.get(m.ClassificationRevision, original)
    row.state, row.error_category, row.attempts = "failed", "claude_auth", 5
    db.session.commit()
    worker.app.config["CLASSIFICATION_ENABLED"] = False
    for _ in range(2):
        result = invoke(worker, "retry", "--item-id", str(worker.env.item.id))
        assert result.exit_code == 0, result.output
    assert m.ClassificationRevision.query.count() == 2 and not worker.sent
    latest = m.ClassificationRevision.query.order_by(m.ClassificationRevision.id.desc()).first()
    assert latest.idempotency_key == f"manual_retry:{original}" and latest.attempts == 0
    latest.state, latest.error_category = "failed", "claude_config"
    db.session.commit()
    result = invoke(worker, "retry", "--item-id", str(worker.env.item.id))
    assert result.exit_code == 0 and m.ClassificationRevision.query.count() == 3
    assert row.attempts == 5 and row.state == "failed"


@pytest.mark.parametrize("arguments", [("auth-smoke",), ("smoke", "--item-id", "1"),
    ("smoke", "--acknowledge-claude-cost"),
    ("smoke", "--item-id", "1", "--oldest-completed", "--acknowledge-claude-cost")])
def test_smokes_require_acknowledgement_and_exactly_one_selection(worker, arguments):
    result = invoke(worker, *arguments)
    assert result.exit_code != 0 and not worker.env.classifier.calls


@pytest.mark.parametrize("selector", ["item", "oldest"])
def test_one_item_smoke_runs_synchronously_while_disabled(worker, selector):
    add_item(worker.env, external_id="unselected")
    worker.app.config["CLASSIFICATION_ENABLED"] = False
    arguments = ["--item-id", str(worker.env.item.id)] if selector == "item" else ["--oldest-completed"]
    result = invoke(worker, "smoke", *arguments, "--acknowledge-claude-cost")
    assert result.exit_code == 0, result.output
    assert "state=completed" in result.output and "cost_microusd=25" in result.output
    assert m.ClassificationRevision.query.count() == 1
    assert not worker.sent and len(worker.env.classifier.calls) == 1
    assert worker.env.transcript.segments[0].text not in result.output


def test_cli_failure_suppresses_private_exception(worker, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE transcript https://secret.example/key")
    monkeypatch.setattr(worker.env.service, "ensure_revision", fail)
    result = invoke(worker, "smoke", "--oldest-completed", "--acknowledge-claude-cost")
    assert result.exit_code != 0 and "PRIVATE" not in result.output and "https:" not in result.output


@pytest.mark.parametrize("failure", [None, "auth", "model"])
def test_auth_smoke_uses_production_adapter_without_private_data_or_artifacts(worker, monkeypatch, tmp_path, failure):
    import json
    import os
    import claude_classifier as adapter
    from tests.test_claude_classifier import PipeChild
    calls, children = [], []
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    worker.app.config["CLASSIFICATION_ENABLED"] = False
    def factory(argv, **kwargs):
        calls.append((argv, kwargs))
        output = {"structured_output": {"ranges": [{"first_segment_id": "seg-000001-aaaaaaaaaaaaaaaa",
            "last_segment_id": "seg-000001-aaaaaaaaaaaaaaaa", "label": "editorial", "confidence": 1,
            "reason_codes": ["substantive_content"]}]}, "total_cost_usd": .000025,
            "modelUsage": {"wrong-model" if failure == "model" else "claude-sonnet-5-5": {}}}
        child = PipeChild(json.dumps(output).encode(), stderr=b"OAuth private credential" if failure == "auth" else b"",
            returncode=1 if failure == "auth" else 0)
        children.append(child)
        return child
    monkeypatch.setattr(os, "killpg", lambda *args: [child.killed.set() for child in children])
    original = adapter.ClaudeClassifier.__init__
    def initialize(self, settings):
        original(self, settings, popen_factory=factory)
    monkeypatch.setattr(adapter.ClaudeClassifier, "__init__", initialize)
    result = invoke(worker, "auth-smoke", "--acknowledge-claude-cost")
    assert len(calls) == 1, result.output
    argv, options = calls[0]
    assert argv[:7] == ["claude", "--restricted", "--safe-mode", "-p", "--tools", "", "--disallowedTools"]
    assert "--no-session-persistence" in argv and "--strict-mcp-config" in argv
    assert options["env"]["CLAUDE_CONFIG_DIR"] == str(tmp_path)
    assert not set(options["env"]) & {"AWS_SECRET_ACCESS_KEY", "DATABASE_URL", "YOUTUBE_API_KEY"}
    assert worker.env.transcript.segments[0].text.encode() not in children[0].input
    assert m.ClassificationRevision.query.count() == 0 and not worker.env.storage.reads and not worker.env.storage.puts
    if failure:
        assert result.exit_code != 0 and "private credential" not in result.output
    else:
        assert result.exit_code == 0, result.output
        assert result.output.strip() == "cli=2.1.303 model=claude-sonnet-5-5 cost_microusd=25"
