"""
Instagram Graph API utility functions for sending messages and replies.
"""
import requests


def send_private_reply(comment_id: str, message: str, page_access_token: str, page_id: str = None) -> dict:
    """
    Send a private reply to an Instagram comment.
    
    This endpoint allows replying to comments without the 24-hour messaging window restriction.
    For Instagram, we use the standard messages endpoint with a special recipient format.
    
    Args:
        comment_id: The Instagram comment ID (e.g., "17890603191406594")
        message: The message text to send as a private reply
        page_access_token: The Facebook Page access token
        page_id: Optional page ID. If provided, uses {page_id}/messages, otherwise uses me/messages
        
    Returns:
        dict: API response
        
    Raises:
        Exception: If the API request fails
    """
    # Use page_id if provided, otherwise use 'me' (resolves to the page with page token)
    endpoint = f"{page_id}/messages" if page_id else "me/messages"
    url = f"https://graph.facebook.com/v19.0/{endpoint}"
    
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
        raise Exception(f"Failed to send private reply: {error_detail}")
    
    return response.json()


def send_dm(recipient_id: str, message: str, page_id: str, page_access_token: str) -> dict:
    """
    Send a direct message to an Instagram user.
    
    Note: This requires the recipient to have messaged you first, or you need
    to be within the 24-hour messaging window for standard messaging.
    
    Args:
        recipient_id: The Instagram user ID to send the message to
        message: The message text
        page_id: The Facebook Page ID
        page_access_token: The Facebook Page access token
        
    Returns:
        dict: API response
        
    Raises:
        Exception: If the API request fails
    """
    url = f"https://graph.facebook.com/v19.0/{page_id}/messages"
    
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": message}
    }
    
    params = {
        "access_token": page_access_token
    }
    
    response = requests.post(url, json=payload, params=params)
    
    if response.status_code != 200:
        error_detail = response.text
        raise Exception(f"Failed to send DM: {error_detail}")
    
    return response.json()