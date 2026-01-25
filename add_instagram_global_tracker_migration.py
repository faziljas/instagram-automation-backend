"""
Migration: Create instagram_global_trackers table for persistent usage tracking per Instagram account (IGSID).
This table tracks DM and rule creation usage independently of user accounts to prevent free tier abuse.
"""
from sqlalchemy import create_engine, text
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is not set")


def run_migration():
    """Create instagram_global_trackers table if it doesn't exist."""
    engine = create_engine(DATABASE_URL)

    try:
        with engine.connect() as conn:
            # Check if table already exists
            r = conn.execute(text("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_name = 'instagram_global_trackers'
                )
            """))
            table_exists = r.fetchone()[0]

            if not table_exists:
                # Create the table
                conn.execute(text("""
                    CREATE TABLE instagram_global_trackers (
                        instagram_id VARCHAR(255) PRIMARY KEY,
                        dms_sent_count INTEGER NOT NULL DEFAULT 0,
                        rules_created_count INTEGER NOT NULL DEFAULT 0,
                        last_reset_date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                """))
                
                # Create index on instagram_id (already primary key, but explicit index for clarity)
                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS ix_instagram_global_trackers_instagram_id 
                    ON instagram_global_trackers(instagram_id)
                """))
                
                print("✅ Created instagram_global_trackers table")
            else:
                print("✅ instagram_global_trackers table already exists")

            conn.commit()
            print("✅ Instagram global tracker migration completed successfully")
            return True

    except Exception as e:
        print(f"❌ Instagram global tracker migration failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    run_migration()
