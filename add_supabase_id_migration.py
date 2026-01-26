"""
Migration: Add supabase_id column to users table.
This allows tracking which Supabase user is associated with each backend user,
preventing duplicate registrations when users try to register with email/password
after already signing up with Google OAuth (or vice versa).
"""
from sqlalchemy import create_engine, text
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is not set")


def run_migration():
    """Add supabase_id column to users table."""
    engine = create_engine(DATABASE_URL)

    try:
        with engine.connect() as conn:
            # Check if supabase_id column already exists
            r = conn.execute(text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='users' AND column_name='supabase_id'
            """))
            if not r.fetchone():
                # Add supabase_id column (nullable, unique, indexed)
                conn.execute(text("""
                    ALTER TABLE users ADD COLUMN supabase_id VARCHAR(255)
                """))
                conn.execute(text("""
                    CREATE UNIQUE INDEX IF NOT EXISTS ix_users_supabase_id ON users(supabase_id)
                """))
                print("✅ Added supabase_id column to users table")
            else:
                print("✅ supabase_id column already exists")

            conn.commit()
            print("✅ supabase_id migration completed successfully")
            return True

    except Exception as e:
        print(f"❌ supabase_id migration failed: {str(e)}")
        return False


if __name__ == "__main__":
    run_migration()
