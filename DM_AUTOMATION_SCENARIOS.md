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

| What happens | Result |
|--------------|--------|
| `email_skipped` = **true**, `email_received` = **false** | No lead captured. |
| Bot sends **primary / final DM** | Same final DM as in 3.1 (e.g. PDF message, unfollow disclaimer). |
| **Flow for this interaction:** | Complete (final DM sent). **Lead:** not captured. |

**When the same user comments again later:**

Behavior depends on rule config: **`reask_email_on_comment_if_no_lead`** (alias: `reaskEmailOnCommentIfNoLead`).

- **Default (BAU):** `reask_email_on_comment_if_no_lead` = **false** or not set  
  - Bot sends **final DM again** (same as after “Skip for Now”).  
  - Still no email ask.

- **Opt-in:** `reask_email_on_comment_if_no_lead` = **true**  
  - Bot sends **email request again** (Share Email \| Skip for Now \| optional Use My Email).  
  - User can then: [actual email](#31-user-types-actual-email-valid-email-in-message), [Share Email](#32-user-clicks-share-email), or [Skip for Now](#33-user-clicks-skip-for-now) again.

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
| Bot keeps state **waiting for email** | DM trigger: may send one retry/reminder message per invalid input (or friendly reminder if they typed “done” while waiting for email). Comment trigger: resend email question as reminder. |
| **Retry limit:** | **None.** The bot does not give up or send the final DM after N invalid attempts; it keeps waiting for a valid email (or Skip for Now / Share Email). |
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
| 3c | Email | **Skip for Now** | ❌ No | Final DM → flow complete |
| 3d | Email | **Use My Email** (if shown) | ✅ Yes | Final DM → flow complete |
| 3e | Email | Invalid / other text | ❌ No | Wait / reminder; still in email step |
| 4a | Comment again (after Skip) | Any comment (BAU) | — | Final DM again |
| 4b | Comment again (after Skip) | Any comment (`reask_email_on_comment_if_no_lead=true`) | — | Email request again (then 3a–3e) |

---

## 5. Optional config (per rule)

**Canonical keys are snake_case.** The API/frontend may accept camelCase aliases for the same setting.

| Config key (canonical) | Alias | Default | Effect |
|------------------------|-------|--------|--------|
| `require_follow_confirmation` | `requireFollowConfirmation` | `false` | When **true**, “Follow Me” only sends reminder; email step only after “I'm following” or “done”. |
| `reask_email_on_comment_if_no_lead` | `reaskEmailOnCommentIfNoLead` | `false` | When **true**, if user commented again and we still have no lead (e.g. they skipped), bot re-sends email request instead of final DM again. |

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
