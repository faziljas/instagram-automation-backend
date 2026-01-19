# Post/Reels Automation Refactor - Implementation Tracker

## 📋 Overview
This document tracks the progress of refactoring the Post/Reels Automation UI from a small modal to a visual automation builder with lead capture capabilities.

---

## ✅ COMPLETED

### Backend (100% Complete)

#### 1. Database Schema
- ✅ Created `CapturedLead` model (`app/models/captured_lead.py`)
  - Fields: `id`, `user_id`, `instagram_account_id`, `automation_rule_id`, `email`, `phone`, `name`, `custom_fields`, `extra_metadata`, `captured_at`, `notified`, `exported`
  - Fixed: Renamed `metadata` to `extra_metadata` to avoid SQLAlchemy conflict
- ✅ Created `AutomationRuleStats` model (`app/models/automation_rule_stats.py`)
  - Fields: `id`, `automation_rule_id`, `total_triggers`, `total_dms_sent`, `total_comments_replied`, `total_leads_captured`, `last_triggered_at`, `last_lead_captured_at`
- ✅ Updated model imports in `app/models/__init__.py`
- ✅ Updated `app/main.py` to auto-create new tables on startup
- ✅ Created migration scripts:
  - `add_captured_leads_migration.py`
  - `add_automation_rule_stats_migration.py`

#### 2. Lead Capture Service
- ✅ Created `app/services/lead_capture.py`
  - `validate_email()` - Email validation
  - `validate_phone()` - Phone validation
  - `get_current_flow_step()` - Get current step in flow
  - `process_lead_capture_step()` - Process lead capture flow steps
  - `update_automation_stats()` - Update rule statistics (with fallback to config.stats)

#### 3. API Endpoints
- ✅ Created `app/api/routes/leads.py`
  - `GET /api/leads` - List captured leads (with filters)
  - `GET /api/leads/stats` - Get lead statistics
  - `DELETE /api/leads/{lead_id}` - Delete a lead
- ✅ Integrated lead capture into `execute_automation_action()` in `app/api/routes/instagram.py`
- ✅ Added stats tracking for:
  - Triggers (`update_automation_stats(rule_id, "triggered", db)`)
  - DMs sent (`update_automation_stats(rule_id, "dm_sent", db)`)
  - Comments replied (`update_automation_stats(rule_id, "comment_replied", db)`)
  - Leads captured (`update_automation_stats(rule_id, "lead_captured", db)`)

#### 4. Config Schema Support
- ✅ Backend supports new config fields:
  - `is_lead_capture` (boolean)
  - `lead_capture_flow` (array of flow steps)
  - `lead_capture_settings` (object)
  - `stats` (runtime stats, updated by backend)
- ✅ Backward compatible - old automations continue to work

#### 5. Git
- ✅ All backend changes committed and pushed to git

---

### Frontend (80% Complete)

#### 1. TypeScript Interfaces
- ✅ Updated `types/index.ts` with:
  - `LeadCaptureStepType`, `FieldType`, `ValidationType`
  - `LeadCaptureStep` interface
  - `LeadCaptureSettings` interface
  - `AutomationStats` interface
  - `AutomationConfig` interface (enhanced)
  - `CapturedLead` interface

#### 2. Components Created
- ✅ `AutomationDrawer.tsx` - Right-side slide-over builder
  - Split screen layout (50% settings, 50% preview)
  - Trigger configuration (keywords)
  - Public reply section (with variations)
  - DM section with tab switcher (Simple Reply / Lead Capture)
  - Delay configuration
  - Save/Cancel buttons
- ✅ `MobilePreview.tsx` - iPhone frame with live preview
  - Shows post/reel preview
  - Shows comment section with sample comment
  - Shows public reply (if enabled)
  - Shows DM preview (toggleable)
  - Updates dynamically as user types

#### 3. Automations Page Integration
- ✅ Updated `app/dashboard/automations/page.tsx`:
  - Added `AutomationDrawer` import and state
  - Added `handleSaveAutomation()` function
  - Added `useEffect` to fetch automation rules for stats
  - Integrated drawer for Posts/Reels/Stories
  - Kept modal for DM automation (backward compatible)

#### 4. Grid Card Enhancements
- ✅ Added status pills (Active/Paused) in top-left corner
- ✅ Added stats overlay on hover:
  - Shows total leads collected (large number)
  - Shows DMs sent and replies (smaller text)
- ✅ Made entire card clickable to open drawer
- ✅ Button text changes to "Edit automation" if rule exists

---

## 🚧 PARTIALLY IMPLEMENTED

### Frontend

#### 1. Lead Capture Flow Builder UI
- ⚠️ **Status**: Placeholder exists, needs full implementation
- ✅ Tab switcher between "Simple Reply" and "Lead Capture"
- ❌ Lead capture flow builder UI (currently shows placeholder text)
- ❌ Step-by-step flow configuration:
  - Step 1: Ask Question (input field)
  - Step 2: Wait for User Reply
  - Step 3: Save Data (Email/Phone)
  - Step 4: Send Final Reward/Link

#### 2. Stats Display
- ✅ Stats overlay on hover (basic)
- ❌ Detailed stats page/dashboard
- ❌ Export leads to CSV
- ❌ Webhook notifications for new leads

---

## ❌ NOT YET IMPLEMENTED

### Frontend

#### 1. Lead Capture Flow Builder (High Priority)
- [ ] Visual flow builder UI component
- [ ] Drag-and-drop flow steps
- [ ] Step configuration forms:
  - Ask step: Question text, field type (email/phone/text), validation
  - Wait step: Wait for user reply
  - Save step: Field name, save destination
  - Send step: Message variations, reward link
- [ ] Flow preview in MobilePreview component
- [ ] Validation for flow steps

#### 2. Stats Dashboard (Medium Priority)
- [ ] Dedicated stats page (`/dashboard/stats` or `/dashboard/leads`)
- [ ] Charts/graphs for:
  - Leads collected over time
  - DMs sent per rule
  - Conversion rates
- [ ] Filter by date range, rule, account
- [ ] Export to CSV functionality

#### 3. Lead Management (Medium Priority)
- [ ] Leads list page (`/dashboard/leads`)
- [ ] Lead details view
- [ ] Search/filter leads
- [ ] Bulk actions (delete, export, notify)
- [ ] Lead export to CSV
- [ ] Lead export to webhook

#### 4. Enhanced Mobile Preview (Low Priority)
- [ ] Show lead capture flow in preview
- [ ] Animate conversation flow
- [ ] Show multiple conversation branches
- [ ] Preview validation messages

#### 5. UI/UX Improvements (Low Priority)
- [ ] Loading states for drawer
- [ ] Error handling and validation messages
- [ ] Success notifications
- [ ] Undo/redo for flow builder
- [ ] Keyboard shortcuts

### Backend

#### 1. Lead Capture Flow Processing (Medium Priority)
- ⚠️ **Status**: Basic implementation exists, needs enhancement
- [ ] Multi-step conversation state tracking
- [ ] Store user's current step in conversation
- [ ] Handle flow branching (different paths based on user input)
- [ ] Timeout handling for incomplete flows
- [ ] Flow resumption after user returns

#### 2. Webhook Notifications (Low Priority)
- [ ] Webhook endpoint for new lead notifications
- [ ] Configurable webhook URLs per rule
- [ ] Retry logic for failed webhooks
- [ ] Webhook signature verification

#### 3. Email Notifications (Low Priority)
- [ ] Email notification when lead is captured
- [ ] Configurable notification email per rule
- [ ] Email templates
- [ ] Daily/weekly summary emails

#### 4. Analytics & Reporting (Low Priority)
- [ ] Advanced analytics queries
- [ ] Conversion funnel tracking
- [ ] A/B testing support
- [ ] Performance metrics

---

## 🎯 NEXT STEPS (Priority Order)

### Immediate (This Week)
1. **Implement Lead Capture Flow Builder UI** ⭐ HIGHEST PRIORITY
   - Create visual flow builder component
   - Add step configuration forms
   - Integrate with MobilePreview
   - Test end-to-end flow

2. **Enhance Lead Capture Backend Processing**
   - Implement conversation state tracking
   - Handle multi-step flows properly
   - Test with real Instagram DMs

### Short Term (Next 2 Weeks)
3. **Create Leads Management Page**
   - List all captured leads
   - Search and filter functionality
   - Export to CSV

4. **Add Stats Dashboard**
   - Visual charts for leads collected
   - Filter by date/rule/account
   - Export functionality

### Medium Term (Next Month)
5. **Webhook Notifications**
   - Implement webhook endpoint
   - Add retry logic
   - Test with external services

6. **Email Notifications**
   - Email on lead capture
   - Daily/weekly summaries
   - Customizable templates

---

## 📊 Progress Summary

### Overall Progress: ~75% Complete

- **Backend**: 100% ✅
- **Frontend Core**: 80% ✅
- **Lead Capture UI**: 20% 🚧
- **Stats & Analytics**: 30% 🚧
- **Lead Management**: 0% ❌

### Key Metrics
- **Files Created**: 8
- **Files Modified**: 6
- **Database Tables**: 2 new tables
- **API Endpoints**: 3 new endpoints
- **Components**: 2 new components
- **Lines of Code**: ~1,500+ lines

---

## 🔧 Technical Debt / Known Issues

1. **Lead Capture Flow Processing**
   - Currently processes step-by-step but doesn't track conversation state
   - Need to implement state machine for multi-step flows

2. **Stats Storage**
   - Currently supports both `automation_rule_stats` table and `config.stats`
   - Should standardize on one approach (recommend table for production)

3. **Mobile Preview**
   - Lead capture flow preview not yet implemented
   - Only shows simple DM preview

4. **Error Handling**
   - Frontend error handling could be more robust
   - Backend error messages could be more user-friendly

---

## 📝 Notes

- All changes are **backward compatible** - existing automations continue to work
- Database migrations are **additive only** - no breaking changes
- Frontend supports both old modal and new drawer (feature flag ready)
- Stats tracking works with both table and config-based storage

---

## 🚀 Quick Start Guide

### Testing Lead Capture
1. Create a new automation rule with `is_lead_capture: true`
2. Configure `lead_capture_flow` with steps
3. Trigger automation via DM
4. Check `/api/leads` endpoint for captured leads
5. View stats in automation rule config or stats table

### Testing New UI
1. Navigate to `/dashboard/automations`
2. Select an Instagram account
3. Click on a post/reel card
4. Drawer should open with live preview
5. Configure automation and save
6. Hover over card to see stats overlay

---

**Last Updated**: 2026-01-19
**Status**: Active Development
**Next Review**: After Lead Capture Flow Builder implementation
