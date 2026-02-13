"""Add trigger to cascade delete from users table when auth.users is deleted.

This trigger automatically deletes the corresponding row from the public.users table
when a user is deleted from Supabase Auth (auth.users table).

Revision ID: 006_add_auth_users_cascade_trigger
Revises: 005_add_profile_picture_url
Create Date: 2026-02-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "006_add_auth_users_cascade_trigger"
down_revision: Union[str, None] = "005_add_profile_picture_url"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Create a trigger function and trigger that automatically deletes from public.users
    when a user is deleted from auth.users.
    
    This ensures data consistency when users are deleted directly from Supabase Auth dashboard.
    """
    conn = op.get_bind()
    
    # Create the trigger function
    # This function will be called AFTER a row is deleted from auth.users
    conn.execute(
        sa.text("""
            CREATE OR REPLACE FUNCTION public.handle_auth_user_deleted()
            RETURNS TRIGGER AS $$
            BEGIN
                -- Delete the corresponding user from public.users table
                -- where supabase_id matches the deleted auth.users id
                DELETE FROM public.users 
                WHERE supabase_id = OLD.id;
                
                RETURN OLD;
            END;
            $$ LANGUAGE plpgsql SECURITY DEFINER;
        """)
    )
    
    # Create the trigger on auth.users table
    # This trigger fires AFTER DELETE on auth.users
    conn.execute(
        sa.text("""
            DROP TRIGGER IF EXISTS on_auth_user_deleted ON auth.users;
            
            CREATE TRIGGER on_auth_user_deleted
            AFTER DELETE ON auth.users
            FOR EACH ROW
            EXECUTE FUNCTION public.handle_auth_user_deleted();
        """)
    )
    
    print("✅ Created trigger to cascade delete from public.users when auth.users is deleted")


def downgrade() -> None:
    """Remove the trigger and function."""
    conn = op.get_bind()
    
    # Drop the trigger first
    conn.execute(
        sa.text("DROP TRIGGER IF EXISTS on_auth_user_deleted ON auth.users;")
    )
    
    # Drop the function
    conn.execute(
        sa.text("DROP FUNCTION IF EXISTS public.handle_auth_user_deleted();")
    )
    
    print("✅ Removed auth.users cascade delete trigger")
