"""
Model for storing captured leads from lead capture automation flows.
"""
from sqlalchemy import Column, Integer, String, JSON, Boolean, ForeignKey, DateTime
from datetime import datetime
from app.db.base import Base


class CapturedLead(Base):
    __tablename__ = "captured_leads"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    instagram_account_id = Column(Integer, ForeignKey("instagram_accounts.id"), nullable=False, index=True)
    automation_rule_id = Column(Integer, ForeignKey("automation_rules.id"), nullable=False, index=True)
    
    # Lead data fields
    email = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    name = Column(String, nullable=True)
    custom_fields = Column(JSON, nullable=True)  # For custom field data (e.g., {"company": "Acme Inc"})
    metadata = Column(JSON, nullable=True)  # Additional data (IP, user agent, conversation context, etc.)
    
    # Status flags
    captured_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    notified = Column(Boolean, default=False)  # Email notification sent?
    exported = Column(Boolean, default=False)  # Exported to CSV/webhook?
    
    def __repr__(self):
        return f"<CapturedLead(id={self.id}, email={self.email}, phone={self.phone}, rule_id={self.automation_rule_id})>"
