"""
Global conversion check service.
Checks if a user is already converted (has email + is following) across all automations.
"""
from sqlalchemy.orm import Session
from datetime import datetime
from app.models.instagram_audience import InstagramAudience
from app.models.captured_lead import CapturedLead


def get_or_create_audience(db: Session, sender_id: str, instagram_account_id: int, user_id: int, username: str = None) -> InstagramAudience:
    """
    Get or create an InstagramAudience record for a user.
    
    Args:
        db: Database session
        sender_id: Instagram user ID (sender_id)
        instagram_account_id: Our Instagram account ID
        user_id: Our app's user ID
        username: Instagram username (optional)
        
    Returns:
        InstagramAudience instance
    """
    audience = db.query(InstagramAudience).filter(
        InstagramAudience.sender_id == str(sender_id),
        InstagramAudience.instagram_account_id == instagram_account_id
    ).first()
    
    if not audience:
        audience = InstagramAudience(
            sender_id=str(sender_id),
            instagram_account_id=instagram_account_id,
            user_id=user_id,
            username=username,
            first_interaction_at=datetime.utcnow(),
            last_interaction_at=datetime.utcnow()
        )
        db.add(audience)
        db.commit()
        db.refresh(audience)
    else:
        # Update last interaction time
        audience.last_interaction_at = datetime.utcnow()
        if username and not audience.username:
            audience.username = username
        db.commit()
    
    return audience


def check_global_conversion_status(db: Session, sender_id: str, instagram_account_id: int, user_id: int, username: str = None) -> dict:
    """
    Check if a user is globally converted (has email AND phone AND is following).
    This is the "VIP" check: only when all three are collected do we treat as VIP and send primary DM directly.
    If any one is missed, not VIP — we ask for what each rule needs (email / phone / follow).
    
    Args:
        db: Database session
        sender_id: Instagram user ID
        instagram_account_id: Our Instagram account ID
        user_id: Our app's user ID
        username: Instagram username (optional)
        
    Returns:
        dict with keys:
            - is_converted: bool (True if user has email AND phone AND is following)
            - has_email: bool
            - has_phone: bool
            - is_following: bool
            - audience: InstagramAudience instance
    """
    # Get or create audience record
    audience = get_or_create_audience(db, sender_id, instagram_account_id, user_id, username)
    
    # Check if user has email (from any automation)
    has_email = bool(audience.email)
    
    # If no email in audience, check CapturedLead table (backward compatibility)
    # OPTIMIZED: Use JSONB query instead of loading all leads into memory
    if not has_email:
        try:
            from sqlalchemy import cast
            from sqlalchemy.dialects.postgresql import JSONB
            
            sender_id_str = str(sender_id)
            # Use PostgreSQL JSONB query to find lead with matching sender_id in metadata
            # This is much faster than loading all leads into memory
            lead = db.query(CapturedLead).filter(
                CapturedLead.instagram_account_id == instagram_account_id,
                CapturedLead.email.isnot(None),
                cast(CapturedLead.extra_metadata, JSONB)['sender_id'].astext == sender_id_str
            ).first()
            
            if lead:
                has_email = True
                # Update audience with email for future lookups
                audience.email = lead.email
                audience.email_captured_at = lead.captured_at
                db.commit()
        except Exception as e:
            # Fallback: If JSONB query fails, skip the check (don't block the request)
            print(f"⚠️ Error checking CapturedLead for email: {str(e)}")
    
    # Check following status
    is_following = audience.is_following
    
    # Check if user has phone (from any automation for this account)
    has_phone = False
    try:
        from sqlalchemy import cast
        from sqlalchemy.dialects.postgresql import JSONB
        sender_id_str = str(sender_id)
        lead_with_phone = db.query(CapturedLead).filter(
            CapturedLead.instagram_account_id == instagram_account_id,
            CapturedLead.phone.isnot(None),
            cast(CapturedLead.extra_metadata, JSONB)["sender_id"].astext == sender_id_str,
        ).first()
        if lead_with_phone and lead_with_phone.phone and str(lead_with_phone.phone).strip():
            has_phone = True
    except Exception as e:
        print(f"⚠️ Error checking CapturedLead for phone: {str(e)}")
    
    # VIP = all three collected: email AND phone AND following. If any one is missed, not VIP.
    is_converted = has_email and has_phone and is_following
    
    return {
        "is_converted": is_converted,
        "has_email": has_email,
        "has_phone": has_phone,
        "is_following": is_following,
        "audience": audience
    }


def update_audience_email(db: Session, sender_id: str, instagram_account_id: int, user_id: int, email: str) -> InstagramAudience:
    """
    Update the audience record with email.
    Called when a user provides their email in any automation.
    
    Args:
        db: Database session
        sender_id: Instagram user ID
        instagram_account_id: Our Instagram account ID
        user_id: Our app's user ID
        email: Email address
        
    Returns:
        Updated InstagramAudience instance
    """
    audience = get_or_create_audience(db, sender_id, instagram_account_id, user_id)
    
    # Only update if email is not already set
    if not audience.email:
        audience.email = email
        audience.email_captured_at = datetime.utcnow()
        db.commit()
        db.refresh(audience)
    
    return audience


def update_audience_following(db: Session, sender_id: str, instagram_account_id: int, user_id: int, is_following: bool = True) -> InstagramAudience:
    """
    Update the audience record with following status.
    Called when a user confirms they're following or when we receive a follow webhook.
    
    Args:
        db: Database session
        sender_id: Instagram user ID
        instagram_account_id: Our Instagram account ID
        user_id: Our app's user ID
        is_following: Following status (default True)
        
    Returns:
        Updated InstagramAudience instance
    """
    audience = get_or_create_audience(db, sender_id, instagram_account_id, user_id)
    
    # Only update if status changed
    if audience.is_following != is_following:
        audience.is_following = is_following
        if is_following and not audience.follow_confirmed_at:
            audience.follow_confirmed_at = datetime.utcnow()
        db.commit()
        db.refresh(audience)
    
    return audience
