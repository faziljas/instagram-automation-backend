"""
Analytics API routes for tracking automation performance.
"""
from typing import Optional, List
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Header, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, desc
from urllib.parse import unquote, urlencode
from app.db.session import get_db
from app.models.analytics_event import AnalyticsEvent, EventType
from app.models.automation_rule import AutomationRule
from app.models.instagram_account import InstagramAccount
from app.utils.auth import verify_token
from pydantic import BaseModel

router = APIRouter()


def get_current_user_id(authorization: str = Header(None)) -> int:
    """Extract user_id from JWT token."""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required"
        )
    
    try:
        scheme, token = authorization.split()
        if scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication scheme"
            )
        
        payload = verify_token(token)
        if not payload:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token"
            )
        
        user_id = int(payload.get("sub"))
        return user_id
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token format"
        )


@router.get("/track/redirect")
async def track_link_click(
    url: str = Query(..., description="Target URL to redirect to"),
    rule_id: Optional[int] = Query(None, description="Automation rule ID"),
    user_id: Optional[int] = Query(None, description="Business owner user ID"),
    media_id: Optional[str] = Query(None, description="Instagram media ID"),
    instagram_account_id: Optional[int] = Query(None, description="Instagram account ID"),
    db: Session = Depends(get_db)
):
    """
    Track link clicks and redirect to target URL.
    This endpoint logs a LINK_CLICKED event and immediately redirects the user.
    
    Usage: Wrap Instagram button URLs with this tracker:
    https://yourdomain.com/api/analytics/track/redirect?url={encoded_url}&rule_id={rule_id}&user_id={user_id}
    """
    try:
        # Decode the target URL
        target_url = unquote(url)
        
        # Validate URL format
        if not target_url.startswith(("http://", "https://")):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid URL format"
            )
        
        # Log the analytics event
        if user_id and rule_id:
            event = AnalyticsEvent(
                user_id=user_id,
                rule_id=rule_id,
                instagram_account_id=instagram_account_id,
                media_id=media_id,
                event_type=EventType.LINK_CLICKED,
                event_metadata={
                    "url": target_url,
                    "clicked_at": datetime.utcnow().isoformat()
                }
            )
            db.add(event)
            db.commit()
            print(f"✅ Tracked link click: rule_id={rule_id}, url={target_url[:50]}...")
        else:
            print(f"⚠️ Link click tracking skipped: missing user_id or rule_id")
        
        # Immediately redirect to target URL
        return RedirectResponse(url=target_url, status_code=302)
        
    except Exception as e:
        print(f"❌ Error tracking link click: {str(e)}")
        # Still redirect even if tracking fails
        try:
            target_url = unquote(url)
            return RedirectResponse(url=target_url, status_code=302)
        except:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to process redirect"
            )


class AnalyticsSummary(BaseModel):
    """Analytics summary response model."""
    total_triggers: int
    total_dms_sent: int
    leads_collected: int
    link_clicks: int
    follow_button_clicks: int
    im_following_clicks: int
    profile_visits: int
    comment_replies: int
    top_posts: List[dict]
    
    class Config:
        from_attributes = True


@router.get("/dashboard", response_model=AnalyticsSummary)
def get_analytics_dashboard(
    days: int = Query(7, ge=1, le=90, description="Number of days to analyze"),
    rule_id: Optional[int] = Query(None, description="Filter by specific rule ID"),
    instagram_account_id: Optional[int] = Query(None, description="Filter by Instagram account"),
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id)
):
    """
    Get analytics dashboard summary for the requested time range.
    
    Returns aggregated statistics including:
    - Total triggers, DMs sent, leads collected
    - Button clicks (Follow, I'm following, Profile visits)
    - Top performing posts/media
    """
    try:
        # Calculate date range
        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=days)
        
        # Build base query - filter by user_id and date range
        base_query = db.query(AnalyticsEvent).filter(
            and_(
                AnalyticsEvent.user_id == user_id,
                AnalyticsEvent.created_at >= start_date,
                AnalyticsEvent.created_at <= end_date
            )
        )
        
        # Apply optional filters
        if rule_id:
            base_query = base_query.filter(AnalyticsEvent.rule_id == rule_id)
        
        if instagram_account_id:
            base_query = base_query.filter(AnalyticsEvent.instagram_account_id == instagram_account_id)
        
        # Aggregate counts by event type
        total_triggers = base_query.filter(
            AnalyticsEvent.event_type == EventType.TRIGGER_MATCHED
        ).count()
        
        total_dms_sent = base_query.filter(
            AnalyticsEvent.event_type == EventType.DM_SENT
        ).count()
        
        leads_collected = base_query.filter(
            AnalyticsEvent.event_type == EventType.EMAIL_COLLECTED
        ).count()
        
        link_clicks = base_query.filter(
            AnalyticsEvent.event_type == EventType.LINK_CLICKED
        ).count()
        
        follow_button_clicks = base_query.filter(
            AnalyticsEvent.event_type == EventType.FOLLOW_BUTTON_CLICKED
        ).count()
        
        im_following_clicks = base_query.filter(
            AnalyticsEvent.event_type == EventType.IM_FOLLOWING_CLICKED
        ).count()
        
        profile_visits = base_query.filter(
            AnalyticsEvent.event_type == EventType.PROFILE_VISIT
        ).count()
        
        comment_replies = base_query.filter(
            AnalyticsEvent.event_type == EventType.COMMENT_REPLIED
        ).count()
        
        # Get top performing posts/media (grouped by media_id)
        top_posts_query = base_query.filter(
            AnalyticsEvent.media_id.isnot(None),
            AnalyticsEvent.event_type == EventType.TRIGGER_MATCHED
        ).with_entities(
            AnalyticsEvent.media_id,
            func.count(AnalyticsEvent.id).label("trigger_count")
        ).group_by(
            AnalyticsEvent.media_id
        ).order_by(
            desc("trigger_count")
        ).limit(10)
        
        top_posts = []
        for media_id, trigger_count in top_posts_query.all():
            # Get additional stats for this media
            media_leads = base_query.filter(
                AnalyticsEvent.media_id == media_id,
                AnalyticsEvent.event_type == EventType.EMAIL_COLLECTED
            ).count()
            
            media_dms = base_query.filter(
                AnalyticsEvent.media_id == media_id,
                AnalyticsEvent.event_type == EventType.DM_SENT
            ).count()
            
            top_posts.append({
                "media_id": media_id,
                "trigger_count": trigger_count,
                "leads_count": media_leads,
                "dms_count": media_dms
            })
        
        return AnalyticsSummary(
            total_triggers=total_triggers,
            total_dms_sent=total_dms_sent,
            leads_collected=leads_collected,
            link_clicks=link_clicks,
            follow_button_clicks=follow_button_clicks,
            im_following_clicks=im_following_clicks,
            profile_visits=profile_visits,
            comment_replies=comment_replies,
            top_posts=top_posts
        )
        
    except Exception as e:
        print(f"❌ Error fetching analytics: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch analytics: {str(e)}"
        )


@router.post("/events")
def log_analytics_event(
    event_type: EventType,
    rule_id: Optional[int] = None,
    media_id: Optional[str] = None,
    instagram_account_id: Optional[int] = None,
    metadata: Optional[dict] = None,
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id)
):
    """
    Manually log an analytics event.
    Useful for tracking events that happen in the backend.
    """
    try:
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
        
        return {
            "success": True,
            "event_id": event.id,
            "message": "Event logged successfully"
        }
    except Exception as e:
        db.rollback()
        print(f"❌ Error logging analytics event: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to log event: {str(e)}"
        )
