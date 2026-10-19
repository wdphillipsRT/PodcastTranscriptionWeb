"""Immutable administrator corrections and bounded source-rule recovery, offline."""
from datetime import timedelta
from hashlib import sha256
import json

import pytest
import sqlalchemy as sa

from extensions import db
import models as m
import tasks
from s3_utils import StorageTransientError
from transcripts import normalize_vtt
from tests.test_classification_service import env, add_item, finish, artifacts
from tests.test_classification_tasks import worker


def record(env, active, first=0, last=None, label="paid_commercial", **changes):
    values = dict(item_id=env.item.id, admin_id=env.owner.id, active_revision_id=active,
        first_segment_id=env.transcript.segments[first].id,
        last_segment_id=env.transcript.segments[first if last is None else last].id, label=label)
    values.update(changes)
    return env.service.record_override(**values)


def complete(env, revision_id):
    prepared = env.service.prepare_revision(revision_id)
    for ordinal in prepared.block_ordinals:
        env.service.run_block(revision_id, ordinal)
    return env.service.finalize_revision(revision_id)


def test_correction_atomically_creates_metadata_only_learning_and_pending_outboxes(env):
    active, _ = finish(env)
    result = record(env, active, 0, 1)
    correction = db.session.get(m.SegmentOverride, result.override_id)
    learned = db.session.get(m.SourceClassificationRule, result.rule_id)
    revision = db.session.get(m.ClassificationRevision, result.revision_id)
    sweep = db.session.get(m.ClassificationSweep, result.sweep_id)
    assert correction.policy_revision_id == active and correction.supersedes_id is None
    assert learned.originating_override_id == correction.id
    assert learned.fingerprint_sha256 == correction.range_fingerprint_sha256
    assert revision.request_reason == "override" and revision.previous_revision_id == active
    assert revision.state == "pending" and revision.error_category == "dispatch_pending"
    assert result.next_dispatch.revision_id == revision.id
    assert sweep.state == "pending" and sweep.error_category == "dispatch_pending"
    assert env.item.active_classification_revision_id == active
    for table in (m.SegmentOverride, m.SourceClassificationRule, m.ClassificationSweep):
        assert not {"text", "transcript", "example_text"} & set(table.__table__.columns.keys())
    inputs = env.service._read_inputs(env.item.id)
    assert inputs.examples[0].segments == env.transcript.segments[:2]


@pytest.mark.parametrize("changes", [dict(admin_id=9999), dict(active_revision_id=9999),
    dict(active_revision_id=True), dict(first_segment_id="missing"),
    dict(last_segment_id="missing"), dict(label="invalid"),
    dict(first_segment_id=None), dict(admin_id=True)])
def test_invalid_corrections_write_nothing(env, changes):
    active, _ = finish(env)
    with pytest.raises(env.module.ClassificationServiceError):
        record(env, active, **changes)
    assert m.SegmentOverride.query.count() == m.SourceClassificationRule.query.count() == m.ClassificationSweep.query.count() == 0
    assert m.ClassificationRevision.query.count() == 1


def test_reversed_and_stale_browser_ranges_are_rejected(env):
    active, _ = finish(env)
    with pytest.raises(env.module.ClassificationServiceError):
        record(env, active, 2, 0)
    result = record(env, active, 0)
    complete(env, result.revision_id)
    with pytest.raises(env.module.ClassificationServiceError):
        record(env, active, 1)
    assert m.SegmentOverride.query.count() == 1


@pytest.mark.parametrize("change", ["hash", "policy", "membership"])
def test_correction_revalidates_canonical_and_current_policy(env, change):
    active, _ = finish(env)
    if change == "hash":
        env.item.segments_sha256 = "f" * 64
    elif change == "policy":
        row = db.session.get(m.ClassificationRevision, active)
        env.storage.objects[row.policy_s3_key] = b"private invalid policy"
    else:
        db.session.execute(sa.delete(m.SourceItem).where(m.SourceItem.item_id == env.item.id))
    db.session.commit()
    with pytest.raises(env.module.ClassificationServiceError):
        record(env, active)
    assert m.SegmentOverride.query.count() == 0


def test_overlaps_resolve_newest_per_segment_and_identical_relabels_link_history(env):
    active, _ = finish(env)
    first = record(env, active, 0, 2)
    complete(env, first.revision_id)
    second = record(env, first.revision_id, 1, 1, "editorial")
    complete(env, second.revision_id)
    _, policy, _ = artifacts(env, second.revision_id)
    assert [(row.label, row.override_id) for row in policy.ranges] == [
        ("paid_commercial", first.override_id), ("editorial", second.override_id),
        ("paid_commercial", first.override_id)]
    third = record(env, second.revision_id, 1, 1, "housekeeping")
    assert db.session.get(m.SegmentOverride, third.override_id).supersedes_id == second.override_id
    assert db.session.get(m.SegmentOverride, second.override_id).label == "editorial"


def test_replay_is_idempotent_even_after_activation_and_preserves_old_artifacts(env):
    active, _ = finish(env)
    old, _, _ = artifacts(env, active)
    old_policy, old_projection = old.policy_s3_key, old.projection_s3_key
    before = dict(env.storage.objects)
    first = record(env, active, 1)
    assert record(env, active, 1) == first
    complete(env, first.revision_id)
    assert record(env, active, 1) == first
    latest, _, projection = artifacts(env, first.revision_id)
    assert latest.projection_sha256 != old.projection_sha256
    assert env.storage.objects[old_policy] == before[old_policy]
    assert env.storage.objects[old_projection] == before[old_projection]
    assert len(projection.segments) == 2
    assert m.SegmentOverride.query.count() == m.SourceClassificationRule.query.count() == m.ClassificationSweep.query.count() == 1


def test_rules_match_normalized_same_source_only_and_relabels_reuse_unique_rules(env):
    active, _ = finish(env)
    first = record(env, active, 1)
    complete(env, first.revision_id)
    second = record(env, first.revision_id, 1, label="editorial")
    complete(env, second.revision_id)
    third = record(env, second.revision_id, 1)
    assert third.rule_id == first.rule_id and m.SourceClassificationRule.query.count() == 2
    matching, transcript = add_item(env, external_id="same", texts=("  BUY NOW with promo code  ",))
    inputs = env.service._read_inputs(matching.id)
    assert len(inputs.exact) == 1 and inputs.exact[0].label == "paid_commercial"
    assert inputs.exact[0].decision_source == "source_rule"
    foreign, _ = add_item(env, source=env.other, external_id="foreign", texts=(transcript.segments[0].text,))
    assert env.service._read_inputs(foreign.id).exact == ()


def test_old_example_loads_by_stored_transcript_hash_not_current_pointer(env):
    active, _ = finish(env)
    record(env, active, 1)
    old_key = env.item.segments_s3_key
    env.item.transcript_sha256 = "e" * 64
    env.item.segments_s3_key = "items/podcast/episode/transcripts/" + "e" * 64 + ".json"
    db.session.commit()
    target, _ = add_item(env, external_id="future", texts=("Unrelated editorial",))
    env.storage.reads.clear()
    inputs = env.service._read_inputs(target.id)
    assert inputs.examples[0].segments == env.transcript.segments[1:2]
    assert old_key in [key for key, _ in env.storage.reads]


def test_sweep_is_keyset_bounded_exact_only_and_completed_replay_is_immutable(env):
    active, _ = finish(env)
    correction = record(env, active, 1)
    for number in range(53):
        add_item(env, external_id=f"scan-{number}", texts=("Buy now with promo code" if number % 2 else "Unrelated insight",))
    foreign, _ = add_item(env, source=env.other, external_id="other-source", texts=("Buy now with promo code",))
    env.storage.reads.clear()
    first = env.service.scan_source_rule(correction.sweep_id)
    assert first.state == "running" and first.items_scanned == 50 and first.items_queued == 24
    assert first.next_task_id and len(first.revision_ids) == 24
    assert all("/transcripts/" in key for key, _ in env.storage.reads)
    assert not m.ClassificationRevision.query.filter_by(item_id=foreign.id).count()
    second = env.service.scan_source_rule(correction.sweep_id)
    assert second.state == "completed" and second.items_scanned == 54 and second.items_queued == 26
    assert second.next_task_id is None
    sweep = db.session.get(m.ClassificationSweep, correction.sweep_id)
    before = {column.name: getattr(sweep, column.name) for column in sweep.__table__.columns}
    env.service.scan_source_rule(correction.sweep_id)
    assert {column.name: getattr(sweep, column.name) for column in sweep.__table__.columns} == before


def test_sweep_storage_failure_retains_cursor_and_active_revision_then_resumes(env, monkeypatch):
    active, _ = finish(env)
    correction = record(env, active, 1)
    target, _ = add_item(env, external_id="retry-target", texts=("Buy now with promo code",))
    original = env.storage.get_bytes
    def broken(key, **kwargs):
        if "/retry-target/" in key:
            raise StorageTransientError("private storage message")
        return original(key, **kwargs)
    monkeypatch.setattr(env.storage, "get_bytes", broken)
    result = env.service.scan_source_rule(correction.sweep_id)
    assert result.items_scanned == 1 and result.items_queued == 0 and result.state == "running"
    assert env.item.active_classification_revision_id == active
    monkeypatch.setattr(env.storage, "get_bytes", original)
    failed_at = db.session.get(m.ClassificationSweep, correction.sweep_id).updated_at
    monkeypatch.setattr(env.module, "utc_now", lambda: failed_at + timedelta(seconds=30))
    result = env.service.scan_source_rule(correction.sweep_id)
    assert result.state == "completed" and result.items_scanned == 2 and result.items_queued == 1


def test_sweep_permanent_failure_retains_active_revision(env):
    active, _ = finish(env)
    correction = record(env, active, 1)
    target, _ = add_item(env, external_id="invalid-target")
    env.storage.objects[target.segments_s3_key] = b"private invalid transcript"
    result = env.service.scan_source_rule(correction.sweep_id)
    assert result.state == "failed" and env.item.active_classification_revision_id == active
    assert db.session.get(m.ClassificationSweep, correction.sweep_id).error_category == "invalid_canonical"


def test_correction_database_failure_rolls_back_every_new_row(env, monkeypatch):
    active, _ = finish(env)
    original = env.service._insert_revision
    def broken(*args, **kwargs):
        original(*args, **kwargs)
        raise sa.exc.OperationalError("statement", {}, RuntimeError("private database message"))
    monkeypatch.setattr(env.service, "_insert_revision", broken)
    with pytest.raises(sa.exc.SQLAlchemyError):
        record(env, active)
    assert m.SegmentOverride.query.count() == m.SourceClassificationRule.query.count() == m.ClassificationSweep.query.count() == 0
    assert m.ClassificationRevision.query.count() == 1


def test_sweep_database_failure_does_not_advance_match_or_duplicate_request(env, monkeypatch):
    active, _ = finish(env)
    correction = record(env, active, 1)
    target, _ = add_item(env, external_id="db-retry", texts=("Buy now with promo code",))
    original = env.service._insert_revision
    def broken(*args, **kwargs):
        original(*args, **kwargs)
        raise sa.exc.OperationalError("statement", {}, RuntimeError("private database message"))
    monkeypatch.setattr(env.service, "_insert_revision", broken)
    with pytest.raises(sa.exc.SQLAlchemyError):
        env.service.scan_source_rule(correction.sweep_id)
    sweep = db.session.get(m.ClassificationSweep, correction.sweep_id)
    assert sweep.items_scanned == 1 and sweep.items_queued == 0
    assert not m.ClassificationRevision.query.filter_by(item_id=target.id).count()
    monkeypatch.setattr(env.service, "_insert_revision", original)
    result = env.service.scan_source_rule(correction.sweep_id)
    assert result.items_queued == 1 and m.ClassificationRevision.query.filter_by(item_id=target.id).count() == 1


def test_worker_publishes_committed_sweep_pages_and_recovers_unknown_broker_outcome(worker, monkeypatch):
    env = worker.env
    active, _ = finish(env)
    correction = record(env, active, 1)
    for number in range(50):
        add_item(env, external_id=f"page-{number}", texts=("Unrelated editorial",))
    def broken(*args, **kwargs):
        assert not db.session().in_transaction()
        raise RuntimeError("private broker message")
    monkeypatch.setattr(tasks.celery_app, "send_task", broken)
    tasks.scan_source_rule.run(correction.sweep_id)
    sweep = db.session.get(m.ClassificationSweep, correction.sweep_id)
    expected = sweep.celery_task_id
    assert sweep.items_scanned == 50 and sweep.error_category == "dispatch_pending"
    monkeypatch.setattr(tasks.celery_app, "send_task", worker.send)
    tasks.reconcile_classifications.run()
    dispatch = next(entry for entry in worker.sent if entry[0] == "tasks.scan_source_rule")
    assert dispatch[1] == [correction.sweep_id] and dispatch[2]["task_id"] == expected
    tasks.scan_source_rule.run(correction.sweep_id)
    assert sweep.state == "completed" and sweep.items_scanned == 51


def test_sweep_publication_cleanup_cannot_overwrite_fast_completion(worker, monkeypatch):
    env = worker.env
    active, _ = finish(env)
    correction = record(env, active, 1)
    for number in range(50):
        add_item(env, external_id=f"fast-{number}", texts=("Unrelated editorial",))
    def fast(name, *, args=None, **kwargs):
        worker.send(name, args=args, **kwargs)
        if name == "tasks.scan_source_rule":
            tasks.scan_source_rule.run(*args)
    monkeypatch.setattr(tasks.celery_app, "send_task", fast)
    tasks.scan_source_rule.run(correction.sweep_id)
    sweep = db.session.get(m.ClassificationSweep, correction.sweep_id)
    assert sweep.state == "completed" and sweep.error_category is None and sweep.items_scanned == 51


def test_override_handoff_immediately_reclassifies_current_item_without_scanning_first(worker):
    env = worker.env
    active, _ = finish(env)
    correction = record(env, active, 1)
    tasks.publish_override(correction)
    assert [entry[0] for entry in worker.sent] == ["tasks.classify_item", "tasks.scan_source_rule"]
    tasks.classify_item.run(*worker.sent[0][1])
    while db.session.get(m.ClassificationRevision, correction.revision_id).state != "completed":
        message = next(entry for entry in reversed(worker.sent) if entry[0] == "tasks.classify_item")
        tasks.classify_item.run(*message[1])
    assert env.item.active_classification_revision_id == correction.revision_id
    assert db.session.get(m.ClassificationSweep, correction.sweep_id).items_scanned == 0


def test_disabled_acknowledged_sweep_delivery_restores_durable_outbox(worker):
    env = worker.env
    active, _ = finish(env)
    correction = record(env, active, 1)
    tasks.publish_override(correction)
    sweep = db.session.get(m.ClassificationSweep, correction.sweep_id)
    assert sweep.error_category is None
    worker.app.config["CLASSIFICATION_ENABLED"] = False
    tasks.scan_source_rule.run(correction.sweep_id)
    assert sweep.error_category == "dispatch_pending" and sweep.items_scanned == 0


def test_reconciler_recovers_aged_acknowledged_sweep_and_renews_delivery_grace(worker):
    env = worker.env
    active, _ = finish(env)
    correction = record(env, active, 1)
    tasks.publish_override(correction)
    sweep = db.session.get(m.ClassificationSweep, correction.sweep_id)
    sweep.updated_at = m.utc_now() - timedelta(days=7)
    db.session.commit()
    worker.sent.clear()
    tasks.reconcile_classifications.run()
    assert len([entry for entry in worker.sent if entry[0] == "tasks.scan_source_rule"]) == 1
    tasks.reconcile_classifications.run()
    assert len([entry for entry in worker.sent if entry[0] == "tasks.scan_source_rule"]) == 1


def test_unfinished_memberships_advance_sweep_without_reading_nonexistent_transcripts(env):
    active, _ = finish(env)
    correction = record(env, active, 1)
    pending, _ = add_item(env, external_id="unfinished")
    pending.status, pending.segments_s3_key = "discovered", None
    db.session.commit()
    env.storage.reads.clear()
    result = env.service.scan_source_rule(correction.sweep_id)
    assert result.state == "completed" and result.items_scanned == 2 and result.items_queued == 0
    assert all("unfinished" not in key for key, _ in env.storage.reads)


def test_sweep_task_result_is_json_serializable_for_celery_backend(worker):
    env = worker.env
    active, _ = finish(env)
    correction = record(env, active, 1)
    result = tasks.scan_source_rule.run(correction.sweep_id)
    assert json.loads(json.dumps(result))["state"] == "completed"


def replace_canonical(env, item, text):
    """Install a distinct immutable artifact, changing only the current pointer."""
    transcript = normalize_vtt("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n" + text,
        platform=item.platform, external_id=item.external_id, item_id=item.id,
        source_id=env.source.id, language="en", tool_version="test", origin="creator_caption",
        provider_version="test", canonical_url=item.canonical_url)
    raw = transcript.to_json().encode()
    item.transcript_sha256, item.segments_sha256 = transcript.transcript_sha256, sha256(raw).hexdigest()
    item.segments_s3_key = f"items/{item.platform}/{item.external_id}/transcripts/{item.transcript_sha256}.json"
    item.transcript_s3_key = item.segments_s3_key.removesuffix(".json") + ".txt"
    env.storage.objects[item.segments_s3_key] = raw
    db.session.commit()
    return transcript


@pytest.mark.parametrize("timing", ["before_preparation", "during_preparation", "after_preparation"])
def test_sweep_matches_are_bound_to_the_canonical_snapshot_until_insertion(env, monkeypatch, timing):
    active, _ = finish(env)
    correction = record(env, active, 1)
    target, _ = add_item(env, external_id="race-target", texts=("Buy now with promo code",))
    original = env.service.prepare_revision_request
    original_read = env.storage.get_bytes
    replaced = []
    def racing(item_id, **kwargs):
        if item_id != target.id or replaced:
            return original(item_id, **kwargs)
        if timing == "before_preparation":
            replaced.append(replace_canonical(env, target, "Replacement with no matching commercial"))
        if timing == "during_preparation":
            def read_then_replace(key, **options):
                raw = original_read(key, **options)
                if "/race-target/" in key and not replaced:
                    replaced.append(replace_canonical(env, target, "Replacement with no matching commercial"))
                return raw
            monkeypatch.setattr(env.storage, "get_bytes", read_then_replace)
        request = original(item_id, **kwargs)
        if timing == "after_preparation":
            replaced.append(replace_canonical(env, target, "Replacement with no matching commercial"))
        return request
    monkeypatch.setattr(env.service, "prepare_revision_request", racing)
    first = env.service.scan_source_rule(correction.sweep_id)
    assert replaced and first.state == "running"
    assert first.cursor_item_id == env.item.id and first.items_scanned == 1 and first.items_queued == 0
    assert not m.ClassificationRevision.query.filter_by(item_id=target.id).count()
    assert env.item.active_classification_revision_id == active
    second = env.service.scan_source_rule(correction.sweep_id)
    assert second.state == "completed" and second.items_scanned == 2 and second.items_queued == 0
    assert not m.ClassificationRevision.query.filter_by(item_id=target.id).count()


def test_repeated_transient_sweep_failures_have_a_durable_thirty_second_floor(worker, monkeypatch):
    env = worker.env
    active, _ = finish(env)
    correction = record(env, active, 1)
    add_item(env, external_id="delayed-target", texts=("Buy now with promo code",))
    now = [m.utc_now()]
    monkeypatch.setattr(env.module, "utc_now", lambda: now[0])
    monkeypatch.setattr(m, "utc_now", lambda: now[0])
    original = env.storage.get_bytes
    failures = []
    def broken(key, **kwargs):
        if "/delayed-target/" in key:
            failures.append(now[0])
            raise StorageTransientError("private storage message")
        return original(key, **kwargs)
    monkeypatch.setattr(env.storage, "get_bytes", broken)
    tasks.scan_source_rule.run(correction.sweep_id)
    assert not [entry for entry in worker.sent if entry[0] == "tasks.scan_source_rule"]
    sweep = db.session.get(m.ClassificationSweep, correction.sweep_id)
    task_id = sweep.celery_task_id
    assert sweep.error_category == "storage_transient" and sweep.updated_at == now[0]
    for elapsed in (0, 29):
        now[0] = failures[0] + timedelta(seconds=elapsed)
        tasks.scan_source_rule.run(correction.sweep_id)
        assert tasks._publish_source_sweep(correction.sweep_id) is False
        tasks._reconcile_source_sweeps()
        assert len(failures) == 1 and not worker.sent
        assert sweep.updated_at == failures[0]
    now[0] = failures[0] + timedelta(seconds=30)
    tasks._reconcile_source_sweeps()
    assert worker.sent[-1][1] == [correction.sweep_id] and worker.sent[-1][2]["task_id"] == task_id
    tasks.scan_source_rule.run(correction.sweep_id)
    assert len(failures) == 2 and sweep.error_category == "storage_transient"
    assert sweep.updated_at == now[0]
    worker.sent.clear()
    tasks._reconcile_source_sweeps()
    assert tasks._publish_source_sweep(correction.sweep_id) is False and not worker.sent
    now[0] += timedelta(seconds=30)
    monkeypatch.setattr(env.storage, "get_bytes", original)
    tasks._reconcile_source_sweeps()
    tasks.scan_source_rule.run(correction.sweep_id)
    assert sweep.state == "completed" and sweep.items_scanned == 2 and sweep.items_queued == 1
    assert env.item.active_classification_revision_id == active


def test_delayed_sweep_unknown_broker_outcome_and_disabled_rollout_preserve_eligibility(worker, monkeypatch):
    env = worker.env
    active, _ = finish(env)
    correction = record(env, active, 1)
    now = [m.utc_now()]
    monkeypatch.setattr(env.module, "utc_now", lambda: now[0])
    monkeypatch.setattr(m, "utc_now", lambda: now[0])
    original = env.storage.get_bytes
    monkeypatch.setattr(env.storage, "get_bytes", lambda *a, **kw: (_ for _ in ()).throw(StorageTransientError("private")))
    tasks.scan_source_rule.run(correction.sweep_id)
    sweep = db.session.get(m.ClassificationSweep, correction.sweep_id)
    failed_at, task_id = sweep.updated_at, sweep.celery_task_id
    attempts = []
    def unknown(*args, **kwargs):
        attempts.append(now[0])
        raise RuntimeError("private broker message")
    monkeypatch.setattr(tasks.celery_app, "send_task", unknown)
    now[0] = failed_at + timedelta(seconds=29)
    assert tasks._publish_source_sweep(correction.sweep_id) is False
    tasks._reconcile_source_sweeps()
    assert attempts == []
    worker.app.config["CLASSIFICATION_ENABLED"] = False
    tasks.scan_source_rule.run(correction.sweep_id)
    assert sweep.error_category == "storage_transient" and sweep.updated_at == failed_at
    worker.app.config["CLASSIFICATION_ENABLED"] = True
    now[0] = failed_at + timedelta(seconds=30)
    tasks._reconcile_source_sweeps()
    assert attempts == [now[0]] and sweep.error_category == "storage_transient" and sweep.updated_at == failed_at
    monkeypatch.setattr(tasks.celery_app, "send_task", worker.send)
    tasks._reconcile_source_sweeps()
    assert worker.sent[-1][2]["task_id"] == task_id
    monkeypatch.setattr(env.storage, "get_bytes", original)
    tasks.scan_source_rule.run(correction.sweep_id)
    assert sweep.state == "completed" and env.item.active_classification_revision_id == active


def test_retry_publication_cleanup_preserves_faster_failure_at_the_same_cursor(worker, monkeypatch):
    env = worker.env
    active, _ = finish(env)
    correction = record(env, active, 1)
    now = [m.utc_now()]
    monkeypatch.setattr(env.module, "utc_now", lambda: now[0])
    monkeypatch.setattr(m, "utc_now", lambda: now[0])
    monkeypatch.setattr(env.storage, "get_bytes", lambda *a, **kw: (_ for _ in ()).throw(StorageTransientError("private")))
    tasks.scan_source_rule.run(correction.sweep_id)
    sweep = db.session.get(m.ClassificationSweep, correction.sweep_id)
    task_id = sweep.celery_task_id
    now[0] = sweep.updated_at + timedelta(seconds=30)
    def fast(name, *, args=None, **kwargs):
        worker.send(name, args=args, **kwargs)
        tasks.scan_source_rule.run(*args)
    monkeypatch.setattr(tasks.celery_app, "send_task", fast)
    tasks._reconcile_source_sweeps()
    assert len(worker.sent) == 1
    assert sweep.error_category == "storage_transient" and sweep.updated_at == now[0]
    assert sweep.celery_task_id == task_id and sweep.items_scanned == 0
    assert tasks._publish_source_sweep(correction.sweep_id) is False
