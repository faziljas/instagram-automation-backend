"""
Facebook Login for Business OAuth Routes
Implements OAuth flow for connecting Instagram Business accounts via Facebook Pages
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
FACEBOOK_API_VERSION = "v19.0"


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
    Generate Facebook Login OAuth authorization URL.
    Frontend redirects user to this URL to start OAuth flow.
    """
    if not INSTAGRAM_APP_ID or not INSTAGRAM_APP_SECRET:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Instagram OAuth not configured"
        )
    
    # Required scopes for Instagram Business API with Auto DM on Comment
    scopes = [
        "instagram_basic",
        "instagram_manage_comments",
        "instagram_manage_messages",
        "pages_show_list",
        "pages_read_engagement"
    ]
    
    redirect_uri = INSTAGRAM_REDIRECT_URI.strip()
    
    # Build Facebook OAuth URL
    oauth_url = (
        f"https://www.facebook.com/{FACEBOOK_API_VERSION}/dialog/oauth"
        f"?client_id={INSTAGRAM_APP_ID}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&scope={','.join(scopes)}"
        f"&state={user_id}"  # Pass user_id to identify user after callback
    )
    
    print(f"🔗 Facebook OAuth authorize URL - redirect_uri: '{redirect_uri}'")
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
    Handle Facebook OAuth callback.
    Exchange authorization code for User Access Token, fetch Pages, find Instagram Business Account.
    """
    try:
        user_id = int(state) if state else None
        
        print(f"📥 OAuth callback received: code={code[:20]}..., user_id={user_id}")
        
        if not user_id:
            print("⚠️ No user_id in state, cannot save to database")
            return RedirectResponse(
                url=f"{FRONTEND_URL}/dashboard/accounts?error=no_user_id"
            )
        
        # Step 1: Exchange code for User Access Token
        token_url = f"https://graph.facebook.com/{FACEBOOK_API_VERSION}/oauth/access_token"
        token_params = {
            "client_id": INSTAGRAM_APP_ID,
            "client_secret": INSTAGRAM_APP_SECRET,
            "redirect_uri": INSTAGRAM_REDIRECT_URI.strip(),
            "code": code
        }
        
        print(f"🔄 Exchanging code for User Access Token...")
        token_response = requests.get(token_url, params=token_params)
        
        if token_response.status_code != 200:
            error_detail = token_response.text
            print(f"❌ Token exchange failed: {error_detail}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to exchange code for token: {error_detail}"
            )
        
        token_data = token_response.json()
        user_access_token = token_data.get("access_token")
        print(f"✅ Got User Access Token")
        
        # Step 2: Fetch user's Facebook Pages with Instagram Business Accounts
        pages_url = f"https://graph.facebook.com/{FACEBOOK_API_VERSION}/me/accounts"
        pages_params = {
            "fields": "id,name,access_token,instagram_business_account{id,username}",
            "access_token": user_access_token
        }
        
        print(f"🔄 Fetching Facebook Pages with Instagram Business Accounts...")
        pages_response = requests.get(pages_url, params=pages_params)
        
        if pages_response.status_code != 200:
            error_detail = pages_response.text
            print(f"❌ Failed to fetch pages: {error_detail}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to fetch pages: {error_detail}"
            )
        
        pages_data = pages_response.json()
        pages = pages_data.get("data", [])
        
        if not pages:
            print("❌ No Facebook Pages found")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No Facebook Pages found. Please create a Facebook Page and connect it to an Instagram Business account."
            )
        
        # Step 3: Find first page with Instagram Business Account
        page_with_instagram = None
        for page in pages:
            instagram_account = page.get("instagram_business_account")
            if instagram_account:
                page_with_instagram = {
                    "page_id": page.get("id"),
                    "page_name": page.get("name"),
                    "page_token": page.get("access_token"),
                    "instagram_id": instagram_account.get("id"),
                    "instagram_username": instagram_account.get("username")
                }
                break
        
        if not page_with_instagram:
            print("❌ No Facebook Page with connected Instagram Business Account found")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No Facebook Page with connected Instagram Business Account found. Please connect an Instagram Business account to your Facebook Page."
            )
        
        print(f"✅ Found Instagram Business Account:")
        print(f"   Page ID: {page_with_instagram['page_id']}")
        print(f"   Page Name: {page_with_instagram['page_name']}")
        print(f"   Instagram ID: {page_with_instagram['instagram_id']}")
        print(f"   Instagram Username: {page_with_instagram['instagram_username']}")
        
        # Step 4: Subscribe page to webhooks (feed, comments, mentions)
        try:
            webhook_subscribe_url = f"https://graph.facebook.com/{FACEBOOK_API_VERSION}/{page_with_instagram['page_id']}/subscribed_apps"
            webhook_params = {
                "subscribed_fields": "feed,comments,mentions",
                "access_token": page_with_instagram['page_token']
            }
            
            print(f"🔄 Subscribing page to webhooks (feed, comments, mentions)...")
            webhook_response = requests.post(webhook_subscribe_url, params=webhook_params)
            
            if webhook_response.status_code == 200:
                print(f"✅ Webhook subscription successful")
            else:
                print(f"⚠️ Webhook subscription warning: {webhook_response.text}")
        except Exception as e:
            print(f"⚠️ Webhook subscription error (non-critical): {str(e)}")
        
        # Step 5: Save or update Instagram account
        existing_account = db.query(InstagramAccount).filter(
            InstagramAccount.user_id == user_id,
            InstagramAccount.igsid == page_with_instagram['instagram_id']
        ).first()
        
        if existing_account:
            # Update existing account
            print(f"📝 Updating existing account: {page_with_instagram['instagram_username']}")
            existing_account.username = page_with_instagram['instagram_username']
            existing_account.igsid = page_with_instagram['instagram_id']
            existing_account.page_id = page_with_instagram['page_id']
            existing_account.encrypted_page_token = encrypt_credentials(page_with_instagram['page_token'])
            db.commit()
            account_id = existing_account.id
        else:
            # Create new account
            print(f"✨ Creating new account: {page_with_instagram['instagram_username']}")
            new_account = InstagramAccount(
                user_id=user_id,
                username=page_with_instagram['instagram_username'],
                encrypted_credentials="",  # Legacy field, kept empty
                encrypted_page_token=encrypt_credentials(page_with_instagram['page_token']),
                page_id=page_with_instagram['page_id'],
                igsid=page_with_instagram['instagram_id']
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
    Refresh Facebook Page Access Token (long-lived tokens last 60 days).
    Note: This endpoint may need to be updated based on Facebook's token refresh requirements.
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
    
    if not account.page_id or not account.encrypted_page_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Account does not have Facebook Page credentials"
        )
    
    # Exchange short-lived token for long-lived token
    from app.utils.encryption import decrypt_credentials
    current_token = decrypt_credentials(account.encrypted_page_token)
    
    exchange_url = f"https://graph.facebook.com/{FACEBOOK_API_VERSION}/oauth/access_token"
    exchange_params = {
        "grant_type": "fb_exchange_token",
        "client_id": INSTAGRAM_APP_ID,
        "client_secret": INSTAGRAM_APP_SECRET,
        "fb_exchange_token": current_token
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
