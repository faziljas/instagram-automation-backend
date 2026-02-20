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
from alembic.utils.safe_enum_addition import safe_add_enum_value


revision: str = "011_add_phone_collected"
down_revision: Union[str, None] = "010_merge_heads"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add new eventtype enum value 'phone_collected' for PHONE_COLLECTED events.
    # SQLAlchemy now uses enum VALUES (phone_collected) not enum NAMES (PHONE_COLLECTED) via values_callable.
    # Uses safe_add_enum_value helper to prevent transaction abort errors
    safe_add_enum_value('eventtype', 'phone_collected')


def downgrade() -> None:
    # PostgreSQL does not support removing an enum value. Downgrade is a no-op.
    # To fully revert, you would need to recreate the type and column (data loss).
    pass
