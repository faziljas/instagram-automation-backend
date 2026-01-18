from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Header
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models.automation_rule import AutomationRule
from app.models.instagram_account import InstagramAccount
from app.schemas.automation import AutomationRuleCreate, AutomationRuleUpdate, AutomationRuleResponse
from app.utils.auth import verify_token
from app.utils.plan_enforcement import check_rule_limit

router = APIRouter()


def get_current_user_id(authorization: str = Header(None)) -> int:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required"
        )
    
    try:
        scheme, token = authorization.split()
        if scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication scheme"
            )
        
        payload = verify_token(token)
        if not payload:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token"
            )
        
        user_id = int(payload.get("sub"))
        return user_id
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token format"
        )


@router.post("/rules", response_model=AutomationRuleResponse, status_code=status.HTTP_201_CREATED)
def create_automation_rule(
    rule_data: AutomationRuleCreate,
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id)
):
    # Verify Instagram account belongs to user
    ig_account = db.query(InstagramAccount).filter(
        InstagramAccount.id == rule_data.instagram_account_id,
        InstagramAccount.user_id == user_id
    ).first()

    if not ig_account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Instagram account not found"
        )

    # Check rule limit BEFORE creating
    check_rule_limit(user_id, db)

    # Create automation rule
    rule = AutomationRule(
        instagram_account_id=rule_data.instagram_account_id,
        name=rule_data.name,
        trigger_type=rule_data.trigger_type,
        action_type=rule_data.action_type,
        config=rule_data.config,
        media_id=rule_data.config.get('media_id'),  # Extract media_id from config if present
        is_active=True
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)

    return rule


@router.get("/rules", response_model=List[AutomationRuleResponse])
def list_automation_rules(
    instagram_account_id: int = None,
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id)
):
    query = db.query(AutomationRule).join(InstagramAccount).filter(
        InstagramAccount.user_id == user_id
    )

    if instagram_account_id:
        query = query.filter(AutomationRule.instagram_account_id == instagram_account_id)

    rules = query.all()
    return rules


@router.get("/rules/{rule_id}", response_model=AutomationRuleResponse)
def get_automation_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id)
):
    rule = db.query(AutomationRule).join(InstagramAccount).filter(
        AutomationRule.id == rule_id,
        InstagramAccount.user_id == user_id
    ).first()

    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Automation rule not found"
        )

    return rule


@router.put("/rules/{rule_id}", response_model=AutomationRuleResponse)
def update_automation_rule(
    rule_id: int,
    rule_update: AutomationRuleUpdate,
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id)
):
    rule = db.query(AutomationRule).join(InstagramAccount).filter(
        AutomationRule.id == rule_id,
        InstagramAccount.user_id == user_id
    ).first()

    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Automation rule not found"
        )

    # Update fields
    if rule_update.name is not None:
        rule.name = rule_update.name
    if rule_update.trigger_type is not None:
        rule.trigger_type = rule_update.trigger_type
    if rule_update.action_type is not None:
        rule.action_type = rule_update.action_type
    if rule_update.config is not None:
        rule.config = rule_update.config
    if rule_update.is_active is not None:
        rule.is_active = rule_update.is_active

    db.commit()
    db.refresh(rule)

    return rule


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_automation_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id)
):
    rule = db.query(AutomationRule).join(InstagramAccount).filter(
        AutomationRule.id == rule_id,
        InstagramAccount.user_id == user_id
    ).first()

    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Automation rule not found"
        )

    db.delete(rule)
    db.commit()

    return None
