"""Store invoice amount as decimal (major units) instead of integer (minor units).

Revision ID: 009_invoices_amount_decimal
Revises: 008_add_unique_constraint_conversations
Create Date: 2026-02-14

Converts invoices.amount from INTEGER (cents/minor units) to NUMERIC(12,2)
(major units, e.g. 11.81 for SGD 11.81). Existing values are converted by
dividing by 100.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "009_invoices_amount_decimal"
down_revision: Union[str, None] = "008_add_unique_constraint_conversations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Convert amount to NUMERIC(12,2) storing exact decimal (e.g. 11.81).
    # Existing data: values >= 100 were stored in minor units (cents) -> divide by 100;
    # values < 100 may have been wrongly stored as rounded dollars (e.g. 12 for 11.81) -> keep as-is.
    op.execute(
        """
        ALTER TABLE invoices
        ALTER COLUMN amount TYPE NUMERIC(12, 2) USING (
            CASE WHEN amount >= 100 THEN amount / 100.0 ELSE amount::numeric END
        )
        """
    )


def downgrade() -> None:
    # Convert back to integer minor units (rounds to nearest cent)
    op.execute(
        """
        ALTER TABLE invoices
        ALTER COLUMN amount TYPE INTEGER USING ROUND(amount * 100)::INTEGER
        """
    )
