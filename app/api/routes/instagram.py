import asyncio
import json
import os
import requests
from fastapi import APIRouter, Depends, HTTPException, status, Header, Query, Request, BackgroundTasks
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

# In-memory cache to track recently processed message IDs (prevents duplicate processing)
# Note: This is cleared on restart, but should prevent short-term loops
_processed_message_ids = set()
_MAX_CACHE_SIZE = 1000  # Limit cache size to prevent memory issues

# Track rules that are currently being processed with delays (prevents duplicate triggering)
# Format: (message_id, rule_id) -> timestamp when processing started
_processing_rules = {}
_MAX_PROCESSING_CACHE_SIZE = 1000


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
     
    print(f"🔔 Webhook verification request:")
    print(f"   mode={hub_mode}, challenge={hub_challenge}, token={hub_verify_token}")
    print(f"   Expected token: {verify_token}")

    if hub_mode == "subscribe" and hub_verify_token == verify_token:
        # Meta expects plain text response, not JSON
        # Return challenge as plain text string
        from fastapi.responses import Response
        print(f"✅ Verification successful! Returning challenge: {hub_challenge}")
        return Response(content=hub_challenge, media_type="text/plain")
    
    print(f"❌ Verification failed! Token mismatch or invalid mode")
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
        body = await request.json()
        print(f"📥 Received webhook: {json.dumps(body, indent=2)}")
        
        # Process webhook event
        if body.get("object") == "instagram":
            for entry in body.get("entry", []):
                # Process messaging events (DMs)
                for messaging_event in entry.get("messaging", []):
                    # Check if this is a regular message event (not message_edit, message_reactions, etc.)
                    # Only process events with a "message" field containing text
                    if "message" in messaging_event:
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
                        print(f"⏭️ Skipping {event_type} event (not a regular message)")
                
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
        print(f"❌ Webhook error: {str(e)}")
        import traceback
        traceback.print_exc()
        # Always return 200 to Meta to prevent retries
        return {"status": "error", "message": str(e)}

async def process_instagram_message(event: dict, db: Session):
    """Process incoming Instagram message and trigger automation rules."""
    try:
        # Validate that this is a regular message event (not message_edit, etc.)
        if "message" not in event:
            print(f"⚠️ Skipping event - no 'message' field found. Event keys: {list(event.keys())}")
            return
        
        sender_id = event.get("sender", {}).get("id")
        recipient_id = event.get("recipient", {}).get("id")
        message = event.get("message", {})
        message_text = message.get("text", "")
        message_id = message.get("mid")  # Message ID for deduplication
        
        # Deduplication: Skip if we've already processed this message
        if message_id and message_id in _processed_message_ids:
            print(f"🚫 Ignoring duplicate message (already processed): mid={message_id}")
            return
        
        # Check for echo messages (messages sent by the bot itself)
        # Echo can be at message level or event level
        is_echo = message.get("is_echo", False) or event.get("is_echo", False)
        if is_echo:
            print(f"🚫 Ignoring bot's own message (echo flag): {message_text}")
            if message_id:
                _processed_message_ids.add(message_id)
                # Clean cache if it gets too large
                if len(_processed_message_ids) > _MAX_CACHE_SIZE:
                    _processed_message_ids.clear()
            return
        
        # Skip if no text (reactions, stickers, etc.)
        if not message_text or not message_text.strip():
            print(f"🚫 Ignoring message with no text content (mid: {message_id})")
            if message_id:
                _processed_message_ids.add(message_id)
            return
        
        print(f"📨 Message from {sender_id} (type: {type(sender_id).__name__}) to {recipient_id}: {message_text} (mid: {message_id})")
        
        # Match Instagram account by IGSID (recipient_id should be the bot's IGSID)
        from app.models.instagram_account import InstagramAccount
        print(f"🔍 Looking for Instagram account (IGSID: {recipient_id})")
        
        # First try to match by IGSID (most accurate)
        account = db.query(InstagramAccount).filter(
            InstagramAccount.igsid == str(recipient_id),
            InstagramAccount.is_active == True
        ).first()
        
        # Fallback to first active account if IGSID matching fails
        if not account:
            print(f"⚠️ No account found by IGSID, trying fallback...")
        account = db.query(InstagramAccount).filter(
            InstagramAccount.is_active == True
        ).first()
        
        if not account:
            print(f"❌ No active Instagram accounts found")
            return
        
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
            print(f"🚫 IGNORING message from bot's own account!")
            print(f"   sender_id={sender_id_str}, IGSID={account_igsid_str}, PageID={account_page_id_str}")
            # Mark as processed to prevent retry
            if message_id:
                _processed_message_ids.add(message_id)
            return
        
        print(f"✅ Found account: {account.username} (ID: {account.id}, IGSID: {account.igsid}, PageID: {account.page_id})")
        
        # Mark message as processed BEFORE triggering actions (prevents loops if action triggers webhook)
        if message_id:
            _processed_message_ids.add(message_id)
            # Clean cache if it gets too large
            if len(_processed_message_ids) > _MAX_CACHE_SIZE:
                _processed_message_ids.clear()
        
        # Find active automation rules for DMs
        # We need to check BOTH:
        # 1. Rules with trigger_type='new_message' (trigger on all DMs)
        # 2. Rules with trigger_type='keyword' (trigger if keyword matches message text)
        # Note: We exclude 'post_comment' and 'live_comment' rules as they don't apply to DMs
        from app.models.automation_rule import AutomationRule
        new_message_rules = db.query(AutomationRule).filter(
            AutomationRule.instagram_account_id == account.id,
            AutomationRule.trigger_type == "new_message",
            AutomationRule.is_active == True
        ).all()
        
        keyword_rules = db.query(AutomationRule).filter(
            AutomationRule.instagram_account_id == account.id,
            AutomationRule.trigger_type == "keyword",
            AutomationRule.is_active == True
        ).all()
        
        print(f"📋 Found {len(new_message_rules)} 'new_message' rules and {len(keyword_rules)} 'keyword' rules for this account")
        
        # First, check if any keyword rule matches (exact match only)
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
        
        # Process new_message rules ONLY if no keyword rule matched
        if not keyword_rule_matched:
            for rule in new_message_rules:
                print(f"🔄 Processing 'new_message' rule: {rule.name or 'New Message Rule'} → {rule.action_type}")
                print(f"✅ 'new_message' rule triggered (no keyword match)!")
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
                asyncio.create_task(execute_automation_action(
                    rule, 
                    sender_id, 
                    account, 
                    db,
                    trigger_type="new_message",
                    message_id=message_id
                ))
            else:
                print(f"⏭️ Skipping 'new_message' rules because keyword rule matched")
                
    except Exception as e:
        print(f"❌ Error processing message: {str(e)}")
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
        if commenter_id_str and (commenter_id_str == account_igsid_str or commenter_id_str == igsid_str):
            is_bot_own_comment = True
            print(f"🚫 Ignoring bot's own comment/reply: Commenter ID {commenter_id_str} matches account IGSID {account_igsid_str or igsid_str}")
        elif commenter_username_lower and account_username_lower and commenter_username_lower == account_username_lower:
            is_bot_own_comment = True
            print(f"🚫 Ignoring bot's own comment/reply: Commenter username @{commenter_username} matches account username @{account.username}")
        
        if is_bot_own_comment:
            print(f"   This prevents infinite loops when the bot replies to comments")
            return
        
        # Find active automation rules for comments
        # We need to check BOTH:
        # 1. Rules with trigger_type='post_comment' (with optional keyword filtering)
        # 2. Rules with trigger_type='keyword' (if keyword matches comment text)
        from app.models.automation_rule import AutomationRule
        post_comment_rules = db.query(AutomationRule).filter(
            AutomationRule.instagram_account_id == account.id,
            AutomationRule.trigger_type == "post_comment",
            AutomationRule.is_active == True
        ).all()
        
        keyword_rules = db.query(AutomationRule).filter(
            AutomationRule.instagram_account_id == account.id,
            AutomationRule.trigger_type == "keyword",
            AutomationRule.is_active == True
        ).all()
        
        print(f"📋 Found {len(post_comment_rules)} 'post_comment' rules and {len(keyword_rules)} 'keyword' rules for account '{account.username}' (ID: {account.id})")
        
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
                print(f"     Rule: {rule.name or 'Unnamed'} | Trigger: {rule.trigger_type} | Active: {rule.is_active}")
        
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
                            print(f"🚫 Rule {rule.id} already processing for comment {comment_id}, skipping duplicate")
                            break
                        # Mark as processing
                        _processing_rules[processing_key] = True
                        if len(_processing_rules) > _MAX_PROCESSING_CACHE_SIZE:
                            _processing_rules.clear()
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
        
        # Process post_comment rules ONLY if no keyword rule matched
        if not keyword_rule_matched:
            for rule in post_comment_rules:
                print(f"🔄 Processing 'post_comment' rule: {rule.name or 'Comment Rule'} → {rule.action_type}")
                print(f"✅ 'post_comment' rule triggered (no keyword match)!")
                # Check if this rule is already being processed for this comment
                processing_key = f"{comment_id}_{rule.id}"
                if processing_key in _processing_rules:
                    print(f"🚫 Rule {rule.id} already processing for comment {comment_id}, skipping duplicate")
                    continue
                # Mark as processing
                _processing_rules[processing_key] = True
                if len(_processing_rules) > _MAX_PROCESSING_CACHE_SIZE:
                    _processing_rules.clear()
                # Run in background task
                asyncio.create_task(execute_automation_action(
                    rule, 
                    commenter_id, 
                    account, 
                    db,
                    trigger_type="post_comment",
                    comment_id=comment_id,
                    message_id=comment_id  # Use comment_id as identifier
                ))
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
        if commenter_id_str and (commenter_id_str == account_igsid_str or commenter_id_str == igsid_str):
            is_bot_own_comment = True
            print(f"🚫 Ignoring bot's own live comment/reply: Commenter ID {commenter_id_str} matches account IGSID {account_igsid_str or igsid_str}")
        elif commenter_username_lower and account_username_lower and commenter_username_lower == account_username_lower:
            is_bot_own_comment = True
            print(f"🚫 Ignoring bot's own live comment/reply: Commenter username @{commenter_username} matches account username @{account.username}")
        
        if is_bot_own_comment:
            print(f"   This prevents infinite loops when the bot replies to live comments")
            return
        
        # Find active automation rules for live comments
        # We need to check BOTH:
        # 1. Rules with trigger_type='live_comment' (with optional keyword filtering)
        # 2. Rules with trigger_type='keyword' (if keyword matches comment text)
        from app.models.automation_rule import AutomationRule
        live_comment_rules = db.query(AutomationRule).filter(
            AutomationRule.instagram_account_id == account.id,
            AutomationRule.trigger_type == "live_comment",
            AutomationRule.is_active == True
        ).all()
        
        keyword_rules = db.query(AutomationRule).filter(
            AutomationRule.instagram_account_id == account.id,
            AutomationRule.trigger_type == "keyword",
            AutomationRule.is_active == True
        ).all()
        
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
                            print(f"🚫 Rule {rule.id} already processing for live comment {comment_id}, skipping duplicate")
                            break
                        # Mark as processing
                        _processing_rules[processing_key] = True
                        if len(_processing_rules) > _MAX_PROCESSING_CACHE_SIZE:
                            _processing_rules.clear()
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
                    print(f"🚫 Rule {rule.id} already processing for live comment {comment_id}, skipping duplicate")
                    continue
                # Mark as processing
                _processing_rules[processing_key] = True
                if len(_processing_rules) > _MAX_PROCESSING_CACHE_SIZE:
                    _processing_rules.clear()
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
        message_id: str = None
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
        if rule.action_type == "send_dm":
            # Check monthly DM limit BEFORE sending
            from app.utils.plan_enforcement import check_dm_limit
            if not check_dm_limit(account.user_id, db):
                print(f"⚠️ Monthly DM limit reached for user {account.user_id}. Skipping DM send.")
                return  # Don't send DM if limit reached
            
            # Get message template from config
            # Support message_variations for randomization, fallback to message_template
            message_variations = rule.config.get("message_variations", [])
            if message_variations and isinstance(message_variations, list) and len(message_variations) > 0:
                # Randomly select one message from variations
                import random
                message_template = random.choice([m for m in message_variations if m and str(m).strip()])
                print(f"🎲 Randomly selected message from {len(message_variations)} variations")
            else:
                message_template = rule.config.get("message_template", "")
            
            if not message_template:
                print("⚠️ No message template configured")
                return
            
            # Apply delay if configured (delay is in minutes, convert to seconds)
            delay_minutes = rule.config.get("delay_minutes", 0)
            if delay_minutes and delay_minutes > 0:
                delay_seconds = delay_minutes * 60
                print(f"⏳ Waiting {delay_minutes} minute(s) ({delay_seconds} seconds) before sending message...")
                await asyncio.sleep(delay_seconds)
                print(f"✅ Delay complete, proceeding to send message")
            
            # Send DM using Instagram Graph API (for OAuth accounts)
            from app.utils.encryption import decrypt_credentials
            from app.utils.instagram_api import send_private_reply, send_dm as send_dm_api
            
            try:
                # Get access token - use encrypted_page_token for OAuth accounts, fallback to encrypted_credentials
                if account.encrypted_page_token:
                    access_token = decrypt_credentials(account.encrypted_page_token)
                    print(f"✅ Using OAuth page token for sending message")
                elif account.encrypted_credentials:
                    access_token = decrypt_credentials(account.encrypted_credentials)
                    print(f"⚠️ Using legacy encrypted credentials")
                else:
                    raise Exception("No access token found for account")
                
                # Check if auto-reply to comments is enabled for post_comment/live_comment triggers
                if trigger_type in ["post_comment", "live_comment"] and comment_id:
                    # Check if auto-reply to comments is enabled
                    auto_reply_to_comments = rule.config.get("auto_reply_to_comments", False)
                    comment_replies = rule.config.get("comment_replies", [])
                    
                    if auto_reply_to_comments and comment_replies and isinstance(comment_replies, list):
                        # Filter out empty replies
                        valid_replies = [r for r in comment_replies if r and str(r).strip()]
                        if valid_replies:
                            # Randomly select one comment reply
                            import random
                            selected_reply = random.choice(valid_replies)
                            print(f"💬 Auto-reply enabled: Sending PUBLIC comment reply (selected from {len(valid_replies)} variations)")
                            try:
                                from app.utils.instagram_api import send_public_comment_reply
                                # Use Instagram Business Account token (already have it as access_token)
                                # Instagram Graph API supports public comment replies on your own content
                                send_public_comment_reply(comment_id, selected_reply, access_token)
                                print(f"✅ Public comment reply sent to comment {comment_id}: {selected_reply[:50]}...")
                            except Exception as reply_error:
                                print(f"⚠️ Failed to send public comment reply: {str(reply_error)}")
                                print(f"   This might be due to missing permissions (instagram_business_manage_comments),")
                                print(f"   comment ID format, or the comment is not on your own content.")
                                print(f"   Continuing with DM send...")
                                # Continue to send DM even if public reply fails
                    
                    # Always send DM for post_comment/live_comment triggers (if message is configured)
                    if message_template:
                        page_id_for_dm = account.page_id if account.page_id else None
                        if page_id_for_dm:
                            print(f"📤 Sending DM via Page API: Page ID={page_id_for_dm}, Recipient={sender_id}")
                        else:
                            print(f"📤 Sending DM via me/messages (no page_id): Recipient={sender_id}")
                        send_dm_api(sender_id, message_template, access_token, page_id_for_dm)
                        print(f"✅ DM sent to {sender_id}")
                else:
                    # Send standard DM for new_message/keyword triggers
                    # send_dm now supports page_id=None (uses me/messages)
                    page_id_for_dm = account.page_id if account.page_id else None
                    if page_id_for_dm:
                        print(f"📤 Sending DM via Page API: Page ID={page_id_for_dm}, Recipient={sender_id}")
                    else:
                        print(f"📤 Sending DM via me/messages (no page_id): Recipient={sender_id}")
                    send_dm_api(sender_id, message_template, access_token, page_id_for_dm)
                    print(f"✅ DM sent to {sender_id}")
                
                # Log the DM
                from app.models.dm_log import DmLog
                dm_log = DmLog(
                    user_id=account.user_id,
                    instagram_account_id=account.id,
                    recipient_username=str(sender_id),  # Using sender_id as recipient username (ID format)
                    message=message_template
                )
                db.add(dm_log)
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
        print(f"❌ Error executing action: {str(e)}")
        import traceback
        traceback.print_exc()
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
    
    # Delete associated automation rules
    db.query(AutomationRule).filter(
        AutomationRule.instagram_account_id == account_id
    ).delete()
    
    # Delete associated DM logs
    db.query(DmLog).filter(
        DmLog.instagram_account_id == account_id
    ).delete()
    
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
    """
    try:
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
            url = f"https://graph.instagram.com/v21.0/{igsid}/stories"
            params = {
                "fields": "id,media_type,media_url,timestamp",
                "limit": limit,
                "access_token": access_token
            }
            
            response = requests.get(url, params=params)
            
            if response.status_code != 200:
                error_detail = response.text
                print(f"⚠️ Stories may not be available: {error_detail}")
                # Stories might not be available, return empty list
                media_items = []
            else:
                data = response.json()
                media_items = data.get("data", [])
        
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

# @router.get("/test-api")
# async def test_instagram_api():
#     token = os.getenv("INSTAGRAM_ACCESS_TOKEN")
    
#     # First get your pages
#     url = f"https://graph.facebook.com/v18.0/me/accounts?access_token={token}"
#     response = requests.get(url)
    
#     return response.json()