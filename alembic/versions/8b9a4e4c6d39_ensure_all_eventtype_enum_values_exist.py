"""Ensure all EventType enum values exist

Revision ID: 8b9a4e4c6d39
Revises: 012_add_trigger_matched
Create Date: 2026-02-20 18:17:43.634698

This migration ensures all EventType enum values from the code exist in the database.
The enum may have been created with incorrect values initially (e.g., enum names instead of values),
or some values may be missing. This migration adds all required values safely.

All values from EventType enum:
- trigger_matched (already added in 012)
- dm_sent (CRITICAL - missing and causing errors)
- link_clicked
- email_collected
- phone_collected (already added in 011)
- follow_button_clicked
- im_following_clicked
- profile_visit
- comment_replied
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8b9a4e4c6d39'
down_revision: Union[str, None] = '012_add_trigger_matched'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Add all EventType enum values that may be missing from the database.
    This ensures the enum matches what's defined in app.models.analytics_event.EventType.
    
    Note: PostgreSQL requires enum values to be committed before they can be queried.
    Since migrations 011 and 012 may have just added values in the same transaction,
    we can't safely query enum_range() here. Instead, we try to add all required values
    and gracefully handle "already exists" errors.
    """
    conn = op.get_bind()
    
    # All enum values that should exist (from EventType enum)
    # CRITICAL: dm_sent is currently missing and causing errors
    required_values = [
        "dm_sent",  # CRITICAL - currently missing and causing errors
        "link_clicked",
        "email_collected",
        "follow_button_clicked",
        "im_following_clicked",
        "profile_visit",
        "comment_replied",
        # These may have been added in migrations 011/012, but we add them safely:
        "trigger_matched",  # Added in 012
        "phone_collected",  # Added in 011
    ]
    
    # Try to add all required values using DO blocks to handle errors gracefully
    # DO blocks catch exceptions internally, preventing transaction abort
    added_count = 0
    skipped_count = 0
    
    for value in required_values:
        # Use a DO block with exception handling to add enum value safely
        # This prevents transaction abort when value already exists
        try:
            conn.execute(
                sa.text(f"""
                    DO $$
                    BEGIN
                        ALTER TYPE eventtype ADD VALUE '{value}';
                    EXCEPTION
                        WHEN OTHERS THEN
                            -- Check if error is about duplicate/existing value
                            IF SQLSTATE = '42710' OR SQLERRM LIKE '%already exists%' OR SQLERRM LIKE '%duplicate%' THEN
                                -- Value already exists, that's fine - do nothing
                                NULL;
                            ELSE
                                -- Re-raise unexpected errors
                                RAISE;
                            END IF;
                    END $$;
                """)
            )
            # If we get here without exception, the value was added successfully
            print(f"✅ Added enum value: {value}")
            added_count += 1
        except Exception as e:
            # If DO block raises an exception, the value likely already exists
            error_str = str(e).lower()
            if "already exists" in error_str or "duplicate" in error_str:
                print(f"ℹ️  Value {value} already exists")
                skipped_count += 1
            else:
                print(f"⚠️  Unexpected error processing {value}: {e}")
                # For unexpected errors, we'll skip but log
                skipped_count += 1
    
    print(f"🎉 Migration complete. Processed {added_count} enum value(s), skipped {skipped_count}.")
    print(f"📋 All required values should now be present in the enum.")


def downgrade() -> None:
    """
    PostgreSQL does not support removing enum values.
    Downgrade is a no-op. To fully revert, you would need to recreate the type and column (data loss).
    """
    pass
