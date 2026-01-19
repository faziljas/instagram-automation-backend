"""
Migration: Add captured_leads table for lead capture functionality
This is an additive migration - no existing tables are modified.
"""
from sqlalchemy import create_engine, Column, Integer, String, JSON, Boolean, DateTime, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime
from app.db.session import engine
from app.db.base import Base

# Define the new table
class CapturedLead(Base):
    __tablename__ = "captured_leads"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    instagram_account_id = Column(Integer, ForeignKey("instagram_accounts.id"), nullable=False, index=True)
    automation_rule_id = Column(Integer, ForeignKey("automation_rules.id"), nullable=False, index=True)
    email = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    name = Column(String, nullable=True)
    custom_fields = Column(JSON, nullable=True)  # For custom field data
    metadata = Column(JSON, nullable=True)  # Additional data (IP, user agent, etc.)
    captured_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    notified = Column(Boolean, default=False)
    exported = Column(Boolean, default=False)

def run_migration():
    """Create the captured_leads table"""
    try:
        Base.metadata.create_all(engine)
        print("✅ Successfully created 'captured_leads' table")
        return True
    except Exception as e:
        print(f"❌ Error creating 'captured_leads' table: {str(e)}")
        return False

if __name__ == "__main__":
    run_migration()
