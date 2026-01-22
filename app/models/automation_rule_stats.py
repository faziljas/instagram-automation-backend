"""
Model for storing automation rule statistics.
This is optional - can also use config.stats for MVP.
"""
from sqlalchemy import Column, Integer, DateTime, ForeignKey
from datetime import datetime
from app.db.base import Base


class AutomationRuleStats(Base):
    __tablename__ = "automation_rule_stats"
    
    id = Column(Integer, primary_key=True, index=True)
    automation_rule_id = Column(Integer, ForeignKey("automation_rules.id"), unique=True, nullable=False, index=True)
    
    # Counters
    total_triggers = Column(Integer, default=0)
    total_dms_sent = Column(Integer, default=0)
    total_comments_replied = Column(Integer, default=0)
    total_leads_captured = Column(Integer, default=0)
    total_follow_button_clicks = Column(Integer, default=0)  # Track "Follow Me" button clicks
    total_profile_visits = Column(Integer, default=0)  # Track "Visit Profile" button clicks
    total_im_following_clicks = Column(Integer, default=0)  # Track "I'm following" button clicks
    
    # Timestamps
    last_triggered_at = Column(DateTime, nullable=True)
    last_lead_captured_at = Column(DateTime, nullable=True)
    last_follow_button_clicked_at = Column(DateTime, nullable=True)
    last_profile_visit_at = Column(DateTime, nullable=True)
    last_im_following_clicked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    
    def __repr__(self):
        return f"<AutomationRuleStats(rule_id={self.automation_rule_id}, triggers={self.total_triggers}, dms={self.total_dms_sent})>"
