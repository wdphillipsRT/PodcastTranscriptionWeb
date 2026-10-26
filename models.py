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
from ingestion_types import normalize_public_url, person_names


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
CLASSIFICATION_STATES = ("pending", "running", "retrying", "completed", "failed", "superseded")
CLASSIFICATION_REASONS = ("transcript_completed", "backfill", "override", "policy_upgrade", "source_rule", "manual_retry", "live_smoke")
RULE_SCAN_STATES = ("pending", "running", "completed", "failed")
CLASSIFICATION_BLOCK_STATES = ("pending", "running", "completed", "failed", "superseded")
CLASSIFICATION_LABELS = ("editorial", "paid_commercial", "self_promotion", "cross_promotion", "housekeeping", "uncertain")
GUEST_EXTRACTION_STATES = ("completed", "failed")
KNOWLEDGE_BATCH_STATES = ("pending", "written", "abandoned", "failed")


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

MAX_DESCRIPTOR_GUESTS = 20


def normalized_descriptor(value):
    """Allow only the small, typed projection needed for item finalization.

    Provider responses, auth headers, signed media URLs, and arbitrary metadata
    have no place in this staging table. None clears the staging payload once
    the candidate has been finalized; identities remain for deduplication.
    """
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - DESCRIPTOR_STRINGS - {"duration_seconds", "requires_reacquisition",
                                                                       "guests"}:
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
        elif key == "guests":
            if (not isinstance(entry, list) or not entry or not all(isinstance(name, str) for name in entry)
                    or list(person_names(entry, limit=MAX_DESCRIPTOR_GUESTS)) != entry):
                raise ValueError("descriptor guests must be a non-empty list of normalized names")
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
    # Knowledge export people: lists of display names. The feed (or YouTube
    # channel owner) seeds feed_hosts; a non-null admin_hosts always wins.
    feed_hosts = db.Column(db.JSON(none_as_null=True))
    admin_hosts = db.Column(db.JSON(none_as_null=True))
    hosts_updated_at = db.Column(UTCDateTime())
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
    active_classification_revision_id = db.Column(db.Integer, db.ForeignKey(
        "classification_revisions.id", name="fk_content_items_active_classification_revision",
        ondelete="SET NULL", use_alter=True), index=True)
    # Guests named by <podcast:person role="guest">; a non-null admin_guests
    # (possibly empty) always wins over feed and inferred guests.
    feed_guests = db.Column(db.JSON(none_as_null=True))
    admin_guests = db.Column(db.JSON(none_as_null=True))
    guests_updated_at = db.Column(UTCDateTime())
    active_classification_revision = db.relationship("ClassificationRevision",
        foreign_keys=[active_classification_revision_id], post_update=True)
    classification_revisions = db.relationship("ClassificationRevision", back_populates="item",
        foreign_keys="ClassificationRevision.item_id", cascade="all, delete-orphan", passive_deletes=True)
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


class ClassificationRevision(Timestamps, db.Model):
    """Audited classifier inputs and immutable completed artifact references."""

    __tablename__ = "classification_revisions"
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False, index=True)
    source_id = db.Column(db.Integer, db.ForeignKey("content_sources.id", ondelete="CASCADE"), nullable=False, index=True)
    previous_revision_id = db.Column(db.Integer, db.ForeignKey("classification_revisions.id", ondelete="SET NULL"), index=True)
    revision = db.Column(db.Integer, nullable=False)
    request_reason = db.Column(db.String(32), nullable=False)
    state = db.Column(db.String(32), nullable=False, default="pending", server_default="pending", index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    celery_task_id = db.Column(db.String(255))
    transcript_sha256 = db.Column(db.String(64), nullable=False)
    segments_sha256 = db.Column(db.String(64), nullable=False)
    input_identity_sha256 = db.Column(db.String(64), nullable=False)
    idempotency_key = db.Column(db.String(255), nullable=False)
    policy_version = db.Column(db.String(128), nullable=False)
    prompt_version = db.Column(db.String(128), nullable=False)
    prompt_sha256 = db.Column(db.String(64), nullable=False)
    requested_model = db.Column(db.String(128), nullable=False)
    resolved_model = db.Column(db.String(128))
    classifier_cli_version = db.Column(db.String(128), nullable=False)
    source_rule_set_sha256 = db.Column(db.String(64), nullable=False)
    override_set_sha256 = db.Column(db.String(64), nullable=False)
    policy_s3_key = db.Column(db.String(2048))
    policy_sha256 = db.Column(db.String(64))
    projection_s3_key = db.Column(db.String(2048))
    projection_sha256 = db.Column(db.String(64))
    included_segments = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    excluded_segments = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    uncertain_segments = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    included_milliseconds = db.Column(db.BigInteger, nullable=False, default=0, server_default="0")
    excluded_milliseconds = db.Column(db.BigInteger, nullable=False, default=0, server_default="0")
    uncertain_milliseconds = db.Column(db.BigInteger, nullable=False, default=0, server_default="0")
    total_cost_microusd = db.Column(db.BigInteger, nullable=False, default=0, server_default="0")
    review_required = db.Column(db.Boolean, nullable=False, default=False, server_default=sa.false())
    heartbeat_at = db.Column(UTCDateTime())
    started_at = db.Column(UTCDateTime())
    completed_at = db.Column(UTCDateTime())
    error_category = db.Column(db.String(64))
    item = db.relationship("ContentItem", back_populates="classification_revisions", foreign_keys=[item_id])
    source = db.relationship("ContentSource")
    previous_revision = db.relationship("ClassificationRevision", remote_side=[id], foreign_keys=[previous_revision_id])
    blocks = db.relationship("ClassificationBlock", back_populates="revision", cascade="all, delete-orphan", passive_deletes=True)
    __table_args__ = (
        sa.UniqueConstraint("item_id", "transcript_sha256", "revision", name="uq_classification_revisions_item_transcript_revision"),
        sa.UniqueConstraint("item_id", "idempotency_key", name="uq_classification_revisions_item_idempotency"),
        enum_check("state", CLASSIFICATION_STATES, "ck_classification_revisions_state"),
        enum_check("request_reason", CLASSIFICATION_REASONS, "ck_classification_revisions_reason"),
        sa.CheckConstraint("revision > 0", name="ck_classification_revisions_revision"),
        sa.CheckConstraint("attempts >= 0", name="ck_classification_revisions_attempts"),
        sa.CheckConstraint("included_segments >= 0 AND excluded_segments >= 0 AND uncertain_segments >= 0 "
            "AND included_milliseconds >= 0 AND excluded_milliseconds >= 0 AND uncertain_milliseconds >= 0 "
            "AND total_cost_microusd >= 0", name="ck_classification_revisions_totals"),
        sa.CheckConstraint("state != 'completed' OR (policy_s3_key IS NOT NULL AND policy_sha256 IS NOT NULL "
            "AND projection_s3_key IS NOT NULL AND projection_sha256 IS NOT NULL "
            "AND resolved_model IS NOT NULL AND completed_at IS NOT NULL AND error_category IS NULL)", name="ck_classification_revisions_completed"),
    )


@sa.event.listens_for(ClassificationRevision, "before_update")
def _immutable_completed_classification(_mapper, connection, target):
    state = sa.inspect(target)
    if not any(state.attrs[column.key].history.has_changes() for column in state.mapper.column_attrs):
        return
    # Hold the row lock through the ORM UPDATE so another writer cannot
    # complete the revision between this state check and that update.
    persisted_state = connection.scalar(sa.select(ClassificationRevision.__table__.c.state).where(
        ClassificationRevision.__table__.c.id == state.identity[0]).with_for_update())
    if persisted_state == "completed":
        raise ValueError("completed classification revisions are immutable")


class ClassificationBlock(Timestamps, db.Model):
    __tablename__ = "classification_blocks"
    id = db.Column(db.Integer, primary_key=True)
    revision_id = db.Column(db.Integer, db.ForeignKey("classification_revisions.id", ondelete="CASCADE"), nullable=False, index=True)
    ordinal = db.Column(db.Integer, nullable=False)
    state = db.Column(db.String(32), nullable=False, default="pending", server_default="pending", index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    # Router idempotency-key generation; advances only after the router may have stored an outcome.
    request_generation = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    # When the current generation's key was first sent; a 202 long after it means a stuck key.
    request_generation_started_at = db.Column(UTCDateTime())
    target_first_segment_id = db.Column(db.String(27), nullable=False)
    target_last_segment_id = db.Column(db.String(27), nullable=False)
    input_sha256 = db.Column(db.String(64), nullable=False)
    decision_s3_key = db.Column(db.String(2048))
    decision_sha256 = db.Column(db.String(64))
    cost_microusd = db.Column(db.BigInteger, nullable=False, default=0, server_default="0")
    heartbeat_at = db.Column(UTCDateTime())
    started_at = db.Column(UTCDateTime())
    completed_at = db.Column(UTCDateTime())
    error_category = db.Column(db.String(64))
    revision = db.relationship("ClassificationRevision", back_populates="blocks")
    __table_args__ = (
        sa.UniqueConstraint("revision_id", "ordinal", name="uq_classification_blocks_revision_ordinal"),
        enum_check("state", CLASSIFICATION_BLOCK_STATES, "ck_classification_blocks_state"),
        sa.CheckConstraint("ordinal > 0", name="ck_classification_blocks_ordinal"),
        sa.CheckConstraint("attempts >= 0 AND cost_microusd >= 0", name="ck_classification_blocks_counters"),
        sa.CheckConstraint("state != 'completed' OR (decision_s3_key IS NOT NULL AND decision_sha256 IS NOT NULL "
            "AND completed_at IS NOT NULL)", name="ck_classification_blocks_completed"),
    )


class SegmentOverride(db.Model):
    __tablename__ = "segment_overrides"
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False, index=True)
    source_id = db.Column(db.Integer, db.ForeignKey("content_sources.id", ondelete="CASCADE"), nullable=False, index=True)
    transcript_sha256 = db.Column(db.String(64), nullable=False)
    policy_revision_id = db.Column(db.Integer, db.ForeignKey("classification_revisions.id", ondelete="SET NULL"), index=True)
    first_segment_id = db.Column(db.String(27), nullable=False)
    last_segment_id = db.Column(db.String(27), nullable=False)
    label = db.Column(db.String(32), nullable=False)
    range_fingerprint_sha256 = db.Column(db.String(64), nullable=False)
    supersedes_id = db.Column(db.Integer, db.ForeignKey("segment_overrides.id", ondelete="SET NULL"), index=True)
    admin_id = db.Column(db.Integer, db.ForeignKey("admins.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = db.Column(UTCDateTime(), nullable=False, default=utc_now, server_default=sa.func.now())
    item = db.relationship("ContentItem")
    source = db.relationship("ContentSource")
    policy_revision = db.relationship("ClassificationRevision")
    supersedes = db.relationship("SegmentOverride", remote_side=[id], foreign_keys=[supersedes_id])
    admin = db.relationship("Admin")
    __table_args__ = (enum_check("label", CLASSIFICATION_LABELS, "ck_segment_overrides_label"),)


class SourceClassificationRule(db.Model):
    __tablename__ = "source_classification_rules"
    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.Integer, db.ForeignKey("content_sources.id", ondelete="CASCADE"), nullable=False, index=True)
    originating_override_id = db.Column(db.Integer, db.ForeignKey("segment_overrides.id", ondelete="SET NULL"), index=True)
    fingerprint_sha256 = db.Column(db.String(64), nullable=False)
    label = db.Column(db.String(32), nullable=False)
    fingerprint_version = db.Column(db.String(128), nullable=False)
    supersedes_id = db.Column(db.Integer, db.ForeignKey("source_classification_rules.id", ondelete="SET NULL"), index=True)
    created_at = db.Column(UTCDateTime(), nullable=False, default=utc_now, server_default=sa.func.now())
    source = db.relationship("ContentSource")
    originating_override = db.relationship("SegmentOverride")
    supersedes = db.relationship("SourceClassificationRule", remote_side=[id], foreign_keys=[supersedes_id])
    __table_args__ = (
        sa.UniqueConstraint("source_id", "fingerprint_version", "fingerprint_sha256", "label",
            name="uq_source_classification_rules_fingerprint_label"),
        enum_check("label", CLASSIFICATION_LABELS, "ck_source_classification_rules_label"),
    )


class ClassificationSweep(Timestamps, db.Model):
    __tablename__ = "classification_sweeps"
    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.Integer, db.ForeignKey("content_sources.id", ondelete="CASCADE"), nullable=False, index=True)
    rule_id = db.Column(db.Integer, db.ForeignKey("source_classification_rules.id", ondelete="CASCADE"), nullable=False, index=True)
    state = db.Column(db.String(32), nullable=False, default="pending", server_default="pending", index=True)
    cursor_item_id = db.Column(db.Integer)
    items_scanned = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    items_queued = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    celery_task_id = db.Column(db.String(255))
    error_category = db.Column(db.String(64))
    started_at = db.Column(UTCDateTime())
    completed_at = db.Column(UTCDateTime())
    source = db.relationship("ContentSource")
    rule = db.relationship("SourceClassificationRule")
    __table_args__ = (
        enum_check("state", RULE_SCAN_STATES, "ck_classification_sweeps_state"),
        sa.CheckConstraint("items_scanned >= 0 AND items_queued >= 0", name="ck_classification_sweeps_counters"),
        sa.CheckConstraint("cursor_item_id IS NULL OR cursor_item_id > 0", name="ck_classification_sweeps_cursor"),
    )


class GuestExtraction(db.Model):
    """One restricted model-router guest extraction over an item's title and description."""

    __tablename__ = "guest_extractions"
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False, index=True)
    input_sha256 = db.Column(db.String(64), nullable=False)
    state = db.Column(db.String(16), nullable=False)
    guests = db.Column(db.JSON(none_as_null=True))
    error_category = db.Column(db.String(64))
    model = db.Column(db.String(128), nullable=False)
    cost_microusd = db.Column(db.BigInteger, nullable=False, default=0, server_default="0")
    created_at = db.Column(UTCDateTime(), nullable=False, default=utc_now, server_default=sa.func.now(), index=True)
    item = db.relationship("ContentItem")
    __table_args__ = (
        sa.UniqueConstraint("item_id", "input_sha256", name="uq_guest_extractions_item_input"),
        enum_check("state", GUEST_EXTRACTION_STATES, "ck_guest_extractions_state"),
        sa.CheckConstraint("cost_microusd >= 0", name="ck_guest_extractions_cost"),
        sa.CheckConstraint("(state = 'completed' AND guests IS NOT NULL AND error_category IS NULL) OR "
            "(state = 'failed' AND error_category IS NOT NULL)", name="ck_guest_extractions_outcome"),
    )


class KnowledgeExportBatch(Timestamps, db.Model):
    """One knowledge batch reserved, then written, into the podcast spool inbox."""

    __tablename__ = "knowledge_export_batches"
    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(db.String(32), nullable=False)
    run_date = db.Column(db.String(8), nullable=False)
    sequence = db.Column(db.Integer, nullable=False)
    state = db.Column(db.String(16), nullable=False, default="pending", server_default="pending", index=True)
    record_count = db.Column(db.Integer, nullable=False)
    blob_count = db.Column(db.Integer, nullable=False)
    error_category = db.Column(db.String(64))
    written_at = db.Column(UTCDateTime())
    items = db.relationship("KnowledgeItemExport", back_populates="batch", cascade="all, delete-orphan", passive_deletes=True)
    __table_args__ = (
        sa.UniqueConstraint("batch_id", name="uq_knowledge_export_batches_batch_id"),
        sa.UniqueConstraint("run_date", "sequence", name="uq_knowledge_export_batches_run_sequence"),
        enum_check("state", KNOWLEDGE_BATCH_STATES, "ck_knowledge_export_batches_state"),
        sa.CheckConstraint("sequence BETWEEN 1 AND 999", name="ck_knowledge_export_batches_sequence"),
        sa.CheckConstraint("record_count BETWEEN 1 AND 10000 AND blob_count >= 0", name="ck_knowledge_export_batches_counts"),
    )


class KnowledgeItemExport(db.Model):
    """The episode record (and its passages) one batch carries for one item."""

    __tablename__ = "knowledge_item_exports"
    id = db.Column(db.Integer, primary_key=True)
    export_batch_id = db.Column(db.Integer, db.ForeignKey("knowledge_export_batches.id", ondelete="CASCADE"), nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False, index=True)
    # A historical identifier embedded in record IDs, deliberately not a foreign key.
    classification_revision_id = db.Column(db.Integer, nullable=False)
    generation = db.Column(db.Integer, nullable=False)
    episode_record_id = db.Column(db.String(512), nullable=False)
    payload_sha256 = db.Column(db.String(64), nullable=False)
    passage_count = db.Column(db.Integer, nullable=False)
    supersedes_record_id = db.Column(db.String(512))
    created_at = db.Column(UTCDateTime(), nullable=False, default=utc_now, server_default=sa.func.now())
    batch = db.relationship("KnowledgeExportBatch", back_populates="items")
    item = db.relationship("ContentItem")
    __table_args__ = (
        sa.UniqueConstraint("episode_record_id", name="uq_knowledge_item_exports_episode_record_id"),
        sa.UniqueConstraint("item_id", "classification_revision_id", "generation", name="uq_knowledge_item_exports_generation"),
        sa.CheckConstraint("generation > 0 AND passage_count >= 0", name="ck_knowledge_item_exports_counts"),
    )
