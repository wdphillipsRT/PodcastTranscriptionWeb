from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import importlib

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Query

from extensions import db
from ingestion_types import DiscoveryPage, ItemDescriptor
from models import Admin, ContentSource, ContentItem, SourceItem, DiscoveryRun, DiscoveryCandidate, ProcessingJob


@pytest.fixture
def store(app, monkeypatch):
    import socket
    def forbidden(*args, **kwargs):
        raise AssertionError("Discovery tests must not contact production services")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    with app.app_context():
        db.create_all()
        owner = Admin(email="discovery@example.com", password_hash="hash")
        db.session.add(owner)
        db.session.flush()
        def source(kind="podcast_rss", identity="feed"):
            row = ContentSource(admin_id=owner.id, source_type=kind, external_id=identity,
                                canonical_url="https://example.com/" + identity, name=identity)
            db.session.add(row)
            db.session.commit()
            return row
        yield source
        db.session.remove()
        db.drop_all()


def descriptor(number, youtube=False, **changes):
    values = dict(source_type="youtube_playlist" if youtube else "podcast_rss", source_external_id="feed",
                  external_id=str(number), title=f"Item {number}",
                  published_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=number),
                  canonical_url=f"https://example.com/items/{number}",
                  platform="youtube" if youtube else "podcast", item_type="youtube_video" if youtube else "podcast_episode",
                  media_url=None if youtube else f"https://example.com/media/{number}.mp3")
    values.update(changes)
    return ItemDescriptor(**values)


class Adapter:
    def __init__(self, pages):
        self.pages, self.calls = pages, []
    def discover_page(self, source, cursor=None):
        self.calls.append(cursor)
        return self.pages[int(cursor or 0)]
    def resolve_video(self, url):
        from source_classifier import youtube_locator
        self.resolved_url = url
        return descriptor(9, True, canonical_url=url, external_id=youtube_locator(url)[2],
                          channel_id=getattr(self, "channel_id", "feed"))
    def source_contains_video(self, source, video_id):
        self.membership_lookup = (source.external_id, video_id)
        return getattr(self, "contains_video", True)


def service(adapter, dispatch=None, **config):
    module = importlib.import_module("discovery_service")
    settings = dict(YOUTUBE_MAX_PAGES_PER_TASK=5, YOUTUBE_MAX_ITEMS_PER_BACKFILL_TASK=100,
                    BACKFILL_REQUEST_MAX_ITEMS=5000, DISCOVERY_INTERVAL_SECONDS=3600)
    settings.update(config)
    return module.DiscoveryService(db.session, SimpleNamespace(**settings), lambda source: adapter,
                                   dispatch or (lambda name, ident, **kwargs: None))


@pytest.mark.parametrize("youtube", [False, True])
def test_initial_newest_25_order_independent_and_replay(store, youtube):
    source = store("youtube_channel" if youtube else "podcast_rss")
    adapter = Adapter([DiscoveryPage(tuple(descriptor(n, youtube) for n in range(35)))])
    sent = []
    svc = service(adapter, lambda name, ident, **kw: sent.append((name, ident, kw)))
    run_id = svc.discover_source(source.id)
    assert {int(item.external_id) for item in ContentItem.query.all()} == set(range(10, 35))
    assert SourceItem.query.count() == ProcessingJob.query.count() == 25
    svc.continue_discovery(run_id)
    assert len([message for message in sent if message[0] == "process_item"]) == 25
    assert all(candidate.descriptor is None for candidate in DiscoveryCandidate.query.all())


def test_playlist_stages_five_pages_then_finalizes_globally(store):
    source = store("youtube_playlist")
    pages = [DiscoveryPage(tuple(descriptor(n, True) for n in range(i * 10, i * 10 + 10)),
                           next_cursor=str(i + 1) if i < 5 else None, exhausted=i == 5) for i in range(6)]
    adapter, sent = Adapter(pages), []
    svc = service(adapter, lambda name, ident, **kw: sent.append((name, ident)))
    run_id = svc.discover_source(source.id)
    assert len(adapter.calls) == 5
    assert DiscoveryCandidate.query.count() == 50
    assert ContentItem.query.count() == 0
    assert sent == [("continue_discovery", run_id)]
    svc.continue_discovery(run_id)
    assert {int(row.external_id) for row in ContentItem.query.all()} == set(range(35, 60))
    assert db.session.get(DiscoveryRun, run_id).state == "completed"


def test_scheduled_scan_adds_unseen_memberships_and_one_canonical_dispatch(store):
    first, second = store("youtube_playlist", "one"), store("youtube_channel", "two")
    adapter = Adapter([DiscoveryPage((descriptor(1, True), descriptor(2, True)))])
    sent = []
    svc = service(adapter, lambda name, ident, **kw: sent.append((name, ident)))
    svc.discover_source(first.id)
    svc.discover_source(second.id)
    adapter.pages = [DiscoveryPage((descriptor(1, True),), "1", False), DiscoveryPage((descriptor(3, True),))]
    svc.discover_source(first.id, "scheduled")
    assert ContentItem.query.count() == ProcessingJob.query.count() == 3
    assert SourceItem.query.count() == 5
    assert len([message for message in sent if message[0] == "process_item"]) == 3


def test_commit_precedes_dispatch_and_timeout_replays_same_durable_id(store):
    source = store()
    attempts = []
    def dispatch(name, ident, **kwargs):
        if name != "process_item":
            return
        job = ProcessingJob.query.filter_by(item_id=ident).one()
        with db.engine.connect() as connection:
            assert connection.execute(sa.select(ProcessingJob.celery_task_id)).scalar_one() == kwargs["task_id"]
            assert connection.execute(sa.select(sa.func.count()).select_from(ContentItem)).scalar_one() == 1
        attempts.append(kwargs["task_id"])
        if len(attempts) == 1:
            raise TimeoutError("provider secret: token=unsafe")
    svc = service(Adapter([DiscoveryPage((descriptor(1),))]), dispatch)
    run_id = svc.discover_source(source.id)
    assert ProcessingJob.query.one().error_category == "dispatch_pending"
    svc.continue_discovery(run_id)
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert ContentItem.query.count() == SourceItem.query.count() == ProcessingJob.query.count() == 1


def test_fast_worker_changes_survive_dispatch_return(store):
    source = store()
    def dispatch(name, ident, **kwargs):
        if name == "process_item":
            job = ProcessingJob.query.filter_by(item_id=ident).one()
            job.state, job.attempts, job.error_category = "running", 1, None
            db.session.commit()
    service(Adapter([DiscoveryPage((descriptor(1),))]), dispatch).discover_source(source.id)
    job = ProcessingJob.query.one()
    assert (job.state, job.attempts, job.error_category) == ("running", 1, None)


def test_disabled_skips_and_check_all_failures_are_independent(store):
    disabled, bad, good = store(identity="disabled"), store(identity="bad"), store(identity="good")
    disabled.enabled = False
    db.session.commit()
    sent = []
    def dispatch(name, ident, **kwargs):
        sent.append(ident)
        if ident == bad.id:
            raise RuntimeError("secret credential")
    svc = service(Adapter([]), dispatch)
    assert svc.discover_source(disabled.id) is None
    svc.check_all_sources()
    assert sent == [bad.id, good.id]
    assert bad.error_category == "dispatch_failed"
    assert DiscoveryRun.query.filter_by(source_id=bad.id, state="pending").count() == 1


def test_provider_failure_is_redacted_and_resume_keeps_staged_pages(store):
    source = store("youtube_playlist")
    adapter = Adapter([DiscoveryPage((descriptor(1, True),), "1", False)])
    svc = service(adapter)
    run_id = svc.discover_source(source.id)
    run = db.session.get(DiscoveryRun, run_id)
    assert run.state == "failed" and run.error_category == "discovery_failed"
    assert "secret" not in str(run.error_category)
    assert DiscoveryCandidate.query.count() == 1
    adapter.pages.append(DiscoveryPage((descriptor(2, True),)))
    svc.continue_discovery(run_id)
    assert run.state == "completed" and ContentItem.query.count() == 2


def test_candidate_projection_omits_raw_metadata_and_transient_reacquisition(store):
    source = store("youtube_playlist")
    item = descriptor(1, True, requires_reacquisition=True, provider_metadata={"raw": "provider response"})
    adapter = Adapter([DiscoveryPage((item,), "1", False)])
    svc = service(adapter, YOUTUBE_MAX_PAGES_PER_TASK=1)
    svc.discover_source(source.id)
    candidate = DiscoveryCandidate.query.one()
    assert candidate.descriptor["requires_reacquisition"] is True
    assert "provider_metadata" not in candidate.descriptor
    assert "media_url" not in candidate.descriptor


def test_uniqueness_conflicts_requery_winner_without_rolling_back_work(store, monkeypatch):
    source = store()
    svc = service(Adapter([DiscoveryPage((descriptor(1),))]))
    svc.discover_source(source.id)
    item = ContentItem.query.one()
    original_first = Query.first
    missed = {ContentItem, SourceItem, ProcessingJob}
    def miss_once(query):
        model = query.column_descriptions[0]["entity"]
        if model in missed:
            missed.remove(model)
            return None
        return original_first(query)
    monkeypatch.setattr(Query, "first", miss_once)
    for model, identity, values in (
        (ContentItem, dict(platform=item.platform, external_id=item.external_id),
         dict(admin_id=source.admin_id, canonical_url=item.canonical_url, title="Duplicate", item_type=item.item_type)),
        (SourceItem, dict(source_id=source.id, item_id=item.id), {}),
        (ProcessingJob, dict(item_id=item.id, stage="podcast_audio"), {}),
    ):
        winner, created = svc._unique(model, identity, values)
        assert winner.id and not created
    db.session.commit()
    assert ContentItem.query.count() == SourceItem.query.count() == ProcessingJob.query.count() == 1


def test_failed_continuation_publish_is_recovered_by_hourly_scan(store):
    source = store("youtube_playlist")
    adapter = Adapter([DiscoveryPage((descriptor(1, True),), "1", False), DiscoveryPage((descriptor(2, True),))])
    attempted = []
    def failed(name, ident, **kwargs):
        attempted.append((name, ident))
        raise TimeoutError("unknown acknowledgement")
    svc = service(adapter, failed, YOUTUBE_MAX_PAGES_PER_TASK=1)
    run_id = svc.discover_source(source.id)
    assert db.session.get(DiscoveryRun, run_id).cursor == "1"
    assert DiscoveryCandidate.query.count() == 1
    svc.dispatch = lambda name, ident, **kw: attempted.append((name, ident))
    svc.check_all_sources()
    svc.discover_source(source.id)
    assert db.session.get(DiscoveryRun, run_id).state == "completed"
    assert DiscoveryRun.query.count() == 1 and ContentItem.query.count() == 2


def test_invalid_cursor_is_rejected_before_advancing_durable_checkpoint(store):
    source = store("youtube_playlist")
    adapter = Adapter([DiscoveryPage((descriptor(1, True),), "x" * 4097, False)])
    run_id = service(adapter).discover_source(source.id)
    run = db.session.get(DiscoveryRun, run_id)
    assert run.state == "failed" and run.cursor is None
    assert run.items_scanned == DiscoveryCandidate.query.count() == 0


def test_check_all_includes_each_source_when_a_service_operation_fails(store, monkeypatch):
    first, second = store(identity="one"), store(identity="two")
    svc = service(Adapter([]))
    original = svc._pending_run
    calls = []
    def fail_first(source, run_type):
        calls.append(source.id)
        if source.id == first.id:
            raise RuntimeError("database query failed")
        return original(source, run_type)
    monkeypatch.setattr(svc, "_pending_run", fail_first)
    svc.check_all_sources()
    assert calls == [first.id, second.id]
    assert DiscoveryRun.query.filter_by(source_id=second.id).count() == 1


def test_staging_page_failure_rolls_back_all_candidates(store):
    source = store("youtube_playlist")
    unsafe = descriptor(2, True, duration_seconds=-1)
    adapter = Adapter([DiscoveryPage((descriptor(1, True), unsafe))])
    run_id = service(adapter).discover_source(source.id)
    assert db.session.get(DiscoveryRun, run_id).state == "failed"
    assert DiscoveryCandidate.query.count() == 0


@pytest.mark.parametrize("mode", ["global", "source", "completed_run"])
def test_disabled_only_pending_jobs_are_not_replayed(store, mode):
    source = store()
    svc = service(Adapter([DiscoveryPage((descriptor(1),))]), lambda *a, **k: (_ for _ in ()).throw(TimeoutError()))
    run_id = svc.discover_source(source.id)
    source.enabled = False
    db.session.commit()
    sent = []
    svc.dispatch = lambda name, ident, **kw: sent.append((name, ident))
    if mode == "completed_run":
        svc.continue_discovery(run_id)
    else:
        svc.replay_pending_dispatches(source.id if mode == "source" else None)
    assert sent == []
    assert ProcessingJob.query.one().error_category == "dispatch_pending"
    source.enabled = True
    db.session.commit()
    svc.replay_pending_dispatches()
    assert sent == [("process_item", ContentItem.query.one().id)]
    assert ProcessingJob.query.one().error_category is None


def test_pending_job_rechecks_enabled_membership_at_send_boundary(store, monkeypatch):
    source = store()
    svc = service(Adapter([DiscoveryPage((descriptor(1),))]))
    original = svc._send
    def disable_then_send(name, ident, **kw):
        if name == "process_item":
            source.enabled = False
            db.session.commit()
        return original(name, ident, **kw)
    sent = []
    svc.dispatch = lambda name, ident, **kw: sent.append((name, ident))
    monkeypatch.setattr(svc, "_send", disable_then_send)
    svc.discover_source(source.id)
    assert sent == [] and ProcessingJob.query.one().error_category == "dispatch_pending"


def test_disabling_source_after_first_send_leaves_remaining_jobs_pending(store):
    source = store()
    sent = []
    def dispatch(name, ident, **kw):
        if name == "process_item":
            sent.append(ident)
            source.enabled = False
            db.session.commit()
    svc = service(Adapter([DiscoveryPage(tuple(descriptor(n) for n in range(3)))]), dispatch)
    svc.discover_source(source.id)
    assert len(sent) == 1
    assert ProcessingJob.query.filter_by(error_category="dispatch_pending").count() == 2


def test_shared_pending_item_can_dispatch_via_another_enabled_membership(store):
    disabled, enabled = store(identity="disabled"), store(identity="enabled")
    svc = service(Adapter([DiscoveryPage((descriptor(1),))]), lambda *a, **k: (_ for _ in ()).throw(TimeoutError()))
    svc.discover_source(disabled.id)
    disabled.enabled = False
    db.session.add(SourceItem(source_id=enabled.id, item_id=ContentItem.query.one().id))
    db.session.commit()
    sent = []
    svc.dispatch = lambda name, ident, **kw: sent.append((name, ident))
    svc.replay_pending_dispatches(disabled.id)
    assert sent == [("process_item", ContentItem.query.one().id)]
    assert ProcessingJob.query.one().error_category is None
