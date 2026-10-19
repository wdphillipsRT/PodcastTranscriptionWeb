"""Phase 1 acceptance proof: real application paths, offline I/O boundaries."""

from collections import Counter, deque
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
import socket
from types import SimpleNamespace
from unittest.mock import Mock

from botocore.exceptions import ClientError
from flask_login import current_user
import pytest
import requests

from extensions import db
from models import (Admin, BackfillRequest, ContentItem, ContentSource,
                    DiscoveryCandidate, DiscoveryRun, ProcessingJob, SourceItem)
from s3_utils import BUCKET_NAME, S3Manager
from transcripts import canonical_json
import tasks
import transcribe
from youtube_media import YouTubeMediaClient


FIXTURES = Path(__file__).with_name("fixtures")
CHANNEL = "UCabcdefghijklmnopqrstuv"
PLAYLIST = "PLabcdefghijklmnopqrstuv"
UPLOADS = "UUabcdefghijklmnopqrstuv"
FEED_URL = "https://feeds.example.com/smoke.xml"
AUDIO_URL = "https://media.example.com/smoke.mp3"
AUDIO = b"synthetic bounded audio"
VTT = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nSynthetic private words.\n"
PASSWORD = "synthetic-smoke-password-123"


def video_id(number):
    return f"smoke{number:06d}"


def fixture(name):
    return json.loads((FIXTURES / "youtube" / (name + ".json")).read_text())


@pytest.fixture
def smoke(app_factory, monkeypatch):
    """Stub only HTTP, downloader execution, AWS SDK and broker transports."""
    def forbidden(*args, **kwargs):
        raise AssertionError("Smoke proof attempted live network access")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kwargs:
        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))])
    app = app_factory(WTF_CSRF_ENABLED=True, SQLALCHEMY_DATABASE_URI="sqlite:///:memory:")
    queue, dispatched, downloads, paths = deque(), [], [], []
    objects, puts, provider_jobs, starts, http_calls = {}, [], {}, [], []
    settings = SimpleNamespace(**app.config)

    def response(body, url, content_type):
        result = requests.Response()
        result.status_code, result.url = 200, url
        result.headers.update({"Content-Type": content_type, "Content-Length": str(len(body))})
        result.raw = BytesIO(body)
        return result

    episodes = "".join(
        f'<item><guid>smoke-episode-{n}</guid><title>Smoke episode {n}</title>'
        f'<pubDate>{(datetime(2026, 1, 1) + timedelta(days=n)).isoformat()}Z</pubDate>'
        f'<link>https://example.com/episode-{n}</link>'
        f'<enclosure url="{AUDIO_URL}" type="audio/mpeg" length="{len(AUDIO)}"/></item>'
        for n in range(30))
    feed = (f'<rss version="2.0"><channel><title>Smoke podcast</title><link>{FEED_URL}</link>'
            f'<description>Offline proof</description>{episodes}</channel></rss>').encode()

    def http(self, method, url, **kwargs):
        assert kwargs["stream"] and kwargs["allow_redirects"] is False
        assert len(kwargs["timeout"]) == 2
        http_calls.append((method, url))
        if url == FEED_URL:
            return response(b"" if method == "HEAD" else feed, url, "application/rss+xml")
        if url == AUDIO_URL:
            return response(AUDIO, url, "audio/mpeg")
        assert url.startswith("https://www.googleapis.com/youtube/v3/")
        params = kwargs["params"]
        assert params["key"] == app.config["YOUTUBE_API_KEY"]
        resource = url.rsplit("/", 1)[-1]
        if resource == "channels":
            data = fixture("channel")
        elif resource == "playlists":
            data = fixture("playlist")
        elif resource == "videos":
            data = {"items": []}
            for identity in params["id"].split(","):
                entry = deepcopy(fixture("public_video")["items"][0])
                number = int(identity[5:])
                entry["id"] = identity
                entry["snippet"].update(title=f"Smoke video {number}", publishedAt=(
                    datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=number)).isoformat())
                data["items"].append(entry)
        else:
            assert resource == "playlistItems" and params["playlistId"] in {PLAYLIST, UPLOADS}
            if "videoId" in params:
                identities, next_page = [params["videoId"]], None
            else:
                # Six pages force continuation; arbitrary order puts the newest
                # items on the last page. Duplicate memberships exercise staging.
                order = list(range(30)) if params["playlistId"] == UPLOADS else list(range(25, 30)) + list(range(25))
                offset = int(params.get("pageToken", "0"))
                identities = [video_id(n) for n in order[offset:offset + 5]]
                identities += identities[:1]
                next_page = str(offset + 5) if offset < 25 else None
            entries = []
            for identity in identities:
                entry = deepcopy(fixture("playlist_page")["items"][0])
                entry["contentDetails"]["videoId"] = identity
                entry["snippet"]["resourceId"]["videoId"] = identity
                entry["snippet"]["playlistId"] = params["playlistId"]
                entries.append(entry)
            data = {"items": entries}
            if next_page:
                data["nextPageToken"] = next_page
        return response(json.dumps(data).encode(), url, "application/json")

    monkeypatch.setattr(requests.sessions.Session, "request", http)

    aws = Mock()
    aws.meta.region_name = "us-east-1"
    aws.get_bucket_location.return_value = {"LocationConstraint": None}

    def put(**kwargs):
        assert kwargs["Bucket"] == BUCKET_NAME
        assert kwargs["ServerSideEncryption"] == "AES256" and "ACL" not in kwargs
        objects[kwargs["Key"]] = kwargs["Body"]
        puts.append(kwargs["Key"])
        return {}

    def upload(filename, bucket, key, ExtraArgs):
        return put(Bucket=bucket, Key=key, Body=Path(filename).read_bytes(), **ExtraArgs)

    def get(**kwargs):
        assert kwargs["Bucket"] == BUCKET_NAME
        data = objects[kwargs["Key"]]
        return {"Body": BytesIO(data), "ContentLength": len(data)}

    aws.put_object.side_effect, aws.upload_file.side_effect, aws.get_object.side_effect = put, upload, get
    storage = S3Manager(settings, client=aws)
    monkeypatch.setattr(tasks, "get_processing_storage", lambda: storage)
    monkeypatch.setattr("web.routes.get_s3_manager", lambda configuration: storage)
    client = Mock()
    client.meta.region_name = "us-east-1"

    def get_job(**kwargs):
        name = kwargs["TranscriptionJobName"]
        if name not in provider_jobs:
            raise ClientError({"Error": {"Code": "NotFoundException"}}, "GetTranscriptionJob")
        return {"TranscriptionJob": provider_jobs[name]}

    def start_job(**kwargs):
        assert kwargs["OutputBucketName"] == BUCKET_NAME
        assert kwargs["Media"]["MediaFileUri"].startswith(f"s3://{BUCKET_NAME}/items/")
        key = kwargs["OutputKey"]
        result = json.loads((FIXTURES / "transcribe-result.json").read_bytes())
        result["jobName"] = kwargs["TranscriptionJobName"]
        objects[key] = json.dumps(result).encode()
        provider_jobs[kwargs["TranscriptionJobName"]] = {
            **kwargs, "TranscriptionJobStatus": "COMPLETED",
            "Transcript": {"TranscriptFileUri": f"s3://{BUCKET_NAME}/{key}"}}
        starts.append(kwargs["TranscriptionJobName"])

    client.get_transcription_job.side_effect, client.start_transcription_job.side_effect = get_job, start_job
    monkeypatch.setattr(transcribe, "get_transcribe_client", lambda configuration: client)

    def run_media(self, url, options, directory, *, machine_output=False):
        identity = url.split("v=")[1]
        number = int(identity[5:])
        action = "inventory" if machine_output else "audio" if "--extract-audio" in options else (
            "creator" if "--write-subs" in options else "automatic")
        downloads.append((identity, action))
        if machine_output:
            return json.dumps({"id": identity, "availability": "public",
                "subtitles": {"en": [{"ext": "vtt"}]},
                "automatic_captions": {"en": [{"ext": "vtt"}]}}).encode()
        path = directory / ("media.mp3" if action == "audio" else "media.en.vtt")
        # Corrupt creator captions test real normalization rejection and priority.
        data = AUDIO if action == "audio" else b"invalid VTT" if (
            number == 27 or number == 28 and action == "creator") else VTT
        path.write_bytes(data)
        paths.append(path)
        return b""

    monkeypatch.setattr(YouTubeMediaClient, "_run", run_media)

    def dispatch(name, identity, **options):
        assert type(identity) is int and not db.session.new and not db.session.dirty
        queue.append((name, identity))
        dispatched.append((name, identity))

    monkeypatch.setattr(tasks, "dispatch_task", dispatch)

    def drain():
        invocations = 0
        while queue:
            name, identity = queue.popleft()
            getattr(tasks, name).run(identity)
            invocations += 1
            assert invocations < 1000, "Unbounded task continuation"

    with app.app_context():
        db.create_all()
        yield SimpleNamespace(app=app, client=app.test_client(), drain=drain, queue=queue,
            dispatched=dispatched, downloads=downloads, objects=objects, puts=puts, starts=starts,
            paths=paths, http_calls=http_calls, storage=storage)
        db.session.remove()
        db.drop_all()


def csrf(client, path="/dashboard"):
    # This database fixture retains an app context; browsers get a fresh g on
    # every request, including after login clears the pre-authentication session.
    from flask import g
    g.pop("csrf_token", None)
    page = client.get(path)
    token = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
    assert token, "CSRF token absent"
    return {"X-CSRFToken": token.group(1)}


def counts():
    return tuple(model.query.count() for model in (
        Admin, ContentSource, ContentItem, SourceItem, BackfillRequest, ProcessingJob))


def test_phase1_ingests_backfills_and_replays_privately(smoke, monkeypatch, caplog):
    browser = smoke.client
    cli = smoke.app.test_cli_runner()
    created = cli.invoke(args=["admin", "create", "--email", "Smoke@Example.com"],
                         input=f"{PASSWORD}\n{PASSWORD}\n")
    assert created.exit_code == 0 and PASSWORD not in created.output
    refused = cli.invoke(args=["admin", "create", "--email", "second@example.com"])
    assert refused.exit_code != 0 and "already exists" in refused.output
    assert browser.get("/api/items/1").status_code == 401
    token = csrf(browser, "/login")
    assert browser.post("/login", headers=token,
        data={"email": "smoke@example.com", "password": PASSWORD}).status_code == 302
    headers = csrf(browser)
    sources = {}
    for kind, url in (("rss", FEED_URL), ("video", f"https://youtu.be/{video_id(29)}"),
                      ("channel", f"https://youtube.com/channel/{CHANNEL}"),
                      ("playlist", f"https://youtube.com/playlist?list={PLAYLIST}")):
        response = browser.post("/api/sources", json={"url": url}, headers=headers)
        assert response.status_code == 201, f"Synthetic {kind} intake failed: {smoke.http_calls}"
        sources[kind] = response.json["id"]
        smoke.drain()
    assert counts()[:5] == (1, 4, 50, 76, 0)
    rss = db.session.get(ContentSource, sources["rss"])
    expected_rss = {sha256((rss.external_id + f"smoke-episode-{n}").encode()).hexdigest() for n in range(5, 30)}
    assert {row.external_id for row in ContentItem.query.filter_by(platform="podcast")} == expected_rss
    newest = {video_id(n) for n in range(5, 30)}
    for kind in ("channel", "playlist"):
        rows = ContentItem.query.join(SourceItem).filter(SourceItem.source_id == sources[kind]).all()
        assert {row.external_id for row in rows} == newest
        assert db.session.get(ContentSource, sources[kind]).initial_item_limit == 25
    shared = ContentItem.query.filter_by(external_id=video_id(29)).one()
    assert SourceItem.query.filter_by(item_id=shared.id).count() == 3
    assert sum(name == "process_item" and identity == shared.id for name, identity in smoke.dispatched) == 1
    assert sum(name == "continue_discovery" for name, _ in smoke.dispatched) >= 2

    selectors = [
        ("channel", {"selector_type": "date_range", "start_date": "2026-01-04", "end_date": "2026-01-05"}, 2, 2),
        ("playlist", {"selector_type": "count", "requested_count": 28}, 28, 1),
        ("channel", {"selector_type": "explicit_url", "explicit_url": f"https://www.youtube.com/watch?v={video_id(1)}"}, 1, 1),
    ]
    request_ids = []
    for kind, selector, accepted, queued in selectors:
        response = browser.post(f"/api/sources/{sources[kind]}/backfills", json=selector, headers=headers)
        assert response.status_code == 201
        request_ids.append(response.json["id"])
        smoke.drain()
        status = browser.get(f"/api/backfills/{response.json['id']}").json
        assert status["state"] == "completed" and status["items_scanned"] >= accepted
        assert (status["items_accepted"], status["items_queued"]) == (accepted, queued)
        before = counts()
        tasks.run_backfill.run(response.json["id"])
        smoke.drain()
        assert counts() == before
    assert counts()[:5] == (1, 4, 54, 82, 3)

    # Scheduled scans discover remaining history and add shared memberships only.
    for source_id in sources.values():
        tasks.discover_source.run(source_id)
    smoke.drain()
    assert counts() == (1, 4, 60, 91, 3, 121)
    assert {row.status for row in ContentItem.query.all()} == {"completed"}, [
        (row.external_id, row.status, row.processing_stage, row.error_category)
        for row in ContentItem.query.all() if row.status != "completed"]
    assert {row.state for row in ProcessingJob.query.all()} == {"completed"}
    assert {row.attempts for row in ProcessingJob.query.all()} == {1}
    assert all(row.descriptor is None for row in DiscoveryCandidate.query.all())
    assert {row.state for row in DiscoveryRun.query.all()} == {"completed"}
    assert {row.run_type for row in DiscoveryRun.query.all()} == {"initial", "scheduled", "backfill"}
    origins = Counter(row.transcript_origin for row in ContentItem.query.all())
    assert origins == {"creator_caption": 28, "automatic_caption": 1, "amazon_transcribe": 31}
    for number, expected in ((29, ["inventory", "creator"]),
                              (28, ["inventory", "creator", "automatic"]),
                              (27, ["inventory", "creator", "automatic", "audio"])):
        assert [action for identity, action in smoke.downloads if identity == video_id(number)] == expected
    assert len(smoke.starts) == len(set(smoke.starts)) == 31
    assert sum(url == AUDIO_URL for method, url in smoke.http_calls) == 30
    assert len(smoke.puts) == len(set(smoke.puts)), "Duplicate processing wrote artifacts again"
    assert all(not path.exists() for path in smoke.paths)

    transcript_ids = []
    for row in ContentItem.query.order_by(ContentItem.id):
        transcript_ids.append(row.id)
        payload = json.loads(smoke.objects[row.segments_s3_key])
        assert payload["schema"] == "media-transcript-segments/v1"
        assert payload["origin"] == row.transcript_origin and payload["item_id"] == row.id
        assert payload["source_id"] in {member.source_id for member in row.sources}
        if row.id == shared.id:
            assert payload["source_id"] == sources["video"]
        assert payload["external_id"] == row.external_id and payload["platform"] == row.platform
        assert payload["language"] == row.language and payload["provider_version"] and payload["tool_version"]
        digest = payload.pop("transcript_sha256")
        assert digest == row.transcript_sha256 == sha256(canonical_json(payload).encode()).hexdigest()
        assert row.segments_sha256 == sha256(smoke.objects[row.segments_s3_key]).hexdigest()
        provider = smoke.objects[row.provider_transcript_s3_key]
        if row.transcript_origin == "amazon_transcribe":
            provider = canonical_json(json.loads(provider)).encode()
            assert row.transcription_job_name.startswith(f"media-{row.id}-{row.media_sha256[:32]}-")
        assert row.provider_transcript_sha256 == sha256(provider).hexdigest()
        lineage = {key: payload[key] for key in ("platform", "external_id", "item_id", "source_id",
                   "origin", "provider", "provider_version", "tool_version", "language")}
        for ordinal, segment in enumerate(payload["segments"]):
            value = {key: segment[key] for key in ("start", "end", "text", "citation")}
            expected_hash = sha256(canonical_json({**lineage, "ordinal": ordinal, **value}).encode()).hexdigest()
            assert segment["sha256"] == expected_hash
            assert segment["id"] == f"seg-{ordinal + 1:06d}-{expected_hash[:16]}"
            expected = (f"https://www.youtube.com/watch?v={row.external_id}&t="
                        if row.platform == "youtube" else row.canonical_url + "#t=")
            assert segment["citation"].startswith(expected)
        keys = [row.transcript_s3_key, row.segments_s3_key, row.provider_transcript_s3_key]
        if row.media_s3_key:
            keys.append(row.media_s3_key)
            assert row.media_sha256 == sha256(smoke.objects[row.media_s3_key]).hexdigest()
        assert all(key.startswith(f"items/{row.platform}/{row.external_id}/") and "://" not in key for key in keys)
        for path in (f"/items/{row.id}", f"/api/items/{row.id}", f"/items/{row.id}/transcript"):
            response = browser.get(path)
            assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
            assert not any(key in response.text for key in keys)
            assert "amazonaws.com" not in response.text and "s3://" not in response.text
            if path.endswith("/transcript"):
                assert payload["segments"][0]["text"] in response.text
            else:
                assert payload["segments"][0]["text"] not in response.text

    before = counts(), dict(smoke.objects), list(smoke.downloads), list(smoke.starts), list(smoke.puts)
    for run in DiscoveryRun.query.all():
        tasks.continue_discovery.run(run.id)
        tasks.finalize_discovery.run(run.id)
    for request_id in request_ids:
        tasks.run_backfill.run(request_id)
    for identity in transcript_ids:
        tasks.process_item.run(identity)
    for source_id in sources.values():
        tasks.discover_source.run(source_id)
    smoke.drain()
    assert (counts(), smoke.objects, smoke.downloads, smoke.starts, smoke.puts) == before
    assert {job.attempts for job in ProcessingJob.query.all()} == {1}
    assert PASSWORD not in caplog.text and smoke.app.config["YOUTUBE_API_KEY"] not in caplog.text
    assert "Synthetic private words" not in caplog.text
    assert not any(key in caplog.text for key in smoke.objects)
    # Owner scoping remains effective even though the database permits one admin.
    monkeypatch.setattr("web.routes.current_user", SimpleNamespace(id=999))
    assert browser.get(f"/items/{shared.id}/transcript").status_code == 404
    monkeypatch.setattr("web.routes.current_user", current_user)
    assert browser.post("/logout", headers=headers).status_code == 302
    for identity in transcript_ids:
        assert browser.get(f"/items/{identity}/transcript").status_code == 302
        assert browser.get(f"/api/items/{identity}").status_code == 401


def test_live_verification_is_opt_in_and_documented():
    root = Path(__file__).resolve().parents[1]
    assert (root / "tests/live/test_youtube_live.py").is_file(), "Missing opt-in live proof"
    for name in ("README.md", "docs/operations.md"):
        text = (root / name).read_text()
        assert "RUN_LIVE_YOUTUBE" in text, "Missing explicit live-test opt-in instructions"
        assert "tests/test_phase1_smoke.py" in text
