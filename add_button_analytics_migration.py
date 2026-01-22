"""
Migration script to add button analytics tracking fields to automation_rule_stats table.
Adds tracking for profile visits and "I'm following" button clicks.
"""
import os
import sys
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# Get database URL from environment
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    print("❌ DATABASE_URL environment variable not set")
    sys.exit(1)

# Create engine
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)

def run_migration():
    """Add new columns for button analytics tracking."""
    db = SessionLocal()
    try:
        print("🔄 Adding button analytics tracking columns...")
        
        # Check if columns already exist and add them if they don't
        with engine.connect() as conn:
            # Check and add total_profile_visits
            result = conn.execute(text("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name='automation_rule_stats' 
                AND column_name='total_profile_visits'
            """))
            if not result.fetchone():
                conn.execute(text("ALTER TABLE automation_rule_stats ADD COLUMN total_profile_visits INTEGER DEFAULT 0"))
                print("✅ Added column 'total_profile_visits'")
            else:
                print("✅ Column 'total_profile_visits' already exists")
            
            # Check and add total_im_following_clicks
            result = conn.execute(text("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name='automation_rule_stats' 
                AND column_name='total_im_following_clicks'
            """))
            if not result.fetchone():
                conn.execute(text("ALTER TABLE automation_rule_stats ADD COLUMN total_im_following_clicks INTEGER DEFAULT 0"))
                print("✅ Added column 'total_im_following_clicks'")
            else:
                print("✅ Column 'total_im_following_clicks' already exists")
            
            # Check and add last_profile_visit_at
            result = conn.execute(text("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name='automation_rule_stats' 
                AND column_name='last_profile_visit_at'
            """))
            if not result.fetchone():
                conn.execute(text("ALTER TABLE automation_rule_stats ADD COLUMN last_profile_visit_at TIMESTAMP"))
                print("✅ Added column 'last_profile_visit_at'")
            else:
                print("✅ Column 'last_profile_visit_at' already exists")
            
            # Check and add last_im_following_clicked_at
            result = conn.execute(text("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name='automation_rule_stats' 
                AND column_name='last_im_following_clicked_at'
            """))
            if not result.fetchone():
                conn.execute(text("ALTER TABLE automation_rule_stats ADD COLUMN last_im_following_clicked_at TIMESTAMP"))
                print("✅ Added column 'last_im_following_clicked_at'")
            else:
                print("✅ Column 'last_im_following_clicked_at' already exists")
            
            conn.commit()
        
        print("✅ Migration completed successfully!")
        
    except Exception as e:
        print(f"❌ Migration failed: {str(e)}")
        import traceback
        traceback.print_exc()
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    run_migration()
