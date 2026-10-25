"""Optional reminder policies and one durable notification per assignment.

Revision ID: 031c7a9d2458
Revises: f31a8b2c7406
"""

import sqlalchemy as sa

from alembic import op

revision = "031c7a9d2458"
down_revision = "f31a8b2c7406"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reminder_policies",
        sa.Column("ministry_id", sa.Integer(), sa.ForeignKey("ministries.id"), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("days_before", sa.Integer(), nullable=False),
        sa.Column("local_time", sa.Time(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("days_before BETWEEN 0 AND 30", name="valid_reminder_days"),
    )
    with op.batch_alter_table("outbound_messages") as batch:
        batch.add_column(sa.Column("reminder_assignment_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("reminder_fingerprint", sa.String(64), nullable=True))
        batch.add_column(sa.Column("reminder_due_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key(
            "fk_outbound_reminder", "assignments", ["reminder_assignment_id"], ["id"]
        )
        batch.create_unique_constraint("uq_outbound_reminder", ["reminder_assignment_id"])


def downgrade() -> None:
    with op.batch_alter_table("outbound_messages") as batch:
        batch.drop_constraint("uq_outbound_reminder", type_="unique")
        batch.drop_constraint("fk_outbound_reminder", type_="foreignkey")
        batch.drop_column("reminder_due_at")
        batch.drop_column("reminder_fingerprint")
        batch.drop_column("reminder_assignment_id")
    op.drop_table("reminder_policies")
