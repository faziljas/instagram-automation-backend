"""
Stripe Checkout Session Routes
Handles Stripe Checkout Session creation for subscription upgrades
"""
import os
import stripe
from fastapi import APIRouter, Depends, HTTPException, status, Header
from sqlalchemy.orm import Session
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
