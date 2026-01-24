"""
Analytics API routes for tracking automation performance.
"""
from typing import Optional, List
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Header, Query, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, desc
from urllib.parse import unquote, urlencode, urlparse
from app.db.session import get_db
from app.models.analytics_event import AnalyticsEvent, EventType
from app.models.automation_rule import AutomationRule
from app.models.instagram_account import InstagramAccount
from app.utils.auth import verify_token
from app.utils.encryption import decrypt_credentials
from pydantic import BaseModel
import requests

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
    if not ua:
        return False
    u = ua.lower()
    return any(x in u for x in ("instagram", "iphone", "ipad", "android", "mobile"))


def _html_redirect_page(dest_url: str, label: str = "Instagram", deep_link_url: str = None) -> str:
    """Return HTML that redirects via meta refresh + fallback link.
    Works better than 302 in Instagram in-app browser (avoids empty screen).
    FIXED: Always redirect to web URL first to avoid Facebook redirect issues.
    Deep link is provided as optional fallback link only."""
    esc = dest_url.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    deep_link_esc = deep_link_url.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;") if deep_link_url else None
    
    # FIXED: Always redirect to web URL (Instagram.com) first
    # This prevents the issue where deep link redirects to Facebook page
    # Deep link is provided as an optional link for users who want to open in app
    html_content = (
        f'<!DOCTYPE html><html><head><meta charset="utf-8">'
        f'<title>Opening Instagram…</title>'
        f'<meta http-equiv="refresh" content="0;url={esc}">'
        f'</head><body>'
        f'<p>Redirecting to {label}…</p>'
        f'<p><a href="{esc}">Click here if you are not redirected</a>.</p>'
    )
    
    # Add deep link as optional link (not auto-redirect)
    if deep_link_esc:
        html_content += f'<p><a href="{deep_link_esc}">Open in Instagram app</a></p>'
    
    html_content += f"</body></html>"
    return html_content


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
                # Generate Instagram deep link for mobile native app
                # Format: instagram://user?username={username}
                deep_link_url = f"instagram://user?username={username}"

        # Use HTML redirect page instead of 302 for profile links. Instagram in-app
        # browser often ignores 302 and shows empty screen; meta refresh + link works.
        # For mobile, try deep link first to open in native Instagram app.
        if is_profile and redirect_to.startswith("https://www.instagram.com/"):
            html = _html_redirect_page(redirect_to, "Instagram profile", deep_link_url)
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
        # IMPORTANT: These counts include ALL events (posts, reels, stories) regardless of whether
        # the media still exists. Deleted/expired stories are still counted based on date filters.
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
        
        # Get top performing posts/media (grouped by media_id); include account for media fetch
        top_posts_query = base_query.filter(
            AnalyticsEvent.media_id.isnot(None),
            AnalyticsEvent.event_type == EventType.TRIGGER_MATCHED
        ).with_entities(
            AnalyticsEvent.media_id,
            func.count(AnalyticsEvent.id).label("trigger_count"),
            func.max(AnalyticsEvent.instagram_account_id).label("instagram_account_id")
        ).group_by(
            AnalyticsEvent.media_id
        ).order_by(
            desc("trigger_count")
        ).limit(10)
        
        top_posts = []
        for row in top_posts_query.all():
            media_id = row[0]
            trigger_count = row[1]
            instagram_account_id = row[2]
            # Get additional stats for this media
            media_leads = base_query.filter(
                AnalyticsEvent.media_id == media_id,
                AnalyticsEvent.event_type == EventType.EMAIL_COLLECTED
            ).count()
            media_dms = base_query.filter(
                AnalyticsEvent.media_id == media_id,
                AnalyticsEvent.event_type == EventType.DM_SENT
            ).count()
            # Fetch media from Instagram API. Only show in Top Performing if media still exists.
            # Stories expire after 24h or can be deleted by user - exclude them if they can't be fetched.
            # Analytics counts (totals) always include all events regardless of media existence.
            media_url_val = None
            permalink_val = None
            is_deleted = False
            is_story = False
            
            if instagram_account_id:
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
                            r = requests.get(
                                f"https://graph.instagram.com/v21.0/{media_id}",
                                params={"fields": "media_type,media_url,thumbnail_url,permalink,media_product_type", "access_token": tok},
                                timeout=10
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
                                    print(f"⚠️ Failed to fetch media info for {media_id}: {r.status_code} - {error_message}")
                except Exception as e:
                    print(f"⚠️ Exception fetching media info for {media_id}: {str(e)}")
            
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
        
        # Calculate daily breakdown for activity chart
        daily_breakdown = []
        for i in range(days):
            day_start = start_date + timedelta(days=i)
            day_end = day_start + timedelta(days=1)
            
            # Count events for this day
            day_query = base_query.filter(
                AnalyticsEvent.created_at >= day_start,
                AnalyticsEvent.created_at < day_end
            )
            
            day_triggers = day_query.filter(
                AnalyticsEvent.event_type == EventType.TRIGGER_MATCHED
            ).count()
            
            day_dms = day_query.filter(
                AnalyticsEvent.event_type == EventType.DM_SENT
            ).count()
            
            day_leads = day_query.filter(
                AnalyticsEvent.event_type == EventType.EMAIL_COLLECTED
            ).count()
            
            # Format date for display
            date_str = day_start.strftime('%b %d')
            date_label = day_start.strftime('%m/%d')
            
            daily_breakdown.append({
                "date": date_str,  # "Jan 18"
                "date_label": date_label,  # "01/18"
                "triggers": day_triggers,
                "dms_sent": day_dms,
                "leads": day_leads,
                "total": day_triggers + day_dms + day_leads  # Total activity for the day
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
            top_posts=top_posts,
            daily_breakdown=daily_breakdown
        )
        
    except Exception as e:
        print(f"❌ Error fetching analytics: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch analytics: {str(e)}"
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
    """
    try:
        from app.models.automation_rule import AutomationRule
        
        # Calculate date range
        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=days)
        
        # Get all rules for this user (and optionally filtered by account)
        # Join with InstagramAccount to filter by user_id
        rules_query = db.query(AutomationRule).join(
            InstagramAccount,
            AutomationRule.instagram_account_id == InstagramAccount.id
        ).filter(
            InstagramAccount.user_id == user_id
        )
        
        if instagram_account_id:
            rules_query = rules_query.filter(
                AutomationRule.instagram_account_id == instagram_account_id
            )
        
        rules = rules_query.all()
        
        # Group rules by media_id
        media_rules_map: dict[str, list[AutomationRule]] = {}
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
        
        # Get analytics for each media_id
        results = []
        for media_id, rules_list in media_rules_map.items():
            # Get the active rule (or first rule if none active)
            active_rule = next((r for r in rules_list if r.is_active), rules_list[0] if rules_list else None)
            if not active_rule:
                continue
            
            # Get all rule_ids for this media
            rule_ids = [r.id for r in rules_list]
            
            # Query analytics events for these rules and this media
            base_query = db.query(AnalyticsEvent).filter(
                and_(
                    AnalyticsEvent.user_id == user_id,
                    AnalyticsEvent.media_id == media_id,
                    AnalyticsEvent.created_at >= start_date,
                    AnalyticsEvent.created_at <= end_date,
                    AnalyticsEvent.rule_id.in_(rule_ids)
                )
            )
            
            # Count events
            triggers = base_query.filter(
                AnalyticsEvent.event_type == EventType.TRIGGER_MATCHED
            ).count()
            
            dms_sent = base_query.filter(
                AnalyticsEvent.event_type == EventType.DM_SENT
            ).count()
            
            leads_collected = base_query.filter(
                AnalyticsEvent.event_type == EventType.EMAIL_COLLECTED
            ).count()
            
            follow_button_clicks = base_query.filter(
                AnalyticsEvent.event_type == EventType.FOLLOW_BUTTON_CLICKED
            ).count()
            
            profile_visits = base_query.filter(
                AnalyticsEvent.event_type == EventType.PROFILE_VISIT
            ).count()
            
            im_following_clicks = base_query.filter(
                AnalyticsEvent.event_type == EventType.IM_FOLLOWING_CLICKED
            ).count()
            
            link_clicks = base_query.filter(
                AnalyticsEvent.event_type == EventType.LINK_CLICKED
            ).count()
            
            comment_replies = base_query.filter(
                AnalyticsEvent.event_type == EventType.COMMENT_REPLIED
            ).count()
            
            total_clicks = follow_button_clicks + profile_visits + im_following_clicks + link_clicks
            
            # Get last modified date from rule (use created_at since there's no updated_at field)
            last_modified = active_rule.created_at.isoformat() if active_rule.created_at else None
            
            results.append(MediaAnalytics(
                media_id=media_id,
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
