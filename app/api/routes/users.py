from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from sqlalchemy import func, case
from app.db.session import get_db
from app.models.user import User
from app.models.instagram_account import InstagramAccount
from app.models.automation_rule import AutomationRule
from app.models.dm_log import DmLog
from app.models.subscription import Subscription
from app.models.automation_rule_stats import AutomationRuleStats
from app.models.captured_lead import CapturedLead
from app.models.analytics_event import AnalyticsEvent
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.follower import Follower
from app.models.instagram_audience import InstagramAudience
from app.models.instagram_global_tracker import InstagramGlobalTracker
from app.models.invoice import Invoice
from app.schemas.auth import (
    UserResponse,
    DashboardStatsResponse,
    SubscriptionResponse,
    SubscriptionUsage,
    UserUpdate,
    PasswordChange,
)
from app.utils.auth import hash_password, verify_password
from app.dependencies.auth import get_current_user_id
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
        "profile_picture_url": user.profile_picture_url,
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
    if user_data.profile_picture_url is not None:
        user.profile_picture_url = user_data.profile_picture_url
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
        "profile_picture_url": user.profile_picture_url,
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
    """Change user password.

    For regular users (email/password): Requires old_password verification.
    For Supabase/Google OAuth users (supabase_id set): password changes are disabled.
    They should continue signing in with Google instead.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    # Block password changes for Supabase/Google OAuth users
    if user.supabase_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password changes are disabled for Google sign-in accounts. Please use 'Sign in with Google' instead."
        )

    # For regular users (no supabase_id), old_password is required
    if not password_data.old_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is required"
        )

    # Verify old password for regular users
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
        "created_at": account.created_at.isoformat() if account.created_at else None
    } for account in accounts]


@router.get("/me/dashboard", response_model=DashboardStatsResponse)
def get_dashboard_stats(
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id)
):
    """Get dashboard statistics for current user"""
    # OPTIMIZED: Get user and accounts in single query with join
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    # OPTIMIZED: Get all data in fewer queries
    # Get accounts and their IDs in one query
    user_accounts = db.query(InstagramAccount).filter(
        InstagramAccount.user_id == user_id
    ).all()
    
    accounts_count = len(user_accounts)
    user_account_ids = [acc.id for acc in user_accounts]
    
    # OPTIMIZED: Calculate today's start once
    now = datetime.utcnow()
    today_start = datetime(now.year, now.month, now.day, 0, 0, 0)
    
    # OPTIMIZED: Get all counts in parallel queries (if user_account_ids exist)
    active_rules_count = 0
    dms_sent_today = 0
    total_dms = 0
    
    if user_account_ids:
        # Count active rules
        active_rules_count = db.query(AutomationRule).filter(
            AutomationRule.instagram_account_id.in_(user_account_ids),
            AutomationRule.is_active == True,
            AutomationRule.deleted_at.is_(None)
        ).count()
        
        # Count DMs sent today and total in single query using conditional aggregation
        from sqlalchemy import case
        dm_counts = db.query(
            func.sum(case((DmLog.sent_at >= today_start, 1), else_=0)).label("today_count"),
            func.count(DmLog.id).label("total_count")
        ).filter(
            DmLog.instagram_account_id.in_(user_account_ids)
        ).first()
        
        dms_sent_today = int(dm_counts.today_count or 0)
        total_dms = int(dm_counts.total_count or 0)
    
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
    # Default response for new users or error cases
    # Must match SubscriptionResponse model exactly
    default_response = SubscriptionResponse(
        plan_tier="free",
        effective_plan_tier="free",
        status="active",
        stripe_subscription_id=None,
        cancellation_end_date=None,
        usage=SubscriptionUsage(
            accounts=0,
            rules=0,
            dms_sent_this_month=0,
        ),
    )
    
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            # Be defensive for brand‑new auth users who might not
            # have a local User row yet. Treat them as Free tier with
            # zero usage instead of failing the whole subscription page.
            return default_response
        
        # Ensure plan_tier is set (safety check for edge cases)
        if not user.plan_tier:
            user.plan_tier = "free"
            db.commit()
        
        subscription = db.query(Subscription).filter(
            Subscription.user_id == user_id
        ).first()
        
        # Get usage stats
        accounts_count = db.query(InstagramAccount).filter(
            InstagramAccount.user_id == user_id
        ).count()
        
        # Get user's Instagram accounts
        all_user_accounts = db.query(InstagramAccount).filter(
            InstagramAccount.user_id == user_id
        ).all()
        
        # Calculate usage stats - use tracker for rules (total created, even if deleted)
        # For free tier: Show lifetime total rules created (persists even after deletion)
        from app.utils.plan_enforcement import get_billing_cycle_start
        from app.services.instagram_usage_tracker import get_or_create_tracker
        rules_count = 0
        dms_display_count = 0
        
        # Only calculate usage if user has connected accounts
        if all_user_accounts:
            user_account_ids = [acc.id for acc in all_user_accounts]
            
            # Rules count: Use tracker's rules_created_count (total rules ever created, even if deleted)
            # This ensures the count persists even after deletion, matching the limit enforcement
            max_rules_created = 0
            for account in all_user_accounts:
                if account.igsid:
                    try:
                        tracker = get_or_create_tracker(user_id, account.igsid, db)
                        if tracker.rules_created_count > max_rules_created:
                            max_rules_created = tracker.rules_created_count
                    except Exception as e:
                        # If tracker doesn't exist or error, fall back to counting active rules
                        print(f"⚠️ Error getting tracker for account {account.igsid}: {str(e)}")
            
            rules_count = max_rules_created
            
            # DMs count: Count DMs sent by this user in current billing cycle (user-based tracking)
            try:
                cycle_start = get_billing_cycle_start(user_id, db)
                try:
                    dms_display_count = db.query(DmLog).filter(
                        DmLog.user_id == user_id,
                        DmLog.sent_at >= cycle_start
                    ).count()
                except Exception as db_e:
                    # If database query fails, log and use 0 as fallback
                    print(f"⚠️ Error querying DMs for user {user_id}: {str(db_e)}")
                    dms_display_count = 0
            except Exception as e:
                # If billing cycle calculation fails, use calendar month as fallback
                print(f"⚠️ Error calculating billing cycle for user {user_id}: {str(e)}")
                try:
                    from datetime import datetime
                    cycle_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                    dms_display_count = db.query(DmLog).filter(
                        DmLog.user_id == user_id,
                        DmLog.sent_at >= cycle_start
                    ).count()
                except Exception as db_e:
                    # If fallback query also fails, use 0
                    print(f"⚠️ Error querying DMs (fallback) for user {user_id}: {str(db_e)}")
                    dms_display_count = 0
        
        # Free plan users should show as "active" (they have an active free plan)
        # Paid users show their subscription status
        status_value = "active" if user.plan_tier == "free" else (subscription.status if subscription else "inactive")
        
        # Determine effective plan tier for display purposes
        # If Free user is still within paid Pro cycle period, show Pro limits
        # Also check if cancelled Pro subscription is still within paid period
        effective_plan_tier = user.plan_tier or "free"
        cancellation_end_date = None
        
        if subscription and subscription.billing_cycle_start_date:
            try:
                from datetime import datetime, timedelta
                cycle_start = subscription.billing_cycle_start_date
                now = datetime.utcnow()
                days_since_start = (now - cycle_start).days
                
                # Calculate current cycle end (30 days from cycle start)
                cycles_passed = days_since_start // 30
                current_cycle_start = cycle_start + timedelta(days=cycles_passed * 30)
                current_cycle_end = current_cycle_start + timedelta(days=30)
                
                # If still within paid Pro cycle period
                if now < current_cycle_end:
                    # If Free tier but within Pro cycle, show Pro limits
                    if user.plan_tier == "free":
                        effective_plan_tier = "pro"
                        print(f"✅ User {user_id} is Free tier but still within Pro cycle period - showing Pro limits")
                    
                    # If cancelled Pro subscription, calculate when access ends
                    if subscription.status == "cancelled" and user.plan_tier in ["pro", "enterprise"]:
                        cancellation_end_date = current_cycle_end
                        print(f"✅ User {user_id} has cancelled Pro subscription - access until {cancellation_end_date}")
                else:
                    # Pro cycle has ended - if subscription was cancelled, downgrade to Free now
                    if subscription.status == "cancelled" and user.plan_tier in ["pro", "enterprise"]:
                        user.plan_tier = "free"
                        subscription.billing_cycle_start_date = None  # Clear since cycle ended
                        db.commit()
                        print(f"✅ User {user_id} Pro cycle ended - downgraded to Free tier")
            except Exception as e:
                print(f"⚠️ Error processing subscription cycle for user {user_id}: {str(e)}")
                # Continue with default values if cycle calculation fails
        
        return SubscriptionResponse(
            plan_tier=user.plan_tier or "free",
            effective_plan_tier=effective_plan_tier,  # Use this for display limits
            status=status_value,
            stripe_subscription_id=subscription.stripe_subscription_id if subscription else None,
            cancellation_end_date=cancellation_end_date.isoformat() if cancellation_end_date else None,
            usage=SubscriptionUsage(
                accounts=accounts_count,
                rules=rules_count,  # Total rules created (from tracker, persists even after deletion)
                dms_sent_this_month=dms_display_count  # DMs sent by this user in current billing cycle (user-based tracking)
            )
        )
    except HTTPException:
        # Re-raise HTTP exceptions (like 401, 404) as-is so frontend can handle them properly
        raise
    except Exception as e:
        # Log the error for debugging but return a valid response
        print(f"❌ Error in get_subscription for user {user_id}: {str(e)}")
        import traceback
        traceback.print_exc()
        # Return default response to prevent frontend error
        return default_response


@router.get("/invoices")
async def list_invoices(
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    """
    List invoices for the current user (most recent first).

    Invoices come from:
    1. Dodo `payment.succeeded` / `payment.failed` webhooks (when user pays).
    2. Automatic fetch from Dodo API on every request (ensures latest invoices
       appear without manual sync, including if webhooks were missed).
    """
    # Always sync invoices from Dodo API so they appear automatically after payment
    try:
        from app.api.routes.dodo import _sync_invoices_from_dodo_api
        sync_result = await _sync_invoices_from_dodo_api(db, user_id, raise_on_error=False)
        if sync_result:
            print(f"[Invoices] Synced {sync_result.get('synced', 0)} invoices for user {user_id}")
    except Exception as e:
        print(f"[Invoices] Error syncing from Dodo (non-critical): {str(e)}")
        import traceback
        traceback.print_exc()

    invoices = (
        db.query(Invoice)
        .filter(Invoice.user_id == user_id)
        .order_by(Invoice.paid_at.desc().nullslast(), Invoice.created_at.desc())
        .all()
    )

    return [
        {
            "id": inv.id,
            "amount": inv.amount,
            "currency": inv.currency,
            "status": inv.status,
            "invoice_url": inv.invoice_url,
            "paid_at": inv.paid_at.isoformat() if inv.paid_at else None,
            "created_at": inv.created_at.isoformat() if inv.created_at else None,
        }
        for inv in invoices
    ]


@router.post("/subscription/cancel")
def cancel_subscription(
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id)
):
    """Cancel user's subscription - user keeps Pro access until paid period ends"""
    subscription = db.query(Subscription).filter(
        Subscription.user_id == user_id
    ).first()
    
    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active subscription found"
        )
    
    user = db.query(User).filter(User.id == user_id).first()
    
    # Update subscription status to cancelled
    subscription.status = "cancelled"
    subscription.updated_at = datetime.utcnow()
    
    # DON'T downgrade plan_tier immediately - user paid for 30 days, so keep Pro access
    # Plan will automatically downgrade after billing cycle ends (handled by webhook or cycle logic)
    # DON'T clear billing_cycle_start_date - needed to calculate when Pro access ends
    
    # Calculate when Pro access ends (30 days from billing cycle start)
    cancellation_end_date = None
    if subscription.billing_cycle_start_date:
        from datetime import timedelta
        cycle_start = subscription.billing_cycle_start_date
        now = datetime.utcnow()
        days_since_start = (now - cycle_start).days
        
        # Calculate current cycle end (30 days from cycle start)
        cycles_passed = days_since_start // 30
        current_cycle_start = cycle_start + timedelta(days=cycles_passed * 30)
        cancellation_end_date = current_cycle_start + timedelta(days=30)
    
    print(f"✅ User {user_id} cancelled subscription - keeping Pro access until {cancellation_end_date}")
    
    db.commit()
    
    return {
        "message": "Subscription cancelled successfully",
        "plan_tier": user.plan_tier,  # Still Pro until cycle ends
        "cancellation_end_date": cancellation_end_date.isoformat() if cancellation_end_date else None
    }


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
def delete_user_account(
    request: Request,
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id)
):
    """Delete user account and all associated data. Order respects FK constraints.
    Also deletes the user from Supabase Auth."""
    from app.dependencies.auth import verify_supabase_token
    import os
    import requests
    
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    # Extract Supabase user ID from token to delete from Supabase Auth
    supabase_user_id = None
    try:
        # Get authorization header from request
        authorization = request.headers.get("Authorization")
        if authorization:
            payload = verify_supabase_token(authorization)
            if payload:
                supabase_user_id = payload.get("sub")  # Supabase user ID
    except Exception as e:
        print(f"[DELETE] Could not extract Supabase user ID: {e}")
        # Continue with backend deletion even if Supabase deletion fails
    
    # Delete from Supabase Auth if we have the user ID (non-blocking)
    # Run this in a separate try-catch so it doesn't block backend deletion
    if supabase_user_id:
        try:
            supabase_url = os.getenv("SUPABASE_URL")
            supabase_service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
            
            if supabase_url and supabase_service_key:
                # Delete user from Supabase Auth using Admin API
                delete_url = f"{supabase_url}/auth/v1/admin/users/{supabase_user_id}"
                headers = {
                    "apikey": supabase_service_key,
                    "Authorization": f"Bearer {supabase_service_key}",
                    "Content-Type": "application/json"
                }
                
                # Use shorter timeout to prevent hanging
                response = requests.delete(delete_url, headers=headers, timeout=5)
                if response.status_code == 200 or response.status_code == 204:
                    print(f"[DELETE] Successfully deleted user {supabase_user_id} from Supabase Auth")
                else:
                    print(f"[DELETE] Failed to delete from Supabase Auth: {response.status_code} - {response.text}")
            else:
                print("[DELETE] SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set, skipping Supabase deletion")
        except requests.exceptions.Timeout:
            print(f"[DELETE] Supabase deletion timed out - continuing with backend deletion")
        except Exception as e:
            print(f"[DELETE] Error deleting from Supabase Auth: {e}")
            # Continue with backend deletion even if Supabase deletion fails

    # Wrap all database operations in try-catch to handle any errors gracefully
    try:
        account_ids = [a.id for a in db.query(InstagramAccount).filter(InstagramAccount.user_id == user_id).all()]
        rule_ids = []
        if account_ids:
            rule_ids = [r.id for r in db.query(AutomationRule.id).filter(
                AutomationRule.instagram_account_id.in_(account_ids)
            ).all()]

        # 1. automation_rules dependents (FK → automation_rules)
        if rule_ids:
            try:
                db.query(AutomationRuleStats).filter(
                    AutomationRuleStats.automation_rule_id.in_(rule_ids)
                ).delete(synchronize_session=False)
                db.query(CapturedLead).filter(
                    CapturedLead.automation_rule_id.in_(rule_ids)
                ).delete(synchronize_session=False)
                db.query(AnalyticsEvent).filter(
                    AnalyticsEvent.rule_id.in_(rule_ids)
                ).update({"rule_id": None}, synchronize_session=False)
            except Exception as e:
                print(f"[DELETE] Error deleting automation rule dependents: {e}")
                db.rollback()

        # 2. automation_rules (FK → instagram_accounts)
        if account_ids:
            try:
                db.query(AutomationRule).filter(
                    AutomationRule.instagram_account_id.in_(account_ids)
                ).delete(synchronize_session=False)
            except Exception as e:
                print(f"[DELETE] Error deleting automation rules: {e}")
                db.rollback()

        # 3. instagram_accounts dependents: messages (FK → conversations, instagram_accounts)
        #    Delete messages before conversations (message.conversation_id → conversations)
        if account_ids:
            try:
                db.query(Message).filter(Message.instagram_account_id.in_(account_ids)).delete(synchronize_session=False)
                db.query(Conversation).filter(Conversation.instagram_account_id.in_(account_ids)).delete(synchronize_session=False)
                db.query(AnalyticsEvent).filter(AnalyticsEvent.instagram_account_id.in_(account_ids)).delete(synchronize_session=False)
                db.query(Follower).filter(Follower.instagram_account_id.in_(account_ids)).delete(synchronize_session=False)
                db.query(InstagramAudience).filter(InstagramAudience.instagram_account_id.in_(account_ids)).delete(synchronize_session=False)
                db.query(DmLog).filter(DmLog.instagram_account_id.in_(account_ids)).delete(synchronize_session=False)
            except Exception as e:
                print(f"[DELETE] Error deleting instagram account dependents: {e}")
                db.rollback()

        # 4. user-level data (FK → users) – must delete ALL user-related records before deleting user
        try:
            db.query(Invoice).filter(Invoice.user_id == user_id).delete(synchronize_session=False)
            db.query(AnalyticsEvent).filter(AnalyticsEvent.user_id == user_id).delete(synchronize_session=False)
            db.query(Message).filter(Message.user_id == user_id).delete(synchronize_session=False)
            db.query(Conversation).filter(Conversation.user_id == user_id).delete(synchronize_session=False)
            db.query(CapturedLead).filter(CapturedLead.user_id == user_id).delete(synchronize_session=False)
            db.query(InstagramAudience).filter(InstagramAudience.user_id == user_id).delete(synchronize_session=False)
            db.query(DmLog).filter(DmLog.user_id == user_id).delete(synchronize_session=False)
        except Exception as e:
            print(f"[DELETE] Error deleting user-level data: {e}")
            db.rollback()
        
        # 4.5. Delete InstagramGlobalTracker records (FK → users)
        try:
            db.query(InstagramGlobalTracker).filter(InstagramGlobalTracker.user_id == user_id).delete(synchronize_session=False)
        except Exception as e:
            print(f"[DELETE] Error deleting InstagramGlobalTracker: {e}")
            db.rollback()

        # 5. instagram_accounts, subscription (FK → users)
        try:
            db.query(InstagramAccount).filter(InstagramAccount.user_id == user_id).delete(synchronize_session=False)
            db.query(Subscription).filter(Subscription.user_id == user_id).delete(synchronize_session=False)
        except Exception as e:
            print(f"[DELETE] Error deleting instagram accounts and subscription: {e}")
            db.rollback()

        # 6. user
        try:
            db.delete(user)
            db.commit()
            print(f"[DELETE] Successfully deleted user {user_id} and all associated data")
        except Exception as e:
            print(f"[DELETE] Error deleting user: {e}")
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete user account: {str(e)}"
            )
        
        return None
    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        # Log the full error for debugging
        import traceback
        print(f"[DELETE] Unexpected error deleting user {user_id}: {str(e)}")
        traceback.print_exc()
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete user account: {str(e)}"
        )
