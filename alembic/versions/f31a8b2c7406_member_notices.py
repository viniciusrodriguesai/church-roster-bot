"""Link proactive member notices for consent revalidation; preserve legacy outbox rows.

Revision ID: f31a8b2c7406
Revises: e31b4a6c8205
"""

import sqlalchemy as sa

from alembic import op

revision = "f31a8b2c7406"
down_revision = "e31b4a6c8205"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("outbound_messages") as batch:
        batch.add_column(sa.Column("volunteer_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_outbound_volunteer_notice", "volunteers", ["volunteer_id"], ["id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("outbound_messages") as batch:
        batch.drop_constraint("fk_outbound_volunteer_notice", type_="foreignkey")
        batch.drop_column("volunteer_id")
