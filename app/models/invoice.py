from sqlalchemy import Column, Integer, String, ForeignKey, DateTime
from datetime import datetime
from app.db.base import Base


class Invoice(Base):
    """
    Stores individual payment/invoice records coming from Dodo Payments.

    One row per successful (or failed) payment so we can render
    invoice history in the LogicDM UI.
    """

    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    # Dodo identifiers
    provider = Column(String, nullable=False, default="dodo")
    provider_invoice_id = Column(String, nullable=True, index=True, unique=True)
    provider_payment_id = Column(String, nullable=True, index=True, unique=True)

    # Amounts are stored in minor units (e.g. cents)
    amount = Column(Integer, nullable=False)  # total amount in minor units
    currency = Column(String, nullable=False)

    status = Column(String, nullable=False, default="succeeded")
    invoice_url = Column(String, nullable=True)

    paid_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

