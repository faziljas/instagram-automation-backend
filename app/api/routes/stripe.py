"""
Stripe Checkout Session Routes
Handles Stripe Checkout Session creation for subscription upgrades
"""
import os
import stripe
from fastapi import APIRouter, Depends, HTTPException, status, Header, Body
from sqlalchemy.orm import Session
from pydantic import BaseModel
from app.db.session import get_db
from app.models.user import User
from app.models.subscription import Subscription
from app.utils.auth import verify_token

router = APIRouter()

# Initialize Stripe
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PRICE_ID_PRO = os.getenv("STRIPE_PRICE_ID_PRO", "")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")


def get_current_user_id(authorization: str = Header(None)) -> int:
    """Extract and verify user ID from JWT token in Authorization header."""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization token"
        )
    
    try:
        # Extract token from "Bearer <token>"
        token = authorization.replace("Bearer ", "")
        payload = verify_token(token)
        user_id = payload.get("sub")
        
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token payload"
            )
        
        return int(user_id)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token"
        )


@router.post("/create-checkout-session")
async def create_checkout_session(
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id)
):
    """
    Create a Stripe Checkout Session for subscription upgrade.
    Returns the checkout URL to redirect the user to.
    """
    if not STRIPE_PRICE_ID_PRO:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stripe price ID not configured"
        )
    
    # Get user
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    # Check if user already has an active subscription
    subscription = db.query(Subscription).filter(
        Subscription.user_id == user_id
    ).first()
    
    if subscription and subscription.status == "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User already has an active subscription"
        )
    
    try:
        # Create or retrieve Stripe customer
        stripe_customer_id = None
        
        # Check if user has existing Stripe customer ID in subscription
        if subscription and subscription.stripe_subscription_id:
            # Try to get customer from existing subscription
            try:
                existing_sub = stripe.Subscription.retrieve(subscription.stripe_subscription_id)
                stripe_customer_id = existing_sub.customer if existing_sub else None
            except:
                pass
        
        # Create new customer if not exists
        if not stripe_customer_id:
            customer = stripe.Customer.create(
                email=user.email,
                metadata={
                    "user_id": str(user_id),
                    "user_email": user.email
                }
            )
            stripe_customer_id = customer.id
        
        # Create Checkout Session
        checkout_session = stripe.checkout.Session.create(
            customer=stripe_customer_id,
            payment_method_types=["card"],
            line_items=[
                {
                    "price": STRIPE_PRICE_ID_PRO,
                    "quantity": 1,
                }
            ],
            mode="subscription",
            success_url=f"{FRONTEND_URL}/dashboard/subscription?success=true&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{FRONTEND_URL}/dashboard/subscription?canceled=true",
            metadata={
                "user_id": str(user_id),
                "user_email": user.email
            },
            subscription_data={
                "metadata": {
                    "user_id": str(user_id),
                    "user_email": user.email
                }
            }
        )
        
        print(f"✅ Created Stripe Checkout Session: {checkout_session.id} for user {user_id}")
        
        return {
            "checkout_url": checkout_session.url,
            "session_id": checkout_session.id
        }
        
    except stripe.error.StripeError as e:
        print(f"❌ Stripe error creating checkout session: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create checkout session: {str(e)}"
        )
    except Exception as e:
        print(f"❌ Error creating checkout session: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create checkout session: {str(e)}"
        )


class VerifyCheckoutRequest(BaseModel):
    session_id: str


@router.post("/verify-checkout-session")
async def verify_checkout_session(
    request: VerifyCheckoutRequest = Body(...),
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id)
):
    """
    Verify and sync subscription status from Stripe checkout session.
    Called after successful checkout to immediately update user's plan.
    """
    try:
        # Retrieve checkout session from Stripe
        checkout_session = stripe.checkout.Session.retrieve(request.session_id)
        
        # Verify this session belongs to the current user
        session_user_id = checkout_session.metadata.get("user_id")
        if not session_user_id or int(session_user_id) != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This checkout session does not belong to you"
            )
        
        # Check if checkout is completed
        if checkout_session.payment_status != "paid":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Checkout session is not paid"
            )
        
        # Get subscription ID from checkout session
        subscription_id_raw = checkout_session.subscription
        if not subscription_id_raw:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No subscription found in checkout session"
            )
        
        # Handle subscription_id - it might be a string or an object
        if isinstance(subscription_id_raw, str):
            subscription_id_str = subscription_id_raw
        elif hasattr(subscription_id_raw, 'id'):
            subscription_id_str = subscription_id_raw.id
        else:
            subscription_id_str = str(subscription_id_raw)
        
        # Retrieve subscription from Stripe
        subscription = stripe.Subscription.retrieve(subscription_id_str)
        
        # Get user
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
        
        # Create or update subscription record
        db_subscription = db.query(Subscription).filter(
            Subscription.user_id == user_id
        ).first()
        
        if db_subscription:
            db_subscription.stripe_subscription_id = subscription_id_str
            db_subscription.status = subscription.status
        else:
            db_subscription = Subscription(
                user_id=user_id,
                stripe_subscription_id=subscription_id_str,
                status=subscription.status
            )
            db.add(db_subscription)
        
        # Update user plan tier based on subscription
        from app.api.routes.webhooks import get_plan_tier_from_subscription
        plan_tier = get_plan_tier_from_subscription(subscription.to_dict())
        user.plan_tier = plan_tier
        
        # Set billing cycle start date for Pro/Enterprise users (30-day cycle from upgrade date)
        if plan_tier in ["pro", "enterprise"] and not db_subscription.billing_cycle_start_date:
            from datetime import datetime
            db_subscription.billing_cycle_start_date = datetime.utcnow()
            print(f"✅ Set billing cycle start date for user {user_id}: {db_subscription.billing_cycle_start_date}")
        
        # Reset global trackers for all user's Instagram accounts when upgrading to Pro
        if plan_tier in ["pro", "enterprise"]:
            from app.models.instagram_account import InstagramAccount
            from app.services.instagram_usage_tracker import get_or_create_tracker, reset_tracker_for_pro_upgrade
            
            user_accounts = db.query(InstagramAccount).filter(
                InstagramAccount.user_id == user_id,
                InstagramAccount.igsid.isnot(None)
            ).all()
            
            for account in user_accounts:
                if account.igsid:
                    try:
                        tracker = get_or_create_tracker(user_id, account.igsid, db)
                        reset_tracker_for_pro_upgrade(tracker, db)
                        print(f"✅ Reset tracker for Pro upgrade - IGSID {account.igsid}")
                    except Exception as e:
                        print(f"⚠️ Failed to reset tracker for IGSID {account.igsid}: {str(e)}")
        
        db.commit()
        db.refresh(user)
        db.refresh(db_subscription)
        
        print(f"✅ User {user_id} subscription synced: {plan_tier} plan (status: {subscription.status})")
        
        return {
            "message": "Subscription verified and updated",
            "plan_tier": plan_tier,
            "status": subscription.status
        }
        
    except stripe.error.StripeError as e:
        print(f"❌ Stripe error verifying checkout session: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to verify checkout session: {str(e)}"
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error verifying checkout session: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to verify checkout session: {str(e)}"
        )
