-- Migration: Add igsid column to instagram_accounts table
-- This is REQUIRED for multi-user support and correct account matching
-- Run this SQL in your PostgreSQL database

-- Add the column if it doesn't exist
DO $$ 
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'instagram_accounts' AND column_name = 'igsid'
    ) THEN
        ALTER TABLE instagram_accounts ADD COLUMN igsid VARCHAR(255);
        CREATE INDEX IF NOT EXISTS ix_instagram_accounts_igsid ON instagram_accounts(igsid);
        RAISE NOTICE 'Column igsid added successfully';
    ELSE
        RAISE NOTICE 'Column igsid already exists';
    END IF;
END $$;

-- Update existing accounts: Set IGSID for accounts that don't have it
-- Note: This will only work if you have the IGSID from OAuth
-- For existing accounts added via password, you'll need to re-connect via OAuth
