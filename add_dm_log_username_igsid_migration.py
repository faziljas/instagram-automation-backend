"""
Migration: Add instagram_username and instagram_igsid to dm_logs; make instagram_account_id nullable.
This allows preserving usage history when an Instagram account is deleted:
we nullify instagram_account_id but keep username/igsid for usage tracking.
"""
from sqlalchemy import create_engine, text
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is not set")


def run_migration():
    """Add columns, backfill, and make instagram_account_id nullable."""
    engine = create_engine(DATABASE_URL)

    try:
        with engine.connect() as conn:
            # 1. Add instagram_username if not exists
            r = conn.execute(text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='dm_logs' AND column_name='instagram_username'
            """))
            if not r.fetchone():
                conn.execute(text("""
                    ALTER TABLE dm_logs ADD COLUMN instagram_username VARCHAR(255)
                """))
                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS ix_dm_logs_instagram_username ON dm_logs(instagram_username)
                """))
                print("✅ Added instagram_username to dm_logs")
            else:
                print("✅ instagram_username already exists")

            # 2. Add instagram_igsid if not exists
            r = conn.execute(text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='dm_logs' AND column_name='instagram_igsid'
            """))
            if not r.fetchone():
                conn.execute(text("""
                    ALTER TABLE dm_logs ADD COLUMN instagram_igsid VARCHAR(255)
                """))
                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS ix_dm_logs_instagram_igsid ON dm_logs(instagram_igsid)
                """))
                print("✅ Added instagram_igsid to dm_logs")
            else:
                print("✅ instagram_igsid already exists")

            # 3. Backfill from instagram_accounts (only rows not yet backfilled)
            conn.execute(text("""
                UPDATE dm_logs d SET
                    instagram_username = a.username,
                    instagram_igsid = a.igsid
                FROM instagram_accounts a
                WHERE d.instagram_account_id = a.id
                  AND d.instagram_username IS NULL
            """))
            print("✅ Backfilled instagram_username/igsid from instagram_accounts")

            # 4. Make instagram_account_id nullable
            r = conn.execute(text("""
                SELECT is_nullable FROM information_schema.columns
                WHERE table_name='dm_logs' AND column_name='instagram_account_id'
            """))
            row = r.fetchone()
            if row and row[0] == "NO":
                conn.execute(text("""
                    ALTER TABLE dm_logs ALTER COLUMN instagram_account_id DROP NOT NULL
                """))
                print("✅ instagram_account_id is now nullable")
            else:
                print("✅ instagram_account_id already nullable")

            conn.commit()
            print("✅ dm_logs migration completed successfully")
            return True

    except Exception as e:
        print(f"❌ dm_logs migration failed: {str(e)}")
        return False


if __name__ == "__main__":
    run_migration()
