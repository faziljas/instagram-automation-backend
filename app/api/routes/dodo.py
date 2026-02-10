"""
Dodo Payments (Merchant of Record) integration.
Handles checkout session creation, verification, and billing portal.
Replace Stripe with Dodo credentials in .env (see DODO_INTEGRATION.md).
"""
import os
import traceback
import httpx
from fastapi import APIRouter, Depends, HTTPException, status, Body, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from app.db.session import get_db
from app.models.user import User
from app.models.subscription import Subscription
from app.dependencies.auth import get_current_user_id

router = APIRouter()

# Prefer the new env var name, but fall back to the old one for safety.
# Strip whitespace to avoid invisible copy/paste errors.
DODO_API_KEY = (
    os.getenv("DODO_PAYMENTS_API_KEY")
    or os.getenv("DODO_API_KEY", "")
).strip()
DODO_WEBHOOK_SECRET = os.getenv("DODO_WEBHOOK_SECRET", "")
DODO_PRODUCT_OR_PLAN_ID = os.getenv("DODO_PRODUCT_OR_PLAN_ID", "")  # Pro plan in test mode
DODO_BASE_URL = os.getenv("DODO_BASE_URL", "").rstrip("/")  # Test base URL, e.g. https://test.dodopayments.com
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")


def _dodo_configured() -> bool:
    # Dodo only requires API key, base URL, and product/plan ID for checkout.
    return bool(DODO_API_KEY and DODO_BASE_URL and DODO_PRODUCT_OR_PLAN_ID)


def _dodo_missing_config_message() -> str:
    """Human-readable message listing which Dodo env vars are missing."""
    missing_parts: list[str] = []
    if not DODO_API_KEY:
        missing_parts.append("API_KEY")
    if not DODO_BASE_URL:
        missing_parts.append("BASE_URL")
    if not DODO_PRODUCT_OR_PLAN_ID:
        missing_parts.append("PRODUCT_ID")
    missing = " ".join(missing_parts) if missing_parts else "UNKNOWN"
    return (
        "Payment system not configured. Missing: "
        f"{missing}"
    )


@router.get("/check-config")
async def check_dodo_config():
    """Debug endpoint to verify Dodo env vars are loaded in this process."""
    return {
        "api_key_exists": bool(DODO_API_KEY),
        "api_key_length": len(DODO_API_KEY) if DODO_API_KEY else 0,
        "api_key_prefix": DODO_API_KEY[:15] if DODO_API_KEY else "NOT_SET",
        "base_url": DODO_BASE_URL or "NOT_SET",
        "product_id": DODO_PRODUCT_OR_PLAN_ID or "NOT_SET",
        "webhook_secret_exists": bool(DODO_WEBHOOK_SECRET),
        "all_env_vars": {
            "DODO_PAYMENTS_API_KEY": bool(os.getenv("DODO_PAYMENTS_API_KEY")),
            "DODO_API_KEY": bool(os.getenv("DODO_API_KEY")),
            "DODO_BASE_URL": bool(os.getenv("DODO_BASE_URL")),
            "DODO_PRODUCT_OR_PLAN_ID": bool(os.getenv("DODO_PRODUCT_OR_PLAN_ID")),
        },
    }


@router.post("/create-checkout-session")
async def create_checkout_session(
    request: Request,
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    """
    Create a Dodo Payments checkout session for subscription upgrade.
    Returns the checkout URL to redirect the user to.
    """
    # Helpful diagnostics without leaking full secrets
    print(
        "[Dodo] create_checkout_session: "
        f"api_key_loaded={bool(DODO_API_KEY)}, "
        f"key_prefix={DODO_API_KEY[:5] if DODO_API_KEY else 'None'}, "
        f"base_url={DODO_BASE_URL}, "
        f"product_set={bool(DODO_PRODUCT_OR_PLAN_ID)}"
    )

    if not _dodo_configured():
        print("[Dodo] Configuration missing; aborting checkout session creation.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_dodo_missing_config_message(),
        )

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    subscription = db.query(Subscription).filter(Subscription.user_id == user_id).first()
    if subscription and subscription.status == "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User already has an active subscription",
        )

    # Try to inspect request body for additional context (e.g. email/name overrides).
    # This mirrors the debugging pattern suggested in Dodo integration docs.
    try:
        body = await request.json()
    except Exception:
        body = {}

    request_email = body.get("email")
    request_name = body.get("name")
    effective_email = request_email or user.email
    print(f"[Dodo] Creating checkout for user: {effective_email}")
    if body:
        print(f"[Dodo] Raw request body: {body}")

    # Call Dodo API to create a checkout session for the Pro product.
    try:
        print("[Dodo] Building checkout payload.")
        headers = {
            "Authorization": f"Bearer {DODO_API_KEY}",
            "Content-Type": "application/json",
        }
        full_name = " ".join(
            part for part in [request_name, user.first_name, user.last_name] if part
        ).strip() or effective_email

        payload = {
            "product_cart": [
                {
                    "product_id": DODO_PRODUCT_OR_PLAN_ID,
                    "quantity": 1,
                }
            ],
            "customer": {
                "email": effective_email,
                "name": full_name,
            },
            "return_url": f"{FRONTEND_URL}/dashboard/subscription",
            "metadata": {
                "user_id": str(user_id),
            },
        }

        print(f"[Dodo] Payload sent to Dodo: {payload}")

        dodo_url = f"{DODO_BASE_URL}/checkouts"
        print(f"[Dodo] Making request to: {dodo_url}")

        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                dodo_url,
                json=payload,
                headers=headers,
            )

        print(f"[Dodo] Response status: {r.status_code}")
        print(f"[Dodo] Response body (truncated): {r.text[:500] if r.text else 'NO_BODY'}")

        if r.status_code != 200:
            # Surface Dodo's status code directly so it's not always 502.
            print(f"[Dodo] Non-200 response from Dodo: {r.status_code} - {r.text}")
            # Avoid leaking low-level messages like "Invalid bearer token" directly to users.
            raw_text = r.text or ""
            if "Invalid bearer token" in raw_text:
                user_detail = (
                    "Dodo API error: Our payment provider rejected the request. "
                    "Please try again or contact support if this continues."
                )
            else:
                user_detail = f"Dodo API error: {raw_text or r.status_code}"

            raise HTTPException(
                status_code=r.status_code,
                detail=user_detail,
            )

        data = r.json()
        checkout_url = data.get("checkout_url")
        print(f"[Dodo] Parsed Dodo response: checkout_url={checkout_url}")
        if not checkout_url:
            print(f"[Dodo] Missing checkout_url in response: {data}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Dodo did not return checkout_url. See DODO_INTEGRATION.md for response shape.",
            )
        return {"checkout_url": checkout_url}

    except httpx.TimeoutException as e:
        print(f"[Dodo] Timeout error when calling Dodo: {e}")
        raise HTTPException(status_code=504, detail="Dodo API timeout")
    except httpx.RequestError as e:
        print(f"[Dodo] Request error when calling Dodo: {e}")
        raise HTTPException(status_code=502, detail=f"Dodo request failed: {str(e)}")
    except HTTPException:
        # Re-raise HTTPExceptions we created above.
        raise
    except Exception as e:
        print(f"[Dodo] Unexpected error during checkout session creation: {type(e).__name__}: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")


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
            detail=_dodo_missing_config_message(),
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
            detail=_dodo_missing_config_message(),
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
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Dodo request failed: {str(e)}"
        )


@router.get("/test-auth")
async def test_dodo_auth():
    """Test if Dodo API key is valid against Dodo test API."""
    headers = {
        "Authorization": f"Bearer {DODO_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            # Simple read operation to verify bearer token validity
            r = await client.get(f"{DODO_BASE_URL}/products", headers=headers)
            return {
                "status": r.status_code,
                "api_key_length": len(DODO_API_KEY),
                "api_key_prefix": DODO_API_KEY[:10],
                "base_url": DODO_BASE_URL,
                "response": r.text[:200] if r.text else None,
            }
        except Exception as e:
            return {"error": str(e)}
