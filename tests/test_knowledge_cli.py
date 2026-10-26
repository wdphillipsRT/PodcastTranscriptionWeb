"""Backfill and status commands write ordinary batches and print only aggregates."""

from datetime import datetime, timezone
import os

import pytest

from extensions import db
import models as m
from tests.test_classification_service import add_item, env, finish  # noqa: F401


@pytest.fixture
def cli(env, app, tmp_path, monkeypatch):  # noqa: F811
    import tasks
    env.items = []
    for number, published in enumerate((datetime(2025, 3, 1, tzinfo=timezone.utc), datetime(2026, 9, 1, tzinfo=timezone.utc),
                                        None)):
        if number:
            env.item, env.transcript = add_item(env, external_id=f"episode-{number}")
        env.item.published_at = published
        env.item.title = "PRIVATE TITLE"
        finish(env)
        env.items.append(env.item)
    env.source.feed_hosts = ["Ada Host"]
    db.session.commit()
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    os.chmod(inbox, 0o2770)
    app.config.update(KNOWLEDGE_EXPORT_ENABLED=True, KNOWLEDGE_EXPORT_INBOX=str(inbox),
                      KNOWLEDGE_EXPORT_MAX_RECORDS_PER_BATCH=3)
    monkeypatch.setattr(tasks, "get_processing_storage", lambda: env.storage)
    env.inbox, env.runner = inbox, app.test_cli_runner()
    return env


def invoke(cli, *args):
    return cli.runner.invoke(args=["knowledge", *args])


def test_backfill_window_writes_sequenced_batches(cli):
    result = invoke(cli, "export", "--since", "2025-01-01", "--until", "2026-12-31")
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "status=completed reason=- batches=2 items=2 records=4"
    assert sorted(path.name[-3:] for path in cli.inbox.iterdir() if path.name.startswith("podcast-")) == ["001", "002"]
    assert "PRIVATE" not in result.output
    result = invoke(cli, "export")
    assert result.output.strip() == "status=completed reason=- batches=1 items=1 records=2 unchanged=2"


def test_backfill_respects_the_batch_cap(cli):
    result = invoke(cli, "export", "--max-batches", "1")
    assert result.output.strip() == "status=completed reason=- batches=1 items=1 records=2"


@pytest.mark.parametrize("args", [("--since", "2026-13-01"), ("--until", "yesterday"), ("--since", "2026-W01-1"), ("--until", "9999-12-31"), ("--max-batches", "0"),
                                  ("--max-batches", "1000"), ("--since", "2026-02-01", "--until", "2026-01-01")])
def test_invalid_options_fail_without_echoing_input(cli, args):
    result = invoke(cli, "export", *args)
    assert result.exit_code != 0
    assert "yesterday" not in result.output and "13" not in result.output
    assert m.KnowledgeExportBatch.query.count() == 0


def test_skipped_and_failed_exports_report_their_reason(cli, monkeypatch):
    os.chmod(cli.inbox, 0o770)
    result = invoke(cli, "export")
    assert result.exit_code == 0 and result.output.strip().startswith("status=skipped reason=inbox_group")
    os.chmod(cli.inbox, 0o2770)
    from integrations.knowledge import exporter
    monkeypatch.setattr(exporter, "write_podcast_batch", lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
    result = invoke(cli, "export")
    assert result.exit_code == 1 and "status=failed reason=write_failed" in result.output


def test_status_reports_batches_exports_and_missing_hosts(cli):
    invoke(cli, "export", "--max-batches", "2")
    other = m.ContentSource.query.filter_by(external_id="other").one()
    assert other.feed_hosts is None
    result = invoke(cli, "status")
    assert result.exit_code == 0, result.output
    assert result.output.strip() == ("batches_pending=0 batches_written=2 batches_abandoned=0 batches_failed=0 "
                                     "items_exported=2 sources_without_host=1")
