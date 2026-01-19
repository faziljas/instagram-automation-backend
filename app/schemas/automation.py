from pydantic import BaseModel
from typing import Dict, Any
from datetime import datetime


class AutomationRuleCreate(BaseModel):
    instagram_account_id: int
    name: str
    trigger_type: str
    action_type: str
    config: Dict[str, Any]


class AutomationRuleUpdate(BaseModel):
    name: str | None = None
    trigger_type: str | None = None
    action_type: str | None = None
    config: Dict[str, Any] | None = None
    is_active: bool | None = None


class AutomationRuleResponse(BaseModel):
    id: int
    instagram_account_id: int
    name: str | None
    trigger_type: str
    action_type: str
    config: Dict[str, Any]
    media_id: str | None = None  # Instagram media ID (post/reel/story) this rule is tied to
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True
