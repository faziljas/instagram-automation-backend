import os
import stripe
from fastapi import APIRouter, Request, HTTPException, status, Depends
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models.user import User
from app.models.subscription import Subscription

router = APIRouter()

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")


@router.post("/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid payload"
        )
    except stripe.error.SignatureVerificationError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid signature"
        )

    # Handle the event
    if event["type"] == "customer.subscription.created":
        handle_subscription_created(event["data"]["object"], db)
    elif event["type"] == "customer.subscription.updated":
        handle_subscription_updated(event["data"]["object"], db)
    elif event["type"] == "customer.subscription.deleted":
        handle_subscription_deleted(event["data"]["object"], db)

    return {"status": "success"}


def handle_subscription_created(subscription_data: dict, db: Session):
    """Handle new subscription creation from Stripe."""
    stripe_subscription_id = subscription_data["id"]
    stripe_customer_id = subscription_data["customer"]
    status = subscription_data["status"]

    # Get user by stripe customer ID (assumes user.email or custom metadata)
    # For simplicity, we'll look up by subscription metadata
    user_id = subscription_data.get("metadata", {}).get("user_id")
    if not user_id:
        return

    user = db.query(User).filter(User.id == int(user_id)).first()
    if not user:
        return

    # Create or update subscription
    subscription = db.query(Subscription).filter(
        Subscription.user_id == user.id
    ).first()

    if subscription:
        subscription.stripe_subscription_id = stripe_subscription_id
        subscription.status = status
    else:
        subscription = Subscription(
            user_id=user.id,
            stripe_subscription_id=stripe_subscription_id,
            status=status
        )
        db.add(subscription)

    # Update user plan tier based on subscription
    plan_tier = get_plan_tier_from_subscription(subscription_data)
    user.plan_tier = plan_tier

    db.commit()


def handle_subscription_updated(subscription_data: dict, db: Session):
    """Handle subscription updates (status changes, plan changes)."""
    stripe_subscription_id = subscription_data["id"]
    status = subscription_data["status"]

    subscription = db.query(Subscription).filter(
        Subscription.stripe_subscription_id == stripe_subscription_id
    ).first()

    if not subscription:
        return

    subscription.status = status

    # Update user plan tier
    user = db.query(User).filter(User.id == subscription.user_id).first()
    if user:
        if status == "active":
            plan_tier = get_plan_tier_from_subscription(subscription_data)
            user.plan_tier = plan_tier
        elif status in ["canceled", "incomplete_expired", "past_due"]:
            user.plan_tier = "free"

    db.commit()


def handle_subscription_deleted(subscription_data: dict, db: Session):
    """Handle subscription cancellation."""
    stripe_subscription_id = subscription_data["id"]

    subscription = db.query(Subscription).filter(
        Subscription.stripe_subscription_id == stripe_subscription_id
    ).first()

    if not subscription:
        return

    subscription.status = "canceled"

    # Downgrade user to free tier
    user = db.query(User).filter(User.id == subscription.user_id).first()
    if user:
        user.plan_tier = "free"

    db.commit()


def get_plan_tier_from_subscription(subscription_data: dict) -> str:
    """Extract plan tier from Stripe subscription data."""
    # Get price ID from subscription items
    items = subscription_data.get("items", {}).get("data", [])
    if not items:
        return "free"

    price_id = items[0].get("price", {}).get("id", "")

    # Map price IDs to plan tiers (configure these in environment or database)
    PRICE_TO_TIER = {
        os.getenv("STRIPE_BASIC_PRICE_ID", ""): "basic",
        os.getenv("STRIPE_PRO_PRICE_ID", ""): "pro",
        os.getenv("STRIPE_ENTERPRISE_PRICE_ID", ""): "enterprise",
    }

    return PRICE_TO_TIER.get(price_id, "free")
