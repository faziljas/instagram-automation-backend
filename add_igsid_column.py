"""
Migration script to add igsid column to instagram_accounts table.
Run this once to update your database schema.
"""
import os
import sys
from sqlalchemy import create_engine, text
from app.db.base import Base
from app.core.config import settings

# Database URL from environment or settings
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/instagram_automation")

def add_igsid_column():
    """Add igsid column to instagram_accounts table if it doesn't exist."""
    engine = create_engine(DATABASE_URL)
    
    with engine.connect() as conn:
        # Check if column already exists
        check_query = text("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name='instagram_accounts' AND column_name='igsid'
        """)
        result = conn.execute(check_query)
        exists = result.fetchone() is not None
        
        if exists:
            print("✅ Column 'igsid' already exists in instagram_accounts table")
            return
        
        # Add the column
        alter_query = text("""
            ALTER TABLE instagram_accounts 
            ADD COLUMN igsid VARCHAR(255),
            CREATE INDEX IF NOT EXISTS ix_instagram_accounts_igsid ON instagram_accounts(igsid)
        """)
        
        try:
            conn.execute(alter_query)
            conn.commit()
            print("✅ Successfully added 'igsid' column to instagram_accounts table")
        except Exception as e:
            print(f"❌ Error adding column: {str(e)}")
            conn.rollback()
            raise

if __name__ == "__main__":
    print("🔄 Adding igsid column to instagram_accounts table...")
    add_igsid_column()
    print("✅ Migration complete!")
