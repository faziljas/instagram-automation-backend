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
from fastapi import APIRouter, Request, HTTPException, status, Depends
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models.user import User
from app.models.subscription import Subscription

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

    The resulting HMAC-SHA256 digest (with DODO_WEBHOOK_SECRET as key) is sent in the
    `webhook-signature` header, typically prefixed with "v1,".
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

    # Build the signed message exactly as Dodo/Standard Webhooks expect:
    # "<webhook-id>.<webhook-timestamp>.<raw_body>"
    signed_payload = f"{webhook_id}.{webhook_timestamp}.{payload.decode()}".encode()

    mac = hmac.new(
        DODO_WEBHOOK_SECRET.encode(),
        signed_payload,
        hashlib.sha256,
    )
    expected_hex = mac.hexdigest()

    # Signature is hex digest; keep comparison timing‑safe.
    return hmac.compare_digest(raw, expected_hex)


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
        # For now we only log these; subscription status is handled by subscription.* events.
        print(f"[Dodo webhook] payment event: {event_type} for subscription_id={sub_id} customer_id={customer_id}")

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
