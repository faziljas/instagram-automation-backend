"""
Service for managing persistent Instagram account usage tracking.
Handles usage limits and resets for Free (lifetime) and Pro (monthly) tiers.
"""
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from app.models.instagram_global_tracker import InstagramGlobalTracker
from app.models.user import User
from app.models.instagram_account import InstagramAccount
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
    
    try:
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
    except Exception as e:
        # Check if error is due to missing user_id column (migration not run)
        error_msg = str(e).lower()
        if "column" in error_msg and "user_id" in error_msg:
            raise ValueError(
                f"Database migration not completed. The instagram_global_trackers table "
                f"does not have the user_id column. Please restart the server to run migrations."
            ) from e
        raise


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
    
    # If limit is -1, unlimited rules allowed (High Volume pricing for free tier)
    if limit == -1:
        return True, ""
    
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
    
    # If limit is -1, unlimited DMs allowed (Pro tier with High Volume pricing)
    if limit == -1:
        return True, ""
    
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


def reset_tracker_for_pro_upgrade(user_id: int, db: Session) -> None:
    """
    Reset all InstagramGlobalTracker instances for a user when they upgrade to Pro.
    This resets usage quotas for all Instagram accounts linked to the user.
    
    Args:
        user_id: The user ID whose trackers should be reset
        db: Database session
    """
    # Query all InstagramAccount rows linked to the user_id
    user_accounts = db.query(InstagramAccount).filter(
        InstagramAccount.user_id == user_id,
        InstagramAccount.igsid.isnot(None)
    ).all()
    
    if not user_accounts:
        print(f"ℹ️ No Instagram accounts found for user {user_id} to reset trackers")
        return
    
    reset_count = 0
    # For each account, find the corresponding InstagramGlobalTracker by instagram_id
    for account in user_accounts:
        if account.igsid:
            try:
                tracker = db.query(InstagramGlobalTracker).filter(
                    InstagramGlobalTracker.user_id == user_id,
                    InstagramGlobalTracker.instagram_id == account.igsid
                ).first()
                
                if tracker:
                    # Reset dms_sent_count and rules_created_count to 0
                    tracker.dms_sent_count = 0
                    tracker.rules_created_count = 0
                    # Update last_reset_date to datetime.utcnow()
                    tracker.last_reset_date = datetime.utcnow()
                    reset_count += 1
                    print(f"✅ Reset tracker for Pro upgrade - User {user_id}, IGSID {account.igsid}")
                else:
                    print(f"ℹ️ No tracker found for User {user_id}, IGSID {account.igsid} - will be created on first use")
            except Exception as e:
                print(f"⚠️ Failed to reset tracker for User {user_id}, IGSID {account.igsid}: {str(e)}")
    
    # Commit the changes
    db.commit()
    print(f"✅ Reset {reset_count} tracker(s) for Pro upgrade - User {user_id}")
