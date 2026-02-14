"""Merge multiple heads: 009_invoices_amount_decimal and add_analytics_composite_index.

Revision ID: 010_merge_heads
Revises: 009_invoices_amount_decimal, add_analytics_composite_index
Create Date: 2026-02-14

"""
from typing import Sequence, Union

from alembic import op


revision: str = "010_merge_heads"
down_revision: Union[str, None] = ("009_invoices_amount_decimal", "add_analytics_composite_index")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass  # Both parent migrations already applied; this only merges the branch


def downgrade() -> None:
    pass
