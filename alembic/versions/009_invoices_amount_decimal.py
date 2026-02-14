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
    # Change amount from INTEGER to NUMERIC(12,2) so exact values (e.g. 11.81) are stored.
    # Safe when column is already numeric: USING keeps current value. When integer: >=100 -> cents to dollars, <100 -> keep.
    op.execute(
        "ALTER TABLE public.invoices "
        "ALTER COLUMN amount TYPE NUMERIC(12, 2) USING "
        "(CASE WHEN amount >= 100 THEN amount / 100.0 ELSE amount::numeric END)"
    )


def downgrade() -> None:
    # Convert back to integer minor units (rounds to nearest cent)
    op.execute(
        "ALTER TABLE public.invoices "
        "ALTER COLUMN amount TYPE INTEGER USING ROUND(amount * 100)::INTEGER"
    )
