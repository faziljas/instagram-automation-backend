"""
Instagram Graph API utility functions for sending messages and replies.
"""
import requests


def send_public_comment_reply(comment_id: str, message: str, instagram_access_token: str) -> dict:
    """
    Send a PUBLIC reply to an Instagram comment (visible on the post/reel).
    
    This endpoint creates a public comment reply that appears on the post/reel,
    not a private DM. This is different from send_private_reply which sends a DM.
    
    Instagram Graph API DOES support public comment replies on your own content.
    The endpoint is: POST /{comment_id}/replies on graph.instagram.com
    
    Args:
        comment_id: The Instagram comment ID (e.g., "17890603191406594")
        message: The message text to send as a public comment reply
        instagram_access_token: The Instagram Business Account access token (Instagram-native token)
        
    Returns:
        dict: API response with reply ID
        
    Raises:
        Exception: If the API request fails
    """
    # Instagram Graph API public comment reply endpoint
    # POST /{comment_id}/replies on graph.instagram.com
    url = f"https://graph.instagram.com/v21.0/{comment_id}/replies"
    
    # Debug logging
    token_preview = instagram_access_token[:10] + "..." if instagram_access_token else "None"
    print(f"💬 Sending PUBLIC comment reply via Instagram Graph API:")
    print(f"   URL: {url}")
    print(f"   Using Token: {token_preview}")
    print(f"   Comment ID: {comment_id}")
    print(f"   Message: {message[:50]}..." if len(message) > 50 else f"   Message: {message}")
    
    # Instagram Graph API public comment reply format
    payload = {
        "message": message
    }
    
    headers = {
        "Authorization": f"Bearer {instagram_access_token}"
    }
    
    response = requests.post(url, json=payload, headers=headers)
    
    if response.status_code != 200:
        error_detail = response.text
        print(f"❌ Failed to send public comment reply: {error_detail}")
        raise Exception(f"Failed to send public comment reply: {error_detail}")
    
    result = response.json()
    reply_id = result.get("id", "unknown")
    print(f"✅ Public comment reply sent successfully!")
    print(f"   Reply ID: {reply_id}")
    print(f"   This reply should now be visible on Instagram under the comment")
    print(f"   Full API response: {result}")
    return result


def send_private_reply(comment_id: str, message: str, page_access_token: str, page_id: str = None) -> dict:
    """
    Send a private reply to an Instagram comment.
    
    This endpoint allows replying to comments without the 24-hour messaging window restriction.
    For Instagram, we use the standard messages endpoint with a special recipient format.
    
    For Instagram Business Login flow, we use Instagram Graph API (graph.instagram.com)
    instead of Facebook Graph API (graph.facebook.com) since we have Instagram-native tokens.
    
    Args:
        comment_id: The Instagram comment ID (e.g., "17890603191406594")
        message: The message text to send as a private reply
        page_access_token: The Instagram Business Account access token (Instagram-native)
        page_id: Optional page ID. For Instagram-native tokens, this is typically None (uses me/messages)
        
    Returns:
        dict: API response
        
    Raises:
        Exception: If the API request fails
    """
    # For Instagram Business Login flow, use Instagram Graph API
    # Use page_id if provided, otherwise use 'me' (works with Instagram Business Account token)
    endpoint = f"{page_id}/messages" if page_id else "me/messages"
    url = f"https://graph.instagram.com/v21.0/{endpoint}"
    
    # Debug logging
    token_preview = page_access_token[:10] + "..." if page_access_token else "None"
    print(f"💬 Sending private reply via Instagram Graph API:")
    print(f"   URL: {url}")
    print(f"   Using Token: {token_preview}")
    print(f"   Comment ID: {comment_id}")
    print(f"   Message: {message[:50]}..." if len(message) > 50 else f"   Message: {message}")
    
    # Instagram private reply format: recipient uses comment_id instead of id
    payload = {
        "recipient": {
            "comment_id": comment_id
        },
        "message": {
            "text": message
        }
    }
    
    headers = {
        "Authorization": f"Bearer {page_access_token}"
    }
    
    response = requests.post(url, json=payload, headers=headers)
    
    if response.status_code != 200:
        error_detail = response.text
        print(f"❌ Failed to send private reply: {error_detail}")
        raise Exception(f"Failed to send private reply: {error_detail}")
    
    result = response.json()
    print(f"✅ Private reply sent successfully: {result}")
    return result


def send_dm(recipient_id: str, message: str, page_access_token: str, page_id: str = None, buttons: list = None) -> dict:
    """
    Send a direct message to an Instagram user with optional buttons/quick replies.
    
    Note: This requires the recipient to have messaged you first, or you need
    to be within the 24-hour messaging window for standard messaging.
    
    For Instagram Business Login flow, we use Instagram Graph API (graph.instagram.com)
    instead of Facebook Graph API (graph.facebook.com) since we have Instagram-native tokens.
    
    Args:
        recipient_id: The Instagram user ID to send the message to
        message: The message text
        page_access_token: The Instagram Business Account access token (Instagram-native)
        page_id: Optional page ID. For Instagram-native tokens, this is typically None (uses me/messages)
        buttons: Optional list of button objects with format: [{"text": "Button Text", "url": "https://..."}]
                 Maximum 13 buttons, text max 20 characters
        
    Returns:
        dict: API response
        
    Raises:
        Exception: If the API request fails
    """
    # For Instagram Business Login flow, use Instagram Graph API
    # Use page_id if provided, otherwise use 'me' (works with Instagram Business Account token)
    endpoint = f"{page_id}/messages" if page_id else "me/messages"
    url = f"https://graph.instagram.com/v21.0/{endpoint}"
    
    # Debug logging
    token_preview = page_access_token[:10] + "..." if page_access_token else "None"
    print(f"📤 Sending DM via Instagram Graph API:")
    print(f"   URL: {url}")
    print(f"   Using Token: {token_preview}")
    print(f"   Recipient ID: {recipient_id}")
    print(f"   Message: {message[:50]}..." if len(message) > 50 else f"   Message: {message}")
    
    # Build message payload
    # Instagram quick_replies only support text buttons (content_type: "text")
    # For URL buttons, we need to use a generic template format instead
    if buttons and isinstance(buttons, list) and len(buttons) > 0:
        # Filter valid buttons (must have text and url)
        valid_buttons = [b for b in buttons if b.get("text") and b.get("url")]
        if valid_buttons:
            # Limit to 3 buttons (Instagram's max for generic template)
            valid_buttons = valid_buttons[:3]
            
            # Build button array for generic template
            template_buttons = []
            for button in valid_buttons:
                # Truncate button text to 20 characters (Instagram's max)
                button_text = str(button["text"])[:20]
                button_url = str(button["url"])
                
                template_buttons.append({
                    "type": "web_url",
                    "url": button_url,
                    "title": button_text
                })
            
            if template_buttons:
                # Use generic template format for messages with URL buttons
                # Generic template allows combining text with URL buttons
                message_payload = {
                    "attachment": {
                        "type": "template",
                        "payload": {
                            "template_type": "generic",
                            "elements": [
                                {
                                    "title": message[:80] if len(message) > 80 else message,  # Title is required
                                    "subtitle": message if len(message) > 80 else "",  # Optional subtitle
                                    "buttons": template_buttons
                                }
                            ]
                        }
                    }
                }
                print(f"   Using generic template with {len(template_buttons)} URL button(s)")
                for i, btn in enumerate(template_buttons, 1):
                    print(f"      Button {i}: {btn['title']} -> {btn['url']}")
            else:
                # Fallback to plain text if no valid buttons
                message_payload = {"text": message}
        else:
            # No valid buttons, use plain text
            message_payload = {"text": message}
    else:
        # No buttons, use plain text message
        message_payload = {
            "text": message
        }
    
    payload = {
        "recipient": {"id": recipient_id},
        "message": message_payload
    }
    
    headers = {
        "Authorization": f"Bearer {page_access_token}"
    }
    
    response = requests.post(url, json=payload, headers=headers)
    
    if response.status_code != 200:
        error_detail = response.text
        print(f"❌ Failed to send DM: {error_detail}")
        raise Exception(f"Failed to send DM: {error_detail}")
    
    result = response.json()
    print(f"✅ DM sent successfully: {result}")
    return result