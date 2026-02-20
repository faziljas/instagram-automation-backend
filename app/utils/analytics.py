"""
Analytics utility functions for tracking events and generating tracking URLs.
"""
import os
import requests
from urllib.parse import quote
from typing import Optional


def get_base_url() -> str:
    """
    Get the base URL for the API.
    Falls back to localhost for development.
    """
    # Check for explicit API URL first
    api_url = os.getenv("API_URL")
    if api_url:
        return api_url.rstrip("/")
    
    # Check for Render deployment URL
    render_url = os.getenv("RENDER_EXTERNAL_URL")
    if render_url:
        return render_url.rstrip("/")
    
    # Check for custom domain
    custom_domain = os.getenv("CUSTOM_DOMAIN")
    if custom_domain:
        return f"https://{custom_domain}".rstrip("/")
    
    # Default to localhost for development
    return "http://localhost:8000"


def fetch_media_preview_url(media_id: str, access_token: str) -> Optional[str]:
    """
    Fetch media preview URL (thumbnail_url or media_url) from Instagram API.
    This is cached in analytics events to preserve previews even if media is deleted.
    
    Args:
        media_id: Instagram media ID
        access_token: Instagram access token
        
    Returns:
        Optional[str]: Media preview URL (thumbnail_url for videos, media_url for photos) or None if fetch fails
    """
    try:
        r = requests.get(
            f"https://graph.instagram.com/v21.0/{media_id}",
            params={"fields": "media_type,media_url,thumbnail_url", "access_token": access_token},
            timeout=5
        )
        if r.status_code == 200:
            d = r.json()
            # Use thumbnail_url for videos, media_url for photos
            media_url = d.get("thumbnail_url") or d.get("media_url")
            return media_url
        else:
            print(f"⚠️ Failed to fetch media preview for {media_id}: {r.status_code}")
            return None
    except Exception as e:
        print(f"⚠️ Exception fetching media preview for {media_id}: {str(e)}")
        return None


def generate_tracking_url(
    target_url: str,
    rule_id: int,
    user_id: int,
    media_id: Optional[str] = None,
    instagram_account_id: Optional[int] = None
) -> str:
    """
    Generate a tracking URL that logs clicks and redirects to the target URL.
    
    Args:
        target_url: The destination URL to redirect to
        rule_id: Automation rule ID
        user_id: Business owner user ID
        media_id: Optional Instagram media ID
        instagram_account_id: Optional Instagram account ID
    
    Returns:
        str: Tracking URL that will log the click and redirect
    """
    base_url = get_base_url()
    
    # Build query parameters
    params = {
        "url": target_url,
        "rule_id": rule_id,
        "user_id": user_id
    }
    
    if media_id:
        params["media_id"] = media_id
    
    if instagram_account_id:
        params["instagram_account_id"] = instagram_account_id
    
    # Build the tracking URL
    query_string = "&".join([f"{k}={quote(str(v))}" for k, v in params.items()])
    tracking_url = f"{base_url}/api/analytics/track/redirect?{query_string}"
    
    return tracking_url


def log_analytics_event_sync(
    db,
    user_id: int,
    event_type: str,
    rule_id: Optional[int] = None,
    media_id: Optional[str] = None,
    instagram_account_id: Optional[int] = None,
    metadata: Optional[dict] = None
) -> Optional[int]:
    """
    Synchronously log an analytics event to the database.
    If media_id is provided, fetches and caches the media preview URL immediately
    (while the media still exists) to preserve previews even if media is deleted later.
    
    Args:
        db: SQLAlchemy database session
        user_id: Business owner user ID
        event_type: Event type (from EventType enum)
        rule_id: Optional automation rule ID
        media_id: Optional Instagram media ID
        instagram_account_id: Optional Instagram account ID
        metadata: Optional additional metadata
    
    Returns:
        Optional[int]: Event ID if successful, None otherwise
    """
    try:
        from app.models.analytics_event import AnalyticsEvent, EventType
        from app.models.instagram_account import InstagramAccount
        from app.utils.encryption import decrypt_credentials
        
        # Convert string to EventType enum if needed
        if isinstance(event_type, str):
            try:
                event_type = EventType(event_type)
            except ValueError:
                print(f"⚠️ Invalid event type: {event_type}")
                return None
        
        # CRITICAL PERFORMANCE FIX: Don't fetch media preview URL synchronously
        # This blocks the async event loop with a 5-second HTTP request
        # Media preview can be fetched later in a background task if needed
        # For now, log the event immediately without blocking
        event = AnalyticsEvent(
            user_id=user_id,
            rule_id=rule_id,
            instagram_account_id=instagram_account_id,
            media_id=media_id,
            media_preview_url=None,  # Will be fetched in background if needed
            event_type=event_type,
            event_metadata=metadata or {}
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        
        # Invalidate analytics cache so dashboard/analytics pages show fresh data
        try:
            from app.api.routes.analytics import invalidate_analytics_cache_for_user
            invalidate_analytics_cache_for_user(user_id)
        except ImportError:
            pass
        
        return event.id
    except Exception as e:
        db.rollback()
        error_str = str(e).lower()
        
        # Check if this is an enum value error
        if "invalid input value for enum" in error_str or "invalidtextrepresentation" in error_str:
            print(f"⚠️ Failed to log analytics event: {str(e)}")
            print(f"   This indicates a missing enum value in the database.")
            print(f"   The startup validation should have caught this - check startup logs.")
            # Try to auto-fix by ensuring enum values exist
            try:
                from app.utils.enum_validator import ensure_eventtype_enum_values
                if ensure_eventtype_enum_values(db):
                    print(f"   ✅ Auto-fixed missing enum values. Retrying event log...")
                    # Retry once after fixing
                    try:
                        event = AnalyticsEvent(
                            user_id=user_id,
                            rule_id=rule_id,
                            instagram_account_id=instagram_account_id,
                            media_id=media_id,
                            media_preview_url=None,
                            event_type=event_type,
                            event_metadata=metadata or {}
                        )
                        db.add(event)
                        db.commit()
                        db.refresh(event)
                        return event.id
                    except Exception as retry_error:
                        print(f"   ⚠️ Retry after enum fix also failed: {retry_error}")
            except Exception as fix_error:
                print(f"   ⚠️ Failed to auto-fix enum: {fix_error}")
        else:
            print(f"⚠️ Failed to log analytics event: {str(e)}")
        
        return None
