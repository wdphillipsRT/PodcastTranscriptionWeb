"""Feed-declared hosts and guests reach the database; YouTube owners are seeded."""

import pytest

from extensions import db
from ingestion_types import DiscoveryPage, SourceDescriptor
from models import ContentItem, ContentSource, SourceItem, utc_now
from tests.test_discovery_service import Adapter, descriptor, service, store  # noqa: F401
from tests.test_web_sources import workspace  # noqa: F401


def discover(source, *pages):
    svc = service(Adapter(list(pages)))
    svc.discover_source(source.id)
    db.session.expire_all()
    return svc


def test_discovery_records_feed_hosts_and_guests(store):
    source = store()
    discover(source, DiscoveryPage(items=[descriptor(1, guests=["James Clear"]), descriptor(2)],
                                   source_hosts=["Ada Host"]))
    assert source.feed_hosts == ["Ada Host"] and source.hosts_updated_at is not None
    items = {row.external_id: row for row in ContentItem.query.all()}
    # Items are created after staging, so the next fetch records their guests.
    assert items["1"].feed_guests is None
    discover(source, DiscoveryPage(items=[descriptor(1, guests=["James Clear"]), descriptor(2)],
                                   source_hosts=["Ada Host"]))
    assert items["1"].feed_guests == ["James Clear"] and items["1"].guests_updated_at is not None
    assert items["2"].feed_guests is None and items["2"].guests_updated_at is None


def test_feed_changes_never_move_admin_overrides(store):
    source = store()
    source.admin_hosts, source.hosts_updated_at = ["Admin Host"], None
    db.session.commit()
    discover(source, DiscoveryPage(items=[descriptor(1)], source_hosts=["Feed Host"]))
    assert source.feed_hosts == ["Feed Host"] and source.admin_hosts == ["Admin Host"]
    assert source.hosts_updated_at is None
    item = ContentItem.query.one()
    item.admin_guests = []
    db.session.commit()
    discover(source, DiscoveryPage(items=[descriptor(1, guests=["Feed Guest"])], source_hosts=["Feed Host"]))
    assert item.feed_guests == ["Feed Guest"] and item.admin_guests == [] and item.guests_updated_at is None


def test_pages_without_host_reports_leave_hosts_alone(store):
    source = store(kind="youtube_playlist", identity="playlist")
    source.feed_hosts = ["Channel Owner"]
    db.session.commit()
    discover(source, DiscoveryPage(items=[descriptor(1, youtube=True)]))
    assert source.feed_hosts == ["Channel Owner"]


def test_added_source_stores_resolved_hosts(workspace, monkeypatch):  # noqa: F811
    from web import routes
    import tasks
    monkeypatch.setattr(tasks, "dispatch_task", lambda *a, **k: None)
    monkeypatch.setattr(routes, "resolve_source", lambda *a: SourceDescriptor(
        "podcast_rss", "feed", "https://example.com/feed", "Feed", hosts=("Ada Host",)))
    response = workspace.client.post("/api/sources", json={"url": "https://example.com/feed.xml"})
    assert response.status_code == 201
    with workspace.app.app_context():
        row = db.session.get(ContentSource, response.json["id"])
        assert row.feed_hosts == ["Ada Host"] and row.admin_hosts is None and row.hosts_updated_at


def test_seed_hosts_fills_only_missing_youtube_hosts(workspace):  # noqa: F811
    with workspace.app.app_context():
        channel = ContentSource.query.one()
        playlist = ContentSource(admin_id=channel.admin_id, source_type="youtube_playlist", external_id="PLx",
            canonical_url="https://www.youtube.com/playlist?list=PLx", name="Lessons")
        video = ContentSource(admin_id=channel.admin_id, source_type="youtube_video", external_id="vid",
            canonical_url="https://www.youtube.com/watch?v=vid", name="One video", feed_hosts=["Kept"])
        podcast = ContentSource(admin_id=channel.admin_id, source_type="podcast_rss", external_id="feed",
            canonical_url="https://example.com/feed", name="Feed")
        db.session.add_all([playlist, video, podcast])
        db.session.flush()
        for number, name in enumerate(("Teacher", "Teacher", "Guest Channel")):
            item = ContentItem(admin_id=channel.admin_id, item_type="youtube_video", platform="youtube",
                external_id=f"v{number}", canonical_url=f"https://www.youtube.com/watch?v=v{number}",
                title="Video", channel_name=name)
            db.session.add(item)
            db.session.flush()
            db.session.add(SourceItem(source_id=playlist.id, item_id=item.id))
        db.session.commit()
    result = workspace.app.test_cli_runner().invoke(args=["knowledge", "seed-hosts"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "seeded=2 missing=1"
    with workspace.app.app_context():
        hosts = {row.source_type: row.feed_hosts for row in ContentSource.query.all()}
        assert hosts == {"youtube_channel": ["Example channel"], "youtube_playlist": ["Teacher"],
                         "youtube_video": ["Kept"], "podcast_rss": None}
        assert ContentSource.query.filter_by(source_type="youtube_playlist").one().hosts_updated_at is not None
