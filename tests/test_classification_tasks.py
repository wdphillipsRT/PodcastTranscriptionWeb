"""Classification worker/outbox contract with real checkpoints and offline boundaries."""
from datetime import timedelta
import socket
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from extensions import db
import models as m
import tasks
from router_classifier import ClassifierError
from tests.test_classification_service import env, add_item, finish


@pytest.fixture
def worker(env, app, monkeypatch):
    production_factory = getattr(tasks, "get_classification_service", None)
    def forbidden(*args, **kwargs):
        raise AssertionError("live boundary forbidden")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(tasks, "get_classification_service", lambda: env.service, raising=False)
    monkeypatch.setattr(tasks, "get_youtube_media_client", forbidden)
    monkeypatch.setattr(tasks, "get_processing_http_session", forbidden)
    app.config["CLASSIFICATION_ENABLED"] = True
    sent = []
    def send(name, *, args=None, **options):
        assert not db.session().in_transaction(), "publication preceded commit"
        if name == "tasks.classify_item":
            revision = db.session.get(m.ClassificationRevision, args[0])
            assert revision.error_category == "dispatch_pending"
            assert revision.celery_task_id == options["task_id"]
            item_id = revision.item_id
            db.session.commit()
            with tasks._item_lock(item_id) as acquired:
                assert acquired, "publication retained worker lock"
        sent.append((name, args, options))
    monkeypatch.setattr(tasks.celery_app, "send_task", send)
    return SimpleNamespace(env=env, sent=sent, app=app, send=send, production_factory=production_factory)


def task():
    value = getattr(tasks, "classify_item", None)
    assert callable(value), "classification task missing"
    return value


def reconcile(**kwargs):
    value = getattr(tasks, "reconcile_classifications", None)
    assert callable(value), "classification reconciler missing"
    return value.run(**kwargs)


def test_checkpoint_chain_commits_before_publish_and_replays_without_reclassification(worker):
    revision_id = worker.env.service.ensure_revision(worker.env.item.id, "backfill")
    task().run(revision_id, "prepare")
    assert worker.sent[-1][1] == [revision_id, "block", 1]
    task().run(*worker.sent[-1][1])
    assert worker.sent[-1][1] == [revision_id, "finalize", None]
    task().run(*worker.sent[-1][1])
    revision = db.session.get(m.ClassificationRevision, revision_id)
    assert revision.state == "completed" and revision.attempts == 0
    assert worker.env.item.active_classification_revision_id == revision_id
    assert len(worker.env.classifier.calls) == 1
    count = len(worker.sent)
    task().run(revision_id, "prepare")
    task().run(revision_id, "block", 1)
    task().run(revision_id, "finalize")
    assert len(worker.sent) == count and len(worker.env.classifier.calls) == 1


@pytest.mark.parametrize("phase,ordinal", [("unknown", None), ("block", None), ("block", True),
    ("block", 0), ("prepare", 1), ("finalize", 1)])
def test_phase_contract_is_closed(worker, phase, ordinal):
    with pytest.raises(ValueError):
        task().run(worker.env.item.id, phase, ordinal)
    assert not worker.sent


def test_worker_lock_and_disabled_switch_prevent_invocation(worker):
    identity = worker.env.service.ensure_revision(worker.env.item.id, "backfill")
    with tasks._item_lock(worker.env.item.id) as acquired:
        assert acquired
        task().run(identity, "prepare")
    worker.app.config["CLASSIFICATION_ENABLED"] = False
    task().run(identity, "prepare")
    assert not worker.sent and m.ClassificationBlock.query.count() == 0


def test_unknown_broker_outcome_remains_replayable(worker, monkeypatch):
    identity = worker.env.service.ensure_revision(worker.env.item.id, "backfill")
    def broken(*args, **kwargs):
        raise RuntimeError("private broker response")
    monkeypatch.setattr(tasks.celery_app, "send_task", broken)
    task().run(identity, "prepare")
    revision = db.session.get(m.ClassificationRevision, identity)
    expected = revision.celery_task_id
    assert revision.error_category == "dispatch_pending"
    monkeypatch.setattr(tasks.celery_app, "send_task", worker.send)
    reconcile()
    assert worker.sent[-1][2]["task_id"] == expected


def test_fast_worker_cannot_be_overwritten_by_publication_cleanup(worker, monkeypatch):
    identity = worker.env.service.ensure_revision(worker.env.item.id, "backfill")
    def fast(name, *, args, **kwargs):
        worker.send(name, args=args, **kwargs)
        # Run each downstream checkpoint before the sending worker returns.
        task().run(*args)
    monkeypatch.setattr(tasks.celery_app, "send_task", fast)
    task().run(identity, "prepare")
    revision = db.session.get(m.ClassificationRevision, identity)
    assert revision.state == "completed" and revision.error_category is None
    assert revision.celery_task_id.endswith("-finalize")


def test_invalid_model_output_gets_two_attempts_because_temperature_zero_replays_it(worker):
    identity = worker.env.service.ensure_revision(worker.env.item.id, "backfill")
    task().run(identity, "prepare")
    worker.env.classifier.error = ClassifierError("model_invalid_output", advance_key=True)
    task().run(identity, "block", 1)
    assert db.session.get(m.ClassificationRevision, identity).state == "retrying"
    task().run(identity, "block", 1)
    revision = db.session.get(m.ClassificationRevision, identity)
    assert (revision.state, revision.error_category, revision.attempts) == ("failed", "model_invalid_output", 2)


@pytest.mark.parametrize("category", ["storage_transient"])
def test_transient_errors_exhaust_at_five_persisted_failures(worker, category):
    identity = worker.env.service.ensure_revision(worker.env.item.id, "backfill")
    task().run(identity, "prepare")
    worker.sent.clear()
    if category == "storage_transient":
        from s3_utils import StorageTransientError
        worker.env.classifier.error = StorageTransientError("private storage payload")
    else:
        worker.env.classifier.error = ClassifierError(category)
    for attempt in range(1, 6):
        task().run(identity, "block", 1)
        revision = db.session.get(m.ClassificationRevision, identity)
        assert revision.attempts == attempt
        assert revision.error_category == category
        assert revision.state == ("retrying" if attempt < 5 else "failed")
        if attempt < 5:
            options = worker.sent[-1][2]
            assert options["task_id"].endswith("-b1")
            assert 30 * 2 ** (attempt - 1) + 1 <= options["countdown"] <= 30 * 2 ** (attempt - 1) + 30
    assert len(worker.sent) == 4
    task().run(identity, "block", 1)
    assert len(worker.env.classifier.calls) == 5


@pytest.mark.parametrize("category", ["model_auth", "model_config", "model_mismatch"])
def test_permanent_classifier_errors_stop_immediately(worker, category):
    identity = worker.env.service.ensure_revision(worker.env.item.id, "backfill")
    task().run(identity, "prepare")
    worker.sent.clear()
    worker.env.classifier.error = ClassifierError(category)
    task().run(identity, "block", 1)
    revision = db.session.get(m.ClassificationRevision, identity)
    assert (revision.state, revision.attempts, revision.error_category) == ("failed", 1, category)
    assert not worker.sent


def test_invalid_canonical_stops_automatic_retry(worker):
    identity = worker.env.service.ensure_revision(worker.env.item.id, "backfill")
    worker.env.storage.objects[worker.env.item.segments_s3_key] = b"invalid private text"
    task().run(identity, "prepare")
    row = db.session.get(m.ClassificationRevision, identity)
    assert (row.state, row.error_category, row.attempts) == ("failed", "invalid_canonical", 1)
    assert not worker.sent


def test_stale_block_requires_expired_safety_window_and_lifetime_lock(worker):
    identity = worker.env.service.ensure_revision(worker.env.item.id, "backfill")
    task().run(identity, "prepare")
    revision = db.session.get(m.ClassificationRevision, identity)
    block = m.ClassificationBlock.query.one()
    block.state = "running"
    revision.error_category = None
    block.started_at = block.heartbeat_at = m.utc_now() - timedelta(seconds=2159)
    revision.heartbeat_at = block.heartbeat_at
    db.session.commit()
    worker.sent.clear()
    reconcile()
    assert revision.attempts == 0 and not worker.sent
    block.heartbeat_at = block.started_at = revision.heartbeat_at = m.utc_now() - timedelta(seconds=2161)
    db.session.commit()
    with tasks._item_lock(worker.env.item.id) as acquired:
        assert acquired
        reconcile()
    assert revision.attempts == 0 and not worker.sent
    reconcile()
    assert (revision.attempts, revision.state, revision.error_category) == (1, "retrying", "worker_timeout")
    assert (block.state, block.error_category) == ("failed", "worker_timeout")
    assert worker.sent[-1][1] == [identity, "block", 1]


def test_reconciliation_is_bounded_keyset_and_ignores_source_enabled(worker):
    worker.env.source.enabled = False
    db.session.commit()
    for number in range(4):
        add_item(worker.env, external_id=f"backlog-{number}")
    worker.app.config["CLASSIFICATION_RECONCILE_BATCH_SIZE"] = 2
    result = reconcile()
    assert result["scanned"] == result["created"] == 2
    assert m.ClassificationRevision.query.count() == 2
    continuation = next(entry for entry in worker.sent if entry[0] == "tasks.reconcile_classifications")
    result = reconcile(**continuation[2]["kwargs"])
    assert result["scanned"] == result["created"] == 2
    assert m.ClassificationRevision.query.count() == 4


def test_disabled_reconciliation_persists_without_publishing_or_recreating_failures(worker):
    worker.app.config["CLASSIFICATION_ENABLED"] = False
    reconcile()
    row = m.ClassificationRevision.query.one()
    assert row.state == "pending" and row.error_category == "dispatch_pending"
    row.state, row.error_category, row.attempts = "failed", "claude_auth", 1
    db.session.commit()
    reconcile()
    assert m.ClassificationRevision.query.count() == 1 and not worker.sent


def test_policy_upgrade_keeps_active_prior_revision(worker):
    identity, _ = finish(worker.env)
    # Emulate an earlier code-owned policy without mutating ORM immutable rows.
    db.session.execute(sa.update(m.ClassificationRevision).where(m.ClassificationRevision.id == identity)
        .values(policy_version="older-policy"))
    db.session.commit()
    reconcile()
    newest = m.ClassificationRevision.query.order_by(m.ClassificationRevision.id.desc()).first()
    assert newest.request_reason == "policy_upgrade" and newest.state == "pending"
    assert worker.env.item.active_classification_revision_id == identity


def test_superseded_revision_hands_off_to_current_inputs(worker):
    from tests.test_classification_service import override
    identity = worker.env.service.ensure_revision(worker.env.item.id, "backfill")
    override(worker.env, 0)
    task().run(identity, "prepare")
    assert db.session.get(m.ClassificationRevision, identity).state == "superseded"
    newest = m.ClassificationRevision.query.order_by(m.ClassificationRevision.id.desc()).first()
    assert newest.id != identity and newest.request_reason == "policy_upgrade"
    assert worker.sent[-1][1] == [newest.id, "prepare", None]


def test_source_rule_task_is_lazy_and_disabled(worker, monkeypatch):
    calls = []
    monkeypatch.setattr(worker.env.service, "scan_source_rule", lambda identity: calls.append(identity), raising=False)
    value = getattr(tasks, "scan_source_rule", None)
    assert callable(value), "source rule task missing"
    value.run(1)
    worker.app.config["CLASSIFICATION_ENABLED"] = False
    value.run(2)
    assert calls == [1]


@pytest.mark.parametrize("phase", ["prepare", "finalize", "block"])
def test_unknown_retry_publication_replays_stable_checkpoint_safely(worker, monkeypatch, phase):
    from s3_utils import StorageTransientError
    identity = worker.env.service.ensure_revision(worker.env.item.id, "backfill")
    if phase != "prepare":
        task().run(identity, "prepare")
    if phase == "finalize":
        task().run(identity, "block", 1)
    original_read = worker.env.storage.get_bytes
    def storage_failure(*args, **kwargs):
        raise StorageTransientError("PRIVATE storage https://example.com/private")
    def broker_failure(*args, **kwargs):
        raise RuntimeError("PRIVATE broker")
    monkeypatch.setattr(worker.env.storage, "get_bytes", storage_failure)
    monkeypatch.setattr(tasks.celery_app, "send_task", broker_failure)
    task().run(identity, phase, 1 if phase == "block" else None)
    row = db.session.get(m.ClassificationRevision, identity)
    assert row.attempts == 1 and row.error_category == "dispatch_pending"
    expected = row.celery_task_id
    monkeypatch.setattr(worker.env.storage, "get_bytes", original_read)
    monkeypatch.setattr(tasks.celery_app, "send_task", worker.send)
    reconcile()
    assert worker.sent[-1][2]["task_id"] == expected
    assert "PRIVATE" not in repr(worker.sent[-1])
    assert 31 <= worker.sent[-1][2]["countdown"] <= 60
    assert row.error_category == ("storage_transient" if phase == "block" else None)
    task().run(*worker.sent[-1][1])
    assert row.attempts == 1


@pytest.mark.parametrize("revision_state", ["running", "retrying"])
def test_stale_recovery_exhaustion_does_not_publish(worker, revision_state):
    identity = worker.env.service.ensure_revision(worker.env.item.id, "backfill")
    task().run(identity, "prepare")
    revision = db.session.get(m.ClassificationRevision, identity)
    block = m.ClassificationBlock.query.one()
    block.state, revision.error_category = "running", None
    block.heartbeat_at = block.started_at = revision.heartbeat_at = m.utc_now() - timedelta(seconds=3000)
    revision.state = revision_state
    if revision_state == "retrying":
        revision.heartbeat_at = m.utc_now()
    revision.attempts = 4
    db.session.commit()
    worker.sent.clear()
    reconcile()
    assert (revision.state, revision.attempts, revision.error_category) == ("failed", 5, "worker_timeout")
    assert not worker.sent


def test_completed_block_checkpoint_resumes_without_recalling_claude(worker):
    item, _ = add_item(worker.env, external_id="large", texts=tuple(f"Editorial {n}" for n in range(85)))
    identity = worker.env.service.ensure_revision(item.id, "backfill")
    preparation = worker.env.service.prepare_revision(identity)
    assert len(preparation.block_ordinals) == 2
    # Simulate death after the block's durable commit but before its publication.
    worker.env.service.run_block(identity, 1)
    reconcile()
    assert any(entry[1] == [identity, "block", 2] for entry in worker.sent)
    task().run(identity, "block", 1)
    assert len(worker.env.classifier.calls) == 1
    task().run(identity, "block", 2)
    task().run(identity, "finalize")
    revision = db.session.get(m.ClassificationRevision, identity)
    assert revision.state == "completed" and revision.total_cost_microusd == 50
    assert revision.attempts == 0 and len(worker.env.classifier.calls) == 2


def test_reconciliation_caps_total_candidates_at_one_hundred(worker):
    for number in range(104):
        add_item(worker.env, external_id=f"bounded-{number}")
    worker.app.config["CLASSIFICATION_RECONCILE_BATCH_SIZE"] = 500
    worker.app.config["CLASSIFICATION_ENABLED"] = False
    result = reconcile()
    assert result["scanned"] == result["created"] == 100
    assert m.ClassificationRevision.query.count() == 100
    assert not worker.sent


def test_lazy_service_construction_reads_no_router_credentials(worker, monkeypatch):
    from router_classifier import RouterClassifier
    monkeypatch.setattr(tasks, "get_processing_storage", lambda: worker.env.storage)
    service = worker.production_factory()
    assert isinstance(service.classifier, RouterClassifier)
    assert service.classifier.client.token_file == worker.app.config["ROUTER_TOKEN_FILE"]
    assert service.classifier.client.expected_model == worker.app.config["ROUTER_EXPECTED_MODEL"]


def test_queued_message_delivered_during_disabled_rollout_remains_recoverable(worker):
    identity = worker.env.service.ensure_revision(worker.env.item.id, "backfill")
    row = db.session.get(m.ClassificationRevision, identity)
    row.error_category = None  # Publisher already received the broker acknowledgement.
    db.session.commit()
    worker.app.config["CLASSIFICATION_ENABLED"] = False
    task().run(identity, "prepare")
    assert row.state == "pending" and row.error_category == "dispatch_pending"
    assert not worker.sent and not worker.env.classifier.calls
    worker.app.config["CLASSIFICATION_ENABLED"] = True
    reconcile()
    assert worker.sent[-1][1] == [identity, "prepare", None]


def test_fast_retry_same_task_id_cannot_lose_new_outbox(worker, monkeypatch):
    identity = worker.env.service.ensure_revision(worker.env.item.id, "backfill")
    task().run(identity, "prepare")
    from s3_utils import StorageTransientError
    worker.env.classifier.error = StorageTransientError("private storage payload")
    sending = []
    def fast(name, *, args, **options):
        worker.send(name, args=args, **options)
        if sending:
            raise RuntimeError("unknown broker outcome")
        sending.append(True)
        task().run(*args)
    monkeypatch.setattr(tasks.celery_app, "send_task", fast)
    task().run(identity, "block", 1)
    row = db.session.get(m.ClassificationRevision, identity)
    assert (row.attempts, row.state, row.error_category) == (2, "retrying", "dispatch_pending")


def test_worker_preserves_the_generation_time_limit(worker):
    item, _ = add_item(worker.env, external_id="budget", texts=tuple(f"Line {n}" for n in range(81)))
    identity = worker.env.service.ensure_revision(item.id, "backfill")
    task().run(identity, "prepare")
    task().run(identity, "block", 1)
    row = db.session.get(m.ClassificationRevision, identity)
    block = m.ClassificationBlock.query.filter_by(revision_id=identity, ordinal=1).one()
    settings = worker.env.settings
    block.started_at = block.completed_at - timedelta(
        seconds=settings.CLASSIFIER_MAX_REVISION_SECONDS - settings.CLASSIFIER_TIMEOUT_SECONDS + 1)
    db.session.commit()
    worker.sent.clear()
    task().run(identity, "block", 2)
    assert row.state == "failed" and row.error_category == "revision_budget_exhausted"
    assert row.total_cost_microusd == 25 and len(worker.env.classifier.calls) == 1 and not worker.sent


def test_acknowledged_prepare_lost_to_contention_replays_after_delivery_grace(worker):
    identity = worker.env.service.ensure_revision(worker.env.item.id, "backfill")
    item_id = worker.env.item.id
    revision = db.session.get(m.ClassificationRevision, identity)
    dispatch = tasks._revision_dispatch(revision)
    assert tasks._publish_classification(dispatch)
    assert revision.error_category is None and len(worker.sent) == 1
    with tasks._item_lock(item_id) as acquired:
        assert acquired
        task().run(identity, "prepare")
    assert revision.error_category is None and revision.state == "pending"
    assert revision.attempts == 0 and m.ClassificationBlock.query.count() == 0
    reconcile()
    assert len(worker.sent) == 1

    # Seven days after the acknowledged delivery, no worker ever started it.
    revision.updated_at = m.utc_now() - timedelta(days=7)
    db.session.commit()
    worker.app.config["CLASSIFICATION_ENABLED"] = False
    reconcile()
    assert len(worker.sent) == 1 and revision.error_category is None
    worker.app.config["CLASSIFICATION_ENABLED"] = True
    with tasks._item_lock(item_id) as acquired:
        assert acquired
        reconcile()
    assert len(worker.sent) == 1 and revision.error_category is None
    result = reconcile()
    assert result["scanned"] == result["published"] == 1
    assert len(worker.sent) == 2 and worker.sent[-1][1] == [identity, "prepare", None]
    assert worker.sent[-1][2]["task_id"] == dispatch.task_id
    assert m.ClassificationRevision.query.count() == 1 and revision.attempts == 0
    assert revision.state == "pending" and revision.error_category is None
    reconcile()
    assert len(worker.sent) == 2, "successful delivery replay must renew the delivery grace"


@pytest.mark.parametrize("phase", ["prepare", "block", "finalize"])
def test_aged_queued_retry_replays_without_consuming_fifth_attempt(worker, phase):
    identity = worker.env.service.ensure_revision(worker.env.item.id, "backfill")
    if phase != "prepare":
        task().run(identity, "prepare")
    if phase == "finalize":
        task().run(identity, "block", 1)
    revision = db.session.get(m.ClassificationRevision, identity)
    revision.state, revision.attempts, revision.error_category = "retrying", 4, "storage_transient"
    revision.heartbeat_at = revision.updated_at = m.utc_now() - timedelta(days=7)
    if phase == "block":
        block = m.ClassificationBlock.query.one()
        block.state, block.error_category = "failed", "storage_transient"
    task_id = revision.celery_task_id
    db.session.commit()
    worker.sent.clear()
    reconcile()
    assert (revision.state, revision.attempts, revision.error_category) == ("retrying", 4, "storage_transient")
    assert len(worker.sent) == 1
    assert worker.sent[-1][1] == [identity, phase, 1 if phase == "block" else None]
    assert worker.sent[-1][2]["task_id"] == task_id
    assert 241 <= worker.sent[-1][2]["countdown"] <= 270
    reconcile()
    assert len(worker.sent) == 1 and revision.attempts == 4


def test_slow_provider_failures_on_one_block_fail_the_revision_on_the_third(worker, monkeypatch):
    monkeypatch.setattr(tasks, "INPUT_FAILURE_MIN_SECONDS", 0)
    identity = worker.env.service.ensure_revision(worker.env.item.id, "backfill")
    task().run(identity, "prepare")
    worker.env.classifier.error = ClassifierError("router_busy", reason="provider_unavailable", advance_key=True)
    for attempt in (1, 2, 3):
        task().run(identity, "block", 1)
        revision = db.session.get(m.ClassificationRevision, identity)
        block = m.ClassificationBlock.query.filter_by(revision_id=identity, ordinal=1).one()
        assert block.provider_failures == attempt
        if attempt < 3:
            assert (revision.state, revision.attempts, block.state) == ("retrying", 0, "pending")
    assert (revision.state, revision.error_category, revision.attempts) == ("failed", "model_unavailable", 1)
    assert (block.state, block.error_category) == ("failed", "model_unavailable")
    prefix = f"podcast.classification:rev-{identity}:block-1:g"
    assert worker.env.classifier.keys == [prefix + "0", prefix + "1", prefix + "2"]


def test_fast_provider_failures_stay_busy_and_are_not_counted(worker):
    identity = worker.env.service.ensure_revision(worker.env.item.id, "backfill")
    task().run(identity, "prepare")
    worker.env.classifier.error = ClassifierError("router_busy", reason="provider_unavailable", advance_key=True)
    for _ in range(5):
        task().run(identity, "block", 1)
    revision = db.session.get(m.ClassificationRevision, identity)
    block = m.ClassificationBlock.query.filter_by(revision_id=identity, ordinal=1).one()
    assert (revision.state, revision.error_category, block.provider_failures) == ("retrying", "router_busy", 0)


@pytest.mark.parametrize("reason", ["rate_limited", "in_progress", "timeout", "transport", "proxy", "no_candidate", "router_failure"])
def test_other_busy_outcomes_never_count_as_provider_failures(worker, monkeypatch, reason):
    monkeypatch.setattr(tasks, "INPUT_FAILURE_MIN_SECONDS", 0)
    identity = worker.env.service.ensure_revision(worker.env.item.id, "backfill")
    task().run(identity, "prepare")
    worker.env.classifier.error = ClassifierError("router_busy", reason=reason)
    for _ in range(4):
        task().run(identity, "block", 1)
    block = m.ClassificationBlock.query.filter_by(revision_id=identity, ordinal=1).one()
    assert block.provider_failures == 0
    assert db.session.get(m.ClassificationRevision, identity).state == "retrying"


def test_a_stale_claims_provider_failure_changes_nothing(worker, monkeypatch):
    monkeypatch.setattr(tasks, "INPUT_FAILURE_MIN_SECONDS", 0)
    identity = worker.env.service.ensure_revision(worker.env.item.id, "backfill")
    task().run(identity, "prepare")
    error = ClassifierError("router_busy", reason="provider_unavailable", advance_key=True)

    def superseded(*args, **kwargs):
        # Another claim takes the block mid-call, so this claim's token no longer matches.
        block = m.ClassificationBlock.query.filter_by(revision_id=identity, ordinal=1).one()
        block.attempts += 1
        db.session.commit()
        raise error

    monkeypatch.setattr(worker.env.classifier, "classify", superseded)
    worker.sent.clear()
    task().run(identity, "block", 1)
    revision = db.session.get(m.ClassificationRevision, identity)
    block = m.ClassificationBlock.query.filter_by(revision_id=identity, ordinal=1).one()
    assert block.provider_failures == 0
    assert revision.state not in {"failed", "retrying"} and block.state == "running" and not worker.sent


def test_a_busy_router_retries_the_same_block_with_doubling_back_off(worker):
    identity = worker.env.service.ensure_revision(worker.env.item.id, "backfill")
    task().run(identity, "prepare")
    worker.sent.clear()
    worker.env.classifier.error = ClassifierError("router_busy", advance_key=False)
    for attempt in range(1, 9):
        task().run(identity, "block", 1)
        revision = db.session.get(m.ClassificationRevision, identity)
        block = m.ClassificationBlock.query.filter_by(revision_id=identity, ordinal=1).one()
        assert (revision.state, revision.attempts, revision.error_category) == ("retrying", 0, "router_busy")
        assert (block.state, block.error_category, block.attempts) == ("pending", "router_busy", attempt)
        options = worker.sent[-1][2]
        assert options["task_id"].endswith("-b1") and options["countdown"] == min(600, 30 * 2 ** (attempt - 1))
    assert [entry[1] for entry in worker.sent] == [[identity, "block", 1]] * 8
    assert set(worker.env.classifier.keys) == {f"podcast.classification:rev-{identity}:block-1:g0"}
    worker.env.classifier.error = None
    task().run(identity, "block", 1)
    task().run(identity, "finalize")
    assert db.session.get(m.ClassificationRevision, identity).state == "completed"


def test_only_stored_outcomes_change_the_block_key_and_only_real_failures_count(worker):
    identity = worker.env.service.ensure_revision(worker.env.item.id, "backfill")
    task().run(identity, "prepare")
    for error in (ClassifierError("router_busy"), ClassifierError("router_busy", advance_key=False),
                  ClassifierError("model_invalid_output"), None):
        worker.env.classifier.error = error
        task().run(identity, "block", 1)
    prefix = f"podcast.classification:rev-{identity}:block-1:g"
    assert worker.env.classifier.keys == [prefix + "0", prefix + "1", prefix + "1", prefix + "2"]
    revision = db.session.get(m.ClassificationRevision, identity)
    assert revision.attempts == 1 and revision.state == "running"


def test_a_key_conflict_retries_under_a_new_key_within_the_attempt_budget(worker):
    # The block's stored request no longer matches (for example CLASSIFIER_TIMEOUT_SECONDS changed
    # while it waited): a new key generation is tried, and each try is a counted failure.
    identity = worker.env.service.ensure_revision(worker.env.item.id, "backfill")
    task().run(identity, "prepare")
    worker.sent.clear()
    worker.env.classifier.error = ClassifierError("model_config", reason="conflict")
    for attempt in range(1, 6):
        task().run(identity, "block", 1)
        revision = db.session.get(m.ClassificationRevision, identity)
        assert (revision.attempts, revision.error_category) == (attempt, "model_config")
        assert revision.state == ("retrying" if attempt < 5 else "failed")
    assert len(worker.sent) == 4 and all(entry[1] == [identity, "block", 1] for entry in worker.sent)
    prefix = f"podcast.classification:rev-{identity}:block-1:g"
    assert worker.env.classifier.keys == [prefix + str(generation) for generation in range(5)]
    task().run(identity, "block", 1)
    assert len(worker.env.classifier.keys) == 5


def test_a_key_conflict_that_then_succeeds_completes_the_revision(worker):
    identity = worker.env.service.ensure_revision(worker.env.item.id, "backfill")
    task().run(identity, "prepare")
    worker.env.classifier.error = ClassifierError("model_config", reason="conflict")
    task().run(identity, "block", 1)
    worker.env.classifier.error = None
    task().run(identity, "block", 1)
    task().run(identity, "finalize")
    revision = db.session.get(m.ClassificationRevision, identity)
    assert (revision.state, revision.attempts) == ("completed", 1)


@pytest.mark.parametrize("legacy,current", [("claude_timeout", "router_busy"),
                                            ("claude_invalid_output", "model_invalid_output")])
@pytest.mark.parametrize("phase", ["prepare", "block"])
def test_aged_claude_era_retry_replays_with_its_current_category(worker, legacy, current, phase):
    identity = worker.env.service.ensure_revision(worker.env.item.id, "backfill")
    if phase == "block":
        task().run(identity, "prepare")
    revision = db.session.get(m.ClassificationRevision, identity)
    revision.state, revision.attempts, revision.error_category = "retrying", 2, legacy
    revision.heartbeat_at = revision.updated_at = m.utc_now() - timedelta(days=7)
    if phase == "block":
        # The block keeps the Claude-era category; the revision's own field is not retryable.
        revision.error_category = "invalid_policy"
        block = m.ClassificationBlock.query.one()
        block.state, block.error_category = "failed", legacy
    db.session.commit()
    worker.sent.clear()
    reconcile()
    assert (revision.state, revision.attempts, revision.error_category) == ("retrying", 2, current)
    assert len(worker.sent) == 1 and worker.sent[-1][2]["countdown"] > 0
