"""
Migration: Add instagram_audience table for global conversion tracking
This table tracks whether users have provided email and are following across all automations.
"""
from sqlalchemy import text, inspect
from app.db.session import engine


def run_migration():
    """Create instagram_audience table if it doesn't exist"""
    try:
        with engine.begin() as conn:
            # Check if table already exists
            inspector = inspect(engine)
            existing_tables = inspector.get_table_names()
            
            if 'instagram_audience' in existing_tables:
                print("✅ instagram_audience table already exists")
                return
            
            # Create instagram_audience table
            print("🔄 Creating instagram_audience table...")
            
            # Check database type for compatibility
            db_type = str(engine.url).split("://")[0]
            
            if db_type == "postgresql":
                # PostgreSQL syntax
                conn.execute(text("""
                    CREATE TABLE instagram_audience (
                        id SERIAL PRIMARY KEY,
                        sender_id VARCHAR(255) NOT NULL,
                        instagram_account_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        email VARCHAR(255),
                        is_following BOOLEAN NOT NULL DEFAULT FALSE,
                        first_interaction_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        last_interaction_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        email_captured_at TIMESTAMP,
                        follow_confirmed_at TIMESTAMP,
                        username VARCHAR(255),
                        extra_metadata JSONB
                    )
                """))
                
                # Create indexes
                conn.execute(text("""
                    CREATE UNIQUE INDEX IF NOT EXISTS ix_instagram_audience_sender_id ON instagram_audience(sender_id)
                """))
                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS ix_instagram_audience_instagram_account_id ON instagram_audience(instagram_account_id)
                """))
                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS ix_instagram_audience_user_id ON instagram_audience(user_id)
                """))
                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS ix_instagram_audience_email ON instagram_audience(email)
                """))
                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS ix_instagram_audience_is_following ON instagram_audience(is_following)
                """))
                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS ix_instagram_audience_last_interaction_at ON instagram_audience(last_interaction_at)
                """))
                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS idx_sender_account ON instagram_audience(sender_id, instagram_account_id)
                """))
                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS idx_converted ON instagram_audience(email, is_following)
                """))
                
                # Add foreign key constraints
                conn.execute(text("""
                    ALTER TABLE instagram_audience 
                    ADD CONSTRAINT fk_instagram_audience_instagram_account_id 
                    FOREIGN KEY (instagram_account_id) REFERENCES instagram_accounts(id)
                """))
                conn.execute(text("""
                    ALTER TABLE instagram_audience 
                    ADD CONSTRAINT fk_instagram_audience_user_id 
                    FOREIGN KEY (user_id) REFERENCES users(id)
                """))
                
            else:
                # SQLite syntax
                conn.execute(text("""
                    CREATE TABLE instagram_audience (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        sender_id VARCHAR(255) NOT NULL UNIQUE,
                        instagram_account_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        email VARCHAR(255),
                        is_following BOOLEAN NOT NULL DEFAULT 0,
                        first_interaction_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        last_interaction_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        email_captured_at TIMESTAMP,
                        follow_confirmed_at TIMESTAMP,
                        username VARCHAR(255),
                        extra_metadata TEXT
                    )
                """))
                
                # Create indexes (SQLite)
                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS ix_instagram_audience_instagram_account_id ON instagram_audience(instagram_account_id)
                """))
                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS ix_instagram_audience_user_id ON instagram_audience(user_id)
                """))
                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS ix_instagram_audience_email ON instagram_audience(email)
                """))
                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS ix_instagram_audience_is_following ON instagram_audience(is_following)
                """))
                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS ix_instagram_audience_last_interaction_at ON instagram_audience(last_interaction_at)
                """))
                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS idx_sender_account ON instagram_audience(sender_id, instagram_account_id)
                """))
                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS idx_converted ON instagram_audience(email, is_following)
                """))
            
            print("✅ instagram_audience table created successfully!")
            
    except Exception as e:
        print(f"⚠️ Migration warning (table may already exist): {str(e)}")
        # Don't raise - migration is idempotent


if __name__ == "__main__":
    run_migration()
