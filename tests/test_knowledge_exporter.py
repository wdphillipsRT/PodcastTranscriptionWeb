"""Nightly knowledge export: editorial-only batches, change detection, supersession, recovery."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import stat

import pytest
import sqlalchemy as sa

from config import ConfigError, load_settings
from extensions import db
from integrations.knowledge import exporter
from integrations.knowledge.contract import validate_podcast_batch
import models as m
from tests.test_classification_service import add_item, env, finish  # noqa: F401
from tests.test_config import valid_environment  # noqa: F401


NOW = datetime(2026, 10, 22, 4, 0, tzinfo=timezone.utc)
TEXTS = ("First editorial insight", "Buy now with promo code PRIVATE", "Final editorial insight")


@pytest.fixture
def ready(env, tmp_path):  # noqa: F811
    env.classifier.labels = {env.transcript.segments[1].id: ("paid_commercial", 1.0)}
    finish(env)
    env.source.feed_hosts = ["Ada Host"]
    db.session.commit()
    env.inbox = tmp_path / "inbox"
    env.inbox.mkdir()
    os.chmod(env.inbox, 0o2770)
    env.export_settings = replace(env.settings, KNOWLEDGE_EXPORT_ENABLED=True)
    return env


def export(ready, *, now=NOW, **options):
    settings = replace(ready.export_settings, **options.pop("settings", {}))
    return exporter.export_knowledge(session=db.session, storage=ready.storage, settings=settings,
                                     inbox=ready.inbox, now=now, **options)


def batch_records(ready, batch_id):
    validate_podcast_batch(ready.inbox / batch_id)
    return [json.loads(line) for line in (ready.inbox / batch_id / "records.ndjson").read_bytes().splitlines()]


def add_classified(ready, external_id, texts=TEXTS):
    ready.item, ready.transcript = add_item(ready, external_id=external_id, texts=texts)
    ready.classifier.labels[ready.transcript.segments[1].id] = ("paid_commercial", 1.0)
    finish(ready)
    return ready.item


def test_disabled_export_writes_nothing(ready):
    report = export(ready, settings={"KNOWLEDGE_EXPORT_ENABLED": False})
    assert (report.status, report.reason) == ("skipped", "disabled")
    assert list(ready.inbox.iterdir()) == []


@pytest.mark.parametrize("problem, reason", [("missing", "inbox_missing"), ("file", "inbox_missing"),
                                             ("no_setgid", "inbox_group"), ("read_only", "inbox_unwritable")])
def test_unusable_inbox_is_skipped_with_a_warning(ready, caplog, problem, reason):
    os.chmod(ready.inbox, 0o2770)
    if problem in {"missing", "file"}:
        ready.inbox.rmdir()
        if problem == "file":
            ready.inbox.write_text("not a directory")
    elif problem == "no_setgid":
        os.chmod(ready.inbox, 0o770)
    else:
        os.chmod(ready.inbox, 0o2550)
    with caplog.at_level(logging.WARNING, logger="integrations.knowledge.exporter"):
        report = export(ready)
    os.chmod(ready.inbox, 0o2770) if ready.inbox.is_dir() else None
    assert (report.status, report.reason) == ("skipped", reason)
    assert any(record.getMessage() == "knowledge_export_skipped" and record.knowledge_export["reason"] == reason
               for record in caplog.records)
    assert m.KnowledgeExportBatch.query.count() == 0


def test_first_export_writes_one_valid_editorial_only_batch(ready):
    report = export(ready)
    assert (report.status, report.batches, report.items, report.records) == (
        "completed", ["podcast-20261022-001"], 1, 3)
    batch = ready.inbox / "podcast-20261022-001"
    records = batch_records(ready, "podcast-20261022-001")
    episode, *segments = records
    item_id, revision_id = ready.item.id, ready.item.active_classification_revision_id
    assert episode["record_id"] == f"podcast:podcast.episode:item-{item_id}/rev-{revision_id}/e1"
    assert episode["supersedes"] is None and episode["source"]["publisher"] == "Feed"
    assert episode["participants"] == [{"name": "Ada Host", "role": "host", "basis": "feed_declared",
                                        "confidence": 1, "entity_id": "person:ada-host"}]
    assert [s["attributes"]["segment_ids"] for s in segments] == [
        [ready.transcript.segments[0].id], [ready.transcript.segments[2].id]]
    blobs = b"".join(path.read_bytes() for path in (batch / "blobs").iterdir())
    raw = (batch / "records.ndjson").read_bytes() + blobs
    assert b"First editorial insight" in raw and b"Final editorial insight" in raw
    assert b"PRIVATE" not in raw and b"promo" not in raw
    assert stat.S_IMODE(batch.stat().st_mode) == 0o2770 and batch.stat().st_gid == ready.inbox.stat().st_gid
    assert stat.S_IMODE((batch / "records.ndjson").stat().st_mode) == 0o660
    row = m.KnowledgeExportBatch.query.one()
    assert (row.state, row.record_count, row.blob_count, row.written_at is not None) == ("written", 3, 2, True)
    exported = m.KnowledgeItemExport.query.one()
    assert (exported.generation, exported.passage_count, exported.episode_record_id) == (1, 2, episode["record_id"])


def test_rerunning_the_same_night_exports_nothing_new(ready):
    export(ready)
    report = export(ready, now=NOW + timedelta(hours=1))
    assert (report.status, report.batches, report.skipped) == ("completed", [], {"unchanged": 1})
    assert sorted(path.name for path in ready.inbox.iterdir() if not path.name.startswith(".")) == [
        "podcast-20261022-001"]


def test_a_host_correction_emits_a_superseding_generation(ready):
    export(ready)
    first = m.KnowledgeItemExport.query.one().episode_record_id
    revision = db.session.get(m.ClassificationRevision, ready.item.active_classification_revision_id)
    corrected = (revision.completed_at + timedelta(hours=1)).replace(microsecond=0)
    ready.source.admin_hosts, ready.source.hosts_updated_at = ["Real Host"], corrected
    db.session.commit()
    report = export(ready, now=NOW + timedelta(days=1))
    assert report.batches == ["podcast-20261023-001"]
    episode = batch_records(ready, "podcast-20261023-001")[0]
    assert episode["record_id"] == first[:-2] + "e2" and episode["supersedes"] == first
    assert episode["participants"][0]["name"] == "Real Host" and episode["participants"][0]["basis"] == "admin_set"
    assert episode["observed_at"] == corrected.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_new_classification_revision_supersedes_the_exported_one(ready):
    export(ready)
    first = m.KnowledgeItemExport.query.one().episode_record_id
    ready.classifier.labels = {}
    finish(ready, reason="manual_retry", idempotency_key="manual_retry:test")
    revision_id = ready.item.active_classification_revision_id
    report = export(ready, now=NOW + timedelta(days=1))
    episode, *segments = batch_records(ready, report.batches[0])
    assert episode["record_id"] == f"podcast:podcast.episode:item-{ready.item.id}/rev-{revision_id}/e1"
    assert episode["supersedes"] == first and len(segments) == 1
    assert segments[0]["attributes"]["segment_ids"] == [segment.id for segment in ready.transcript.segments]


def test_items_without_a_host_or_awaiting_guests_wait(ready):
    ready.source.feed_hosts = None
    db.session.commit()
    assert export(ready).skipped == {"no_host": 1}
    ready.source.feed_hosts = ["Ada Host"]
    db.session.commit()
    report = export(ready, settings={"GUEST_EXTRACTION_ENABLED": True})
    assert report.skipped == {"awaiting_guests": 1} and report.batches == []
    ready.item.feed_guests = ["James Clear"]
    db.session.commit()
    report = export(ready, settings={"GUEST_EXTRACTION_ENABLED": True})
    assert report.batches == ["podcast-20261022-001"]
    guest = batch_records(ready, "podcast-20261022-001")[0]["participants"][1]
    assert (guest["name"], guest["basis"], guest["role"]) == ("James Clear", "feed_declared", "guest")


def test_unavailable_projection_skips_the_item_without_reading_raw_text(ready):
    newer = ready.service.ensure_revision(ready.item.id, "manual_retry", idempotency_key="pending-newer")
    assert db.session.get(m.ClassificationRevision, newer).state == "pending"
    report = export(ready)
    assert report.skipped == {"projection_unavailable": 1} and report.batches == []
    assert not any(key.endswith(".txt") for key, _ in ready.storage.reads)


def test_large_exports_split_into_sequenced_batches_capped_per_run(ready):
    for number in range(4):
        add_classified(ready, f"episode-{number}")
    report = export(ready, settings={"KNOWLEDGE_EXPORT_MAX_RECORDS_PER_BATCH": 6,
                                     "KNOWLEDGE_EXPORT_MAX_BATCHES_PER_RUN": 2})
    assert report.batches == ["podcast-20261022-001", "podcast-20261022-002"] and report.items == 4
    assert [len(batch_records(ready, name)) for name in report.batches] == [6, 6]
    report = export(ready, now=NOW + timedelta(minutes=5), settings={"KNOWLEDGE_EXPORT_MAX_RECORDS_PER_BATCH": 6})
    assert report.batches == ["podcast-20261022-003"] and report.items == 1
    assert m.KnowledgeItemExport.query.count() == 5


def test_an_item_larger_than_one_batch_is_skipped(ready):
    report = export(ready, settings={"KNOWLEDGE_EXPORT_MAX_RECORDS_PER_BATCH": 2})
    assert report.skipped == {"too_large": 1} and report.batches == []


def test_since_and_until_select_items_by_publication_date(ready):
    ready.item.published_at = datetime(2026, 1, 15, tzinfo=timezone.utc)
    db.session.commit()
    day = datetime(2026, 1, 15).date()
    assert export(ready, since=day + timedelta(days=1)).items == 0
    assert export(ready, until=day - timedelta(days=1)).items == 0
    assert export(ready, since=day, until=day).items == 1


def test_pending_batches_are_reconciled_before_new_work(ready, monkeypatch):
    def crash(*args, **kwargs):
        raise KeyboardInterrupt  # A process killed after the reservation commit.
    monkeypatch.setattr(exporter, "write_podcast_batch", crash)
    with pytest.raises(KeyboardInterrupt):
        export(ready)
    monkeypatch.undo()
    (ready.inbox / ".tmp-podcast-20261022-001").mkdir()
    report = export(ready)
    assert report.reconciled == {"written": 0, "abandoned": 1}
    assert not (ready.inbox / ".tmp-podcast-20261022-001").exists()
    assert report.batches == ["podcast-20261022-002"]
    episode = batch_records(ready, "podcast-20261022-002")[0]
    assert episode["record_id"].endswith("/e2") and episode["supersedes"] is None
    states = dict(db.session.execute(sa.select(m.KnowledgeExportBatch.batch_id, m.KnowledgeExportBatch.state)).all())
    assert states == {"podcast-20261022-001": "abandoned", "podcast-20261022-002": "written"}


def test_a_written_but_unrecorded_batch_is_confirmed_from_the_inbox(ready):
    export(ready)
    row = m.KnowledgeExportBatch.query.one()
    row.state, row.written_at = "pending", None
    db.session.commit()
    report = export(ready, now=NOW + timedelta(minutes=1))
    assert report.reconciled == {"written": 1, "abandoned": 0} and report.skipped == {"unchanged": 1}


def test_a_failed_write_is_recorded_and_retried_with_a_new_generation(ready, monkeypatch):
    def full(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(exporter, "write_podcast_batch", full)
    report = export(ready)
    assert (report.status, report.reason) == ("failed", "write_failed")
    assert m.KnowledgeExportBatch.query.one().state == "failed"
    monkeypatch.undo()
    report = export(ready)
    assert report.batches == ["podcast-20261022-002"]
    assert batch_records(ready, "podcast-20261022-002")[0]["record_id"].endswith("/e2")


def test_a_concurrent_export_is_skipped_while_the_lock_is_held(ready):
    import fcntl
    with open(ready.inbox / exporter.LOCK_NAME, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        assert (export(ready).status, export(ready).reason) == ("skipped", "busy")


def test_export_settings_defaults_and_validation(valid_environment):  # noqa: F811
    settings = load_settings(valid_environment)
    assert settings.KNOWLEDGE_EXPORT_ENABLED is False
    assert settings.KNOWLEDGE_EXPORT_INBOX == "/spool/podcast/inbox"
    assert settings.KNOWLEDGE_EXPORT_MAX_BATCHES_PER_RUN == 5
    assert settings.KNOWLEDGE_EXPORT_MAX_RECORDS_PER_BATCH == 10_000
    for name, value in (("KNOWLEDGE_EXPORT_ENABLED", "1"), ("KNOWLEDGE_EXPORT_INBOX", "relative/inbox"),
                        ("KNOWLEDGE_EXPORT_MAX_RECORDS_PER_BATCH", "10001"),
                        ("KNOWLEDGE_EXPORT_MAX_BATCHES_PER_RUN", "1000")):
        with pytest.raises(ConfigError, match=name):
            load_settings({**valid_environment, name: value})


def test_report_is_json_safe(ready):
    report = export(ready).as_dict()
    assert json.loads(json.dumps(report)) == report
    assert set(report) == {"status", "reason", "batches", "items", "records", "skipped", "reconciled"}


def test_exporter_reads_transcripts_only_through_the_gateway():
    from tests.test_learning_projection import forbidden_artifact_references
    root = Path(__file__).resolve().parents[1]
    modules = sorted((root / "integrations/knowledge").glob("*.py"))
    assert {path.name for path in modules} >= {"exporter.py", "records.py", "people.py", "contract.py"}
    assert forbidden_artifact_references(root) == []
    source = (root / "integrations/knowledge/exporter.py").read_text()
    assert "load_active_learning_projection" in source
    assert not any(name in path.read_text() for path in modules for name in ("get_bytes", "s3_key", "boto3"))


def test_an_item_with_an_invalid_record_is_skipped_and_the_rest_export(ready, caplog):
    poisoned = ready.item.id
    ready.item.canonical_url = ""
    db.session.commit()
    healthy = add_classified(ready, "healthy")
    with caplog.at_level(logging.WARNING, logger=exporter.__name__):
        report = export(ready)
    assert report.status == "completed" and report.skipped == {"invalid_record": 1}
    assert report.batches == ["podcast-20261022-001"] and report.items == 1
    assert [row.item_id for row in m.KnowledgeItemExport.query.all()] == [healthy.id]
    assert any(getattr(r, "item_id", None) == poisoned for r in caplog.records
               if r.getMessage() == "knowledge_export_invalid_record")


def test_a_batch_published_before_a_later_write_error_is_left_for_reconcile(ready, monkeypatch):
    from integrations.knowledge.vendor.reference import knowledge_contract
    real = knowledge_contract._fsync_directory

    def fsync(path):
        if path == ready.inbox:
            raise OSError("EIO")
        real(path)
    monkeypatch.setattr(knowledge_contract, "_fsync_directory", fsync)
    report = export(ready)
    assert (report.status, report.reason) == ("failed", "write_failed")
    assert m.KnowledgeExportBatch.query.one().state == "pending"
    monkeypatch.undo()
    report = export(ready, now=NOW + timedelta(hours=1))
    assert report.reconciled == {"written": 1, "abandoned": 0}
    assert report.batches == [] and report.skipped == {"unchanged": 1}


def test_the_lock_file_is_group_writable_and_never_followed(ready, tmp_path):
    previous = os.umask(0o022)
    try:
        export(ready)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(os.lstat(ready.inbox / exporter.LOCK_NAME).st_mode) == 0o660
    (ready.inbox / exporter.LOCK_NAME).unlink()
    outside = tmp_path / "outside"
    outside.write_text("x")
    (ready.inbox / exporter.LOCK_NAME).symlink_to(outside)
    report = export(ready, now=NOW + timedelta(hours=1))
    assert (report.status, report.reason) == ("skipped", "inbox_unwritable")
    assert outside.read_text() == "x"


def test_an_unopenable_lock_file_skips_the_run(ready):
    lock = ready.inbox / exporter.LOCK_NAME
    lock.touch()
    os.chmod(lock, 0o400)
    try:
        report = export(ready)
    finally:
        os.chmod(lock, 0o600)
    assert (report.status, report.reason) == ("skipped", "inbox_unwritable") and report.batches == []


def test_batch_names_already_in_the_inbox_are_never_reused(ready, tmp_path):
    (ready.inbox / "podcast-20261022-001").mkdir()
    (ready.inbox / "podcast-20261022-002").symlink_to(tmp_path / "nowhere")  # Dangling.
    (ready.inbox / ".tmp-podcast-20261022-003").mkdir()
    assert export(ready).batches == ["podcast-20261022-004"]


def test_reconcile_does_not_confirm_a_symlinked_batch(ready, monkeypatch, tmp_path):
    def crash(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(exporter, "write_podcast_batch", crash)
    with pytest.raises(KeyboardInterrupt):
        export(ready)
    monkeypatch.undo()
    (tmp_path / "elsewhere").mkdir()
    (ready.inbox / "podcast-20261022-001").symlink_to(tmp_path / "elsewhere")
    report = export(ready, now=NOW + timedelta(hours=1))
    assert report.reconciled == {"written": 0, "abandoned": 1}
    assert report.batches == ["podcast-20261022-002"]


def test_a_failed_export_is_logged_as_a_warning(ready, monkeypatch, caplog):
    def full(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(exporter, "write_podcast_batch", full)
    with caplog.at_level(logging.INFO, logger=exporter.__name__):
        export(ready)
    [record] = [r for r in caplog.records if r.getMessage() == "knowledge_export_failed"]
    assert record.levelno == logging.WARNING


def test_max_batches_must_be_positive_and_defaults_from_settings(ready):
    with pytest.raises(ValueError, match="max_batches"):
        export(ready, max_batches=0)
    assert m.KnowledgeExportBatch.query.count() == 0
    add_classified(ready, "second")
    report = export(ready, max_batches=None, settings={"KNOWLEDGE_EXPORT_MAX_RECORDS_PER_BATCH": 3,
                                                       "KNOWLEDGE_EXPORT_MAX_BATCHES_PER_RUN": 1})
    assert report.batches == ["podcast-20261022-001"] and report.items == 1


def test_batches_split_on_the_byte_cap_and_an_oversized_item_is_not_split(ready, monkeypatch):
    add_classified(ready, "two")
    monkeypatch.setattr(exporter, "MAX_BATCH_PAYLOAD_BYTES", 3000)
    report = export(ready)
    assert report.batches == ["podcast-20261022-001", "podcast-20261022-002"] and report.items == 2
    assert [len(batch_records(ready, name)) for name in report.batches] == [3, 3]
    add_classified(ready, "three")
    monkeypatch.setattr(exporter, "MAX_BATCH_PAYLOAD_BYTES", 10)
    report = export(ready, now=NOW + timedelta(hours=1))
    assert report.skipped == {"unchanged": 2, "too_large": 1} and report.batches == []


def test_a_pending_batch_claimed_by_the_intake_is_abandoned_and_re_exported(ready, monkeypatch, tmp_path):
    real = exporter.write_podcast_batch

    def write_then_die(*args, **kwargs):
        real(*args, **kwargs)
        raise KeyboardInterrupt  # Killed after the rename, before the batch is marked written.
    monkeypatch.setattr(exporter, "write_podcast_batch", write_then_die)
    with pytest.raises(KeyboardInterrupt):
        export(ready)
    monkeypatch.undo()
    first = json.loads((ready.inbox / "podcast-20261022-001" / "records.ndjson").read_bytes().splitlines()[0])
    (tmp_path / "processing").mkdir()
    os.rename(ready.inbox / "podcast-20261022-001", tmp_path / "processing" / "podcast-20261022-001")
    report = export(ready, now=NOW + timedelta(hours=1))
    assert report.reconciled == {"written": 0, "abandoned": 1}
    assert report.batches == ["podcast-20261022-002"]
    second = batch_records(ready, "podcast-20261022-002")[0]
    assert first["record_id"].endswith("/e1") and second["record_id"].endswith("/e2")
    assert db.session.get(m.KnowledgeExportBatch, 1).state == "abandoned"
