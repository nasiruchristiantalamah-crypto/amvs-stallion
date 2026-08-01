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
    Base.metadata.create_all() (called once at API startup, via init_db()
    below) is enough to stand up the initial schema, but it can't apply
    migrations to an already-populated table (e.g. adding a column later).
    If/when the schema needs to change after go-live, introduce Alembic
    then — premature to add migration tooling before there's anything to
    migrate.

init_db() never raises:
    A database that's unreachable at boot (wrong DATABASE_URL, the
    Postgres plugin not yet attached on Railway, a network hiccup) must
    NOT take the whole app down. Before this was guarded, create_all()
    raising during FastAPI's lifespan startup meant uvicorn logged
    "Application startup failed. Exiting." and never bound to a port at
    all — every route 502'd, including /health, because nothing was
    listening. init_db() catches and logs instead, so the app always
    starts; routes that actually need the database (auth, valuation-run
    logging) then fail on their own with a clear per-request error instead
    of a total outage. See api/main.py's lifespan.
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

print(f"STARTUP: db/database.py loaded — DATABASE_URL scheme = "
      f"{DATABASE_URL.split('://', 1)[0] if '://' in DATABASE_URL else '(unrecognised)'}")

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


def init_db() -> None:
    """
    Create users/clients/valuation_runs if they don't already exist.
    Deliberately swallows and logs any failure rather than raising — see
    the module docstring's "init_db() never raises" section for why. Call
    this from FastAPI's lifespan startup (api/main.py); do not call
    Base.metadata.create_all() directly from anywhere else.
    """
    print("STARTUP: init_db() — about to call Base.metadata.create_all()...")
    try:
        Base.metadata.create_all(bind=engine)
        print("STARTUP: init_db() — tables created/verified OK.")
    except Exception as e:
        print(f"STARTUP WARNING: init_db() failed — {type(e).__name__}: {e}")
        print("STARTUP WARNING: the app will still start, but every route that touches the "
              "database (login, register, and valuation-run logging on /pricing, /ifrs17, etc.) "
              "will fail until DATABASE_URL is fixed and the service is redeployed or restarted.")
