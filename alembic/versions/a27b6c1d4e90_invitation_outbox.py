"""Link each invitation to one durable delivery and deduplicate notifications.

Revision ID: a27b6c1d4e90
Revises: 88ca949752d6
"""

import sqlalchemy as sa

from alembic import op

revision = "a27b6c1d4e90"
down_revision = "88ca949752d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("outbound_messages") as batch:
        batch.add_column(sa.Column("invitation_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("dedupe_key", sa.String(128), nullable=True))
        batch.create_foreign_key(
            "fk_outbound_invitation", "third_day_requests", ["invitation_id"], ["id"]
        )
        batch.create_unique_constraint("uq_outbound_invitation", ["invitation_id"])
        batch.create_unique_constraint("uq_outbound_dedupe_key", ["dedupe_key"])


def downgrade() -> None:
    with op.batch_alter_table("outbound_messages") as batch:
        batch.drop_constraint("uq_outbound_dedupe_key", type_="unique")
        batch.drop_constraint("uq_outbound_invitation", type_="unique")
        batch.drop_constraint("fk_outbound_invitation", type_="foreignkey")
        batch.drop_column("dedupe_key")
        batch.drop_column("invitation_id")
