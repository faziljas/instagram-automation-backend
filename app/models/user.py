from sqlalchemy import Column, Integer, String, Boolean, DateTime
from sqlalchemy.sql import func
from app.db.base import Base

class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    supabase_id = Column(String, unique=True, index=True, nullable=True)  # Track Supabase user ID
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)
    profile_picture_url = Column(String, nullable=True)  # Profile picture URL (base64 data URL or external URL)
    notify_product_updates = Column(Boolean, default=True, nullable=False)  # Email for product/news
    notify_billing = Column(Boolean, default=True, nullable=False)  # Email for billing/invoices
    is_active = Column(Boolean, default=True)
    is_verified = Column(Boolean, default=False)
    plan_tier = Column(String, default="free", nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())