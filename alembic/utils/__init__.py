"""
Alembic utility functions for safe migrations.
"""
from alembic.utils.safe_enum_addition import (
    safe_add_enum_value,
    safe_add_multiple_enum_values
)

__all__ = [
    'safe_add_enum_value',
    'safe_add_multiple_enum_values',
]
