import json
import os
import requests
from fastapi import APIRouter, Depends, HTTPException, status, Header, Query, Request
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models.instagram_account import InstagramAccount
from app.models.automation_rule import AutomationRule
from app.models.dm_log import DmLog
from app.schemas.instagram import InstagramAccountCreate, InstagramAccountResponse
from app.utils.encryption import encrypt_credentials
from app.services.instagram_client import InstagramClient
from app.utils.auth import verify_token
from app.utils.plan_enforcement import check_account_limit

router = APIRouter()

# In-memory cache to track recently processed message IDs (prevents duplicate processing)
# Note: This is cleared on restart, but should prevent short-term loops
_processed_message_ids = set()
_MAX_CACHE_SIZE = 1000  # Limit cache size to prevent memory issues


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
                    await process_instagram_message(messaging_event, db)
                
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
        
        # Find active automation rules for this account
        from app.models.automation_rule import AutomationRule
        rules = db.query(AutomationRule).filter(
            AutomationRule.instagram_account_id == account.id,
            AutomationRule.is_active == True
        ).all()
        
        print(f"📋 Found {len(rules)} active rules for this account")
        
        # Process each rule
        for rule in rules:
            print(f"🔄 Processing rule: {rule.name or rule.trigger_type} → {rule.action_type}")
            
            # Check if rule should be triggered
            should_trigger = False
            
            if rule.trigger_type == "new_message":
                # Trigger on any new message
                should_trigger = True
            elif rule.trigger_type == "keyword":
                # Keyword trigger: check if keyword exists in message text
                if rule.config and rule.config.get("keyword"):
                    keyword = rule.config.get("keyword", "").lower()
                    if keyword in message_text.lower():
                        should_trigger = True
                        print(f"✅ Keyword '{keyword}' found in message")
                    else:
                        print(f"⏭️ Keyword '{keyword}' not found in message")
                else:
                    print(f"⚠️ Keyword trigger rule has no keyword configured, skipping")
                    continue
            
            if should_trigger:
                print(f"✅ Rule triggered! Executing action: {rule.action_type}")
                await execute_automation_action(
                    rule, 
                    sender_id, 
                    account, 
                    db,
                    trigger_type=rule.trigger_type  # Pass the actual trigger type
                )
            else:
                print(f"⏭️ Rule not triggered")
                
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
        
        # Find active automation rules for "post_comment" trigger
        from app.models.automation_rule import AutomationRule
        rules = db.query(AutomationRule).filter(
            AutomationRule.instagram_account_id == account.id,
            AutomationRule.trigger_type == "post_comment",
            AutomationRule.is_active == True
        ).all()
        
        print(f"📋 Found {len(rules)} active 'post_comment' rules for account '{account.username}' (ID: {account.id})")
        
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
        
        if len(rules) == 0:
            print(f"⚠️ WARNING: No 'post_comment' rules found for account '{account.username}' (ID: {account.id})")
            print(f"   Make sure you created a rule with trigger_type='post_comment' for this Instagram account!")
        
        # Execute action for each rule
        for rule in rules:
            print(f"🔄 Processing rule: {rule.name or 'Comment Rule'} → {rule.action_type}")
            # Check keyword filter if configured
            should_trigger = True
            if rule.config and rule.config.get("keyword"):
                keyword = rule.config.get("keyword", "").lower()
                if keyword not in comment_text.lower():
                    should_trigger = False
                    print(f"⏭️ Keyword '{keyword}' not found in comment")
            
            if should_trigger:
                await execute_automation_action(
                    rule, 
                    commenter_id, 
                    account, 
                    db,
                    trigger_type="post_comment",
                    comment_id=comment_id
                )
                
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
        
        # Find active automation rules for "live_comment" trigger
        from app.models.automation_rule import AutomationRule
        rules = db.query(AutomationRule).filter(
            AutomationRule.instagram_account_id == account.id,
            AutomationRule.trigger_type == "live_comment",
            AutomationRule.is_active == True
        ).all()
        
        print(f"📋 Found {len(rules)} active 'live_comment' rules for this account")
        
        # Execute action for each rule
        for rule in rules:
            print(f"🔄 Processing rule: {rule.name or 'Live Comment Rule'} → {rule.action_type}")
            # Check keyword filter if configured
            should_trigger = True
            if rule.config and rule.config.get("keyword"):
                keyword = rule.config.get("keyword", "").lower()
                if keyword not in comment_text.lower():
                    should_trigger = False
                    print(f"⏭️ Keyword '{keyword}' not found in live comment")
            
            if should_trigger:
                await execute_automation_action(
                    rule,
                    commenter_id,
                    account,
                    db,
                    trigger_type="live_comment",
                    comment_id=comment_id
                )
                
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
    comment_id: str = None
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
    """
    try:
        if rule.action_type == "send_dm":
            # Get message template from config
            message_template = rule.config.get("message_template", "")
            if not message_template:
                print("⚠️ No message template configured")
                return
            
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
                
                if not account.page_id:
                    raise Exception("Page ID not found. Account may not be properly connected via OAuth.")
                
                # Use appropriate endpoint based on trigger type
                # For comments, use private_replies endpoint (no 24h window restriction)
                # For messages/DMs, use standard messages endpoint
                if trigger_type in ["post_comment", "live_comment"] and comment_id:
                    # Send private reply to comment using Instagram private reply format
                    print(f"💬 Sending private reply to comment: Comment ID={comment_id}")
                    send_private_reply(comment_id, message_template, access_token, account.page_id)
                    print(f"✅ Private reply sent to comment {comment_id}")
                else:
                    # Send standard DM
                    print(f"📤 Sending DM via Page API: Page ID={account.page_id}, Recipient={sender_id}")
                    send_dm_api(sender_id, message_template, account.page_id, access_token)
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

# @router.get("/test-api")
# async def test_instagram_api():
#     token = os.getenv("INSTAGRAM_ACCESS_TOKEN")
    
#     # First get your pages
#     url = f"https://graph.facebook.com/v18.0/me/accounts?access_token={token}"
#     response = requests.get(url)
    
#     return response.json()