"""Create invoices table for Dodo Payments.

Revision ID: 004_dodo_invoices_table
Revises: 003_dodo_payments_columns
Create Date: 2026-02-10
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "004_dodo_invoices_table"
down_revision: Union[str, None] = "003_dodo_payments_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
  """Create invoices table (idempotent for repeated deploys)."""
  conn = op.get_bind()

  # Use plain SQL so we can guard with IF NOT EXISTS for Postgres.
  conn.execute(
      sa.text(
          """
          CREATE TABLE IF NOT EXISTS invoices (
              id SERIAL PRIMARY KEY,
              user_id INTEGER NOT NULL REFERENCES users (id),
              provider VARCHAR(50) NOT NULL DEFAULT 'dodo',
              provider_invoice_id VARCHAR(255) UNIQUE,
              provider_payment_id VARCHAR(255) UNIQUE,
              amount INTEGER NOT NULL,
              currency VARCHAR(10) NOT NULL,
              status VARCHAR(50) NOT NULL,
              invoice_url TEXT,
              paid_at TIMESTAMP,
              created_at TIMESTAMP NOT NULL DEFAULT NOW(),
              updated_at TIMESTAMP NOT NULL DEFAULT NOW()
          );
          """
      )
  )


def downgrade() -> None:
  """Keep invoices for safety; no-op downgrade."""
  pass

