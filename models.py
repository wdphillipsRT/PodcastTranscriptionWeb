"""Source-neutral operational records; transcript bodies belong in private S3."""

from datetime import datetime, timezone
import sqlite3
from urllib.parse import urlsplit

from flask_login import UserMixin
import sqlalchemy as sa
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import validates
from sqlalchemy.types import TypeDecorator

from extensions import db
from ingestion_types import normalize_public_url


SOURCE_TYPES = ("podcast_rss", "youtube_channel", "youtube_playlist", "youtube_video")
ITEM_TYPES = ("podcast_episode", "youtube_video")
PLATFORMS = ("podcast", "youtube")
ITEM_STATUSES = ("discovered", "acquiring", "transcribing", "completed", "failed", "deferred", "unavailable")
TRANSCRIPT_ORIGINS = ("creator_caption", "automatic_caption", "amazon_transcribe")
RUN_TYPES = ("initial", "scheduled", "backfill")
DISCOVERY_STATES = ("pending", "scanning", "finalizing", "completed", "cancelled", "failed")
SELECTOR_TYPES = ("date_range", "count", "explicit_url")
BACKFILL_STATES = ("pending", "running", "completed", "cancelled", "failed")
JOB_STAGES = ("podcast_audio", "youtube_captions", "youtube_audio", "transcribe", "normalize")
JOB_STATES = ("pending", "running", "retrying", "completed", "failed")


def utc_now():
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Reject ambiguous writes and restore UTC awareness on SQLite reads."""

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must have a timezone")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


@sa.event.listens_for(sa.engine.Engine, "connect")
def _sqlite_foreign_keys(connection, _record):
    # SQLite otherwise silently ignores ON DELETE and referential integrity.
    if isinstance(connection, sqlite3.Connection):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def enum_check(column, values, name):
    choices = ", ".join(repr(value) for value in values)
    return sa.CheckConstraint(f"{column} IN ({choices})", name=name)


def safe_url(value):
    """Normalized descriptors may contain public HTTP URLs, never credentials."""
    try:
        parsed = urlsplit(value)
        parsed.port
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError
        normalized = normalize_public_url(value)
    except (TypeError, ValueError):
        raise ValueError("descriptor URL must be public and contain no credentials or transient query data") from None
    return normalized


class SafeRemoteMediaURL(TypeDecorator):
    """Apply URL safety to ORM, Core, and bulk writes before binding values."""

    impl = sa.String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else safe_url(value)


DESCRIPTOR_STRINGS = {
    "platform", "item_type", "external_id", "canonical_url", "remote_media_url", "title",
    "description", "creator_name", "creator_id", "channel_id", "channel_name", "channel_url",
    "thumbnail_url", "published_at", "language",
}


def normalized_descriptor(value):
    """Allow only the small, typed projection needed for item finalization.

    Provider responses, auth headers, signed media URLs, and arbitrary metadata
    have no place in this staging table. None clears the staging payload once
    the candidate has been finalized; identities remain for deduplication.
    """
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - DESCRIPTOR_STRINGS - {"duration_seconds", "requires_reacquisition"}:
        raise ValueError("descriptor must contain normalized fields only")
    result = dict(value)
    for key, entry in result.items():
        if entry is None:
            continue
        if key == "requires_reacquisition":
            if type(entry) is not bool:
                raise ValueError("descriptor requires_reacquisition must be a boolean")
        elif key == "duration_seconds":
            if type(entry) is not int or entry < 0:
                raise ValueError("descriptor duration_seconds must be a nonnegative integer")
        elif not isinstance(entry, str):
            raise ValueError("descriptor text fields must be strings")
        elif key.endswith("_url"):
            result[key] = safe_url(entry)
        elif key == "published_at":
            try:
                parsed = datetime.fromisoformat(entry)
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    raise ValueError
            except ValueError:
                raise ValueError("descriptor published_at must have a timezone") from None
            result[key] = parsed.astimezone(timezone.utc).isoformat()
        elif key == "platform" and entry not in PLATFORMS:
            raise ValueError("descriptor platform is invalid")
        elif key == "item_type" and entry not in ITEM_TYPES:
            raise ValueError("descriptor item_type is invalid")
    return result


class DescriptorJSON(TypeDecorator):
    """Revalidate at bind time, including MutableDict updates and Core writes."""

    impl = sa.JSON
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return normalized_descriptor(value)


class Timestamps:
    created_at = db.Column(UTCDateTime(), nullable=False, default=utc_now, server_default=sa.func.now())
    updated_at = db.Column(UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now, server_default=sa.func.now())


class Admin(UserMixin, Timestamps, db.Model):
    __tablename__ = "admins"
    id = db.Column(db.Integer, primary_key=True)
    singleton_key = db.Column(db.Boolean, nullable=False, default=True, server_default=sa.true())
    email = db.Column(db.String(254), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    sources = db.relationship("ContentSource", back_populates="admin", cascade="all, delete-orphan", passive_deletes=True)
    items = db.relationship("ContentItem", back_populates="admin", cascade="all, delete-orphan", passive_deletes=True)
    __table_args__ = (
        sa.UniqueConstraint("singleton_key", name="uq_admins_singleton_key"),
        sa.UniqueConstraint("email", name="uq_admins_email"),
        sa.CheckConstraint("singleton_key = true", name="ck_admins_singleton_true"),
    )


class ContentSource(Timestamps, db.Model):
    __tablename__ = "content_sources"
    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey("admins.id", ondelete="CASCADE"), nullable=False, index=True)
    source_type = db.Column(db.String(32), nullable=False)
    external_id = db.Column(db.String(1024), nullable=False)
    canonical_url = db.Column(db.String(4096), nullable=False)
    name = db.Column(db.String(500), nullable=False)
    description = db.Column(db.Text)
    image_url = db.Column(db.String(4096))
    provider_metadata = db.Column(db.JSON, nullable=False, default=dict)
    enabled = db.Column(db.Boolean, nullable=False, default=True, server_default=sa.true(), index=True)
    initial_item_limit = db.Column(db.Integer, nullable=False, default=25, server_default="25")
    discovery_cursor = db.Column(db.Text)
    last_success_at = db.Column(UTCDateTime())
    next_check_at = db.Column(UTCDateTime())
    error_category = db.Column(db.String(64))
    admin = db.relationship("Admin", back_populates="sources")
    items = db.relationship("SourceItem", back_populates="source", cascade="all, delete-orphan", passive_deletes=True)
    discovery_runs = db.relationship("DiscoveryRun", back_populates="source", cascade="all, delete-orphan", passive_deletes=True)
    backfill_requests = db.relationship("BackfillRequest", back_populates="source", cascade="all, delete-orphan", passive_deletes=True)
    __table_args__ = (
        sa.UniqueConstraint("source_type", "external_id", name="uq_content_sources_identity"),
        enum_check("source_type", SOURCE_TYPES, "ck_content_sources_source_type"),
        sa.CheckConstraint("initial_item_limit > 0", name="ck_content_sources_initial_item_limit"),
    )


class ContentItem(Timestamps, db.Model):
    __tablename__ = "content_items"
    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey("admins.id", ondelete="CASCADE"), nullable=False, index=True)
    item_type = db.Column(db.String(32), nullable=False)
    platform = db.Column(db.String(32), nullable=False)
    external_id = db.Column(db.String(1024), nullable=False)
    canonical_url = db.Column(db.String(4096), nullable=False)
    remote_media_url = db.Column(SafeRemoteMediaURL(4096))
    title = db.Column(db.String(1000), nullable=False)
    description = db.Column(db.Text)
    published_at = db.Column(UTCDateTime(), index=True)
    duration_seconds = db.Column(db.Integer)
    creator_id = db.Column(db.String(255))
    creator_name = db.Column(db.String(500))
    channel_id = db.Column(db.String(255))
    channel_name = db.Column(db.String(500))
    channel_url = db.Column(db.String(4096))
    thumbnail_url = db.Column(db.String(4096))
    status = db.Column(db.String(32), nullable=False, default="discovered", server_default="discovered", index=True)
    processing_stage = db.Column(db.String(32))
    last_successful_stage = db.Column(db.String(32))
    error_category = db.Column(db.String(64))
    transcript_origin = db.Column(db.String(32))
    media_s3_key = db.Column(db.String(2048))
    media_sha256 = db.Column(db.String(64))
    provider_transcript_s3_key = db.Column(db.String(2048))
    provider_transcript_sha256 = db.Column(db.String(64))
    transcript_s3_key = db.Column(db.String(2048))
    transcript_sha256 = db.Column(db.String(64))
    segments_s3_key = db.Column(db.String(2048))
    segments_sha256 = db.Column(db.String(64))
    transcription_provider = db.Column(db.String(64))
    transcription_job_name = db.Column(db.String(200))
    language = db.Column(db.String(16))
    admin = db.relationship("Admin", back_populates="items")
    sources = db.relationship("SourceItem", back_populates="item", cascade="all, delete-orphan", passive_deletes=True)
    processing_jobs = db.relationship("ProcessingJob", back_populates="item", cascade="all, delete-orphan", passive_deletes=True)
    __table_args__ = (
        sa.UniqueConstraint("platform", "external_id", name="uq_content_items_identity"),
        enum_check("item_type", ITEM_TYPES, "ck_content_items_item_type"),
        enum_check("platform", PLATFORMS, "ck_content_items_platform"),
        sa.CheckConstraint("(platform = 'podcast' AND item_type = 'podcast_episode') OR (platform = 'youtube' AND item_type = 'youtube_video')", name="ck_content_items_platform_type"),
        enum_check("status", ITEM_STATUSES, "ck_content_items_status"),
        enum_check("processing_stage", JOB_STAGES, "ck_content_items_processing_stage"),
        enum_check("last_successful_stage", JOB_STAGES, "ck_content_items_last_successful_stage"),
        enum_check("transcript_origin", TRANSCRIPT_ORIGINS, "ck_content_items_transcript_origin"),
        sa.CheckConstraint("duration_seconds IS NULL OR duration_seconds >= 0", name="ck_content_items_duration"),
    )

    @validates("remote_media_url")
    def validate_remote_media_url(self, _key, value):
        return None if value is None else safe_url(value)


class SourceItem(Timestamps, db.Model):
    __tablename__ = "source_items"
    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.Integer, db.ForeignKey("content_sources.id", ondelete="CASCADE"), nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False, index=True)
    source = db.relationship("ContentSource", back_populates="items")
    item = db.relationship("ContentItem", back_populates="sources")
    __table_args__ = (sa.UniqueConstraint("source_id", "item_id", name="uq_source_items_membership"),)


SELECTOR_CHECK = (
    "(selector_type = 'date_range' AND start_date IS NOT NULL AND end_date IS NOT NULL "
    "AND start_date <= end_date AND requested_count IS NULL AND explicit_url IS NULL) OR "
    "(selector_type = 'count' AND requested_count IS NOT NULL AND requested_count > 0 "
    "AND start_date IS NULL AND end_date IS NULL AND explicit_url IS NULL) OR "
    "(selector_type = 'explicit_url' AND explicit_url IS NOT NULL AND length(trim(explicit_url)) > 0 "
    "AND start_date IS NULL AND end_date IS NULL AND requested_count IS NULL)"
)


class BackfillRequest(Timestamps, db.Model):
    __tablename__ = "backfill_requests"
    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.Integer, db.ForeignKey("content_sources.id", ondelete="CASCADE"), nullable=False, index=True)
    selector_type = db.Column(db.String(32), nullable=False)
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    requested_count = db.Column(db.Integer)
    explicit_url = db.Column(db.String(4096))
    state = db.Column(db.String(32), nullable=False, default="pending", server_default="pending", index=True)
    cursor = db.Column(db.Text)
    items_scanned = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    items_accepted = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    items_queued = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    cancel_requested = db.Column(db.Boolean, nullable=False, default=False, server_default=sa.false())
    started_at = db.Column(UTCDateTime())
    completed_at = db.Column(UTCDateTime())
    error_category = db.Column(db.String(64))
    source = db.relationship("ContentSource", back_populates="backfill_requests")
    discovery_runs = db.relationship("DiscoveryRun", back_populates="backfill", passive_deletes=True)
    __table_args__ = (
        enum_check("selector_type", SELECTOR_TYPES, "ck_backfill_requests_selector_type"),
        sa.CheckConstraint(SELECTOR_CHECK, name="ck_backfill_requests_exactly_one_selector"),
        enum_check("state", BACKFILL_STATES, "ck_backfill_requests_state"),
        sa.CheckConstraint("items_scanned >= 0 AND items_accepted >= 0 AND items_queued >= 0", name="ck_backfill_requests_counters"),
    )

    def validate_selector(self):
        dates = self.start_date is not None and self.end_date is not None
        valid = False
        if self.selector_type == "date_range":
            valid = dates and self.start_date <= self.end_date and self.requested_count is None and self.explicit_url is None
        elif self.selector_type == "count":
            valid = type(self.requested_count) is int and self.requested_count > 0 and self.start_date is None and self.end_date is None and self.explicit_url is None
        elif self.selector_type == "explicit_url":
            valid = isinstance(self.explicit_url, str) and bool(self.explicit_url.strip()) and self.start_date is None and self.end_date is None and self.requested_count is None
        if not valid:
            raise ValueError("backfill requires exactly one valid selector")


@sa.event.listens_for(BackfillRequest, "before_insert")
@sa.event.listens_for(BackfillRequest, "before_update")
def _validate_backfill(_mapper, _connection, target):
    target.validate_selector()


class DiscoveryRun(Timestamps, db.Model):
    __tablename__ = "discovery_runs"
    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.Integer, db.ForeignKey("content_sources.id", ondelete="CASCADE"), nullable=False, index=True)
    backfill_id = db.Column(db.Integer, db.ForeignKey("backfill_requests.id", ondelete="SET NULL"), index=True)
    run_type = db.Column(db.String(32), nullable=False)
    state = db.Column(db.String(32), nullable=False, default="pending", server_default="pending", index=True)
    cursor = db.Column(db.Text)
    items_scanned = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    items_accepted = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    items_queued = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    started_at = db.Column(UTCDateTime())
    completed_at = db.Column(UTCDateTime())
    error_category = db.Column(db.String(64))
    source = db.relationship("ContentSource", back_populates="discovery_runs")
    backfill = db.relationship("BackfillRequest", back_populates="discovery_runs")
    candidates = db.relationship("DiscoveryCandidate", back_populates="run", cascade="all, delete-orphan", passive_deletes=True)
    __table_args__ = (
        enum_check("run_type", RUN_TYPES, "ck_discovery_runs_run_type"),
        enum_check("state", DISCOVERY_STATES, "ck_discovery_runs_state"),
        sa.CheckConstraint("items_scanned >= 0 AND items_accepted >= 0 AND items_queued >= 0", name="ck_discovery_runs_counters"),
    )


class DiscoveryCandidate(Timestamps, db.Model):
    __tablename__ = "discovery_candidates"
    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, db.ForeignKey("discovery_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    platform = db.Column(db.String(32), nullable=False)
    external_id = db.Column(db.String(1024), nullable=False)
    descriptor = db.Column(MutableDict.as_mutable(DescriptorJSON(none_as_null=True)))
    # Retained after descriptor cleanup: unavailable identities may be revisited,
    # whereas usable initial history omitted by the limit requires backfill.
    was_available = db.Column(db.Boolean, nullable=False, default=True, server_default=sa.true())
    run = db.relationship("DiscoveryRun", back_populates="candidates")
    __table_args__ = (
        sa.UniqueConstraint("run_id", "platform", "external_id", name="uq_discovery_candidates_identity"),
        enum_check("platform", PLATFORMS, "ck_discovery_candidates_platform"),
    )

    @validates("descriptor")
    def validate_descriptor(self, _key, value):
        return normalized_descriptor(value)


class ProcessingJob(Timestamps, db.Model):
    __tablename__ = "processing_jobs"
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False, index=True)
    stage = db.Column(db.String(32), nullable=False)
    state = db.Column(db.String(32), nullable=False, default="pending", server_default="pending", index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    celery_task_id = db.Column(db.String(255))
    started_at = db.Column(UTCDateTime())
    completed_at = db.Column(UTCDateTime())
    error_category = db.Column(db.String(64))
    item = db.relationship("ContentItem", back_populates="processing_jobs")
    __table_args__ = (
        sa.UniqueConstraint("item_id", "stage", name="uq_processing_jobs_item_stage"),
        enum_check("stage", JOB_STAGES, "ck_processing_jobs_stage"),
        enum_check("state", JOB_STATES, "ck_processing_jobs_state"),
        sa.CheckConstraint("attempts >= 0", name="ck_processing_jobs_attempts"),
    )
