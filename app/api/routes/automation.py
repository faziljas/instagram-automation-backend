from datetime import datetime
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Header
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models.automation_rule import AutomationRule
from app.models.automation_rule_stats import AutomationRuleStats
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
    # Return all non-deleted rules (active + inactive) so toggled-off rules remain visible
    query = db.query(AutomationRule).join(InstagramAccount).filter(
        InstagramAccount.user_id == user_id,
        AutomationRule.deleted_at.is_(None)
    )

    if instagram_account_id:
        query = query.filter(AutomationRule.instagram_account_id == instagram_account_id)

    rules = query.all()
    rule_ids = [r.id for r in rules]
    stats_map: dict[int, AutomationRuleStats] = {}
    if rule_ids:
        for s in db.query(AutomationRuleStats).filter(
            AutomationRuleStats.automation_rule_id.in_(rule_ids)
        ):
            stats_map[s.automation_rule_id] = s

    result: List[AutomationRuleResponse] = []
    for r in rules:
        st = stats_map.get(r.id)
        result.append(AutomationRuleResponse(
            id=r.id,
            instagram_account_id=r.instagram_account_id,
            name=r.name,
            trigger_type=r.trigger_type,
            action_type=r.action_type,
            config=r.config,
            media_id=r.media_id,
            is_active=r.is_active,
            created_at=r.created_at,
            total_triggers=st.total_triggers if st else 0,
            last_triggered_at=st.last_triggered_at if st else None,
        ))
    return result


@router.get("/rules/{rule_id}", response_model=AutomationRuleResponse)
def get_automation_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id)
):
    # Allow viewing/editing inactive rules; exclude deleted
    rule = db.query(AutomationRule).join(InstagramAccount).filter(
        AutomationRule.id == rule_id,
        InstagramAccount.user_id == user_id,
        AutomationRule.deleted_at.is_(None)
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
        InstagramAccount.user_id == user_id,
        AutomationRule.deleted_at.is_(None)
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

    # Soft delete: set deleted_at so rule is excluded from list (toggled-off rules stay visible).
    # Preserve analytics, stats, and leads; set rule_id to NULL on analytics events.
    from app.models.analytics_event import AnalyticsEvent

    updated_analytics = db.query(AnalyticsEvent).filter(
        AnalyticsEvent.rule_id == rule_id
    ).update({"rule_id": None})
    rule.is_active = False
    rule.deleted_at = datetime.utcnow()
    db.commit()

    print(f"✅ Rule {rule_id} soft deleted (deleted_at set) - excluded from list, analytics preserved")
    print(f"   Analytics events: {updated_analytics} preserved (rule_id set to NULL)")

    return None
