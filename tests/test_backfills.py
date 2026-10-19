from datetime import date
import pytest
import sqlalchemy as sa

from extensions import db
from ingestion_types import DiscoveryPage
from models import BackfillRequest, ContentItem, SourceItem, ProcessingJob, DiscoveryCandidate, DiscoveryRun
from tests.test_discovery_service import store, descriptor, Adapter, service


def request(source, **selector):
    row = BackfillRequest(source_id=source.id, **selector)
    db.session.add(row)
    db.session.commit()
    return row


def test_backfill_date_range_is_inclusive_and_only_accepts_matches(store):
    source = store()
    row = request(source, selector_type="date_range", start_date=date(2026, 1, 3), end_date=date(2026, 1, 5))
    svc = service(Adapter([DiscoveryPage(tuple(descriptor(n) for n in range(8)))]))
    svc.run_backfill(row.id)
    assert {item.external_id for item in ContentItem.query.all()} == {"2", "3", "4"}
    assert (row.items_scanned, row.items_accepted, row.items_queued, row.state) == (8, 3, 3, "completed")
    svc.run_backfill(row.id)
    assert SourceItem.query.count() == 3


def test_backfill_count_selects_newest_and_resumes_at_100(store):
    source = store("youtube_playlist")
    row = request(source, selector_type="count", requested_count=205)
    sent = []
    svc = service(Adapter([DiscoveryPage(tuple(descriptor(n, True) for n in range(230)))]),
                  lambda name, ident, **kw: sent.append((name, ident)))
    svc.run_backfill(row.id)
    assert row.items_accepted == row.items_queued == 100 and row.state == "running"
    assert row.cursor and ("run_backfill", row.id) in sent
    svc.run_backfill(row.id)
    assert row.items_accepted == 200
    svc.run_backfill(row.id)
    assert (row.items_accepted, row.items_scanned, row.state) == (205, 230, "completed")
    assert {int(item.external_id) for item in ContentItem.query.all()} == set(range(25, 230))
    svc.run_backfill(row.id)
    assert ProcessingJob.query.count() == 205


def test_backfill_pages_are_bounded_and_candidates_survive_continuation(store):
    source = store("youtube_channel")
    row = request(source, selector_type="count", requested_count=10)
    adapter = Adapter([DiscoveryPage((descriptor(n, True),), str(n + 1) if n < 6 else None, n == 6) for n in range(7)])
    svc = service(adapter)
    svc.run_backfill(row.id)
    assert adapter.calls == [None, "1", "2", "3", "4"]
    assert row.items_scanned == 5 and row.items_accepted == 0
    svc.run_backfill(row.id)
    assert (row.items_scanned, row.items_accepted, row.state) == (7, 7, "completed")


def test_explicit_video_url_is_resolved_and_canonicalized(store):
    source = store("youtube_channel")
    row = request(source, selector_type="explicit_url", explicit_url="https://youtu.be/abcdefghijk")
    adapter = Adapter([])
    service(adapter).run_backfill(row.id)
    assert adapter.resolved_url == "https://www.youtube.com/watch?v=abcdefghijk"
    assert row.items_accepted == 1 and row.state == "completed"


@pytest.mark.parametrize("selector", [
    {"selector_type": "count", "requested_count": 5001},
    {"selector_type": "date_range", "start_date": date(2026, 1, 2), "end_date": date(2026, 1, 1)},
    {"selector_type": "count", "requested_count": 2, "explicit_url": "https://example.com/x"},
    {"selector_type": "explicit_url", "explicit_url": "https://youtube.com/playlist?list=PLabcdefghijk"},
    {"selector_type": "explicit_url", "explicit_url": "https://youtube.com/watch?v=abcdefghijk&token=secret"},
])
def test_request_validation_before_persistence_and_network(store, selector):
    source = store("youtube_channel")
    svc = service(Adapter([]))
    with pytest.raises(ValueError):
        svc.create_backfill(source.id, **selector)
    assert BackfillRequest.query.count() == 0


def test_cancellation_between_items_preserves_progress_and_can_be_replayed(store):
    source = store()
    row = request(source, selector_type="count", requested_count=20)
    svc = None
    def dispatch(name, ident, **kwargs):
        if name == "process_item":
            svc.cancel_backfill(row.id)
    svc = service(Adapter([DiscoveryPage(tuple(descriptor(n) for n in range(20)))]), dispatch)
    svc.run_backfill(row.id)
    assert row.state == "cancelled" and row.items_accepted == 1
    svc.run_backfill(row.id)
    assert ContentItem.query.count() == 1


def test_old_rss_unavailable_history_is_explicit(store):
    source = store()
    row = request(source, selector_type="date_range", start_date=date(1990, 1, 1), end_date=date(1990, 1, 2))
    service(Adapter([DiscoveryPage((descriptor(1),))])).run_backfill(row.id)
    assert row.state == "completed" and row.items_accepted == 0
    assert row.error_category == "rss_history_limited"


def test_backfill_broker_failure_resumes_without_duplicate_progress(store):
    source = store()
    row = request(source, selector_type="count", requested_count=2)
    attempts = []
    def dispatch(name, ident, **kw):
        if name == "process_item":
            attempts.append(kw["task_id"])
            if len(attempts) == 1:
                raise TimeoutError("secret")
    svc = service(Adapter([DiscoveryPage((descriptor(1), descriptor(2)))]), dispatch)
    svc.run_backfill(row.id)
    svc.run_backfill(row.id)
    assert row.items_accepted == row.items_queued == ProcessingJob.query.count() == 2
    assert attempts.count(attempts[0]) == 2
    assert all(candidate.descriptor is None for candidate in DiscoveryCandidate.query.all())


def test_backfill_bounds_rejected_candidates_and_preserves_resume(store):
    source = store()
    row = request(source, selector_type="date_range", start_date=date(1990, 1, 1), end_date=date(1990, 1, 2))
    sent = []
    svc = service(Adapter([DiscoveryPage(tuple(descriptor(n) for n in range(250)))]),
                  lambda name, ident, **kw: sent.append((name, ident)))
    svc.run_backfill(row.id)
    assert row.state == "running" and row.items_accepted == 0
    assert DiscoveryCandidate.query.filter(DiscoveryCandidate.descriptor.is_not(None)).count() == 150
    assert ("run_backfill", row.id) in sent
    svc.run_backfill(row.id)
    svc.run_backfill(row.id)
    assert row.state == "completed" and row.items_scanned == 250


def test_source_disabled_between_backfill_pages_cancels_request(store):
    source = store("youtube_playlist")
    row = request(source, selector_type="count", requested_count=2)
    class Disable(Adapter):
        def discover_page(self, descriptor, cursor=None):
            page = super().discover_page(descriptor, cursor)
            source.enabled = False
            db.session.commit()
            return page
    svc = service(Disable([DiscoveryPage((descriptor(1, True),), "1", False)]))
    svc.run_backfill(row.id)
    assert row.state == "cancelled" and row.error_category == "source_disabled"


def test_failed_backfill_continuation_publish_can_resume_from_durable_state(store):
    source = store()
    row = request(source, selector_type="count", requested_count=101)
    def fail(name, ident, **kw):
        if name == "run_backfill":
            raise TimeoutError("broker")
    svc = service(Adapter([DiscoveryPage(tuple(descriptor(n) for n in range(101)))]), fail)
    svc.run_backfill(row.id)
    assert row.state == "running" and row.items_accepted == 100
    svc.run_backfill(row.id)
    assert row.state == "completed" and row.items_accepted == 101


def test_cancellation_after_last_accepted_item_is_not_overwritten(store):
    source = store()
    row = request(source, selector_type="count", requested_count=1)
    svc = None
    def dispatch(name, ident, **kw):
        if name == "process_item":
            svc.cancel_backfill(row.id)
    svc = service(Adapter([DiscoveryPage((descriptor(1),))]), dispatch)
    svc.run_backfill(row.id)
    assert row.state == "cancelled" and row.items_accepted == 1


def test_rss_explicit_url_matches_one_available_canonical_item(store):
    source = store()
    row = request(source, selector_type="explicit_url", explicit_url="https://example.com/items/2")
    svc = service(Adapter([DiscoveryPage(tuple(descriptor(n) for n in range(4)))]))
    svc.run_backfill(row.id)
    assert ContentItem.query.one().external_id == "2"
    assert row.items_accepted == 1 and row.state == "completed"


def test_rss_explicit_url_rejects_ambiguous_canonical_identity(store):
    source = store()
    url = "https://example.com/shared"
    row = request(source, selector_type="explicit_url", explicit_url=url)
    svc = service(Adapter([DiscoveryPage((descriptor(1, canonical_url=url), descriptor(2, canonical_url=url)))]))
    svc.run_backfill(row.id)
    assert row.state == "failed" and ContentItem.query.count() == 0


def test_request_5000_is_allowed_and_committed_before_initial_dispatch(store):
    source = store()
    sent = []
    def dispatch(name, ident, **kw):
        assert name == "run_backfill"
        assert db.session.get(BackfillRequest, ident).requested_count == 5000
        assert not db.session.new
        sent.append(ident)
        raise TimeoutError("broker timeout")
    svc = service(Adapter([]), dispatch)
    ident = svc.create_backfill(source.id, selector_type="count", requested_count=5000)
    assert sent == [ident] and db.session.get(BackfillRequest, ident).state == "pending"


def test_backfill_loads_only_a_bounded_candidate_batch(store):
    source = store()
    row = request(source, selector_type="count", requested_count=250)
    svc = service(Adapter([DiscoveryPage(tuple(descriptor(n) for n in range(250)))]))
    loaded = []
    def record(candidate, context):
        loaded.append(candidate.id)
    sa.event.listen(DiscoveryCandidate, "load", record)
    try:
        svc.run_backfill(row.id)
    finally:
        sa.event.remove(DiscoveryCandidate, "load", record)
    assert len(loaded) <= 101
    assert row.items_accepted == 100 and row.state == "running"


def test_failed_backfill_scan_replays_with_clean_run_status(store):
    source = store("youtube_playlist")
    row = request(source, selector_type="count", requested_count=2)
    adapter = Adapter([DiscoveryPage((descriptor(1, True),), "1", False)])
    svc = service(adapter)
    svc.run_backfill(row.id)
    run = DiscoveryRun.query.one()
    assert row.state == run.state == "failed"
    adapter.pages.append(DiscoveryPage((descriptor(2, True),)))
    svc.run_backfill(row.id)
    assert row.state == run.state == "completed"
    assert run.started_at and run.completed_at and run.error_category is None


def staged_request(source, svc, count=205):
    row = request(source, selector_type="count", requested_count=count)
    row.state = "running"
    run = DiscoveryRun(source_id=source.id, backfill_id=row.id, run_type="backfill", state="finalizing")
    db.session.add(run)
    db.session.flush()
    svc._stage(run, tuple(descriptor(n) for n in range(count)))
    row.items_scanned = count
    db.session.commit()
    return row, run


def test_205_item_interleaving_does_not_complete_at_101_and_clear_remaining(store, monkeypatch):
    source = store()
    svc = service(Adapter([]))
    row, run = staged_request(source, svc)
    original = svc._ordered_candidates
    interleaved = False
    def paused_snapshot(active_run, limit=None):
        nonlocal interleaved
        snapshot = original(active_run, limit)
        if not interleaved:
            interleaved = True
            # Reproduce B accepting the first 100 while A holds a stale batch.
            for candidate in original(active_run, 100):
                svc._accept(source, candidate)
                row.items_accepted += 1
                row.items_queued += 1
                db.session.commit()
        return snapshot
    monkeypatch.setattr(svc, "_ordered_candidates", paused_snapshot)
    svc._finalize_backfill(run, row)
    remaining = DiscoveryCandidate.query.filter(DiscoveryCandidate.descriptor.is_not(None)).count()
    assert row.state == "running" and remaining > 0
    assert row.items_accepted == 200 and remaining == 5
    assert row.items_accepted + remaining == 205
    svc.run_backfill(row.id)
    assert row.state == "completed" and row.items_accepted == ContentItem.query.count() == 205


def test_request_for_update_lock_covers_candidate_reads_and_completion(store):
    from sqlalchemy.dialects import postgresql
    source = store()
    svc = service(Adapter([]))
    row, run = staged_request(source, svc, 2)
    locked = False
    locks = []
    session = db.session()
    def committed(session):
        nonlocal locked
        if not session.in_nested_transaction():
            locked = False
    def before_read(context):
        nonlocal locked
        if not context.is_select:
            return
        statement = context.statement
        entities = {value.get("entity") for value in statement.column_descriptions}
        if BackfillRequest in entities and statement._for_update_arg is not None:
            locked = True
            locks.append(str(statement.compile(dialect=postgresql.dialect())))
        if DiscoveryCandidate in entities:
            assert locked, "candidate selection/remaining-work reads require the request FOR UPDATE lock"
    def before_flush(session, context, instances):
        if any(isinstance(value, (BackfillRequest, ContentItem, SourceItem)) for value in session.dirty | session.new):
            assert locked, "acceptance/progress/completion must commit while holding the request lock"
    sa.event.listen(session, "do_orm_execute", before_read)
    sa.event.listen(session, "after_commit", committed)
    sa.event.listen(session, "before_flush", before_flush)
    try:
        svc._finalize_backfill(run, row)
    finally:
        sa.event.remove(session, "do_orm_execute", before_read)
        sa.event.remove(session, "after_commit", committed)
        sa.event.remove(session, "before_flush", before_flush)
    assert locks and all("FOR UPDATE" in sql for sql in locks)
    assert row.state == "completed" and row.items_accepted == 2


def test_empty_local_candidate_batch_cannot_clear_known_remaining_work(store, monkeypatch):
    source = store()
    sent = []
    svc = service(Adapter([]), lambda name, ident, **kw: sent.append((name, ident)))
    row, run = staged_request(source, svc, 205)
    monkeypatch.setattr(svc, "_ordered_candidates", lambda active_run, limit=None: [])
    svc._finalize_backfill(run, row)
    assert row.state == "running" and row.items_accepted == 0
    assert DiscoveryCandidate.query.filter(DiscoveryCandidate.descriptor.is_not(None)).count() == 205
    assert sent == [("run_backfill", row.id)]


def test_backfill_cannot_finalize_before_provider_pagination_exhausts(store):
    source = store()
    sent = []
    svc = service(Adapter([]), lambda name, ident, **kw: sent.append((name, ident)))
    row, run = staged_request(source, svc, 2)
    run.state, run.cursor = "scanning", "next-page"
    db.session.commit()
    svc._finalize_backfill(run, row)
    assert row.state == "running" and row.items_accepted == 0
    assert DiscoveryCandidate.query.filter(DiscoveryCandidate.descriptor.is_not(None)).count() == 2
    assert sent == [("run_backfill", row.id)]


@pytest.mark.parametrize("kind,identity,channel,member,accepted", [
    ("youtube_video", "abcdefghijk", "channel", True, True),
    ("youtube_video", "zyxwvutsrqp", "channel", True, False),
    ("youtube_channel", "channel", "channel", True, True),
    ("youtube_channel", "channel", "other", True, False),
    ("youtube_playlist", "selected-playlist", "channel", True, True),
    ("youtube_playlist", "selected-playlist", "channel", False, False),
])
def test_explicit_youtube_video_must_belong_to_selected_source(store, kind, identity, channel, member, accepted):
    source = store(kind, identity)
    row = request(source, selector_type="explicit_url", explicit_url="https://youtu.be/abcdefghijk")
    adapter = Adapter([])
    adapter.channel_id, adapter.contains_video = channel, member
    service(adapter).run_backfill(row.id)
    assert row.state == ("completed" if accepted else "failed")
    assert ContentItem.query.count() == SourceItem.query.count() == int(accepted)
    assert DiscoveryCandidate.query.count() == int(accepted)
    if kind == "youtube_playlist":
        assert adapter.membership_lookup == (identity, "abcdefghijk")


def test_podcast_explicit_url_outside_selected_feed_cannot_be_attached(store):
    source = store()
    row = request(source, selector_type="explicit_url", explicit_url="https://another-feed.example/episode")
    service(Adapter([DiscoveryPage((descriptor(1),))])).run_backfill(row.id)
    assert row.state == "completed" and row.items_accepted == SourceItem.query.count() == 0


def test_disabled_completed_backfill_does_not_publish_pending_jobs(store):
    source = store()
    row = request(source, selector_type="count", requested_count=1)
    svc = service(Adapter([DiscoveryPage((descriptor(1),))]), lambda *a, **k: (_ for _ in ()).throw(TimeoutError()))
    svc.run_backfill(row.id)
    source.enabled = False
    db.session.commit()
    sent = []
    svc.dispatch = lambda name, ident, **kw: sent.append((name, ident))
    svc.run_backfill(row.id)
    assert row.state == "completed" and sent == []
    assert ProcessingJob.query.one().error_category == "dispatch_pending"


@pytest.mark.parametrize("terminal", ["completed", "cancelled"])
def test_delayed_failure_after_rollback_cannot_overwrite_other_worker_terminal_state(store, monkeypatch, terminal):
    source = store()
    svc = service(Adapter([]))
    row, run = staged_request(source, svc, 2)
    run_id, request_id = run.id, row.id
    # A has changes that must be discarded when its operation fails.
    row.cursor, run.error_category = "uncommitted-A", "uncommitted-secret"
    original_rollback = db.session.rollback
    interleaved = False
    expected = {}
    def state():
        return (row.state, row.items_accepted, row.items_queued, row.cursor, row.error_category, row.completed_at,
                run.state, run.items_accepted, run.items_queued, run.error_category, run.completed_at, source.error_category)
    def b_finishes_after_a_rolls_back():
        nonlocal interleaved
        original_rollback()
        if interleaved:
            return
        interleaved = True
        if terminal == "completed":
            svc._finalize_backfill(run, row)
        else:
            svc._accept(source, svc._ordered_candidates(run, 1)[0])
            row.items_accepted = row.items_queued = run.items_accepted = run.items_queued = 1
            db.session.commit()
            svc.cancel_backfill(request_id)
        expected["state"] = state()
    monkeypatch.setattr(db.session, "rollback", b_finishes_after_a_rolls_back)
    svc._fail(run_id, request_id)
    assert interleaved and row.state == run.state == terminal
    assert state() == expected["state"]
    assert BackfillRequest.query.count() == DiscoveryRun.query.count() == 1


def test_failure_lock_order_matches_request_run_source_and_never_autoflushes_early(store):
    from models import ContentSource
    from sqlalchemy.dialects import postgresql
    source = store()
    svc = service(Adapter([]))
    row, run = staged_request(source, svc, 2)
    session = db.session()
    locks, sql = [], []
    def before_read(context):
        if context.is_select and context.statement._for_update_arg is not None:
            entity = context.statement.column_descriptions[0].get("entity")
            locks.append(entity)
            sql.append(str(context.statement.compile(dialect=postgresql.dialect())))
    def before_flush(session, context, instances):
        if any(isinstance(value, (DiscoveryRun, ContentSource)) for value in session.dirty):
            assert locks[:2] == [BackfillRequest, DiscoveryRun], "failure writes must not precede request/run locks"
    sa.event.listen(session, "do_orm_execute", before_read)
    sa.event.listen(session, "before_flush", before_flush)
    try:
        svc._fail(run.id, row.id)
    finally:
        sa.event.remove(session, "do_orm_execute", before_read)
        sa.event.remove(session, "before_flush", before_flush)
    assert locks == [BackfillRequest, DiscoveryRun, ContentSource]
    assert all("FOR UPDATE" in value for value in sql)
    assert row.state == run.state == "failed"
    assert row.error_category == run.error_category == source.error_category == "discovery_failed"
    assert db.session.query(BackfillRequest).count() == 1


@pytest.mark.parametrize("terminal", ["completed", "cancelled"])
def test_conditional_failure_updates_guard_terminal_state_when_sqlite_ignores_row_locks(store, monkeypatch, terminal):
    from models import ContentSource, utc_now
    source = store()
    svc = service(Adapter([]))
    row, run = staged_request(source, svc, 2)
    request_id, run_id = row.id, run.id
    original_locked = svc._locked
    interleaved = False
    def b_commits_after_a_reads_active_rows(model, identity):
        nonlocal interleaved
        value = original_locked(model, identity)
        if model is ContentSource and not interleaved:
            interleaved = True
            with db.engine.begin() as connection:
                connection.execute(sa.update(BackfillRequest).where(BackfillRequest.id == request_id).values(
                    state=terminal, items_accepted=2, completed_at=utc_now()))
                connection.execute(sa.update(DiscoveryRun).where(DiscoveryRun.id == run_id).values(
                    state=terminal, items_accepted=2, completed_at=utc_now()))
        return value
    monkeypatch.setattr(svc, "_locked", b_commits_after_a_reads_active_rows)
    svc._fail(run_id, request_id)
    db.session.expire_all()
    assert interleaved and row.state == run.state == terminal
    assert row.items_accepted == run.items_accepted == 2
    assert row.error_category is run.error_category is source.error_category is None


def test_backfill_failure_without_explicit_request_id_still_locks_request_first(store, monkeypatch):
    source = store()
    svc = service(Adapter([]))
    row, run = staged_request(source, svc, 2)
    locked = []
    original = svc._locked
    def record(model, identity):
        locked.append(model)
        return original(model, identity)
    monkeypatch.setattr(svc, "_locked", record)
    svc._fail(run.id)
    assert locked[:2] == [BackfillRequest, DiscoveryRun]
    assert row.state == run.state == "failed"


@pytest.mark.parametrize("boundary", ["lock", "commit"])
def test_failure_recording_error_rolls_back_and_leaves_session_usable_without_secrets(store, monkeypatch, boundary):
    source = store()
    svc = service(Adapter([]))
    row, run = staged_request(source, svc, 2)
    if boundary == "lock":
        original = svc._locked
        def error(model, identity):
            original(model, identity)
            raise RuntimeError("secret-provider-token")
        monkeypatch.setattr(svc, "_locked", error)
    else:
        monkeypatch.setattr(db.session, "commit", lambda: (_ for _ in ()).throw(RuntimeError("secret-provider-token")))
    assert svc._fail(run.id, row.id) is False
    assert not db.session().in_transaction()
    assert row.state == "running" and run.state == "finalizing"
    assert row.error_category is run.error_category is source.error_category is None
    assert BackfillRequest.query.count() == 1


def test_concurrent_failure_and_finalizer_finish_and_preserve_recoverable_work(app_factory, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from models import Admin, ContentSource
    app = app_factory(SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'failure-finalizer.sqlite'}",
                      SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"timeout": 2}})
    page = DiscoveryPage((descriptor(0), descriptor(1)))
    with app.app_context():
        db.create_all()
        owner = Admin(email="concurrent@example.com", password_hash="hash")
        db.session.add(owner)
        db.session.flush()
        source = ContentSource(admin_id=owner.id, source_type="podcast_rss", external_id="concurrent",
                               canonical_url="https://example.com/concurrent", name="Concurrent")
        db.session.add(source)
        db.session.commit()
        row, run = staged_request(source, service(Adapter([page])), 2)
        request_id, run_id = row.id, run.id
    start = Barrier(2)
    def worker(fail):
        with app.app_context():
            svc = service(Adapter([page]))
            order = []
            original = svc._locked
            def record(model, identity):
                order.append(model)
                return original(model, identity)
            svc._locked = record
            start.wait(timeout=5)
            if fail:
                svc._fail(run_id, request_id)
            else:
                svc.run_backfill(request_id)
            assert order[0] is BackfillRequest
            if DiscoveryRun in order:
                assert order.index(BackfillRequest) < order.index(DiscoveryRun)
            # A handler returning after a failed recording still leaves a usable session.
            assert db.session.query(BackfillRequest).count() == 1
            return order
    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.submit(worker, True), executor.submit(worker, False)
        first.result(timeout=10)
        second.result(timeout=10)
    with app.app_context():
        svc = service(Adapter([page]))
        svc.run_backfill(request_id)
        row, run = db.session.get(BackfillRequest, request_id), db.session.get(DiscoveryRun, run_id)
        assert row.state == run.state == "completed"
        assert row.items_accepted == run.items_accepted == ContentItem.query.count() == 2
        assert SourceItem.query.count() == ProcessingJob.query.count() == 2
        db.session.remove()
        db.drop_all()


@pytest.mark.parametrize("terminal", ["completed", "cancelled", "failed"])
@pytest.mark.parametrize("cancel_requested", [False, True])
def test_cancel_service_preserves_every_terminal_request_and_returns_locked_outcome(store, terminal, cancel_requested):
    from models import utc_now
    source = store()
    row = request(source, selector_type="count", requested_count=2)
    row.state, row.completed_at, row.error_category = terminal, utc_now(), "preserved"
    row.cancel_requested = cancel_requested
    db.session.commit()
    before = (row.state, row.cancel_requested, row.completed_at, row.error_category)
    outcome = service(Adapter([])).cancel_backfill(row.id)
    assert outcome == {"id": row.id, "state": terminal, "cancel_requested": cancel_requested, "changed": False}
    assert (row.state, row.cancel_requested, row.completed_at, row.error_category) == before


@pytest.mark.parametrize("terminal", ["completed", "cancelled", "failed"])
def test_cancel_finalization_guards_terminal_commit_when_sqlite_ignores_row_locks(store, monkeypatch, terminal):
    from models import ContentSource, utc_now
    source = store()
    svc = service(Adapter([]))
    row, run = staged_request(source, svc, 2)
    row.cancel_requested = True
    db.session.commit()
    request_id, run_id = row.id, run.id
    original = svc._locked
    interleaved = False
    completed_at = utc_now()
    def worker_finishes_after_cancel_reads_active_rows(model, identity):
        nonlocal interleaved
        value = original(model, identity)
        if model is ContentSource and not interleaved:
            interleaved = True
            with db.engine.begin() as connection:
                connection.execute(sa.update(BackfillRequest).where(BackfillRequest.id == request_id).values(
                    state=terminal, completed_at=completed_at, error_category="preserved"))
                connection.execute(sa.update(DiscoveryRun).where(DiscoveryRun.id == run_id).values(
                    state=terminal, completed_at=completed_at, error_category="preserved"))
        return value
    monkeypatch.setattr(svc, "_locked", worker_finishes_after_cancel_reads_active_rows)
    svc._cancelled(row)
    db.session.expire_all()
    assert interleaved and row.state == run.state == terminal
    assert row.error_category == run.error_category == "preserved"
    assert row.completed_at == run.completed_at == completed_at


def test_cancel_finalization_uses_request_run_source_lock_order_before_writes(store):
    from models import ContentSource
    source = store()
    svc = service(Adapter([]))
    row, run = staged_request(source, svc, 2)
    row.cancel_requested = True
    db.session.commit()
    original, locks = svc._locked, []
    def record(model, identity):
        locks.append(model)
        return original(model, identity)
    svc._locked = record
    svc._cancelled(row)
    assert locks == [BackfillRequest, DiscoveryRun, ContentSource]
    assert row.state == run.state == "cancelled"


@pytest.mark.parametrize("terminal", ["completed", "cancelled", "failed"])
def test_cancel_flag_guard_preserves_terminal_commit_after_locked_read(store, monkeypatch, terminal):
    source = store()
    svc = service(Adapter([]))
    row = request(source, selector_type="count", requested_count=2)
    request_id = row.id
    original = svc._locked
    interleaved = False
    def worker_finishes_after_cancel_observes_pending(model, identity):
        nonlocal interleaved
        value = original(model, identity)
        if model is BackfillRequest and not interleaved:
            interleaved = True
            with db.engine.begin() as connection:
                connection.execute(sa.update(BackfillRequest).where(BackfillRequest.id == request_id).values(state=terminal))
        return value
    monkeypatch.setattr(svc, "_locked", worker_finishes_after_cancel_observes_pending)
    outcome = svc.cancel_backfill(request_id)
    assert interleaved and outcome == {"id": row.id, "state": terminal, "cancel_requested": False, "changed": False}
    assert row.state == terminal and row.cancel_requested is False


@pytest.mark.parametrize("terminal", ["completed", "failed"])
def test_cancellation_finalization_rechecks_terminal_after_flag_commit(store, monkeypatch, terminal):
    source = store()
    svc = service(Adapter([]))
    row, run = staged_request(source, svc, 2)
    original = svc._cancelled
    def worker_finishes_before_finalization(active):
        assert active.cancel_requested and not db.session.dirty
        active.state, run.state = terminal, terminal
        active.error_category, run.error_category = "preserved", "preserved"
        db.session.commit()
        return original(active)
    monkeypatch.setattr(svc, "_cancelled", worker_finishes_before_finalization)
    outcome = svc.cancel_backfill(row.id)
    assert outcome == {"id": row.id, "state": terminal, "cancel_requested": True, "changed": False}
    assert row.state == run.state == terminal
    assert row.error_category == run.error_category == "preserved"


def test_source_disable_cannot_turn_failed_backfill_into_cancelled_during_replay(store):
    source = store()
    svc = service(Adapter([]))
    row, run = staged_request(source, svc, 2)
    row.state = run.state = "failed"
    row.error_category = run.error_category = "preserved"
    source.enabled = False
    db.session.commit()
    svc.run_backfill(row.id)
    assert row.state == run.state == "failed"
    assert row.cancel_requested is False and row.error_category == "preserved"
