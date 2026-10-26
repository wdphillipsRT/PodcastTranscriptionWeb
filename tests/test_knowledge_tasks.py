"""The nightly export task runs only on the exporter's queue and never raises."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from extensions import db
from tests.test_classification_service import env, finish  # noqa: F401


@pytest.fixture
def ready(env, tmp_path, monkeypatch):  # noqa: F811
    from flask import current_app
    import tasks
    finish(env)
    env.source.feed_hosts = ["Ada Host"]
    db.session.commit()
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    os.chmod(inbox, 0o2770)
    current_app.config.update(KNOWLEDGE_EXPORT_ENABLED=True, KNOWLEDGE_EXPORT_INBOX=str(inbox))
    monkeypatch.setattr(tasks, "get_processing_storage", lambda: env.storage)
    env.inbox = inbox
    return env


def test_task_exports_with_application_settings(ready):
    import tasks
    report = tasks.export_knowledge.run()
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    assert report["status"] == "completed" and report["batches"] == [f"podcast-{today}-001"]
    assert (ready.inbox / report["batches"][0] / "manifest.json").is_file()
    assert tasks.export_knowledge.queue == "knowledge"
    assert json.loads(json.dumps(report)) == report


def test_unexpected_failures_are_reported_not_raised(ready, monkeypatch, caplog):
    import tasks
    from integrations.knowledge import exporter

    def broken(**kwargs):
        raise RuntimeError("private diagnostic /secret/path")
    monkeypatch.setattr(exporter, "export_knowledge", broken)
    assert tasks.export_knowledge.run() == {"status": "failed", "reason": "unexpected_error"}
    assert "private diagnostic" not in caplog.text
    failure = next(r for r in caplog.records if r.getMessage() == "knowledge_export_failed")
    assert failure.knowledge_export == {"status": "failed", "reason": "unexpected_error"}
    assert failure.error_type == "RuntimeError"


@pytest.mark.parametrize("enabled", ["true", "false"])
def test_nightly_export_is_routed_to_its_own_queue_and_scheduled_only_when_enabled(enabled):
    root = Path(__file__).resolve().parents[1]
    script = ("import celeryconfig, json; entry = celeryconfig.beat_schedule.get('knowledge-export-nightly');"
              "print(json.dumps([sorted(q.name for q in celeryconfig.task_queues),"
              " celeryconfig.task_routes['tasks.export_knowledge'],"
              " entry and [entry['task'], str(entry['schedule']), entry['options']]]))")
    result = subprocess.run([sys.executable, "-c", script], cwd=root, capture_output=True, text=True, check=True,
                            env={"PATH": os.environ["PATH"], "KNOWLEDGE_EXPORT_ENABLED": enabled})
    queues, route, entry = json.loads(result.stdout)
    assert queues == ["classification", "default", "discovery", "downloads", "knowledge", "transcription"]
    assert route == {"queue": "knowledge"}
    expected = ["tasks.export_knowledge", "<crontab: 0 4 * * * (m/h/dM/MY/d)>", {"expires": 21600}]
    assert entry == (expected if enabled == "true" else None)
