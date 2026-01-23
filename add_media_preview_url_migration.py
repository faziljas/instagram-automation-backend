"""
Migration: Add media_preview_url column to analytics_events table
This caches media preview URLs to preserve previews even if media is deleted from Instagram.
"""
from sqlalchemy import text
from app.db.session import SessionLocal


def run_migration():
    """Add media_preview_url column to analytics_events table if it doesn't exist."""
    db = SessionLocal()
    try:
        # Check if column already exists
        result = db.execute(text("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name='analytics_events' 
            AND column_name='media_preview_url'
        """))
        
        if result.fetchone():
            print("✅ Column media_preview_url already exists, skipping migration")
            return
        
        # Add the column
        db.execute(text("""
            ALTER TABLE analytics_events 
            ADD COLUMN media_preview_url VARCHAR(500)
        """))
        
        db.commit()
        print("✅ Migration completed successfully: media_preview_url column added to analytics_events")
        
    except Exception as e:
        db.rollback()
        if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
            print(f"⚠️ Column may already exist: {str(e)}")
        else:
            print(f"❌ Migration failed: {str(e)}")
            raise
    finally:
        db.close()


if __name__ == "__main__":
    print("🔄 Running media_preview_url migration...")
    run_migration()
    print("✅ Migration completed!")
