#!/usr/bin/env python3
"""
Stress-test data seeder for dashboard pagination, lists, and speed testing.

Connects to the Render PostgreSQL database (via DATABASE_URL), prompts for a
target user_id (or use --email / --supabase-id), then seeds:
  - 5 connected Instagram accounts (@load_test_1 .. @load_test_5)
  - 5,000 analytics events (1,000 per account) as "media post"–like data
  - 1,000 DmLog rows (200 per account) as automation logs

Uses bulk inserts for performance. Run from project root:
  python scripts/seed_stress_test.py
  python scripts/seed_stress_test.py --email your@email.com
  python scripts/seed_stress_test.py --supabase-id 5cff2718-2d6a-42ba-aab8-ce494aad3074

Requires: DATABASE_URL in environment (.env or export).

For stress-testing the production UI (logicdm.app): use Render's Postgres URL,
e.g. run:
  DATABASE_URL='postgresql://...' python scripts/seed_stress_test.py --email you@example.com
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Run from project root; ensure app is importable
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv

load_dotenv(project_root / ".env")
load_dotenv(project_root / ".env.local")

# Optional: normalize postgres:// -> postgresql:// for SQLAlchemy
_database_url = os.getenv("DATABASE_URL")
if _database_url and _database_url.startswith("postgres://"):
    os.environ["DATABASE_URL"] = "postgresql://" + _database_url[10:]

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.models.analytics_event import AnalyticsEvent, EventType
from app.models.dm_log import DmLog
from app.models.instagram_account import InstagramAccount

BATCH_SIZE = 1000
MEDIA_TYPES = ("IMAGE", "VIDEO", "CAROUSEL")
PLACEHOLDER_CREDENTIALS = "load_test_placeholder"


def get_session():
    url = os.getenv("DATABASE_URL")
    if not url:
        print("ERROR: DATABASE_URL not set. Add it to .env or export it.")
        sys.exit(1)
    engine = create_engine(url)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return Session()


def ensure_user_exists(sess, user_id: int) -> None:
    row = sess.execute(text("SELECT 1 FROM users WHERE id = :id"), {"id": user_id}).fetchone()
    if not row:
        print(f"ERROR: No user with id={user_id}. Use an existing users.id.")
        sys.exit(1)


def resolve_user_id(sess, email: str | None, supabase_id: str | None) -> int:
    """Look up backend users.id by email or supabase_id."""
    if email:
        row = sess.execute(
            text("SELECT id, email FROM users WHERE email = :email"),
            {"email": email.strip()},
        ).fetchone()
        if not row:
            print(f"ERROR: No user with email {email!r} in backend users table.")
            sys.exit(1)
        print(f"Using user id={row[0]} (email={row[1]})")
        return int(row[0])
    if supabase_id:
        row = sess.execute(
            text("SELECT id, email FROM users WHERE supabase_id = :sid"),
            {"sid": supabase_id.strip()},
        ).fetchone()
        if not row:
            print(f"ERROR: No user with supabase_id {supabase_id!r} in backend users table.")
            sys.exit(1)
        print(f"Using user id={row[0]} (email={row[1]})")
        return int(row[0])
    raise ValueError("need --email or --supabase-id")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed stress-test data (5 accounts, 5k events, 1k DmLogs) for a user."
    )
    parser.add_argument("--email", type=str, help="User email (from backend users table)")
    parser.add_argument("--supabase-id", type=str, dest="supabase_id", help="Supabase Auth UID")
    args = parser.parse_args()

    print("Stress-test seeder — bulk data for pagination/lists/speed testing\n")

    sess = get_session()
    try:
        if args.email or args.supabase_id:
            user_id = resolve_user_id(sess, args.email, args.supabase_id)
        else:
            user_id_in = input("Enter target user_id (integer, from users.id): ").strip()
            if not user_id_in:
                print("Aborted.")
                sys.exit(0)
            try:
                user_id = int(user_id_in)
            except ValueError:
                print("Invalid user_id. Must be an integer.")
                sys.exit(1)
        ensure_user_exists(sess, user_id)
    finally:
        sess.close()

    sess = get_session()
    try:
        ensure_user_exists(sess, user_id)
        # 1. Create 5 load-test Instagram accounts
        print("Creating 5 connected accounts (@load_test_1 .. @load_test_5)...")
        accounts = []
        for i in range(1, 6):
            acc = InstagramAccount(
                user_id=user_id,
                username=f"load_test_{i}",
                encrypted_credentials=PLACEHOLDER_CREDENTIALS,
                is_active=True,
            )
            sess.add(acc)
            accounts.append(acc)
        sess.flush()
        account_ids = [a.id for a in accounts]
        account_usernames = {a.id: a.username for a in accounts}
        print(f"  Created account ids: {account_ids}")

        # 2. Bulk-insert 5,000 "media post"–like analytics events (1,000 per account)
        print("Bulk-inserting 5,000 analytics events (1,000 per account)...")
        events = []
        base_ts = datetime.now(timezone.utc)
        for aid in account_ids:
            for i in range(1000):
                media_type = random.choice(MEDIA_TYPES)
                ts = base_ts - timedelta(days=random.uniform(0, 60))
                events.append({
                    "user_id": user_id,
                    "rule_id": None,
                    "instagram_account_id": aid,
                    "media_id": f"load_test_media_{aid}_{i}",
                    "media_preview_url": None,
                    "event_type": EventType.TRIGGER_MATCHED,
                    "event_metadata": {"media_type": media_type},
                    "created_at": ts,
                })
        for start in range(0, len(events), BATCH_SIZE):
            batch = events[start : start + BATCH_SIZE]
            sess.bulk_insert_mappings(AnalyticsEvent, batch)
            sess.flush()
        print(f"  Inserted {len(events)} analytics events.")

        # 3. Bulk-insert 1,000 DmLogs (200 per account) — automation logs
        # Note: dm_logs has no status column; all rows represent sent activity.
        print("Bulk-inserting 1,000 DmLogs (200 per account)...")
        dm_logs = []
        for aid in account_ids:
            uname = account_usernames[aid]
            for i in range(200):
                ts = base_ts - timedelta(days=random.uniform(0, 60))
                dm_logs.append({
                    "user_id": user_id,
                    "instagram_account_id": aid,
                    "instagram_username": uname,
                    "instagram_igsid": None,
                    "recipient_username": f"load_test_recipient_{aid}_{i}",
                    "message": f"Load test DM {i} for account {aid}.",
                    "sent_at": ts,
                })
        for start in range(0, len(dm_logs), BATCH_SIZE):
            batch = dm_logs[start : start + BATCH_SIZE]
            sess.bulk_insert_mappings(DmLog, batch)
            sess.flush()
        print(f"  Inserted {len(dm_logs)} DmLogs.")

        sess.commit()
        print("\nDone. Stress-test data seeded successfully.")
    except Exception as e:
        sess.rollback()
        print(f"ERROR: {e}")
        raise
    finally:
        sess.close()
        # Always print cleanup SQL (run in order; FK constraints)
        print("\n--- Cleanup (run these in order to remove only this test data) ---\n")
        print("-- 1. DmLogs for load-test accounts")
        print("DELETE FROM dm_logs WHERE instagram_account_id IN (SELECT id FROM instagram_accounts WHERE username LIKE 'load_test_%');")
        print()
        print("-- 2. Analytics events for load-test accounts")
        print("DELETE FROM analytics_events WHERE instagram_account_id IN (SELECT id FROM instagram_accounts WHERE username LIKE 'load_test_%');")
        print()
        print("-- 3. Load-test Instagram accounts")
        print("DELETE FROM instagram_accounts WHERE username LIKE 'load_test_%';")
        print()


if __name__ == "__main__":
    main()
