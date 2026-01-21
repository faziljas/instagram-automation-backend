"""
Migration: Add follow_button_clicks tracking to automation_rule_stats table
This adds columns to track "Follow Me" button clicks for analytics.
"""
from sqlalchemy import text
from app.db.session import engine

def run_migration():
    """Add follow_button_clicks columns to automation_rule_stats table"""
    try:
        with engine.connect() as conn:
            # Check if column already exists
            result = conn.execute(text("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name='automation_rule_stats' 
                AND column_name='total_follow_button_clicks'
            """))
            
            if result.fetchone():
                print("✅ Column 'total_follow_button_clicks' already exists")
            else:
                # Add new columns
                conn.execute(text("""
                    ALTER TABLE automation_rule_stats 
                    ADD COLUMN total_follow_button_clicks INTEGER DEFAULT 0
                """))
                print("✅ Added column 'total_follow_button_clicks'")
            
            # Check if timestamp column exists
            result = conn.execute(text("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name='automation_rule_stats' 
                AND column_name='last_follow_button_clicked_at'
            """))
            
            if result.fetchone():
                print("✅ Column 'last_follow_button_clicked_at' already exists")
            else:
                conn.execute(text("""
                    ALTER TABLE automation_rule_stats 
                    ADD COLUMN last_follow_button_clicked_at TIMESTAMP
                """))
                print("✅ Added column 'last_follow_button_clicked_at'")
            
            conn.commit()
            print("✅ Migration completed successfully!")
            
    except Exception as e:
        print(f"❌ Migration failed: {str(e)}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    run_migration()
