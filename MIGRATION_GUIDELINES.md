# Alembic Migration Guidelines

## ⚠️ CRITICAL: Preventing Migration Failures

This document outlines best practices to prevent migration issues that cause deployment failures.

## Common Issues We've Fixed

1. **Enum value additions failing** - Transaction abort when enum value already exists
2. **Schema dependencies** - Migrations failing when schemas don't exist (e.g., `auth` schema)
3. **Transaction abort** - PostgreSQL aborting transactions on errors

## Best Practices

### 1. Adding Enum Values

**❌ NEVER DO THIS:**
```python
def upgrade() -> None:
    import sqlalchemy as sa
    try:
        op.execute(sa.text("ALTER TYPE eventtype ADD VALUE 'new_value'"))
    except Exception as e:
        if "already exists" not in str(e).lower():
            raise
```

**✅ ALWAYS DO THIS:**
```python
from alembic.utils.safe_enum_addition import safe_add_enum_value

def upgrade() -> None:
    safe_add_enum_value('eventtype', 'new_value')
```

**Why:** The try/except approach aborts the PostgreSQL transaction. DO blocks handle errors gracefully without aborting.

### 2. Adding Multiple Enum Values

**✅ USE THIS:**
```python
from alembic.utils.safe_enum_addition import safe_add_multiple_enum_values

def upgrade() -> None:
    safe_add_multiple_enum_values('eventtype', [
        'value1',
        'value2',
        'value3'
    ])
```

### 3. Checking Schema Existence

**❌ NEVER DO THIS:**
```python
def upgrade() -> None:
    conn.execute(sa.text("CREATE TRIGGER ... ON auth.users ..."))
```

**✅ ALWAYS DO THIS:**
```python
def upgrade() -> None:
    conn = op.get_bind()
    
    # Check if schema exists before creating trigger
    result = conn.execute(
        sa.text("""
            SELECT EXISTS(
                SELECT 1 FROM information_schema.schemata 
                WHERE schema_name = 'auth'
            );
        """)
    ).scalar()
    
    if not result:
        print("⚠️  Schema 'auth' does not exist. Skipping trigger creation.")
        return
    
    # Now safe to create trigger
    conn.execute(sa.text("CREATE TRIGGER ... ON auth.users ..."))
```

### 4. Handling Optional Dependencies

**✅ ALWAYS CHECK:**
- Schema existence before creating objects in that schema
- Column existence before modifying columns (`IF NOT EXISTS` / `IF EXISTS`)
- Constraint existence before dropping constraints (`IF EXISTS`)
- Table existence before creating tables (`IF NOT EXISTS`)

### 5. Idempotent Migrations

**✅ MAKE MIGRATIONS IDEMPOTENT:**
- Use `IF NOT EXISTS` / `IF EXISTS` clauses
- Check for existence before creating/modifying
- Handle "already exists" errors gracefully
- Use safe helper functions (like `safe_add_enum_value`)

### 6. Testing Migrations

**✅ BEFORE COMMITTING:**
1. Test migration on a fresh database
2. Test migration on a database that already has the changes (idempotency)
3. Test rollback (`alembic downgrade`)
4. Check for transaction abort issues

## Migration Template

Use this template for new migrations:

```python
"""Description of what this migration does.

Revision ID: xyz_description
Revises: previous_revision
Create Date: YYYY-MM-DD
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# Import safe helpers
from alembic.utils.safe_enum_addition import safe_add_enum_value

revision: str = "xyz_description"
down_revision: Union[str, None] = "previous_revision"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Apply migration changes."""
    conn = op.get_bind()
    
    # Example: Adding enum value
    safe_add_enum_value('eventtype', 'new_value')
    
    # Example: Checking schema existence
    schema_exists = conn.execute(
        sa.text("""
            SELECT EXISTS(
                SELECT 1 FROM information_schema.schemata 
                WHERE schema_name = 'schema_name'
            );
        """)
    ).scalar()
    
    if not schema_exists:
        print("⚠️  Schema does not exist. Skipping...")
        return
    
    # Example: Adding column with IF NOT EXISTS
    conn.execute(sa.text("""
        ALTER TABLE table_name 
        ADD COLUMN IF NOT EXISTS column_name VARCHAR(255)
    """))


def downgrade() -> None:
    """Revert migration changes."""
    # PostgreSQL enums can't be removed, so this is often a no-op
    # For other changes, provide proper rollback logic
    pass
```

## Common Patterns

### Pattern 1: Adding Enum Value
```python
from alembic.utils.safe_enum_addition import safe_add_enum_value

def upgrade() -> None:
    safe_add_enum_value('eventtype', 'new_value')
```

### Pattern 2: Adding Column Safely
```python
def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("""
        ALTER TABLE table_name 
        ADD COLUMN IF NOT EXISTS column_name VARCHAR(255)
    """))
```

### Pattern 3: Creating Index Safely
```python
def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_name 
        ON table_name(column_name)
    """))
```

### Pattern 4: Modifying Column Type Safely
```python
def upgrade() -> None:
    conn = op.get_bind()
    # Check current type first
    result = conn.execute(sa.text("""
        SELECT data_type FROM information_schema.columns
        WHERE table_schema = 'public' 
        AND table_name = 'table_name' 
        AND column_name = 'column_name'
    """)).fetchone()
    
    if result and result[0] != 'target_type':
        conn.execute(sa.text("""
            ALTER TABLE table_name
            ALTER COLUMN column_name TYPE target_type
        """))
```

## Checklist Before Committing Migration

- [ ] Uses `safe_add_enum_value` for enum additions
- [ ] Checks schema existence before using schemas
- [ ] Uses `IF NOT EXISTS` / `IF EXISTS` where possible
- [ ] Handles "already exists" errors gracefully
- [ ] Tested on fresh database
- [ ] Tested on database with existing changes (idempotency)
- [ ] No hard dependencies on optional schemas/objects
- [ ] Migration can run multiple times safely

## Emergency: If Migration Fails in Production

1. **Don't panic** - The app won't start, but data is safe
2. **Check logs** - Find the exact error
3. **Fix migration** - Use safe patterns above
4. **Test locally** - Verify fix works
5. **Push fix** - Deploy will retry automatically
6. **Monitor** - Ensure migration completes successfully

## Future Improvements

Consider:
- Using a different approach for enums (e.g., VARCHAR with CHECK constraint)
- Creating a migration validation script
- Adding pre-commit hooks to check migration patterns
- Using migration testing framework
