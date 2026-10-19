"""Real database transitions, with provider, storage and broker boundaries offline."""

from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from celery.exceptions import Retry
import pytest

from extensions import db
from ingestion_types import DownloadedMedia, IngestionError, TransientMediaLocator
from models import Admin, ContentItem, ContentSource, ProcessingJob, SourceItem, utc_now
from s3_utils import S3Manager, StorageTransientError, StorageIntegrityError
from transcripts import normalize_amazon_transcribe
from youtube_media import CaptionCandidate, CaptionInventory, YouTubeMediaError
import tasks


VTT = b"WEBVTT\n\n00:00:01.000 --> 00:00:03.500\nA useful caption.\n"
AUDIO = b"private audio bytes"
VIDEO = "dQw4w9WgXcQ"


class MemoryStorage:
    generate_s3_key = staticmethod(S3Manager.generate_s3_key)

    def __init__(self):
        self.objects, self.uploads = {}, []

    def upload_file(self, path, key, content_type):
        self.objects[key] = Path(path).read_bytes()
        self.uploads.append((key, content_type))
        return key

    def upload_binary_content(self, content, key, content_type):
        self.objects[key] = content
        self.uploads.append((key, content_type))
        return key

    def upload_text_content(self, content, key, content_type="text/plain"):
        return self.upload_binary_content(content.encode(), key, content_type)

    def get_bytes(self, key, *, max_bytes):
        if key not in self.objects:
            raise StorageIntegrityError("artifact unavailable")
        result = self.objects[key]
        assert len(result) <= max_bytes
        return result

    def verify_sha256(self, key, *, expected_sha256=None, max_bytes):
        result = sha256(self.get_bytes(key, max_bytes=max_bytes)).hexdigest()
        if expected_sha256 is not None and result != expected_sha256:
            raise StorageIntegrityError("artifact hash mismatch")
        return result


@pytest.fixture
def pipeline(app, monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("live network forbidden")))
    with app.app_context():
        db.create_all()
        owner = Admin(email="processing@example.com", password_hash="hash")
        db.session.add(owner)
        db.session.commit()
        storage = MemoryStorage()
        media, http = Mock(), Mock()
        sent, paths = [], []
        monkeypatch.setattr(tasks, "get_processing_storage", lambda: storage, raising=False)
        monkeypatch.setattr(tasks, "get_youtube_media_client", lambda: media, raising=False)
        monkeypatch.setattr(tasks, "get_processing_http_session", lambda: http, raising=False)

        def item(youtube=False, signed=False, memberships=1):
            row = ContentItem(admin_id=owner.id, platform="youtube" if youtube else "podcast",
                item_type="youtube_video" if youtube else "podcast_episode", external_id=VIDEO if youtube else "episode-123",
                canonical_url=f"https://www.youtube.com/watch?v={VIDEO}" if youtube else "https://example.com/episode",
                remote_media_url=None if youtube or signed else "https://example.com/audio.mp3", title="An item")
            db.session.add(row)
            db.session.flush()
            for number in range(memberships):
                source = ContentSource(admin_id=owner.id, source_type="youtube_channel" if youtube else "podcast_rss",
                    external_id=f"source-{number}", canonical_url=f"https://example.com/feed-{number}", name="Source")
                db.session.add(source)
                db.session.flush()
                db.session.add(SourceItem(source_id=source.id, item_id=row.id))
            stage = "youtube_captions" if youtube else "podcast_audio"
            db.session.add(ProcessingJob(item_id=row.id, stage=stage, celery_task_id=f"process-item-{row.id}-{stage}",
                error_category="dispatch_pending"))
            db.session.commit()
            return row

        def download(locator, destination, settings, session):
            assert settings.MAX_AUDIO_BYTES == app.config["MAX_AUDIO_BYTES"]
            assert session is http
            paths.append(Path(destination))
            Path(destination).write_bytes(AUDIO)
            return DownloadedMedia(Path(destination), len(AUDIO), "audio/mpeg", sha256(AUDIO).hexdigest())

        def caption(url, candidate, directory):
            path = Path(directory) / f"{candidate.kind}.vtt"
            path.write_bytes(VTT)
            paths.append(path)
            return path

        def extract(url, directory):
            return download(url, Path(directory) / "media.mp3", SimpleNamespace(**app.config), http)

        def amazon(key, **kwargs):
            assert storage.objects[key] == AUDIO
            raw = {"jobName": f"media-{kwargs['item_id']}-{kwargs['audio_sha256'][:32]}-v1",
                "results": {"items": [{"type": "pronunciation", "start_time": "1", "end_time": "2",
                    "alternatives": [{"content": "Hello"}]}]}}
            transcript = normalize_amazon_transcribe(raw, platform=kwargs["platform"], external_id=kwargs["external_id"],
                item_id=kwargs["item_id"], source_id=kwargs["source_id"], language="en-US", canonical_url=kwargs["canonical_url"])
            digest = transcript.transcript_sha256
            base = f"items/{kwargs['platform']}/{kwargs['external_id']}"
            provider_key = f"{base}/provider/{raw['jobName']}.json"
            storage.objects[provider_key] = json.dumps(raw).encode()
            storage.objects[f"{base}/transcripts/{digest}.json"] = transcript.to_json().encode()
            storage.objects[f"{base}/transcripts/{digest}.txt"] = transcript.to_text().encode()
            return dict(transcript_key=f"{base}/transcripts/{digest}.txt", segments_key=f"{base}/transcripts/{digest}.json",
                provider_result_key=provider_key, audio_sha256=kwargs["audio_sha256"], source_sha256=transcript.source_sha256,
                transcript_sha256=digest, language="en-US", duration=2.0, provider="amazon_transcribe", job_name=raw["jobName"])

        def send(name, identity, **options):
            assert type(identity) is int
            next_stage = {"acquire_podcast_audio": "podcast_audio", "acquire_youtube_transcript": "youtube_captions",
                "transcribe_item_audio": "transcribe", "normalize_item_transcript": "normalize"}.get(name)
            if name == "acquire_youtube_transcript":
                next_stage = db.session.get(ContentItem, identity).processing_stage
            if next_stage:
                job = ProcessingJob.query.filter_by(item_id=identity, stage=next_stage).one()
                assert job.celery_task_id == options["task_id"]
                assert job.error_category == "dispatch_pending"
                # A fresh connection observes the persisted outbox before send.
                import sqlalchemy as sa
                with db.engine.connect() as connection:
                    assert connection.execute(sa.select(ProcessingJob.state).where(ProcessingJob.id == job.id)).scalar_one() == job.state
            sent.append((name, identity, options))

        import episode_fetcher, transcribe
        monkeypatch.setattr(episode_fetcher, "download_podcast_audio", Mock(side_effect=download))
        monkeypatch.setattr(episode_fetcher, "reacquire_podcast_media", Mock(return_value=TransientMediaLocator("https://example.com/signed?token=SECRET")))
        monkeypatch.setattr(transcribe, "transcribe_audio_from_s3", Mock(side_effect=amazon))
        monkeypatch.setattr(tasks, "dispatch_task", send)
        media.inventory.return_value = CaptionInventory(VIDEO, (CaptionCandidate("creator", "en"), CaptionCandidate("automatic", "en")))
        media.download_caption.side_effect = caption
        media.extract_audio.side_effect = extract
        yield SimpleNamespace(item=item, storage=storage, media=media, http=http, sent=sent, paths=paths,
            download=episode_fetcher.download_podcast_audio, reacquire=episode_fetcher.reacquire_podcast_media,
            amazon=transcribe.transcribe_audio_from_s3, caption=caption)
        db.session.remove()
        db.drop_all()


def drain(pipeline):
    while pipeline.sent:
        name, identity, options = pipeline.sent.pop(0)
        getattr(tasks, name).run(identity)


def test_terminal_stage_atomically_creates_revision_and_releases_lock_before_publish(pipeline, app, monkeypatch):
    from models import ClassificationRevision
    import sqlalchemy as sa
    row = pipeline.item()
    app.config["CLASSIFICATION_ENABLED"] = True
    item_id = row.id
    observed = []
    def send(name, *, args, **options):
        assert name == "tasks.classify_item" and args[1:] == ["prepare", None]
        with tasks._item_lock(item_id) as acquired:
            assert acquired
        assert not db.session().in_transaction()
        with db.engine.connect() as connection:
            assert connection.scalar(sa.select(ContentItem.status).where(ContentItem.id == item_id)) == "completed"
            assert connection.scalar(sa.select(ProcessingJob.state).where(ProcessingJob.item_id == item_id,
                ProcessingJob.stage == "transcribe")) == "completed"
            assert connection.scalar(sa.select(ClassificationRevision.error_category).where(
                ClassificationRevision.id == args[0])) == "dispatch_pending"
        observed.append(args)
    monkeypatch.setattr(tasks.celery_app, "send_task", send)
    tasks.process_item.run(row.id)
    drain(pipeline)
    assert len(observed) == 1 and ClassificationRevision.query.count() == 1
    tasks.process_item.run(row.id)
    assert ClassificationRevision.query.count() == 1


@pytest.mark.parametrize("failure_stage", ["prepare", "insert"])
def test_classification_hook_failure_never_fails_valid_transcript(pipeline, monkeypatch, failure_stage):
    row = pipeline.item()
    import classification_service
    method = "prepare_revision_request" if failure_stage == "prepare" else "ensure_revision"
    def fail(*args, **kwargs):
        raise RuntimeError("private classification error")
    monkeypatch.setattr(classification_service.ClassificationService, method, fail)
    tasks.process_item.run(row.id)
    drain(pipeline)
    assert row.status == "completed" and row.error_category is None
    assert ProcessingJob.query.filter_by(stage="transcribe").one().state == "completed"


def test_disabled_completion_persists_revision_before_item_completed(pipeline, monkeypatch):
    from models import ClassificationRevision
    import classification_service
    row = pipeline.item()
    original = classification_service.ClassificationService.ensure_revision
    observed = []
    def insert(service, identity, reason, **kwargs):
        assert kwargs["commit"] is False
        assert db.session.get(ContentItem, identity).status == "transcribing"
        assert ProcessingJob.query.filter_by(item_id=identity, stage="transcribe").one().state == "running"
        result = original(service, identity, reason, **kwargs)
        observed.append(result)
        return result
    monkeypatch.setattr(classification_service.ClassificationService, "ensure_revision", insert)
    tasks.process_item.run(row.id)
    drain(pipeline)
    assert len(observed) == 1
    revision = ClassificationRevision.query.one()
    assert revision.state == "pending" and revision.error_category == "dispatch_pending"


def test_classification_request_preparation_has_committed_canonical_refs_without_transaction(pipeline, monkeypatch):
    import classification_service
    import sqlalchemy as sa
    row = pipeline.item()
    original = classification_service.ClassificationService.prepare_revision_request
    observed = []
    def prepare(service, identity, **kwargs):
        observed.append(not db.session().in_transaction())
        with db.engine.connect() as connection:
            state = connection.execute(sa.select(ContentItem.status, ContentItem.segments_s3_key).where(
                ContentItem.id == identity)).one()
            assert state.status == "transcribing" and state.segments_s3_key
            assert connection.scalar(sa.select(ProcessingJob.state).where(ProcessingJob.item_id == identity,
                ProcessingJob.stage == "transcribe")) == "running"
        return original(service, identity, **kwargs)
    monkeypatch.setattr(classification_service.ClassificationService, "prepare_revision_request", prepare)
    tasks.process_item.run(row.id)
    drain(pipeline)
    assert observed == [True]


def test_podcast_bounded_hashed_private_audio_then_amazon_and_replay(pipeline):
    row = pipeline.item(signed=True)
    function = getattr(tasks, "process_item", None)
    assert callable(function), "process_item pipeline missing"
    function.run(row.id)
    db.session.refresh(row)
    assert row.media_sha256 == sha256(AUDIO).hexdigest()
    assert row.media_s3_key == f"items/podcast/episode-123/audio/{row.media_sha256}.mp3"
    assert row.last_successful_stage == "podcast_audio"
    assert ProcessingJob.query.filter_by(stage="podcast_audio").one().state == "completed"
    assert all(not path.exists() for path in pipeline.paths)
    drain(pipeline)
    assert row.status == "completed" and row.transcript_origin == "amazon_transcribe"
    assert row.provider_transcript_sha256 and row.transcript_sha256 and row.segments_sha256
    assert row.transcription_job_name and row.language == "en-US"
    function.run(row.id)
    tasks.acquire_podcast_audio.run(row.id)
    pipeline.download.assert_called_once()
    pipeline.reacquire.assert_called_once()
    pipeline.amazon.assert_called_once()


def test_youtube_creator_priority_persists_full_lineage_and_citations(pipeline):
    row = pipeline.item(youtube=True, memberships=2)
    function = getattr(tasks, "process_item", None)
    assert callable(function), "process_item pipeline missing"
    function.run(row.id)
    drain(pipeline)
    assert row.status == "completed" and row.transcript_origin == "creator_caption"
    assert row.transcription_provider == "yt-dlp" and row.language == "en"
    assert row.provider_transcript_sha256 == sha256(VTT).hexdigest()
    payload = json.loads(pipeline.storage.objects[row.segments_s3_key])
    assert payload["item_id"] == row.id and payload["source_id"] == min(m.source_id for m in row.sources)
    assert payload["platform"] == "youtube" and payload["external_id"] == VIDEO
    assert payload["provider_version"] == "2026.08.19" and payload["tool_version"]
    assert payload["origin"] == "creator_caption" and payload["source_sha256"] == sha256(VTT).hexdigest()
    assert payload["segments"][0]["citation"] == f"https://www.youtube.com/watch?v={VIDEO}&t=1s"
    assert payload["segments"][0]["sha256"] and payload["transcript_sha256"]
    assert row.segments_sha256 == sha256(pipeline.storage.objects[row.segments_s3_key]).hexdigest()
    pipeline.media.inventory.assert_called_once()
    pipeline.media.download_caption.assert_called_once()
    pipeline.media.extract_audio.assert_not_called()
    pipeline.amazon.assert_not_called()
    assert ProcessingJob.query.count() == 2
    assert all(not path.exists() for path in pipeline.paths)
    function.run(row.id)
    pipeline.media.inventory.assert_called_once()


@pytest.mark.parametrize("automatic", [True, False])
def test_permanent_caption_failure_uses_automatic_then_audio(pipeline, automatic):
    row = pipeline.item(youtube=True)
    def candidate(url, selected, directory):
        if selected.kind == "creator" or not automatic:
            raise YouTubeMediaError("invalid_output")
        return pipeline.caption(url, selected, directory)
    pipeline.media.download_caption.side_effect = candidate
    tasks.process_item.run(row.id)
    drain(pipeline)
    assert row.status == "completed"
    assert row.transcript_origin == ("automatic_caption" if automatic else "amazon_transcribe")
    assert pipeline.media.download_caption.call_count == 2
    assert pipeline.media.extract_audio.call_count == (0 if automatic else 1)
    assert pipeline.amazon.call_count == (0 if automatic else 1)
    pipeline.media.inventory.assert_called_once()
    assert all(not path.exists() for path in pipeline.paths)


@pytest.mark.parametrize("category,status,retries", [
    ("timeout", "acquiring", True), ("download_failed", "acquiring", True),
    ("unavailable", "unavailable", False), ("invalid_url", "failed", False),
])
def test_provider_failure_mapping_and_redaction(pipeline, monkeypatch, category, status, retries):
    row = pipeline.item(youtube=True)
    pipeline.media.inventory.side_effect = YouTubeMediaError(category)
    retry = Mock(side_effect=Retry("safe retry"))
    monkeypatch.setattr(tasks.process_item, "retry", retry)
    if retries:
        with pytest.raises(Retry):
            tasks.process_item.run(row.id)
        assert retry.call_args.kwargs["countdown"] >= 1
        assert retry.call_args.kwargs["args"] == [row.id]
        assert retry.call_args.kwargs["task_id"] == f"process-item-{row.id}-youtube_captions"
    else:
        tasks.process_item.run(row.id)
        retry.assert_not_called()
    assert row.status == status
    assert row.error_category in {"provider_transient", "unavailable", "invalid_input"}
    assert "http" not in (row.error_category or "")


def test_transient_creator_failure_retries_without_downgrade_and_exhausts_deferred(pipeline, monkeypatch):
    row = pipeline.item(youtube=True)
    pipeline.media.download_caption.side_effect = YouTubeMediaError("download_failed")
    retry = Mock(side_effect=Retry("safe retry"))
    monkeypatch.setattr(tasks.process_item, "retry", retry)
    for number in range(5):
        with pytest.raises(Retry):
            tasks.process_item.run(row.id)
        assert pipeline.media.download_caption.call_count == number + 1
        assert ProcessingJob.query.one().state == "retrying"
    tasks.process_item.run(row.id)
    assert row.status == "deferred" and ProcessingJob.query.one().state == "failed"
    pipeline.media.extract_audio.assert_not_called()
    pipeline.amazon.assert_not_called()


def test_podcast_http_failure_retries_without_storing_secrets_and_cleans_temp(pipeline, monkeypatch):
    row = pipeline.item()
    def failure(locator, destination, settings, session):
        pipeline.paths.append(Path(destination).parent)
        raise IngestionError("http_failed")
    pipeline.download.side_effect = failure
    monkeypatch.setattr(tasks.process_item, "retry", Mock(side_effect=Retry("safe")))
    with pytest.raises(Retry):
        tasks.process_item.run(row.id)
    assert row.error_category == "http_transient"
    assert all(not path.exists() for path in pipeline.paths)
    assert row.media_s3_key is None and row.transcript_s3_key is None


def test_caption_artifact_commit_survives_normalization_retry_without_inventory(pipeline, monkeypatch):
    row = pipeline.item(youtube=True)
    tasks.process_item.run(row.id)
    assert row.provider_transcript_s3_key and row.provider_transcript_sha256
    original = pipeline.storage.upload_text_content
    pipeline.storage.upload_text_content = Mock(side_effect=StorageTransientError("SECRET https://private?token=x"))
    monkeypatch.setattr(tasks.normalize_item_transcript, "retry", Mock(side_effect=Retry("safe")))
    with pytest.raises(Retry):
        tasks.normalize_item_transcript.run(row.id)
    assert row.error_category == "storage_transient"
    pipeline.storage.upload_text_content = original
    tasks.normalize_item_transcript.run(row.id)
    assert row.status == "completed"
    pipeline.media.inventory.assert_called_once()
    pipeline.media.download_caption.assert_called_once()


def test_duplicate_delivery_cannot_enter_an_active_stage(pipeline):
    row = pipeline.item()
    original = pipeline.download.side_effect
    def nested(*args):
        tasks.process_item.run(row.id)
        assert ProcessingJob.query.one().attempts == 1
        return original(*args)
    pipeline.download.side_effect = nested
    tasks.process_item.run(row.id)
    drain(pipeline)
    pipeline.download.assert_called_once()
    assert row.status == "completed"


def test_lost_worker_running_stage_recovers_once_lock_is_released(pipeline):
    row = pipeline.item()
    job = ProcessingJob.query.one()
    job.state, job.started_at, job.attempts = "running", utc_now() - timedelta(hours=3), 1
    db.session.commit()
    tasks.process_item.run(row.id)
    drain(pipeline)
    assert job.attempts == 2 and row.status == "completed"
    pipeline.download.assert_called_once()


def test_unknown_broker_outcome_replays_committed_next_stage(pipeline, monkeypatch):
    row = pipeline.item()
    real_send = tasks.dispatch_task
    monkeypatch.setattr(tasks, "dispatch_task", Mock(side_effect=OSError("SECRET broker token")))
    tasks.process_item.run(row.id)
    job = ProcessingJob.query.filter_by(stage="transcribe").one()
    assert job.state == "pending" and job.error_category == "dispatch_pending"
    assert row.media_s3_key and row.last_successful_stage == "podcast_audio"
    monkeypatch.setattr(tasks, "dispatch_task", real_send)
    tasks.process_item.run(row.id)
    drain(pipeline)
    assert row.status == "completed"
    pipeline.download.assert_called_once()


def test_fast_worker_state_is_not_overwritten_by_dispatch_sender(pipeline, monkeypatch):
    row = pipeline.item(youtube=True)
    def fast(name, identity, **options):
        getattr(tasks, name).run(identity)
    monkeypatch.setattr(tasks, "dispatch_task", fast)
    tasks.process_item.run(row.id)
    assert row.status == "completed"
    assert all(job.state == "completed" and job.error_category is None for job in ProcessingJob.query.all())


def test_incomplete_audio_tuple_reacquires_and_valid_tuple_never_reacquires(pipeline):
    row = pipeline.item()
    row.media_s3_key = "items/podcast/episode-123/audio/unknown.mp3"
    db.session.commit()
    tasks.process_item.run(row.id)
    drain(pipeline)
    assert row.status == "completed" and pipeline.download.call_count == 1
    row.status, row.segments_sha256 = "transcribing", None
    db.session.commit()
    tasks.process_item.run(row.id)
    drain(pipeline)
    assert row.status == "completed" and row.segments_sha256
    assert pipeline.download.call_count == 1


@pytest.mark.parametrize("value", [True, 0, "1", None])
def test_pipeline_tasks_reject_non_integer_ids_without_opening_context(monkeypatch, value):
    context = Mock(side_effect=AssertionError("must validate before application creation"))
    monkeypatch.setattr(tasks, "application_context", context)
    for name in ("process_item", "acquire_podcast_audio", "acquire_youtube_transcript", "transcribe_item_audio", "normalize_item_transcript"):
        with pytest.raises(ValueError):
            getattr(tasks, name).run(value)
    context.assert_not_called()


def test_malformed_creator_vtt_advances_to_automatic(pipeline):
    row = pipeline.item(youtube=True)
    def candidate(url, selected, directory):
        path = pipeline.caption(url, selected, directory)
        if selected.kind == "creator":
            path.write_bytes(b"not a valid VTT caption")
        return path
    pipeline.media.download_caption.side_effect = candidate
    tasks.process_item.run(row.id)
    drain(pipeline)
    assert row.status == "completed" and row.transcript_origin == "automatic_caption"
    assert pipeline.media.download_caption.call_count == 2
    pipeline.media.extract_audio.assert_not_called()


@pytest.mark.parametrize("damage", ["hash", "oversize", "format"])
def test_invalid_media_is_failed_and_temp_is_cleaned(pipeline, damage, app):
    row = pipeline.item()
    original = pipeline.download.side_effect
    def invalid(*args):
        result = original(*args)
        if damage == "hash":
            return DownloadedMedia(result.path, result.size_bytes, result.content_type, "f" * 64)
        if damage == "oversize":
            app.config["MAX_AUDIO_BYTES"] = len(AUDIO) - 1
            result.path.write_bytes(AUDIO * 2)
            return DownloadedMedia(result.path, len(AUDIO) * 2, result.content_type, sha256(AUDIO * 2).hexdigest())
        return DownloadedMedia(result.path, result.size_bytes, "image/jpeg", result.sha256)
    if damage == "oversize":
        app.config["MAX_AUDIO_BYTES"] = len(AUDIO) - 1
    pipeline.download.side_effect = invalid
    tasks.process_item.run(row.id)
    assert row.status == "failed" and row.error_category == "invalid_input"
    assert row.media_s3_key is None and not pipeline.storage.uploads
    assert all(not path.exists() for path in pipeline.paths)


@pytest.mark.parametrize("damage", ["audio_hash", "job", "foreign_segments", "provider", "text", "source_hash", "segments_identity"])
def test_mismatched_amazon_metadata_cannot_complete(pipeline, damage):
    row = pipeline.item()
    tasks.process_item.run(row.id)
    original = pipeline.amazon.side_effect
    def invalid(key, **kwargs):
        result = original(key, **kwargs)
        if damage == "audio_hash":
            result["audio_sha256"] = "f" * 64
        elif damage == "job":
            result["job_name"] = "media-wrong"
        elif damage == "foreign_segments":
            foreign = result["segments_key"].replace("episode-123", "another-episode")
            pipeline.storage.objects[foreign] = pipeline.storage.objects[result["segments_key"]]
            result["segments_key"] = foreign
        elif damage == "provider":
            pipeline.storage.objects[result["provider_result_key"]] = b'{"jobName":"wrong","results":{}}'
        elif damage == "text":
            pipeline.storage.objects[result["transcript_key"]] = b"corrupt transcript"
        elif damage == "source_hash":
            result["source_sha256"] = "f" * 64
        else:
            payload = json.loads(pipeline.storage.objects[result["segments_key"]])
            payload["item_id"] = 999
            pipeline.storage.objects[result["segments_key"]] = json.dumps(payload).encode()
        return result
    pipeline.amazon.side_effect = invalid
    drain(pipeline)
    assert row.status == "failed" and row.error_category == "invalid_input"
    assert row.transcript_s3_key is None and row.segments_s3_key is None


def test_caption_unavailable_does_not_fall_back_to_nonpublic_audio(pipeline):
    row = pipeline.item(youtube=True)
    pipeline.media.download_caption.side_effect = YouTubeMediaError("unavailable")
    tasks.process_item.run(row.id)
    assert row.status == "unavailable" and row.error_category == "unavailable"
    pipeline.media.download_caption.assert_called_once()
    pipeline.media.extract_audio.assert_not_called()


def test_transcription_transient_retries_reusing_valid_audio(pipeline, monkeypatch):
    from transcribe import TranscriptionTransientError
    row = pipeline.item()
    tasks.process_item.run(row.id)
    original = pipeline.amazon.side_effect
    pipeline.amazon.side_effect = TranscriptionTransientError("SECRET https://signed?token=x")
    retry = Mock(side_effect=Retry("safe"))
    monkeypatch.setattr(tasks.transcribe_item_audio, "retry", retry)
    with pytest.raises(Retry):
        tasks.transcribe_item_audio.run(row.id)
    assert row.error_category == "transcription_transient"
    assert str(retry.call_args.kwargs["exc"]) == "transcription_transient"
    pipeline.amazon.side_effect = original
    tasks.process_item.run(row.id)
    drain(pipeline)
    assert row.status == "completed"
    pipeline.download.assert_called_once()


def test_disabled_membership_never_acquires_media(pipeline):
    row = pipeline.item()
    row.sources[0].source.enabled = False
    db.session.commit()
    tasks.process_item.run(row.id)
    assert row.status == "unavailable"
    pipeline.download.assert_not_called()


def test_stage_explicit_task_uses_transcription_queue():
    assert tasks.transcribe_item_audio._get_exec_options().get("queue") == "transcription"


def test_item_lock_excludes_other_process_and_is_released_on_worker_exit(pipeline):
    import multiprocessing
    row = pipeline.item()
    context = multiprocessing.get_context("fork")
    ready, release = context.Event(), context.Event()
    def hold_lock():
        with tasks._item_lock(row.id) as acquired:
            assert acquired
            ready.set()
            assert release.wait(10)
    worker = context.Process(target=hold_lock)
    worker.start()
    try:
        assert ready.wait(5)
        with tasks._item_lock(row.id) as acquired:
            assert not acquired
    finally:
        release.set()
        worker.join(5)
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
    assert worker.exitcode == 0
    with tasks._item_lock(row.id) as acquired:
        assert acquired


@pytest.mark.parametrize("acquired,crash", [(True, False), (True, True), (False, False)])
def test_postgres_lifetime_lock_releases_connection_and_unlocks_across_commits(monkeypatch, acquired, crash):
    import extensions
    connection, engine = Mock(), Mock()
    engine.dialect.name = "postgresql"
    engine.connect.return_value = connection
    connection.execute.return_value.scalar.return_value = acquired
    monkeypatch.setattr(extensions, "db", SimpleNamespace(engine=engine))
    def operation():
        with tasks._item_lock(17) as result:
            assert result is acquired
            connection.commit()
            if crash:
                raise RuntimeError("worker failed")
    if crash:
        with pytest.raises(RuntimeError):
            operation()
    else:
        operation()
    calls = connection.execute.call_args_list
    assert "pg_try_advisory_lock" in str(calls[0].args[0])
    assert len(calls) == (2 if acquired else 1)
    if acquired:
        assert "pg_advisory_unlock" in str(calls[1].args[0])
        assert calls[0].args[1] == calls[1].args[1]
    connection.close.assert_called_once()


def test_terminated_worker_releases_lock_and_redelivery_finishes_stage(pipeline):
    row = pipeline.item()
    original = pipeline.download.side_effect
    pipeline.download.side_effect = SystemExit("worker terminated")
    with pytest.raises(SystemExit):
        tasks.process_item.run(row.id)
    assert ProcessingJob.query.one().state == "running"
    pipeline.download.side_effect = original
    tasks.process_item.run(row.id)
    drain(pipeline)
    assert row.status == "completed" and pipeline.download.call_count == 2


def test_temporary_public_video_state_retries_instead_of_becoming_unavailable(pipeline, monkeypatch):
    row = pipeline.item(youtube=True)
    pipeline.media.inventory.side_effect = YouTubeMediaError("not_ready")
    monkeypatch.setattr(tasks.process_item, "retry", Mock(side_effect=Retry("safe")))
    with pytest.raises(Retry):
        tasks.process_item.run(row.id)
    assert row.status == "acquiring" and row.error_category == "provider_transient"


def test_celery_soft_deadline_retries_safely(pipeline, monkeypatch):
    from billiard.exceptions import SoftTimeLimitExceeded
    row = pipeline.item(youtube=True)
    pipeline.media.inventory.side_effect = SoftTimeLimitExceeded("SECRET")
    monkeypatch.setattr(tasks.process_item, "retry", Mock(side_effect=Retry("safe")))
    with pytest.raises(Retry):
        tasks.process_item.run(row.id)
    assert row.status == "acquiring" and row.error_category == "worker_timeout"


@pytest.mark.parametrize("status,category,retryable,unavailable", [
    (400, "invalid_input", False, False), (403, "unavailable", False, True),
    (404, "unavailable", False, True), (429, "http_transient", True, False),
    (503, "http_transient", True, False),
])
def test_http_status_failure_categories_are_safe(status, category, retryable, unavailable):
    from requests import HTTPError, Response
    response = Response()
    response.status_code = status
    error = HTTPError("SECRET https://private?token=x", response=response)
    assert tasks._processing_failure(error) == (category, retryable, unavailable)


def test_repaired_provider_tuple_cannot_reuse_stale_canonical_lineage(pipeline):
    row = pipeline.item(youtube=True)
    tasks.process_item.run(row.id)
    drain(pipeline)
    previous_transcript = row.transcript_sha256
    row.provider_transcript_sha256 = None
    row.status = "acquiring"
    db.session.commit()
    def replacement(url, candidate, directory):
        path = pipeline.caption(url, candidate, directory)
        path.write_bytes(VTT.replace(b"A useful caption.", b"A replacement caption."))
        return path
    pipeline.media.download_caption.side_effect = replacement
    tasks.process_item.run(row.id)
    drain(pipeline)
    assert row.status == "completed"
    assert row.transcript_sha256 != previous_transcript
    canonical = json.loads(pipeline.storage.objects[row.segments_s3_key])
    assert canonical["source_sha256"] == row.provider_transcript_sha256
    assert canonical["segments"][0]["text"] == "A replacement caption."


def test_completed_transcript_with_mismatched_segments_hash_is_repaired_without_acquisition(pipeline):
    row = pipeline.item(youtube=True)
    tasks.process_item.run(row.id)
    drain(pipeline)
    digest = row.segments_sha256
    row.segments_sha256 = "f" * 64
    db.session.commit()
    tasks.process_item.run(row.id)
    drain(pipeline)
    assert row.status == "completed" and row.segments_sha256 == digest
    pipeline.media.inventory.assert_called_once()
    pipeline.media.download_caption.assert_called_once()


def test_replayed_acquisition_dispatches_existing_audio_to_transcription_queue(pipeline):
    row = pipeline.item()
    tasks.process_item.run(row.id)
    assert row.media_s3_key
    tasks.acquire_podcast_audio.run(row.id)
    pipeline.download.assert_called_once()
    pipeline.amazon.assert_not_called()
    assert all(name == "transcribe_item_audio" and options["queue"] == "transcription"
        for name, _, options in pipeline.sent)
    drain(pipeline)
    assert row.status == "completed"


@pytest.mark.parametrize("damage", ["missing", "corrupt", "incomplete"])
@pytest.mark.parametrize("canonical_complete", [True, False])
def test_creator_provider_repair_keeps_caption_priority_over_reusable_audio(pipeline, damage, canonical_complete):
    row = pipeline.item(youtube=True)
    tasks.process_item.run(row.id)
    drain(pipeline)
    old_key = row.provider_transcript_s3_key
    row.media_sha256 = sha256(AUDIO).hexdigest()
    row.media_s3_key = f"items/youtube/{VIDEO}/audio/{row.media_sha256}.mp3"
    pipeline.storage.objects[row.media_s3_key] = AUDIO
    if damage == "missing":
        del pipeline.storage.objects[old_key]
    elif damage == "corrupt":
        pipeline.storage.objects[old_key] = b"corrupt VTT bytes"
    else:
        row.provider_transcript_sha256 = None
    if not canonical_complete:
        row.transcript_s3_key = None
    db.session.commit()
    tasks.process_item.run(row.id)
    drain(pipeline)
    assert row.status == "completed" and row.transcript_origin == "creator_caption"
    assert pipeline.media.inventory.call_count == pipeline.media.download_caption.call_count == 2
    assert pipeline.storage.objects[row.provider_transcript_s3_key] == VTT
    pipeline.media.extract_audio.assert_not_called()
    pipeline.amazon.assert_not_called()


@pytest.mark.parametrize("value", [None, 4, "scalar", []])
def test_scalar_canonical_payload_repairs_from_provider_without_acquisition(pipeline, value):
    row = pipeline.item(youtube=True)
    tasks.process_item.run(row.id)
    drain(pipeline)
    payload = json.dumps(value).encode()
    pipeline.storage.objects[row.segments_s3_key] = payload
    row.segments_sha256 = sha256(payload).hexdigest()
    db.session.commit()
    tasks.process_item.run(row.id)
    drain(pipeline)
    assert row.status == "completed"
    assert isinstance(json.loads(pipeline.storage.objects[row.segments_s3_key]), dict)
    pipeline.media.inventory.assert_called_once()
    pipeline.media.download_caption.assert_called_once()


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
@pytest.mark.parametrize("completed", [True, False])
def test_audio_reuse_checks_s3_bytes_and_repairs_missing_or_mismatched_object(pipeline, damage, completed):
    row = pipeline.item()
    tasks.process_item.run(row.id)
    if completed:
        drain(pipeline)
    old_key = row.media_s3_key
    if damage == "missing":
        del pipeline.storage.objects[old_key]
    else:
        pipeline.storage.objects[old_key] = b"corrupt audio content"
    tasks.process_item.run(row.id)
    drain(pipeline)
    assert row.status == "completed"
    assert pipeline.download.call_count == 2
    assert pipeline.storage.objects[row.media_s3_key] == AUDIO
    assert pipeline.amazon.call_count == (2 if completed else 1)


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_completed_amazon_provider_output_must_exist_and_match_source_hash(pipeline, damage):
    row = pipeline.item()
    tasks.process_item.run(row.id)
    drain(pipeline)
    provider_key = row.provider_transcript_s3_key
    if damage == "missing":
        del pipeline.storage.objects[provider_key]
    else:
        pipeline.storage.objects[provider_key] = b'{"jobName":"wrong","results":{}}'
    tasks.process_item.run(row.id)
    drain(pipeline)
    assert row.status == "completed" and pipeline.amazon.call_count == 2
    pipeline.download.assert_called_once()
    assert json.loads(pipeline.storage.objects[row.provider_transcript_s3_key])["jobName"] == row.transcription_job_name


def test_irreparable_deterministic_amazon_output_is_safe_terminal_failure(pipeline):
    from transcribe import TranscriptionPermanentError
    row = pipeline.item()
    tasks.process_item.run(row.id)
    drain(pipeline)
    del pipeline.storage.objects[row.provider_transcript_s3_key]
    pipeline.amazon.side_effect = TranscriptionPermanentError("invalid transcription artifact")
    tasks.process_item.run(row.id)
    drain(pipeline)
    assert row.status == "failed" and row.error_category == "invalid_input"
    assert pipeline.amazon.call_count == 2
    pipeline.download.assert_called_once()


def test_valid_creator_provider_with_invalid_canonical_and_audio_reuses_provider(pipeline):
    row = pipeline.item(youtube=True)
    tasks.process_item.run(row.id)
    drain(pipeline)
    row.media_sha256 = sha256(AUDIO).hexdigest()
    row.media_s3_key = f"items/youtube/{VIDEO}/audio/{row.media_sha256}.mp3"
    pipeline.storage.objects[row.media_s3_key] = AUDIO
    row.segments_sha256 = "f" * 64
    db.session.commit()
    tasks.process_item.run(row.id)
    drain(pipeline)
    assert row.status == "completed" and row.transcript_origin == "creator_caption"
    pipeline.media.inventory.assert_called_once()
    pipeline.media.download_caption.assert_called_once()
    pipeline.amazon.assert_not_called()


def test_caption_provider_lost_between_verification_and_normalization_returns_to_acquisition(pipeline):
    row = pipeline.item(youtube=True)
    tasks.process_item.run(row.id)
    original = pipeline.storage.get_bytes
    count = 0
    def lost(key, **kwargs):
        nonlocal count
        if key == row.provider_transcript_s3_key:
            count += 1
            if count == 2:
                raise StorageIntegrityError("artifact unavailable")
        return original(key, **kwargs)
    pipeline.storage.get_bytes = lost
    drain(pipeline)
    assert row.status == "completed" and row.transcript_origin == "creator_caption"
    assert pipeline.media.inventory.call_count == 2
    pipeline.amazon.assert_not_called()


def test_repair_decision_clears_completed_status_before_publishing_acquisition(pipeline):
    row = pipeline.item(youtube=True)
    tasks.process_item.run(row.id)
    drain(pipeline)
    del pipeline.storage.objects[row.provider_transcript_s3_key]
    tasks.normalize_item_transcript.run(row.id)
    assert row.status == "acquiring"
    assert row.provider_transcript_s3_key is None and row.transcript_s3_key is None
    assert pipeline.sent[0][0] == "acquire_youtube_transcript"
    drain(pipeline)
    assert row.status == "completed"


def test_invalid_caption_provider_with_no_current_captions_reuses_audio_after_inventory(pipeline):
    row = pipeline.item(youtube=True)
    tasks.process_item.run(row.id)
    drain(pipeline)
    del pipeline.storage.objects[row.provider_transcript_s3_key]
    row.media_sha256 = sha256(AUDIO).hexdigest()
    row.media_s3_key = f"items/youtube/{VIDEO}/audio/{row.media_sha256}.mp3"
    pipeline.storage.objects[row.media_s3_key] = AUDIO
    db.session.commit()
    pipeline.media.inventory.return_value = CaptionInventory(VIDEO, ())
    tasks.process_item.run(row.id)
    drain(pipeline)
    assert row.status == "completed" and row.transcript_origin == "amazon_transcribe"
    assert pipeline.media.inventory.call_count == 2
    pipeline.media.extract_audio.assert_not_called()
    pipeline.amazon.assert_called_once()


def test_caption_provider_verification_transient_retries_without_reacquisition(pipeline, monkeypatch):
    row = pipeline.item(youtube=True)
    tasks.process_item.run(row.id)
    drain(pipeline)
    key = row.provider_transcript_s3_key
    original = pipeline.storage.get_bytes
    def temporary(artifact, **kwargs):
        if artifact == key:
            raise StorageTransientError("SECRET")
        return original(artifact, **kwargs)
    pipeline.storage.get_bytes = temporary
    monkeypatch.setattr(tasks.normalize_item_transcript, "retry", Mock(side_effect=Retry("safe")))
    with pytest.raises(Retry):
        tasks.normalize_item_transcript.run(row.id)
    assert row.error_category == "storage_transient" and row.provider_transcript_s3_key == key
    pipeline.media.inventory.assert_called_once()
    pipeline.amazon.assert_not_called()


def test_preexisting_audio_without_caption_inventory_does_not_bypass_creator(pipeline):
    row = pipeline.item(youtube=True)
    row.media_sha256 = sha256(AUDIO).hexdigest()
    row.media_s3_key = f"items/youtube/{VIDEO}/audio/{row.media_sha256}.mp3"
    pipeline.storage.objects[row.media_s3_key] = AUDIO
    db.session.commit()
    tasks.process_item.run(row.id)
    drain(pipeline)
    assert row.status == "completed" and row.transcript_origin == "creator_caption"
    pipeline.media.inventory.assert_called_once()
    pipeline.media.extract_audio.assert_not_called()
    pipeline.amazon.assert_not_called()


def test_amazon_provider_hash_uses_canonical_json_semantics_on_reuse(pipeline):
    row = pipeline.item()
    tasks.process_item.run(row.id)
    drain(pipeline)
    key = row.provider_transcript_s3_key
    raw = json.loads(pipeline.storage.objects[key])
    pipeline.storage.objects[key] = json.dumps(raw, indent=4, sort_keys=True).encode()
    tasks.process_item.run(row.id)
    drain(pipeline)
    assert row.status == "completed"
    pipeline.download.assert_called_once()
    pipeline.amazon.assert_called_once()


def test_terminal_item_does_not_clear_references_without_durable_repair_decision(pipeline):
    row = pipeline.item(youtube=True)
    tasks.process_item.run(row.id)
    drain(pipeline)
    provider, canonical = row.provider_transcript_s3_key, row.transcript_s3_key
    del pipeline.storage.objects[provider]
    row.status = "deferred"
    db.session.commit()
    tasks.process_item.run(row.id)
    assert row.status == "deferred"
    assert row.provider_transcript_s3_key == provider and row.transcript_s3_key == canonical
    assert not db.session.dirty
    pipeline.media.inventory.assert_called_once()


@pytest.mark.parametrize("unknown_outcome", [False, True])
def test_old_processing_sender_preserves_new_pending_generation_same_task_id(pipeline, monkeypatch, unknown_outcome):
    row = pipeline.item()
    def fast_repair(name, item_id, **options):
        assert name == "transcribe_item_audio" and item_id == row.id
        job = ProcessingJob.query.filter_by(item_id=item_id, stage="transcribe").one()
        assert job.attempts == 0 and job.error_category == "dispatch_pending"
        original_task_id = job.celery_task_id
        assert original_task_id == options["task_id"]
        # A fast worker claims attempt 1 and prepares a same-stage repair
        # before the older attempt-0 publication returns from the broker.
        job.attempts, job.state, job.error_category = 1, "pending", "dispatch_pending"
        db.session.commit()
        assert job.celery_task_id == original_task_id
        if unknown_outcome:
            raise TimeoutError("unknown broker outcome")
    monkeypatch.setattr(tasks, "dispatch_task", fast_repair)
    tasks.process_item.run(row.id)
    job = ProcessingJob.query.filter_by(item_id=row.id, stage="transcribe").one()
    assert job.attempts == 1 and job.state == "pending"
    assert job.error_category == "dispatch_pending"


def test_unchanged_processing_publication_generation_clears_its_own_marker(pipeline):
    row = pipeline.item()
    tasks.process_item.run(row.id)
    job = ProcessingJob.query.filter_by(item_id=row.id, stage="transcribe").one()
    assert job.attempts == 0 and job.state == "pending"
    assert job.celery_task_id == f"process-item-{row.id}-transcribe"
    assert job.error_category is None
