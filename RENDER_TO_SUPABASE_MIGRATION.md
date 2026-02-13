# Render → Supabase Database Migration Guide

This guide covers migrating your **PostgreSQL database** from Render to Supabase so that all app data lives in Supabase, **without changing application code**. Auth is already in Supabase; only the backend database (and optionally Redis) need attention.

---

## Quick start – use your Supabase DB

**Your Supabase connection string (Transaction pooler, ap-southeast-2):**

```text
postgresql://postgres.uikdyytqtecmdjsfmzbn:[YOUR-PASSWORD]@aws-1-ap-southeast-2.pooler.supabase.com:6543/postgres
```

**Do this now:**

1. **Set it in `.env`** (do not commit; `.env` is gitignored):
   ```bash
   DATABASE_URL=postgresql://postgres.uikdyytqtecmdjsfmzbn:YOUR_ACTUAL_PASSWORD@aws-1-ap-southeast-2.pooler.supabase.com:6543/postgres
   ```
   Replace `YOUR_ACTUAL_PASSWORD` with your Supabase database password. If the password contains `@`, `#`, or `%`, URL-encode it.

2. **Create schema on Supabase** (from backend repo root):
   ```bash
   cd /path/to/Claude_Code_BE
   alembic upgrade head
   python run_migrations.py
   ```

3. **Export data from Render** → **Import into Supabase** (see sections 4.2 and 4.3 below), then switch the running app’s `DATABASE_URL` to this Supabase URI and redeploy.

---

## 1. Current vs target setup

| Component | Current (Render) | After migration |
|-----------|------------------|------------------|
| **Auth** | Supabase (unchanged) | Supabase |
| **PostgreSQL** | Render PostgreSQL (`DATABASE_URL`) | **Supabase PostgreSQL** |
| **Redis** | Render Redis (`REDIS_URL`) for Celery | **Unchanged** – keep Render Redis, or use e.g. Upstash (Supabase does not provide Redis) |

Your backend uses:

- **One env var for DB:** `DATABASE_URL` (used in `app/db/session.py`, Alembic, `run_migrations.py`, and all routes).
- **Supabase only for:** Auth (JWT verification, sync-user, delete user). User rows are stored in **your** PostgreSQL (today: Render; after migration: Supabase).

So the only **database** migration is: **Render PostgreSQL → Supabase PostgreSQL**. Redis stays as-is (or you move it elsewhere; see section 6).

---

## 2. What you need to have ready

### From Supabase (same project you use for Auth)

1. **Database connection string**
   - Supabase Dashboard → **Project Settings** → **Database**.
   - Under **Connection string** choose:
     - **URI** (recommended for backend).
     - Prefer **Transaction** pooler (port **6543**) for serverless/pooled apps; use **Session** (port **5432**) if you prefer a direct connection.
   - Copy the URI. It looks like:
     - Pooler: `postgresql://postgres.[PROJECT-REF]:[YOUR-PASSWORD]@aws-0-[REGION].pooler.supabase.com:6543/postgres`
     - Direct: `postgresql://postgres:[YOUR-PASSWORD]@db.[PROJECT-REF].supabase.co:5432/postgres`
   - Replace `[YOUR-PASSWORD]` with the **database password** (from the same Database settings page; you can reset it if needed).
   - **Important:** Use `postgresql://` (not `postgres://`). If your env ever uses `postgres://`, the app normalizes it in Alembic; adding the same normalization in `session.py` is recommended (see below).

2. **Already have (for Auth)**  
   No change needed; keep using:
   - `SUPABASE_URL`
   - `SUPABASE_JWT_SECRET`
   - `SUPABASE_SERVICE_ROLE_KEY` (for delete-user in backend)

### From Render (for export)

1. **Render PostgreSQL internal URL**
   - Render Dashboard → your **PostgreSQL** service → **Info** → **Internal Database URL** (or **External** if you run migration from your laptop).
   - Use this as source for `pg_dump` (see below).

2. **Render Redis**
   - No migration needed for Supabase (Supabase has no Redis). Keep `REDIS_URL` pointing to Render Redis, or later switch to e.g. Upstash and update `REDIS_URL`.

---

## 3. Tables to migrate (backend schema)

Your app uses these **models** (all in `app/models/`); they must exist in Supabase PostgreSQL with the same structure:

| Table (model) | Purpose |
|---------------|---------|
| `users` | User accounts (synced with Supabase Auth via `supabase_id`) |
| `subscriptions` | Plan / Dodo subscription state |
| `instagram_accounts` | Linked IG accounts |
| `automation_rules` | Automation rules |
| `automation_rule_stats` | Rule statistics |
| `dm_logs` | DM logs |
| `followers` | Follower data |
| `captured_leads` | Lead capture |
| `analytics_events` | Analytics |
| `messages` | Messages |
| `conversations` | Conversations |
| `instagram_audience` | Audience data |
| `instagram_global_tracker` | Global tracker |
| `invoices` | Dodo invoices |

Plus any tables created by ad‑hoc migrations in `run_migrations.py` (e.g. profile picture, billing cycle, etc.). The schema is defined by your **Alembic migrations** + **run_migrations.py** scripts; the data is in Render PostgreSQL.

---

## 4. Step-by-step migration (minimal downtime)

### 4.1 Prepare Supabase database (schema only, no code change)

1. **Set Supabase as target (temporarily)**  
   In `.env` (or env where you run migrations), set:
   ```bash
   DATABASE_URL=<Supabase connection URI from step 2>
   ```
   Do **not** point the running app to Supabase yet.

2. **Create schema in Supabase**
   - From backend repo root:
     ```bash
     cd /path/to/Claude_Code_BE
     # Ensure .env has Supabase DATABASE_URL
     alembic upgrade head
     python run_migrations.py
     ```
   - This creates all tables and columns in Supabase. No app traffic uses Supabase yet.

### 4.2 Export data from Render PostgreSQL

From a machine that can reach Render DB (e.g. your laptop with Render external URL, or a Render shell):

```bash
# Set source to Render PostgreSQL (internal or external URL)
export RENDER_DB_URL="postgresql://..."   # Your Render PostgreSQL URL

pg_dump "$RENDER_DB_URL" \
  --no-owner \
  --no-acl \
  --clean \
  --if-exists \
  --format=custom \
  -f render_backup.dump
```

Or schema + data as SQL (alternative):

```bash
pg_dump "$RENDER_DB_URL" --no-owner --no-acl -f render_backup.sql
```

### 4.3 Import data into Supabase PostgreSQL

Using the custom format dump:

```bash
export SUPABASE_DB_URL="postgresql://postgres.[ref]:[password]@...pooler.supabase.com:6543/postgres"

pg_restore \
  --no-owner \
  --no-acl \
  --clean \
  --if-exists \
  -d "$SUPABASE_DB_URL" \
  render_backup.dump
```

If you used SQL dump:

```bash
psql "$SUPABASE_DB_URL" -f render_backup.sql
```

- Resolve any errors (e.g. existing tables from `alembic upgrade head`). Using `--clean --if-exists` with pg_restore will drop objects before restore; run against a dedicated target DB or ensure you’re okay with that.
- **Recommended:** Run schema creation (4.1) first, then use `pg_restore` **without** `--clean` and use `--data-only` so you only load data and don’t drop tables. Example:

  ```bash
  pg_restore --no-owner --no-acl --data-only -d "$SUPABASE_DB_URL" render_backup.dump
  ```

  If you already ran `alembic upgrade head` + `run_migrations.py`, tables exist; data-only restore is usually best.

### 4.4 Switch app to Supabase

1. **Backend**
   - Set `DATABASE_URL` to the **Supabase** connection string everywhere the app runs (e.g. Render web service env vars, or local `.env`).
   - Redeploy or restart so the app uses the new DB.
   - No code change required; only `DATABASE_URL` changes.

2. **Frontend**  
   No change. It already uses Supabase for Auth and talks to your backend API.

3. **Verify**
   - Log in (Supabase Auth + backend sync-user).
   - Check that existing users, subscriptions, Instagram accounts, and key flows (e.g. automation, leads) read/write correctly against Supabase.

### 4.5 Optional: `postgres://` normalization in backend

If `DATABASE_URL` might ever be set with `postgres://`, normalize it so SQLAlchemy never sees `postgres://`:

In `app/db/session.py`, set the URL before creating the engine:

```python
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@localhost/dbname")
if SQLALCHEMY_DATABASE_URL.startswith("postgres://"):
    SQLALCHEMY_DATABASE_URL = "postgresql://" + SQLALCHEMY_DATABASE_URL[10:]

# ... rest unchanged
```

This keeps behavior consistent with Alembic and avoids connection errors.

---

## 5. Environment variables checklist

| Variable | Where | After migration |
|----------|--------|-------------------|
| `DATABASE_URL` | Backend (Render env / .env) | **Supabase PostgreSQL URI** (pooler or direct) |
| `SUPABASE_URL` | Backend | No change |
| `SUPABASE_JWT_SECRET` | Backend | No change |
| `SUPABASE_SERVICE_ROLE_KEY` | Backend | No change (already used for delete user) |
| `REDIS_URL` | Backend (Celery) | No change (keep Render Redis or move to Upstash later) |
| Frontend Supabase vars | Frontend | No change |

Nothing else in the codebase is tied to “Render” for the database; only `DATABASE_URL` matters.

---

## 6. Redis (Celery)

- **Supabase does not provide Redis.** Your Celery broker/backend use `REDIS_URL`.
- **Options:**
  - **Keep Render Redis:** Leave `REDIS_URL` as-is; only the DB moves to Supabase.
  - **Move later:** e.g. Upstash Redis; then set `REDIS_URL` to Upstash and redeploy. No DB migration needed for that.

---

## 7. Summary: what you need from yourself / dashboards

- **From Supabase:**  
  - Database connection URI (password set) for the **same project** you use for Auth.  
  - No new Supabase project required unless you want a separate DB project.

- **From Render:**  
  - PostgreSQL connection URL (for `pg_dump`).  
  - (Optional) Decision on keeping or replacing Redis.

- **Details to have:**  
  - Supabase DB password (for the connection string).  
  - Confirmation that Supabase Auth (and `SUPABASE_URL`, `SUPABASE_JWT_SECRET`, `SUPABASE_SERVICE_ROLE_KEY`) stay as they are.

- **Code:**  
  - No change required; only `DATABASE_URL` is updated to the Supabase PostgreSQL URI. Optionally add `postgres://` → `postgresql://` in `app/db/session.py` for robustness.

After this, your “complete database” (all app tables) runs on Supabase PostgreSQL while Auth remains in Supabase and user info continues to be stored in your own tables in that same Supabase DB.
