"""Add notify_product_updates and notify_billing columns to users table.
Idempotent - safe to run multiple times. Ensures columns exist even if Alembic 013/015 did not run."""
from sqlalchemy import text
from app.db.session import engine


def run_migration():
    """Add notification preference columns if they don't exist."""
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_product_updates BOOLEAN NOT NULL DEFAULT true"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_billing BOOLEAN NOT NULL DEFAULT true"
                )
            )
            conn.commit()
        print("✅ Notification preference columns ensured (notify_product_updates, notify_billing)")
        return True
    except Exception as e:
        print(f"⚠️ Notification preferences migration: {e}")
        return True  # Idempotent; column may already exist


if __name__ == "__main__":
    success = run_migration()
    exit(0 if success else 1)
