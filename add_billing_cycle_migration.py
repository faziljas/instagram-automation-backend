"""
Migration: Add billing_cycle_start_date column to subscriptions table
This allows Pro/Enterprise users to have 30-day billing cycles from upgrade date
instead of calendar month cycles.
"""
from sqlalchemy import create_engine, text
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is not set")

def run_migration():
    """Add billing_cycle_start_date column to subscriptions table"""
    engine = create_engine(DATABASE_URL)
    
    try:
        with engine.connect() as conn:
            # Check if column already exists
            result = conn.execute(text("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name='subscriptions' AND column_name='billing_cycle_start_date'
            """))
            
            if result.fetchone():
                print("✅ Column 'billing_cycle_start_date' already exists in 'subscriptions' table")
                return True
            
            # Add the column
            conn.execute(text("""
                ALTER TABLE subscriptions 
                ADD COLUMN billing_cycle_start_date TIMESTAMP NULL
            """))
            conn.commit()
            
            print("✅ Successfully added 'billing_cycle_start_date' column to 'subscriptions' table")
            return True
            
    except Exception as e:
        print(f"❌ Error adding 'billing_cycle_start_date' column: {str(e)}")
        return False

if __name__ == "__main__":
    run_migration()
