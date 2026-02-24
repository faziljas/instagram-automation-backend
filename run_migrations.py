"""
Run schema migrations before the app starts.
Render startCommand runs: python run_migrations.py && uvicorn ...
So every auto-deploy from git runs all migrations with no manual step.

Add new migration modules to MIGRATIONS (same order as app/main.py startup).
Migrations must be idempotent (safe to run multiple times).
"""
import importlib.util
import os
import sys

# Migrations to run on every deploy, in order. Each must define run_migration() and be idempotent.
# Order matches app/main.py startup so Render auto-deploy runs all migrations with no manual step.
MIGRATIONS = [
    "add_follow_button_clicks_migration",
    "add_conversation_migration",
    "add_instagram_audience_migration",
    "add_billing_cycle_migration",
    "add_dm_log_username_igsid_migration",
    "add_instagram_account_created_at_migration",
    "add_instagram_global_tracker_migration",
    "update_instagram_global_tracker_user_id_migration",
    "make_automation_rules_account_id_nullable_migration",
    "make_captured_leads_instagram_account_id_nullable_migration",
    "add_supabase_id_migration",
    "update_high_volume_limits_migration",
    "update_pro_plan_limits_migration",
    "add_profile_picture",
    "add_free_tier_usage_migration",
]


def run():
    root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(root)
    ran = 0
    for name in MIGRATIONS:
        path = os.path.join(root, f"{name}.py")
        if not os.path.exists(path):
            print(f"⚠️ Skip {name}: file not found")
            continue
        print(f"▶ Running {name}...")
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if not hasattr(mod, "run_migration"):
            print(f"⚠️ No run_migration() in {name}")
            continue
        ok = mod.run_migration()
        if not ok:
            print(f"❌ {name} failed")
            return False
        ran += 1
    print(f"✅ Migrations completed ({ran} ran)")
    return True


if __name__ == "__main__":
    success = run()
    sys.exit(0 if success else 1)
