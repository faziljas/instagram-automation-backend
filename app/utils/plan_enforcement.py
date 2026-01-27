from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from fastapi import HTTPException, status
from app.models.user import User
from app.models.instagram_account import InstagramAccount
from app.models.dm_log import DmLog
from app.models.automation_rule import AutomationRule
from app.models.subscription import Subscription
from app.core.plan_limits import get_plan_limit
from app.services.instagram_usage_tracker import (
    get_or_create_tracker,
    check_and_reset_usage,
    check_dm_limit as check_global_dm_limit,
    check_rule_limit as check_global_rule_limit,
    increment_dm_count
)


def get_billing_cycle_start(user_id: int, db: Session) -> datetime:
    """
    Get the billing cycle start date for Pro/Enterprise users.
    For Pro users: Returns billing_cycle_start_date from subscription (30-day cycle from upgrade)
    For Free/Basic users: 
      - If they have a billing_cycle_start_date from previous Pro subscription and are still within
        the paid 30-day period, continue using Pro cycle logic (they paid for 30 days)
      - Otherwise, use calendar month
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    subscription = db.query(Subscription).filter(
        Subscription.user_id == user_id
    ).first()
    
    # Check if user has a billing cycle start date (from Pro subscription)
    if subscription and subscription.billing_cycle_start_date:
        cycle_start = subscription.billing_cycle_start_date
        now = datetime.utcnow()
        
        # Calculate how many days since the original Pro cycle started
        days_since_start = (now - cycle_start).days
        
        # For Pro/Enterprise users, use Pro cycle logic (30-day cycles)
        if user.plan_tier in ["pro", "enterprise"]:
            # Find the most recent cycle start date (every 30 days)
            cycles_passed = days_since_start // 30
            current_cycle_start = cycle_start + timedelta(days=cycles_passed * 30)
            return current_cycle_start
        
        # For Free/Basic users: Check if still within their current Pro cycle period
        # User paid for Pro subscription, so they should get Pro benefits until current cycle ends
        # Calculate which cycle they're in and when it ends
        cycles_passed = days_since_start // 30
        current_cycle_start = cycle_start + timedelta(days=cycles_passed * 30)
        current_cycle_end = current_cycle_start + timedelta(days=30)
        
        # If still within the current Pro cycle (before cycle end), use Pro cycle logic
        if now < current_cycle_end:
            # Still within paid Pro cycle - use Pro cycle logic
            return current_cycle_start
        else:
            # Current Pro cycle has ended - switch to Free calendar month
            # Clear the billing_cycle_start_date since it's no longer needed
            subscription.billing_cycle_start_date = None
            db.commit()
            return datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    # For Free/Basic users without Pro cycle history, use calendar month
    return datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def check_account_limit(user_id: int, db: Session) -> bool:
    """
    Check if user can add another Instagram account based on their plan.
    Raises HTTPException if limit exceeded.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    max_accounts = get_plan_limit(user.plan_tier, "max_accounts")
    current_accounts = db.query(InstagramAccount).filter(
        InstagramAccount.user_id == user_id
    ).count()

    if current_accounts >= max_accounts:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Account limit reached. Your {user.plan_tier} plan allows {max_accounts} account(s). Upgrade to add more."
        )

    return True


def check_rule_limit(user_id: int, db: Session, instagram_account_id: int = None) -> bool:
    """
    Check if user can create another automation rule based on their plan.
    Uses persistent InstagramGlobalTracker to track total rules created (even if deleted).
    This ensures limits persist across disconnect/reconnect.
    
    For Free tier: Checks lifetime limit (total rules ever created for this Instagram account)
    For Pro/Enterprise: Checks monthly limit (resets every 30 days)
    
    Raises HTTPException if limit exceeded.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    max_rules = get_plan_limit(user.plan_tier, "max_automation_rules")
    
    # If max_rules is -1, unlimited rules allowed (High Volume pricing for free tier)
    if max_rules == -1:
        return True
    
    # If instagram_account_id is provided, check limit for that specific account
    if instagram_account_id:
        account = db.query(InstagramAccount).filter(
            InstagramAccount.id == instagram_account_id,
            InstagramAccount.user_id == user_id
        ).first()
        
        if not account:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Instagram account not found"
            )
        
        # Use persistent global tracker for this Instagram account (IGSID)
        if account.igsid:
            tracker = get_or_create_tracker(user_id, account.igsid, db)
            check_and_reset_usage(tracker, user.plan_tier, db)
            
            # Check global tracker limit (persistent across disconnect/reconnect)
            is_allowed, error_message = check_global_rule_limit(tracker, user.plan_tier)
            if not is_allowed:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=error_message
                )
        else:
            # Fallback: Check active rules if IGSID not available
            current_rules = db.query(AutomationRule).filter(
                AutomationRule.instagram_account_id == instagram_account_id,
                AutomationRule.deleted_at.is_(None)
            ).count()
            
            # Only check limit if max_rules is not unlimited (-1)
            if max_rules != -1 and current_rules >= max_rules:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Rule limit reached. Your {user.plan_tier} plan allows {max_rules} automation rule(s). Upgrade to add more."
                )
    else:
        # Check limit across all user's Instagram accounts
        # Get all user's Instagram account IGSIDs
        user_accounts = db.query(InstagramAccount).filter(
            InstagramAccount.user_id == user_id
        ).all()
        
        if not user_accounts:
            # No accounts yet, allow first rule creation
            return True
        
        # Check each account's tracker and find the one with highest rule count
        max_rules_created = 0
        limiting_account = None
        
        for account in user_accounts:
            if account.igsid:
                tracker = get_or_create_tracker(user_id, account.igsid, db)
                check_and_reset_usage(tracker, user.plan_tier, db)
                
                if tracker.rules_created_count > max_rules_created:
                    max_rules_created = tracker.rules_created_count
                    limiting_account = account
        
        # Check if any account has reached the limit (only if max_rules is not unlimited)
        if max_rules != -1 and max_rules_created >= max_rules:
            account_name = limiting_account.username if limiting_account else "your account"
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Rule limit reached. Your {user.plan_tier} plan allows {max_rules} automation rule(s). "
                       f"The Instagram account @{account_name} has already created {max_rules_created} rule(s). "
                       f"Upgrade to add more."
            )

    return True


def get_instagram_account_usage(instagram_account_id: int, cycle_start: datetime, db: Session, user_id: int = None) -> int:
    """
    Get DM usage for a specific Instagram account (by account ID).
    Counts DMs by account_id and by username/igsid in dm_logs (orphaned rows after account delete).
    For MVP: Also filters by user_id to ensure per-user per-Instagram tracking.
    """
    account = db.query(InstagramAccount).filter(
        InstagramAccount.id == instagram_account_id
    ).first()
    
    if not account:
        return 0
    
    username = account.username
    igsid = getattr(account, "igsid", None)
    
    # Build base filter: current billing cycle + user_id (for per-user tracking)
    base = db.query(DmLog).filter(DmLog.sent_at >= cycle_start)
    if user_id:
        base = base.filter(DmLog.user_id == user_id)
    
    from sqlalchemy import or_
    clauses = []
    clauses.append(DmLog.instagram_account_id == instagram_account_id)
    if username:
        clauses.append(DmLog.instagram_username == username)
    if igsid:
        clauses.append(DmLog.instagram_igsid == igsid)
    
    q = base.filter(or_(*clauses))
    return q.count()


def check_dm_limit(user_id: int, db: Session, instagram_account_id: int = None) -> bool:
    """
    Check if user can send another DM based on their plan.
    For Pro/Enterprise users: Uses 30-day billing cycle from upgrade date
    For Free/Basic users: Uses calendar month
    
    If instagram_account_id is provided, checks usage for that specific account.
    Otherwise, checks total usage across all user's accounts.
    
    Also checks persistent global usage tracker per Instagram account (IGSID) to prevent abuse.
    
    Returns True if limit not reached, logs warning if at limit but doesn't raise exception.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    max_dms = get_plan_limit(user.plan_tier, "max_dms_per_month")

    # Get billing cycle start (calendar month for free/basic, 30-day cycle for pro/enterprise)
    cycle_start = get_billing_cycle_start(user_id, db)
    
    # If checking for a specific account, use account-level tracking
    if instagram_account_id:
        dms_this_cycle = get_instagram_account_usage(instagram_account_id, cycle_start, db, user_id=user_id)
        
        # Check persistent global tracker for ALL tiers to ensure limits persist across disconnect/reconnect
        account = db.query(InstagramAccount).filter(
            InstagramAccount.id == instagram_account_id
        ).first()
        
        if account and account.igsid:
            tracker = get_or_create_tracker(user_id, account.igsid, db)
            check_and_reset_usage(tracker, user.plan_tier, db)
            
            # Check global tracker limit (lifetime for free tier, monthly for pro/enterprise)
            is_allowed, error_message = check_global_dm_limit(tracker, user.plan_tier)
            if not is_allowed:
                print(f"⚠️ Global DM limit reached for IGSID {account.igsid}: {error_message}")
                return False
            
            # For Free tier: Global tracker enforces lifetime limit (1000 DMs total, never resets) - High Volume pricing
            # For Pro/Enterprise: Global tracker enforces monthly limit (resets every 30 days)
            # No need to check monthly limit for free tier since it's lifetime
            if user.plan_tier in ["pro", "enterprise"]:
                # For Pro/Enterprise, also check monthly limit as secondary check
                if dms_this_cycle >= max_dms:
                    print(f"⚠️ Monthly DM limit reached for user {user_id}: {dms_this_cycle}/{max_dms} DMs sent in current cycle")
                    return False
    else:
        # Count DMs sent in current billing cycle across all user's accounts
        dms_this_cycle = db.query(DmLog).filter(
            DmLog.user_id == user_id,
            DmLog.sent_at >= cycle_start
        ).count()

    if dms_this_cycle >= max_dms:
        print(f"⚠️ DM limit reached for user {user_id}: {dms_this_cycle}/{max_dms} DMs sent in current cycle")
        # Don't raise exception, just log and return False
        # This prevents API calls but doesn't break the webhook flow
        return False

    return True


def log_dm_sent(
    user_id: int,
    instagram_account_id: int,
    recipient_username: str,
    message: str,
    db: Session,
    instagram_username: str | None = None,
    instagram_igsid: str | None = None,
):
    """
    Log a sent DM for tracking. Store username/igsid so usage survives account delete.
    Also increments persistent global tracker per Instagram account (IGSID).
    """
    if instagram_username is None or instagram_igsid is None:
        acc = db.query(InstagramAccount).filter(
            InstagramAccount.id == instagram_account_id
        ).first()
        if acc:
            instagram_username = instagram_username or acc.username
            instagram_igsid = instagram_igsid or acc.igsid
    
    # Log to DmLog (existing tracking)
    dm_log = DmLog(
        user_id=user_id,
        instagram_account_id=instagram_account_id,
        instagram_username=instagram_username,
        instagram_igsid=instagram_igsid,
        recipient_username=recipient_username,
        message=message,
        sent_at=datetime.utcnow(),
    )
    db.add(dm_log)
    db.commit()
    
    # Increment persistent global tracker per Instagram account (IGSID)
    if instagram_igsid:
        try:
            tracker = get_or_create_tracker(user_id, instagram_igsid, db)
            increment_dm_count(tracker, db)
        except Exception as e:
            print(f"⚠️ Failed to increment global DM tracker: {str(e)}")
            # Don't fail the whole operation if tracker update fails


def get_remaining_dms(user_id: int, db: Session) -> int:
    """
    Get the number of remaining DMs user can send in current billing cycle.
    For Pro/Enterprise users: Uses 30-day billing cycle from upgrade date
    For Free/Basic users: Uses calendar month
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return 0

    max_dms = get_plan_limit(user.plan_tier, "max_dms_per_month")

    # Get billing cycle start (calendar month for free/basic, 30-day cycle for pro/enterprise)
    cycle_start = get_billing_cycle_start(user_id, db)
    
    # Count DMs sent in current billing cycle
    dms_this_cycle = db.query(DmLog).filter(
        DmLog.user_id == user_id,
        DmLog.sent_at >= cycle_start
    ).count()

    return max(0, max_dms - dms_this_cycle)


def check_pro_plan_access(user_id: int, db: Session) -> bool:
    """
    Check if user has Pro plan or higher (Pro/Enterprise) to access Stories, DMs, and IG Live features.
    Raises HTTPException if user doesn't have Pro plan.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    # Pro features require Pro or Enterprise plan
    if user.plan_tier not in ["pro", "enterprise"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Stories, DMs, and IG Live automation are Pro features. Your current plan ({user.plan_tier}) doesn't include these features. Please upgrade to Pro to access them."
        )

    return True


def has_pro_plan(user_id: int, db: Session) -> bool:
    """
    Check if user has Pro plan or higher (returns True/False, doesn't raise exception).
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return False
    
    return user.plan_tier in ["pro", "enterprise"]
