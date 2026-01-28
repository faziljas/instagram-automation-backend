#!/usr/bin/env python3
"""
Remove all load-test data from the database.

Deletes only rows linked to Instagram accounts with username LIKE 'load_test_%'
(created by scripts/seed_stress_test.py). Order respects FKs.

Run from project root with DATABASE_URL set (same as stress test):
  python scripts/cleanup_load_test.py
  DATABASE_URL='postgresql://...' python scripts/cleanup_load_test.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
load_dotenv(project_root / ".env")
load_dotenv(project_root / ".env.local")

_database_url = os.getenv("DATABASE_URL")
if _database_url and _database_url.startswith("postgres://"):
    os.environ["DATABASE_URL"] = "postgresql://" + _database_url[10:]

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


def main() -> None:
    url = os.getenv("DATABASE_URL")
    if not url:
        print("ERROR: DATABASE_URL not set. Add it to .env or export it.")
        sys.exit(1)

    engine = create_engine(url)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    sess = Session()

    try:
        # Resolve load-test account ids once (won't change during cleanup)
        rows = sess.execute(
            text("SELECT id FROM instagram_accounts WHERE username LIKE 'load_test_%'")
        ).fetchall()
        ids = [r[0] for r in rows]
        if not ids:
            print("No load_test_* accounts found. Nothing to clean.")
            return

        id_list = ",".join(str(i) for i in ids)
        print(f"Found {len(ids)} load_test_* accounts (ids: {id_list}). Cleaning...")

        def run(sql: str, label: str) -> int:
            r = sess.execute(text(sql))
            n = r.rowcount
            print(f"  {label}: {n}")
            return n

        # Order respects FKs; avoid subquery after we delete accounts
        run(
            f"DELETE FROM captured_leads WHERE instagram_account_id IN ({id_list})",
            "captured_leads",
        )
        run(
            f"DELETE FROM automation_rules WHERE instagram_account_id IN ({id_list})",
            "automation_rules",
        )
        run(
            f"DELETE FROM messages WHERE instagram_account_id IN ({id_list})",
            "messages",
        )
        run(
            f"DELETE FROM conversations WHERE instagram_account_id IN ({id_list})",
            "conversations",
        )
        run(
            f"DELETE FROM analytics_events WHERE instagram_account_id IN ({id_list})",
            "analytics_events",
        )
        run(
            f"DELETE FROM dm_logs WHERE instagram_account_id IN ({id_list})",
            "dm_logs",
        )
        try:
            run(
                f"DELETE FROM followers WHERE instagram_account_id IN ({id_list})",
                "followers",
            )
        except Exception:
            pass  # table may not exist
        try:
            run(
                f"DELETE FROM instagram_audience WHERE instagram_account_id IN ({id_list})",
                "instagram_audience",
            )
        except Exception:
            pass
        run(
            f"DELETE FROM instagram_accounts WHERE id IN ({id_list})",
            "instagram_accounts",
        )

        sess.commit()
        print("Done. All load_test_* data removed.")
    except Exception as e:
        sess.rollback()
        print(f"ERROR: {e}")
        raise
    finally:
        sess.close()


if __name__ == "__main__":
    main()
