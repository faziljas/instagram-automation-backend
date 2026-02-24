"""Ensure notification preference columns exist on users (idempotent).

Revision ID: 015_ensure_notification_columns
Revises: 014_merge_heads
Create Date: 2026-02-24

Use ADD COLUMN IF NOT EXISTS so this fixes DBs where 013 did not run (e.g. deploy order).
Safe to run even if columns already exist.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "015_ensure_notification_columns"
down_revision: Union[str, None] = "014_merge_heads"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS notify_product_updates BOOLEAN NOT NULL DEFAULT true;
            """
        )
    )
    conn.execute(
        sa.text(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS notify_billing BOOLEAN NOT NULL DEFAULT true;
            """
        )
    )


def downgrade() -> None:
    pass
