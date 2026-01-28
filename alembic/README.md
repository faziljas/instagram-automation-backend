# Alembic migrations

Migrations run **automatically on app startup** (see `app/main.py` → `run_migrations()`). On Render they run on every deploy when you push.

**Revision files:** `001_initial`, `002_legacy_schema`, etc. Each push → Render deploys → startup runs `alembic upgrade head` → any new revision files are applied.

- **Add a new migration:**  
  `alembic revision -m "add_new_column"`  
  Edit the new file in `versions/` and implement `upgrade()` / `downgrade()`. Commit and push; Render will apply it on deploy.

- **Run locally:**  
  `alembic upgrade head`

- **DB URL:** From `DATABASE_URL` in `.env` or environment; `postgres://` → `postgresql://` is handled.
