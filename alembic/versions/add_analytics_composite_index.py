"""Add composite index for analytics queries

Revision ID: add_analytics_composite_index
Revises: 
Create Date: 2026-02-13

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'add_analytics_composite_index'
down_revision = '008_add_unique_constraint_conversations'  # Latest migration
branch_labels = None
depends_on = None

def upgrade():
    # Add composite index for analytics queries (user_id, created_at, event_type)
    # This dramatically speeds up analytics dashboard queries
    op.create_index(
        'ix_analytics_events_user_created_type',
        'analytics_events',
        ['user_id', 'created_at', 'event_type'],
        unique=False
    )
    
    # Add composite index for instagram_account_id filtering
    op.create_index(
        'ix_analytics_events_user_account_created',
        'analytics_events',
        ['user_id', 'instagram_account_id', 'created_at'],
        unique=False
    )

def downgrade():
    op.drop_index('ix_analytics_events_user_account_created', table_name='analytics_events')
    op.drop_index('ix_analytics_events_user_created_type', table_name='analytics_events')
