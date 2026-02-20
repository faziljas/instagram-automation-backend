"""
Enum validation utility to ensure database enum values match code definitions.
This prevents the recurring issue where enum values are missing from the database.
"""
from sqlalchemy import text
from app.models.analytics_event import EventType


def validate_eventtype_enum(db) -> tuple[bool, list[str]]:
    """
    Validate that all EventType enum values exist in the PostgreSQL enum type.
    
    Args:
        db: SQLAlchemy database session
        
    Returns:
        tuple: (is_valid, missing_values)
        - is_valid: True if all values exist, False otherwise
        - missing_values: List of enum values that are missing from the database
    """
    try:
        # Get all enum values from the database
        result = db.execute(text("""
            SELECT unnest(enum_range(NULL::eventtype))::text AS enum_value
        """))
        db_enum_values = {row[0] for row in result}
        
        # Get all enum values from the code
        code_enum_values = {event_type.value for event_type in EventType}
        
        # Find missing values
        missing_values = code_enum_values - db_enum_values
        
        is_valid = len(missing_values) == 0
        
        if not is_valid:
            print(f"⚠️  ENUM VALIDATION FAILED: Missing {len(missing_values)} enum value(s): {sorted(missing_values)}")
            print(f"   Database has: {sorted(db_enum_values)}")
            print(f"   Code expects: {sorted(code_enum_values)}")
        else:
            print(f"✅ Enum validation passed: All {len(code_enum_values)} EventType values exist in database")
        
        return is_valid, sorted(missing_values)
        
    except Exception as e:
        print(f"⚠️  Failed to validate enum: {e}")
        # If we can't validate, assume invalid to be safe
        return False, []


def ensure_eventtype_enum_values(db) -> bool:
    """
    Ensure all EventType enum values exist in the database.
    Adds any missing values automatically.
    
    Args:
        db: SQLAlchemy database session
        
    Returns:
        bool: True if all values now exist, False if there were errors
    """
    try:
        from app.models.analytics_event import EventType
        
        # Get all enum values from the code
        required_values = {event_type.value for event_type in EventType}
        
        # Try to get existing values (may fail if enum doesn't exist)
        try:
            result = db.execute(text("""
                SELECT unnest(enum_range(NULL::eventtype))::text AS enum_value
            """))
            existing_values = {row[0] for row in result}
        except Exception:
            # Enum type might not exist yet, assume empty
            existing_values = set()
        
        # Add missing values
        missing_values = required_values - existing_values
        added_count = 0
        
        for value in sorted(missing_values):
            try:
                # Use DO block to handle "already exists" errors gracefully
                db.execute(text(f"""
                    DO $$
                    BEGIN
                        ALTER TYPE eventtype ADD VALUE '{value}';
                    EXCEPTION
                        WHEN OTHERS THEN
                            IF SQLSTATE = '42710' OR SQLERRM LIKE '%already exists%' OR SQLERRM LIKE '%duplicate%' THEN
                                NULL;
                            ELSE
                                RAISE;
                            END IF;
                    END $$;
                """))
                print(f"✅ Added missing enum value: {value}")
                added_count += 1
            except Exception as e:
                error_str = str(e).lower()
                if "already exists" in error_str or "duplicate" in error_str:
                    print(f"ℹ️  Enum value {value} already exists")
                else:
                    print(f"⚠️  Failed to add enum value {value}: {e}")
                    return False
        
        if added_count > 0:
            db.commit()
            print(f"✅ Successfully added {added_count} missing enum value(s)")
        
        return True
        
    except Exception as e:
        print(f"⚠️  Failed to ensure enum values: {e}")
        db.rollback()
        return False
