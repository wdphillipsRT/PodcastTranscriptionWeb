"""Regressions for source baselines, mixed YouTube pages, and historical counts."""
from copy import deepcopy

import pytest
import sqlalchemy as sa

from extensions import db
from ingestion_types import DiscoveryPage, SourceDescriptor
from models import ContentItem, DiscoveryCandidate, DiscoveryRun, ProcessingJob, SourceItem
from tests.test_backfills import request
from tests.test_discovery_service import Adapter, descriptor, service, store
from tests.test_youtube_discovery import (CHANNEL, PLAYLIST, UPLOADS, VIDEO, client_with,
                                         fixture, response)


@pytest.mark.parametrize("kind", ["podcast_rss", "youtube_playlist", "youtube_channel"])
def test_scheduled_baseline_survives_session_restart_and_all_pages(store, kind):
    source = store(kind)
    source_id = source.id
    youtube = kind != "podcast_rss"
    pages = [DiscoveryPage(tuple(descriptor(n, youtube) for n in range(i * 5, i * 5 + 5)),
                           str(i + 1) if i < 6 else None, i == 6) for i in range(7)]
    adapter = Adapter(pages)
    svc = service(adapter)
    run_id = svc.discover_source(source_id)
    assert SourceItem.query.count() == 0
    svc.continue_discovery(run_id)
    assert SourceItem.query.count() == 25
    assert DiscoveryCandidate.query.filter_by(run_id=run_id).count() == 35
    db.session.remove()
    svc = service(adapter)
    scheduled = svc.discover_source(source_id)
    svc.continue_discovery(scheduled)
    assert SourceItem.query.count() == 25
    assert ProcessingJob.query.count() == 25
    assert db.session.get(DiscoveryRun, scheduled).items_accepted == 0
    # Identity, not publication time, determines novelty. Arbitrary playlist
    # position and backdated publication must not conceal a genuinely new item.
    adapter.pages[-1] = DiscoveryPage((*pages[-1].items, descriptor(-1, youtube)))
    newest = svc.discover_source(source_id)
    svc.continue_discovery(newest)
    assert SourceItem.query.count() == ProcessingJob.query.count() == 26
    assert ContentItem.query.filter_by(external_id="-1").one()


def test_observation_is_source_scoped_and_rollback_safe(store, monkeypatch):
    first, second = store("youtube_playlist", "first"), store("youtube_playlist", "second")
    adapter = Adapter([DiscoveryPage(tuple(descriptor(n, True) for n in range(35)))])
    svc = service(adapter)
    svc.discover_source(first.id)
    second_id = second.id
    original = svc._stage
    def interrupt(run, items, *args):
        original(run, items, *args)
        raise RuntimeError("interrupted page transaction")
    monkeypatch.setattr(svc, "_stage", interrupt)
    failed = svc.discover_source(second_id)
    assert DiscoveryCandidate.query.filter_by(run_id=failed).count() == 0
    monkeypatch.setattr(svc, "_stage", original)
    svc.continue_discovery(failed)
    assert SourceItem.query.filter_by(source_id=second_id).count() == 25
    assert ProcessingJob.query.count() == 25


def test_scheduled_observation_baseline_belongs_to_its_source(store):
    first, second = store("youtube_playlist", "first"), store("youtube_playlist", "second")
    adapter = Adapter([DiscoveryPage((descriptor(1, True), descriptor(2, True)))])
    svc = service(adapter)
    svc.discover_source(first.id)
    adapter.pages = [DiscoveryPage((descriptor(1, True),))]
    svc.discover_source(second.id)
    adapter.pages = [DiscoveryPage((descriptor(1, True), descriptor(2, True)))]
    svc.discover_source(second.id)
    assert SourceItem.query.filter_by(source_id=second.id).count() == 2
    assert ProcessingJob.query.count() == 2


@pytest.mark.parametrize("kind", ["podcast_rss", "youtube_playlist"])
def test_count_backfill_counts_new_memberships_and_overlap_replay(store, kind):
    source = store(kind)
    svc = service(Adapter([DiscoveryPage(tuple(descriptor(n, kind != "podcast_rss") for n in range(35)))]))
    svc.discover_source(source.id)
    row = request(source, selector_type="count", requested_count=5)
    svc.run_backfill(row.id)
    assert (row.state, row.items_accepted, row.items_queued) == ("completed", 5, 5)
    assert SourceItem.query.count() == ProcessingJob.query.count() == 30
    assert {int(i.external_id) for i in ContentItem.query.all()} == set(range(5, 35))
    svc.run_backfill(row.id)
    assert SourceItem.query.count() == 30
    overlap = request(source, selector_type="count", requested_count=5)
    svc.run_backfill(overlap.id)
    assert overlap.items_accepted == overlap.items_queued == 5
    assert SourceItem.query.count() == ProcessingJob.query.count() == 35
    exhausted = request(source, selector_type="count", requested_count=5)
    svc.run_backfill(exhausted.id)
    assert exhausted.state == "completed" and exhausted.items_accepted == exhausted.items_queued == 0


@pytest.mark.parametrize("kind", ["youtube_playlist", "youtube_channel"])
@pytest.mark.parametrize("unavailable", ["private", "deleted", "missing", "upcoming", "live", "region", "age",
                                         "video_private", "video_deleted", "unlisted", "uploading", "rejected"])
def test_mixed_youtube_page_preserves_public_members_and_cursor(kind, unavailable):
    other = "abcdefghijk"
    playlist = fixture("playlist_page")
    bad_member = deepcopy(playlist["items"][0])
    bad_member["contentDetails"]["videoId"] = other
    bad_member["snippet"]["resourceId"]["videoId"] = other
    if unavailable == "private":
        bad_member["status"]["privacyStatus"] = "private"
    if unavailable == "deleted":
        bad_member["status"]["uploadStatus"] = "deleted"
    playlist["items"].append(bad_member)
    videos = fixture("public_video")
    bad_video = deepcopy(videos["items"][0])
    bad_video["id"] = other
    if unavailable in {"upcoming", "live"}:
        bad_video["snippet"]["liveBroadcastContent"] = unavailable
    if unavailable == "region":
        bad_video["contentDetails"]["regionRestriction"] = {"blocked": ["US"]}
    if unavailable == "age":
        bad_video["contentDetails"]["contentRating"] = {"ytRating": "ytAgeRestricted"}
    if unavailable in {"video_private", "unlisted"}:
        bad_video["status"]["privacyStatus"] = "private" if unavailable == "video_private" else "unlisted"
    if unavailable in {"video_deleted", "uploading", "rejected"}:
        bad_video["status"]["uploadStatus"] = "deleted" if unavailable == "video_deleted" else unavailable
    if unavailable not in {"private", "deleted", "missing"}:
        videos["items"].append(bad_video)
    final = deepcopy(fixture("playlist_page"))
    final.pop("nextPageToken")
    client, _ = client_with(response(playlist), response(videos), response(final), response(fixture("public_video")))
    source = SourceDescriptor(kind, PLAYLIST if kind == "youtube_playlist" else CHANNEL,
                              "https://youtube.com/", "Source", provider_metadata={"uploads_playlist_id": UPLOADS})
    page = client.discover_page(source)
    assert [item.external_id for item in page.items] == [VIDEO]
    assert page.unavailable_ids == (other,)
    assert page.next_cursor == "CAUQAA" and not page.exhausted
    assert client.discover_page(source, page.next_cursor).exhausted


def test_unavailable_observation_is_durable_and_revisited_when_public(store):
    source = store("youtube_playlist")
    source_id = source.id
    adapter = Adapter([DiscoveryPage((descriptor(1, True),), unavailable_ids=("2",))])
    svc = service(adapter)
    initial = svc.discover_source(source.id)
    assert DiscoveryCandidate.query.filter_by(run_id=initial, external_id="2").one().was_available is False
    db.session.remove()
    adapter.pages = [DiscoveryPage((descriptor(1, True), descriptor(2, True)))]
    svc.discover_source(source_id)
    assert SourceItem.query.count() == ProcessingJob.query.count() == 2
    svc.discover_source(source_id)
    assert SourceItem.query.count() == ProcessingJob.query.count() == 2


def test_candidate_availability_migration_model_parity(app_factory, tmp_path):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    column = DiscoveryCandidate.__table__.c.was_available
    assert not column.nullable and isinstance(column.type, sa.Boolean)
    app = app_factory(SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'observation.sqlite'}")
    result = app.test_cli_runner().invoke(args=["db", "upgrade"])
    assert result.exit_code == 0, result.output
    with app.app_context(), db.engine.connect() as connection:
        reflected = next(c for c in sa.inspect(connection).get_columns("discovery_candidates")
                         if c["name"] == "was_available")
        assert not reflected["nullable"] and isinstance(reflected["type"], sa.Boolean)
        assert reflected["default"] == "1"
        assert compare_metadata(MigrationContext.configure(connection), db.metadata) == []


@pytest.mark.parametrize("bad", ["malformed", "quota"])
def test_mixed_page_does_not_swallow_invalid_response_or_request_failure(bad):
    from youtube_discovery import YouTubeDiscoveryError

    members = fixture("playlist_page")
    videos = fixture("public_video")
    if bad == "malformed":
        duplicate = deepcopy(videos["items"][0])
        duplicate["id"] = "abcdefghijk"
        duplicate["snippet"]["liveBroadcastContent"] = "invalid"
        videos["items"].append(duplicate)
        member = deepcopy(members["items"][0])
        member["contentDetails"]["videoId"] = "abcdefghijk"
        member["snippet"]["resourceId"]["videoId"] = "abcdefghijk"
        members["items"].append(member)
        second = response(videos)
        category = "invalid_response"
    else:
        second = response({"error": {"errors": [{"reason": "quotaExceeded"}]}}, status=403)
        category = "quota_exhausted"
    client, _ = client_with(response(members), second)
    source = SourceDescriptor("youtube_playlist", PLAYLIST, "https://youtube.com/", "Source")
    with pytest.raises(YouTubeDiscoveryError, match=category):
        client.discover_page(source)


@pytest.mark.parametrize("identifiable", [True, False])
def test_private_placeholder_without_video_details_preserves_siblings(identifiable):
    members = fixture("playlist_page")
    placeholder = {"status": {"privacyStatus": "private"}, "snippet": {"title": "Private video"}}
    if identifiable:
        placeholder["snippet"]["resourceId"] = {"kind": "youtube#video", "videoId": "abcdefghijk"}
    members["items"].append(placeholder)
    client, _ = client_with(response(members), response(fixture("public_video")))
    source = SourceDescriptor("youtube_playlist", PLAYLIST, "https://youtube.com/", "Source")
    page = client.discover_page(source)
    assert [item.external_id for item in page.items] == [VIDEO]
    assert page.unavailable_ids == (("abcdefghijk",) if identifiable else ())
    assert page.next_cursor == "CAUQAA"
