"""
Model for tracking persistent usage per (User + Instagram Account) combination.
Tracks usage per user per Instagram account to ensure limits persist correctly.
"""
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from datetime import datetime
from app.db.base import Base


class InstagramGlobalTracker(Base):
    __tablename__ = "instagram_global_trackers"
    
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, nullable=False, index=True)  # User ID
    instagram_id = Column(String, primary_key=True, nullable=False, index=True)  # IGSID (Instagram Business Account ID)
    dms_sent_count = Column(Integer, default=0, nullable=False)
    rules_created_count = Column(Integer, default=0, nullable=False)
    last_reset_date = Column(DateTime, default=datetime.utcnow, nullable=False)  # Used for Pro monthly cycles
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    
    def __repr__(self):
        return f"<InstagramGlobalTracker(user_id={self.user_id}, instagram_id={self.instagram_id}, dms={self.dms_sent_count}, rules={self.rules_created_count})>"
