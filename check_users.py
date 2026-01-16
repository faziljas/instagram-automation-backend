import sys
import os

# Add the parent directory to the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import create_engine, text
from app.core.config import settings

# Create engine
engine = create_engine(str(settings.DATABASE_URL))

# Query users
with engine.connect() as conn:
    result = conn.execute(text("SELECT id, email, is_active, created_at FROM users WHERE email LIKE '%fazil%' ORDER BY id"))
    users = result.fetchall()
    
    if users:
        print("Users found:")
        for user in users:
            print(f"  ID: {user[0]}, Email: {user[1]}, Active: {user[2]}, Created: {user[3]}")
    else:
        print("No users found with 'fazil' in email")
        
    # Count total users
    result = conn.execute(text("SELECT COUNT(*) FROM users"))
    total = result.fetchone()[0]
    print(f"\nTotal users in database: {total}")
