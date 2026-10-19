"""Private workspace behavior; provider and broker boundaries are synthetic."""
from datetime import date
from types import SimpleNamespace

import pytest

from extensions import db
from ingestion_types import SourceDescriptor
from models import Admin, BackfillRequest, ContentItem, ContentSource, DiscoveryRun, ProcessingJob, SourceItem


@pytest.fixture
def workspace(app_factory):
    app = app_factory()
    with app.app_context():
        db.create_all()
        admin = Admin(email="owner@example.com", password_hash="unused")
        db.session.add(admin)
        db.session.flush()
        source = ContentSource(admin_id=admin.id, source_type="youtube_channel", external_id="UC" + "a" * 22,
            canonical_url="https://www.youtube.com/channel/UC" + "a" * 22, name="Example channel")
        db.session.add(source)
        db.session.flush()
        item = ContentItem(admin_id=admin.id, item_type="youtube_video", platform="youtube", external_id="abcdefghijk",
            canonical_url="https://www.youtube.com/watch?v=abcdefghijk", title="Example item", status="failed",
            transcript_origin="creator_caption", transcription_provider="yt-dlp", language="en",
            transcript_s3_key="items/youtube/abcdefghijk/transcripts/private.txt", media_s3_key="items/youtube/abcdefghijk/audio/private.mp3",
            error_category="secret command output /tmp/private key=secret")
        db.session.add(item)
        db.session.flush()
        db.session.add(SourceItem(source_id=source.id, item_id=item.id))
        db.session.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"], session["_fresh"] = "1", True
    yield SimpleNamespace(app=app, client=client)
    with app.app_context():
        db.session.remove()
        db.drop_all()


URLS = [
    ("https://podcasts.apple.com/us/podcast/example/id123", "podcast_rss"),
    ("https://example.com/feed.xml", "podcast_rss"),
    ("https://www.youtube.com/channel/UC" + "a" * 22, "youtube_channel"),
    ("https://youtube.com/@example", "youtube_channel"),
    ("https://youtube.com/user/example", "youtube_channel"),
    ("https://youtube.com/playlist?list=PLabcdefghijk", "youtube_playlist"),
    ("https://youtube.com/watch?v=abcdefghijk", "youtube_video"),
    ("https://youtu.be/abcdefghijk", "youtube_video"),
    ("https://youtube.com/shorts/abcdefghijk", "youtube_video"),
]


@pytest.mark.parametrize("url,kind", URLS)
def test_add_classifies_resolves_and_commits_before_dispatch(workspace, monkeypatch, url, kind):
    from web import routes
    import tasks
    calls = []
    canonical = "https://example.com/canonical" if kind == "podcast_rss" else {
        "youtube_channel": "https://www.youtube.com/channel/UC" + "b" * 22,
        "youtube_playlist": "https://www.youtube.com/playlist?list=PLabcdefghijk",
        "youtube_video": "https://www.youtube.com/watch?v=abcdefghijk"}[kind]
    descriptor = SourceDescriptor(kind, "canonical-identity", canonical, "Canonical title")
    def resolve(value, settings):
        calls.append(value)
        return descriptor
    monkeypatch.setattr(routes, "resolve_source", resolve)
    def dispatch(name, identity, **options):
        assert name == "discover_source"
        assert not db.session.new and not db.session.dirty
        assert db.session.get(ContentSource, identity).canonical_url == canonical
        assert DiscoveryRun.query.filter_by(source_id=identity, state="pending").one()
    monkeypatch.setattr(tasks, "dispatch_task", dispatch)
    response = workspace.client.post("/api/sources", json={"url": url})
    assert response.status_code == 201
    assert calls == [url]
    with workspace.app.app_context():
        source = ContentSource.query.filter_by(external_id="canonical-identity").one()
        assert source.source_type == kind and source.initial_item_limit == 25
        assert source.provider_metadata == {}


def test_real_resolution_selects_existing_adapters(workspace, monkeypatch):
    from web import routes
    import feed_resolver
    import youtube_discovery
    seen = []
    def podcast(url, settings, session):
        seen.append("podcast")
        return SourceDescriptor("podcast_rss", "feed", "https://example.com/feed", "Feed")
    monkeypatch.setattr(feed_resolver, "resolve_podcast_source", podcast)
    monkeypatch.setattr(youtube_discovery.YouTubeDiscoveryClient, "resolve_source", lambda self, url: seen.append("youtube"))
    with workspace.app.app_context():
        routes.resolve_source(URLS[0][0], SimpleNamespace(**workspace.app.config))
        routes.resolve_source(URLS[-1][0], SimpleNamespace(**workspace.app.config))
    assert seen == ["podcast", "youtube"]


def test_duplicate_and_broker_failure_are_safe(workspace, monkeypatch):
    from web import routes
    import tasks
    monkeypatch.setattr(routes, "resolve_source", lambda *a: SourceDescriptor("podcast_rss", "feed", "https://example.com/feed", "Feed"))
    monkeypatch.setattr(tasks, "dispatch_task", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("private broker URL")))
    assert workspace.client.post("/api/sources", json={"url": "https://example.com/feed"}).status_code == 201
    response = workspace.client.post("/api/sources", json={"url": "https://example.com/feed"})
    assert response.status_code == 409 and response.json == {"error": "Source already exists"}
    with workspace.app.app_context():
        assert ContentSource.query.count() == 2
        assert DiscoveryRun.query.one().state == "pending"


def test_private_pages_memberships_and_lineage(workspace):
    for path in ("/", "/dashboard", "/sources/1", "/sources/1/items", "/items/1"):
        response = workspace.client.get(path)
        assert response.status_code == 200
        assert "secret command output" not in response.text
    source = workspace.client.get("/sources/1").text
    assert "newest 25" in source.lower() and "YouTube channel" in source
    item = workspace.client.get("/items/1").text
    assert "Example channel" in item and "Creator captions" in item


@pytest.mark.parametrize("selector", [
    {}, {"selector_type": "count", "requested_count": 0}, {"selector_type": "count", "requested_count": 5001},
    {"selector_type": "count", "requested_count": True},
    {"selector_type": "count", "requested_count": 2, "explicit_url": "https://youtu.be/abcdefghijk"},
    {"selector_type": "date_range", "start_date": "2026-02-02", "end_date": "2026-01-01"},
    {"selector_type": "date_range", "start_date": "bad", "end_date": "2026-01-01"},
    {"selector_type": "explicit_url", "explicit_url": "https://youtube.com/playlist?list=PLabcdefghijk"},
    {"selector_type": "explicit_url", "explicit_url": "https://youtu.be/abcdefghijk?token=secret"},
])
def test_backfill_rejects_invalid_or_multiple_selectors(workspace, selector):
    response = workspace.client.post("/api/sources/1/backfills", json=selector)
    assert response.status_code == 400
    with workspace.app.app_context():
        assert BackfillRequest.query.count() == 0


@pytest.mark.parametrize("selector", [
    {"selector_type": "count", "requested_count": 5000},
    {"selector_type": "date_range", "start_date": "2026-01-01", "end_date": "2026-01-01"},
    {"selector_type": "explicit_url", "explicit_url": "https://youtu.be/abcdefghijk"},
])
def test_backfill_commit_progress_and_cancel(workspace, monkeypatch, selector):
    import tasks
    def dispatch(name, identity, **options):
        row = db.session.get(BackfillRequest, identity)
        assert row is not None and not db.session.new and not db.session.dirty
        raise RuntimeError("redis://private:secret")
    monkeypatch.setattr(tasks, "dispatch_task", dispatch)
    response = workspace.client.post("/api/sources/1/backfills", json=selector)
    assert response.status_code == 201
    with workspace.app.app_context():
        row = db.session.get(BackfillRequest, response.json["id"])
        if row.selector_type == "date_range":
            assert row.start_date == row.end_date == date(2026, 1, 1)
        row.items_scanned, row.items_accepted, row.items_queued = 9, 5, 4
        db.session.commit()
    response = workspace.client.get("/api/backfills/1")
    assert response.json["items_scanned"] == 9 and response.json["items_queued"] == 4
    assert workspace.client.post("/api/backfills/1/cancel").status_code == 200
    with workspace.app.app_context():
        row = db.session.get(BackfillRequest, 1)
        assert row.cancel_requested and row.items_accepted == 5


def test_retry_has_durable_outbox_before_broker_failure(workspace, monkeypatch):
    import tasks
    def dispatch(name, identity, **options):
        item = db.session.get(ContentItem, identity)
        job = ProcessingJob.query.filter_by(item_id=identity).one()
        assert item.status == "discovered" and job.state == "pending"
        assert job.error_category == "dispatch_pending" and not db.session.dirty
        raise RuntimeError("private command output")
    monkeypatch.setattr(tasks, "dispatch_task", dispatch)
    assert workspace.client.post("/api/items/1/retry").status_code == 202
    with workspace.app.app_context():
        assert ProcessingJob.query.one().error_category == "dispatch_pending"
    assert workspace.client.post("/api/items/1/retry").status_code == 409


def test_all_lookups_are_owner_scoped(workspace, monkeypatch):
    from web import routes
    with workspace.app.app_context():
        db.session.add(BackfillRequest(source_id=1, selector_type="count", requested_count=1))
        db.session.commit()
    monkeypatch.setattr(routes, "current_user", SimpleNamespace(id=999))
    for method, path in [("get", "/sources/1"), ("get", "/sources/1/items"), ("get", "/items/1"),
                         ("get", "/items/1/transcript"), ("get", "/api/items/1"), ("get", "/api/backfills/1"),
                         ("post", "/api/items/1/retry"), ("post", "/api/backfills/1/cancel"),
                         ("post", "/api/sources/1/backfills")]:
        assert getattr(workspace.client, method)(path, json={} if method == "post" else None).status_code == 404


def test_browser_links_and_forms_use_browser_routes_and_work_without_js(workspace, monkeypatch):
    import tasks
    from web import routes
    monkeypatch.setattr(tasks, "dispatch_task", lambda *a, **k: None)
    monkeypatch.setattr(routes, "resolve_source", lambda *a: SourceDescriptor("podcast_rss", "feed", "https://example.com/feed", "Feed"))
    dashboard = workspace.client.get("/dashboard").text
    assert 'action="/sources"' in dashboard
    page = workspace.client.get("/sources/1").text
    assert 'href="/items/1"' in page and 'action="/sources/1/backfills"' in page
    item = workspace.client.get("/items/1").text
    assert 'action="/items/1/retry"' in item
    assert workspace.client.post("/sources", data={"url": "https://example.com/feed"}).status_code == 303
    response = workspace.client.post("/sources/1/backfills", data={"selector_type": "count", "requested_count": "2"})
    assert response.status_code == 303 and response.headers["Location"] == "/sources/1"
    assert workspace.client.post("/backfills/1/cancel").status_code == 303
    assert workspace.client.post("/items/1/retry").status_code == 303


def test_source_uniqueness_race_rolls_back_and_is_generic(workspace, monkeypatch):
    from web import routes
    from sqlalchemy.exc import IntegrityError
    monkeypatch.setattr(routes, "resolve_source", lambda *a: SourceDescriptor("podcast_rss", "feed", "https://example.com/feed", "Feed"))
    original = db.session.flush
    def raced(*args, **kwargs):
        if any(isinstance(row, ContentSource) for row in db.session.new):
            raise IntegrityError("private SQL", {}, Exception("secret database URL"))
        return original(*args, **kwargs)
    monkeypatch.setattr(db.session, "flush", raced)
    response = workspace.client.post("/api/sources", json={"url": "https://example.com/feed"})
    assert response.status_code == 409 and response.json == {"error": "Source already exists"}
    assert workspace.client.get("/dashboard").status_code == 200


def test_retry_resets_failed_stages_preserves_completed_and_does_not_touch_artifacts(workspace, monkeypatch):
    import tasks
    monkeypatch.setattr(tasks, "dispatch_task", lambda *a, **k: None)
    with workspace.app.app_context():
        item = db.session.get(ContentItem, 1)
        item.processing_stage = "normalize"
        db.session.add_all([ProcessingJob(item_id=1, stage="youtube_captions", state="failed"),
                            ProcessingJob(item_id=1, stage="normalize", state="failed"),
                            ProcessingJob(item_id=1, stage="youtube_audio", state="completed")])
        db.session.commit()
    assert workspace.client.post("/api/items/1/retry").status_code == 202
    with workspace.app.app_context():
        assert not ProcessingJob.query.filter_by(state="failed").count()
        assert ProcessingJob.query.filter_by(stage="youtube_audio").one().state == "completed"
        assert db.session.get(ContentItem, 1).transcript_s3_key.endswith("private.txt")


@pytest.mark.parametrize("url", ["https://youtube.com/watch?v=abcdefghijk&list=PLabcdefghijk",
    "https://youtube.com/c/unsupported", "https://private:secret@example.com/feed",
    "https://example.com/feed?token=secret", "file:///tmp/private", "not a URL"])
def test_invalid_intake_is_rejected_before_resolution_and_storage(workspace, monkeypatch, url):
    from web import routes
    called = []
    monkeypatch.setattr(routes, "resolve_source", lambda *a: called.append(a))
    assert workspace.client.post("/api/sources", json={"url": url}).status_code == 400
    assert not called
    with workspace.app.app_context():
        assert ContentSource.query.count() == 1 and DiscoveryRun.query.count() == 0


def test_only_canonical_descriptor_and_typed_metadata_are_persisted(workspace, monkeypatch):
    from web import routes
    import tasks
    monkeypatch.setattr(tasks, "dispatch_task", lambda *a, **k: None)
    descriptor = SourceDescriptor("youtube_channel", "UC" + "b" * 22,
        "https://www.youtube.com/channel/UC" + "b" * 22, "Canonical channel",
        provider_metadata={"uploads_playlist_id": "UU" + "b" * 22, "unexpected_payload": "raw private response"})
    monkeypatch.setattr(routes, "resolve_source", lambda *a: descriptor)
    response = workspace.client.post("/api/sources", json={"url": "https://youtube.com/@example?tracking=raw-input"})
    assert response.status_code == 201
    with workspace.app.app_context():
        row = db.session.get(ContentSource, response.json["id"])
        assert row.provider_metadata == {"uploads_playlist_id": "UU" + "b" * 22}
        assert "raw-input" not in row.canonical_url


def test_retry_while_worker_holds_lock_is_a_safe_conflict(workspace, monkeypatch):
    from contextlib import contextmanager
    import tasks
    @contextmanager
    def held(_identity):
        yield False
    monkeypatch.setattr(tasks, "_item_lock", held)
    assert workspace.client.post("/api/items/1/retry").status_code == 409
    with workspace.app.app_context():
        assert db.session.get(ContentItem, 1).status == "failed"
        assert ProcessingJob.query.count() == 0


@pytest.mark.parametrize("state", ["completed", "cancelled", "failed"])
def test_cancel_terminal_backfill_preserves_state_and_progress(workspace, state):
    with workspace.app.app_context():
        db.session.add(BackfillRequest(source_id=1, selector_type="count", requested_count=2,
            state=state, items_scanned=7, items_accepted=2, items_queued=2))
        db.session.commit()
    response = workspace.client.post("/api/backfills/1/cancel")
    assert response.status_code == 200 and response.json["state"] == state
    assert response.json["items_accepted"] == 2


def test_nonexistent_ids_have_uniform_404s(workspace):
    for method, path in [("get", "/sources/999"), ("get", "/sources/999/items"), ("get", "/items/999"),
        ("get", "/items/999/transcript"), ("get", "/api/items/999"), ("get", "/api/backfills/999"),
        ("post", "/items/999/retry"), ("post", "/backfills/999/cancel"), ("post", "/sources/999/backfills")]:
        assert getattr(workspace.client, method)(path).status_code == 404
