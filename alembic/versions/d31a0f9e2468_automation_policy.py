"""Add opt-in ministry automation policies, leaving existing schedules untouched.

Revision ID: d31a0f9e2468
Revises: c31a2e6b8904
"""

import sqlalchemy as sa

from alembic import op

revision = "d31a0f9e2468"
down_revision = "c31a2e6b8904"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "automation_policies",
        sa.Column("ministry_id", sa.Integer(), sa.ForeignKey("ministries.id"), primary_key=True),
        sa.Column("opening_enabled", sa.Boolean(), nullable=False),
        sa.Column("opening_day", sa.Integer(), nullable=False),
        sa.Column("opening_time", sa.Time(timezone=False), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("opening_day BETWEEN 1 AND 28", name="valid_opening_day"),
    )


def downgrade() -> None:
    op.drop_table("automation_policies")
