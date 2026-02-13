from sqlalchemy import Column, Integer, String, JSON, Boolean, ForeignKey, DateTime
from datetime import datetime
from app.db.base import Base


class AutomationRule(Base):
    __tablename__ = "automation_rules"

    id = Column(Integer, primary_key=True, index=True)
    instagram_account_id = Column(Integer, ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=True)
    name = Column(String, nullable=True)
    trigger_type = Column(String, nullable=False)
    action_type = Column(String, nullable=False)
    config = Column(JSON, nullable=False)
    media_id = Column(String, nullable=True)  # Instagram media ID (post/reel/story) this rule is tied to
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    deleted_at = Column(DateTime, nullable=True)  # Set when user deletes rule; exclude from list
