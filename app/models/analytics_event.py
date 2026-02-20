"""
Model for storing granular analytics events for automation tracking.
"""
from sqlalchemy import Column, Integer, String, JSON, ForeignKey, DateTime, Enum as SQLEnum
from datetime import datetime
from enum import Enum
from app.db.base import Base


class EventType(str, Enum):
    """Types of analytics events that can be tracked."""
    TRIGGER_MATCHED = "trigger_matched"  # Keyword/comment matched, rule triggered
    DM_SENT = "dm_sent"  # Direct message sent to user
    LINK_CLICKED = "link_clicked"  # User clicked a tracked link (e.g., "Visit Profile")
    EMAIL_COLLECTED = "email_collected"  # User provided their email
    PHONE_COLLECTED = "phone_collected"  # User provided their phone number
    FOLLOW_BUTTON_CLICKED = "follow_button_clicked"  # User clicked "Follow Me" button
    IM_FOLLOWING_CLICKED = "im_following_clicked"  # User clicked "I'm following" button
    PROFILE_VISIT = "profile_visit"  # User visited profile via button
    COMMENT_REPLIED = "comment_replied"  # Public comment reply sent


class AnalyticsEvent(Base):
    __tablename__ = "analytics_events"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)  # Business owner
    rule_id = Column(Integer, ForeignKey("automation_rules.id", ondelete="CASCADE"), nullable=True, index=True)  # Automation rule
    instagram_account_id = Column(Integer, ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=True, index=True)
    media_id = Column(String, nullable=True, index=True)  # Instagram Post/Reel ID
    media_preview_url = Column(String, nullable=True)  # Cached media preview URL (thumbnail_url or media_url) - preserved even if media is deleted
    
    # Event details
    event_type = Column(SQLEnum(EventType), nullable=False, index=True)
    event_metadata = Column(JSON, nullable=True)  # Store additional data (url, email, sender_id, etc.)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    
    def __repr__(self):
        return f"<AnalyticsEvent(id={self.id}, type={self.event_type}, rule_id={self.rule_id}, created_at={self.created_at})>"
