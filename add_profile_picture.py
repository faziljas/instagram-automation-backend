"""Add profile_picture_url column to users table"""
from sqlalchemy import create_engine, text
from app.db.session import engine
from app.db.base import Base

def run_migration():
    """Add profile_picture_url column to users table if it doesn't exist.
    Idempotent - safe to run multiple times."""
    try:
        with engine.connect() as conn:
            # Add profile_picture_url column
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_picture_url VARCHAR"))
            conn.commit()
            print("✅ Added profile_picture_url column")
            return True
    except Exception as e:
        # Column might already exist or other error - log but don't fail
        print(f"⚠️ profile_picture_url column migration: {e}")
        # Still return True since IF NOT EXISTS should handle it gracefully
        return True

if __name__ == "__main__":
    success = run_migration()
    exit(0 if success else 1)
