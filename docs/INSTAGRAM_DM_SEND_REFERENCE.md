# Instagram DM Send – Code Reference (Missing Media Attachment Debug)

This document points to the exact code that builds the JSON payload and POSTs to the Instagram Graph API for DMs, and how image/video attachments are sent. Use it to debug the “missing media attachment” issue.

---

## 1. API endpoint and main send function

**File:** `app/utils/instagram_api.py`  
**Function:** `send_dm(recipient_id, message, page_access_token, page_id=None, buttons=None, quick_replies=None, media_url=None, media_type=None, card_image_url=None, ...)`

- **Endpoint:** `https://graph.instagram.com/v21.0/{page_id|me}/messages`
- **Line ~184:** `endpoint = f"{page_id}/messages" if page_id else "me/messages"`
- **Line ~185:** `api_url = f"https://graph.instagram.com/v21.0/{endpoint}"`
- **Headers:** `Authorization: Bearer {page_access_token}`

`recipient_id` is the Instagram user ID (IGSID) we send the DM to.

---

## 2. How image/video attachment is sent (separate POST)

**File:** `app/utils/instagram_api.py`  
**Lines:** ~198–235

Media is **not** appended to the text message. It is sent in a **separate** POST to the same `/messages` URL.

1. If `media_url` is set and not localhost:
   - Infer `type`: `"image"` | `"video"` | `"audio"` from URL extension (default `"image"` if none).
   - Build payload:
   ```python
   media_payload = {
       "recipient": {"id": recipient_id},
       "message": {
           "attachment": {
               "type": inferred_type,   # "image" or "video"
               "payload": {"url": media_url_clean}
           }
       }
   }
   ```
   - **POST** `api_url` with `json=media_payload`.
   - On 200: optionally return if no text/buttons/quick_replies.
   - On non-200: log `resp.status_code` and `resp.text` (and first 80 chars of `media_url_clean`).

2. Then, if there is text/buttons/quick_replies, a **second** POST is made with the text/template payload (see section 3).

Relevant code (exact lines):

```python
# Send media attachment first if provided (must be publicly accessible URL - not localhost)
if media_url and str(media_url).strip():
    media_url_clean = str(media_url).strip()
    if "localhost" in media_url_clean or "127.0.0.1" in media_url_clean:
        print(f"⚠️ Skipping media attachment: URL must be publicly accessible ...")
    else:
        inferred_type = media_type or inferred from URL (.mp4/.mov → "video", else "image")
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
        # then check resp.status_code, log on failure
```

---

## 3. Text / buttons / quick_replies payload (second POST when applicable)

**File:** `app/utils/instagram_api.py`  
**Lines:** ~280–446

- If no text and no buttons and no quick_replies after media/card: skip (return).
- Otherwise build `message_payload`:
  - With buttons: generic template `attachment.type: "template"`, `template_type: "generic"`, `elements` with `title`, `subtitle`, `buttons`.
  - Without buttons: `message_payload = {"text": message}`.
- If `quick_replies` provided: add `message_payload["quick_replies"]` (list of `content_type`, `title`, `payload`).
- Final payload:
  ```python
  payload = {
      "recipient": {"id": recipient_id},
      "message": message_payload
  }
  response = requests.post(api_url, json=payload, headers=headers)
  ```

So: **first POST = optional media (attachment.url), second POST = optional text/template/quick_replies.** Image/video is never in the same JSON as the text; it’s a separate request.

---

## 4. Where automation passes media into send_dm (rule config → media_url)

**File:** `app/api/routes/instagram.py`  
**Function:** logic lives inside `execute_automation_action` (async).  
**Relevant block:** ~6026–6177 (extract config, then call `send_dm` with `media_url=...`).

**Step A – Read media from rule config**

- **Lines ~6030–6068:**  
  - `_cfg = rule.config` (dict).  
  - `dm_media_url_val = _cfg.get("dm_media_url") or _cfg.get("dmMediaUrl") or _cfg.get("lead_dm_media_url")` (strip).  
  - `dm_type_val = _cfg.get("dm_type") or _cfg.get("dmType")`; normalized to `"image_video"` if `"image/video"`.  
  - If `dm_type_val == "image_video"` and `dm_media_url_val`: set `media_url_to_send = dm_media_url_val`, `media_type_to_send = None` (infer in send_dm).  
  - Fallback: if `dm_media_url_val` set and not voice/card, set `media_url_to_send = dm_media_url_val`.

**Step B – Comment-based trigger (keyword/post_comment/live_comment)**

- **If** `buttons or quick_replies`:  
  - **Line ~6112:**  
    `send_dm(sender_id, _msg, access_token, page_id_for_dm, buttons, quick_replies, media_url=media_url_to_send, media_type=media_type_to_send, card_...)`  
  - So when automation has buttons/quick_replies, **one** `send_dm` call carries both text and `media_url`; in `send_dm`, media is sent first (section 2), then text (section 3).

- **Else** (no buttons/quick_replies):  
  - **Line ~6128:** `send_private_reply(comment_id, message_template, ...)` — text only (no attachment).  
  - **Lines ~6135–6140:** If `media_url_to_send or card_config`:  
    `send_dm(sender_id, "", access_token, page_id_for_dm, media_url=media_url_to_send, media_type=media_type_to_send, card_...)`  
  - So for comment trigger with no buttons: **first** message = private reply (text), **second** = follow-up DM with only media (and no text in that call). If `media_url_to_send` is None, this follow-up is skipped (only text DM is sent).

**Step C – Non–comment-based (e.g. direct message trigger)**

- **Line ~6175:**  
  `send_dm(sender_id, _msg, access_token, page_id_for_dm, buttons, quick_replies, media_url=media_url_to_send, media_type=media_type_to_send, card_...)`

**Summary for debug:** If automation is comment-triggered and has no buttons/quick_replies, media is sent only in the follow-up `send_dm(..., media_url=media_url_to_send)`. If `media_url_to_send` is None (config missing or wrong key), that call is skipped and you only see the private-reply text. Check logs for `[DM MEDIA]` and `[FOLLOW-UP]` to confirm whether `media_url_to_send` was set and whether the media POST was attempted or failed.

---

## 5. Files and line ranges (quick reference)

| What | File | Lines / function |
|------|------|-------------------|
| Build media payload + POST (attachment) | `app/utils/instagram_api.py` | 198–235 inside `send_dm` |
| Build text/template payload + POST | `app/utils/instagram_api.py` | 280–446 inside `send_dm` |
| send_dm signature and endpoint | `app/utils/instagram_api.py` | 151–195 |
| Rule config → media_url_to_send | `app/api/routes/instagram.py` | 6030–6068 |
| Call send_dm with media (comment, with buttons) | `app/api/routes/instagram.py` | 6112 |
| Call send_dm with media (comment, no buttons – follow-up) | `app/api/routes/instagram.py` | 6135–6139 |
| Call send_dm with media (non-comment) | `app/api/routes/instagram.py` | 6175 |

---

## 6. Exact JSON shape for media POST (for reference)

```json
POST https://graph.instagram.com/v21.0/me/messages
Authorization: Bearer <page_access_token>
Content-Type: application/json

{
  "recipient": { "id": "<IGSID>" },
  "message": {
    "attachment": {
      "type": "image",
      "payload": { "url": "https://public-url-to-image-or-video" }
    }
  }
}
```

`type` is `"image"`, `"video"`, or `"audio"`; Instagram fetches the URL server-side. The URL must be publicly reachable (no localhost).
