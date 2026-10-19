"""Initial source-neutral media foundation.

Revision ID: 0001_media_foundation
Revises: None

Keep this revision independent of live model code so future model changes do
not change the schema produced by historical upgrades.
"""

from alembic import op
import sqlalchemy as sa


revision = "0001_media_foundation"
down_revision = None
branch_labels = None
depends_on = None


def timestamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def upgrade():
    op.create_table(
        "admins",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("singleton_key", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        *timestamps(),
        sa.UniqueConstraint("singleton_key", name="uq_admins_singleton_key"),
        sa.UniqueConstraint("email", name="uq_admins_email"),
        sa.CheckConstraint("singleton_key = true", name="ck_admins_singleton_true"),
    )
    op.create_table(
        "content_sources",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("admin_id", sa.Integer, sa.ForeignKey("admins.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(1024), nullable=False),
        sa.Column("canonical_url", sa.String(4096), nullable=False),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("image_url", sa.String(4096)),
        sa.Column("provider_metadata", sa.JSON, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("initial_item_limit", sa.Integer, nullable=False, server_default="25"),
        sa.Column("discovery_cursor", sa.Text),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("next_check_at", sa.DateTime(timezone=True)),
        sa.Column("error_category", sa.String(64)),
        *timestamps(),
        sa.UniqueConstraint("source_type", "external_id", name="uq_content_sources_identity"),
        sa.CheckConstraint("source_type IN ('podcast_rss', 'youtube_channel', 'youtube_playlist', 'youtube_video')", name="ck_content_sources_source_type"),
        sa.CheckConstraint("initial_item_limit > 0", name="ck_content_sources_initial_item_limit"),
    )
    op.create_index("ix_content_sources_admin_id", "content_sources", ["admin_id"])
    op.create_index("ix_content_sources_enabled", "content_sources", ["enabled"])
    op.create_table(
        "content_items",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("admin_id", sa.Integer, sa.ForeignKey("admins.id", ondelete="CASCADE"), nullable=False),
        sa.Column("item_type", sa.String(32), nullable=False),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(1024), nullable=False),
        sa.Column("canonical_url", sa.String(4096), nullable=False),
        sa.Column("remote_media_url", sa.String(4096)),
        sa.Column("title", sa.String(1000), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("duration_seconds", sa.Integer),
        sa.Column("creator_id", sa.String(255)),
        sa.Column("creator_name", sa.String(500)),
        sa.Column("channel_id", sa.String(255)),
        sa.Column("channel_name", sa.String(500)),
        sa.Column("channel_url", sa.String(4096)),
        sa.Column("thumbnail_url", sa.String(4096)),
        sa.Column("status", sa.String(32), nullable=False, server_default="discovered"),
        sa.Column("processing_stage", sa.String(32)),
        sa.Column("last_successful_stage", sa.String(32)),
        sa.Column("error_category", sa.String(64)),
        sa.Column("transcript_origin", sa.String(32)),
        sa.Column("media_s3_key", sa.String(2048)),
        sa.Column("media_sha256", sa.String(64)),
        sa.Column("provider_transcript_s3_key", sa.String(2048)),
        sa.Column("provider_transcript_sha256", sa.String(64)),
        sa.Column("transcript_s3_key", sa.String(2048)),
        sa.Column("transcript_sha256", sa.String(64)),
        sa.Column("segments_s3_key", sa.String(2048)),
        sa.Column("segments_sha256", sa.String(64)),
        sa.Column("transcription_provider", sa.String(64)),
        sa.Column("transcription_job_name", sa.String(200)),
        sa.Column("language", sa.String(16)),
        *timestamps(),
        sa.UniqueConstraint("platform", "external_id", name="uq_content_items_identity"),
        sa.CheckConstraint("item_type IN ('podcast_episode', 'youtube_video')", name="ck_content_items_item_type"),
        sa.CheckConstraint("platform IN ('podcast', 'youtube')", name="ck_content_items_platform"),
        sa.CheckConstraint("(platform = 'podcast' AND item_type = 'podcast_episode') OR (platform = 'youtube' AND item_type = 'youtube_video')", name="ck_content_items_platform_type"),
        sa.CheckConstraint("status IN ('discovered', 'acquiring', 'transcribing', 'completed', 'failed', 'deferred', 'unavailable')", name="ck_content_items_status"),
        sa.CheckConstraint("processing_stage IN ('podcast_audio', 'youtube_captions', 'youtube_audio', 'transcribe', 'normalize')", name="ck_content_items_processing_stage"),
        sa.CheckConstraint("last_successful_stage IN ('podcast_audio', 'youtube_captions', 'youtube_audio', 'transcribe', 'normalize')", name="ck_content_items_last_successful_stage"),
        sa.CheckConstraint("transcript_origin IN ('creator_caption', 'automatic_caption', 'amazon_transcribe')", name="ck_content_items_transcript_origin"),
        sa.CheckConstraint("duration_seconds IS NULL OR duration_seconds >= 0", name="ck_content_items_duration"),
    )
    op.create_index("ix_content_items_admin_id", "content_items", ["admin_id"])
    op.create_index("ix_content_items_status", "content_items", ["status"])
    op.create_index("ix_content_items_published_at", "content_items", ["published_at"])
    op.create_table(
        "source_items",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source_id", sa.Integer, sa.ForeignKey("content_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("item_id", sa.Integer, sa.ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False),
        *timestamps(),
        sa.UniqueConstraint("source_id", "item_id", name="uq_source_items_membership"),
    )
    op.create_index("ix_source_items_source_id", "source_items", ["source_id"])
    op.create_index("ix_source_items_item_id", "source_items", ["item_id"])
    op.create_table(
        "backfill_requests",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source_id", sa.Integer, sa.ForeignKey("content_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("selector_type", sa.String(32), nullable=False),
        sa.Column("start_date", sa.Date),
        sa.Column("end_date", sa.Date),
        sa.Column("requested_count", sa.Integer),
        sa.Column("explicit_url", sa.String(4096)),
        sa.Column("state", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("cursor", sa.Text),
        sa.Column("items_scanned", sa.Integer, nullable=False, server_default="0"),
        sa.Column("items_accepted", sa.Integer, nullable=False, server_default="0"),
        sa.Column("items_queued", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cancel_requested", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("error_category", sa.String(64)),
        *timestamps(),
        sa.CheckConstraint("selector_type IN ('date_range', 'count', 'explicit_url')", name="ck_backfill_requests_selector_type"),
        sa.CheckConstraint(
            "(selector_type = 'date_range' AND start_date IS NOT NULL AND end_date IS NOT NULL "
            "AND start_date <= end_date AND requested_count IS NULL AND explicit_url IS NULL) OR "
            "(selector_type = 'count' AND requested_count IS NOT NULL AND requested_count > 0 "
            "AND start_date IS NULL AND end_date IS NULL AND explicit_url IS NULL) OR "
            "(selector_type = 'explicit_url' AND explicit_url IS NOT NULL AND length(trim(explicit_url)) > 0 "
            "AND start_date IS NULL AND end_date IS NULL AND requested_count IS NULL)",
            name="ck_backfill_requests_exactly_one_selector"),
        sa.CheckConstraint("state IN ('pending', 'running', 'completed', 'cancelled', 'failed')", name="ck_backfill_requests_state"),
        sa.CheckConstraint("items_scanned >= 0 AND items_accepted >= 0 AND items_queued >= 0", name="ck_backfill_requests_counters"),
    )
    op.create_index("ix_backfill_requests_source_id", "backfill_requests", ["source_id"])
    op.create_index("ix_backfill_requests_state", "backfill_requests", ["state"])
    op.create_table(
        "discovery_runs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source_id", sa.Integer, sa.ForeignKey("content_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("backfill_id", sa.Integer, sa.ForeignKey("backfill_requests.id", ondelete="SET NULL")),
        sa.Column("run_type", sa.String(32), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("cursor", sa.Text),
        sa.Column("items_scanned", sa.Integer, nullable=False, server_default="0"),
        sa.Column("items_accepted", sa.Integer, nullable=False, server_default="0"),
        sa.Column("items_queued", sa.Integer, nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("error_category", sa.String(64)),
        *timestamps(),
        sa.CheckConstraint("run_type IN ('initial', 'scheduled', 'backfill')", name="ck_discovery_runs_run_type"),
        sa.CheckConstraint("state IN ('pending', 'scanning', 'finalizing', 'completed', 'cancelled', 'failed')", name="ck_discovery_runs_state"),
        sa.CheckConstraint("items_scanned >= 0 AND items_accepted >= 0 AND items_queued >= 0", name="ck_discovery_runs_counters"),
    )
    op.create_index("ix_discovery_runs_source_id", "discovery_runs", ["source_id"])
    op.create_index("ix_discovery_runs_backfill_id", "discovery_runs", ["backfill_id"])
    op.create_index("ix_discovery_runs_state", "discovery_runs", ["state"])
    op.create_table(
        "discovery_candidates",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("run_id", sa.Integer, sa.ForeignKey("discovery_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(1024), nullable=False),
        sa.Column("descriptor", sa.JSON(none_as_null=True)),
        *timestamps(),
        sa.UniqueConstraint("run_id", "platform", "external_id", name="uq_discovery_candidates_identity"),
        sa.CheckConstraint("platform IN ('podcast', 'youtube')", name="ck_discovery_candidates_platform"),
    )
    op.create_index("ix_discovery_candidates_run_id", "discovery_candidates", ["run_id"])
    op.create_table(
        "processing_jobs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("item_id", sa.Integer, sa.ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("celery_task_id", sa.String(255)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("error_category", sa.String(64)),
        *timestamps(),
        sa.UniqueConstraint("item_id", "stage", name="uq_processing_jobs_item_stage"),
        sa.CheckConstraint("stage IN ('podcast_audio', 'youtube_captions', 'youtube_audio', 'transcribe', 'normalize')", name="ck_processing_jobs_stage"),
        sa.CheckConstraint("state IN ('pending', 'running', 'retrying', 'completed', 'failed')", name="ck_processing_jobs_state"),
        sa.CheckConstraint("attempts >= 0", name="ck_processing_jobs_attempts"),
    )
    op.create_index("ix_processing_jobs_item_id", "processing_jobs", ["item_id"])
    op.create_index("ix_processing_jobs_state", "processing_jobs", ["state"])


def downgrade():
    op.drop_index("ix_processing_jobs_state", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_item_id", table_name="processing_jobs")
    op.drop_table("processing_jobs")
    op.drop_index("ix_discovery_candidates_run_id", table_name="discovery_candidates")
    op.drop_table("discovery_candidates")
    op.drop_index("ix_discovery_runs_state", table_name="discovery_runs")
    op.drop_index("ix_discovery_runs_backfill_id", table_name="discovery_runs")
    op.drop_index("ix_discovery_runs_source_id", table_name="discovery_runs")
    op.drop_table("discovery_runs")
    op.drop_index("ix_backfill_requests_state", table_name="backfill_requests")
    op.drop_index("ix_backfill_requests_source_id", table_name="backfill_requests")
    op.drop_table("backfill_requests")
    op.drop_index("ix_source_items_item_id", table_name="source_items")
    op.drop_index("ix_source_items_source_id", table_name="source_items")
    op.drop_table("source_items")
    op.drop_index("ix_content_items_published_at", table_name="content_items")
    op.drop_index("ix_content_items_status", table_name="content_items")
    op.drop_index("ix_content_items_admin_id", table_name="content_items")
    op.drop_table("content_items")
    op.drop_index("ix_content_sources_enabled", table_name="content_sources")
    op.drop_index("ix_content_sources_admin_id", table_name="content_sources")
    op.drop_table("content_sources")
    op.drop_table("admins")
