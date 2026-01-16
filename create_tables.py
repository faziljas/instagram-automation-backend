from app.db.session import engine
from app.db.base import Base
from app.models import *  # Import all models

print("Creating database tables...")
Base.metadata.create_all(bind=engine)
print("✅ All tables created successfully!")
