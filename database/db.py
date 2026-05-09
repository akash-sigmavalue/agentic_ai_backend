from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from core.config import settings

# Centralized database configuration with connection pooling
engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    """FastAPI dependency injection pattern - yields a session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_session():
    """Manual session management pattern - returns a session."""
    return SessionLocal()
