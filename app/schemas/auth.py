from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import datetime


class UserCreate(BaseModel):
    email: EmailStr
    password: str


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str


class UserResponse(BaseModel):
    id: int
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    plan_tier: str
    is_active: bool
    is_verified: bool
    created_at: Optional[str] = None
    
    class Config:
        from_attributes = True


class UserUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[EmailStr] = None


class PasswordChange(BaseModel):
    old_password: str
    new_password: str


class UserSyncRequest(BaseModel):
    id: str  # Supabase user ID
    email: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


class DashboardUser(BaseModel):
    id: int
    email: str
    plan_tier: str
    created_at: Optional[str] = None


class DashboardStats(BaseModel):
    accounts_count: int
    active_rules_count: int
    dms_sent_today: int
    total_dms_sent: int


class DashboardStatsResponse(BaseModel):
    user: DashboardUser
    stats: DashboardStats


class SubscriptionUsage(BaseModel):
    accounts: int
    rules: int
    dms_sent_this_month: int


class SubscriptionResponse(BaseModel):
    plan_tier: str
    status: str
    stripe_subscription_id: Optional[str] = None
    usage: SubscriptionUsage
