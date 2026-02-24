"""
Webhooks for payment provider (Dodo Payments).
Stripe has been removed; implement signature verification and event parsing per Dodo docs.
"""
import os
import json
import hmac
import hashlib
import base64
from datetime import datetime
from decimal import Decimal
from fastapi import APIRouter, Request, HTTPException, status, Depends
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models.user import User
from app.models.subscription import Subscription
from app.models.invoice import Invoice

router = APIRouter()

DODO_WEBHOOK_SECRET = os.getenv("DODO_WEBHOOK_SECRET", "")


def _verify_dodo_signature(
    payload: bytes,
    signature_header: str | None,
    webhook_id: str | None,
    webhook_timestamp: str | None,
) -> bool:
    """
    Verify webhook signature using the Standard Webhooks spec that Dodo implements.

    Docs: https://docs.dodopayments.com/developer-resources/webhooks
    Signed payload format:
        f"{webhook_id}.{webhook_timestamp}.{raw_body}"

    The resulting HMAC-SHA256 digest (with the decoded webhook secret as key) is sent
    in the `webhook-signature` header, typically prefixed with "v1,".
    """
    if (
        not DODO_WEBHOOK_SECRET
        or not signature_header
        or not webhook_id
        or not webhook_timestamp
    ):
        return False
    raw = signature_header.strip()
    # Dodo sends signatures in the form "v1,<signature>" where <signature> is the
    # hex‑encoded HMAC digest. Normalize it.
    if raw.startswith("v1,"):
        raw = raw.split(",", 1)[1].strip()
    elif raw.startswith("v1="):
        raw = raw.split("=", 1)[1].strip()

    # Build the signed message exactly as Dodo/Standard Webhooks expects:
    # "<webhook-id>.<webhook-timestamp>.<raw_body>"
    signed_payload = f"{webhook_id}.{webhook_timestamp}.{payload.decode()}".encode()

    # Standard Webhooks secrets are base64-encoded and prefixed with "whsec_".
    # Decode to obtain the actual HMAC key bytes.
    secret = DODO_WEBHOOK_SECRET
    try:
        if secret.startswith("whsec_"):
            key_bytes = base64.b64decode(secret.split("_", 1)[1])
        else:
            key_bytes = secret.encode()
    except Exception:
        return False

    mac = hmac.new(key_bytes, signed_payload, hashlib.sha256)
    digest = mac.digest()
    expected_b64 = base64.b64encode(digest).decode()
    expected_hex = mac.hexdigest()

    # Dodo/Standard Webhooks use base64, but accept hex as fallback just in case.
    return hmac.compare_digest(raw, expected_b64) or hmac.compare_digest(raw, expected_hex)


@router.post("/dodo")
async def dodo_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Dodo Payments webhook. Register this URL in Dodo dashboard (test mode):
    https://your-backend.com/webhooks/dodo
    """
    payload = await request.body()
    sig_header = request.headers.get("webhook-signature")
    webhook_id = request.headers.get("webhook-id")
    webhook_timestamp = request.headers.get("webhook-timestamp")

    if DODO_WEBHOOK_SECRET and not _verify_dodo_signature(
        payload, sig_header, webhook_id, webhook_timestamp
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook signature"
        )

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON")

    event_type = data.get("type")
    # Dodo payload shape: {"type": "...", "data": {...}}
    obj = data.get("data") or {}

    print(f"[Dodo webhook] type={event_type} keys={list(obj.keys()) if isinstance(obj, dict) else 'n/a'}")

    sub_id = obj.get("subscription_id")
    customer = obj.get("customer") or {}
    customer_id = customer.get("customer_id") or obj.get("customer_id")
    meta = obj.get("metadata", {}) or {}
    user_id = meta.get("user_id")
    customer_email = customer.get("email")

    if event_type in ("subscription.active", "subscription.updated"):
        _handle_subscription_active(db, obj, user_id, customer_email, sub_id, customer_id)
    elif event_type == "subscription.cancelled":
        _handle_subscription_cancelled(db, obj, sub_id)
    elif event_type in ("payment.succeeded", "payment.failed"):
        _handle_payment_event(db, obj, event_type)

    return {"status": "success"}


def _handle_subscription_active(
    db: Session,
    obj: dict,
    user_id: str | None,
    customer_email: str | None,
    sub_id: str | None,
    customer_id: str | None,
) -> None:
    """Handle subscription.active – user has an active Pro subscription."""
    if not sub_id:
        return

    user = None

    # Prefer explicit user_id from metadata if available
    if user_id is not None:
        try:
            user_id_int = int(user_id)
            user = db.query(User).filter(User.id == user_id_int).first()
        except (TypeError, ValueError):
            user = None

    # Fallback: resolve by customer email from Dodo payload
    if user is None and customer_email:
        user = db.query(User).filter(User.email == customer_email).first()

    if not user:
        return

    user_id_int = user.id


    subscription = db.query(Subscription).filter(Subscription.user_id == user_id_int).first()
    status_val = (obj.get("status") or "active").lower()
    if status_val == "canceled":
        status_val = "cancelled"

    if subscription:
        subscription.dodo_subscription_id = str(sub_id)
        subscription.dodo_customer_id = customer_id
        subscription.status = status_val
    else:
        subscription = Subscription(
            user_id=user_id_int,
            dodo_subscription_id=str(sub_id),
            dodo_customer_id=customer_id,
            status=status_val,
        )
        db.add(subscription)

    if status_val == "active":
        user.plan_tier = "pro"
        if not subscription.billing_cycle_start_date:
            subscription.billing_cycle_start_date = datetime.utcnow()
        from app.services.instagram_usage_tracker import reset_tracker_for_pro_upgrade

        reset_tracker_for_pro_upgrade(user_id_int, db)

    db.commit()
    from app.api.routes.users import invalidate_subscription_cache
    invalidate_subscription_cache(user_id_int)


def _handle_subscription_cancelled(
    db: Session,
    obj: dict,
    sub_id: str | None,
) -> None:
    """Handle subscription.cancelled – mark subscription as cancelled but keep access until period end."""
    if not sub_id:
        return

    subscription = (
        db.query(Subscription)
        .filter(Subscription.dodo_subscription_id == str(sub_id))
        .first()
    )
    if not subscription:
        return

    subscription.status = "cancelled"
    db.commit()
    from app.api.routes.users import invalidate_subscription_cache
    invalidate_subscription_cache(subscription.user_id)


def _handle_payment_event(
    db: Session,
    obj: dict,
    event_type: str,
) -> None:
    """
    Handle payment.succeeded / payment.failed to build invoice history.

    Example payload (simplified):
    {
      "type": "payment.succeeded",
      "data": {
        "customer": {"customer_id": "cus_...", "email": "..."},
        "invoice_id": "inv_...",
        "invoice_url": "https://...",
        "payment_id": "pay_...",
        "total_amount": 400,
        "currency": "USD",
        "status": "succeeded",
        "created_at": "2025-08-04T05:30:31.152232Z",
        "subscription_id": null,
        "metadata": {"user_id": "123"}
      }
    }
    """
    data = obj or {}

    customer = data.get("customer") or {}
    customer_id = customer.get("customer_id")
    customer_email = customer.get("email")
    metadata = data.get("metadata", {}) or {}
    user_id_from_metadata = metadata.get("user_id")

    invoice_id = data.get("invoice_id")
    invoice_url = data.get("invoice_url")
    payment_id = data.get("payment_id")
    total_amount = data.get("total_amount")  # already in minor units per Dodo docs
    currency = (data.get("currency") or "").upper() or "USD"
    status = (data.get("status") or "succeeded").lower()
    created_at_str = data.get("created_at")

    # Normalise status based on event type
    if event_type == "payment.failed":
        status = "failed"

    if not total_amount or not currency:
        # Nothing useful to record
        print(f"[Dodo webhook] payment event {event_type} missing amount/currency, skipping invoice creation")
        return

    # Resolve user via multiple methods (in priority order):
    # 1. user_id from metadata (most reliable - set during checkout)
    # 2. subscription.dodo_customer_id lookup
    # 3. email lookup (fallback)
    user = None
    
    # Method 1: Check metadata.user_id first (set during checkout creation)
    if user_id_from_metadata:
        try:
            user_id_int = int(user_id_from_metadata)
            user = db.query(User).filter(User.id == user_id_int).first()
            if user:
                print(f"[Dodo webhook] payment event {event_type} resolved user via metadata.user_id: {user_id_int}")
        except (TypeError, ValueError):
            pass

    # Method 2: Lookup via subscription.dodo_customer_id
    if user is None and customer_id:
        subscription = (
            db.query(Subscription)
            .filter(Subscription.dodo_customer_id == customer_id)
            .first()
        )
        if subscription:
            user = db.query(User).filter(User.id == subscription.user_id).first()
            if user:
                print(f"[Dodo webhook] payment event {event_type} resolved user via subscription.customer_id: {customer_id}")

    # Method 3: Fallback to email lookup
    if user is None and customer_email:
        user = db.query(User).filter(User.email == customer_email).first()
        if user:
            print(f"[Dodo webhook] payment event {event_type} resolved user via email: {customer_email}")

    if user is None:
        print(
            f"[Dodo webhook] payment event {event_type} could not resolve user "
            f"(metadata.user_id={user_id_from_metadata}, customer_id={customer_id}, email={customer_email})"
        )
        return

    # Parse created_at timestamp if present
    paid_at = None
    if created_at_str:
        try:
            paid_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        except Exception:
            paid_at = None

    # Upsert by provider_invoice_id or payment_id
    invoice = None
    if invoice_id:
        invoice = (
            db.query(Invoice)
            .filter(Invoice.provider_invoice_id == invoice_id)
            .first()
        )
    if invoice is None and payment_id:
        invoice = (
            db.query(Invoice)
            .filter(Invoice.provider_payment_id == payment_id)
            .first()
        )

    # Dodo sends amount in minor units (e.g. cents). Store exact decimal (e.g. 11.81) — never round.
    amount_major = (Decimal(str(total_amount)) / 100).quantize(Decimal("0.01"))

    if invoice:
        # Update existing invoice
        invoice.amount = amount_major
        invoice.currency = currency
        invoice.status = status
        invoice.invoice_url = invoice_url or invoice.invoice_url
        invoice.paid_at = paid_at or invoice.paid_at
        print(f"[Dodo webhook] Updated existing invoice {invoice.id} for user {user.id} (amount: {amount_major} {currency})")
    else:
        # Create new invoice
        invoice = Invoice(
            user_id=user.id,
            provider="dodo",
            provider_invoice_id=invoice_id,
            provider_payment_id=payment_id,
            amount=amount_major,
            currency=currency,
            status=status,
            invoice_url=invoice_url,
            paid_at=paid_at,
        )
        db.add(invoice)
        print(f"[Dodo webhook] Created new invoice for user {user.id} (amount: {amount_major} {currency}, invoice_id: {invoice_id}, payment_id: {payment_id})")

    try:
        db.commit()
        print(f"[Dodo webhook] Successfully saved invoice for user {user.id}")
    except Exception as e:
        db.rollback()
        print(f"[Dodo webhook] Error committing invoice for user {user.id}: {str(e)}")
        import traceback
        traceback.print_exc()
        raise

    # Send receipt email when payment succeeded and user has "Billing & invoices" toggle on
    if event_type == "payment.succeeded" and status == "succeeded":
        notify_billing = getattr(user, "notify_billing", True)
        if notify_billing and user.email:
            from app.services.billing_email import send_invoice_receipt_email
            send_invoice_receipt_email(
                to_email=user.email,
                amount=amount_major,
                currency=currency,
                invoice_url=invoice_url,
                paid_at=paid_at,
            )
