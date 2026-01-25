"""
Lead Capture Service
Handles multi-step lead capture flows for automation rules.
"""
import re
from typing import Dict, Any, Optional, Tuple
from datetime import datetime
from sqlalchemy.orm import Session
from app.models.automation_rule import AutomationRule
from app.models.captured_lead import CapturedLead
from app.models.automation_rule_stats import AutomationRuleStats
from app.models.instagram_account import InstagramAccount


def validate_email(email: str) -> Tuple[bool, str]:
    """
    Validate email format with strict rules.
    Returns: (is_valid: bool, error_message: str)
    """
    if not email or not email.strip():
        return False, "Email cannot be empty."
    
    email = email.strip().lower()
    
    # Basic format check
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    if not re.match(pattern, email):
        return False, "Please enter a valid email address (e.g., example@domain.com)"
    
    # Check for common mistakes
    if email.startswith('.') or email.startswith('@'):
        return False, "Email cannot start with a dot or @ symbol."
    
    if email.count('@') != 1:
        return False, "Email must contain exactly one @ symbol."
    
    if '..' in email:
        return False, "Email cannot contain consecutive dots."
    
    # Check domain part
    local_part, domain = email.split('@')
    
    # STRICT: Local part must be at least 4 characters (reject very short emails like "Abc", "Xyz")
    if len(local_part) < 4:
        return False, "Email address is too short. Please enter a valid email address (at least 4 characters before @)."
    
    # STRICT: Local part must contain at least one letter (reject pure numbers like "1234@...")
    if not re.search(r'[a-zA-Z]', local_part):
        return False, "Email address must contain at least one letter. Please enter a valid email address."
    
    if len(domain) < 4:  # x.co (minimum)
        return False, "Email domain is too short."
    
    if not '.' in domain:
        return False, "Email domain must contain a dot (e.g., gmail.com)."
    
    # Check TLD (top-level domain)
    tld = domain.split('.')[-1]
    if len(tld) < 2:
        return False, "Email must have a valid domain extension (e.g., .com, .org)."
    
    # STRICT: Reject common fake/test patterns (case-insensitive)
    fake_patterns = [
        r'^test@test\.',
        r'^abc@abc\.',
        r'^123@123\.',
        r'^fake@fake\.',
        r'^example@example\.',
        r'^demo@demo\.',
        r'^sample@sample\.',
        r'^temp@temp\.',
        r'^user@user\.',
        r'^email@email\.',
    ]
    for pattern in fake_patterns:
        if re.match(pattern, email, re.IGNORECASE):
            return False, "Please enter a real email address."
    
    # STRICT: Reject common fake patterns even if they're 4+ characters
    # Check for common fake patterns in local part
    common_fake_patterns = ['abc', 'xyz', 'test', 'demo', 'fake', 'temp', 'user', 'mail', 'sample', 'example']
    if local_part.lower() in common_fake_patterns:
        return False, "Please enter a real email address."
    
    # STRICT: Reject local parts that are too simple (like "abcd", "test123", etc.)
    # If local part is exactly 4 characters and all letters, check if it's a common pattern
    if len(local_part) == 4 and re.match(r'^[a-zA-Z]{4}$', local_part):
        # Reject common 4-letter fake patterns
        common_4letter = ['abcd', 'test', 'demo', 'fake', 'temp', 'user', 'mail', 'name', 'info', 'data']
        if local_part.lower() in common_4letter:
            return False, "Please enter a real email address."
    
    # STRICT: Reject domains that look fake
    fake_domains = ['test.com', 'example.com', 'fake.com', 'demo.com', 'sample.com', 'temp.com', 'abc.com', 'xyz.com']
    if domain.lower() in fake_domains:
        return False, "Please enter a real email address."
    
    return True, ""


def validate_phone(phone: str) -> Tuple[bool, str]:
    """
    Validate phone number format (international support).
    Returns: (is_valid: bool, error_message: str)
    """
    if not phone or not phone.strip():
        return False, "Phone number cannot be empty."
    
    # Remove common formatting characters
    cleaned = re.sub(r'[\s\-\(\)\+\.]', '', phone.strip())
    
    # Must contain only digits (after cleaning)
    if not cleaned.isdigit():
        return False, "Phone number can only contain digits, spaces, dashes, and parentheses."
    
    # Length validation (international: 10-15 digits)
    if len(cleaned) < 10:
        return False, "Phone number is too short. Please include area code (minimum 10 digits)."
    
    if len(cleaned) > 15:
        return False, "Phone number is too long (maximum 15 digits)."
    
    # Reject obviously fake numbers
    # All same digits (e.g., 1111111111, 0000000000)
    if len(set(cleaned)) <= 2:
        return False, "This doesn't look like a valid phone number. Please check and try again."
    
    # Sequential patterns (e.g., 1234567890, 9876543210)
    if cleaned in ['1234567890', '9876543210', '0123456789']:
        return False, "Please enter a real phone number."
    
    # Check for common invalid patterns
    invalid_patterns = [
        '123456789', '987654321', '000000000', '111111111',
        '222222222', '333333333', '444444444', '555555555',
        '666666666', '777777777', '888888888', '999999999'
    ]
    if cleaned in invalid_patterns:
        return False, "Please enter a real phone number."
    
    return True, ""


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
        error_message = ""
        
        if field_type == "email" or validation == "email":
            is_valid, error_message = validate_email(user_message.strip())
        elif field_type == "phone" or validation == "phone":
            is_valid, error_message = validate_phone(user_message.strip())
        else:
            # For text/custom fields, accept any non-empty input
            if len(user_message.strip()) > 0:
                is_valid = True
            else:
                is_valid = False
                error_message = "Please provide a response."
        
        if not is_valid:
            # Return helpful error message asking for valid input
            # Combine the original question with validation error
            original_question = ask_step.get("text", "Please provide your information.")
            error_response = f"{error_message}\n\n{original_question}"
            
            return {
                "action": "ask",
                "message": error_response,
                "saved_lead": None,
                "validation_failed": True
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
                
                # FIX: Log EMAIL_COLLECTED analytics event so it shows up in dashboard count
                try:
                    from app.utils.analytics import log_analytics_event_sync
                    from app.models.analytics_event import EventType
                    media_id = rule.config.get("media_id") if isinstance(rule.config, dict) else None
                    log_analytics_event_sync(
                        db=db,
                        user_id=account.user_id,
                        event_type=EventType.EMAIL_COLLECTED,
                        rule_id=rule.id,
                        media_id=media_id,
                        instagram_account_id=rule.instagram_account_id,
                        metadata={
                            "sender_id": sender_id,
                            "email": lead_data.get("email") or lead_data.get("phone") or "custom_field",
                            "captured_via": "lead_capture_flow",
                            "field_type": field_type
                        }
                    )
                    print(f"✅ EMAIL_COLLECTED analytics event logged for lead capture flow")
                except Exception as analytics_err:
                    print(f"⚠️ Failed to log EMAIL_COLLECTED event: {str(analytics_err)}")
                
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
    event_type: "triggered" | "dm_sent" | "comment_replied" | "lead_captured" | "follow_button_clicked" | "profile_visit" | "im_following_clicked" | "follower_gained"
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
        elif event_type == "follow_button_clicked":
            stats.total_follow_button_clicks += 1
            stats.last_follow_button_clicked_at = datetime.utcnow()
        elif event_type == "profile_visit":
            stats.total_profile_visits += 1
            stats.last_profile_visit_at = datetime.utcnow()
        elif event_type == "im_following_clicked":
            stats.total_im_following_clicks += 1
            stats.last_im_following_clicked_at = datetime.utcnow()
        elif event_type == "follower_gained":
            # FIX ISSUE 3: Track follower gain count
            if not hasattr(stats, 'total_followers_gained'):
                # Field doesn't exist yet, skip for now (will be added via migration)
                print(f"⚠️ total_followers_gained field not found in stats, skipping update")
            else:
                stats.total_followers_gained = (stats.total_followers_gained or 0) + 1
                print(f"✅ Follower gain count incremented for rule {rule_id}")
        
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
                        "total_follow_button_clicks": 0,
                        "total_followers_gained": 0,
                        "last_triggered_at": None,
                        "last_lead_captured_at": None,
                        "last_follow_button_clicked_at": None
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
                elif event_type == "follow_button_clicked":
                    stats_dict["total_follow_button_clicks"] = stats_dict.get("total_follow_button_clicks", 0) + 1
                    stats_dict["last_follow_button_clicked_at"] = datetime.utcnow().isoformat()
                elif event_type == "profile_visit":
                    stats_dict["total_profile_visits"] = stats_dict.get("total_profile_visits", 0) + 1
                    stats_dict["last_profile_visit_at"] = datetime.utcnow().isoformat()
                elif event_type == "im_following_clicked":
                    stats_dict["total_im_following_clicks"] = stats_dict.get("total_im_following_clicks", 0) + 1
                    stats_dict["last_im_following_clicked_at"] = datetime.utcnow().isoformat()
                elif event_type == "follower_gained":
                    # FIX ISSUE 3: Track follower gain count in config fallback
                    stats_dict["total_followers_gained"] = stats_dict.get("total_followers_gained", 0) + 1
                    print(f"✅ Follower gain count incremented in config for rule {rule_id}")
                
                rule.config = rule.config  # Trigger SQLAlchemy to detect change
                db.commit()
        except Exception as e2:
            print(f"⚠️ Error updating stats in config: {str(e2)}")
            db.rollback()
