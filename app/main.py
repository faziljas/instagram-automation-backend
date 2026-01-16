from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.routes import auth, instagram, instagram_oauth, automation, webhooks, users
from app.db.session import engine
from app.db.base import Base
from sqlalchemy import text
# Import all models to ensure they're registered with Base
from app.models import User, Subscription, InstagramAccount, AutomationRule, DmLog, Follower

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
    
    # Auto-migrate: Add igsid column if it doesn't exist
    try:
        with engine.begin() as conn:
            # Check if column exists
            result = conn.execute(text("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name='instagram_accounts' AND column_name='igsid'
            """))
            exists = result.fetchone() is not None
            
            if not exists:
                print("🔄 Auto-migrating: Adding igsid column to instagram_accounts...", file=sys.stderr)
                conn.execute(text("""
                    ALTER TABLE instagram_accounts ADD COLUMN igsid VARCHAR(255);
                    CREATE INDEX IF NOT EXISTS ix_instagram_accounts_igsid ON instagram_accounts(igsid);
                """))
                print("✅ Auto-migration complete: igsid column added", file=sys.stderr)
            else:
                print("✅ igsid column already exists", file=sys.stderr)
    except Exception as e:
        print(f"⚠️ Auto-migration warning: {str(e)}", file=sys.stderr)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],  # Local development
    allow_origin_regex=r"https://.*\.onrender\.com",  # Render deployment
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
