"""Durable normal invitations and their outbox links; no assignments are changed.

Revision ID: c31a2e6b8904
Revises: b31a0c4e6721
"""

import sqlalchemy as sa

from alembic import op

revision = "c31a2e6b8904"
down_revision = "b31a0c4e6721"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "automation_invitations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("source_key", sa.String(128), nullable=False, unique=True),
        sa.Column("slot_id", sa.Integer(), sa.ForeignKey("slots.id"), nullable=False),
        sa.Column("volunteer_id", sa.Integer(), sa.ForeignKey("volunteers.id"), nullable=False),
        sa.Column(
            "proposal_item_id", sa.Integer(), sa.ForeignKey("roster_proposal_items.id"), unique=True
        ),
        sa.Column("replacement_request_id", sa.Integer(), sa.ForeignKey("replacement_requests.id")),
        sa.Column("assignment_id", sa.Integer(), sa.ForeignKey("assignments.id"), unique=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("service_fingerprint", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "kind IN ('distribution', 'waitlist', 'replacement', 'emergency')",
            name="valid_automation_invitation_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'declined', 'expired', 'cancelled')",
            name="valid_automation_invitation_status",
        ),
        sa.CheckConstraint("expires_at > created_at", name="valid_automation_invitation_expiry"),
    )
    op.create_index(
        "uq_pending_automation_pair",
        "automation_invitations",
        ["volunteer_id", "slot_id"],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    )
    with op.batch_alter_table("outbound_messages") as batch:
        batch.add_column(sa.Column("automation_invitation_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_outbound_automation_invitation",
            "automation_invitations",
            ["automation_invitation_id"],
            ["id"],
        )
        batch.create_unique_constraint(
            "uq_outbound_automation_invitation", ["automation_invitation_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("outbound_messages") as batch:
        batch.drop_constraint("uq_outbound_automation_invitation", type_="unique")
        batch.drop_constraint("fk_outbound_automation_invitation", type_="foreignkey")
        batch.drop_column("automation_invitation_id")
    op.drop_index("uq_pending_automation_pair", table_name="automation_invitations")
    op.drop_table("automation_invitations")
