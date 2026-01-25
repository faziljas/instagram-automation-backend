"""
Service for managing persistent Instagram account usage tracking.
Handles usage limits and resets for Free (lifetime) and Pro (monthly) tiers.
"""
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from app.models.instagram_global_tracker import InstagramGlobalTracker
from app.models.user import User
from app.core.plan_limits import FREE_DM_LIMIT, PRO_DM_LIMIT, FREE_RULE_LIMIT, PRO_RULE_LIMIT


def get_or_create_tracker(user_id: int, instagram_id: str, db: Session) -> InstagramGlobalTracker:
    """
    Get or create an InstagramGlobalTracker for the given (user_id, IGSID) combination.
    If tracker exists, return it. If not, create a new one.
    """
    if not instagram_id:
        raise ValueError("instagram_id (IGSID) is required")
    if not user_id:
        raise ValueError("user_id is required")
    
    tracker = db.query(InstagramGlobalTracker).filter(
        InstagramGlobalTracker.user_id == user_id,
        InstagramGlobalTracker.instagram_id == instagram_id
    ).first()
    
    if not tracker:
        tracker = InstagramGlobalTracker(
            user_id=user_id,
            instagram_id=instagram_id,
            dms_sent_count=0,
            rules_created_count=0,
            last_reset_date=datetime.utcnow()
        )
        db.add(tracker)
        db.commit()
        db.refresh(tracker)
        print(f"✅ Created new InstagramGlobalTracker for user {user_id}, IGSID: {instagram_id}")
    
    return tracker


def check_and_reset_usage(tracker: InstagramGlobalTracker, subscription_plan: str, db: Session) -> None:
    """
    Smart reset service: Check if usage should be reset based on plan tier.
    
    Logic:
    - FREE tier: Do NOTHING (limits never reset - lifetime caps)
    - PRO tier: Reset monthly (every 30 days from last_reset_date)
    
    Args:
        tracker: InstagramGlobalTracker instance
        subscription_plan: User's plan tier ("free", "pro", "enterprise", etc.)
        db: Database session
    """
    # FREE tier: Never reset (lifetime limits)
    if subscription_plan == "free":
        return
    
    # PRO/ENTERPRISE tier: Reset monthly (every 30 days)
    if subscription_plan in ["pro", "enterprise"]:
        now = datetime.utcnow()
        days_since_reset = (now - tracker.last_reset_date).days
        
        if days_since_reset >= 30:
            # Reset counts and update reset date
            tracker.dms_sent_count = 0
            tracker.rules_created_count = 0
            tracker.last_reset_date = now
            db.commit()
            print(f"✅ Monthly usage reset for Pro account IGSID {tracker.instagram_id}. "
                  f"Days since last reset: {days_since_reset}")


def check_rule_limit(tracker: InstagramGlobalTracker, subscription_plan: str) -> tuple[bool, str]:
    """
    Check if the Instagram account can create another automation rule.
    
    Returns:
        (is_allowed, error_message)
        - is_allowed: True if limit not reached, False otherwise
        - error_message: Error message if limit reached, empty string otherwise
    """
    # Determine limit based on plan
    if subscription_plan in ["pro", "enterprise"]:
        limit = PRO_RULE_LIMIT
        limit_type = "monthly"
    else:
        limit = FREE_RULE_LIMIT
        limit_type = "lifetime"
    
    # Check if limit reached
    if tracker.rules_created_count >= limit:
        return False, (
            f"Automation rule limit reached. This Instagram account has created {tracker.rules_created_count} "
            f"rules ({limit_type} limit: {limit}). Upgrade to Pro to create more rules."
        )
    
    return True, ""


def check_dm_limit(tracker: InstagramGlobalTracker, subscription_plan: str) -> tuple[bool, str]:
    """
    Check if the Instagram account can send another DM.
    
    Returns:
        (is_allowed, error_message)
        - is_allowed: True if limit not reached, False otherwise
        - error_message: Error message if limit reached, empty string otherwise
    """
    # Determine limit based on plan
    if subscription_plan in ["pro", "enterprise"]:
        limit = PRO_DM_LIMIT
        limit_type = "monthly"
    else:
        limit = FREE_DM_LIMIT
        limit_type = "lifetime"
    
    # Check if limit reached
    if tracker.dms_sent_count >= limit:
        return False, (
            f"DM limit reached. This Instagram account has sent {tracker.dms_sent_count} "
            f"DMs ({limit_type} limit: {limit}). Upgrade to Pro to send more DMs."
        )
    
    return True, ""


def increment_rule_count(tracker: InstagramGlobalTracker, db: Session) -> None:
    """Increment the rules_created_count for the tracker."""
    tracker.rules_created_count += 1
    db.commit()
    print(f"✅ Incremented rule count for IGSID {tracker.instagram_id}: {tracker.rules_created_count}")


def increment_dm_count(tracker: InstagramGlobalTracker, db: Session) -> None:
    """Increment the dms_sent_count for the tracker."""
    tracker.dms_sent_count += 1
    db.commit()
    print(f"✅ Incremented DM count for IGSID {tracker.instagram_id}: {tracker.dms_sent_count}")


def reset_tracker_for_new_user(tracker: InstagramGlobalTracker, db: Session) -> None:
    """
    Reset tracker counts when a new user connects a previously used Instagram account.
    This ensures new users get a fresh start even if the Instagram account was used before.
    """
    tracker.dms_sent_count = 0
    tracker.rules_created_count = 0
    tracker.last_reset_date = datetime.utcnow()
    db.commit()
    print(f"✅ Reset tracker for new user connection - IGSID {tracker.instagram_id}")
