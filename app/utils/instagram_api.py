"""
Instagram Graph API utility functions for sending messages and replies.
"""
import requests


def send_public_comment_reply(comment_id: str, message: str, page_access_token: str, page_id: str = None) -> dict:
    """
    Send a PUBLIC reply to an Instagram comment (visible on the post/reel).
    
    This endpoint creates a public comment reply that appears on the post/reel,
    not a private DM. This is different from send_private_reply which sends a DM.
    
    IMPORTANT: Instagram Graph API (graph.instagram.com) does NOT support public comment replies.
    We must use Facebook Graph API (graph.facebook.com) with the Facebook Page token instead.
    
    Args:
        comment_id: The Instagram comment ID (e.g., "17890603191406594")
        message: The message text to send as a public comment reply
        page_access_token: The Facebook Page access token (required for public replies)
        page_id: Optional page ID (not required for this endpoint)
        
    Returns:
        dict: API response with reply ID
        
    Raises:
        Exception: If the API request fails
    """
    # Instagram public comment replies MUST use Facebook Graph API, not Instagram Graph API
    # The endpoint is: POST /{comment_id}/replies on graph.facebook.com
    url = f"https://graph.facebook.com/v21.0/{comment_id}/replies"
    
    # Debug logging
    token_preview = page_access_token[:10] + "..." if page_access_token else "None"
    print(f"💬 Sending PUBLIC comment reply via Facebook Graph API:")
    print(f"   URL: {url}")
    print(f"   Using Token: {token_preview}")
    print(f"   Comment ID: {comment_id}")
    print(f"   Message: {message[:50]}..." if len(message) > 50 else f"   Message: {message}")
    
    # Facebook Graph API public comment reply format
    payload = {
        "message": message
    }
    
    params = {
        "access_token": page_access_token
    }
    
    response = requests.post(url, json=payload, params=params)
    
    if response.status_code != 200:
        error_detail = response.text
        print(f"❌ Failed to send public comment reply: {error_detail}")
        raise Exception(f"Failed to send public comment reply: {error_detail}")
    
    result = response.json()
    print(f"✅ Public comment reply sent successfully: {result}")
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


def send_dm(recipient_id: str, message: str, page_access_token: str, page_id: str = None) -> dict:
    """
    Send a direct message to an Instagram user.
    
    Note: This requires the recipient to have messaged you first, or you need
    to be within the 24-hour messaging window for standard messaging.
    
    For Instagram Business Login flow, we use Instagram Graph API (graph.instagram.com)
    instead of Facebook Graph API (graph.facebook.com) since we have Instagram-native tokens.
    
    Args:
        recipient_id: The Instagram user ID to send the message to
        message: The message text
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
    print(f"📤 Sending DM via Instagram Graph API:")
    print(f"   URL: {url}")
    print(f"   Using Token: {token_preview}")
    print(f"   Recipient ID: {recipient_id}")
    print(f"   Message: {message[:50]}..." if len(message) > 50 else f"   Message: {message}")
    
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": message}
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