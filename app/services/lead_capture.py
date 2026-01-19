"""
Lead Capture Service
Handles multi-step lead capture flows for automation rules.
"""
import re
from typing import Dict, Any, Optional
from datetime import datetime
from sqlalchemy.orm import Session
from app.models.automation_rule import AutomationRule
from app.models.captured_lead import CapturedLead
from app.models.automation_rule_stats import AutomationRuleStats
from app.models.instagram_account import InstagramAccount


def validate_email(email: str) -> bool:
    """Validate email format"""
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, email))


def validate_phone(phone: str) -> bool:
    """Validate phone format (basic validation)"""
    # Remove common characters
    cleaned = re.sub(r'[\s\-\(\)\+]', '', phone)
    # Check if it's all digits and has reasonable length
    return cleaned.isdigit() and 10 <= len(cleaned) <= 15


def get_current_flow_step(rule: AutomationRule, user_id: str) -> Optional[Dict[str, Any]]:
    """
    Get the current step in the lead capture flow for a user.
    Returns the step that should be executed next, or None if flow is complete.
    """
    if not rule.config.get("is_lead_capture") or not rule.config.get("lead_capture_flow"):
        return None
    
    flow = rule.config.get("lead_capture_flow", [])
    if not flow:
        return None
    
    # For MVP: Always start from step 1
    # In production, you'd track user's progress in a separate table
    return flow[0] if flow else None


def process_lead_capture_step(
    rule: AutomationRule,
    user_message: str,
    sender_id: str,
    db: Session
) -> Dict[str, Any]:
    """
    Process a step in the lead capture flow.
    Returns: {
        "action": "ask" | "save" | "send",
        "message": str,
        "saved_lead": CapturedLead | None
    }
    """
    if not rule.config.get("is_lead_capture"):
        return {"action": "skip", "message": None, "saved_lead": None}
    
    flow = rule.config.get("lead_capture_flow", [])
    if not flow:
        return {"action": "skip", "message": None, "saved_lead": None}
    
    # Get current step (for MVP, we'll process step by step)
    # Step 1: Ask for information
    ask_step = next((s for s in flow if s.get("type") == "ask"), None)
    if ask_step:
        # Check if user has already provided the information
        # For MVP, we'll check if the message matches the expected format
        field_type = ask_step.get("field_type", "text")
        validation = ask_step.get("validation", "none")
        
        # Validate user input
        is_valid = False
        if field_type == "email" or validation == "email":
            is_valid = validate_email(user_message.strip())
        elif field_type == "phone" or validation == "phone":
            is_valid = validate_phone(user_message.strip())
        else:
            # For text/custom fields, accept any non-empty input
            is_valid = len(user_message.strip()) > 0
        
        if not is_valid:
            # Return error message asking for valid input
            return {
                "action": "ask",
                "message": ask_step.get("text", "Please provide a valid response."),
                "saved_lead": None
            }
        
        # Input is valid, proceed to save step
        save_step = next((s for s in flow if s.get("type") == "save"), None)
        if save_step:
            # Save the lead
            field = save_step.get("field", "email")
            lead_data = {}
            
            if field == "email":
                lead_data["email"] = user_message.strip()
            elif field == "phone":
                lead_data["phone"] = user_message.strip()
            else:
                lead_data["custom_fields"] = {field: user_message.strip()}
            
            # Get account to find user_id
            account = db.query(InstagramAccount).filter(
                InstagramAccount.id == rule.instagram_account_id
            ).first()
            
            if account:
                # Create captured lead
                captured_lead = CapturedLead(
                    user_id=account.user_id,
                    instagram_account_id=rule.instagram_account_id,
                    automation_rule_id=rule.id,
                    email=lead_data.get("email"),
                    phone=lead_data.get("phone"),
                    custom_fields=lead_data.get("custom_fields"),
                    extra_metadata={
                        "sender_id": sender_id,
                        "captured_via": "dm_automation",
                        "timestamp": datetime.utcnow().isoformat()
                    }
                )
                db.add(captured_lead)
                db.commit()
                db.refresh(captured_lead)
                
                # Update stats
                update_automation_stats(rule.id, "lead_captured", db)
                
                # Get send step
                send_step = next((s for s in flow if s.get("type") == "send"), None)
                if send_step:
                    # Get message (with variations support)
                    message_variations = send_step.get("message_variations", [])
                    if message_variations and len(message_variations) > 0:
                        import random
                        message = random.choice([m for m in message_variations if m and str(m).strip()])
                    else:
                        message = send_step.get("message", "Thank you! We'll be in touch soon.")
                    
                    return {
                        "action": "send",
                        "message": message,
                        "saved_lead": captured_lead
                    }
                
                return {
                    "action": "send",
                    "message": "Thank you! We've received your information.",
                    "saved_lead": captured_lead
                }
    
    # Default: no action needed
    return {"action": "skip", "message": None, "saved_lead": None}


def update_automation_stats(rule_id: int, event_type: str, db: Session):
    """
    Update automation rule statistics.
    event_type: "triggered" | "dm_sent" | "comment_replied" | "lead_captured"
    """
    try:
        # Try to get existing stats
        stats = db.query(AutomationRuleStats).filter(
            AutomationRuleStats.automation_rule_id == rule_id
        ).first()
        
        if not stats:
            # Create new stats record
            stats = AutomationRuleStats(
                automation_rule_id=rule_id,
                total_triggers=0,
                total_dms_sent=0,
                total_comments_replied=0,
                total_leads_captured=0
            )
            db.add(stats)
        
        # Update counters
        if event_type == "triggered":
            stats.total_triggers += 1
            stats.last_triggered_at = datetime.utcnow()
        elif event_type == "dm_sent":
            stats.total_dms_sent += 1
        elif event_type == "comment_replied":
            stats.total_comments_replied += 1
        elif event_type == "lead_captured":
            stats.total_leads_captured += 1
            stats.last_lead_captured_at = datetime.utcnow()
        
        stats.updated_at = datetime.utcnow()
        db.commit()
    except Exception as e:
        print(f"⚠️ Error updating automation stats: {str(e)}")
        db.rollback()
        # Fallback: Update stats in config JSON
        try:
            rule = db.query(AutomationRule).filter(AutomationRule.id == rule_id).first()
            if rule:
                if "stats" not in rule.config:
                    rule.config["stats"] = {
                        "total_triggers": 0,
                        "total_dms_sent": 0,
                        "total_comments_replied": 0,
                        "total_leads_captured": 0,
                        "last_triggered_at": None,
                        "last_lead_captured_at": None
                    }
                
                stats_dict = rule.config["stats"]
                if event_type == "triggered":
                    stats_dict["total_triggers"] = stats_dict.get("total_triggers", 0) + 1
                    stats_dict["last_triggered_at"] = datetime.utcnow().isoformat()
                elif event_type == "dm_sent":
                    stats_dict["total_dms_sent"] = stats_dict.get("total_dms_sent", 0) + 1
                elif event_type == "comment_replied":
                    stats_dict["total_comments_replied"] = stats_dict.get("total_comments_replied", 0) + 1
                elif event_type == "lead_captured":
                    stats_dict["total_leads_captured"] = stats_dict.get("total_leads_captured", 0) + 1
                    stats_dict["last_lead_captured_at"] = datetime.utcnow().isoformat()
                
                rule.config = rule.config  # Trigger SQLAlchemy to detect change
                db.commit()
        except Exception as e2:
            print(f"⚠️ Error updating stats in config: {str(e2)}")
            db.rollback()
