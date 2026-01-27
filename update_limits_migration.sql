-- Migration: Update DM limits from 50 to 1000 for High Volume pricing strategy
-- Run this in Supabase SQL Editor

-- Step 1: Update existing users' DM limits (if column exists)
-- Note: This assumes you have a dms_limit or monthly_limit column in your users table
-- If your limits are stored elsewhere (e.g., in Supabase auth.users metadata), adjust accordingly

-- Option A: If you have a dms_limit column in users table
-- UPDATE users SET dms_limit = 1000 WHERE dms_limit = 50 OR dms_limit IS NULL;

-- Option B: If you have a monthly_limit column in users table
-- UPDATE users SET monthly_limit = 1000 WHERE monthly_limit = 50 OR monthly_limit IS NULL;

-- Option C: If limits are stored in Supabase auth.users metadata
-- UPDATE auth.users 
-- SET raw_user_meta_data = jsonb_set(
--   COALESCE(raw_user_meta_data, '{}'::jsonb),
--   '{dms_limit}',
--   '1000'::jsonb
-- )
-- WHERE (raw_user_meta_data->>'dms_limit')::int = 50 
--    OR raw_user_meta_data->>'dms_limit' IS NULL;

-- Step 2: Alter table to set default value for new users (if column exists)
-- ALTER TABLE users ALTER COLUMN dms_limit SET DEFAULT 1000;
-- OR
-- ALTER TABLE users ALTER COLUMN monthly_limit SET DEFAULT 1000;

-- Note: Since the codebase uses constants in plan_limits.py rather than database columns,
-- the actual limits are controlled by code. This SQL is provided in case you have
-- additional metadata stored in Supabase that needs updating.

-- If you're using Supabase and storing limits in auth.users metadata, uncomment Option C above.
-- Otherwise, the code changes in plan_limits.py will handle the limit updates.
