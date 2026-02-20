"""
Utility function for safely adding enum values in Alembic migrations.

This prevents transaction abort errors when enum values already exist.
Use this helper in all migrations that add enum values.
"""
from alembic import op
import sqlalchemy as sa


def safe_add_enum_value(enum_type_name: str, enum_value: str) -> None:
    """
    Safely add an enum value to a PostgreSQL enum type.
    
    This function handles the case where the enum value already exists without
    aborting the transaction. It uses a DO block with exception handling to
    gracefully handle duplicate values.
    
    Args:
        enum_type_name: Name of the PostgreSQL enum type (e.g., 'eventtype')
        enum_value: The enum value to add (e.g., 'phone_collected')
    
    Example:
        safe_add_enum_value('eventtype', 'phone_collected')
    """
    op.execute(sa.text(f"""
        DO $$
        BEGIN
            ALTER TYPE {enum_type_name} ADD VALUE '{enum_value}';
        EXCEPTION
            WHEN OTHERS THEN
                -- Check if error is about duplicate/existing value
                IF SQLSTATE = '42710' OR SQLERRM LIKE '%already exists%' OR SQLERRM LIKE '%duplicate%' THEN
                    -- Value already exists, that's fine - do nothing
                    NULL;
                ELSE
                    -- Re-raise unexpected errors
                    RAISE;
                END IF;
        END $$;
    """))


def safe_add_multiple_enum_values(enum_type_name: str, enum_values: list[str]) -> None:
    """
    Safely add multiple enum values to a PostgreSQL enum type.
    
    This is more efficient than calling safe_add_enum_value multiple times
    as it uses a single DO block.
    
    Args:
        enum_type_name: Name of the PostgreSQL enum type (e.g., 'eventtype')
        enum_values: List of enum values to add
    
    Example:
        safe_add_multiple_enum_values('eventtype', ['phone_collected', 'trigger_matched'])
    """
    if not enum_values:
        return
    
    # Build the ALTER TYPE statements inside the DO block
    alter_statements = '\n'.join([
        f"        ALTER TYPE {enum_type_name} ADD VALUE '{value}';"
        for value in enum_values
    ])
    
    op.execute(sa.text(f"""
        DO $$
        BEGIN
{alter_statements}
        EXCEPTION
            WHEN OTHERS THEN
                -- Check if error is about duplicate/existing value
                IF SQLSTATE = '42710' OR SQLERRM LIKE '%already exists%' OR SQLERRM LIKE '%duplicate%' THEN
                    -- Value already exists, that's fine - do nothing
                    NULL;
                ELSE
                    -- Re-raise unexpected errors
                    RAISE;
                END IF;
        END $$;
    """))
