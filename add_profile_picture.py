"""Add profile_picture_url column to users table"""
from sqlalchemy import create_engine, text
from app.db.session import engine
from app.db.base import Base

def add_profile_picture_column():
    """Add profile_picture_url column to users table if it doesn't exist"""
    with engine.connect() as conn:
        try:
            # Add profile_picture_url column
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_picture_url VARCHAR"))
            conn.commit()
            print("✅ Added profile_picture_url column")
        except Exception as e:
            print(f"profile_picture_url column might already exist: {e}")
            conn.rollback()

if __name__ == "__main__":
    add_profile_picture_column()
