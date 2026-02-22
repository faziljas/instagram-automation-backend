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
from app.services.lead_capture import validate_email, validate_phone, update_automation_stats
from app.utils.disposable_email import is_disposable_email


# In-memory state tracker for pre-DM conversations
# Key: f"{sender_id}_{rule_id}", Value: {"step": "follow"|"email"|"primary", "followed": bool, "email_sent": bool}
_pre_dm_states: Dict[str, Dict[str, Any]] = {}


_MAX_COMMENT_REPLIED_IDS = 50  # Keep last N comment IDs we replied to (per sender+rule)

def get_pre_dm_state(sender_id: str, rule_id: int) -> Dict[str, Any]:
    """Get the current pre-DM state for a sender-rule combination."""
    key = f"{sender_id}_{rule_id}"
    return _pre_dm_states.get(key, {
        "step": "initial",
        "follow_request_sent": False,
        "email_request_sent": False,
        "email_received": False,
        "phone_request_sent": False,
        "phone_received": False,
        "primary_dm_sent": False,
        "follow_exit_sent": False,  # user said No to "Are you following me?" → exit message sent; no DM reply until they comment again
        "comment_replied_comment_ids": [],
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
            "phone_request_sent": False,
            "phone_received": False,
            "primary_dm_sent": False,
            "follow_exit_sent": False,
            "comment_replied_comment_ids": [],
        }
    _pre_dm_states[key].update(updates)

    # Clean up old states (keep only last 1000)
    if len(_pre_dm_states) > 1000:
        keys_to_remove = list(_pre_dm_states.keys())[:100]
        for k in keys_to_remove:
            del _pre_dm_states[k]


def mark_comment_replied(sender_id: str, rule_id: int, comment_id: str) -> None:
    """Record that we sent a public comment reply to this specific comment.
    Used to avoid replying twice to the same comment, while still replying to each new comment."""
    state = get_pre_dm_state(sender_id, rule_id)
    ids = list(state.get("comment_replied_comment_ids") or [])
    if comment_id and comment_id not in ids:
        ids.append(comment_id)
        if len(ids) > _MAX_COMMENT_REPLIED_IDS:
            ids = ids[-_MAX_COMMENT_REPLIED_IDS:]
        update_pre_dm_state(sender_id, rule_id, {"comment_replied_comment_ids": ids})


def was_comment_replied(sender_id: str, rule_id: int, comment_id: str) -> bool:
    """Return True if we already sent a comment reply to this specific comment_id."""
    if not comment_id:
        return False
    state = get_pre_dm_state(sender_id, rule_id)
    ids = state.get("comment_replied_comment_ids") or []
    return comment_id in ids


def clear_pre_dm_state(sender_id: str, rule_id: int):
    """Clear the pre-DM state for a sender-rule combination."""
    key = f"{sender_id}_{rule_id}"
    if key in _pre_dm_states:
        del _pre_dm_states[key]


def normalize_follow_recheck_message(msg: Optional[str], default: str = "Are you following me?") -> str:
    """Use 'Are you following me?' even if rule config still has old 'Are you followed?'."""
    if not msg or not str(msg).strip():
        return default
    s = str(msg).strip().lower().replace("?", "").strip()
    if s == "are you followed":
        return "Are you following me?"
    return str(msg).strip()


def sender_primary_dm_complete(
    sender_id: str,
    account_id: int,
    rules: list,
    db: Session,
) -> bool:
    """
    Return True if this sender has completed primary DM with ALL rules in the provided list.
    CRITICAL FIX: Check per-rule, not globally. Each reel/post should work independently.
    
    When True, no automation should run for this user — all messages go to real user.

    - Simple reply: primary_dm_sent for that rule → complete.
    - Lead capture: primary_dm_sent AND lead captured for THAT SPECIFIC rule → complete.

    PERFORMANCE: In-memory state first (no DB). Only queries DB when needed.
    """
    sender_id_str = str(sender_id)
    
    if not rules:
        return False
    
    # CRITICAL FIX: Check each rule independently
    # Only return True if ALL rules have completed their flow
    # This ensures Reel A (phone) doesn't skip when Reel B (email) collected lead
    for rule in rules:
        state = get_pre_dm_state(sender_id_str, rule.id)
        
        # Check if this specific rule has completed
        if not state.get("primary_dm_sent"):
            # This rule hasn't completed yet
            return False
        
        # For lead capture rules, also check if lead was captured for THIS specific rule
        is_lead = (rule.config or {}).get("is_lead_capture", False)
        if is_lead:
            # Check if lead exists for THIS specific rule
            try:
                from sqlalchemy import cast
                from sqlalchemy.dialects.postgresql import JSONB
                has_lead_for_this_rule = (
                    db.query(CapturedLead.id)
                    .filter(
                        CapturedLead.instagram_account_id == account_id,
                        CapturedLead.automation_rule_id == rule.id,  # CRITICAL: Check for THIS rule
                        cast(CapturedLead.extra_metadata, JSONB)["sender_id"].astext == sender_id_str,
                    )
                    .limit(1)
                    .first()
                )
                if not has_lead_for_this_rule:
                    # This rule hasn't captured lead yet
                    return False
            except Exception:
                # On error, assume not complete to be safe
                return False
    
    # All rules have completed
    return True


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
        "followp",  # FIX ISSUE 3: Added "followp" variation
        "follow p",  # FIX ISSUE 3: Added "follow p" variation
        "follow up",  # Lead capture flow: user confirms via "follow up"
        "followup",   # Lead capture flow: user confirms via "followup"
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
    exact_matches = ["follow", "following", "done", "ok", "okay", "yes", "followed", "y", "k", "sure", "yep", "yup", "yeah", "got it", "finished", "complete", "followp", "follow p", "follow up", "followup"]  # Lead capture + simple flow
    if message_lower in exact_matches:
        return True
    
    return False


def is_follow_me_intent(message_text: str) -> bool:
    """
    Requirement Rule 2: User says FOLLOW ME (button or text) → ask "Are you following me?".
    Returns True if message indicates "Follow Me" intent (e.g. follow me, follow, follow me 👇).
    """
    if not message_text or not isinstance(message_text, str):
        return False
    msg = message_text.strip().lower()
    if not msg:
        return False
    # Exact/short: "follow me", "follow", "follow me 👇" (emoji stripped for comparison)
    if msg in ["follow me", "follow", "follow me 👇", "follow me 👋", "follow me please", "follow me!"]:
        return True
    # Starts with "follow" and short (e.g. "follow me", "follow pls")
    if msg.startswith("follow") and len(msg) <= 25:
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
    skip_growth_steps: bool = False,  # If True, skip follow/email steps and go directly to primary DM
    is_bot_message: bool = False  # If True, ignore this message (bot's own message)
) -> Dict[str, Any]:
    """
    Process pre-DM actions (Ask to Follow, Ask for Email) before sending primary DM.
    
    Args:
        skip_growth_steps: If True, skip follow/email steps and go directly to primary DM (VIP users)
        is_bot_message: If True, ignore this message and continue waiting for user response
    
    Returns:
        {
            "action": "send_follow_request" | "send_email_request" | "send_primary" | "wait_for_email",
            "message": str,
            "should_save_email": bool,
            "email": str | None
        }
    """
    # CRITICAL FIX: Ignore bot's own messages when waiting for user responses
    # This prevents bot messages from breaking the flow
    if is_bot_message and incoming_message:
        state = get_pre_dm_state(sender_id, rule.id)
        # If we're waiting for follow confirmation, continue waiting
        if state.get("follow_request_sent") and not state.get("follow_confirmed"):
            print(f"🚫 [PRE-DM FIX] Ignoring bot message while waiting for follow confirmation: '{incoming_message}'")
            return {
                "action": "wait_for_follow",
                "message": None,
                "should_save_email": False,
                "email": None
            }
        # If we're waiting for email, continue waiting
        if state.get("email_request_sent") and not state.get("email_received"):
            print(f"🚫 [PRE-DM FIX] Ignoring bot message while waiting for email: '{incoming_message}'")
            return {
                "action": "wait_for_email",
                "message": None,
                "should_save_email": False,
                "email": None
            }
    
    # Persist trigger type on first run so "No" button / postback can show correct exit message (Story reply again vs Comment again).
    # Must run before skip_growth_steps so VIP users also get trigger_type stored.
    state = get_pre_dm_state(sender_id, rule.id)
    if trigger_type and not state.get("trigger_type"):
        update_pre_dm_state(sender_id, rule.id, {"trigger_type": trigger_type})
        state = get_pre_dm_state(sender_id, rule.id)
    
    # VIP USER CHECK: If skip_growth_steps is True, skip directly to primary DM
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
    
    # NEW SIMPLIFIED MVP APPROACH: Single toggle "Pre-DM Engagement Message"
    # If enable_pre_dm_engagement is set, use it to control both follow and email
    # Otherwise, fall back to old behavior (backward compatibility)
    enable_pre_dm_engagement = config.get("enable_pre_dm_engagement")
    
    # CRITICAL: Check if phone flow is enabled - if so, don't ask for email
    simple_dm_flow_phone = config.get("simple_dm_flow_phone", False) or config.get("simpleDmFlowPhone", False)
    
    if enable_pre_dm_engagement is not None:
        # New simplified mode: single toggle controls both
        ask_to_follow = enable_pre_dm_engagement
        # CRITICAL FIX: If phone flow is enabled, don't ask for email (phone flow replaces email)
        ask_for_email = enable_pre_dm_engagement and not simple_dm_flow_phone
    else:
        # Backward compatibility: use old individual checkboxes
        ask_to_follow = config.get("ask_to_follow", False)
        # CRITICAL FIX: If phone flow is enabled, don't ask for email (phone flow replaces email)
        ask_for_email = config.get("ask_for_email", False) and not simple_dm_flow_phone
    
    # Three independent flows: Follower, Email, Phone. Each acts alone; no mixing.
    # Follower flow = follow only → primary DM (no email, no phone).
    simple_dm_flow = config.get("simple_dm_flow", False) or config.get("simpleDmFlow", False)
    is_follower_flow = ask_to_follow and not simple_dm_flow and not simple_dm_flow_phone
    if is_follower_flow:
        ask_for_email = False  # Follower flow never asks for email; remove email step completely
    
    ask_to_follow_message = config.get("ask_to_follow_message", "Hey! Would you mind following me? I share great content! 🙌")
    ask_for_email_message = config.get("ask_for_email_message", "Quick question - what's your email? I'd love to send you something special! 📧")
    
    # EXIT = after "No problem! Comment again!" bot does not respond to DMs until user comments/replies to story again (or we asked "Are you following me?" and they reply).
    comment_triggers = ["post_comment", "keyword", "live_comment", "story_reply"]
    if state.get("follow_exit_sent") and not state.get("follow_confirmed"):
        if trigger_type not in comment_triggers:
            # DM after exit: no reply (EXIT) — except when we just asked "Are you following me?" (re-comment), then we must handle Yes → primary or else → exit message
            if state.get("follow_recheck_sent") and incoming_message:
                pass  # fall through: handle Yes → primary, else → exit message (Rule 4 & 5)
            else:
                print(f"📩 [EXIT] DM after exit — not replying until user comments again on post or replies to story")
                return {"action": "wait", "message": None, "should_save_email": False, "email": None}
    
    # ---------------------------------------------------------
    # Simple DM flow (Email): one follow+email message, then loop email until valid
    # No "I'm following" / "Follow Me" / "Are you following me?" / "Share Email" / "Skip"
    # simple_dm_flow already set above for flow-type detection
    # ---------------------------------------------------------
    # When we're already in phone flow, don't run email logic (would wrongly treat e.g. "97920453" as invalid email)
    # CRITICAL: In phone flow the first message is "follow + phone" in one, so we only set follow_request_sent.
    # Treat as phone flow when we've sent that so we don't accept email as valid (validate phone only).
    # CRITICAL: For phone-only rules (simple_dm_flow_phone and not simple_dm_flow), never run email block.
    in_phone_flow = (
        state.get("step") == "phone"
        or state.get("phone_request_sent")
        or (simple_dm_flow_phone and state.get("follow_request_sent"))
        or (simple_dm_flow_phone and not simple_dm_flow)
    )
    
    # CRITICAL FIX: If config changed from phone to email, clear old phone state to allow email collection
    # This handles scenario where Reel A was configured for phone (collected phone), then changed to email
    simple_dm_flow_phone = config.get("simple_dm_flow_phone", False) or config.get("simpleDmFlowPhone", False)
    if simple_dm_flow and not simple_dm_flow_phone:
        # Config is now email-only, but state might have old phone data - clear it
        if state.get("phone_received") or state.get("phone_request_sent") or state.get("step") == "phone":
            print(f"🔄 [CONFIG CHANGE] Rule {rule.id} changed from phone to email. Clearing old phone state to allow email collection.")
            update_pre_dm_state(sender_id, rule.id, {
                "phone_received": False,
                "phone_request_sent": False,
                "phone": None,
                "step": "email" if state.get("email_request_sent") else "initial"
            })
            # Update in_phone_flow check after clearing state
            in_phone_flow = False
    
    if simple_dm_flow and not in_phone_flow:
        simple_flow_message = config.get("simple_flow_message") or config.get("simpleFlowMessage") or (
            "Follow me to get the guide 👇 Reply with your email and I'll send it! 📧"
        )
        simple_flow_email_question = config.get("simple_flow_email_question") or config.get("simpleFlowEmailQuestion") or (
            "What's your email? Reply here and I'll send you the guide! 📧"
        )
        # Already have email (this rule or captured earlier on any rule for this account) → send primary, don't ask again
        if state.get("email_received") or state.get("email"):
            return {
                "action": "send_primary",
                "message": None,
                "should_save_email": False,
                "email": state.get("email"),
            }
        try:
            from sqlalchemy import cast
            from sqlalchemy.dialects.postgresql import JSONB
            sender_id_str = str(sender_id) if sender_id else None
            lead = db.query(CapturedLead).filter(
                CapturedLead.instagram_account_id == account.id,
                CapturedLead.email.isnot(None),
                cast(CapturedLead.extra_metadata, JSONB)["sender_id"].astext == sender_id_str,
            ).first()
            if lead and lead.email:
                return {
                    "action": "send_primary",
                    "message": None,
                    "should_save_email": False,
                    "email": lead.email,
                }
        except Exception:
            pass
        # We already sent the first message; this is a reply (comment or DM)
        if state.get("follow_request_sent") or state.get("email_request_sent"):
            if incoming_message:
                is_email, email_address = check_if_email_response(incoming_message)
                if is_email:
                    # Reject disposable/temp domains (same blocklist as sign-up)
                    if is_disposable_email(email_address):
                        invalid_msg = config.get("email_invalid_retry_message") or config.get("emailInvalidRetryMessage") or config.get("email_retry_message") or config.get("emailRetryMessage") or (
                            "That doesn't look like a valid email. 🤔 Please share your correct email so I can send you the guide! 📧"
                        )
                        return {
                            "action": "send_email_retry",
                            "message": invalid_msg,
                            "should_save_email": False,
                            "email": None,
                        }
                    update_pre_dm_state(sender_id, rule.id, {
                        "email_received": True,
                        "email": email_address,
                        # DO NOT set primary_dm_sent here - it will be set AFTER execute_automation_action successfully sends the DM
                    })
                    try:
                        captured_lead = CapturedLead(
                            user_id=account.user_id,
                            instagram_account_id=account.id,
                            automation_rule_id=rule.id,
                            email=email_address,
                            extra_metadata={
                                "sender_id": sender_id,
                                "captured_via": "simple_dm_flow",
                                "timestamp": datetime.utcnow().isoformat(),
                            },
                        )
                        db.add(captured_lead)
                        db.commit()
                        db.refresh(captured_lead)
                        try:
                            from app.services.global_conversion_check import update_audience_email
                            update_audience_email(db, sender_id, account.id, account.user_id, email_address)
                        except Exception:
                            pass
                        try:
                            update_automation_stats(rule.id, "lead_captured", db)
                        except Exception:
                            pass
                        # FIX ISSUE 1: Log EMAIL_COLLECTED analytics event so dashboard "Leads Collected" count increases
                        try:
                            from app.utils.analytics import log_analytics_event_sync
                            from app.models.analytics_event import EventType
                            media_id = rule.config.get("media_id") if hasattr(rule, "config") else None
                            log_analytics_event_sync(
                                db=db,
                                user_id=account.user_id,
                                event_type=EventType.EMAIL_COLLECTED,
                                rule_id=rule.id,
                                media_id=media_id,
                                instagram_account_id=account.id,
                                metadata={
                                    "sender_id": sender_id,
                                    "email": email_address,
                                    "captured_via": "simple_dm_flow",
                                },
                            )
                            print(f"✅ EMAIL_COLLECTED analytics event logged for email: {email_address}")
                        except Exception as analytics_err:
                            print(f"⚠️ Failed to log EMAIL_COLLECTED event: {str(analytics_err)}")
                    except Exception as e:
                        print(f"⚠️ Error saving email (simple flow): {str(e)}")
                        db.rollback()
                    return {
                        "action": "send_primary",
                        "message": None,
                        "should_save_email": False,
                        "email": email_address,
                        "send_email_success": True,
                    }
                # Not a valid email: if they said ok/done/okay/following etc. → re-ask email question; else → invalid-email message
                if check_if_follow_confirmation(incoming_message):
                    return {
                        "action": "send_email_request",
                        "message": simple_flow_email_question,
                        "should_save_email": False,
                        "email": None,
                    }
                # Reject phone number when we asked for email: do not accept phone as valid
                is_valid_phone, _ = validate_phone(incoming_message.strip())
                if is_valid_phone:
                    print(f"⚠️ [EMAIL FLOW] User sent phone number while we asked for email — rejecting, asking for email again")
                    invalid_msg = config.get("email_not_phone_retry_message") or config.get("emailNotPhoneRetryMessage") or (
                        "We need your email for this, not your phone number. 📧 Please reply with your email address!"
                    )
                    return {
                        "action": "send_email_retry",
                        "message": invalid_msg,
                        "should_save_email": False,
                        "email": None,
                    }
                invalid_msg = config.get("email_invalid_retry_message") or config.get("emailInvalidRetryMessage") or config.get("email_retry_message") or config.get("emailRetryMessage") or (
                    "That doesn't look like a valid email. 🤔 Please share your correct email so I can send you the guide! 📧"
                )
                return {
                    "action": "send_email_retry",
                    "message": invalid_msg,
                    "should_save_email": False,
                    "email": None,
                }
            # No incoming message (e.g. timeout) → still ask for email
            return {
                "action": "send_email_request",
                "message": simple_flow_email_question,
                "should_save_email": False,
                "email": None,
            }
        # First time: send the one combined message (follow + reply with email)
        if trigger_type in ["post_comment", "keyword", "new_message", "story_reply"]:
            update_pre_dm_state(sender_id, rule.id, {
                "follow_request_sent": True,
                "email_request_sent": True,
                "step": "email",
            })
            return {
                "action": "send_simple_flow_start",
                "message": simple_flow_message,
                "should_save_email": False,
                "email": None,
            }
        return {"action": "ignore", "message": None, "should_save_email": False, "email": None}
    
    # ---------------------------------------------------------
    # Simple DM flow (Phone): one follow+phone message, then loop until valid phone
    # Same as email simple flow but collect phone; no disposable-phone list (format validation only).
    # FIX ISSUE 2: Only run phone flow if email flow is NOT active
    # This prevents asking for phone number when user only configured email collection
    # ---------------------------------------------------------
    simple_dm_flow_phone = config.get("simple_dm_flow_phone", False) or config.get("simpleDmFlowPhone", False)
    
    # CRITICAL FIX: If config changed from email to phone, clear old email state to allow phone collection
    # This handles scenario where Reel A was configured for email (collected email), then changed to phone
    if simple_dm_flow_phone and not simple_dm_flow:
        # Config is now phone-only, but state might have old email data - clear it
        if state.get("email_received") or state.get("email_request_sent") or state.get("step") == "email":
            print(f"🔄 [CONFIG CHANGE] Rule {rule.id} changed from email to phone. Clearing old email state to allow phone collection.")
            update_pre_dm_state(sender_id, rule.id, {
                "email_received": False,
                "email_request_sent": False,
                "email": None,
                "step": "phone" if state.get("phone_request_sent") else "initial"
            })
    
    # CRITICAL FIX: Don't run phone flow if email flow is active (even if email not received yet)
    # This prevents the bug where system asks for phone after email is collected or when email flow is configured
    # Phone flow should only run if email flow is explicitly disabled
    if simple_dm_flow_phone and not simple_dm_flow:
        simple_flow_phone_message = config.get("simple_flow_phone_message") or config.get("simpleFlowPhoneMessage") or (
            "Follow me to get the guide 👇 Reply with your phone number and I'll send it! 📱"
        )
        simple_flow_phone_question = config.get("simple_flow_phone_question") or config.get("simpleFlowPhoneQuestion") or (
            "What's your phone number? Reply here and I'll send you the guide! 📱"
        )
        phone_invalid_msg = config.get("phone_invalid_retry_message") or config.get("phoneInvalidRetryMessage") or (
            "That doesn't look like a valid phone number. 🤔 Please share your correct number so I can send you the guide! 📱"
        )
        if state.get("phone_received") or state.get("phone"):
            return {
                "action": "send_primary",
                "message": None,
                "should_save_email": False,
                "email": None,
                "phone": state.get("phone"),
            }
        try:
            from sqlalchemy import cast
            from sqlalchemy.dialects.postgresql import JSONB
            sender_id_str = str(sender_id) if sender_id else None
            lead = db.query(CapturedLead).filter(
                CapturedLead.instagram_account_id == account.id,
                CapturedLead.phone.isnot(None),
                cast(CapturedLead.extra_metadata, JSONB)["sender_id"].astext == sender_id_str,
            ).first()
            if lead and lead.phone:
                return {
                    "action": "send_primary",
                    "message": None,
                    "should_save_email": False,
                    "email": None,
                    "phone": lead.phone,
                }
        except Exception:
            pass
        if state.get("follow_request_sent") or state.get("phone_request_sent"):
            if incoming_message:
                # Reject email when we asked for phone: do not accept email as valid
                is_email_like, _ = check_if_email_response(incoming_message)
                if is_email_like:
                    print(f"⚠️ [PHONE FLOW] User sent email while we asked for phone — rejecting, asking for phone again")
                    return {
                        "action": "send_phone_retry",
                        "message": config.get("phone_not_email_retry_message") or config.get("phoneNotEmailRetryMessage") or (
                            "We need your phone number for this, not your email. 📱 Please reply with your phone number!"
                        ),
                        "should_save_email": False,
                        "email": None,
                    }
                is_valid_phone, _ = validate_phone(incoming_message.strip())
                if is_valid_phone:
                    phone_number = re.sub(r'[\s\-\(\)\+\.]', '', incoming_message.strip())
                    update_pre_dm_state(sender_id, rule.id, {
                        "phone_received": True,
                        "phone": phone_number,
                        # DO NOT set primary_dm_sent here - it will be set AFTER execute_automation_action successfully sends the DM
                    })
                    try:
                        captured_lead = CapturedLead(
                            user_id=account.user_id,
                            instagram_account_id=account.id,
                            automation_rule_id=rule.id,
                            email=None,
                            phone=phone_number,
                            extra_metadata={
                                "sender_id": sender_id,
                                "captured_via": "simple_dm_flow_phone",
                                "timestamp": datetime.utcnow().isoformat(),
                            },
                        )
                        db.add(captured_lead)
                        db.commit()
                        db.refresh(captured_lead)
                        try:
                            update_automation_stats(rule.id, "lead_captured", db)
                        except Exception:
                            pass
                    except Exception as e:
                        print(f"⚠️ Error saving phone (simple flow): {str(e)}")
                        db.rollback()
                    # Log PHONE_COLLECTED so dashboard "Leads Collected" and Top Performing LEADS count it
                    try:
                        from app.utils.analytics import log_analytics_event_sync
                        from app.models.analytics_event import EventType
                        media_id = rule.config.get("media_id") if hasattr(rule, "config") else None
                        log_analytics_event_sync(
                            db=db,
                            user_id=account.user_id,
                            event_type=EventType.PHONE_COLLECTED,
                            rule_id=rule.id,
                            media_id=media_id,
                            instagram_account_id=account.id,
                            metadata={
                                "sender_id": sender_id,
                                "phone": phone_number,
                                "captured_via": "simple_dm_flow_phone",
                            },
                        )
                        print(f"✅ PHONE_COLLECTED analytics event logged for phone: {phone_number}")
                    except Exception as analytics_err:
                        print(f"⚠️ Failed to log PHONE_COLLECTED event: {str(analytics_err)}")
                    return {
                        "action": "send_primary",
                        "message": None,
                        "should_save_email": False,
                        "email": None,
                        "phone": phone_number,
                        "send_email_success": False,
                    }
                if check_if_follow_confirmation(incoming_message):
                    return {
                        "action": "send_phone_request",
                        "message": simple_flow_phone_question,
                        "should_save_email": False,
                        "email": None,
                    }
                return {
                    "action": "send_phone_retry",
                    "message": phone_invalid_msg,
                    "should_save_email": False,
                    "email": None,
                }
            return {
                "action": "send_phone_request",
                "message": simple_flow_phone_question,
                "should_save_email": False,
                "email": None,
            }
        if trigger_type in ["post_comment", "keyword", "new_message", "story_reply"]:
            update_pre_dm_state(sender_id, rule.id, {
                "follow_request_sent": True,
                "phone_request_sent": True,
                "step": "phone",
            })
            return {
                "action": "send_simple_flow_start_phone",
                "message": simple_flow_phone_message,
                "should_save_email": False,
                "email": None,
            }
        return {"action": "ignore", "message": None, "should_save_email": False, "email": None}
    
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
        comment_triggers = ["post_comment", "keyword", "live_comment", "story_reply"]

        # v2 Re-Comment / Re-Story after Skip (Use Case 1 & 2): skip_for_now_no_final_dm → no Final DM on skip; on re-comment or story reply ask follow then email or email directly
        skip_no_final_dm = config.get("skip_for_now_no_final_dm", True) or config.get("skipForNowNoFinalDm", True)
        if skip_no_final_dm and trigger_type in comment_triggers and state.get("email_skipped") and not state.get("email_received") and ask_for_email:
            sender_id_str = str(sender_id) if sender_id else None
            has_lead = False
            try:
                from sqlalchemy import cast
                from sqlalchemy.dialects.postgresql import JSONB
                has_lead = db.query(CapturedLead.id).filter(
                    CapturedLead.instagram_account_id == account.id,
                    CapturedLead.automation_rule_id == rule.id,
                    cast(CapturedLead.extra_metadata, JSONB)["sender_id"].astext == sender_id_str,
                ).limit(1).first() is not None
            except Exception:
                pass
            if not has_lead:
                # Always ask "Are you following me?" on re-comment (don't skip to email even if follow was confirmed)
                print(f"📧 [v2 Re-Comment] No lead — sending 'Are you following me?' (always ask on re-comment)")
                reengagement_msg = config.get("reengagement_follow_message", "Are you following me?")
                return {
                    "action": "send_reengagement_follow_check",
                    "message": reengagement_msg,
                    "should_save_email": False,
                    "email": None,
                }

        # RE-ENGAGEMENT (opt-in): When user commented again but we never collected lead (they skipped email),
        # re-ask for email instead of sending final DM again. Only when rule config enables it (BAU unchanged).
        reask_email_if_no_lead = config.get("reask_email_on_comment_if_no_lead", False) or config.get("reaskEmailOnCommentIfNoLead", False)
        if reask_email_if_no_lead and (trigger_type in comment_triggers and state.get("primary_dm_sent") and state.get("email_skipped")
            and not state.get("email_received") and ask_for_email):
            sender_id_str = str(sender_id) if sender_id else None
            has_lead = False
            try:
                from sqlalchemy import cast
                from sqlalchemy.dialects.postgresql import JSONB
                has_lead = db.query(CapturedLead.id).filter(
                    CapturedLead.instagram_account_id == account.id,
                    CapturedLead.automation_rule_id == rule.id,
                    cast(CapturedLead.extra_metadata, JSONB)["sender_id"].astext == sender_id_str,
                ).limit(1).first() is not None
            except Exception:
                pass
            if not has_lead:
                update_pre_dm_state(sender_id, rule.id, {"email_skipped": False})
                print(f"📧 [RE-ENGAGEMENT] No lead for sender {sender_id} — re-asking for email on comment")
                return {
                    "action": "send_email_request",
                    "message": ask_for_email_message,
                    "should_save_email": False,
                    "email": None,
                }

        # User commented again (or replied to story) after "No" (exit) — ask only "Are you following me?" with Yes/No (loop until Yes).
        # Applies to comment and story_reply triggers; for plain DM we handle "No" response below (send exit again).
        if ask_to_follow and state.get("follow_exit_sent") and not state.get("follow_confirmed") and trigger_type in comment_triggers:
            raw = config.get("follow_recheck_message") or config.get("followRecheckMessage") or "Are you following me?"
            follow_recheck_msg = normalize_follow_recheck_message(raw)
            update_pre_dm_state(sender_id, rule.id, {"follow_recheck_sent": True})
            print(f"📩 Re-comment after exit — sending 'Are you following me?' with Yes/No (loop until positive reply)")
            return {
                "action": "send_follow_recheck",
                "message": follow_recheck_msg,
                "should_save_email": False,
                "email": None,
            }

        # Case A: THIS FLOW has been completed (follow confirmed AND email received if required)
        # Only skip to primary when THIS FLOW was completed in a previous interaction
        # v2: If they skipped email and we never captured lead, do NOT treat as completed (re-engagement will handle on next comment)
        if flow_has_completed:
            if skip_no_final_dm and state.get("email_skipped") and not state.get("email_received"):
                _has_lead = False
                try:
                    from sqlalchemy import cast
                    from sqlalchemy.dialects.postgresql import JSONB
                    _sid = str(sender_id) if sender_id else None
                    _has_lead = db.query(CapturedLead.id).filter(
                        CapturedLead.instagram_account_id == account.id,
                        CapturedLead.automation_rule_id == rule.id,
                        cast(CapturedLead.extra_metadata, JSONB)["sender_id"].astext == _sid,
                    ).limit(1).first() is not None
                except Exception:
                    pass
                if not _has_lead:
                    # v2: Don't send primary; wait for re-comment to trigger Use Case 1 or 2
                    return {"action": "wait_for_email", "message": None, "should_save_email": False, "email": None}
                else:
                    # DO NOT set primary_dm_sent here - it will be set AFTER execute_automation_action successfully sends the DM
                    return {"action": "send_primary", "message": None, "should_save_email": False, "email": state.get("email")}
            else:
                # DO NOT set primary_dm_sent here - it will be set AFTER execute_automation_action successfully sends the DM
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
            # DO NOT set primary_dm_sent here - it will be set AFTER execute_automation_action successfully sends the DM
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
            
            # FIX ISSUE 3: Track follower gain count when user confirms following via text
            try:
                update_automation_stats(rule.id, "follower_gained", db)
                print(f"✅ Follower gain count updated for rule {rule.id}")
            except Exception as stats_err:
                print(f"⚠️ Failed to update follower gain count: {str(stats_err)}")
            
            # Log IM_FOLLOWING_CLICKED so "Followers Gained via AutoDM" increases on analytics dashboard
            try:
                from app.utils.analytics import log_analytics_event_sync
                from app.models.analytics_event import EventType
                media_id = (rule.config or {}).get("media_id")
                log_analytics_event_sync(
                    db=db,
                    user_id=account.user_id,
                    event_type=EventType.IM_FOLLOWING_CLICKED,
                    rule_id=rule.id,
                    media_id=media_id,
                    instagram_account_id=account.id,
                    metadata={
                        "sender_id": str(sender_id),
                        "confirmed_via": "text",
                        "message": incoming_message[:200] if incoming_message else None,
                    },
                )
                print(f"✅ [FIX] IM_FOLLOWING_CLICKED analytics event logged for text confirmation: '{incoming_message}'")
            except Exception as analytics_err:
                print(f"⚠️ Failed to log IM_FOLLOWING_CLICKED for text confirmation: {str(analytics_err)}")
            
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
                # DO NOT set primary_dm_sent here - it will be set AFTER execute_automation_action successfully sends the DM
                return {
                    "action": "send_primary",
                    "message": None,
                    "should_save_email": False,
                    "email": None
                }
        else:
            # User replied with random text that isn't a follow confirmation
            # Check if they're responding to "Are you following me?" question
            if state.get("follow_recheck_sent", False):
                # Rule 4 & 5: Responding to "Are you following me?" — Yes/positive → primary DM, EXIT; else (no, negative, rubbish, gibberish) → exit message, EXIT
                if check_if_follow_confirmation(incoming_message):
                    # Rule 4: Yes → primary DM, EXIT
                    print(f"✅ [Rule 4] User said Yes to 'Are you following me?' — primary DM, EXIT")
                    update_pre_dm_state(sender_id, rule.id, {
                        "follow_confirmed": True,
                        "follow_recheck_sent": False
                    })
                    try:
                        update_automation_stats(rule.id, "follower_gained", db)
                    except Exception:
                        pass
                    try:
                        from app.utils.analytics import log_analytics_event_sync
                        from app.models.analytics_event import EventType
                        media_id = (rule.config or {}).get("media_id")
                        log_analytics_event_sync(
                            db=db,
                            user_id=account.user_id,
                            event_type=EventType.IM_FOLLOWING_CLICKED,
                            rule_id=rule.id,
                            media_id=media_id,
                            instagram_account_id=account.id,
                            metadata={
                                "sender_id": str(sender_id),
                                "confirmed_via": "are_you_following_me_yes",
                                "message": incoming_message[:200] if incoming_message else None,
                            },
                        )
                    except Exception:
                        pass
                    try:
                        from app.services.global_conversion_check import update_audience_following
                        update_audience_following(db, sender_id, account.id, account.user_id, is_following=True)
                    except Exception:
                        pass
                    # Follower flow: after Yes to "Are you following me?" send ONLY primary DM (UI config). No email-request message.
                    update_pre_dm_state(sender_id, rule.id, {"email_skipped": True})
                    return {
                        "action": "send_primary",
                        "message": None,
                        "should_save_email": False,
                        "email": None
                    }
                # Rule 5: No / negative / rubbish / gibberish → exit message, EXIT (loop continues when they comment/reply to story again)
                _default_exit = "No problem! Story reply again anytime when you'd like the guide. 📩" if trigger_type == "story_reply" else "No problem! Comment again anytime when you'd like the guide. 📩"
                exit_msg = config.get("follow_no_exit_message") or config.get("followNoExitMessage") or _default_exit
                update_pre_dm_state(sender_id, rule.id, {
                    "follow_recheck_sent": False,
                    "follow_exit_sent": True,
                    "follow_request_sent": True,
                })
                print(f"📩 [Rule 5] User said No/other to 'Are you following me?' — exit message, EXIT")
                return {
                    "action": "send_follow_no_exit",
                    "message": exit_msg,
                    "should_save_email": False,
                    "email": None,
                }
            else:
                # Already sent exit message — do NOT reply to any DM until user comments again (new comment restarts flow)
                if state.get("follow_exit_sent"):
                    print(f"📩 User sent '{incoming_message}' after exit message — ignoring until they comment again")
                    return {
                        "action": "wait",
                        "message": None,
                        "should_save_email": False,
                        "email": None,
                    }
                # Rule 2 vs Rule 3: Initial message — only "Follow Me" intent → ask "Are you following me?"; any other text (no, negative, rubbish, gibberish) → exit
                if is_follow_me_intent(incoming_message):
                    raw = config.get("follow_recheck_message") or config.get("followRecheckMessage") or "Are you following me?"
                    follow_recheck_msg = normalize_follow_recheck_message(raw)
                    update_pre_dm_state(sender_id, rule.id, {"follow_recheck_sent": True})
                    print(f"📩 [Rule 2] User said Follow Me — asking '{follow_recheck_msg}'")
                    return {
                        "action": "send_follow_recheck",
                        "message": follow_recheck_msg,
                        "should_save_email": False,
                        "email": None,
                    }
                # Rule 3: Any other reply to initial message (no, negative, random, rubbish, gibberish) → exit message, EXIT
                _default_exit = "No problem! Story reply again anytime when you'd like the guide. 📩" if trigger_type == "story_reply" else "No problem! Comment again anytime when you'd like the guide. 📩"
                exit_msg = config.get("follow_no_exit_message") or config.get("followNoExitMessage") or _default_exit
                update_pre_dm_state(sender_id, rule.id, {
                    "follow_recheck_sent": False,
                    "follow_exit_sent": True,
                    "follow_request_sent": True,
                })
                print(f"📩 [Rule 3] Initial message reply (not following / not Follow Me) — sending exit, EXIT")
                return {
                    "action": "send_follow_no_exit",
                    "message": exit_msg,
                    "should_save_email": False,
                    "email": None,
                }
    
    # Check if this is a response to an email request (email flow only; follower flow never asks for email)
    # IMPORTANT: Only process emails from DMs, NOT from comments
    # Comments should only trigger resending the email question as a reminder
    if ask_for_email and incoming_message and state.get("email_request_sent") and not state.get("email_received"):
        # Skip email processing for comment triggers (post/reel/live) - they should only resend email question via private reply.
        # story_reply is intentionally excluded: user sends a DM, so we parse incoming_message as email (same as new_message).
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
            # Reject disposable/temp domains (same blocklist as sign-up)
            if is_disposable_email(email_address):
                print(f"⚠️ Disposable email domain rejected: {email_address}")
                invalid_email_msg = config.get("email_invalid_retry_message") or config.get("emailInvalidRetryMessage") or config.get("email_retry_message") or config.get("emailRetryMessage") or (
                    "That doesn't look like a valid email address. 🤔\n\nPlease share your email so we can send you the guide! 📧"
                )
                return {
                    "action": "send_email_retry",
                    "message": invalid_email_msg,
                    "should_save_email": False,
                    "email": None,
                }
            # STRICT MODE: Valid email received! Save it and proceed DIRECTLY to primary DM
            print(f"✅ [STRICT MODE] Valid email received: {email_address}")
            update_pre_dm_state(sender_id, rule.id, {
                "email_received": True,
                "email": email_address,
                # DO NOT set primary_dm_sent here - it will be set AFTER execute_automation_action successfully sends the DM
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
                try:
                    update_automation_stats(rule.id, "lead_captured", db)
                    print(f"✅ Stats updated: lead_captured for rule {rule.id}")
                except Exception as stats_err:
                    print(f"⚠️ Failed to update stats: {str(stats_err)}")
                print(f"✅ Email saved to database: {email_address}")
                
                # FIX ISSUE 4: Log EMAIL_COLLECTED analytics event (ensure it's always logged)
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
                    print(f"✅ [FIX ISSUE 4] EMAIL_COLLECTED analytics event logged for email: {email_address}")
                except Exception as analytics_err:
                    print(f"⚠️ [FIX ISSUE 4] Failed to log EMAIL_COLLECTED event: {str(analytics_err)}")
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
            # User typed follow confirmation (e.g. "Yes", "done") while waiting for email — resend only the UI-configured email request message (no old/default message).
            if check_if_follow_confirmation(incoming_message):
                print(f"💬 User typed follow confirmation while waiting for email — resending configured email request message")
                return {
                    "action": "send_email_retry",
                    "message": ask_for_email_message,
                    "should_save_email": False,
                    "email": None
                }
            
            # Invalid / non-email text while waiting for email — ask for a valid email (we have this message)
            print(f"⚠️ Invalid email input while waiting for email: '{incoming_message}' — sending retry message")
            invalid_email_msg = config.get("email_invalid_retry_message",
                "That doesn't look like a valid email address. 🤔\n\nPlease share your email so we can send you the guide! 📧")
            return {
                "action": "send_email_retry",
                "message": invalid_email_msg,
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
        # Cross-reel: if user already follows (from another reel / Follower table), treat as follow_completed for this rule too
        follow_completed = not ask_to_follow or state.get("follow_confirmed", False) or (ask_to_follow and already_following)
        # Email is completed if: not asking for email, email received, OR email was skipped
        email_completed = not ask_for_email or state.get("email_received", False) or state.get("email_skipped", False)
        flow_completed = follow_completed and email_completed
        
        print(f"🔍 [PRE-DM DEBUG] trigger_type={trigger_type}, ask_to_follow={ask_to_follow}, ask_for_email={ask_for_email}")
        print(f"🔍 [PRE-DM DEBUG] state: follow_request_sent={state.get('follow_request_sent')}, follow_confirmed={state.get('follow_confirmed')}, email_request_sent={state.get('email_request_sent')}, email_received={state.get('email_received')}")
        print(f"🔍 [PRE-DM DEBUG] flow_completed={flow_completed} (follow_completed={follow_completed}, email_completed={email_completed}, already_following={already_following})")
        
        # If flow is completed, skip directly to primary DM
        if flow_completed:
            print(f"✅ [PRE-DM] Flow completed - skipping to primary DM")
            state_updates = {"step": "primary"}
            if ask_to_follow and already_following and not state.get("follow_confirmed"):
                state_updates["follow_request_sent"] = True
                state_updates["follow_confirmed"] = True
            update_pre_dm_state(sender_id, rule.id, state_updates)
            # DO NOT set primary_dm_sent here - it will be set AFTER execute_automation_action successfully sends the DM
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
                "step": "primary"
                # DO NOT set primary_dm_sent here - it will be set AFTER execute_automation_action successfully sends the DM
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
