from typing import Dict

# Plan limits configuration
# Free tier: 1 Account, Unlimited Rules, 1000 DMs/month (High Volume pricing)
PLAN_LIMITS: Dict[str, Dict[str, int]] = {
    "free": {
        "max_accounts": 1,
        "max_dms_per_month": 1000,  # Monthly limit, not daily
        "max_automation_rules": -1,  # -1 means unlimited
    },
    "basic": {
        "max_accounts": 3,
        "max_dms_per_month": 500,
        "max_automation_rules": 10,
    },
    "pro": {
        "max_accounts": 3,
        "max_dms_per_month": -1,  # -1 means unlimited (High Volume pricing)
        "max_automation_rules": -1,  # -1 means unlimited
    },
    "enterprise": {
        "max_accounts": 50,
        "max_dms_per_month": 10000,
        "max_automation_rules": 100,
    },
}

# Global usage tracking limits (per Instagram account, not per user)
# These are used for persistent tracking to prevent free tier abuse
FREE_DM_LIMIT = 1000  # Lifetime limit for free tier (High Volume pricing)
PRO_DM_LIMIT = -1  # -1 means unlimited for pro tier (High Volume pricing)
FREE_RULE_LIMIT = -1  # -1 means unlimited for free tier (High Volume pricing)
PRO_RULE_LIMIT = -1  # -1 means unlimited for pro tier (High Volume pricing)


def get_plan_limit(plan_tier: str, limit_type: str) -> int:
    """Get the limit value for a specific plan and limit type."""
    return PLAN_LIMITS.get(plan_tier, PLAN_LIMITS["free"]).get(limit_type, 0)
