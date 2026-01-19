# Backend Schema Proposal: Post/Reels Automation Refactor

## Current Structure Analysis

### Database Model (`automation_rules` table)
- `id` (Integer, PK)
- `instagram_account_id` (Integer, FK)
- `name` (String, nullable)
- `trigger_type` (String) - e.g., "post_comment", "keyword", "new_message"
- `action_type` (String) - e.g., "send_dm"
- `config` (JSON) - **This is where all automation logic is stored**
- `media_id` (String, nullable) - Links rule to specific post/reel/story
- `is_active` (Boolean, default=True)
- `created_at` (DateTime)

### Current Config JSON Structure
```json
{
  "keywords": ["keyword1", "keyword2"],
  "auto_reply_to_comments": true,
  "comment_replies": ["Reply 1", "Reply 2", "Reply 3"],
  "message_variations": ["DM 1", "DM 2", "DM 3"],
  "dmType": "text" | "text_button",
  "buttons": [
    { "text": "Click me", "url": "https://..." }
  ],
  "delay_minutes": 5
}
```

---

## Proposed Additive Changes

### ✅ **NO DATABASE MIGRATION REQUIRED**
All changes are **additive** to the existing `config` JSON field. Old automations will continue to work.

---

## 1. Comment Reply Variations (Already Supported, Enhanced)

### Current Implementation
- ✅ `comment_replies` (array) already exists and supports multiple variations
- ✅ Backend randomly selects from array

### Enhancement (Documentation + Validation)
**No code changes needed** - just ensure the UI allows 3-5 variations (currently supports unlimited).

**Config Structure:**
```json
{
  "comment_replies": [
    "Thanks for your comment!",
    "Appreciate your feedback!",
    "Great to hear from you!"
  ]
}
```

**Backward Compatibility:**
- If `comment_replies` is missing → use empty array (no public reply)
- If `comment_replies` is a single string → wrap in array for consistency

---

## 2. Lead Capture Flow (NEW)

### Config Structure
```json
{
  "dmType": "text" | "text_button" | "lead_capture",
  "is_lead_capture": true,  // NEW: Boolean flag
  "lead_capture_flow": [    // NEW: Array of flow steps
    {
      "step": 1,
      "type": "ask",
      "text": "What's your email address?",
      "field_type": "email",  // "email" | "phone" | "text" | "custom"
      "validation": "email"   // "email" | "phone" | "none"
    },
    {
      "step": 2,
      "type": "wait",
      "wait_for": "user_reply"
    },
    {
      "step": 3,
      "type": "save",
      "field": "email",
      "save_to": "lead_data"  // Store in separate table or config
    },
    {
      "step": 4,
      "type": "send",
      "message": "Thanks! Here's your reward: [LINK]",
      "message_variations": ["Thanks! Here's your reward: [LINK]", "Check this out: [LINK]"]
    }
  ],
  "lead_capture_settings": {  // NEW: Optional settings
    "save_to_database": true,
    "notification_email": "admin@example.com",
    "webhook_url": "https://..."  // Optional webhook for lead notifications
  }
}
```

### Backward Compatibility
- If `is_lead_capture` is missing or `false` → Use existing `message_variations` logic
- If `lead_capture_flow` is missing → Fall back to simple DM (`message_variations`)

---

## 3. Stats Tracking (NEW - For Dashboard Widget)

### Config Structure
```json
{
  "stats": {  // NEW: Runtime stats (updated by backend)
    "total_triggers": 0,
    "total_dms_sent": 0,
    "total_comments_replied": 0,
    "total_leads_captured": 0,
    "last_triggered_at": null,
    "last_lead_captured_at": null
  }
}
```

### Implementation Note
- Stats are **runtime data** (not stored in `config` initially)
- Backend will update `config.stats` on each trigger
- OR: Create separate `automation_stats` table (better for analytics)

**Option A: Store in config (simpler)**
```python
# Update stats in config
rule.config["stats"] = {
    "total_triggers": rule.config.get("stats", {}).get("total_triggers", 0) + 1,
    "total_dms_sent": rule.config.get("stats", {}).get("total_dms_sent", 0) + 1,
    "last_triggered_at": datetime.utcnow().isoformat()
}
db.commit()
```

**Option B: Separate table (recommended for production)**
```python
# New table: automation_rule_stats
class AutomationRuleStats(Base):
    __tablename__ = "automation_rule_stats"
    
    id = Column(Integer, primary_key=True)
    automation_rule_id = Column(Integer, ForeignKey("automation_rules.id"))
    total_triggers = Column(Integer, default=0)
    total_dms_sent = Column(Integer, default=0)
    total_comments_replied = Column(Integer, default=0)
    total_leads_captured = Column(Integer, default=0)
    last_triggered_at = Column(DateTime, nullable=True)
    last_lead_captured_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
```

**Recommendation:** Start with **Option A** (config-based) for MVP, migrate to **Option B** later if needed.

---

## 4. Lead Data Storage (NEW)

### Option A: Store in Config (Simple)
```json
{
  "captured_leads": [  // NEW: Array of captured leads
    {
      "email": "user@example.com",
      "phone": "+1234567890",
      "captured_at": "2026-01-19T10:30:00Z",
      "source": "automation_rule_123",
      "metadata": {}
    }
  ]
}
```

**Limitation:** Config JSON can grow large with many leads.

### Option B: Separate Table (Recommended)
```python
# New table: captured_leads
class CapturedLead(Base):
    __tablename__ = "captured_leads"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    instagram_account_id = Column(Integer, ForeignKey("instagram_accounts.id"), nullable=False)
    automation_rule_id = Column(Integer, ForeignKey("automation_rules.id"), nullable=False)
    email = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    name = Column(String, nullable=True)
    custom_fields = Column(JSON, nullable=True)  # For custom field data
    metadata = Column(JSON, nullable=True)  # Additional data (IP, user agent, etc.)
    captured_at = Column(DateTime, default=datetime.utcnow)
    notified = Column(Boolean, default=False)  # Email notification sent?
    exported = Column(Boolean, default=False)  # Exported to CSV/webhook?
```

**Recommendation:** Use **Option B** (separate table) for production.

---

## 5. Updated Config Schema (Complete)

### Full Config Structure (All Features)
```json
{
  // === EXISTING FIELDS (Backward Compatible) ===
  "keywords": ["keyword1", "keyword2"],
  "auto_reply_to_comments": true,
  "comment_replies": ["Reply 1", "Reply 2"],
  "message_variations": ["DM 1", "DM 2"],
  "dmType": "text" | "text_button" | "lead_capture",
  "buttons": [{ "text": "Click", "url": "https://..." }],
  "delay_minutes": 5,
  
  // === NEW FIELDS (Additive) ===
  "is_lead_capture": false,
  "lead_capture_flow": [
    {
      "step": 1,
      "type": "ask",
      "text": "What's your email?",
      "field_type": "email",
      "validation": "email"
    },
    {
      "step": 2,
      "type": "wait",
      "wait_for": "user_reply"
    },
    {
      "step": 3,
      "type": "save",
      "field": "email",
      "save_to": "lead_data"
    },
    {
      "step": 4,
      "type": "send",
      "message": "Thanks! Here's your reward: [LINK]",
      "message_variations": ["Thanks! Here's your reward: [LINK]"]
    }
  ],
  "lead_capture_settings": {
    "save_to_database": true,
    "notification_email": null,
    "webhook_url": null
  },
  
  // === STATS (Runtime, Updated by Backend) ===
  "stats": {
    "total_triggers": 0,
    "total_dms_sent": 0,
    "total_comments_replied": 0,
    "total_leads_captured": 0,
    "last_triggered_at": null,
    "last_lead_captured_at": null
  }
}
```

---

## Migration Strategy

### Phase 1: Additive Changes (No Breaking Changes)
1. ✅ Backend: Add support for `is_lead_capture` and `lead_capture_flow` in config
2. ✅ Backend: Add stats tracking (update `config.stats` on trigger)
3. ✅ Backend: Create `captured_leads` table (optional, but recommended)
4. ✅ Frontend: Build new UI components (drawer, preview, etc.)
5. ✅ Frontend: Support both old modal and new drawer (feature flag)

### Phase 2: Data Migration (Optional)
- Migrate stats from config to `automation_rule_stats` table (if using Option B)
- Migrate leads from config to `captured_leads` table (if using Option A initially)

---

## Database Migration Scripts

### Migration 1: Create `captured_leads` Table
```python
# File: add_captured_leads_migration.py
from sqlalchemy import create_engine, Column, Integer, String, JSON, Boolean, DateTime, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime

Base = declarative_base()

class CapturedLead(Base):
    __tablename__ = "captured_leads"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    instagram_account_id = Column(Integer, ForeignKey("instagram_accounts.id"), nullable=False)
    automation_rule_id = Column(Integer, ForeignKey("automation_rules.id"), nullable=False)
    email = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    name = Column(String, nullable=True)
    custom_fields = Column(JSON, nullable=True)
    metadata = Column(JSON, nullable=True)
    captured_at = Column(DateTime, default=datetime.utcnow)
    notified = Column(Boolean, default=False)
    exported = Column(Boolean, default=False)

# Run migration
def run_migration():
    from app.db.session import engine
    Base.metadata.create_all(engine)
    print("✅ Created 'captured_leads' table")
```

### Migration 2: Create `automation_rule_stats` Table (Optional)
```python
# File: add_automation_rule_stats_migration.py
class AutomationRuleStats(Base):
    __tablename__ = "automation_rule_stats"
    
    id = Column(Integer, primary_key=True)
    automation_rule_id = Column(Integer, ForeignKey("automation_rules.id"), unique=True, nullable=False)
    total_triggers = Column(Integer, default=0)
    total_dms_sent = Column(Integer, default=0)
    total_comments_replied = Column(Integer, default=0)
    total_leads_captured = Column(Integer, default=0)
    last_triggered_at = Column(DateTime, nullable=True)
    last_lead_captured_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
```

---

## Backend Code Changes Required

### 1. Update `execute_automation_action` in `instagram.py`
- Add logic to check `is_lead_capture` flag
- If true, execute `lead_capture_flow` step by step
- Store captured leads in `captured_leads` table
- Update stats in config or `automation_rule_stats` table

### 2. Create Lead Capture Handler
- New function: `process_lead_capture_flow(rule, user_message, db)`
- Handle multi-step conversation flow
- Validate email/phone inputs
- Save to database

### 3. Create Stats Update Function
- New function: `update_automation_stats(rule_id, event_type, db)`
- Increment counters
- Update timestamps

---

## Summary

✅ **NO BREAKING CHANGES**
- All existing automations continue to work
- New fields are optional and additive
- Backward compatibility maintained

✅ **RECOMMENDED ADDITIONS**
1. `captured_leads` table (for lead storage)
2. `automation_rule_stats` table (optional, for better analytics)
3. New config fields: `is_lead_capture`, `lead_capture_flow`, `lead_capture_settings`

✅ **NEXT STEPS**
1. Review and approve this schema proposal
2. Create migration scripts
3. Update backend models and schemas
4. Update TypeScript interfaces
5. Build frontend components
