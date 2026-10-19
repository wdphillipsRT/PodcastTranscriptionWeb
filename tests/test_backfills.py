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
