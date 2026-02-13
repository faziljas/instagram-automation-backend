"""
Model for tracking global Instagram user state across all automations.
Tracks whether a user has provided email and is following to enable "VIP" treatment.
"""
from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, DateTime, Index, JSON
from datetime import datetime
from app.db.base import Base


class InstagramAudience(Base):
    __tablename__ = "instagram_audience"
    
    id = Column(Integer, primary_key=True, index=True)
    
    # User identification
    sender_id = Column(String, nullable=False, index=True, unique=True)  # Instagram user ID
    instagram_account_id = Column(Integer, ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)  # Our app's user ID
    
    # Global conversion state
    email = Column(String, nullable=True, index=True)  # Email if provided
    is_following = Column(Boolean, default=False, nullable=False, index=True)  # Following status
    
    # Metadata
    first_interaction_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_interaction_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    email_captured_at = Column(DateTime, nullable=True)  # When email was first captured
    follow_confirmed_at = Column(DateTime, nullable=True)  # When follow was confirmed
    
    # Additional metadata
    username = Column(String, nullable=True)  # Instagram username if available
    extra_metadata = Column(JSON, nullable=True)  # Additional data
    
    # Index for fast lookups
    __table_args__ = (
        Index('idx_sender_account', 'sender_id', 'instagram_account_id'),
        Index('idx_converted', 'email', 'is_following'),
    )
    
    def __repr__(self):
        return f"<InstagramAudience(sender_id={self.sender_id}, email={self.email}, is_following={self.is_following})>"
    
    @property
    def is_converted(self):
        """Check if user is fully converted (has email AND is following)"""
        return bool(self.email) and self.is_following
