"""Persist immutable classification revisions and correction history.

Revision ID: 0002_commercial_classification
Revises: 0001_media_foundation

This schema snapshot deliberately imports no live models or policy code.
"""

from contextlib import contextmanager

from alembic import op
import sqlalchemy as sa


revision = "0002_commercial_classification"
down_revision = "0001_media_foundation"
branch_labels = None
depends_on = None


def timestamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def counter(name, *, large=False):
    return sa.Column(name, sa.BigInteger if large else sa.Integer, nullable=False, server_default="0")


def indexes(table, *columns):
    for column in columns:
        op.create_index(f"ix_{table}_{column}", table, [column])


@contextmanager
def alter_item_pointer():
    """SQLite batch rebuilds must not cascade-delete the item's dependents.

    PRAGMA foreign_keys is ineffective inside a SQLite transaction. The short
    autocommit block applies the setting before batch copy/drop and restores it
    afterward. PostgreSQL retains its normal transactional ALTER behavior.
    """
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        with op.get_context().autocommit_block():
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            try:
                with op.batch_alter_table("content_items") as batch:
                    yield batch
            finally:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    else:
        with op.batch_alter_table("content_items") as batch:
            yield batch


def upgrade():
    op.create_table(
        "classification_revisions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("item_id", sa.Integer, sa.ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", sa.Integer, sa.ForeignKey("content_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("previous_revision_id", sa.Integer, sa.ForeignKey("classification_revisions.id", ondelete="SET NULL")),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("request_reason", sa.String(32), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="pending"),
        counter("attempts"),
        sa.Column("celery_task_id", sa.String(255)),
        sa.Column("transcript_sha256", sa.String(64), nullable=False),
        sa.Column("segments_sha256", sa.String(64), nullable=False),
        sa.Column("input_identity_sha256", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("policy_version", sa.String(128), nullable=False),
        sa.Column("prompt_version", sa.String(128), nullable=False),
        sa.Column("prompt_sha256", sa.String(64), nullable=False),
        sa.Column("requested_model", sa.String(128), nullable=False),
        sa.Column("resolved_model", sa.String(128)),
        sa.Column("classifier_cli_version", sa.String(128), nullable=False),
        sa.Column("source_rule_set_sha256", sa.String(64), nullable=False),
        sa.Column("override_set_sha256", sa.String(64), nullable=False),
        sa.Column("policy_s3_key", sa.String(2048)),
        sa.Column("policy_sha256", sa.String(64)),
        sa.Column("projection_s3_key", sa.String(2048)),
        sa.Column("projection_sha256", sa.String(64)),
        counter("included_segments"), counter("excluded_segments"), counter("uncertain_segments"),
        counter("included_milliseconds", large=True), counter("excluded_milliseconds", large=True),
        counter("uncertain_milliseconds", large=True), counter("total_cost_microusd", large=True),
        sa.Column("review_required", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("error_category", sa.String(64)),
        *timestamps(),
        sa.UniqueConstraint("item_id", "transcript_sha256", "revision", name="uq_classification_revisions_item_transcript_revision"),
        sa.UniqueConstraint("item_id", "idempotency_key", name="uq_classification_revisions_item_idempotency"),
        sa.CheckConstraint("state IN ('pending', 'running', 'retrying', 'completed', 'failed', 'superseded')", name="ck_classification_revisions_state"),
        sa.CheckConstraint("request_reason IN ('transcript_completed', 'backfill', 'override', 'policy_upgrade', 'source_rule', 'manual_retry', 'live_smoke')", name="ck_classification_revisions_reason"),
        sa.CheckConstraint("revision > 0", name="ck_classification_revisions_revision"),
        sa.CheckConstraint("attempts >= 0", name="ck_classification_revisions_attempts"),
        sa.CheckConstraint("included_segments >= 0 AND excluded_segments >= 0 AND uncertain_segments >= 0 "
            "AND included_milliseconds >= 0 AND excluded_milliseconds >= 0 AND uncertain_milliseconds >= 0 "
            "AND total_cost_microusd >= 0", name="ck_classification_revisions_totals"),
        sa.CheckConstraint("state != 'completed' OR (policy_s3_key IS NOT NULL AND policy_sha256 IS NOT NULL "
            "AND projection_s3_key IS NOT NULL AND projection_sha256 IS NOT NULL "
            "AND resolved_model IS NOT NULL AND completed_at IS NOT NULL AND error_category IS NULL)", name="ck_classification_revisions_completed"),
    )
    indexes("classification_revisions", "item_id", "source_id", "previous_revision_id", "state")
    op.create_table(
        "classification_blocks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("revision_id", sa.Integer, sa.ForeignKey("classification_revisions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="pending"),
        counter("attempts"),
        sa.Column("target_first_segment_id", sa.String(27), nullable=False),
        sa.Column("target_last_segment_id", sa.String(27), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("decision_s3_key", sa.String(2048)),
        sa.Column("decision_sha256", sa.String(64)),
        counter("cost_microusd", large=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("error_category", sa.String(64)),
        *timestamps(),
        sa.UniqueConstraint("revision_id", "ordinal", name="uq_classification_blocks_revision_ordinal"),
        sa.CheckConstraint("state IN ('pending', 'running', 'completed', 'failed', 'superseded')", name="ck_classification_blocks_state"),
        sa.CheckConstraint("ordinal > 0", name="ck_classification_blocks_ordinal"),
        sa.CheckConstraint("attempts >= 0 AND cost_microusd >= 0", name="ck_classification_blocks_counters"),
        sa.CheckConstraint("state != 'completed' OR (decision_s3_key IS NOT NULL AND decision_sha256 IS NOT NULL "
            "AND completed_at IS NOT NULL)", name="ck_classification_blocks_completed"),
    )
    indexes("classification_blocks", "revision_id", "state")
    op.create_table(
        "segment_overrides",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("item_id", sa.Integer, sa.ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", sa.Integer, sa.ForeignKey("content_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("transcript_sha256", sa.String(64), nullable=False),
        sa.Column("policy_revision_id", sa.Integer, sa.ForeignKey("classification_revisions.id", ondelete="SET NULL")),
        sa.Column("first_segment_id", sa.String(27), nullable=False),
        sa.Column("last_segment_id", sa.String(27), nullable=False),
        sa.Column("label", sa.String(32), nullable=False),
        sa.Column("range_fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column("supersedes_id", sa.Integer, sa.ForeignKey("segment_overrides.id", ondelete="SET NULL")),
        sa.Column("admin_id", sa.Integer, sa.ForeignKey("admins.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("label IN ('editorial', 'paid_commercial', 'self_promotion', 'cross_promotion', 'housekeeping', 'uncertain')", name="ck_segment_overrides_label"),
    )
    indexes("segment_overrides", "item_id", "source_id", "policy_revision_id", "supersedes_id", "admin_id")
    op.create_table(
        "source_classification_rules",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source_id", sa.Integer, sa.ForeignKey("content_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("originating_override_id", sa.Integer, sa.ForeignKey("segment_overrides.id", ondelete="SET NULL")),
        sa.Column("fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column("label", sa.String(32), nullable=False),
        sa.Column("fingerprint_version", sa.String(128), nullable=False),
        sa.Column("supersedes_id", sa.Integer, sa.ForeignKey("source_classification_rules.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("source_id", "fingerprint_version", "fingerprint_sha256", "label", name="uq_source_classification_rules_fingerprint_label"),
        sa.CheckConstraint("label IN ('editorial', 'paid_commercial', 'self_promotion', 'cross_promotion', 'housekeeping', 'uncertain')", name="ck_source_classification_rules_label"),
    )
    indexes("source_classification_rules", "source_id", "originating_override_id", "supersedes_id")
    op.create_table(
        "classification_sweeps",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source_id", sa.Integer, sa.ForeignKey("content_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rule_id", sa.Integer, sa.ForeignKey("source_classification_rules.id", ondelete="CASCADE"), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("cursor_item_id", sa.Integer),
        counter("items_scanned"), counter("items_queued"),
        sa.Column("celery_task_id", sa.String(255)),
        sa.Column("error_category", sa.String(64)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        *timestamps(),
        sa.CheckConstraint("state IN ('pending', 'running', 'completed', 'failed')", name="ck_classification_sweeps_state"),
        sa.CheckConstraint("items_scanned >= 0 AND items_queued >= 0", name="ck_classification_sweeps_counters"),
        sa.CheckConstraint("cursor_item_id IS NULL OR cursor_item_id > 0", name="ck_classification_sweeps_cursor"),
    )
    indexes("classification_sweeps", "source_id", "rule_id", "state")
    # Add the cyclic edge only after every target exists.
    with alter_item_pointer() as batch:
        batch.add_column(sa.Column("active_classification_revision_id", sa.Integer))
        batch.create_foreign_key("fk_content_items_active_classification_revision", "classification_revisions",
            ["active_classification_revision_id"], ["id"], ondelete="SET NULL")
        batch.create_index("ix_content_items_active_classification_revision_id", ["active_classification_revision_id"])


def downgrade():
    # Break the cycle before dropping the revision table.
    with alter_item_pointer() as batch:
        batch.drop_index("ix_content_items_active_classification_revision_id")
        batch.drop_constraint("fk_content_items_active_classification_revision", type_="foreignkey")
        batch.drop_column("active_classification_revision_id")
    op.drop_table("classification_sweeps")
    op.drop_table("source_classification_rules")
    op.drop_table("segment_overrides")
    op.drop_table("classification_blocks")
    op.drop_table("classification_revisions")
