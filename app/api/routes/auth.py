from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models.user import User
from app.models.instagram_account import InstagramAccount
from app.models.automation_rule import AutomationRule
from app.models.dm_log import DmLog
from app.models.subscription import Subscription
from app.schemas.auth import UserCreate, UserLogin, TokenResponse
from app.utils.auth import hash_password, verify_password, create_access_token

router = APIRouter()


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(user_data: UserCreate, db: Session = Depends(get_db)):
    # Check for existing user (case-insensitive email comparison)
    existing_user = db.query(User).filter(
        User.email.ilike(user_data.email)
    ).first()
    
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An account with this email already exists. Please login instead."
        )

    hashed_password = hash_password(user_data.password)
    new_user = User(
        email=user_data.email.lower(),  # Store email in lowercase
        hashed_password=hashed_password
    )
    
    try:
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Registration failed. This email may already be in use."
        )

    access_token = create_access_token(data={"sub": str(new_user.id)})
    return TokenResponse(access_token=access_token, token_type="bearer")


@router.post("/login", response_model=TokenResponse)
def login(user_data: UserLogin, db: Session = Depends(get_db)):
    # Case-insensitive email lookup
    user = db.query(User).filter(
        User.email.ilike(user_data.email)
    ).first()
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password. Please check your credentials and try again."
        )

    if not verify_password(user_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password. Please check your credentials and try again."
        )

    access_token = create_access_token(data={"sub": str(user.id)})
    return TokenResponse(access_token=access_token, token_type="bearer")


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
