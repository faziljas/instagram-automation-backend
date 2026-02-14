from fastapi import APIRouter, Depends, HTTPException, status, Header, Response
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models.user import User
from app.models.instagram_account import InstagramAccount
from app.models.automation_rule import AutomationRule
from app.models.dm_log import DmLog
from app.models.subscription import Subscription
from app.schemas.auth import ForgotPasswordRequest, UserSyncRequest
from app.dependencies.auth import verify_supabase_token
from app.utils.disposable_email import is_disposable_email

router = APIRouter()

DISPOSABLE_EMAIL_MESSAGE = (
    "Temporary or disposable email addresses are not allowed. "
    "Please use a permanent email address to sign up."
)


@router.get("/validate-email")
def validate_email(email: str = ""):
    """
    Check if an email is allowed for sign-up (not a disposable/temp domain).
    Returns 204 if valid, 400 with detail if disposable.
    No auth required; used by frontend before calling Supabase signUp.
    """
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid email format.",
        )
    if is_disposable_email(email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=DISPOSABLE_EMAIL_MESSAGE,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/check-email/{email}")
def check_email(email: str, db: Session = Depends(get_db)):
    """
    Check if an email already exists in the database (case-insensitive).
    This helps prevent duplicate registrations with different email cases.
    Returns 200 if email exists, 404 if it doesn't.
    """
    # Normalize email to lowercase for comparison
    normalized_email = email.lower().strip()
    if is_disposable_email(normalized_email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=DISPOSABLE_EMAIL_MESSAGE,
        )
    existing_user = db.query(User).filter(
        User.email.ilike(normalized_email)
    ).first()
    
    if existing_user:
        return {
            "exists": True,
            "message": "This email is already registered. Please log in instead."
        }
    else:
        return {
            "exists": False,
            "message": "Email is available."
        }


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
    
    # Check if user exists by email (case-insensitive) OR by supabase_id
    existing_user = db.query(User).filter(
        User.email.ilike(user_data.email)
    ).first()
    
    # Also check by supabase_id in case email lookup fails (shouldn't happen, but safety check)
    if not existing_user:
        existing_user = db.query(User).filter(
            User.supabase_id == user_data.id
        ).first()
    
    # Reject disposable/temporary email only for NEW signups (don't block existing users)
    if not existing_user and is_disposable_email(user_data.email):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=DISPOSABLE_EMAIL_MESSAGE,
        )
    
    if existing_user:
        # Check if this is a different Supabase user with the same email
        if existing_user.supabase_id:
            # User already has a Supabase ID - check if it matches
            if existing_user.supabase_id != user_data.id:
                # Same email, different Supabase ID - user already has an account (e.g. email signup).
                # Block duplicate signup (e.g. Google signup with same email); they must log in instead.
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="This email is already registered. Please log in instead."
                )
            # Same Supabase user - update name fields only if they're not already set
            # This preserves user's custom names while allowing initial sync from Google metadata
            # IMPORTANT: Never overwrite existing names - user may have customized them in settings
            updated = False
            if user_data.first_name and user_data.first_name.strip() and not existing_user.first_name:
                existing_user.first_name = user_data.first_name.strip()
                updated = True
            if user_data.last_name and user_data.last_name.strip() and not existing_user.last_name:
                existing_user.last_name = user_data.last_name.strip()
                updated = True
            
            if updated:
                db.commit()
                db.refresh(existing_user)
            
            # Return success even if nothing was updated (idempotent sync)
            return {
                "message": "User synced successfully",
                "user_id": existing_user.id
            }
        else:
            # User exists but doesn't have a Supabase ID (created via old /auth/register endpoint or auto-created)
            # Update the user to add the Supabase ID instead of rejecting
            # This handles the case where user was auto-created by get_current_user_id before sync-user was called
            print(f"[AUTH] Sync-user: Adding supabase_id to existing user {existing_user.id} (email: {user_data.email})")
            existing_user.supabase_id = user_data.id
            updated = True
            if user_data.first_name and user_data.first_name.strip() and not existing_user.first_name:
                existing_user.first_name = user_data.first_name.strip()
            if user_data.last_name and user_data.last_name.strip() and not existing_user.last_name:
                existing_user.last_name = user_data.last_name.strip()
            
            db.commit()
            db.refresh(existing_user)
            
            return {
                "message": "User synced successfully",
                "user_id": existing_user.id
            }
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
            first_name=user_data.first_name,  # Sync from Supabase metadata if available
            last_name=user_data.last_name,  # Sync from Supabase metadata if available
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
