"""Record knowledge-export people, guest extractions, and export state.

Revision ID: 0003_knowledge_export
Revises: 0002_commercial_classification

This schema snapshot deliberately imports no live models or exporter code.
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_knowledge_export"
down_revision = "0002_commercial_classification"
branch_labels = None
depends_on = None

NEW_COLUMNS = {
    "content_sources": ("feed_hosts", "admin_hosts", "hosts_updated_at"),
    "content_items": ("feed_guests", "admin_guests", "guests_updated_at"),
}


def upgrade():
    # Nullable columns without constraints are added in place, so SQLite never
    # rebuilds (and cascades through) content_items.
    for table, (feed, admin, updated) in NEW_COLUMNS.items():
        op.add_column(table, sa.Column(feed, sa.JSON))
        op.add_column(table, sa.Column(admin, sa.JSON))
        op.add_column(table, sa.Column(updated, sa.DateTime(timezone=True)))
    op.create_table(
        "guest_extractions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("item_id", sa.Integer, sa.ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("guests", sa.JSON),
        sa.Column("error_category", sa.String(64)),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("cost_microusd", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("item_id", "input_sha256", name="uq_guest_extractions_item_input"),
        sa.CheckConstraint("state IN ('completed', 'failed')", name="ck_guest_extractions_state"),
        sa.CheckConstraint("cost_microusd >= 0", name="ck_guest_extractions_cost"),
        sa.CheckConstraint("(state = 'completed' AND guests IS NOT NULL AND error_category IS NULL) OR "
            "(state = 'failed' AND error_category IS NOT NULL)", name="ck_guest_extractions_outcome"),
    )
    op.create_index("ix_guest_extractions_item_id", "guest_extractions", ["item_id"])
    op.create_index("ix_guest_extractions_created_at", "guest_extractions", ["created_at"])
    op.create_table(
        "knowledge_export_batches",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("batch_id", sa.String(32), nullable=False),
        sa.Column("run_date", sa.String(8), nullable=False),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("record_count", sa.Integer, nullable=False),
        sa.Column("blob_count", sa.Integer, nullable=False),
        sa.Column("error_category", sa.String(64)),
        sa.Column("written_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("batch_id", name="uq_knowledge_export_batches_batch_id"),
        sa.UniqueConstraint("run_date", "sequence", name="uq_knowledge_export_batches_run_sequence"),
        sa.CheckConstraint("state IN ('pending', 'written', 'abandoned', 'failed')", name="ck_knowledge_export_batches_state"),
        sa.CheckConstraint("sequence BETWEEN 1 AND 999", name="ck_knowledge_export_batches_sequence"),
        sa.CheckConstraint("record_count BETWEEN 1 AND 10000 AND blob_count >= 0", name="ck_knowledge_export_batches_counts"),
    )
    op.create_index("ix_knowledge_export_batches_state", "knowledge_export_batches", ["state"])
    op.create_table(
        "knowledge_item_exports",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("export_batch_id", sa.Integer, sa.ForeignKey("knowledge_export_batches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("item_id", sa.Integer, sa.ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("classification_revision_id", sa.Integer, nullable=False),
        sa.Column("generation", sa.Integer, nullable=False),
        sa.Column("episode_record_id", sa.String(512), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("passage_count", sa.Integer, nullable=False),
        sa.Column("supersedes_record_id", sa.String(512)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("episode_record_id", name="uq_knowledge_item_exports_episode_record_id"),
        sa.UniqueConstraint("item_id", "classification_revision_id", "generation", name="uq_knowledge_item_exports_generation"),
        sa.CheckConstraint("generation > 0 AND passage_count >= 0", name="ck_knowledge_item_exports_counts"),
    )
    op.create_index("ix_knowledge_item_exports_export_batch_id", "knowledge_item_exports", ["export_batch_id"])
    op.create_index("ix_knowledge_item_exports_item_id", "knowledge_item_exports", ["item_id"])


def downgrade():
    op.drop_table("knowledge_item_exports")
    op.drop_table("knowledge_export_batches")
    op.drop_table("guest_extractions")
    for table, columns in NEW_COLUMNS.items():
        for column in reversed(columns):
            op.drop_column(table, column)
