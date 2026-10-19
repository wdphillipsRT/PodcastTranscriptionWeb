"""ID-only task boundary; application/provider clients are created lazily."""
from contextlib import contextmanager
import os
from types import SimpleNamespace

from celery import Celery
from flask import current_app, has_app_context

celery_app = Celery("podcast_transcription")
celery_app.config_from_object("celeryconfig")

# Populated only when a task needs its own app context, never at import time.
_worker_application = None


@contextmanager
def application_context():
    global _worker_application
    if has_app_context():
        yield
    else:
        pid = os.getpid()
        if _worker_application is not None and _worker_application[0] != pid:
            inherited_app = _worker_application[1]
            _worker_application = None
            # Detach inherited pools without closing the parent's connections.
            from extensions import db
            with inherited_app.app_context():
                for engine in db.engines.values():
                    engine.dispose(close=False)
        if _worker_application is None:
            from app import create_app
            _worker_application = (pid, create_app())
        app = _worker_application[1]
        # Flask-SQLAlchemy's teardown removes the task's scoped session, also
        # when the task raises; only the app/engine live across task contexts.
        with app.app_context():
            yield


def dispatch_task(name, identity, **options):
    from discovery_service import positive_id
    positive_id(identity)
    return celery_app.send_task("tasks." + name, args=[identity], **options)


def get_discovery_service():
    from discovery_service import DiscoveryService, adapter_for_source
    from extensions import db
    settings = SimpleNamespace(**dict(current_app.config))
    return DiscoveryService(db.session, settings, lambda source: adapter_for_source(source, settings), dispatch_task)


def _execute(method, identity=None):
    if method != "check_all_sources":
        from discovery_service import positive_id
        positive_id(identity)
    with application_context():
        function = getattr(get_discovery_service(), method)
        return function() if identity is None else function(identity)


@celery_app.task(name="tasks.discover_source")
def discover_source(source_id):
    return _execute("discover_source", source_id)


@celery_app.task(name="tasks.continue_discovery")
def continue_discovery(run_id):
    return _execute("continue_discovery", run_id)


@celery_app.task(name="tasks.finalize_discovery")
def finalize_discovery(run_id):
    return _execute("finalize_discovery", run_id)


@celery_app.task(name="tasks.run_backfill")
def run_backfill(request_id):
    return _execute("run_backfill", request_id)


@celery_app.task(name="tasks.cancel_backfill")
def cancel_backfill(request_id):
    return _execute("cancel_backfill", request_id)


@celery_app.task(name="tasks.check_all_sources")
def check_all_sources():
    return _execute("check_all_sources")


def get_processing_storage():
    from s3_utils import get_s3_manager
    return get_s3_manager(SimpleNamespace(**dict(current_app.config)))


def get_youtube_media_client():
    from youtube_media import YouTubeMediaClient
    return YouTubeMediaClient(SimpleNamespace(**dict(current_app.config)))


def get_processing_http_session():
    import requests
    return requests.Session()


@contextmanager
def _item_lock(item_id):
    """A worker-lifetime lock, independent of commits and wall-clock leases.

    PostgreSQL releases a session advisory lock on worker/connection death.
    SQLite uses a kernel flock shared by workers opening the same database.
    A running job is recoverable only after this exclusive lock is obtained;
    elapsed time alone never authorizes stealing a live worker's stage.
    """
    from extensions import db
    if db.engine.dialect.name == "postgresql":
        from sqlalchemy import text
        # A dedicated connection retains the lock across ORM commits. Pool
        # return alone does not release session locks: always unlock explicitly.
        connection = db.engine.connect()
        acquired = False
        try:
            acquired = bool(connection.execute(text("SELECT pg_try_advisory_lock(:key)"),
                {"key": 0x5054000000000000 + item_id}).scalar())
            connection.commit()
            yield acquired
        finally:
            if acquired:
                try:
                    connection.execute(text("SELECT pg_advisory_unlock(:key)"),
                        {"key": 0x5054000000000000 + item_id})
                    connection.commit()
                except Exception:
                    connection.invalidate()
            connection.close()
    else:
        import fcntl
        from hashlib import sha256
        from pathlib import Path
        import os
        import tempfile
        database = db.engine.url.database
        identity = str(Path(database).resolve()) if database and database != ":memory:" else str(id(db.engine))
        name = sha256(f"{identity}:{item_id}".encode()).hexdigest()
        # Never unlink lock files: unlinking an inode with waiters permits two
        # different locks for the same item. The contents contain no media data.
        descriptor = os.open(Path(tempfile.gettempdir()) / f"media-processing-{name}.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        acquired = False
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
            yield acquired
        finally:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _digest(value):
    import re
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _artifact(item, key, kind):
    from s3_utils import validate_artifact_key
    try:
        validate_artifact_key(key)
        return key.split("/")[1:4] == [item.platform, item.external_id, kind]
    except ValueError:
        return False


def _audio_valid(item):
    return (_digest(item.media_sha256) and _artifact(item, item.media_s3_key, "audio")
        and item.media_s3_key.rsplit("/", 1)[-1] in {
            item.media_sha256 + "." + extension for extension in ("mp3", "mp4", "m4a", "wav", "flac", "ogg", "amr", "webm")})


def _caption_valid(item):
    import re
    return (item.platform == "youtube" and item.transcript_origin in {"creator_caption", "automatic_caption"}
        and item.transcription_provider == "yt-dlp" and isinstance(item.language, str)
        and re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,15}", item.language) is not None
        and _digest(item.provider_transcript_sha256) and _artifact(item, item.provider_transcript_s3_key, "provider")
        and item.provider_transcript_s3_key.rsplit("/", 1)[-1]
            == f"{item.transcript_origin}-{item.language}-{item.provider_transcript_sha256}.vtt")


def _completed_valid(item):
    from transcribe import deterministic_job_name
    if not (all(_digest(value) for value in (item.transcript_sha256, item.segments_sha256, item.provider_transcript_sha256))
            and _artifact(item, item.transcript_s3_key, "transcripts")
            and _artifact(item, item.segments_s3_key, "transcripts") and item.language
            and item.transcript_s3_key.rsplit("/", 1)[-1] == item.transcript_sha256 + ".txt"
            and item.segments_s3_key.rsplit("/", 1)[-1] == item.transcript_sha256 + ".json"):
        return False
    if item.transcript_origin in {"creator_caption", "automatic_caption"}:
        return bool(_caption_valid(item) and item.transcription_job_name is None)
    return (item.transcript_origin == "amazon_transcribe" and item.transcription_provider == "amazon_transcribe"
        and _audio_valid(item) and _artifact(item, item.provider_transcript_s3_key, "provider")
        and item.transcription_job_name == deterministic_job_name(item.id, item.media_sha256)
        and item.provider_transcript_s3_key.rsplit("/", 1)[-1] == item.transcription_job_name + ".json")


class _ProcessingPipeline:
    """Durable stages; provider objects and transient locators stay in process."""

    def __init__(self, task):
        from extensions import db
        self.session, self.task = db.session, task
        self.settings = SimpleNamespace(**dict(current_app.config))
        self._storage = None

    @property
    def storage(self):
        if self._storage is None:
            self._storage = get_processing_storage()
        return self._storage

    def _job(self, item_id, stage):
        from models import ProcessingJob
        from sqlalchemy.exc import IntegrityError
        job = self.session.query(ProcessingJob).filter_by(item_id=item_id, stage=stage).one_or_none()
        if job is None:
            try:
                with self.session.begin_nested():
                    job = ProcessingJob(item_id=item_id, stage=stage, state="pending",
                        celery_task_id=f"process-item-{item_id}-{stage}", error_category="dispatch_pending")
                    self.session.add(job)
                    self.session.flush()
            except IntegrityError:
                job = self.session.query(ProcessingJob).filter_by(item_id=item_id, stage=stage).one()
        return job

    def _source(self, item):
        from models import ContentSource, SourceItem
        source = (self.session.query(ContentSource).join(SourceItem)
            .filter(SourceItem.item_id == item.id, ContentSource.enabled.is_(True), ContentSource.admin_id == item.admin_id)
            .order_by(ContentSource.id).first())
        if source is None:
            from ingestion_types import IngestionError
            raise IngestionError("source_unavailable")
        return source

    def _queue(self, item, stage):
        job = self._job(item.id, stage)
        # Existing completed metadata may be incomplete/corrupt. Repair the
        # same durable stage; never delete unknown S3 objects or invent a version.
        if job.state == "completed":
            job.state, job.completed_at = "pending", None
        if job.state == "failed":
            self.session.rollback()
            return None
        item.processing_stage = stage
        job.celery_task_id = job.celery_task_id or f"process-item-{item.id}-{stage}"
        job.error_category = "dispatch_pending"
        self.session.flush()
        # Capture the generation in the transaction that durably prepares this
        # publication. A post-commit read could observe a newer repair attempt.
        message = job.id, stage, job.celery_task_id, item.id, job.attempts
        self.session.commit()
        return message

    def _verify_completed(self, item):
        from hashlib import sha256
        import json
        from transcripts import CanonicalTranscript, TranscriptSegment, SCHEMA
        segments = self.storage.get_bytes(item.segments_s3_key, max_bytes=self.settings.YOUTUBE_CAPTION_MAX_BYTES)
        if sha256(segments).hexdigest() != item.segments_sha256:
            return False
        payload = json.loads(segments)
        if not isinstance(payload, dict):
            return False
        schema, digest = payload.pop("schema"), payload.pop("transcript_sha256")
        payload["segments"] = tuple(TranscriptSegment(**segment) for segment in payload["segments"])
        canonical = CanonicalTranscript(**payload)
        return (schema == SCHEMA and digest == canonical.transcript_sha256 == item.transcript_sha256
            and canonical.item_id == item.id and canonical.platform == item.platform
            and canonical.external_id == item.external_id and canonical.origin == item.transcript_origin
            and canonical.source_id in {membership.source_id for membership in item.sources}
            and canonical.provider == item.transcription_provider and canonical.language == item.language
            and canonical.source_sha256 == item.provider_transcript_sha256
            and self.storage.get_bytes(item.transcript_s3_key, max_bytes=self.settings.YOUTUBE_CAPTION_MAX_BYTES)
                == canonical.to_text().encode())

    def _canonical_reusable(self, item):
        from s3_utils import StorageError, StorageIntegrityError
        if not _completed_valid(item):
            return False
        try:
            return self._verify_completed(item)
        except StorageIntegrityError:
            return False
        except StorageError:
            raise
        except (ValueError, TypeError, KeyError):
            return False

    @staticmethod
    def _caption_expected(item):
        if item.platform != "youtube":
            return False
        caption_lineage = (item.transcript_origin in {"creator_caption", "automatic_caption"}
            or item.transcription_provider == "yt-dlp" or item.processing_stage in {"youtube_captions", "normalize"}
            or bool(item.provider_transcript_s3_key and item.provider_transcript_s3_key.endswith(".vtt")))
        audio_fallback_proven = (item.transcript_origin == "amazon_transcribe"
            or item.processing_stage in {"youtube_audio", "transcribe"}
            or item.last_successful_stage in {"youtube_audio", "transcribe"})
        return caption_lineage or not audio_fallback_proven

    def _caption_reusable(self, item):
        from hashlib import sha256
        from s3_utils import StorageIntegrityError
        if not _caption_valid(item):
            return False
        try:
            content = self.storage.get_bytes(item.provider_transcript_s3_key, max_bytes=self.settings.YOUTUBE_CAPTION_MAX_BYTES)
            return sha256(content).hexdigest() == item.provider_transcript_sha256
        except StorageIntegrityError:
            return False

    def _audio_reusable(self, item):
        from hashlib import sha256
        from s3_utils import StorageIntegrityError
        if not _audio_valid(item) or item.media_sha256 == sha256(b"").hexdigest():
            return False
        try:
            self.storage.verify_sha256(item.media_s3_key, expected_sha256=item.media_sha256,
                max_bytes=self.settings.MAX_AUDIO_BYTES)
            return True
        except StorageIntegrityError:
            return False

    def _amazon_provider_reusable(self, item):
        from hashlib import sha256
        import json
        from s3_utils import StorageError, StorageIntegrityError
        from transcribe import deterministic_job_name
        from transcripts import canonical_json
        name = deterministic_job_name(item.id, item.media_sha256)
        if not (_digest(item.provider_transcript_sha256) and item.transcription_job_name == name
                and _artifact(item, item.provider_transcript_s3_key, "provider")
                and item.provider_transcript_s3_key.rsplit("/", 1)[-1] == name + ".json"):
            return False
        try:
            raw = json.loads(self.storage.get_bytes(item.provider_transcript_s3_key,
                max_bytes=self.settings.YOUTUBE_CAPTION_MAX_BYTES))
            return (isinstance(raw, dict) and raw.get("jobName") == name
                and sha256(canonical_json(raw).encode()).hexdigest() == item.provider_transcript_sha256)
        except StorageIntegrityError:
            return False
        except StorageError:
            raise
        except (ValueError, TypeError):
            return False

    def _plan(self, item):
        """Validate upstream objects before deciding which durable stage repairs them."""
        if self._caption_expected(item):
            if self._caption_reusable(item):
                if self._canonical_reusable(item):
                    return None
                self._clear_canonical(item)
                return "normalize"
            # A valid audio object does not authorize bypassing creator or
            # automatic captions whose provider artifact needs repair.
            self._clear_canonical(item)
            item.provider_transcript_s3_key = item.provider_transcript_sha256 = None
            return "youtube_captions"
        if self._audio_reusable(item):
            if self._amazon_provider_reusable(item) and self._canonical_reusable(item):
                return None
            self._clear_canonical(item)
            return "transcribe"
        if item.media_s3_key or item.media_sha256:
            item.media_s3_key = item.media_sha256 = None
            self._clear_canonical(item)
        if item.platform == "youtube":
            return "youtube_audio" if (item.processing_stage in {"youtube_audio", "transcribe"}
                or item.transcript_origin == "amazon_transcribe") else "youtube_captions"
        return "podcast_audio"

    def _verification_stage(self, item):
        if self._caption_expected(item):
            return "normalize" if _caption_valid(item) else "youtube_captions"
        if _audio_valid(item):
            return "transcribe"
        if item.platform == "youtube":
            return "youtube_audio" if item.processing_stage == "youtube_audio" else "youtube_captions"
        return "podcast_audio"

    def run(self, item_id, requested_stage=None):
        from models import ContentItem, ProcessingJob, utc_now
        item = self.session.get(ContentItem, item_id)
        if item is None:
            return None
        verification_error = None
        try:
            stage = self._plan(item)
        except Exception as error:
            # Verification itself is a stage operation: transient reads retry
            # without clearing references or silently lowering caption priority.
            verification_error = error
            stage = self._verification_stage(item)
        if stage is None:
            item.status, item.error_category = "completed", None
            self.session.commit()
            return None
        if item.status in {"failed", "unavailable", "deferred"}:
            self.session.rollback()
            return None
        item.status = "transcribing" if stage in {"transcribe", "normalize"} else "acquiring"
        initial_stage = "podcast_audio" if item.platform == "podcast" else "youtube_captions"
        execution_stage = requested_stage or initial_stage
        if execution_stage != stage and not (execution_stage == "youtube_captions" and stage == "youtube_audio"):
            # Replayed acquisition/dispatcher messages route durable later
            # stages to their proper worker queue instead of doing AWS work
            # in a downloads worker. Caption and audio acquisition share a task.
            return self._queue(item, stage)
        job = self._job(item_id, stage)
        if job.state == "failed":
            self.session.rollback()
            return None
        job.celery_task_id = job.celery_task_id or f"process-item-{item_id}-{stage}"
        # The exclusive lifetime lock proves no previous claim is still live,
        # including a running record abandoned before its durable stage commit.
        self.session.query(ProcessingJob).filter_by(id=job.id).update({
            "state": "running", "attempts": ProcessingJob.attempts + 1,
            "started_at": utc_now(), "completed_at": None, "error_category": None}, synchronize_session="fetch")
        item.processing_stage, item.error_category = stage, None
        item.status = "transcribing" if stage in {"transcribe", "normalize"} else "acquiring"
        self.session.commit()
        try:
            if verification_error is not None:
                raise verification_error
            source = self._source(item)
            following = getattr(self, "_" + stage)(item, source)
            job.state, job.completed_at, job.error_category = "completed", utc_now(), None
            item.last_successful_stage, item.error_category = stage, None
            if following is None:
                if not _completed_valid(item):
                    raise ValueError("invalid artifact lineage")
                item.status = "completed"
            self.session.commit()
            return self._queue(item, following) if following else None
        except Exception as error:
            # Roll back partial metadata changes; completed upstream artifact
            # tuples are committed in their own stage and remain reusable.
            self.session.rollback()
            category, retryable, unavailable = _processing_failure(error)
            item, job = self.session.get(ContentItem, item_id), self.session.get(ProcessingJob, job.id)
            from s3_utils import StorageIntegrityError
            if stage == "normalize" and isinstance(error, StorageIntegrityError):
                # The verified VTT can disappear/change before its second read.
                # Persist a repair handoff, retaining caption priority.
                self._clear_canonical(item)
                item.provider_transcript_s3_key = item.provider_transcript_sha256 = None
                item.status, item.error_category = "acquiring", "artifact_repair"
                job.state, job.error_category = "retrying", "artifact_repair"
                return self._queue(item, "youtube_captions")
            item.error_category = category
            if retryable and job.attempts <= 5:
                job.state, job.error_category = "retrying", "dispatch_pending"
                self.session.commit()
                return ("retry", item_id, job.attempts, category, job.celery_task_id)
            job.state, job.error_category, job.completed_at = "failed", category, utc_now()
            item.status = "deferred" if retryable else "unavailable" if unavailable else "failed"
            self.session.commit()
            return None

    def _save_audio(self, item, media):
        from hashlib import sha256
        from pathlib import Path
        digest, size = sha256(), 0
        with Path(media.path).open("rb") as stream:
            while block := stream.read(min(65536, self.settings.MAX_AUDIO_BYTES - size + 1)):
                size += len(block)
                if size > self.settings.MAX_AUDIO_BYTES:
                    raise ValueError("invalid media size")
                digest.update(block)
        if not size or media.size_bytes != size or media.sha256 != digest.hexdigest():
            raise ValueError("invalid media hash")
        formats = {"audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mp4": "m4a", "video/mp4": "mp4",
            "audio/x-m4a": "m4a", "audio/wav": "wav", "audio/x-wav": "wav", "audio/flac": "flac",
            "audio/ogg": "ogg", "application/ogg": "ogg", "audio/amr": "amr", "audio/webm": "webm"}
        extension = formats.get(media.content_type)
        if extension is None:
            raise ValueError("unsupported media format")
        key = self.storage.generate_s3_key(item.platform, item.external_id, "audio", digest.hexdigest() + "." + extension)
        self.storage.upload_file(media.path, key, media.content_type)
        self._clear_canonical(item)
        item.provider_transcript_s3_key = item.provider_transcript_sha256 = None
        item.transcript_origin = item.transcription_provider = item.transcription_job_name = item.language = None
        item.media_s3_key, item.media_sha256 = key, digest.hexdigest()

    @staticmethod
    def _clear_canonical(item):
        # The new upstream tuple cannot inherit completion from old output.
        # Only references are cleared; unknown/private S3 objects are retained.
        item.transcript_s3_key = item.transcript_sha256 = None
        item.segments_s3_key = item.segments_sha256 = None

    def _podcast_audio(self, item, source):
        from contextlib import closing
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from episode_fetcher import download_podcast_audio, reacquire_podcast_media
        with TemporaryDirectory(prefix="podcast-processing-") as directory, closing(get_processing_http_session()) as http:
            locator = item.remote_media_url or reacquire_podcast_media(source, item.external_id, self.settings, http)
            media = download_podcast_audio(locator, Path(directory) / "media", self.settings, http)
            self._save_audio(item, media)
        return "transcribe"

    def _youtube_captions(self, item, source):
        from hashlib import sha256
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from transcripts import normalize_vtt, TranscriptValidationError
        from youtube_media import CaptionInventory, select_caption, YouTubeMediaError
        client = get_youtube_media_client()
        inventory = client.inventory(item.canonical_url)
        if inventory.video_id != item.external_id:
            raise ValueError("invalid inventory identity")
        remaining = list(inventory.candidates)
        while remaining:
            selected = select_caption(CaptionInventory(inventory.video_id, tuple(remaining)),
                self.settings.YOUTUBE_PREFERRED_CAPTION_LANGUAGES)
            remaining.remove(selected)
            try:
                with TemporaryDirectory(prefix="caption-processing-") as directory:
                    path = client.download_caption(item.canonical_url, selected, directory)
                    with Path(path).open("rb") as stream:
                        content = stream.read(self.settings.YOUTUBE_CAPTION_MAX_BYTES + 1)
                    if not 0 < len(content) <= self.settings.YOUTUBE_CAPTION_MAX_BYTES:
                        raise TranscriptValidationError("invalid caption size")
                    origin = "creator_caption" if selected.kind == "creator" else "automatic_caption"
                    # Validate before committing a provider candidate. A corrupt
                    # creator file may advance to an automatic caption here.
                    normalize_vtt(content, platform=item.platform, external_id=item.external_id,
                        item_id=item.id, source_id=source.id, language=selected.language, origin=origin,
                        provider_version=self.settings.YTDLP_EXPECTED_VERSION)
                    if len(selected.language) > 16:
                        raise TranscriptValidationError("invalid caption language")
                    digest = sha256(content).hexdigest()
                    key = self.storage.generate_s3_key(item.platform, item.external_id, "provider",
                        f"{origin}-{selected.language}-{digest}.vtt")
                    self.storage.upload_binary_content(content, key, "text/vtt")
                    self._clear_canonical(item)
                    item.provider_transcript_s3_key, item.provider_transcript_sha256 = key, digest
                    item.transcript_origin, item.transcription_provider, item.language = origin, "yt-dlp", selected.language
                    item.transcription_job_name = None
                    return "normalize"
            except YouTubeMediaError as error:
                if error.category not in {"invalid_caption", "invalid_output", "output_too_large"}:
                    raise
            except TranscriptValidationError:
                pass
        # The current inventory proved captions unusable. Only this durable
        # decision permits a reusable audio object to advance to Amazon.
        self._clear_canonical(item)
        item.provider_transcript_s3_key = item.provider_transcript_sha256 = None
        item.transcript_origin = item.transcription_provider = item.transcription_job_name = item.language = None
        return "transcribe" if self._audio_reusable(item) else "youtube_audio"

    def _youtube_audio(self, item, source):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory(prefix="youtube-audio-processing-") as directory:
            media = get_youtube_media_client().extract_audio(item.canonical_url, directory)
            self._save_audio(item, media)
        return "transcribe"

    def _normalize(self, item, source):
        from hashlib import sha256
        from s3_utils import StorageIntegrityError
        from transcripts import normalize_vtt
        content = self.storage.get_bytes(item.provider_transcript_s3_key, max_bytes=self.settings.YOUTUBE_CAPTION_MAX_BYTES)
        if sha256(content).hexdigest() != item.provider_transcript_sha256:
            raise StorageIntegrityError("artifact hash mismatch")
        transcript = normalize_vtt(content, platform=item.platform, external_id=item.external_id,
            item_id=item.id, source_id=source.id, language=item.language, origin=item.transcript_origin,
            provider_version=self.settings.YTDLP_EXPECTED_VERSION)
        digest, segments = transcript.transcript_sha256, transcript.to_json()
        text_key = self.storage.generate_s3_key(item.platform, item.external_id, "transcripts", digest + ".txt")
        segments_key = self.storage.generate_s3_key(item.platform, item.external_id, "transcripts", digest + ".json")
        self.storage.upload_text_content(transcript.to_text(), text_key, "text/plain")
        self.storage.upload_text_content(segments, segments_key, "application/json")
        item.transcript_s3_key, item.transcript_sha256 = text_key, digest
        item.segments_s3_key, item.segments_sha256 = segments_key, sha256(segments.encode()).hexdigest()

    def _transcribe(self, item, source):
        from hashlib import sha256
        import json
        from transcribe import transcribe_audio_from_s3, deterministic_job_name
        from transcripts import CanonicalTranscript, TranscriptSegment, SCHEMA, canonical_json
        result = transcribe_audio_from_s3(item.media_s3_key, item_id=item.id, source_id=source.id,
            platform=item.platform, external_id=item.external_id, audio_sha256=item.media_sha256,
            settings=self.settings, s3_manager=self.storage, canonical_url=item.canonical_url)
        if (result["audio_sha256"] != item.media_sha256 or result["provider"] != "amazon_transcribe"
                or result["job_name"] != deterministic_job_name(item.id, item.media_sha256)):
            raise ValueError("invalid transcription identity")
        digest = result["transcript_sha256"]
        if not _digest(digest) or not _digest(result["source_sha256"]):
            raise ValueError("invalid transcription hash")
        expected = {
            "segments_key": self.storage.generate_s3_key(item.platform, item.external_id, "transcripts", digest + ".json"),
            "transcript_key": self.storage.generate_s3_key(item.platform, item.external_id, "transcripts", digest + ".txt"),
            "provider_result_key": self.storage.generate_s3_key(item.platform, item.external_id, "provider", result["job_name"] + ".json"),
        }
        if any(result[field] != key for field, key in expected.items()):
            raise ValueError("invalid transcription artifact identity")
        # Amazon's API already uploads canonical artifacts. Validate the exact
        # private tuple and canonical identity before treating it as completed.
        segments = self.storage.get_bytes(result["segments_key"], max_bytes=self.settings.YOUTUBE_CAPTION_MAX_BYTES)
        payload = json.loads(segments)
        if not isinstance(payload, dict):
            raise ValueError("invalid transcription canonical object")
        schema, digest = payload.pop("schema"), payload.pop("transcript_sha256")
        payload["segments"] = tuple(TranscriptSegment(**segment) for segment in payload["segments"])
        transcript = CanonicalTranscript(**payload)
        if (schema != SCHEMA or digest != transcript.transcript_sha256 or digest != result["transcript_sha256"]
                or transcript.item_id != item.id or transcript.source_id != source.id
                or transcript.platform != item.platform or transcript.external_id != item.external_id
                or transcript.origin != "amazon_transcribe" or transcript.provider != result["provider"]
                or transcript.language != result["language"] or transcript.source_sha256 != result["source_sha256"]):
            raise ValueError("invalid transcription lineage")
        raw = json.loads(self.storage.get_bytes(result["provider_result_key"], max_bytes=self.settings.YOUTUBE_CAPTION_MAX_BYTES))
        text = self.storage.get_bytes(result["transcript_key"], max_bytes=self.settings.YOUTUBE_CAPTION_MAX_BYTES)
        if (not isinstance(raw, dict) or raw.get("jobName") != result["job_name"]
                or sha256(canonical_json(raw).encode()).hexdigest() != transcript.source_sha256
                or text != transcript.to_text().encode()):
            raise ValueError("invalid transcription artifact hash")
        item.transcript_origin, item.transcription_provider = "amazon_transcribe", result["provider"]
        item.transcription_job_name, item.language = result["job_name"], result["language"]
        item.provider_transcript_s3_key, item.provider_transcript_sha256 = result["provider_result_key"], result["source_sha256"]
        item.transcript_s3_key, item.transcript_sha256 = result["transcript_key"], digest
        item.segments_s3_key, item.segments_sha256 = result["segments_key"], sha256(segments).hexdigest()


def _processing_failure(error):
    """Only closed categories cross persistence/retry boundaries; never str(error)."""
    from ingestion_types import IngestionError
    from s3_utils import StorageTransientError
    from transcribe import TranscriptionTransientError
    from youtube_media import YouTubeMediaError
    from requests import RequestException, HTTPError
    from billiard.exceptions import SoftTimeLimitExceeded
    from botocore.exceptions import ConnectionError, HTTPClientError, ClientError
    if isinstance(error, StorageTransientError):
        return "storage_transient", True, False
    if isinstance(error, TranscriptionTransientError):
        return "transcription_transient", True, False
    if isinstance(error, SoftTimeLimitExceeded):
        return "worker_timeout", True, False
    if isinstance(error, YouTubeMediaError):
        if error.retryable:
            return "provider_transient", True, False
        if error.category == "unavailable":
            return "unavailable", False, True
    if isinstance(error, IngestionError):
        # Podcast adapter errors are stable categories; allow only known values.
        if error.args and error.args[0] in {"http_failed", "incomplete_response", "media_write_failed"}:
            return "http_transient", True, False
        if error.args and error.args[0] in {"source_unavailable", "item_not_found", "unsafe_destination"}:
            return "unavailable", False, True
    if isinstance(error, HTTPError):
        status = error.response.status_code if error.response is not None else None
        if status in {401, 403, 404, 410}:
            return "unavailable", False, True
        if status is not None and 400 <= status < 500 and status not in {408, 429}:
            return "invalid_input", False, False
    if isinstance(error, RequestException):
        return "http_transient", True, False
    if isinstance(error, (ConnectionError, HTTPClientError, OSError)):
        return "transport_transient", True, False
    if isinstance(error, ClientError) and error.response.get("Error", {}).get("Code") in {
            "SlowDown", "Throttling", "ThrottlingException", "InternalError", "ServiceUnavailable", "RequestTimeout"}:
        return "provider_transient", True, False
    return "invalid_input", False, False


def _publish_processing(message):
    if message is None:
        return
    job_id, stage, task_id, item_id, attempts = message
    from extensions import db
    from models import ProcessingJob
    names = {"podcast_audio": "acquire_podcast_audio", "youtube_captions": "acquire_youtube_transcript",
        "youtube_audio": "acquire_youtube_transcript", "transcribe": "transcribe_item_audio", "normalize": "normalize_item_transcript"}
    try:
        dispatch_task(names[stage], item_id, task_id=task_id, queue="transcription" if stage == "transcribe" else "downloads")
    except Exception:
        # Unknown broker outcome: the committed outbox can be replayed by
        # discovery's process_item dispatcher. No worker updates are undone.
        return
    db.session.query(ProcessingJob).filter_by(id=job_id, item_id=item_id, stage=stage, state="pending",
        celery_task_id=task_id, attempts=attempts, error_category="dispatch_pending").update(
        {"error_category": None}, synchronize_session="fetch")
    db.session.commit()


def _run_processing(task, item_id, stage=None):
    from discovery_service import positive_id
    positive_id(item_id)
    with application_context():
        with _item_lock(item_id) as acquired:
            if not acquired:
                return None
            message = _ProcessingPipeline(task).run(item_id, stage)
        # Release lifetime lock before publishing so a fast downstream worker
        # can claim immediately. Publication never overwrites a completed job.
        if message and message[0] == "retry":
            import random
            _, identity, attempts, category, task_id = message
            countdown = min(3600, 30 * 2 ** (attempts - 1)) + random.uniform(1, 30)
            raise task.retry(args=[identity], task_id=task_id, exc=RuntimeError(category), countdown=countdown, max_retries=None)
        _publish_processing(message)


@celery_app.task(bind=True, name="tasks.process_item", queue="downloads", acks_late=True, reject_on_worker_lost=True)
def process_item(self, item_id):
    return _run_processing(self, item_id)


@celery_app.task(bind=True, name="tasks.acquire_podcast_audio", queue="downloads", acks_late=True, reject_on_worker_lost=True)
def acquire_podcast_audio(self, item_id):
    return _run_processing(self, item_id, "podcast_audio")


@celery_app.task(bind=True, name="tasks.acquire_youtube_transcript", queue="downloads", acks_late=True, reject_on_worker_lost=True)
def acquire_youtube_transcript(self, item_id):
    return _run_processing(self, item_id, "youtube_captions")


@celery_app.task(bind=True, name="tasks.transcribe_item_audio", queue="transcription", acks_late=True, reject_on_worker_lost=True)
def transcribe_item_audio(self, item_id):
    return _run_processing(self, item_id, "transcribe")


@celery_app.task(bind=True, name="tasks.normalize_item_transcript", queue="downloads", acks_late=True, reject_on_worker_lost=True)
def normalize_item_transcript(self, item_id):
    return _run_processing(self, item_id, "normalize")
