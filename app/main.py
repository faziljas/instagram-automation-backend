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
    """Run Alembic migrations programmatically. Uses alembic.ini and DATABASE_URL."""
    try:
        project_root = Path(__file__).resolve().parent.parent
        alembic_ini = project_root / "alembic.ini"
        if not alembic_ini.exists():
            logger.info("alembic.ini not found, skipping Alembic migrations")
            return
        alembic_cfg = Config(str(alembic_ini))
        db_url = os.getenv("DATABASE_URL")
        if db_url:
            alembic_cfg.set_main_option("sqlalchemy.url", db_url)
        command.upgrade(alembic_cfg, "head")
        logger.info("Alembic migrations completed successfully")
    except Exception as e:
        logger.exception("Alembic migration error: %s", e)


from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.routes import auth, instagram, instagram_oauth, automation, webhooks, users, dodo as dodo_router, leads, analytics, support
from app.db.session import engine
from app.db.base import Base
# Import all models to ensure they're registered with Base
from app.models import User, Subscription, InstagramAccount, AutomationRule, DmLog, Follower, CapturedLead, AutomationRuleStats, AnalyticsEvent, Message, Conversation, InstagramAudience, InstagramGlobalTracker

app = FastAPI(title="Instagram Automation SaaS")

@app.on_event("startup")
async def startup_event():
    """Create tables, then run Alembic migrations on every server restart. All schema changes live in revision files."""
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
        print(f"⚠️ Alembic migration warning: {str(e)}", file=sys.stderr)
    
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