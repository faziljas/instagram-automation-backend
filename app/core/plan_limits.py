from typing import Dict

# Plan limits configuration
# Free tier: 1 Account, 3 Rules, 50 DMs/month
PLAN_LIMITS: Dict[str, Dict[str, int]] = {
    "free": {
        "max_accounts": 1,
        "max_dms_per_month": 50,  # Monthly limit, not daily
        "max_automation_rules": 3,
    },
    "basic": {
        "max_accounts": 3,
        "max_dms_per_month": 500,
        "max_automation_rules": 10,
    },
    "pro": {
        "max_accounts": 10,
        "max_dms_per_month": 5000,
        "max_automation_rules": 50,
    },
    "enterprise": {
        "max_accounts": 50,
        "max_dms_per_month": 10000,
        "max_automation_rules": 100,
    },
}

# Global usage tracking limits (per Instagram account, not per user)
# These are used for persistent tracking to prevent free tier abuse
FREE_DM_LIMIT = 50  # Lifetime limit for free tier
PRO_DM_LIMIT = 5000  # Monthly limit for pro tier
FREE_RULE_LIMIT = 3  # Lifetime limit for free tier (total rules created, not active)
PRO_RULE_LIMIT = 100  # Monthly limit for pro tier (total rules created, not active)


def get_plan_limit(plan_tier: str, limit_type: str) -> int:
    """Get the limit value for a specific plan and limit type."""
    return PLAN_LIMITS.get(plan_tier, PLAN_LIMITS["free"]).get(limit_type, 0)
