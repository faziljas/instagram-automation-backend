"""
Model for storing all Instagram DM messages (both sent and received).
"""
from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Boolean, JSON
from datetime import datetime
from app.db.base import Base


class Message(Base):
    __tablename__ = "messages"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    instagram_account_id = Column(Integer, ForeignKey("instagram_accounts.id"), nullable=False, index=True)
    
    # Conversation participants
    sender_id = Column(String, nullable=False, index=True)  # Instagram user ID
    sender_username = Column(String, nullable=True)  # Instagram username (if available)
    recipient_id = Column(String, nullable=False, index=True)  # Instagram user ID (our account or other user)
    recipient_username = Column(String, nullable=True)  # Instagram username (if available)
    
    # Message content
    message_text = Column(String, nullable=True)  # Text content
    message_id = Column(String, nullable=True, index=True)  # Instagram message ID (mid)
    is_from_bot = Column(Boolean, default=False)  # True if sent by our bot, False if received
    has_attachments = Column(Boolean, default=False)  # True if message has media attachments
    attachments = Column(JSON, nullable=True)  # Store attachment info (type, url, etc.)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    
    def __repr__(self):
        direction = "→" if self.is_from_bot else "←"
        return f"<Message(id={self.id}, {direction} {self.sender_username or self.sender_id}, text={self.message_text[:30] if self.message_text else 'None'}...)>"
