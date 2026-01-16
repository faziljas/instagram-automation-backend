from sqlalchemy import Column, Integer, String, Boolean, ForeignKey
from app.db.base import Base


class InstagramAccount(Base):
    __tablename__ = "instagram_accounts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    username = Column(String, nullable=False)
    encrypted_credentials = Column(String, nullable=False)  # Legacy field, kept for backward compatibility
    encrypted_page_token = Column(String, nullable=True)  # Encrypted Facebook Page Access Token
    page_id = Column(String, nullable=True)  # Facebook Page ID
    igsid = Column(String, nullable=True, index=True)  # Instagram Business Account ID
    is_active = Column(Boolean, default=True)
