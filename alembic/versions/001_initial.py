"""Initial revision — no-op so alembic upgrade head succeeds.

Tables are created by app startup (Base.metadata.create_all + ad-hoc migrations).
Use this as the first Alembic revision; add real migrations after this.

Revision ID: 001_initial
Revises:
Create Date: 2026-01-28

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
