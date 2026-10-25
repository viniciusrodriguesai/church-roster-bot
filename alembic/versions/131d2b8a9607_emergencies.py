"""Opt-in emergency policy; no existing roster or invitation is modified.

Revision ID: 131d2b8a9607
Revises: 031c7a9d2458
"""

import sqlalchemy as sa

from alembic import op

revision = "131d2b8a9607"
down_revision = "031c7a9d2458"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "emergency_policies",
        sa.Column("ministry_id", sa.Integer(), sa.ForeignKey("ministries.id"), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("horizon_hours", sa.Integer(), nullable=False),
        sa.Column("response_minutes", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("horizon_hours BETWEEN 1 AND 72", name="valid_emergency_horizon"),
        sa.CheckConstraint("response_minutes BETWEEN 1 AND 120", name="valid_emergency_response"),
    )


def downgrade() -> None:
    op.drop_table("emergency_policies")
