"""Add profile_picture_url column to users table.

Revision ID: 005_add_profile_picture_url
Revises: 004_dodo_invoices_table
Create Date: 2026-02-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "005_add_profile_picture_url"
down_revision: Union[str, None] = "004_dodo_invoices_table"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add profile_picture_url column to users table (idempotent for repeated deploys)."""
    conn = op.get_bind()
    
    # Use plain SQL so we can guard with IF NOT EXISTS for Postgres.
    # This makes it safe to run multiple times.
    # Alembic handles commits automatically, so we don't need conn.commit()
    conn.execute(
        sa.text(
            """
            ALTER TABLE users 
            ADD COLUMN IF NOT EXISTS profile_picture_url VARCHAR;
            """
        )
    )


def downgrade() -> None:
    """Remove profile_picture_url column (optional - keep for safety)."""
    # Uncomment if you want to support downgrade
    # conn = op.get_bind()
    # conn.execute(
    #     sa.text("ALTER TABLE users DROP COLUMN IF EXISTS profile_picture_url;")
    # )
    pass
