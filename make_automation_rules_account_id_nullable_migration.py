"""
Migration: Make instagram_account_id nullable in automation_rules table.
This allows rules to persist across account disconnect/reconnect (per user per Instagram tracking).
"""
from sqlalchemy import create_engine, text
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is not set")


def run_migration():
    """Make instagram_account_id nullable in automation_rules table."""
    engine = create_engine(DATABASE_URL)

    try:
        with engine.connect() as conn:
            # Check if column is already nullable
            r = conn.execute(text("""
                SELECT is_nullable 
                FROM information_schema.columns 
                WHERE table_name = 'automation_rules' 
                AND column_name = 'instagram_account_id'
            """))
            result = r.fetchone()
            
            if result:
                is_nullable = result[0]
                if is_nullable == 'YES':
                    print("✅ instagram_account_id is already nullable")
                else:
                    # Make the column nullable
                    print("🔧 Making instagram_account_id nullable...")
                    conn.execute(text("""
                        ALTER TABLE automation_rules 
                        ALTER COLUMN instagram_account_id DROP NOT NULL
                    """))
                    conn.commit()
                    print("✅ Made instagram_account_id nullable in automation_rules table")
            else:
                print("⚠️ Column instagram_account_id not found in automation_rules table")

            print("✅ Automation rules account_id nullable migration completed successfully")
            return True

    except Exception as e:
        print(f"❌ Migration failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    run_migration()
