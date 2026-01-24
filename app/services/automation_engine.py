import json
from typing import List, Dict, Any
from sqlalchemy.orm import Session
from app.models.automation_rule import AutomationRule
from app.models.follower import Follower
from app.models.instagram_account import InstagramAccount
from app.services.instagram_client import InstagramClient
from app.utils.encryption import decrypt_credentials
from app.utils.plan_enforcement import check_dm_limit, log_dm_sent


class AutomationEngine:
    def __init__(self, db: Session):
        self.db = db

    def detect_new_followers(self, instagram_account_id: int) -> List[Dict[str, Any]]:
        """
        Detect new followers by checking followers fetched in the last hour.
        Returns list of new follower data.
        """
        from datetime import datetime, timedelta

        one_hour_ago = datetime.utcnow() - timedelta(hours=1)

        new_followers = self.db.query(Follower).filter(
            Follower.instagram_account_id == instagram_account_id,
            Follower.fetched_at >= one_hour_ago
        ).all()

        return [
            {
                "user_id": follower.user_id,
                "username": follower.username,
                "full_name": follower.full_name
            }
            for follower in new_followers
        ]

    def get_active_rules(self, instagram_account_id: int, trigger_type: str) -> List[AutomationRule]:
        """
        Get all active automation rules for an account and trigger type.
        """
        return self.db.query(AutomationRule).filter(
            AutomationRule.instagram_account_id == instagram_account_id,
            AutomationRule.trigger_type == trigger_type,
            AutomationRule.is_active == True
        ).all()

    def send_dm_action(
        self,
        instagram_account: InstagramAccount,
        rule: AutomationRule,
        trigger_data: Dict[str, Any]
    ) -> bool:
        """
        Execute send_dm action using rule configuration.
        """
        try:
            # Check DM limit (per Instagram account to track usage across reconnections)
            try:
                check_dm_limit(instagram_account.user_id, self.db, instagram_account_id=instagram_account.id)
            except Exception as e:
                print(f"DM limit reached: {str(e)}")
                return False

            # Decrypt credentials
            credentials_json = decrypt_credentials(instagram_account.encrypted_credentials)
            credentials = json.loads(credentials_json)

            # Authenticate Instagram client
            client = InstagramClient()
            client.authenticate(credentials["username"], credentials["password"])

            # Get message template from config
            message_template = rule.config.get("message", "")

            # Replace placeholders with trigger data
            message = message_template.format(
                username=trigger_data.get("username", ""),
                full_name=trigger_data.get("full_name", "")
            )

            # Get user ID to send DM
            user_id = trigger_data.get("user_id")
            if not user_id:
                return False

            # Send DM
            client.send_dm([user_id], message)

            # Log DM sent
            log_dm_sent(
                user_id=instagram_account.user_id,
                instagram_account_id=instagram_account.id,
                recipient_username=trigger_data.get("username", ""),
                message=message,
                db=self.db,
                instagram_username=instagram_account.username,
                instagram_igsid=getattr(instagram_account, "igsid", None),
            )

            return True

        except Exception as e:
            print(f"Error sending DM: {str(e)}")
            return False

    def execute_rule(
        self,
        instagram_account_id: int,
        rule: AutomationRule,
        trigger_data: Dict[str, Any]
    ) -> bool:
        """
        Execute an automation rule based on its action type.
        """
        instagram_account = self.db.query(InstagramAccount).filter(
            InstagramAccount.id == instagram_account_id
        ).first()

        if not instagram_account:
            return False

        if rule.action_type == "send_dm":
            return self.send_dm_action(instagram_account, rule, trigger_data)

        return False

    def process_new_follower_trigger(self, instagram_account_id: int) -> Dict[str, Any]:
        """
        Process new_follower trigger for an Instagram account.
        Detects new followers and executes matching automation rules.
        """
        # Detect new followers
        new_followers = self.detect_new_followers(instagram_account_id)

        if not new_followers:
            return {
                "status": "success",
                "new_followers_count": 0,
                "actions_executed": 0
            }

        # Get active rules for new_follower trigger
        rules = self.get_active_rules(instagram_account_id, "new_follower")

        actions_executed = 0

        # Execute each rule for each new follower
        for follower in new_followers:
            for rule in rules:
                success = self.execute_rule(instagram_account_id, rule, follower)
                if success:
                    actions_executed += 1

        return {
            "status": "success",
            "new_followers_count": len(new_followers),
            "actions_executed": actions_executed
        }
