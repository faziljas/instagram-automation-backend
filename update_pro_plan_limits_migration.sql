-- Migration: Update Pro Plan Limits
-- This migration updates account_limit for Pro users and creates the column if it doesn't exist

-- Step 1: Check if profiles table exists, if not, check users table
-- Note: If profiles table doesn't exist, you may need to create it or use users table instead

-- Option A: If profiles table exists, add/update account_limit column
DO $$
BEGIN
    -- Check if account_limit column exists in profiles table
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'profiles' AND column_name = 'account_limit'
    ) THEN
        -- Add account_limit column if it doesn't exist
        ALTER TABLE profiles ADD COLUMN account_limit INT DEFAULT 1;
        RAISE NOTICE 'Added account_limit column to profiles table';
    ELSE
        RAISE NOTICE 'account_limit column already exists in profiles table';
    END IF;
END $$;

-- Step 2: Update account_limit for Pro users to 3
-- This assumes profiles table has a user_id or id column that references users table
-- Adjust the JOIN condition based on your schema
UPDATE profiles 
SET account_limit = 3
WHERE user_id IN (
    SELECT id FROM users WHERE plan_tier = 'pro'
)
AND account_limit != 3;

-- Alternative: If profiles table doesn't exist and you want to use users table instead
-- Uncomment the following if you need to add account_limit to users table:

-- DO $$
-- BEGIN
--     IF NOT EXISTS (
--         SELECT 1 FROM information_schema.columns 
--         WHERE table_name = 'users' AND column_name = 'account_limit'
--     ) THEN
--         ALTER TABLE users ADD COLUMN account_limit INT DEFAULT 1;
--         RAISE NOTICE 'Added account_limit column to users table';
--     END IF;
-- END $$;
--
-- UPDATE users 
-- SET account_limit = 3
-- WHERE plan_tier = 'pro' AND (account_limit IS NULL OR account_limit != 3);
