from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from fastapi import HTTPException, status
from app.models.user import User
from app.models.instagram_account import InstagramAccount
from app.models.dm_log import DmLog
from app.models.automation_rule import AutomationRule
from app.models.subscription import Subscription
from app.core.plan_limits import get_plan_limit


def get_billing_cycle_start(user_id: int, db: Session) -> datetime:
    """
    Get the billing cycle start date for Pro/Enterprise users.
    For Pro users: Returns billing_cycle_start_date from subscription (30-day cycle from upgrade)
    For Free/Basic users: Returns start of current calendar month
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    # For Pro/Enterprise users, use billing cycle start date
    if user.plan_tier in ["pro", "enterprise"]:
        subscription = db.query(Subscription).filter(
            Subscription.user_id == user_id
        ).first()
        
        if subscription and subscription.billing_cycle_start_date:
            # Calculate current billing cycle start (30 days from original start)
            cycle_start = subscription.billing_cycle_start_date
            now = datetime.utcnow()
            
            # Find the most recent cycle start date (every 30 days)
            days_since_start = (now - cycle_start).days
            cycles_passed = days_since_start // 30
            current_cycle_start = cycle_start + timedelta(days=cycles_passed * 30)
            
            return current_cycle_start
    
    # For Free/Basic users, use calendar month
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


def check_rule_limit(user_id: int, db: Session) -> bool:
    """
    Check if user can create another automation rule based on their plan.
    Raises HTTPException if limit exceeded.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    max_rules = get_plan_limit(user.plan_tier, "max_automation_rules")
    
    # Get user's Instagram account IDs
    user_account_ids = [acc.id for acc in db.query(InstagramAccount.id).filter(
        InstagramAccount.user_id == user_id
    ).all()]
    
    current_rules = 0
    if user_account_ids:
        current_rules = db.query(AutomationRule).filter(
            AutomationRule.instagram_account_id.in_(user_account_ids)
        ).count()

    if current_rules >= max_rules:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Rule limit reached. Your {user.plan_tier} plan allows {max_rules} automation rule(s). Upgrade to add more."
        )

    return True


def get_instagram_account_usage(instagram_account_id: int, cycle_start: datetime, db: Session) -> int:
    """
    Get DM usage for a specific Instagram account (by account ID).
    Counts DMs by account_id and by username/igsid in dm_logs (orphaned rows after account delete).
    """
    account = db.query(InstagramAccount).filter(
        InstagramAccount.id == instagram_account_id
    ).first()
    
    if not account:
        return 0
    
    username = account.username
    igsid = getattr(account, "igsid", None)
    
    # Build base filter: current billing cycle
    base = db.query(DmLog).filter(DmLog.sent_at >= cycle_start)
    
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
        dms_this_cycle = get_instagram_account_usage(instagram_account_id, cycle_start, db)
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
    """
    if instagram_username is None or instagram_igsid is None:
        acc = db.query(InstagramAccount).filter(
            InstagramAccount.id == instagram_account_id
        ).first()
        if acc:
            instagram_username = instagram_username or acc.username
            instagram_igsid = instagram_igsid or acc.igsid
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
