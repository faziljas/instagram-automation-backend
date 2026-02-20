"""Add PHONE_COLLECTED to eventtype enum for analytics_events.

Revision ID: 011_add_phone_collected
Revises: 010_merge_heads
Create Date: 2026-02-20

PostgreSQL enum 'eventtype' (used by analytics_events.event_type) did not
include 'phone_collected', causing invalid input errors when querying or
inserting PHONE_COLLECTED events. This migration adds the new value.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "011_add_phone_collected"
down_revision: Union[str, None] = "010_merge_heads"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add new eventtype enum value so PHONE_COLLECTED is valid.
    # Error showed invalid value "PHONE_COLLECTED". Add both forms for enum name/value binding.
    import sqlalchemy as sa
    for value in ("phone_collected", "PHONE_COLLECTED"):
        try:
            op.execute(sa.text(f"ALTER TYPE eventtype ADD VALUE {repr(value)}"))
        except Exception as e:
            # Re-running migration: value already exists (e.g. duplicate_object / already exists)
            if "already exists" not in str(e).lower() and "duplicate" not in str(e).lower():
                raise


def downgrade() -> None:
    # PostgreSQL does not support removing an enum value. Downgrade is a no-op.
    # To fully revert, you would need to recreate the type and column (data loss).
    pass
