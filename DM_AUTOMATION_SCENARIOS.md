# DM Automation – All Scenarios

This document describes every user path in the comment-triggered DM automation flow: **Follow step** → **Email step** → **Final DM**, including optional config flags.

---

## 1. Entry: User comments keyword

- User comments a configured **keyword** on a post/reel.
- Bot sends **first DM** (e.g. “Hey! Would you mind following me? …”) with:
  - **Quick replies:** `I'm following` | `Follow Me 👆`
  - (If comment trigger) Optional: **Visit Profile** (URL button) and same quick replies in message.

---

## 2. Follow step: User’s first choice

### 2.1 User clicks **“I'm following”**

| What happens | Result |
|--------------|--------|
| `follow_confirmed` = **true** | Follow step done. |
| Bot sends **email request** | Message + quick replies: **Share Email** \| **Skip for Now** \| (optional) **Use My Email** (account owner’s email). |
| **Next:** User goes to [Email step](#3-email-step) (actual email, Share Email, or Skip for Now). |

---

### 2.2 User clicks **“Follow Me 👆”**

Behavior depends on rule config: **`require_follow_confirmation`** (alias in API/config: `requireFollowConfirmation`).

#### A) Default (BAU): `require_follow_confirmation` = **false** or not set

| What happens | Result |
|--------------|--------|
| `follow_confirmed` = **true** | Treated as “will follow / confirmed”. |
| Bot sends **email request** immediately | Same as “I'm following”: **Share Email** \| **Skip for Now** \| (optional) **Use My Email**. |
| **Next:** User goes to [Email step](#3-email-step). |

#### B) Opt-in: `require_follow_confirmation` = **true**

| What happens | Result |
|--------------|--------|
| `follow_confirmed` = **false** | Not confirmed yet. |
| Bot sends **reminder** | “Great! Once you've followed, click 'I'm following' or type 'done' to continue! 😊” + same quick replies (**I'm following** \| **Follow Me 👆**). |
| **Next:** User must either: click **“I'm following”** or type **“done” / “followed”** (or other confirmation phrase). Then bot sends email request and flow continues to [Email step](#3-email-step). |

---

### 2.3 User types text (e.g. “done”, “followed”, “I'm following”)

| What happens | Result |
|--------------|--------|
| If message matches follow-confirmation phrases | `follow_confirmed` = **true**. |
| Bot sends **email request** | Same quick replies: **Share Email** \| **Skip for Now** \| (optional) **Use My Email**. |
| **Next:** User goes to [Email step](#3-email-step). |

---

### 2.4 User clicks **“Visit Profile”** (if shown)

| What happens | Result |
|--------------|--------|
| `follow_confirmed` = **false** | Still waiting for confirmation. |
| Bot sends **reminder** | “Great! Once you've followed, click 'I'm following' or type 'done' to continue! 😊” (and quick replies again). |
| **Next:** Same as 2.2 B – user must confirm via **“I'm following”** or **“done”** to get to email step. |

---

## 3. Email step: User’s choice after follow is confirmed

After the **email request** message (with **Share Email** \| **Skip for Now** \| optional **Use My Email**), the following cases apply.

---

### 3.1 User types **actual email** (valid email in message)

| What happens | Result |
|--------------|--------|
| Email validated & saved | Stored in **CapturedLead** (and global audience if used). |
| Bot sends **primary / final DM** | Per automation configuration (e.g. “Here’s your PDF …”, unfollow disclaimer, etc.). |
| **Flow:** | **Complete.** Lead captured. Future comments from this user → no automation (handled by human or “primary complete” logic). |

---

### 3.2 User clicks **“Share Email”**

| What happens | Result |
|--------------|--------|
| State set to **waiting for email** | No message sent; bot waits for next DM. |
| **Next:** User must **type their email** in a follow-up message. |
| When they send a valid email | Same as [3.1](#31-user-types-actual-email-valid-email-in-message): lead captured, final DM sent, flow complete. |

---

### 3.3 User clicks **“Skip for Now”**

Behavior depends on rule config **`skip_for_now_no_final_dm`** (alias: `skipForNowNoFinalDm`).

#### v2 (default): `skip_for_now_no_final_dm` = **true**

| What happens | Result |
|--------------|--------|
| `email_skipped` = **true**, `email_received` = **false** | No lead captured. |
| **No Final DM sent** | “No email = no doc to share”; optional short ack: “Comment again anytime when you’d like the guide.” |
| **Flow:** | Not complete; user can re-comment to re-engage (Use Case 1 or 2 below). |

**When the same user comments again later (v2 re-comment):**

- We **always** send **one question**: “Are you following me?” (quick replies: I'm following \| Follow Me). User confirms → then Email Step → user types email → Final DM sent. (We do not skip the follow question on re-comment.)

#### BAU: `skip_for_now_no_final_dm` = **false**

| What happens | Result |
|--------------|--------|
| `email_skipped` = **true** | No lead captured. |
| Bot sends **primary / final DM** | Same as 3.1 (e.g. PDF, unfollow disclaimer). |
| **Re-comment:** | Controlled by **`reask_email_on_comment_if_no_lead`** — if true, bot re-sends email request; if false, bot sends final DM again. |

---

### 3.4 User clicks **“Use My Email”** (if shown)

**What it is:** The **Instagram account owner’s** (platform user’s) email is shown as a one-tap quick reply so the commenter can submit that address—e.g. for testing or when the owner wants the lead/asset tied to that email.

| What happens | Result |
|--------------|--------|
| That email is auto-submitted | Validated and saved as in 3.1. |
| Lead captured, final DM sent | Same as [3.1](#31-user-types-actual-email-valid-email-in-message). |
| **Flow:** | **Complete.** |

---

### 3.5 User sends something that is **not** a valid email (e.g. random text)

| What happens | Result |
|--------------|--------|
| Bot does **not** treat as email | No lead saved, no final DM. |
| Bot sends **invalid-email retry message** | e.g. “That doesn’t look like a valid email address. Please share your email so we can send you the guide!” (config: `email_invalid_retry_message`). |
| **Retry limit:** | **None.** The bot keeps asking for a valid email until the user sends one or uses Skip for Now / Share Email. |
| **Next:** User can type a valid email or use **Share Email** / **Skip for Now** / **Use My Email** when available. |

---

## 4. Scenario matrix (quick reference)

| # | Trigger / Step | User action | Lead captured? | Bot sends next |
|---|----------------|-------------|----------------|----------------|
| 1 | Comment | Keyword comment | — | First DM (follow ask + **I'm following** \| **Follow Me**) |
| 2a | Follow | **I'm following** | — | Email request (Share Email \| Skip for Now \| Use My Email) |
| 2b | Follow | **Follow Me** (BAU) | — | Email request (same as 2a) |
| 2c | Follow | **Follow Me** (`require_follow_confirmation=true`) | — | Reminder; wait for “I'm following” or “done” |
| 2d | Follow | Type “done” / “followed” | — | Email request (same as 2a) |
| 2e | Follow | **Visit Profile** | — | Reminder; wait for confirmation |
| 3a | Email | Type **valid email** | ✅ Yes | Final DM → flow complete |
| 3b | Email | **Share Email** then type email | ✅ Yes | Final DM → flow complete |
| 3c | Email | **Skip for Now** (v2) | ❌ No | No Final DM; ack; re-comment → Use Case 1 or 2 |
| 3c′ | Email | **Skip for Now** (BAU) | ❌ No | Final DM → flow complete |
| 3d | Email | **Use My Email** (if shown) | ✅ Yes | Final DM → flow complete |
| 3e | Email | Invalid / other text | ❌ No | Wait / reminder; still in email step |
| 4a | Comment again (after Skip, v2) | Any | — | “Are you following me?” → confirm → Email Step → Final DM (always ask follow on re-comment) |
| 4b | Comment again (after Skip, BAU) | Any comment | — | Final DM again (or email request if `reask_email_on_comment_if_no_lead=true`) |

---

## 5. Optional config (per rule)

**Canonical keys are snake_case.** The API/frontend may accept camelCase aliases for the same setting.

| Config key (canonical) | Alias | Default | Effect |
|------------------------|-------|--------|--------|
| `skip_for_now_no_final_dm` | `skipForNowNoFinalDm` | **true** (v2) | When **true**, “Skip for Now” does not send Final DM; re-comment triggers Use Case 1 or 2. When **false** (BAU), Skip sends Final DM. |
| `require_follow_confirmation` | `requireFollowConfirmation` | `false` | When **true**, “Follow Me” only sends reminder; email step only after “I'm following” or “done”. |
| `reask_email_on_comment_if_no_lead` | `reaskEmailOnCommentIfNoLead` | `false` | When **true** (and BAU: `skip_for_now_no_final_dm` false), if user commented again with no lead, bot re-sends email request instead of final DM again. |
| `reengagement_follow_message` | `reengagementFollowMessage` | `"Are you following me?"` | Message shown on re-comment (v2; we always ask this before email step). |
| `email_invalid_retry_message` | `emailInvalidRetryMessage` | (see below) | Message sent when user types invalid/non-email text while we’re waiting for email. Default: “That doesn’t look like a valid email address. Please share your email so we can send you the guide!” |

---

## 6. Edge cases

### 6.1 User goes silent after clicking **“Share Email”**

| Question | Answer |
|----------|--------|
| Is there a timeout? | **No.** The bot does not schedule a timeout or auto-advance. |
| Does state reset? | **No.** State remains “waiting for email” (`email_request_sent` true, `email_received` false). |
| What happens next? | The next time the user sends **any** message (DM or comment, depending on trigger), the bot re-evaluates: if it’s a valid email → capture and send final DM; if invalid → wait or send retry/reminder as in [3.5](#35-user-sends-something-that-is-not-a-valid-email-eg-random-text). |

So the conversation can sit in “waiting for email” indefinitely until the user sends another message.

---

### 6.2 Invalid email – retry limit

There is **no fixed retry limit**. The bot does not “give up” after N invalid attempts or send the final DM anyway. It keeps waiting for one of: valid email, **Skip for Now**, or **Share Email** (then valid email). Per invalid input, behavior is as in [3.5](#35-user-sends-something-that-is-not-a-valid-email-eg-random-text) (reminder/retry or resend email question for comment triggers).

---

### 6.3 User comments the keyword **multiple times** before completing the follow step

| Question | Answer |
|----------|--------|
| Duplicate states? | **No.** State is per **(sender_id, rule_id)**. One conversation, one state. |
| What does the second comment do? | The bot sees “follow request sent but not confirmed” and **resends the same follow request** (same message + “type done or followed” + quick replies). So the user gets a reminder, not a second parallel flow. |

---

### 6.4 **“Use My Email”** – whose email?

It is the **platform user who owns the Instagram account** (the creator/brand), not the commenter. That user’s email is offered as a one-tap quick reply so the commenter can submit it—e.g. for testing or when the owner wants the lead/asset associated with that address. When the commenter taps it, that email is captured as the lead’s email and the flow completes as in [3.1](#31-user-types-actual-email-valid-email-in-message).

---

## 7. State / completion rules (short)

- **Follow step done:** `follow_confirmed` = true (via “I'm following”, “Follow Me” in BAU, or typed “done”/“followed”).  
- **Email step done with lead:** `email_received` = true, lead in DB → primary/final DM sent, flow complete.  
- **Email step done without lead:** `email_skipped` = true → final DM sent; on next comment, behavior depends on `reask_email_on_comment_if_no_lead`.  
- **Primary complete (no more automation):** For lead-capture rules, we consider primary complete only when we have a **captured lead** for that sender; otherwise automation can still run (e.g. re-ask email if `reask_email_on_comment_if_no_lead` is on).

---

*Document reflects: comment → follow (I'm following / Follow Me / Visit Profile / text) → email (actual email / Share Email / Skip for Now / Use My Email) → final DM, with optional config for follow confirmation and re-asking email on next comment. Config keys are canonical snake_case with camelCase aliases.*
