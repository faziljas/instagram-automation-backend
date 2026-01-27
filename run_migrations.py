"""
Run schema migrations before each deploy.
Called by Render's preDeployCommand so migrations run automatically on every deployment.

Add new migration modules here when they should run on deploy.
Migrations must be idempotent (safe to run multiple times).
"""
import importlib.util
import os
import sys

# Migrations to run on every deploy, in order. Each must define run_migration() and be idempotent.
MIGRATIONS = [
    "make_automation_rules_account_id_nullable_migration",
    "make_captured_leads_instagram_account_id_nullable_migration",
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
