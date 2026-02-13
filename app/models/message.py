"""
Model for storing all Instagram DM messages (both sent and received).
"""
from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Boolean, JSON
from datetime import datetime
from app.db.base import Base


class Message(Base):
    __tablename__ = "messages"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    instagram_account_id = Column(Integer, ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    
    # Link to Conversation
    conversation_id = Column(Integer, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=True, index=True)
    
    # Conversation participants
    sender_id = Column(String, nullable=False, index=True)  # Instagram user ID (IGSID)
    sender_username = Column(String, nullable=True)  # Instagram username (if available)
    recipient_id = Column(String, nullable=False, index=True)  # Instagram user ID (our account or other user)
    recipient_username = Column(String, nullable=True)  # Instagram username (if available)
    
    # Message content
    message_text = Column(String, nullable=True)  # Text content (alias for content)
    content = Column(String, nullable=True)  # Text content (alias for message_text)
    message_id = Column(String, nullable=True, index=True)  # Instagram message ID (mid)
    platform_message_id = Column(String, nullable=True, index=True)  # Instagram Message ID (usually 'm_...')
    is_from_bot = Column(Boolean, default=False)  # True if sent by our bot, False if received
    has_attachments = Column(Boolean, default=False)  # True if message has media attachments
    attachments = Column(JSON, nullable=True)  # Store attachment info (type, url, etc.)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    
    def __repr__(self):
        direction = "→" if self.is_from_bot else "←"
        content = self.message_text or self.content or 'None'
        return f"<Message(id={self.id}, conv_id={self.conversation_id}, {direction} {self.sender_username or self.sender_id}, text={content[:30] if content else 'None'}...)>"
    
    def get_content(self):
        """Get message content (from either message_text or content field)."""
        return self.message_text or self.content
