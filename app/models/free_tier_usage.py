"""
Model for tracking emails that have already used the free tier (e.g. after account deletion).
Used to prevent re-granting free benefits (1000 DMs, 1 IG account) on re-signup.
"""
from sqlalchemy import Column, String, DateTime
from sqlalchemy.sql import func
from app.db.base import Base


class FreeTierUsage(Base):
    __tablename__ = "free_tier_usage"

    email_normalized = Column(String(255), primary_key=True, nullable=False)
    used_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
