"""
Lead Capture Service
Handles multi-step lead capture flows for automation rules.
"""
import os
import re
from typing import Dict, Any, Optional, Tuple

from email_validator import EmailNotValidError, validate_email as validate_email_rfc
from datetime import datetime
from sqlalchemy.orm import Session
from app.models.automation_rule import AutomationRule
from app.models.captured_lead import CapturedLead
from app.models.automation_rule_stats import AutomationRuleStats
from app.models.instagram_account import InstagramAccount


def _email_check_deliverability() -> bool:
    """DNS MX/A check via email-validator (no third-party HTTP APIs). Disabled in tests via env."""
    return os.getenv("EMAIL_CHECK_DELIVERABILITY", "true").lower() in ("1", "true", "yes")


def _email_dns_timeout_sec() -> int:
    return max(3, min(30, int(os.getenv("EMAIL_DNS_TIMEOUT_SEC", "8"))))


_VOWELS = frozenset("aeiouyAEIOUY")


def _max_consonant_run(local_ascii: str) -> int:
    max_run = 0
    run = 0
    for ch in local_ascii:
        if ch.isalpha():
            if ch in _VOWELS:
                run = 0
            else:
                run += 1
                max_run = max(max_run, run)
        else:
            run = 0
    return max_run


def _vowel_ratio_letters_only(local: str) -> float:
    letters = [c for c in local if c.isalpha()]
    if not letters:
        return 1.0
    vowels = sum(1 for c in letters if c in _VOWELS)
    return vowels / len(letters)


def _local_part_looks_like_keyboard_mash(local: str) -> bool:
    """
    Heuristic: random-keyboard locals often have long consonant runs and few vowels (e.g. Hjdhjej).
    Require both so real words like "rhythm" (y as vowel) are not blocked. No external services.
    """
    if len(local) < 6:
        return False
    if not re.match(r"^[a-zA-Z]+$", local):
        return False
    if _vowel_ratio_letters_only(local) >= 0.22:
        return False
    return _max_consonant_run(local) >= 5


def validate_email(email: str) -> Tuple[bool, str, Optional[str]]:
    """
    Validate email for lead capture: RFC syntax + optional DNS deliverability + light heuristics.
    No third-party verification APIs.

    Returns: (is_valid, error_message, normalized_email_or_none)
    """
    if not email or not email.strip():
        return False, "Email cannot be empty.", None

    raw = email.strip()
    check_mx = _email_check_deliverability()
    timeout = _email_dns_timeout_sec() if check_mx else None

    try:
        info = validate_email_rfc(
            raw,
            check_deliverability=check_mx,
            timeout=timeout,
        )
        normalized = info.normalized
    except EmailNotValidError:
        return (
            False,
            "Please enter a valid email address (check spelling and domain).",
            None,
        )

    if "@" not in normalized:
        return False, "Please enter a valid email address.", None

    local_part, _, domain = normalized.partition("@")
    domain_lower = domain.lower()
    normalized_lower = normalized.lower()

    if len(local_part) < 4:
        return (
            False,
            "Email address is too short before @. Please enter your full address.",
            None,
        )

    if not re.search(r"[a-zA-Z]", local_part):
        return False, "Email address must contain at least one letter.", None

    # Reject common fake/test patterns (case-insensitive)
    fake_patterns = [
        r"^test@test\.",
        r"^abc@abc\.",
        r"^123@123\.",
        r"^fake@fake\.",
        r"^example@example\.",
        r"^demo@demo\.",
        r"^sample@sample\.",
        r"^temp@temp\.",
        r"^user@user\.",
        r"^email@email\.",
    ]
    for pattern in fake_patterns:
        if re.match(pattern, normalized_lower, re.IGNORECASE):
            return False, "Please enter a real email address.", None

    common_fake_locals = frozenset(
        ("abc", "xyz", "test", "demo", "fake", "temp", "user", "mail", "sample", "example")
    )
    if local_part.lower() in common_fake_locals:
        return False, "Please enter a real email address.", None

    if len(local_part) == 4 and re.match(r"^[a-zA-Z]{4}$", local_part):
        common_4letter = frozenset(
            ("abcd", "test", "demo", "fake", "temp", "user", "mail", "name", "info", "data")
        )
        if local_part.lower() in common_4letter:
            return False, "Please enter a real email address.", None

    fake_domains = frozenset(
        ("test.com", "example.com", "fake.com", "demo.com", "sample.com", "temp.com", "abc.com", "xyz.com")
    )
    if domain_lower in fake_domains:
        return False, "Please enter a real email address.", None

    if _local_part_looks_like_keyboard_mash(local_part):
        return (
            False,
            "That does not look like a real email. Please enter the address you actually use.",
            None,
        )

    return True, "", normalized_lower


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
        normalized_email: Optional[str] = None

        if field_type == "email" or validation == "email":
            is_valid, error_message, normalized_email = validate_email(user_message.strip())
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
                lead_data["email"] = normalized_email or user_message.strip()
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
