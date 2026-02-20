"""Add TRIGGER_MATCHED to eventtype enum for analytics_events.

Revision ID: 012_add_trigger_matched
Revises: 011_add_phone_collected
Create Date: 2026-02-20

PostgreSQL enum 'eventtype' (used by analytics_events.event_type) did not
include 'trigger_matched', causing invalid input errors when querying or
inserting TRIGGER_MATCHED events. This migration adds the new value.
"""

from typing import Sequence, Union

from alembic import op
from alembic.utils.safe_enum_addition import safe_add_enum_value


revision: str = "012_add_trigger_matched"
down_revision: Union[str, None] = "011_add_phone_collected"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add new eventtype enum value 'trigger_matched' for TRIGGER_MATCHED events.
    # SQLAlchemy now uses enum VALUES (trigger_matched) not enum NAMES (TRIGGER_MATCHED) via values_callable.
    # Uses safe_add_enum_value helper to prevent transaction abort errors
    safe_add_enum_value('eventtype', 'trigger_matched')


def downgrade() -> None:
    # PostgreSQL does not support removing an enum value. Downgrade is a no-op.
    # To fully revert, you would need to recreate the type and column (data loss).
    pass
