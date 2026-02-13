"""Add unique constraint to conversations table to prevent duplicates.

This migration adds a unique constraint on (user_id, instagram_account_id, participant_id)
to prevent duplicate conversations from being created for the same participant.

Revision ID: 008_add_unique_constraint_conversations
Revises: 007_add_cascade_delete_to_foreign_keys
Create Date: 2026-02-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "008_add_unique_constraint_conversations"
down_revision: Union[str, None] = "007_add_cascade_delete_to_foreign_keys"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Add unique constraint to prevent duplicate conversations.
    First, remove any existing duplicates, then add the constraint.
    """
    conn = op.get_bind()
    
    # Step 1: Find and remove duplicate conversations
    # Keep the one with the most recent updated_at, delete the rest
    print("🔍 Checking for duplicate conversations...")
    
    duplicates_query = sa.text("""
        SELECT user_id, instagram_account_id, participant_id, COUNT(*) as count
        FROM conversations
        GROUP BY user_id, instagram_account_id, participant_id
        HAVING COUNT(*) > 1
    """)
    
    duplicates = conn.execute(duplicates_query).fetchall()
    
    if duplicates:
        print(f"⚠️ Found {len(duplicates)} groups of duplicate conversations")
        
        for user_id, account_id, participant_id, count in duplicates:
            # Get all conversations for this participant
            convs_query = sa.text("""
                SELECT id, updated_at
                FROM conversations
                WHERE user_id = :user_id 
                  AND instagram_account_id = :account_id
                  AND participant_id = :participant_id
                ORDER BY updated_at DESC, id DESC
            """)
            
            convs = conn.execute(convs_query, {
                "user_id": user_id,
                "account_id": account_id,
                "participant_id": participant_id
            }).fetchall()
            
            if len(convs) > 1:
                # Keep the first one (most recent), delete the rest
                keep_id = convs[0][0]
                delete_ids = [conv[0] for conv in convs[1:]]
                
                # Move messages from deleted conversations to the kept one
                for delete_id in delete_ids:
                    update_messages = sa.text("""
                        UPDATE messages
                        SET conversation_id = :keep_id
                        WHERE conversation_id = :delete_id
                    """)
                    conn.execute(update_messages, {
                        "keep_id": keep_id,
                        "delete_id": delete_id
                    })
                
                # Delete duplicate conversations
                delete_convs = sa.text("""
                    DELETE FROM conversations
                    WHERE id = ANY(:delete_ids)
                """)
                # Convert to PostgreSQL array format
                delete_ids_str = "{" + ",".join(str(did) for did in delete_ids) + "}"
                conn.execute(sa.text(f"""
                    DELETE FROM conversations
                    WHERE id = ANY(ARRAY[{','.join(str(did) for did in delete_ids)}])
                """))
                
                print(f"✅ Merged {len(delete_ids)} duplicate conversations into conversation {keep_id}")
        
        conn.commit()
    
    # Step 2: Add unique constraint
    try:
        # Check if constraint already exists
        constraint_check = sa.text("""
            SELECT constraint_name
            FROM information_schema.table_constraints
            WHERE table_name = 'conversations'
              AND constraint_type = 'UNIQUE'
              AND constraint_name = 'uq_conversations_user_account_participant'
        """)
        
        existing = conn.execute(constraint_check).fetchone()
        
        if not existing:
            # Add unique constraint
            op.create_unique_constraint(
                'uq_conversations_user_account_participant',
                'conversations',
                ['user_id', 'instagram_account_id', 'participant_id']
            )
            print("✅ Added unique constraint on (user_id, instagram_account_id, participant_id)")
        else:
            print("✅ Unique constraint already exists")
    except Exception as e:
        print(f"⚠️ Error adding unique constraint: {e}")
        # Don't fail migration if constraint already exists
        if "already exists" not in str(e).lower() and "duplicate" not in str(e).lower():
            raise


def downgrade() -> None:
    """Remove unique constraint."""
    try:
        op.drop_constraint(
            'uq_conversations_user_account_participant',
            'conversations',
            type_='unique'
        )
        print("✅ Removed unique constraint")
    except Exception as e:
        print(f"⚠️ Error removing unique constraint: {e}")
