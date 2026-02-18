Step 1: Remove the trigger temporarily
In Supabase → SQL Editor, run:
DROP TRIGGER IF EXISTS on_auth_user_deleted ON auth.users;

Step 2: Delete the user in the dashboard
Go to Authentication → Users, select sedodab806@flemist.com (UID 319297d1-c4be-4038-b09b-5a6e4a1f0cb6) and delete. It should succeed with the trigger gone.

Step 3: Clean up app data for this user (if needed)
Because the trigger didn’t run, the row in public.users might still be there. Run:
DELETE FROM public.users WHERE supabase_id = '319297d1-c4be-4038-b09b-5a6e4a1f0cb6';
(Use the same UUID, no angle brackets.)

Step 4: Put the trigger back
So future dashboard deletes still clean up public.users, run:
CREATE TRIGGER on_auth_user_deletedAFTER DELETE ON auth.usersFOR EACH ROWEXECUTE FUNCTION public.handle_auth_user_deleted();
(The function public.handle_auth_user_deleted() should still exist; we only dropped the trigger.)
