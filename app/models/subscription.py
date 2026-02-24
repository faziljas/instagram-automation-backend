from sqlalchemy import Column, Integer, String, ForeignKey, DateTime
from datetime import datetime
from app.db.base import Base


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    stripe_subscription_id = Column(String, unique=True, nullable=True)
    # Dodo Payments identifiers (Merchant of Record)
    # Kept separate from Stripe to avoid confusion and ease migration.
    dodo_subscription_id = Column(String, unique=True, nullable=True)
    dodo_customer_id = Column(String, unique=False, nullable=True)
    status = Column(String, nullable=False, default="inactive")
    billing_cycle_start_date = Column(DateTime, nullable=True)  # For Pro users: cycle start from upgrade
    billing_interval = Column(String, nullable=True, default="monthly")  # "monthly" (30d) or "yearly" (365d)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
