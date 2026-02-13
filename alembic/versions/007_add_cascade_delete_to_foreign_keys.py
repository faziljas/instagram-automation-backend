"""Add CASCADE DELETE to all foreign key constraints.

This migration updates all existing foreign key constraints to include ON DELETE CASCADE,
ensuring that when a parent record is deleted, all related child records are automatically deleted.

Cascade chain:
- users → subscriptions, invoices, instagram_accounts, analytics_events, messages, 
         conversations, captured_leads, instagram_audience, dm_logs, instagram_global_trackers
- instagram_accounts → automation_rules, followers, messages, conversations, 
                      analytics_events, instagram_audience, dm_logs
- automation_rules → automation_rule_stats, captured_leads, analytics_events
- conversations → messages

Revision ID: 007_add_cascade_delete_to_foreign_keys
Revises: 006_add_auth_users_cascade_trigger
Create Date: 2026-02-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "007_add_cascade_delete_to_foreign_keys"
down_revision: Union[str, None] = "006_add_auth_users_cascade_trigger"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Update all foreign key constraints to include ON DELETE CASCADE.
    
    This ensures automatic deletion of related records when parent records are deleted.
    Dynamically finds FK constraints and updates them.
    """
    conn = op.get_bind()
    
    # List of FK relationships to update: (table_name, column_name, referenced_table, referenced_column)
    fk_relationships = [
        # Foreign keys referencing users.id
        ("subscriptions", "user_id", "users", "id"),
        ("invoices", "user_id", "users", "id"),
        ("instagram_accounts", "user_id", "users", "id"),
        ("analytics_events", "user_id", "users", "id"),
        ("messages", "user_id", "users", "id"),
        ("conversations", "user_id", "users", "id"),
        ("captured_leads", "user_id", "users", "id"),
        ("instagram_audience", "user_id", "users", "id"),
        ("dm_logs", "user_id", "users", "id"),
        ("instagram_global_trackers", "user_id", "users", "id"),
        
        # Foreign keys referencing instagram_accounts.id
        ("automation_rules", "instagram_account_id", "instagram_accounts", "id"),
        ("followers", "instagram_account_id", "instagram_accounts", "id"),
        ("messages", "instagram_account_id", "instagram_accounts", "id"),
        ("conversations", "instagram_account_id", "instagram_accounts", "id"),
        ("analytics_events", "instagram_account_id", "instagram_accounts", "id"),
        ("instagram_audience", "instagram_account_id", "instagram_accounts", "id"),
        ("dm_logs", "instagram_account_id", "instagram_accounts", "id"),
        ("captured_leads", "instagram_account_id", "instagram_accounts", "id"),
        
        # Foreign keys referencing automation_rules.id
        ("automation_rule_stats", "automation_rule_id", "automation_rules", "id"),
        ("captured_leads", "automation_rule_id", "automation_rules", "id"),
        ("analytics_events", "rule_id", "automation_rules", "id"),
        
        # Foreign keys referencing conversations.id
        ("messages", "conversation_id", "conversations", "id"),
    ]
    
    for table_name, column_name, ref_table, ref_column in fk_relationships:
        try:
            # Find the actual constraint name
            find_constraint_sql = sa.text("""
                SELECT tc.constraint_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu 
                    ON tc.constraint_name = kcu.constraint_name
                WHERE tc.table_name = :table_name
                    AND kcu.column_name = :column_name
                    AND tc.constraint_type = 'FOREIGN KEY'
                    AND tc.table_schema = 'public'
            """)
            result = conn.execute(find_constraint_sql, {
                "table_name": table_name,
                "column_name": column_name
            }).fetchone()
            
            if result:
                constraint_name = result[0]
                
                # Drop existing constraint
                drop_sql = sa.text(f'ALTER TABLE {table_name} DROP CONSTRAINT IF EXISTS "{constraint_name}"')
                conn.execute(drop_sql)
                
                # Recreate with CASCADE DELETE
                create_sql = sa.text(f"""
                    ALTER TABLE {table_name} 
                    ADD CONSTRAINT {constraint_name} 
                    FOREIGN KEY ({column_name}) 
                    REFERENCES {ref_table}({ref_column}) 
                    ON DELETE CASCADE
                """)
                conn.execute(create_sql)
                print(f"✅ Updated FK constraint {constraint_name} on {table_name}.{column_name} to CASCADE DELETE")
            else:
                print(f"⚠️ No FK constraint found for {table_name}.{column_name} → {ref_table}.{ref_column}")
        except Exception as e:
            print(f"⚠️ Error updating FK constraint on {table_name}.{column_name}: {e}")
            # Continue with other constraints
    
    conn.commit()
    print("✅ CASCADE DELETE migration completed")


def downgrade() -> None:
    """
    Remove CASCADE DELETE from foreign key constraints.
    Note: This will change constraints back to RESTRICT (default), which may cause
    deletion errors if child records exist.
    """
    conn = op.get_bind()
    
    # Same list of relationships
    fk_relationships = [
        ("subscriptions", "user_id", "users", "id"),
        ("invoices", "user_id", "users", "id"),
        ("instagram_accounts", "user_id", "users", "id"),
        ("analytics_events", "user_id", "users", "id"),
        ("messages", "user_id", "users", "id"),
        ("conversations", "user_id", "users", "id"),
        ("captured_leads", "user_id", "users", "id"),
        ("instagram_audience", "user_id", "users", "id"),
        ("dm_logs", "user_id", "users", "id"),
        ("instagram_global_trackers", "user_id", "users", "id"),
        ("automation_rules", "instagram_account_id", "instagram_accounts", "id"),
        ("followers", "instagram_account_id", "instagram_accounts", "id"),
        ("messages", "instagram_account_id", "instagram_accounts", "id"),
        ("conversations", "instagram_account_id", "instagram_accounts", "id"),
        ("analytics_events", "instagram_account_id", "instagram_accounts", "id"),
        ("instagram_audience", "instagram_account_id", "instagram_accounts", "id"),
        ("dm_logs", "instagram_account_id", "instagram_accounts", "id"),
        ("captured_leads", "instagram_account_id", "instagram_accounts", "id"),
        ("automation_rule_stats", "automation_rule_id", "automation_rules", "id"),
        ("captured_leads", "automation_rule_id", "automation_rules", "id"),
        ("analytics_events", "rule_id", "automation_rules", "id"),
        ("messages", "conversation_id", "conversations", "id"),
    ]
    
    for table_name, column_name, ref_table, ref_column in fk_relationships:
        try:
            # Find the actual constraint name
            find_constraint_sql = sa.text("""
                SELECT tc.constraint_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu 
                    ON tc.constraint_name = kcu.constraint_name
                WHERE tc.table_name = :table_name
                    AND kcu.column_name = :column_name
                    AND tc.constraint_type = 'FOREIGN KEY'
                    AND tc.table_schema = 'public'
            """)
            result = conn.execute(find_constraint_sql, {
                "table_name": table_name,
                "column_name": column_name
            }).fetchone()
            
            if result:
                constraint_name = result[0]
                
                # Drop constraint
                drop_sql = sa.text(f'ALTER TABLE {table_name} DROP CONSTRAINT IF EXISTS "{constraint_name}"')
                conn.execute(drop_sql)
                
                # Recreate without CASCADE (defaults to RESTRICT)
                create_sql = sa.text(f"""
                    ALTER TABLE {table_name} 
                    ADD CONSTRAINT {constraint_name} 
                    FOREIGN KEY ({column_name}) 
                    REFERENCES {ref_table}({ref_column})
                """)
                conn.execute(create_sql)
                print(f"✅ Removed CASCADE from FK constraint {constraint_name} on {table_name}")
        except Exception as e:
            print(f"⚠️ Error updating FK constraint on {table_name}.{column_name}: {e}")
    
    conn.commit()
    print("✅ CASCADE DELETE downgrade completed")
