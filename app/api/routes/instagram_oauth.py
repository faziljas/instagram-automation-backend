"""
Facebook Login for Business OAuth Routes
Implements OAuth flow for connecting Instagram Business accounts via Facebook Pages
Supports both server-side redirect flow and Facebook SDK popup flow
"""
import os
import requests
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status, Query, Header, Request, Body
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import update
from app.db.session import get_db
from app.models.instagram_account import InstagramAccount
from app.dependencies.auth import get_current_user_id
from app.utils.encryption import encrypt_credentials
from app.utils.plan_enforcement import check_account_limit

router = APIRouter()

# Instagram App Configuration
# Fallback to FACEBOOK_* variables if INSTAGRAM_* are not set (they're the same in Meta)
INSTAGRAM_APP_ID = os.getenv("INSTAGRAM_APP_ID", os.getenv("FACEBOOK_APP_ID", ""))
INSTAGRAM_APP_SECRET = os.getenv("INSTAGRAM_APP_SECRET", os.getenv("FACEBOOK_APP_SECRET", ""))
INSTAGRAM_REDIRECT_URI = os.getenv("INSTAGRAM_REDIRECT_URI", os.getenv("FACEBOOK_REDIRECT_URI", "https://instagram-automation-backend-065d.onrender.com/api/instagram/oauth/callback"))
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")
FACEBOOK_API_VERSION = "v19.0"
# Note: FRONTEND_URL validation happens at runtime in endpoints to avoid import failures


class ConnectSDKRequest(BaseModel):
    access_token: str


class ExchangeCodeRequest(BaseModel):
    code: str


@router.get("/oauth/authorize")
def get_instagram_auth_url(user_id: int = Depends(get_current_user_id)):
    """
    Generate Instagram Business Login OAuth authorization URL.
    Frontend redirects user to this URL to start OAuth flow.
    Uses Instagram's native OAuth endpoint for Instagram-branded login screen.
    Redirects to frontend callback URL to allow popup window closure.
    """
    if not INSTAGRAM_APP_ID or not INSTAGRAM_APP_SECRET:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Instagram OAuth not configured"
        )
    
    if not FRONTEND_URL:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="FRONTEND_URL environment variable is required for OAuth callback"
        )
    
    # Instagram Business Login scopes (2025)
    scopes = [
        "instagram_business_basic",
        "instagram_business_manage_messages",
        "instagram_business_manage_comments",
        "instagram_business_content_publish"
    ]
    
    # Construct frontend callback URL (popup redirects here to close window)
    redirect_uri = f"{FRONTEND_URL.strip().rstrip('/')}/dashboard/callback"
    
    # Build Instagram Business Login OAuth URL
    oauth_url = (
        f"https://www.instagram.com/oauth/authorize"
        f"?client_id={INSTAGRAM_APP_ID}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&scope={','.join(scopes)}"
        f"&state={user_id}"  # Pass user_id to identify user after callback
    )
    
    print(f"🔗 Instagram Business Login OAuth URL - redirect_uri: '{redirect_uri}'")
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
    
    if not FRONTEND_URL:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="FRONTEND_URL environment variable is required for OAuth callback"
        )
    
    # Get config_id from environment (for SuperProfile/Instagram Login flow)
    config_id = os.getenv("FACEBOOK_CONFIG_ID", "")
    
    # Build redirect URI for popup callback (will be handled by frontend)
    # For popup flow, we'll use a special callback that posts message back to parent
    popup_redirect_uri = f"{FRONTEND_URL.strip().rstrip('/')}/dashboard/callback"
    
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
    code: str = Query(None),
    state: str = Query(None),  # This is the user_id
    error: str = Query(None),  # OAuth error from Facebook
    error_reason: str = Query(None),  # Error reason from Facebook
    error_description: str = Query(None),  # Error description from Facebook
    db: Session = Depends(get_db),
    request: Request = None
):
    """
    Handle Facebook OAuth callback.
    Exchange authorization code for User Access Token, fetch Pages, find Instagram Business Account.
    """
    try:
        # Handle OAuth errors (user denied, invalid request, etc.)
        if error:
            error_msg = error_description or error_reason or error
            print(f"❌ OAuth error received: {error} - {error_msg}")
            return RedirectResponse(
                url=f"{FRONTEND_URL}/dashboard/accounts?error=oauth_error&message={error_msg}"
            )
        
        # Check if code is missing
        if not code:
            print("❌ OAuth callback received without code parameter")
            return RedirectResponse(
                url=f"{FRONTEND_URL}/dashboard/accounts?error=no_code&message=Authorization code not provided"
            )
        
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
            
            # Create tracker for this (user_id, IGSID) combination
            # Each user gets their own tracker per Instagram account automatically
            from app.services.instagram_usage_tracker import get_or_create_tracker
            
            if new_account.igsid:
                # Get or create tracker for this (user_id, igsid) combination
                tracker = get_or_create_tracker(user_id, new_account.igsid, db)
                print(f"✅ Tracker for user {user_id}, IGSID {new_account.igsid}: rules={tracker.rules_created_count}, dms={tracker.dms_sent_count}")
            
            # Reconnect any disconnected automation rules for this user + IGSID
            # Also restore analytics data if same user, or delete if different user
            from app.models.automation_rule import AutomationRule
            from app.models.analytics_event import AnalyticsEvent
            from app.models.captured_lead import CapturedLead
            from app.models.automation_rule_stats import AutomationRuleStats
            from sqlalchemy import update
            from sqlalchemy.orm.attributes import flag_modified
            
            disconnected_rules = db.query(AutomationRule).filter(
                AutomationRule.instagram_account_id.is_(None),
                AutomationRule.deleted_at.is_(None)
            ).all()
            
            print(f"🔍 [RECONNECT] Found {len(disconnected_rules)} disconnected rules. Looking for IGSID: {new_account.igsid}, user_id: {user_id}")
            
            # Check if this is a different user connecting to the same IG account
            is_different_user = False
            for rule in disconnected_rules:
                if rule.config and isinstance(rule.config, dict):
                    disconnected_igsid = str(rule.config.get("disconnected_igsid", ""))
                    disconnected_user_id = rule.config.get("disconnected_user_id")
                    if disconnected_igsid == str(new_account.igsid) and disconnected_user_id != user_id:
                        is_different_user = True
                        print(f"⚠️ [RECONNECT] Different user connecting to same IG account. Will reset analytics data.")
                        break
            
            if is_different_user:
                # Delete analytics data for this IG account (from previous user)
                rules_to_clean = [r for r in disconnected_rules if r.config and isinstance(r.config, dict) and 
                                str(r.config.get("disconnected_igsid", "")) == str(new_account.igsid)]
                rule_ids_to_clean = [r.id for r in rules_to_clean]
                
                if rule_ids_to_clean:
                    db.query(AnalyticsEvent).filter(AnalyticsEvent.rule_id.in_(rule_ids_to_clean)).delete(synchronize_session=False)
                    db.query(AutomationRuleStats).filter(AutomationRuleStats.automation_rule_id.in_(rule_ids_to_clean)).delete(synchronize_session=False)
                    db.query(CapturedLead).filter(CapturedLead.automation_rule_id.in_(rule_ids_to_clean)).delete(synchronize_session=False)
                    for rule in rules_to_clean:
                        rule.deleted_at = datetime.utcnow()
                    db.flush()
            
            # Now reconnect rules for the same user
            reconnected_count = 0
            restored_analytics_count = 0
            restored_leads_count = 0
            
            for rule in disconnected_rules:
                if rule.config and isinstance(rule.config, dict):
                    disconnected_igsid = str(rule.config.get("disconnected_igsid", ""))
                    disconnected_user_id = rule.config.get("disconnected_user_id")
                    if disconnected_igsid == str(new_account.igsid) and disconnected_user_id == user_id:
                        rule.instagram_account_id = new_account.id
                        
                        # Restore analytics events for this rule
                        analytics_restored = db.execute(
                            update(AnalyticsEvent).where(
                                AnalyticsEvent.rule_id == rule.id,
                                AnalyticsEvent.instagram_account_id.is_(None)
                            ).values(instagram_account_id=new_account.id)
                        )
                        restored_analytics_count += analytics_restored.rowcount
                        
                        # Restore captured leads for this rule
                        leads_restored = db.execute(
                            update(CapturedLead).where(
                                CapturedLead.automation_rule_id == rule.id,
                                CapturedLead.instagram_account_id.is_(None)
                            ).values(instagram_account_id=new_account.id)
                        )
                        restored_leads_count += leads_restored.rowcount
                        
                        if "disconnected_igsid" in rule.config:
                            del rule.config["disconnected_igsid"]
                        if "disconnected_username" in rule.config:
                            del rule.config["disconnected_username"]
                        if "disconnected_user_id" in rule.config:
                            del rule.config["disconnected_user_id"]
                        flag_modified(rule, "config")
                        reconnected_count += 1
                        print(f"✅ [RECONNECT] Reconnecting rule {rule.id} to account {new_account.id}")
            
            if reconnected_count > 0:
                db.commit()
                print(f"✅ Reconnected {reconnected_count} automation rule(s) to account {new_account.username}")
                print(f"✅ Restored {restored_analytics_count} analytics events and {restored_leads_count} captured leads")
            else:
                print(f"⚠️ [RECONNECT] No rules matched for reconnection (IGSID: {new_account.igsid}, user_id: {user_id})")
        
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
        
        # Ensure tracker exists for this (user_id, IGSID) combination
        from app.services.instagram_usage_tracker import get_or_create_tracker
        if existing_account.igsid:
            get_or_create_tracker(user_id, existing_account.igsid, db)
        
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
        
        # Create tracker for this (user_id, IGSID) combination
        # Each user gets their own tracker per Instagram account automatically
        from app.services.instagram_usage_tracker import get_or_create_tracker
        
        if new_account.igsid:
            # Get or create tracker for this (user_id, igsid) combination
            tracker = get_or_create_tracker(user_id, new_account.igsid, db)
            print(f"✅ Tracker for user {user_id}, IGSID {new_account.igsid}: rules={tracker.rules_created_count}, dms={tracker.dms_sent_count}")
        
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
    Exchange Instagram Business Login OAuth authorization code for access token.
    Handles Instagram native OAuth flow with code exchange.
    """
    try:
        code = request_data.code
        print(f"📥 Instagram OAuth code exchange request received for user {user_id}")
        
        if not code:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Authorization code is required"
            )
        
        if not FRONTEND_URL:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="FRONTEND_URL environment variable is required for OAuth callback"
            )
        
        # Build redirect URI (must match frontend callback URL used in OAuth authorization)
        redirect_uri = f"{FRONTEND_URL.strip().rstrip('/')}/dashboard/callback"
        
        # Step 1: Exchange code for short-lived access token (Instagram OAuth)
        token_url = "https://api.instagram.com/oauth/access_token"
        token_data = {
            "client_id": INSTAGRAM_APP_ID,
            "client_secret": INSTAGRAM_APP_SECRET,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code
        }
        
        print(f"🔄 Step 1: Exchanging code for short-lived token...")
        token_response = requests.post(token_url, data=token_data)
        
        if token_response.status_code != 200:
            error_detail = token_response.text
            print(f"❌ Token exchange failed: {error_detail}")
            
            # Check for specific Instagram OAuth errors
            try:
                error_json = token_response.json()
                error_type = error_json.get("error_type", "")
                error_message = error_json.get("error_message", "")
                
                # Handle "Insufficient Developer Role" error (app in development mode)
                if "Insufficient Developer Role" in error_message or "insufficient developer role" in error_message.lower():
                    user_friendly_message = (
                        "This Instagram app is currently in development mode. "
                        "Only test users added to the app can connect their accounts. "
                        "Please contact the app administrator to be added as a test user, "
                        "or wait until the app is approved and published."
                    )
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=user_friendly_message
                    )
                
                # Handle other Instagram OAuth errors
                if error_type or error_message:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Instagram OAuth error: {error_message or error_type}"
                    )
            except HTTPException:
                raise
            except Exception:
                # If JSON parsing fails, use raw error text
                pass
            
            # Generic error if we couldn't parse it
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to exchange code for token: {error_detail}"
            )
        
        token_result = token_response.json()
        short_lived_token = token_result.get("access_token")
        user_id_from_token = token_result.get("user_id")
        
        if not short_lived_token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No access token received from Instagram"
            )
        
        print(f"✅ Step 1 complete: Got short-lived token")
        
        # Step 2: Exchange short-lived token for long-lived token (60 days)
        exchange_url = "https://graph.instagram.com/access_token"
        exchange_params = {
            "grant_type": "ig_exchange_token",
            "client_secret": INSTAGRAM_APP_SECRET,
            "access_token": short_lived_token
        }
        
        print(f"🔄 Step 2: Exchanging short-lived token for long-lived token...")
        exchange_response = requests.get(exchange_url, params=exchange_params)
        
        if exchange_response.status_code != 200:
            error_detail = exchange_response.text
            print(f"❌ Long-lived token exchange failed: {error_detail}")
            # Fallback: Use short-lived token if long-lived exchange fails
            print(f"⚠️ Falling back to short-lived token")
            long_lived_token = short_lived_token
            expires_in = 3600  # Short-lived tokens expire in 1 hour
        else:
            exchange_result = exchange_response.json()
            long_lived_token = exchange_result.get("access_token")
            expires_in = exchange_result.get("expires_in", 5184000)  # Default 60 days
            
            if not long_lived_token:
                # Fallback to short-lived token
                print(f"⚠️ No long-lived token, using short-lived token")
                long_lived_token = short_lived_token
                expires_in = 3600
            else:
                print(f"✅ Step 2 complete: Got long-lived token (expires in {expires_in} seconds ~{expires_in // 86400} days)")
        
        # Step 3: Get Instagram user info and associated Facebook Page
        user_info_url = f"https://graph.instagram.com/{user_id_from_token}"
        user_info_params = {
            "fields": "id,username,account_type",
            "access_token": long_lived_token
        }
        
        print(f"🔄 Step 3: Fetching Instagram account info...")
        user_info_response = requests.get(user_info_url, params=user_info_params)
        
        instagram_username = None
        account_type = None
        if user_info_response.status_code == 200:
            user_info = user_info_response.json()
            instagram_username = user_info.get("username")
            account_type = user_info.get("account_type")
            print(f"✅ Instagram account info: username={instagram_username}, type={account_type}")
            
            # Validate account type (must be BUSINESS or CREATOR)
            if account_type == "PERSONAL":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Personal Instagram accounts are not supported. Please switch to a Business or Creator account."
                )
        else:
            error_detail = user_info_response.text
            print(f"⚠️ Failed to fetch user info: {error_detail}")
            # Continue anyway, we'll use the user_id from token
        
        # Step 3.5: Subscribe Instagram Business Account to webhooks
        # This is CRITICAL - without this, the bot cannot receive messages
        # We subscribe the Instagram Business Account directly (not the Facebook Page)
        
        # ------------------------------------------------------------------
        # CORRECT FORMAT: A single string with comma-separated values.
        # DO NOT put quotes around the individual words.
        # Valid fields: messages, messaging_postbacks, messaging_optins, 
        # message_reactions, message_edit, standby, comments, live_comments, mentions
        # NOTE: message_deliveries and message_reads are NOT valid fields!
        # ------------------------------------------------------------------
        subscribed_fields = "messages,messaging_postbacks,messaging_optins,message_reactions,message_edit,standby,comments,live_comments"
        
        webhook_subscribe_url = f"https://graph.instagram.com/v21.0/{user_id_from_token}/subscribed_apps"
        
        print(f"🔄 Step 3.5: Subscribing Instagram Business Account to webhooks...")
        print(f"   Endpoint: {webhook_subscribe_url}")
        print(f"   Fields: {subscribed_fields}")
        
        try:
            # Pass it to the API
            webhook_response = requests.post(
                webhook_subscribe_url,
                params={
                    "subscribed_fields": subscribed_fields,
                    "access_token": long_lived_token
                }
            )
            
            if webhook_response.status_code == 200:
                print(f"✅ Subscribed IG User {user_id_from_token} to Webhooks")
                webhook_result = webhook_response.json()
                print(f"   Response: {webhook_result}")
                
                # Verify subscription by checking what fields were actually subscribed
                print(f"🔄 Verifying webhook subscription...")
                verify_url = f"https://graph.instagram.com/v21.0/{user_id_from_token}/subscribed_apps"
                verify_params = {
                    "access_token": long_lived_token
                }
                verify_response = requests.get(verify_url, params=verify_params)
                
                if verify_response.status_code == 200:
                    verify_result = verify_response.json()
                    subscribed = verify_result.get("data", [])
                    if subscribed:
                        for sub in subscribed:
                            fields = sub.get("subscribed_fields", [])
                            print(f"   ✅ Verified: Subscribed fields: {', '.join(fields)}")
                            if "messages" not in fields:
                                print(f"   ⚠️ WARNING: 'messages' field is NOT in subscribed fields!")
                                print(f"   ⚠️ This means new message webhooks will NOT be received!")
                            else:
                                print(f"   ✅ 'messages' field is subscribed - new message webhooks should work!")
                    else:
                        print(f"   ⚠️ No subscription data found in verification response")
                else:
                    print(f"   ⚠️ Could not verify subscription: {verify_response.text}")
                
                # CRITICAL: For 'messages' webhooks, Instagram might require Page-level subscription
                # Try to get the associated Facebook Page and subscribe there as well
                # This is required because 'messages' webhooks often need Page-level subscription
                print(f"🔄 Attempting Page-level webhook subscription for 'messages' field...")
                try:
                    # Try to get connected Facebook Page using Instagram Business Account
                    # Query: GET /{ig-user-id}?fields=connected_facebook_page
                    page_lookup_url = f"https://graph.instagram.com/v21.0/{user_id_from_token}"
                    page_lookup_params = {
                        "fields": "connected_facebook_page{id,name,access_token}",
                        "access_token": long_lived_token
                    }
                    
                    page_response = requests.get(page_lookup_url, params=page_lookup_params)
                    
                    if page_response.status_code == 200:
                        page_data = page_response.json()
                        connected_page = page_data.get("connected_facebook_page")
                        
                        if connected_page and isinstance(connected_page, dict):
                            page_id = connected_page.get("id")
                            page_token = connected_page.get("access_token")
                            
                            if page_id and page_token:
                                print(f"   ✅ Found connected Facebook Page: {page_id}")
                                
                                # Subscribe Page to messages webhook
                                page_subscribe_url = f"https://graph.facebook.com/v21.0/{page_id}/subscribed_apps"
                                page_subscribe_params = {
                                    "subscribed_fields": "messages",
                                    "access_token": page_token
                                }
                                
                                page_webhook_response = requests.post(page_subscribe_url, params=page_subscribe_params)
                                
                                if page_webhook_response.status_code == 200:
                                    print(f"   ✅ Successfully subscribed Page {page_id} to 'messages' webhook")
                                    page_result = page_webhook_response.json()
                                    print(f"      Response: {page_result}")
                                else:
                                    print(f"   ⚠️ Page-level subscription failed: {page_webhook_response.text}")
                            else:
                                print(f"   ⚠️ No Page ID or token found in connected_page: {connected_page}")
                        elif connected_page:
                            # Sometimes it's just an ID
                            page_id = str(connected_page)
                            print(f"   ⚠️ Found Page ID but no token (ID only): {page_id}")
                            print(f"   ⚠️ Cannot subscribe at Page level without Page access token")
                        else:
                            print(f"   ⚠️ No connected Facebook Page found")
                            print(f"   ⚠️ 'messages' webhooks might not work without Page-level subscription")
                    else:
                        print(f"   ⚠️ Could not fetch connected Facebook Page: {page_response.text}")
                except Exception as e:
                    print(f"   ⚠️ Error attempting Page-level subscription: {str(e)}")
                    # Don't fail the whole flow - continue anyway
            else:
                error_detail = webhook_response.text
                print(f"❌ CRITICAL: Webhook subscription failed (status {webhook_response.status_code}): {error_detail}")
                # Raise exception - without webhooks, the bot cannot receive messages
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to subscribe Instagram account to webhooks. This is required for the bot to receive messages. Error: {error_detail}"
                )
        except HTTPException:
            raise
        except Exception as e:
            error_msg = str(e)
            print(f"❌ CRITICAL: Webhook subscription error: {error_msg}")
            # Raise exception - without webhooks, the bot cannot receive messages
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to subscribe Instagram account to webhooks. This is required for the bot to receive messages. Error: {error_msg}"
            )
        
        # Step 4: Check account limit BEFORE connecting
        try:
            check_account_limit(user_id, db)
        except HTTPException as e:
            print(f"❌ Account limit check failed: {e.detail}")
            raise
        
        # Step 5: Check if account already exists and handle gracefully
        # Note: For Instagram native OAuth, we store the token directly
        # The user_id_from_token is the Instagram Business Account ID
        username = instagram_username or f"instagram_{user_id_from_token}"
        
        # Check if this Instagram account is already connected to the same user
        existing_account_same_user = db.query(InstagramAccount).filter(
            InstagramAccount.user_id == user_id,
            InstagramAccount.igsid == str(user_id_from_token)
        ).first()
        
        if existing_account_same_user:
            # Account already connected to this user - update token and reconnect rules
            print(f"📝 Account already connected to this user. Updating token...")
            if instagram_username:
                existing_account_same_user.username = instagram_username
            existing_account_same_user.encrypted_page_token = encrypt_credentials(long_lived_token)
            existing_account_same_user.is_active = True  # Ensure it's active
            db.commit()
            
            # Reconnect any disconnected automation rules for this user + IGSID
            # Also restore analytics data if same user, or delete if different user
            from app.models.automation_rule import AutomationRule
            from app.models.analytics_event import AnalyticsEvent
            from app.models.captured_lead import CapturedLead
            from app.models.automation_rule_stats import AutomationRuleStats
            from sqlalchemy import update
            from sqlalchemy.orm.attributes import flag_modified
            
            disconnected_rules = db.query(AutomationRule).filter(
                AutomationRule.instagram_account_id.is_(None),
                AutomationRule.deleted_at.is_(None)
            ).all()
            
            # Check if this is a different user (shouldn't happen in this path, but check anyway)
            is_different_user = False
            for rule in disconnected_rules:
                if rule.config and isinstance(rule.config, dict):
                    disconnected_igsid = str(rule.config.get("disconnected_igsid", ""))
                    disconnected_user_id = rule.config.get("disconnected_user_id")
                    if disconnected_igsid == str(existing_account_same_user.igsid) and disconnected_user_id != user_id:
                        is_different_user = True
                        break
            
            if is_different_user:
                rules_to_clean = [r for r in disconnected_rules if r.config and isinstance(r.config, dict) and 
                                str(r.config.get("disconnected_igsid", "")) == str(existing_account_same_user.igsid)]
                rule_ids_to_clean = [r.id for r in rules_to_clean]
                if rule_ids_to_clean:
                    db.query(AnalyticsEvent).filter(AnalyticsEvent.rule_id.in_(rule_ids_to_clean)).delete(synchronize_session=False)
                    db.query(AutomationRuleStats).filter(AutomationRuleStats.automation_rule_id.in_(rule_ids_to_clean)).delete(synchronize_session=False)
                    db.query(CapturedLead).filter(CapturedLead.automation_rule_id.in_(rule_ids_to_clean)).delete(synchronize_session=False)
                    for rule in rules_to_clean:
                        rule.deleted_at = datetime.utcnow()
                    db.flush()
            
            # Now reconnect rules for the same user
            reconnected_count = 0
            restored_analytics_count = 0
            restored_leads_count = 0
            
            for rule in disconnected_rules:
                if rule.config and isinstance(rule.config, dict):
                    disconnected_igsid = str(rule.config.get("disconnected_igsid", ""))
                    disconnected_user_id = rule.config.get("disconnected_user_id")
                    if disconnected_igsid == str(existing_account_same_user.igsid) and disconnected_user_id == user_id:
                        rule.instagram_account_id = existing_account_same_user.id
                        
                        # Restore analytics events for this rule
                        analytics_restored = db.execute(
                            update(AnalyticsEvent).where(
                                AnalyticsEvent.rule_id == rule.id,
                                AnalyticsEvent.instagram_account_id.is_(None)
                            ).values(instagram_account_id=existing_account_same_user.id)
                        )
                        restored_analytics_count += analytics_restored.rowcount
                        
                        # Restore captured leads for this rule
                        leads_restored = db.execute(
                            update(CapturedLead).where(
                                CapturedLead.automation_rule_id == rule.id,
                                CapturedLead.instagram_account_id.is_(None)
                            ).values(instagram_account_id=existing_account_same_user.id)
                        )
                        restored_leads_count += leads_restored.rowcount
                        
                        if "disconnected_igsid" in rule.config:
                            del rule.config["disconnected_igsid"]
                        if "disconnected_username" in rule.config:
                            del rule.config["disconnected_username"]
                        if "disconnected_user_id" in rule.config:
                            del rule.config["disconnected_user_id"]
                        flag_modified(rule, "config")
                        reconnected_count += 1
            
            if reconnected_count > 0:
                db.commit()
                print(f"✅ Reconnected {reconnected_count} automation rule(s) to account {existing_account_same_user.username}")
                print(f"✅ Restored {restored_analytics_count} analytics events and {restored_leads_count} captured leads")
            
            print(f"✅ Instagram account {existing_account_same_user.username} reconnected successfully for user {user_id}!")
            
            return {
                "success": True,
                "already_connected": True,
                "account": {
                    "id": existing_account_same_user.id,
                    "username": existing_account_same_user.username,
                    "igsid": str(user_id_from_token),
                    "is_active": True
                },
                "message": f"Instagram account @{existing_account_same_user.username} is already connected. Token has been refreshed."
            }
        
        # Check if this Instagram account is already connected to a different user
        existing_account_other_user = db.query(InstagramAccount).filter(
            InstagramAccount.igsid == str(user_id_from_token),
            InstagramAccount.user_id != user_id
        ).first()
        
        if existing_account_other_user:
            # Account already connected to a different user - return graceful error
            print(f"⚠️ Instagram account {user_id_from_token} is already connected to a different user")
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"This Instagram account is already connected to another user. Please disconnect it from the other account first, or use a different Instagram account."
            )
        
        # Account doesn't exist - create new account
        print(f"✨ Creating new account: {username}")
        new_account = InstagramAccount(
            user_id=user_id,
            username=username,
            encrypted_credentials="",  # Legacy field, kept empty
            encrypted_page_token=encrypt_credentials(long_lived_token),
            page_id="",  # Not needed for Instagram native OAuth - messages use me/messages endpoint
            igsid=str(user_id_from_token)
        )
        db.add(new_account)
        db.commit()
        db.refresh(new_account)
        
        # Create tracker for this (user_id, IGSID) combination
        # Each user gets their own tracker per Instagram account automatically
        from app.services.instagram_usage_tracker import get_or_create_tracker
        
        if new_account.igsid:
            # Get or create tracker for this (user_id, igsid) combination
            # If this is first time this user connects this Instagram account, tracker is created fresh
            # If user reconnects same Instagram account, existing tracker is found (limits persist)
            tracker = get_or_create_tracker(user_id, new_account.igsid, db)
            print(f"✅ Tracker for user {user_id}, IGSID {new_account.igsid}: rules={tracker.rules_created_count}, dms={tracker.dms_sent_count}")
        
        # Reconnect any disconnected automation rules for this user + IGSID
        # Also restore analytics data if same user, or delete if different user
        from app.models.automation_rule import AutomationRule
        from app.models.analytics_event import AnalyticsEvent
        from app.models.captured_lead import CapturedLead
        from app.models.automation_rule_stats import AutomationRuleStats
        from sqlalchemy.orm.attributes import flag_modified
        
        disconnected_rules = db.query(AutomationRule).filter(
            AutomationRule.instagram_account_id.is_(None),
            AutomationRule.deleted_at.is_(None)
        ).all()
        
        print(f"🔍 [RECONNECT] Found {len(disconnected_rules)} disconnected rules. Looking for IGSID: {new_account.igsid}, user_id: {user_id}")
        
        # Check if this is a different user connecting to the same IG account
        # If so, delete analytics data from previous user
        is_different_user = False
        for rule in disconnected_rules:
            if rule.config and isinstance(rule.config, dict):
                disconnected_igsid = str(rule.config.get("disconnected_igsid", ""))
                disconnected_user_id = rule.config.get("disconnected_user_id")
                # If IGSID matches but user_id is different, it's a different user
                if disconnected_igsid == str(new_account.igsid) and disconnected_user_id != user_id:
                    is_different_user = True
                    print(f"⚠️ [RECONNECT] Different user connecting to same IG account. Will reset analytics data.")
                    break
        
        if is_different_user:
            # Delete analytics data for this IG account (from previous user)
            print(f"🗑️ [RECONNECT] Deleting analytics data for IG account {new_account.igsid} (different user)")
            
            # Find all rules that were connected to this IG account (by IGSID)
            rules_to_clean = [r for r in disconnected_rules if r.config and isinstance(r.config, dict) and 
                            str(r.config.get("disconnected_igsid", "")) == str(new_account.igsid)]
            
            rule_ids_to_clean = [r.id for r in rules_to_clean]
            
            if rule_ids_to_clean:
                # Delete analytics events for these rules
                deleted_analytics = db.query(AnalyticsEvent).filter(
                    AnalyticsEvent.rule_id.in_(rule_ids_to_clean)
                ).delete(synchronize_session=False)
                print(f"✅ [RECONNECT] Deleted {deleted_analytics} analytics events from previous user")
                
                # Delete rule stats
                deleted_stats = db.query(AutomationRuleStats).filter(
                    AutomationRuleStats.automation_rule_id.in_(rule_ids_to_clean)
                ).delete(synchronize_session=False)
                print(f"✅ [RECONNECT] Deleted {deleted_stats} rule stats from previous user")
                
                # Delete captured leads
                deleted_leads = db.query(CapturedLead).filter(
                    CapturedLead.automation_rule_id.in_(rule_ids_to_clean)
                ).delete(synchronize_session=False)
                print(f"✅ [RECONNECT] Deleted {deleted_leads} captured leads from previous user")
                
                # Delete the disconnected rules themselves (different user, so they shouldn't get these rules)
                for rule in rules_to_clean:
                    rule.deleted_at = datetime.utcnow()
                print(f"✅ [RECONNECT] Marked {len(rules_to_clean)} rules as deleted (different user)")
                
                db.flush()
        
        # Now reconnect rules for the same user
        reconnected_count = 0
        restored_analytics_count = 0
        restored_leads_count = 0
        
        for rule in disconnected_rules:
            if rule.config and isinstance(rule.config, dict):
                disconnected_igsid = str(rule.config.get("disconnected_igsid", ""))
                disconnected_user_id = rule.config.get("disconnected_user_id")
                print(f"🔍 [RECONNECT] Rule {rule.id}: disconnected_igsid={disconnected_igsid}, disconnected_user_id={disconnected_user_id}, new_igsid={new_account.igsid}, user_id: {user_id}")
                # Match by IGSID AND user_id to ensure we only reconnect rules for the same user
                if disconnected_igsid == str(new_account.igsid) and disconnected_user_id == user_id:
                    # Reconnect this rule to the new account
                    rule.instagram_account_id = new_account.id
                    
                    # Restore analytics events for this rule
                    analytics_restored = db.execute(
                        update(AnalyticsEvent).where(
                            AnalyticsEvent.rule_id == rule.id,
                            AnalyticsEvent.instagram_account_id.is_(None)
                        ).values(instagram_account_id=new_account.id)
                    )
                    restored_analytics_count += analytics_restored.rowcount
                    
                    # Restore captured leads for this rule
                    leads_restored = db.execute(
                        update(CapturedLead).where(
                            CapturedLead.automation_rule_id == rule.id,
                            CapturedLead.instagram_account_id.is_(None)
                        ).values(instagram_account_id=new_account.id)
                    )
                    restored_leads_count += leads_restored.rowcount
                    
                    # Remove disconnected_igsid from config
                    if "disconnected_igsid" in rule.config:
                        del rule.config["disconnected_igsid"]
                    if "disconnected_username" in rule.config:
                        del rule.config["disconnected_username"]
                    if "disconnected_user_id" in rule.config:
                        del rule.config["disconnected_user_id"]
                    # Mark JSON column as modified so SQLAlchemy saves the changes
                    flag_modified(rule, "config")
                    reconnected_count += 1
                    print(f"✅ [RECONNECT] Reconnecting rule {rule.id} to account {new_account.id}")
        
        if reconnected_count > 0:
            db.commit()
            print(f"✅ Reconnected {reconnected_count} automation rule(s) to account {new_account.username}")
            print(f"✅ Restored {restored_analytics_count} analytics events and {restored_leads_count} captured leads")
        else:
            print(f"⚠️ [RECONNECT] No rules matched for reconnection (IGSID: {new_account.igsid}, user_id: {user_id})")
        
        print(f"✅ Instagram account {new_account.username} connected successfully for user {user_id}!")
        
        return {
            "success": True,
            "already_connected": False,
            "account": {
                "id": new_account.id,
                "username": new_account.username,
                "igsid": str(user_id_from_token),
                "is_active": True
            },
            "message": f"Instagram account @{new_account.username} connected successfully!"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Instagram code exchange error: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to connect Instagram account: {str(e)}"
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
