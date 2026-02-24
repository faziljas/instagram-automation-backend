"""Merge heads: 013_notification_preferences and 8b9a4e4c6d39 (eventtype enum).

Revision ID: 014_merge_heads
Revises: 013_notification_preferences, 8b9a4e4c6d39
Create Date: 2026-02-24

Both branches already applied; this only merges the graph so 'alembic upgrade head' has a single target.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "014_merge_heads"
down_revision: Union[str, None] = ("013_notification_preferences", "8b9a4e4c6d39")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
