"""
Instagram Graph API utility functions for sending messages and replies.
"""
import time
import requests

# Instagram private reply / DM text limit (conservative to avoid Meta "unknown error")
PRIVATE_REPLY_MESSAGE_MAX_LENGTH = 500


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


def send_private_reply(comment_id: str, message: str, page_access_token: str, page_id: str = None, quick_replies: list = None) -> dict:
    """
    Send a private reply to an Instagram comment with optional quick replies.
    
    This endpoint allows replying to comments without the 24-hour messaging window restriction.
    For Instagram, we use the standard messages endpoint with a special recipient format.
    
    For Instagram Business Login flow, we use Instagram Graph API (graph.instagram.com)
    instead of Facebook Graph API (graph.facebook.com) since we have Instagram-native tokens.
    
    Args:
        comment_id: The Instagram comment ID (e.g., "17890603191406594")
        message: The message text to send as a private reply
        page_access_token: The Instagram Business Account access token (Instagram-native)
        page_id: Optional page ID. For Instagram-native tokens, this is typically None (uses me/messages)
        quick_replies: Optional list of quick reply objects with format: [{"content_type": "text", "title": "Button Text", "payload": "PAYLOAD"}]
                      Maximum 13 quick replies, title max 20 characters
        
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
    if quick_replies:
        print(f"   Quick Replies: {len(quick_replies)} button(s)")
    
    # Instagram private reply format: recipient uses comment_id instead of id
    # Truncate message to avoid Meta "unknown error" (OAuthException code 1) from long text
    text = (message or "").strip()
    if len(text) > PRIVATE_REPLY_MESSAGE_MAX_LENGTH:
        text = text[:PRIVATE_REPLY_MESSAGE_MAX_LENGTH - 3] + "..."
        print(f"⚠️ [PRIVATE REPLY] Message truncated to {PRIVATE_REPLY_MESSAGE_MAX_LENGTH} chars")
    message_payload = {
        "text": text or " "
    }
    
    # Add quick replies if provided
    if quick_replies and isinstance(quick_replies, list) and len(quick_replies) > 0:
        # Filter valid quick replies (must have content_type, title, and payload)
        valid_quick_replies = []
        for qr in quick_replies:
            if qr.get("content_type") == "text" and qr.get("title") and qr.get("payload"):
                # Truncate title to 20 characters (Instagram's max)
                valid_quick_replies.append({
                    "content_type": "text",
                    "title": str(qr["title"])[:20],
                    "payload": str(qr["payload"])[:1000]  # payload max ~1000
                })
        
        # Limit to 13 quick replies (Instagram's max)
        valid_quick_replies = valid_quick_replies[:13]
        
        if valid_quick_replies:
            message_payload["quick_replies"] = valid_quick_replies
    
    payload = {
        "recipient": {
            "comment_id": comment_id
        },
        "message": message_payload
    }
    
    headers = {
        "Authorization": f"Bearer {page_access_token}"
    }
    
    def _do_post():
        return requests.post(url, json=payload, headers=headers)
    
    response = _do_post()
    if response.status_code != 200:
        error_detail = response.text
        # Do NOT retry on Meta code 1 (unknown error): the message is often delivered before the error.
        # Retrying would send a duplicate message.
        print(f"❌ Failed to send private reply: {error_detail}")
        raise Exception(f"Failed to send private reply: {error_detail}")
    
    result = response.json()
    print(f"✅ Private reply sent successfully: {result}")
    return result


def send_dm(recipient_id: str, message: str, page_access_token: str, page_id: str = None, buttons: list = None, quick_replies: list = None, media_url: str = None, media_type: str = None, card_image_url: str = None, card_title: str = None, card_subtitle: str = None, card_button: dict = None) -> dict:
    """
    Send a direct message to an Instagram user with optional buttons/quick replies/media.
    
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
                 Maximum 3 buttons for generic template, text max 20 characters
        quick_replies: Optional list of quick reply objects with format: [{"content_type": "text", "title": "Button Text", "payload": "PAYLOAD"}]
                      Maximum 13 quick replies, title max 20 characters
        media_url: Optional public URL for image or video attachment (must be accessible by Instagram servers)
        media_type: "image" or "video" when media_url is set; inferred from URL if not provided
        card_image_url: Optional public URL for card image (generic template with image)
        card_title: Title for card element (max 80 chars)
        card_subtitle: Subtitle for card element (optional)
        card_button: {"text": str, "url": str} for card button (optional)
        
    Returns:
        dict: API response
        
    Raises:
        Exception: If the API request fails
    """
    # For Instagram Business Login flow, use Instagram Graph API
    # Use page_id if provided, otherwise use 'me' (works with Instagram Business Account token)
    endpoint = f"{page_id}/messages" if page_id else "me/messages"
    api_url = f"https://graph.instagram.com/v21.0/{endpoint}"
    
    # Debug logging
    token_preview = page_access_token[:10] + "..." if page_access_token else "None"
    print(f"📤 Sending DM via Instagram Graph API:")
    print(f"   URL: {api_url}")
    print(f"   Using Token: {token_preview}")
    print(f"   Recipient ID: {recipient_id}")
    print(f"   Message: {message[:50]}..." if len(message) > 50 else f"   Message: {message}")
    
    headers = {"Authorization": f"Bearer {page_access_token}"}
    
    # Send media attachment first if provided (must be publicly accessible URL - not localhost)
    if media_url and str(media_url).strip():
        media_url_clean = str(media_url).strip()
        if "localhost" in media_url_clean or "127.0.0.1" in media_url_clean:
            print(f"⚠️ Skipping media attachment: URL must be publicly accessible (localhost/127.0.0.1 not reachable by Instagram)")
        else:
            # Infer media_type from URL if not provided (common extensions)
            inferred_type = media_type
            if not inferred_type:
                lower = media_url_clean.lower()
                if any(ext in lower for ext in (".mp3", ".m4a", ".ogg", ".wav", ".aac")):
                    inferred_type = "audio"
                elif any(ext in lower for ext in (".mp4", ".mov", ".webm", ".avi")):
                    inferred_type = "video"
                elif any(ext in lower for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp")):
                    inferred_type = "image"
                else:
                    # Extensionless URL detected (LogicDM uploads)! Dynamically check Content-Type
                    try:
                        print(f"🔍 [MEDIA CHECK] Checking MIME type for extensionless URL...")
                        head_req = requests.head(media_url_clean, allow_redirects=True, timeout=5)
                        content_type = head_req.headers.get("Content-Type", "").lower()
                        if "video" in content_type:
                            inferred_type = "video"
                        elif "audio" in content_type:
                            inferred_type = "audio"
                        else:
                            inferred_type = "image"
                        print(f"✅ [MEDIA CHECK] Detected {content_type} -> Set to {inferred_type}")
                    except Exception as e:
                        print(f"⚠️ [MEDIA CHECK] Failed to get headers: {e}. Defaulting to image.")
                        inferred_type = "image"
            if inferred_type not in ("image", "video", "audio"):
                inferred_type = "image"
            try:
                media_payload = {
                    "recipient": {"id": recipient_id},
                    "message": {
                        "attachment": {
                            "type": inferred_type,
                            "payload": {"url": media_url_clean}
                        }
                    }
                }
                resp = requests.post(api_url, json=media_payload, headers=headers)
                if resp.status_code == 200:
                    print(f"✅ Media ({inferred_type}) sent successfully")
                    # If no text/buttons/quick_replies, we're done
                    if not (message and str(message).strip()) and not buttons and not (quick_replies and len(quick_replies) > 0):
                        return resp.json()
                else:
                    err_body = resp.text
                    print(f"⚠️ Failed to send media: {resp.status_code} {err_body}")
                    # Log so debugging attached media issues is easier (Instagram may reject URL or format)
                    if resp.status_code >= 400:
                        print(f"   Media URL (first 80 chars): {media_url_clean[:80]}...")
            except Exception as media_err:
                print(f"⚠️ Error sending media attachment: {media_err}")
    
    # Card: generic template with image, title, subtitle, optional button
    if card_image_url and str(card_image_url).strip():
        card_url_clean = str(card_image_url).strip()
        if "localhost" in card_url_clean or "127.0.0.1" in card_url_clean:
            print(f"⚠️ Skipping card: image URL must be publicly accessible (localhost/127.0.0.1 not reachable by Instagram)")
        else:
            element = {
                "image_url": card_url_clean,
                "title": (card_title or "")[:80] or " ",
                "subtitle": (card_subtitle or "")[:80] if card_subtitle else "",
            }
            template_buttons = []
            if card_button and card_button.get("text") and card_button.get("url"):
                template_buttons.append({
                    "type": "web_url",
                    "url": str(card_button["url"]),
                    "title": str(card_button["text"])[:20]
                })
            if template_buttons:
                element["buttons"] = template_buttons
            try:
                card_payload = {
                    "recipient": {"id": recipient_id},
                    "message": {
                        "attachment": {
                            "type": "template",
                            "payload": {
                                "template_type": "generic",
                                "elements": [element]
                            }
                        }
                    }
                }
                resp = requests.post(api_url, json=card_payload, headers=headers)
                if resp.status_code == 200:
                    print(f"✅ Card sent successfully")
                    if not (message and str(message).strip()) and not (quick_replies and len(quick_replies) > 0):
                        return resp.json()
                else:
                    print(f"⚠️ Failed to send card: {resp.status_code} {resp.text}")
            except Exception as card_err:
                print(f"⚠️ Error sending card: {card_err}")
    
    # Don't send empty text — Instagram returns "Empty text" (code 100, subcode 2534052)
    if not (message and str(message).strip()) and not buttons and not (quick_replies and len(quick_replies) > 0):
        print(f"⏭️ No text/buttons/quick_replies to send; skipping text message (media/card may have been sent above)")
        return {}
    
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
                # Check if we have both buttons and quick_replies (combined follow + email)
                # If so, create TWO elements: one for follow question, one for email question
                # Each element gets its own title (bold) and respective buttons
                if "\n\n" in message and quick_replies:
                    # Split message into follow question and email question
                    follow_question, email_question = message.split("\n\n", 1)
                    follow_question = follow_question.strip()
                    email_question = email_question.strip()
                    
                    # Create two elements for better UX
                    elements = []
                    
                    # Element 1: Follow question with Follow Me button
                    if follow_question:
                        elements.append({
                            "title": follow_question[:80],  # Bold title
                            "subtitle": "",  # Empty subtitle
                            "buttons": template_buttons  # Follow Me button
                        })
                    
                    # Element 2: Email question (will get quick replies at message level)
                    if email_question:
                        elements.append({
                            "title": email_question[:80],  # Bold title
                            "subtitle": "",  # Empty subtitle
                            "buttons": []  # No URL buttons, will use quick replies
                        })
                    
                    message_payload = {
                        "attachment": {
                            "type": "template",
                            "payload": {
                                "template_type": "generic",
                                "elements": elements
                            }
                        }
                    }
                    print(f"   Using generic template with {len(elements)} element(s)")
                    print(f"      Element 1 (Follow): {follow_question[:50]}... with {len(template_buttons)} button(s)")
                    print(f"      Element 2 (Email): {email_question[:50]}... (quick replies below)")
                else:
                    # Single element (no quick replies or no double newline)
                    title_text = message
                    subtitle_text = ""
                    if "\n\n" in message:
                        first, rest = message.split("\n\n", 1)
                        title_text = first.strip() or message
                        subtitle_text = rest.strip()
                    else:
                        # Fallback: truncate long text into title/subtitle
                        if len(message) > 80:
                            title_text = message[:80]
                            subtitle_text = message
                        else:
                            title_text = message
                            subtitle_text = ""

                    # Use generic template format for messages with URL buttons
                    element = {
                        "title": title_text,
                        "subtitle": subtitle_text,
                        "buttons": template_buttons
                    }
                    
                    message_payload = {
                        "attachment": {
                            "type": "template",
                            "payload": {
                                "template_type": "generic",
                                "elements": [element]
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
        # No buttons - use plain text message
        message_payload = {
            "text": message
        }
    
    # Add quick replies if provided (Quick Replies work with both text and template messages)
    if quick_replies and isinstance(quick_replies, list) and len(quick_replies) > 0:
        # Filter valid quick replies (must have content_type, title, and payload)
        valid_quick_replies = [
            qr for qr in quick_replies 
            if qr.get("content_type") and qr.get("title") and qr.get("payload")
        ]
        if valid_quick_replies:
            # Limit to 13 quick replies (Instagram's max)
            valid_quick_replies = valid_quick_replies[:13]
            
            # Build quick reply array
            quick_reply_buttons = []
            for qr in valid_quick_replies:
                # Truncate title to 20 characters (Instagram's max)
                qr_title = str(qr["title"])[:20]
                qr_payload = str(qr["payload"])
                qr_content_type = str(qr["content_type"])
                
                quick_reply_buttons.append({
                    "content_type": qr_content_type,
                    "title": qr_title,
                    "payload": qr_payload
                })
            
            if quick_reply_buttons:
                message_payload["quick_replies"] = quick_reply_buttons
                print(f"   Added {len(quick_reply_buttons)} quick reply button(s)")
                for i, qr in enumerate(quick_reply_buttons, 1):
                    print(f"      Quick Reply {i}: {qr['title']} (payload: {qr['payload']})")
    
    payload = {
        "recipient": {"id": recipient_id},
        "message": message_payload
    }
    
    response = requests.post(api_url, json=payload, headers=headers)
    
    if response.status_code != 200:
        error_detail = response.text
        print(f"❌ Failed to send DM: {error_detail}")
        raise Exception(f"Failed to send DM: {error_detail}")
    
    result = response.json()
    print(f"✅ DM sent successfully: {result}")
    return result