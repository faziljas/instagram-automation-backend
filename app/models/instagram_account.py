from sqlalchemy import Column, Integer, String, Boolean, ForeignKey
from app.db.base import Base


class InstagramAccount(Base):
    __tablename__ = "instagram_accounts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    username = Column(String, nullable=False)
    encrypted_credentials = Column(String, nullable=False)
    igsid = Column(String, nullable=True, index=True)  # Instagram Business Account ID (from webhook entry.id)
    is_active = Column(Boolean, default=True)
