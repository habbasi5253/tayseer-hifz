"""Create the schema against whatever DATABASE_URL points at.

Deliberately a separate, deliberate step rather than something the app does on
startup: on serverless the startup hook runs on every cold start, and concurrent
cold starts issuing CREATE TABLE at each other is a real race.

    DATABASE_URL=postgresql://... python -m scripts.migrate

Runs the Alembic migrations in `migrations/`, so it is safe against a database
that already holds data — unlike `create_all`, which silently skips any change
to a table it already sees.

    alembic revision --autogenerate -m "what changed"   # after editing models
    alembic upgrade head                                 # or run this script
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.db import run_migrations  # noqa: E402

if __name__ == "__main__":
    target = settings.database_url
    # Never print credentials back to a terminal or a CI log.
    safe = target.split("@")[-1] if "@" in target else target
    print(f"Migrating {safe} to head ...")
    run_migrations()
    print("Done.")
