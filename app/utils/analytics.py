"""
Analytics utility functions for tracking events and generating tracking URLs.
"""
import os
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
        
        # Convert string to EventType enum if needed
        if isinstance(event_type, str):
            try:
                event_type = EventType(event_type)
            except ValueError:
                print(f"⚠️ Invalid event type: {event_type}")
                return None
        
        event = AnalyticsEvent(
            user_id=user_id,
            rule_id=rule_id,
            instagram_account_id=instagram_account_id,
            media_id=media_id,
            event_type=event_type,
            event_metadata=metadata or {}
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        
        return event.id
    except Exception as e:
        db.rollback()
        print(f"⚠️ Failed to log analytics event: {str(e)}")
        return None
