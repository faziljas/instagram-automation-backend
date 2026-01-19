"""
Migration: Add automation_rule_stats table for better analytics
This is an additive migration - no existing tables are modified.
OPTIONAL: Can use config.stats instead for MVP.
"""
from sqlalchemy import create_engine, Column, Integer, DateTime, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime
from app.db.session import engine
from app.db.base import Base

# Define the new table
class AutomationRuleStats(Base):
    __tablename__ = "automation_rule_stats"
    
    id = Column(Integer, primary_key=True, index=True)
    automation_rule_id = Column(Integer, ForeignKey("automation_rules.id"), unique=True, nullable=False, index=True)
    total_triggers = Column(Integer, default=0)
    total_dms_sent = Column(Integer, default=0)
    total_comments_replied = Column(Integer, default=0)
    total_leads_captured = Column(Integer, default=0)
    last_triggered_at = Column(DateTime, nullable=True)
    last_lead_captured_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

def run_migration():
    """Create the automation_rule_stats table"""
    try:
        Base.metadata.create_all(engine)
        print("✅ Successfully created 'automation_rule_stats' table")
        return True
    except Exception as e:
        print(f"❌ Error creating 'automation_rule_stats' table: {str(e)}")
        return False

if __name__ == "__main__":
    run_migration()
