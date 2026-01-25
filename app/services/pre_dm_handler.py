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
from app.models.follower import Follower
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
    
    # Common follow confirmation phrases (20+ variations for strict mode)
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
        "done",
        "followed",
        "i followed",
        "i've followed",
        "ive followed",
        "finished",
        "complete",
        "completed",
        "yes",
        "yep",
        "yup",
        "ok",
        "okay",
        "sure",
        "did it",
        "i did it",
        "just followed",
        "followed you",
        "following now",
        "i'm following now",
        "im following now",
        "following already",
        "got it",
        "👍",
        "✅",
        "✓",
        "check",
        "checked",
    ]
    
    for phrase in follow_confirmations:
        if phrase in message_lower:
            return True
    
    # Check if message is exactly these short confirmations (case-insensitive)
    exact_matches = ["follow", "done", "ok", "yes", "followed", "y", "k", "sure", "yep", "yup", "yeah", "got it", "finished", "complete"]
    if message_lower in exact_matches:
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
    trigger_type: str = None,
    skip_growth_steps: bool = False  # If True, skip follow/email steps and go directly to primary DM
) -> Dict[str, Any]:
    """
    Process pre-DM actions (Ask to Follow, Ask for Email) before sending primary DM.
    
    Args:
        skip_growth_steps: If True, skip follow/email steps and go directly to primary DM (VIP users)
    
    Returns:
        {
            "action": "send_follow_request" | "send_email_request" | "send_primary" | "wait_for_email",
            "message": str,
            "should_save_email": bool,
            "email": str | None
        }
    """
    # VIP USER CHECK: If skip_growth_steps is True, skip directly to primary DM
    # This MUST be checked FIRST before any other processing
    if skip_growth_steps:
        print(f"⭐ [VIP] Skipping ALL growth steps for rule {rule.id} - user is already converted (email + following)")
        print(f"⭐ [VIP] Returning send_primary action immediately - no follow/email requests will be sent")
        return {
            "action": "send_primary",
            "message": None,
            "should_save_email": False,
            "email": None,
            "send_email_success": False  # VIP users already provided email, no success message needed
        }
    
    config = rule.config
    state = get_pre_dm_state(sender_id, rule.id)
    
    ask_to_follow = config.get("ask_to_follow", False)
    ask_for_email = config.get("ask_for_email", False)
    ask_to_follow_message = config.get("ask_to_follow_message", "Hey! Would you mind following me? I share great content! 🙌")
    ask_for_email_message = config.get("ask_for_email_message", "Quick question - what's your email? I'd love to send you something special! 📧")
    
    # Note: lead capture flows are not used for pre‑DM email; follow/email
    # behaviour is driven entirely by these flags and messages.
    
    # ---------------------------------------------------------
    # Short‑circuit checks: already follower / already have email
    # ---------------------------------------------------------
    already_following = False
    already_has_email = False
    existing_email: Optional[str] = None
    
    try:
        # 1) Check followers table to see if this sender already follows the account
        #    We try matching by numeric user_id first (preferred), then fall back to username.
        try:
            sender_id_int = int(sender_id)
        except (TypeError, ValueError):
            sender_id_int = None
        
        follower_query = db.query(Follower).filter(
            Follower.instagram_account_id == account.id
        )
        if sender_id_int is not None:
            follower_query = follower_query.filter(Follower.user_id == sender_id_int)
        else:
            # Fallback: best‑effort username match using extra_metadata from previous leads
            follower_query = follower_query.filter(Follower.username.isnot(None))
        
        follower_obj = follower_query.first()
        if follower_obj:
            already_following = True
        
        # 2) Check captured_leads for an existing email tied to this sender/account
        leads = db.query(CapturedLead).filter(
            CapturedLead.instagram_account_id == account.id,
            CapturedLead.email.isnot(None)
        ).all()
        sender_id_str = str(sender_id) if sender_id is not None else None
        for lead in leads:
            meta = lead.extra_metadata or {}
            if sender_id_str and str(meta.get("sender_id")) == sender_id_str:
                already_has_email = True
                existing_email = lead.email
                break
    except Exception as e:
        # Never let analytics/lookup failures break the main DM flow
        print(f"⚠️ [STRICT MODE] Failed pre-check for existing follower/email: {str(e)}")
    
    # 3) Use these flags to potentially skip pre‑DM steps
    # IMPORTANT: Only short-circuit when THIS FLOW has been COMPLETED (both follow confirmed AND email received).
    # This isolates Post/Reel vs Story: completing lead capture on a Post must NOT
    # skip the full pre-DM sequence when the same user triggers a Story (different rule).
    # CRITICAL: Only skip to primary DM if THIS FLOW was completed, not just if user already follows/has email.
    # CRITICAL FIX: When ask_to_follow is False, follow is considered completed (no follow step needed)
    # But if ask_to_follow was False before and now True, we need to check if user already follows
    follow_completed_for_flow = not ask_to_follow or state.get("follow_confirmed", False)
    
    # SPECIAL CASE: If email was skipped/completed but follow was never asked (ask_to_follow was False before)
    # and now follow is enabled, check if user already follows (VIP user)
    # If VIP user already follows, mark follow as confirmed and proceed to primary DM
    # If not VIP, ask for follow
    if ask_to_follow and not state.get("follow_confirmed", False) and not state.get("follow_request_sent", False):
        # Follow is now enabled but was never asked before
        # Check if user already follows (VIP user)
        if already_following:
            # VIP user already follows - mark as confirmed and proceed
            print(f"⭐ [VIP] User already follows, marking follow as confirmed (follow was added after email was skipped)")
            update_pre_dm_state(sender_id, rule.id, {
                "follow_request_sent": True,
                "follow_confirmed": True
            })
            follow_completed_for_flow = True
    
    # Email is completed if: not asking for email, email received, OR email was skipped
    email_completed_for_flow = not ask_for_email or state.get("email_received", False) or state.get("email_skipped", False)
    flow_has_completed = follow_completed_for_flow and email_completed_for_flow
    
    if ask_to_follow or ask_for_email:
        # Case A: THIS FLOW has been completed (follow confirmed AND email received if required)
        # Only skip to primary when THIS FLOW was completed in a previous interaction
        if flow_has_completed:
            update_pre_dm_state(sender_id, rule.id, {
                "primary_dm_sent": True
            })
            return {
                "action": "send_primary",
                "message": None,
                "should_save_email": False,
                "email": state.get("email")
            }
        
        # Case B: User already follows AND already has email, but THIS FLOW hasn't been completed
        # Don't skip - still need to go through the flow to mark it as completed
        # This ensures the flow is tracked properly even if user already follows/has email
        if already_following and already_has_email and not flow_has_completed:
            # User already follows and has email, but flow not completed - still send follow request
            # This will mark the flow as completed when they confirm
            pass  # Continue to normal flow below
        
        # Case C: already following, but no email yet and ask_for_email is enabled
        # Only skip to email when THIS FLOW has already sent the follow request
        flow_has_sent_follow = state.get("follow_request_sent", False)
        if already_following and ask_for_email and not already_has_email and flow_has_sent_follow and state.get("follow_confirmed", False):
            # Skip follow step, go straight to email question (flow already started)
            update_pre_dm_state(sender_id, rule.id, {
                "follow_request_sent": True,
                "follow_confirmed": True
            })
            return {
                "action": "send_email_request",
                "message": ask_for_email_message,
                "should_save_email": False,
                "email": None
            }
        
        # Case D: not following, but we already have email → only ask to follow (no email step)
        if ask_to_follow and already_has_email and not ask_for_email:
            # Mark email as satisfied so we don't try to re‑ask later
            update_pre_dm_state(sender_id, rule.id, {
                "email_request_sent": True,
                "email_received": True,
                "email": existing_email
            })
            # Continue normal flow so they still get a follow request
    
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
            update_pre_dm_state(sender_id, rule.id, {
                "primary_dm_sent": True  # Mark as sent to prevent duplicate from scheduled task
            })
            return {
                "action": "send_primary",
                "message": None,
                "should_save_email": False,
                "email": None
            }
    
    # Check if this is a response to a follow request (text-based confirmation)
    if incoming_message and state.get("follow_request_sent") and not state.get("follow_confirmed"):
        if check_if_follow_confirmation(incoming_message):
            print(f"🔍 [DEBUG] Follow confirmation received: '{incoming_message}' from {sender_id} for rule {rule.id}")
            print(f"🔍 [DEBUG] ask_for_email={ask_for_email}, email_request_sent={state.get('email_request_sent')}")
            # User confirmed they're following - mark as confirmed and proceed to email request
            update_pre_dm_state(sender_id, rule.id, {
                "follow_confirmed": True
            })
            
            # Update global audience record with following status
            try:
                from app.services.global_conversion_check import update_audience_following
                update_audience_following(db, sender_id, account.id, account.user_id, is_following=True)
                print(f"✅ Follow status updated in global audience for {sender_id}")
            except Exception as audience_err:
                print(f"⚠️ Failed to update global audience with follow status: {str(audience_err)}")
            
            # If email request is enabled, proceed to email request
            if ask_for_email and not state.get("email_request_sent"):
                print(f"✅ [DEBUG] Sending email request to {sender_id} for rule {rule.id}")
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
                print(f"⚠️ [DEBUG] Skipping email request: ask_for_email={ask_for_email}, email_request_sent={state.get('email_request_sent')}")
                # No email request, proceed to primary DM
                update_pre_dm_state(sender_id, rule.id, {
                    "primary_dm_sent": True  # Mark as sent to prevent duplicate from scheduled task
                })
                return {
                    "action": "send_primary",
                    "message": None,
                    "should_save_email": False,
                    "email": None
                }
        else:
            # STRICT MODE: Random text received while waiting for follow confirmation - IGNORE
            print(f"⏳ [STRICT MODE] Waiting for follow confirmation from {sender_id}, ignoring message: '{incoming_message}'")
            return {
                "action": "ignore",
                "message": None,
                "should_save_email": False,
                "email": None
            }
    
    # Check if this is a response to an email request
    # IMPORTANT: Only process emails from DMs, NOT from comments
    # Comments should only trigger resending the email question as a reminder
    if incoming_message and state.get("email_request_sent") and not state.get("email_received"):
        # Skip email processing for comment triggers - they should only resend email question
        is_comment_trigger = trigger_type in ["post_comment", "keyword", "live_comment"]
        
        if is_comment_trigger:
            # Comment received while waiting for email - don't process as email, just return wait_for_email
            # The execute_automation_action will handle resending the email question
            print(f"💬 Comment received while waiting for email: '{incoming_message}' - will resend email question as reminder")
            return {
                "action": "wait_for_email",
                "message": None,
                "should_save_email": False,
                "email": None
            }
        
        # For DM triggers, check if it's an email
        is_email, email_address = check_if_email_response(incoming_message)
        if is_email:
            # STRICT MODE: Valid email received! Save it and proceed DIRECTLY to primary DM
            print(f"✅ [STRICT MODE] Valid email received: {email_address}")
            update_pre_dm_state(sender_id, rule.id, {
                "email_received": True,
                "email": email_address,
                "primary_dm_sent": True  # Mark as sent to prevent duplicates
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
                
                # Update global audience record with email
                try:
                    from app.services.global_conversion_check import update_audience_email
                    update_audience_email(db, sender_id, account.id, account.user_id, email_address)
                    print(f"✅ Email updated in global audience: {email_address}")
                except Exception as audience_err:
                    print(f"⚠️ Failed to update global audience with email: {str(audience_err)}")
                
                # Update stats
                update_automation_stats(rule.id, "lead_captured", db)
                print(f"✅ Email saved to database: {email_address}")
                
                # Log EMAIL_COLLECTED analytics event
                try:
                    from app.utils.analytics import log_analytics_event_sync
                    from app.models.analytics_event import EventType
                    media_id = rule.config.get("media_id") if hasattr(rule, 'config') else None
                    log_analytics_event_sync(
                        db=db,
                        user_id=account.user_id,
                        event_type=EventType.EMAIL_COLLECTED,
                        rule_id=rule.id,
                        media_id=media_id,
                        instagram_account_id=account.id,
                        metadata={
                            "sender_id": sender_id,
                            "email": email_address,  # Store email in metadata for analytics
                            "captured_via": "pre_dm_email_request"
                        }
                    )
                except Exception as analytics_err:
                    print(f"⚠️ Failed to log EMAIL_COLLECTED event: {str(analytics_err)}")
            except Exception as e:
                print(f"⚠️ Error saving email: {str(e)}")
                db.rollback()
            
            # STRICT MODE: Proceed DIRECTLY to primary DM (no intermediate success message)
            print(f"✅ [STRICT MODE] Proceeding to primary DM after valid email")
            return {
                "action": "send_primary",
                "message": None,
                "should_save_email": False,
                "email": email_address,
                # Hint for execute_automation_action: send email success message before primary DM
                "send_email_success": True
            }
        else:
            # Check if user typed a follow confirmation (like "done") while waiting for email
            # Send friendly reminder instead of generic retry message
            if check_if_follow_confirmation(incoming_message):
                print(f"💬 [FRIENDLY REMINDER] User typed follow confirmation '{incoming_message}' while waiting for email")
                friendly_reminder = config.get("email_friendly_reminder_message", 
                    "I see you confirmed following! 👋\n\nNow I just need your email address so I can send you the guide! 📧")
                return {
                    "action": "send_email_retry",
                    "message": friendly_reminder,
                    "should_save_email": False,
                    "email": None
                }
            
            # STRICT MODE: Invalid email - send retry message and WAIT
            print(f"⚠️ [STRICT MODE] Invalid email format: {incoming_message}")
            email_retry_message = config.get("email_retry_message", "")
            # Ensure we have a valid retry message - use default if empty or not configured
            if not email_retry_message or not email_retry_message.strip():
                email_retry_message = "Hmm, that doesn't look like a valid email address. 🤔\n\nPlease type it again so I can send you the guide! 📧"
            return {
                "action": "send_email_retry",
                "message": email_retry_message,
                "should_save_email": False,
                "email": None
            }
    
    # Initial trigger - start pre-DM sequence
    # Also handle timeout trigger (5 seconds after follow button sent)
    # Handle email_timeout trigger (5 seconds after email request sent)
    # story_reply = user replying to story via DM (each flow separate from post_comment)
    if trigger_type in ["post_comment", "keyword", "new_message", "timeout", "email_timeout", "story_reply"] and not state.get("primary_dm_sent"):
        # Check if flow is COMPLETED (both follow confirmed AND email received if required)
        # Only skip to primary DM if flow was completed in a previous interaction
        follow_completed = not ask_to_follow or state.get("follow_confirmed", False)
        # Email is completed if: not asking for email, email received, OR email was skipped
        email_completed = not ask_for_email or state.get("email_received", False) or state.get("email_skipped", False)
        flow_completed = follow_completed and email_completed
        
        print(f"🔍 [PRE-DM DEBUG] trigger_type={trigger_type}, ask_to_follow={ask_to_follow}, ask_for_email={ask_for_email}")
        print(f"🔍 [PRE-DM DEBUG] state: follow_request_sent={state.get('follow_request_sent')}, follow_confirmed={state.get('follow_confirmed')}, email_request_sent={state.get('email_request_sent')}, email_received={state.get('email_received')}")
        print(f"🔍 [PRE-DM DEBUG] flow_completed={flow_completed} (follow_completed={follow_completed}, email_completed={email_completed})")
        
        # If flow is completed, skip directly to primary DM
        if flow_completed:
            print(f"✅ [PRE-DM] Flow completed - skipping to primary DM")
            update_pre_dm_state(sender_id, rule.id, {
                "primary_dm_sent": True,
                "step": "primary"
            })
            return {
                "action": "send_primary",
                "message": None,
                "should_save_email": False,
                "email": state.get("email")
            }
        
        # Step 1: Send Follow Request (if enabled and not sent yet OR not confirmed yet)
        # IMPORTANT: Always send follow request if not confirmed, even if it was sent before
        if ask_to_follow and not state.get("follow_confirmed"):
            print(f"🔍 [PRE-DM] Checking follow request: follow_request_sent={state.get('follow_request_sent')}")
            # If follow request was already sent but not confirmed, user commented again
            # We should still wait for confirmation, but if this is a new comment, resend follow request
            if not state.get("follow_request_sent"):
                # First time - send follow request
                print(f"✅ [PRE-DM] First time - sending follow request")
                return {
                    "action": "send_follow_request",
                    "message": ask_to_follow_message,
                    "should_save_email": False,
                    "email": None
                }
            else:
                # Follow request was sent but not confirmed - wait for user to confirm
                print(f"⏳ [PRE-DM] Follow request sent but not confirmed - returning wait_for_follow")
                return {
                    "action": "wait_for_follow",
                    "message": None,
                    "should_save_email": False,
                    "email": None
                }
        
        # Step 2: Send Email Request (if enabled and follow is completed but email not received)
        # CRITICAL FIX: When ask_to_follow is False, follow_completed is True, so we should send email request immediately
        # When ask_to_follow is True, we need to wait for follow_confirmed before sending email request
        # Note: follow_completed is already calculated above at line 485
        if ask_for_email and follow_completed and not state.get("email_received"):
            print(f"🔍 [PRE-DM] Checking email request: email_request_sent={state.get('email_request_sent')}, follow_completed={follow_completed}")
            if not state.get("email_request_sent"):
                # Send email request for the first time
                print(f"✅ [PRE-DM] Sending email request (ask_to_follow={ask_to_follow}, follow_completed={follow_completed})")
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
                # Email request was sent but not received - wait for email
                print(f"⏳ [PRE-DM] Email request sent but not received - returning wait_for_email")
                return {
                    "action": "wait_for_email",
                    "message": None,
                    "should_save_email": False,
                    "email": None
                }
        
        # Step 3: Send Primary DM (if pre-DM actions are done)
        # This should only happen if both follow and email are completed
        if follow_completed and email_completed:
            print(f"✅ [PRE-DM] Both follow and email completed - sending primary DM")
            update_pre_dm_state(sender_id, rule.id, {
                "primary_dm_sent": True,
                "step": "primary"
            })
            return {
                "action": "send_primary",
                "message": None,
                "should_save_email": False,
                "email": state.get("email")
            }
        
        # If we reach here, something unexpected - wait for user action
        print(f"⚠️ [PRE-DM] Unexpected state - returning wait action")
        return {
            "action": "wait",
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
