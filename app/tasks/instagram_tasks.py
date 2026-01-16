import json
from datetime import datetime
from app.celery_app import celery_app
from app.db.session import SessionLocal
from app.models.instagram_account import InstagramAccount
from app.models.follower import Follower
from app.services.instagram_client import InstagramClient
from app.utils.encryption import decrypt_credentials


@celery_app.task(name="fetch_followers_for_account")
def fetch_followers_for_account(instagram_account_id: int):
    db = SessionLocal()
    try:
        # Get Instagram account
        ig_account = db.query(InstagramAccount).filter(
            InstagramAccount.id == instagram_account_id,
            InstagramAccount.is_active == True
        ).first()

        if not ig_account:
            return {"status": "error", "message": "Account not found or inactive"}

        # Decrypt credentials
        credentials_json = decrypt_credentials(ig_account.encrypted_credentials)
        credentials = json.loads(credentials_json)

        # Authenticate Instagram client
        client = InstagramClient()
        client.authenticate(credentials["username"], credentials["password"])

        # Fetch followers
        followers = client.get_followers()

        # Store followers
        for follower in followers:
            existing = db.query(Follower).filter(
                Follower.instagram_account_id == instagram_account_id,
                Follower.username == follower["username"]
            ).first()

            if not existing:
                new_follower = Follower(
                    instagram_account_id=instagram_account_id,
                    username=follower["username"],
                    user_id=follower.get("user_id"),
                    full_name=follower.get("full_name"),
                    fetched_at=datetime.utcnow()
                )
                db.add(new_follower)

        db.commit()
        return {
            "status": "success",
            "account_id": instagram_account_id,
            "followers_count": len(followers)
        }

    except Exception as e:
        db.rollback()
        return {
            "status": "error",
            "account_id": instagram_account_id,
            "message": str(e)
        }
    finally:
        db.close()


@celery_app.task(name="fetch_all_followers")
def fetch_all_followers():
    db = SessionLocal()
    try:
        active_accounts = db.query(InstagramAccount).filter(
            InstagramAccount.is_active == True
        ).all()

        for account in active_accounts:
            fetch_followers_for_account.delay(account.id)

        return {
            "status": "success",
            "accounts_scheduled": len(active_accounts)
        }
    finally:
        db.close()

@celery_app.task(name="process_automation_rules")
def process_automation_rules():
    """Run automation engine for all active accounts"""
    db = SessionLocal()
    try:
        from app.services.automation_engine import AutomationEngine
        engine = AutomationEngine(db)
        
        active_accounts = db.query(InstagramAccount).filter(
            InstagramAccount.is_active == True
        ).all()
        
        results = []
        for account in active_accounts:
            result = engine.process_new_follower_trigger(account.id)
            results.append(result)
        
        return {"status": "success", "accounts_processed": len(results)}
    finally:
        db.close()
