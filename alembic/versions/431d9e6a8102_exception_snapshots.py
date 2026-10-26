"""Preserve exceptional authorizations and record future service snapshots.

Revision ID: 431d9e6a8102
Revises: 331c6a8e4509
"""

import sqlalchemy as sa

from alembic import op

revision = "431d9e6a8102"
down_revision = "331c6a8e4509"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("third_day_requests") as batch:
        batch.add_column(sa.Column("service_fingerprint", sa.String(64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("third_day_requests") as batch:
        batch.drop_column("service_fingerprint")
