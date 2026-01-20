"""
Pre-DM Handler Service
Handles sequential pre-DM actions (Ask to Follow, Ask for Email) before sending primary DM.
"""
import re
from typing import Dict, Any, Optional, Tuple
from datetime import datetime
from sqlalchemy.orm import Session
from app.models.automation_rule import AutomationRule
from app.models.captured_lead import CapturedLead
from app.models.instagram_account import InstagramAccount
from app.services.lead_capture import validate_email, update_automation_stats


# In-memory state tracker for pre-DM conversations
# Key: f"{sender_id}_{rule_id}", Value: {"step": "follow"|"email"|"primary", "followed": bool, "email_sent": bool}
_pre_dm_states: Dict[str, Dict[str, Any]] = {}


def get_pre_dm_state(sender_id: str, rule_id: int) -> Dict[str, Any]:
    """Get the current pre-DM state for a sender-rule combination."""
    key = f"{sender_id}_{rule_id}"
    return _pre_dm_states.get(key, {
        "step": "initial",
        "follow_request_sent": False,
        "email_request_sent": False,
        "email_received": False,
        "primary_dm_sent": False
    })


def update_pre_dm_state(sender_id: str, rule_id: int, updates: Dict[str, Any]):
    """Update the pre-DM state for a sender-rule combination."""
    key = f"{sender_id}_{rule_id}"
    if key not in _pre_dm_states:
        _pre_dm_states[key] = {
            "step": "initial",
            "follow_request_sent": False,
            "email_request_sent": False,
            "email_received": False,
            "primary_dm_sent": False
        }
    _pre_dm_states[key].update(updates)
    
    # Clean up old states (keep only last 1000)
    if len(_pre_dm_states) > 1000:
        # Remove oldest entries (simple FIFO)
        keys_to_remove = list(_pre_dm_states.keys())[:100]
        for k in keys_to_remove:
            del _pre_dm_states[k]


def clear_pre_dm_state(sender_id: str, rule_id: int):
    """Clear the pre-DM state for a sender-rule combination."""
    key = f"{sender_id}_{rule_id}"
    if key in _pre_dm_states:
        del _pre_dm_states[key]


def check_if_follow_confirmation(message_text: str) -> bool:
    """
    Check if a message text indicates the user is already following.
    Returns: bool
    """
    if not message_text:
        return False
    
    message_lower = message_text.strip().lower()
    
    # Common follow confirmation phrases
    follow_confirmations = [
        "already following",
        "already follow",
        "i'm following",
        "im following",
        "i am following",
        "already followed",
        "following you",
        "follow you",
        "i follow you",
        "already following you",
        "yes following",
        "yes i'm following",
        "yes im following",
        "yes i am following",
    ]
    
    for phrase in follow_confirmations:
        if phrase in message_lower:
            return True
    
    # Check if message is exactly "follow" (case-insensitive)
    if message_lower == "follow":
        return True
    
    return False


def check_if_email_response(message_text: str) -> Tuple[bool, Optional[str]]:
    """
    Check if a message text looks like an email address.
    Returns: (is_email: bool, email_address: str | None)
    """
    if not message_text:
        return False, None
    
    # Try to extract email from message
    message_text = message_text.strip()
    
    # Basic email pattern
    email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
    matches = re.findall(email_pattern, message_text)
    
    if matches:
        # Validate the first email found
        email = matches[0]
        is_valid, _ = validate_email(email)
        if is_valid:
            return True, email
    
    return False, None


async def process_pre_dm_actions(
    rule: AutomationRule,
    sender_id: str,
    account: InstagramAccount,
    db: Session,
    incoming_message: str = None,
    trigger_type: str = None
) -> Dict[str, Any]:
    """
    Process pre-DM actions (Ask to Follow, Ask for Email) before sending primary DM.
    
    Returns:
        {
            "action": "send_follow_request" | "send_email_request" | "send_primary" | "wait_for_email",
            "message": str,
            "should_save_email": bool,
            "email": str | None
        }
    """
    config = rule.config
    state = get_pre_dm_state(sender_id, rule.id)
    
    ask_to_follow = config.get("ask_to_follow", False)
    ask_for_email = config.get("ask_for_email", False)
    ask_to_follow_message = config.get("ask_to_follow_message", "Hey! Would you mind following me? I share great content! 🙌")
    ask_for_email_message = config.get("ask_for_email_message", "Quick question - what's your email? I'd love to send you something special! 📧")
    
    # Check if follow button was clicked (postback event)
    if trigger_type == "postback" and state.get("follow_button_clicked"):
        # User clicked follow button - proceed to email request
        if ask_for_email and not state.get("email_request_sent"):
            update_pre_dm_state(sender_id, rule.id, {
                "email_request_sent": True,
                "step": "email"
            })
            return {
                "action": "send_email_request",
                "message": ask_for_email_message,
                "should_save_email": False,
                "email": None
            }
        else:
            # No email request, proceed to primary DM
            return {
                "action": "send_primary",
                "message": None,
                "should_save_email": False,
                "email": None
            }
    
    # Check if this is a response to a follow request (text-based confirmation)
    if incoming_message and state.get("follow_request_sent") and not state.get("follow_confirmed"):
        if check_if_follow_confirmation(incoming_message):
            # User confirmed they're following - mark as confirmed and proceed to email request
            update_pre_dm_state(sender_id, rule.id, {
                "follow_confirmed": True
            })
            
            # If email request is enabled, proceed to email request
            if ask_for_email and not state.get("email_request_sent"):
                update_pre_dm_state(sender_id, rule.id, {
                    "email_request_sent": True,
                    "step": "email"
                })
                return {
                    "action": "send_email_request",
                    "message": ask_for_email_message,
                    "should_save_email": False,
                    "email": None
                }
            else:
                # No email request, proceed to primary DM
                return {
                    "action": "send_primary",
                    "message": None,
                    "should_save_email": False,
                    "email": None
                }
    
    # Check if this is a response to an email request
    if incoming_message and state.get("email_request_sent") and not state.get("email_received"):
        is_email, email_address = check_if_email_response(incoming_message)
        if is_email:
            # Email received! Save it and proceed to primary DM
            update_pre_dm_state(sender_id, rule.id, {
                "email_received": True,
                "email": email_address
            })
            
            # Save email to leads database
            try:
                captured_lead = CapturedLead(
                    user_id=account.user_id,
                    instagram_account_id=account.id,
                    automation_rule_id=rule.id,
                    email=email_address,
                    extra_metadata={
                        "sender_id": sender_id,
                        "captured_via": "pre_dm_email_request",
                        "timestamp": datetime.utcnow().isoformat()
                    }
                )
                db.add(captured_lead)
                db.commit()
                db.refresh(captured_lead)
                
                # Update stats
                update_automation_stats(rule.id, "lead_captured", db)
                print(f"✅ Pre-DM email captured: {email_address}")
            except Exception as e:
                print(f"⚠️ Error saving pre-DM email: {str(e)}")
                db.rollback()
            
            # Proceed to primary DM
            return {
                "action": "send_primary",
                "message": None,
                "should_save_email": False,
                "email": email_address
            }
        else:
            # Not a valid email, ask again
            return {
                "action": "send_email_request",
                "message": f"Please provide a valid email address.\n\n{ask_for_email_message}",
                "should_save_email": False,
                "email": None
            }
    
    # Initial trigger - start pre-DM sequence
    # Also handle timeout trigger (5 seconds after follow button sent)
    # Handle email_timeout trigger (5 seconds after email request sent)
    if trigger_type in ["post_comment", "keyword", "new_message", "timeout", "email_timeout"] and not state.get("primary_dm_sent"):
        # Step 1: Send Follow Request (if enabled and not sent yet)
        if ask_to_follow and not state.get("follow_request_sent"):
            update_pre_dm_state(sender_id, rule.id, {
                "follow_request_sent": True,
                "step": "follow"
            })
            return {
                "action": "send_follow_request",
                "message": ask_to_follow_message,
                "should_save_email": False,
                "email": None
            }
        
        # Step 2: Send Email Request (if enabled and follow request was sent or not needed)
        if ask_for_email and not state.get("email_request_sent"):
            # If follow is enabled, we send email request after follow (with small delay)
            # For now, we'll send it immediately if follow was sent
            if not ask_to_follow or state.get("follow_request_sent"):
                update_pre_dm_state(sender_id, rule.id, {
                    "email_request_sent": True,
                    "step": "email"
                })
                return {
                    "action": "send_email_request",
                    "message": ask_for_email_message,
                    "should_save_email": False,
                    "email": None
                }
        
        # Step 3: Send Primary DM (if pre-DM actions are done or disabled)
        # If email is enabled, wait for email response
        if ask_for_email and state.get("email_request_sent") and not state.get("email_received"):
            # Still waiting for email
            return {
                "action": "wait_for_email",
                "message": None,
                "should_save_email": False,
                "email": None
            }
        
        # All pre-DM actions done (or disabled), send primary DM
        update_pre_dm_state(sender_id, rule.id, {
            "primary_dm_sent": True,
            "step": "primary"
        })
        return {
            "action": "send_primary",
            "message": None,
            "should_save_email": False,
            "email": None
        }
    
    # Default: Send primary DM if no pre-DM actions
    if not ask_to_follow and not ask_for_email:
        return {
            "action": "send_primary",
            "message": None,
            "should_save_email": False,
            "email": None
        }
    
    # Fallback
    return {
        "action": "send_primary",
        "message": None,
        "should_save_email": False,
        "email": None
    }


def reset_pre_dm_state_for_rule(rule_id: int):
    """Reset all pre-DM states for a specific rule (useful when rule is updated)."""
    keys_to_remove = [k for k in _pre_dm_states.keys() if k.endswith(f"_{rule_id}")]
    for key in keys_to_remove:
        del _pre_dm_states[key]
