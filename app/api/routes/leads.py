"""
API endpoints for managing captured leads.
"""
from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.orm import Session
from typing import List, Optional
from app.db.session import get_db
from app.models.captured_lead import CapturedLead
from app.models.automation_rule import AutomationRule
from app.models.instagram_account import InstagramAccount
from app.dependencies.auth import get_current_user_id
from pydantic import BaseModel
from datetime import datetime

router = APIRouter()


class CapturedLeadResponse(BaseModel):
    id: int
    user_id: int
    instagram_account_id: int | None  # Can be None when account is disconnected
    automation_rule_id: int
    email: str | None
    phone: str | None
    name: str | None
    custom_fields: dict | None
    extra_metadata: dict | None
    captured_at: datetime
    notified: bool
    exported: bool

    class Config:
        from_attributes = True


@router.get("/leads", response_model=List[CapturedLeadResponse])
def get_captured_leads(
    authorization: str = Header(None),
    automation_rule_id: Optional[int] = None,
    instagram_account_id: Optional[int] = None,
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id)
):
    """
    Get all captured leads for the current user.
    Optionally filter by automation_rule_id or instagram_account_id.
    Excludes leads with NULL instagram_account_id (disconnected accounts) to match analytics behavior.
    """
    try:
        query = db.query(CapturedLead).filter(
            CapturedLead.user_id == user_id,
            CapturedLead.instagram_account_id.isnot(None)  # Exclude disconnected account leads
        )
        
        if automation_rule_id:
            query = query.filter(CapturedLead.automation_rule_id == automation_rule_id)
        
        if instagram_account_id:
            query = query.filter(CapturedLead.instagram_account_id == instagram_account_id)
        
        leads = query.order_by(CapturedLead.captured_at.desc()).all()
        
        return leads
    except HTTPException:
        # Re-raise HTTP exceptions (like 401, 404) as-is
        raise
    except Exception as e:
        print(f"❌ Error fetching leads for user {user_id}: {str(e)}")
        import traceback
        traceback.print_exc()
        # Return empty list instead of raising 500 error
        # This prevents network errors for new users or users with no leads
        return []


@router.get("/leads/stats")
def get_leads_stats(
    authorization: str = Header(None),
    automation_rule_id: Optional[int] = None,
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id)
):
    """
    Get statistics about captured leads.
    """
    try:
        query = db.query(CapturedLead).filter(CapturedLead.user_id == user_id)
        
        if automation_rule_id:
            query = query.filter(CapturedLead.automation_rule_id == automation_rule_id)
        
        total_leads = query.count()
        total_with_email = query.filter(CapturedLead.email.isnot(None)).count()
        total_with_phone = query.filter(CapturedLead.phone.isnot(None)).count()
        
        return {
            "total_leads": total_leads,
            "total_with_email": total_with_email,
            "total_with_phone": total_with_phone,
        }
    except HTTPException:
        # Re-raise HTTP exceptions (like 401, 404) as-is
        raise
    except Exception as e:
        print(f"❌ Error fetching leads stats for user {user_id}: {str(e)}")
        import traceback
        traceback.print_exc()
        # Return zero stats instead of raising 500 error
        return {
            "total_leads": 0,
            "total_with_email": 0,
            "total_with_phone": 0,
        }


@router.delete("/leads/{lead_id}")
def delete_captured_lead(
    lead_id: int,
    authorization: str = Header(None),
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id)
):
    """
    Delete a captured lead (only if it belongs to the current user).
    """
    lead = db.query(CapturedLead).filter(
        CapturedLead.id == lead_id,
        CapturedLead.user_id == user_id
    ).first()
    
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    
    db.delete(lead)
    db.commit()
    
    return {"status": "success", "message": "Lead deleted successfully"}
