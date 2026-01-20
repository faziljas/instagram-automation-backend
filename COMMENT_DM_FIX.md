# Comment to DM Automation Fix

## Problem Summary

When users commented on Instagram posts/reels with trigger keywords, the system was **NOT sending automated DMs** with follow-up and email requests. However, DMs **were working correctly** when users sent direct messages.

## Root Cause

Instagram has a **24-hour messaging window restriction**:
- You can only send DMs to users who have messaged you first OR within 24 hours of their last message
- **Comments on posts/reels DO NOT count as DM initiation**
- When the system tried to send regular DMs after a comment, Instagram's API silently rejected them due to this restriction

## The Solution

Instagram provides a special endpoint: **Private Replies to Comments** (`send_private_reply`)
- Uses `comment_id` instead of `user_id` as the recipient
- **Bypasses the 24-hour messaging window** restriction
- Opens the DM conversation, allowing follow-up messages

## Changes Made

### File: `app/api/routes/instagram.py`

#### 1. Primary DM Messages (Lines ~1789-1850)
**Before:** Always used `send_dm(sender_id, ...)` regardless of trigger type
**After:** 
- Detects if trigger is from a comment (`trigger_type` in ["post_comment", "keyword", "live_comment"])
- Uses `send_private_reply(comment_id, ...)` for the initial message
- Sends follow-up messages with buttons/quick replies as regular DMs (conversation is now open)

#### 2. Pre-DM Messages (Follow/Email Requests) (Lines ~1521-1600)
**Before:** Always used `send_dm` for follow and email request messages
**After:**
- For comment triggers, sends first message via `send_private_reply`
- Sends follow-up with buttons as regular DM
- Sends email request with quick replies as regular DM

## How It Works Now

### Scenario 1: User comments on a reel with keyword "Hello"

1. **Webhook receives comment event** → `process_comment_event()` triggered
2. **System detects** `trigger_type = "keyword"` and `comment_id` is present
3. **Private Reply sent** to bypass 24-hour window:
   ```
   send_private_reply(comment_id, follow_message, access_token)
   ```
4. **Conversation is now open** → Follow-up messages sent as regular DMs:
   - Follow button message
   - Email request with quick replies
   - Primary DM after 15 seconds

### Scenario 2: User sends a direct message with keyword

1. **Webhook receives message event** → `process_instagram_message()` triggered
2. **System detects** `trigger_type = "new_message"`, no `comment_id`
3. **Regular DM flow** (no restriction, already in conversation):
   ```
   send_dm(sender_id, message, access_token, buttons, quick_replies)
   ```

## Testing Steps

### 1. Test Comment-Based Trigger

1. **Setup:**
   - Create an automation for a post/reel
   - Add trigger keyword: "test"
   - Enable "Pre-DM Engagement" toggle
   - Check "Include follow request"
   - Check "Include email request"
   - Add a primary DM message
   - **Important:** Keep "Public Comment Replies" toggle OFF

2. **Test:**
   - From a different Instagram account, comment "test" on the reel
   - **Expected behavior:**
     - You should receive a **private DM** (not a public reply)
     - Message 1: Follow request with "Follow Me" button
     - Message 2: Email request with quick reply buttons
     - After 15 seconds: Primary DM message

3. **Check backend logs** for these messages:
   ```
   💬 Comment-based trigger detected! Using PRIVATE REPLY to bypass 24-hour window
   ✅ Private reply sent to comment {comment_id} from user {sender_id}
   ✅ Follow-up DM with buttons/quick replies sent to {sender_id}
   ```

### 2. Test DM-Based Trigger

1. **Setup:** Same automation as above

2. **Test:**
   - From a different Instagram account, send a DM with "test"
   - **Expected behavior:**
     - Message 1: Follow request with "Follow Me" button
     - Message 2: Email request with quick reply buttons
     - After 15 seconds: Primary DM message

3. **Check backend logs** for:
   ```
   📤 Sending DM via me/messages (no page_id): Recipient={sender_id}
   ✅ DM sent to {sender_id}
   ```

### 3. Test Public Comment Replies (Optional)

1. **Setup:**
   - Same automation
   - **Turn ON** "Public Comment Replies" toggle
   - Add comment reply variations

2. **Test:**
   - Comment "test" on the reel
   - **Expected behavior:**
     - You should see a **public reply** to your comment (visible on the post)
     - **AND** still receive private DMs as before

## Key Code Changes

### Before (Broken):
```python
# Always sent regular DM, which failed for comment triggers
from app.utils.instagram_api import send_dm
send_dm(sender_id, message_template, access_token, page_id_for_dm, buttons, quick_replies)
```

### After (Fixed):
```python
# Detect comment trigger and use private reply
if comment_id and trigger_type in ["post_comment", "keyword", "live_comment"]:
    from app.utils.instagram_api import send_private_reply
    send_private_reply(comment_id, message_template, access_token, page_id_for_dm)
    # Then send buttons/quick replies as follow-up DM
    if buttons or quick_replies:
        await asyncio.sleep(1)
        send_dm(sender_id, follow_up_message, access_token, page_id_for_dm, buttons, quick_replies)
else:
    # Regular DM for non-comment triggers
    send_dm(sender_id, message_template, access_token, page_id_for_dm, buttons, quick_replies)
```

## Monitoring

After deployment, monitor backend logs for:
- ✅ `Private reply sent successfully` - indicates comment triggers are working
- ✅ `Follow-up DM with buttons/quick replies sent` - indicates buttons are working
- ❌ `Failed to send DM: ...` - if you see this for comment triggers, check:
  - Webhook subscriptions (should include `comments` field)
  - Access token has `instagram_business_manage_messages` permission
  - Account is Instagram Business or Creator (not Personal)

## Rollback Plan

If issues occur, revert changes to `app/api/routes/instagram.py`:
```bash
git diff app/api/routes/instagram.py
git checkout app/api/routes/instagram.py  # Revert to previous version
```

## Additional Notes

- The `send_private_reply` function was already defined in `app/utils/instagram_api.py` but was never being called
- This fix does not require any frontend changes
- No database migrations needed
- No changes to webhook subscriptions required (already subscribed to `comments`)

## Related Documentation

- Instagram Graph API - Private Replies: https://developers.facebook.com/docs/messenger-platform/instagram/features/private-replies
- Instagram Messaging Window: https://developers.facebook.com/docs/messenger-platform/policy/policy-overview#standard_messaging
