"""
Migration: Add free_tier_usage table and users.free_tier_used column.

Prevents free-tier abuse: when a user deletes their account, we record their email
in free_tier_usage. On re-signup with the same email, the new user gets free_tier_used=True
and receives 0 free DMs and 0 free Instagram account connections.

Idempotent - safe to run multiple times (see MIGRATION_GUIDELINES.md).
"""
from sqlalchemy import create_engine, text
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is not set")


def run_migration():
    """Create free_tier_usage table and add users.free_tier_used column."""
    engine = create_engine(DATABASE_URL)

    try:
        with engine.connect() as conn:
            # 1. Create free_tier_usage table if not exists
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS free_tier_usage (
                    email_normalized VARCHAR(255) PRIMARY KEY,
                    used_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
                )
            """))
            conn.commit()

            # 2. Add users.free_tier_used column if not exists (PostgreSQL 9.5+)
            r = conn.execute(text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'users' AND column_name = 'free_tier_used'
            """))
            if not r.fetchone():
                conn.execute(text("""
                    ALTER TABLE users ADD COLUMN free_tier_used BOOLEAN NOT NULL DEFAULT false
                """))
                conn.commit()
                print("✅ Added users.free_tier_used column")
            else:
                print("✅ users.free_tier_used column already exists")

            print("✅ free_tier_usage migration completed successfully")
            return True

    except Exception as e:
        print(f"❌ free_tier_usage migration failed: {str(e)}")
        return False


if __name__ == "__main__":
    run_migration()
