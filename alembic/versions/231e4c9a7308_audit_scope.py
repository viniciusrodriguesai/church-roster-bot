"""Preserve ministry provenance in audit history, retaining ambiguous legacy records.

Revision ID: 231e4c9a7308
Revises: 131d2b8a9607
"""

import sqlalchemy as sa

from alembic import op

revision = "231e4c9a7308"
down_revision = "131d2b8a9607"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("audit_events") as batch:
        batch.add_column(sa.Column("ministry_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_audit_ministry", "ministries", ["ministry_id"], ["id"])
        batch.create_index("ix_audit_scope_id", ["church_id", "ministry_id", "id"])
    direct = " JOIN ministries m ON m.id=x.ministry_id"
    via_slot = (
        " JOIN slots t ON t.id=x.slot_id JOIN worship_services s ON s.id=t.service_id"
        " JOIN ministries m ON m.id=s.ministry_id"
    )
    paths = {
        "ministry": ("ministries m", "m.id"),
        "service": ("worship_services x" + direct, "x.id"),
        "role": ("roles x" + direct, "x.id"),
        "schedule": ("monthly_schedules x" + direct, "x.id"),
        "roster_proposal": ("roster_proposals x" + direct, "x.id"),
        "slot": (
            "slots x JOIN worship_services s ON s.id=x.service_id "
            "JOIN ministries m ON m.id=s.ministry_id",
            "x.id",
        ),
        "assignment": ("assignments x" + via_slot, "x.id"),
        "automation_invitation": ("automation_invitations x" + via_slot, "x.id"),
        "third_day_request": ("third_day_requests x" + via_slot, "x.id"),
        "waitlist_entry": ("waitlist_entries x" + via_slot, "x.id"),
        "replacement_request": (
            "replacement_requests x JOIN assignments a ON a.id=x.assignment_id "
            "JOIN slots t ON t.id=a.slot_id JOIN worship_services s ON s.id=t.service_id "
            "JOIN ministries m ON m.id=s.ministry_id",
            "x.id",
        ),
        "schedule_version": (
            "schedule_versions x JOIN monthly_schedules q ON q.id=x.schedule_id "
            "JOIN ministries m ON m.id=q.ministry_id",
            "x.id",
        ),
    }
    for kind, (path, key) in paths.items():
        op.execute(
            sa.text(
                f"UPDATE audit_events SET ministry_id=(SELECT m.id FROM {path} "
                f"WHERE {key}=audit_events.entity_id AND m.church_id=audit_events.church_id) "
                "WHERE entity_type=:kind"
            ).bindparams(kind=kind)
        )


def downgrade() -> None:
    with op.batch_alter_table("audit_events") as batch:
        batch.drop_index("ix_audit_scope_id")
        batch.drop_constraint("fk_audit_ministry", type_="foreignkey")
        batch.drop_column("ministry_id")
