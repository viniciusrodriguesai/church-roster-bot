"""Persist FIFO waiting entries without reserving or changing existing assignments.

Revision ID: e31b4a6c8205
Revises: d31a0f9e2468
"""

import sqlalchemy as sa

from alembic import op

revision = "e31b4a6c8205"
down_revision = "d31a0f9e2468"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "waitlist_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("volunteer_id", sa.Integer(), sa.ForeignKey("volunteers.id"), nullable=False),
        sa.Column("slot_id", sa.Integer(), sa.ForeignKey("slots.id"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("service_fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "offer_id", sa.Integer(), sa.ForeignKey("automation_invitations.id"), unique=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('waiting', 'offered', 'accepted', 'declined', "
            "'expired', 'cancelled', 'ineligible')",
            name="valid_waitlist_status",
        ),
    )
    op.create_index(
        "uq_active_waitlist_pair",
        "waitlist_entries",
        ["volunteer_id", "slot_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('waiting', 'offered')"),
        postgresql_where=sa.text("status IN ('waiting', 'offered')"),
    )


def downgrade() -> None:
    op.drop_index("uq_active_waitlist_pair", table_name="waitlist_entries")
    op.drop_table("waitlist_entries")
