from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.db.session import get_db
from app.models.user import User
from app.models.instagram_account import InstagramAccount
from app.models.automation_rule import AutomationRule
from app.models.dm_log import DmLog
from app.models.subscription import Subscription
from app.schemas.auth import UserResponse, DashboardStatsResponse, SubscriptionResponse, UserUpdate, PasswordChange
from app.utils.auth import hash_password, verify_password
from app.api.routes.instagram import get_current_user_id
from datetime import datetime, timedelta

router = APIRouter()


@router.get("/me", response_model=UserResponse)
def get_current_user(
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id)
):
    """Get current user profile"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    return {
        "id": user.id,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "plan_tier": user.plan_tier,
        "is_active": user.is_active,
        "is_verified": user.is_verified,
        "created_at": user.created_at.isoformat() if user.created_at else None
    }


@router.put("/me", response_model=UserResponse)
def update_user_profile(
    user_data: UserUpdate,
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id)
):
    """Update user profile information"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    # Update fields if provided
    if user_data.first_name is not None:
        user.first_name = user_data.first_name
    if user_data.last_name is not None:
        user.last_name = user_data.last_name
    if user_data.email is not None:
        # Check if email is already taken by another user
        existing_user = db.query(User).filter(
            User.email == user_data.email.lower(),
            User.id != user_id
        ).first()
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already in use"
            )
        user.email = user_data.email.lower()
    
    db.commit()
    db.refresh(user)
    
    return {
        "id": user.id,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "plan_tier": user.plan_tier,
        "is_active": user.is_active,
        "is_verified": user.is_verified,
        "created_at": user.created_at.isoformat() if user.created_at else None
    }


@router.put("/me/password")
def change_password(
    password_data: PasswordChange,
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id)
):
    """Change user password"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    # Verify old password
    if not verify_password(password_data.old_password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect"
        )
    
    # Check if new password is the same as old password (exact match)
    if password_data.old_password == password_data.new_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password cannot be the same as current password"
        )
    
    # Check if new password is the same as old password (case-insensitive)
    if password_data.old_password.lower() == password_data.new_password.lower():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password cannot be the same as current password (case-insensitive check)"
        )
    
    # Validate new password length
    if len(password_data.new_password) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be at least 6 characters"
        )
    
    # Update password
    user.hashed_password = hash_password(password_data.new_password)
    db.commit()
    
    return {
        "message": "Password changed successfully"
    }


@router.get("/me/accounts")
def get_user_accounts(
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id)
):
    """Get all Instagram accounts for current user"""
    accounts = db.query(InstagramAccount).filter(
        InstagramAccount.user_id == user_id
    ).all()
    
    return [{
        "id": account.id,
        "username": account.username,
        "is_active": account.is_active,
        "created_at": None  # InstagramAccount model doesn't have created_at field
    } for account in accounts]


@router.get("/me/dashboard", response_model=DashboardStatsResponse)
def get_dashboard_stats(
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id)
):
    """Get dashboard statistics for current user"""
    # Get user
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    # Count accounts
    accounts_count = db.query(InstagramAccount).filter(
        InstagramAccount.user_id == user_id
    ).count()
    
    # Count active rules (via instagram accounts)
    user_account_ids = [acc.id for acc in db.query(InstagramAccount.id).filter(
        InstagramAccount.user_id == user_id
    ).all()]
    
    active_rules_count = 0
    if user_account_ids:
        active_rules_count = db.query(AutomationRule).filter(
            AutomationRule.instagram_account_id.in_(user_account_ids),
            AutomationRule.is_active == True
        ).count()
    
    # Count DMs sent in last 24 hours
    yesterday = datetime.utcnow() - timedelta(days=1)
    dms_sent_today = db.query(DmLog).filter(
        DmLog.user_id == user_id,
        DmLog.sent_at >= yesterday
    ).count()
    
    # Count total DMs sent
    total_dms = db.query(DmLog).filter(
        DmLog.user_id == user_id
    ).count()
    
    return {
        "user": {
            "id": user.id,
            "email": user.email,
            "plan_tier": user.plan_tier,
            "created_at": user.created_at.isoformat() if user.created_at else None
        },
        "stats": {
            "accounts_count": accounts_count,
            "active_rules_count": active_rules_count,
            "dms_sent_today": dms_sent_today,
            "total_dms_sent": total_dms
        }
    }


@router.get("/subscription", response_model=SubscriptionResponse)
def get_subscription(
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id)
):
    """Get user's subscription details"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    subscription = db.query(Subscription).filter(
        Subscription.user_id == user_id
    ).first()
    
    # Get usage stats
    accounts_count = db.query(InstagramAccount).filter(
        InstagramAccount.user_id == user_id
    ).count()
    
    # Get user's Instagram account IDs
    user_account_ids = [acc.id for acc in db.query(InstagramAccount.id).filter(
        InstagramAccount.user_id == user_id
    ).all()]
    
    rules_count = 0
    if user_account_ids:
        rules_count = db.query(AutomationRule).filter(
            AutomationRule.instagram_account_id.in_(user_account_ids)
        ).count()
    
    # Count DMs sent - use billing cycle for Pro/Enterprise, calendar month for Free/Basic
    from app.utils.plan_enforcement import get_billing_cycle_start
    cycle_start = get_billing_cycle_start(user_id, db)
    dms_this_month = db.query(DmLog).filter(
        DmLog.user_id == user_id,
        DmLog.sent_at >= cycle_start
    ).count()
    
    # Free plan users should show as "active" (they have an active free plan)
    # Paid users show their subscription status
    status_value = "active" if user.plan_tier == "free" else (subscription.status if subscription else "inactive")
    
    return {
        "plan_tier": user.plan_tier,
        "status": status_value,
        "stripe_subscription_id": subscription.stripe_subscription_id if subscription else None,
        "usage": {
            "accounts": accounts_count,
            "rules": rules_count,
            "dms_sent_this_month": dms_this_month
        }
    }


@router.post("/subscription/cancel")
def cancel_subscription(
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id)
):
    """Cancel user's subscription"""
    subscription = db.query(Subscription).filter(
        Subscription.user_id == user_id
    ).first()
    
    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active subscription found"
        )
    
    # Update subscription status
    subscription.status = "cancelled"
    subscription.updated_at = datetime.utcnow()
    
    # Downgrade user to free plan
    user = db.query(User).filter(User.id == user_id).first()
    user.plan_tier = "free"
    
    db.commit()
    
    return {
        "message": "Subscription cancelled successfully",
        "plan_tier": "free"
    }


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
def delete_user_account(
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id)
):
    """Delete user account and all associated data"""
    # Get user
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    # Delete associated data
    # Delete DM logs (via instagram accounts)
    user_accounts = db.query(InstagramAccount).filter(
        InstagramAccount.user_id == user_id
    ).all()
    
    for account in user_accounts:
        # Delete automation rules for this account
        db.query(AutomationRule).filter(
            AutomationRule.instagram_account_id == account.id
        ).delete()
        
        # Delete DM logs for this account
        db.query(DmLog).filter(
            DmLog.instagram_account_id == account.id
        ).delete()
    
    # Delete Instagram accounts
    db.query(InstagramAccount).filter(
        InstagramAccount.user_id == user_id
    ).delete()
    
    # Delete subscription
    db.query(Subscription).filter(
        Subscription.user_id == user_id
    ).delete()
    
    # Finally, delete the user
    db.delete(user)
    db.commit()
    
    return None
