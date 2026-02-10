"""
Dodo Payments (Merchant of Record) integration.
Handles checkout session creation, verification, and billing portal.
Replace Stripe with Dodo credentials in .env (see DODO_INTEGRATION.md).
"""
import os
import httpx
from fastapi import APIRouter, Depends, HTTPException, status, Body
from sqlalchemy.orm import Session
from pydantic import BaseModel
from app.db.session import get_db
from app.models.user import User
from app.models.subscription import Subscription
from app.dependencies.auth import get_current_user_id

router = APIRouter()

DODO_API_KEY = os.getenv("DODO_API_KEY", "")
DODO_WEBHOOK_SECRET = os.getenv("DODO_WEBHOOK_SECRET", "")
DODO_PRODUCT_OR_PLAN_ID = os.getenv("DODO_PRODUCT_OR_PLAN_ID", "")  # Pro plan in test mode
DODO_BASE_URL = os.getenv("DODO_BASE_URL", "").rstrip("/")  # Test base URL, e.g. https://test.dodopayments.com
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")


def _dodo_configured() -> bool:
    return bool(DODO_API_KEY and DODO_PRODUCT_OR_PLAN_ID and DODO_BASE_URL)


@router.post("/create-checkout-session")
async def create_checkout_session(
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id)
):
    """
    Create a Dodo Payments checkout session for subscription upgrade.
    Returns the checkout URL to redirect the user to.
    """
    if not _dodo_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Dodo Payments not configured. Set DODO_API_KEY, DODO_PRODUCT_OR_PLAN_ID, and DODO_BASE_URL. See DODO_INTEGRATION.md."
        )

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    subscription = db.query(Subscription).filter(Subscription.user_id == user_id).first()
    if subscription and subscription.status == "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User already has an active subscription"
        )

    # Call Dodo API to create a checkout session for the Pro product.
    try:
        headers = {"Authorization": f"Bearer {DODO_API_KEY}", "Content-Type": "application/json"}
        # NOTE: Billing address values here are placeholders suitable for test mode.
        # If you want to collect real billing info, pass it from the frontend and
        # populate the fields below instead of hard-coding them.
        payload = {
            "product_cart": [
                {
                    "product_id": DODO_PRODUCT_OR_PLAN_ID,
                    "quantity": 1,
                }
            ],
            "billing": {
                "country": "US",
                "city": "New York",
                "state": "NY",
                "zipcode": "10001",
                "street": "123 Main St",
            },
            "customer": {
                "email": user.email,
                "name": user.full_name or user.email,
            },
            "return_url": f"{FRONTEND_URL}/dashboard/subscription",
            "payment_link": True,
            "metadata": {
                "user_id": str(user_id),
            },
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Adjust endpoint to Dodo's actual API (check their docs)
            r = await client.post(
                f"{DODO_BASE_URL}/checkout/sessions",
                json=payload,
                headers=headers,
            )
        if r.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Dodo Payments error: {r.text or r.status_code}"
            )
        data = r.json()
        checkout_url = data.get("checkout_url") or data.get("url") or data.get("redirect_url")
        session_id = data.get("session_id") or data.get("id") or data.get("checkout_id")
        if not checkout_url or not session_id:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Dodo did not return checkout_url and session_id. See DODO_INTEGRATION.md for response shape."
            )
        return {"checkout_url": checkout_url, "session_id": str(session_id)}
    except Exception as e:
        if "httpx" in str(type(e).__module__):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Dodo Payments request failed: {str(e)}"
            )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Dodo integration requires httpx. Install with: pip install httpx. Error: {e}"
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
    Best-effort verification endpoint.

    Dodo uses webhooks (subscription.active) as the source of truth for subscription
    status. This endpoint simply returns the latest known subscription status for
    the current user so the frontend can update UI while polling continues.
    """
    if not _dodo_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Dodo Payments not configured. See DODO_INTEGRATION.md."
        )

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    db_sub = db.query(Subscription).filter(Subscription.user_id == user_id).first()
    if not db_sub:
        # No subscription yet – webhook probably hasn't fired.
        return {
            "message": "Subscription not yet created; waiting for Dodo webhook.",
            "plan_tier": user.plan_tier,
            "status": "inactive",
        }

    return {
        "message": "Subscription status from database.",
        "plan_tier": user.plan_tier,
        "status": db_sub.status,
    }


@router.post("/create-portal-session")
async def create_portal_session(
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    """
    Create a Dodo billing/customer portal session for managing subscription and payment methods.
    """
    if not _dodo_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Dodo Payments not configured. See DODO_INTEGRATION.md."
        )

    subscription = db.query(Subscription).filter(Subscription.user_id == user_id).first()
    if not subscription or not subscription.dodo_customer_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No active subscription found for this user",
        )

    # TODO: Call Dodo API to create customer portal link (e.g. POST /portal or /billing/portal).
    try:
        headers = {"Authorization": f"Bearer {DODO_API_KEY}", "Content-Type": "application/json"}
        payload = {
            "return_url": f"{FRONTEND_URL}/dashboard/settings",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{DODO_BASE_URL}/customers/{subscription.dodo_customer_id}/customer-portal/session",
                json=payload,
                headers=headers,
            )
        if r.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Dodo portal error: {r.text or r.status_code}"
            )
        data = r.json()
        # Dodo returns {"link": "https://billing.dodopayments.com/p/login/..."}
        portal_url = data.get("link") or data.get("portal_url") or data.get("url") or data.get("redirect_url")
        if not portal_url:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Dodo did not return portal_url. See DODO_INTEGRATION.md."
            )
        return {"portal_url": portal_url}
    except Exception as e:
        if "httpx" in str(type(e).__module__):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Dodo request failed: {str(e)}"
            )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Install httpx: pip install httpx. Error: {e}"
        )
