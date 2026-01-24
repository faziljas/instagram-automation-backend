"""
Migration: Add created_at column to instagram_accounts table
This tracks when each Instagram account was connected.
"""
from sqlalchemy import create_engine, text
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is not set")


def run_migration():
    """Add created_at column to instagram_accounts table"""
    engine = create_engine(DATABASE_URL)

    try:
        with engine.connect() as conn:
            # Check if column already exists
            r = conn.execute(text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='instagram_accounts' AND column_name='created_at'
            """))
            if r.fetchone():
                print("✅ Column 'created_at' already exists in 'instagram_accounts' table")
                return True

            # Add the column with default value for existing rows
            # For existing rows, set to current timestamp (approximation)
            # For new rows, will use default datetime.utcnow()
            conn.execute(text("""
                ALTER TABLE instagram_accounts 
                ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
            """))
            conn.commit()

            print("✅ Successfully added 'created_at' column to 'instagram_accounts' table")
            return True

    except Exception as e:
        print(f"❌ Error adding 'created_at' column: {str(e)}")
        return False


if __name__ == "__main__":
    run_migration()
