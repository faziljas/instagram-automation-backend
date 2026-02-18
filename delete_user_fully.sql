-- Safe "delete user" flow: clean public.users first, then delete from Auth in dashboard.
-- Keep the trigger ON so normal deletes still auto-clean public.users.
-- Run this in Supabase → SQL Editor.

-- Optional: one-off function to remove app data for a user by supabase_id.
-- After running it, go to Authentication → Users and delete the auth user.
-- The trigger will fire but find no row in public.users (already deleted); no errors.
CREATE OR REPLACE FUNCTION public.delete_user_app_data(target_supabase_id UUID)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  DELETE FROM public.users WHERE supabase_id = target_supabase_id;
  -- CASCADE on FKs will remove: subscriptions, invoices, instagram_accounts,
  -- analytics_events, messages, conversations, captured_leads, dm_logs, etc.
END;
$$;

-- Usage (replace with the real user UUID from Authentication → Users):
-- SELECT public.delete_user_app_data('319297d1-c4be-4038-b09b-5a6e4a1f0cb6');
-- Then in dashboard: Authentication → Users → delete that user.
