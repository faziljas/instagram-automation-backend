"""
Migration script to create the messages table for storing all Instagram DM messages.
Run this script to add the messages table to your database.
"""
import os
import sys
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# Get database URL from environment or use default
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    print("❌ DATABASE_URL environment variable not set")
    sys.exit(1)

def run_migration():
    """Create the messages table if it doesn't exist."""
    engine = create_engine(DATABASE_URL)
    Session = sessionmaker(bind=engine)
    session = Session()
    
    try:
        # Check if table already exists
        result = session.execute(text("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'messages'
            );
        """))
        table_exists = result.scalar()
        
        if table_exists:
            print("✅ Table 'messages' already exists")
        else:
            # Create the messages table
            session.execute(text("""
                CREATE TABLE messages (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    instagram_account_id INTEGER NOT NULL,
                    sender_id VARCHAR NOT NULL,
                    sender_username VARCHAR,
                    recipient_id VARCHAR NOT NULL,
                    recipient_username VARCHAR,
                    message_text TEXT,
                    message_id VARCHAR,
                    is_from_bot BOOLEAN DEFAULT FALSE,
                    has_attachments BOOLEAN DEFAULT FALSE,
                    attachments JSON,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """))
            
            # Create indexes
            session.execute(text("""
                CREATE INDEX idx_messages_user_id ON messages(user_id);
            """))
            session.execute(text("""
                CREATE INDEX idx_messages_instagram_account_id ON messages(instagram_account_id);
            """))
            session.execute(text("""
                CREATE INDEX idx_messages_sender_id ON messages(sender_id);
            """))
            session.execute(text("""
                CREATE INDEX idx_messages_recipient_id ON messages(recipient_id);
            """))
            session.execute(text("""
                CREATE INDEX idx_messages_message_id ON messages(message_id);
            """))
            session.execute(text("""
                CREATE INDEX idx_messages_created_at ON messages(created_at);
            """))
            
            # Add foreign key constraints
            session.execute(text("""
                ALTER TABLE messages 
                ADD CONSTRAINT fk_messages_user_id 
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
            """))
            session.execute(text("""
                ALTER TABLE messages 
                ADD CONSTRAINT fk_messages_instagram_account_id 
                FOREIGN KEY (instagram_account_id) REFERENCES instagram_accounts(id) ON DELETE CASCADE;
            """))
            
            session.commit()
            print("✅ Table 'messages' created successfully with indexes and foreign keys")
        
    except Exception as e:
        session.rollback()
        print(f"❌ Error creating messages table: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        session.close()

if __name__ == "__main__":
    print("🔄 Running messages table migration...")
    run_migration()
    print("✅ Migration completed!")
