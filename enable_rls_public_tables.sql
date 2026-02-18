-- Enable Row Level Security (RLS) on all public tables reported by Security Advisor.
-- Run this in Supabase → SQL Editor.
--
-- Your backend uses DATABASE_URL (direct Postgres connection), which bypasses RLS,
-- so the app will keep working. This only locks down direct access via Supabase
-- client (anon/authenticated). No app code changes or deploy needed.

-- Tables from Security Advisor (RLS Disabled in Public)
ALTER TABLE public.subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.instagram_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.instagram_global_trackers ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.automation_rules ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.dm_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.invoices ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.followers ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.instagram_audience ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.captured_leads ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.automation_rule_stats ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.analytics_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.alembic_version ENABLE ROW LEVEL SECURITY;

-- Verify (rowsecurity should be true)
SELECT schemaname, tablename, rowsecurity
FROM pg_tables
WHERE schemaname = 'public'
  AND tablename IN (
    'subscriptions', 'instagram_accounts', 'instagram_global_trackers', 'automation_rules',
    'dm_logs', 'invoices', 'conversations', 'followers', 'instagram_audience',
    'captured_leads', 'automation_rule_stats', 'analytics_events', 'messages', 'users', 'alembic_version'
  )
ORDER BY tablename;
