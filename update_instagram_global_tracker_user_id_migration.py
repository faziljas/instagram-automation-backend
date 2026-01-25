"""
Migration: Update instagram_global_trackers table to use composite primary key (user_id + instagram_id).
This changes tracking from per Instagram account to per (User + Instagram Account) combination.
"""
from sqlalchemy import create_engine, text, inspect
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is not set")


def run_migration():
    """Update instagram_global_trackers table to include user_id and change primary key."""
    engine = create_engine(DATABASE_URL)

    try:
        with engine.connect() as conn:
            # Check if table exists
            inspector = inspect(engine)
            table_exists = "instagram_global_trackers" in inspector.get_table_names()
            
            if not table_exists:
                # Table doesn't exist - create it with new schema
                conn.execute(text("""
                    CREATE TABLE instagram_global_trackers (
                        user_id INTEGER NOT NULL,
                        instagram_id VARCHAR(255) NOT NULL,
                        dms_sent_count INTEGER NOT NULL DEFAULT 0,
                        rules_created_count INTEGER NOT NULL DEFAULT 0,
                        last_reset_date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (user_id, instagram_id),
                        FOREIGN KEY (user_id) REFERENCES users(id)
                    )
                """))
                
                # Create indexes
                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS ix_instagram_global_trackers_user_id 
                    ON instagram_global_trackers(user_id)
                """))
                
                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS ix_instagram_global_trackers_instagram_id 
                    ON instagram_global_trackers(instagram_id)
                """))
                
                print("✅ Created instagram_global_trackers table with user_id")
                conn.commit()
                return True
            
            # Table exists - check if user_id column exists
            columns = [col['name'] for col in inspector.get_columns("instagram_global_trackers")]
            has_user_id = "user_id" in columns
            
            if has_user_id:
                print("✅ instagram_global_trackers table already has user_id column")
                return True
            
            # Table exists but doesn't have user_id - need to migrate
            print("🔄 Migrating instagram_global_trackers table to include user_id...")
            
            # Step 1: Create new table with correct schema
            conn.execute(text("""
                CREATE TABLE instagram_global_trackers_new (
                    user_id INTEGER NOT NULL,
                    instagram_id VARCHAR(255) NOT NULL,
                    dms_sent_count INTEGER NOT NULL DEFAULT 0,
                    rules_created_count INTEGER NOT NULL DEFAULT 0,
                    last_reset_date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, instagram_id),
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
            """))
            
            # Step 2: Get all Instagram accounts to map IGSID to user_id
            # We'll create trackers for all currently connected accounts
            result = conn.execute(text("""
                SELECT DISTINCT user_id, igsid 
                FROM instagram_accounts 
                WHERE igsid IS NOT NULL
            """))
            
            migrated_count = 0
            for row in result:
                user_id, igsid = row[0], row[1]
                if igsid:
                    # Get existing tracker data if it exists
                    old_tracker = conn.execute(text("""
                        SELECT dms_sent_count, rules_created_count, last_reset_date, created_at
                        FROM instagram_global_trackers
                        WHERE instagram_id = :igsid
                        LIMIT 1
                    """), {"igsid": igsid}).fetchone()
                    
                    if old_tracker:
                        # Migrate existing data
                        conn.execute(text("""
                            INSERT INTO instagram_global_trackers_new 
                            (user_id, instagram_id, dms_sent_count, rules_created_count, last_reset_date, created_at)
                            VALUES (:user_id, :igsid, :dms, :rules, :reset_date, :created_at)
                        """), {
                            "user_id": user_id,
                            "igsid": igsid,
                            "dms": old_tracker[0],
                            "rules": old_tracker[1],
                            "reset_date": old_tracker[2],
                            "created_at": old_tracker[3]
                        })
                        migrated_count += 1
                    else:
                        # Create new tracker entry
                        conn.execute(text("""
                            INSERT INTO instagram_global_trackers_new 
                            (user_id, instagram_id, dms_sent_count, rules_created_count, last_reset_date, created_at)
                            VALUES (:user_id, :igsid, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        """), {
                            "user_id": user_id,
                            "igsid": igsid
                        })
            
            # Step 3: Drop old table and rename new table
            conn.execute(text("DROP TABLE instagram_global_trackers"))
            conn.execute(text("ALTER TABLE instagram_global_trackers_new RENAME TO instagram_global_trackers"))
            
            # Step 4: Create indexes
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_instagram_global_trackers_user_id 
                ON instagram_global_trackers(user_id)
            """))
            
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_instagram_global_trackers_instagram_id 
                ON instagram_global_trackers(instagram_id)
            """))
            
            conn.commit()
            print(f"✅ Migrated instagram_global_trackers table: {migrated_count} entries migrated")
            print("✅ Instagram global tracker user_id migration completed successfully")
            return True

    except Exception as e:
        print(f"❌ Instagram global tracker user_id migration failed: {str(e)}")
        import traceback
        traceback.print_exc()
        conn.rollback()
        return False


if __name__ == "__main__":
    run_migration()
