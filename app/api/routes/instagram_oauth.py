"""
Instagram OAuth Authentication Routes
Implements secure OAuth flow for connecting Instagram accounts
"""
import os
import requests
from urllib.parse import quote, urlparse
from fastapi import APIRouter, Depends, HTTPException, status, Query, Header, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models.instagram_account import InstagramAccount
from app.utils.auth import verify_token
from app.utils.encryption import encrypt_credentials

router = APIRouter()

# Instagram OAuth Configuration
# IMPORTANT: Use Instagram App ID (1236315365125564), NOT Facebook App ID (1312634277295614)
# The Instagram App ID is shown in Meta Console → Instagram → API setup → App Credentials
INSTAGRAM_APP_ID = os.getenv("INSTAGRAM_APP_ID", "1236315365125564")  # Instagram App ID (correct)
INSTAGRAM_APP_SECRET = os.getenv("INSTAGRAM_APP_SECRET", "ebb1f998812da792755")  # Instagram App Secret
# IMPORTANT: Trailing slash is required - Meta Console automatically adds it to saved URIs
# The redirect_uri must match EXACTLY (including trailing slash) what's registered in Meta Console
INSTAGRAM_REDIRECT_URI = os.getenv("INSTAGRAM_REDIRECT_URI", "https://instagram-automation-backend-23mp.onrender.com/api/instagram/oauth/callback/")
# Frontend URL for OAuth redirects after successful authentication
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


@router.get("/oauth/authorize")
def get_instagram_auth_url(user_id: int = Depends(get_current_user_id)):
    """
    Generate Instagram Business OAuth authorization URL.
    Frontend redirects user to this URL to start OAuth flow.
    """
    if not INSTAGRAM_APP_ID:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Instagram OAuth not configured"
        )
    
    # Build Instagram Business OAuth URL with all required permissions for DM automation
    scopes = [
        "instagram_business_basic",
        "instagram_business_manage_messages",
        "instagram_business_manage_comments",
        "instagram_business_content_publish",
        "instagram_business_manage_insights"
    ]
    
    # Build OAuth URL - Instagram requires redirect_uri to match EXACTLY
    # CRITICAL: redirect_uri must match EXACTLY in:
    # 1. Meta Developer Console → Valid OAuth Redirect URIs
    # 2. Authorize request (this URL)
    # 3. Token exchange request
    redirect_uri_for_auth = INSTAGRAM_REDIRECT_URI.strip()  # Remove any whitespace
    
    # Build URL with raw redirect_uri - browser will URL-encode it automatically
    # Instagram compares the RAW redirect_uri values (after decoding)
    # So we send raw in both authorize and token exchange
    oauth_url = (
        f"https://www.instagram.com/oauth/authorize"
        f"?force_reauth=true"
        f"&client_id={INSTAGRAM_APP_ID}"
        f"&redirect_uri={redirect_uri_for_auth}"  # Raw value - browser encodes it
        f"&response_type=code"
        f"&scope={','.join(scopes)}"
        f"&state={user_id}"  # Pass user_id to identify user after callback
    )
    
    print(f"🔗 OAuth authorize URL - redirect_uri: '{redirect_uri_for_auth}'")
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
    Handle Instagram Business OAuth callback.
    Exchange authorization code for access token and save account.
    """
    try:
        user_id = int(state) if state else None
        
        # Get the actual callback URL that Instagram redirected to
        callback_url = str(request.url) if request else "unknown"
        # Extract base redirect_uri from callback URL (without query params)
        parsed_url = urlparse(callback_url)
        actual_redirect_uri = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
        
        print(f"📥 OAuth callback received: code={code[:20]}..., user_id={user_id}")
        print(f"📥 Callback URL from Instagram: {callback_url}")
        print(f"📥 Extracted redirect_uri from callback: {actual_redirect_uri}")
        print(f"🔗 Configured redirect_uri: {INSTAGRAM_REDIRECT_URI}")
        print(f"🔗 Actual redirect_uri used by Instagram: {actual_redirect_uri}")
        
        # Exchange code for short-lived access token
        # CRITICAL: Use the exact redirect_uri that Instagram actually used, not our configured one
        # Instagram normalizes URLs by removing trailing slashes, so they may differ
        # We must use the actual redirect_uri from the callback URL to match what Instagram expects
        token_url = "https://api.instagram.com/oauth/access_token"
        
        # CRITICAL: Use the actual redirect_uri that Instagram used, not our configured one
        # Instagram normalizes URLs by removing trailing slashes, so they may differ
        redirect_uri_for_exchange = actual_redirect_uri
        
        # Build token exchange request
        # Note: requests.post(data=...) sends as application/x-www-form-urlencoded
        # This will URL-encode the values, but Instagram compares raw values
        token_data = {
            "client_id": INSTAGRAM_APP_ID,
            "client_secret": INSTAGRAM_APP_SECRET,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri_for_exchange,  # Use the ACTUAL redirect_uri Instagram used
            "code": code
        }
        
        print(f"🔄 Exchanging code for token...")
        print(f"   client_id: {INSTAGRAM_APP_ID}")
        print(f"   redirect_uri: '{redirect_uri_for_exchange}'")
        print(f"🔗 Using actual redirect_uri for token exchange")
        print(f"   Full request data: {token_data}")
        
        # Send POST request - requests will form-encode the data
        # IMPORTANT: Instagram compares redirect_uri byte-by-byte
        # Make sure it's exactly the same as in authorize URL
        import urllib.parse
        print(f"   🔍 Debug: redirect_uri URL-encoded would be: {urllib.parse.quote(redirect_uri_for_exchange, safe='')}")
        print(f"   🔍 Debug: redirect_uri should match authorize URL exactly")
        
        token_response = requests.post(token_url, data=token_data)
        
        # Debug: Print what was actually sent
        print(f"   Request URL: {token_url}")
        print(f"   Response status: {token_response.status_code}")
        if token_response.status_code != 200:
            error_detail = token_response.text
            print(f"   Response body: {error_detail}")
            print(f"❌ Token exchange failed: {error_detail}")
            
            # Check if it's a redirect_uri mismatch
            if "redirect_uri" in error_detail.lower():
                print(f"\n🔍 REDIRECT_URI MISMATCH DETECTED!")
                print(f"   Instagram App ID: {INSTAGRAM_APP_ID}")
                print(f"   redirect_uri used in authorize: {INSTAGRAM_REDIRECT_URI}")
                print(f"   redirect_uri used in token exchange: {redirect_uri_for_exchange}")
                print(f"\n   ⚠️ CRITICAL: redirect_uri is NOT registered in Meta Console!")
                print(f"   → Go to: Meta Developer Console → Your App → Settings → Basic")
                print(f"   → Find: 'Valid OAuth Redirect URIs' section")
                print(f"   → Add EXACTLY (copy-paste this):")
                print(f"   → {INSTAGRAM_REDIRECT_URI}")
                print(f"   → Click 'Save Changes'")
                print(f"   → Wait 2-3 minutes for Meta to process")
                print(f"   → Then try OAuth again")
                print(f"\n   📋 Also verify:")
                print(f"   → Instagram App ID in code matches Meta Console: {INSTAGRAM_APP_ID}")
                print(f"   → No trailing slash, no extra spaces")
                print(f"   → Protocol is https:// (not http://)")
            
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to exchange code for token: {error_detail}"
            )
        
        token_result = token_response.json()
        access_token = token_result.get("access_token")
        instagram_user_id = token_result.get("user_id")
        
        print(f"✅ Got access token for user: {instagram_user_id}")
        
        # Get Instagram Business Account ID and username using Graph API
        # First, get the Facebook Page connected to this Instagram account
        me_url = "https://graph.facebook.com/v18.0/me"
        me_params = {
            "fields": "id,name",
            "access_token": access_token
        }
        
        me_response = requests.get(me_url, params=me_params)
        if me_response.status_code != 200:
            print(f"⚠️ Failed to get user info: {me_response.text}")
        
        # Get Instagram Business Account from Facebook Page
        accounts_url = f"https://graph.facebook.com/v18.0/{instagram_user_id}/accounts"
        accounts_params = {
            "access_token": access_token
        }
        
        accounts_response = requests.get(accounts_url, params=accounts_params)
        
        # Try to get Instagram Business Account info
        ig_account_id = instagram_user_id
        username = f"ig_user_{instagram_user_id[:8]}"  # Default username
        
        # Try to get actual Instagram username via Graph API
        try:
            ig_user_url = f"https://graph.facebook.com/v18.0/{instagram_user_id}"
            ig_user_params = {
                "fields": "username,id,name",
                "access_token": access_token
            }
            ig_user_response = requests.get(ig_user_url, params=ig_user_params)
            
            if ig_user_response.status_code == 200:
                ig_user_data = ig_user_response.json()
                username = ig_user_data.get("username", username)
                ig_account_id = ig_user_data.get("id", instagram_user_id)
                print(f"✅ Got Instagram username: {username}, ID: {ig_account_id}")
        except Exception as e:
            print(f"⚠️ Could not get Instagram username: {str(e)}")
        
        # If we don't have a user_id from state, we can't save to database
        if not user_id:
            print("⚠️ No user_id in state, cannot save to database")
            return RedirectResponse(
                url=f"{FRONTEND_URL}/dashboard/accounts?error=no_user_id"
            )
        
        # Check if account already exists
        existing_account = db.query(InstagramAccount).filter(
            InstagramAccount.user_id == user_id,
            InstagramAccount.igsid == ig_account_id
        ).first()
        
        if existing_account:
            # Update existing account
            print(f"📝 Updating existing account: {username}")
            existing_account.encrypted_credentials = encrypt_credentials(access_token)
            existing_account.username = username
            existing_account.igsid = ig_account_id
            db.commit()
            account_id = existing_account.id
        else:
            # Create new account
            print(f"✨ Creating new account: {username}")
            new_account = InstagramAccount(
                user_id=user_id,
                username=username,
                encrypted_credentials=encrypt_credentials(access_token),
                igsid=ig_account_id
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
    Exchange short-lived token for long-lived token (Instagram Business API).
    Short-lived tokens expire in 1 hour, long-lived tokens last 60 days.
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
    
    # Exchange short-lived token for long-lived token
    from app.utils.encryption import decrypt_credentials
    current_token = decrypt_credentials(account.encrypted_credentials)
    
    exchange_url = "https://graph.instagram.com/access_token"
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
    expires_in = data.get("expires_in")  # Should be ~5184000 seconds (60 days)
    
    print(f"✅ Token exchanged! Expires in: {expires_in} seconds (~{expires_in // 86400} days)")
    
    # Update token in database
    account.encrypted_credentials = encrypt_credentials(new_token)
    db.commit()
    
    return {
        "message": "Token exchanged for long-lived token successfully",
        "expires_in": expires_in
    }
