"""Add notification preference columns to users table.

Revision ID: 013_notification_preferences
Revises: 012_add_trigger_matched
Create Date: 2026-02-24

Adds notify_product_updates and notify_billing to users for email preference toggles.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "013_notification_preferences"
down_revision: Union[str, None] = "012_add_trigger_matched"
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
