from sqlalchemy import Column, Integer, String, ForeignKey, DateTime
from datetime import datetime
from app.db.base import Base


class DmLog(Base):
    __tablename__ = "dm_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    instagram_account_id = Column(Integer, ForeignKey("instagram_accounts.id"), nullable=True, index=True)
    instagram_username = Column(String, nullable=True, index=True)
    instagram_igsid = Column(String, nullable=True, index=True)
    recipient_username = Column(String, nullable=False)
    message = Column(String, nullable=False)
    sent_at = Column(DateTime, default=datetime.utcnow, index=True)
