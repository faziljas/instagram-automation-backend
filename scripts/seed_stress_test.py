#!/usr/bin/env python3
"""
Stress-test data seeder for dashboard pagination, lists, and speed testing.

Connects to the Render PostgreSQL database (via DATABASE_URL), prompts for a
target user_id (or use --email / --supabase-id), then seeds:
  - 5 connected Instagram accounts (@load_test_1 .. @load_test_5)
  - 5,000 analytics events (1,000 per account) as "media post"–like data
  - 1,000 DmLog rows (200 per account) as automation logs
  - 3,000 captured leads (600 per account) for "Recent Email leads" load-test

Uses bulk inserts for performance. Run from project root:
  python scripts/seed_stress_test.py
  python scripts/seed_stress_test.py --email your@email.com
  python scripts/seed_stress_test.py --supabase-id 5cff2718-2d6a-42ba-aab8-ce494aad3074

Requires: DATABASE_URL in environment (.env or export).

For stress-testing the production UI (logicdm.app): use Render's Postgres URL,
e.g. run:
  DATABASE_URL='postgresql://...'   python scripts/seed_stress_test.py --email you@example.com

  Leads-only (use existing load-test accounts):
  python scripts/seed_stress_test.py --email you@example.com --leads-only
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
from app.models.automation_rule import AutomationRule
from app.models.captured_lead import CapturedLead
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


def _seed_leads_only(sess, user_id: int, account_ids: list[int], account_to_rule: dict[int, int]) -> None:
    base_ts = datetime.now(timezone.utc)
    num_leads_per_account = 600
    leads = []
    for aid in account_ids:
        rid = account_to_rule[aid]
        for i in range(num_leads_per_account):
            ts = base_ts - timedelta(days=random.uniform(0, 60))
            leads.append({
                "user_id": user_id,
                "instagram_account_id": aid,
                "automation_rule_id": rid,
                "email": f"load_test_lead_{aid}_{i}@example.com",
                "phone": None,
                "name": f"Load Test Lead {aid}-{i}",
                "custom_fields": None,
                "extra_metadata": None,
                "captured_at": ts,
                "notified": False,
                "exported": False,
            })
    for start in range(0, len(leads), BATCH_SIZE):
        batch = leads[start : start + BATCH_SIZE]
        sess.bulk_insert_mappings(CapturedLead, batch)
        sess.flush()
    print(f"  Inserted {len(leads)} captured leads.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed stress-test data (5 accounts, 5k events, 1k DmLogs, 3k leads) for a user."
    )
    parser.add_argument("--email", type=str, help="User email (from backend users table)")
    parser.add_argument("--supabase-id", type=str, dest="supabase_id", help="Supabase Auth UID")
    parser.add_argument("--leads-only", action="store_true", help="Only seed 3k leads; use existing load_test_* accounts")
    args = parser.parse_args()

    if args.leads_only and not (args.email or args.supabase_id):
        print("ERROR: --leads-only requires --email or --supabase-id.")
        sys.exit(1)

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

        if args.leads_only:
            # Use existing load-test accounts; ensure rules; seed 3k leads only
            print("Leads-only mode: using existing load_test_* accounts.\n")
            rows = sess.execute(
                text("SELECT id, username FROM instagram_accounts WHERE user_id = :uid AND username LIKE 'load_test_%' ORDER BY id"),
                {"uid": user_id},
            ).fetchall()
            if not rows:
                print("ERROR: No load_test_* accounts found for this user. Run full seed first.")
                sys.exit(1)
            account_ids = [r[0] for r in rows]
            account_usernames = {r[0]: r[1] for r in rows}
            print(f"  Found {len(account_ids)} load-test accounts: {account_ids}")

            # Get or create one rule per account
            account_to_rule: dict[int, int] = {}
            for aid in account_ids:
                existing = sess.execute(
                    text("SELECT id FROM automation_rules WHERE instagram_account_id = :aid AND deleted_at IS NULL LIMIT 1"),
                    {"aid": aid},
                ).fetchone()
                if existing:
                    account_to_rule[aid] = existing[0]
                else:
                    r = AutomationRule(
                        instagram_account_id=aid,
                        name=f"Load-test rule #{aid}",
                        trigger_type="post_comment",
                        action_type="send_dm",
                        config={"stats": {}},
                        is_active=True,
                    )
                    sess.add(r)
                    sess.flush()
                    account_to_rule[aid] = r.id
            print(f"  Rules per account: {account_to_rule}")

            print("Bulk-inserting 3,000 captured leads (600 per account)...")
            _seed_leads_only(sess, user_id, account_ids, account_to_rule)
            sess.commit()
            print("\nDone. 3,000 leads seeded.")
            return

        # Full seed
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

        # 4. Create one minimal automation rule per load-test account (for leads FK)
        print("Creating 5 automation rules (one per load-test account)...")
        rules = []
        for aid in account_ids:
            r = AutomationRule(
                instagram_account_id=aid,
                name=f"Load-test rule #{aid}",
                trigger_type="post_comment",
                action_type="send_dm",
                config={"stats": {}},
                is_active=True,
            )
            sess.add(r)
            rules.append(r)
        sess.flush()
        rule_ids = [r.id for r in rules]
        account_to_rule = dict(zip(account_ids, rule_ids))
        print(f"  Created rule ids: {rule_ids}")

        # 5. Bulk-insert 3,000 captured leads (600 per account)
        print("Bulk-inserting 3,000 captured leads (600 per account)...")
        _seed_leads_only(sess, user_id, account_ids, account_to_rule)

        sess.commit()
        print("\nDone. Stress-test data seeded successfully.")
    except Exception as e:
        sess.rollback()
        print(f"ERROR: {e}")
        raise
    finally:
        sess.close()
        if args.leads_only:
            print("\n--- Cleanup (leads only) ---\n")
            print("DELETE FROM captured_leads WHERE instagram_account_id IN (SELECT id FROM instagram_accounts WHERE username LIKE 'load_test_%');")
            print()
        else:
            print("\n--- Cleanup (run these in order to remove only this test data) ---\n")
            print("-- 1. Captured leads for load-test accounts")
            print("DELETE FROM captured_leads WHERE instagram_account_id IN (SELECT id FROM instagram_accounts WHERE username LIKE 'load_test_%');")
            print()
            print("-- 2. Automation rules for load-test accounts")
            print("DELETE FROM automation_rules WHERE instagram_account_id IN (SELECT id FROM instagram_accounts WHERE username LIKE 'load_test_%');")
            print()
            print("-- 3. DmLogs for load-test accounts")
            print("DELETE FROM dm_logs WHERE instagram_account_id IN (SELECT id FROM instagram_accounts WHERE username LIKE 'load_test_%');")
            print()
            print("-- 4. Analytics events for load-test accounts")
            print("DELETE FROM analytics_events WHERE instagram_account_id IN (SELECT id FROM instagram_accounts WHERE username LIKE 'load_test_%');")
            print()
            print("-- 5. Load-test Instagram accounts")
            print("DELETE FROM instagram_accounts WHERE username LIKE 'load_test_%';")
            print()


if __name__ == "__main__":
    main()
