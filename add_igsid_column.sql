-- Migration: Add igsid column to instagram_accounts table
-- Run this SQL script in your PostgreSQL database

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
