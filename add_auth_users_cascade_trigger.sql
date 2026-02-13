-- SQL script to add cascade delete trigger for auth.users → public.users
-- 
-- This trigger automatically deletes from public.users when a user is deleted
-- from Supabase Auth (auth.users table).
--
-- IMPORTANT: Run this directly in Supabase SQL Editor if the Alembic migration
-- doesn't work due to permissions. Supabase may require elevated privileges
-- to create triggers on auth.users.

-- Step 1: Create the trigger function
CREATE OR REPLACE FUNCTION public.handle_auth_user_deleted()
RETURNS TRIGGER AS $$
BEGIN
    -- Delete the corresponding user from public.users table
    -- where supabase_id matches the deleted auth.users id
    DELETE FROM public.users 
    WHERE supabase_id = OLD.id;
    
    RETURN OLD;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Step 2: Create the trigger on auth.users table
DROP TRIGGER IF EXISTS on_auth_user_deleted ON auth.users;

CREATE TRIGGER on_auth_user_deleted
AFTER DELETE ON auth.users
FOR EACH ROW
EXECUTE FUNCTION public.handle_auth_user_deleted();

-- Verify the trigger was created
SELECT 
    trigger_name,
    event_manipulation,
    event_object_table,
    action_statement
FROM information_schema.triggers
WHERE trigger_name = 'on_auth_user_deleted';
