"""
Migration script to create conversations table and update messages table.
"""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from app.db.base import Base
from app.models.conversation import Conversation
from app.models.message import Message
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./sql_app.db")

def run_migration():
    engine = create_engine(DATABASE_URL)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    with SessionLocal() as db:
        try:
            # Check if conversations table exists
            inspector = db.connection().in_transaction(lambda conn: conn.run_sync(lambda sync_conn: sync_conn.dialect.has_table(sync_conn, Conversation.__tablename__)))
            
            if not inspector:
                print(f"🔄 Creating '{Conversation.__tablename__}' table...")
                Conversation.__table__.create(engine)
                print(f"✅ Table '{Conversation.__tablename__}' created successfully.")
            else:
                print(f"✅ Table '{Conversation.__tablename__}' already exists. Skipping creation.")
            
            # Add new columns to messages table if they don't exist
            try:
                # Check if conversation_id column exists
                result = db.execute(text("""
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_name='messages' AND column_name='conversation_id'
                """))
                if not result.fetchone():
                    print("🔄 Adding 'conversation_id' column to 'messages' table...")
                    db.execute(text("ALTER TABLE messages ADD COLUMN conversation_id INTEGER"))
                    db.execute(text("CREATE INDEX IF NOT EXISTS ix_messages_conversation_id ON messages(conversation_id)"))
                    print("✅ Column 'conversation_id' added successfully.")
                else:
                    print("✅ Column 'conversation_id' already exists.")
            except Exception as e:
                # SQLite doesn't support information_schema, try direct ALTER
                try:
                    db.execute(text("ALTER TABLE messages ADD COLUMN conversation_id INTEGER"))
                    db.execute(text("CREATE INDEX IF NOT EXISTS ix_messages_conversation_id ON messages(conversation_id)"))
                    print("✅ Column 'conversation_id' added successfully.")
                except Exception as alter_err:
                    if "duplicate column" in str(alter_err).lower() or "already exists" in str(alter_err).lower():
                        print("✅ Column 'conversation_id' already exists.")
                    else:
                        print(f"⚠️ Could not add conversation_id column: {str(alter_err)}")
            
            try:
                # Check if content column exists
                result = db.execute(text("""
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_name='messages' AND column_name='content'
                """))
                if not result.fetchone():
                    print("🔄 Adding 'content' column to 'messages' table...")
                    db.execute(text("ALTER TABLE messages ADD COLUMN content VARCHAR"))
                    print("✅ Column 'content' added successfully.")
                else:
                    print("✅ Column 'content' already exists.")
            except Exception as e:
                try:
                    db.execute(text("ALTER TABLE messages ADD COLUMN content VARCHAR"))
                    print("✅ Column 'content' added successfully.")
                except Exception as alter_err:
                    if "duplicate column" in str(alter_err).lower() or "already exists" in str(alter_err).lower():
                        print("✅ Column 'content' already exists.")
                    else:
                        print(f"⚠️ Could not add content column: {str(alter_err)}")
            
            try:
                # Check if platform_message_id column exists
                result = db.execute(text("""
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_name='messages' AND column_name='platform_message_id'
                """))
                if not result.fetchone():
                    print("🔄 Adding 'platform_message_id' column to 'messages' table...")
                    db.execute(text("ALTER TABLE messages ADD COLUMN platform_message_id VARCHAR"))
                    db.execute(text("CREATE INDEX IF NOT EXISTS ix_messages_platform_message_id ON messages(platform_message_id)"))
                    print("✅ Column 'platform_message_id' added successfully.")
                else:
                    print("✅ Column 'platform_message_id' already exists.")
            except Exception as e:
                try:
                    db.execute(text("ALTER TABLE messages ADD COLUMN platform_message_id VARCHAR"))
                    db.execute(text("CREATE INDEX IF NOT EXISTS ix_messages_platform_message_id ON messages(platform_message_id)"))
                    print("✅ Column 'platform_message_id' added successfully.")
                except Exception as alter_err:
                    if "duplicate column" in str(alter_err).lower() or "already exists" in str(alter_err).lower():
                        print("✅ Column 'platform_message_id' already exists.")
                    else:
                        print(f"⚠️ Could not add platform_message_id column: {str(alter_err)}")
            
            # Copy message_text to content for existing messages
            try:
                print("🔄 Copying message_text to content for existing messages...")
                db.execute(text("UPDATE messages SET content = message_text WHERE content IS NULL AND message_text IS NOT NULL"))
                print("✅ Copied message_text to content.")
            except Exception as e:
                print(f"⚠️ Could not copy message_text to content: {str(e)}")
            
            # Copy message_id to platform_message_id for existing messages
            try:
                print("🔄 Copying message_id to platform_message_id for existing messages...")
                db.execute(text("UPDATE messages SET platform_message_id = message_id WHERE platform_message_id IS NULL AND message_id IS NOT NULL"))
                print("✅ Copied message_id to platform_message_id.")
            except Exception as e:
                print(f"⚠️ Could not copy message_id to platform_message_id: {str(e)}")
            
            db.commit()
            print("✅ Migration completed successfully!")
            
        except Exception as e:
            db.rollback()
            print(f"❌ Error during migration: {e}")
            import traceback
            traceback.print_exc()
            raise

if __name__ == "__main__":
    run_migration()
