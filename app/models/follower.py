from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from datetime import datetime
from app.db.base import Base


class Follower(Base):
    __tablename__ = "followers"

    id = Column(Integer, primary_key=True, index=True)
    instagram_account_id = Column(Integer, ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=False)
    username = Column(String, nullable=False)
    user_id = Column(Integer, nullable=True)
    full_name = Column(String, nullable=True)
    fetched_at = Column(DateTime, default=datetime.utcnow)
