"""
Instagram Automation Backend API
Version: 2.1.0 - Strict Mode Lead Capture
Last Updated: 2026-01-21
"""
from dotenv import load_dotenv
load_dotenv()

import logging
import sys

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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.routes import auth, instagram, instagram_oauth, automation, webhooks, users, stripe as stripe_router, leads, analytics
from app.db.session import engine
from app.db.base import Base
from sqlalchemy import text
# Import all models to ensure they're registered with Base
from app.models import User, Subscription, InstagramAccount, AutomationRule, DmLog, Follower, CapturedLead, AutomationRuleStats, AnalyticsEvent, Message, Conversation, InstagramAudience

app = FastAPI(title="Instagram Automation SaaS")

@app.on_event("startup")
async def startup_event():
    """Create all database tables and handle migrations"""
    import sys
    
    # Create all tables first
    try:
        print("🔄 Creating database tables...", file=sys.stderr)
        Base.metadata.create_all(bind=engine)
        print("✅ Database tables created successfully", file=sys.stderr)
    except Exception as e:
        print(f"⚠️ Error creating tables: {str(e)}", file=sys.stderr)
        raise
    
    # Run migrations automatically
    try:
        print("🔄 Running database migrations...", file=sys.stderr)
        from add_follow_button_clicks_migration import run_migration
        run_migration()
        print("✅ Migrations completed successfully", file=sys.stderr)
    except Exception as e:
        print(f"⚠️ Migration warning (may already be applied): {str(e)}", file=sys.stderr)
        # Don't raise - migrations are idempotent and may already be applied
    
    # Run conversation migration
    try:
        from add_conversation_migration import run_migration as run_conv_migration
        run_conv_migration()
        print("✅ Conversation migration completed", file=sys.stderr)
    except Exception as e:
        print(f"⚠️ Conversation migration warning (may already be applied): {str(e)}", file=sys.stderr)
        # Don't raise - migrations are idempotent
    
    # Run InstagramAudience migration (for global conversion tracking)
    try:
        print("🔄 Running InstagramAudience migration...", file=sys.stderr)
        from add_instagram_audience_migration import run_migration as run_audience_migration
        run_audience_migration()
        print("✅ InstagramAudience migration completed", file=sys.stderr)
    except Exception as e:
        print(f"⚠️ InstagramAudience migration warning (may already be applied): {str(e)}", file=sys.stderr)
        # Don't raise - migrations are idempotent
    
    # Run billing cycle migration
    try:
        print("🔄 Running billing cycle migration...", file=sys.stderr)
        from add_billing_cycle_migration import run_migration as run_billing_cycle_migration
        run_billing_cycle_migration()
        print("✅ Billing cycle migration completed", file=sys.stderr)
    except Exception as e:
        print(f"⚠️ Billing cycle migration warning (may already be applied): {str(e)}", file=sys.stderr)
        # Don't raise - migrations are idempotent

    # Run dm_logs username/igsid migration (allows account delete while preserving usage)
    try:
        print("🔄 Running dm_logs username/igsid migration...", file=sys.stderr)
        from add_dm_log_username_igsid_migration import run_migration as run_dm_log_migration
        run_dm_log_migration()
        print("✅ dm_logs migration completed", file=sys.stderr)
    except Exception as e:
        print(f"⚠️ dm_logs migration warning (may already be applied): {str(e)}", file=sys.stderr)
        # Don't raise - migrations are idempotent

    # Run instagram_accounts created_at migration
    try:
        print("🔄 Running instagram_accounts created_at migration...", file=sys.stderr)
        from add_instagram_account_created_at_migration import run_migration as run_account_created_at_migration
        run_account_created_at_migration()
        print("✅ instagram_accounts created_at migration completed", file=sys.stderr)
    except Exception as e:
        print(f"⚠️ instagram_accounts created_at migration warning (may already be applied): {str(e)}", file=sys.stderr)
        # Don't raise - migrations are idempotent
    
    # Try Alembic migrations (if Alembic is configured)
    try:
        import subprocess
        import os
        print("🔄 Attempting Alembic migrations...", file=sys.stderr)
        # Check if alembic.ini exists
        if os.path.exists("alembic.ini"):
            # Run alembic upgrade head
            result = subprocess.run(
                ["alembic", "upgrade", "head"],
                capture_output=True,
                text=True,
                timeout=60
            )
            if result.returncode == 0:
                print("✅ Alembic migrations completed successfully", file=sys.stderr)
            else:
                print(f"⚠️ Alembic migration output: {result.stdout}", file=sys.stderr)
                print(f"⚠️ Alembic migration errors: {result.stderr}", file=sys.stderr)
        else:
            print("ℹ️ Alembic not configured (alembic.ini not found), skipping Alembic migrations", file=sys.stderr)
    except FileNotFoundError:
        print("ℹ️ Alembic command not found, skipping Alembic migrations", file=sys.stderr)
    except Exception as e:
        print(f"⚠️ Alembic migration warning (may not be configured): {str(e)}", file=sys.stderr)
        # Don't raise - Alembic is optional
    
    # Auto-migrate: Add columns if they don't exist
    try:
        with engine.begin() as conn:
            # Check database type for compatibility
            db_type = str(engine.url).split("://")[0]
            
            def column_exists(table_name: str, column_name: str) -> bool:
                """Check if a column exists in a table (SQLite/PostgreSQL compatible)"""
                if db_type == "postgresql":
                    result = conn.execute(text(f"""
                        SELECT column_name 
                        FROM information_schema.columns 
                        WHERE table_name='{table_name}' AND column_name='{column_name}'
                    """))
                    return result.fetchone() is not None
                else:  # SQLite
                    pragma_result = conn.execute(text(f"PRAGMA table_info({table_name})"))
                    columns = [row[1] for row in pragma_result.fetchall()]
                    return column_name in columns
            
            # Check and add igsid column
            if not column_exists('instagram_accounts', 'igsid'):
                print("🔄 Auto-migrating: Adding igsid column to instagram_accounts...", file=sys.stderr)
                conn.execute(text("ALTER TABLE instagram_accounts ADD COLUMN igsid VARCHAR(255)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_instagram_accounts_igsid ON instagram_accounts(igsid)"))
                print("✅ Auto-migration complete: igsid column added", file=sys.stderr)
            else:
                print("✅ igsid column already exists", file=sys.stderr)
            
            # Check and add page_id column
            if not column_exists('instagram_accounts', 'page_id'):
                print("🔄 Auto-migrating: Adding page_id column to instagram_accounts...", file=sys.stderr)
                conn.execute(text("ALTER TABLE instagram_accounts ADD COLUMN page_id VARCHAR(255)"))
                print("✅ Auto-migration complete: page_id column added", file=sys.stderr)
            else:
                print("✅ page_id column already exists", file=sys.stderr)
            
            # Check and add encrypted_page_token column
            if not column_exists('instagram_accounts', 'encrypted_page_token'):
                print("🔄 Auto-migrating: Adding encrypted_page_token column to instagram_accounts...", file=sys.stderr)
                conn.execute(text("ALTER TABLE instagram_accounts ADD COLUMN encrypted_page_token TEXT"))
                print("✅ Auto-migration complete: encrypted_page_token column added", file=sys.stderr)
            else:
                print("✅ encrypted_page_token column already exists", file=sys.stderr)
            
            # Check and add media_id column to automation_rules
            if not column_exists('automation_rules', 'media_id'):
                print("🔄 Auto-migrating: Adding media_id column to automation_rules...", file=sys.stderr)
                conn.execute(text("ALTER TABLE automation_rules ADD COLUMN media_id VARCHAR(255)"))
                print("✅ Auto-migration complete: media_id column added", file=sys.stderr)
            else:
                print("✅ media_id column already exists", file=sys.stderr)
            
            # Check and add follow_button_clicks columns to automation_rule_stats
            if not column_exists('automation_rule_stats', 'total_follow_button_clicks'):
                print("🔄 Auto-migrating: Adding total_follow_button_clicks column...", file=sys.stderr)
                conn.execute(text("ALTER TABLE automation_rule_stats ADD COLUMN total_follow_button_clicks INTEGER DEFAULT 0"))
                print("✅ Auto-migration complete: total_follow_button_clicks column added", file=sys.stderr)
            else:
                print("✅ total_follow_button_clicks column already exists", file=sys.stderr)
            
            if not column_exists('automation_rule_stats', 'last_follow_button_clicked_at'):
                print("🔄 Auto-migrating: Adding last_follow_button_clicked_at column...", file=sys.stderr)
                conn.execute(text("ALTER TABLE automation_rule_stats ADD COLUMN last_follow_button_clicked_at TIMESTAMP"))
                print("✅ Auto-migration complete: last_follow_button_clicked_at column added", file=sys.stderr)
            else:
                print("✅ last_follow_button_clicked_at column already exists", file=sys.stderr)
            
            # Check and add button analytics columns to automation_rule_stats
            if not column_exists('automation_rule_stats', 'total_profile_visits'):
                print("🔄 Auto-migrating: Adding total_profile_visits column...", file=sys.stderr)
                conn.execute(text("ALTER TABLE automation_rule_stats ADD COLUMN total_profile_visits INTEGER DEFAULT 0"))
                print("✅ Auto-migration complete: total_profile_visits column added", file=sys.stderr)
            else:
                print("✅ total_profile_visits column already exists", file=sys.stderr)
            
            if not column_exists('automation_rule_stats', 'total_im_following_clicks'):
                print("🔄 Auto-migrating: Adding total_im_following_clicks column...", file=sys.stderr)
                conn.execute(text("ALTER TABLE automation_rule_stats ADD COLUMN total_im_following_clicks INTEGER DEFAULT 0"))
                print("✅ Auto-migration complete: total_im_following_clicks column added", file=sys.stderr)
            else:
                print("✅ total_im_following_clicks column already exists", file=sys.stderr)
            
            if not column_exists('automation_rule_stats', 'last_profile_visit_at'):
                print("🔄 Auto-migrating: Adding last_profile_visit_at column...", file=sys.stderr)
                conn.execute(text("ALTER TABLE automation_rule_stats ADD COLUMN last_profile_visit_at TIMESTAMP"))
                print("✅ Auto-migration complete: last_profile_visit_at column added", file=sys.stderr)
            else:
                print("✅ last_profile_visit_at column already exists", file=sys.stderr)
            
            if not column_exists('automation_rule_stats', 'last_im_following_clicked_at'):
                print("🔄 Auto-migrating: Adding last_im_following_clicked_at column...", file=sys.stderr)
                conn.execute(text("ALTER TABLE automation_rule_stats ADD COLUMN last_im_following_clicked_at TIMESTAMP"))
                print("✅ Auto-migration complete: last_im_following_clicked_at column added", file=sys.stderr)
            else:
                print("✅ last_im_following_clicked_at column already exists", file=sys.stderr)
            
            # Check and add media_preview_url column to analytics_events
            if not column_exists('analytics_events', 'media_preview_url'):
                print("🔄 Auto-migrating: Adding media_preview_url column to analytics_events...", file=sys.stderr)
                conn.execute(text("ALTER TABLE analytics_events ADD COLUMN media_preview_url VARCHAR(500)"))
                print("✅ Auto-migration complete: media_preview_url column added", file=sys.stderr)
            else:
                print("✅ media_preview_url column already exists", file=sys.stderr)
            
    except Exception as e:
        print(f"⚠️ Auto-migration warning: {str(e)}", file=sys.stderr)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001", "https://gnathonic-lashell-unconversable.ngrok-free.dev"],  # Local development + ngrok
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
app.include_router(stripe_router.router, prefix="/api/stripe", tags=["Stripe"])
app.include_router(leads.router, prefix="/api", tags=["Leads"])
app.include_router(analytics.router, prefix="/api/analytics", tags=["Analytics"])