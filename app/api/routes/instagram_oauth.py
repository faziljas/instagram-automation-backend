"""
Facebook Login for Business OAuth Routes
Implements OAuth flow for connecting Instagram Business accounts via Facebook Pages
Supports both server-side redirect flow and Facebook SDK popup flow
"""
import os
import requests
from fastapi import APIRouter, Depends, HTTPException, status, Query, Header, Request, Body
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models.instagram_account import InstagramAccount
from app.utils.auth import verify_token
from app.utils.encryption import encrypt_credentials
from app.utils.plan_enforcement import check_account_limit

router = APIRouter()

# Instagram App Configuration
# Fallback to FACEBOOK_* variables if INSTAGRAM_* are not set (they're the same in Meta)
INSTAGRAM_APP_ID = os.getenv("INSTAGRAM_APP_ID", os.getenv("FACEBOOK_APP_ID", ""))
INSTAGRAM_APP_SECRET = os.getenv("INSTAGRAM_APP_SECRET", os.getenv("FACEBOOK_APP_SECRET", ""))
INSTAGRAM_REDIRECT_URI = os.getenv("INSTAGRAM_REDIRECT_URI", os.getenv("FACEBOOK_REDIRECT_URI", "https://instagram-automation-backend-23mp.onrender.com/api/instagram/oauth/callback"))
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")
FACEBOOK_API_VERSION = "v19.0"


class ConnectSDKRequest(BaseModel):
    access_token: str


class ExchangeCodeRequest(BaseModel):
    code: str


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
    # business_management is needed for Pages inside Meta Business Suite portfolio
    # pages_messaging is required to subscribe to 'messages' webhook field for DMs
    scopes = [
        "instagram_basic",
        "instagram_manage_comments",
        "instagram_manage_messages",
        "pages_show_list",
        "pages_read_engagement",
        "pages_manage_metadata",
        "business_management",
        "pages_messaging"
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


@router.get("/oauth/authorize-popup")
def get_instagram_auth_url_popup(user_id: int = Depends(get_current_user_id)):
    """
    Generate OAuth authorization URL for popup flow with config_id support.
    Used by frontend to open Instagram-branded login in popup window.
    """
    if not INSTAGRAM_APP_ID or not INSTAGRAM_APP_SECRET:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Instagram OAuth not configured"
        )
    
    # Get config_id from environment (for SuperProfile/Instagram Login flow)
    config_id = os.getenv("FACEBOOK_CONFIG_ID", "")
    
    # Build redirect URI for popup callback (will be handled by frontend)
    # For popup flow, we'll use a special callback that posts message back to parent
    popup_redirect_uri = f"{FRONTEND_URL}/dashboard/accounts/oauth-callback"
    
    # Build OAuth URL with config_id if available (shows Instagram branding)
    if config_id:
        # When using config_id, Instagram Login product shows Instagram branding
        oauth_url = (
            f"https://www.facebook.com/{FACEBOOK_API_VERSION}/dialog/oauth"
            f"?client_id={INSTAGRAM_APP_ID}"
            f"&redirect_uri={popup_redirect_uri}"
            f"&response_type=token"  # Use token for popup (no server-side callback needed)
            f"&config_id={config_id}"
            f"&state={user_id}"
        )
        print(f"🔗 Instagram OAuth URL with config_id: {config_id}")
    else:
        # Fallback: Use scope-based flow
        scopes = [
            "instagram_basic",
            "instagram_manage_comments",
            "instagram_manage_messages",
            "pages_show_list",
            "pages_read_engagement",
            "pages_manage_metadata",
            "business_management",
            "pages_messaging"
        ]
        oauth_url = (
            f"https://www.facebook.com/{FACEBOOK_API_VERSION}/dialog/oauth"
            f"?client_id={INSTAGRAM_APP_ID}"
            f"&redirect_uri={popup_redirect_uri}"
            f"&response_type=token"
            f"&scope={','.join(scopes)}"
            f"&state={user_id}"
        )
        print(f"⚠️ No config_id found, using scope-based flow")
    
    return {
        "authorization_url": oauth_url,
        "popup_window": True
    }


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
        # Add limit=100 to avoid pagination issues, and use type=page as fallback
        pages_url = f"https://graph.facebook.com/{FACEBOOK_API_VERSION}/me/accounts"
        pages_params = {
            "fields": "id,name,access_token,instagram_business_account{id,username}",
            "limit": "100",
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
        
        # Fallback: Try fetching with type=page if no pages found
        if not pages:
            print("⚠️ No pages found with me/accounts, trying fallback with type=page...")
            fallback_pages_params = {
                "fields": "id,name,access_token,instagram_business_account{id,username}",
                "type": "page",
                "limit": "100",
                "access_token": user_access_token
            }
            fallback_response = requests.get(pages_url, params=fallback_pages_params)
            
            if fallback_response.status_code == 200:
                fallback_data = fallback_response.json()
                pages = fallback_data.get("data", [])
                if pages:
                    print(f"✅ Found {len(pages)} pages using fallback method")
                else:
                    print("❌ No Facebook Pages found even with fallback method")
            else:
                print(f"⚠️ Fallback request failed: {fallback_response.text}")
        
        if not pages:
            print("❌ No Facebook Pages found")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No Facebook Pages found. Please create a Facebook Page and connect it to an Instagram Business account."
            )
        
        # Step 2.5: Check account limit BEFORE connecting
        try:
            check_account_limit(user_id, db)
        except HTTPException as e:
            print(f"❌ Account limit check failed: {e.detail}")
            return RedirectResponse(
                url=f"{FRONTEND_URL}/dashboard/accounts?error=account_limit_reached&message={e.detail}"
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
        
        # Step 4: Subscribe page to webhooks (feed, mention, messages)
        # Note: 'feed' handles posts and comments, 'mention' handles mentions, 'messages' handles DMs
        try:
            webhook_subscribe_url = f"https://graph.facebook.com/{FACEBOOK_API_VERSION}/{page_with_instagram['page_id']}/subscribed_apps"
            webhook_params = {
                "subscribed_fields": "feed,mention,messages",
                "access_token": page_with_instagram['page_token']
            }
            
            print(f"🔄 Subscribing page to webhooks (feed, mention, messages)...")
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


def exchange_token_for_long_lived(short_lived_token: str) -> dict:
    """
    Exchange short-lived User Access Token for long-lived token (60 days).
    Returns dict with access_token and expires_in.
    """
    exchange_url = f"https://graph.facebook.com/{FACEBOOK_API_VERSION}/oauth/access_token"
    exchange_params = {
        "grant_type": "fb_exchange_token",
        "client_id": INSTAGRAM_APP_ID,
        "client_secret": INSTAGRAM_APP_SECRET,
        "fb_exchange_token": short_lived_token
    }
    
    print(f"🔄 Exchanging short-lived token for long-lived token...")
    response = requests.get(exchange_url, params=exchange_params)
    
    if response.status_code != 200:
        error_detail = response.text
        print(f"❌ Token exchange failed: {error_detail}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to exchange token: {error_detail}"
        )
    
    token_data = response.json()
    access_token = token_data.get("access_token")
    expires_in = token_data.get("expires_in", 0)
    
    print(f"✅ Token exchanged successfully! Expires in: {expires_in} seconds (~{expires_in // 86400} days)")
    return {"access_token": access_token, "expires_in": expires_in}


def fetch_pages_with_instagram(user_access_token: str) -> list:
    """
    Fetch user's Facebook Pages with connected Instagram Business Accounts.
    Returns list of pages with Instagram account data.
    """
    pages_url = f"https://graph.facebook.com/{FACEBOOK_API_VERSION}/me/accounts"
    pages_params = {
        "fields": "id,name,access_token,instagram_business_account{id,username}",
        "limit": "100",
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
    
    # Fallback: Try fetching with type=page if no pages found
    if not pages:
        print("⚠️ No pages found with me/accounts, trying fallback with type=page...")
        fallback_pages_params = {
            "fields": "id,name,access_token,instagram_business_account{id,username}",
            "type": "page",
            "limit": "100",
            "access_token": user_access_token
        }
        fallback_response = requests.get(pages_url, params=fallback_pages_params)
        
        if fallback_response.status_code == 200:
            fallback_data = fallback_response.json()
            pages = fallback_data.get("data", [])
            if pages:
                print(f"✅ Found {len(pages)} pages using fallback method")
            else:
                print("❌ No Facebook Pages found even with fallback method")
        else:
            print(f"⚠️ Fallback request failed: {fallback_response.text}")
    
    if not pages:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No Facebook Pages found. Please create a Facebook Page and connect it to an Instagram Business account."
        )
    
    return pages


def find_instagram_account_from_pages(pages: list) -> dict:
    """
    Find first page with connected Instagram Business Account.
    Returns dict with page_id, page_name, page_token, instagram_id, instagram_username.
    """
    for page in pages:
        instagram_account = page.get("instagram_business_account")
        if instagram_account:
            return {
                "page_id": page.get("id"),
                "page_name": page.get("name"),
                "page_token": page.get("access_token"),
                "instagram_id": instagram_account.get("id"),
                "instagram_username": instagram_account.get("username")
            }
    
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="No Facebook Page with connected Instagram Business Account found. Please connect an Instagram Business account to your Facebook Page."
    )


def save_or_update_instagram_account(
    user_id: int,
    page_with_instagram: dict,
    db: Session
) -> InstagramAccount:
    """
    Save or update Instagram account in database.
    Returns the saved/updated InstagramAccount instance.
    """
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
        return existing_account
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
        return new_account


@router.post("/connect-sdk")
async def connect_instagram_sdk(
    request_data: ConnectSDKRequest = Body(...),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    Handle Instagram account connection via Facebook SDK popup.
    Receives short-lived access token from SDK, exchanges for long-lived token,
    fetches pages, finds Instagram account, and saves to database.
    """
    try:
        short_lived_token = request_data.access_token
        print(f"📥 SDK connection request received for user {user_id}")
        
        # Step 1: Exchange short-lived token for long-lived token
        long_lived_data = exchange_token_for_long_lived(short_lived_token)
        long_lived_token = long_lived_data["access_token"]
        
        # Step 2: Check account limit BEFORE connecting
        try:
            check_account_limit(user_id, db)
        except HTTPException as e:
            print(f"❌ Account limit check failed: {e.detail}")
            raise
        
        # Step 3: Fetch user's Facebook Pages with Instagram Business Accounts
        pages = fetch_pages_with_instagram(long_lived_token)
        
        # Step 4: Find first page with Instagram Business Account
        page_with_instagram = find_instagram_account_from_pages(pages)
        
        print(f"✅ Found Instagram Business Account:")
        print(f"   Page ID: {page_with_instagram['page_id']}")
        print(f"   Page Name: {page_with_instagram['page_name']}")
        print(f"   Instagram ID: {page_with_instagram['instagram_id']}")
        print(f"   Instagram Username: {page_with_instagram['instagram_username']}")
        
        # Step 5: Subscribe page to webhooks (feed, mention, messages)
        try:
            webhook_subscribe_url = f"https://graph.facebook.com/{FACEBOOK_API_VERSION}/{page_with_instagram['page_id']}/subscribed_apps"
            webhook_params = {
                "subscribed_fields": "feed,mention,messages",
                "access_token": page_with_instagram['page_token']
            }
            
            print(f"🔄 Subscribing page to webhooks (feed, mention, messages)...")
            webhook_response = requests.post(webhook_subscribe_url, params=webhook_params)
            
            if webhook_response.status_code == 200:
                print(f"✅ Webhook subscription successful")
            else:
                print(f"⚠️ Webhook subscription warning: {webhook_response.text}")
        except Exception as e:
            print(f"⚠️ Webhook subscription error (non-critical): {str(e)}")
        
        # Step 6: Save or update Instagram account
        account = save_or_update_instagram_account(user_id, page_with_instagram, db)
        
        print(f"✅ Account saved successfully!")
        
        return {
            "success": True,
            "account": {
                "id": account.id,
                "username": account.username,
                "is_active": account.is_active,
                "created_at": account.created_at.isoformat() if account.created_at else None
            },
            "message": "Instagram account connected successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ SDK connection error: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to connect Instagram account: {str(e)}"
        )


@router.post("/exchange-code")
async def exchange_instagram_code(
    request_data: ExchangeCodeRequest = Body(...),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    Exchange Facebook OAuth authorization code for Instagram Business account.
    Handles Instagram Business Login via Facebook OAuth flow with code exchange.
    """
    try:
        code = request_data.code
        print(f"📥 Facebook OAuth code exchange request received for user {user_id}")
        
        if not code:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Authorization code is required"
            )
        
        # Build redirect URI (must match frontend callback URL)
        redirect_uri = f"{FRONTEND_URL}/dashboard/callback"
        
        # Step 1: Exchange code for User Access Token (Facebook OAuth)
        token_url = f"https://graph.facebook.com/{FACEBOOK_API_VERSION}/oauth/access_token"
        token_params = {
            "client_id": INSTAGRAM_APP_ID,
            "client_secret": INSTAGRAM_APP_SECRET,
            "redirect_uri": redirect_uri,
            "code": code
        }
        
        print(f"🔄 Step 1: Exchanging code for User Access Token...")
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
        
        if not user_access_token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No access token received from Facebook"
            )
        
        print(f"✅ Step 1 complete: Got User Access Token")
        
        # Step 2: Fetch user's Facebook Pages with Instagram Business Accounts
        pages_url = f"https://graph.facebook.com/{FACEBOOK_API_VERSION}/me/accounts"
        pages_params = {
            "fields": "id,name,access_token,instagram_business_account",
            "limit": 100,
            "access_token": user_access_token
        }
        
        print(f"🔄 Step 2: Fetching Facebook Pages with Instagram accounts...")
        pages_response = requests.get(pages_url, params=pages_params)
        
        if pages_response.status_code != 200:
            error_detail = pages_response.text
            print(f"❌ Failed to fetch pages: {error_detail}")
            # Try fallback query
            pages_url_fallback = f"https://graph.facebook.com/{FACEBOOK_API_VERSION}/me/accounts?type=page"
            pages_response = requests.get(pages_url_fallback, params={"access_token": user_access_token, "limit": 100})
            if pages_response.status_code != 200:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Failed to fetch Facebook Pages: {error_detail}"
                )
        
        pages_data = pages_response.json()
        pages = pages_data.get("data", [])
        
        if not pages:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No Facebook Pages found. Please ensure you have a Facebook Page connected to your Instagram Business account."
            )
        
        # Step 3: Find first page with Instagram Business Account
        page_with_instagram = None
        for page in pages:
            ig_account = page.get("instagram_business_account")
            if ig_account:
                page_with_instagram = {
                    "page_id": page["id"],
                    "page_name": page.get("name", ""),
                    "page_access_token": page.get("access_token", ""),
                    "instagram_id": ig_account.get("id") if isinstance(ig_account, dict) else ig_account
                }
                break
        
        if not page_with_instagram or not page_with_instagram.get("instagram_id"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No Instagram Business Account found. Please ensure your Facebook Page has an Instagram Business account connected."
            )
        
        page_access_token = page_with_instagram["page_access_token"]
        instagram_id = str(page_with_instagram["instagram_id"])
        
        # Step 4: Get Instagram account details
        instagram_info_url = f"https://graph.facebook.com/{FACEBOOK_API_VERSION}/{instagram_id}"
        instagram_info_params = {
            "fields": "id,username,account_type",
            "access_token": page_access_token
        }
        
        print(f"🔄 Step 3: Fetching Instagram account info...")
        instagram_info_response = requests.get(instagram_info_url, params=instagram_info_params)
        
        instagram_username = None
        account_type = None
        if instagram_info_response.status_code == 200:
            instagram_info = instagram_info_response.json()
            instagram_username = instagram_info.get("username")
            account_type = instagram_info.get("account_type")
            print(f"✅ Instagram account info: username={instagram_username}, type={account_type}")
        else:
            print(f"⚠️ Could not fetch Instagram details, using page info")
            instagram_username = page_with_instagram.get("page_name")
        
        # Step 5: Check account limit BEFORE connecting
        try:
            check_account_limit(user_id, db)
        except HTTPException as e:
            print(f"❌ Account limit check failed: {e.detail}")
            raise
        
        # Step 6: Save or update Instagram account
        existing_account = db.query(InstagramAccount).filter(
            InstagramAccount.user_id == user_id,
            InstagramAccount.igsid == instagram_id
        ).first()
        
        if existing_account:
            print(f"📝 Updating existing account: {instagram_username or instagram_id}")
            if instagram_username:
                existing_account.username = instagram_username
            existing_account.igsid = instagram_id
            existing_account.page_id = page_with_instagram["page_id"]
            existing_account.encrypted_page_token = encrypt_credentials(page_access_token)
            db.commit()
            account_id = existing_account.id
        else:
            username = instagram_username or f"instagram_{instagram_id}"
            print(f"✨ Creating new account: {username}")
            new_account = InstagramAccount(
                user_id=user_id,
                username=username,
                encrypted_credentials="",  # Legacy field
                encrypted_page_token=encrypt_credentials(page_access_token),
                page_id=page_with_instagram["page_id"],
                igsid=instagram_id
            )
            db.add(new_account)
            db.commit()
            db.refresh(new_account)
            account_id = new_account.id
        
        print(f"✅ Account saved successfully! Account ID: {account_id}")
        
        return {
            "success": True,
            "account": {
                "id": account_id,
                "username": instagram_username or f"instagram_{instagram_id}",
                "igsid": instagram_id,
                "is_active": True
            },
            "message": "Instagram account connected successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Facebook OAuth code exchange error: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to exchange OAuth code: {str(e)}"
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
