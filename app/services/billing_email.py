"""
Send billing-related emails (invoice receipts) when user has notify_billing enabled.
Uses Resend if RESEND_API_KEY is set; otherwise no-op so webhooks never fail.
"""
import os
from decimal import Decimal
from datetime import datetime
from typing import Optional

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
# Use same sender as Supabase auth (signup/forgot password); override with BILLING_FROM_EMAIL if you add billing@ later
FROM_EMAIL = os.getenv("BILLING_FROM_EMAIL", "LogicDM <admin@logicdm.app>")
APP_NAME = os.getenv("APP_NAME", "LogicDM")


def send_invoice_receipt_email(
    to_email: str,
    amount: Decimal,
    currency: str,
    invoice_url: Optional[str] = None,
    paid_at: Optional[datetime] = None,
) -> bool:
    """
    Send a payment receipt email after a successful payment.
    Returns True if sent (or attempted), False if skipped (no API key).
    Does not raise; logs errors so webhook processing is never broken.
    """
    if not RESEND_API_KEY or not to_email:
        return False

    try:
        import resend
        resend.api_key = RESEND_API_KEY
    except ImportError:
        print("[billing_email] resend package not installed; pip install resend")
        return False

    amount_str = f"{amount:.2f}"
    currency_display = currency.upper() if currency else "USD"
    date_str = paid_at.strftime("%B %d, %Y") if paid_at else ""

    subject = f"Your {APP_NAME} payment receipt – {currency_display} {amount_str}"
    html = f"""
    <p>Hi,</p>
    <p>Your payment has been received.</p>
    <p><strong>Amount:</strong> {currency_display} {amount_str}</p>
    <p><strong>Date:</strong> {date_str}</p>
    """
    if invoice_url:
        html += f'<p><a href="{invoice_url}">View or download your invoice</a></p>'
    html += f"""
    <p>Thank you for using {APP_NAME}.</p>
    """

    try:
        params = {
            "from": FROM_EMAIL,
            "to": [to_email],
            "subject": subject,
            "html": html.strip(),
        }
        resend.Emails.send(params)
        print(f"[billing_email] Receipt email sent to {to_email}")
        return True
    except Exception as e:
        print(f"[billing_email] Failed to send receipt to {to_email}: {e}")
        return False
