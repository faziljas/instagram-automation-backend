import asyncio
import json
import os
import sys
import logging
import requests
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Header, Query, Request, BackgroundTasks, Body
from sqlalchemy.orm import Session
from app.db.session import get_db, SessionLocal
from app.models.instagram_account import InstagramAccount
from app.models.automation_rule import AutomationRule
from app.models.dm_log import DmLog
from app.schemas.instagram import InstagramAccountCreate, InstagramAccountResponse
from app.utils.encryption import encrypt_credentials, decrypt_credentials
from app.services.instagram_client import InstagramClient
from app.dependencies.auth import get_current_user_id
from app.utils.plan_enforcement import check_account_limit
from app.services.pre_dm_handler import normalize_follow_recheck_message

router = APIRouter()

# Set up logger for this module
logger = logging.getLogger(__name__)

# Helper function to log to both logger and print (for backward compatibility)
def log_print(message: str, level: str = "INFO"):
    """Log message using logging module and also print for immediate visibility"""
    if level == "INFO":
        logger.info(message)
    elif level == "WARNING":
        logger.warning(message)
    elif level == "ERROR":
        logger.error(message)
    elif level == "DEBUG":
        logger.debug(message)
    # Also print with flush for immediate output (Render compatibility)
    print(message, file=sys.stderr, flush=True)

# In-memory cache to track recently processed message IDs (prevents duplicate processing)
# Note: This is cleared on restart, but should prevent short-term loops
_processed_message_ids = set()
_MAX_CACHE_SIZE = 1000  # Limit cache size to prevent memory issues

# Track rules that are currently being processed with delays (prevents duplicate triggering)
# Format: (message_id, rule_id) -> timestamp when processing started
_processing_rules = {}
_MAX_PROCESSING_CACHE_SIZE = 1000


def get_or_create_conversation(
    db: Session,
    user_id: int,
    account_id: int,
    participant_id: str,
    participant_name: str = None,
    platform_conversation_id: str = None
):
    """
    Get or create a Conversation record for a participant.
    
    Args:
        db: Database session
        user_id: User ID
        account_id: Instagram account ID
        participant_id: IGSID of the participant (customer)
        participant_name: Username of the participant (optional)
        platform_conversation_id: Instagram Thread ID (optional)
        
    Returns:
        Conversation object
    """
    from app.models.conversation import Conversation
    from datetime import datetime
    
    # Find existing conversation (handle race conditions)
    # Use a loop with retry to handle potential race conditions
    max_retries = 3
    for attempt in range(max_retries):
        conversation = db.query(Conversation).filter(
            Conversation.instagram_account_id == account_id,
            Conversation.user_id == user_id,
            Conversation.participant_id == participant_id
        ).first()
        
        if conversation:
            # Update participant name if provided and different
            if participant_name and conversation.participant_name != participant_name:
                conversation.participant_name = participant_name
            # Update platform_conversation_id if provided
            if platform_conversation_id and not conversation.platform_conversation_id:
                conversation.platform_conversation_id = platform_conversation_id
            return conversation
        else:
            # Create new conversation
            try:
                conversation = Conversation(
                    user_id=user_id,
                    instagram_account_id=account_id,
                    participant_id=participant_id,
                    participant_name=participant_name,
                    platform_conversation_id=platform_conversation_id,
                    updated_at=datetime.utcnow()
                )
                db.add(conversation)
                db.flush()  # Flush to get the ID
                return conversation
            except Exception as e:
                # Handle unique constraint violation (race condition)
                if "unique" in str(e).lower() or "duplicate" in str(e).lower() or "23505" in str(e):
                    # Another process created the conversation, retry query
                    db.rollback()
                    if attempt < max_retries - 1:
                        continue
                    else:
                        # Final attempt: query again
                        conversation = db.query(Conversation).filter(
                            Conversation.instagram_account_id == account_id,
                            Conversation.user_id == user_id,
                            Conversation.participant_id == participant_id
                        ).first()
                        if conversation:
                            return conversation
                        raise
                else:
                    # Different error, re-raise
                    db.rollback()
                    raise
    
    # Should not reach here, but just in case
    raise Exception("Failed to get or create conversation after retries")


@router.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(alias="hub.mode"),
    hub_challenge: str = Query(alias="hub.challenge"),
    hub_verify_token: str = Query(alias="hub.verify_token")
):
    """
    Verify webhook subscription for Instagram Business API.
    Meta sends GET request with hub.mode=subscribe, hub.challenge, and hub.verify_token.
    Must return the challenge value as plain text (not JSON) if verify_token matches.
    """
    verify_token = os.getenv("INSTAGRAM_WEBHOOK_VERIFY_TOKEN", "my_verify_token_123")
     
    log_print(f"🔔 Webhook verification request:")
    log_print(f"   mode={hub_mode}, challenge={hub_challenge}, token={hub_verify_token}")
    log_print(f"   Expected token: {verify_token}")

    if hub_mode == "subscribe" and hub_verify_token == verify_token:
        # Meta expects plain text response, not JSON
        # Return challenge as plain text string
        from fastapi.responses import Response
        log_print(f"✅ Verification successful! Returning challenge: {hub_challenge}")
        return Response(content=hub_challenge, media_type="text/plain")
    
    log_print(f"❌ Verification failed! Token mismatch or invalid mode", "ERROR")
    raise HTTPException(status_code=403, detail="Invalid verify token")

@router.post("/webhook")
async def receive_webhook(
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Receive Instagram webhook events from Meta.
    Processes incoming messages, followers, and other events.
    """
    try:
        # Log request headers for debugging
        headers_dict = dict(request.headers)
        log_print(f"📥 Received webhook request:")
        log_print(f"   Headers: {json.dumps({k: v for k, v in headers_dict.items() if k.lower() in ['content-type', 'x-hub-signature', 'x-hub-signature-256']}, indent=2)}")
        
        body = await request.json()
        log_print(f"📥 Received webhook body: {json.dumps(body, indent=2)}")
        
        # Process webhook event
        if body.get("object") == "instagram":
            for entry in body.get("entry", []):
                # Process messaging events (DMs)
                messaging_events = entry.get("messaging", [])
                if messaging_events:
                    log_print(f"📬 Found {len(messaging_events)} messaging event(s) in webhook entry")
                elif entry.get("changes"):
                    # Comment/live webhooks have "changes" but no "messaging" — normal
                    log_print(f"📬 Entry has 0 messaging events (comment/live or other change event)")
                
                for messaging_event in messaging_events:
                    # Check if this is a postback event (button click)
                    if "postback" in messaging_event:
                        log_print(f"🔘 Processing postback event (button click)")
                        await process_postback_event(messaging_event, db)
                    # Check if this is a regular message event (not message_edit, message_reactions, etc.)
                    # Only process events with a "message" field containing text
                    elif "message" in messaging_event:
                        log_print(f"✅ Processing message event with 'message' field")
                        await process_instagram_message(messaging_event, db)
                    else:
                        # Log other event types (message_edit, message_reactions, etc.) but skip processing
                        event_type = None
                        if "message_edit" in messaging_event:
                            event_type = "message_edit"
                        elif "message_reactions" in messaging_event:
                            event_type = "message_reactions"
                        elif "standby" in messaging_event:
                            event_type = "standby"
                        else:
                            event_type = "unknown"
                            # Log unknown event structure for debugging
                            log_print(f"⚠️ Unknown messaging event type. Event keys: {list(messaging_event.keys())}", "WARNING")
                        log_print(f"⏭️ Skipping {event_type} event (not a regular message)")
                
                # Process changes (comments, live comments, etc.)
                for change in entry.get("changes", []):
                    field = change.get("field")
                    
                    if field == "comments":
                        # Regular post/reel comments
                        await process_comment_event(change, entry.get("id"), db)
                    elif field == "live_comments":
                        # Live video comments
                        await process_live_comment_event(change, entry.get("id"), db)
        
        return {"status": "success"}
    except Exception as e:
        log_print(f"❌ Webhook error: {str(e)}", "ERROR")
        import traceback
        traceback.print_exc(file=sys.stderr)
        # Always return 200 to Meta to prevent retries
        return {"status": "error", "message": str(e)}

async def process_instagram_message(event: dict, db: Session):
    """Process incoming Instagram message and trigger automation rules."""
    try:
        # Validate that this is a regular message event (not message_edit, etc.)
        if "message" not in event:
            log_print(f"⚠️ Skipping event - no 'message' field found. Event keys: {list(event.keys())}", "WARNING")
            return
        
        sender_id = str(event.get("sender", {}).get("id"))  # Ensure string for state key consistency
        recipient_id = event.get("recipient", {}).get("id")
        message = event.get("message", {})
        message_text = message.get("text", "")
        message_id = message.get("mid")  # Message ID for deduplication
        
        # Extract Instagram's timestamp from webhook event (matches Instagram DM timing exactly)
        # Instagram webhook timestamp can be in milliseconds or seconds (Unix timestamp in UTC)
        # Instagram displays times in UTC+8, so we add 8 hours to match Instagram's display
        instagram_timestamp = None
        if "timestamp" in event:
            try:
                timestamp_value = event.get("timestamp", 0)
                # Try to determine if it's milliseconds (> year 2100 in seconds) or seconds
                # Timestamps > 4102444800 (Jan 1, 2100) are likely milliseconds
                timestamp_int = int(timestamp_value)
                if timestamp_int > 4102444800:
                    # Likely milliseconds, convert to seconds and use UTC
                    instagram_timestamp = datetime.utcfromtimestamp(timestamp_int / 1000.0)
                else:
                    # Likely seconds, use UTC
                    instagram_timestamp = datetime.utcfromtimestamp(float(timestamp_int))
                
                # Add 8 hours to match Instagram's display timezone (UTC+8)
                instagram_timestamp = instagram_timestamp + timedelta(hours=8)
                log_print(f"📅 Using Instagram webhook timestamp (UTC+8): {instagram_timestamp.isoformat()}")
            except (ValueError, TypeError, OSError) as ts_err:
                log_print(f"⚠️ Failed to parse Instagram timestamp: {str(ts_err)}, using current time")
                instagram_timestamp = None
        
        # Check if message has attachments (images, videos, etc.)
        attachments = message.get("attachments", [])
        if attachments and not message_text:
            log_print(f"🚫 [STRICT MODE] Ignoring message with only attachments (no text) - mid: {message_id}")
            return
        
        # STRICT MODE: If message has attachments WITH text, still ignore in strict mode flow
        # User should only send text for follow confirmations and emails
        if attachments:
            log_print(f"🚫 [STRICT MODE] Message has attachments - will check if waiting for follow/email before ignoring")
            # Continue processing to check if we're in strict mode flow
        
        # Check for echo messages (messages sent by the bot itself) FIRST before processing
        is_echo = message.get("is_echo", False) or event.get("is_echo", False)
        if is_echo:
            log_print(f"🚫 Ignoring bot's own message (echo flag)")
            if message_id:
                _processed_message_ids.add(message_id)
                # Clean cache if it gets too large
                if len(_processed_message_ids) > _MAX_CACHE_SIZE:
                    _processed_message_ids.clear()
            return

        # DEDUPLICATION: Skip if we've already processed this message_id (Meta can retry events)
        if message_id:
            if message_id in _processed_message_ids:
                log_print(f"⏭️ Skipping duplicate message event (mid={message_id}) - already processed")
                return
            _processed_message_ids.add(message_id)
            # Clean cache if it gets too large
            if len(_processed_message_ids) > _MAX_CACHE_SIZE:
                _processed_message_ids.clear()
        
        # Find account using Smart Fallback (same as comment webhook logic)
        # This must happen BEFORE checking pre-DM actions or triggering rules
        from app.models.instagram_account import InstagramAccount
        log_print(f"🔍 [DM] Looking for Instagram account (IGSID: {recipient_id})")
        
        # First try to match by IGSID (most accurate)
        account = db.query(InstagramAccount).filter(
            InstagramAccount.igsid == str(recipient_id),
            InstagramAccount.is_active == True
        ).first()
        
        if account:
            log_print(f"✅ [DM] Found account by IGSID: {account.username} (ID: {account.id}, User ID: {account.user_id})")
        else:
            # Smart Fallback: If IGSID not stored, find account that has rules for this trigger
            log_print(f"⚠️ [DM] No account found by IGSID, trying smart fallback matching...", "WARNING")
            from app.models.automation_rule import AutomationRule
            
            # Find account that has active rules for DM triggers (new_message or keyword)
            accounts_with_rules = db.query(InstagramAccount).join(AutomationRule).filter(
                InstagramAccount.is_active == True,
                AutomationRule.trigger_type.in_(["new_message", "keyword"]),
                AutomationRule.is_active == True
            ).all()
            
            if accounts_with_rules:
                account = accounts_with_rules[0]
                log_print(f"✅ [DM] Found account with matching rules: {account.username} (ID: {account.id})")
                log_print(f"   NOTE: Re-connect via OAuth to store IGSID ({recipient_id}) for accurate matching")
            else:
                # Last resort: use first active account
                account = db.query(InstagramAccount).filter(
                    InstagramAccount.is_active == True
                ).first()
                if account:
                    log_print(f"⚠️ [DM] Using first active account: {account.username} (ID: {account.id})")
                    log_print(f"   NOTE: Re-connect Instagram account via OAuth to store IGSID ({recipient_id})")
        
        if not account:
            log_print(f"❌ [DM] No active Instagram accounts found", "ERROR")
            return
        
        # Store incoming message in Message table (for Messages UI)
        try:
            from app.models.message import Message
            # Try to get sender username from event if available
            sender_username = None
            if "sender" in event:
                sender_username = event["sender"].get("username")
            
            # Get recipient username (our account)
            recipient_username = account.username
            
            # Check if message already exists (deduplication)
            existing_message = None
            if message_id:
                existing_message = db.query(Message).filter(
                    Message.message_id == message_id
                ).first()
            
            if not existing_message:
                # PREVENT SELF-CONVERSATIONS: Skip if sender matches account's own IGSID or username
                account_igsid = account.igsid
                account_username = account.username
                
                if account_igsid and sender_id == account_igsid:
                    log_print(f"🚫 Ignoring self-message (sender_id={sender_id} matches account IGSID)")
                    return
                if account_username and sender_username == account_username:
                    log_print(f"🚫 Ignoring self-message (sender_username={sender_username} matches account username)")
                    return
                
                # Get or create conversation for this participant
                conversation = get_or_create_conversation(
                    db=db,
                    user_id=account.user_id,
                    account_id=account.id,
                    participant_id=sender_id,
                    participant_name=sender_username
                )
                
                # Update conversation's last_message and updated_at
                message_preview = message_text or "[Media]"
                if len(message_preview) > 100:
                    message_preview = message_preview[:100] + "..."
                conversation.last_message = message_preview
                conversation.updated_at = datetime.utcnow()
                
                # Use Instagram's timestamp from webhook (matches Instagram DM timing exactly)
                # Instagram timestamps are already adjusted to UTC+8 in the extraction above
                # For fallback, add 8 hours to UTC time to match Instagram's display
                if instagram_timestamp:
                    message_timestamp = instagram_timestamp
                    log_print(f"✅ Using Instagram webhook timestamp (UTC+8) for incoming message: {message_timestamp.isoformat()}")
                else:
                    # Fallback: add 8 hours to UTC to match Instagram's display timezone
                    message_timestamp = datetime.utcnow() + timedelta(hours=8)
                    log_print(f"⚠️ Instagram timestamp not available, using current time (UTC+8): {message_timestamp.isoformat()}")
                
                incoming_message = Message(
                    user_id=account.user_id,
                    instagram_account_id=account.id,
                    conversation_id=conversation.id,
                    sender_id=sender_id,
                    sender_username=sender_username,
                    recipient_id=str(recipient_id),
                    recipient_username=recipient_username,
                    message_text=message_text,
                    content=message_text,  # Also set content field
                    message_id=message_id,
                    platform_message_id=message_id,  # Also set platform_message_id
                    is_from_bot=False,  # This is an incoming message
                    has_attachments=len(attachments) > 0,
                    attachments=attachments if attachments else None,
                    created_at=message_timestamp  # Use Instagram's timestamp for exact match
                )
                db.add(incoming_message)
                db.commit()
                log_print(f"💾 Stored incoming message from {sender_username or sender_id} (conversation_id: {conversation.id})")
        except Exception as store_err:
            log_print(f"⚠️ Failed to store incoming message: {str(store_err)}")
            db.rollback()
            # Don't fail the whole process if message storage fails
        
        # Now handle quick reply buttons if needed (account is now available)
        if "message" in event and "quick_reply" in event.get("message", {}):
            quick_reply_payload = event["message"]["quick_reply"].get("payload", "")
            
            # 1) Handle "Skip for Now" email skip button
            if quick_reply_payload == "email_skip":
                # v2: skip_for_now_no_final_dm = true → no Final DM (no email = no doc to share). BAU: false → send Final DM.
                from app.models.automation_rule import AutomationRule
                rules = db.query(AutomationRule).filter(
                    AutomationRule.instagram_account_id == account.id,
                    AutomationRule.is_active == True,
                    AutomationRule.action_type == "send_dm"
                ).all()
                ack_sent = False
                for rule in rules:
                    if rule.config.get("ask_for_email", False):
                        from app.services.pre_dm_handler import update_pre_dm_state
                        skip_no_final_dm = rule.config.get("skip_for_now_no_final_dm", True) or rule.config.get("skipForNowNoFinalDm", True)
                        update_pre_dm_state(sender_id, rule.id, {
                            "email_skipped": True,
                            "email_request_sent": True,
                            "email_received": False,
                        })
                        if skip_no_final_dm:
                            log_print(f"⏭️ [v2] User clicked 'Skip for Now' — no Final DM sent (comment again to re-engage)")
                            if not ack_sent:
                                try:
                                    from app.utils.encryption import decrypt_credentials
                                    from app.utils.instagram_api import send_dm as send_dm_api
                                    if account.encrypted_page_token:
                                        access_token = decrypt_credentials(account.encrypted_page_token)
                                    elif account.encrypted_credentials:
                                        access_token = decrypt_credentials(account.encrypted_credentials)
                                    else:
                                        access_token = None
                                    if access_token:
                                        ack = "No problem! Comment again anytime when you'd like the guide. 📩"
                                        send_dm_api(sender_id, ack, access_token, account.page_id, buttons=None, quick_replies=None)
                                        log_print(f"✅ [v2] Sent Skip acknowledgment to {sender_id}")
                                        ack_sent = True
                                except Exception as e:
                                    log_print(f"⚠️ Failed to send Skip ack: {str(e)}", "WARNING")
                        else:
                            log_print(f"⏭️ User clicked 'Skip for Now', proceeding to primary DM for {sender_id}")
                            asyncio.create_task(execute_automation_action(
                                rule, sender_id, account, db,
                                trigger_type="email_skip",
                                message_id=message_id,
                                pre_dm_result_override={"action": "send_primary"}
                            ))
                return
            
            # 1.5) Handle "Share Email" quick reply button
            if quick_reply_payload == "email_shared":
                # User clicked "Share Email" - just mark state to wait for email input
                # Don't send email question again - it was already sent with the quick_replies buttons
                log_print(f"📧 User clicked 'Share Email', waiting for email input for {sender_id}")
                from app.models.automation_rule import AutomationRule
                from app.services.pre_dm_handler import update_pre_dm_state
                
                # Find active rules that have email enabled
                rules = db.query(AutomationRule).filter(
                    AutomationRule.instagram_account_id == account.id,
                    AutomationRule.is_active == True,
                    AutomationRule.action_type == "send_dm"
                ).all()
                
                for rule in rules:
                    if rule.config.get("ask_for_email", False):
                        # Mark email request as sent and set state to wait for email
                        # Don't send any message - the email question was already sent with quick_replies
                        update_pre_dm_state(sender_id, rule.id, {
                            "email_request_sent": True,
                            "step": "email",
                            "waiting_for_email": True
                        })
                        log_print(f"✅ State updated - waiting for email input from {sender_id} for rule {rule.id}")
                
                # Don't process further - wait for user to type their email
                return  # Exit early, don't process as regular message
            
            # 1.6) Handle "Use My Email" quick reply button (user's logged-in email)
            if quick_reply_payload.startswith("email_use_"):
                # User clicked their email button - auto-submit that email
                user_email = quick_reply_payload.replace("email_use_", "")
                log_print(f"📧 User clicked their email button, auto-submitting: {user_email}")
                from app.models.automation_rule import AutomationRule
                from app.services.pre_dm_handler import (
                    update_pre_dm_state,
                    check_if_email_response,  # kept for backward compatibility / future use
                    get_pre_dm_state,
                )
                from app.services.lead_capture import validate_email, update_automation_stats
                
                # Validate the email
                is_valid, _ = validate_email(user_email)
                if not is_valid:
                    log_print(f"⚠️ Invalid email from quick reply button: {user_email}", "WARNING")
                    return
                
                # Find active rules that have email enabled
                rules = db.query(AutomationRule).filter(
                    AutomationRule.instagram_account_id == account.id,
                    AutomationRule.is_active == True,
                    AutomationRule.action_type == "send_dm"
                ).all()

                # Process only the rule that is currently waiting for this sender's email
                processed_rule = False
                for rule in rules:
                    if not rule.config.get("ask_for_email", False):
                        continue

                    # Only handle rules where this sender is actually in the email step
                    state = get_pre_dm_state(str(sender_id), rule.id)
                    if not state.get("email_request_sent") or state.get("email_received"):
                        continue

                    # Mark email as received and proceed to primary DM
                    update_pre_dm_state(sender_id, rule.id, {
                        "email_received": True,
                        "email": user_email,
                        "email_request_sent": True
                    })
                    log_print(f"✅ Email auto-submitted from quick reply: {user_email} for rule {rule.id}")

                    # Save email to leads database
                    try:
                        from app.models.captured_lead import CapturedLead
                        from sqlalchemy import and_

                        # Check if lead already exists
                        existing_lead = db.query(CapturedLead).filter(
                            and_(
                                CapturedLead.instagram_account_id == account.id,
                                CapturedLead.automation_rule_id == rule.id,
                                CapturedLead.email == user_email
                            )
                        ).first()

                        if not existing_lead:
                            # Mirror the structure used in pre_dm_handler so leads behave consistently
                            captured_lead = CapturedLead(
                                user_id=account.user_id,
                                instagram_account_id=account.id,
                                automation_rule_id=rule.id,
                                email=user_email,
                                extra_metadata={
                                    "sender_id": str(sender_id),
                                    "captured_via": "quick_reply_button",
                                    "timestamp": datetime.utcnow().isoformat(),
                                },
                            )
                            db.add(captured_lead)
                            db.commit()
                            db.refresh(captured_lead)
                            log_print(f"✅ Saved email to leads database: {user_email} (lead_id={captured_lead.id})")
                        else:
                            log_print(f"ℹ️ Lead already exists for email: {user_email}")
                    except Exception as save_err:
                        log_print(f"⚠️ Failed to save email to leads: {str(save_err)}", "WARNING")
                        db.rollback()

                    # Update global audience + automation stats + analytics so analytics screen matches text‑email flow
                    try:
                        # Mark email on global audience so VIP detection works immediately
                        try:
                            from app.services.global_conversion_check import update_audience_email
                            update_audience_email(db, str(sender_id), account.id, account.user_id, user_email)
                            log_print(f"✅ Updated global audience email for sender {sender_id}: {user_email}")
                        except Exception as audience_err:
                            log_print(f"⚠️ Failed to update global audience with email for quick reply: {str(audience_err)}", "WARNING")

                        # Increment lead_captured counter
                        update_automation_stats(rule.id, "lead_captured", db)

                        # Log EMAIL_COLLECTED analytics event
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
                                    "sender_id": str(sender_id),
                                    "email": user_email,
                                    "captured_via": "quick_reply_button",
                                },
                            )
                            log_print(f"✅ Logged EMAIL_COLLECTED analytics event for quick reply email: {user_email}")
                        except Exception as analytics_err:
                            log_print(f"⚠️ Failed to log EMAIL_COLLECTED analytics event: {str(analytics_err)}", "WARNING")
                    except Exception as stats_err:
                        log_print(f"⚠️ Failed to update automation stats for quick reply email: {str(stats_err)}", "WARNING")

                    # Proceed to primary DM (single rule only)
                    asyncio.create_task(execute_automation_action(
                        rule, sender_id, account, db,
                        trigger_type="postback",
                        message_id=message_id,
                        pre_dm_result_override={
                            "action": "send_primary",
                            "email": user_email,
                            "send_email_success": True
                        }
                    ))

                    processed_rule = True
                    break

                if not processed_rule:
                    log_print(f"ℹ️ No matching rule found waiting for email for quick reply payload, nothing to execute")

                return  # Exit early, don't process as regular message
            
            # 1.5) Handle "Are you following me?" Yes/No quick reply buttons
            if quick_reply_payload.startswith("follow_recheck_yes_") or quick_reply_payload.startswith("follow_recheck_no_"):
                is_yes = quick_reply_payload.startswith("follow_recheck_yes_")
                rule_id_from_payload = None
                try:
                    if is_yes:
                        rule_id_from_payload = int(quick_reply_payload.split("follow_recheck_yes_")[1])
                    else:
                        rule_id_from_payload = int(quick_reply_payload.split("follow_recheck_no_")[1])
                except (ValueError, IndexError):
                    log_print(f"⚠️ Could not parse rule_id from payload: {quick_reply_payload}", "WARNING")
                
                from app.models.automation_rule import AutomationRule
                from app.services.pre_dm_handler import update_pre_dm_state, get_pre_dm_state, normalize_follow_recheck_message
                from app.services.lead_capture import update_automation_stats
                
                # Find the specific rule
                if rule_id_from_payload:
                    rules = db.query(AutomationRule).filter(
                        AutomationRule.id == rule_id_from_payload,
                        AutomationRule.instagram_account_id == account.id,
                        AutomationRule.is_active == True
                    ).all()
                else:
                    rules = db.query(AutomationRule).filter(
                        AutomationRule.instagram_account_id == account.id,
                        AutomationRule.is_active == True
                    ).all()
                
                for rule in rules:
                    ask_to_follow = rule.config.get("ask_to_follow", False) if rule.config else False
                    ask_for_email = rule.config.get("ask_for_email", False) if rule.config else False
                    
                    if not ask_to_follow:
                        continue
                    
                    state = get_pre_dm_state(str(sender_id), rule.id)
                    
                    if is_yes:
                        # User confirmed they're following
                        log_print(f"✅ User clicked 'Yes' on 'Are you following me?' for rule {rule.id}")
                        update_pre_dm_state(str(sender_id), rule.id, {
                            "follow_confirmed": True,
                            "follow_recheck_sent": False
                        })
                        
                        # Track follower gain count
                        try:
                            update_automation_stats(rule.id, "follower_gained", db)
                        except Exception:
                            pass
                        
                        # Log analytics
                        try:
                            from app.utils.analytics import log_analytics_event_sync
                            from app.models.analytics_event import EventType
                            media_id = rule.config.get("media_id") if hasattr(rule, 'config') else None
                            log_analytics_event_sync(
                                db=db,
                                user_id=account.user_id,
                                event_type=EventType.IM_FOLLOWING_CLICKED,
                                rule_id=rule.id,
                                media_id=media_id,
                                instagram_account_id=account.id,
                                metadata={
                                    "sender_id": sender_id,
                                    "source": "follow_recheck_yes_button",
                                    "clicked_at": datetime.utcnow().isoformat()
                                }
                            )
                        except Exception:
                            pass
                        
                        # Update global audience
                        try:
                            from app.services.global_conversion_check import update_audience_following
                            update_audience_following(db, str(sender_id), account.id, account.user_id, is_following=True)
                        except Exception:
                            pass
                        
                        # If email request is enabled, send email question
                        if ask_for_email:
                            ask_for_email_message = rule.config.get(
                                "ask_for_email_message",
                                "Quick question - what's your email? I'd love to send you something special! 📧"
                            )
                            from app.utils.encryption import decrypt_credentials
                            from app.utils.instagram_api import send_dm
                            try:
                                if account.encrypted_page_token:
                                    access_token = decrypt_credentials(account.encrypted_page_token)
                                elif account.encrypted_credentials:
                                    access_token = decrypt_credentials(account.encrypted_credentials)
                                else:
                                    raise Exception("No access token found")
                                
                                page_id_for_dm = account.page_id
                                send_dm(sender_id, ask_for_email_message, access_token, page_id_for_dm, buttons=None, quick_replies=None)
                                update_pre_dm_state(str(sender_id), rule.id, {
                                    "email_request_sent": True,
                                    "step": "email"
                                })
                                log_print(f"✅ Email request sent after 'Are you following me?' Yes confirmation")
                            except Exception as e:
                                log_print(f"❌ Failed to send email request: {str(e)}", "ERROR")
                        else:
                            # No email request, proceed directly to primary DM
                            log_print(f"✅ Follow confirmed via 'Are you following me?' Yes, proceeding to primary DM")
                            asyncio.create_task(execute_automation_action(
                                rule, sender_id, account, db,
                                trigger_type="postback",
                                message_id=message_id,
                                pre_dm_result_override={"action": "send_primary"}
                            ))
                    else:
                        # User said No — send exit message. Use comment vs story reply text based on how they entered (stored when we sent "Are you following me?")
                        _trigger = (state or {}).get("follow_recheck_trigger_type") or "post_comment"
                        _default_exit = "No problem! Story reply again anytime when you'd like the guide. 📩" if _trigger == "story_reply" else "No problem! Comment again anytime when you'd like the guide. 📩"
                        exit_msg = (rule.config or {}).get("follow_no_exit_message") or (rule.config or {}).get("followNoExitMessage") or _default_exit
                        update_pre_dm_state(str(sender_id), rule.id, {
                            "follow_recheck_sent": False,
                            "follow_exit_sent": True,
                            "follow_request_sent": True,
                        })
                        from app.utils.encryption import decrypt_credentials
                        from app.utils.instagram_api import send_dm
                        try:
                            if account.encrypted_page_token:
                                access_token = decrypt_credentials(account.encrypted_page_token)
                            elif account.encrypted_credentials:
                                access_token = decrypt_credentials(account.encrypted_credentials)
                            else:
                                raise Exception("No access token found")
                            send_dm(sender_id, exit_msg, access_token, account.page_id, buttons=None, quick_replies=None)
                            log_print(f"📩 User clicked No to 'Are you following me?' — sent exit message (no initial message resend)")
                        except Exception as e:
                            log_print(f"❌ Failed to send exit message: {str(e)}", "ERROR")
                    
                    # Only process the first matching rule
                    return
            
            # 2) Handle "I'm following" quick reply button (payload: im_following_{rule_id})
            if quick_reply_payload.startswith("im_following_"):
                log_print(f"✅ [STRICT MODE] User clicked 'I'm following' button! Payload={quick_reply_payload}")
                
                # Extract rule_id from payload
                rule_id_from_payload = None
                try:
                    rule_id_from_payload = int(quick_reply_payload.split("im_following_")[1])
                except (ValueError, IndexError):
                    log_print(f"⚠️ Could not parse rule_id from payload: {quick_reply_payload}", "WARNING")
                
                from app.models.automation_rule import AutomationRule
                from app.services.pre_dm_handler import update_pre_dm_state
                from app.services.lead_capture import update_automation_stats
                
                # Find the specific rule
                if rule_id_from_payload:
                    rules = db.query(AutomationRule).filter(
                        AutomationRule.id == rule_id_from_payload,
                        AutomationRule.instagram_account_id == account.id,
                        AutomationRule.is_active == True
                    ).all()
                else:
                    rules = db.query(AutomationRule).filter(
                        AutomationRule.instagram_account_id == account.id,
                        AutomationRule.is_active == True
                    ).all()
                
                for rule in rules:
                    ask_to_follow = rule.config.get("ask_to_follow", False)
                    ask_for_email = rule.config.get("ask_for_email", False)
                    
                    if not (ask_to_follow or ask_for_email):
                        continue
                    
                    # Mark follow as confirmed (user says they're already following)
                    update_pre_dm_state(str(sender_id), rule.id, {
                        "follow_confirmed": True,
                        "im_following_clicked": True,
                        "follow_request_sent": True  # Mark as sent to prevent re-asking
                    })
                    log_print(f"✅ Marked 'I'm following' confirmation for rule {rule.id}")
                    
                    # Update global audience record with following status (for VIP check across all automations)
                    try:
                        from app.services.global_conversion_check import update_audience_following
                        update_audience_following(db, str(sender_id), account.id, account.user_id, is_following=True)
                        log_print(f"✅ Follow status updated in global audience for {sender_id}")
                    except Exception as audience_err:
                        log_print(f"⚠️ Failed to update global audience with follow status: {str(audience_err)}", "WARNING")
                    
                    # Track analytics: "I'm following" button click
                    from app.services.lead_capture import update_automation_stats
                    update_automation_stats(rule.id, "im_following_clicked", db)
                    
                    # Log analytics event for "I'm following" button click
                    try:
                        from app.utils.analytics import log_analytics_event_sync
                        from app.models.analytics_event import EventType
                        media_id = rule.config.get("media_id") if hasattr(rule, 'config') else None
                        log_analytics_event_sync(
                            db=db,
                            user_id=account.user_id,
                            event_type=EventType.IM_FOLLOWING_CLICKED,
                            rule_id=rule.id,
                            media_id=media_id,
                            instagram_account_id=account.id,
                            metadata={
                                "sender_id": sender_id,
                                "source": "im_following_button_click",
                                "clicked_at": datetime.utcnow().isoformat()
                            }
                        )
                        log_print(f"✅ Logged IM_FOLLOWING_CLICKED analytics event for rule {rule.id}")
                    except Exception as analytics_err:
                        log_print(f"⚠️ Failed to log IM_FOLLOWING_CLICKED event: {str(analytics_err)}", "WARNING")
                    
                    # If email is enabled, send email question immediately
                    if ask_for_email:
                        ask_for_email_message = rule.config.get(
                            "ask_for_email_message",
                            "Quick question - what's your email? I'd love to send you something special! 📧"
                        )
                        
                        log_print(f"📧 [STRICT MODE] Sending email request immediately after 'I'm following' click")
                        from app.utils.encryption import decrypt_credentials
                        from app.utils.instagram_api import send_dm
                        
                        try:
                            if account.encrypted_page_token:
                                access_token = decrypt_credentials(account.encrypted_page_token)
                            elif account.encrypted_credentials:
                                access_token = decrypt_credentials(account.encrypted_credentials)
                            else:
                                raise Exception("No access token found")
                            
                            page_id_for_dm = account.page_id
                            
                            # Create Quick Reply buttons for email collection
                            quick_replies = [
                                {
                                    "content_type": "text",
                                    "title": "Share Email",
                                    "payload": "email_shared"
                                },
                                {
                                    "content_type": "text",
                                    "title": "Skip for Now",
                                    "payload": "email_skip"
                                }
                            ]
                            
                            # IMPROVEMENT: Add user's email as quick reply button if available
                            try:
                                from app.models.user import User
                                user = db.query(User).filter(User.id == account.user_id).first()
                                if user and user.email:
                                    email_display = user.email
                                    if len(email_display) > 20:
                                        email_parts = email_display.split('@')
                                        if len(email_parts) > 0:
                                            username = email_parts[0]
                                            if len(username) <= 15:
                                                email_display = f"{username}@{email_parts[1][:15-len(username)]}..."
                                            else:
                                                email_display = f"{username[:17]}..."
                                        else:
                                            email_display = email_display[:17] + "..."
                                    
                                    quick_replies.insert(0, {
                                        "content_type": "text",
                                        "title": email_display,
                                        "payload": f"email_use_{user.email}"
                                    })
                                    print(f"✅ Added user's email ({user.email}) as quick reply button")
                            except Exception as email_err:
                                print(f"⚠️ Could not add user email to quick replies: {str(email_err)}")
                            
                            # Send email request with quick reply buttons
                            send_dm(sender_id, ask_for_email_message, access_token, page_id_for_dm, buttons=None, quick_replies=quick_replies)
                            log_print(f"✅ Email request sent after 'I'm following' button click with quick replies")
                            
                            # Log DM sent (tracks in DmLog and increments global tracker)
                            try:
                                from app.utils.plan_enforcement import log_dm_sent
                                log_dm_sent(
                                    user_id=account.user_id,
                                    instagram_account_id=account.id,
                                    recipient_username=str(sender_id),
                                    message=ask_for_email_message,
                                    db=db,
                                    instagram_username=account.username,
                                    instagram_igsid=getattr(account, "igsid", None)
                                )
                            except Exception as log_err:
                                log_print(f"⚠️ Failed to log DM: {str(log_err)}", "WARNING")
                            
                            # Update state to mark that we're now waiting for email
                            update_pre_dm_state(str(sender_id), rule.id, {
                                "email_request_sent": True,
                                "step": "email",
                                "waiting_for_email_text": True
                            })
                        except Exception as e:
                            log_print(f"❌ Failed to send email request after 'I'm following' click: {str(e)}", "ERROR")
                    
                    # If no email configured, proceed directly to primary DM
                    else:
                        log_print(f"✅ Follow confirmed via 'I'm following' button, proceeding directly to primary DM")
                        asyncio.create_task(execute_automation_action(
                            rule, sender_id, account, db,
                            trigger_type="postback",
                            message_id=message_id,
                            pre_dm_result_override={"action": "send_primary"}
                        ))
                    
                    # Only process the first matching rule
                    return
            
            # 3) Handle "Visit Profile" quick reply button (payload: visit_profile_{rule_id})
            if quick_reply_payload.startswith("visit_profile_"):
                log_print(f"🔗 [STRICT MODE] User clicked 'Visit Profile' button! Payload={quick_reply_payload}")
                
                # Extract rule_id from payload
                rule_id_from_payload = None
                try:
                    rule_id_from_payload = int(quick_reply_payload.split("visit_profile_")[1])
                except (ValueError, IndexError):
                    log_print(f"⚠️ Could not parse rule_id from payload: {quick_reply_payload}", "WARNING")
                
                from app.models.automation_rule import AutomationRule
                from app.services.pre_dm_handler import update_pre_dm_state
                
                # Find the specific rule
                if rule_id_from_payload:
                    rules = db.query(AutomationRule).filter(
                        AutomationRule.id == rule_id_from_payload,
                        AutomationRule.instagram_account_id == account.id,
                        AutomationRule.is_active == True
                    ).all()
                else:
                    rules = db.query(AutomationRule).filter(
                        AutomationRule.instagram_account_id == account.id,
                        AutomationRule.is_active == True
                    ).all()
                
                for rule in rules:
                    ask_to_follow = rule.config.get("ask_to_follow", False)
                    
                    if not ask_to_follow:
                        continue
                    
                    # Track profile visit (user clicked to visit profile)
                    update_pre_dm_state(str(sender_id), rule.id, {
                        "profile_visited": True,
                        "profile_visit_time": str(asyncio.get_event_loop().time())
                    })
                    log_print(f"✅ Tracked profile visit for rule {rule.id}")
                    
                    # Track analytics: Profile visit button click
                    from app.services.lead_capture import update_automation_stats
                    update_automation_stats(rule.id, "profile_visit", db)
                    
                    # Send a simple reminder message WITHOUT URL to avoid link preview card
                    # (The original follow request message already contains the profile URL)
                    reminder_message = "Great! Once you've followed, click 'I'm following' or type 'done' to continue! 😊"
                    
                    from app.utils.encryption import decrypt_credentials
                    from app.utils.instagram_api import send_dm
                    
                    try:
                        if account.encrypted_page_token:
                            access_token = decrypt_credentials(account.encrypted_page_token)
                        elif account.encrypted_credentials:
                            access_token = decrypt_credentials(account.encrypted_credentials)
                        else:
                            raise Exception("No access token found")
                        
                        page_id_for_dm = account.page_id
                        
                        # Send reminder with profile URL
                        send_dm(sender_id, reminder_message, access_token, page_id_for_dm, buttons=None, quick_replies=None)
                        log_print(f"✅ Profile visit reminder sent")
                        
                        # Log DM sent (tracks in DmLog and increments global tracker)
                        try:
                            from app.utils.plan_enforcement import log_dm_sent
                            log_dm_sent(
                                user_id=account.user_id,
                                instagram_account_id=account.id,
                                recipient_username=str(sender_id),
                                message=reminder_message,
                                db=db,
                                instagram_username=account.username,
                                instagram_igsid=getattr(account, "igsid", None)
                            )
                        except Exception as log_err:
                            log_print(f"⚠️ Failed to log DM: {str(log_err)}", "WARNING")
                    except Exception as e:
                        log_print(f"❌ Failed to send profile visit reminder: {str(e)}", "ERROR")
                    
                    # Only process the first matching rule
                    return
            
            # 4) Handle "Follow Me" quick reply button (payload: follow_me_{rule_id})
            if quick_reply_payload.startswith("follow_me_"):
                log_print(f"👥 [STRICT MODE] User clicked 'Follow Me' quick reply button! Payload={quick_reply_payload}")
                
                # Extract rule_id from payload
                rule_id_from_payload = None
                try:
                    rule_id_from_payload = int(quick_reply_payload.split("follow_me_")[1])
                except (ValueError, IndexError):
                    log_print(f"⚠️ Could not parse rule_id from payload: {quick_reply_payload}", "WARNING")
                
                from app.models.automation_rule import AutomationRule
                from app.services.pre_dm_handler import update_pre_dm_state
                from app.services.lead_capture import update_automation_stats
                
                # Find the specific rule (if rule_id provided), otherwise fall back to active rules
                if rule_id_from_payload:
                    rules = db.query(AutomationRule).filter(
                        AutomationRule.id == rule_id_from_payload,
                        AutomationRule.instagram_account_id == account.id,
                        AutomationRule.is_active == True
                    ).all()
                else:
                    rules = db.query(AutomationRule).filter(
                        AutomationRule.instagram_account_id == account.id,
                        AutomationRule.is_active == True
                    ).all()
                
                for rule in rules:
                    ask_to_follow = rule.config.get("ask_to_follow", False)
                    ask_for_email = rule.config.get("ask_for_email", False)
                    
                    if not (ask_to_follow or ask_for_email):
                        continue
                    
                    # Track button click in stats for this rule
                    try:
                        update_automation_stats(rule.id, "follow_button_clicked", db)
                    except Exception as e:
                        log_print(f"⚠️ Failed to update follow_button_clicked stats: {str(e)}", "WARNING")
                    
                    # Log analytics event for "Follow Me" button click
                    try:
                        from app.utils.analytics import log_analytics_event_sync
                        from app.models.analytics_event import EventType
                        media_id = rule.config.get("media_id") if hasattr(rule, 'config') else None
                        log_analytics_event_sync(
                            db=db,
                            user_id=account.user_id,
                            event_type=EventType.FOLLOW_BUTTON_CLICKED,
                            rule_id=rule.id,
                            media_id=media_id,
                            instagram_account_id=account.id,
                            metadata={
                                "sender_id": sender_id,
                                "source": "follow_me_button_click",
                                "clicked_at": datetime.utcnow().isoformat()
                            }
                        )
                        log_print(f"✅ Logged FOLLOW_BUTTON_CLICKED analytics event for rule {rule.id}")
                    except Exception as analytics_err:
                        log_print(f"⚠️ Failed to log FOLLOW_BUTTON_CLICKED event: {str(analytics_err)}", "WARNING")
                    
                    # Followers-only: "Follow Me" → ask "Are you following me?" with Yes/No (no primary until Yes)
                    if not ask_for_email:
                        from app.services.pre_dm_handler import normalize_follow_recheck_message as _norm_recheck
                        raw = (rule.config or {}).get("follow_recheck_message") or (rule.config or {}).get("followRecheckMessage") or "Are you following me?"
                        follow_recheck_msg = _norm_recheck(raw)
                        # Store how they entered so "No" shows Comment again vs Story reply again (story from message.reply_to or existing state)
                        _story_id = (message or {}).get("reply_to", {}).get("story", {}).get("id")
                        _recheck_trigger = "story_reply" if _story_id else "post_comment"
                        if not _story_id:
                            from app.services.pre_dm_handler import get_pre_dm_state
                            _existing = get_pre_dm_state(str(sender_id), rule.id)
                            if _existing.get("follow_recheck_trigger_type") == "story_reply":
                                _recheck_trigger = "story_reply"  # Keep story context (e.g. quick reply payload may not include reply_to)
                        update_pre_dm_state(str(sender_id), rule.id, {"follow_recheck_sent": True, "follow_recheck_trigger_type": _recheck_trigger})
                        from app.utils.encryption import decrypt_credentials
                        from app.utils.instagram_api import send_dm as send_dm_api
                        try:
                            if account.encrypted_page_token:
                                _tok = decrypt_credentials(account.encrypted_page_token)
                            elif account.encrypted_credentials:
                                _tok = decrypt_credentials(account.encrypted_credentials)
                            else:
                                raise Exception("No access token found")
                            yes_no_quick_replies = [
                                {"content_type": "text", "title": "Yes", "payload": f"follow_recheck_yes_{rule.id}"},
                                {"content_type": "text", "title": "No", "payload": f"follow_recheck_no_{rule.id}"},
                            ]
                            send_dm_api(sender_id, follow_recheck_msg, _tok, account.page_id, buttons=None, quick_replies=yes_no_quick_replies)
                            log_print(f"📩 [FOLLOWERS] User clicked 'Follow Me' — sent 'Are you following me?' with Yes/No")
                            try:
                                from app.utils.plan_enforcement import log_dm_sent
                                log_dm_sent(user_id=account.user_id, instagram_account_id=account.id, recipient_username=str(sender_id), message=follow_recheck_msg, db=db, instagram_username=account.username, instagram_igsid=getattr(account, "igsid", None))
                            except Exception:
                                pass
                        except Exception as e:
                            log_print(f"❌ Failed to send follow recheck: {str(e)}", "ERROR")
                        return
                    
                    # Optional: require explicit follow confirmation after "Follow Me" (config: require_follow_confirmation).
                    # Default BAU: "Follow Me" = confirm and send email immediately (no blocking).
                    require_follow_confirmation = rule.config.get("require_follow_confirmation", False) or rule.config.get("requireFollowConfirmation", False)
                    
                    if require_follow_confirmation:
                        # New behavior: do NOT confirm until they click "I'm following" or type "done"
                        update_pre_dm_state(str(sender_id), rule.id, {
                            "follow_button_clicked": True,
                            "follow_request_sent": True,
                            "follow_confirmed": False,
                            "follow_button_clicked_time": str(asyncio.get_event_loop().time())
                        })
                        log_print(f"✅ Marked 'Follow Me' click for rule {rule.id} (waiting for confirmation)")
                        reminder_message = "Great! Once you've followed, click 'I'm following' or type 'done' to continue! 😊"
                        from app.utils.encryption import decrypt_credentials
                        from app.utils.instagram_api import send_dm as send_dm_api
                        try:
                            if account.encrypted_page_token:
                                access_token = decrypt_credentials(account.encrypted_page_token)
                            elif account.encrypted_credentials:
                                access_token = decrypt_credentials(account.encrypted_credentials)
                            else:
                                raise Exception("No access token found")
                            page_id_for_dm = account.page_id
                            follow_quick_reply = [
                                {"content_type": "text", "title": "I'm following", "payload": f"im_following_{rule.id}"},
                                {"content_type": "text", "title": "Follow Me 👆", "payload": f"follow_me_{rule.id}"}
                            ]
                            send_dm_api(sender_id, reminder_message, access_token, page_id_for_dm, buttons=None, quick_replies=follow_quick_reply)
                            log_print(f"✅ Sent follow confirmation reminder (require_follow_confirmation=True)")
                            try:
                                from app.utils.plan_enforcement import log_dm_sent
                                log_dm_sent(
                                    user_id=account.user_id,
                                    instagram_account_id=account.id,
                                    recipient_username=str(sender_id),
                                    message=reminder_message,
                                    db=db,
                                    instagram_username=account.username,
                                    instagram_igsid=getattr(account, "igsid", None)
                                )
                            except Exception as log_err:
                                log_print(f"⚠️ Failed to log DM: {str(log_err)}", "WARNING")
                        except Exception as e:
                            log_print(f"❌ Failed to send follow confirmation reminder: {str(e)}", "ERROR")
                        return
                    
                    # BAU: "Follow Me" = treat as confirmed, send email (or primary) immediately
                    update_pre_dm_state(str(sender_id), rule.id, {
                        "follow_button_clicked": True,
                        "follow_confirmed": True,
                        "follow_request_sent": True,
                        "follow_button_clicked_time": str(asyncio.get_event_loop().time())
                    })
                    log_print(f"✅ Marked follow button click + confirmation for rule {rule.id}")
                    try:
                        from app.services.global_conversion_check import update_audience_following
                        update_audience_following(db, str(sender_id), account.id, account.user_id, is_following=True)
                        log_print(f"✅ Follow status updated in global audience for {sender_id}")
                    except Exception as audience_err:
                        log_print(f"⚠️ Failed to update global audience with follow status: {str(audience_err)}", "WARNING")
                    
                    if ask_for_email:
                        ask_for_email_message = rule.config.get(
                            "ask_for_email_message",
                            "Quick question - what's your email? I'd love to send you something special! 📧"
                        )
                        log_print(f"📧 [STRICT MODE] Sending email request immediately after Follow Me click")
                        from app.utils.encryption import decrypt_credentials
                        from app.utils.instagram_api import send_dm
                        try:
                            if account.encrypted_page_token:
                                access_token = decrypt_credentials(account.encrypted_page_token)
                            elif account.encrypted_credentials:
                                access_token = decrypt_credentials(account.encrypted_credentials)
                            else:
                                raise Exception("No access token found")
                            page_id_for_dm = account.page_id
                            quick_replies = [
                                {"content_type": "text", "title": "Share Email", "payload": "email_shared"},
                                {"content_type": "text", "title": "Skip for Now", "payload": "email_skip"}
                            ]
                            try:
                                from app.models.user import User
                                user = db.query(User).filter(User.id == account.user_id).first()
                                if user and user.email:
                                    email_display = user.email
                                    if len(email_display) > 20:
                                        email_parts = email_display.split('@')
                                        if len(email_parts) > 0:
                                            username = email_parts[0]
                                            email_display = f"{username}@{email_parts[1][:15-len(username)]}..." if len(username) <= 15 else f"{username[:17]}..."
                                        else:
                                            email_display = email_display[:17] + "..."
                                    quick_replies.insert(0, {"content_type": "text", "title": email_display, "payload": f"email_use_{user.email}"})
                            except Exception:
                                pass
                            send_dm(sender_id, ask_for_email_message, access_token, page_id_for_dm, buttons=None, quick_replies=quick_replies)
                            log_print(f"✅ Email request sent after Follow Me button click with quick replies")
                            try:
                                from app.utils.plan_enforcement import log_dm_sent
                                log_dm_sent(
                                    user_id=account.user_id,
                                    instagram_account_id=account.id,
                                    recipient_username=str(sender_id),
                                    message=ask_for_email_message,
                                    db=db,
                                    instagram_username=account.username,
                                    instagram_igsid=getattr(account, "igsid", None)
                                )
                            except Exception as log_err:
                                log_print(f"⚠️ Failed to log DM: {str(log_err)}", "WARNING")
                            update_pre_dm_state(str(sender_id), rule.id, {
                                "email_request_sent": True,
                                "step": "email",
                                "waiting_for_email_text": True
                            })
                        except Exception as e:
                            log_print(f"❌ Failed to send email request after Follow Me click: {str(e)}", "ERROR")
                    else:
                        log_print(f"✅ Follow confirmed via Follow Me button, proceeding directly to primary DM")
                        asyncio.create_task(execute_automation_action(
                            rule, sender_id, account, db,
                            trigger_type="postback",
                            message_id=message_id,
                            pre_dm_result_override={"action": "send_primary"}
                        ))
                    return
        
        # Extract story_id early. For story replies we run pre_dm_rules but only for the
        # matching Story rule (filter inside the loop). This lets Stories use the same
        # state machine (done, email, retry) as Post/Reels without Post/Reel rules blocking.
        story_id = None
        if message.get("reply_to", {}).get("story", {}).get("id"):
            story_id = str(message.get("reply_to", {}).get("story", {}).get("id"))
            log_print(f"📖 Story reply detected (early) - Story ID: {story_id}")
        
        # GLOBAL CONVERSION CHECK: Check if user is already converted (VIP) before processing any rules
        # This ensures users who have provided email + are following skip growth steps across ALL automations
        from app.services.global_conversion_check import check_global_conversion_status
        conversion_status = check_global_conversion_status(
            db, sender_id, account.id, account.user_id, 
            username=event.get("sender", {}).get("username")
        )
        is_vip_user = conversion_status["is_converted"]
        
        if is_vip_user:
            log_print(f"⭐ [VIP USER] User {sender_id} is already converted (email + phone + following). Skipping growth steps for all automations.")
            log_print(f"   Email: {conversion_status['has_email']}, Phone: {conversion_status.get('has_phone', False)}, Following: {conversion_status['is_following']}")
        
        # CRITICAL FIX: Check if sender is the bot itself BEFORE processing pre-DM actions
        # This prevents bot's own messages from breaking the flow when waiting for user responses
        # Bot messages should be ignored and flow should continue waiting for actual user responses
        sender_matches_bot = False
        sender_id_str = None
        if account and sender_id:
            sender_id_str = str(sender_id) if sender_id else None
            account_igsid_str = str(account.igsid) if account.igsid else None
            account_page_id_str = str(account.page_id) if account.page_id else None
            
            if sender_id_str and account_igsid_str and sender_id_str == account_igsid_str:
                sender_matches_bot = True
            elif sender_id_str and account_page_id_str and sender_id_str == account_page_id_str:
                sender_matches_bot = True
        
        if sender_matches_bot:
            log_print(f"🚫 [PRE-DM FIX] Ignoring bot's own message - sender_id={sender_id_str} matches bot account")
            log_print(f"   Bot IGSID: {account.igsid}, Bot Page ID: {account.page_id}")
            log_print(f"   This prevents bot messages from breaking pre-DM flow when waiting for user responses")
            log_print(f"   Flow will continue waiting for actual user messages (follow confirmation or email)")
            return
        
        # Check if this might be a response to a pre-DM follow/email request (now that account is found)
        # Do NOT skip for story_id. Instead we filter inside the loop so Stories use the same
        # state machine (done, email, retry) as Post/Reels, without Post/Reel rules blocking.
        # IMPORTANT: Check ALL rules with pre-DM actions, including those with media_id (for comment-based rules)
        if account and message_text:
            from app.models.automation_rule import AutomationRule
            from app.services.pre_dm_handler import process_pre_dm_actions, check_if_email_response, check_if_follow_confirmation
            
            # Find rules with pre-DM actions enabled (include ALL rules, not just new_message)
            # This allows comment-based rules with pre-DM actions to respond to DMs
            pre_dm_rules = db.query(AutomationRule).filter(
                AutomationRule.instagram_account_id == account.id,
                AutomationRule.is_active == True,
                AutomationRule.action_type == "send_dm"
            ).all()
            
            # CRITICAL FIX: Filter rules to prevent conflicting lead-capture types (email vs phone)
            # Only filter conflicts when rules share the SAME context (same media_id or both are general)
            # This allows Reel A (phone) and Reel B (email) to work independently
            # But prevents conflicts when multiple rules match the same trigger context
            
            # Group rules by media_id to detect conflicts within same context
            rules_by_media = {}  # media_id -> {email_rules: [], phone_rules: [], other_rules: []}
            
            for rule in pre_dm_rules:
                config = rule.config or {}
                media_id = str(config.get("media_id", "")) or "general"  # "general" for rules without media_id
                simple_dm_flow = config.get("simple_dm_flow", False) or config.get("simpleDmFlow", False)
                simple_dm_flow_phone = config.get("simple_dm_flow_phone", False) or config.get("simpleDmFlowPhone", False)
                
                if media_id not in rules_by_media:
                    rules_by_media[media_id] = {"email_rules": [], "phone_rules": [], "other_rules": []}
                
                if simple_dm_flow:
                    rules_by_media[media_id]["email_rules"].append(rule)
                elif simple_dm_flow_phone:
                    rules_by_media[media_id]["phone_rules"].append(rule)
                else:
                    rules_by_media[media_id]["other_rules"].append(rule)
            
            # Filter conflicts per media_id context
            filtered_rules = []
            for media_id, rule_groups in rules_by_media.items():
                email_rules = rule_groups["email_rules"]
                phone_rules = rule_groups["phone_rules"]
                other_rules = rule_groups["other_rules"]
                
                # If both email and phone rules exist for SAME media_id, prioritize email
                if email_rules and phone_rules:
                    log_print(f"⚠️ [FIX] Found conflicting lead-capture types for media_id={media_id}: {len(email_rules)} email rule(s) and {len(phone_rules)} phone rule(s). Prioritizing email flow and excluding phone rules.")
                    filtered_rules.extend(email_rules + other_rules)
                else:
                    # No conflict for this media_id, include all rules
                    filtered_rules.extend(email_rules + phone_rules + other_rules)
            
            pre_dm_rules = filtered_rules
            
            # CRITICAL: After primary DM is sent to this user, bot must not reply to any further *DMs* in this thread.
            # This applies only to incoming messaging events (DMs). Comments are handled by process_comment_event
            # and will still trigger primary DM when user comments again (email already collected / VIP).
            from app.services.pre_dm_handler import get_pre_dm_state
            for _r in pre_dm_rules:
                _state = get_pre_dm_state(sender_id, _r.id)
                if _state.get("primary_dm_sent"):
                    log_print(f"🚫 [FIX] Primary DM already sent to {sender_id} (rule {_r.id}). Skipping all automation — bot will not reply.")
                    return
            
            # CRITICAL FIX: Only process rules that have an active state for this sender
            # This prevents rules from different reels/posts from interfering with each other
            # A rule is "active" if it has started a conversation (follow_request_sent, email_request_sent, or phone_request_sent)
            active_rules = []
            for rule in pre_dm_rules:
                state = get_pre_dm_state(sender_id, rule.id)
                # Rule is active if it has started any part of the flow
                is_active = (
                    state.get("follow_request_sent", False) or
                    state.get("email_request_sent", False) or
                    state.get("phone_request_sent", False) or
                    state.get("primary_dm_sent", False)
                )
                
                # Also include rules with trigger_type="new_message" (they don't need comment trigger)
                # and rules with trigger_type="story_reply" if this is a story reply
                trigger_type = getattr(rule, "trigger_type", None)
                if trigger_type == "new_message":
                    is_active = True  # Always process new_message rules
                elif trigger_type == "story_reply" and story_id:
                    is_active = True  # Process story_reply rules for story replies
                
                if is_active:
                    active_rules.append(rule)
                    log_print(f"✅ Rule {rule.id} ({rule.name}) is active for sender {sender_id}")
                else:
                    log_print(f"⏭️ Rule {rule.id} ({rule.name}) is NOT active for sender {sender_id} (no conversation started), skipping")
            
            pre_dm_rules = active_rules
            
            # REMOVED: Global completion check - now checking per-rule in the loop below
            # This ensures each reel/post works independently (Reel A phone doesn't skip when Reel B email completed)
            
            # Track if we processed any rules to avoid duplicate retry messages
            processed_rules_count = 0
            sent_retry_message = False
            sent_email_request = False  # Track if we already sent email question (only send once for "done")
            sent_phone_request = False  # Same for phone simple flow
            processed_email = None  # Store processed email to use for all rules
            processed_phone = None  # Store processed phone for phone simple flow
            rules_waiting_for_email = []  # Collect all rules waiting for email/phone lead to send primary DM
            
            # VIP USER HANDLING: If user is already converted, send primary DM for ONLY ONE matching rule
            # IMPORTANT STRICT MODE: Only do this for Story replies (story_id present).
            # For plain DMs (no story_id), do NOT auto-send anything for VIP users – their
            # messages should be handled manually by the account owner.
            # EXCEPTION: If we're in an active flow waiting for phone or email, process the message so we can complete the flow and send primary DM.
            if is_vip_user:
                if story_id is None:
                    waiting_for_phone_or_email = False
                    for _r in pre_dm_rules:
                        _st = get_pre_dm_state(sender_id, _r.id)
                        if _st.get("phone_request_sent") and not _st.get("phone_received"):
                            waiting_for_phone_or_email = True
                            log_print(f"📱 [VIP] Rule {_r.id} waiting for phone — processing DM so we can complete flow and send primary DM")
                            break
                        if _st.get("email_request_sent") and not _st.get("email_received"):
                            waiting_for_phone_or_email = True
                            log_print(f"📧 [VIP] Rule {_r.id} waiting for email — processing DM so we can complete flow and send primary DM")
                            break
                    if not waiting_for_phone_or_email:
                        log_print(f"⭐ [VIP] User {sender_id} is converted and this DM is NOT a story reply. Skipping all pre-DM VIP auto-send.")
                        # Do not send any primary DM here; message will be handled manually.
                        return

                vip_rule_processed = False
                for rule in pre_dm_rules:
                    # If this is a Story reply, only process rules that match this Story
                    if story_id is not None and str(rule.media_id or "") != story_id:
                        continue
                    
                    ask_to_follow = rule.config.get("ask_to_follow", False)
                    ask_for_email = rule.config.get("ask_for_email", False)
                    
                    if not (ask_to_follow or ask_for_email):
                        continue

                    # STRICT MODE: For story-based KEYWORD rules, only auto-send for VIP
                    # if this message actually matches one of the configured keywords.
                    try:
                        if getattr(rule, "trigger_type", None) == "keyword":
                            cfg = rule.config or {}
                            keywords_list = []
                            if cfg.get("keywords") and isinstance(cfg.get("keywords"), list):
                                keywords_list = [str(k).strip().lower() for k in cfg.get("keywords") if k and str(k).strip()]
                            elif cfg.get("keyword"):
                                keywords_list = [str(cfg.get("keyword", "")).strip().lower()]

                            matched_keyword = None
                            if keywords_list and message_text:
                                message_text_lower = message_text.strip().lower()
                                for kw in keywords_list:
                                    kw_clean = kw.strip().lower()
                                    msg_clean = message_text_lower
                                    # Exact match
                                    if kw_clean == msg_clean:
                                        matched_keyword = kw
                                        break
                                    # Whole-word contains
                                    if kw_clean in msg_clean:
                                        import re
                                        pattern = r'\b' + re.escape(kw_clean) + r'\b'
                                        if re.search(pattern, msg_clean):
                                            matched_keyword = kw
                                            break

                            if keywords_list and not matched_keyword:
                                log_print(f"⏭️ [VIP] Story reply '{message_text}' does NOT match any keywords for rule '{rule.name}' (ID: {rule.id}), skipping VIP auto-send for this rule")
                                continue
                    except Exception as vip_kw_err:
                        log_print(f"⚠️ [VIP] Error while checking story keyword match for rule {rule.id}: {str(vip_kw_err)}")
                    
                    # Only process the FIRST matching rule for VIP users to avoid duplicates
                    if not vip_rule_processed:
                        log_print(f"⭐ [VIP] Sending primary DM for rule '{rule.name}' (ID: {rule.id}) - user is already converted")
                        # Skip directly to primary DM for this rule (no email success message needed - they already provided email)
                        asyncio.create_task(execute_automation_action(
                            rule, sender_id, account, db,
                            trigger_type="story_reply" if story_id else "new_message",
                            message_id=message_id,
                            pre_dm_result_override={"action": "send_primary"},
                            skip_growth_steps=True
                        ))
                        vip_rule_processed = True
                        processed_rules_count += 1
                        break  # Exit loop after processing first matching rule
                
                # If we processed a VIP rule, return early to prevent duplicate processing
                if vip_rule_processed:
                    log_print(f"✅ [VIP] Processed primary DM for VIP user, skipping further rule processing")
                    return
            
            for rule in pre_dm_rules:
                # If this is a Story reply, only process rules that match this Story.
                # Post/Reel rules (different media_id) would return "ignore" and block the Story flow.
                if story_id is not None and str(rule.media_id or "") != story_id:
                    continue

                _rule_cfg = rule.config or {}
                ask_to_follow = _rule_cfg.get("ask_to_follow", False)
                ask_for_email = _rule_cfg.get("ask_for_email", False)
                has_simple_flow = _rule_cfg.get("simple_dm_flow") or _rule_cfg.get("simpleDmFlow")
                has_simple_phone_flow = _rule_cfg.get("simple_dm_flow_phone") or _rule_cfg.get("simpleDmFlowPhone")
                # Include rules with Email/Phone/Followers pre-DM so we process invalid email/phone replies (e.g. send retry)
                if not (ask_to_follow or ask_for_email or has_simple_flow or has_simple_phone_flow):
                    continue
                
                # CRITICAL FIX: Check if THIS SPECIFIC rule has completed, not globally
                # This ensures Reel A (phone) doesn't skip when Reel B (email) completed
                from app.services.pre_dm_handler import get_pre_dm_state, check_if_email_response
                from app.services.lead_capture import validate_phone
                state = get_pre_dm_state(sender_id, rule.id)
                
                # Check if this specific rule has completed its flow
                rule_completed = False
                if state.get("primary_dm_sent"):
                    # Check if lead was captured for THIS specific rule (if lead capture)
                    config = rule.config or {}
                    is_lead = config.get("is_lead_capture", False)
                    if is_lead:
                        try:
                            from sqlalchemy import cast
                            from sqlalchemy.dialects.postgresql import JSONB
                            from app.models.captured_lead import CapturedLead
                            has_lead_for_this_rule = (
                                db.query(CapturedLead.id)
                                .filter(
                                    CapturedLead.instagram_account_id == account.id,
                                    CapturedLead.automation_rule_id == rule.id,  # Check THIS rule only
                                    cast(CapturedLead.extra_metadata, JSONB)["sender_id"].astext == str(sender_id),
                                )
                                .limit(1)
                                .first()
                            )
                            rule_completed = has_lead_for_this_rule is not None
                        except Exception:
                            rule_completed = False
                    else:
                        # Simple reply rule - if primary_dm_sent, it's complete
                        rule_completed = True
                
                if rule_completed:
                    log_print(f"⏭️ Rule {rule.id} ({rule.name}) already completed for sender {sender_id}, skipping")
                    continue
                
                # CRITICAL FIX: Ensure rules only process responses matching their flow type
                # Reel A (phone) shouldn't process email responses, Reel B (email) shouldn't process phone responses
                config = rule.config or {}
                simple_dm_flow = config.get("simple_dm_flow", False) or config.get("simpleDmFlow", False)
                simple_dm_flow_phone = config.get("simple_dm_flow_phone", False) or config.get("simpleDmFlowPhone", False)
                
                # If this is a phone flow rule, skip if message looks like an email
                if simple_dm_flow_phone and message_text:
                    is_email, _ = check_if_email_response(message_text)
                    if is_email:
                        log_print(f"⏭️ Rule {rule.id} (phone flow) skipping email response '{message_text[:50]}...' - this rule only processes phone numbers")
                        continue
                
                # If this is an email flow rule, skip if message looks like a phone number
                if simple_dm_flow and not simple_dm_flow_phone and message_text:
                    is_valid_phone, _ = validate_phone(message_text.strip())
                    if is_valid_phone:
                        log_print(f"⏭️ Rule {rule.id} (email flow) skipping phone response '{message_text[:50]}...' - this rule only processes emails")
                        continue
                
                # Debug logging to trace why follow confirmation isn't triggering
                is_follow_confirmation = check_if_follow_confirmation(message_text)
                log_print(f"🔍 [DEBUG] Message '{message_text}' from {sender_id}, rule {rule.id}: ask_to_follow={ask_to_follow}, is_follow_confirmation={is_follow_confirmation}, state={state}")
                
                if ask_to_follow and is_follow_confirmation and state.get("follow_request_sent") and not state.get("follow_confirmed"):
                    log_print(f"✅ Follow confirmation detected from {sender_id} for rule '{rule.name}' (Rule ID: {rule.id})")
                    log_print(f"🔍 DEBUG: ask_to_follow={ask_to_follow}, ask_for_email={ask_for_email}, state={state}")
                    # User confirmed they're following - mark as followed and proceed
                    pre_dm_result = await process_pre_dm_actions(
                        rule, sender_id, account, db,
                        incoming_message=message_text,
                        trigger_type="story_reply" if story_id else "new_message"
                    )
                    
                    log_print(f"🔍 DEBUG: pre_dm_result action={pre_dm_result.get('action')}, message={pre_dm_result.get('message', '')[:50] if pre_dm_result.get('message') else 'None'}")
                    
                    if pre_dm_result["action"] == "send_email_request":
                        # RACE CONDITION FIX: Only send ONE email question even if multiple rules are waiting
                        if not sent_email_request:
                            # STRICT MODE: Send email request IMMEDIATELY after follow confirmation
                            log_print(f"✅ [STRICT MODE] Follow confirmed from {sender_id} for rule {rule.id}, sending email request now")
                            
                            # Get email request message
                            email_message = pre_dm_result.get("message", "")
                            
                            # Send email request as TEXT-ONLY (no buttons)
                            from app.utils.encryption import decrypt_credentials
                            from app.utils.instagram_api import send_dm
                            
                            try:
                                if account.encrypted_page_token:
                                    access_token = decrypt_credentials(account.encrypted_page_token)
                                elif account.encrypted_credentials:
                                    access_token = decrypt_credentials(account.encrypted_credentials)
                                else:
                                    raise Exception("No access token found")
                                
                                page_id = account.page_id
                                
                                # Send email request as plain text (ONLY ONCE)
                                send_dm(sender_id, email_message, access_token, page_id, buttons=None, quick_replies=None)
                                log_print(f"✅ Email request sent (single message for all rules)")
                                sent_email_request = True
                                processed_rules_count += 1
                                
                                # Log DM sent (tracks in DmLog and increments global tracker)
                                try:
                                    from app.utils.plan_enforcement import log_dm_sent
                                    log_dm_sent(
                                        user_id=account.user_id,
                                        instagram_account_id=account.id,
                                        recipient_username=str(sender_id),
                                        message=email_message,
                                        db=db,
                                        instagram_username=account.username,
                                        instagram_igsid=getattr(account, "igsid", None)
                                    )
                                except Exception as log_err:
                                    log_print(f"⚠️ Failed to log DM: {str(log_err)}", "WARNING")
                                
                            except Exception as e:
                                log_print(f"❌ Failed to send email request: {str(e)}", "ERROR")
                            
                            # Mark all matching rules as email_request_sent (they all share the same email question)
                            for r in pre_dm_rules:
                                if (story_id is None or str(r.media_id or "") == story_id) and r.config.get("ask_for_email", False):
                                    from app.services.pre_dm_handler import update_pre_dm_state, get_pre_dm_state, normalize_follow_recheck_message
                                    r_state = get_pre_dm_state(sender_id, r.id)
                                    if r_state.get("follow_request_sent") and not r_state.get("email_request_sent"):
                                        update_pre_dm_state(sender_id, r.id, {
                                            "email_request_sent": True,
                                            "step": "email"
                                        })
                                        log_print(f"✅ Marked rule {r.id} as email_request_sent")
                        
                        # Break after processing first matching rule (only one email question)
                        break
                    elif pre_dm_result["action"] == "send_primary":
                        # Skip to primary DM
                        log_print(f"✅ Follow confirmed from {sender_id} for rule {rule.id}, proceeding to primary DM")
                        asyncio.create_task(execute_automation_action(
                            rule,
                            sender_id,
                            account,
                            db,
                            trigger_type="story_reply" if story_id else "new_message",
                            message_id=message_id
                        ))
                        processed_rules_count += 1
                        # Continue to process other rules
                        continue
                
                # Check if user sent ANY message (could be email or invalid response)
                # Process pre-DM actions to check state and handle the message appropriately
                if ask_to_follow or ask_for_email or has_simple_flow or has_simple_phone_flow:
                    pre_dm_result = await process_pre_dm_actions(
                        rule, sender_id, account, db,
                        incoming_message=message_text,
                        trigger_type="story_reply" if story_id else "new_message"
                    )
                    log_print(f"🔍 [DEBUG] pre_dm_result action={pre_dm_result.get('action')} for rule {rule.id}")
                    
                    # Simple flow: one combined message (follow + email ask), then loop email until valid
                    if pre_dm_result["action"] == "send_simple_flow_start":
                        simple_msg = pre_dm_result.get("message", "Follow me to get the guide 👇 Reply with your email and I'll send it! 📧")
                        from app.utils.encryption import decrypt_credentials
                        from app.utils.instagram_api import send_dm
                        try:
                            if account.encrypted_page_token:
                                access_token = decrypt_credentials(account.encrypted_page_token)
                            elif account.encrypted_credentials:
                                access_token = decrypt_credentials(account.encrypted_credentials)
                            else:
                                raise Exception("No access token found")
                            send_dm(sender_id, simple_msg, access_token, account.page_id, buttons=None, quick_replies=None)
                            log_print(f"✅ [Simple flow] Start message sent to {sender_id}")
                            try:
                                from app.utils.plan_enforcement import log_dm_sent
                                log_dm_sent(
                                    user_id=account.user_id,
                                    instagram_account_id=account.id,
                                    recipient_username=str(sender_id),
                                    message=simple_msg,
                                    db=db,
                                    instagram_username=account.username,
                                    instagram_igsid=getattr(account, "igsid", None),
                                )
                            except Exception as log_err:
                                log_print(f"⚠️ Failed to log DM: {str(log_err)}", "WARNING")
                        except Exception as e:
                            log_print(f"❌ Failed to send simple flow message: {str(e)}", "ERROR")
                        return
                    
                    # Simple flow (Phone): one combined message (follow + phone ask), then loop until valid phone
                    if pre_dm_result["action"] == "send_simple_flow_start_phone":
                        simple_phone_msg = pre_dm_result.get("message", "Follow me to get the guide 👇 Reply with your phone number and I'll send it! 📱")
                        from app.utils.encryption import decrypt_credentials
                        from app.utils.instagram_api import send_dm
                        try:
                            if account.encrypted_page_token:
                                access_token = decrypt_credentials(account.encrypted_page_token)
                            elif account.encrypted_credentials:
                                access_token = decrypt_credentials(account.encrypted_credentials)
                            else:
                                raise Exception("No access token found")
                            send_dm(sender_id, simple_phone_msg, access_token, account.page_id, buttons=None, quick_replies=None)
                            log_print(f"✅ [Simple flow Phone] Start message sent to {sender_id}")
                            try:
                                from app.utils.plan_enforcement import log_dm_sent
                                log_dm_sent(
                                    user_id=account.user_id,
                                    instagram_account_id=account.id,
                                    recipient_username=str(sender_id),
                                    message=simple_phone_msg,
                                    db=db,
                                    instagram_username=account.username,
                                    instagram_igsid=getattr(account, "igsid", None),
                                )
                            except Exception as log_err:
                                log_print(f"⚠️ Failed to log DM: {str(log_err)}", "WARNING")
                        except Exception as e:
                            log_print(f"❌ Failed to send simple flow phone message: {str(e)}", "ERROR")
                        return
                    
                    # Handle follow reminder action (random text while waiting for follow confirmation)
                    if pre_dm_result["action"] == "send_follow_reminder":
                        # Send reminder message to user
                        follow_reminder_msg = pre_dm_result.get("message", 
                            "Hey! I'm waiting for you to confirm that you're following me. Please type 'done', 'followed', or 'I'm following' to continue! 😊")
                        log_print(f"💬 [FIX ISSUE 2] Sending follow reminder to {sender_id}: {follow_reminder_msg[:50]}...")
                        
                        from app.utils.encryption import decrypt_credentials
                        from app.utils.instagram_api import send_dm
                        
                        try:
                            if account.encrypted_page_token:
                                access_token = decrypt_credentials(account.encrypted_page_token)
                            elif account.encrypted_credentials:
                                access_token = decrypt_credentials(account.encrypted_credentials)
                            else:
                                raise Exception("No access token found")
                            
                            page_id = account.page_id
                            send_dm(sender_id, follow_reminder_msg, access_token, page_id, buttons=None, quick_replies=None)
                            log_print(f"✅ Follow reminder sent successfully")
                            
                            # Log DM sent
                            try:
                                from app.utils.plan_enforcement import log_dm_sent
                                log_dm_sent(
                                    user_id=account.user_id,
                                    instagram_account_id=account.id,
                                    recipient_username=str(sender_id),
                                    message=follow_reminder_msg,
                                    db=db,
                                    instagram_username=account.username,
                                    instagram_igsid=getattr(account, "igsid", None)
                                )
                            except Exception as log_err:
                                log_print(f"⚠️ Failed to log DM: {str(log_err)}", "WARNING")
                        except Exception as send_err:
                            log_print(f"⚠️ Failed to send follow reminder: {str(send_err)}", "ERROR")
                        
                        # Continue to check other rules
                        continue
                    
                    # Handle Followers-only "No" exit (no primary DM; next comment/story reply asks "Are you following me?" again)
                    if pre_dm_result["action"] == "send_follow_no_exit":
                        _default_exit = "No problem! Story reply again anytime when you'd like the guide. 📩" if trigger_type == "story_reply" else "No problem! Comment again anytime when you'd like the guide. 📩"
                        exit_msg = pre_dm_result.get("message", _default_exit)
                        log_print(f"📩 [FOLLOWERS] Sending exit message (no primary DM) to {sender_id}")
                        from app.services.pre_dm_handler import update_pre_dm_state
                        update_pre_dm_state(str(sender_id), rule.id, {
                            "follow_recheck_sent": False,
                            "follow_exit_sent": True,
                            "follow_request_sent": True,
                        })
                        from app.utils.encryption import decrypt_credentials
                        from app.utils.instagram_api import send_dm
                        try:
                            if account.encrypted_page_token:
                                _tok = decrypt_credentials(account.encrypted_page_token)
                            elif account.encrypted_credentials:
                                _tok = decrypt_credentials(account.encrypted_credentials)
                            else:
                                raise Exception("No access token found")
                            send_dm(sender_id, exit_msg, _tok, account.page_id, buttons=None, quick_replies=None)
                            log_print(f"✅ Exit message sent")
                            try:
                                from app.utils.plan_enforcement import log_dm_sent
                                log_dm_sent(
                                    user_id=account.user_id,
                                    instagram_account_id=account.id,
                                    recipient_username=str(sender_id),
                                    message=exit_msg,
                                    db=db,
                                    instagram_username=account.username,
                                    instagram_igsid=getattr(account, "igsid", None),
                                )
                            except Exception as log_err:
                                log_print(f"⚠️ Failed to log DM: {str(log_err)}", "WARNING")
                        except Exception as send_err:
                            log_print(f"⚠️ Failed to send exit message: {str(send_err)}", "ERROR")
                        continue
                    
                    # Handle "Are you following me?" recheck with Yes/No buttons
                    if pre_dm_result["action"] == "send_follow_recheck":
                        from app.services.pre_dm_handler import normalize_follow_recheck_message as _norm_follow_recheck
                        follow_recheck_msg = _norm_follow_recheck(pre_dm_result.get("message") or "Are you following me?")
                        log_print(f"💬 Sending follow recheck question to {sender_id}: {follow_recheck_msg}")
                        
                        from app.utils.encryption import decrypt_credentials
                        from app.utils.instagram_api import send_dm
                        
                        try:
                            if account.encrypted_page_token:
                                access_token = decrypt_credentials(account.encrypted_page_token)
                            elif account.encrypted_credentials:
                                access_token = decrypt_credentials(account.encrypted_credentials)
                            else:
                                raise Exception("No access token found")
                            
                            page_id = account.page_id
                            
                            # Create Yes/No quick reply buttons
                            yes_no_quick_replies = [
                                {
                                    "content_type": "text",
                                    "title": "Yes",
                                    "payload": f"follow_recheck_yes_{rule.id}"
                                },
                                {
                                    "content_type": "text",
                                    "title": "No",
                                    "payload": f"follow_recheck_no_{rule.id}"
                                }
                            ]
                            
                            send_dm(sender_id, follow_recheck_msg, access_token, page_id, buttons=None, quick_replies=yes_no_quick_replies)
                            log_print(f"✅ 'Are you following me?' question sent with Yes/No buttons")
                            # Store how they entered so "No" quick reply can show Comment again vs Story reply again
                            _recheck_trigger = "story_reply" if story_id else "post_comment"
                            update_pre_dm_state(str(sender_id), rule.id, {"follow_recheck_trigger_type": _recheck_trigger})
                            # Log DM sent
                            try:
                                from app.utils.plan_enforcement import log_dm_sent
                                log_dm_sent(
                                    user_id=account.user_id,
                                    instagram_account_id=account.id,
                                    recipient_username=str(sender_id),
                                    message=follow_recheck_msg,
                                    db=db,
                                    instagram_username=account.username,
                                    instagram_igsid=getattr(account, "igsid", None)
                                )
                            except Exception as log_err:
                                log_print(f"⚠️ Failed to log DM: {str(log_err)}", "WARNING")
                        except Exception as send_err:
                            log_print(f"⚠️ Failed to send follow recheck: {str(send_err)}", "ERROR")
                        
                        # Continue to check other rules
                        continue
                    
                    # Handle ignore action (random text while waiting for follow confirmation) - DEPRECATED, use send_follow_reminder instead
                    if pre_dm_result["action"] == "ignore":
                        log_print(f"⏳ [STRICT MODE] Ignoring random text while waiting for follow confirmation: '{message_text}' for rule {rule.id}")
                        # Continue to check other rules - one rule ignoring doesn't mean all should
                        continue
                    
                    # Handle email request action (follow confirmed, now send email question)
                    if pre_dm_result["action"] == "send_email_request":
                        # RACE CONDITION FIX: Only send ONE email question even if multiple rules are waiting
                        if not sent_email_request:
                            log_print(f"✅ [STRICT MODE] Follow confirmed from {sender_id} for rule {rule.id}, sending email request now")
                            
                            # Get email request message
                            email_message = pre_dm_result.get("message", "")
                            
                            # Send email request as TEXT-ONLY (no buttons)
                            from app.utils.encryption import decrypt_credentials
                            from app.utils.instagram_api import send_dm
                            
                            try:
                                if account.encrypted_page_token:
                                    access_token = decrypt_credentials(account.encrypted_page_token)
                                elif account.encrypted_credentials:
                                    access_token = decrypt_credentials(account.encrypted_credentials)
                                else:
                                    raise Exception("No access token found")
                                
                                page_id = account.page_id
                                
                                # Send email request as plain text (ONLY ONCE)
                                send_dm(sender_id, email_message, access_token, page_id, buttons=None, quick_replies=None)
                                log_print(f"✅ Email request sent (single message for all rules)")
                                sent_email_request = True
                                processed_rules_count += 1
                                
                                # Log DM sent (tracks in DmLog and increments global tracker)
                                try:
                                    from app.utils.plan_enforcement import log_dm_sent
                                    log_dm_sent(
                                        user_id=account.user_id,
                                        instagram_account_id=account.id,
                                        recipient_username=str(sender_id),
                                        message=email_message,
                                        db=db,
                                        instagram_username=account.username,
                                        instagram_igsid=getattr(account, "igsid", None)
                                    )
                                except Exception as log_err:
                                    log_print(f"⚠️ Failed to log DM: {str(log_err)}", "WARNING")
                                
                            except Exception as e:
                                log_print(f"❌ Failed to send email request: {str(e)}", "ERROR")
                            
                            # Mark all matching rules as email_request_sent (they all share the same email question)
                            for r in pre_dm_rules:
                                if (story_id is None or str(r.media_id or "") == story_id) and r.config.get("ask_for_email", False):
                                    from app.services.pre_dm_handler import update_pre_dm_state, get_pre_dm_state, normalize_follow_recheck_message
                                    r_state = get_pre_dm_state(sender_id, r.id)
                                    if r_state.get("follow_request_sent") and not r_state.get("email_request_sent"):
                                        update_pre_dm_state(sender_id, r.id, {
                                            "email_request_sent": True,
                                            "step": "email"
                                        })
                                        log_print(f"✅ Marked rule {r.id} as email_request_sent")
                        
                        # Break after processing first matching rule (only one email question)
                        break
                    
                    # Handle phone request (simple flow phone: re-ask phone question)
                    if pre_dm_result["action"] == "send_phone_request":
                        if not sent_phone_request:
                            phone_message = pre_dm_result.get("message", "What's your phone number? Reply here and I'll send you the guide! 📱")
                            from app.utils.encryption import decrypt_credentials
                            from app.utils.instagram_api import send_dm
                            try:
                                if account.encrypted_page_token:
                                    access_token = decrypt_credentials(account.encrypted_page_token)
                                elif account.encrypted_credentials:
                                    access_token = decrypt_credentials(account.encrypted_credentials)
                                else:
                                    raise Exception("No access token found")
                                send_dm(sender_id, phone_message, access_token, account.page_id, buttons=None, quick_replies=None)
                                log_print(f"✅ [Simple flow Phone] Phone question sent to {sender_id}")
                                sent_phone_request = True
                                processed_rules_count += 1
                                try:
                                    from app.utils.plan_enforcement import log_dm_sent
                                    log_dm_sent(
                                        user_id=account.user_id,
                                        instagram_account_id=account.id,
                                        recipient_username=str(sender_id),
                                        message=phone_message,
                                        db=db,
                                        instagram_username=account.username,
                                        instagram_igsid=getattr(account, "igsid", None),
                                    )
                                except Exception as log_err:
                                    log_print(f"⚠️ Failed to log DM: {str(log_err)}", "WARNING")
                            except Exception as e:
                                log_print(f"❌ Failed to send phone request: {str(e)}", "ERROR")
                        return
                    
                    # Handle valid email or phone - proceed to primary DM (RACE CONDITION FIX: Process once, send primary DM for ALL rules)
                    if pre_dm_result["action"] == "send_primary" and (pre_dm_result.get("email") or pre_dm_result.get("phone")):
                        # RACE CONDITION FIX: Process email/phone only once, then send primary DM for ALL matching rules
                        if pre_dm_result.get("email") and processed_email is None:
                            processed_email = pre_dm_result.get("email")
                            log_print(f"✅ [STRICT MODE] Valid email received: {processed_email}")
                        if pre_dm_result.get("phone") and processed_phone is None:
                            processed_phone = pre_dm_result.get("phone")
                            log_print(f"✅ [Simple flow Phone] Valid phone received: {processed_phone}")
                        if processed_email or processed_phone:
                            log_print(f"📤 Will send primary DM for ALL rules waiting for lead")
                            for r in pre_dm_rules:
                                if (story_id is None or str(r.media_id or "") == story_id):
                                    r_state = get_pre_dm_state(sender_id, r.id)
                                    if (r_state.get("email_request_sent") and not r_state.get("email_received")) or (r_state.get("phone_request_sent") and not r_state.get("phone_received")):
                                        if r not in rules_waiting_for_email:
                                            rules_waiting_for_email.append(r)
                                            log_print(f"📋 Rule {r.id} is waiting for lead, will send primary DM")
                        
                        if rule not in rules_waiting_for_email:
                            rules_waiting_for_email.append(rule)
                            log_print(f"📋 Added current rule {rule.id} to waiting list for primary DM")
                        
                        from app.services.pre_dm_handler import update_pre_dm_state
                        if processed_email:
                            update_pre_dm_state(sender_id, rule.id, {"email_received": True, "email": processed_email})
                            log_print(f"✅ Marked rule {rule.id} as email_received: {processed_email}")
                        if processed_phone:
                            update_pre_dm_state(sender_id, rule.id, {"phone_received": True, "phone": processed_phone})
                            log_print(f"✅ Marked rule {rule.id} as phone_received: {processed_phone}")
                        
                        continue
                    
                    # Handle invalid phone - send retry message (simple flow phone)
                    if pre_dm_result["action"] == "send_phone_retry":
                        if not sent_retry_message:
                            log_print(f"⚠️ [STRICT MODE] Invalid phone format, sending retry message")
                            from app.utils.encryption import decrypt_credentials
                            from app.utils.instagram_api import send_dm
                            try:
                                if account.encrypted_page_token:
                                    access_token = decrypt_credentials(account.encrypted_page_token)
                                elif account.encrypted_credentials:
                                    access_token = decrypt_credentials(account.encrypted_credentials)
                                else:
                                    raise Exception("No access token found")
                                page_id = account.page_id
                                retry_msg = pre_dm_result.get("message", "") or "That doesn't look like a valid phone number. 🤔 Please share your correct number so I can send you the guide! 📱"
                                send_dm(sender_id, retry_msg, access_token, page_id, buttons=None, quick_replies=None)
                                log_print(f"✅ Retry message sent, waiting for valid phone")
                                sent_retry_message = True
                                processed_rules_count += 1
                                try:
                                    from app.utils.plan_enforcement import log_dm_sent
                                    log_dm_sent(
                                        user_id=account.user_id,
                                        instagram_account_id=account.id,
                                        recipient_username=str(sender_id),
                                        message=retry_msg,
                                        db=db,
                                        instagram_username=account.username,
                                        instagram_igsid=getattr(account, "igsid", None)
                                    )
                                except Exception as log_err:
                                    log_print(f"⚠️ Failed to log DM: {str(log_err)}", "WARNING")
                            except Exception as e:
                                log_print(f"❌ Failed to send phone retry message: {str(e)}", "ERROR")
                        log_print(f"⏳ Waiting for valid phone, skipping primary DM")
                        return
                    
                    # Handle invalid email - send retry message (only once, not per rule)
                    # Use standalone 'if' so this is always evaluated (not tied to send_primary branch)
                    if pre_dm_result["action"] == "send_email_retry":
                        # STRICT MODE: Email was invalid! Send retry message and WAIT
                        # Only send retry message once, even if multiple rules are waiting
                        if not sent_retry_message:
                            log_print(f"⚠️ [STRICT MODE] Invalid email format, sending retry message")
                            
                            # Send retry message
                            from app.utils.encryption import decrypt_credentials
                            from app.utils.instagram_api import send_dm
                            
                            try:
                                if account.encrypted_page_token:
                                    access_token = decrypt_credentials(account.encrypted_page_token)
                                elif account.encrypted_credentials:
                                    access_token = decrypt_credentials(account.encrypted_credentials)
                                else:
                                    raise Exception("No access token found")
                                
                                page_id = account.page_id
                                retry_msg = pre_dm_result.get("message", "")
                                
                                # Ensure retry message is not empty - use default if empty
                                if not retry_msg or not retry_msg.strip():
                                    retry_msg = "Hmm, that doesn't look like a valid email address. 🤔\n\nPlease type it again so I can send you the guide! 📧"
                                
                                send_dm(sender_id, retry_msg, access_token, page_id, buttons=None, quick_replies=None)
                                log_print(f"✅ Retry message sent, waiting for valid email")
                                sent_retry_message = True
                                processed_rules_count += 1
                                
                                # Log DM sent (tracks in DmLog and increments global tracker)
                                try:
                                    from app.utils.plan_enforcement import log_dm_sent
                                    log_dm_sent(
                                        user_id=account.user_id,
                                        instagram_account_id=account.id,
                                        recipient_username=str(sender_id),
                                        message=retry_msg,
                                        db=db,
                                        instagram_username=account.username,
                                        instagram_igsid=getattr(account, "igsid", None)
                                    )
                                except Exception as log_err:
                                    log_print(f"⚠️ Failed to log DM: {str(log_err)}", "WARNING")
                                
                            except Exception as e:
                                log_print(f"❌ Failed to send retry message: {str(e)}", "ERROR")
                        
                        # CRITICAL: After sending retry message, return early to prevent primary DM from being sent
                        # Don't continue processing other rules - we're waiting for valid email
                        log_print(f"⏳ Waiting for valid email, skipping primary DM")
                        return  # Exit early - don't send primary DM when email is invalid
                    
                    # If we're waiting for something and got random text (no action matched), ignore and continue
                    from app.services.pre_dm_handler import get_pre_dm_state
                    state = get_pre_dm_state(sender_id, rule.id)
                    
                    if state.get("follow_request_sent") and not state.get("follow_confirmed"):
                        log_print(f"⏳ [STRICT MODE] Waiting for follow confirmation from {sender_id} for rule {rule.id}")
                        if attachments:
                            log_print(f"   🚫 Image/attachment ignored - only text confirmations accepted")
                        else:
                            log_print(f"   Message '{message_text}' ignored - not a valid confirmation")
                        # Continue to check other rules
                        continue
                    
                    if state.get("email_request_sent") and not state.get("email_received"):
                        log_print(f"⏳ [STRICT MODE] Waiting for email from {sender_id} for rule {rule.id}")
                        if attachments:
                            log_print(f"   🚫 Image/attachment ignored - only email text accepted")
                        else:
                            log_print(f"   Message '{message_text}' ignored - not a valid email")
                        continue
                    if state.get("phone_request_sent") and not state.get("phone_received"):
                        log_print(f"⏳ [Simple flow Phone] Waiting for phone from {sender_id} for rule {rule.id}")
                        if attachments:
                            log_print(f"   🚫 Image/attachment ignored - only phone text accepted")
                        else:
                            log_print(f"   Message '{message_text}' ignored - not a valid phone")
                        continue
            
            # RACE CONDITION FIX: If we processed an email or phone, send primary DM for ALL rules waiting for lead
            if (processed_email or processed_phone) and rules_waiting_for_email:
                lead_info = processed_email or processed_phone
                log_print(f"📤 [RACE CONDITION FIX] Sending primary DM for {len(rules_waiting_for_email)} rule(s) with lead: {lead_info}")
                for idx, r in enumerate(rules_waiting_for_email):
                    if idx > 0:
                        await asyncio.sleep(0.1 * idx)
                    
                    from app.services.pre_dm_handler import get_pre_dm_state
                    r_state = get_pre_dm_state(sender_id, r.id)
                    stored_comment_id = r_state.get("comment_id")
                    
                    from app.services.pre_dm_handler import update_pre_dm_state
                    # Do NOT set primary_dm_sent here — execute_automation_action would see it and skip sending. Set it after we actually send.
                    if processed_email:
                        update_pre_dm_state(sender_id, r.id, {
                            "email_received": True,
                            "email": processed_email,
                        })
                    if processed_phone:
                        update_pre_dm_state(sender_id, r.id, {
                            "phone_received": True,
                            "phone": processed_phone,
                        })
                    
                    log_print(f"📤 Sending primary DM for rule {r.id} (lead: {lead_info})")
                    
                    override = {"action": "send_primary"}
                    if processed_email:
                        override["email"] = processed_email
                        override["send_email_success"] = True
                    if processed_phone:
                        override["phone"] = processed_phone
                    
                    # Pass incoming_message so execute_automation_action doesn't treat as "no message" and return early.
                    try:
                        await execute_automation_action(
                            r,
                            sender_id,
                            account,
                            db,
                            trigger_type="story_reply" if story_id else "new_message",
                            message_id=message_id,
                            comment_id=stored_comment_id,
                            pre_dm_result_override=override,
                            incoming_message=message_text,
                        )
                        # Only mark primary_dm_sent after we actually sent (so future messages don't re-trigger)
                        update_pre_dm_state(sender_id, r.id, {"primary_dm_sent": True})
                        processed_rules_count += 1
                    except Exception as e:
                        log_print(f"❌ Failed to send primary DM for rule {r.id}: {e}", "ERROR")
                        import traceback
                        traceback.print_exc()
                
                log_print(f"✅ Sent primary DM for {processed_rules_count} rule(s)" + (f" ({(len(rules_waiting_for_email) - processed_rules_count)} failed)" if processed_rules_count < len(rules_waiting_for_email) else ""))
            
            # If we processed any rule, don't continue to new_message rules (must return so we don't fall through to "Primary DM already complete")
            if processed_rules_count > 0:
                log_print(f"✅ Processed {processed_rules_count} rule(s) in pre_dm_rules, skipping new_message rules")
                return
        
        # story_id was already extracted above (before pre_dm_rules) for story replies
        # NOTE: Deduplication already done at webhook entry (262-267). Do NOT check again here
        # or we would always return (same request) and never reach keyword/story DM logic.

        # Check for echo messages (messages sent by the bot itself)
        # Echo can be at message level or event level
        is_echo = message.get("is_echo", False) or event.get("is_echo", False)
        if is_echo:
            log_print(f"🚫 Ignoring bot's own message (echo flag): {message_text}")
            if message_id:
                _processed_message_ids.add(message_id)
                # Clean cache if it gets too large
                if len(_processed_message_ids) > _MAX_CACHE_SIZE:
                    _processed_message_ids.clear()
            return
        
        # Skip if no text (reactions, stickers, etc.)
        if not message_text or not message_text.strip():
            log_print(f"🚫 Ignoring message with no text content (mid: {message_id})")
            if message_id:
                _processed_message_ids.add(message_id)
            return
        
        log_print(f"📨 [DM] Message from {sender_id} (type: {type(sender_id).__name__}) to {recipient_id}: {message_text} (mid: {message_id})")
        
        # CRITICAL: Check if sender is the bot itself
        # Compare sender_id with account IGSID AND page_id (both can be the bot)
        # Normalize all IDs to strings for comparison
        sender_id_str = str(sender_id) if sender_id else None
        account_igsid_str = str(account.igsid) if account.igsid else None
        account_page_id_str = str(account.page_id) if account.page_id else None
        
        sender_matches_bot = False
        if sender_id_str and account_igsid_str and sender_id_str == account_igsid_str:
            sender_matches_bot = True
            print(f"🚫 Sender ID {sender_id_str} matches account IGSID {account_igsid_str}")
        
        if sender_id_str and account_page_id_str and sender_id_str == account_page_id_str:
            sender_matches_bot = True
            print(f"🚫 Sender ID {sender_id_str} matches account Page ID {account_page_id_str}")
        
        if sender_matches_bot:
            log_print(f"🚫 [DM] IGNORING message from bot's own account!")
            log_print(f"   sender_id={sender_id_str}, IGSID={account_igsid_str}, PageID={account_page_id_str}")
            # Mark as processed to prevent retry
            if message_id:
                _processed_message_ids.add(message_id)
            return
        
        log_print(f"✅ [DM] Found account: {account.username} (ID: {account.id}, IGSID: {account.igsid}, PageID: {account.page_id})")
        
        # Mark message as processed BEFORE triggering actions (prevents loops if action triggers webhook)
        if message_id:
            _processed_message_ids.add(message_id)
            # Clean cache if it gets too large
            if len(_processed_message_ids) > _MAX_CACHE_SIZE:
                _processed_message_ids.clear()
        
        # Find active automation rules for DMs
        # For story DMs: We need to check rules set up for stories (which may have trigger_type='post_comment' or 'keyword')
        # For regular DMs: Only check 'new_message' and global 'keyword' rules
        from app.models.automation_rule import AutomationRule
        from sqlalchemy import or_
        
        # For story DMs, also check post_comment rules with matching media_id (stories set up via Posts/Reels tab)
        story_post_comment_rules = []
        story_keyword_rules = []
        
        if story_id:
            # For story DMs, check post_comment rules set up for this specific story
            # (When user sets up automation for a story, it might be created as 'post_comment' type)
            story_post_comment_rules = db.query(AutomationRule).filter(
                AutomationRule.instagram_account_id == account.id,
                AutomationRule.trigger_type == "post_comment",
                AutomationRule.is_active == True,
                AutomationRule.media_id == story_id
            ).all()
            log_print(f"🔍 [STORY DM] Looking for rules with story_id: {story_id}")
            log_print(f"📋 [STORY DM] Found {len(story_post_comment_rules)} 'post_comment' rules for story_id: {story_id}")
            
            # If no rules found, list all rules to help debug
            if len(story_post_comment_rules) == 0:
                all_story_rules = db.query(AutomationRule).filter(
            AutomationRule.instagram_account_id == account.id,
            AutomationRule.is_active == True
        ).all()
                log_print(f"⚠️ [STORY DM] NO rules found for story {story_id}! Available rules:", "WARNING")
                for rule in all_story_rules:
                    log_print(f"   - {rule.name}: trigger={rule.trigger_type}, media_id={rule.media_id}")
        
        # new_message rules (work for all DMs including stories)
        # BACKEND STRICT MODE: Disable trigger_type="new_message" for now.
        # We only want automations to fire when they were initiated from
        # post/reel comments, story replies, or explicit keyword triggers.
        # Frontend still allows creating 'new_message' rules, but they are
        # ignored here to avoid unexpected DMs when users type arbitrary text.
        new_message_rules = []
        log_print(f"📋 [DM] Ignoring 'new_message' trigger_type rules for account '{account.username}' (backend disabled)")
        
        # Filter keyword rules for DMs
        # For story DMs: match rules specifically for that story OR global rules (no media_id)
        # For regular DMs: only match global rules (no media_id)
        keyword_rules_query = db.query(AutomationRule).filter(
            AutomationRule.instagram_account_id == account.id,
            AutomationRule.trigger_type == "keyword",
            AutomationRule.is_active == True
        )
        
        if story_id:
            # For story DMs, match rules set up for this specific story OR global rules (no media_id)
            keyword_rules_query = keyword_rules_query.filter(
                or_(
                    AutomationRule.media_id == story_id,
                    AutomationRule.media_id.is_(None)  # Also include global keyword rules
                )
            )
            log_print(f"🔍 [STORY DM] Filtering keyword rules for story_id: {story_id} (including global rules)")
        else:
            # For regular DMs, only match keyword rules without media_id (global DM rules)
            keyword_rules_query = keyword_rules_query.filter(
                AutomationRule.media_id.is_(None)
            )
            log_print(f"🔍 [DM] Filtering keyword rules for regular DM (only global rules, no media_id)")
        
        keyword_rules = keyword_rules_query.all()
        
        log_print(f"📋 [DM] Found {len(new_message_rules)} 'new_message' rules, {len(keyword_rules)} 'keyword' rules (global), and {len(story_post_comment_rules)} 'post_comment' rules for story")
        
        # Log all rules found for debugging
        if len(new_message_rules) == 0 and len(keyword_rules) == 0 and len(story_post_comment_rules) == 0:
            log_print(f"⚠️ [DM] NO automation rules found for this account! Check rule configuration.", "WARNING")
        
        # Debug: List all rules for this account to help troubleshoot
        all_rules = db.query(AutomationRule).filter(
            AutomationRule.instagram_account_id == account.id,
            AutomationRule.is_active == True
        ).all()
        print(f"🔍 DEBUG: All active rules for account '{account.username}' (ID: {account.id}):")
        for rule in all_rules:
            media_info = f" | Media ID: {rule.media_id}" if rule.media_id else " | Media ID: None (global)"
            keywords_info = ""
            if rule.config:
                if rule.config.get("keywords"):
                    keywords_info = f" | Keywords: {rule.config.get('keywords')}"
                elif rule.config.get("keyword"):
                    keywords_info = f" | Keyword: {rule.config.get('keyword')}"
            print(f"   - Rule: {rule.name or 'Unnamed'} | Trigger: {rule.trigger_type} | Active: {rule.is_active}{media_info}{keywords_info}")
        
        # VIP USER CHECK: If user is already converted, skip regular rule processing to prevent duplicate primary DMs.
        # Exception: for story replies (story_id set), allow story-specific rules to run so story automations work.
        if is_vip_user and story_id is None:
            log_print(f"⭐ [VIP] User is already converted, skipping ALL regular rule processing (keyword, new_message) to prevent duplicate primary DMs")
            return
        if is_vip_user and story_id:
            log_print(f"⭐ [VIP] User is converted; processing only story-specific rules for story_id={story_id} (no global keyword/new_message)")
        
        # Get all active rules for this account (used for primary-DM-complete check and lead capture)
        all_active_rules = db.query(AutomationRule).filter(
            AutomationRule.instagram_account_id == account.id,
            AutomationRule.is_active == True
        ).all()
        
        # FIX: After Simple Reply OR Lead Capture primary DM is complete, do NOT trigger ANY automation.
        # User typing anything → handled by real user only.
        from app.services.pre_dm_handler import get_pre_dm_state, sender_primary_dm_complete
        if sender_primary_dm_complete(sender_id, account.id, all_active_rules, db):
            log_print(f"🚫 [FIX] Primary DM already complete for this user (simple reply or lead capture). Skipping ALL automation.")
            log_print(f"   💬 User messages will be handled by real user only — no system reply.")
            return
        
        # FIX: Check for lead capture flow processing when user sends message after primary DM
        # This handles cases where user provides email/lead info after primary DM was sent
        from app.services.lead_capture import process_lead_capture_step
        from app.utils.encryption import decrypt_credentials
        from app.utils.instagram_api import send_dm as send_dm_api
        
        lead_capture_processed = False
        for rule in all_active_rules:
            if rule.config.get("is_lead_capture", False):
                rule_state = get_pre_dm_state(sender_id, rule.id)
                # If primary DM was sent and user sends a message, process it through lead capture flow
                if rule_state.get("primary_dm_sent") and message_text and message_text.strip():
                    # Check if lead was already captured - if yes, don't send any messages
                    from app.models.captured_lead import CapturedLead
                    from sqlalchemy import cast
                    from sqlalchemy.dialects.postgresql import JSONB
                    existing_lead = db.query(CapturedLead).filter(
                        CapturedLead.automation_rule_id == rule.id,
                        CapturedLead.instagram_account_id == account.id,
                        cast(CapturedLead.extra_metadata, JSONB)['sender_id'].astext == str(sender_id)
                    ).first()
                    # Only treat as "lead captured" if lead matches current flow type (email vs phone)
                    lead_matches_flow = False
                    if existing_lead:
                        cfg = rule.config or {}
                        simple_dm_flow_phone = cfg.get("simple_dm_flow_phone", False) or cfg.get("simpleDmFlowPhone", False)
                        simple_dm_flow = cfg.get("simple_dm_flow", False) or cfg.get("simpleDmFlow", False)
                        ask_for_email = cfg.get("ask_for_email", False) or cfg.get("askForEmail", False)
                        if simple_dm_flow_phone:
                            lead_matches_flow = bool(existing_lead.phone and str(existing_lead.phone).strip())
                        elif simple_dm_flow or ask_for_email:
                            lead_matches_flow = bool(existing_lead.email and str(existing_lead.email).strip())
                        else:
                            lead_matches_flow = True
                    if existing_lead and lead_matches_flow:
                        # FIX: Lead already captured for this flow type - flow is complete, stop ALL automation
                        log_print(f"🚫 [FIX] Lead already captured for rule {rule.id}, stopping automation completely")
                        log_print(f"   💬 All further messages will be handled by real user, not automation")
                        lead_capture_processed = True  # Mark as processed to skip further rule processing
                        break
                    
                    log_print(f"📧 [LEAD CAPTURE] Processing message '{message_text}' for lead capture rule {rule.id} (primary DM already sent)")
                    lead_result = process_lead_capture_step(rule, message_text, sender_id, db)
                    
                    if lead_result.get("action") == "send" and lead_result.get("saved_lead"):
                        # Lead was captured successfully - count should be incremented in process_lead_capture_step
                        log_print(f"✅ [LEAD CAPTURE] Lead captured successfully: {lead_result['saved_lead'].email or lead_result['saved_lead'].phone}")
                        
                        # Send confirmation message
                        try:
                            if account.encrypted_page_token:
                                access_token_lead = decrypt_credentials(account.encrypted_page_token)
                                account_page_id_lead = account.page_id
                            elif account.encrypted_credentials:
                                access_token_lead = decrypt_credentials(account.encrypted_credentials)
                                account_page_id_lead = account.page_id
                            else:
                                raise Exception("No access token found")
                            
                            confirmation_msg = lead_result.get("message", "Thank you! We've received your information.")
                            send_dm_api(sender_id, confirmation_msg, access_token_lead, account_page_id_lead, buttons=None, quick_replies=None)
                            log_print(f"✅ [LEAD CAPTURE] Confirmation message sent")
                            
                            # Log DM sent
                            try:
                                from app.utils.plan_enforcement import log_dm_sent
                                log_dm_sent(
                                    user_id=account.user_id,
                                    instagram_account_id=account.id,
                                    recipient_username=str(sender_id),
                                    message=confirmation_msg,
                                    db=db,
                                    instagram_username=account.username,
                                    instagram_igsid=getattr(account, "igsid", None)
                                )
                            except Exception as log_err:
                                log_print(f"⚠️ Failed to log DM: {str(log_err)}", "WARNING")
                        except Exception as send_err:
                            log_print(f"⚠️ Failed to send lead capture confirmation: {str(send_err)}", "WARNING")
                        
                        lead_capture_processed = True
                        break  # Process only first matching lead capture rule
                    elif lead_result.get("action") == "ask":
                        # Need to ask for more info or resend question
                        ask_msg = lead_result.get("message", "")
                        if ask_msg:
                            try:
                                if account.encrypted_page_token:
                                    access_token_lead = decrypt_credentials(account.encrypted_page_token)
                                    account_page_id_lead = account.page_id
                                elif account.encrypted_credentials:
                                    access_token_lead = decrypt_credentials(account.encrypted_credentials)
                                    account_page_id_lead = account.page_id
                                else:
                                    raise Exception("No access token found")
                                
                                send_dm_api(sender_id, ask_msg, access_token_lead, account_page_id_lead, buttons=None, quick_replies=None)
                                log_print(f"✅ [LEAD CAPTURE] Question/reminder sent: {ask_msg[:50]}...")
                                
                                # Log DM sent
                                try:
                                    from app.utils.plan_enforcement import log_dm_sent
                                    log_dm_sent(
                                        user_id=account.user_id,
                                        instagram_account_id=account.id,
                                        recipient_username=str(sender_id),
                                        message=ask_msg,
                                        db=db,
                                        instagram_username=account.username,
                                        instagram_igsid=getattr(account, "igsid", None)
                                    )
                                except Exception as log_err:
                                    log_print(f"⚠️ Failed to log DM: {str(log_err)}", "WARNING")
                            except Exception as send_err:
                                log_print(f"⚠️ Failed to send lead capture question: {str(send_err)}", "WARNING")
                            
                            lead_capture_processed = True
                            break
        
        # If lead capture was processed, skip further rule processing to prevent duplicate triggers
        if lead_capture_processed:
            log_print(f"✅ [LEAD CAPTURE] Lead capture processed, skipping further rule processing")
            return
        
        # For story DMs, first check if any story-specific post_comment rule should trigger (any comment/DM on that story)
        story_rule_matched = False
        if story_id and story_post_comment_rules:
            log_print(f"🎯 [STORY DM] Processing {len(story_post_comment_rules)} story rule(s) for story {story_id}")
            for rule in story_post_comment_rules:
                # FIX ISSUE 1: Check if primary DM was already sent (in-memory only; sender_primary_dm_complete already handles DB exit)
                from app.services.pre_dm_handler import get_pre_dm_state
                rule_state = get_pre_dm_state(sender_id, rule.id)
                if rule_state.get("primary_dm_sent"):
                    log_print(f"🚫 [FIX] Skipping story rule {rule.id} - primary DM already sent to {sender_id}")
                    log_print(f"   💬 Message will be handled by real user, not automation")
                    continue
                
                log_print(f"🔄 [STORY DM] Processing story 'post_comment' rule: {rule.name or 'Story Rule'} → {rule.action_type}")
                log_print(f"✅ [STORY DM] Story 'post_comment' rule triggered for story {story_id}!")
                # Check if this rule is already being processed for this message
                processing_key = f"{message_id}_{rule.id}"
                if processing_key in _processing_rules:
                    print(f"🚫 Rule {rule.id} already processing for message {message_id}, skipping duplicate")
                    continue
                # Mark as processing
                _processing_rules[processing_key] = True
                # Clean cache if too large
                if len(_processing_rules) > _MAX_PROCESSING_CACHE_SIZE:
                    _processing_rules.clear()
                # Run in background task to avoid blocking webhook handler
                # Use trigger_type="story_reply" so pre-DM flow runs per Story separately from Post/Reel.
                # Pass incoming_message so "done"/email etc. in the Story thread are handled by process_pre_dm_actions.
                asyncio.create_task(execute_automation_action(
                    rule, 
                    sender_id, 
                    account, 
                    db,
                    trigger_type="story_reply",  # Isolate Story flow from post_comment (Post/Reel)
                    message_id=message_id,
                    incoming_message=message_text
                ))
                story_rule_matched = True
                break  # Only trigger first matching story rule
        
        # Then check if any keyword rule matches (exact match only)
        # If keyword rule matches, ONLY trigger that rule, skip new_message rules
        keyword_rule_matched = False
        for rule in keyword_rules:
            # For VIP + story reply: only process rules for this story (skip global keyword rules)
            if is_vip_user and story_id and (rule.media_id is None or str(rule.media_id or "") != story_id):
                continue
            if rule.config:
                # Check keywords array first (new format), fallback to single keyword (old format)
                keywords_list = []
                if rule.config.get("keywords") and isinstance(rule.config.get("keywords"), list):
                    keywords_list = [str(k).strip().lower() for k in rule.config.get("keywords") if k and str(k).strip()]
                elif rule.config.get("keyword"):
                    # Fallback to single keyword for backward compatibility
                    keywords_list = [str(rule.config.get("keyword", "")).strip().lower()]
                
                # Story trigger = same as comment: message must match keyword (no "any message" trigger)
                matched_keyword = None
                if keywords_list:
                    message_text_lower = message_text.strip().lower()
                    # Check if message is EXACTLY any of the keywords (case-insensitive)
                    # Also check if message CONTAINS the keyword (for flexibility)
                    for keyword in keywords_list:
                        keyword_clean = keyword.strip().lower()
                        message_clean = message_text_lower.strip()
                        
                        # Exact match (case-insensitive)
                        if keyword_clean == message_clean:
                            matched_keyword = keyword
                            log_print(f"✅ Keyword '{matched_keyword}' EXACTLY matches message '{message_text}'")
                            break
                        # Also check if message contains keyword as whole word (for flexibility)
                        elif keyword_clean in message_clean:
                            # Check if it's a whole word match (not part of another word)
                            import re
                            pattern = r'\b' + re.escape(keyword_clean) + r'\b'
                            if re.search(pattern, message_clean):
                                matched_keyword = keyword
                                log_print(f"✅ Keyword '{matched_keyword}' found as whole word in message '{message_text}'")
                                break
                
                # Trigger when message matches rule keyword (same as comment: keyword in comment vs keyword in story DM)
                if matched_keyword:
                    keyword_rule_matched = True
                    if story_id:
                        log_print(f"✅ [STORY DM] Keyword '{matched_keyword}' matches story reply. Rule: {rule.name} (ID: {rule.id})")
                    else:
                        log_print(f"✅ Keyword '{matched_keyword}' matches message, triggering keyword rule!")
                        log_print(f"   Message: '{message_text}' | Keyword: '{matched_keyword}' | Rule: {rule.name} (ID: {rule.id})")
                    
                    # For VIP: still run rule but only send primary DM (skip_growth_steps). Do not skip entirely.
                    from app.services.pre_dm_handler import get_pre_dm_state
                    rule_state = get_pre_dm_state(sender_id, rule.id)
                    if rule_state.get("primary_dm_sent") and not is_vip_user:
                        log_print(f"🚫 [FIX] Skipping keyword rule {rule.id} - primary DM already sent to {sender_id}")
                        log_print(f"   💬 Message will be handled by real user, not automation")
                        break  # Skip this keyword rule (non-VIP only)
                    
                    # Check if this rule is already being processed for this message
                    processing_key = f"{message_id}_{rule.id}"
                    if processing_key in _processing_rules:
                        print(f"🚫 Rule {rule.id} already processing for message {message_id}, skipping duplicate")
                        break
                    # Mark as processing
                    _processing_rules[processing_key] = True
                    # Clean cache if too large
                    if len(_processing_rules) > _MAX_PROCESSING_CACHE_SIZE:
                        _processing_rules.clear()
                    # Run in background task. For VIP: send primary DM only (skip_growth_steps).
                    trigger_for_action = "story_reply" if story_id else "keyword"
                    asyncio.create_task(execute_automation_action(
                        rule,
                        sender_id,
                        account,
                        db,
                        trigger_type=trigger_for_action,
                        message_id=message_id,
                        incoming_message=message_text if story_id else None,
                        skip_growth_steps=is_vip_user  # VIP: primary DM only
                    ))
                    break  # Only trigger first matching keyword rule
        
        if not keyword_rule_matched and len(keyword_rules) > 0:
            log_print(f"❌ [DM] No keyword rules matched the message: '{message_text}'")
                
        # Process new_message rules ONLY if no keyword rule matched AND no story rule matched
        if not keyword_rule_matched and not story_rule_matched:
            # Fallback: user may be replying in DMs to a follow/email request that was started from a comment (rule has media_id, so not in keyword_rules for regular DM). Route this message to the rule that has pending pre-DM state so "No" / "done" etc. get the retry flow.
            from app.services.pre_dm_handler import get_pre_dm_state
            for rule in all_active_rules:
                if not getattr(rule, "config", None):
                    continue
                cfg = rule.config or {}
                has_pre_dm = (
                    cfg.get("ask_to_follow") or cfg.get("askForFollow") or
                    cfg.get("ask_for_email") or cfg.get("askForEmail") or
                    cfg.get("enable_pre_dm_engagement") or
                    cfg.get("simple_dm_flow") or cfg.get("simpleDmFlow") or
                    cfg.get("simple_dm_flow_phone") or cfg.get("simpleDmFlowPhone")
                )
                if not has_pre_dm:
                    continue
                state = get_pre_dm_state(sender_id, rule.id)
                pending_follow = state.get("follow_request_sent") and not state.get("follow_confirmed")
                pending_email = state.get("email_request_sent") and not state.get("email_received")
                if pending_follow or pending_email:
                    log_print(f"📩 [DM] Routing message to rule {rule.id} ({rule.name}) — pending pre-DM reply (follow={pending_follow}, email={pending_email})")
                    processing_key = f"{message_id}_{rule.id}"
                    if processing_key not in _processing_rules:
                        _processing_rules[processing_key] = True
                        if len(_processing_rules) > _MAX_PROCESSING_CACHE_SIZE:
                            _processing_rules.clear()
                        asyncio.create_task(execute_automation_action(
                            rule,
                            sender_id,
                            account,
                            db,
                            trigger_type="new_message",
                            message_id=message_id,
                            incoming_message=message_text,
                        ))
                    return
            if len(new_message_rules) > 0:
                # STRICT MODE: Check if user is waiting for follow/email confirmation
                # If yes, ignore new_message rules to prevent unwanted triggers
                from app.services.pre_dm_handler import get_pre_dm_state
                user_in_waiting_state = False
                
                for rule in new_message_rules:
                    if rule.config.get("ask_to_follow") or rule.config.get("ask_for_email"):
                        state = get_pre_dm_state(sender_id, rule.id)
                        if state.get("follow_request_sent") and not state.get("follow_confirmed"):
                            log_print(f"⏳ [STRICT MODE] User {sender_id} waiting for follow - ignoring new_message rule")
                            user_in_waiting_state = True
                            break
                        if state.get("email_request_sent") and not state.get("email_received"):
                            log_print(f"⏳ [STRICT MODE] User {sender_id} waiting for email - ignoring new_message rule")
                            user_in_waiting_state = True
                            break
                
                if user_in_waiting_state:
                    log_print(f"🚫 [STRICT MODE] Skipping all new_message rules - user in waiting state")
                else:
                    log_print(f"🎯 [DM] Processing {len(new_message_rules)} 'new_message' rule(s)...")
                    for rule in new_message_rules:
                        # FIX ISSUE 1: Check if primary DM was already sent (in-memory only; sender_primary_dm_complete already handles DB exit)
                        from app.services.pre_dm_handler import get_pre_dm_state
                        rule_state = get_pre_dm_state(sender_id, rule.id)
                        if rule_state.get("primary_dm_sent"):
                            log_print(f"🚫 [FIX] Skipping rule {rule.id} - primary DM already sent to {sender_id}")
                            log_print(f"   💬 Message will be handled by real user, not automation")
                            continue
                        
                        log_print(f"🔄 [DM] Processing 'new_message' rule: {rule.name or 'New Message Rule'} → {rule.action_type}")
                        log_print(f"✅ [DM] 'new_message' rule triggered (no keyword match)!")
                        # Check if this rule is already being processed for this message
                        processing_key = f"{message_id}_{rule.id}"
                        if processing_key in _processing_rules:
                            log_print(f"🚫 Skipping execution: Rule {rule.id} already processing for message {message_id} (User {sender_id} already received this DM)")
                            continue
                        # Mark as processing
                        _processing_rules[processing_key] = True
                        # Clean cache if too large
                        if len(_processing_rules) > _MAX_PROCESSING_CACHE_SIZE:
                            _processing_rules.clear()
                        log_print(f"🚀 Executing automation action for new_message rule '{rule.name}' (Rule ID: {rule.id}, Sender: {sender_id})")
                        # Run in background task to avoid blocking webhook handler
                        asyncio.create_task(execute_automation_action(
                            rule, 
                            sender_id, 
                            account, 
                            db,
                            trigger_type="new_message",
                            message_id=message_id,
                            incoming_message=message_text  # Pass message text for lead capture processing
                        ))
            else:
                log_print(f"⚠️ [DM] No 'new_message' rules found to process. Keyword/story rule matched or no rules configured.", "WARNING")
        else:
            log_print(f"⏭️ [DM] Skipping 'new_message' rules because keyword/story rule matched")
                
    except Exception as e:
        print(f"❌ Error processing message: {str(e)}")
        import traceback
        traceback.print_exc()

async def process_postback_event(event: dict, db: Session):
    """Process button click (postback) events from Instagram."""
    try:
        sender_id = event.get("sender", {}).get("id")
        recipient_id = event.get("recipient", {}).get("id")
        postback = event.get("postback", {})
        payload = postback.get("payload", "")
        title = postback.get("title", "")
        
        print(f"🔘 Button clicked by {sender_id}: '{title}' (payload: {payload})")
        
        # Find Instagram account by recipient ID (the bot's account)
        account = db.query(InstagramAccount).filter(
            InstagramAccount.igsid == str(recipient_id),
            InstagramAccount.is_active == True
        ).first()
        
        if not account:
            # Fallback: use first active account
            account = db.query(InstagramAccount).filter(
                InstagramAccount.is_active == True
            ).first()
        
        if not account:
            print(f"❌ No active Instagram account found for postback")
            return
        
        print(f"✅ Found account: {account.username} (ID: {account.id})")
        
        # Handle "I'm following" button click (quick reply postback)
        # Payload format: "im_following_{rule_id}"
        if "im_following" in payload.lower() or ("i'm following" in title.lower() or "im following" in title.lower()):
            print(f"✅ [STRICT MODE] User clicked 'I'm following' button! Payload: {payload}, Title: {title}")
            
            # Extract rule_id from payload if present
            rule_id_from_payload = None
            if "im_following_" in payload:
                try:
                    rule_id_from_payload = int(payload.split("im_following_")[1])
                except (ValueError, IndexError):
                    pass
            
            # Find active rules for this account
            if rule_id_from_payload:
                rules = db.query(AutomationRule).filter(
                    AutomationRule.id == rule_id_from_payload,
                    AutomationRule.instagram_account_id == account.id,
                    AutomationRule.is_active == True
                ).all()
            else:
                rules = db.query(AutomationRule).filter(
                    AutomationRule.instagram_account_id == account.id,
                    AutomationRule.is_active == True
                ).all()
            
            for rule in rules:
                ask_for_email = rule.config.get("ask_for_email", False)
                ask_to_follow = rule.config.get("ask_to_follow", False)
                
                if ask_to_follow or ask_for_email:
                    # Mark follow as confirmed (user says they're already following)
                    from app.services.pre_dm_handler import update_pre_dm_state
                    update_pre_dm_state(str(sender_id), rule.id, {
                        "follow_confirmed": True,
                        "im_following_clicked": True,
                        "follow_request_sent": True
                    })
                    print(f"✅ Marked 'I'm following' confirmation for rule {rule.id}")
                    
                    # Update global audience record with following status (for VIP check across all automations)
                    try:
                        from app.services.global_conversion_check import update_audience_following
                        update_audience_following(db, str(sender_id), account.id, account.user_id, is_following=True)
                        print(f"✅ Follow status updated in global audience for {sender_id}")
                    except Exception as audience_err:
                        print(f"⚠️ Failed to update global audience with follow status: {str(audience_err)}")
                    
                    # Track analytics: "I'm following" button click
                    from app.services.lead_capture import update_automation_stats
                    update_automation_stats(rule.id, "im_following_clicked", db)
                    
                    # FIX ISSUE 3: Track follower gain count when user clicks "I'm following" button
                    try:
                        update_automation_stats(rule.id, "follower_gained", db)
                        print(f"✅ Follower gain count updated for rule {rule.id} (button click)")
                    except Exception as stats_err:
                        print(f"⚠️ Failed to update follower gain count: {str(stats_err)}")
                    
                    # Log analytics event for "I'm following" button click
                    try:
                        from app.utils.analytics import log_analytics_event_sync
                        from app.models.analytics_event import EventType
                        media_id = rule.config.get("media_id") if hasattr(rule, 'config') else None
                        log_analytics_event_sync(
                            db=db,
                            user_id=account.user_id,
                            event_type=EventType.IM_FOLLOWING_CLICKED,
                            rule_id=rule.id,
                            media_id=media_id,
                            instagram_account_id=account.id,
                            metadata={
                                "sender_id": sender_id,
                                "source": "im_following_button_click",
                                "clicked_at": datetime.utcnow().isoformat()
                            }
                        )
                        print(f"✅ Logged IM_FOLLOWING_CLICKED analytics event for rule {rule.id}")
                    except Exception as analytics_err:
                        print(f"⚠️ Failed to log IM_FOLLOWING_CLICKED event: {str(analytics_err)}")
                    
                    # Also track profile visit (user likely visited profile before clicking "I'm following")
                    # This compensates for not tracking URL button clicks directly
                    try:
                        from app.utils.analytics import log_analytics_event_sync
                        from app.models.analytics_event import EventType
                        log_analytics_event_sync(
                            db=db,
                            user_id=account.user_id,
                            event_type=EventType.PROFILE_VISIT,
                            rule_id=rule.id,
                            instagram_account_id=account.id,
                            metadata={
                                "source": "im_following_button_click",
                                "assumed_profile_visit": True
                            }
                        )
                        update_automation_stats(rule.id, "profile_visit", db)
                        print(f"✅ Tracked assumed profile visit (via 'I'm following' click)")
                    except Exception as analytics_err:
                        print(f"⚠️ Failed to track profile visit: {str(analytics_err)}")
                    
                    # If email is enabled, send email request immediately
                    if ask_for_email:
                        ask_for_email_message = rule.config.get("ask_for_email_message", "Quick question - what's your email? I'd love to send you something special! 📧")
                        
                        print(f"📧 [STRICT MODE] Sending email request immediately (I'm following button clicked)")
                        from app.utils.encryption import decrypt_credentials
                        from app.utils.instagram_api import send_dm
                        
                        try:
                            if account.encrypted_page_token:
                                access_token = decrypt_credentials(account.encrypted_page_token)
                            elif account.encrypted_credentials:
                                access_token = decrypt_credentials(account.encrypted_credentials)
                            else:
                                raise Exception("No access token found")
                            
                            page_id_for_dm = account.page_id
                            
                            send_dm(sender_id, ask_for_email_message, access_token, page_id_for_dm, buttons=None, quick_replies=None)
                            print(f"✅ Email request sent after 'I'm following' button click")
                            
                            # Log DM sent (tracks in DmLog and increments global tracker)
                            try:
                                from app.utils.plan_enforcement import log_dm_sent
                                log_dm_sent(
                                    user_id=account.user_id,
                                    instagram_account_id=account.id,
                                    recipient_username=str(sender_id),
                                    message=ask_for_email_message,
                                    db=db,
                                    instagram_username=account.username,
                                    instagram_igsid=getattr(account, "igsid", None)
                                )
                            except Exception as log_err:
                                print(f"⚠️ Failed to log DM: {str(log_err)}")
                            
                            update_pre_dm_state(str(sender_id), rule.id, {
                                "email_request_sent": True,
                                "step": "email",
                                "waiting_for_email_text": True
                            })
                        except Exception as e:
                            print(f"❌ Failed to send email request after 'I'm following' click: {str(e)}")
                    
                    # If no email configured, proceed directly to primary DM
                    else:
                        print(f"✅ Follow confirmed via 'I'm following' button, proceeding directly to primary DM")
                        asyncio.create_task(execute_automation_action(
                            rule,
                            sender_id,
                            account,
                            db,
                            trigger_type="postback",
                            pre_dm_result_override={"action": "send_primary"}
                        ))
                    
                    break  # Only process first matching rule
        
        # Handle "Visit Profile" button click (quick reply postback)
        # Payload format: "visit_profile_{rule_id}"
        elif "visit_profile" in payload.lower() or ("visit profile" in title.lower()):
            print(f"🔗 [STRICT MODE] User clicked 'Visit Profile' button! Payload: {payload}, Title: {title}")
            
            # Extract rule_id from payload if present
            rule_id_from_payload = None
            if "visit_profile_" in payload:
                try:
                    rule_id_from_payload = int(payload.split("visit_profile_")[1])
                except (ValueError, IndexError):
                    pass
            
            # Find active rules for this account
            if rule_id_from_payload:
                rules = db.query(AutomationRule).filter(
                    AutomationRule.id == rule_id_from_payload,
                    AutomationRule.instagram_account_id == account.id,
                    AutomationRule.is_active == True
                ).all()
            else:
                rules = db.query(AutomationRule).filter(
                    AutomationRule.instagram_account_id == account.id,
                    AutomationRule.is_active == True
                ).all()
            
            for rule in rules:
                ask_to_follow = rule.config.get("ask_to_follow", False)
                
                if ask_to_follow:
                    # Track profile visit
                    from app.services.pre_dm_handler import update_pre_dm_state
                    update_pre_dm_state(str(sender_id), rule.id, {
                        "profile_visited": True,
                        "profile_visit_time": str(asyncio.get_event_loop().time())
                    })
                    print(f"✅ Tracked profile visit for rule {rule.id}")
                    
                    # Track analytics: Profile visit button click
                    from app.services.lead_capture import update_automation_stats
                    update_automation_stats(rule.id, "profile_visit", db)
                    
                    # Send a simple reminder message WITHOUT URL to avoid link preview card
                    # (The original follow request message already contains the profile URL)
                    reminder_message = "Great! Once you've followed, click 'I'm following' or type 'done' to continue! 😊"
                    
                    from app.utils.encryption import decrypt_credentials
                    from app.utils.instagram_api import send_dm
                    
                    try:
                        if account.encrypted_page_token:
                            access_token = decrypt_credentials(account.encrypted_page_token)
                        elif account.encrypted_credentials:
                            access_token = decrypt_credentials(account.encrypted_credentials)
                        else:
                            raise Exception("No access token found")
                        
                        page_id_for_dm = account.page_id
                        
                        send_dm(sender_id, reminder_message, access_token, page_id_for_dm, buttons=None, quick_replies=None)
                        print(f"✅ Profile visit reminder sent")
                        
                        # Log DM sent (tracks in DmLog and increments global tracker)
                        try:
                            from app.utils.plan_enforcement import log_dm_sent
                            log_dm_sent(
                                user_id=account.user_id,
                                instagram_account_id=account.id,
                                recipient_username=str(sender_id),
                                message=reminder_message,
                                db=db,
                                instagram_username=account.username,
                                instagram_igsid=getattr(account, "igsid", None)
                            )
                        except Exception as log_err:
                            print(f"⚠️ Failed to log DM: {str(log_err)}")
                    except Exception as e:
                        print(f"❌ Failed to send profile visit reminder: {str(e)}")
                    
                    break  # Only process first matching rule
        
        # Handle "Follow Me" button click (quick reply postback)
        # Payload format: "follow_me_{rule_id}" or just contains "follow"
        elif "follow_me" in payload.lower() or ("follow" in payload.lower() and "follow" in title.lower()):
            print(f"👥 [STRICT MODE] User clicked 'Follow Me' button! Payload: {payload}, Title: {title}")
            
            # Extract rule_id from payload if present (format: "follow_me_{rule_id}")
            rule_id_from_payload = None
            if "follow_me_" in payload:
                try:
                    rule_id_from_payload = int(payload.split("follow_me_")[1])
                except (ValueError, IndexError):
                    pass
            
            # Find active rules for this account that have email requests enabled
            if rule_id_from_payload:
                # If we have rule_id from payload, use that specific rule
                rules = db.query(AutomationRule).filter(
                    AutomationRule.id == rule_id_from_payload,
                    AutomationRule.instagram_account_id == account.id,
                    AutomationRule.is_active == True
                ).all()
            else:
                # Fallback: find all active rules for this account
                rules = db.query(AutomationRule).filter(
                    AutomationRule.instagram_account_id == account.id,
                    AutomationRule.is_active == True
                ).all()
            
            # Find the rule that sent this follow button (check if ask_for_email is enabled)
            for rule in rules:
                ask_for_email = rule.config.get("ask_for_email", False)
                ask_to_follow = rule.config.get("ask_to_follow", False)
                
                # Only process if this rule has follow/email enabled
                if ask_to_follow or ask_for_email:
                    # Track the button click in stats
                    from app.services.lead_capture import update_automation_stats
                    update_automation_stats(rule.id, "follow_button_clicked", db)
                    print(f"📊 Tracked follow button click for rule {rule.id}")
                    
                    # Log analytics event for "Follow Me" button click
                    try:
                        from app.utils.analytics import log_analytics_event_sync
                        from app.models.analytics_event import EventType
                        media_id = rule.config.get("media_id") if hasattr(rule, 'config') else None
                        log_analytics_event_sync(
                            db=db,
                            user_id=account.user_id,
                            event_type=EventType.FOLLOW_BUTTON_CLICKED,
                            rule_id=rule.id,
                            media_id=media_id,
                            instagram_account_id=account.id,
                            metadata={
                                "sender_id": sender_id,
                                "source": "follow_me_button_click",
                                "clicked_at": datetime.utcnow().isoformat()
                            }
                        )
                        print(f"✅ Logged FOLLOW_BUTTON_CLICKED analytics event for rule {rule.id}")
                    except Exception as analytics_err:
                        print(f"⚠️ Failed to log FOLLOW_BUTTON_CLICKED event: {str(analytics_err)}")
                    
                    # Followers-only: "Follow Me" → ask "Are you following me?" with Yes/No (no primary until Yes)
                    if not ask_for_email:
                        from app.services.pre_dm_handler import update_pre_dm_state
                        raw = (rule.config or {}).get("follow_recheck_message") or (rule.config or {}).get("followRecheckMessage") or "Are you following me?"
                        follow_recheck_msg = normalize_follow_recheck_message(raw)
                        # Store how they entered so "No" shows Comment again vs Story reply again
                        _story_id = (event.get("message") or {}).get("reply_to", {}).get("story", {}).get("id")
                        _recheck_trigger = "story_reply" if _story_id else "post_comment"
                        update_pre_dm_state(str(sender_id), rule.id, {"follow_recheck_sent": True, "follow_recheck_trigger_type": _recheck_trigger})
                        from app.utils.encryption import decrypt_credentials
                        from app.utils.instagram_api import send_dm
                        try:
                            if account.encrypted_page_token:
                                _tok = decrypt_credentials(account.encrypted_page_token)
                            elif account.encrypted_credentials:
                                _tok = decrypt_credentials(account.encrypted_credentials)
                            else:
                                raise Exception("No access token found")
                            yes_no_quick_replies = [
                                {"content_type": "text", "title": "Yes", "payload": f"follow_recheck_yes_{rule.id}"},
                                {"content_type": "text", "title": "No", "payload": f"follow_recheck_no_{rule.id}"},
                            ]
                            send_dm(sender_id, follow_recheck_msg, _tok, account.page_id, buttons=None, quick_replies=yes_no_quick_replies)
                            print(f"📩 [FOLLOWERS] User clicked 'Follow Me' (postback) — sent 'Are you following me?' with Yes/No")
                            try:
                                from app.utils.plan_enforcement import log_dm_sent
                                log_dm_sent(user_id=account.user_id, instagram_account_id=account.id, recipient_username=str(sender_id), message=follow_recheck_msg, db=db, instagram_username=account.username, instagram_igsid=getattr(account, "igsid", None))
                            except Exception:
                                pass
                        except Exception as e:
                            print(f"❌ Failed to send follow recheck: {str(e)}")
                        break
                    
                    # Mark that follow button was clicked in state (Email/Phone flow)
                    from app.services.pre_dm_handler import update_pre_dm_state
                    update_pre_dm_state(str(sender_id), rule.id, {
                        "follow_button_clicked": True,
                        "follow_confirmed": True,
                        "follow_button_clicked_time": str(asyncio.get_event_loop().time())
                    })
                    print(f"✅ Marked follow button click for rule {rule.id}")
                    
                    # Update global audience record with following status (for VIP check across all automations)
                    try:
                        from app.services.global_conversion_check import update_audience_following
                        update_audience_following(db, str(sender_id), account.id, account.user_id, is_following=True)
                        print(f"✅ Follow status updated in global audience for {sender_id}")
                    except Exception as audience_err:
                        print(f"⚠️ Failed to update global audience with follow status: {str(audience_err)}")
                    
                    # STRICT MODE: If email is enabled, send email request immediately
                    if ask_for_email:
                        ask_for_email_message = rule.config.get("ask_for_email_message", "Quick question - what's your email? I'd love to send you something special! 📧")
                        
                        print(f"📧 [STRICT MODE] Sending email request immediately (Follow Me button clicked)")
                        from app.utils.encryption import decrypt_credentials
                        from app.utils.instagram_api import send_dm
                        
                        try:
                            # Get access token
                            if account.encrypted_page_token:
                                access_token = decrypt_credentials(account.encrypted_page_token)
                            elif account.encrypted_credentials:
                                access_token = decrypt_credentials(account.encrypted_credentials)
                            else:
                                raise Exception("No access token found")
                            
                            page_id_for_dm = account.page_id
                            
                            # Send email request as PLAIN TEXT (NO buttons or quick_replies)
                            send_dm(sender_id, ask_for_email_message, access_token, page_id_for_dm, buttons=None, quick_replies=None)
                            print(f"✅ Email request sent immediately after Follow Me button click")
                            
                            # Update state to mark email request as sent and waiting for typed email
                            update_pre_dm_state(str(sender_id), rule.id, {
                                "email_request_sent": True,
                                "step": "email",
                                "waiting_for_email_text": True  # NEW: Strict mode flag
                            })
                        except Exception as e:
                            print(f"❌ Failed to send email request: {str(e)}")
                    else:
                        # No email request, proceed directly to primary DM
                        print(f"✅ Follow confirmed via button click, proceeding to primary DM")
                        asyncio.create_task(execute_automation_action(
                            rule,
                            sender_id,
                            account,
                            db,
                            trigger_type="postback",
                            pre_dm_result_override={"action": "send_primary"}
                        ))
                    
                    break  # Only process first matching rule
        
    except Exception as e:
        print(f"❌ Error processing postback event: {str(e)}")
        import traceback
        traceback.print_exc()

async def process_comment_event(change: dict, igsid: str, db: Session):
    """Process comment on post/reel and trigger automation rules."""
    try:
        value = change.get("value", {})
        comment_id = value.get("id")
        commenter_id = value.get("from", {}).get("id")
        commenter_username = value.get("from", {}).get("username")
        comment_text = value.get("text", "")
        media_id = value.get("media", {}).get("id")
        
        print(f"💬 Comment from @{commenter_username} ({commenter_id}): {comment_text}")
        print(f"   Media ID: {media_id}, Comment ID: {comment_id}")
        
        # Find Instagram account by IGSID (from webhook entry.id)
        # This ensures correct account matching for multi-user scenarios
        from app.models.instagram_account import InstagramAccount
        print(f"🔍 Looking for Instagram account (IGSID from webhook: {igsid})")
        
        # First, try to match by IGSID (most accurate)
        account = db.query(InstagramAccount).filter(
            InstagramAccount.igsid == igsid,
            InstagramAccount.is_active == True
        ).first()
        
        if account:
            print(f"✅ Found account by IGSID: {account.username} (ID: {account.id}, User ID: {account.user_id})")
        else:
            # Fallback: If IGSID not stored, find account that has rules for this trigger
            print(f"⚠️ No account found by IGSID, trying smart fallback matching...")
            from app.models.automation_rule import AutomationRule
            
            # Find account that has active rules for this trigger type
            accounts_with_rules = db.query(InstagramAccount).join(AutomationRule).filter(
                InstagramAccount.is_active == True,
                AutomationRule.trigger_type == "post_comment",
                AutomationRule.is_active == True
            ).all()
            
            if accounts_with_rules:
                account = accounts_with_rules[0]
                print(f"✅ Found account with matching rules: {account.username} (ID: {account.id})")
                print(f"   NOTE: Re-connect via OAuth to store IGSID ({igsid}) for accurate matching")
            else:
                # Last resort: use first active account
                account = db.query(InstagramAccount).filter(
                    InstagramAccount.is_active == True
                ).first()
                if account:
                    print(f"⚠️ Using first active account: {account.username} (ID: {account.id})")
                    print(f"   NOTE: Re-connect Instagram account via OAuth to store IGSID ({igsid})")
        
        if not account:
            print(f"❌ No active Instagram accounts found")
            return
        
        print(f"✅ Found account: {account.username} (ID: {account.id})")
        
        # CRITICAL: Check if commenter is the bot itself (to prevent infinite loops)
        # When the bot replies to a comment, Instagram sends a webhook for that reply
        # We need to skip processing the bot's own comments
        commenter_id_str = str(commenter_id) if commenter_id else None
        commenter_username_lower = commenter_username.lower() if commenter_username else None
        account_igsid_str = str(account.igsid) if account.igsid else None
        account_username_lower = account.username.lower() if account.username else None
        igsid_str = str(igsid) if igsid else None
        
        # Check if commenter matches the account owner (by ID or username)
        is_bot_own_comment = False
        match_reason = None
        
        # Check by ID: commenter ID matches webhook entry ID (account's IGSID) or stored account IGSID
        if commenter_id_str and commenter_id_str == igsid_str:
            is_bot_own_comment = True
            match_reason = f"Commenter ID {commenter_id_str} matches webhook entry IGSID {igsid_str}"
        elif commenter_id_str and account_igsid_str and commenter_id_str == account_igsid_str:
            is_bot_own_comment = True
            match_reason = f"Commenter ID {commenter_id_str} matches stored account IGSID {account_igsid_str}"
        # Check by username (case-insensitive)
        elif commenter_username_lower and account_username_lower and commenter_username_lower == account_username_lower:
            is_bot_own_comment = True
            match_reason = f"Commenter username @{commenter_username} matches account username @{account.username}"
        
        if is_bot_own_comment:
            print(f"🚫 Ignoring bot's own comment/reply: {match_reason}")
            print(f"   This prevents infinite loops when the bot replies to comments")
            return
        
        # Debug: Show comparison values
        print(f"✅ Processing comment from external user:")
        print(f"   Commenter ID: {commenter_id_str}, Username: @{commenter_username}")
        print(f"   Account IGSID (stored): {account_igsid_str}, Webhook IGSID: {igsid_str}, Username: @{account.username}")
        
        # GLOBAL CONVERSION CHECK: Check if user is already converted (VIP) before processing any rules
        from app.services.global_conversion_check import check_global_conversion_status
        conversion_status = check_global_conversion_status(
            db, commenter_id_str, account.id, account.user_id,
            username=commenter_username
        )
        is_vip_user = conversion_status["is_converted"]
        
        if is_vip_user:
            print(f"⭐ [VIP USER] User {commenter_id_str} is already converted (email + phone + following). Skipping growth steps for all automations.")
            print(f"   Email: {conversion_status['has_email']}, Phone: {conversion_status.get('has_phone', False)}, Following: {conversion_status['is_following']}")
        
        # Find active automation rules for comments
        # We need to check BOTH:
        # 1. Rules with trigger_type='post_comment' (with optional keyword filtering)
        # 2. Rules with trigger_type='keyword' (if keyword matches comment text)
        # CRITICAL: Filter by media_id to only trigger rules for the specific post/reel
        from app.models.automation_rule import AutomationRule
        from sqlalchemy import or_
        
        # Convert media_id to string for comparison (both stored and incoming are strings)
        media_id_str = str(media_id) if media_id else None
        
        print(f"🔍 Filtering rules by media_id: {media_id_str}")
        
        # CRITICAL: Only trigger rules that match the specific media_id
        # Rules with media_id set should ONLY work on that specific post/reel
        # For strict matching: only include rules where media_id exactly matches
        # BUT: Also include rules with NO media_id (global rules) if media_id is provided
        if media_id_str:
            post_comment_rules = db.query(AutomationRule).filter(
            AutomationRule.instagram_account_id == account.id,
            AutomationRule.trigger_type == "post_comment",
                AutomationRule.is_active == True,
                or_(
                    AutomationRule.media_id == media_id_str,  # Rules for this specific media
                    AutomationRule.media_id.is_(None)  # Global rules (no media_id)
                )
        ).all()
        
            keyword_rules = db.query(AutomationRule).filter(
                AutomationRule.instagram_account_id == account.id,
                AutomationRule.trigger_type == "keyword",
                AutomationRule.is_active == True,
                or_(
                    AutomationRule.media_id == media_id_str,  # Rules for this specific media
                    AutomationRule.media_id.is_(None)  # Global rules (no media_id)
                )
            ).all()
        else:
            # If media_id is not provided in webhook, fallback to rules without media_id (backward compatibility)
            post_comment_rules = db.query(AutomationRule).filter(
                AutomationRule.instagram_account_id == account.id,
                AutomationRule.trigger_type == "post_comment",
                AutomationRule.is_active == True,
                AutomationRule.media_id.is_(None)
            ).all()
            
            keyword_rules = db.query(AutomationRule).filter(
                AutomationRule.instagram_account_id == account.id,
                AutomationRule.trigger_type == "keyword",
                AutomationRule.is_active == True,
                AutomationRule.media_id.is_(None)
            ).all()
        
        print(f"📋 After media_id filtering: Found {len(post_comment_rules)} 'post_comment' rules and {len(keyword_rules)} 'keyword' rules for media_id {media_id_str}")
        
        # SEND PUBLIC COMMENT REPLY IMMEDIATELY (before processing automation rules)
        # This ensures comment replies are sent right away, regardless of pre-DM flow
        comment_reply_sent = False
        
        # Helper function to get config value with camelCase and snake_case fallback
        def get_config_value(config, snake_key, camel_key=None, default=None):
            """Get config value checking both snake_case and camelCase formats for backward compatibility."""
            if camel_key is None:
                # Auto-generate camelCase from snake_case (e.g., "lead_auto_reply_to_comments" -> "leadAutoReplyToComments")
                parts = snake_key.split('_')
                camel_key = parts[0] + ''.join(word.capitalize() for word in parts[1:])
            # Check snake_case first (preferred), then camelCase, then default
            return config.get(snake_key) or config.get(camel_key) or default
        
        # Helper function to send comment reply for a rule
        def send_comment_reply_for_rule(rule, comment_id, commenter_id, account, db):
            """Send public comment reply for a rule if enabled."""
            if not rule.config:
                return False
            
            # Check if auto-reply to comments is enabled for this rule
            is_lead_capture = rule.config.get("is_lead_capture", False) or rule.config.get("isLeadCapture", False)
            
            if is_lead_capture:
                # Check both snake_case and camelCase formats for backward compatibility
                auto_reply_to_comments = (
                    get_config_value(rule.config, "lead_auto_reply_to_comments", default=False) or
                    get_config_value(rule.config, "auto_reply_to_comments", default=False)
                )
                comment_replies = (
                    get_config_value(rule.config, "lead_comment_replies", default=[]) or
                    get_config_value(rule.config, "comment_replies", default=[])
                )
            else:
                # Check both snake_case and camelCase formats for backward compatibility
                auto_reply_to_comments = (
                    get_config_value(rule.config, "simple_auto_reply_to_comments", default=False) or
                    get_config_value(rule.config, "auto_reply_to_comments", default=False)
                )
                comment_replies = (
                    get_config_value(rule.config, "simple_comment_replies", default=[]) or
                    get_config_value(rule.config, "comment_replies", default=[])
                )
            
            print(f"🔍 [COMMENT REPLY] Checking rule {rule.id}: auto_reply_to_comments={auto_reply_to_comments}, comment_replies={comment_replies}")
            
            # Check if we already replied to this comment
            from app.services.pre_dm_handler import was_comment_replied
            if was_comment_replied(str(commenter_id), rule.id, comment_id):
                print(f"⏭️ [COMMENT REPLY] Already replied to comment {comment_id} for rule {rule.id}, skipping")
                return False
            
            # Send public comment reply if enabled
            if not auto_reply_to_comments:
                print(f"⏭️ [COMMENT REPLY] Rule {rule.id}: auto_reply_to_comments is False")
                return False
            
            if not comment_replies or not isinstance(comment_replies, list):
                print(f"⏭️ [COMMENT REPLY] Rule {rule.id}: No comment_replies configured (type={type(comment_replies)})")
                return False
            
            valid_replies = [r for r in comment_replies if r and str(r).strip()]
            if not valid_replies:
                print(f"⏭️ [COMMENT REPLY] Rule {rule.id}: All comment_replies are empty after filtering")
                return False
            
            import random
            selected_reply = random.choice(valid_replies)
            print(f"💬 [IMMEDIATE] Sending public comment reply immediately for rule {rule.id}")
            print(f"   Comment ID: {comment_id}, Reply: {selected_reply[:50]}...")
            
            try:
                from app.utils.instagram_api import send_public_comment_reply
                from app.utils.encryption import decrypt_credentials
                
                # Get access token
                if account.encrypted_page_token:
                    access_token = decrypt_credentials(account.encrypted_page_token)
                elif account.encrypted_credentials:
                    access_token = decrypt_credentials(account.encrypted_credentials)
                else:
                    print(f"⚠️ [COMMENT REPLY] No access token found for account {account.id}")
                    return False
                
                send_public_comment_reply(comment_id, selected_reply, access_token)
                print(f"✅ Public comment reply sent immediately: {selected_reply[:50]}...")
                
                # Mark as replied
                from app.services.pre_dm_handler import mark_comment_replied
                mark_comment_replied(str(commenter_id), rule.id, comment_id)
                
                # Update stats
                from app.services.lead_capture import update_automation_stats
                update_automation_stats(rule.id, "comment_replied", db)
                
                # Log analytics
                try:
                    from app.utils.analytics import log_analytics_event_sync
                    from app.models.analytics_event import EventType
                    _mid = rule.config.get("media_id") if isinstance(rule.config, dict) else None
                    log_analytics_event_sync(
                        db=db, 
                        user_id=account.user_id, 
                        event_type=EventType.COMMENT_REPLIED, 
                        rule_id=rule.id, 
                        media_id=_mid, 
                        instagram_account_id=account.id, 
                        metadata={"comment_id": comment_id}
                    )
                except Exception as _ae:
                    pass
                
                return True
            except Exception as reply_error:
                print(f"⚠️ Failed to send public comment reply: {str(reply_error)}")
                import traceback
                traceback.print_exc()
                return False
        
        # First, check keyword rules (only if keyword matches)
        comment_text_lower = comment_text.strip().lower()
        for rule in keyword_rules:
            if comment_reply_sent:
                break
            
            if not rule.config:
                continue
            
            # Check if keyword matches
            keywords_list = []
            if rule.config.get("keywords") and isinstance(rule.config.get("keywords"), list):
                keywords_list = [str(k).strip().lower() for k in rule.config.get("keywords") if k and str(k).strip()]
            elif rule.config.get("keyword"):
                keywords_list = [str(rule.config.get("keyword", "")).strip().lower()]
            
            if keywords_list:
                # Check if comment matches any keyword
                keyword_matched = False
                for keyword in keywords_list:
                    keyword_clean = keyword.strip().lower()
                    comment_clean = comment_text_lower.strip()
                    
                    # Exact match (case-insensitive)
                    if keyword_clean == comment_clean:
                        keyword_matched = True
                        break
                    # Also check if comment contains keyword as whole word
                    elif keyword_clean in comment_clean:
                        import re
                        pattern = r'\b' + re.escape(keyword_clean) + r'\b'
                        if re.search(pattern, comment_clean):
                            keyword_matched = True
                            break
                
                if keyword_matched:
                    print(f"✅ [COMMENT REPLY] Keyword rule {rule.id} matches comment, checking for comment reply")
                    if send_comment_reply_for_rule(rule, comment_id, commenter_id, account, db):
                        comment_reply_sent = True
                        break
        
        # If no keyword rule sent reply, check post_comment rules
        if not comment_reply_sent:
            for rule in post_comment_rules:
                if comment_reply_sent:
                    break
                
                print(f"🔍 [COMMENT REPLY] Checking post_comment rule {rule.id} for comment reply")
                if send_comment_reply_for_rule(rule, comment_id, commenter_id, account, db):
                    comment_reply_sent = True
                    break
        
        if not comment_reply_sent:
            print(f"⏭️ [COMMENT REPLY] No comment reply sent for comment {comment_id} (no matching rules with comment replies enabled)")
        
        # DEBUG: Show all accounts and all rules for troubleshooting
        all_accounts = db.query(InstagramAccount).filter(InstagramAccount.is_active == True).all()
        print(f"🔍 DEBUG: All active Instagram accounts:")
        for acc in all_accounts:
            acc_rules = db.query(AutomationRule).filter(
                AutomationRule.instagram_account_id == acc.id,
                AutomationRule.is_active == True
            ).all()
            print(f"   - Account: {acc.username} (ID: {acc.id})")
            for rule in acc_rules:
                media_info = f" | Media ID: {rule.media_id}" if rule.media_id else " | Media ID: None (global)"
                print(f"     Rule: {rule.name or 'Unnamed'} | Trigger: {rule.trigger_type} | Active: {rule.is_active}{media_info}")
        
        # First, check if any keyword rule matches (exact match only)
        # If keyword rule matches, ONLY trigger that rule, skip post_comment rules
        keyword_rule_matched = False
        for rule in keyword_rules:
            if rule.config:
                # Check keywords array first (new format), fallback to single keyword (old format)
                keywords_list = []
                if rule.config.get("keywords") and isinstance(rule.config.get("keywords"), list):
                    keywords_list = [str(k).strip().lower() for k in rule.config.get("keywords") if k and str(k).strip()]
                elif rule.config.get("keyword"):
                    # Fallback to single keyword for backward compatibility
                    keywords_list = [str(rule.config.get("keyword", "")).strip().lower()]
                
                if keywords_list:
                    comment_text_lower = comment_text.strip().lower()
                    # Check if comment is EXACTLY any of the keywords (case-insensitive)
                    # Also check if comment CONTAINS the keyword (for flexibility)
                    matched_keyword = None
                    for keyword in keywords_list:
                        keyword_clean = keyword.strip().lower()
                        comment_clean = comment_text_lower.strip()
                        
                        # Exact match (case-insensitive)
                        if keyword_clean == comment_clean:
                            matched_keyword = keyword
                            print(f"✅ Keyword '{matched_keyword}' EXACTLY matches comment '{comment_text}'")
                            break
                        # Also check if comment contains keyword as whole word (for flexibility)
                        elif keyword_clean in comment_clean:
                            # Check if it's a whole word match (not part of another word)
                            import re
                            pattern = r'\b' + re.escape(keyword_clean) + r'\b'
                            if re.search(pattern, comment_clean):
                                matched_keyword = keyword
                                print(f"✅ Keyword '{matched_keyword}' found as whole word in comment '{comment_text}'")
                                break
                    
                    if matched_keyword:
                        keyword_rule_matched = True
                        print(f"✅ Keyword '{matched_keyword}' matches comment, triggering keyword rule!")
                        print(f"   Comment: '{comment_text}' | Keyword: '{matched_keyword}' | Rule: {rule.name} (ID: {rule.id})")
                        # Check if this rule is already being processed for this comment
                        processing_key = f"{comment_id}_{rule.id}"
                        if processing_key in _processing_rules:
                            print(f"🚫 Skipping execution: Rule {rule.id} already processing for comment {comment_id} (User {commenter_id} already received this DM)")
                            break
                        # Mark as processing
                        _processing_rules[processing_key] = True
                        if len(_processing_rules) > _MAX_PROCESSING_CACHE_SIZE:
                            _processing_rules.clear()
                        print(f"🚀 Executing automation action for keyword rule '{rule.name}' (Rule ID: {rule.id}, Commenter: {commenter_id})")
                        # Run in background task
                        asyncio.create_task(execute_automation_action(
                            rule,
                            commenter_id,
                            account,
                            db,
                            trigger_type="keyword",
                            comment_id=comment_id,
                            message_id=comment_id,  # Use comment_id as identifier
                            skip_growth_steps=is_vip_user  # Skip growth steps for VIP users
                        ))
                        break  # Only trigger first matching keyword rule
                    else:
                        # Log when keyword doesn't match (for debugging)
                        if keywords_list:  # Only log if we have keywords to check
                            print(f"🔍 Keyword check: '{comment_text_lower}' does not exactly match keyword '{keyword}' (Rule ID: {rule.id})")
        
        # Process post_comment rules ONLY if no keyword rule matched
        if not keyword_rule_matched:
            for rule in post_comment_rules:
                print(f"🔄 Processing 'post_comment' rule: {rule.name or 'Comment Rule'} → {rule.action_type}")
                print(f"✅ 'post_comment' rule triggered (no keyword match)!")
                # Check if this rule is already being processed for this comment
                processing_key = f"{comment_id}_{rule.id}"
                if processing_key in _processing_rules:
                    print(f"🚫 Skipping execution: Rule {rule.id} already processing for comment {comment_id} (User {commenter_id} already received this DM)")
                    continue
                # Mark as processing
                _processing_rules[processing_key] = True
                if len(_processing_rules) > _MAX_PROCESSING_CACHE_SIZE:
                    _processing_rules.clear()
                print(f"🚀 Executing automation action for post_comment rule '{rule.name}' (Rule ID: {rule.id}, Commenter: {commenter_id})")
                # Run in background task with error handling
                async def run_with_error_handling():
                    try:
                        await execute_automation_action(
                            rule, 
                            str(commenter_id), 
                            account,
                            db,
                            trigger_type="post_comment",
                            comment_id=comment_id,
                            message_id=comment_id,  # Use comment_id as identifier
                            incoming_message=comment_text,  # Pass comment text to check if it's an email
                            skip_growth_steps=is_vip_user  # Skip growth steps for VIP users
                        )
                    except Exception as e:
                        print(f"❌ [TASK ERROR] Error in execute_automation_action task: {str(e)}")
                        import traceback
                        traceback.print_exc()
                asyncio.create_task(run_with_error_handling())
        else:
            print(f"⏭️ Skipping 'post_comment' rules because keyword rule matched")
                
    except Exception as e:
        print(f"❌ Error processing comment event: {str(e)}")
        import traceback
        traceback.print_exc()

async def process_live_comment_event(change: dict, igsid: str, db: Session):
    """Process live video comment and trigger automation rules."""
    try:
        value = change.get("value", {})
        comment_id = value.get("id")
        commenter_id = value.get("from", {}).get("id")
        commenter_username = value.get("from", {}).get("username")
        comment_text = value.get("text", "")
        live_video_id = value.get("live_video_id")
        
        print(f"🎥 Live comment from @{commenter_username} ({commenter_id}): {comment_text}")
        print(f"   Live Video ID: {live_video_id}, Comment ID: {comment_id}")
        
        # GLOBAL CONVERSION CHECK: Check if user is already converted (VIP) before processing any rules
        # Note: We'll get account first, then check conversion status
        # Find Instagram account by IGSID (from webhook entry.id)
        # This ensures correct account matching for multi-user scenarios
        from app.models.instagram_account import InstagramAccount
        print(f"🔍 Looking for Instagram account (IGSID from webhook: {igsid})")
        
        # First, try to match by IGSID (most accurate)
        account = db.query(InstagramAccount).filter(
            InstagramAccount.igsid == igsid,
            InstagramAccount.is_active == True
        ).first()
        
        if account:
            print(f"✅ Found account by IGSID: {account.username} (ID: {account.id}, User ID: {account.user_id})")
        else:
            # Fallback: If IGSID not stored, find account that has rules for this trigger
            print(f"⚠️ No account found by IGSID, trying smart fallback matching...")
            from app.models.automation_rule import AutomationRule
            
            # Find account that has active rules for this trigger type
            accounts_with_rules = db.query(InstagramAccount).join(AutomationRule).filter(
                InstagramAccount.is_active == True,
                AutomationRule.trigger_type == "post_comment",
                AutomationRule.is_active == True
            ).all()
            
            if accounts_with_rules:
                account = accounts_with_rules[0]
                print(f"✅ Found account with matching rules: {account.username} (ID: {account.id})")
                print(f"   NOTE: Re-connect via OAuth to store IGSID ({igsid}) for accurate matching")
            else:
                # Last resort: use first active account
                account = db.query(InstagramAccount).filter(
                    InstagramAccount.is_active == True
                ).first()
                if account:
                    print(f"⚠️ Using first active account: {account.username} (ID: {account.id})")
                    print(f"   NOTE: Re-connect Instagram account via OAuth to store IGSID ({igsid})")
        
        if not account:
            print(f"❌ No active Instagram accounts found")
            return
        
        print(f"✅ Found account: {account.username} (ID: {account.id})")
        
        # CRITICAL: Check if commenter is the bot itself (to prevent infinite loops)
        # When the bot replies to a live comment, Instagram sends a webhook for that reply
        # We need to skip processing the bot's own comments
        commenter_id_str = str(commenter_id) if commenter_id else None
        commenter_username_lower = commenter_username.lower() if commenter_username else None
        account_igsid_str = str(account.igsid) if account.igsid else None
        account_username_lower = account.username.lower() if account.username else None
        igsid_str = str(igsid) if igsid else None
        
        # Check if commenter matches the account owner (by ID or username)
        is_bot_own_comment = False
        match_reason = None
        
        # Check by ID: commenter ID matches webhook entry ID (account's IGSID) or stored account IGSID
        if commenter_id_str and commenter_id_str == igsid_str:
            is_bot_own_comment = True
            match_reason = f"Commenter ID {commenter_id_str} matches webhook entry IGSID {igsid_str}"
        elif commenter_id_str and account_igsid_str and commenter_id_str == account_igsid_str:
            is_bot_own_comment = True
            match_reason = f"Commenter ID {commenter_id_str} matches stored account IGSID {account_igsid_str}"
        # Check by username (case-insensitive)
        elif commenter_username_lower and account_username_lower and commenter_username_lower == account_username_lower:
            is_bot_own_comment = True
            match_reason = f"Commenter username @{commenter_username} matches account username @{account.username}"
        
        if is_bot_own_comment:
            print(f"🚫 Ignoring bot's own live comment/reply: {match_reason}")
            print(f"   This prevents infinite loops when the bot replies to live comments")
            return
        
        # Debug: Show comparison values
        print(f"✅ Processing live comment from external user:")
        print(f"   Commenter ID: {commenter_id_str}, Username: @{commenter_username}")
        print(f"   Account IGSID (stored): {account_igsid_str}, Webhook IGSID: {igsid_str}, Username: @{account.username}")
        
        # GLOBAL CONVERSION CHECK: Check if user is already converted (VIP) before processing any rules
        from app.services.global_conversion_check import check_global_conversion_status
        conversion_status = check_global_conversion_status(
            db, commenter_id_str, account.id, account.user_id,
            username=commenter_username
        )
        is_vip_user = conversion_status["is_converted"]
        
        if is_vip_user:
            print(f"⭐ [VIP USER] User {commenter_id_str} is already converted (email + phone + following). Skipping growth steps for all automations.")
            print(f"   Email: {conversion_status['has_email']}, Phone: {conversion_status.get('has_phone', False)}, Following: {conversion_status['is_following']}")
        
        # Find active automation rules for live comments
        # We need to check BOTH:
        # 1. Rules with trigger_type='live_comment' (with optional keyword filtering)
        # 2. Rules with trigger_type='keyword' (if keyword matches comment text)
        # CRITICAL: Filter by live_video_id to only trigger rules for the specific live video
        from app.models.automation_rule import AutomationRule
        
        # Use live_video_id as media_id for filtering (live videos are also media)
        live_video_id_str = str(live_video_id) if live_video_id else None
        
        print(f"🔍 Filtering live comment rules by live_video_id: {live_video_id_str}")
        
        # CRITICAL: Only trigger rules that match the specific live_video_id
        # BUT: Also include rules with NO media_id (global rules) if live_video_id is provided
        if live_video_id_str:
            live_comment_rules = db.query(AutomationRule).filter(
            AutomationRule.instagram_account_id == account.id,
            AutomationRule.trigger_type == "live_comment",
                AutomationRule.is_active == True,
                or_(
                    AutomationRule.media_id == live_video_id_str,  # Rules for this specific live video
                    AutomationRule.media_id.is_(None)  # Global rules (no media_id)
                )
        ).all()
        
            keyword_rules = db.query(AutomationRule).filter(
                AutomationRule.instagram_account_id == account.id,
                AutomationRule.trigger_type == "keyword",
                AutomationRule.is_active == True,
                or_(
                    AutomationRule.media_id == live_video_id_str,  # Rules for this specific live video
                    AutomationRule.media_id.is_(None)  # Global rules (no media_id)
                )
            ).all()
        else:
            # If live_video_id is not provided, fallback to rules without media_id (backward compatibility)
            live_comment_rules = db.query(AutomationRule).filter(
                AutomationRule.instagram_account_id == account.id,
                AutomationRule.trigger_type == "live_comment",
                AutomationRule.is_active == True,
                AutomationRule.media_id.is_(None)
            ).all()
            
            keyword_rules = db.query(AutomationRule).filter(
                AutomationRule.instagram_account_id == account.id,
                AutomationRule.trigger_type == "keyword",
                AutomationRule.is_active == True,
                AutomationRule.media_id.is_(None)
            ).all()
        
        print(f"📋 After live_video_id filtering: Found {len(live_comment_rules)} 'live_comment' rules and {len(keyword_rules)} 'keyword' rules for live_video_id {live_video_id_str}")
        
        print(f"📋 Found {len(live_comment_rules)} 'live_comment' rules and {len(keyword_rules)} 'keyword' rules for this account")
        
        # First, check if any keyword rule matches (exact match only)
        # If keyword rule matches, ONLY trigger that rule, skip live_comment rules
        keyword_rule_matched = False
        for rule in keyword_rules:
            if rule.config:
                # Check keywords array first (new format), fallback to single keyword (old format)
                keywords_list = []
                if rule.config.get("keywords") and isinstance(rule.config.get("keywords"), list):
                    keywords_list = [str(k).strip().lower() for k in rule.config.get("keywords") if k and str(k).strip()]
                elif rule.config.get("keyword"):
                    # Fallback to single keyword for backward compatibility
                    keywords_list = [str(rule.config.get("keyword", "")).strip().lower()]
                
                if keywords_list:
                    comment_text_lower = comment_text.strip().lower()
                    # Check if comment is EXACTLY any of the keywords (case-insensitive)
                    # Also check if comment CONTAINS the keyword (for flexibility)
                    matched_keyword = None
                    for keyword in keywords_list:
                        keyword_clean = keyword.strip().lower()
                        comment_clean = comment_text_lower.strip()
                        
                        # Exact match (case-insensitive)
                        if keyword_clean == comment_clean:
                            matched_keyword = keyword
                            print(f"✅ Keyword '{matched_keyword}' EXACTLY matches live comment '{comment_text}'")
                            break
                        # Also check if comment contains keyword as whole word (for flexibility)
                        elif keyword_clean in comment_clean:
                            # Check if it's a whole word match (not part of another word)
                            import re
                            pattern = r'\b' + re.escape(keyword_clean) + r'\b'
                            if re.search(pattern, comment_clean):
                                matched_keyword = keyword
                                print(f"✅ Keyword '{matched_keyword}' found as whole word in live comment '{comment_text}'")
                                break
                    
                    if matched_keyword:
                        keyword_rule_matched = True
                        print(f"✅ Keyword '{matched_keyword}' matches live comment, triggering keyword rule!")
                        print(f"   Comment: '{comment_text}' | Keyword: '{matched_keyword}' | Rule: {rule.name} (ID: {rule.id})")
                        # Check if this rule is already being processed for this comment
                        processing_key = f"{comment_id}_{rule.id}"
                        if processing_key in _processing_rules:
                            print(f"🚫 Skipping execution: Rule {rule.id} already processing for live comment {comment_id} (User {commenter_id} already received this DM)")
                            break
                        # Mark as processing
                        _processing_rules[processing_key] = True
                        if len(_processing_rules) > _MAX_PROCESSING_CACHE_SIZE:
                            _processing_rules.clear()
                        print(f"🚀 Executing automation action for live keyword rule '{rule.name}' (Rule ID: {rule.id}, Commenter: {commenter_id})")
                        # Run in background task
                        asyncio.create_task(execute_automation_action(
                            rule,
                            str(commenter_id),
                            account,
                            db,
                            trigger_type="keyword",
                            comment_id=comment_id,
                            message_id=comment_id,  # Use comment_id as identifier
                            skip_growth_steps=is_vip_user  # Skip growth steps for VIP users
                        ))
                        break  # Only trigger first matching keyword rule
        
        # Process live_comment rules ONLY if no keyword rule matched
        if not keyword_rule_matched:
            for rule in live_comment_rules:
                print(f"🔄 Processing 'live_comment' rule: {rule.name or 'Live Comment Rule'} → {rule.action_type}")
                print(f"✅ 'live_comment' rule triggered (no keyword match)!")
                # Check if this rule is already being processed for this comment
                processing_key = f"{comment_id}_{rule.id}"
                if processing_key in _processing_rules:
                    print(f"🚫 Skipping execution: Rule {rule.id} already processing for live comment {comment_id} (User {commenter_id} already received this DM)")
                    continue
                # Mark as processing
                _processing_rules[processing_key] = True
                if len(_processing_rules) > _MAX_PROCESSING_CACHE_SIZE:
                    _processing_rules.clear()
                print(f"🚀 Executing automation action for live_comment rule '{rule.name}' (Rule ID: {rule.id}, Commenter: {commenter_id})")
                # Run in background task
                asyncio.create_task(execute_automation_action(
                    rule,
                    str(commenter_id),
                    account,
                    db,
                    trigger_type="live_comment",
                    comment_id=comment_id,
                    message_id=comment_id,  # Use comment_id as identifier
                    skip_growth_steps=is_vip_user  # Skip growth steps for VIP users
                ))
        else:
            print(f"⏭️ Skipping 'live_comment' rules because keyword rule matched")
                
    except Exception as e:
        print(f"❌ Error processing live comment event: {str(e)}")
        import traceback
        traceback.print_exc()

async def execute_automation_action(
    rule: AutomationRule,
    sender_id: str,
    account: InstagramAccount,
    db: Session,
    trigger_type: str = None,
    comment_id: str = None,
    message_id: str = None,
    pre_dm_result_override: dict = None,  # Optional: pre-computed pre-DM result to avoid re-processing
    incoming_message: str = None,  # For story/DM: user's text (follow confirmation, email, etc.)
    skip_growth_steps: bool = False  # If True, skip follow/email steps and go directly to primary DM
):
    """
    Execute the automation action defined in the rule.

    Args:
        rule: The automation rule to execute
        sender_id: The user ID who triggered the action (recipient for DMs)
        account: The Instagram account to use
        db: Database session (request-scoped; we use a task-scoped session inside for background tasks)
        trigger_type: The type of trigger (e.g., 'post_comment', 'new_message', 'live_comment')
        comment_id: The comment ID (required for post_comment triggers to use private_replies)
        message_id: The message or comment ID (used for deduplication cache cleanup)
    """
    _rule_id = None
    _db_task = None
    try:
        # Log function entry FIRST - before accessing any attributes
        print(f"🔍 [EXECUTE] ✅ FUNCTION CALLED - Sender: {sender_id}, Trigger: {trigger_type}")

        # Check if rule and account are valid BEFORE accessing attributes
        if not rule:
            print(f"❌ [EXECUTE] Rule is None!")
            return
        if not account:
            print(f"❌ [EXECUTE] Account is None!")
            return

        # Extract IDs before any await (request session still open). Background tasks receive
        # rule/account/db from the request; once the request ends the session closes and
        # those objects become detached. We create a task-scoped session and re-fetch.
        try:
            _rule_id = int(rule.id)
            _account_id = int(account.id)
        except Exception as e:
            print(f"❌ [EXECUTE] Failed to get rule/account IDs: {e}")
            return

        _db_task = SessionLocal()
        rule = _db_task.query(AutomationRule).filter(AutomationRule.id == _rule_id).first()
        account = _db_task.query(InstagramAccount).filter(InstagramAccount.id == _account_id).first()
        if not rule or not account:
            print(f"❌ [EXECUTE] Rule or account not found after re-fetch (rule_id={_rule_id}, account_id={_account_id})")
            _db_task.close()
            return
        db = _db_task

        # Now safe to access attributes (rule/account are bound to task session)
        try:
            rule_id_val = rule.id
            action_type_val = rule.action_type
            print(f"🔍 [EXECUTE] Rule ID: {rule_id_val}, Action: {action_type_val}")
        except Exception as attr_error:
            print(f"❌ [EXECUTE] Error accessing rule attributes: {str(attr_error)}")
            return

        if rule.action_type == "send_dm":
            # IMPORTANT: Store all needed attributes from account and rule BEFORE any async operations
            # This prevents DetachedInstanceError when objects are passed across async boundaries
            try:
                user_id = account.user_id
                account_id = account.id
                username = account.username
                account_igsid = getattr(account, "igsid", None)
                rule_id = rule.id
                print(f"🔍 [EXECUTE] Stored values - user_id: {user_id}, account_id: {account_id}, username: {username}, rule_id: {rule_id}")
                
                # Flow-type aware VIP: only skip growth steps when we have what THIS rule needs
                from app.services.pre_dm_handler import get_pre_dm_state
                rule_state = get_pre_dm_state(str(sender_id), rule_id)
                if skip_growth_steps:
                    cfg = rule.config or {}
                    simple_dm_flow_phone = cfg.get("simple_dm_flow_phone", False) or cfg.get("simpleDmFlowPhone", False)
                    simple_dm_flow = cfg.get("simple_dm_flow", False) or cfg.get("simpleDmFlow", False)
                    ask_to_follow = cfg.get("ask_to_follow", False) or cfg.get("askToFollow", False)
                    # Phone flow: only skip if we have phone for this account+sender (any rule) — matches VIP / pre_dm_handler
                    if simple_dm_flow_phone:
                        from app.models.captured_lead import CapturedLead
                        from sqlalchemy import cast
                        from sqlalchemy.dialects.postgresql import JSONB
                        lead_with_phone = db.query(CapturedLead).filter(
                            CapturedLead.instagram_account_id == account_id,
                            CapturedLead.phone.isnot(None),
                            cast(CapturedLead.extra_metadata, JSONB)["sender_id"].astext == str(sender_id),
                        ).first()
                        if not (lead_with_phone and lead_with_phone.phone and str(lead_with_phone.phone).strip()):
                            skip_growth_steps = False
                            print(f"⭐ [VIP] Rule {rule_id} is phone flow but no phone for this sender — will ask for phone (not skipping growth steps)")
                    # Follower flow: only skip if follow confirmed for this rule+sender (email+phone from before ≠ VIP for follower rule)
                    is_follower_flow = ask_to_follow and not simple_dm_flow and not simple_dm_flow_phone
                    if is_follower_flow and not rule_state.get("follow_confirmed", False):
                        skip_growth_steps = False
                        print(f"⭐ [VIP] Rule {rule_id} is follower flow but follow not confirmed for this sender — will ask for follow (not skipping growth steps)")
                
                # FIX ISSUE 1: Check if primary DM was already sent BEFORE any processing
                # This prevents primary DM from being re-triggered when user sends random text
                # BUT: If pre_dm_result_override has send_email_success=True, we need to send success message first
                # BUT: For VIP users commenting again (post_comment/live_comment) → reply to comment + primary DM only, no early exit
                should_send_email_success_first = (
                    pre_dm_result_override and 
                    isinstance(pre_dm_result_override, dict) and
                    pre_dm_result_override.get("send_email_success", False) and
                    not skip_growth_steps
                )
                # VIP triggering again: skip early-return if VIP AND (comment or story) trigger — send primary DM only
                # Comment: post_comment, live_comment, keyword (has comment_id). Story: story_reply (DM reply to story).
                is_comment_trigger = comment_id and trigger_type in ("post_comment", "live_comment", "keyword")
                is_story_trigger = trigger_type == "story_reply"
                vip_comment_again = skip_growth_steps and (is_comment_trigger or is_story_trigger)
                print(f"🔍 [EMAIL SUCCESS CHECK] primary_dm_sent={rule_state.get('primary_dm_sent')}, should_send_email_success_first={should_send_email_success_first}, vip_comment_again={vip_comment_again} (skip_growth_steps={skip_growth_steps}, trigger_type={trigger_type}, comment_id={comment_id})")
                
                if rule_state.get("primary_dm_sent") and not should_send_email_success_first and not vip_comment_again:
                    # Primary DM was already sent - check if lead capture flow is also completed
                    is_lead_capture = rule.config.get("is_lead_capture", False) or rule.config.get("isLeadCapture", False)
                    # FIX ISSUE 1: Check for simple reply rules (not lead capture)
                    # Helper to get config with camelCase/snake_case fallback
                    def get_cfg(key_snake, key_camel=None, default=None):
                        if key_camel is None:
                            parts = key_snake.split('_')
                            key_camel = parts[0] + ''.join(word.capitalize() for word in parts[1:])
                        return rule.config.get(key_snake) or rule.config.get(key_camel) or default
                    
                    is_simple_reply = not is_lead_capture and (
                        get_cfg("simple_auto_reply_to_comments", default=False) or 
                        get_cfg("auto_reply_to_comments", default=False) or
                        rule.config.get("message_template") or rule.config.get("messageTemplate") or
                        rule.config.get("message_variations") or rule.config.get("messageVariations")
                    )
                    has_incoming_message = incoming_message and incoming_message.strip()
                    
                    # When we skip due to primary_dm_sent/lead captured, still reply to COMMENT triggers so user sees "Check your DMs!"
                    is_comment_trigger_here = comment_id and trigger_type in ("post_comment", "live_comment", "keyword")
                    def _send_check_dms_reply_if_comment():
                        if not is_comment_trigger_here or not comment_id:
                            return
                        import random
                        cfg = rule.config or {}
                        # Use only UI-configured messages; no hardcoded default
                        _msg = cfg.get("check_dms_comment_reply") or cfg.get("checkDmsCommentReply")
                        if not _msg or not str(_msg).strip():
                            lead_dm = cfg.get("lead_dm_messages") or cfg.get("leadDmMessages") or []
                            if isinstance(lead_dm, list) and lead_dm:
                                valid = [m for m in lead_dm if m and str(m).strip()]
                                if valid:
                                    _msg = random.choice(valid)
                            if not _msg or not str(_msg).strip():
                                variations = cfg.get("message_variations") or cfg.get("messageVariations") or []
                                if isinstance(variations, list) and variations:
                                    valid = [m for m in variations if m and str(m).strip()]
                                    if valid:
                                        _msg = random.choice(valid)
                            if not _msg or not str(_msg).strip():
                                _msg = (cfg.get("message_template") or cfg.get("messageTemplate") or "").strip() or None
                        if not _msg or not str(_msg).strip():
                            print(f"⏭️ [COMMENT AGAIN] No configured message for comment-again reply (rule {rule_id}), skipping")
                            return
                        try:
                            from app.utils.encryption import decrypt_credentials
                            from app.utils.instagram_api import send_private_reply
                            if account.encrypted_page_token:
                                _tok = decrypt_credentials(account.encrypted_page_token)
                            elif account.encrypted_credentials:
                                _tok = decrypt_credentials(account.encrypted_credentials)
                            else:
                                return
                            _page_id = account.page_id
                            send_private_reply(comment_id, _msg, _tok, _page_id, quick_replies=None)
                            print(f"✅ [COMMENT AGAIN] Sent reply to comment {comment_id} using UI config (primary DM already sent)")
                        except Exception as _e:
                            print(f"⚠️ Failed to send comment-again reply: {_e}")
                    
                    # Check if lead was already captured for this sender and rule (match current flow type)
                    # So switching rule from email → phone still asks for phone; phone → email still asks for email
                    lead_already_captured = False
                    if is_lead_capture:
                        from app.models.captured_lead import CapturedLead
                        from sqlalchemy import cast
                        from sqlalchemy.dialects.postgresql import JSONB
                        existing_lead = db.query(CapturedLead).filter(
                            CapturedLead.automation_rule_id == rule_id,
                            CapturedLead.instagram_account_id == account_id,
                            cast(CapturedLead.extra_metadata, JSONB)['sender_id'].astext == str(sender_id)
                        ).first()
                        if existing_lead:
                            cfg = rule.config or {}
                            simple_dm_flow_phone = cfg.get("simple_dm_flow_phone", False) or cfg.get("simpleDmFlowPhone", False)
                            simple_dm_flow = cfg.get("simple_dm_flow", False) or cfg.get("simpleDmFlow", False)
                            ask_for_email = cfg.get("ask_for_email", False) or cfg.get("askForEmail", False)
                            if simple_dm_flow_phone:
                                lead_already_captured = bool(existing_lead.phone and str(existing_lead.phone).strip())
                            elif simple_dm_flow or ask_for_email:
                                lead_already_captured = bool(existing_lead.email and str(existing_lead.email).strip())
                            else:
                                lead_already_captured = True
                            if lead_already_captured:
                                print(f"✅ Lead already captured for sender {sender_id} and rule {rule_id} (matches current flow type)")
                    
                    # FIX: After primary DM completion, stop ALL automation - let real users handle it
                    # For simple reply rules, if primary DM was sent, don't re-trigger
                    if is_simple_reply:
                        print(f"🚫 [FIX ISSUE 1] Simple reply rule {rule_id} - primary DM already sent, skipping to prevent re-triggering")
                        print(f"   trigger_type={trigger_type}, has_incoming_message={has_incoming_message}")
                        print(f"   💬 Message will be handled by real user, not automation")
                        _send_check_dms_reply_if_comment()
                        return  # Exit early - don't send any messages
                    
                    # FIX: For lead capture rules, if lead is already captured, stop automation completely
                    if is_lead_capture and lead_already_captured:
                        print(f"🚫 [FIX] Lead capture rule {rule_id} - lead already captured, stopping automation")
                        print(f"   💬 All further messages will be handled by real user, not automation")
                        _send_check_dms_reply_if_comment()
                        return  # Exit early - don't send any messages
                    
                    # Only allow processing if it's a lead capture flow AND there's an incoming message AND lead not yet captured
                    if is_lead_capture and has_incoming_message and not lead_already_captured:
                        print(f"📧 [LEAD CAPTURE] Primary DM already sent, but processing incoming message for lead capture flow")
                        # Continue to process lead capture flow
                    else:
                        # Not a lead capture flow OR no incoming message OR lead already captured - skip completely
                        if lead_already_captured:
                            print(f"🚫 [FIX] Lead already captured for rule {rule_id}, skipping to prevent any messages")
                            print(f"   💬 Message will be handled by real user, not automation")
                        else:
                            print(f"🚫 [FIX ISSUE 1] Primary DM already sent for rule {rule_id}, skipping to prevent re-triggering")
                            print(f"   💬 Message will be handled by real user, not automation")
                        print(f"   trigger_type={trigger_type}, is_lead_capture={is_lead_capture}, has_incoming_message={has_incoming_message}, lead_already_captured={lead_already_captured}")
                        _send_check_dms_reply_if_comment()
                        return  # Exit early - don't send any messages
                
                # Log TRIGGER_MATCHED analytics event
                try:
                    from app.utils.analytics import log_analytics_event_sync
                    from app.models.analytics_event import EventType
                    # Get media_id from rule if available
                    media_id = rule.config.get("media_id") if hasattr(rule, 'config') else None
                    log_analytics_event_sync(
                        db=db,
                        user_id=user_id,
                        event_type=EventType.TRIGGER_MATCHED,
                        rule_id=rule_id,
                        media_id=media_id,
                        instagram_account_id=account_id,
                        metadata={
                            "trigger_type": trigger_type,
                            "sender_id": sender_id,
                            "comment_id": comment_id
                        }
                    )
                except Exception as analytics_err:
                    print(f"⚠️ Failed to log TRIGGER_MATCHED event: {str(analytics_err)}")
            except Exception as e:
                print(f"❌ [EXECUTE] Error accessing account/rule attributes: {str(e)}")
                import traceback
                traceback.print_exc()
                # Try to refresh the objects
                try:
                    db.refresh(account)
                    db.refresh(rule)
                    user_id = account.user_id
                    account_id = account.id
                    username = account.username
                    account_igsid = getattr(account, "igsid", None)
                    rule_id = rule.id
                except Exception as refresh_error:
                    print(f"❌ Failed to refresh account/rule: {str(refresh_error)}")
                return
            
            # Check monthly DM limit BEFORE sending
            from app.utils.plan_enforcement import check_dm_limit
            if not check_dm_limit(user_id, db, instagram_account_id=account_id):
                print(f"⚠️ Monthly DM limit reached for user {user_id} on account {account_id}. Skipping DM send.")
                return  # Don't send DM if limit reached
            
            # Initialize message_template
            message_template = None
            
            # Pre-DM: use rule_cfg (single ref) so UnboundLocalError cannot occur
            rule_cfg = rule.config or {}
            enable_pre_dm_engagement = rule_cfg.get("enable_pre_dm_engagement")
            if enable_pre_dm_engagement is not None:
                ask_to_follow = enable_pre_dm_engagement
                ask_for_email = rule_cfg.get("ask_for_email", enable_pre_dm_engagement)
            else:
                ask_to_follow = rule_cfg.get("ask_to_follow", False)
                ask_for_email = rule_cfg.get("ask_for_email", False)
            pre_dm_result = pre_dm_result_override
            _run_pre_dm = (
                ask_to_follow or ask_for_email or
                rule_cfg.get("simple_dm_flow") or rule_cfg.get("simpleDmFlow") or
                rule_cfg.get("simple_dm_flow_phone") or rule_cfg.get("simpleDmFlowPhone")
            ) and pre_dm_result is None
            print(f"🔍 [DEBUG] Pre-DM check: ask_to_follow={ask_to_follow}, ask_for_email={ask_for_email}, simple_dm_flow={rule_cfg.get('simple_dm_flow') or rule_cfg.get('simpleDmFlow')}, simple_dm_flow_phone={rule_cfg.get('simple_dm_flow_phone') or rule_cfg.get('simpleDmFlowPhone')}, run_pre_dm={_run_pre_dm}, pre_dm_result={pre_dm_result}")
            
            # If override says "send_primary", skip all pre-DM processing
            # BUT: If skip_growth_steps is False, we might still need to check for email success message
            if pre_dm_result and pre_dm_result.get("action") == "send_primary":
                # Direct primary DM - skip to primary DM logic
                print(f"✅ Skipping pre-DM actions, proceeding directly to primary DM")
                print(f"🔍 [DEBUG] pre_dm_result_override: {pre_dm_result}, send_email_success={pre_dm_result.get('send_email_success', False)}")
                # For VIP users (skip_growth_steps=True), don't send email success message
                # For non-VIP users, check if email was just provided (send_email_success flag should be in override)
                if skip_growth_steps:
                    # VIP user - ensure email success is not sent
                    if "send_email_success" not in pre_dm_result:
                        pre_dm_result["send_email_success"] = False
                else:
                    # Non-VIP user - ensure send_email_success flag is preserved from override
                    if "send_email_success" not in pre_dm_result:
                        # If not in override, check if email was provided
                        if pre_dm_result.get("email"):
                            pre_dm_result["send_email_success"] = True
                            print(f"✅ [FIX] Set send_email_success=True for non-VIP user with email")
                # pre_dm_result already set to override, continue to primary DM logic below
            elif _run_pre_dm:
                print(f"🔍 [DEBUG] Processing pre-DM actions: ask_to_follow={ask_to_follow}, ask_for_email={ask_for_email}, skip_growth_steps={skip_growth_steps}")
                # Process pre-DM actions (unless override is provided)
                # CRITICAL: If skip_growth_steps=True (VIP user), process_pre_dm_actions will return send_primary immediately
                from app.services.pre_dm_handler import process_pre_dm_actions, normalize_follow_recheck_message
                
                pre_dm_result = await process_pre_dm_actions(
                    rule, sender_id, account, db,
                    trigger_type=trigger_type,
                    incoming_message=incoming_message,
                    skip_growth_steps=skip_growth_steps
                )
                
                # Log the result for debugging
                if pre_dm_result:
                    print(f"🔍 [DEBUG] Pre-DM result: action={pre_dm_result.get('action')}, send_email_success={pre_dm_result.get('send_email_success', False)}")
                
                # v2 Use Case 1: Re-engagement follow check — one question "Are you following me?" (not full first-time flow)
                if pre_dm_result and pre_dm_result["action"] == "send_reengagement_follow_check":
                    from app.services.pre_dm_handler import update_pre_dm_state
                    reengagement_msg = normalize_follow_recheck_message(pre_dm_result.get("message") or "Are you following me?")
                    update_pre_dm_state(str(sender_id), rule_id, {"follow_request_sent": True, "step": "follow", "follow_recheck_trigger_type": trigger_type})
                    if comment_id:
                        update_pre_dm_state(str(sender_id), rule_id, {"comment_id": comment_id})
                    from app.utils.instagram_api import send_dm as send_dm_api
                    from app.utils.encryption import decrypt_credentials
                    try:
                        if account.encrypted_page_token:
                            access_token = decrypt_credentials(account.encrypted_page_token)
                        elif account.encrypted_credentials:
                            access_token = decrypt_credentials(account.encrypted_credentials)
                        else:
                            raise Exception("No access token found")
                        page_id_for_dm = account.page_id
                        follow_quick_reply = [
                            {"content_type": "text", "title": "I'm following", "payload": f"im_following_{rule_id}"},
                            {"content_type": "text", "title": "Follow Me 👆", "payload": f"follow_me_{rule_id}"}
                        ]
                        is_comment_trigger = comment_id and trigger_type in ["post_comment", "keyword", "live_comment"]
                        if is_comment_trigger:
                            from app.utils.instagram_api import send_private_reply
                            send_private_reply(comment_id, reengagement_msg, access_token, page_id_for_dm, quick_replies=follow_quick_reply)
                            print(f"✅ [v2 Use Case 1] Re-engagement follow check sent via private reply")
                        else:
                            send_dm_api(str(sender_id), reengagement_msg, access_token, page_id_for_dm, buttons=None, quick_replies=follow_quick_reply)
                            print(f"✅ [v2 Use Case 1] Re-engagement follow check sent via DM")
                        try:
                            from app.utils.plan_enforcement import log_dm_sent
                            log_dm_sent(user_id=user_id, instagram_account_id=account_id, recipient_username=str(sender_id), message=reengagement_msg, db=db, instagram_username=username, instagram_igsid=account_igsid)
                        except Exception:
                            pass
                    except Exception as e:
                        print(f"❌ Failed to send re-engagement follow check: {str(e)}")
                    return

                # Followers-only: "No" → exit message (no primary DM); next comment/story reply will ask "Are you following me?" again
                if pre_dm_result and pre_dm_result["action"] == "send_follow_no_exit":
                    _default_exit = "No problem! Story reply again anytime when you'd like the guide. 📩" if trigger_type == "story_reply" else "No problem! Comment again anytime when you'd like the guide. 📩"
                    exit_msg = pre_dm_result.get("message", _default_exit)
                    from app.services.pre_dm_handler import update_pre_dm_state
                    update_pre_dm_state(str(sender_id), rule_id, {"follow_recheck_sent": False, "follow_exit_sent": True, "follow_request_sent": True})
                    from app.utils.instagram_api import send_dm as send_dm_api
                    from app.utils.encryption import decrypt_credentials
                    try:
                        if account.encrypted_page_token:
                            _tok = decrypt_credentials(account.encrypted_page_token)
                        elif account.encrypted_credentials:
                            _tok = decrypt_credentials(account.encrypted_credentials)
                        else:
                            raise Exception("No access token found")
                        page_id_for_dm = account.page_id
                        if comment_id and trigger_type in ["post_comment", "keyword", "live_comment"]:
                            from app.utils.instagram_api import send_private_reply
                            send_private_reply(comment_id, exit_msg, _tok, page_id_for_dm, quick_replies=None)
                        else:
                            send_dm_api(str(sender_id), exit_msg, _tok, page_id_for_dm, buttons=None, quick_replies=None)
                        print(f"📩 [FOLLOWERS] Exit message sent (no primary DM)")
                        try:
                            from app.utils.plan_enforcement import log_dm_sent
                            log_dm_sent(user_id=user_id, instagram_account_id=account_id, recipient_username=str(sender_id), message=exit_msg, db=db, instagram_username=username, instagram_igsid=account_igsid)
                        except Exception:
                            pass
                    except Exception as e:
                        print(f"❌ Failed to send exit message: {str(e)}")
                    return

                # Followers re-comment or "Follow me" / "No" → "Are you following me?" with Yes/No only
                if pre_dm_result and pre_dm_result["action"] == "send_follow_recheck":
                    follow_recheck_msg = normalize_follow_recheck_message(pre_dm_result.get("message") or "Are you following me?")
                    from app.utils.instagram_api import send_dm as send_dm_api
                    from app.utils.encryption import decrypt_credentials
                    from app.services.pre_dm_handler import update_pre_dm_state
                    try:
                        if account.encrypted_page_token:
                            _tok = decrypt_credentials(account.encrypted_page_token)
                        elif account.encrypted_credentials:
                            _tok = decrypt_credentials(account.encrypted_credentials)
                        else:
                            raise Exception("No access token found")
                        page_id_for_dm = account.page_id
                        yes_no_quick_replies = [
                            {"content_type": "text", "title": "Yes", "payload": f"follow_recheck_yes_{rule_id}"},
                            {"content_type": "text", "title": "No", "payload": f"follow_recheck_no_{rule_id}"},
                        ]
                        if comment_id and trigger_type in ["post_comment", "keyword", "live_comment"]:
                            from app.utils.instagram_api import send_private_reply
                            send_private_reply(comment_id, follow_recheck_msg, _tok, page_id_for_dm, quick_replies=yes_no_quick_replies)
                        else:
                            send_dm_api(str(sender_id), follow_recheck_msg, _tok, page_id_for_dm, buttons=None, quick_replies=yes_no_quick_replies)
                        print(f"✅ 'Are you following me?' sent with Yes/No buttons")
                        # Store how they entered so "No" quick reply can show Comment again vs Story reply again
                        update_pre_dm_state(str(sender_id), rule_id, {"follow_recheck_trigger_type": trigger_type})
                        try:
                            from app.utils.plan_enforcement import log_dm_sent
                            log_dm_sent(user_id=user_id, instagram_account_id=account_id, recipient_username=str(sender_id), message=follow_recheck_msg, db=db, instagram_username=username, instagram_igsid=account_igsid)
                        except Exception:
                            pass
                    except Exception as e:
                        print(f"❌ Failed to send follow recheck: {str(e)}")
                    return

                # Simple flow: one combined message (follow + email ask), text only, no quick replies
                if pre_dm_result and pre_dm_result["action"] == "send_simple_flow_start":
                    simple_msg = pre_dm_result.get("message", "Follow me to get the guide 👇 Reply with your email and I'll send it! 📧")
                    from app.utils.instagram_api import send_dm as send_dm_api
                    from app.utils.encryption import decrypt_credentials
                    try:
                        if account.encrypted_page_token:
                            access_token = decrypt_credentials(account.encrypted_page_token)
                        elif account.encrypted_credentials:
                            access_token = decrypt_credentials(account.encrypted_credentials)
                        else:
                            raise Exception("No access token found")
                        page_id_for_dm = account.page_id
                        is_comment_trigger = comment_id and trigger_type in ["post_comment", "keyword", "live_comment"]
                        if is_comment_trigger:
                            from app.utils.instagram_api import send_private_reply
                            send_private_reply(comment_id, simple_msg, access_token, page_id_for_dm, quick_replies=None)
                            print(f"✅ [Simple flow] Start message sent via private reply")
                        else:
                            send_dm_api(str(sender_id), simple_msg, access_token, page_id_for_dm, buttons=None, quick_replies=None)
                            print(f"✅ [Simple flow] Start message sent via DM")
                        try:
                            from app.utils.plan_enforcement import log_dm_sent
                            log_dm_sent(user_id=user_id, instagram_account_id=account_id, recipient_username=str(sender_id), message=simple_msg, db=db, instagram_username=username, instagram_igsid=account_igsid)
                        except Exception:
                            pass
                    except Exception as e:
                        print(f"❌ Failed to send simple flow start: {str(e)}")
                    return

                # Simple flow (Phone): one combined message (follow + phone ask), text only
                if pre_dm_result and pre_dm_result["action"] == "send_simple_flow_start_phone":
                    simple_phone_msg = pre_dm_result.get("message", "Follow me to get the guide 👇 Reply with your phone number and I'll send it! 📱")
                    from app.utils.instagram_api import send_dm as send_dm_api
                    from app.utils.encryption import decrypt_credentials
                    try:
                        if account.encrypted_page_token:
                            access_token = decrypt_credentials(account.encrypted_page_token)
                        elif account.encrypted_credentials:
                            access_token = decrypt_credentials(account.encrypted_credentials)
                        else:
                            raise Exception("No access token found")
                        page_id_for_dm = account.page_id
                        is_comment_trigger = comment_id and trigger_type in ["post_comment", "keyword", "live_comment"]
                        if is_comment_trigger:
                            from app.utils.instagram_api import send_private_reply
                            send_private_reply(comment_id, simple_phone_msg, access_token, page_id_for_dm, quick_replies=None)
                            print(f"✅ [Simple flow Phone] Start message sent via private reply")
                        else:
                            send_dm_api(str(sender_id), simple_phone_msg, access_token, page_id_for_dm, buttons=None, quick_replies=None)
                            print(f"✅ [Simple flow Phone] Start message sent via DM")
                        try:
                            from app.utils.plan_enforcement import log_dm_sent
                            log_dm_sent(user_id=user_id, instagram_account_id=account_id, recipient_username=str(sender_id), message=simple_phone_msg, db=db, instagram_username=username, instagram_igsid=account_igsid)
                        except Exception:
                            pass
                    except Exception as e:
                        print(f"❌ Failed to send simple flow phone start: {str(e)}")
                    return

                # Phone flow: user sent email or invalid input — send retry and stop (no primary DM)
                if pre_dm_result and pre_dm_result["action"] == "send_phone_retry":
                    retry_msg = pre_dm_result.get("message", "") or "We need your phone number for this, not your email. 📱 Please reply with your phone number!"
                    print(f"⚠️ [PHONE FLOW] Rejecting email/invalid input, sending phone retry (action=send_phone_retry)")
                    from app.utils.encryption import decrypt_credentials
                    from app.utils.instagram_api import send_dm as send_dm_api
                    try:
                        if account.encrypted_page_token:
                            _tok = decrypt_credentials(account.encrypted_page_token)
                        elif account.encrypted_credentials:
                            _tok = decrypt_credentials(account.encrypted_credentials)
                        else:
                            raise Exception("No access token")
                        send_dm_api(str(sender_id), retry_msg, _tok, account.page_id, buttons=None, quick_replies=None)
                        print(f"✅ Phone retry message sent")
                    except Exception as e:
                        print(f"⚠️ Failed to send phone retry: {e}")
                    return

                # Email flow: user sent phone or invalid input — send retry and stop (no primary DM)
                if pre_dm_result and pre_dm_result["action"] == "send_email_retry":
                    retry_msg = pre_dm_result.get("message", "") or "That doesn't look like a valid email. 🤔 Please share your correct email so I can send you the guide! 📧"
                    print(f"⚠️ [EMAIL FLOW] Rejecting phone/invalid input, sending email retry (action=send_email_retry)")
                    from app.utils.encryption import decrypt_credentials
                    from app.utils.instagram_api import send_dm as send_dm_api
                    try:
                        if account.encrypted_page_token:
                            _tok = decrypt_credentials(account.encrypted_page_token)
                        elif account.encrypted_credentials:
                            _tok = decrypt_credentials(account.encrypted_credentials)
                        else:
                            raise Exception("No access token")
                        send_dm_api(str(sender_id), retry_msg, _tok, account.page_id, buttons=None, quick_replies=None)
                        print(f"✅ Email retry message sent")
                    except Exception as e:
                        print(f"⚠️ Failed to send email retry: {e}")
                    return

                if pre_dm_result and pre_dm_result["action"] == "send_follow_request":
                    # STRICT MODE: Send follow request with text-based confirmation (most reliable)
                    follow_message = pre_dm_result["message"]
                    
                    # FIXED: Do NOT include Instagram URL to avoid unwanted @username preview bubble
                    # Instagram automatically creates a rich preview/embed for Instagram URLs,
                    # which shows "@username" in a separate message bubble (the issue user reported)
                    # Instead, just ask them to follow with clear instructions
                    follow_message_with_instructions = f"{follow_message}\n\n✅ Once you've followed, type 'done' or 'followed' to continue!"
                    
                    # NO buttons - just plain text message (most reliable approach)
                    # User will type "done", "followed", "yes" etc. to confirm
                    
                    # STRICT MODE: If email is enabled, send follow request, then WAIT for text confirmation
                    if ask_for_email:
                        # Get email request message (will be sent ONLY after button click)
                        ask_for_email_message = rule.config.get("ask_for_email_message", "Quick question - what's your email? I'd love to send you something special! 📧")
                        
                        # Mark only follow as sent (NOT email yet)
                        from app.services.pre_dm_handler import update_pre_dm_state
                        state_updates = {
                            "follow_request_sent": True,
                            "step": "follow",
                            "waiting_for_follow_confirmation": True  # NEW: Strict mode flag
                        }
                        # Store comment_id in state if available (for comment triggers)
                        if comment_id:
                            state_updates["comment_id"] = comment_id
                            print(f"💾 [COMMENT ID] Storing comment_id in pre-DM state: {comment_id}")
                        update_pre_dm_state(str(sender_id), rule_id, state_updates)
                        
                        # Change action to signal we'll send follow, then WAIT for button click
                        pre_dm_result["action"] = "send_follow_strict_mode"
                        
                        # Store messages for later use
                        pre_dm_result["follow_message"] = follow_message_with_instructions
                        pre_dm_result["email_message"] = ask_for_email_message
                        
                        # Send follow request
                        print(f"📩 [STRICT MODE] Sending follow request with text confirmation to {sender_id}")
                        print(f"   ⚠️ Email question will ONLY be sent after user types 'done' or 'followed'")
                        print(f"   🚫 No timeouts - waiting indefinitely for user confirmation")
                        
                        # Get access token NOW before sending
                        from app.utils.instagram_api import send_dm as send_dm_api
                        from app.utils.encryption import decrypt_credentials
                        
                        try:
                            if account.encrypted_page_token:
                                access_token = decrypt_credentials(account.encrypted_page_token)
                                page_id_for_dm = account.page_id
                                print(f"✅ Using OAuth page token for sending pre-DM messages")
                            elif account.encrypted_credentials:
                                access_token = decrypt_credentials(account.encrypted_credentials)
                                page_id_for_dm = account.page_id
                            else:
                                raise Exception("No access token found for account")
                        except Exception as e:
                            try:
                                db.refresh(account)
                                if account.encrypted_page_token:
                                    access_token = decrypt_credentials(account.encrypted_page_token)
                                    page_id_for_dm = account.page_id
                                elif account.encrypted_credentials:
                                    access_token = decrypt_credentials(account.encrypted_credentials)
                                    page_id_for_dm = account.page_id
                                else:
                                    raise Exception("No access token found for account")
                            except Exception as refresh_error:
                                print(f"❌ Failed to get access token: {str(refresh_error)}")
                                return
                        
                        # Check if comment-based trigger for private reply
                        is_comment_trigger = comment_id and trigger_type in ["post_comment", "keyword", "live_comment"]
                        
                        # Build Instagram profile URL for "Visit Profile" button
                        # Use direct Instagram URL - Instagram's in-app browser will open it in native app automatically
                        # No tracking URL needed - Instagram handles the native app opening
                        profile_url_direct = f"https://www.instagram.com/{username}"
                        
                        # Use direct Instagram URL (no tracking) - Instagram in-app browser opens native app automatically
                        profile_url = profile_url_direct
                        
                        # Build URL button for "Visit Profile" (enables navigation to bio page)
                        # Note: URL buttons require generic template format (card layout)
                        # Direct Instagram URL opens in native app when clicked from Instagram's in-app browser
                        visit_profile_button = [{
                            "text": "Visit Profile",
                            "url": profile_url  # Direct Instagram URL - opens native app automatically
                        }]
                        
                        # Build quick reply buttons for "I'm following" and "Follow Me"
                        # These can track clicks and work with plain text messages
                        follow_quick_reply = [
                            {
                                "content_type": "text",
                                "title": "I'm following",
                                "payload": f"im_following_{rule_id}"  # Mark as already following
                            },
                            {
                                "content_type": "text",
                                "title": "Follow Me 👆",
                                "payload": f"follow_me_{rule_id}"  # Include rule_id for tracking
                            }
                        ]
                        
                        # STRICT MODE: Send follow request with URL button for "Visit Profile" and quick replies for others
                        if is_comment_trigger:
                            print(f"💬 Opening conversation via private reply (comment trigger)")
                            try:
                                from app.utils.instagram_api import send_private_reply
                                # Send minimal opener to open conversation (bypasses 24-hour window)
                                opener_message = "Hi! 👋"
                                send_private_reply(comment_id, opener_message, access_token, page_id_for_dm)
                                print(f"✅ Conversation opened via private reply")
                                
                                # Small delay to ensure conversation is open
                                await asyncio.sleep(1)
                                
                                # CRITICAL FIX: Use private reply for follow-up messages too (bypasses 24-hour window)
                                # Instagram treats private replies (comment_id) differently from regular DMs (sender_id)
                                # We must continue using comment_id for all messages to avoid "outside allowed window" errors
                                try:
                                    # Send follow message as private reply (plain text, no URL buttons - Instagram private replies don't support URL buttons)
                                    # Include profile URL in message text since buttons aren't supported
                                    # Combine follow message with profile URL and quick reply prompt
                                    follow_with_quick_replies = f"{follow_message_with_instructions}\n\n🔗 Visit my profile: {profile_url}\n\nClick one of the options below:"
                                    follow_sent = False
                                    try:
                                        send_private_reply(comment_id, follow_with_quick_replies, access_token, page_id_for_dm, quick_replies=follow_quick_reply)
                                        follow_sent = True
                                        print(f"✅ Follow request sent via private reply with quick replies (bypasses 24-hour window)")
                                    except Exception as qr_err:
                                        # Meta often returns OAuthException code 1 for quick_replies on private reply; fallback to text-only so flow continues and primary DM (with media URL) can be sent
                                        print(f"⚠️ Private reply with quick_replies failed ({str(qr_err)}), retrying as text-only...")
                                        send_private_reply(comment_id, follow_with_quick_replies, access_token, page_id_for_dm, quick_replies=None)
                                        follow_sent = True
                                        print(f"✅ Follow request sent via private reply (text-only fallback)")
                                    if not follow_sent:
                                        raise RuntimeError("Follow request private reply failed")
                                    
                                    # Log DM sent (tracks in DmLog and increments global tracker)
                                    try:
                                        from app.utils.plan_enforcement import log_dm_sent
                                        log_dm_sent(
                                            user_id=account.user_id,
                                            instagram_account_id=account.id,
                                            recipient_username=str(sender_id),
                                            message=follow_with_quick_replies,
                                            db=db,
                                            instagram_username=account.username,
                                            instagram_igsid=getattr(account, "igsid", None)
                                        )
                                    except Exception as log_err:
                                        print(f"⚠️ Failed to log DM: {str(log_err)}")
                                    
                                    # Send public comment reply IMMEDIATELY after follow-up message (not waiting for email)
                                    if comment_id:
                                        is_lead_capture = rule.config.get("is_lead_capture", False) or rule.config.get("isLeadCapture", False)
                                        
                                        # Determine which comment reply fields to use based on rule type
                                        # Helper to get config with camelCase/snake_case fallback
                                        def get_cfg_val(key_snake, key_camel=None, default=None):
                                            if key_camel is None:
                                                parts = key_snake.split('_')
                                                key_camel = parts[0] + ''.join(word.capitalize() for word in parts[1:])
                                            return rule.config.get(key_snake) or rule.config.get(key_camel) or default
                                        
                                        if is_lead_capture:
                                            auto_reply_to_comments = get_cfg_val("lead_auto_reply_to_comments", default=False) or get_cfg_val("auto_reply_to_comments", default=False)
                                            comment_replies = get_cfg_val("lead_comment_replies", default=[]) or get_cfg_val("comment_replies", default=[])
                                        else:
                                            auto_reply_to_comments = get_cfg_val("simple_auto_reply_to_comments", default=False) or get_cfg_val("auto_reply_to_comments", default=False)
                                            comment_replies = get_cfg_val("simple_comment_replies", default=[]) or get_cfg_val("comment_replies", default=[])
                                        
                                        if auto_reply_to_comments and comment_replies and isinstance(comment_replies, list):
                                            valid_replies = [r for r in comment_replies if r and str(r).strip()]
                                            if valid_replies:
                                                import random
                                                selected_reply = random.choice(valid_replies)
                                                print(f"💬 [IMMEDIATE] Sending public comment reply immediately after follow-up message")
                                                try:
                                                    from app.utils.instagram_api import send_public_comment_reply
                                                    send_public_comment_reply(comment_id, selected_reply, access_token)
                                                    print(f"✅ Public comment reply sent immediately: {selected_reply[:50]}...")
                                                    
                                                    from app.services.pre_dm_handler import mark_comment_replied
                                                    mark_comment_replied(str(sender_id), rule_id, comment_id)
                                                    from app.services.lead_capture import update_automation_stats
                                                    update_automation_stats(rule.id, "comment_replied", db)
                                                    try:
                                                        from app.utils.analytics import log_analytics_event_sync
                                                        from app.models.analytics_event import EventType
                                                        _mid = rule.config.get("media_id") if isinstance(getattr(rule, "config", None), dict) else None
                                                        log_analytics_event_sync(db=db, user_id=account.user_id, event_type=EventType.COMMENT_REPLIED, rule_id=rule.id, media_id=_mid, instagram_account_id=account.id, metadata={"comment_id": comment_id})
                                                    except Exception as _ae:
                                                        pass
                                                except Exception as reply_error:
                                                    print(f"⚠️ Failed to send immediate comment reply: {str(reply_error)}")
                                except Exception as btn_error:
                                    print(f"⚠️ Could not send follow message via private reply: {str(btn_error)}")
                                    print(f"🔄 This should not happen - private replies bypass 24-hour window. Error: {str(btn_error)}")
                                    # Note: If private reply fails, there's no fallback - this is unexpected
                                    # The error will be logged and flow will wait for user interaction
                            except Exception as e:
                                print(f"❌ Failed to send follow request: {str(e)}")
                        else:
                            try:
                                # Send full message (same format as FE: base + instructions + profile link + prompt) with quick replies
                                follow_with_full_format = f"{follow_message_with_instructions}\n\n🔗 Visit my profile: {profile_url}\n\nClick one of the options below:"
                                send_dm_api(sender_id, follow_with_full_format, access_token, page_id_for_dm, buttons=None, quick_replies=follow_quick_reply)
                                print(f"✅ Follow request sent (same format as FE: base + profile link + quick replies)")
                                
                                # Log DM sent
                                try:
                                    from app.utils.plan_enforcement import log_dm_sent
                                    log_dm_sent(
                                        user_id=account.user_id,
                                        instagram_account_id=account.id,
                                        recipient_username=str(sender_id),
                                        message=follow_with_full_format,
                                        db=db,
                                        instagram_username=account.username,
                                        instagram_igsid=getattr(account, "igsid", None)
                                    )
                                except Exception as log_err:
                                    print(f"⚠️ Failed to log DM: {str(log_err)}")
                                
                                # Send public comment reply IMMEDIATELY after follow-up message (not waiting for email)
                                if comment_id:
                                    is_lead_capture = rule.config.get("is_lead_capture", False) or rule.config.get("isLeadCapture", False)
                                    
                                    # Determine which comment reply fields to use based on rule type
                                    # Helper to get config with camelCase/snake_case fallback
                                    def get_cfg_val(key_snake, key_camel=None, default=None):
                                        if key_camel is None:
                                            parts = key_snake.split('_')
                                            key_camel = parts[0] + ''.join(word.capitalize() for word in parts[1:])
                                        return rule.config.get(key_snake) or rule.config.get(key_camel) or default
                                    
                                    if is_lead_capture:
                                        auto_reply_to_comments = get_cfg_val("lead_auto_reply_to_comments", default=False) or get_cfg_val("auto_reply_to_comments", default=False)
                                        comment_replies = get_cfg_val("lead_comment_replies", default=[]) or get_cfg_val("comment_replies", default=[])
                                    else:
                                        auto_reply_to_comments = get_cfg_val("simple_auto_reply_to_comments", default=False) or get_cfg_val("auto_reply_to_comments", default=False)
                                        comment_replies = get_cfg_val("simple_comment_replies", default=[]) or get_cfg_val("comment_replies", default=[])
                                    
                                    if auto_reply_to_comments and comment_replies and isinstance(comment_replies, list):
                                        valid_replies = [r for r in comment_replies if r and str(r).strip()]
                                        if valid_replies:
                                            import random
                                            selected_reply = random.choice(valid_replies)
                                            print(f"💬 [IMMEDIATE] Sending public comment reply immediately after follow-up message")
                                            try:
                                                from app.utils.instagram_api import send_public_comment_reply
                                                send_public_comment_reply(comment_id, selected_reply, access_token)
                                                print(f"✅ Public comment reply sent immediately: {selected_reply[:50]}...")
                                                
                                                from app.services.pre_dm_handler import mark_comment_replied
                                                mark_comment_replied(str(sender_id), rule_id, comment_id)
                                                from app.services.lead_capture import update_automation_stats
                                                update_automation_stats(rule.id, "comment_replied", db)
                                                try:
                                                    from app.utils.analytics import log_analytics_event_sync
                                                    from app.models.analytics_event import EventType
                                                    _mid = rule.config.get("media_id") if isinstance(getattr(rule, "config", None), dict) else None
                                                    log_analytics_event_sync(db=db, user_id=account.user_id, event_type=EventType.COMMENT_REPLIED, rule_id=rule.id, media_id=_mid, instagram_account_id=account.id, metadata={"comment_id": comment_id})
                                                except Exception as _ae:
                                                    pass
                                            except Exception as reply_error:
                                                print(f"⚠️ Failed to send immediate comment reply: {str(reply_error)}")
                            except Exception as e:
                                print(f"❌ Failed to send follow request: {str(e)}")
                        
                        # STRICT MODE: No automatic scheduling
                        # Email will ONLY be sent when postback event (button click) is received
                        # Primary DM will ONLY be sent after valid email is provided
                        print(f"✅ [STRICT MODE] Follow request sent. Flow:")
                        print(f"   1️⃣ Waiting for user to click 'Follow Me' button")
                        print(f"   2️⃣ Email question will be sent ONLY after button click")
                        print(f"   3️⃣ Primary DM will be sent ONLY after valid email provided")
                        print(f"   🚫 No timeouts - strictly waiting for user actions")
                        
                        return  # Done - wait for postback event
                    else:
                        # Followers-only: send first question DM (follow request with "I'm following" / "Follow Me" buttons)
                        from app.services.pre_dm_handler import update_pre_dm_state
                        from app.utils.instagram_api import send_dm as send_dm_api
                        from app.utils.encryption import decrypt_credentials
                        state_updates = {"follow_request_sent": True, "step": "follow", "follow_recheck_trigger_type": trigger_type}
                        if comment_id:
                            state_updates["comment_id"] = comment_id
                        update_pre_dm_state(str(sender_id), rule_id, state_updates)
                        follow_quick_reply = [
                            {"content_type": "text", "title": "I'm following", "payload": f"im_following_{rule_id}"},
                            {"content_type": "text", "title": "Follow Me 👆", "payload": f"follow_me_{rule_id}"},
                        ]
                        try:
                            if account.encrypted_page_token:
                                access_token = decrypt_credentials(account.encrypted_page_token)
                                page_id_for_dm = account.page_id
                            elif account.encrypted_credentials:
                                access_token = decrypt_credentials(account.encrypted_credentials)
                                page_id_for_dm = account.page_id
                            else:
                                raise Exception("No access token found")
                        except Exception as e:
                            print(f"❌ Failed to get access token for Followers flow: {str(e)}")
                            return
                        is_comment_trigger = comment_id and trigger_type in ["post_comment", "keyword", "live_comment"]
                        profile_url = f"https://www.instagram.com/{username}"
                        follow_with_prompt = f"{follow_message}\n\n✅ Once you've followed, type 'done' or 'followed' to continue!\n\n🔗 Visit my profile: {profile_url}\n\nClick one of the options below:"
                        if is_comment_trigger:
                            from app.utils.instagram_api import send_private_reply
                            from app.services.pre_dm_handler import update_pre_dm_state
                            try:
                                send_private_reply(comment_id, "Hi! 👋", access_token, page_id_for_dm)
                                await asyncio.sleep(1)
                            except Exception:
                                pass
                            # Mark follow request as sent BEFORE sending the long message so that webhook
                            # retries or duplicate deliveries never resend (prevents duplicate follow DMs)
                            update_pre_dm_state(str(sender_id), rule_id, {
                                "follow_request_sent": True,
                                "step": "follow",
                                "follow_recheck_trigger_type": trigger_type,
                            })
                            # Send follower question only once, with buttons (no text-only retry to avoid duplicate)
                            send_private_reply(comment_id, follow_with_prompt, access_token, page_id_for_dm, quick_replies=follow_quick_reply)
                            print(f"✅ [Followers] First question sent via private reply to {sender_id} (with buttons)")
                        else:
                            from app.services.pre_dm_handler import update_pre_dm_state
                            send_dm_api(str(sender_id), follow_with_prompt, access_token, page_id_for_dm, buttons=None, quick_replies=follow_quick_reply)
                            update_pre_dm_state(str(sender_id), rule_id, {"follow_request_sent": True, "step": "follow", "follow_recheck_trigger_type": trigger_type})
                            print(f"✅ [Followers] First question sent via DM to {sender_id}")
                        try:
                            from app.utils.plan_enforcement import log_dm_sent
                            log_dm_sent(user_id=user_id, instagram_account_id=account_id, recipient_username=str(sender_id), message=follow_message, db=db, instagram_username=username, instagram_igsid=account_igsid)
                        except Exception:
                            pass
                        return  # Wait for user to click "I'm following" or "Follow Me" (handled by quick reply handler)
                elif pre_dm_result and pre_dm_result["action"] == "send_email_request":
                    # Send email request message with Quick Reply buttons
                    message_template = pre_dm_result["message"]
                    # Create Quick Reply buttons for email collection
                    quick_replies = [
                        {
                            "content_type": "text",
                            "title": "Share Email",
                            "payload": "email_shared"
                        },
                        {
                            "content_type": "text",
                            "title": "Skip for Now",
                            "payload": "email_skip"
                        }
                    ]
                    
                    # IMPROVEMENT: Add user's email as quick reply button if available
                    # This allows users to quickly select their logged-in email instead of typing
                    try:
                        from app.models.user import User
                        user = db.query(User).filter(User.id == account.user_id).first()
                        if user and user.email:
                            # Instagram quick reply title limit is 20 characters
                            # Truncate email to fit, showing the most important part (before @)
                            email_display = user.email
                            if len(email_display) > 20:
                                # Show first part of email (before @) if possible
                                email_parts = email_display.split('@')
                                if len(email_parts) > 0:
                                    # Try to fit username + @ + first few chars of domain
                                    username = email_parts[0]
                                    if len(username) <= 15:
                                        email_display = f"{username}@{email_parts[1][:15-len(username)]}..."
                                    else:
                                        email_display = f"{username[:17]}..."
                                else:
                                    email_display = email_display[:17] + "..."
                            
                            # Add email button as first option (most convenient)
                            quick_replies.insert(0, {
                                "content_type": "text",
                                "title": email_display,
                                "payload": f"email_use_{user.email}"  # Include full email in payload
                            })
                            print(f"✅ Added user's email ({user.email}) as quick reply button")
                    except Exception as email_err:
                        print(f"⚠️ Could not add user email to quick replies: {str(email_err)}")
                        # Continue without email button - not critical
                    
                    pre_dm_result["quick_replies"] = quick_replies
                    print(f"📧 Sending email request DM to {sender_id} with Quick Reply buttons")
                    
                    # CRITICAL FIX: Actually send the email request message with quick_replies
                    from app.utils.instagram_api import send_dm as send_dm_api
                    from app.utils.encryption import decrypt_credentials
                    
                    # Get access token
                    try:
                        if account.encrypted_page_token:
                            access_token = decrypt_credentials(account.encrypted_page_token)
                            page_id_for_dm = account.page_id
                        elif account.encrypted_credentials:
                            access_token = decrypt_credentials(account.encrypted_credentials)
                            page_id_for_dm = account.page_id
                        else:
                            raise Exception("No access token found")
                    except Exception as e:
                        print(f"❌ Failed to get access token for email request: {str(e)}")
                        return
                    
                    # Mark email request as sent in state
                    from app.services.pre_dm_handler import update_pre_dm_state
                    update_pre_dm_state(str(sender_id), rule_id, {
                        "email_request_sent": True,
                        "step": "email"
                    })
                    
                    # Check if comment-based trigger (use private reply)
                    is_comment_trigger = comment_id and trigger_type in ["post_comment", "keyword", "live_comment"]
                    
                    # Only send "Hi👋" opener if follow request is enabled (needed for follow flow)
                    # For email-only flows, send email request directly via private_reply to open conversation
                    ask_to_follow = rule.config.get("ask_to_follow", False)
                    
                    if is_comment_trigger:
                        # For comment triggers, use private reply to bypass 24-hour window
                        from app.utils.instagram_api import send_private_reply
                        if ask_to_follow:
                            # Follow flow: Send opener first, then email request via regular DM
                            send_private_reply(comment_id, "Hi! 👋", access_token, page_id_for_dm)
                            await asyncio.sleep(1)  # Small delay
                            # Send email request with quick_replies via regular DM
                            send_dm_api(str(sender_id), message_template, access_token, page_id_for_dm, buttons=None, quick_replies=quick_replies)
                            print(f"✅ Email request sent via private reply + DM with quick_replies (comment trigger, follow enabled)")
                            
                            # Log DM sent (tracks in DmLog and increments global tracker)
                            try:
                                from app.utils.plan_enforcement import log_dm_sent
                                log_dm_sent(
                                    user_id=user_id,
                                    instagram_account_id=account_id,
                                    recipient_username=str(sender_id),
                                    message=message_template,
                                    db=db,
                                    instagram_username=username,
                                    instagram_igsid=account_igsid
                                )
                            except Exception as log_err:
                                print(f"⚠️ Failed to log DM: {str(log_err)}")
                        else:
                            # Email-only flow: Send "Hi! 👋" as first message via private_reply (opens conversation)
                            # Then send email question with quick_replies via regular DM
                            send_private_reply(comment_id, "Hi! 👋", access_token, page_id_for_dm)
                            await asyncio.sleep(1)  # Small delay
                            # Send email question with quick_replies via regular DM
                            send_dm_api(str(sender_id), message_template, access_token, page_id_for_dm, buttons=None, quick_replies=quick_replies)
                            print(f"✅ Email request sent: Hi👋 first, then email question with quick_replies (comment trigger, email-only)")
                            
                            # Log DM sent (tracks in DmLog and increments global tracker)
                            try:
                                from app.utils.plan_enforcement import log_dm_sent
                                log_dm_sent(
                                    user_id=user_id,
                                    instagram_account_id=account_id,
                                    recipient_username=str(sender_id),
                                    message=message_template,
                                    db=db,
                                    instagram_username=username,
                                    instagram_igsid=account_igsid
                                )
                            except Exception as log_err:
                                print(f"⚠️ Failed to log DM: {str(log_err)}")
                    else:
                        # For DM triggers, send directly with quick_replies
                        send_dm_api(str(sender_id), message_template, access_token, page_id_for_dm, buttons=None, quick_replies=quick_replies)
                        print(f"✅ Email request sent via DM with quick_replies")
                        
                        # Log DM sent (tracks in DmLog and increments global tracker)
                        try:
                            from app.utils.plan_enforcement import log_dm_sent
                            log_dm_sent(
                                user_id=user_id,
                                instagram_account_id=account_id,
                                recipient_username=str(sender_id),
                                message=message_template,
                                db=db,
                                instagram_username=username,
                                instagram_igsid=account_igsid
                            )
                        except Exception as log_err:
                            print(f"⚠️ Failed to log DM: {str(log_err)}")
                    
                    # CRITICAL FIX: Don't schedule delayed primary DM for email flows
                    # Email flows should wait for user interaction (button click or email input)
                    # Only schedule delayed primary DM for follow-only flows
                    ask_to_follow = rule.config.get("ask_to_follow", False)
                    ask_for_email = rule.config.get("ask_for_email", False)
                    
                    if ask_to_follow and not ask_for_email:
                        # Follow-only flow: Schedule delayed primary DM after 15 seconds
                        sender_id_for_dm = str(sender_id)
                        rule_id_for_dm = int(rule_id)
                        user_id_for_dm = int(user_id)
                        account_id_for_dm = int(account_id)
                        
                        async def delayed_primary_dm_simple():
                            """Simplified background task to send primary DM after 15 seconds."""
                            from app.db.session import SessionLocal
                            from app.models.automation_rule import AutomationRule
                            from app.models.instagram_account import InstagramAccount
                            
                            db_session = SessionLocal()
                            try:
                                print(f"⏰ [PRIMARY DM] Starting 15-second delay for sender {sender_id_for_dm}, rule {rule_id_for_dm}")
                                await asyncio.sleep(15)  # Wait 15 seconds
                                print(f"⏰ [PRIMARY DM] 15 seconds elapsed, checking if primary DM already sent")
                                
                                # Re-fetch rule and account
                                rule_refresh = db_session.query(AutomationRule).filter(AutomationRule.id == rule_id_for_dm).first()
                                account_refresh = db_session.query(InstagramAccount).filter(
                                    InstagramAccount.user_id == user_id_for_dm,
                                    InstagramAccount.id == account_id_for_dm
                                ).first()
                                
                                if not rule_refresh or not account_refresh:
                                    print(f"⚠️ [PRIMARY DM] Rule or account not found")
                                    return
                                
                                # Check if primary DM already sent
                                from app.services.pre_dm_handler import get_pre_dm_state
                                current_state = get_pre_dm_state(sender_id_for_dm, rule_id_for_dm)
                                if current_state.get("primary_dm_sent"):
                                    print(f"⏭️ [PRIMARY DM] Primary DM already sent, skipping")
                                    return
                                
                                # Mark as sent to prevent duplicates
                                from app.services.pre_dm_handler import update_pre_dm_state
                                update_pre_dm_state(sender_id_for_dm, rule_id_for_dm, {
                                    "primary_dm_sent": True
                                })
                                
                                # Send primary DM directly from rule config
                                print(f"✅ [PRIMARY DM] Sending primary DM from rule config")
                                await execute_automation_action(
                                    rule_refresh, sender_id_for_dm, account_refresh, db_session,
                                    trigger_type="primary_timeout",
                                    message_id=None,
                                    pre_dm_result_override={"action": "send_primary"}
                                )
                                print(f"✅ [PRIMARY DM] Primary DM sent successfully")
                            except Exception as e:
                                print(f"❌ [PRIMARY DM] Error in delayed primary DM: {str(e)}")
                                import traceback
                                traceback.print_exc()
                            finally:
                                db_session.close()
                                print(f"🔒 [PRIMARY DM] Database session closed")
                        
                        print(f"🚀 [PRIMARY DM] Scheduling primary DM after 15 seconds for sender {sender_id_for_dm}, rule {rule_id_for_dm}")
                        asyncio.create_task(delayed_primary_dm_simple())
                    else:
                        # Email flow: Don't schedule delayed primary DM - wait for user interaction
                        print(f"⏳ [EMAIL FLOW] Not scheduling delayed primary DM - waiting for user to click button or provide email")
                elif pre_dm_result and pre_dm_result["action"] == "wait_for_follow":
                    # Follow request was sent but not confirmed - user commented again
                    # Resend follow request to remind user
                    print(f"⏳ Follow request sent but not confirmed. User commented again, resending follow request...")
                    # Reset follow_request_sent to False so it gets resent
                    from app.services.pre_dm_handler import update_pre_dm_state
                    update_pre_dm_state(str(sender_id), rule_id, {
                        "follow_request_sent": False,
                        "follow_confirmed": False
                    })
                    # Re-process pre-DM to get follow request action
                    pre_dm_result = await process_pre_dm_actions(
                        rule, str(sender_id), account, db,
                        incoming_message=None,
                        trigger_type=trigger_type,
                        skip_growth_steps=skip_growth_steps
                    )
                    print(f"🔍 [DEBUG] After wait_for_follow reset, pre_dm_result action={pre_dm_result.get('action')}")
                    # Now handle the follow request - if it's send_follow_request, it will be handled by the if block above
                    if pre_dm_result and pre_dm_result["action"] == "send_follow_request":
                        # Re-process to send follow request - this will be handled by the send_follow_request block above
                        # We need to manually trigger it since we're in an elif block
                        # Actually, we can't easily fall through, so let's handle it here
                        follow_message = pre_dm_result.get("message") or rule.config.get("ask_to_follow_message", "Hey! Would you mind following me? I share great content! 🙌")
                        profile_url = f"https://www.instagram.com/{username}"
                        follow_message_with_instructions = f"{follow_message}\n\n✅ Once you've followed, type 'done' or 'followed' to continue!\n\n🔗 Visit my profile: {profile_url}\n\nClick one of the options below:"
                        
                        # Mark follow as sent
                        update_pre_dm_state(str(sender_id), rule_id, {
                            "follow_request_sent": True,
                            "step": "follow"
                        })
                        
                        # Send follow request message
                        from app.utils.instagram_api import send_dm as send_dm_api
                        from app.utils.encryption import decrypt_credentials
                        
                        try:
                            if account.encrypted_page_token:
                                access_token = decrypt_credentials(account.encrypted_page_token)
                                page_id_for_dm = account.page_id
                            elif account.encrypted_credentials:
                                access_token = decrypt_credentials(account.encrypted_credentials)
                                page_id_for_dm = account.page_id
                            else:
                                raise Exception("No access token found")
                        except Exception as e:
                            print(f"❌ Failed to get access token: {str(e)}")
                            return
                        
                        # Check if comment-based trigger
                        is_comment_trigger = comment_id and trigger_type in ["post_comment", "keyword", "live_comment"]
                        
                        if is_comment_trigger:
                            from app.utils.instagram_api import send_private_reply
                            send_private_reply(comment_id, follow_message_with_instructions, access_token, page_id_for_dm)
                            print(f"✅ Follow request resent via private reply")
                        else:
                            send_dm_api(str(sender_id), follow_message_with_instructions, access_token, page_id_for_dm)
                            print(f"✅ Follow request resent via DM")
                            
                            # Log DM sent (tracks in DmLog and increments global tracker)
                            try:
                                from app.utils.plan_enforcement import log_dm_sent
                                log_dm_sent(
                                    user_id=user_id,
                                    instagram_account_id=account_id,
                                    recipient_username=str(sender_id),
                                    message=follow_message_with_instructions,
                                    db=db,
                                    instagram_username=username,
                                    instagram_igsid=account_igsid
                                )
                            except Exception as log_err:
                                print(f"⚠️ Failed to log DM: {str(log_err)}")
                        
                        # Don't continue to primary DM - wait for user response
                        return
                    else:
                        # If something went wrong, don't send primary DM
                        print(f"⚠️ Failed to resend follow request, action={pre_dm_result.get('action') if pre_dm_result else 'None'}")
                        return
            elif pre_dm_result and pre_dm_result["action"] == "wait":
                # Waiting for user action - don't send anything
                print(f"⏳ Waiting for user action, not sending primary DM")
                return
            elif pre_dm_result and pre_dm_result["action"] == "send_phone_retry":
                    # Phone flow: user sent email or invalid input - send retry message (DM path from "Routing message to rule")
                    retry_message = pre_dm_result.get("message", "")
                    if not retry_message or not retry_message.strip():
                        retry_message = "We need your phone number for this, not your email. 📱 Please reply with your phone number!"
                    print(f"⚠️ [PHONE FLOW] Invalid/email input in DM, sending phone retry message")
                    from app.utils.encryption import decrypt_credentials
                    from app.utils.instagram_api import send_dm as send_dm_api
                    try:
                        if account.encrypted_page_token:
                            access_token = decrypt_credentials(account.encrypted_page_token)
                            page_id_for_dm = account.page_id
                        elif account.encrypted_credentials:
                            access_token = decrypt_credentials(account.encrypted_credentials)
                            page_id_for_dm = account.page_id
                        else:
                            raise Exception("No access token found for account")
                        send_dm_api(str(sender_id), retry_message, access_token, page_id_for_dm, buttons=None, quick_replies=None)
                        print(f"✅ Phone retry message sent via DM")
                    except Exception as e:
                        print(f"⚠️ Failed to send phone retry: {e}")
                    return
            elif pre_dm_result and pre_dm_result["action"] == "send_email_retry":
                    # Invalid email format received - send retry message
                    # NOTE: This should only happen for DM triggers, not comment triggers
                    # Comment triggers should resend email question, not retry message
                    retry_message = pre_dm_result.get("message", "")
                    if not retry_message or not retry_message.strip():
                        retry_message = "Hmm, that doesn't look like a valid email address. 🤔\n\nPlease type it again so I can send you the guide! 📧"
                    
                    print(f"⚠️ Invalid email format detected, sending retry message")
                    
                    # Get access token
                    from app.utils.encryption import decrypt_credentials
                    from app.utils.instagram_api import send_dm as send_dm_api
                    
                    try:
                        if account.encrypted_page_token:
                            access_token = decrypt_credentials(account.encrypted_page_token)
                            page_id_for_dm = account.page_id
                        elif account.encrypted_credentials:
                            access_token = decrypt_credentials(account.encrypted_credentials)
                            page_id_for_dm = account.page_id
                        else:
                            raise Exception("No access token found for account")
                    except (AttributeError, Exception) as e:
                        try:
                            db.refresh(account)
                            if account.encrypted_page_token:
                                access_token = decrypt_credentials(account.encrypted_page_token)
                                page_id_for_dm = account.page_id
                            elif account.encrypted_credentials:
                                access_token = decrypt_credentials(account.encrypted_credentials)
                                page_id_for_dm = account.page_id
                            else:
                                raise Exception("No access token found for account")
                        except Exception as refresh_error:
                            print(f"❌ Failed to get access token: {str(refresh_error)}")
                            return
                    
                    # Only send retry message for DM triggers (not comment triggers)
                    is_comment_trigger = comment_id and trigger_type in ["post_comment", "keyword", "live_comment"]
                    if is_comment_trigger:
                        # For comment triggers, don't send retry - should have been handled as wait_for_email
                        print(f"⏭️ Comment trigger - skipping retry message, will resend email question instead")
                        return
                    else:
                        # For DM triggers, send retry message
                        send_dm_api(str(sender_id), retry_message, access_token, page_id_for_dm, buttons=None, quick_replies=None)
                        print(f"✅ Email retry message sent via DM")
                    
                    return  # Wait for valid email input
            elif pre_dm_result and pre_dm_result["action"] == "wait_for_email":
                    # Still waiting for email response
                    # For comment triggers, resend email question as reminder (if comment matches keywords)
                    # For DM triggers, just wait silently
                    is_comment_trigger = comment_id and trigger_type in ["post_comment", "keyword", "live_comment"]
                    
                    if is_comment_trigger:
                        # Comment received while waiting for email - resend email question as reminder
                        print(f"💬 Comment received while waiting for email: '{incoming_message}' - resending email question as reminder")
                        
                        # Get email request message from config
                        ask_for_email_message = rule.config.get("ask_for_email_message", "Quick question - what's your email? I'd love to send you something special! 📧")
                        quick_replies = [
                            {
                                "content_type": "text",
                                "title": "Share Email",
                                "payload": "email_shared"
                            },
                            {
                                "content_type": "text",
                                "title": "Skip for Now",
                                "payload": "email_skip"
                            }
                        ]
                        
                        # IMPROVEMENT: Add user's email as quick reply button if available
                        try:
                            from app.models.user import User
                            user = db.query(User).filter(User.id == account.user_id).first()
                            if user and user.email:
                                # Instagram quick reply title limit is 20 characters
                                email_display = user.email
                                if len(email_display) > 20:
                                    email_parts = email_display.split('@')
                                    if len(email_parts) > 0:
                                        username = email_parts[0]
                                        if len(username) <= 15:
                                            email_display = f"{username}@{email_parts[1][:15-len(username)]}..."
                                        else:
                                            email_display = f"{username[:17]}..."
                                    else:
                                        email_display = email_display[:17] + "..."
                                
                                # Add email button as first option
                                quick_replies.insert(0, {
                                    "content_type": "text",
                                    "title": email_display,
                                    "payload": f"email_use_{user.email}"
                                })
                                print(f"✅ Added user's email ({user.email}) as quick reply button for comment reminder")
                        except Exception as email_err:
                            print(f"⚠️ Could not add user email to quick replies: {str(email_err)}")
                        
                        # Get access token
                        from app.utils.encryption import decrypt_credentials
                        from app.utils.instagram_api import send_dm as send_dm_api, send_private_reply
                        
                        try:
                            if account.encrypted_page_token:
                                access_token = decrypt_credentials(account.encrypted_page_token)
                                page_id_for_dm = account.page_id
                            elif account.encrypted_credentials:
                                access_token = decrypt_credentials(account.encrypted_credentials)
                                page_id_for_dm = account.page_id
                            else:
                                raise Exception("No access token found for account")
                        except (AttributeError, Exception) as e:
                            try:
                                db.refresh(account)
                                if account.encrypted_page_token:
                                    access_token = decrypt_credentials(account.encrypted_page_token)
                                    page_id_for_dm = account.page_id
                                elif account.encrypted_credentials:
                                    access_token = decrypt_credentials(account.encrypted_credentials)
                                    page_id_for_dm = account.page_id
                                else:
                                    raise Exception("No access token found for account")
                            except Exception as refresh_error:
                                print(f"❌ Failed to get access token: {str(refresh_error)}")
                                return
                        
                        # Send reminder via private reply + DM
                        send_private_reply(comment_id, ask_for_email_message, access_token, page_id_for_dm)
                        await asyncio.sleep(1)
                        send_dm_api(str(sender_id), ask_for_email_message, access_token, page_id_for_dm, buttons=None, quick_replies=quick_replies)
                        print(f"✅ Email question resent as reminder via private reply + DM (comment trigger)")
                        
                        # Log DM sent (tracks in DmLog and increments global tracker)
                        try:
                            from app.utils.plan_enforcement import log_dm_sent
                            log_dm_sent(
                                user_id=user_id,
                                instagram_account_id=account_id,
                                recipient_username=str(sender_id),
                                message=ask_for_email_message,
                                db=db,
                                instagram_username=username,
                                instagram_igsid=account_igsid
                            )
                        except Exception as log_err:
                            print(f"⚠️ Failed to log DM: {str(log_err)}")
                        
                        return
                    
                    # For non-comment triggers (DMs), just wait silently
                    print(f"⏳ Email requested but not received yet. Waiting for user interaction (button click or email input)")
                    print(f"   User can: 1) Click 'Share Email' button, 2) Click 'Skip for Now' button, or 3) Type their email")
                    return  # Don't send anything, just wait
            elif pre_dm_result and pre_dm_result["action"] == "send_combined_pre_dm":
                # Send TWO separate messages: follow question first, then email question
                # This ensures vertical (portrait) display instead of carousel (side-by-side)
                from app.utils.instagram_api import send_dm as send_dm_api
                from app.utils.encryption import decrypt_credentials
                
                # Get access token and page_id BEFORE sending messages
                try:
                    if account.encrypted_page_token:
                        access_token = decrypt_credentials(account.encrypted_page_token)
                        page_id_for_dm = account.page_id
                        print(f"✅ Using OAuth page token for sending pre-DM messages")
                    elif account.encrypted_credentials:
                        access_token = decrypt_credentials(account.encrypted_credentials)
                        page_id_for_dm = account.page_id
                        print(f"⚠️ Using legacy encrypted credentials for pre-DM messages")
                    else:
                        raise Exception("No access token found for account")
                except (AttributeError, Exception) as e:
                    # If detached, refresh from DB
                    try:
                        db.refresh(account)
                        if account.encrypted_page_token:
                            access_token = decrypt_credentials(account.encrypted_page_token)
                            page_id_for_dm = account.page_id
                            print(f"✅ Using OAuth page token for sending pre-DM messages (refreshed)")
                        elif account.encrypted_credentials:
                            access_token = decrypt_credentials(account.encrypted_credentials)
                            page_id_for_dm = account.page_id
                            print(f"⚠️ Using legacy encrypted credentials for pre-DM messages (refreshed)")
                        else:
                            raise Exception("No access token found for account")
                    except Exception as refresh_error:
                        print(f"❌ Failed to get access token for pre-DM messages: {str(refresh_error)}")
                        return
                
                # Get stored values
                follow_msg = pre_dm_result.get("follow_message", "")
                follow_btns = pre_dm_result.get("follow_buttons", [])
                email_msg = pre_dm_result.get("email_message", "")
                email_qr = pre_dm_result.get("quick_replies", [])
                
                print(f"📤 Preparing to send 2 pre-DM messages to {sender_id}")
                print(f"   Follow message: {follow_msg[:50] if follow_msg else 'None'}...")
                print(f"   Email message: {email_msg[:50] if email_msg else 'None'}...")
                print(f"   Follow buttons: {len(follow_btns) if follow_btns else 0}")
                print(f"   Email quick replies: {len(email_qr) if email_qr else 0}")
                
                # CRITICAL FIX: For comment-based triggers, use private reply for first message
                # to bypass 24-hour window, then regular DM for follow-ups
                is_comment_trigger = comment_id and trigger_type in ["post_comment", "keyword", "live_comment"]
                
                # Send first message: Follow question with Follow Me button
                if follow_msg:
                    if is_comment_trigger:
                        print(f"💬 Sending follow request via PRIVATE REPLY (comment trigger, Message 1/2)")
                        print(f"   Comment ID: {comment_id}, Commenter: {sender_id}")
                        try:
                            from app.utils.instagram_api import send_private_reply
                            # Send as private reply to bypass 24-hour window
                            send_private_reply(comment_id, follow_msg, access_token, page_id_for_dm)
                            print(f"✅ Follow request sent via private reply")
                            
                            # Log the DM
                            from app.models.dm_log import DmLog
                            dm_log = DmLog(
                                user_id=user_id,
                                instagram_account_id=account_id,
                                instagram_username=username,
                                instagram_igsid=account_igsid,
                                recipient_username=str(sender_id),
                                message=follow_msg
                            )
                            db.add(dm_log)
                            db.commit()
                            
                            # Small delay before sending button follow-up
                            await asyncio.sleep(1)
                            
                            # If buttons are configured, send follow-up with buttons
                            # After private reply, conversation is open for regular DMs
                            if follow_btns:
                                print(f"📤 Sending follow-up with Follow button...")
                                try:
                                    send_dm_api(
                                        sender_id,
                                        follow_msg,
                                        access_token,
                                        page_id_for_dm,
                                        buttons=follow_btns,
                                        quick_replies=None
                                    )
                                    print(f"✅ Follow button sent successfully")
                                except Exception as btn_error:
                                    print(f"⚠️ Could not send follow button: {str(btn_error)}")
                                    
                        except Exception as e:
                            print(f"❌ Failed to send follow request via private reply: {str(e)}")
                            import traceback
                            traceback.print_exc()
                    else:
                        print(f"📤 Sending follow request DM (Message 1/2)")
                        try:
                            send_dm_api(
                                sender_id,
                                follow_msg,
                                access_token,
                                page_id_for_dm,
                                buttons=follow_btns,
                                quick_replies=None
                            )
                            print(f"✅ Follow request DM sent successfully")
                            
                            # Log the DM (tracks in DmLog and increments global tracker)
                            from app.utils.plan_enforcement import log_dm_sent
                            try:
                                log_dm_sent(
                                    user_id=user_id,
                                    instagram_account_id=account_id,
                                    recipient_username=str(sender_id),
                                    message=follow_msg,
                                    db=db,
                                    instagram_username=username,
                                    instagram_igsid=account_igsid
                                )
                            except Exception as log_err:
                                print(f"⚠️ Failed to log DM: {str(log_err)}")
                        except Exception as e:
                            print(f"❌ Failed to send follow request DM: {str(e)}")
                            import traceback
                            traceback.print_exc()
                
                # Small delay to ensure messages are sent in order
                await asyncio.sleep(0.5)
                
                # Send second message: Email question with Quick Reply buttons
                if email_msg:
                    print(f"📤 Sending email request DM (Message 2/2) with {len(email_qr) if email_qr else 0} quick reply button(s)")
                    try:
                        # After private reply, conversation is open, so regular DM should work
                        send_dm_api(
                            sender_id,
                            email_msg,
                            access_token,
                            page_id_for_dm,
                            buttons=None,
                            quick_replies=email_qr
                        )
                        print(f"✅ Email request DM sent successfully")
                        
                        # Log the DM (tracks in DmLog and increments global tracker)
                        from app.utils.plan_enforcement import log_dm_sent
                        try:
                            log_dm_sent(
                                user_id=user_id,
                                instagram_account_id=account_id,
                                recipient_username=str(sender_id),
                                message=email_msg,
                                db=db,
                                instagram_username=username,
                                instagram_igsid=account_igsid
                            )
                        except Exception as log_err:
                            print(f"⚠️ Failed to log DM: {str(log_err)}")
                        
                        # Update stats for both DMs
                        from app.services.lead_capture import update_automation_stats
                        update_automation_stats(rule.id, "dm_sent", db)
                        update_automation_stats(rule.id, "dm_sent", db)  # Count as 2 DMs sent
                    except Exception as e:
                        print(f"❌ Failed to send email request DM: {str(e)}")
                        import traceback
                        traceback.print_exc()
                else:
                    print(f"⚠️ Email message is empty, skipping email DM")
                    
                    # Schedule primary DM after 15 seconds (after both pre-DM messages sent)
                    sender_id_for_dm = str(sender_id)
                    rule_id_for_dm = int(rule_id)
                    user_id_for_dm = int(user_id)
                    account_id_for_dm = int(account_id)
                    
                    async def delayed_primary_dm_simple():
                        """Simplified background task to send primary DM after 15 seconds."""
                        from app.db.session import SessionLocal
                        from app.models.automation_rule import AutomationRule
                        from app.models.instagram_account import InstagramAccount
                        
                        db_session = SessionLocal()
                        try:
                            print(f"⏰ [PRIMARY DM] Starting 15-second delay for sender {sender_id_for_dm}, rule {rule_id_for_dm}")
                            await asyncio.sleep(15)  # Wait 15 seconds
                            print(f"⏰ [PRIMARY DM] 15 seconds elapsed, checking if primary DM already sent")
                            
                            # Re-fetch rule and account
                            rule_refresh = db_session.query(AutomationRule).filter(AutomationRule.id == rule_id_for_dm).first()
                            account_refresh = db_session.query(InstagramAccount).filter(
                                InstagramAccount.user_id == user_id_for_dm,
                                InstagramAccount.id == account_id_for_dm
                            ).first()
                            
                            if not rule_refresh or not account_refresh:
                                print(f"⚠️ [PRIMARY DM] Rule or account not found")
                                return
                            
                            # Simple check: if primary DM already sent, skip
                            from app.services.pre_dm_handler import get_pre_dm_state
                            current_state = get_pre_dm_state(sender_id_for_dm, rule_id_for_dm)
                            if current_state.get("primary_dm_sent"):
                                print(f"⏭️ [PRIMARY DM] Primary DM already sent, skipping")
                                return
                            
                            # Mark as sent to prevent duplicates
                            from app.services.pre_dm_handler import update_pre_dm_state
                            update_pre_dm_state(sender_id_for_dm, rule_id_for_dm, {
                                "primary_dm_sent": True
                            })
                            
                            # Send primary DM directly from rule config (no pre-DM state checking)
                            print(f"✅ [PRIMARY DM] Sending primary DM from rule config")
                            await execute_automation_action(
                                rule_refresh, sender_id_for_dm, account_refresh, db_session,
                                trigger_type="primary_timeout",
                                message_id=None,
                                pre_dm_result_override={"action": "send_primary"}  # Skip pre-DM processing
                            )
                            print(f"✅ [PRIMARY DM] Primary DM sent successfully")
                        except Exception as e:
                            print(f"❌ [PRIMARY DM] Error in delayed primary DM task: {str(e)}")
                            import traceback
                            traceback.print_exc()
                        finally:
                            db_session.close()
                
                # Start the delayed primary DM task
                print(f"🚀 [PRIMARY DM] Scheduling primary DM after 15 seconds for sender {sender_id_for_dm}, rule {rule_id_for_dm}")
                asyncio.create_task(delayed_primary_dm_simple())
                    
                # Don't send primary DM yet - wait for user response or timeout
                print(f"✅ Both pre-DM messages sent vertically (portrait mode), primary DM scheduled for 15 seconds")
                return
            elif pre_dm_result and pre_dm_result["action"] == "send_primary":
                    # Pre-DM actions complete, proceed to primary DM
                    # IMPORTANT: Only proceed if flow is actually completed (not just skipped)
                    if pre_dm_result.get("email"):
                        print(f"✅ Pre-DM email received: {pre_dm_result['email']}, proceeding to primary DM")
                    # Continue to primary DM logic below - message_template will be loaded in the elif block below
                    print(f"🔍 [DEBUG] Pre-DM complete, will load message_template in elif block")
            
            # CRITICAL CHECK: Don't send primary DM if pre-DM actions are waiting for user response
            # This prevents primary DM from being sent when flow is not completed
            if pre_dm_result and pre_dm_result["action"] in ["wait", "wait_for_follow", "wait_for_email", "ignore", "send_follow_reminder"]:
                if pre_dm_result["action"] == "send_follow_reminder":
                    # Handle follow reminder - send message to user
                    follow_reminder_msg = pre_dm_result.get("message", 
                        "Hey! I'm waiting for you to confirm that you're following me. Please type 'done', 'followed', or 'I'm following' to continue! 😊")
                    print(f"💬 [FIX ISSUE 2] Sending follow reminder to {sender_id}")
                    
                    # Get access token first
                    from app.utils.encryption import decrypt_credentials
                    from app.utils.instagram_api import send_dm as send_dm_api
                    try:
                        if account.encrypted_page_token:
                            access_token_reminder = decrypt_credentials(account.encrypted_page_token)
                            account_page_id_reminder = account.page_id
                        elif account.encrypted_credentials:
                            access_token_reminder = decrypt_credentials(account.encrypted_credentials)
                            account_page_id_reminder = account.page_id
                        else:
                            raise Exception("No access token found")
                        
                        send_dm_api(sender_id, follow_reminder_msg, access_token_reminder, account_page_id_reminder, buttons=None, quick_replies=None)
                        print(f"✅ Follow reminder sent successfully")
                        
                        # Log DM sent
                        try:
                            from app.utils.plan_enforcement import log_dm_sent
                            log_dm_sent(
                                user_id=user_id,
                                instagram_account_id=account_id,
                                recipient_username=str(sender_id),
                                message=follow_reminder_msg,
                                db=db,
                                instagram_username=username,
                                instagram_igsid=account_igsid
                            )
                        except Exception as log_err:
                            print(f"⚠️ Failed to log DM: {str(log_err)}")
                    except Exception as send_err:
                        print(f"⚠️ Failed to send follow reminder: {str(send_err)}")
                else:
                    print(f"⏳ Pre-DM action is '{pre_dm_result['action']}' - waiting for user response, NOT sending primary DM")
                return
            
            # FIX ISSUE 2: Handle followup reminder action
            if pre_dm_result and pre_dm_result.get("action") == "send_followup_reminder":
                followup_reminder_msg = pre_dm_result.get("message", "Hey! I sent you a message earlier. Please check it and reply when you're ready! 😊")
                print(f"💬 [FIX ISSUE 2] Sending followup reminder to {sender_id}")
                # Get access token first
                from app.utils.encryption import decrypt_credentials
                from app.utils.instagram_api import send_dm as send_dm_api
                try:
                    if account.encrypted_page_token:
                        access_token = decrypt_credentials(account.encrypted_page_token)
                        account_page_id = account.page_id
                    elif account.encrypted_credentials:
                        access_token = decrypt_credentials(account.encrypted_credentials)
                        account_page_id = account.page_id
                    else:
                        raise Exception("No access token found")
                    
                    send_dm_api(sender_id, followup_reminder_msg, access_token, account_page_id, buttons=None, quick_replies=None)
                    print(f"✅ Followup reminder sent successfully")
                    
                    # Log DM sent
                    try:
                        from app.utils.plan_enforcement import log_dm_sent
                        log_dm_sent(
                            user_id=user_id,
                            instagram_account_id=account_id,
                            recipient_username=str(sender_id),
                            message=followup_reminder_msg,
                            db=db,
                            instagram_username=username,
                            instagram_igsid=account_igsid
                        )
                    except Exception as log_err:
                        print(f"⚠️ Failed to log DM: {str(log_err)}")
                except Exception as send_err:
                    print(f"⚠️ Failed to send followup reminder: {str(send_err)}")
                return  # Don't continue to primary DM
            
            # Check if this is a lead capture flow
            # Support both camelCase (from frontend) and snake_case (legacy) formats
            is_lead_capture = rule.config.get("is_lead_capture", False) or rule.config.get("isLeadCapture", False)
            
            # Process lead capture flow if:
            # 1. It's a lead capture rule AND we're not coming from pre-DM with send_primary, OR
            # 2. Primary DM was already sent and user is sending a new message (for lead capture)
            should_process_lead_capture = False
            if is_lead_capture:
                from app.services.pre_dm_handler import get_pre_dm_state
                rule_state = get_pre_dm_state(str(sender_id), rule.id)
                # Check if primary DM was sent and user is sending a message
                if rule_state.get("primary_dm_sent") and incoming_message and incoming_message.strip():
                    should_process_lead_capture = True
                    print(f"📧 [LEAD CAPTURE] Primary DM already sent, processing incoming message for lead capture")
                elif not (pre_dm_result and pre_dm_result.get("action") == "send_primary"):
                    should_process_lead_capture = True
            
            if should_process_lead_capture:
                # Process lead capture flow
                from app.services.lead_capture import process_lead_capture_step, update_automation_stats
                
                # Get user message from event (for DMs, this would be in the message text)
                # For comments, we'd need to extract from comment text
                user_message = incoming_message if incoming_message else ""
                if trigger_type in ["new_message", "keyword"] and not user_message:
                    # For DMs, we need to get the message text from the webhook event
                    # This is a simplified version - in production, you'd track conversation state
                    user_message = ""  # Will be extracted from webhook context
                
                # FIX ISSUE 2: Check if primary DM was already sent and user is sending random text
                # If lead was already captured, don't send any reminders - flow is complete
                from app.services.pre_dm_handler import get_pre_dm_state
                lead_state = get_pre_dm_state(str(sender_id), rule.id)
                if lead_state.get("primary_dm_sent") and user_message and user_message.strip():
                    # Check if lead was already captured
                    from app.models.captured_lead import CapturedLead
                    from sqlalchemy import cast
                    from sqlalchemy.dialects.postgresql import JSONB
                    existing_lead = db.query(CapturedLead).filter(
                        CapturedLead.automation_rule_id == rule.id,
                        CapturedLead.instagram_account_id == account_id,
                        cast(CapturedLead.extra_metadata, JSONB)['sender_id'].astext == str(sender_id)
                    ).first()
                    # Only treat as "lead captured" if lead matches current flow type (email vs phone)
                    lead_matches_flow = False
                    if existing_lead:
                        cfg = rule.config or {}
                        simple_dm_flow_phone = cfg.get("simple_dm_flow_phone", False) or cfg.get("simpleDmFlowPhone", False)
                        simple_dm_flow = cfg.get("simple_dm_flow", False) or cfg.get("simpleDmFlow", False)
                        ask_for_email = cfg.get("ask_for_email", False) or cfg.get("askForEmail", False)
                        if simple_dm_flow_phone:
                            lead_matches_flow = bool(existing_lead.phone and str(existing_lead.phone).strip())
                        elif simple_dm_flow or ask_for_email:
                            lead_matches_flow = bool(existing_lead.email and str(existing_lead.email).strip())
                        else:
                            lead_matches_flow = True
                    if existing_lead and lead_matches_flow:
                        # Lead already captured for this flow type - flow is complete, don't send any messages
                        print(f"🚫 [FIX] Lead already captured for rule {rule.id}, ignoring random text - no messages will be sent")
                        return  # Exit - flow is complete, don't send anything
                    
                    # Primary DM was sent, check if this is a valid lead capture response
                    lead_result = process_lead_capture_step(rule, user_message, sender_id, db)
                    
                    # If validation failed (user sent random text), send reminder
                    if lead_result.get("action") == "ask" and lead_result.get("validation_failed"):
                        # User sent invalid response, send reminder
                        followup_reminder_message = rule.config.get("followup_reminder_message", 
                            "Hey! I sent you a message earlier asking for your information. Please check it and reply with the requested details! 😊")
                        print(f"💬 [FIX ISSUE 2] User sent random text after followup request, sending reminder")
                        
                        # Get access token
                        from app.utils.encryption import decrypt_credentials
                        from app.utils.instagram_api import send_dm as send_dm_api
                        try:
                            if account.encrypted_page_token:
                                access_token_reminder = decrypt_credentials(account.encrypted_page_token)
                                account_page_id_reminder = account.page_id
                            elif account.encrypted_credentials:
                                access_token_reminder = decrypt_credentials(account.encrypted_credentials)
                                account_page_id_reminder = account.page_id
                            else:
                                raise Exception("No access token found")
                            
                            send_dm_api(sender_id, followup_reminder_message, access_token_reminder, account_page_id_reminder, buttons=None, quick_replies=None)
                            print(f"✅ Followup reminder sent successfully")
                            
                            # Log DM sent
                            try:
                                from app.utils.plan_enforcement import log_dm_sent
                                log_dm_sent(
                                    user_id=user_id,
                                    instagram_account_id=account_id,
                                    recipient_username=str(sender_id),
                                    message=followup_reminder_message,
                                    db=db,
                                    instagram_username=username,
                                    instagram_igsid=account_igsid
                                )
                            except Exception as log_err:
                                print(f"⚠️ Failed to log DM: {str(log_err)}")
                        except Exception as send_err:
                            print(f"⚠️ Failed to send followup reminder: {str(send_err)}")
                        return  # Don't continue processing
                    elif lead_result.get("action") == "ask":
                        # Valid ask action (resend question), continue processing
                        message_template = lead_result["message"]
                    elif lead_result.get("action") == "send":
                        # Valid send action (final message), continue processing
                        message_template = lead_result["message"]
                        if lead_result.get("saved_lead"):
                            print(f"✅ Lead captured: {lead_result['saved_lead'].email or lead_result['saved_lead'].phone}")
                    else:
                        # Skip lead capture for now (fallback to regular DM)
                        message_template = rule.config.get("message_template", "")
                else:
                    # Process lead capture step normally
                    lead_result = process_lead_capture_step(rule, user_message, sender_id, db)
                    
                    if lead_result["action"] == "ask":
                        # Send the ask message
                        message_template = lead_result["message"]
                    elif lead_result["action"] == "send":
                        # Send the final message
                        message_template = lead_result["message"]
                        if lead_result["saved_lead"]:
                            print(f"✅ Lead captured: {lead_result['saved_lead'].email or lead_result['saved_lead'].phone}")
                    else:
                        # Skip lead capture for now (fallback to regular DM)
                        message_template = rule.config.get("message_template", "")
            elif not pre_dm_result or pre_dm_result.get("action") == "send_primary":
                # Regular DM flow (or primary DM after pre-DM actions)
                # Get message template from config
                # For Lead Capture rules, check lead_dm_messages first, then fallback to message_variations
                # CRITICAL: Always load template when action is "send_primary" (even if message_template was set to None above)
                # This ensures template is loaded when coming from pre-DM actions with send_primary override
                # Check if we need to load template (either None or when send_primary action)
                should_load_template = message_template is None or (pre_dm_result and pre_dm_result.get("action") == "send_primary")
                if should_load_template:
                    # Support both camelCase (from frontend) and snake_case (legacy) formats
                    is_lead_capture = rule.config.get("is_lead_capture", False) or rule.config.get("isLeadCapture", False)
                    
                    # For Lead Capture rules, try lead_dm_messages first
                    if is_lead_capture:
                        lead_dm_messages = rule.config.get("lead_dm_messages", [])
                        print(f"🔍 [DEBUG] Lead Capture rule: checking lead_dm_messages={lead_dm_messages}")
                        if lead_dm_messages and isinstance(lead_dm_messages, list) and len(lead_dm_messages) > 0:
                            valid_messages = [m for m in lead_dm_messages if m and str(m).strip()]
                            if valid_messages:
                                import random
                                message_template = random.choice(valid_messages)
                                print(f"🎲 Selected message from {len(valid_messages)} lead_dm_messages variations")
                                print(f"✅ [DEBUG] message_template set to: {repr(message_template)}")
                            else:
                                print(f"⚠️ All lead_dm_messages are empty, trying fallback to message_variations")
                        else:
                            print(f"⚠️ lead_dm_messages is empty, trying fallback to message_variations")
                    
                    # If still no template, try message_variations (shared or simple reply messages)
                    if not message_template:
                        message_variations = rule.config.get("message_variations", [])
                        print(f"🔍 [DEBUG] Loading message template: message_variations={message_variations}, type={type(message_variations)}")
                        if message_variations and isinstance(message_variations, list) and len(message_variations) > 0:
                            # Filter out empty messages
                            valid_messages = [m for m in message_variations if m and str(m).strip()]
                            if valid_messages:
                                # Randomly select one message from variations
                                import random
                                message_template = random.choice(valid_messages)
                                print(f"🎲 Randomly selected message from {len(valid_messages)} valid variations")
                            else:
                                print(f"⚠️ All message variations are empty, trying fallback to message_template")
                                message_template = rule.config.get("message_template", "")
                        else:
                            message_template = rule.config.get("message_template", "")
                            print(f"🔍 [DEBUG] Using message_template fallback: '{message_template[:50] if message_template else 'None'}...'")
                
                # Debug: Log final message_template value after loading
                print(f"✅ [DEBUG] Final message_template after loading: {repr(message_template)}, type: {type(message_template)}")

            # Get access token BEFORE checking message_template
            # This allows us to send email success message even if primary DM template is missing
            from app.utils.encryption import decrypt_credentials
            from app.utils.instagram_api import send_private_reply, send_dm as send_dm_api
            
            access_token = None
            account_page_id = None
            try:
                # Get access token - refresh account if needed to avoid DetachedInstanceError
                try:
                    # Try to access encrypted tokens directly
                    if account.encrypted_page_token:
                        access_token = decrypt_credentials(account.encrypted_page_token)
                        print(f"✅ Using OAuth page token for sending message")
                        account_page_id = account.page_id
                    elif account.encrypted_credentials:
                        access_token = decrypt_credentials(account.encrypted_credentials)
                        print(f"⚠️ Using legacy encrypted credentials")
                        account_page_id = account.page_id
                    else:
                        raise Exception("No access token found for account")
                except (AttributeError, Exception) as e:
                    # If detached, refresh from DB
                    try:
                        db.refresh(account)
                        if account.encrypted_page_token:
                            access_token = decrypt_credentials(account.encrypted_page_token)
                            print(f"✅ Using OAuth page token for sending message (refreshed)")
                            account_page_id = account.page_id
                        elif account.encrypted_credentials:
                            access_token = decrypt_credentials(account.encrypted_credentials)
                            print(f"⚠️ Using legacy encrypted credentials (refreshed)")
                            account_page_id = account.page_id
                        else:
                            raise Exception("No access token found for account")
                    except Exception as refresh_error:
                        print(f"❌ Failed to refresh account and get access token: {str(refresh_error)}")
                        raise Exception(f"Could not access account credentials: {str(refresh_error)}")
                
                # IMPORTANT: Send email success message BEFORE checking message_template
                # This ensures it's sent even if primary DM template is missing
                print(f"🔍 [EMAIL SUCCESS] Checking: pre_dm_result={pre_dm_result}, send_email_success={pre_dm_result.get('send_email_success') if pre_dm_result else None}")
                
                # Check if we should send email success message
                # IMPORTANT: Only send for non-VIP users who just provided their email
                # VIP users already provided email, so skip the success message
                should_send_email_success = False
                if pre_dm_result and not skip_growth_steps:  # Only for non-VIP users
                    send_email_success_flag = pre_dm_result.get("send_email_success", False)
                    print(f"🔍 [EMAIL SUCCESS] send_email_success flag: {send_email_success_flag}, type: {type(send_email_success_flag)}, skip_growth_steps={skip_growth_steps}")
                    if send_email_success_flag is True or str(send_email_success_flag).lower() == 'true':
                        should_send_email_success = True
                        print(f"✅ [EMAIL SUCCESS] Will send email success message for non-VIP user")
                else:
                    if skip_growth_steps:
                        print(f"⏭️ [EMAIL SUCCESS] Skipping: VIP user (skip_growth_steps=True) - already provided email")
                    else:
                        print(f"⏭️ [EMAIL SUCCESS] Skipping: pre_dm_result is None or send_email_success flag not set")
                
                if should_send_email_success:
                    email_success_message = rule.config.get("email_success_message")
                    print(f"🔍 [EMAIL SUCCESS] email_success_message from config: '{email_success_message}'")
                    
                    # Use default message if not configured (same as frontend default)
                    if not email_success_message or str(email_success_message).strip() == '' or str(email_success_message).lower() == 'none':
                        email_success_message = "Got it! Check your inbox (and maybe spam/promotions) in about 2 minutes. 🎁"
                        print(f"🔍 [EMAIL SUCCESS] Using default email success message")
                    
                    # FIXED: Send PDF link as button instead of plain text to prevent unwanted link preview card
                    # Frontend saves as lead_magnet_link (PDF/Link to Share). Also check legacy names.
                    pdf_link = (
                        rule.config.get("lead_magnet_link") or
                        rule.config.get("pdf_link") or 
                        rule.config.get("link_to_share") or 
                        rule.config.get("share_link") or
                        rule.config.get("pdf_link_to_share")
                    )
                    
                    # Prepare buttons list (empty by default)
                    pdf_buttons = None
                    
                    if pdf_link and str(pdf_link).strip():
                        pdf_link_clean = str(pdf_link).strip()
                        # Send PDF link as a button instead of appending to text
                        # This prevents Instagram from auto-creating an unwanted link preview card
                        pdf_buttons = [{
                            "text": "Get PDF",
                            "url": pdf_link_clean
                        }]
                        print(f"✅ [EMAIL SUCCESS] Will send PDF link as button: {pdf_link_clean[:50]}...")
                    else:
                        print(f"🔍 [EMAIL SUCCESS] No PDF link configured in rule config")
                    
                    if email_success_message and str(email_success_message).strip():
                        print(f"📧 Sending email success message before primary DM")
                        try:
                            # Check if this is a comment-based trigger (use private reply to bypass 24-hour window)
                            is_comment_trigger = comment_id and trigger_type in ["post_comment", "keyword", "live_comment"]
                            
                            # Determine which message to send and log
                            message_to_send = email_success_message
                            
                            if is_comment_trigger:
                                # For comment triggers: Use private reply (bypasses 24-hour window)
                                # Private replies don't support URL buttons, so include PDF URL in message text
                                if pdf_link and str(pdf_link).strip():
                                    pdf_url_in_text = f"\n\n🔗 Get your PDF here: {pdf_link_clean}"
                                    message_to_send = f"{email_success_message}{pdf_url_in_text}"
                                
                                from app.utils.instagram_api import send_private_reply
                                send_private_reply(comment_id, message_to_send, access_token, account_page_id)
                                print(f"✅ Email success message sent via private reply (bypasses 24-hour window)")
                            else:
                                # For non-comment triggers: Use regular DM with button (conversation already active)
                                send_dm_api(sender_id, message_to_send, access_token, account_page_id, buttons=pdf_buttons, quick_replies=None)
                                print(f"✅ Email success message sent successfully with PDF button")
                            
                            # Log DM sent (tracks in DmLog and increments global tracker)
                            try:
                                from app.utils.plan_enforcement import log_dm_sent
                                log_dm_sent(
                                    user_id=user_id,
                                    instagram_account_id=account_id,
                                    recipient_username=str(sender_id),
                                    message=message_to_send,
                                    db=db,
                                    instagram_username=username,
                                    instagram_igsid=account_igsid
                                )
                            except Exception as log_err:
                                print(f"⚠️ Failed to log DM: {str(log_err)}")
                            
                            # Small delay between messages
                            await asyncio.sleep(1)
                        except Exception as send_err:
                            print(f"⚠️ Failed to send email success message: {str(send_err)}")
                            import traceback
                            traceback.print_exc()
                    else:
                        print(f"⏭️ [EMAIL SUCCESS] Skipping: email_success_message is empty or not configured")
                else:
                    print(f"⏭️ [EMAIL SUCCESS] Skipping: send_email_success is False or pre_dm_result is None (pre_dm_result={pre_dm_result})")
            except Exception as token_err:
                print(f"❌ Failed to get access token for email success message: {str(token_err)}")
                import traceback
                traceback.print_exc()
            
            # Now check if we have a message template for the primary DM
            # CRITICAL: Check both None and empty string, and ensure template was loaded
            if not message_template or (isinstance(message_template, str) and not message_template.strip()):
                print(f"⚠️ No message template configured for rule {rule.id}, action: {pre_dm_result.get('action') if pre_dm_result else 'None'}")
                print(f"🔍 [DEBUG] message_template value: {repr(message_template)}, type: {type(message_template)}")
                print(f"🔍 [DEBUG] Rule config - lead_dm_messages: {rule.config.get('lead_dm_messages')}, message_variations: {rule.config.get('message_variations')}, message_template: {rule.config.get('message_template')}")
                print(f"🔍 [DEBUG] is_lead_capture: {is_lead_capture}, pre_dm_result: {pre_dm_result}")
                # Try to load template one more time as fallback
                if is_lead_capture:
                    lead_dm_messages = rule.config.get("lead_dm_messages", [])
                    if lead_dm_messages and isinstance(lead_dm_messages, list) and len(lead_dm_messages) > 0:
                        valid_messages = [m for m in lead_dm_messages if m and str(m).strip()]
                        if valid_messages:
                            import random
                            message_template = random.choice(valid_messages)
                            print(f"🔄 [FALLBACK] Loaded message_template from lead_dm_messages: {repr(message_template)}")
                
                # If still no template but we have send_primary (lead phone/email OR VIP commenting again), use default so primary DM is sent
                if not message_template or (isinstance(message_template, str) and not message_template.strip()):
                    is_send_primary = pre_dm_result and pre_dm_result.get("action") == "send_primary"
                    has_lead_data = pre_dm_result and (pre_dm_result.get("phone") or pre_dm_result.get("email"))
                    if is_send_primary and (has_lead_data or skip_growth_steps):
                        message_template = "Thanks! Here’s your guide. 📱 Check your DMs for the link."
                        print(f"⚠️ No primary DM template for rule {rule.id}; using default message so primary DM is sent (lead/VIP comment)")
                    else:
                        print(f"❌ Still no message template after fallback, returning early")
                        return
            
            # Update stats
            from app.services.lead_capture import update_automation_stats
            update_automation_stats(rule.id, "triggered", db)
            
            # Apply delay if configured (delay is in minutes, convert to seconds)
            delay_minutes = rule.config.get("delay_minutes", 0)
            if delay_minutes and delay_minutes > 0:
                delay_seconds = delay_minutes * 60
                print(f"⏳ Waiting {delay_minutes} minute(s) ({delay_seconds} seconds) before sending message...")
                await asyncio.sleep(delay_seconds)
                print(f"✅ Delay complete, proceeding to send message")
            
            # Send DM using Instagram Graph API (for OAuth accounts)
            try:
                print(f"🔍 [COMMENT REPLY] Starting check: comment_id={comment_id}, trigger_type={trigger_type}")
                
                # Check if auto-reply to comments is enabled
                # This applies to post_comment, live_comment, AND keyword triggers when comment_id is present
                # (keyword triggers can come from comments if the rule has keywords configured)
                
                # Check for comment reply settings - support both old format and new simple/lead format
                # Support both camelCase (from frontend) and snake_case (legacy) formats
                is_lead_capture = rule.config.get("is_lead_capture", False) or rule.config.get("isLeadCapture", False)
                
                # Determine which comment reply fields to use based on rule type
                # Helper to get config with camelCase/snake_case fallback for backward compatibility
                def get_cfg_val(key_snake, key_camel=None, default=None):
                    if key_camel is None:
                        parts = key_snake.split('_')
                        key_camel = parts[0] + ''.join(word.capitalize() for word in parts[1:])
                    return rule.config.get(key_snake) or rule.config.get(key_camel) or default
                
                if is_lead_capture:
                    # Lead Capture rule: check lead-specific fields first, then fallback to shared
                    # Supports both camelCase (from frontend) and snake_case (legacy) formats
                    auto_reply_to_comments = get_cfg_val("lead_auto_reply_to_comments", default=False) or get_cfg_val("auto_reply_to_comments", default=False)
                    comment_replies = get_cfg_val("lead_comment_replies", default=[]) or get_cfg_val("comment_replies", default=[])
                else:
                    # Simple Reply rule: check simple-specific fields first, then fallback to shared
                    # Supports both camelCase (from frontend) and snake_case (legacy) formats
                    auto_reply_to_comments = get_cfg_val("simple_auto_reply_to_comments", default=False) or get_cfg_val("auto_reply_to_comments", default=False)
                    comment_replies = get_cfg_val("simple_comment_replies", default=[]) or get_cfg_val("comment_replies", default=[])
                
                print(f"🔍 [COMMENT REPLY] Rule {rule.id} (is_lead_capture={is_lead_capture}): auto_reply_to_comments={auto_reply_to_comments}, comment_replies type={type(comment_replies)}, len={len(comment_replies) if isinstance(comment_replies, list) else 'N/A'}")
                print(f"🔍 [COMMENT REPLY] Config fields: auto_reply_to_comments={rule.config.get('auto_reply_to_comments')}, simple_auto_reply_to_comments={rule.config.get('simple_auto_reply_to_comments')}, lead_auto_reply_to_comments={rule.config.get('lead_auto_reply_to_comments')}")
                print(f"🔍 [COMMENT REPLY] comment_replies={rule.config.get('comment_replies')}, simple_comment_replies={rule.config.get('simple_comment_replies')}, lead_comment_replies={rule.config.get('lead_comment_replies')}")
                
                # Check if we already replied to this specific comment (per comment_id, not per user)
                comment_reply_already_sent = False
                if comment_id:
                    from app.services.pre_dm_handler import was_comment_replied
                    if was_comment_replied(str(sender_id), rule.id, comment_id):
                        comment_reply_already_sent = True
                        print(f"⏭️ [COMMENT REPLY] Skipping: Already replied to this comment (comment_id={comment_id})")
                
                # If we have a comment_id and auto-reply is enabled, send public comment reply
                # (Only skip if we already replied to this exact comment; new comments always get a reply)
                if not comment_reply_already_sent and comment_id and auto_reply_to_comments and comment_replies and isinstance(comment_replies, list):
                    # Filter out empty replies
                    valid_replies = [r for r in comment_replies if r and str(r).strip()]
                    print(f"🔍 [COMMENT REPLY] After filtering: {len(valid_replies)} valid replies out of {len(comment_replies)} total")
                    if valid_replies:
                        # Randomly select one comment reply
                        import random
                        selected_reply = random.choice(valid_replies)
                        print(f"💬 Auto-reply enabled: Sending PUBLIC comment reply (selected from {len(valid_replies)} variations)")
                        print(f"   Trigger type: {trigger_type}, Comment ID: {comment_id}")
                        try:
                            from app.utils.instagram_api import send_public_comment_reply
                            # Use Instagram Business Account token (already have it as access_token)
                            # Instagram Graph API supports public comment replies on your own content
                            send_public_comment_reply(comment_id, selected_reply, access_token)
                            print(f"✅ Public comment reply sent to comment {comment_id}: {selected_reply[:50]}...")
                            from app.services.pre_dm_handler import mark_comment_replied
                            mark_comment_replied(str(sender_id), rule.id, comment_id)
                            from app.services.lead_capture import update_automation_stats
                            update_automation_stats(rule.id, "comment_replied", db)
                            try:
                                from app.utils.analytics import log_analytics_event_sync
                                from app.models.analytics_event import EventType
                                _mid = rule.config.get("media_id") if isinstance(getattr(rule, "config", None), dict) else None
                                log_analytics_event_sync(db=db, user_id=account.user_id, event_type=EventType.COMMENT_REPLIED, rule_id=rule.id, media_id=_mid, instagram_account_id=account.id, metadata={"comment_id": comment_id})
                            except Exception as _ae:
                                pass
                            # Wait 3 seconds after sending public comment reply before sending DM
                            print(f"⏳ Waiting 3 seconds after comment reply before sending DM...")
                            await asyncio.sleep(3)
                            print(f"✅ 3-second delay complete, proceeding to send DM")
                        except Exception as reply_error:
                            print(f"⚠️ Failed to send public comment reply: {str(reply_error)}")
                            print(f"   This might be due to missing permissions (instagram_business_manage_comments),")
                            print(f"   comment ID format, or the comment is not on your own content.")
                            print(f"   Continuing with DM send...")
                            # Continue to send DM even if public reply fails
                    else:
                        print(f"⏭️ [COMMENT REPLY] Skipping: All comment_replies are empty after filtering for rule {rule.id}")
                else:
                    # Log why comment reply is not being sent
                    if comment_reply_already_sent:
                        print(f"⏭️ [COMMENT REPLY] Skipping: Already replied to this comment (comment_id={comment_id})")
                    elif not comment_id:
                        print(f"⏭️ [COMMENT REPLY] Skipping: No comment_id provided (trigger_type={trigger_type})")
                    elif not auto_reply_to_comments:
                        print(f"⏭️ [COMMENT REPLY] Skipping: auto_reply_to_comments is False for rule {rule.id}")
                    elif not comment_replies or not isinstance(comment_replies, list) or len(comment_replies) == 0:
                        print(f"⏭️ [COMMENT REPLY] Skipping: No comment_replies configured for rule {rule.id} (comment_replies={comment_replies})")
                
                # Always send DM if message is configured (for all trigger types)
                # Each new comment gets both comment reply AND primary DM
                if message_template:
                    if comment_id and skip_growth_steps:
                        print(f"📤 [PRIMARY DM] Sending primary DM for comment trigger (VIP / already have email) → sender_id={sender_id} rule_id={rule.id}")
                    print(f"📤 [PRIMARY DM] Sending primary DM to sender_id={sender_id} rule_id={rule.id} (comment_id={comment_id}, trigger_type={trigger_type})")
                    # Get buttons from pre_dm_result first (for follow button), then from rule config
                    buttons = None
                    if pre_dm_result and pre_dm_result.get("buttons"):
                        buttons = pre_dm_result.get("buttons")
                        print(f"📎 Using {len(buttons)} button(s) from pre-DM result")
                    elif rule.config.get("buttons") and isinstance(rule.config.get("buttons"), list):
                        buttons = rule.config.get("buttons")
                        print(f"📎 Found {len(buttons)} button(s) in rule config")
                    
                    # Get quick replies from pre_dm_result (for email collection)
                    quick_replies = None
                    if pre_dm_result and pre_dm_result.get("quick_replies"):
                        quick_replies = pre_dm_result.get("quick_replies")
                        print(f"📎 Using {len(quick_replies)} quick reply button(s) from pre-DM result")
                    
                    # Use stored account_page_id (set when getting access token) or try to access account.page_id
                    try:
                        if 'account_page_id' in locals():
                            page_id_for_dm = account_page_id
                        else:
                            page_id_for_dm = account.page_id if account.page_id else None
                    except Exception:
                        # If detached, use None (not critical for DM sending)
                        page_id_for_dm = None
                    
                    # DM automation supports text and text+button only (no image/video/card/voice)
                    media_url_to_send = None
                    card_config = None

                    # CRITICAL FIX: For comment-based triggers, use Private Reply endpoint to bypass 24-hour window
                    # Comments don't count as DM initiation, so normal send_dm would fail
                    # Private replies use comment_id instead of user_id and bypass the restriction
                    if comment_id and trigger_type in ["post_comment", "keyword", "live_comment"]:
                        print(f"💬 Comment-based trigger detected! Using PRIVATE REPLY to bypass 24-hour window")
                        print(f"   Trigger type: {trigger_type}, Comment ID: {comment_id}")
                        print(f"   Recipient (commenter): {sender_id}")
                        
                        # Check if conversation was already opened (email or follow request was already sent)
                        # If so, don't send "Hi! 👋" opener again
                        from app.services.pre_dm_handler import get_pre_dm_state
                        state = get_pre_dm_state(str(sender_id), rule.id)
                        conversation_already_opened = state.get("email_request_sent", False) or state.get("follow_request_sent", False)
                        
                        # For comment-based triggers, use private reply to open conversation
                        # Then send the actual message with buttons as regular DM
                        from app.utils.instagram_api import send_private_reply
                        
                        if buttons or quick_replies:
                            # If buttons/quick replies needed, check if conversation was already opened
                            if not conversation_already_opened:
                                # Conversation not opened yet - send simple opener via private reply
                                print(f"💬 Sending simple opener via private reply to open conversation")
                                opener_message = "Hi! 👋"
                                send_private_reply(comment_id, opener_message, access_token, page_id_for_dm)
                                print(f"✅ Conversation opened via private reply")
                                
                                # Small delay to ensure private reply is processed
                                await asyncio.sleep(1)
                            else:
                                print(f"⏭️ Conversation already opened (email/follow request was sent), skipping opener")
                            
                            # Capture timestamp RIGHT BEFORE sending actual message to match Instagram's timing exactly
                            # Instagram displays times in UTC+8, so we add 8 hours to match Instagram's display
                            message_timestamp = datetime.utcnow() + timedelta(hours=8)
                            
                            # Now send the actual message with buttons/quick replies (text only)
                            print(f"📤 Sending DM with buttons/quick replies...")
                            from app.utils.instagram_api import send_dm
                            from app.utils.plan_enforcement import log_dm_sent
                            send_dm(sender_id, message_template, access_token, page_id_for_dm, buttons, quick_replies)
                            print(f"✅ DM with buttons/quick replies sent to {sender_id}")
                            
                            # Log DM sent (tracks in DmLog and increments global tracker)
                            try:
                                log_dm_sent(
                                    user_id=user_id,
                                    instagram_account_id=account_id,
                                    recipient_username=str(sender_id),
                                    message=message_template,
                                    db=db,
                                    instagram_username=username,
                                    instagram_igsid=account_igsid
                                )
                            except Exception as log_err:
                                print(f"⚠️ Failed to log DM: {str(log_err)}")
                        else:
                            # Capture timestamp RIGHT BEFORE sending to match Instagram's timing exactly
                            # Instagram displays times in UTC+8, so we add 8 hours to match Instagram's display
                            message_timestamp = datetime.utcnow() + timedelta(hours=8)
                            send_private_reply(comment_id, message_template, access_token, page_id_for_dm)
                            print(f"✅ Private reply sent to comment {comment_id} from user {sender_id}")
                            # Log DM sent (tracks in DmLog and increments global tracker)
                            # Note: Private replies are counted as DMs for tracking purposes
                            try:
                                from app.utils.plan_enforcement import log_dm_sent
                                log_dm_sent(
                                    user_id=user_id,
                                    instagram_account_id=account_id,
                                    recipient_username=str(sender_id),
                                    message=message_template,
                                    db=db,
                                    instagram_username=username,
                                    instagram_igsid=account_igsid
                                )
                            except Exception as log_err:
                                print(f"⚠️ Failed to log DM: {str(log_err)}")
                    else:
                        # For direct message triggers or when no comment_id, use regular DM
                        if page_id_for_dm:
                            print(f"📤 Sending DM via Page API: Page ID={page_id_for_dm}, Recipient={sender_id}")
                        else:
                            print(f"📤 Sending DM via me/messages (no page_id): Recipient={sender_id}")
                        # Capture timestamp RIGHT BEFORE sending to match Instagram's timing exactly
                        # Instagram displays times in UTC+8, so we add 8 hours to match Instagram's display
                        message_timestamp = datetime.utcnow() + timedelta(hours=8)
                        # Import send_dm and call it with quick_replies
                        from app.utils.instagram_api import send_dm
                        from app.utils.plan_enforcement import log_dm_sent
                        send_dm(sender_id, message_template, access_token, page_id_for_dm, buttons, quick_replies)
                        print(f"✅ DM sent to {sender_id}")
                        
                        # Log DM sent (tracks in DmLog and increments global tracker)
                        try:
                            log_dm_sent(
                                user_id=user_id,
                                instagram_account_id=account_id,
                                recipient_username=str(sender_id),
                                message=message_template,
                                db=db,
                                instagram_username=username,
                                instagram_igsid=account_igsid
                            )
                        except Exception as log_err:
                            print(f"⚠️ Failed to log DM: {str(log_err)}")
                    
                    # Mark primary DM sent so further messages from this user are not auto-replied (endpoint = primary DM)
                    from app.services.pre_dm_handler import update_pre_dm_state
                    update_pre_dm_state(str(sender_id), rule.id, {"primary_dm_sent": True})
                    
                    # Update stats
                    from app.services.lead_capture import update_automation_stats
                    update_automation_stats(rule.id, "dm_sent", db)
                    
                    # Log DM_SENT analytics event
                    try:
                        from app.utils.analytics import log_analytics_event_sync
                        from app.models.analytics_event import EventType
                        media_id = rule.config.get("media_id") if hasattr(rule, 'config') else None
                        log_analytics_event_sync(
                            db=db,
                            user_id=user_id,
                            event_type=EventType.DM_SENT,
                            rule_id=rule.id,
                            media_id=media_id,
                            instagram_account_id=account.id,
                            metadata={
                                "sender_id": sender_id,
                                "message_preview": message_template[:100] if message_template else None,
                                "trigger_type": trigger_type
                            }
                        )
                    except Exception as analytics_err:
                        print(f"⚠️ Failed to log DM_SENT event: {str(analytics_err)}")
                
                # Log the DM (use stored values to avoid DetachedInstanceError)
                from app.models.dm_log import DmLog
                dm_log = DmLog(
                    user_id=user_id,
                    instagram_account_id=account_id,
                    instagram_username=username,
                    instagram_igsid=account_igsid,
                    recipient_username=str(sender_id),
                    message=message_template
                )
                db.add(dm_log)
                
                # Also store in Message table for Messages UI
                try:
                    from app.models.message import Message
                    # Get recipient username if available (sender_id might be username or ID)
                    recipient_username = str(sender_id)  # Default to sender_id
                    
                    # PREVENT SELF-CONVERSATIONS: Skip if sender_id matches account's own IGSID
                    account_igsid = account.igsid or str(account_id)
                    conversation = None
                    
                    if str(sender_id) == account_igsid:
                        print(f"🚫 Skipping self-conversation creation (sender_id={sender_id} matches account IGSID)")
                        # Skip conversation creation and message storage for self-messages
                        # Don't store messages sent to self
                    else:
                        # Try to get username from previous messages
                        previous_msg = db.query(Message).filter(
                            Message.instagram_account_id == account_id,
                            Message.user_id == user_id,
                            Message.sender_id == str(sender_id)
                        ).first()
                        if previous_msg and previous_msg.sender_username:
                            recipient_username = previous_msg.sender_username
                        
                        # Get or create conversation for this participant
                        conversation = get_or_create_conversation(
                            db=db,
                            user_id=user_id,
                            account_id=account_id,
                            participant_id=str(sender_id),
                            participant_name=recipient_username
                        )
                        
                        # Update conversation's last_message and updated_at
                        message_preview = message_template or "[Media]"
                        if len(message_preview) > 100:
                            message_preview = message_preview[:100] + "..."
                        conversation.last_message = message_preview
                        conversation.updated_at = datetime.utcnow()
                        
                        # Use timestamp captured before API call (matches Instagram's timing exactly)
                        sent_message = Message(
                            user_id=user_id,
                            instagram_account_id=account_id,
                            conversation_id=conversation.id,
                            sender_id=str(account.igsid or account_id),  # Our account ID
                            sender_username=username,  # Our account username
                            recipient_id=str(sender_id),  # Recipient ID
                            recipient_username=recipient_username,  # Will be updated when we have it
                            message_text=message_template,
                            content=message_template,  # Also set content field
                            message_id=None,  # Instagram doesn't return message ID immediately
                            platform_message_id=None,  # Will be updated if we get it later
                            is_from_bot=True,  # This is an outgoing message
                            has_attachments=False,
                            attachments=None,
                            created_at=message_timestamp  # Explicit timestamp for precise timing
                        )
                        db.add(sent_message)
                except Exception as msg_err:
                    print(f"⚠️ Failed to store message in Message table: {str(msg_err)}")
                    # Don't fail if Message storage fails
                
                db.commit()
                print(f"✅ DM logged successfully")
                
            except Exception as e:
                print(f"❌ Failed to send message: {str(e)}")
                import traceback
                traceback.print_exc()
                # Note: Not logging failed DMs to avoid cluttering the log table
                # Errors are already logged via print statements above
                
        elif rule.action_type == "add_to_list":
            # Implementation for adding user to a list
            list_name = rule.config.get("list_name", "")
            print(f"📝 Would add {sender_id} to list: {list_name}")
            # TODO: Implement list management
            
    except Exception as e:
        print(f"❌ [EXECUTE] CRITICAL ERROR in execute_automation_action: {str(e)}")
        print(f"❌ [EXECUTE] Rule ID: {_rule_id if _rule_id is not None else 'None'}, Sender: {sender_id}")
        import traceback
        traceback.print_exc()
        raise  # Re-raise to be caught by task wrapper
    finally:
        # Close task-scoped DB session (avoids DetachedInstanceError when run as background task)
        if _db_task is not None:
            try:
                _db_task.close()
            except Exception as close_err:
                print(f"⚠️ [EXECUTE] Failed to close task DB session: {close_err}")
        # Clean up processing cache after completion (whether success or failure)
        identifier = comment_id if comment_id else message_id
        if identifier and _rule_id is not None:
            processing_key = f"{identifier}_{_rule_id}"
            _processing_rules.pop(processing_key, None)
            print(f"🧹 Cleaned up processing cache for {processing_key}")

@router.post("/accounts", response_model=InstagramAccountResponse, status_code=status.HTTP_201_CREATED)
def create_instagram_account(
    account_data: InstagramAccountCreate,
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id)
):
    # Check account limit
    check_account_limit(user_id, db)

    # Verify Instagram login with real credentials
    client = InstagramClient()
    try:
        client.authenticate(account_data.username, account_data.password)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Instagram authentication failed: {str(e)}"
        )

    # Encrypt credentials
    credentials_dict = {
        "username": account_data.username,
        "password": account_data.password
    }
    encrypted_creds = encrypt_credentials(json.dumps(credentials_dict))

    # Store in database
    ig_account = InstagramAccount(
        user_id=user_id,
        username=account_data.username,
        encrypted_credentials=encrypted_creds,
        is_active=True
    )
    db.add(ig_account)
    db.commit()
    db.refresh(ig_account)

    return ig_account


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_instagram_account(
    account_id: int,
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id)
):
    """Delete an Instagram account and all associated data"""
    # Get the account
    account = db.query(InstagramAccount).filter(
        InstagramAccount.id == account_id,
        InstagramAccount.user_id == user_id
    ).first()
    
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Instagram account not found or you don't have permission to delete it"
        )
    
    # Get all automation rules for this account
    from app.models.automation_rule import AutomationRule
    automation_rules = db.query(AutomationRule).filter(
        AutomationRule.instagram_account_id == account_id
    ).all()
    
    # PRESERVE analytics data: nullify instagram_account_id instead of deleting
    # This allows analytics to persist across disconnect/reconnect for the same user
    # Analytics will be deleted only if a different user connects to the same IG account
    from app.models.automation_rule_stats import AutomationRuleStats
    from app.models.captured_lead import CapturedLead
    from app.models.analytics_event import AnalyticsEvent
    from sqlalchemy import update
    
    print(f"🔍 [DISCONNECT] Preserving analytics data by nullifying instagram_account_id (will be restored on reconnect if same user)")
    
    # Nullify instagram_account_id in analytics events (preserve for potential reconnection)
    analytics_updated = db.execute(
        update(AnalyticsEvent).where(AnalyticsEvent.instagram_account_id == account_id).values(
            instagram_account_id=None
        )
    )
    print(f"✅ [DISCONNECT] Nullified instagram_account_id for {analytics_updated.rowcount} analytics events")
    
    # Nullify instagram_account_id in automation rule stats (preserve stats)
    # Note: AutomationRuleStats doesn't have instagram_account_id, but we'll preserve by rule_id
    # Stats are linked to rules, which will be reconnected, so stats will be accessible again
    
    # Nullify instagram_account_id in captured leads (preserve leads)
    leads_updated = db.execute(
        update(CapturedLead).where(CapturedLead.instagram_account_id == account_id).values(
            instagram_account_id=None
        )
    )
    print(f"✅ [DISCONNECT] Nullified instagram_account_id for {leads_updated.rowcount} captured leads")
    
    # Flush to ensure updates are processed
    db.flush()
    
    # Preserve automation rules: store igsid and user_id in config and nullify instagram_account_id
    # This allows rules to persist across disconnect/reconnect (per user per Instagram account tracking)
    # Similar to how DmLog preserves records by nullifying instagram_account_id
    from sqlalchemy import update
    from sqlalchemy.orm.attributes import flag_modified
    account_igsid = account.igsid
    if account_igsid:
        # Store igsid and user_id in rule config for reconnection matching
        print(f"🔍 [DISCONNECT] Storing disconnected info for {len(automation_rules)} rules. IGSID: {account_igsid}, user_id: {account.user_id}")
        for rule in automation_rules:
            if rule.config and isinstance(rule.config, dict):
                rule.config["disconnected_igsid"] = str(account_igsid)
                rule.config["disconnected_username"] = account.username
                rule.config["disconnected_user_id"] = account.user_id
                # Mark JSON column as modified so SQLAlchemy saves the changes
                flag_modified(rule, "config")
                print(f"✅ [DISCONNECT] Stored disconnected info for rule {rule.id}: igsid={account_igsid}, user_id={account.user_id}")
        db.commit()  # Commit config changes before nullifying instagram_account_id
        print(f"✅ [DISCONNECT] Committed config changes for {len(automation_rules)} rules")
    
    # Nullify instagram_account_id instead of deleting rules (preserves rule count per user per Instagram)
    print(f"🔍 [DISCONNECT] Nullifying instagram_account_id for {len(automation_rules)} rules")
    db.execute(
        update(AutomationRule).where(AutomationRule.instagram_account_id == account_id).values(
            instagram_account_id=None
        )
    )
    db.flush()
    print(f"✅ [DISCONNECT] Nullified instagram_account_id for rules")
    
    # Nullify instagram_account_id in dm_logs (keep rows for usage tracking)
    # username/igsid are stored in dm_logs so usage survives account delete
    from sqlalchemy import update
    from app.models.dm_log import DmLog
    db.execute(
        update(DmLog).where(DmLog.instagram_account_id == account_id).values(
            instagram_account_id=None,
            instagram_username=account.username,
            instagram_igsid=account.igsid,
        )
    )
    db.flush()

    # Delete associated Messages (from Message table)
    from app.models.message import Message
    db.query(Message).filter(
        Message.instagram_account_id == account_id
    ).delete()
    
    # Delete associated Conversations (from Conversation table)
    from app.models.conversation import Conversation
    db.query(Conversation).filter(
        Conversation.instagram_account_id == account_id
    ).delete()
    
    # Delete associated InstagramAudience records (to avoid foreign key constraint violation)
    from app.models.instagram_audience import InstagramAudience
    db.query(InstagramAudience).filter(
        InstagramAudience.instagram_account_id == account_id
    ).delete()
    
    # Delete associated Followers (to avoid foreign key constraint violation)
    from app.models.follower import Follower
    db.query(Follower).filter(
        Follower.instagram_account_id == account_id
    ).delete()
    
    # Flush before deleting account
    db.flush()
    
    # Delete the Instagram account
    db.delete(account)
    db.commit()
    
    return None


@router.get("/test")
async def test():
    return {"status": "ok"}

# OAuth callback moved to instagram_oauth.py
# @router.get("/oauth/callback")
# async def oauth_callback(code: str):
#     return {"code": code, "message": "OAuth successful - save this code"}

@router.get("/test-api")
async def test_instagram_api():
    token = os.getenv("INSTAGRAM_ACCESS_TOKEN")
    account_id = os.getenv("INSTAGRAM_BUSINESS_ACCOUNT_ID")
    
    print(f"Token: {token}")
    print(f"Account ID: {account_id}")
    
    if not token:
        return {"error": "Token not found in .env"}
    
    # Test API call
    url = f"https://graph.facebook.com/v18.0/{account_id}?fields=username,followers_count&access_token={token}"
    response = requests.get(url)
    
    return response.json()


def _dummy_media_for_load_test(media_type: str, limit: int, username: str):
    """Return dummy media for load-test accounts (no Instagram API). Used for stress testing."""
    from datetime import datetime, timezone
    types_map = {
        "posts": [("IMAGE", "FEED"), ("VIDEO", "REELS"), ("CAROUSEL_ALBUM", "FEED")],
        "reels": [("VIDEO", "REELS")],
        "stories": [("IMAGE", "STORY"), ("VIDEO", "STORY")],
        "live": [("VIDEO", "LIVE")],
    }
    variants = types_map.get(media_type, types_map["posts"])
    base = "https://picsum.photos/400/400"
    now = datetime.now(timezone.utc)
    formatted = []
    for i in range(limit):
        mt, mpt = variants[i % len(variants)]
        ts = (now - timedelta(days=i % 60, hours=i % 24)).strftime("%Y-%m-%dT%H:%M:%S+0000")
        formatted.append({
            "id": f"load_test_{username}_{media_type}_{i}",
            "media_type": mt,
            "media_product_type": mpt,
            "caption": f"[Load test] {media_type} #{i + 1} — {username}",
            "media_url": f"{base}?sig={i}",
            "thumbnail_url": f"{base}?sig={i}",
            "permalink": f"https://www.instagram.com/p/load_test_{i}/",
            "timestamp": ts,
            "like_count": (i * 11) % 500,
            "comments_count": (i * 7) % 100,
        })
    return formatted


MEDIA_LIMIT_DEFAULT = 100
MEDIA_LIMIT_MAX = 100


@router.get("/media")
async def get_instagram_media(
    account_id: int = Query(..., description="Instagram account ID"),
    media_type: str = Query("posts", description="Type of media: posts, stories, reels, live"),
    limit: int = Query(MEDIA_LIMIT_DEFAULT, ge=1, le=MEDIA_LIMIT_MAX, description="Items per page (max 100)"),
    after: str | None = Query(None, description="Cursor for next page (from previous response next_cursor)"),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    Fetch Instagram media (posts/reels/stories) for a specific account.
    Returns list of media items with metadata. Supports pagination via 'after' cursor.
    
    For load-test accounts (username load_test_*), returns dummy media instead of calling Instagram API.
    Note: Stories, DMs, and IG Live require Pro plan or higher.
    """
    try:
        # Check Pro plan access for Stories, DMs, and IG Live
        if media_type in ["stories", "live"]:
            from app.utils.plan_enforcement import check_pro_plan_access
            check_pro_plan_access(user_id, db)
        
        # Verify account belongs to user
        account = db.query(InstagramAccount).filter(
            InstagramAccount.id == account_id,
            InstagramAccount.user_id == user_id
        ).first()
        
        if not account:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Instagram account not found"
            )
        
        # Load-test accounts: return dummy media (no API call), at least 1000 for stress testing
        if account.username and account.username.startswith("load_test_"):
            n = max(limit, 1000)
            dummy = _dummy_media_for_load_test(media_type, n, account.username)
            return {"success": True, "media": dummy, "count": len(dummy), "next_cursor": None, "has_more": False}
        
        # Decrypt access token
        if account.encrypted_page_token:
            access_token = decrypt_credentials(account.encrypted_page_token)
        elif account.encrypted_credentials:
            access_token = decrypt_credentials(account.encrypted_credentials)
        else:
            pass  # Will raise exception below
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No access token found for this account"
            )
        
        # Get Instagram Business Account ID
        igsid = account.igsid
        if not igsid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Instagram Business Account ID not found"
            )
        
        media_items = []
        
        next_cursor: str | None = None
        has_more = False

        if media_type == "posts" or media_type == "reels":
            # Fetch posts and reels
            # For Instagram Graph API, we use the media edge
            url = f"https://graph.instagram.com/v21.0/{igsid}/media"
            params = {
                "fields": "id,caption,media_type,media_url,permalink,thumbnail_url,timestamp,like_count,comments_count,media_product_type",
                "limit": limit,
                "access_token": access_token
            }
            if after:
                params["after"] = after

            response = requests.get(url, params=params)

            if response.status_code != 200:
                error_detail = response.text
                print(f"❌ Failed to fetch media: {error_detail}")
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Failed to fetch Instagram media: {error_detail}"
                )

            data = response.json()
            media_items = data.get("data", [])
            paging = data.get("paging") or {}
            cursors = paging.get("cursors") or {}
            next_cursor = cursors.get("after") if isinstance(cursors.get("after"), str) else None
            has_more = "next" in paging and bool(paging.get("next"))

            # Filter by type if specified
            if media_type == "reels":
                # Reels have media_product_type == "REELS"
                media_items = [item for item in media_items if item.get("media_product_type") == "REELS"]
            elif media_type == "posts":
                # Posts/Reels tab: include both FEED (posts) and REELS, but exclude STORY
                media_items = [item for item in media_items if item.get("media_product_type") != "STORY"]

        elif media_type == "stories":
            # Fetch stories (requires stories_read permission and different endpoint)
            # Note: Stories are only available for 24 hours after posting
            url = f"https://graph.instagram.com/v21.0/{igsid}/stories"
            params = {
                "fields": "id,media_type,media_url,thumbnail_url,timestamp,media_product_type,comments_count",
                "limit": limit,
                "access_token": access_token
            }
            if after:
                params["after"] = after

            response = requests.get(url, params=params)

            if response.status_code != 200:
                error_detail = response.text
                print(f"⚠️ Stories may not be available: {error_detail}")
                media_items = []
            else:
                data = response.json()
                media_items = data.get("data", [])
                paging = data.get("paging") or {}
                cursors = paging.get("cursors") or {}
                next_cursor = cursors.get("after") if isinstance(cursors.get("after"), str) else None
                has_more = "next" in paging and bool(paging.get("next"))
                for item in media_items:
                    if "media_product_type" not in item:
                        item["media_product_type"] = "STORY"

        elif media_type == "live":
            url = f"https://graph.instagram.com/v21.0/{igsid}/live_media"
            params = {
                "fields": "id,media_type,media_url,permalink,timestamp,status",
                "limit": limit,
                "access_token": access_token
            }
            if after:
                params["after"] = after

            response = requests.get(url, params=params)

            if response.status_code != 200:
                error_detail = response.text
                print(f"⚠️ Live media may not be available: {error_detail}")
                media_items = []
            else:
                data = response.json()
                media_items = data.get("data", [])
                paging = data.get("paging") or {}
                cursors = paging.get("cursors") or {}
                next_cursor = cursors.get("after") if isinstance(cursors.get("after"), str) else None
                has_more = "next" in paging and bool(paging.get("next"))
        
        # Format response
        formatted_media = []
        for item in media_items:
            formatted_media.append({
                "id": item.get("id"),
                "media_type": item.get("media_type"),  # IMAGE, VIDEO, CAROUSEL_ALBUM
                "media_product_type": item.get("media_product_type"),  # FEED, REELS, STORY, etc.
                "caption": item.get("caption", ""),
                "media_url": item.get("media_url"),
                "thumbnail_url": item.get("thumbnail_url"),
                "permalink": item.get("permalink"),
                "timestamp": item.get("timestamp"),
                "like_count": item.get("like_count", 0),
                "comments_count": item.get("comments_count", 0),
            })
        
        # NOTE: We do NOT auto-disable rules here. Media list is tab-specific (posts OR stories OR reels).
        return {
            "success": True,
            "media": formatted_media,
            "count": len(formatted_media),
            "next_cursor": next_cursor,
            "has_more": has_more,
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error fetching Instagram media: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch Instagram media: {str(e)}"
        )


CONVERSATIONS_LIMIT_DEFAULT = 100
CONVERSATIONS_LIMIT_MAX = 100


@router.get("/conversations")
async def get_instagram_conversations(
    account_id: int = Query(..., description="Instagram account ID"),
    limit: int = Query(CONVERSATIONS_LIMIT_DEFAULT, ge=1, le=CONVERSATIONS_LIMIT_MAX, description="Conversations per page (max 100)"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    sync: bool = Query(False, description="Whether to sync conversations from API"),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    Fetch recent Instagram DM conversations for a specific account.
    Supports pagination via offset/limit. Returns has_more and next_offset for "Load more".
    
    Uses the Conversation model to return structured conversation data.
    Optionally syncs conversations from existing messages if sync=true.
    
    This endpoint requires Pro plan or higher.
    """
    try:
        # Check Pro plan access for DMs
        from app.utils.plan_enforcement import check_pro_plan_access
        check_pro_plan_access(user_id, db)
        
        # Verify account belongs to user
        account = db.query(InstagramAccount).filter(
            InstagramAccount.id == account_id,
            InstagramAccount.user_id == user_id
        ).first()
        
        if not account:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Instagram account not found"
            )
        
        # Get conversations from Conversation table
        from app.models.conversation import Conversation
        from app.models.message import Message
        from sqlalchemy import func, or_, and_
        
        # Sync conversations if requested (do this BEFORE querying)
        if sync:
            from app.services.instagram_sync import sync_instagram_conversations
            try:
                print(f"🔄 Manual sync requested for account {account_id}")
                sync_result = sync_instagram_conversations(user_id, account_id, db, limit)
                print(f"✅ Sync result: {sync_result}")
                # Refresh session to see newly committed data
                db.expire_all()
            except Exception as sync_err:
                print(f"⚠️ Sync warning: {str(sync_err)}")
                import traceback
                traceback.print_exc()
                # Continue even if sync fails
        
        # Query conversations - use subquery to get distinct conversations
        # After migration, unique constraint prevents duplicates, but this handles existing duplicates
        # Get the most recent conversation for each participant
        # FILTER OUT SELF-CONVERSATIONS at database level
        from sqlalchemy import func
        account_igsid = account.igsid
        account_username = account.username
        
        subquery_filter = [
            Conversation.instagram_account_id == account_id,
            Conversation.user_id == user_id
        ]
        
        # Filter out self-conversations at subquery level
        if account_igsid:
            subquery_filter.append(Conversation.participant_id != account_igsid)
        if account_username:
            subquery_filter.append(Conversation.participant_name != account_username)
        
        subquery = db.query(
            func.max(Conversation.id).label('max_id')
        ).filter(*subquery_filter).group_by(
            Conversation.user_id,
            Conversation.instagram_account_id,
            Conversation.participant_id
        ).subquery()
        
        conversations_query = db.query(Conversation).join(
            subquery,
            Conversation.id == subquery.c.max_id
        ).filter(
            Conversation.instagram_account_id == account_id,
            Conversation.user_id == user_id
        )
        
        # Additional filtering at main query level for safety
        if account_igsid:
            conversations_query = conversations_query.filter(Conversation.participant_id != account_igsid)
        if account_username:
            conversations_query = conversations_query.filter(Conversation.participant_name != account_username)
        
        conversations_query = conversations_query.order_by(Conversation.updated_at.desc()).offset(offset).limit(limit + 1)
        
        raw_list = conversations_query.all()
        has_more = len(raw_list) > limit
        conversations_list = raw_list[:limit]
        
        # Debug: Log conversation count
        print(f"📋 Found {len(conversations_list)} conversations in Conversation table for account {account_id}")
        
        # Auto-sync if no conversations exist (first time or after migration)
        # This will build conversations from existing messages in the database
        if len(conversations_list) == 0 and not sync:
            print(f"🔄 No conversations found for account {account_id}, auto-syncing from existing messages...")
            try:
                from app.services.instagram_sync import sync_instagram_conversations
                sync_result = sync_instagram_conversations(user_id, account_id, db, limit)
                print(f"✅ Auto-sync result: {sync_result}")
                # Refresh session to see newly committed data
                db.expire_all()
                # Re-query after sync
                raw_list = conversations_query.all()
                has_more = len(raw_list) > limit
                conversations_list = raw_list[:limit]
                print(f"📊 Conversations after sync: {len(conversations_list)}")
                
                # Debug: Log conversation details
                if len(conversations_list) > 0:
                    for conv in conversations_list:
                        print(f"   - Conversation ID: {conv.id}, Participant: {conv.participant_name or conv.participant_id}, Last message: {conv.last_message[:50] if conv.last_message else 'None'}...")
                
                # If still no conversations after sync, try fallback to Message table
                if len(conversations_list) == 0:
                    print("🔄 Still no conversations after sync, checking Message table directly...")
                    # Check if there are any messages at all
                    message_count = db.query(func.count(Message.id)).filter(
                        Message.instagram_account_id == account_id,
                        Message.user_id == user_id
                    ).scalar() or 0
                    print(f"📨 Total messages in database for this account: {message_count}")
                    # The fallback logic below will handle this
            except Exception as sync_err:
                print(f"⚠️ Auto-sync warning: {str(sync_err)}")
                import traceback
                traceback.print_exc()
                # Continue with empty list - fallback logic will try to build from Message table
        
        # Format conversations from Conversation model
        if len(conversations_list) > 0:
            formatted_conversations = []
            # Get account's IGSID and username for self-conversation filtering
            account_igsid = account.igsid
            account_username = account.username
            
            for conv in conversations_list:
                # FILTER OUT SELF-CONVERSATIONS: Skip if participant matches account's own IGSID or username
                # Convert to strings for comparison to handle type mismatches
                participant_id_str = str(conv.participant_id) if conv.participant_id else None
                account_igsid_str = str(account_igsid) if account_igsid else None
                
                if account_igsid_str and participant_id_str and participant_id_str == account_igsid_str:
                    print(f"🚫 Filtering out self-conversation (participant_id={participant_id_str} matches account IGSID={account_igsid_str})")
                    continue
                if account_username and conv.participant_name == account_username:
                    print(f"🚫 Filtering out self-conversation (participant_name={conv.participant_name} matches account username)")
                    continue
                
                # Additional check: If participant_name is "Unknown" or None, verify participant_id doesn't match account
                # This catches self-conversations that might be labeled as "Unknown"
                if (not conv.participant_name or conv.participant_name == "Unknown") and account_igsid_str and participant_id_str == account_igsid_str:
                    print(f"🚫 Filtering out self-conversation labeled as 'Unknown' (participant_id={participant_id_str} matches account IGSID)")
                    continue
                
                # Get message count for this conversation
                message_count = db.query(func.count(Message.id)).filter(
                    Message.conversation_id == conv.id
                ).scalar() or 0
                
                # Determine username - use participant_name if available, otherwise use participant_id
                # If participant_id is numeric, use "Unknown" for display
                participant_display = conv.participant_name or str(conv.participant_id) if conv.participant_id else "Unknown"
                if participant_display.isdigit():
                    participant_display = "Unknown"
                
                # Get the latest message to determine if it's from bot
                latest_msg = db.query(Message).filter(
                    Message.conversation_id == conv.id
                ).order_by(Message.created_at.desc()).first()
                
                formatted_conversations.append({
                    "id": str(conv.id),
                    "username": participant_display,
                    "user_id": str(conv.participant_id) if conv.participant_id else "",
                    "last_message_at": conv.updated_at.isoformat() if conv.updated_at else None,
                    "last_message": conv.last_message or "",
                    "last_message_is_from_bot": latest_msg.is_from_bot if latest_msg else False,
                    "message_count": message_count
                })
            
            return {
                "success": True,
                "conversations": formatted_conversations,
                "count": len(formatted_conversations),
                "has_more": has_more,
                "next_offset": offset + limit if has_more else None,
            }
        
        # Fallback: If no conversations in Conversation table, use old logic
        # This maintains backward compatibility
        from app.models.dm_log import DmLog
        
        # Get distinct conversations (group by sender/recipient)
        # A conversation is identified by the other party's username
        account_igsid = account.igsid or str(account_id)
        
        # Get conversations where we received messages (incoming) from Message table
        # Group by sender_id (which is always present) to handle cases where sender_username is None
        incoming_convs = db.query(
            Message.sender_username,
            Message.sender_id,
            func.max(Message.created_at).label('last_message_at'),
            func.count(Message.id).label('message_count')
        ).filter(
            Message.instagram_account_id == account_id,
            Message.user_id == user_id,
            Message.is_from_bot == False,  # Received messages
            Message.sender_id.isnot(None)  # Ensure sender_id is not None
        ).group_by(
            Message.sender_id,  # Group by sender_id first (always present)
            Message.sender_username  # Then by username (may be None)
        ).all()
        
        # Get conversations where we sent messages (outgoing) from Message table
        # Group by recipient_id (which is always present) to handle cases where recipient_username is None
        outgoing_convs = db.query(
            Message.recipient_username,
            Message.recipient_id,
            func.max(Message.created_at).label('last_message_at'),
            func.count(Message.id).label('message_count')
        ).filter(
            Message.instagram_account_id == account_id,
            Message.user_id == user_id,
            Message.is_from_bot == True,  # Sent messages
            Message.recipient_id.isnot(None)  # Ensure recipient_id is not None
        ).group_by(
            Message.recipient_id,  # Group by recipient_id first (always present)
            Message.recipient_username  # Then by username (may be None)
        ).all()
        
        # Fallback: Also check DmLog for conversations (if Message table is empty)
        # This helps show conversations even if Message table hasn't been populated yet
        if len(incoming_convs) == 0 and len(outgoing_convs) == 0:
            # Get conversations from DmLog
            dm_log_convs = db.query(
            DmLog.recipient_username,
            func.max(DmLog.sent_at).label('last_message_at'),
            func.count(DmLog.id).label('message_count')
        ).filter(
            DmLog.instagram_account_id == account_id,
            DmLog.user_id == user_id
        ).group_by(
            DmLog.recipient_username
            ).all()
            
            # Convert DmLog conversations to match Message format
            for dm_conv in dm_log_convs:
                username = dm_conv.recipient_username or 'unknown'
                # Get latest message from DmLog
                latest_dm = db.query(DmLog).filter(
                DmLog.instagram_account_id == account_id,
                    DmLog.recipient_username == dm_conv.recipient_username
            ).order_by(DmLog.sent_at.desc()).first()
            
                # Add to outgoing_convs format
                class DmLogConv:
                    def __init__(self, username, last_at, count):
                        self.recipient_username = username
                        self.recipient_id = username  # Use username as ID
                        self.last_message_at = last_at
                        self.message_count = count
                
                outgoing_convs.append(DmLogConv(
                    username,
                    dm_conv.last_message_at,
                    dm_conv.message_count
                ))
        
        # Merge conversations (use participant_id as key to prevent duplicates)
        # This fixes Issue 2: Duplicate recipients displaying same Instagram user twice
        conversations_map = {}
        
        # Get account's IGSID and username for self-conversation filtering
        account_igsid = account.igsid
        account_username = account.username
        
        # Process incoming conversations
        for conv in incoming_convs:
            # Use sender_username if available, otherwise use sender_id as string
            # If sender_id is None, skip this conversation
            if not conv.sender_id:
                continue
            
            # FILTER OUT SELF-CONVERSATIONS: Skip if sender_id matches account's IGSID
            sender_id_str = str(conv.sender_id)
            if account_igsid and sender_id_str == account_igsid:
                print(f"🚫 Filtering out self-conversation (sender_id={sender_id_str} matches account IGSID)")
                continue
            if account_username and conv.sender_username == account_username:
                print(f"🚫 Filtering out self-conversation (sender_username={conv.sender_username} matches account username)")
                continue
            
            username = conv.sender_username or str(conv.sender_id)
            # Use "Unknown" if we only have a numeric ID (not a real username)
            display_username = username if (username and not username.isdigit()) else "Unknown"
            
            # Use participant_id (sender_id) as the key to prevent duplicates
            # This ensures each unique participant appears only once
            participant_key = sender_id_str
            
            # Check if we should add/update this conversation
            should_add = participant_key not in conversations_map
            if not should_add and conv.last_message_at:
                # Compare dates properly
                existing_time_str = conversations_map[participant_key].get('last_message_at')
                if existing_time_str:
                    try:
                        from dateutil import parser
                        existing_dt = parser.parse(existing_time_str)
                        should_add = conv.last_message_at > existing_dt
                    except:
                        should_add = True
                else:
                    should_add = True
            
            if should_add:
                # Get latest message for this conversation
                # Use sender_id for matching (more reliable than username which may be None)
                latest_msg = db.query(Message).filter(
                    Message.instagram_account_id == account_id,
                    Message.user_id == user_id,
                    Message.is_from_bot == False,
                    Message.sender_id == sender_id_str
                ).order_by(Message.created_at.desc()).first()
                
                # Get message content (handle both message_text and content fields)
                message_content = ""
                if latest_msg:
                    message_content = latest_msg.get_content() if hasattr(latest_msg, 'get_content') else (latest_msg.message_text or latest_msg.content or "")
                
                conversations_map[participant_key] = {
                    "id": participant_key,  # Use participant_id as id
                    "username": display_username,
                    "user_id": sender_id_str,
                    "last_message_at": conv.last_message_at.isoformat() if conv.last_message_at else None,
                    "last_message": message_content,
                    "last_message_is_from_bot": latest_msg.is_from_bot if latest_msg else False,
                    "message_count": conv.message_count
                }
        
        # Process outgoing conversations (add if not already in map)
        for conv in outgoing_convs:
            # Use recipient_username if available, otherwise use recipient_id as string
            # If recipient_id is None, skip this conversation
            if not conv.recipient_id:
                continue
            
            recipient_id_str = str(conv.recipient_id)
            
            # FILTER OUT SELF-CONVERSATIONS: Skip if recipient_id matches account's IGSID
            if account_igsid and recipient_id_str == account_igsid:
                print(f"🚫 Filtering out self-conversation (recipient_id={recipient_id_str} matches account IGSID)")
                continue
            if account_username and conv.recipient_username == account_username:
                print(f"🚫 Filtering out self-conversation (recipient_username={conv.recipient_username} matches account username)")
                continue
            
            username = conv.recipient_username or str(conv.recipient_id)
            # Use "Unknown" if we only have a numeric ID (not a real username)
            display_username = username if (username and not username.isdigit()) else "Unknown"
            
            # Use participant_id (recipient_id) as the key to prevent duplicates
            # This ensures each unique participant appears only once
            participant_key = recipient_id_str
                
            if participant_key not in conversations_map:
                # Get latest message for this conversation (try Message table first, then DmLog)
                # Use recipient_id for matching (more reliable than username which may be None)
                latest_msg = db.query(Message).filter(
                    Message.instagram_account_id == account_id,
                    Message.user_id == user_id,
                    Message.is_from_bot == True,
                    Message.recipient_id == recipient_id_str
                ).order_by(Message.created_at.desc()).first()
                
                # If not in Message table, check DmLog
                if not latest_msg:
                    latest_dm = db.query(DmLog).filter(
                        DmLog.instagram_account_id == account_id,
                        DmLog.recipient_username == username
                    ).order_by(DmLog.sent_at.desc()).first()
                    if latest_dm:
                        # Create a mock message object for DmLog
                        class MockMessage:
                            def __init__(self, text, sent_at):
                                self.message_text = text
                                self.is_from_bot = True
                                self.created_at = sent_at
                        latest_msg = MockMessage(latest_dm.message, latest_dm.sent_at) if latest_dm else None
                
                # Get message content (handle both message_text and content fields)
                message_content = ""
                if latest_msg:
                    message_content = latest_msg.get_content() if hasattr(latest_msg, 'get_content') else (latest_msg.message_text or latest_msg.content or "")
                
                conversations_map[participant_key] = {
                    "id": participant_key,  # Use participant_id as id
                    "username": display_username,
                    "user_id": recipient_id_str,
                    "last_message_at": conv.last_message_at.isoformat() if conv.last_message_at else None,
                    "last_message": message_content,
                    "last_message_is_from_bot": latest_msg.is_from_bot if latest_msg else True,
                    "message_count": conv.message_count
                }
        
        # Convert to list and get latest message for each
        conversations = []
        for participant_key, conv_data in conversations_map.items():
            # Get the absolute latest message for this conversation (sent or received)
            # Use user_id for matching (more reliable than username which may be None or numeric)
            user_id_str = conv_data.get('user_id', '')
            
            # FILTER OUT SELF-CONVERSATIONS: Double-check participant doesn't match account
            # Convert to strings for comparison to handle type mismatches
            account_igsid_str = str(account_igsid) if account_igsid else None
            
            if account_igsid_str and user_id_str == account_igsid_str:
                print(f"🚫 Filtering out self-conversation in final check (user_id={user_id_str} matches account IGSID={account_igsid_str})")
                continue
            if account_username and conv_data.get('username') == account_username:
                print(f"🚫 Filtering out self-conversation in final check (username={conv_data.get('username')} matches account username)")
                continue
            
            # Additional check: If username is "Unknown", verify user_id doesn't match account
            # This catches self-conversations that might be labeled as "Unknown"
            if conv_data.get('username') == "Unknown" and account_igsid_str and user_id_str == account_igsid_str:
                print(f"🚫 Filtering out self-conversation labeled as 'Unknown' in final check (user_id={user_id_str} matches account IGSID)")
                continue
            
            latest_msg = db.query(Message).filter(
                Message.instagram_account_id == account_id,
                Message.user_id == user_id,
                or_(
                    (Message.is_from_bot == False) & (Message.sender_id == user_id_str),
                    (Message.is_from_bot == True) & (Message.recipient_id == user_id_str)
                )
            ).order_by(Message.created_at.desc()).first()
            
            # If not in Message table, check DmLog as fallback
            if not latest_msg:
                username = conv_data.get('username', '')
                latest_dm = db.query(DmLog).filter(
                DmLog.instagram_account_id == account_id,
                    DmLog.recipient_username == username
            ).order_by(DmLog.sent_at.desc()).first()
                if latest_dm:
                    class MockMessage:
                        def __init__(self, text, sent_at):
                            self.message_text = text
                            self.is_from_bot = True
                            self.created_at = sent_at
                    latest_msg = MockMessage(latest_dm.message, latest_dm.sent_at)
            
            if latest_msg:
                conv_data['last_message_at'] = latest_msg.created_at.isoformat() if latest_msg.created_at else None
                # Get message content (handle both message_text and content fields)
                if hasattr(latest_msg, 'get_content'):
                    message_content = latest_msg.get_content() or "[Media]"
                else:
                    message_content = (latest_msg.message_text or latest_msg.content or "[Media]")
                conv_data['last_message'] = message_content
                conv_data['last_message_is_from_bot'] = latest_msg.is_from_bot
            elif not conv_data.get('last_message_at'):
                # If no latest message found and no timestamp, skip this conversation
                continue
            
            conversations.append(conv_data)
        
        # Sort by last_message_at (handle None values)
        conversations.sort(key=lambda x: x.get('last_message_at') or '', reverse=True)
        # Paginate fallback list
        paginated = conversations[offset : offset + limit + 1]
        has_more_fb = len(paginated) > limit
        conversations = paginated[:limit]
        
        return {
            "success": True,
            "conversations": conversations,
            "count": len(conversations),
            "has_more": has_more_fb,
            "next_offset": offset + limit if has_more_fb else None,
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error fetching conversations: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch conversations: {str(e)}"
        )


MESSAGES_LIMIT_DEFAULT = 100
MESSAGES_LIMIT_MAX = 100


@router.get("/conversations/{username}/messages")
async def get_conversation_messages(
    username: str,
    account_id: int = Query(..., description="Instagram account ID"),
    limit: int = Query(MESSAGES_LIMIT_DEFAULT, ge=1, le=MESSAGES_LIMIT_MAX, description="Messages per page (max 100)"),
    offset: int = Query(0, ge=0, description="Offset for pagination (0 = newest page)"),
    participant_user_id: str = Query(None, description="Instagram user_id (IGSID) of the participant - more reliable than username for Unknown users"),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    Fetch messages for a specific conversation (by recipient username).
    Newest-first pagination: offset=0 returns most recent messages. Use next_offset for "Load older".
    Returns messages in chronological order (oldest first) for display.
    """
    try:
        # Check Pro plan access for DMs
        from app.utils.plan_enforcement import check_pro_plan_access
        check_pro_plan_access(user_id, db)
        
        # Verify account belongs to user
        account = db.query(InstagramAccount).filter(
            InstagramAccount.id == account_id,
            InstagramAccount.user_id == user_id
        ).first()
        
        if not account:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Instagram account not found"
            )
        
        from app.models.message import Message
        from app.models.dm_log import DmLog
        from app.models.conversation import Conversation
        
        # Get account's Instagram ID (recipient_id in messages)
        account_igsid = account.igsid or str(account_id)
        
        # Get participant_id to search for messages
        # Priority: 1) participant_user_id query param, 2) lookup from Conversation table, 3) use username
        participant_id_to_search = None
        
        # If participant_user_id is provided (more reliable), use it directly
        if participant_user_id:
            participant_id_to_search = str(participant_user_id)
            print(f"🔍 Using provided participant_user_id: {participant_id_to_search}")
        elif username == "Unknown" or not username:
            # Find the conversation - "Unknown" means participant_name is None or numeric ID
            # Try both: participant_name == "Unknown" OR participant_name == None
            from sqlalchemy import or_
            conversation = db.query(Conversation).filter(
                Conversation.instagram_account_id == account_id,
                Conversation.user_id == user_id,  # App user_id
                or_(
                    Conversation.participant_name == "Unknown",
                    Conversation.participant_name.is_(None),
                    Conversation.participant_name == username
                )
            ).first()
            
            if conversation:
                participant_id_to_search = conversation.participant_id  # This is the Instagram user_id (IGSID)
                print(f"🔍 Found conversation for 'Unknown': participant_id={participant_id_to_search}, participant_name={conversation.participant_name}")
            else:
                print(f"⚠️ No conversation found for username='{username}'")
        
        # Build query conditions - search by both username AND user_id
        # This handles cases where username might be "Unknown" but we have the actual Instagram user_id
        from sqlalchemy import or_, and_
        message_conditions = []
        
        # First, try to find conversation by participant_id to get conversation_id (most reliable)
        conversation = None
        if participant_id_to_search:
            conversation = db.query(Conversation).filter(
                Conversation.instagram_account_id == account_id,
                Conversation.user_id == user_id,
                Conversation.participant_id == participant_id_to_search
            ).first()
        
        # If we have a conversation_id, use it directly (most reliable)
        has_more = False
        if conversation and conversation.id:
            query = db.query(Message).filter(
                Message.instagram_account_id == account_id,
                Message.user_id == user_id,
                Message.conversation_id == conversation.id
            )
            raw = query.order_by(Message.created_at.desc()).offset(offset).limit(limit + 1).all()
            has_more = len(raw) > limit
            messages = list(reversed(raw[:limit]))  # chronological (oldest first) for display
            print(f"📨 Found {len(messages)} messages using conversation_id={conversation.id}")
        else:
            # Fallback: search by participant_id and username
            if participant_id_to_search:
                # Search by Instagram participant_id (for Unknown users)
                message_conditions.append(
                    and_(Message.is_from_bot == True, Message.recipient_id == str(participant_id_to_search))
                )
                message_conditions.append(
                    and_(Message.is_from_bot == False, Message.sender_id == str(participant_id_to_search))
                )
            
            # Always also search by username (works for both known and unknown)
            message_conditions.append(
                and_(Message.is_from_bot == True, Message.recipient_username == username)
            )
            message_conditions.append(
                and_(Message.is_from_bot == False, Message.sender_username == username)
            )
            
            # Combine all conditions with OR
            combined_condition = or_(*message_conditions) if message_conditions else None
            
            # Get messages
            query = db.query(Message).filter(
                Message.instagram_account_id == account_id,
                Message.user_id == user_id
            )
            
            if combined_condition:
                query = query.filter(combined_condition)
            
            raw = query.order_by(Message.created_at.desc()).offset(offset).limit(limit + 1).all()
            has_more = len(raw) > limit
            messages = list(reversed(raw[:limit]))
            print(f"📨 Found {len(messages)} messages using participant_id/username search")
        
        print(f"📨 Total messages found: {len(messages)} for username='{username}', participant_id={participant_id_to_search}")
        
        # If no messages in Message table, check DmLog as fallback (no pagination)
        if len(messages) == 0:
            dm_logs = db.query(DmLog).filter(
                DmLog.instagram_account_id == account_id,
                DmLog.user_id == user_id,
                DmLog.recipient_username == username
            ).order_by(DmLog.sent_at.asc()).limit(limit).all()
            
            # Convert DmLog entries to Message-like format
            for dm_log in dm_logs:
                class MockMessage:
                    def __init__(self, log_id, text, sent_at, recipient):
                        self.id = log_id
                        self.message_id = None
                        self.message_text = text
                        self.is_from_bot = True
                        self.sender_username = None
                        self.recipient_username = recipient
                        self.has_attachments = False
                        self.attachments = None
                        self.created_at = sent_at
                messages.append(MockMessage(dm_log.id, dm_log.message, dm_log.sent_at, dm_log.recipient_username))
        
        # DmLog fallback: no pagination (messages are MockMessage, not Message)
        if len(messages) > 0 and not hasattr(messages[0], "conversation_id"):
            has_more = False

        # Format messages
        formatted_messages = []
        for msg in messages:
            # Use get_content() method if available, otherwise fallback to message_text or content
            message_text = msg.get_content() if hasattr(msg, 'get_content') else (msg.message_text or msg.content or "")
            
            formatted_messages.append({
                "id": msg.id,
                "message_id": msg.message_id,
                "text": message_text,
                "is_from_bot": msg.is_from_bot,
                "sender_username": msg.sender_username,
                "recipient_username": msg.recipient_username,
                "has_attachments": msg.has_attachments,
                "attachments": msg.attachments,
                "created_at": msg.created_at.isoformat() if msg.created_at else None
            })
        
        print(f"✅ Returning {len(formatted_messages)} formatted messages")
        
        return {
            "success": True,
            "messages": formatted_messages,
            "count": len(formatted_messages),
            "has_more": has_more,
            "next_offset": offset + limit if has_more else None,
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error fetching conversation messages: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch messages: {str(e)}"
        )


@router.post("/conversations/{username}/messages")
async def send_conversation_message(
    username: str,
    message_data: dict = Body(..., description="Message data with 'text' field"),
    account_id: int = Query(..., description="Instagram account ID"),
    participant_user_id: str = Query(None, description="Instagram user_id (IGSID) of the participant"),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    Send a message to a specific conversation.
    Requires: { "text": "message text" }
    """
    try:
        # Check Pro plan access for DMs
        from app.utils.plan_enforcement import check_pro_plan_access
        check_pro_plan_access(user_id, db)
        
        # Verify account belongs to user
        account = db.query(InstagramAccount).filter(
            InstagramAccount.id == account_id,
            InstagramAccount.user_id == user_id
        ).first()
        
        if not account:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Instagram account not found"
            )
        
        # Get message text from request body
        message_text = message_data.get("text", "").strip() if isinstance(message_data, dict) else ""
        if not message_text:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Message text is required"
            )
        
        # Get recipient user_id from conversation
        from app.models.conversation import Conversation
        from sqlalchemy import or_
        conversation = None
        
        if participant_user_id:
            # Use provided participant_user_id (most reliable)
            conversation = db.query(Conversation).filter(
                Conversation.instagram_account_id == account_id,
                Conversation.user_id == user_id,
                Conversation.participant_id == str(participant_user_id)
            ).first()
        
        if not conversation:
            # Try to find by username
            # For "Unknown" users, also check if participant_name is None
            if username == "Unknown" or not username:
                conversation = db.query(Conversation).filter(
                    Conversation.instagram_account_id == account_id,
                    Conversation.user_id == user_id,
                    or_(
                        Conversation.participant_name == "Unknown",
                        Conversation.participant_name.is_(None)
                    )
                ).first()
            else:
                conversation = db.query(Conversation).filter(
                    Conversation.instagram_account_id == account_id,
                    Conversation.user_id == user_id,
                    Conversation.participant_name == username
                ).first()
        
        if not conversation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conversation not found"
            )
        
        recipient_id = conversation.participant_id
        
        # PREVENT SENDING MESSAGES TO SELF: Check if recipient matches account's own IGSID or username
        account_igsid = account.igsid
        account_username = account.username
        
        if account_igsid and recipient_id == account_igsid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot send messages to yourself"
            )
        if account_username and conversation.participant_name == account_username:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot send messages to yourself"
            )
        
        # Get access token and page_id
        access_token = None
        page_id = None
        
        if account.encrypted_page_token:
            try:
                access_token = decrypt_credentials(account.encrypted_page_token)
            except Exception as e:
                print(f"⚠️ Failed to decrypt page token: {str(e)}")
                if account.encrypted_credentials:
                    access_token = decrypt_credentials(account.encrypted_credentials)
        elif account.encrypted_credentials:
            access_token = decrypt_credentials(account.encrypted_credentials)
        
        if not access_token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Instagram account access token not available"
            )
        
        page_id = account.page_id
        
        # Capture timestamp RIGHT BEFORE sending to match Instagram's timing exactly
        # Instagram displays times in UTC+8, so we add 8 hours to match Instagram's display
        from datetime import datetime, timedelta
        message_timestamp = datetime.utcnow() + timedelta(hours=8)
        
        # Send the message via Instagram API
        from app.utils.instagram_api import send_dm
        try:
            result = send_dm(
                recipient_id=recipient_id,
                message=message_text,
                page_access_token=access_token,
                page_id=page_id,
                buttons=None,
                quick_replies=None
            )
            
            # Store the sent message in the database
            from app.models.message import Message
            
            # Get sender_id (our account's Instagram ID)
            sender_id = account.igsid or str(account_id)
            
            message_id_from_api = result.get("message_id") or result.get("id")
            
            # Use timestamp captured before API call (matches Instagram's timing)
            sent_message = Message(
                instagram_account_id=account_id,
                user_id=user_id,
                conversation_id=conversation.id,
                message_id=message_id_from_api,
                platform_message_id=message_id_from_api,  # Also set platform_message_id
                message_text=message_text,
                content=message_text,  # Also set content field
                is_from_bot=True,  # Sent by us
                sender_id=str(sender_id),  # Our account's Instagram ID
                sender_username=account.username,  # Our account username
                recipient_username=username,
                recipient_id=recipient_id,
                has_attachments=False,
                attachments=None,
                created_at=message_timestamp  # Explicit timestamp for precise timing
            )
            db.add(sent_message)
            
            # Update conversation's last_message
            conversation.last_message = message_text
            conversation.last_message_at = datetime.utcnow()
            conversation.last_message_is_from_bot = True
            conversation.updated_at = datetime.utcnow()
            
            # Log DM_SENT for Analytics (aligns with Message Views "Messages Sent")
            try:
                from app.utils.analytics import log_analytics_event_sync
                from app.models.analytics_event import EventType
                log_analytics_event_sync(
                    db=db,
                    user_id=user_id,
                    event_type=EventType.DM_SENT,
                    rule_id=None,
                    media_id=None,
                    instagram_account_id=account_id,
                    metadata={"recipient_username": username, "source": "messages_ui"}
                )
            except Exception as _ax:
                pass
            
            db.commit()
            db.refresh(sent_message)
            
            return {
                "success": True,
                "message": {
                    "id": sent_message.id,
                    "message_id": sent_message.message_id,
                    "text": sent_message.message_text,
                    "is_from_bot": sent_message.is_from_bot,
                    "sender_username": sent_message.sender_username,
                    "recipient_username": sent_message.recipient_username,
                    "has_attachments": sent_message.has_attachments,
                    "attachments": sent_message.attachments,
                    "created_at": sent_message.created_at.isoformat() if sent_message.created_at else None
                }
            }
            
        except Exception as send_error:
            print(f"❌ Failed to send message via Instagram API: {str(send_error)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to send message: {str(send_error)}"
            )
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error sending message: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to send message: {str(e)}"
        )


@router.post("/conversations/sync")
async def sync_conversations_endpoint(
    account_id: int = Query(..., description="Instagram account ID"),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    Dedicated sync endpoint for conversations (like competitors).
    This can be called independently to force a sync.
    """
    try:
        # Check Pro plan access for DMs
        from app.utils.plan_enforcement import check_pro_plan_access
        check_pro_plan_access(user_id, db)
        
        # Verify account belongs to user
        account = db.query(InstagramAccount).filter(
            InstagramAccount.id == account_id,
            InstagramAccount.user_id == user_id
        ).first()
        
        if not account:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Instagram account not found"
            )
        
        # Sync conversations
        from app.services.instagram_sync import sync_instagram_conversations
        sync_result = sync_instagram_conversations(user_id, account_id, db, limit=100)
        
        # Refresh session
        db.expire_all()
        
        # Get updated conversations count
        from app.models.conversation import Conversation
        conversation_count = db.query(Conversation).filter(
            Conversation.instagram_account_id == account_id,
            Conversation.user_id == user_id
        ).count()
        
        return {
            "success": True,
            "sync_result": sync_result,
            "conversations_count": conversation_count,
            "message": f"Synced successfully. Found {conversation_count} conversation(s)."
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error syncing conversations: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to sync conversations: {str(e)}"
        )


@router.get("/conversations/stats")
async def get_conversation_stats(
    account_id: int = Query(..., description="Instagram account ID"),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    Get conversation statistics for the dashboard.
    Returns: total conversations, unread count, messages sent, messages received
    """
    try:
        # Check Pro plan access for DMs
        from app.utils.plan_enforcement import check_pro_plan_access
        check_pro_plan_access(user_id, db)
        
        # Verify account belongs to user
        account = db.query(InstagramAccount).filter(
            InstagramAccount.id == account_id,
            InstagramAccount.user_id == user_id
        ).first()
        
        if not account:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Instagram account not found"
            )
        
        from app.models.message import Message
        from app.models.conversation import Conversation
        from sqlalchemy import func, distinct
        
        # Get account's IGSID and username for self-conversation filtering
        account_igsid = account.igsid
        account_username = account.username
        
        # Get total conversations from Conversation table using same deduplication logic as list endpoint
        # Use subquery to get distinct conversations by participant_id (same as list endpoint)
        # This ensures count matches what's displayed in the list
        # FILTER OUT SELF-CONVERSATIONS at database level (including "Unknown" self-conversations)
        subquery_filter = [
            Conversation.instagram_account_id == account_id,
            Conversation.user_id == user_id
        ]
        
        # Filter out self-conversations at subquery level
        if account_igsid:
            subquery_filter.append(Conversation.participant_id != account_igsid)
        if account_username:
            subquery_filter.append(Conversation.participant_name != account_username)
        
        subquery = db.query(
            func.max(Conversation.id).label('max_id')
        ).filter(*subquery_filter).group_by(
            Conversation.user_id,
            Conversation.instagram_account_id,
            Conversation.participant_id
        ).subquery()
        
        # Count distinct conversations, filtering out self-conversations
        conversations_query = db.query(Conversation).join(
            subquery,
            Conversation.id == subquery.c.max_id
        ).filter(
            Conversation.instagram_account_id == account_id,
            Conversation.user_id == user_id
        )
        
        # Additional filtering at main query level for safety
        if account_igsid:
            conversations_query = conversations_query.filter(
                Conversation.participant_id != account_igsid
            )
        if account_username:
            conversations_query = conversations_query.filter(
                Conversation.participant_name != account_username
            )
        
        total_conversations = conversations_query.count()
        
        # Fallback: If no conversations in Conversation table, count from Message table
        # Use same deduplication logic as conversations list endpoint
        if total_conversations == 0:
            # Get account's IGSID for filtering
            account_igsid = account.igsid or str(account_id)
            
            # Get all unique participant IDs from incoming messages (excluding self)
            incoming_ids_query = db.query(Message.sender_id).filter(
                Message.instagram_account_id == account_id,
                Message.user_id == user_id,
                Message.is_from_bot == False,
                Message.sender_id.isnot(None)
            )
            if account_igsid:
                incoming_ids_query = incoming_ids_query.filter(Message.sender_id != account_igsid)
            incoming_ids = {row[0] for row in incoming_ids_query.distinct().all()}
            
            # Get all unique participant IDs from outgoing messages (excluding self)
            outgoing_ids_query = db.query(Message.recipient_id).filter(
                Message.instagram_account_id == account_id,
                Message.user_id == user_id,
                Message.is_from_bot == True,
                Message.recipient_id.isnot(None)
            )
            if account_igsid:
                outgoing_ids_query = outgoing_ids_query.filter(Message.recipient_id != account_igsid)
            outgoing_ids = {row[0] for row in outgoing_ids_query.distinct().all()}
            
            # Union to get unique participants (conversations can have both incoming and outgoing)
            unique_participants = incoming_ids.union(outgoing_ids)
            total_conversations = len(unique_participants)
        
        # For now, unread is 0 (we don't track read status yet)
        unread = 0
        
        # Get messages sent (by our bot)
        # Counts all messages where is_from_bot = True (messages sent by the account)
        messages_sent = db.query(Message).filter(
            Message.instagram_account_id == account_id,
            Message.user_id == user_id,
            Message.is_from_bot == True
        ).count()
        
        # Get messages received (from users)
        # Counts all messages where is_from_bot = False (incoming messages from other users)
        # This includes all messages received from Instagram users, excluding self-messages
        # Note: Self-messages are already filtered out at the conversation level, so messages
        # from self-conversations shouldn't exist, but we add an extra safety check here
        messages_received_query = db.query(Message).filter(
            Message.instagram_account_id == account_id,
            Message.user_id == user_id,
            Message.is_from_bot == False
        )
        
        # Additional safety: Filter out self-messages if account_igsid is available
        if account_igsid:
            messages_received_query = messages_received_query.filter(
                Message.sender_id != account_igsid
            )
        
        messages_received = messages_received_query.count()
        
        return {
            "success": True,
            "total_conversations": total_conversations,
            "unread": unread,
            "messages_sent": messages_sent,
            "messages_received": messages_received
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error fetching conversation stats: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch stats: {str(e)}"
        )


# @router.get("/test-api")
# async def test_instagram_api():
#     token = os.getenv("INSTAGRAM_ACCESS_TOKEN")
    
#     # First get your pages
#     url = f"https://graph.facebook.com/v18.0/me/accounts?access_token={token}"
#     response = requests.get(url)
    
#     return response.json()