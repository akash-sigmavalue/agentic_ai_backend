from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import OperationalError

from core.config import Settings

database_url = Settings().DATABASE_URL_RDS
engine_kwargs = {}
if database_url.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}
engine = create_engine(database_url, **engine_kwargs)

try:
    # Validate DB connection at startup so local development can continue
    # even when Postgres credentials are not configured correctly.
    with engine.connect():
        pass
except OperationalError:
    fallback_url = "sqlite:///./local_dev.db"
    engine = create_engine(fallback_url, connect_args={"check_same_thread": False})
    print(
        "Warning: PostgreSQL connection failed. Falling back to SQLite "
        "database at ./local_dev.db for local development."
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
