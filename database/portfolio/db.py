from sqlalchemy import create_engine
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker

from core.config import settings
from database.portfolio.models import Base, PortfolioFlatRecord


engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
    ensure_schema()


def ensure_schema():
    inspector = inspect(engine)
    if not inspector.has_table("records"):
        return
    columns = {column["name"] for column in inspector.get_columns("records")}
    with engine.begin() as connection:
        if "derived_audit" not in columns:
            if engine.dialect.name == "postgresql":
                connection.execute(text("ALTER TABLE records ADD COLUMN IF NOT EXISTS derived_audit JSON DEFAULT '{}'::json"))
            else:
                connection.execute(text("ALTER TABLE records ADD COLUMN derived_audit JSON DEFAULT '{}'"))
    ensure_portfolio_flat_schema(inspector)


def ensure_portfolio_flat_schema(inspector):
    if not inspector.has_table("portfolio_flat_records"):
        return
    existing_columns = {column["name"] for column in inspector.get_columns("portfolio_flat_records")}
    table = PortfolioFlatRecord.__table__
    with engine.begin() as connection:
        for column in table.columns:
            if column.name in existing_columns or column.primary_key:
                continue
            column_type = column.type.compile(dialect=engine.dialect)
            connection.execute(text(f"ALTER TABLE portfolio_flat_records ADD COLUMN {column.name} {column_type}"))
