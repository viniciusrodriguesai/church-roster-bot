"""Record coordinator notice authority without changing legacy message contents.

Revision ID: 331c6a8e4509
Revises: 231e4c9a7308
"""

import sqlalchemy as sa

from alembic import op

revision = "331c6a8e4509"
down_revision = "231e4c9a7308"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("outbound_messages") as batch:
        batch.add_column(sa.Column("coordinator_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("notice_ministry_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_outbound_coordinator_notice", "coordinators", ["coordinator_id"], ["id"]
        )
        batch.create_foreign_key(
            "fk_outbound_notice_ministry", "ministries", ["notice_ministry_id"], ["id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("outbound_messages") as batch:
        batch.drop_constraint("fk_outbound_notice_ministry", type_="foreignkey")
        batch.drop_constraint("fk_outbound_coordinator_notice", type_="foreignkey")
        batch.drop_column("notice_ministry_id")
        batch.drop_column("coordinator_id")
