"""ID-only task boundary; application/provider clients are created lazily."""
from contextlib import contextmanager
import logging
import os
from time import monotonic
from types import SimpleNamespace

from celery import Celery
from flask import current_app, has_app_context

celery_app = Celery("podcast_transcription")
celery_app.config_from_object("celeryconfig")

# Populated only when a task needs its own app context, never at import time.
_worker_application = None


def _observe_classification(event, identity, started, *, result=None, retry_category=None,
                            rule_match_counts=None):
    """Closed event encoder: never accept a payload, exception or artifact.

    Identity is an integer-only item/source/revision snapshot. Policy version
    and event names are code-owned. Counts come from committed service results;
    logging adds no storage reads, database work, or task failure boundary.
    """
    from commercial_policy import POLICY_VERSION
    from classification_service import (FinalizationResult, RETRYABLE_CLASSIFICATION_ERRORS,
                                        PERMANENT_CLASSIFICATION_ERRORS)
    if event not in {"prepare", "block", "finalize", "failed", "source_rule"}:
        return
    if len(identity) != 3 or any(type(value) is not int or value <= 0 for value in identity):
        return
    payload = dict(zip(("item_id", "source_id", "revision_id"), identity))
    payload.update(policy_version=POLICY_VERSION, stage_duration_ms=max(0, round((monotonic() - started) * 1000)))
    if retry_category in RETRYABLE_CLASSIFICATION_ERRORS | PERMANENT_CLASSIFICATION_ERRORS:
        payload["retry_category"] = retry_category
    valid_totals = (type(result) is FinalizationResult and all(
        type(getattr(result, name)) is int and getattr(result, name) >= 0
        for name in ("included_segments", "excluded_segments", "uncertain_segments",
            "included_milliseconds", "excluded_milliseconds", "uncertain_milliseconds"))
        and type(result.review_required) is bool)
    if valid_totals and event == "finalize" and result.state == "completed":
        payload["label_counts"] = dict(editorial=result.included_segments,
            excluded=result.excluded_segments, uncertain=result.uncertain_segments)
        durations = dict(included=result.included_milliseconds, excluded=result.excluded_milliseconds,
            uncertain=result.uncertain_milliseconds)
        total = sum(durations.values())
        payload["duration_ratios"] = {key: value / total if total else 0 for key, value in durations.items()}
        payload["review_required"] = result.review_required
    if rule_match_counts is not None:
        # Reconstruct only the known count fields, never forward caller extras.
        counts = {key: rule_match_counts.get(key) for key in ("scanned", "matched")}
        if all(type(value) is int and value >= 0 for value in counts.values()):
            payload["rule_match_counts"] = counts
    try:
        logging.getLogger(__name__).info("commercial_classification_" + event,
            extra={"commercial_event": payload})
    except Exception:
        # An unavailable log sink cannot change a durable checkpoint or retry.
        pass


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
        from transcripts import parse_canonical_transcript
        segments = self.storage.get_bytes(item.segments_s3_key, max_bytes=self.settings.YOUTUBE_CAPTION_MAX_BYTES)
        canonical = parse_canonical_transcript(segments, expected_item_id=item.id,
            expected_transcript_sha256=item.transcript_sha256, expected_segments_sha256=item.segments_sha256)
        return (canonical.platform == item.platform
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

    def _complete(self, item, job=None):
        """Join classification creation and media completion in one transaction.

        Verified canonical references survive even if the optional handoff fails.
        Request preparation performs storage I/O outside the final transaction.
        """
        from models import ContentItem, ProcessingJob, ClassificationRevision, utc_now
        item_id, job_id = item.id, job.id if job is not None else None
        precompletion = item.status != "completed"
        if precompletion:
            item.status = "transcribing"
        self.session.commit()

        def complete_media():
            current = self.session.get(ContentItem, item_id)
            current.status, current.error_category = "completed", None
            if job_id is not None:
                final_job = self.session.get(ProcessingJob, job_id)
                final_job.state, final_job.error_category = "completed", None
                final_job.completed_at = final_job.completed_at or utc_now()
                current.last_successful_stage = final_job.stage

        try:
            service = get_classification_service()
            request = service.prepare_revision_request(item_id, allow_precompletion=precompletion)
            revision_id = service.ensure_revision(item_id, "transcript_completed", prepared_request=request, commit=False)
            complete_media()
            self.session.commit()
        except Exception:
            # Classification cannot downgrade verified media. Beat repairs the
            # exceptional gap; never feed classifier exceptions to media retry.
            self.session.rollback()
            complete_media()
            self.session.commit()
            return None
        try:
            revision = self.session.get(ClassificationRevision, revision_id)
            message = _revision_dispatch(revision) if revision.error_category == "dispatch_pending" else None
            self.session.commit()
        except Exception:
            # Media completion and the outbox are already durable. Failures in
            # this optional read/dispatch handoff must only defer publication.
            self.session.rollback()
            return None
        return ("classification", message) if message else None

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
            final_job = self.session.query(ProcessingJob).filter_by(item_id=item_id,
                stage=self._verification_stage(item)).one_or_none()
            return self._complete(item, final_job)
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
            if following is None:
                if not _completed_valid(item):
                    raise ValueError("invalid artifact lineage")
            else:
                job.state, job.completed_at, job.error_category = "completed", utc_now(), None
                item.last_successful_stage, item.error_category = stage, None
                self.session.commit()
                return self._queue(item, following)
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
        # The terminal classification handoff cannot enter the media failure
        # handler, including after the final completion commit has succeeded.
        return self._complete(item, job)

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
        from transcripts import parse_canonical_transcript, canonical_json
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
        transcript = parse_canonical_transcript(segments, expected_item_id=item.id,
            expected_transcript_sha256=digest, expected_segments_sha256=None)
        if (transcript.source_id != source.id
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
        if error.args and error.args[0] in {
                "http_failed", "incomplete_response", "media_write_failed", "redirect_failed"}:
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
        if message and message[0] == "classification":
            return _publish_classification(message[1])
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


def get_classification_service():
    """Construct lazily; only the classifier container holds router credentials, read per call."""
    from classification_service import ClassificationService
    from router_classifier import RouterClassifier
    from extensions import db
    settings = SimpleNamespace(**dict(current_app.config))
    return ClassificationService(session=db.session, storage=get_processing_storage(),
        classifier=RouterClassifier(settings), settings=settings)


@celery_app.task(name="tasks.export_knowledge", queue="knowledge")
def export_knowledge():
    """Nightly knowledge export; consumed only by the exporter service."""
    with application_context():
        from extensions import db
        from integrations.knowledge import exporter
        from models import utc_now
        settings = SimpleNamespace(**dict(current_app.config))
        try:
            return exporter.export_knowledge(session=db.session, storage=get_processing_storage(),
                settings=settings, inbox=settings.KNOWLEDGE_EXPORT_INBOX, now=utc_now()).as_dict()
        except Exception as error:
            db.session.rollback()
            result = {"status": "failed", "reason": "unexpected_error"}
            # The type names the cause (e.g. SoftTimeLimitExceeded) without paths or messages.
            logging.getLogger(__name__).warning("knowledge_export_failed", extra={
                "knowledge_export": result, "error_type": type(error).__name__})
            return result


def get_guest_extractor():
    """Construct lazily; only the classifier container holds router credentials."""
    from guest_extractor import RouterGuestExtractor
    return RouterGuestExtractor(SimpleNamespace(**dict(current_app.config)))


@celery_app.task(name="tasks.extract_guests", queue="classification")
def extract_guests():
    with application_context():
        from extensions import db
        from guest_extractor import extract_pending_guests
        counts = extract_pending_guests(session=db.session, extractor=get_guest_extractor(),
            settings=SimpleNamespace(**dict(current_app.config)))
        logging.getLogger(__name__).info("knowledge_guest_extraction", extra={"guest_extraction": counts})
        return counts


def _classification_phase(revision_id, phase, block_ordinal):
    from discovery_service import positive_id
    positive_id(revision_id)
    if phase not in ("prepare", "block", "finalize"):
        raise ValueError("invalid classification phase")
    if phase == "block":
        positive_id(block_ordinal)
    elif block_ordinal is not None:
        raise ValueError("invalid classification ordinal")


def _phase_dispatch(revision, phase, ordinal=None):
    from classification_service import DispatchInstruction
    suffix = f"b{ordinal}" if phase == "block" else phase
    return DispatchInstruction(revision.id, phase, ordinal,
        f"classify-{revision.item_id}-{revision.transcript_sha256[:16]}-r{revision.revision}-{suffix}")


def _revision_dispatch(revision):
    """Decode only a verified, code-owned checkpoint identity from the outbox."""
    import re
    if revision.state in {"completed", "failed", "superseded"}:
        return None
    suffix = (revision.celery_task_id or "").rsplit("-", 1)[-1]
    if suffix in {"prepare", "finalize"}:
        result = _phase_dispatch(revision, suffix)
    elif re.fullmatch(r"b[1-9][0-9]*", suffix):
        result = _phase_dispatch(revision, "block", int(suffix[1:]))
    else:
        raise ValueError("invalid classification checkpoint")
    if result.task_id != revision.celery_task_id:
        raise ValueError("invalid classification checkpoint")
    return result


def _prepare_classification_publication(dispatch):
    from extensions import db
    from models import ClassificationRevision
    revision = db.session.get(ClassificationRevision, dispatch.revision_id, populate_existing=True)
    if revision.state in {"completed", "failed", "superseded"}:
        db.session.commit()
        return None
    revision.celery_task_id, revision.error_category = dispatch.task_id, "dispatch_pending"
    db.session.commit()
    return dispatch


def _publish_classification(dispatch, *, countdown=None, restore_category=None):
    """Publish a committed outbox after releasing the worker-lifetime lock."""
    from extensions import db
    from models import ClassificationRevision
    if dispatch is None or not current_app.config["CLASSIFICATION_ENABLED"]:
        return False
    revision = db.session.get(ClassificationRevision, dispatch.revision_id, populate_existing=True)
    if (revision is None or revision.state in {"completed", "failed", "superseded"}
            or revision.celery_task_id != dispatch.task_id or revision.error_category != "dispatch_pending"):
        db.session.commit()
        return False
    # Attempts and heartbeat distinguish successive retries sharing a task ID.
    generation = dict(state=revision.state, attempts=revision.attempts, heartbeat_at=revision.heartbeat_at)
    db.session.commit()
    options = {"task_id": dispatch.task_id, "queue": "classification"}
    if countdown is not None:
        options["countdown"] = countdown
    try:
        celery_app.send_task("tasks.classify_item", args=[dispatch.revision_id, dispatch.phase,
            dispatch.block_ordinal], **options)
    except Exception:
        # Delivery may have succeeded. The durable stable identity is replayed.
        db.session.rollback()
        return False
    db.session.query(ClassificationRevision).filter_by(id=dispatch.revision_id,
        celery_task_id=dispatch.task_id, error_category="dispatch_pending", **generation).filter(
        ClassificationRevision.state.notin_(("completed", "failed", "superseded"))).update(
        {"error_category": restore_category}, synchronize_session="fetch")
    db.session.commit()
    return True


def _classification_delay(attempts):
    import random
    return min(3600, 30 * 2 ** (attempts - 1)) + random.uniform(1, 30)


# A router-stored 5xx MODEL_PROVIDER_UNAVAILABLE (reason provider_unavailable)
# after the model ran this long is attributed to the block's input; a Forge
# outage fails faster and stays busy.
INPUT_FAILURE_MIN_SECONDS = 60
MAX_BLOCK_PROVIDER_FAILURES = 3


def _busy_delay(block_attempts):
    """30 seconds, doubling per busy claim of the block, capped at 10 minutes."""
    return min(600, 30 * 2 ** (min(max(block_attempts, 1), 6) - 1))


def _classification_failed(revision_id, phase, ordinal, category, reason=None):
    """Caller holds the item lock; revision attempts count failed calls only.

    A busy router is not a failure: the attempt budget is untouched, the block
    returns to pending, and the same block is retried after a back-off that
    doubles from 30 seconds to 10 minutes. A key conflict (`model_config` with
    reason `conflict`) is a counted failure retried under the block's next key.

    A `router_busy` with reason `provider_unavailable` (MODEL_PROVIDER_UNAVAILABLE) that arrives after the claim ran
    at least INPUT_FAILURE_MIN_SECONDS is counted on the block; the third becomes
    the permanent `model_unavailable`, which counts one attempt and fails the
    revision and block without a retry. Faster failures stay busy and uncounted.
    """
    from classification_service import (RETRYABLE_CLASSIFICATION_ERRORS, PERMANENT_CLASSIFICATION_ERRORS,
                                         key_conflict)
    from extensions import db
    from models import ClassificationRevision, ClassificationBlock, utc_now
    if category not in RETRYABLE_CLASSIFICATION_ERRORS | PERMANENT_CLASSIFICATION_ERRORS:
        category = "invalid_policy"
    revision = db.session.query(ClassificationRevision).filter_by(id=revision_id).with_for_update().populate_existing().one()
    if revision.state in {"completed", "superseded"}:
        db.session.commit()
        return None, None, None
    if category == "router_busy" and reason == "provider_unavailable" and ordinal is not None:
        counted = db.session.query(ClassificationBlock).filter_by(revision_id=revision_id, ordinal=ordinal).one_or_none()
        if (counted is not None and counted.started_at is not None
                and (utc_now() - counted.started_at).total_seconds() >= INPUT_FAILURE_MIN_SECONDS):
            counted.provider_failures += 1
            if counted.provider_failures >= MAX_BLOCK_PROVIDER_FAILURES:
                category = "model_unavailable"
    busy = category == "router_busy"
    if not busy:
        revision.attempts += 1
    retry = busy or (category in RETRYABLE_CLASSIFICATION_ERRORS or key_conflict(category, reason)) and revision.attempts < 5
    revision.state = "retrying" if retry else "failed"
    revision.error_category = "dispatch_pending" if retry else category
    revision.heartbeat_at = utc_now()
    block_attempts = 1
    if ordinal is not None:
        block = db.session.query(ClassificationBlock).filter_by(revision_id=revision_id, ordinal=ordinal).one_or_none()
        if block is not None and block.state not in {"completed", "superseded"}:
            block.state, block.error_category = ("pending" if busy else "failed"), category
        if block is not None:
            block_attempts = block.attempts
    dispatch = _phase_dispatch(revision, phase, ordinal) if retry else None
    if dispatch:
        revision.celery_task_id = dispatch.task_id
    attempts = revision.attempts
    db.session.commit()
    countdown = _busy_delay(block_attempts) if busy else _classification_delay(attempts) if retry else None
    return dispatch, countdown, category


def _classification_step(service, revision_id, phase, ordinal=None, *, handoff_superseded=True):
    """One service checkpoint under an already-held lifetime lock, no broker I/O."""
    from classification_service import ClassificationServiceError
    from extensions import db
    from models import ClassificationRevision
    revision = db.session.get(ClassificationRevision, revision_id, populate_existing=True)
    if revision is None or revision.state in {"completed", "failed", "superseded"}:
        db.session.commit()
        return None, None, None
    item_id = revision.item_id
    observation_identity = (item_id, revision.source_id, revision.id)
    db.session.commit()
    started = monotonic()
    try:
        if phase == "block":
            result = service.run_block(revision_id, ordinal)
        else:
            result = getattr(service, {"prepare": "prepare_revision", "finalize": "finalize_revision"}[phase])(revision_id)
    except ClassificationServiceError as error:
        db.session.rollback()
        if not error.record:
            # A live/stale claim or ambiguous commit is repaired by reconciliation.
            return None, None, None
        outcome = _classification_failed(revision_id, phase, ordinal, error.category, error.reason)
        _observe_classification("failed", observation_identity, started, retry_category=outcome[2])
        return outcome
    _observe_classification(phase, observation_identity, started, result=result)
    if result.next_dispatch is not None:
        return _prepare_classification_publication(result.next_dispatch), None, None
    if result.state == "superseded" and handoff_superseded:
        try:
            current = service.active_revision_matches_inputs(item_id)
        except ClassificationServiceError:
            db.session.rollback()
            current = False  # Unknown: hand off as before; ensure_revision repeats the checks.
        if current:
            # A newer revision from the current classifier already completed for
            # these inputs (for example while a reclassification was held).
            db.session.commit()
            return None, None, None
        identity = service.ensure_revision(item_id, "policy_upgrade")
        current = db.session.get(ClassificationRevision, identity)
        dispatch = _revision_dispatch(current)
        db.session.commit()
        return dispatch, None, None
    return None, None, None


RECLASSIFY_QUEUED = "reclassify_queued"
_RECLASSIFY_RELEASE_LOCK = 0x5243_4C53_0000_0001


def _reclassify_prefix(provenance):
    """Idempotency key prefix naming the whole classifier provenance, so a new model pin,
    router route or prompt makes every item a reclassification candidate again."""
    from hashlib import sha256
    digest = sha256(f"{provenance.requested_model}|{provenance.cli_version}|{provenance.prompt_sha256}"
                    .encode("utf-8")).hexdigest()
    return f"reclassify:{digest[:12]}:"


def _not_held():
    import sqlalchemy as sa
    from models import ClassificationRevision
    return sa.or_(ClassificationRevision.error_category.is_(None),
                  ClassificationRevision.error_category != RECLASSIFY_QUEUED)


def queue_reclassification(*, limit=None):
    """Hold a new revision for every completed item the current classifier has not classified, newest first.

    Held revisions (`pending` with error category `reclassify_queued`) are never
    published by the reconciler; `release_reclassification` dispatches them one at a time.
    An item that already has this classifier's reclassification revision for its
    transcript, in any state, is not a candidate: a failed one is retried with
    `classification retry --item-id`, so reruns and `--limit` move on to new items.
    `queued` counts revisions newly held by this run; `existing` counts candidates
    found already classified or queued once their item lock was held.
    """
    import sqlalchemy as sa
    from classification_service import ClassificationServiceError
    from extensions import db
    from models import ClassificationRevision, ContentItem
    from router_classifier import expected_provenance
    service = get_classification_service()
    provenance = expected_provenance(service.settings)
    prefix = _reclassify_prefix(provenance)
    current = sa.exists().where(ClassificationRevision.item_id == ContentItem.id,
        ClassificationRevision.transcript_sha256 == ContentItem.transcript_sha256,
        ClassificationRevision.requested_model == provenance.requested_model,
        ClassificationRevision.classifier_cli_version == provenance.cli_version,
        ClassificationRevision.prompt_sha256 == provenance.prompt_sha256,
        ClassificationRevision.state.in_(("pending", "running", "retrying", "completed")))
    requested = sa.exists().where(ClassificationRevision.item_id == ContentItem.id,
        ClassificationRevision.idempotency_key == sa.literal(prefix) + sa.func.substr(ContentItem.transcript_sha256, 1, 16))
    eligible = sa.select(ContentItem.id).where(ContentItem.status == "completed", ~current, ~requested)
    query = eligible.order_by(ContentItem.published_at.desc().nulls_last(), ContentItem.id.desc())
    if limit is not None:
        query = query.limit(limit)
    candidates = list(db.session.scalars(query))
    db.session.commit()
    counts = dict(candidates=len(candidates), queued=0, existing=0, busy=0, failed=0)
    for item_id in candidates:
        with _item_lock(item_id) as acquired:
            if not acquired:
                counts["busy"] += 1
                continue
            try:
                # Recheck under the lock: the item may have been classified or queued meanwhile.
                still = db.session.scalar(eligible.where(ContentItem.id == item_id))
                db.session.commit()
                if still is None:
                    counts["existing"] += 1
                    continue
                request = service.prepare_revision_request(item_id)
                transcript_sha256 = db.session.get(ContentItem, item_id).transcript_sha256
                revision_id = service.ensure_revision(item_id, "policy_upgrade",
                                                      idempotency_key=prefix + transcript_sha256[:16],
                                                      prepared_request=request, commit=False)
                revision = db.session.get(ClassificationRevision, revision_id)
                fresh = (revision.state == "pending" and revision.started_at is None
                         and revision.error_category == "dispatch_pending")
                if fresh:
                    revision.error_category = RECLASSIFY_QUEUED
                db.session.commit()
                counts["queued" if fresh else "existing"] += 1
            except ClassificationServiceError:
                db.session.rollback()
                counts["failed"] += 1
    return counts


def release_reclassification():
    """Dispatch the oldest held revision once no other revision is pending, running or retrying."""
    import sqlalchemy as sa
    from extensions import db
    from models import ClassificationRevision
    if db.engine.dialect.name == "postgresql":
        db.session.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": _RECLASSIFY_RELEASE_LOCK})
    in_flight = db.session.scalar(sa.select(sa.func.count()).select_from(ClassificationRevision).where(
        ClassificationRevision.state.in_(("pending", "running", "retrying")), _not_held()))
    held = None if in_flight else db.session.scalar(sa.select(ClassificationRevision).where(
        ClassificationRevision.state == "pending", ClassificationRevision.error_category == RECLASSIFY_QUEUED)
        .order_by(ClassificationRevision.id).limit(1))
    if held is None:
        db.session.commit()
        return None
    held.error_category = "dispatch_pending"
    dispatch = _revision_dispatch(held)
    db.session.commit()
    return dispatch


@celery_app.task(name="tasks.classify_item", queue="classification")
def classify_item(revision_id, phase, block_ordinal=None):
    _classification_phase(revision_id, phase, block_ordinal)
    from extensions import db
    from models import ClassificationRevision
    with application_context():
        item_id = db.session.query(ClassificationRevision.item_id).filter_by(id=revision_id).scalar()
        db.session.commit()
        if item_id is None:
            return
        with _item_lock(item_id) as acquired:
            if not acquired:
                return
            if not current_app.config["CLASSIFICATION_ENABLED"]:
                # A previously acknowledged message can arrive after rollout
                # is disabled. Return it to the durable outbox for re-enabling.
                revision = db.session.get(ClassificationRevision, revision_id, populate_existing=True)
                if (revision.state not in {"completed", "failed", "superseded"}
                        and revision.celery_task_id == _phase_dispatch(revision, phase, block_ordinal).task_id):
                    revision.error_category = "dispatch_pending"
                db.session.commit()
                return
            dispatch, countdown, category = _classification_step(get_classification_service(), revision_id, phase, block_ordinal)
        _publish_classification(dispatch, countdown=countdown, restore_category=category)
        if dispatch is None:
            # A chain ended (completed, failed or superseded): start the next held reclassification.
            _publish_classification(release_reclassification())


def _missing_classification_query():
    import sqlalchemy as sa
    from commercial_policy import POLICY_VERSION
    from models import ContentItem, ClassificationRevision
    current = sa.exists().where(ClassificationRevision.item_id == ContentItem.id,
        ClassificationRevision.transcript_sha256 == ContentItem.transcript_sha256,
        ClassificationRevision.policy_version == POLICY_VERSION,
        ClassificationRevision.state != "superseded")
    return sa.select(ContentItem.id).where(ContentItem.status == "completed", ~current)


def _reconcile_revision(revision_id, item_id, cutoff):
    """Separate aged delivery replays from proven abandoned running work."""
    from extensions import db
    from models import ClassificationRevision, ClassificationBlock
    with _item_lock(item_id) as acquired:
        if not acquired:
            return None, None, None
        revision = db.session.get(ClassificationRevision, revision_id, populate_existing=True)
        if revision is None or revision.state in {"completed", "failed", "superseded"}:
            db.session.commit()
            return None, None, None
        block = db.session.query(ClassificationBlock).filter_by(revision_id=revision_id, state="running").order_by(
            ClassificationBlock.ordinal).first()
        heartbeat = (block.heartbeat_at or block.started_at) if block else revision.heartbeat_at or revision.started_at
        if block is not None or revision.state == "running" and revision.error_category != "dispatch_pending":
            if heartbeat is None or heartbeat >= cutoff:
                db.session.commit()
                return None, None, None
            dispatch = _revision_dispatch(revision)
            phase, ordinal = ("block", block.ordinal) if block else (dispatch.phase, dispatch.block_ordinal)
            return _classification_failed(revision_id, phase, ordinal, "worker_timeout")
        if revision.error_category != "dispatch_pending":
            delivered_at = revision.updated_at or revision.created_at
            if delivered_at is None or delivered_at >= cutoff:
                db.session.commit()
                return None, None, None
        dispatch = _revision_dispatch(revision)
        category, countdown = None, None
        if revision.state == "retrying":
            from classification_service import RETRYABLE_CLASSIFICATION_ERRORS, current_error_category
            # Claude-era rows keep their stored names; map them so they keep their retry semantics.
            if current_error_category(revision.error_category) in RETRYABLE_CLASSIFICATION_ERRORS - {"dispatch_pending"}:
                category = current_error_category(revision.error_category)
            # Block failures retain their category while the revision's sole
            # error field is used by the outbox. Prepare/finalize cannot recover
            # the original category after an unknown send; the next call will.
            failed = db.session.query(ClassificationBlock).filter_by(revision_id=revision_id,
                ordinal=dispatch.block_ordinal, state="failed").one_or_none() if dispatch.phase == "block" else None
            if (category is None and failed is not None
                    and current_error_category(failed.error_category) in RETRYABLE_CLASSIFICATION_ERRORS - {"dispatch_pending"}):
                category = current_error_category(failed.error_category)
            countdown = _classification_delay(revision.attempts) if revision.attempts else None
        # Pending and retrying deliveries did not execute a failed checkpoint.
        # Re-arm the stable outbox without consuming the worker-attempt budget;
        # publication updates updated_at, renewing the delivery grace period.
        revision.error_category = "dispatch_pending"
        db.session.commit()
        return dispatch, countdown, category


def reconcile_classification_batch(*, limit=None, after_revision_id=0, after_item_id=0, create_only=False):
    """One bounded keyset page shared by beat and the operator backlog command."""
    from datetime import timedelta
    import sqlalchemy as sa
    import celeryconfig
    from extensions import db
    from models import ClassificationRevision, ClassificationBlock, utc_now
    from classification_service import ClassificationServiceError
    bound = min(current_app.config["CLASSIFICATION_RECONCILE_BATCH_SIZE"], 100, limit if limit is not None else 100)
    if type(bound) is not int or bound < 1:
        raise ValueError("invalid classification limit")
    for cursor in (after_revision_id, after_item_id):
        if type(cursor) is not int or cursor < 0:
            raise ValueError("invalid classification cursor")
    counts = dict(scanned=0, created=0, published=0, failed=0,
        after_revision_id=after_revision_id, after_item_id=after_item_id)
    enabled = current_app.config["CLASSIFICATION_ENABLED"]
    cutoff = utc_now() - timedelta(seconds=celeryconfig.task_time_limit + 300)
    if enabled and not create_only:
        stale_running = sa.and_(ClassificationRevision.state == "running",
            sa.func.coalesce(ClassificationRevision.heartbeat_at, ClassificationRevision.started_at) < cutoff)
        stale_delivery = sa.and_(sa.or_(ClassificationRevision.state == "retrying",
            sa.and_(ClassificationRevision.state == "pending", ClassificationRevision.started_at.is_(None),
                ClassificationRevision.heartbeat_at.is_(None))),
            sa.func.coalesce(ClassificationRevision.updated_at, ClassificationRevision.created_at) < cutoff)
        stale_block = sa.exists().where(ClassificationBlock.revision_id == ClassificationRevision.id,
            ClassificationBlock.state == "running",
            sa.func.coalesce(ClassificationBlock.heartbeat_at, ClassificationBlock.started_at) < cutoff)
        candidates = list(db.session.execute(sa.select(ClassificationRevision.id, ClassificationRevision.item_id).where(
            ClassificationRevision.id > after_revision_id,
            ClassificationRevision.state.in_(("pending", "running", "retrying")), _not_held(),
            sa.or_(ClassificationRevision.error_category == "dispatch_pending", stale_running, stale_delivery, stale_block))
            .order_by(ClassificationRevision.id).limit(bound)))
        db.session.commit()
        for identity, item_id in candidates:
            counts["scanned"] += 1
            counts["after_revision_id"] = identity
            dispatch, countdown, category = _reconcile_revision(identity, item_id, cutoff)
            counts["published"] += int(_publish_classification(dispatch, countdown=countdown, restore_category=category))
    remaining = bound - counts["scanned"]
    if remaining:
        from models import ContentItem
        candidates = list(db.session.scalars(_missing_classification_query().where(ContentItem.id > after_item_id)
            .order_by(ContentItem.id).limit(remaining)))
        db.session.commit()
        for item_id in candidates:
            counts["scanned"] += 1
            counts["after_item_id"] = item_id
            dispatch = None
            with _item_lock(item_id) as acquired:
                if not acquired:
                    continue
                try:
                    # Recheck after acquiring the same lock as the media worker.
                    missing = db.session.scalar(_missing_classification_query().where(ContentItem.id == item_id))
                    had_revision = db.session.scalar(sa.select(ClassificationRevision.id).where(
                        ClassificationRevision.item_id == item_id).limit(1))
                    db.session.commit()
                    if missing is None:
                        continue
                    identity = get_classification_service().ensure_revision(item_id,
                        "policy_upgrade" if had_revision else "backfill")
                    revision = db.session.get(ClassificationRevision, identity)
                    dispatch = _revision_dispatch(revision)
                    db.session.commit()
                    counts["created"] += 1
                except ClassificationServiceError:
                    db.session.rollback()
                    counts["failed"] += 1
            counts["published"] += int(_publish_classification(dispatch))
    return counts


@celery_app.task(name="tasks.reconcile_classifications", queue="classification")
def reconcile_classifications(after_revision_id=0, after_item_id=0):
    with application_context():
        counts = reconcile_classification_batch(after_revision_id=after_revision_id, after_item_id=after_item_id)
        if current_app.config["CLASSIFICATION_ENABLED"]:
            _reconcile_source_sweeps()
            _publish_classification(release_reclassification())
        if (current_app.config["CLASSIFICATION_ENABLED"] and counts["scanned"]
                == min(current_app.config["CLASSIFICATION_RECONCILE_BATCH_SIZE"], 100)):
            try:
                celery_app.send_task("tasks.reconcile_classifications", queue="classification",
                    kwargs={name: counts[name] for name in ("after_revision_id", "after_item_id")})
            except Exception:
                pass  # Periodic argument-free reconciliation restarts this scan.
        return counts


@celery_app.task(name="tasks.scan_source_rule", queue="classification")
def scan_source_rule(sweep_id):
    from discovery_service import positive_id
    positive_id(sweep_id)
    with application_context():
        if not current_app.config["CLASSIFICATION_ENABLED"]:
            import sqlalchemy as sa
            from extensions import db
            from models import ClassificationSweep
            db.session.execute(sa.update(ClassificationSweep).where(ClassificationSweep.id == sweep_id,
                ClassificationSweep.state.in_(("pending", "running")),
                sa.or_(ClassificationSweep.error_category.is_(None), ClassificationSweep.error_category == "dispatch_pending"))
                .values(error_category="dispatch_pending"))
            db.session.commit()
            return
        if current_app.config["CLASSIFICATION_ENABLED"]:
            from extensions import db
            from models import ClassificationRevision, ClassificationSweep, SourceClassificationRule, SegmentOverride
            from sqlalchemy.exc import SQLAlchemyError
            try:
                identity = db.session.query(SegmentOverride.item_id, ClassificationSweep.source_id,
                    SegmentOverride.policy_revision_id).select_from(ClassificationSweep).join(
                    SourceClassificationRule, SourceClassificationRule.id == ClassificationSweep.rule_id).join(
                    SegmentOverride, SegmentOverride.id == SourceClassificationRule.originating_override_id).filter(
                    ClassificationSweep.id == sweep_id).one_or_none()
                db.session.commit()
                started = monotonic()
                result = get_classification_service().scan_source_rule(sweep_id)
                if result is None:
                    return
                if identity is not None:
                    _observe_classification("source_rule", identity, started,
                        rule_match_counts=dict(scanned=result.items_scanned, matched=result.items_queued))
                for revision_id in result.revision_ids:
                    revision = db.session.get(ClassificationRevision, revision_id)
                    dispatch = _revision_dispatch(revision)
                    db.session.commit()
                    _publish_classification(dispatch)
                if result.next_task_id:
                    _publish_source_sweep(sweep_id)
                return dict(sweep_id=result.sweep_id, state=result.state,
                    cursor_item_id=result.cursor_item_id, items_scanned=result.items_scanned,
                    items_queued=result.items_queued)
            except SQLAlchemyError:
                # Cursor/revision writes roll back together. The pending sweep
                # outbox and revision reconciler repair unknown commit results.
                db.session.rollback()


def _publish_source_sweep(sweep_id):
    """Publish only a committed stable cursor outbox; never erase newer work."""
    import sqlalchemy as sa
    from extensions import db
    from models import ClassificationSweep, utc_now
    from classification_service import SOURCE_SWEEP_RETRY_ERRORS, current_error_category, source_sweep_retry_at
    if not current_app.config["CLASSIFICATION_ENABLED"]:
        return False
    sweep = db.session.get(ClassificationSweep, sweep_id, populate_existing=True)
    if (sweep is None or sweep.state not in {"pending", "running"}
            or current_error_category(sweep.error_category) not in SOURCE_SWEEP_RETRY_ERRORS | {"dispatch_pending"}):
        db.session.commit()
        return False
    retry_at = source_sweep_retry_at(sweep)
    if retry_at is not None and utc_now() < retry_at:
        db.session.commit()
        return False
    task_id = f"source-rule-{sweep.id}-after-{sweep.cursor_item_id or 0}"
    if sweep.celery_task_id != task_id:
        db.session.commit()
        return False
    generation = dict(state=sweep.state, cursor_item_id=sweep.cursor_item_id,
        items_scanned=sweep.items_scanned, items_queued=sweep.items_queued,
        updated_at=sweep.updated_at, error_category=sweep.error_category)
    db.session.commit()
    try:
        celery_app.send_task("tasks.scan_source_rule", args=[sweep_id], queue="classification", task_id=task_id)
    except Exception:
        db.session.rollback()
        return False
    try:
        conditions = [ClassificationSweep.id == sweep_id, ClassificationSweep.celery_task_id == task_id]
        conditions.extend(getattr(ClassificationSweep, name) == value for name, value in generation.items())
        db.session.execute(sa.update(ClassificationSweep).where(*conditions).values(error_category=None))
        db.session.commit()
    except Exception:
        db.session.rollback()
    return True


def publish_override(result):
    """Route/UI handoff after record_override has committed both outboxes."""
    try:
        _publish_classification(result.next_dispatch)
    finally:
        _publish_source_sweep(result.sweep_id)


def _reconcile_source_sweeps():
    """Bounded replay for pending and acknowledged-but-lost sweep deliveries."""
    from datetime import timedelta
    import sqlalchemy as sa
    import celeryconfig
    from extensions import db
    from models import ClassificationSweep, utc_now
    from classification_service import SOURCE_SWEEP_RETRY_DELAY_SECONDS, SOURCE_SWEEP_RETRY_STORED_ERRORS
    now = utc_now()
    cutoff = now - timedelta(seconds=celeryconfig.task_time_limit + 300)
    retry_cutoff = now - timedelta(seconds=SOURCE_SWEEP_RETRY_DELAY_SECONDS)
    aged_delivery = sa.and_(ClassificationSweep.error_category.is_(None), ClassificationSweep.updated_at < cutoff)
    retry_ready = sa.and_(ClassificationSweep.error_category.in_(sorted(SOURCE_SWEEP_RETRY_STORED_ERRORS)),
        ClassificationSweep.updated_at <= retry_cutoff)
    identities = list(db.session.scalars(sa.select(ClassificationSweep.id).where(
        ClassificationSweep.state.in_(("pending", "running")),
        sa.or_(ClassificationSweep.error_category == "dispatch_pending", retry_ready, aged_delivery))
        .order_by(ClassificationSweep.id).limit(50)))
    db.session.commit()
    for identity in identities:
        db.session.execute(sa.update(ClassificationSweep).where(ClassificationSweep.id == identity,
            ClassificationSweep.state.in_(("pending", "running")),
            aged_delivery)
            .values(error_category="dispatch_pending"))
        db.session.commit()
        _publish_source_sweep(identity)
