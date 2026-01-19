from datetime import datetime
from sqlalchemy.orm import Session
from fastapi import HTTPException, status
from app.models.user import User
from app.models.instagram_account import InstagramAccount
from app.models.dm_log import DmLog
from app.models.automation_rule import AutomationRule
from app.core.plan_limits import get_plan_limit


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


def check_dm_limit(user_id: int, db: Session) -> bool:
    """
    Check if user can send another DM this month based on their plan.
    Raises HTTPException if limit exceeded.
    Returns True if limit not reached, logs warning if at limit but doesn't raise exception.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    max_dms = get_plan_limit(user.plan_tier, "max_dms_per_month")

    # Count DMs sent this month (from first day of current month)
    start_of_month = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    dms_this_month = db.query(DmLog).filter(
        DmLog.user_id == user_id,
        DmLog.sent_at >= start_of_month
    ).count()

    if dms_this_month >= max_dms:
        print(f"⚠️ Monthly DM limit reached for user {user_id}: {dms_this_month}/{max_dms} DMs sent this month")
        # Don't raise exception, just log and return False
        # This prevents API calls but doesn't break the webhook flow
        return False

    return True


def log_dm_sent(user_id: int, instagram_account_id: int, recipient_username: str, message: str, db: Session):
    """
    Log a sent DM for tracking monthly limits.
    """
    dm_log = DmLog(
        user_id=user_id,
        instagram_account_id=instagram_account_id,
        recipient_username=recipient_username,
        message=message,
        sent_at=datetime.utcnow()
    )
    db.add(dm_log)
    db.commit()


def get_remaining_dms(user_id: int, db: Session) -> int:
    """
    Get the number of remaining DMs user can send this month.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return 0

    max_dms = get_plan_limit(user.plan_tier, "max_dms_per_month")

    # Count DMs sent this month
    start_of_month = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    dms_this_month = db.query(DmLog).filter(
        DmLog.user_id == user_id,
        DmLog.sent_at >= start_of_month
    ).count()

    return max(0, max_dms - dms_this_month)


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
