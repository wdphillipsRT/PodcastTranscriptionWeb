"""Count provider failures per classification block.

Revision ID: 0005_block_provider_failures
Revises: 0004_router_classification

Adds `classification_blocks.provider_failures`: how many router-stored 5xx
outcomes (`router_busy` with reason `router_failure`) arrived after the model
had run for at least a minute on this block's input. The third fails the
revision as `model_unavailable` instead of retrying forever; fast failures (a
Forge outage) are not counted and stay `router_busy`.

This schema snapshot deliberately imports no live models.
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_block_provider_failures"
down_revision = "0004_router_classification"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("classification_blocks",
                  sa.Column("provider_failures", sa.Integer, nullable=False, server_default="0"))


def downgrade():
    op.drop_column("classification_blocks", "provider_failures")
