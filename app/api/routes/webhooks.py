import os
import stripe
from datetime import datetime
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
    if event["type"] == "checkout.session.completed":
        # Handle successful checkout (subscription created)
        handle_checkout_session_completed(event["data"]["object"], db)
    elif event["type"] == "customer.subscription.created":
        handle_subscription_created(event["data"]["object"], db)
    elif event["type"] == "customer.subscription.updated":
        handle_subscription_updated(event["data"]["object"], db)
    elif event["type"] == "customer.subscription.deleted":
        handle_subscription_deleted(event["data"]["object"], db)

    return {"status": "success"}


def handle_checkout_session_completed(session_data: dict, db: Session):
    """Handle successful checkout session completion."""
    print(f"✅ Checkout session completed: {session_data.get('id')}")
    
    # Extract user ID from metadata
    user_id = session_data.get("metadata", {}).get("user_id")
    customer_email = session_data.get("customer_email")
    
    if not user_id:
        print("⚠️ No user_id in checkout session metadata")
        return
    
    user = db.query(User).filter(User.id == int(user_id)).first()
    if not user:
        print(f"❌ User not found: {user_id}")
        return
    
    # Get subscription ID from checkout session
    subscription_id_raw = session_data.get("subscription")
    customer_id = session_data.get("customer")
    
    if not subscription_id_raw:
        print("⚠️ No subscription ID in checkout session")
        return
    
    # Handle subscription_id - it might be a string or an object
    if isinstance(subscription_id_raw, str):
        subscription_id = subscription_id_raw
    elif hasattr(subscription_id_raw, 'id'):
        subscription_id = subscription_id_raw.id
    elif isinstance(subscription_id_raw, dict) and 'id' in subscription_id_raw:
        subscription_id = subscription_id_raw['id']
    else:
        subscription_id = str(subscription_id_raw)
    
    print(f"📋 Extracted subscription ID: {subscription_id}")
    
    # Retrieve subscription details from Stripe
    try:
        subscription = stripe.Subscription.retrieve(subscription_id)
        
        # Create or update subscription record
        db_subscription = db.query(Subscription).filter(
            Subscription.user_id == user.id
        ).first()
        
        if db_subscription:
            db_subscription.stripe_subscription_id = subscription_id
            db_subscription.status = subscription.status
        else:
            db_subscription = Subscription(
                user_id=user.id,
                stripe_subscription_id=subscription_id,
                status=subscription.status
            )
            db.add(db_subscription)
        
        # Update user plan tier
        plan_tier = get_plan_tier_from_subscription(subscription.to_dict())
        print(f"📊 Determined plan tier: {plan_tier} for subscription {subscription_id}")
        
        if plan_tier == "free":
            print(f"⚠️ WARNING: Plan tier is 'free' for subscription {subscription_id}. Price ID: {subscription.items.data[0].price.id if subscription.items.data else 'N/A'}")
            
            # Fallback: If we have an active subscription but plan_tier is free, default to pro
            # This handles cases where STRIPE_PRICE_ID_PRO might not be configured correctly
            if subscription.status == "active" and subscription.items.data:
                price_id = subscription.items.data[0].price.id
                print(f"⚠️ Subscription is active but plan_tier is 'free'. Price ID: {price_id}")
                print(f"⚠️ Defaulting to 'pro' plan for active subscription")
                plan_tier = "pro"
        
        # Check if user is upgrading TO Pro/Enterprise (was on free/basic before)
        previous_plan_tier = user.plan_tier
        is_upgrading_to_pro = previous_plan_tier not in ["pro", "enterprise"] and plan_tier in ["pro", "enterprise"]
        
        user.plan_tier = plan_tier
        
        # Reset billing cycle start date when upgrading TO Pro/Enterprise (fresh start)
        # This ensures DMs count resets to zero on upgrade
        if is_upgrading_to_pro:
            db_subscription.billing_cycle_start_date = datetime.utcnow()
            print(f"✅ Reset billing cycle start date for user {user_id} (upgrading to {plan_tier}): {db_subscription.billing_cycle_start_date}")
        # Set billing cycle start date for Pro/Enterprise users if not set (new subscription)
        elif plan_tier in ["pro", "enterprise"] and not db_subscription.billing_cycle_start_date:
            db_subscription.billing_cycle_start_date = datetime.utcnow()
            print(f"✅ Set billing cycle start date for user {user_id}: {db_subscription.billing_cycle_start_date}")
        
        # Reset global trackers for all user's Instagram accounts when upgrading to Pro
        if plan_tier in ["pro", "enterprise"]:
            from app.services.instagram_usage_tracker import reset_tracker_for_pro_upgrade
            
            # Reset all trackers for this user
            reset_tracker_for_pro_upgrade(user.id, db)
        
        db.commit()
        print(f"✅ User {user_id} upgraded to {plan_tier} plan")
        
    except stripe.error.StripeError as e:
        print(f"❌ Error retrieving subscription from Stripe: {str(e)}")
        db.rollback()


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
    
    # Check if user is upgrading TO Pro/Enterprise (was on free/basic before)
    previous_plan_tier = user.plan_tier
    is_upgrading_to_pro = previous_plan_tier not in ["pro", "enterprise"] and plan_tier in ["pro", "enterprise"]
    
    user.plan_tier = plan_tier
    
    # Reset billing cycle start date when upgrading TO Pro/Enterprise (fresh start)
    # This ensures DMs count resets to zero on upgrade
    if is_upgrading_to_pro:
        subscription.billing_cycle_start_date = datetime.utcnow()
        print(f"✅ Reset billing cycle start date for user {user.id} (upgrading to {plan_tier}): {subscription.billing_cycle_start_date}")
    # Set billing cycle start date for Pro/Enterprise users if not set (new subscription)
    elif plan_tier in ["pro", "enterprise"] and not subscription.billing_cycle_start_date:
        subscription.billing_cycle_start_date = datetime.utcnow()
        print(f"✅ Set billing cycle start date for user {user.id}: {subscription.billing_cycle_start_date}")
    
    # Reset global trackers for all user's Instagram accounts when upgrading to Pro
    if plan_tier in ["pro", "enterprise"]:
        from app.services.instagram_usage_tracker import reset_tracker_for_pro_upgrade
        
        # Reset all trackers for this user
        reset_tracker_for_pro_upgrade(user.id, db)

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
            
            # Check if user is upgrading TO Pro/Enterprise (was on free/basic before)
            previous_plan_tier = user.plan_tier
            is_upgrading_to_pro = previous_plan_tier not in ["pro", "enterprise"] and plan_tier in ["pro", "enterprise"]
            
            user.plan_tier = plan_tier
            
            # Reset billing cycle start date when upgrading TO Pro/Enterprise (fresh start)
            # This ensures DMs count resets to zero on upgrade
            if is_upgrading_to_pro:
                subscription.billing_cycle_start_date = datetime.utcnow()
                print(f"✅ Reset billing cycle start date for user {user.id} (upgrading to {plan_tier}): {subscription.billing_cycle_start_date}")
            # Set billing cycle start date for Pro/Enterprise users if not set (new subscription)
            elif plan_tier in ["pro", "enterprise"] and not subscription.billing_cycle_start_date:
                subscription.billing_cycle_start_date = datetime.utcnow()
                print(f"✅ Set billing cycle start date for user {user.id}: {subscription.billing_cycle_start_date}")
            
            # Reset global trackers for all user's Instagram accounts when upgrading to Pro
            if plan_tier in ["pro", "enterprise"]:
                from app.services.instagram_usage_tracker import reset_tracker_for_pro_upgrade
                
                # Reset all trackers for this user
                reset_tracker_for_pro_upgrade(user.id, db)
        elif status in ["canceled", "incomplete_expired", "past_due"]:
            user.plan_tier = "free"
            # Clear billing cycle start date when downgraded
            subscription.billing_cycle_start_date = None

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
        print("⚠️ No items found in subscription data")
        return "free"

    price_id = items[0].get("price", {}).get("id", "")
    
    if not price_id:
        print("⚠️ No price ID found in subscription items")
        return "free"

    # Map price IDs to plan tiers (configure these in environment or database)
    # Note: STRIPE_PRICE_ID_PRO is the price ID for the Pro plan ($15/mo)
    STRIPE_PRICE_ID_PRO = os.getenv("STRIPE_PRICE_ID_PRO", "")
    STRIPE_BASIC_PRICE_ID = os.getenv("STRIPE_BASIC_PRICE_ID", "")
    STRIPE_ENTERPRISE_PRICE_ID = os.getenv("STRIPE_ENTERPRISE_PRICE_ID", "")
    
    PRICE_TO_TIER = {
        STRIPE_BASIC_PRICE_ID: "basic",
        STRIPE_PRICE_ID_PRO: "pro",  # Pro plan
        STRIPE_ENTERPRISE_PRICE_ID: "enterprise",
    }
    
    print(f"🔍 Checking price ID: {price_id}")
    print(f"🔍 STRIPE_PRICE_ID_PRO: {STRIPE_PRICE_ID_PRO}")
    print(f"🔍 Price mapping: {PRICE_TO_TIER}")
    
    # Check if price ID matches Pro plan
    if price_id == STRIPE_PRICE_ID_PRO:
        print(f"✅ Matched Pro plan for price ID: {price_id}")
        return "pro"
    
    tier = PRICE_TO_TIER.get(price_id, "free")
    if tier != "free":
        print(f"✅ Matched {tier} plan for price ID: {price_id}")
    else:
        print(f"⚠️ No matching plan tier found for price ID: {price_id}. Defaulting to 'free'")
    
    return tier
