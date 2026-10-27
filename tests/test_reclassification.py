"""reclassify-all holds one revision per item, newest first, and the classifier releases them one at a time."""

from datetime import datetime, timedelta, timezone

from contextlib import contextmanager
from dataclasses import replace

import pytest
import sqlalchemy as sa

from extensions import db
import models as m
import tasks
from commercial_policy import POLICY_VERSION
from router_classifier import ClassifierError
from tests.test_classification_service import add_item, finish, override
from tests.test_classification_tasks import env, reconcile, task, worker  # noqa: F401


def invoke(worker, *args):  # noqa: F811
    return worker.app.test_cli_runner().invoke(args=["classification", *args])


def held(item_id):
    return m.ClassificationRevision.query.filter_by(item_id=item_id, state="pending",
                                                    error_category="reclassify_queued").one_or_none()


def published(env, external_id, when):  # noqa: F811
    item, _ = add_item(env, external_id=external_id)
    item.published_at = when
    db.session.commit()
    return item.id


def claude_era(env):  # noqa: F811
    """A completed revision at the current policy version but with Claude-era provenance."""
    revision_id, _ = finish(env)
    db.session.execute(sa.update(m.ClassificationRevision).where(m.ClassificationRevision.id == revision_id).values(
        requested_model="claude-sonnet-4-5", classifier_cli_version="2.0.0 (Claude Code)"))
    db.session.commit()
    revision = db.session.get(m.ClassificationRevision, revision_id)
    assert revision.state == "completed" and revision.policy_version == POLICY_VERSION
    return revision_id


def test_reclassify_all_holds_one_revision_per_item_newest_first_and_releases_one(worker):  # noqa: F811
    env = worker.env
    old = published(env, "old", datetime(2026, 1, 1, tzinfo=timezone.utc))
    newest = published(env, "newest", datetime(2026, 3, 1, tzinfo=timezone.utc))
    middle = published(env, "middle", datetime(2026, 2, 1, tzinfo=timezone.utc))
    result = invoke(worker, "reclassify-all")
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "candidates=4 queued=4 existing=0 busy=0 failed=0 released=1"
    order = [row.item_id for row in m.ClassificationRevision.query.order_by(m.ClassificationRevision.id)]
    assert order == [newest, middle, old, env.item.id]
    first = m.ClassificationRevision.query.filter_by(item_id=newest).one()
    assert first.request_reason == "policy_upgrade" and first.idempotency_key.startswith("reclassify:")
    assert worker.sent == [("tasks.classify_item", [first.id, "prepare", None],
                            {"task_id": first.celery_task_id, "queue": "classification"})]
    assert all(held(item_id) for item_id in (middle, old, env.item.id)) and held(newest) is None


def test_held_revisions_are_released_one_at_a_time_as_each_finishes(worker):  # noqa: F811
    env = worker.env
    second_item = published(env, "second", datetime(2026, 1, 1, tzinfo=timezone.utc))
    env.item.published_at = datetime(2026, 2, 1, tzinfo=timezone.utc)
    db.session.commit()
    assert invoke(worker, "reclassify-all").exit_code == 0
    first = m.ClassificationRevision.query.filter_by(item_id=env.item.id).one()
    second = held(second_item)
    for phase, ordinal in (("prepare", None), ("block", 1)):
        task().run(first.id, phase, ordinal)
        assert held(second_item) is not None
    task().run(first.id, "finalize")
    assert db.session.get(m.ClassificationRevision, first.id).state == "completed"
    assert worker.sent[-1][1] == [second.id, "prepare", None]
    assert held(second_item) is None


def test_the_reconciler_never_publishes_a_held_revision_even_when_it_is_old(worker):  # noqa: F811
    env = worker.env
    published(env, "another", datetime(2026, 1, 1, tzinfo=timezone.utc))
    with worker.app.app_context():
        counts = tasks.queue_reclassification()
    assert counts["queued"] == 2 and not worker.sent
    db.session.execute(sa.update(m.ClassificationRevision).values(
        updated_at=m.utc_now() - timedelta(days=1), created_at=m.utc_now() - timedelta(days=1)))
    db.session.commit()
    reconcile()
    prepares = [entry for entry in worker.sent if entry[0] == "tasks.classify_item"]
    assert len(prepares) == 1
    assert m.ClassificationRevision.query.filter_by(error_category="reclassify_queued").count() == 1


def test_items_already_classified_by_the_router_are_skipped_and_reruns_queue_nothing(worker):  # noqa: F811
    env = worker.env
    finish(env)
    other = published(env, "other", datetime(2026, 1, 1, tzinfo=timezone.utc))
    worker.app.config["CLASSIFICATION_ENABLED"] = False
    assert invoke(worker, "reclassify-all").output.startswith("candidates=1 queued=1 ")
    assert invoke(worker, "reclassify-all").output.startswith("candidates=0 queued=0 ")
    assert m.ClassificationRevision.query.filter_by(item_id=other).count() == 1


def test_status_reports_queued_reclassification(worker):  # noqa: F811
    worker.app.config["CLASSIFICATION_ENABLED"] = False
    published(worker.env, "status", datetime(2026, 1, 1, tzinfo=timezone.utc))
    invoke(worker, "reclassify-all", "--limit", "1")
    assert "reclassify_queued=0" in invoke(worker, "status").output  # The only one queued was released.
    invoke(worker, "reclassify-all")
    assert "reclassify_queued=1" in invoke(worker, "status").output


@pytest.mark.parametrize("limit", ["0", "abc"])
def test_the_limit_is_validated(worker, limit):  # noqa: F811
    result = invoke(worker, "reclassify-all", "--limit", limit)
    assert result.exit_code != 0 and m.ClassificationRevision.query.count() == 0


def test_deploy_does_not_reclassify_claude_era_items_only_reclassify_all_does(worker):  # noqa: F811
    """Guards against mass reclassification on deploy: a completed Claude-era revision at the
    current policy version is current for the reconciler; only reclassify-all replaces it."""
    env = worker.env
    revision_id = claude_era(env)
    with worker.app.app_context():
        counts = tasks.reconcile_classification_batch(create_only=True)
    assert counts["created"] == 0
    reconcile()
    assert not worker.sent
    assert [row.id for row in m.ClassificationRevision.query] == [revision_id]
    result = invoke(worker, "reclassify-all")
    assert result.output.strip() == "candidates=1 queued=1 existing=0 busy=0 failed=0 released=1", result.output
    replacement = m.ClassificationRevision.query.filter(m.ClassificationRevision.id != revision_id).one()
    assert replacement.previous_revision_id == revision_id and replacement.request_reason == "policy_upgrade"
    assert db.session.get(m.ClassificationRevision, revision_id).state == "completed"  # Old revision kept.


def test_held_revisions_are_skipped_by_the_reconciler_and_released_one_at_a_time(worker):  # noqa: F811
    env = worker.env
    others = [published(env, f"held-{number}", datetime(2026, 1, number, tzinfo=timezone.utc)) for number in (1, 2)]
    with worker.app.app_context():
        assert tasks.queue_reclassification()["queued"] == 3
    assert not worker.sent
    # While one released revision is in flight, neither the reconciler nor another release publishes a second.
    reconcile()
    reconcile()
    assert len(worker.sent) == 1
    with worker.app.app_context():
        assert tasks.release_reclassification() is None
        assert tasks.reconcile_classification_batch(create_only=True)["created"] == 0
    assert m.ClassificationRevision.query.filter_by(error_category="reclassify_queued").count() == 2
    seen = 0
    while len(worker.sent) > seen:
        seen = len(worker.sent)
        task().run(*worker.sent[-1][1])
    phases = [(args[0], args[1]) for name, args, _ in worker.sent if name == "tasks.classify_item"]
    finished = [identity for identity, phase in phases if phase == "prepare"]
    # Each released revision reaches finalize before the next one is prepared.
    assert [phase for _, phase in phases] == ["prepare", "block", "finalize"] * 3
    items = [db.session.get(m.ClassificationRevision, identity).item_id for identity in finished]
    assert items == [others[1], others[0], env.item.id]
    assert all(db.session.get(m.ClassificationRevision, identity).state == "completed" for identity in finished)
    assert m.ClassificationRevision.query.filter_by(error_category="reclassify_queued").count() == 0


def run_chain(worker):  # noqa: F811
    """Run the most recently sent classification message until no new one is sent."""
    seen = 0
    while len(worker.sent) > seen:
        seen = len(worker.sent)
        task().run(*worker.sent[-1][1])


def complete_router_revision(env, item_id, key):  # noqa: F811
    revision_id = env.service.ensure_revision(item_id, "manual_retry", idempotency_key=key)
    for ordinal in env.service.prepare_revision(revision_id).block_ordinals:
        env.service.run_block(revision_id, ordinal)
    env.service.finalize_revision(revision_id)
    assert db.session.get(m.ClassificationRevision, revision_id).state == "completed"
    return revision_id


def test_a_failed_reclassification_is_not_requeued_and_limited_reruns_move_on(worker):  # noqa: F811
    env = worker.env
    env.item.published_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db.session.commit()
    newest = published(env, "newest", datetime(2026, 3, 1, tzinfo=timezone.utc))
    assert invoke(worker, "reclassify-all", "--limit", "1").output.strip() == (
        "candidates=1 queued=1 existing=0 busy=0 failed=0 released=1")
    env.classifier.error = ClassifierError("model_config")
    run_chain(worker)
    failed = m.ClassificationRevision.query.filter_by(item_id=newest).one()
    assert failed.state == "failed" and failed.idempotency_key.startswith("reclassify:")
    env.classifier.error = None
    sent = len(worker.sent)
    assert invoke(worker, "reclassify-all", "--limit", "1").output.strip() == (
        "candidates=1 queued=1 existing=0 busy=0 failed=0 released=1")
    assert worker.sent[sent][1][0] == m.ClassificationRevision.query.filter_by(item_id=env.item.id).one().id
    run_chain(worker)
    assert invoke(worker, "reclassify-all").output.strip() == (
        "candidates=0 queued=0 existing=0 busy=0 failed=0 released=0")
    assert m.ClassificationRevision.query.filter_by(item_id=newest).count() == 1


def test_release_continues_after_a_permanent_failure(worker):  # noqa: F811
    env = worker.env
    other = published(env, "other", datetime(2026, 1, 1, tzinfo=timezone.utc))
    env.item.published_at = datetime(2026, 2, 1, tzinfo=timezone.utc)
    db.session.commit()
    assert invoke(worker, "reclassify-all").exit_code == 0
    first = m.ClassificationRevision.query.filter_by(item_id=env.item.id).one()
    second = held(other)
    env.classifier.error = ClassifierError("model_config")
    task().run(first.id, "prepare")
    task().run(first.id, "block", 1)
    assert db.session.get(m.ClassificationRevision, first.id).state == "failed"
    assert worker.sent[-1][1] == [second.id, "prepare", None] and held(other) is None


def test_a_held_revision_superseded_by_a_newer_router_revision_is_not_classified_again(worker):  # noqa: F811
    env = worker.env
    other = published(env, "other", datetime(2026, 1, 1, tzinfo=timezone.utc))
    env.item.published_at = datetime(2026, 2, 1, tzinfo=timezone.utc)
    db.session.commit()
    assert invoke(worker, "reclassify-all").exit_code == 0
    stale = held(other)
    newer = complete_router_revision(env, other, "operator-newer")
    calls = len(env.classifier.calls)
    run_chain(worker)
    assert db.session.get(m.ClassificationRevision, stale.id).state == "superseded"
    assert [row.id for row in m.ClassificationRevision.query.filter_by(item_id=other)
            .order_by(m.ClassificationRevision.id)] == [stale.id, newer]
    assert db.session.get(m.ContentItem, other).active_classification_revision_id == newer
    assert len(env.classifier.calls) == calls + 1  # Only the first item's block; nothing for the stale one.


def test_an_item_classified_while_waiting_for_its_lock_is_not_held(worker, monkeypatch):  # noqa: F811
    env = worker.env
    worker.app.config["CLASSIFICATION_ENABLED"] = False
    original = tasks._item_lock

    @contextmanager
    def racing(item_id):
        complete_router_revision(env, item_id, "racing")
        with original(item_id) as acquired:
            yield acquired
    monkeypatch.setattr(tasks, "_item_lock", racing)
    assert invoke(worker, "reclassify-all").output.strip() == (
        "candidates=1 queued=0 existing=1 busy=0 failed=0 released=0")
    assert m.ClassificationRevision.query.count() == 1


def test_status_reports_revisions_in_flight(worker):  # noqa: F811
    worker.app.config["CLASSIFICATION_ENABLED"] = False
    assert "in_flight=0 oldest_in_flight_updated=none" in invoke(worker, "status").output
    published(worker.env, "flight", datetime(2026, 1, 1, tzinfo=timezone.utc))
    invoke(worker, "reclassify-all")
    output = invoke(worker, "status").output
    assert "in_flight=1 oldest_in_flight_updated=20" in output and "reclassify_queued=1" in output


def test_a_new_model_pin_makes_reclassified_items_candidates_again(worker):  # noqa: F811
    env = worker.env
    assert invoke(worker, "reclassify-all").output.strip() == (
        "candidates=1 queued=1 existing=0 busy=0 failed=0 released=1")
    run_chain(worker)
    first = m.ClassificationRevision.query.one()
    assert first.state == "completed" and first.idempotency_key.startswith("reclassify:")
    assert invoke(worker, "reclassify-all").output.startswith("candidates=0 queued=0 ")
    env.service.settings = env.classifier.settings = replace(env.settings, ROUTER_EXPECTED_MODEL="bonsai2:27b-q4_0")
    assert invoke(worker, "reclassify-all").output.strip() == (
        "candidates=1 queued=1 existing=0 busy=0 failed=0 released=1")
    second = m.ClassificationRevision.query.filter(m.ClassificationRevision.id != first.id).one()
    assert second.idempotency_key.startswith("reclassify:") and second.idempotency_key != first.idempotency_key
    assert second.requested_model == "bonsai2:27b-q4_0"


def test_a_superseded_revision_still_hands_off_when_the_inputs_changed(worker):  # noqa: F811
    env = worker.env
    revision_id = env.service.ensure_revision(env.item.id, "backfill")
    task().run(revision_id, "prepare")
    task().run(revision_id, "block", 1)
    task().run(revision_id, "finalize")
    assert db.session.get(m.ClassificationRevision, revision_id).state == "completed"
    stale = env.service.ensure_revision(env.item.id, "manual_retry", idempotency_key="before-override")
    override(env, 1)  # The active revision keeps the current provenance but no longer matches the inputs.
    sent = len(worker.sent)
    task().run(stale, "prepare")
    assert db.session.get(m.ClassificationRevision, stale).state == "superseded"
    handoff = m.ClassificationRevision.query.filter(m.ClassificationRevision.id > stale).one()
    assert handoff.request_reason == "policy_upgrade" and worker.sent[sent][1] == [handoff.id, "prepare", None]


def test_a_failing_input_check_falls_through_to_the_hand_off(worker, monkeypatch):  # noqa: F811
    from classification_service import ClassificationServiceError
    env = worker.env
    stale = env.service.ensure_revision(env.item.id, "manual_retry", idempotency_key="stale")
    complete_router_revision(env, env.item.id, "newer")

    def failing(item_id):
        raise ClassificationServiceError("storage_transient")
    monkeypatch.setattr(env.service, "active_revision_matches_inputs", failing)
    task().run(stale, "prepare")
    assert db.session.get(m.ClassificationRevision, stale).state == "superseded"
    assert m.ClassificationRevision.query.filter_by(request_reason="policy_upgrade").count() == 1


def test_cancel_supersedes_only_held_revisions_and_requeues_only_under_a_new_provenance(worker):  # noqa: F811
    env = worker.env
    for name in ("a", "b"):
        published(env, name, datetime(2026, 1, 1, tzinfo=timezone.utc))
    active_before = {item.id: item.active_classification_revision_id for item in m.ContentItem.query}
    with worker.app.app_context():
        counts = tasks.queue_reclassification()
        released = tasks.release_reclassification()
        assert counts["queued"] == 3 and released is not None
        assert tasks.cancel_reclassification() == 2
        assert tasks.cancel_reclassification() == 0
    revisions = m.ClassificationRevision.query.all()
    assert m.ClassificationRevision.query.filter_by(state="pending", error_category="reclassify_queued").count() == 0
    survivor = db.session.get(m.ClassificationRevision, released.revision_id)
    assert (survivor.state, survivor.error_category) == ("pending", "dispatch_pending")
    cancelled = [r for r in revisions if r.id != survivor.id]
    assert len(cancelled) == 2 and {(r.state, r.error_category) for r in cancelled} == {("superseded", None)}
    assert all(r.started_at is None and not r.blocks for r in cancelled)
    assert {item.id: item.active_classification_revision_id for item in m.ContentItem.query} == active_before
    # The same provenance finds the cancelled keys again, so nothing is re-queued; a new pin queues all three
    # items anew (the released one too: its revision belongs to the old pin).
    with worker.app.app_context():
        assert tasks.queue_reclassification()["queued"] == 0
    env.service.settings = env.classifier.settings = replace(env.settings, ROUTER_EXPECTED_MODEL="bonsai2:27b-q4_0")
    with worker.app.app_context():
        assert tasks.queue_reclassification()["queued"] == 3
