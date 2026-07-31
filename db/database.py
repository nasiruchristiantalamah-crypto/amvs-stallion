"""
================================================================================
DATABASE — SQLAlchemy engine & session
================================================================================
What this file does:
    Sets up the SQLAlchemy engine, session factory, and declarative Base
    that db/models.py's tables (User, Client, ValuationRun) are built on,
    plus the get_db() FastAPI dependency every request-handling route uses
    to get a request-scoped database session.

    Reads DATABASE_URL from the environment — on Railway this is injected
    automatically when a PostgreSQL plugin is attached to the project (see
    .env.example). If DATABASE_URL isn't set at all (e.g. running locally
    without Postgres configured), falls back to a local SQLite file so the
    API still boots for development — production deployments MUST set
    DATABASE_URL to a real Postgres connection string.

Not using Alembic yet:
    Base.metadata.create_all() (called once at API startup — see
    api/main.py) is enough to stand up the initial schema, but it can't
    apply migrations to an already-populated table (e.g. adding a column
    later). If/when the schema needs to change after go-live, introduce
    Alembic then — premature to add migration tooling before there's
    anything to migrate.
================================================================================
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./amvs_dev.db")

# Railway (and most managed Postgres providers) hand out "postgres://" URLs,
# but SQLAlchemy 2.x's psycopg2 dialect requires the "postgresql://" scheme.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if DATABASE_URL.startswith("sqlite"):
    print("WARNING: DATABASE_URL not set — falling back to local SQLite (amvs_dev.db). "
          "Set DATABASE_URL to a Postgres connection string in production.")
    connect_args = {"check_same_thread": False}
else:
    connect_args = {}

engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI dependency: yields a request-scoped session, always closed after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
