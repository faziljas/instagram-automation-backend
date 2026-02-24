"""
Analytics API routes for tracking automation performance.
"""
from typing import Optional, List
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Header, Query, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, desc, case, cast, String
from urllib.parse import unquote, urlencode, urlparse
from app.db.session import get_db
from app.models.analytics_event import AnalyticsEvent, EventType
from app.models.automation_rule import AutomationRule
from app.models.instagram_account import InstagramAccount
from app.dependencies.auth import get_current_user_id
from app.utils.encryption import decrypt_credentials
from pydantic import BaseModel
import requests
import hashlib
import json

router = APIRouter()

# In-memory cache for analytics responses (5 minute TTL)
_analytics_cache = {}
_cache_ttl_seconds = 300  # 5 minutes

def _get_cache_key(user_id: int, days: int, rule_id: Optional[int], instagram_account_id: Optional[int]) -> str:
    """Generate cache key for analytics query"""
    key_data = f"{user_id}_{days}_{rule_id}_{instagram_account_id}"
    return hashlib.md5(key_data.encode()).hexdigest()

def _get_cached_response(cache_key: str):
    """Get cached response if not expired"""
    if cache_key in _analytics_cache:
        cached_data, timestamp = _analytics_cache[cache_key]
        age = (datetime.utcnow() - timestamp).total_seconds()
        if age < _cache_ttl_seconds:
            return cached_data
        else:
            # Remove expired cache
            del _analytics_cache[cache_key]
    return None

def _set_cached_response(cache_key: str, data: dict):
    """Cache response with timestamp"""
    # Limit cache size to prevent memory issues
    if len(_analytics_cache) > 1000:
        # Remove oldest 100 entries
        sorted_items = sorted(_analytics_cache.items(), key=lambda x: x[1][1])
        for key, _ in sorted_items[:100]:
            del _analytics_cache[key]
    
    _analytics_cache[cache_key] = (data, datetime.utcnow())


def invalidate_analytics_cache_for_user(user_id: int):
    """Remove cached analytics for a user so dashboard shows fresh data after new events."""
    common_days = (7, 14, 30, 90)
    for days in common_days:
        for rule_id in (None,):
            for ig_id in (None,):
                cache_key = _get_cache_key(user_id, days, rule_id, ig_id)
                _analytics_cache.pop(cache_key, None)


def _is_instagram_profile_url(url: str) -> bool:
    u = (url or "").lower()
    if "instagram.com" not in u:
        return False
    rest = u.split("instagram.com")[-1].strip("/").split("?")[0]
    return len(rest) > 0 and "/" not in rest


def _username_from_instagram_url(url: str) -> Optional[str]:
    try:
        p = urlparse(url)
        if "instagram.com" not in (p.netloc or "").lower():
            return None
        path = (p.path or "").strip("/").split("?")[0]
        if not path:
            return None
        return path.split("/")[0]
    except Exception:
        return None


def _user_agent_looks_mobile(ua: Optional[str]) -> bool:
    """Check if user agent indicates mobile device."""
    if not ua:
        return False
    u = ua.lower()
    return any(x in u for x in ("instagram", "iphone", "ipad", "android", "mobile"))


def _instant_deep_link_redirect(deep_link_url: str) -> str:
    """Return minimal HTML that instantly redirects to deep link without showing any content.
    Used for Instagram in-app browser to open native app immediately.
    Uses multiple redirect methods for maximum compatibility and speed."""
    esc_deep = deep_link_url.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    # Ultra-minimal HTML with immediate redirect - execute redirect BEFORE page renders
    # Use window.location.replace() for instant redirect (replaces history, faster than href)
    # Also include meta redirect as fallback
    return (
        f'<!DOCTYPE html><html><head>'
        f'<script>window.location.replace("{esc_deep}");</script>'
        f'<meta http-equiv="refresh" content="0;url={esc_deep}">'
        f'</head><body></body></html>'
    )


def _html_redirect_page(dest_url: str, deep_link_url: Optional[str] = None, label: str = "Instagram") -> str:
    """Return HTML that redirects via JavaScript with smart deep link support.
    For mobile/Instagram in-app browser: uses native app deep link to open Instagram app directly.
    FIXED: Removed meta refresh tag - it was causing redirect through Facebook in Instagram's in-app browser.
    Using JavaScript redirect with immediate execution (IIFE) for reliable redirect."""
    esc_web = dest_url.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    esc_deep = deep_link_url.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;") if deep_link_url else None
    
    # If we have a deep link, use it directly (Instagram in-app browser will handle it)
    if esc_deep:
        return (
            f'<!DOCTYPE html><html><head><meta charset="utf-8">'
            f'<title>Opening Instagram…</title>'
            f'<script type="text/javascript">'
            f'(function(){{'
            f'  // Use native app deep link - opens Instagram app directly'
            f'  window.location.href = "{esc_deep}";'
            f'}})();'
            f'</script>'
            f'</head><body>'
            f'<p>Opening Instagram profile…</p>'
            f'<p><a href="{esc_deep}">Open in Instagram App</a> | <a href="{esc_web}">Open in Browser</a></p>'
            f"</body></html>"
        )
    else:
        # No deep link, use web URL directly
        return (
            f'<!DOCTYPE html><html><head><meta charset="utf-8">'
            f'<title>Opening Instagram…</title>'
            f'<script type="text/javascript">(function(){{window.location.href="{esc_web}";}})();</script>'
            f'</head><body>'
            f'<p>Redirecting to {label}…</p>'
            f'<p><a href="{esc_web}">Click here if you are not redirected</a>.</p>'
            f"</body></html>"
        )


@router.get("/track/redirect")
async def track_link_click(
    request: Request,
    url: str = Query(..., description="Target URL to redirect to"),
    rule_id: Optional[int] = Query(None, description="Automation rule ID"),
    user_id: Optional[int] = Query(None, description="Business owner user ID"),
    media_id: Optional[str] = Query(None, description="Instagram media ID"),
    instagram_account_id: Optional[int] = Query(None, description="Instagram account ID"),
    db: Session = Depends(get_db)
):
    """
    Track link clicks and redirect to target URL.
    For Instagram profile URLs (Visit Profile button): logs PROFILE_VISIT, updates
    automation stats, then redirects. Other links log LINK_CLICKED only.
    """
    try:
        target_url = unquote(url)
        if not target_url.startswith(("http://", "https://")):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid URL format"
            )

        is_profile = _is_instagram_profile_url(target_url)

        if user_id and rule_id:
            if is_profile:
                event = AnalyticsEvent(
                    user_id=user_id,
                    rule_id=rule_id,
                    instagram_account_id=instagram_account_id,
                    media_id=media_id,
                    event_type=EventType.PROFILE_VISIT,
                    event_metadata={
                        "url": target_url,
                        "clicked_at": datetime.utcnow().isoformat(),
                        "source": "visit_profile_button",
                    },
                )
                db.add(event)
                db.commit()
                try:
                    from app.services.lead_capture import update_automation_stats
                    update_automation_stats(rule_id, "profile_visit", db)
                except Exception as su:
                    print(f"⚠️ update_automation_stats(profile_visit) failed: {su}")
                print(f"✅ Tracked profile visit: rule_id={rule_id}, url={target_url[:50]}...")
            else:
                event = AnalyticsEvent(
                    user_id=user_id,
                    rule_id=rule_id,
                    instagram_account_id=instagram_account_id,
                    media_id=media_id,
                    event_type=EventType.LINK_CLICKED,
                    event_metadata={
                        "url": target_url,
                        "clicked_at": datetime.utcnow().isoformat(),
                    },
                )
                db.add(event)
                db.commit()
                print(f"✅ Tracked link click: rule_id={rule_id}, url={target_url[:50]}...")
        else:
            print(f"⚠️ Link click tracking skipped: missing user_id or rule_id")

        redirect_to = target_url
        deep_link_url = None
        
        if is_profile:
            username = _username_from_instagram_url(target_url)
            if username:
                redirect_to = f"https://www.instagram.com/{username}"
                # Generate Instagram native app deep link for mobile devices
                # Format: instagram://user?username={username}
                deep_link_url = f"instagram://user?username={username}"
        
        # Check if user is on mobile/Instagram in-app browser
        user_agent = request.headers.get("user-agent", "").lower()
        is_mobile = _user_agent_looks_mobile(user_agent)
        # For Instagram in-app browser, always use deep link to open native app
        # Check for common Instagram user agent patterns (Instagram, Facebook iOS/Android browsers)
        is_instagram_browser = any(x in user_agent for x in ("instagram", "fbios", "fban", "fbav"))
        
        print(f"🔍 User-Agent: {user_agent[:100]}... | is_mobile: {is_mobile} | is_instagram_browser: {is_instagram_browser}")
        print(f"🔍 Profile URL: {redirect_to} | Deep link: {deep_link_url}")
        
        # FIXED: For Instagram in-app browser or mobile, try direct 302 redirect first, then HTML fallback
        # This opens the native app immediately without showing intermediate page
        if is_profile and redirect_to.startswith("https://www.instagram.com/"):
            # For Instagram in-app browser or mobile: try direct 302 redirect to deep link first
            if (is_instagram_browser or is_mobile) and deep_link_url:
                print(f"✅ Mobile/Instagram browser detected - redirecting to deep link: {deep_link_url}")
                # Try direct 302 redirect first (fastest, no page shown)
                try:
                    return RedirectResponse(url=deep_link_url, status_code=302, headers={
                        "Cache-Control": "no-cache, no-store, must-revalidate",
                        "Pragma": "no-cache",
                        "Expires": "0"
                    })
                except Exception as e:
                    print(f"⚠️ Direct redirect failed, using HTML fallback: {e}")
                    # Fallback to instant HTML redirect if 302 doesn't work
                    html = _instant_deep_link_redirect(deep_link_url)
                    return HTMLResponse(content=html, headers={
                        "Cache-Control": "no-cache, no-store, must-revalidate",
                        "Pragma": "no-cache",
                        "Expires": "0"
                    })
            
            # For desktop: use HTML redirect with web URL
            html = _html_redirect_page(redirect_to, deep_link_url=None, label="Instagram profile")
            return HTMLResponse(content=html)
        return RedirectResponse(url=redirect_to, status_code=302)
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error tracking link click: {str(e)}")
        try:
            target_url = unquote(url)
            return RedirectResponse(url=target_url, status_code=302)
        except Exception:
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
    daily_breakdown: List[dict]  # Daily activity breakdown
    
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
        # Check cache first
        cache_key = _get_cache_key(user_id, days, rule_id, instagram_account_id)
        cached_response = _get_cached_response(cache_key)
        if cached_response:
            return cached_response
        
        # Calculate date range
        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=days)
        
        # Build base query - filter by user_id and date range
        # Exclude events with NULL instagram_account_id (disconnected accounts) to reset analytics to zero
        base_query = db.query(AnalyticsEvent).filter(
            and_(
                AnalyticsEvent.user_id == user_id,
                AnalyticsEvent.created_at >= start_date,
                AnalyticsEvent.created_at <= end_date,
                AnalyticsEvent.instagram_account_id.isnot(None)  # Exclude disconnected account events
            )
        )
        
        # Apply optional filters
        if rule_id:
            base_query = base_query.filter(AnalyticsEvent.rule_id == rule_id)
        
        if instagram_account_id:
            base_query = base_query.filter(AnalyticsEvent.instagram_account_id == instagram_account_id)
        
        # OPTIMIZED: Aggregate all counts in a single query instead of 8 separate queries
        # This reduces database round trips from 8 to 1, significantly improving performance
        from sqlalchemy import case
        counts_query = base_query.with_entities(
            func.sum(case((cast(AnalyticsEvent.event_type, String) == "trigger_matched", 1), else_=0)).label("total_triggers"),
            func.sum(case((cast(AnalyticsEvent.event_type, String) == "dm_sent", 1), else_=0)).label("total_dms_sent"),
            func.sum(case(
                (cast(AnalyticsEvent.event_type, String) == "email_collected", 1),
                (cast(AnalyticsEvent.event_type, String) == "phone_collected", 1),
                else_=0
            )).label("leads_collected"),
            func.sum(case((cast(AnalyticsEvent.event_type, String) == "link_clicked", 1), else_=0)).label("link_clicks"),
            func.sum(case((cast(AnalyticsEvent.event_type, String) == "follow_button_clicked", 1), else_=0)).label("follow_button_clicks"),
            func.sum(case((cast(AnalyticsEvent.event_type, String) == "im_following_clicked", 1), else_=0)).label("im_following_clicks"),
            func.sum(case((cast(AnalyticsEvent.event_type, String) == "profile_visit", 1), else_=0)).label("profile_visits"),
            func.sum(case((cast(AnalyticsEvent.event_type, String) == "comment_replied", 1), else_=0)).label("comment_replies"),
        ).first()
        
        # Extract counts (handle None values)
        total_triggers = int(counts_query.total_triggers or 0)
        total_dms_sent = int(counts_query.total_dms_sent or 0)
        leads_collected = int(counts_query.leads_collected or 0)
        link_clicks = int(counts_query.link_clicks or 0)
        follow_button_clicks = int(counts_query.follow_button_clicks or 0)
        im_following_clicks = int(counts_query.im_following_clicks or 0)
        profile_visits = int(counts_query.profile_visits or 0)
        comment_replies = int(counts_query.comment_replies or 0)
        
        # Get top performing posts/media (grouped by media_id); include account for media fetch
        top_posts_query = base_query.filter(
            AnalyticsEvent.media_id.isnot(None),
            cast(AnalyticsEvent.event_type, String) == "trigger_matched"
        ).with_entities(
            AnalyticsEvent.media_id,
            func.count(AnalyticsEvent.id).label("trigger_count"),
            func.max(AnalyticsEvent.instagram_account_id).label("instagram_account_id")
        ).group_by(
            AnalyticsEvent.media_id
        ).order_by(
            desc("trigger_count")
        ).limit(5)  # Reduced from 10 to 5 for faster response
        
        # OPTIMIZED: Get top posts with aggregated stats in single query
        top_posts_data = top_posts_query.all()
        
        # Get all media IDs upfront
        media_ids = [row[0] for row in top_posts_data]
        
        # OPTIMIZED: Get all media stats in a single aggregated query instead of 2 queries per media
        if media_ids:
            media_stats_query = base_query.filter(
                AnalyticsEvent.media_id.in_(media_ids)
            ).with_entities(
                AnalyticsEvent.media_id,
                func.sum(case(
                    (cast(AnalyticsEvent.event_type, String) == "email_collected", 1),
                    (cast(AnalyticsEvent.event_type, String) == "phone_collected", 1),
                    else_=0
                )).label("leads"),
                func.sum(case((cast(AnalyticsEvent.event_type, String) == "dm_sent", 1), else_=0)).label("dms")
            ).group_by(AnalyticsEvent.media_id)
            
            media_stats = {row[0]: {"leads": int(row[1] or 0), "dms": int(row[2] or 0)} for row in media_stats_query.all()}
        else:
            media_stats = {}
        
        top_posts = []
        # OPTIMIZED: Skip Instagram API calls initially - return data without media_url/permalink
        # Frontend can fetch media URLs separately if needed (non-blocking)
        for row in top_posts_data:
            media_id = row[0]
            trigger_count = row[1]
            instagram_account_id = row[2]
            
            # Get stats from pre-aggregated data
            stats = media_stats.get(media_id, {"leads": 0, "dms": 0})
            media_leads = stats["leads"]
            media_dms = stats["dms"]
            # PERFORMANCE OPTIMIZATION: Skip Instagram API calls to avoid blocking
            # Frontend can fetch media URLs separately if needed (lazy loading)
            # This reduces response time from 5-10 seconds to <500ms
            media_url_val = None
            permalink_val = None
            is_deleted = False
            is_story = False

            # Load-test media IDs (e.g. load_test_media_66_123): use placeholder instead of Instagram API
            if isinstance(media_id, str) and media_id.startswith("load_test_media_"):
                seed = sum(ord(c) for c in media_id) % 10000
                media_url_val = f"https://picsum.photos/400/400?seed={seed}"
                permalink_val = "https://www.instagram.com/"
            
            # Fetch media URLs from Instagram API (enabled for top posts)
            # Limited to top 5 posts to maintain performance
            else:
                try:
                    acc = db.query(InstagramAccount).filter(
                        InstagramAccount.id == instagram_account_id,
                        InstagramAccount.user_id == user_id
                    ).first()
                    if acc:
                        tok = None
                        if acc.encrypted_page_token:
                            tok = decrypt_credentials(acc.encrypted_page_token)
                        elif acc.encrypted_credentials:
                            tok = decrypt_credentials(acc.encrypted_credentials)
                        if tok:
                            # Use shorter timeout (5s) to prevent blocking
                            r = requests.get(
                                f"https://graph.instagram.com/v21.0/{media_id}",
                                params={"fields": "media_type,media_url,thumbnail_url,permalink,media_product_type", "access_token": tok},
                                timeout=5
                            )
                            if r.status_code == 200:
                                d = r.json()
                                media_url_val = d.get("thumbnail_url") or d.get("media_url")
                                permalink_val = d.get("permalink")
                                # Check if this is a story
                                if d.get("media_product_type") == "STORY":
                                    is_story = True
                            else:
                                error_data = r.json() if r.content else {}
                                error_message = (error_data.get("error") or {}).get("message", "") or r.text[:200]
                                
                                # Check if this might be a story by checking rules
                                try:
                                    from app.models.automation_rule import AutomationRule
                                    story_rules = db.query(AutomationRule).filter(
                                        AutomationRule.instagram_account_id == instagram_account_id,
                                        AutomationRule.media_id == media_id,
                                        AutomationRule.is_active == True
                                    ).all()
                                    
                                    # Check rule names/config to detect if it's a story rule
                                    for rule in story_rules:
                                        rule_name_lower = (rule.name or "").lower()
                                        if "story" in rule_name_lower:
                                            is_story = True
                                            break
                                except:
                                    pass
                                
                                # If media doesn't exist (deleted by user or expired), mark as deleted
                                # This applies to both stories and posts/reels
                                if "does not exist" in error_message.lower() or "cannot be loaded" in error_message.lower():
                                    is_deleted = True
                                    if is_story:
                                        print(f"⚠️ Story {media_id} expired (24h) or deleted; excluding from Top Performing, auto-disabling rules.")
                                    else:
                                        print(f"⚠️ Media {media_id} deleted from Instagram; excluding from Top Performing, auto-disabling rules.")
                                else:
                                    # Log but don't fail - continue without media URL
                                    print(f"⚠️ Failed to fetch media info for {media_id}: {r.status_code} - {error_message[:100]}")
                except requests.Timeout:
                    # Timeout - continue without media URL (non-blocking)
                    print(f"⚠️ Timeout fetching media info for {media_id} - continuing without preview")
                except Exception as e:
                    # Any other error - continue without media URL (non-blocking)
                    print(f"⚠️ Exception fetching media info for {media_id}: {str(e)[:100]}")
            
            # If media was deleted/expired: disable rules and exclude from Top Performing.
            # Note: Analytics counts (totals) still include events from deleted/expired media.
            if is_deleted:
                try:
                    from app.models.automation_rule import AutomationRule
                    deleted_rules = db.query(AutomationRule).filter(
                        AutomationRule.instagram_account_id == instagram_account_id,
                        AutomationRule.media_id == media_id,
                        AutomationRule.is_active == True
                    ).all()
                    for rule in deleted_rules:
                        print(f"⚠️ Auto-disabling rule '{rule.name}' (ID: {rule.id}) - media {media_id} deleted/expired")
                        rule.is_active = False
                    if deleted_rules:
                        db.commit()
                        print(f"✅ Auto-disabled {len(deleted_rules)} rule(s) for deleted/expired media {media_id}")
                except Exception as disable_err:
                    print(f"⚠️ Error auto-disabling rules: {str(disable_err)}")
                    db.rollback()
                continue  # Skip this media – do not add to top_posts (exclude from Top Performing)
            
            # Only add entry if media still exists (can be fetched)
            entry = {
                "media_id": media_id,
                "trigger_count": trigger_count,
                "leads_count": media_leads,
                "dms_count": media_dms
            }
            if media_url_val:
                entry["media_url"] = media_url_val
            if permalink_val:
                entry["permalink"] = permalink_val
            if is_story:
                entry["media_type"] = "STORY"  # Mark as story for frontend display
            top_posts.append(entry)
        
        # OPTIMIZED: Calculate daily breakdown in a single aggregated query instead of 3 queries per day
        # This reduces queries from 21 (for 7 days) to 1
        daily_breakdown_query = base_query.with_entities(
            func.date(AnalyticsEvent.created_at).label("date"),
            func.sum(case((cast(AnalyticsEvent.event_type, String) == "trigger_matched", 1), else_=0)).label("triggers"),
            func.sum(case((cast(AnalyticsEvent.event_type, String) == "dm_sent", 1), else_=0)).label("dms_sent"),
            func.sum(case(
                (cast(AnalyticsEvent.event_type, String) == "email_collected", 1),
                (cast(AnalyticsEvent.event_type, String) == "phone_collected", 1),
                else_=0
            )).label("leads")
        ).group_by(func.date(AnalyticsEvent.created_at))
        
        # Create a map of date -> stats
        # Use ISO date string (YYYY-MM-DD) as key for reliable lookup across DB drivers
        # (PostgreSQL returns date, SQLite may return string; normalizing avoids mismatch)
        def _date_key(d):
            if d is None:
                return None
            if hasattr(d, "isoformat"):
                return d.isoformat()
            return str(d)[:10]  # "YYYY-MM-DD"

        daily_stats_map = {}
        for row in daily_breakdown_query.all():
            key = _date_key(row.date)
            if key:
                daily_stats_map[key] = {
                    "triggers": int(row.triggers or 0),
                    "dms_sent": int(row.dms_sent or 0),
                    "leads": int(row.leads or 0)
                }

        # Build daily breakdown array (fill in missing days with zeros)
        # Use calendar days ending with TODAY so the graph shows up to the current date (not yesterday)
        # e.g. "Last 7 days" = today + 6 previous days (7 buckets including today)
        daily_breakdown = []
        end_day = end_date.date()  # today in UTC
        first_day = end_day - timedelta(days=days - 1)
        for i in range(days):
            day_date = first_day + timedelta(days=i)
            lookup_key = _date_key(day_date)

            stats = daily_stats_map.get(lookup_key, {"triggers": 0, "dms_sent": 0, "leads": 0})
            day_triggers = stats["triggers"]
            day_dms = stats["dms_sent"]
            day_leads = stats["leads"]
            
            # Format date for display (day_date is a date object)
            date_str = day_date.strftime('%b %d')
            date_label = day_date.strftime('%m/%d')
            
            daily_breakdown.append({
                "date": date_str,  # "Jan 18"
                "date_label": date_label,  # "01/18"
                "triggers": day_triggers,
                "dms_sent": day_dms,
                "leads": day_leads,
                "total": day_triggers + day_dms + day_leads  # Total activity for the day
            })
        
        response = AnalyticsSummary(
            total_triggers=total_triggers,
            total_dms_sent=total_dms_sent,
            leads_collected=leads_collected,
            link_clicks=link_clicks,
            follow_button_clicks=follow_button_clicks,
            im_following_clicks=im_following_clicks,
            profile_visits=profile_visits,
            comment_replies=comment_replies,
            top_posts=top_posts,
            daily_breakdown=daily_breakdown
        )
        
        # Cache response
        _set_cached_response(cache_key, response)
        
        return response
        
    except HTTPException:
        # Re-raise HTTP exceptions (like 401, 404) as-is
        raise
    except Exception as e:
        print(f"❌ Error fetching analytics: {str(e)}")
        import traceback
        traceback.print_exc()
        # Return empty analytics data instead of raising 500 error
        # This prevents network errors for new users or users with no data
        return AnalyticsSummary(
            total_triggers=0,
            total_dms_sent=0,
            leads_collected=0,
            link_clicks=0,
            follow_button_clicks=0,
            im_following_clicks=0,
            profile_visits=0,
            comment_replies=0,
            top_posts=[],
            daily_breakdown=[]
        )


class MediaAnalytics(BaseModel):
    """Analytics for a specific media item."""
    media_id: str
    rule_id: Optional[int]
    rule_name: Optional[str]
    is_active: bool
    triggers: int  # RUNS
    dms_sent: int
    leads_collected: int
    total_clicks: int  # All button/link clicks combined
    follow_button_clicks: int
    profile_visits: int
    im_following_clicks: int
    comment_replies: int
    last_modified: Optional[str]
    
    class Config:
        from_attributes = True


@router.get("/media", response_model=List[MediaAnalytics])
def get_media_analytics(
    days: int = Query(30, ge=1, le=90, description="Number of days to analyze"),
    instagram_account_id: Optional[int] = Query(None, description="Filter by Instagram account"),
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id)
):
    """
    Get analytics for each media item (post/reel/story/live).
    Returns analytics grouped by media_id with rule information.
    OPTIMIZED: Uses aggregated queries and caching for performance.
    """
    try:
        # Check cache first
        cache_key = f"media_analytics_{user_id}_{days}_{instagram_account_id}"
        cached_response = _get_cached_response(cache_key)
        if cached_response:
            return cached_response
        
        from app.models.automation_rule import AutomationRule
        
        # Calculate date range
        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=days)
        
        # OPTIMIZED: Get account IDs first, then filter rules
        if instagram_account_id:
            account_ids = [instagram_account_id]
            # Verify account belongs to user
            account = db.query(InstagramAccount).filter(
                InstagramAccount.id == instagram_account_id,
                InstagramAccount.user_id == user_id
            ).first()
            if not account:
                return []
        else:
            # Get all account IDs for user
            account_ids = [acc.id for acc in db.query(InstagramAccount.id).filter(
                InstagramAccount.user_id == user_id
            ).all()]
            if not account_ids:
                return []
        
        # OPTIMIZED: Get rules filtered by account IDs (more efficient than join)
        rules_query = db.query(AutomationRule).filter(
            AutomationRule.instagram_account_id.in_(account_ids),
            AutomationRule.deleted_at.is_(None)
        )
        
        rules = rules_query.all()
        
        # OPTIMIZED: Group rules by media_id (only process rules with media_id)
        media_rules_map: dict[str, list[AutomationRule]] = {}
        media_ids_set = set()
        for rule in rules:
            # Get media_id from rule.media_id or from config
            media_id = rule.media_id
            if not media_id and isinstance(rule.config, dict):
                media_id = rule.config.get("media_id")
            
            if media_id:
                media_id_str = str(media_id)  # Ensure it's a string
                if media_id_str not in media_rules_map:
                    media_rules_map[media_id_str] = []
                media_rules_map[media_id_str].append(rule)
                media_ids_set.add(media_id_str)
        
        if not media_ids_set:
            return []
        
        # OPTIMIZED: Get all rule IDs upfront
        all_rule_ids = []
        for rules_list in media_rules_map.values():
            all_rule_ids.extend([r.id for r in rules_list])
        
        # OPTIMIZED: Get all analytics for all media_ids in a single aggregated query
        # This reduces queries from N*8 (N media items × 8 event types) to 1 query
        # FIXED: Match by media_id regardless of rule_id - analytics events may have NULL rule_id
        # or rule_id that doesn't match current rules (e.g., if rule was deleted/updated)
        analytics_base = db.query(AnalyticsEvent).filter(
            and_(
                AnalyticsEvent.user_id == user_id,
                AnalyticsEvent.created_at >= start_date,
                AnalyticsEvent.created_at <= end_date,
                AnalyticsEvent.media_id.in_(list(media_ids_set)),
                # Match by media_id - rule_id can be NULL or any value (don't filter by rule_id)
                # This ensures we get all analytics for the media, even if rule_id is NULL or doesn't match
                AnalyticsEvent.instagram_account_id.in_(account_ids),
                AnalyticsEvent.instagram_account_id.isnot(None)
            )
        )
        
        # Aggregate stats per media_id in single query
        aggregated_stats = analytics_base.with_entities(
            AnalyticsEvent.media_id,
            func.sum(case((cast(AnalyticsEvent.event_type, String) == "trigger_matched", 1), else_=0)).label("triggers"),
            func.sum(case((cast(AnalyticsEvent.event_type, String) == "dm_sent", 1), else_=0)).label("dms_sent"),
            func.sum(case(
                (cast(AnalyticsEvent.event_type, String) == "email_collected", 1),
                (cast(AnalyticsEvent.event_type, String) == "phone_collected", 1),
                else_=0
            )).label("leads_collected"),
            func.sum(case((cast(AnalyticsEvent.event_type, String) == "link_clicked", 1), else_=0)).label("link_clicks"),
            func.sum(case((cast(AnalyticsEvent.event_type, String) == "follow_button_clicked", 1), else_=0)).label("follow_button_clicks"),
            func.sum(case((cast(AnalyticsEvent.event_type, String) == "profile_visit", 1), else_=0)).label("profile_visits"),
            func.sum(case((cast(AnalyticsEvent.event_type, String) == "im_following_clicked", 1), else_=0)).label("im_following_clicks"),
            func.sum(case((cast(AnalyticsEvent.event_type, String) == "comment_replied", 1), else_=0)).label("comment_replies"),
        ).group_by(AnalyticsEvent.media_id).all()
        
        # Create stats map for O(1) lookup
        # FIXED: Ensure media_id keys are strings for consistent lookup
        stats_map = {str(row[0]): {
            "triggers": int(row[1] or 0),
            "dms_sent": int(row[2] or 0),
            "leads_collected": int(row[3] or 0),
            "link_clicks": int(row[4] or 0),
            "follow_button_clicks": int(row[5] or 0),
            "profile_visits": int(row[6] or 0),
            "im_following_clicks": int(row[7] or 0),
            "comment_replies": int(row[8] or 0),
        } for row in aggregated_stats}
        
        # Build results using pre-aggregated stats
        results = []
        for media_id, rules_list in media_rules_map.items():
            # Get the active rule (or first rule if none active)
            active_rule = next((r for r in rules_list if r.is_active), rules_list[0] if rules_list else None)
            if not active_rule:
                continue
            
            # OPTIMIZED: Get stats from pre-aggregated map instead of individual queries
            # FIXED: Ensure media_id is string for consistent lookup (media_id from rules is string)
            media_id_str = str(media_id)
            stats = stats_map.get(media_id_str, {
                "triggers": 0,
                "dms_sent": 0,
                "leads_collected": 0,
                "link_clicks": 0,
                "follow_button_clicks": 0,
                "profile_visits": 0,
                "im_following_clicks": 0,
                "comment_replies": 0,
            })
            
            triggers = stats["triggers"]
            dms_sent = stats["dms_sent"]
            leads_collected = stats["leads_collected"]
            follow_button_clicks = stats["follow_button_clicks"]
            profile_visits = stats["profile_visits"]
            im_following_clicks = stats["im_following_clicks"]
            link_clicks = stats["link_clicks"]
            comment_replies = stats["comment_replies"]
            
            total_clicks = follow_button_clicks + profile_visits + im_following_clicks + link_clicks
            
            # Get last modified date from rule (use created_at since there's no updated_at field)
            last_modified = active_rule.created_at.isoformat() if active_rule.created_at else None
            
            results.append(MediaAnalytics(
                media_id=media_id_str,
                rule_id=active_rule.id,
                rule_name=active_rule.name,
                is_active=active_rule.is_active,
                triggers=triggers,
                dms_sent=dms_sent,
                leads_collected=leads_collected,
                total_clicks=total_clicks,
                follow_button_clicks=follow_button_clicks,
                profile_visits=profile_visits,
                im_following_clicks=im_following_clicks,
                comment_replies=comment_replies,
                last_modified=last_modified
            ))
        
        # Sort by triggers (runs) descending
        results.sort(key=lambda x: x.triggers, reverse=True)
        
        # Cache response
        _set_cached_response(cache_key, results)
        
        return results
        
    except Exception as e:
        print(f"❌ Error fetching media analytics: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch media analytics: {str(e)}"
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
