# Migration Quick Reference

## ⚡ Quick Start

### Adding Enum Value
```python
from alembic import op
import sqlalchemy as sa

def upgrade() -> None:
    op.execute(sa.text("""
        DO $$
        BEGIN
            ALTER TYPE eventtype ADD VALUE 'new_value';
        EXCEPTION
            WHEN OTHERS THEN
                IF SQLSTATE = '42710' OR SQLERRM LIKE '%already exists%' OR SQLERRM LIKE '%duplicate%' THEN
                    NULL;
                ELSE
                    RAISE;
                END IF;
        END $$;
    """))
```

### Adding Multiple Enum Values
```python
from alembic import op
import sqlalchemy as sa

def upgrade() -> None:
    op.execute(sa.text("""
        DO $$
        BEGIN
            ALTER TYPE eventtype ADD VALUE 'value1';
            ALTER TYPE eventtype ADD VALUE 'value2';
        EXCEPTION
            WHEN OTHERS THEN
                IF SQLSTATE = '42710' OR SQLERRM LIKE '%already exists%' OR SQLERRM LIKE '%duplicate%' THEN
                    NULL;
                ELSE
                    RAISE;
                END IF;
        END $$;
    """))
```

### Checking Schema Exists
```python
def upgrade() -> None:
    conn = op.get_bind()
    result = conn.execute(sa.text("""
        SELECT EXISTS(
            SELECT 1 FROM information_schema.schemata 
            WHERE schema_name = 'schema_name'
        );
    """)).scalar()
    
    if not result:
        return  # Skip if schema doesn't exist
```

## 🚫 Never Do This

```python
# ❌ DON'T: This aborts transaction
try:
    op.execute(sa.text("ALTER TYPE eventtype ADD VALUE 'value'"))
except Exception:
    pass  # Transaction already aborted!

# ❌ DON'T: Assume schema exists
op.execute(sa.text("CREATE TRIGGER ... ON auth.users ..."))
```

## ✅ Always Do This

```python
# ✅ DO: Use DO block pattern
op.execute(sa.text("""
    DO $$
    BEGIN
        ALTER TYPE eventtype ADD VALUE 'value';
    EXCEPTION
        WHEN OTHERS THEN
            IF SQLSTATE = '42710' OR SQLERRM LIKE '%already exists%' OR SQLERRM LIKE '%duplicate%' THEN
                NULL;
            ELSE
                RAISE;
            END IF;
    END $$;
"""))

# ✅ DO: Check schema first
if schema_exists:
    create_trigger()
```

## 📋 Pre-Commit Checklist

- [ ] Uses DO block pattern for enum additions
- [ ] Checks schema existence before using schemas  
- [ ] Uses `IF NOT EXISTS` / `IF EXISTS` where possible
- [ ] Tested on fresh database
- [ ] Tested idempotency (run twice)

See `MIGRATION_GUIDELINES.md` for full details.
