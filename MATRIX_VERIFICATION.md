# Matrix verification: "User comments again" behavior

All matrices are implemented. Reference below.

---

## 1. VIP definition

- **VIP** = `has_email AND has_phone AND is_following` (all three).
- **Location:** `app/services/global_conversion_check.py` → `is_converted = has_email and has_phone and is_following`.

---

## 2. Flow-type aware behavior (instagram.py)

Before calling `process_pre_dm_actions`, we set `skip_growth_steps = is_vip_user`, then:

- **Phone rule:** only keep skip if we have **phone** for this account+sender (CapturedLead). Else set `skip_growth_steps = False` → ask for phone.
- **Follower rule:** only keep skip if **follow_confirmed** for this rule+sender. Else set `skip_growth_steps = False` → ask for follow.

So: we send **primary DM** only when we already have what **this rule** needs; otherwise we ask (email / phone / follow).

---

## 3. "Already have email/phone" (pre_dm_handler.py)

- **Email flow** (`simple_dm_flow`): if any CapturedLead for this **account+sender** has email → return `send_primary`. No rule_id filter → works across rules.
- **Phone flow** (`simple_dm_flow_phone`): if any CapturedLead for this **account+sender** has phone → return `send_primary`. No rule_id filter → works across rules.
- **Follower flow:** primary DM when `flow_completed` in general flow, where `follow_completed = state.get("follow_confirmed")` and `email_completed = True` (follower-only has `ask_for_email=False`). So when **follow_confirmed** for this rule → return `send_primary`.

---

## 4. Matrix checklist

| They have              | Rule type | Expected              | Where it's enforced |
|------------------------|-----------|------------------------|---------------------|
| Email only             | Email     | Primary DM             | pre_dm_handler: already have email → send_primary |
| Email only             | Phone     | Phone question         | instagram: phone rule + no phone → skip_growth_steps=False → phone flow |
| Email only             | Followers | Followers question     | instagram: follower rule + no follow_confirmed → skip_growth_steps=False → follow flow |
| Phone only             | Email     | Email question         | pre_dm_handler: no email → email flow → email question |
| Phone only             | Phone     | Primary DM             | pre_dm_handler: already have phone → send_primary |
| Phone only             | Followers | Followers question     | instagram: no follow_confirmed → follow flow |
| Followers only         | Email     | Email question         | pre_dm_handler: no email → email flow |
| Followers only         | Phone     | Phone question         | instagram: no phone → skip_growth_steps=False → phone flow |
| Followers only         | Followers | Primary DM             | pre_dm_handler: flow_completed when follow_confirmed → send_primary |
| Email + phone only     | Email     | Primary DM             | pre_dm_handler: already have email |
| Email + phone only     | Phone     | Primary DM             | pre_dm_handler: already have phone |
| Email + phone only     | Followers | Followers question     | pre_dm_handler: no follow_confirmed for this rule → follow flow |
| Followers + phone only | Email     | Email question         | pre_dm_handler: no email → email flow |
| Followers + phone only | Phone     | Primary DM             | pre_dm_handler: already have phone |
| Followers + phone only | Followers | Primary DM             | pre_dm_handler: follow_confirmed → flow_completed → send_primary |
| Followers + phone + email (VIP) | All | Primary DM    | instagram: skip_growth_steps=True → process_pre_dm_actions returns send_primary immediately |

---

## 5. Summary

- **VIP** and **flow-type** logic in `instagram.py` ensure we only “skip growth” when we have what the **current rule** needs (email / phone / follow).
- **pre_dm_handler** returns `send_primary` when we already have email (email rule), already have phone (phone rule), or flow is completed (follower rule with `follow_confirmed`).
- **Email/phone** “already have” checks use **account + sender** (no rule_id), so one captured email/phone applies to all rules on that account for that user.

All matrix rows are covered by the current implementation.
