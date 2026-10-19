"""Explicit, bounded provider checks; never run implicitly with the offline suite.

Select this exact file AND set RUN_LIVE_YOUTUBE=1. AWS writes additionally
require RUN_LIVE_AWS_TRANSCRIBE=1 and a deliberately selected fallback URL.
No credentials, private objects, transcript bodies or URLs are printed.
"""

from collections import deque
import logging
import os
from pathlib import Path
import secrets

import pytest


@pytest.fixture
def live_settings(request):
    selected = any(Path(str(arg).split("::", 1)[0]).resolve() == Path(__file__).resolve()
                   for arg in request.config.invocation_params.args if not str(arg).startswith("-"))
    if os.environ.get("RUN_LIVE_YOUTUBE") != "1" or not selected:
        pytest.skip("Live providers disabled: select this file and set RUN_LIVE_YOUTUBE=1")
    if not os.environ.get("YOUTUBE_API_KEY"):
        pytest.skip("Live YouTube blocked: YOUTUBE_API_KEY is required")
    from config import load_settings
    environment = dict(os.environ)
    environment.update(SECRET_KEY=secrets.token_urlsafe(48),
        DATABASE_URL="postgresql+psycopg2://unused:unused@postgres.invalid/podcasts",
        REDIS_URL="redis://redis.invalid:6379/0",
        AWS_REGION=os.environ.get("AWS_REGION", "us-east-1"),
        AWS_S3_BUCKET_NAME="podcast-transcriptions-web-2025")
    return load_settings(environment)


@pytest.fixture(autouse=True)
def private_diagnostics():
    # Provider failures are reported only through fixed category messages.
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(previous)


def required(name):
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"Controlled live check blocked: {name} is required")
    return value


def short_video(client, url):
    item = client.resolve_video(url)
    if item.duration_seconds is None or not 0 < item.duration_seconds <= 300:
        pytest.fail("Choose a public completed video of at most 300 seconds", pytrace=False)
    return item


def test_live_caption_priority(live_settings, tmp_path):
    url = required("LIVE_YOUTUBE_VIDEO_URL")
    expected = required("LIVE_YOUTUBE_EXPECTED_ORIGIN")
    if expected not in {"creator_caption", "automatic_caption"}:
        pytest.fail("LIVE_YOUTUBE_EXPECTED_ORIGIN must select a caption origin", pytrace=False)
    from youtube_discovery import YouTubeDiscoveryClient
    from youtube_media import CaptionInventory, YouTubeMediaClient, YouTubeMediaError, select_caption
    from transcripts import normalize_vtt, TranscriptValidationError
    try:
        item = short_video(YouTubeDiscoveryClient(live_settings.YOUTUBE_API_KEY), url)
        media = YouTubeMediaClient(live_settings)
        remaining = list(media.inventory(item.canonical_url).candidates)
        while remaining:
            candidate = select_caption(CaptionInventory(item.external_id, tuple(remaining)),
                                       live_settings.YOUTUBE_PREFERRED_CAPTION_LANGUAGES)
            remaining.remove(candidate)
            origin = "creator_caption" if candidate.kind == "creator" else "automatic_caption"
            try:
                path = media.download_caption(item.canonical_url, candidate, tmp_path)
                transcript = normalize_vtt(path.read_bytes(), platform="youtube", external_id=item.external_id,
                    item_id="controlled-live", source_id="controlled-live", language=candidate.language,
                    origin=origin, provider_version=live_settings.YTDLP_EXPECTED_VERSION)
            except YouTubeMediaError as error:
                if error.category not in {"invalid_caption", "invalid_output", "output_too_large"}:
                    raise
                continue
            except TranscriptValidationError:
                continue
            assert bool(origin == expected), "Highest usable caption origin differs from selected expectation"
            assert bool(transcript.segments), "Live captions have no timed segments"
            assert all(segment.citation.startswith(item.canonical_url + "&t=") for segment in transcript.segments)
            return
        pytest.fail("Selected caption video has no usable preferred-language captions", pytrace=False)
    except Exception:
        pytest.fail("Live caption check failed; inspect provider access privately", pytrace=False)


@pytest.mark.parametrize("setting,kind", [
    ("LIVE_YOUTUBE_CHANNEL_URL", "youtube_channel"),
    ("LIVE_YOUTUBE_PLAYLIST_URL", "youtube_playlist"),
])
def test_live_newest25_and_historical_backfill(live_settings, setting, kind, monkeypatch):
    url = required(setting)
    from discovery_service import DiscoveryService
    from extensions import db
    from models import Admin, BackfillRequest, ContentItem, ContentSource, DiscoveryRun, SourceItem
    from youtube_discovery import YouTubeDiscoveryClient
    # Discovery writes only to a disposable SQLite database; no media is queued
    # to a real broker and no provider artifacts are written by this check.
    try:
        class BoundedClient(YouTubeDiscoveryClient):
            pages = 0

            def discover_page(self, source, page_token=None):
                self.pages += 1
                if self.pages > 10:
                    raise ValueError("Controlled live source exceeds ten-page limit")
                return super().discover_page(source, page_token)

        adapter = BoundedClient(live_settings.YOUTUBE_API_KEY)
        descriptor = adapter.resolve_source(url)
        assert bool(descriptor.source_type == kind), "Live source has the wrong kind"
        # Keep this independent of the offline app_environment fixture, which
        # intentionally replaces the provider key with a synthetic credential.
        from app import create_app
        for name in ("SECRET_KEY", "DATABASE_URL", "REDIS_URL", "AWS_REGION", "AWS_S3_BUCKET_NAME", "YOUTUBE_API_KEY"):
            monkeypatch.setenv(name, getattr(live_settings, name))
        app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
                          "RATELIMIT_ENABLED": False, "RATELIMIT_STORAGE_URI": "memory://"})
        with app.app_context():
            db.create_all()
            try:
                owner = Admin(email="live-proof@example.com", password_hash="unused")
                db.session.add(owner)
                db.session.flush()
                source = ContentSource(admin_id=owner.id, source_type=kind, external_id=descriptor.external_id,
                    canonical_url=descriptor.canonical_url, name=descriptor.title,
                    provider_metadata=dict(descriptor.provider_metadata), initial_item_limit=25)
                db.session.add(source)
                db.session.commit()
                pending = deque()
                service = DiscoveryService(db.session, live_settings, lambda _: adapter,
                    lambda name, identity, **kwargs: pending.append((name, identity)))
                run_id = service.discover_source(source.id)

                def drain():
                    steps = 0
                    while pending:
                        name, identity = pending.popleft()
                        if name != "process_item":
                            getattr(service, name)(identity)
                        steps += 1
                        if steps > 100:
                            pytest.fail("Choose a smaller live source (100 invocation bound)", pytrace=False)

                drain()
                run = db.session.get(DiscoveryRun, run_id)
                assert bool(run.state == "completed"), "Live initial discovery failed"
                assert bool(ContentItem.query.count() == min(25, run.items_scanned)), "Newest-25 count failed"
                # A second bounded provider scan independently checks ordering.
                adapter.pages = 0
                page, cursor, discovered = 0, None, {}
                while True:
                    result = adapter.discover_page(descriptor, cursor)
                    discovered.update({item.external_id: item for item in result.items})
                    page += 1
                    if result.exhausted:
                        break
                    if page >= 10:
                        pytest.fail("Choose a source with at most 10 discovery pages", pytrace=False)
                    cursor = result.next_cursor
                expected = {item.external_id for item in sorted(discovered.values(),
                    key=lambda item: (item.published_at, item.external_id), reverse=True)[:25]}
                assert bool({item.external_id for item in ContentItem.query.all()} == expected), "Newest-25 identities failed"
                adapter.pages = 0
                identity = service.create_backfill(source.id, selector_type="count", requested_count=26)
                drain()
                backfill = db.session.get(BackfillRequest, identity)
                assert bool(backfill.state == "completed"), "Live backfill failed"
                assert bool(backfill.items_accepted == min(26, len(discovered))), "Historical progress failed"
                before = ContentItem.query.count(), SourceItem.query.count()
                service.run_backfill(identity)
                drain()
                assert bool((ContentItem.query.count(), SourceItem.query.count()) == before), "Live replay duplicated rows"
            finally:
                db.session.remove()
                db.drop_all()
    except Exception:
        pytest.fail("Live discovery/backfill check failed; inspect provider access privately", pytrace=False)


def test_live_deliberate_amazon_fallback(live_settings, tmp_path):
    if os.environ.get("RUN_LIVE_AWS_TRANSCRIBE") != "1":
        pytest.skip("AWS writes/costs disabled: set RUN_LIVE_AWS_TRANSCRIBE=1 explicitly")
    url = required("LIVE_YOUTUBE_FALLBACK_URL")
    import boto3
    # Credential resolution stays after both explicit opt-ins. Never print it.
    from hashlib import sha256
    from s3_utils import S3Manager
    from transcribe import transcribe_audio_from_s3
    from youtube_discovery import YouTubeDiscoveryClient
    from youtube_media import YouTubeMediaClient, select_caption
    try:
        if boto3.Session().get_credentials() is None:
            pytest.skip("Live Amazon fallback blocked: AWS credentials are required")
        item = short_video(YouTubeDiscoveryClient(live_settings.YOUTUBE_API_KEY), url)
        media = YouTubeMediaClient(live_settings)
        if select_caption(media.inventory(item.canonical_url), live_settings.YOUTUBE_PREFERRED_CAPTION_LANGUAGES):
            pytest.fail("Fallback selection has captions; choose a deliberately caption-free video", pytrace=False)
        storage = S3Manager(live_settings)
        audio = media.extract_audio(item.canonical_url, tmp_path)
        digest = sha256(audio.path.read_bytes()).hexdigest()
        key = storage.generate_s3_key("youtube", item.external_id, "audio", digest + ".mp3")
        storage.upload_file(audio.path, key, "audio/mpeg")
        arguments = dict(item_id="controlled-live-" + secrets.token_hex(8), source_id="controlled-live",
            platform="youtube", external_id=item.external_id, audio_sha256=digest,
            settings=live_settings, s3_manager=storage, canonical_url=item.canonical_url)
        first = transcribe_audio_from_s3(key, **arguments)
        second = transcribe_audio_from_s3(key, **arguments)
        assert bool(first == second), "Amazon replay changed artifact identity"
        assert bool(first["provider"] == "amazon_transcribe"), "Unexpected fallback provider"
    except Exception:
        pytest.fail("Live Amazon check failed; inspect provider access privately", pytrace=False)


def test_live_small_public_rss(live_settings):
    url = required("LIVE_RSS_FEED_URL")
    import requests
    from feed_resolver import resolve_podcast_source
    from episode_fetcher import discover_podcast_items
    try:
        with requests.Session() as session:
            session.trust_env = False
            source = resolve_podcast_source(url, live_settings, session)
            page = discover_podcast_items(source, live_settings, session, limit=25)
            assert bool(0 < len(page.items) <= 25), "Selected RSS feed has no playable episodes"
    except Exception:
        pytest.fail("Live RSS check failed; inspect feed access privately", pytrace=False)
