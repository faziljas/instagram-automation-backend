"""Add Dodo Payments columns to subscriptions table.

Revision ID: 003_dodo_payments_columns
Revises: 002_legacy_schema
Create Date: 2026-02-10
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "003_dodo_payments_columns"
down_revision: Union[str, None] = "002_legacy_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add Dodo-specific subscription/customer identifiers (idempotent)."""
    conn = op.get_bind()

    # Dodo subscription ID (separate from Stripe)
    conn.execute(
        sa.text(
            "ALTER TABLE subscriptions "
            "ADD COLUMN IF NOT EXISTS dodo_subscription_id VARCHAR(255) UNIQUE"
        )
    )

    # Dodo customer ID used for billing portal, etc.
    conn.execute(
        sa.text(
            "ALTER TABLE subscriptions "
            "ADD COLUMN IF NOT EXISTS dodo_customer_id VARCHAR(255)"
        )
    )


def downgrade() -> None:
    """No-op downgrade (keep columns; safe for existing deployments)."""
    pass

