from typing import Dict

# Plan limits configuration
PLAN_LIMITS: Dict[str, Dict[str, int]] = {
    "free": {
        "max_accounts": 1,
        "max_dms_per_day": 10,
        "max_automation_rules": 1,
    },
    "basic": {
        "max_accounts": 3,
        "max_dms_per_day": 100,
        "max_automation_rules": 5,
    },
    "pro": {
        "max_accounts": 10,
        "max_dms_per_day": 500,
        "max_automation_rules": 20,
    },
    "enterprise": {
        "max_accounts": 50,
        "max_dms_per_day": 2000,
        "max_automation_rules": 100,
    },
}


def get_plan_limit(plan_tier: str, limit_type: str) -> int:
    """Get the limit value for a specific plan and limit type."""
    return PLAN_LIMITS.get(plan_tier, PLAN_LIMITS["free"]).get(limit_type, 0)
