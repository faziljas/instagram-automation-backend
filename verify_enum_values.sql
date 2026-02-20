-- SQL query to verify all EventType enum values exist in Supabase
-- Run this in Supabase SQL Editor to check if all required enum values are present

-- Check all current enum values
SELECT 
    unnest(enum_range(NULL::eventtype))::text AS enum_value 
ORDER BY enum_value;

-- Expected enum values (from EventType enum in app/models/analytics_event.py):
-- - comment_replied
-- - dm_sent (CRITICAL - was missing and causing errors)
-- - email_collected
-- - follow_button_clicked
-- - im_following_clicked
-- - link_clicked
-- - phone_collected
-- - profile_visit
-- - trigger_matched

-- If any values are missing, the migration 8b9a4e4c6d39 should add them automatically
-- when the app restarts and runs migrations.
