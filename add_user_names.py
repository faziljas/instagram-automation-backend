"""Add first_name and last_name to users table"""
from sqlalchemy import create_engine, text
from app.core.config import settings

engine = create_engine(str(settings.DATABASE_URL))

with engine.connect() as conn:
    # Add first_name column
    try:
        conn.execute(text("ALTER TABLE users ADD COLUMN first_name VARCHAR"))
        print("✅ Added first_name column")
    except Exception as e:
        print(f"first_name column might already exist: {e}")
    
    # Add last_name column
    try:
        conn.execute(text("ALTER TABLE users ADD COLUMN last_name VARCHAR"))
        print("✅ Added last_name column")
    except Exception as e:
        print(f"last_name column might already exist: {e}")
    
    conn.commit()
    print("✅ Migration complete!")

