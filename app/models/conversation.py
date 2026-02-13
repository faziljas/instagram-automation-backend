"""
Model for storing Instagram DM conversations.
"""
from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Text
from datetime import datetime
from app.db.base import Base


class Conversation(Base):
    __tablename__ = "conversations"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    instagram_account_id = Column(Integer, ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    
    # Instagram conversation identifier
    platform_conversation_id = Column(String, nullable=True, index=True)  # Instagram Thread ID (usually starts with 't_')
    
    # Participant information
    participant_id = Column(String, nullable=False, index=True)  # IGSID of the customer/other participant
    participant_name = Column(String, nullable=True)  # Username, can be null initially
    
    # Conversation metadata
    last_message = Column(Text, nullable=True)  # Preview text of last message
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    
    def __repr__(self):
        return f"<Conversation(id={self.id}, participant={self.participant_name or self.participant_id}, updated_at={self.updated_at})>"
