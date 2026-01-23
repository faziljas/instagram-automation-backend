import asyncio
import json
import os
import sys
import logging
import requests
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status, Header, Query, Request, BackgroundTasks, Body
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models.instagram_account import InstagramAccount
from app.models.automation_rule import AutomationRule
from app.models.dm_log import DmLog
from app.schemas.instagram import InstagramAccountCreate, InstagramAccountResponse
from app.utils.encryption import encrypt_credentials, decrypt_credentials
from app.services.instagram_client import InstagramClient
from app.utils.auth import verify_token
from app.utils.plan_enforcement import check_account_limit

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
    
    # Find existing conversation
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


def get_current_user_id(authorization: str = Header(None)) -> int:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required"
        )
    
    try:
        # Extract token from "Bearer <token>"
        scheme, token = authorization.split()
        if scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication scheme"
            )
        
        # Verify token using existing verify_token function
        payload = verify_token(token)
        if not payload:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token"
            )
        
        user_id = int(payload.get("sub"))
        return user_id
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token format"
        )
    
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
                log_print(f"📬 Found {len(messaging_events)} messaging event(s) in webhook entry")
                
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
                    attachments=attachments if attachments else None
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
                # User clicked "Skip for Now" - proceed to primary DM
                log_print(f"⏭️ User clicked 'Skip for Now', proceeding to primary DM for {sender_id}")
                # Find active rules and proceed to primary DM
                from app.models.automation_rule import AutomationRule
                rules = db.query(AutomationRule).filter(
                    AutomationRule.instagram_account_id == account.id,
                    AutomationRule.is_active == True,
                    AutomationRule.action_type == "send_dm"
                ).all()
                for rule in rules:
                    if rule.config.get("ask_for_email", False):
                        # Update state to skip email
                        from app.services.pre_dm_handler import update_pre_dm_state
                        update_pre_dm_state(sender_id, rule.id, {
                            "email_skipped": True,
                            "email_request_sent": True  # Mark as sent to prevent re-asking
                        })
                        # Proceed to primary DM
                        asyncio.create_task(execute_automation_action(
                            rule, sender_id, account, db,
                            trigger_type="email_skip",
                            message_id=message_id
                        ))
                return  # Don't process as regular message
            
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
                            
                            # Send email request as plain text (no buttons)
                            send_dm(sender_id, ask_for_email_message, access_token, page_id_for_dm, buttons=None, quick_replies=None)
                            log_print(f"✅ Email request sent after 'I'm following' button click")
                            
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
                    
                    # Mark follow as confirmed in pre-DM state
                    update_pre_dm_state(str(sender_id), rule.id, {
                        "follow_button_clicked": True,
                        "follow_confirmed": True,
                        "follow_button_clicked_time": str(asyncio.get_event_loop().time())
                    })
                    log_print(f"✅ Marked follow button click + confirmation for rule {rule.id}")
                    
                    # If email is enabled, send email question immediately
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
                            
                            # Send email request as plain text (no buttons)
                            send_dm(sender_id, ask_for_email_message, access_token, page_id_for_dm, buttons=None, quick_replies=None)
                            log_print(f"✅ Email request sent after Follow Me button click")
                            
                            # Update state to mark that we're now waiting for email
                            update_pre_dm_state(str(sender_id), rule.id, {
                                "email_request_sent": True,
                                "step": "email",
                                "waiting_for_email_text": True
                            })
                        except Exception as e:
                            log_print(f"❌ Failed to send email request after Follow Me click: {str(e)}", "ERROR")
                    
                    # If no email configured, proceed directly to primary DM
                    else:
                        log_print(f"✅ Follow confirmed via Follow Me button, proceeding directly to primary DM")
                        asyncio.create_task(execute_automation_action(
                            rule, sender_id, account, db,
                            trigger_type="postback",
                            message_id=message_id,
                            pre_dm_result_override={"action": "send_primary"}
                        ))
                    
                    # Only process the first matching rule
                    return
        
        # Extract story_id early. For story replies we run pre_dm_rules but only for the
        # matching Story rule (filter inside the loop). This lets Stories use the same
        # state machine (done, email, retry) as Post/Reels without Post/Reel rules blocking.
        story_id = None
        if message.get("reply_to", {}).get("story", {}).get("id"):
            story_id = str(message.get("reply_to", {}).get("story", {}).get("id"))
            log_print(f"📖 Story reply detected (early) - Story ID: {story_id}")
        
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
            
            for rule in pre_dm_rules:
                # If this is a Story reply, only process rules that match this Story.
                # Post/Reel rules (different media_id) would return "ignore" and block the Story flow.
                if story_id is not None and str(rule.media_id or "") != story_id:
                    continue

                ask_to_follow = rule.config.get("ask_to_follow", False)
                ask_for_email = rule.config.get("ask_for_email", False)

                if not (ask_to_follow or ask_for_email):
                    continue
                
                # Check if this is a follow confirmation (only if we're actually waiting for it)
                from app.services.pre_dm_handler import get_pre_dm_state
                state = get_pre_dm_state(sender_id, rule.id)
                
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
                        # STRICT MODE: Send email request IMMEDIATELY after follow confirmation
                        log_print(f"✅ [STRICT MODE] Follow confirmed from {sender_id}, sending email request now")
                        
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
                            
                            # Send email request as plain text
                            send_dm(sender_id, email_message, access_token, page_id, buttons=None, quick_replies=None)
                            log_print(f"✅ Email request sent (text input only)")
                            
                        except Exception as e:
                            log_print(f"❌ Failed to send email request: {str(e)}", "ERROR")
                        
                        return
                    elif pre_dm_result["action"] == "send_primary":
                        # Skip to primary DM
                        log_print(f"✅ Follow confirmed from {sender_id}, proceeding to primary DM")
                        asyncio.create_task(execute_automation_action(
                            rule,
                            sender_id,
                            account,
                            db,
                            trigger_type="story_reply" if story_id else "new_message",
                            message_id=message_id
                        ))
                        return
                
                # Check if user sent ANY message (could be email or invalid response)
                # Process pre-DM actions to check state and handle the message appropriately
                if ask_to_follow or ask_for_email:
                    pre_dm_result = await process_pre_dm_actions(
                        rule, sender_id, account, db,
                        incoming_message=message_text,
                        trigger_type="story_reply" if story_id else "new_message"
                    )
                    
                    # Handle ignore action (random text while waiting for follow confirmation)
                    if pre_dm_result["action"] == "ignore":
                        log_print(f"⏳ [STRICT MODE] Ignoring random text while waiting for follow confirmation: '{message_text}'")
                        return  # Don't process as new_message rule
                    
                    # Handle email request action (follow confirmed, now send email question)
                    if pre_dm_result["action"] == "send_email_request":
                        log_print(f"✅ [STRICT MODE] Follow confirmed from {sender_id}, sending email request now")
                        
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
                            
                            # Send email request as plain text
                            send_dm(sender_id, email_message, access_token, page_id, buttons=None, quick_replies=None)
                            log_print(f"✅ Email request sent (text input only)")
                            
                        except Exception as e:
                            log_print(f"❌ Failed to send email request: {str(e)}", "ERROR")
                        
                        return  # Don't process as new_message rule
                    
                    # Handle valid email - proceed to primary DM
                    if pre_dm_result["action"] == "send_primary" and pre_dm_result.get("email"):
                        # STRICT MODE: Email was valid! Proceed directly to primary DM
                        log_print(f"✅ [STRICT MODE] Valid email received: {pre_dm_result.get('email')}")
                        log_print(f"📤 Sending primary DM now (both follow + email completed)")
                        
                        # Retrieve comment_id from pre-DM state if this was triggered from a comment
                        from app.services.pre_dm_handler import get_pre_dm_state
                        state = get_pre_dm_state(sender_id, rule.id)
                        stored_comment_id = state.get("comment_id")  # comment_id stored when pre-DM actions started
                        log_print(f"🔍 [COMMENT ID] Retrieved from state: comment_id={stored_comment_id}")
                        
                        # Send primary DM immediately, preserving email + send_email_success flag
                        asyncio.create_task(execute_automation_action(
                            rule,
                            sender_id,
                            account,
                            db,
                            trigger_type="story_reply" if story_id else "new_message",
                            message_id=message_id,
                            comment_id=stored_comment_id,  # Pass comment_id if available (from comment trigger)
                            pre_dm_result_override={
                                "action": "send_primary",
                                "email": pre_dm_result.get("email"),
                                "send_email_success": pre_dm_result.get("send_email_success", False),
                            }
                        ))
                        
                        return  # Don't process as new_message rule
                    
                    # Handle invalid email - send retry message
                    elif pre_dm_result["action"] == "send_email_retry":
                        # STRICT MODE: Email was invalid! Send retry message and WAIT
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
                            retry_msg = pre_dm_result["message"]
                            
                            send_dm(sender_id, retry_msg, access_token, page_id, buttons=None, quick_replies=None)
                            log_print(f"✅ Retry message sent, waiting for valid email")
                            
                        except Exception as e:
                            log_print(f"❌ Failed to send retry message: {str(e)}", "ERROR")
                        
                        return  # Don't process as new_message rule, wait for valid email
                    
                    # If we're waiting for something and got random text, ignore it
                        from app.services.pre_dm_handler import get_pre_dm_state
                        state = get_pre_dm_state(sender_id, rule.id)
                        
                        if state.get("follow_request_sent") and not state.get("follow_confirmed"):
                            log_print(f"⏳ [STRICT MODE] Waiting for follow confirmation from {sender_id}")
                            if attachments:
                                log_print(f"   🚫 Image/attachment ignored - only text confirmations accepted")
                            else:
                                log_print(f"   Message '{message_text}' ignored - not a valid confirmation")
                            return  # Don't process random messages/images while waiting for follow
                        
                        if state.get("email_request_sent") and not state.get("email_received"):
                            log_print(f"⏳ [STRICT MODE] Waiting for email from {sender_id}")
                            if attachments:
                                log_print(f"   🚫 Image/attachment ignored - only email text accepted")
                            else:
                                log_print(f"   Message '{message_text}' ignored - not a valid email")
                            return  # Don't process random messages/images while waiting for email
        
        # story_id was already extracted above (before pre_dm_rules) for story replies
        
        # Deduplication: Skip if we've already processed this message
        if message_id and message_id in _processed_message_ids:
            log_print(f"🚫 Ignoring duplicate message (already processed): mid={message_id}")
            return
        
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
        new_message_rules = db.query(AutomationRule).filter(
            AutomationRule.instagram_account_id == account.id,
            AutomationRule.trigger_type == "new_message",
            AutomationRule.is_active == True
        ).all()
        
        log_print(f"📋 [DM] Found {len(new_message_rules)} 'new_message' rules for account '{account.username}'")
        
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
        
        # For story DMs, first check if any story-specific post_comment rule should trigger (any comment/DM on that story)
        story_rule_matched = False
        if story_id and story_post_comment_rules:
            log_print(f"🎯 [STORY DM] Processing {len(story_post_comment_rules)} story rule(s) for story {story_id}")
            for rule in story_post_comment_rules:
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
            if rule.config:
                # Check keywords array first (new format), fallback to single keyword (old format)
                keywords_list = []
                if rule.config.get("keywords") and isinstance(rule.config.get("keywords"), list):
                    keywords_list = [str(k).strip().lower() for k in rule.config.get("keywords") if k and str(k).strip()]
                elif rule.config.get("keyword"):
                    # Fallback to single keyword for backward compatibility
                    keywords_list = [str(rule.config.get("keyword", "")).strip().lower()]
                
                if keywords_list:
                    message_text_lower = message_text.strip().lower()
                    # Check if message is EXACTLY any of the keywords (case-insensitive)
                    matched_keyword = None
                    for keyword in keywords_list:
                        if keyword == message_text_lower:
                            matched_keyword = keyword
                            break
                    
                    if matched_keyword:
                        keyword_rule_matched = True
                        print(f"✅ Keyword '{matched_keyword}' exactly matches message, triggering keyword rule!")
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
                        # Run in background task to avoid blocking webhook handler
                        asyncio.create_task(execute_automation_action(
                            rule,
                            sender_id,
                            account,
                            db,
                            trigger_type="keyword",
                            message_id=message_id
                        ))
                        break  # Only trigger first matching keyword rule
        
        if not keyword_rule_matched and len(keyword_rules) > 0:
            log_print(f"❌ [DM] No keyword rules matched the message: '{message_text}'")
                
        # Process new_message rules ONLY if no keyword rule matched AND no story rule matched
        if not keyword_rule_matched and not story_rule_matched:
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
                            message_id=message_id
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
                    
                    # Mark that follow button was clicked in state
                    from app.services.pre_dm_handler import update_pre_dm_state
                    update_pre_dm_state(str(sender_id), rule.id, {
                        "follow_button_clicked": True,
                        "follow_confirmed": True,  # Mark as confirmed since they clicked the button
                        "follow_button_clicked_time": str(asyncio.get_event_loop().time())
                    })
                    print(f"✅ Marked follow button click for rule {rule.id}")
                    
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
        if media_id_str:
            post_comment_rules = db.query(AutomationRule).filter(
            AutomationRule.instagram_account_id == account.id,
            AutomationRule.trigger_type == "post_comment",
                AutomationRule.is_active == True,
                AutomationRule.media_id == media_id_str  # Strict match: only rules for this specific media
        ).all()
        
            keyword_rules = db.query(AutomationRule).filter(
                AutomationRule.instagram_account_id == account.id,
                AutomationRule.trigger_type == "keyword",
                AutomationRule.is_active == True,
                AutomationRule.media_id == media_id_str  # Strict match: only rules for this specific media
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
                    matched_keyword = None
                    for keyword in keywords_list:
                        if keyword == comment_text_lower:
                            matched_keyword = keyword
                            break
                    
                    if matched_keyword:
                        keyword_rule_matched = True
                        print(f"✅ Keyword '{matched_keyword}' exactly matches comment, triggering keyword rule!")
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
                            message_id=comment_id  # Use comment_id as identifier
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
                            commenter_id, 
                            account, 
                            db,
                            trigger_type="post_comment",
                            comment_id=comment_id,
                            message_id=comment_id  # Use comment_id as identifier
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
        if live_video_id_str:
            live_comment_rules = db.query(AutomationRule).filter(
            AutomationRule.instagram_account_id == account.id,
            AutomationRule.trigger_type == "live_comment",
                AutomationRule.is_active == True,
                AutomationRule.media_id == live_video_id_str  # Strict match: only rules for this specific live video
        ).all()
        
            keyword_rules = db.query(AutomationRule).filter(
                AutomationRule.instagram_account_id == account.id,
                AutomationRule.trigger_type == "keyword",
                AutomationRule.is_active == True,
                AutomationRule.media_id == live_video_id_str  # Strict match: only rules for this specific live video
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
                    matched_keyword = None
                    for keyword in keywords_list:
                        if keyword == comment_text_lower:
                            matched_keyword = keyword
                            break
                    
                    if matched_keyword:
                        keyword_rule_matched = True
                        print(f"✅ Keyword '{matched_keyword}' exactly matches live comment, triggering keyword rule!")
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
                            commenter_id,
                            account,
                            db,
                            trigger_type="keyword",
                            comment_id=comment_id,
                            message_id=comment_id  # Use comment_id as identifier
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
                    commenter_id,
                    account,
                    db,
                    trigger_type="live_comment",
                    comment_id=comment_id,
                    message_id=comment_id  # Use comment_id as identifier
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
    incoming_message: str = None  # For story/DM: user's text (follow confirmation, email, etc.)
):
    """
    Execute the automation action defined in the rule.
    
    Args:
        rule: The automation rule to execute
        sender_id: The user ID who triggered the action (recipient for DMs)
        account: The Instagram account to use
        db: Database session
        trigger_type: The type of trigger (e.g., 'post_comment', 'new_message', 'live_comment')
        comment_id: The comment ID (required for post_comment triggers to use private_replies)
        message_id: The message or comment ID (used for deduplication cache cleanup)
    """
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
        
        # Now safe to access attributes (with error handling)
        try:
            rule_id_val = rule.id
            action_type_val = rule.action_type
            print(f"🔍 [EXECUTE] Rule ID: {rule_id_val}, Action: {action_type_val}")
        except Exception as attr_error:
            print(f"❌ [EXECUTE] Error accessing rule attributes: {str(attr_error)}")
            print(f"❌ [EXECUTE] Attempting to refresh rule from DB...")
            try:
                db.refresh(rule)
                rule_id_val = rule.id
                action_type_val = rule.action_type
                print(f"🔍 [EXECUTE] Rule ID (after refresh): {rule_id_val}, Action: {action_type_val}")
            except Exception as refresh_error:
                print(f"❌ [EXECUTE] Failed to refresh rule: {str(refresh_error)}")
                return
        
        if rule.action_type == "send_dm":
            # IMPORTANT: Store all needed attributes from account and rule BEFORE any async operations
            # This prevents DetachedInstanceError when objects are passed across async boundaries
            try:
                user_id = account.user_id
                account_id = account.id
                username = account.username
                rule_id = rule.id
                print(f"🔍 [EXECUTE] Stored values - user_id: {user_id}, account_id: {account_id}, username: {username}, rule_id: {rule_id}")
                
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
                    rule_id = rule.id
                except Exception as refresh_error:
                    print(f"❌ Failed to refresh account/rule: {str(refresh_error)}")
                return
            
            # Check monthly DM limit BEFORE sending
            from app.utils.plan_enforcement import check_dm_limit
            if not check_dm_limit(user_id, db):
                print(f"⚠️ Monthly DM limit reached for user {user_id}. Skipping DM send.")
                return  # Don't send DM if limit reached
            
            # Initialize message_template
            message_template = None
            
            # Check for pre-DM actions (Ask to Follow, Ask for Email)
            ask_to_follow = rule.config.get("ask_to_follow", False)
            ask_for_email = rule.config.get("ask_for_email", False)
            pre_dm_result = pre_dm_result_override  # Use override if provided
            
            print(f"🔍 [DEBUG] Pre-DM check: ask_to_follow={ask_to_follow}, ask_for_email={ask_for_email}, pre_dm_result={pre_dm_result}")
            
            # If override says "send_primary", skip all pre-DM processing
            if pre_dm_result and pre_dm_result.get("action") == "send_primary":
                # Direct primary DM - skip to primary DM logic
                print(f"✅ Skipping pre-DM actions, proceeding directly to primary DM")
                # pre_dm_result already set to override, continue to primary DM logic below
            elif (ask_to_follow or ask_for_email) and pre_dm_result is None:
                print(f"🔍 [DEBUG] Processing pre-DM actions: ask_to_follow={ask_to_follow}, ask_for_email={ask_for_email}")
                # Process pre-DM actions (unless override is provided)
                from app.services.pre_dm_handler import process_pre_dm_actions
                
                pre_dm_result = await process_pre_dm_actions(
                    rule, sender_id, account, db,
                    trigger_type=trigger_type,
                    incoming_message=incoming_message
                )
                
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
                        # Use direct Instagram URL (no tracking wrapper) to avoid redirect chain issues
                        # Instagram will handle instagram.com URLs natively and open in the app
                        profile_url = f"https://www.instagram.com/{username}"
                        
                        # Build URL button for "Visit Profile" (enables navigation to bio page)
                        # Note: URL buttons require generic template format (card layout)
                        # Using direct Instagram URL ensures it opens in native app without redirect chain
                        visit_profile_button = [{
                            "text": "Visit Profile",
                            "url": profile_url
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
                                
                                # Send follow message with "Visit Profile" URL button (card format - required for navigation)
                                try:
                                    send_dm_api(sender_id, follow_message_with_instructions, access_token, page_id_for_dm, buttons=visit_profile_button, quick_replies=None)
                                    print(f"✅ Follow request sent with 'Visit Profile' URL button (card format - enables navigation)")
                                    
                                    # Small delay between messages
                                    await asyncio.sleep(1)
                                    
                                    # Send second message with quick replies for "I'm following" and "Follow Me" (plain text format)
                                    quick_reply_message = "Click one of the options below:"
                                    send_dm_api(sender_id, quick_reply_message, access_token, page_id_for_dm, buttons=None, quick_replies=follow_quick_reply)
                                    print(f"✅ Quick reply buttons sent for 'I'm following' and 'Follow Me' (plain text, straight layout)")
                                    
                                    # Send public comment reply IMMEDIATELY after follow-up message (not waiting for email)
                                    if comment_id:
                                        is_lead_capture = rule.config.get("is_lead_capture", False)
                                        
                                        # Determine which comment reply fields to use based on rule type
                                        if is_lead_capture:
                                            auto_reply_to_comments = rule.config.get("lead_auto_reply_to_comments", False) or rule.config.get("auto_reply_to_comments", False)
                                            comment_replies = rule.config.get("lead_comment_replies", []) or rule.config.get("comment_replies", [])
                                        else:
                                            auto_reply_to_comments = rule.config.get("simple_auto_reply_to_comments", False) or rule.config.get("auto_reply_to_comments", False)
                                            comment_replies = rule.config.get("simple_comment_replies", []) or rule.config.get("comment_replies", [])
                                        
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
                                                    
                                                    # Mark comment reply as sent in pre-DM state to avoid duplicate
                                                    from app.services.pre_dm_handler import update_pre_dm_state
                                                    update_pre_dm_state(str(sender_id), rule_id, {
                                                        "comment_reply_sent": True
                                                    })
                                                    
                                                    # Update stats
                                                    from app.services.lead_capture import update_automation_stats
                                                    update_automation_stats(rule.id, "comment_replied", db)
                                                    # Log COMMENT_REPLIED for Analytics dashboard
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
                                    print(f"⚠️ Could not send follow buttons: {str(btn_error)}")
                            except Exception as e:
                                print(f"❌ Failed to send follow request: {str(e)}")
                        else:
                            try:
                                # Send follow message with "Visit Profile" URL button (card format - required for navigation)
                                send_dm_api(sender_id, follow_message_with_instructions, access_token, page_id_for_dm, buttons=visit_profile_button, quick_replies=None)
                                print(f"✅ Follow request sent with 'Visit Profile' URL button (card format - enables navigation)")
                                
                                # Small delay between messages
                                await asyncio.sleep(1)
                                
                                # Send second message with quick replies for "I'm following" and "Follow Me" (plain text format)
                                quick_reply_message = "Click one of the options below:"
                                send_dm_api(sender_id, quick_reply_message, access_token, page_id_for_dm, buttons=None, quick_replies=follow_quick_reply)
                                print(f"✅ Quick reply buttons sent for 'I'm following' and 'Follow Me' (plain text, straight layout)")
                                
                                # Send public comment reply IMMEDIATELY after follow-up message (not waiting for email)
                                if comment_id:
                                    is_lead_capture = rule.config.get("is_lead_capture", False)
                                    
                                    # Determine which comment reply fields to use based on rule type
                                    if is_lead_capture:
                                        auto_reply_to_comments = rule.config.get("lead_auto_reply_to_comments", False) or rule.config.get("auto_reply_to_comments", False)
                                        comment_replies = rule.config.get("lead_comment_replies", []) or rule.config.get("comment_replies", [])
                                    else:
                                        auto_reply_to_comments = rule.config.get("simple_auto_reply_to_comments", False) or rule.config.get("auto_reply_to_comments", False)
                                        comment_replies = rule.config.get("simple_comment_replies", []) or rule.config.get("comment_replies", [])
                                    
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
                                                
                                                # Mark comment reply as sent in pre-DM state to avoid duplicate
                                                from app.services.pre_dm_handler import update_pre_dm_state
                                                update_pre_dm_state(str(sender_id), rule_id, {
                                                    "comment_reply_sent": True
                                                })
                                                
                                                # Update stats
                                                from app.services.lead_capture import update_automation_stats
                                                update_automation_stats(rule.id, "comment_replied", db)
                                                # Log COMMENT_REPLIED for Analytics dashboard
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
                        # Only follow request, no email
                        message_template = follow_message
                        # Mark follow as sent in state
                        from app.services.pre_dm_handler import update_pre_dm_state
                        update_pre_dm_state(str(sender_id), rule_id, {
                            "follow_request_sent": True,
                            "step": "follow"
                        })
                        print(f"📩 Sending follow request DM to {sender_id} with Follow button")
                        
                        # Update pre_dm_result with final message and buttons
                        pre_dm_result["buttons"] = buttons
                        pre_dm_result["message"] = message_template
                        
                        # Schedule primary DM after 15 seconds for follow-only case
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
                                print(f"❌ [PRIMARY DM] Error in delayed primary DM: {str(e)}")
                                import traceback
                                traceback.print_exc()
                            finally:
                                db_session.close()
                                print(f"🔒 [PRIMARY DM] Database session closed")
                        
                        # Start the delayed primary DM task
                        print(f"🚀 [PRIMARY DM] Scheduling primary DM after 15 seconds for sender {sender_id_for_dm}, rule {rule_id_for_dm}")
                        asyncio.create_task(delayed_primary_dm_simple())
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
                    pre_dm_result["quick_replies"] = quick_replies
                    print(f"📧 Sending email request DM to {sender_id} with Quick Reply buttons")
                    
                    # Schedule primary DM after 15 seconds (simplified)
                    # Store IDs for delayed task
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
                            print(f"❌ [PRIMARY DM] Error in delayed primary DM: {str(e)}")
                            import traceback
                            traceback.print_exc()
                        finally:
                            db_session.close()
                            print(f"🔒 [PRIMARY DM] Database session closed")
                    
                    # Start the delayed primary DM task
                    print(f"🚀 [PRIMARY DM] Scheduling primary DM after 15 seconds for sender {sender_id_for_dm}, rule {rule_id_for_dm}")
                    asyncio.create_task(delayed_primary_dm_simple())
                elif pre_dm_result and pre_dm_result["action"] == "wait_for_email":
                    # Still waiting for email response
                    # If this is a comment/keyword trigger (not a DM), user is engaging again
                    # Schedule primary DM after timeout instead of waiting forever
                    if trigger_type in ["post_comment", "keyword", "live_comment"]:
                        print(f"⏳ Email requested but not received yet. User commented again, scheduling primary DM after timeout...")
                        # Schedule primary DM after 15 seconds (same as normal flow)
                        sender_id_for_dm = str(sender_id)
                        rule_id_for_dm = int(rule_id)
                        user_id_for_dm = int(user_id)
                        account_id_for_dm = int(account_id)
                        
                        async def delayed_primary_dm_for_comment():
                            """Send primary DM after timeout when user comments again."""
                            from app.db.session import SessionLocal
                            from app.models.automation_rule import AutomationRule
                            from app.models.instagram_account import InstagramAccount
                            
                            db_session = SessionLocal()
                            try:
                                await asyncio.sleep(15)  # Wait 15 seconds
                                # Re-fetch rule and account
                                rule_refresh = db_session.query(AutomationRule).filter(AutomationRule.id == rule_id_for_dm).first()
                                account_refresh = db_session.query(InstagramAccount).filter(
                                    InstagramAccount.user_id == user_id_for_dm,
                                    InstagramAccount.id == account_id_for_dm
                                ).first()
                                
                                if not rule_refresh or not account_refresh:
                                    print(f"⚠️ [PRIMARY DM] Rule or account not found for comment trigger")
                                    return
                                
                                # Check if primary DM already sent
                                from app.services.pre_dm_handler import get_pre_dm_state
                                current_state = get_pre_dm_state(sender_id_for_dm, rule_id_for_dm)
                                if current_state.get("primary_dm_sent"):
                                    print(f"⏭️ [PRIMARY DM] Primary DM already sent, skipping")
                                    return
                                
                                # Mark as sent and send primary DM
                                from app.services.pre_dm_handler import update_pre_dm_state
                                update_pre_dm_state(sender_id_for_dm, rule_id_for_dm, {
                                    "primary_dm_sent": True
                                })
                                
                                # Send primary DM directly from rule config
                                print(f"✅ [PRIMARY DM] Sending primary DM from rule config (comment trigger)")
                                await execute_automation_action(
                                    rule_refresh, sender_id_for_dm, account_refresh, db_session,
                                    trigger_type="primary_timeout",
                                    message_id=None,
                                    pre_dm_result_override={"action": "send_primary"}
                                )
                                print(f"✅ [PRIMARY DM] Primary DM sent successfully (comment trigger)")
                            except Exception as e:
                                print(f"❌ [PRIMARY DM] Error in delayed primary DM for comment: {str(e)}")
                                import traceback
                                traceback.print_exc()
                            finally:
                                db_session.close()
                        
                        # Start the delayed primary DM
                        asyncio.create_task(delayed_primary_dm_for_comment())
                        print(f"🚀 [PRIMARY DM] Scheduled primary DM after 15 seconds (user engaged via comment)")
                    else:
                        # For DM triggers, just wait
                        print(f"⏳ Waiting for email response from {sender_id}")
                        return
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
                            
                            # Log the DM
                            from app.models.dm_log import DmLog
                            dm_log = DmLog(
                                user_id=user_id,
                                instagram_account_id=account_id,
                                recipient_username=str(sender_id),
                                message=follow_msg
                            )
                            db.add(dm_log)
                            db.commit()
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
                        
                        # Log the DM
                        from app.models.dm_log import DmLog
                        dm_log = DmLog(
                            user_id=user_id,
                            instagram_account_id=account_id,
                            recipient_username=str(sender_id),
                            message=email_msg
                        )
                        db.add(dm_log)
                        db.commit()
                        
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
                    if pre_dm_result.get("email"):
                        print(f"✅ Pre-DM email received: {pre_dm_result['email']}, proceeding to primary DM")
                    # Continue to primary DM logic below
                    message_template = None  # Will be set below
            
            # Check if this is a lead capture flow
            is_lead_capture = rule.config.get("is_lead_capture", False)
            
            # Skip lead capture step processing if we're coming from pre-DM actions
            # (we just need to send the primary DM using lead_dm_messages)
            if is_lead_capture and not (pre_dm_result and pre_dm_result.get("action") == "send_primary"):
                # Process lead capture flow
                from app.services.lead_capture import process_lead_capture_step, update_automation_stats
                
                # Get user message from event (for DMs, this would be in the message text)
                # For comments, we'd need to extract from comment text
                user_message = ""
                if trigger_type in ["new_message", "keyword"]:
                    # For DMs, we need to get the message text from the webhook event
                    # This is a simplified version - in production, you'd track conversation state
                    user_message = ""  # Will be extracted from webhook context
                
                # Process lead capture step
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
                if message_template is None:  # Only set if not already set by pre-DM
                    is_lead_capture = rule.config.get("is_lead_capture", False)
                    
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

                # SOFT REMINDER: If ask_to_follow is enabled, gently remind user to stay followed
                try:
                    if rule.config.get("ask_to_follow", False):
                        reminder = (
                            "\n\n🙏 If you ever unfollow, I may have to pause sending free guides and resources. "
                            "Staying followed helps me keep this running for you. ❤️"
                        )
                        message_template = f"{message_template}{reminder}"
                except Exception:
                    # Never let reminder logic break the main DM
                    pass
            
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
                should_send_email_success = False
                if pre_dm_result:
                    send_email_success_flag = pre_dm_result.get("send_email_success", False)
                    print(f"🔍 [EMAIL SUCCESS] send_email_success flag: {send_email_success_flag}, type: {type(send_email_success_flag)}")
                    if send_email_success_flag is True or str(send_email_success_flag).lower() == 'true':
                        should_send_email_success = True
                
                if should_send_email_success:
                    email_success_message = rule.config.get("email_success_message")
                    print(f"🔍 [EMAIL SUCCESS] email_success_message from config: '{email_success_message}'")
                    
                    # Use default message if not configured (same as frontend default)
                    if not email_success_message or str(email_success_message).strip() == '' or str(email_success_message).lower() == 'none':
                        email_success_message = "Got it! Check your inbox (and maybe spam/promotions) in about 2 minutes. 🎁"
                        print(f"🔍 [EMAIL SUCCESS] Using default email success message")
                    
                    if email_success_message and str(email_success_message).strip():
                        print(f"📧 Sending email success message before primary DM")
                        try:
                            # Always send as a regular DM (not private reply)
                            send_dm_api(sender_id, email_success_message, access_token, account_page_id, buttons=None, quick_replies=None)
                            print(f"✅ Email success message sent successfully")
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
            if not message_template:
                print(f"⚠️ No message template configured for rule {rule.id}, action: {pre_dm_result.get('action') if pre_dm_result else 'None'}")
                # Email success message was already sent above if needed, so we can return now
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
                is_lead_capture = rule.config.get("is_lead_capture", False)
                
                # Determine which comment reply fields to use based on rule type
                if is_lead_capture:
                    # Lead Capture rule: check lead-specific fields first, then fallback to shared
                    auto_reply_to_comments = rule.config.get("lead_auto_reply_to_comments", False) or rule.config.get("auto_reply_to_comments", False)
                    comment_replies = rule.config.get("lead_comment_replies", []) or rule.config.get("comment_replies", [])
                else:
                    # Simple Reply rule: check simple-specific fields first, then fallback to shared
                    auto_reply_to_comments = rule.config.get("simple_auto_reply_to_comments", False) or rule.config.get("auto_reply_to_comments", False)
                    comment_replies = rule.config.get("simple_comment_replies", []) or rule.config.get("comment_replies", [])
                
                print(f"🔍 [COMMENT REPLY] Rule {rule.id} (is_lead_capture={is_lead_capture}): auto_reply_to_comments={auto_reply_to_comments}, comment_replies type={type(comment_replies)}, len={len(comment_replies) if isinstance(comment_replies, list) else 'N/A'}")
                print(f"🔍 [COMMENT REPLY] Config fields: auto_reply_to_comments={rule.config.get('auto_reply_to_comments')}, simple_auto_reply_to_comments={rule.config.get('simple_auto_reply_to_comments')}, lead_auto_reply_to_comments={rule.config.get('lead_auto_reply_to_comments')}")
                print(f"🔍 [COMMENT REPLY] comment_replies={rule.config.get('comment_replies')}, simple_comment_replies={rule.config.get('simple_comment_replies')}, lead_comment_replies={rule.config.get('lead_comment_replies')}")
                
                # Check if comment reply was already sent immediately after follow-up message
                comment_reply_already_sent = False
                if comment_id:
                    from app.services.pre_dm_handler import get_pre_dm_state
                    state = get_pre_dm_state(str(sender_id), rule.id)
                    if state and state.get("comment_reply_sent"):
                        comment_reply_already_sent = True
                        print(f"⏭️ [COMMENT REPLY] Skipping: Comment reply was already sent immediately after follow-up message")
                
                # If we have a comment_id and auto-reply is enabled, send public comment reply
                # (Only if it wasn't already sent immediately after follow-up message)
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
                            
                            # Update stats
                            from app.services.lead_capture import update_automation_stats
                            update_automation_stats(rule.id, "comment_replied", db)
                            # Log COMMENT_REPLIED for Analytics dashboard
                            try:
                                from app.utils.analytics import log_analytics_event_sync
                                from app.models.analytics_event import EventType
                                _mid = rule.config.get("media_id") if isinstance(getattr(rule, "config", None), dict) else None
                                log_analytics_event_sync(db=db, user_id=account.user_id, event_type=EventType.COMMENT_REPLIED, rule_id=rule.id, media_id=_mid, instagram_account_id=account.id, metadata={"comment_id": comment_id})
                            except Exception as _ae:
                                pass
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
                        print(f"⏭️ [COMMENT REPLY] Skipping: Comment reply was already sent immediately after follow-up message")
                    elif not comment_id:
                        print(f"⏭️ [COMMENT REPLY] Skipping: No comment_id provided (trigger_type={trigger_type})")
                    elif not auto_reply_to_comments:
                        print(f"⏭️ [COMMENT REPLY] Skipping: auto_reply_to_comments is False for rule {rule.id}")
                    elif not comment_replies or not isinstance(comment_replies, list) or len(comment_replies) == 0:
                        print(f"⏭️ [COMMENT REPLY] Skipping: No comment_replies configured for rule {rule.id} (comment_replies={comment_replies})")
                
                # Always send DM if message is configured (for all trigger types)
                if message_template:
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
                    
                    # CRITICAL FIX: For comment-based triggers, use Private Reply endpoint to bypass 24-hour window
                    # Comments don't count as DM initiation, so normal send_dm would fail
                    # Private replies use comment_id instead of user_id and bypass the restriction
                    if comment_id and trigger_type in ["post_comment", "keyword", "live_comment"]:
                        print(f"💬 Comment-based trigger detected! Using PRIVATE REPLY to bypass 24-hour window")
                        print(f"   Trigger type: {trigger_type}, Comment ID: {comment_id}")
                        print(f"   Recipient (commenter): {sender_id}")
                        
                        # For comment-based triggers, use private reply to open conversation
                        # Then send the actual message with buttons as regular DM
                        from app.utils.instagram_api import send_private_reply
                        
                        if buttons or quick_replies:
                            # If buttons/quick replies needed, send simple opener via private reply
                            # then send full message with buttons as regular DM
                            print(f"💬 Sending simple opener via private reply to open conversation")
                            opener_message = "Hi! 👋"
                            send_private_reply(comment_id, opener_message, access_token, page_id_for_dm)
                            print(f"✅ Conversation opened via private reply")
                            
                            # Small delay to ensure private reply is processed
                            await asyncio.sleep(1)
                            
                            # Now send the actual message with buttons/quick replies
                            print(f"📤 Sending DM with buttons/quick replies...")
                            from app.utils.instagram_api import send_dm
                            send_dm(sender_id, message_template, access_token, page_id_for_dm, buttons, quick_replies)
                            print(f"✅ DM with buttons/quick replies sent to {sender_id}")
                        else:
                            # No buttons/quick replies, send full message via private reply
                            send_private_reply(comment_id, message_template, access_token, page_id_for_dm)
                            print(f"✅ Private reply sent to comment {comment_id} from user {sender_id}")
                    else:
                        # For direct message triggers or when no comment_id, use regular DM
                        if page_id_for_dm:
                            print(f"📤 Sending DM via Page API: Page ID={page_id_for_dm}, Recipient={sender_id}")
                        else:
                            print(f"📤 Sending DM via me/messages (no page_id): Recipient={sender_id}")
                        # Import send_dm and call it with quick_replies
                        from app.utils.instagram_api import send_dm
                        send_dm(sender_id, message_template, access_token, page_id_for_dm, buttons, quick_replies)
                        print(f"✅ DM sent to {sender_id}")
                    
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
                    user_id=user_id,  # Use stored user_id
                    instagram_account_id=account_id,  # Use stored account_id
                    recipient_username=str(sender_id),  # Using sender_id as recipient username (ID format)
                    message=message_template
                )
                db.add(dm_log)
                
                # Also store in Message table for Messages UI
                try:
                    from app.models.message import Message
                    from datetime import datetime
                    # Get recipient username if available (sender_id might be username or ID)
                    recipient_username = str(sender_id)  # Default to sender_id
                    
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
                        attachments=None
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
        print(f"❌ [EXECUTE] Rule ID: {rule.id if rule else 'None'}, Sender: {sender_id}")
        import traceback
        traceback.print_exc()
        raise  # Re-raise to be caught by task wrapper
    finally:
        # Clean up processing cache after completion (whether success or failure)
        # Use comment_id if available (for comments), otherwise message_id (for DMs)
        identifier = comment_id if comment_id else message_id
        if identifier:
            processing_key = f"{identifier}_{rule.id}"
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
    
    # For each rule, delete associated data first (to avoid foreign key constraint violation)
    from app.models.automation_rule_stats import AutomationRuleStats
    from app.models.captured_lead import CapturedLead
    from app.models.analytics_event import AnalyticsEvent
    
    for rule in automation_rules:
        # Delete analytics events first (they reference automation_rules via foreign key)
        analytics_events = db.query(AnalyticsEvent).filter(
            AnalyticsEvent.rule_id == rule.id
        ).all()
        for event in analytics_events:
            db.delete(event)
        
        # Delete automation rule stats
        stats = db.query(AutomationRuleStats).filter(
            AutomationRuleStats.automation_rule_id == rule.id
        ).all()
        for stat in stats:
            db.delete(stat)
        
        # Delete captured leads
        leads = db.query(CapturedLead).filter(
            CapturedLead.automation_rule_id == rule.id
        ).all()
        for lead in leads:
            db.delete(lead)
    
    # Flush to ensure deletions are processed before deleting rules
    db.flush()
    
    # Also delete any analytics events that reference this account (even if rule_id is NULL)
    db.query(AnalyticsEvent).filter(
        AnalyticsEvent.instagram_account_id == account_id
    ).delete()
    
    # Flush again
    db.flush()
    
    # Now delete the automation rules
    db.query(AutomationRule).filter(
        AutomationRule.instagram_account_id == account_id
    ).delete()
    
    # Delete associated DM logs
    db.query(DmLog).filter(
        DmLog.instagram_account_id == account_id
    ).delete()
    
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


@router.get("/media")
async def get_instagram_media(
    account_id: int = Query(..., description="Instagram account ID"),
    media_type: str = Query("posts", description="Type of media: posts, stories, reels, live"),
    limit: int = Query(25, description="Number of items to fetch"),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    Fetch Instagram media (posts/reels/stories) for a specific account.
    Returns list of media items with metadata.
    
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
        
        if media_type == "posts" or media_type == "reels":
            # Fetch posts and reels
            # For Instagram Graph API, we use the media edge
            url = f"https://graph.instagram.com/v21.0/{igsid}/media"
            params = {
                "fields": "id,caption,media_type,media_url,permalink,thumbnail_url,timestamp,like_count,comments_count,media_product_type",
                "limit": limit,
                "access_token": access_token
            }
            
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
            
            # Filter by type if specified
            if media_type == "reels":
                # Reels have media_product_type == "REELS"
                media_items = [item for item in media_items if item.get("media_product_type") == "REELS"]
            elif media_type == "posts":
                # Posts/Reels tab: include both FEED (posts) and REELS, but exclude STORY
                # This allows the "Posts/Reels" tab to show both types of content
                media_items = [item for item in media_items if item.get("media_product_type") != "STORY"]
        
        elif media_type == "stories":
            # Fetch stories (requires stories_read permission and different endpoint)
            # Note: Stories are only available for 24 hours after posting
            # Note: Stories don't have public comments in the traditional sense - interactions are via DMs (replies)
            # But we'll request comments_count anyway in case Instagram API provides it
            url = f"https://graph.instagram.com/v21.0/{igsid}/stories"
            params = {
                "fields": "id,media_type,media_url,thumbnail_url,timestamp,media_product_type,comments_count",
                "limit": limit,
                "access_token": access_token
            }
            
            response = requests.get(url, params=params)
            
            if response.status_code != 200:
                error_detail = response.text
                print(f"⚠️ Stories may not be available: {error_detail}")
                # Stories might not be available (no active stories, or missing permissions)
                media_items = []
            else:
                data = response.json()
                media_items = data.get("data", [])
                # Ensure all stories have media_product_type set to STORY
                for item in media_items:
                    if "media_product_type" not in item:
                        item["media_product_type"] = "STORY"
        
        elif media_type == "live":
            # For live videos, we'd need to check live_media endpoint
            # This is more complex and may require different permissions
            url = f"https://graph.instagram.com/v21.0/{igsid}/live_media"
            params = {
                "fields": "id,media_type,media_url,permalink,timestamp,status",
                "limit": limit,
                "access_token": access_token
            }
            
            response = requests.get(url, params=params)
            
            if response.status_code != 200:
                error_detail = response.text
                print(f"⚠️ Live media may not be available: {error_detail}")
                media_items = []
            else:
                data = response.json()
                media_items = data.get("data", [])
        
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
        
        return {
            "success": True,
            "media": formatted_media,
            "count": len(formatted_media)
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error fetching Instagram media: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch Instagram media: {str(e)}"
        )


@router.get("/conversations")
async def get_instagram_conversations(
    account_id: int = Query(..., description="Instagram account ID"),
    limit: int = Query(25, description="Number of conversations to fetch"),
    sync: bool = Query(False, description="Whether to sync conversations from API"),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    Fetch recent Instagram DM conversations for a specific account.
    
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
        
        conversations_query = db.query(Conversation).filter(
            Conversation.instagram_account_id == account_id,
            Conversation.user_id == user_id
        ).order_by(Conversation.updated_at.desc()).limit(limit)
        
        conversations_list = conversations_query.all()
        
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
                conversations_list = conversations_query.all()
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
            for conv in conversations_list:
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
                "count": len(formatted_conversations)
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
        
        # Merge conversations (use username as key)
        conversations_map = {}
        
        # Process incoming conversations
        for conv in incoming_convs:
            # Use sender_username if available, otherwise use sender_id as string
            # If sender_id is None, skip this conversation
            if not conv.sender_id:
                continue
            username = conv.sender_username or str(conv.sender_id)
            # Use "Unknown" if we only have a numeric ID (not a real username)
            display_username = username if (username and not username.isdigit()) else "Unknown"
                
            # Check if we should add/update this conversation
            should_add = username not in conversations_map
            if not should_add and conv.last_message_at:
                # Compare dates properly
                existing_time_str = conversations_map[username].get('last_message_at')
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
                    Message.sender_id == str(conv.sender_id)
                ).order_by(Message.created_at.desc()).first()
                
                # Get message content (handle both message_text and content fields)
                message_content = ""
                if latest_msg:
                    message_content = latest_msg.get_content() if hasattr(latest_msg, 'get_content') else (latest_msg.message_text or latest_msg.content or "")
                
                conversations_map[username] = {
                    "id": username,
                    "username": display_username,
                    "user_id": str(conv.sender_id),
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
            username = conv.recipient_username or str(conv.recipient_id)
            # Use "Unknown" if we only have a numeric ID (not a real username)
            display_username = username if (username and not username.isdigit()) else "Unknown"
                
            if username not in conversations_map:
                # Get latest message for this conversation (try Message table first, then DmLog)
                # Use recipient_id for matching (more reliable than username which may be None)
                latest_msg = db.query(Message).filter(
                    Message.instagram_account_id == account_id,
                    Message.user_id == user_id,
                    Message.is_from_bot == True,
                    Message.recipient_id == str(conv.recipient_id)
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
                
                conversations_map[username] = {
                    "id": username,
                    "username": display_username,
                    "user_id": str(conv.recipient_id),
                    "last_message_at": conv.last_message_at.isoformat() if conv.last_message_at else None,
                    "last_message": message_content,
                    "last_message_is_from_bot": latest_msg.is_from_bot if latest_msg else True,
                    "message_count": conv.message_count
                }
        
        # Convert to list and get latest message for each
        conversations = []
        for username, conv_data in conversations_map.items():
            # Get the absolute latest message for this conversation (sent or received)
            # Use user_id for matching (more reliable than username which may be None or numeric)
            user_id_str = conv_data.get('user_id', '')
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
        conversations = conversations[:limit]  # Limit results
        
        return {
            "success": True,
            "conversations": conversations,
            "count": len(conversations)
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error fetching conversations: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch conversations: {str(e)}"
        )


@router.get("/conversations/{username}/messages")
async def get_conversation_messages(
    username: str,
    account_id: int = Query(..., description="Instagram account ID"),
    limit: int = Query(100, description="Number of messages to fetch"),
    participant_user_id: str = Query(None, description="Instagram user_id (IGSID) of the participant - more reliable than username for Unknown users"),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    Fetch messages for a specific conversation (by recipient username).
    Returns both sent and received messages.
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
        if conversation and conversation.id:
            query = db.query(Message).filter(
                Message.instagram_account_id == account_id,
                Message.user_id == user_id,
                Message.conversation_id == conversation.id
            )
            messages = query.order_by(Message.created_at.asc()).limit(limit).all()
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
            
            messages = query.order_by(Message.created_at.asc()).limit(limit).all()
            print(f"📨 Found {len(messages)} messages using participant_id/username search")
        
        print(f"📨 Total messages found: {len(messages)} for username='{username}', participant_id={participant_id_to_search}")
        
        # If no messages in Message table, check DmLog as fallback
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
            "count": len(formatted_messages)
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
        conversation = None
        
        if participant_user_id:
            # Use provided participant_user_id
            conversation = db.query(Conversation).filter(
                Conversation.instagram_account_id == account_id,
                Conversation.user_id == user_id,
                Conversation.participant_id == str(participant_user_id)
            ).first()
        else:
            # Try to find by username
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
            from datetime import datetime
            
            # Get sender_id (our account's Instagram ID)
            sender_id = account.igsid or str(account_id)
            
            message_id_from_api = result.get("message_id") or result.get("id")
            
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
                created_at=datetime.utcnow()
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
        sync_result = sync_instagram_conversations(user_id, account_id, db, limit=50)
        
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
        
        # Get total conversations from Conversation table (more accurate)
        # This counts all conversations regardless of message direction
        total_conversations = db.query(Conversation).filter(
            Conversation.instagram_account_id == account_id,
            Conversation.user_id == user_id
        ).count()
        
        # Fallback: If no conversations in Conversation table, count from Message table
        if total_conversations == 0:
            # Count both incoming and outgoing conversations
            incoming = db.query(
                func.count(func.distinct(
                    func.coalesce(Message.sender_username, Message.sender_id)
                ))
            ).filter(
                Message.instagram_account_id == account_id,
                Message.user_id == user_id,
                Message.is_from_bot == False
            ).scalar() or 0
            
            outgoing = db.query(
                func.count(func.distinct(
                    func.coalesce(Message.recipient_username, Message.recipient_id)
                ))
            ).filter(
                Message.instagram_account_id == account_id,
                Message.user_id == user_id,
                Message.is_from_bot == True
            ).scalar() or 0
            
            # Use max to avoid double counting (conversations can have both incoming and outgoing)
            total_conversations = max(incoming, outgoing)
        
        # For now, unread is 0 (we don't track read status yet)
        unread = 0
        
        # Get messages sent (by our bot)
        messages_sent = db.query(Message).filter(
            Message.instagram_account_id == account_id,
            Message.user_id == user_id,
            Message.is_from_bot == True
        ).count()
        
        # Get messages received (from users)
        messages_received = db.query(Message).filter(
            Message.instagram_account_id == account_id,
            Message.user_id == user_id,
            Message.is_from_bot == False
        ).count()
        
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