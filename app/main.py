"""
Instagram Automation Backend API
Version: 2.1.0 - Strict Mode Lead Capture
Last Updated: 2026-01-21
"""
from dotenv import load_dotenv
load_dotenv()

import logging
import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

# Configure logging for Render compatibility
# Render captures stdout/stderr, but logging module is more reliable
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),  # Use stdout for Render
        logging.StreamHandler(sys.stderr)   # Also use stderr as backup
    ],
    force=True  # Override any existing configuration
)

logger = logging.getLogger(__name__)


def run_migrations() -> None:
    """Run Alembic migrations on startup. Uses alembic.ini and DATABASE_URL.
    Fails startup if migrations fail, so the DB is never left out of sync."""
    project_root = Path(__file__).resolve().parent.parent
    alembic_ini = project_root / "alembic.ini"
    if not alembic_ini.exists():
        logger.warning("alembic.ini not found, skipping Alembic migrations")
        return
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        logger.error(
            "DATABASE_URL is not set. Alembic migrations will not run. "
            "Set DATABASE_URL in your deployment (e.g. Render env) to your Supabase connection string."
        )
        return
    # Normalize postgres:// -> postgresql:// for SQLAlchemy
    if db_url.startswith("postgres://"):
        db_url = "postgresql://" + db_url[10:]
    try:
        alembic_cfg = Config(str(alembic_ini))
        alembic_cfg.set_main_option("sqlalchemy.url", db_url)
        command.upgrade(alembic_cfg, "head")
        logger.info("Alembic migrations completed successfully")
    except Exception as e:
        logger.exception("Alembic migration failed: %s", e)
        raise  # Fail startup so DB is not left out of sync; fix migration or env and redeploy


from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from app.api.routes import auth, instagram, instagram_oauth, automation, webhooks, users, dodo as dodo_router, leads, analytics, support, upload as upload_router
from app.db.session import engine
from app.utils.disposable_email import ensure_blocklist_loaded
from app.db.base import Base
# Import all models to ensure they're registered with Base
from app.models import User, Subscription, InstagramAccount, AutomationRule, DmLog, Follower, CapturedLead, AutomationRuleStats, AnalyticsEvent, Message, Conversation, InstagramAudience, InstagramGlobalTracker

app = FastAPI(title="Instagram Automation SaaS")

@app.on_event("startup")
async def startup_event():
    """Create tables, then run Alembic migrations on every server restart.
    If a migration didn't change your DB (e.g. Supabase still shows old column type):
    1. Check deploy logs for 'Alembic migration failed' or 'DATABASE_URL is not set'.
    2. Ensure DATABASE_URL in production (e.g. Render env) is your Supabase connection string.
    3. If migrations fail, the server will now refuse to start until you fix the cause."""
    import sys
    from sqlalchemy import text

    try:
        print("🔄 Creating database tables...", file=sys.stderr)
        Base.metadata.create_all(bind=engine)
        print("✅ Database tables created", file=sys.stderr)
    except Exception as e:
        print(f"⚠️ Error creating tables: {str(e)}", file=sys.stderr)
        raise

    try:
        print("🔄 Running Alembic migrations...", file=sys.stderr)
        run_migrations()
        print("✅ Alembic migrations completed", file=sys.stderr)
    except Exception as e:
        print(f"❌ Alembic migration failed (server will not start): {str(e)}", file=sys.stderr)
        raise  # Fail startup so you see the error in deploy logs and fix DATABASE_URL / migration
    
    # CRITICAL: Validate and ensure EventType enum values exist
    # This prevents the recurring issue where enum values are missing from the database
    try:
        print("🔄 Validating EventType enum values...", file=sys.stderr)
        from app.utils.enum_validator import validate_eventtype_enum, ensure_eventtype_enum_values
        from app.db.session import SessionLocal
        
        db = SessionLocal()
        try:
            is_valid, missing = validate_eventtype_enum(db)
            if not is_valid:
                print(f"⚠️  Missing enum values detected: {missing}. Attempting to add them...", file=sys.stderr)
                if ensure_eventtype_enum_values(db):
                    # Re-validate after adding
                    is_valid, still_missing = validate_eventtype_enum(db)
                    if is_valid:
                        print("✅ All enum values now exist in database", file=sys.stderr)
                    else:
                        print(f"⚠️  Warning: Some enum values still missing after auto-fix: {still_missing}", file=sys.stderr)
                        # Don't fail startup - migrations should handle this, but log the warning
                else:
                    print("⚠️  Failed to auto-fix missing enum values. Check migrations.", file=sys.stderr)
            else:
                print("✅ EventType enum validation passed", file=sys.stderr)
        finally:
            db.close()
    except Exception as e:
        print(f"⚠️  Enum validation warning: {str(e)}", file=sys.stderr)
        # Don't fail startup - migrations should handle enum creation
        # This is a safety check, not a blocker
    
    # Temporary fix: Ensure profile_picture_url column exists (backup in case migration didn't run)
    try:
        print("🔄 Checking profile_picture_url column...", file=sys.stderr)
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_picture_url VARCHAR"))
            conn.commit()
            print("✅ profile_picture_url column verified", file=sys.stderr)
    except Exception as e:
        print(f"⚠️ profile_picture_url column check warning: {str(e)}", file=sys.stderr)
        # Don't fail startup if this doesn't work - migration should handle it

    # Ensure invoices.amount is NUMERIC so 11.81 is stored correctly (not rounded to 12)
    try:
        print("🔄 Checking invoices.amount column type...", file=sys.stderr)
        with engine.connect() as conn:
            r = conn.execute(text("""
                SELECT data_type FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'invoices' AND column_name = 'amount'
            """)).fetchone()
            if r and r[0] in ("integer", "smallint", "bigint"):
                conn.execute(text("""
                    ALTER TABLE public.invoices
                    ALTER COLUMN amount TYPE NUMERIC(12, 2) USING
                    (CASE WHEN amount >= 100 THEN amount / 100.0 ELSE amount::numeric END)
                """))
                conn.commit()
                print("✅ invoices.amount converted to NUMERIC(12,2)", file=sys.stderr)
            else:
                print("✅ invoices.amount already numeric", file=sys.stderr)
    except Exception as e:
        print(f"⚠️ invoices.amount check warning: {str(e)}", file=sys.stderr)

    # Load disposable email blocklist at startup so production logs show it and we catch missing file early
    try:
        n = ensure_blocklist_loaded()
        print(f"✅ Disposable email blocklist loaded: {n} domains", file=sys.stderr)
    except Exception as e:
        print(f"⚠️ Disposable email blocklist load warning: {str(e)}", file=sys.stderr)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        "http://localhost:5173",
        "https://gnathonic-lashell-unconversable.ngrok-free.dev",  # Local development + ngrok
        "https://www.logicdm.app",  # Production frontend
        "https://logicdm.app",      # Production frontend (without www)
    ],
    allow_origin_regex=r"https://.*\.(onrender\.com|ngrok-free\.app|ngrok\.io)",  # Render deployment + ngrok patterns
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*", "ngrok-skip-browser-warning"],  # Allow ngrok bypass header
)

# Register routers
app.include_router(auth.router, prefix="/auth", tags=["Auth"])
app.include_router(users.router, prefix="/users", tags=["Users"])
app.include_router(instagram.router, prefix="/api/instagram", tags=["Instagram"])
app.include_router(instagram_oauth.router, prefix="/api/instagram", tags=["Instagram OAuth"])
app.include_router(automation.router, prefix="/automation", tags=["Automation"])
app.include_router(webhooks.router, prefix="/webhooks", tags=["Webhooks"])
app.include_router(dodo_router.router, prefix="/api/dodo", tags=["Dodo Payments"])
app.include_router(leads.router, prefix="/api", tags=["Leads"])
app.include_router(analytics.router, prefix="/api/analytics", tags=["Analytics"])
app.include_router(support.router, prefix="/support", tags=["Support"])
app.include_router(upload_router.router, prefix="/api", tags=["Upload"])

# Serve uploaded media (automation video/image) at /uploads/...
try:
    from app.api.routes.upload import get_uploads_dir
    uploads_dir = get_uploads_dir()
    app.mount("/uploads", StaticFiles(directory=str(uploads_dir)), name="uploads")
except Exception as e:
    import sys
    print(f"⚠️ Uploads mount skipped: {e}", file=sys.stderr)