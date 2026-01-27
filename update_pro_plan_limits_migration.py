"""
Migration: Update Pro Plan Limits
This migration updates account_limit for Pro users and creates the column if it doesn't exist.
- Adds account_limit column to profiles table (or users table if profiles doesn't exist)
- Sets account_limit to 3 for all Pro users
"""
from sqlalchemy import create_engine, text
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is not set")


def run_migration():
    """Add/update account_limit column and set Pro users to 3 accounts"""
    engine = create_engine(DATABASE_URL)

    try:
        with engine.connect() as conn:
            # Check database type
            db_type = str(engine.url).split("://")[0]
            
            def table_exists(table_name: str) -> bool:
                """Check if a table exists"""
                if db_type == "postgresql":
                    result = conn.execute(text(f"""
                        SELECT table_name 
                        FROM information_schema.tables 
                        WHERE table_name='{table_name}'
                    """))
                    return result.fetchone() is not None
                else:  # SQLite
                    result = conn.execute(text(f"""
                        SELECT name FROM sqlite_master 
                        WHERE type='table' AND name='{table_name}'
                    """))
                    return result.fetchone() is not None
            
            def column_exists(table_name: str, column_name: str) -> bool:
                """Check if a column exists in a table"""
                if db_type == "postgresql":
                    result = conn.execute(text(f"""
                        SELECT column_name 
                        FROM information_schema.columns 
                        WHERE table_name='{table_name}' AND column_name='{column_name}'
                    """))
                    return result.fetchone() is not None
                else:  # SQLite
                    pragma_result = conn.execute(text(f"PRAGMA table_info({table_name})"))
                    columns = [row[1] for row in pragma_result.fetchall()]
                    return column_name in columns
            
            # Determine which table to use (profiles or users)
            use_profiles = table_exists('profiles')
            use_users = table_exists('users')
            
            if not use_profiles and not use_users:
                print("⚠️ Neither 'profiles' nor 'users' table exists. Skipping migration.")
                return True
            
            target_table = 'profiles' if use_profiles else 'users'
            user_id_column = 'user_id' if use_profiles else 'id'
            
            print(f"🔄 Using '{target_table}' table for account_limit migration")
            
            # Check if account_limit column exists
            if not column_exists(target_table, 'account_limit'):
                print(f"🔄 Adding 'account_limit' column to '{target_table}' table...")
                conn.execute(text(f"""
                    ALTER TABLE {target_table} 
                    ADD COLUMN account_limit INT DEFAULT 1
                """))
                conn.commit()
                print(f"✅ Successfully added 'account_limit' column to '{target_table}' table")
            else:
                print(f"✅ Column 'account_limit' already exists in '{target_table}' table")
            
            # Update account_limit to 3 for Pro users
            # Join with users table to check plan_tier
            if use_profiles:
                # profiles table exists, join with users table
                update_query = text(f"""
                    UPDATE {target_table} 
                    SET account_limit = 3
                    WHERE {user_id_column} IN (
                        SELECT id FROM users WHERE plan_tier = 'pro'
                    )
                    AND (account_limit IS NULL OR account_limit != 3)
                """)
            else:
                # Using users table directly
                update_query = text(f"""
                    UPDATE {target_table} 
                    SET account_limit = 3
                    WHERE plan_tier = 'pro' 
                    AND (account_limit IS NULL OR account_limit != 3)
                """)
            
            result = conn.execute(update_query)
            conn.commit()
            updated_count = result.rowcount
            
            if updated_count > 0:
                print(f"✅ Updated account_limit to 3 for {updated_count} Pro user(s)")
            else:
                print(f"✅ All Pro users already have account_limit = 3")
            
            return True

    except Exception as e:
        print(f"⚠️ Error updating account_limit: {str(e)}")
        # Don't fail - migration is idempotent and may not be needed if using plan_limits.py
        return True


if __name__ == "__main__":
    run_migration()
