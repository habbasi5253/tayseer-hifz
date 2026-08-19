"""Database engine and session handling."""

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import BASE_DIR, settings
from app.models import Base

connect_args = {}
engine_kwargs = {}

if settings.is_sqlite:
    # FastAPI serves requests on a threadpool; SQLite objects would otherwise
    # refuse to cross threads.
    connect_args["check_same_thread"] = False

if settings.serverless:
    # Every serverless invocation is its own short-lived process. A pool here
    # is worse than useless: idle connections are never reused across
    # invocations, but they do count against Postgres' connection limit, and a
    # busy function fleet will exhaust it. NullPool opens one connection and
    # closes it with the request.
    #
    # This still assumes a *pooled* DATABASE_URL (Neon's -pooler host, Supabase's
    # port 6543, Vercel Postgres' pooled string). Pointing a serverless function
    # at a direct Postgres connection will hit max_connections under load.
    engine_kwargs["poolclass"] = NullPool
else:
    engine_kwargs["pool_pre_ping"] = True

engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    future=True,
    **engine_kwargs,
)


if settings.is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover
        cur = dbapi_connection.cursor()
        # ON DELETE CASCADE is off by default in SQLite, which would leave
        # orphaned logs behind a deleted student.
        cur.execute("PRAGMA foreign_keys=ON")
        # WAL keeps a long-running report export from blocking a student's log.
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def run_migrations() -> None:
    """Bring the database up to the latest revision.

    Deliberately not `Base.metadata.create_all()`. That only ever creates tables
    that do not exist — it will not add a column to a table it already sees, and
    it reports success while doing nothing. Every schema change would then be a
    silent no-op followed by a runtime error on the missing column, which is
    exactly what forced this database to be wiped and reseeded during
    development. Alembic applies the versioned scripts in `migrations/`, so a
    change is reviewable in a diff and reversible.
    """
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "migrations"))
    command.upgrade(cfg, "head")


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """For scripts and background jobs."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
