from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Numeric
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
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    # Dodo identifiers
    provider = Column(String, nullable=False, default="dodo")
    provider_invoice_id = Column(String, nullable=True, index=True, unique=True)
    provider_payment_id = Column(String, nullable=True, index=True, unique=True)

    # Amount in major units (e.g. 11.81 for SGD 11.81); stored as decimal for precision
    amount = Column(Numeric(12, 2), nullable=False)
    currency = Column(String, nullable=False)

    status = Column(String, nullable=False, default="succeeded")
    invoice_url = Column(String, nullable=True)

    paid_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

