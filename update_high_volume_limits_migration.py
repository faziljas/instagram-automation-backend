"""
Migration: High Volume Pricing Strategy - Update limits
This migration updates any database metadata related to DM and rule limits.
Since limits are primarily controlled by code constants, this migration is mainly
for updating any metadata stored in Supabase auth.users or other tables.

Note: The actual limits are updated in app/core/plan_limits.py:
- FREE_DM_LIMIT: 50 -> 1000
- FREE_RULE_LIMIT: 3 -> -1 (unlimited)
"""
from sqlalchemy import create_engine, text
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is not set")


def run_migration():
    """Update limits metadata if stored in database."""
    engine = create_engine(DATABASE_URL)

    try:
        with engine.connect() as conn:
            # Check database type
            db_type = str(engine.url).split("://")[0]
            
            if db_type == "postgresql":
                # Option 1: Update Supabase auth.users metadata if limits are stored there
                try:
                    # Check if we can access auth schema (Supabase)
                    result = conn.execute(text("""
                        SELECT EXISTS (
                            SELECT 1 FROM information_schema.schemata 
                            WHERE schema_name = 'auth'
                        )
                    """))
                    has_auth_schema = result.fetchone()[0]
                    
                    if has_auth_schema:
                        # Update auth.users metadata for dms_limit
                        conn.execute(text("""
                            UPDATE auth.users 
                            SET raw_user_meta_data = jsonb_set(
                                COALESCE(raw_user_meta_data, '{}'::jsonb),
                                '{dms_limit}',
                                '1000'::jsonb
                            )
                            WHERE (raw_user_meta_data->>'dms_limit')::int = 50 
                               OR raw_user_meta_data->>'dms_limit' IS NULL
                        """))
                        print("✅ Updated auth.users metadata for dms_limit (50 -> 1000)")
                        
                        # Update auth.users metadata for rule_limit (unlimited)
                        conn.execute(text("""
                            UPDATE auth.users 
                            SET raw_user_meta_data = jsonb_set(
                                COALESCE(raw_user_meta_data, '{}'::jsonb),
                                '{rule_limit}',
                                '-1'::jsonb
                            )
                            WHERE (raw_user_meta_data->>'rule_limit')::int = 3 
                               OR raw_user_meta_data->>'rule_limit' IS NULL
                        """))
                        print("✅ Updated auth.users metadata for rule_limit (3 -> unlimited)")
                except Exception as e:
                    print(f"ℹ️ Could not update auth.users metadata (may not be Supabase or metadata not used): {str(e)}")
                
                # Option 2: If you have a users table with dms_limit/monthly_limit columns
                try:
                    # Check if dms_limit column exists
                    result = conn.execute(text("""
                        SELECT column_name FROM information_schema.columns
                        WHERE table_name='users' AND column_name='dms_limit'
                    """))
                    if result.fetchone():
                        conn.execute(text("""
                            UPDATE users SET dms_limit = 1000 
                            WHERE dms_limit = 50 OR dms_limit IS NULL
                        """))
                        print("✅ Updated users.dms_limit (50 -> 1000)")
                    
                    # Check if monthly_limit column exists
                    result = conn.execute(text("""
                        SELECT column_name FROM information_schema.columns
                        WHERE table_name='users' AND column_name='monthly_limit'
                    """))
                    if result.fetchone():
                        conn.execute(text("""
                            UPDATE users SET monthly_limit = 1000 
                            WHERE monthly_limit = 50 OR monthly_limit IS NULL
                        """))
                        print("✅ Updated users.monthly_limit (50 -> 1000)")
                except Exception as e:
                    print(f"ℹ️ Could not update users table columns (may not exist): {str(e)}")
            
            conn.commit()
            print("✅ High Volume limits migration completed successfully")
            print("ℹ️ Note: Actual limits are controlled by code constants in app/core/plan_limits.py")
            return True

    except Exception as e:
        print(f"⚠️ High Volume limits migration warning (may not be needed): {str(e)}")
        # Don't raise - this migration is optional since limits are in code
        return False


if __name__ == "__main__":
    run_migration()
