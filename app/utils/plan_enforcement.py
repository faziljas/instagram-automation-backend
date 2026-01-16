from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from fastapi import HTTPException, status
from app.models.user import User
from app.models.instagram_account import InstagramAccount
from app.models.dm_log import DmLog
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


def check_dm_limit(user_id: int, db: Session) -> bool:
    """
    Check if user can send another DM today based on their plan.
    Raises HTTPException if limit exceeded.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    max_dms = get_plan_limit(user.plan_tier, "max_dms_per_day")

    # Count DMs sent today
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    dms_today = db.query(DmLog).filter(
        DmLog.user_id == user_id,
        DmLog.sent_at >= today_start
    ).count()

    if dms_today >= max_dms:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Daily DM limit reached. Your {user.plan_tier} plan allows {max_dms} DMs per day. Upgrade for more."
        )

    return True


def log_dm_sent(user_id: int, instagram_account_id: int, recipient_username: str, message: str, db: Session):
    """
    Log a sent DM for tracking daily limits.
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
    Get the number of remaining DMs user can send today.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return 0

    max_dms = get_plan_limit(user.plan_tier, "max_dms_per_day")

    # Count DMs sent today
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    dms_today = db.query(DmLog).filter(
        DmLog.user_id == user_id,
        DmLog.sent_at >= today_start
    ).count()

    return max(0, max_dms - dms_today)
