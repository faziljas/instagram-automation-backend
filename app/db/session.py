import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@localhost/dbname")

# Configure connection pooling to prevent connection exhaustion
# This is critical for handling concurrent requests from multiple users
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    poolclass=QueuePool,
    pool_size=10,  # Number of connections to maintain persistently
    max_overflow=20,  # Maximum number of connections to create beyond pool_size
    pool_timeout=30,  # Seconds to wait before giving up on getting a connection
    pool_pre_ping=True,  # Verify connections before using them (handles stale connections)
    pool_recycle=3600,  # Recycle connections after 1 hour to prevent stale connections
    echo=False  # Set to True for SQL query logging (useful for debugging)
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
