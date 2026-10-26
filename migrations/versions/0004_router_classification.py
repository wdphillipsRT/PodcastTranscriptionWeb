"""Classify through the Platform Model Router.

Revision ID: 0004_router_classification
Revises: 0003_knowledge_export

Adds `classification_blocks.request_generation`: the generation of the block's
router idempotency key (`podcast.classification:rev-<id>:block-<n>:g<generation>`).
It advances only after the router may have stored an outcome for the current key,
so a retried block replays a lost response instead of generating twice, and a
stored failure is never replayed forever. Adds
`classification_blocks.request_generation_started_at`: when the current key
generation was first sent, so a key the router left in progress forever (it
restarted mid-generation) can be advanced. Adds `guest_extractions.router_failures`:
how many router failures were recorded for a guest-extraction input, which is
retried on later runs until the limit. The `model` decision source and the
`model_*`/`router_busy` error categories live in policy artifacts and in
unconstrained string columns, so they need no schema change.

This schema snapshot deliberately imports no live models.
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_router_classification"
down_revision = "0003_knowledge_export"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("classification_blocks",
                  sa.Column("request_generation", sa.Integer, nullable=False, server_default="0"))
    op.add_column("classification_blocks",
                  sa.Column("request_generation_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("guest_extractions",
                  sa.Column("router_failures", sa.Integer, nullable=False, server_default="0"))


def downgrade():
    op.drop_column("guest_extractions", "router_failures")
    op.drop_column("classification_blocks", "request_generation_started_at")
    op.drop_column("classification_blocks", "request_generation")
