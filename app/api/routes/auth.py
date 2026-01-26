from fastapi import APIRouter, Depends, HTTPException, status, Header
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models.user import User
from app.models.instagram_account import InstagramAccount
from app.models.automation_rule import AutomationRule
from app.models.dm_log import DmLog
from app.models.subscription import Subscription
from app.schemas.auth import ForgotPasswordRequest, UserSyncRequest
from app.dependencies.auth import verify_supabase_token

router = APIRouter()


@router.post("/sync-user")
def sync_user(
    user_data: UserSyncRequest,
    db: Session = Depends(get_db),
    authorization: str = Header(None)
):
    """
    Sync user from Supabase Auth to our database.
    Creates user if doesn't exist, updates if exists.
    Requires valid Supabase JWT token.
    """
    # Verify Supabase token using the new dependency
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization token"
        )
    
    try:
        # Use the new verify_supabase_token dependency - it returns the payload
        payload = verify_supabase_token(authorization)
        
        # Extract user ID and email from verified payload
        token_user_id = payload.get("sub")
        token_email = payload.get("email", "").lower()
        
        # Verify that the token's user ID and email match the request
        if token_user_id != user_data.id or token_email != user_data.email.lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Token user does not match sync request"
            )
    except HTTPException:
        raise
    except Exception as e:
        print(f"[AUTH] Error verifying token in sync-user: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization token"
        )
    
    # Check if user exists by email (case-insensitive)
    existing_user = db.query(User).filter(
        User.email.ilike(user_data.email)
    ).first()
    
    if existing_user:
        # Check if this is a different Supabase user trying to register with an existing email
        # This prevents duplicate registrations when a user signs up with Google OAuth
        # and then tries to register with email/password (or vice versa)
        if existing_user.supabase_id:
            # User already has a Supabase ID - check if it matches
            if existing_user.supabase_id != user_data.id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="An account with this email already exists. If you signed up with Google, please use 'Sign in with Google' instead. Otherwise, please sign in with your existing account."
                )
            # Same Supabase user - just return success
            return {
                "message": "User synced successfully",
                "user_id": existing_user.id
            }
        else:
            # User exists but doesn't have a Supabase ID (created via old /auth/register endpoint)
            # This means someone is trying to register with Supabase using an email that's already taken
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="An account with this email already exists. Please sign in with your existing account instead."
            )
    else:
        # Check if a user with this Supabase ID already exists (shouldn't happen, but safety check)
        existing_supabase_user = db.query(User).filter(
            User.supabase_id == user_data.id
        ).first()
        
        if existing_supabase_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A user account already exists for this Supabase account."
            )
        
        # Create new user
        # Use a placeholder password since auth is handled by Supabase
        # The password field is required but won't be used for authentication
        from app.utils.auth import hash_password
        placeholder_password = hash_password(f"supabase_user_{user_data.id}")
        
        new_user = User(
            email=user_data.email.lower(),
            hashed_password=placeholder_password,
            supabase_id=user_data.id,  # Store Supabase user ID
            is_verified=True,  # Supabase handles email verification
        )
        
        try:
            db.add(new_user)
            db.commit()
            db.refresh(new_user)
            return {
                "message": "User created successfully",
                "user_id": new_user.id
            }
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to sync user: {str(e)}"
            )


@router.post("/forgot-password")
def forgot_password(request: ForgotPasswordRequest, db: Session = Depends(get_db)):
    """
    Request a password reset link.
    For security, this endpoint always returns success even if the email doesn't exist.
    """
    # Case-insensitive email lookup
    user = db.query(User).filter(
        User.email.ilike(request.email)
    ).first()
    
    # Always return success for security (prevent email enumeration)
    # In production, you would:
    # 1. Generate a secure reset token
    # 2. Store it in the database with expiration time
    # 3. Send an email with the reset link
    # 4. The reset link would point to /reset-password?token=...
    
    if user:
        # TODO: In production, implement:
        # - Generate secure token (e.g., using secrets.token_urlsafe())
        # - Store token in database with expiration (e.g., 1 hour)
        # - Send email with reset link
        # - Use email service (SendGrid, AWS SES, etc.)
        print(f"[Password Reset] Reset requested for user: {user.email}")
        # For now, just log it
    
    # Always return success to prevent email enumeration
    return {
        "message": "If an account exists with this email, a password reset link has been sent."
    }


@router.delete("/cleanup/{email}")
def cleanup_user_by_email(email: str, db: Session = Depends(get_db)):
    """Admin endpoint to completely remove a user by email (for testing/cleanup)"""
    # Find all users with this email (case-insensitive)
    users = db.query(User).filter(User.email.ilike(email)).all()
    
    if not users:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No user found with email: {email}"
        )
    
    deleted_count = 0
    for user in users:
        # Delete associated data
        user_accounts = db.query(InstagramAccount).filter(
            InstagramAccount.user_id == user.id
        ).all()
        
        for account in user_accounts:
            # Delete automation rules
            db.query(AutomationRule).filter(
                AutomationRule.instagram_account_id == account.id
            ).delete()
            
            # Delete DM logs
            db.query(DmLog).filter(
                DmLog.instagram_account_id == account.id
            ).delete()
        
        # Delete Instagram accounts
        db.query(InstagramAccount).filter(
            InstagramAccount.user_id == user.id
        ).delete()
        
        # Delete subscription
        db.query(Subscription).filter(
            Subscription.user_id == user.id
        ).delete()
        
        # Delete user
        db.delete(user)
        deleted_count += 1
    
    db.commit()
    
    return {
        "message": f"Successfully deleted {deleted_count} user(s) with email: {email}",
        "deleted_count": deleted_count
    }
