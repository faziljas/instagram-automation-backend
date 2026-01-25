"""
Model for tracking persistent global usage per Instagram account (IGSID).
Tracks usage independently of user accounts to prevent free tier abuse.
"""
from sqlalchemy import Column, Integer, String, DateTime
from datetime import datetime
from app.db.base import Base


class InstagramGlobalTracker(Base):
    __tablename__ = "instagram_global_trackers"
    
    instagram_id = Column(String, primary_key=True, index=True)  # IGSID (Instagram Business Account ID)
    dms_sent_count = Column(Integer, default=0, nullable=False)
    rules_created_count = Column(Integer, default=0, nullable=False)
    last_reset_date = Column(DateTime, default=datetime.utcnow, nullable=False)  # Used for Pro monthly cycles
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    
    def __repr__(self):
        return f"<InstagramGlobalTracker(instagram_id={self.instagram_id}, dms={self.dms_sent_count}, rules={self.rules_created_count})>"
