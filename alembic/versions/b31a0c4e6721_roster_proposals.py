"""Persist scoped distribution drafts without changing existing assignments.

Revision ID: b31a0c4e6721
Revises: a27b6c1d4e90
"""

import sqlalchemy as sa

from alembic import op

revision = "b31a0c4e6721"
down_revision = "a27b6c1d4e90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "roster_proposals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ministry_id", sa.Integer(), sa.ForeignKey("ministries.id"), nullable=False),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("policy_version", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_by_type", sa.String(16), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column("unfilled", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft', 'approved', 'cancelled')", name="valid_proposal_status"
        ),
        sa.CheckConstraint(
            "created_by_type IN ('admin', 'coordinator')", name="valid_proposal_actor"
        ),
    )
    op.create_table(
        "roster_proposal_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "proposal_id", sa.Integer(), sa.ForeignKey("roster_proposals.id"), nullable=False
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("slot_id", sa.Integer(), sa.ForeignKey("slots.id"), nullable=False),
        sa.Column("volunteer_id", sa.Integer(), sa.ForeignKey("volunteers.id"), nullable=False),
        sa.Column("score", sa.JSON(), nullable=False),
        sa.UniqueConstraint("proposal_id", "position"),
        sa.UniqueConstraint("proposal_id", "slot_id", "volunteer_id"),
    )


def downgrade() -> None:
    op.drop_table("roster_proposal_items")
    op.drop_table("roster_proposals")
