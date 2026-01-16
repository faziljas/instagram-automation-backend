"""
Instagram Login for Business OAuth Routes
Implements OAuth flow for connecting Instagram Business/Creator accounts
"""
import os
import requests
from fastapi import APIRouter, Depends, HTTPException, status, Query, Header, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models.instagram_account import InstagramAccount
from app.utils.auth import verify_token
from app.utils.encryption import encrypt_credentials

router = APIRouter()

# Instagram App Configuration
# Fallback to FACEBOOK_* variables if INSTAGRAM_* are not set (they're the same in Meta)
INSTAGRAM_APP_ID = os.getenv("INSTAGRAM_APP_ID", os.getenv("FACEBOOK_APP_ID", ""))
INSTAGRAM_APP_SECRET = os.getenv("INSTAGRAM_APP_SECRET", os.getenv("FACEBOOK_APP_SECRET", ""))
INSTAGRAM_REDIRECT_URI = os.getenv("INSTAGRAM_REDIRECT_URI", os.getenv("FACEBOOK_REDIRECT_URI", "https://instagram-automation-backend-23mp.onrender.com/api/instagram/oauth/callback"))
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")
INSTAGRAM_API_VERSION = "v19.0"


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


@router.get("/oauth/authorize")
def get_instagram_auth_url(user_id: int = Depends(get_current_user_id)):
    """
    Generate Instagram Login for Business OAuth authorization URL.
    Frontend redirects user to this URL to start OAuth flow.
    """
    if not INSTAGRAM_APP_ID or not INSTAGRAM_APP_SECRET:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Instagram OAuth not configured"
        )
    
    # Required scopes for Instagram Business API
    scopes = [
        "instagram_business_basic",
        "instagram_manage_messages",
        "instagram_manage_comments",
        "instagram_content_publish"
    ]
    
    redirect_uri = INSTAGRAM_REDIRECT_URI.strip()
    
    # Build Instagram OAuth URL
    oauth_url = (
        f"https://www.instagram.com/oauth/authorize"
        f"?client_id={INSTAGRAM_APP_ID}"
        f"&redirect_uri={redirect_uri}"
        f"&scope={','.join(scopes)}"
        f"&response_type=code"
        f"&state={user_id}"  # Pass user_id to identify user after callback
    )
    
    print(f"🔗 Instagram OAuth authorize URL - redirect_uri: '{redirect_uri}'")
    print(f"🔗 Full OAuth URL: {oauth_url}")
    
    return {"authorization_url": oauth_url}


@router.get("/oauth/callback")
async def instagram_oauth_callback(
    code: str = Query(...),
    state: str = Query(None),  # This is the user_id
    db: Session = Depends(get_db),
    request: Request = None
):
    """
    Handle Instagram OAuth callback.
    Exchange authorization code for User Access Token, validate account type, save account.
    """
    try:
        user_id = int(state) if state else None
        
        print(f"📥 OAuth callback received: code={code[:20]}..., user_id={user_id}")
        
        if not user_id:
            print("⚠️ No user_id in state, cannot save to database")
            return RedirectResponse(
                url=f"{FRONTEND_URL}/dashboard/accounts?error=no_user_id"
            )
        
        # Step 1: Exchange code for User Access Token (Short-lived)
        token_url = "https://api.instagram.com/oauth/access_token"
        token_data = {
            "client_id": INSTAGRAM_APP_ID,
            "client_secret": INSTAGRAM_APP_SECRET,
            "grant_type": "authorization_code",
            "redirect_uri": INSTAGRAM_REDIRECT_URI.strip(),
            "code": code
        }
        
        print(f"🔄 Exchanging code for User Access Token...")
        token_response = requests.post(token_url, data=token_data)
        
        if token_response.status_code != 200:
            error_detail = token_response.text
            print(f"❌ Token exchange failed: {error_detail}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to exchange code for token: {error_detail}"
            )
        
        token_response_data = token_response.json()
        user_access_token = token_response_data.get("access_token")
        user_id_from_token = token_response_data.get("user_id")
        
        if not user_access_token or not user_id_from_token:
            print(f"❌ Invalid token response: {token_response_data}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid token response from Instagram"
            )
        
        print(f"✅ Got User Access Token for user_id: {user_id_from_token}")
        
        # Step 2: Validate Account Type
        account_info_url = f"https://graph.instagram.com/{INSTAGRAM_API_VERSION}/{user_id_from_token}"
        account_params = {
            "fields": "account_type,id,username",
            "access_token": user_access_token
        }
        
        print(f"🔄 Fetching account information to validate account type...")
        account_response = requests.get(account_info_url, params=account_params)
        
        if account_response.status_code != 200:
            error_detail = account_response.text
            print(f"❌ Failed to fetch account info: {error_detail}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to fetch account information: {error_detail}"
            )
        
        account_data = account_response.json()
        account_type = account_data.get("account_type")
        instagram_id = account_data.get("id")
        instagram_username = account_data.get("username")
        
        print(f"📋 Account Type: {account_type}, Username: {instagram_username}")
        
        # Step 3: Validate Account Type (Reject Personal Accounts)
        if account_type == "PERSONAL":
            error_message = "Automation features are not available for Personal accounts. Please switch to a Professional (Creator/Business) account in your Instagram App settings."
            print(f"❌ Personal account rejected: {instagram_username}")
            return RedirectResponse(
                url=f"{FRONTEND_URL}/dashboard/accounts/connect?error=personal_account"
            )
        
        if account_type not in ["BUSINESS", "CREATOR"]:
            error_message = f"Unsupported account type: {account_type}. Please use a Business or Creator account."
            print(f"❌ Unsupported account type: {account_type}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_message
            )
        
        print(f"✅ Account type validated: {account_type}")
        
        # Step 4: Save or update Instagram account
        existing_account = db.query(InstagramAccount).filter(
            InstagramAccount.user_id == user_id,
            InstagramAccount.igsid == instagram_id
        ).first()
        
        if existing_account:
            # Update existing account
            print(f"📝 Updating existing account: {instagram_username}")
            existing_account.username = instagram_username
            existing_account.igsid = instagram_id
            # Store user token in encrypted_page_token field
            existing_account.encrypted_page_token = encrypt_credentials(user_access_token)
            db.commit()
            account_id = existing_account.id
        else:
            # Create new account
            print(f"✨ Creating new account: {instagram_username}")
            new_account = InstagramAccount(
                user_id=user_id,
                username=instagram_username,
                encrypted_credentials="",  # Legacy field, kept empty
                encrypted_page_token=encrypt_credentials(user_access_token),  # Store user token
                page_id=None,  # Not needed for Instagram Login flow
                igsid=instagram_id
            )
            db.add(new_account)
            db.commit()
            db.refresh(new_account)
            account_id = new_account.id
        
        print(f"✅ Account saved successfully! Redirecting to dashboard...")
        
        # Redirect to frontend success page
        return RedirectResponse(
            url=f"{FRONTEND_URL}/dashboard/accounts?success=true&account_id={account_id}"
        )
        
    except ValueError as e:
        print(f"❌ ValueError: {str(e)}")
        return RedirectResponse(
            url=f"{FRONTEND_URL}/dashboard/accounts?error=invalid_state"
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ OAuth callback error: {str(e)}")
        import traceback
        traceback.print_exc()
        
        # Redirect to frontend error page
        return RedirectResponse(
            url=f"{FRONTEND_URL}/dashboard/accounts?error=oauth_failed"
        )


@router.post("/oauth/refresh")
async def refresh_instagram_token(
    account_id: int,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    Refresh Instagram Access Token (long-lived tokens last 60 days).
    Note: This endpoint may need to be updated based on Instagram's token refresh requirements.
    """
    account = db.query(InstagramAccount).filter(
        InstagramAccount.id == account_id,
        InstagramAccount.user_id == user_id
    ).first()
    
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Instagram account not found"
        )
    
    if not account.encrypted_page_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Account does not have Instagram credentials"
        )
    
    # Exchange short-lived token for long-lived token
    from app.utils.encryption import decrypt_credentials
    current_token = decrypt_credentials(account.encrypted_page_token)
    
    # For Instagram Login, use Graph API to exchange token
    exchange_url = f"https://graph.instagram.com/{INSTAGRAM_API_VERSION}/access_token"
    exchange_params = {
        "grant_type": "ig_exchange_token",
        "client_secret": INSTAGRAM_APP_SECRET,
        "access_token": current_token
    }
    
    response = requests.get(exchange_url, params=exchange_params)
    
    if response.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to exchange token: {response.text}"
        )
    
    data = response.json()
    new_token = data.get("access_token")
    expires_in = data.get("expires_in", 0)  # Long-lived tokens typically last 5184000 seconds (60 days)
    
    print(f"✅ Token exchanged! Expires in: {expires_in} seconds (~{expires_in // 86400} days)")
    
    # Update token in database
    account.encrypted_page_token = encrypt_credentials(new_token)
    db.commit()
    
    return {
        "message": "Token exchanged for long-lived token successfully",
        "expires_in": expires_in
    }
