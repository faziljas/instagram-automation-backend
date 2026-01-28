"""Legacy schema updates — idempotent adds (columns, indexes, nullable).

All additive changes from former ad-hoc migrations and main.py auto-migrate.
PostgreSQL only; safe to run on already-migrated DBs (IF NOT EXISTS / checks).

Revision ID: 002_legacy_schema
Revises: 001_initial
Create Date: 2026-01-28

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "002_legacy_schema"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # ----- instagram_accounts -----
    conn.execute(sa.text("ALTER TABLE instagram_accounts ADD COLUMN IF NOT EXISTS igsid VARCHAR(255)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_instagram_accounts_igsid ON instagram_accounts(igsid)"))
    conn.execute(sa.text("ALTER TABLE instagram_accounts ADD COLUMN IF NOT EXISTS page_id VARCHAR(255)"))
    conn.execute(sa.text("ALTER TABLE instagram_accounts ADD COLUMN IF NOT EXISTS encrypted_page_token TEXT"))
    conn.execute(sa.text(
        "ALTER TABLE instagram_accounts ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"
    ))

    # ----- automation_rules -----
    conn.execute(sa.text("ALTER TABLE automation_rules ADD COLUMN IF NOT EXISTS media_id VARCHAR(255)"))
    conn.execute(sa.text("ALTER TABLE automation_rules ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP"))

    # ----- automation_rule_stats -----
    conn.execute(sa.text(
        "ALTER TABLE automation_rule_stats ADD COLUMN IF NOT EXISTS total_follow_button_clicks INTEGER DEFAULT 0"
    ))
    conn.execute(sa.text(
        "ALTER TABLE automation_rule_stats ADD COLUMN IF NOT EXISTS last_follow_button_clicked_at TIMESTAMP"
    ))
    conn.execute(sa.text(
        "ALTER TABLE automation_rule_stats ADD COLUMN IF NOT EXISTS total_profile_visits INTEGER DEFAULT 0"
    ))
    conn.execute(sa.text(
        "ALTER TABLE automation_rule_stats ADD COLUMN IF NOT EXISTS total_im_following_clicks INTEGER DEFAULT 0"
    ))
    conn.execute(sa.text(
        "ALTER TABLE automation_rule_stats ADD COLUMN IF NOT EXISTS last_profile_visit_at TIMESTAMP"
    ))
    conn.execute(sa.text(
        "ALTER TABLE automation_rule_stats ADD COLUMN IF NOT EXISTS last_im_following_clicked_at TIMESTAMP"
    ))

    # ----- analytics_events -----
    conn.execute(sa.text("ALTER TABLE analytics_events ADD COLUMN IF NOT EXISTS media_preview_url VARCHAR(500)"))

    # ----- users -----
    conn.execute(sa.text("ALTER TABLE users ADD COLUMN IF NOT EXISTS supabase_id VARCHAR(255)"))
    conn.execute(sa.text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_supabase_id ON users(supabase_id)"))

    # ----- dm_logs -----
    conn.execute(sa.text("ALTER TABLE dm_logs ADD COLUMN IF NOT EXISTS instagram_username VARCHAR(255)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_dm_logs_instagram_username ON dm_logs(instagram_username)"))
    conn.execute(sa.text("ALTER TABLE dm_logs ADD COLUMN IF NOT EXISTS instagram_igsid VARCHAR(255)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_dm_logs_instagram_igsid ON dm_logs(instagram_igsid)"))
    conn.execute(sa.text("""
        UPDATE dm_logs d SET
            instagram_username = a.username,
            instagram_igsid = a.igsid
        FROM instagram_accounts a
        WHERE d.instagram_account_id = a.id
          AND d.instagram_username IS NULL
    """))
    conn.execute(sa.text("""
        DO $$
        BEGIN
            IF (SELECT is_nullable FROM information_schema.columns
                WHERE table_name = 'dm_logs' AND column_name = 'instagram_account_id') = 'NO' THEN
                ALTER TABLE dm_logs ALTER COLUMN instagram_account_id DROP NOT NULL;
            END IF;
        END $$;
    """))

    # ----- subscriptions -----
    conn.execute(sa.text(
        "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS billing_cycle_start_date TIMESTAMP"
    ))

    # ----- messages -----
    conn.execute(sa.text("ALTER TABLE messages ADD COLUMN IF NOT EXISTS conversation_id INTEGER"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_messages_conversation_id ON messages(conversation_id)"))
    conn.execute(sa.text("ALTER TABLE messages ADD COLUMN IF NOT EXISTS content VARCHAR"))
    conn.execute(sa.text("ALTER TABLE messages ADD COLUMN IF NOT EXISTS platform_message_id VARCHAR"))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_messages_platform_message_id ON messages(platform_message_id)"
    ))
    conn.execute(sa.text(
        "UPDATE messages SET content = message_text WHERE content IS NULL AND message_text IS NOT NULL"
    ))
    conn.execute(sa.text(
        "UPDATE messages SET platform_message_id = message_id "
        "WHERE platform_message_id IS NULL AND message_id IS NOT NULL"
    ))

    # ----- automation_rules: instagram_account_id nullable -----
    conn.execute(sa.text("""
        DO $$
        BEGIN
            IF (SELECT is_nullable FROM information_schema.columns
                WHERE table_name = 'automation_rules' AND column_name = 'instagram_account_id') = 'NO' THEN
                ALTER TABLE automation_rules ALTER COLUMN instagram_account_id DROP NOT NULL;
            END IF;
        END $$;
    """))

    # ----- captured_leads: instagram_account_id nullable -----
    conn.execute(sa.text("""
        DO $$
        BEGIN
            IF (SELECT is_nullable FROM information_schema.columns
                WHERE table_name = 'captured_leads' AND column_name = 'instagram_account_id') = 'NO' THEN
                ALTER TABLE captured_leads ALTER COLUMN instagram_account_id DROP NOT NULL;
            END IF;
        END $$;
    """))


def downgrade() -> None:
    pass  # Additive-only; downgrade would require arbitrary drops, skipped.
