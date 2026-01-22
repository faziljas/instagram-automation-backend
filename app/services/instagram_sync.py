"""
Service for syncing Instagram conversations and messages from Instagram Graph API.
"""
import requests
from typing import List, Dict, Optional
from sqlalchemy.orm import Session
from datetime import datetime
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.instagram_account import InstagramAccount
from app.utils.encryption import decrypt_credentials


def resolve_username(igsid: str, access_token: str) -> Optional[str]:
    """
    Resolve Instagram username from IGSID by calling Instagram Graph API.
    
    Args:
        igsid: Instagram user ID (IGSID)
        access_token: Instagram access token
        
    Returns:
        Username if found, None otherwise
    """
    try:
        url = f"https://graph.instagram.com/v21.0/{igsid}"
        params = {
            "fields": "username",
            "access_token": access_token
        }
        
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            return data.get("username")
        else:
            print(f"⚠️ Could not resolve username for IGSID {igsid}: {response.text}")
            return None
    except Exception as e:
        print(f"❌ Error resolving username for {igsid}: {str(e)}")
        return None


def sync_instagram_conversations(
    user_id: int,
    account_id: int,
    db: Session,
    limit: int = 25
) -> Dict:
    """
    Sync Instagram DM conversations from Instagram Graph API.
    
    Note: Instagram Graph API doesn't have a direct /conversations endpoint like Facebook Messenger.
    We'll use alternative approaches:
    1. Try to fetch conversations if the endpoint exists
    2. Fallback to using webhook-stored messages to build conversations
    
    Args:
        user_id: User ID (business owner)
        account_id: Instagram account ID
        db: Database session
        limit: Maximum number of conversations to fetch
        
    Returns:
        Dict with sync results
    """
    try:
        # Get Instagram account
        account = db.query(InstagramAccount).filter(
            InstagramAccount.id == account_id,
            InstagramAccount.user_id == user_id
        ).first()
        
        if not account:
            raise ValueError(f"Instagram account {account_id} not found for user {user_id}")
        
        # Get access token
        if account.encrypted_page_token:
            access_token = decrypt_credentials(account.encrypted_page_token)
        elif account.encrypted_credentials:
            access_token = decrypt_credentials(account.encrypted_credentials)
        else:
            raise ValueError("No access token found for this account")
        
        igsid = account.igsid
        if not igsid:
            raise ValueError("Instagram Business Account ID (IGSID) not found")
        
        print(f"🔄 Syncing conversations for account {account.username} (IGSID: {igsid})")
        
        # Instagram Graph API doesn't have a direct /conversations endpoint
        # Instead, we'll build conversations from existing messages in our database
        # and try to fetch additional data if available
        
        # Get all unique participants from Message table
        from sqlalchemy import func, distinct, or_
        
        # Get conversations from existing messages
        incoming_participants = db.query(
            Message.sender_id,
            Message.sender_username,
            func.max(Message.created_at).label('last_message_at'),
            func.count(Message.id).label('message_count')
        ).filter(
            Message.instagram_account_id == account_id,
            Message.user_id == user_id,
            Message.is_from_bot == False  # Received messages
        ).group_by(
            Message.sender_id,
            Message.sender_username
        ).all()
        
        outgoing_participants = db.query(
            Message.recipient_id,
            Message.recipient_username,
            func.max(Message.created_at).label('last_message_at'),
            func.count(Message.id).label('message_count')
        ).filter(
            Message.instagram_account_id == account_id,
            Message.user_id == user_id,
            Message.is_from_bot == True  # Sent messages
        ).group_by(
            Message.recipient_id,
            Message.recipient_username
        ).all()
        
        # Merge participants
        participants_map = {}
        
        # Process incoming
        for p in incoming_participants:
            participant_id = str(p.sender_id)
            if participant_id not in participants_map:
                participants_map[participant_id] = {
                    'participant_id': participant_id,
                    'participant_name': p.sender_username,
                    'last_message_at': p.last_message_at,
                    'message_count': p.message_count
                }
            elif p.last_message_at > participants_map[participant_id]['last_message_at']:
                participants_map[participant_id].update({
                    'participant_name': p.sender_username or participants_map[participant_id]['participant_name'],
                    'last_message_at': p.last_message_at,
                    'message_count': participants_map[participant_id]['message_count'] + p.message_count
                })
        
        # Process outgoing
        for p in outgoing_participants:
            participant_id = str(p.recipient_id)
            if participant_id not in participants_map:
                participants_map[participant_id] = {
                    'participant_id': participant_id,
                    'participant_name': p.recipient_username,
                    'last_message_at': p.last_message_at,
                    'message_count': p.message_count
                }
            elif p.last_message_at > participants_map[participant_id]['last_message_at']:
                participants_map[participant_id].update({
                    'participant_name': p.recipient_username or participants_map[participant_id]['participant_name'],
                    'last_message_at': p.last_message_at,
                    'message_count': participants_map[participant_id]['message_count'] + p.message_count
                })
        
        # Create/update Conversation records
        conversations_created = 0
        conversations_updated = 0
        
        for participant_id, data in participants_map.items():
            # Resolve username if not available
            participant_name = data['participant_name']
            if not participant_name:
                print(f"🔍 Resolving username for participant {participant_id}...")
                participant_name = resolve_username(participant_id, access_token)
                if participant_name:
                    print(f"✅ Resolved username: @{participant_name}")
            
            # Find or create conversation
            conversation = db.query(Conversation).filter(
                Conversation.instagram_account_id == account_id,
                Conversation.user_id == user_id,
                Conversation.participant_id == participant_id
            ).first()
            
            # Get latest message for preview
            latest_message = db.query(Message).filter(
                Message.instagram_account_id == account_id,
                Message.user_id == user_id,
                or_(
                    (Message.sender_id == participant_id),
                    (Message.recipient_id == participant_id)
                )
            ).order_by(Message.created_at.desc()).first()
            
            last_message_preview = ""
            if latest_message:
                last_message_preview = latest_message.get_content() or "[Media]"
                if len(last_message_preview) > 100:
                    last_message_preview = last_message_preview[:100] + "..."
            
            if conversation:
                # Update existing conversation
                conversation.participant_name = participant_name or conversation.participant_name
                conversation.last_message = last_message_preview
                conversation.updated_at = data['last_message_at'] or datetime.utcnow()
                conversations_updated += 1
            else:
                # Create new conversation
                conversation = Conversation(
                    user_id=user_id,
                    instagram_account_id=account_id,
                    participant_id=participant_id,
                    participant_name=participant_name,
                    last_message=last_message_preview,
                    updated_at=data['last_message_at'] or datetime.utcnow()
                )
                db.add(conversation)
                conversations_created += 1
            
            # Update messages to link to conversation
            if conversation.id:
                db.query(Message).filter(
                    Message.instagram_account_id == account_id,
                    Message.user_id == user_id,
                    or_(
                        (Message.sender_id == participant_id),
                        (Message.recipient_id == participant_id)
                    ),
                    Message.conversation_id.is_(None)
                ).update({
                    Message.conversation_id: conversation.id
                }, synchronize_session=False)
        
        db.commit()
        
        print(f"✅ Sync complete: {conversations_created} created, {conversations_updated} updated")
        
        return {
            "success": True,
            "conversations_created": conversations_created,
            "conversations_updated": conversations_updated,
            "total_participants": len(participants_map)
        }
        
    except Exception as e:
        db.rollback()
        print(f"❌ Error syncing conversations: {str(e)}")
        import traceback
        traceback.print_exc()
        raise


def sync_conversation_messages(
    conversation_id: int,
    user_id: int,
    account_id: int,
    db: Session,
    limit: int = 100
) -> Dict:
    """
    Sync messages for a specific conversation.
    This is mainly for ensuring all messages are linked to the conversation.
    
    Args:
        conversation_id: Conversation ID
        user_id: User ID
        account_id: Instagram account ID
        db: Database session
        limit: Maximum messages to process
        
    Returns:
        Dict with sync results
    """
    try:
        conversation = db.query(Conversation).filter(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
            Conversation.instagram_account_id == account_id
        ).first()
        
        if not conversation:
            raise ValueError("Conversation not found")
        
        # Link all messages for this participant to the conversation
        updated_count = db.query(Message).filter(
            Message.instagram_account_id == account_id,
            Message.user_id == user_id,
            or_(
                (Message.sender_id == conversation.participant_id),
                (Message.recipient_id == conversation.participant_id)
            ),
            Message.conversation_id.is_(None)
        ).update({
            Message.conversation_id: conversation_id
        }, synchronize_session=False)
        
        # Update conversation's last_message and updated_at
        latest_message = db.query(Message).filter(
            Message.conversation_id == conversation_id
        ).order_by(Message.created_at.desc()).first()
        
        if latest_message:
            last_message_preview = latest_message.get_content() or "[Media]"
            if len(last_message_preview) > 100:
                last_message_preview = last_message_preview[:100] + "..."
            
            conversation.last_message = last_message_preview
            conversation.updated_at = latest_message.created_at or datetime.utcnow()
        
        db.commit()
        
        return {
            "success": True,
            "messages_linked": updated_count
        }
        
    except Exception as e:
        db.rollback()
        print(f"❌ Error syncing conversation messages: {str(e)}")
        raise
