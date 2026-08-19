"""Guards on the migration setup.

These are cheap structural checks, not a substitute for running a migration.
They exist because the failure they catch is silent: `create_all` reports
success while skipping every change to an existing table, and nothing surfaces
until a query hits the missing column in production.
"""

from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def app_sources():
    return list((ROOT / "app").rglob("*.py")) + list((ROOT / "scripts").rglob("*.py"))


def test_no_create_all_call_anywhere_in_the_app():
    """The bug this replaced. It must not creep back in.

    Parsed rather than grepped: `db.py` deliberately names `create_all` in a
    docstring explaining why it is not used, and a text search would flag that
    forever.
    """
    import ast

    offenders = []
    for path in app_sources():
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "create_all"
            ):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == [], f"create_all() called in {offenders}"


def test_there_is_at_least_one_migration():
    versions = list((ROOT / "migrations" / "versions").glob("*.py"))
    assert versions, "no migration scripts — the schema has no recorded history"


def test_migrations_form_a_single_chain():
    """Two heads means `upgrade head` is ambiguous and will fail."""
    import re

    revs, downs = {}, {}
    for p in (ROOT / "migrations" / "versions").glob("*.py"):
        src = p.read_text()
        rev = re.search(r'^revision:\s*str\s*=\s*["\']([^"\']+)', src, re.M)
        down = re.search(r'^down_revision:\s*[^=]*=\s*(?:["\']([^"\']+)["\']|None)', src, re.M)
        assert rev, f"{p.name} has no revision id"
        revs[rev.group(1)] = p.name
        downs[rev.group(1)] = down.group(1) if down and down.group(1) else None

    heads = set(revs) - {d for d in downs.values() if d}
    assert len(heads) == 1, f"expected one head, found {len(heads)}: {heads}"

    roots = [r for r, d in downs.items() if d is None]
    assert len(roots) == 1, f"expected one base revision, found {roots}"


def test_env_uses_batch_mode_for_sqlite():
    """Without it, column changes work on Postgres and fail on SQLite."""
    env = (ROOT / "migrations" / "env.py").read_text()
    assert env.count("render_as_batch=True") >= 2, "batch mode missing from a context.configure"


def test_env_takes_its_url_from_the_app_config():
    """So a migration can never target a different database than the app."""
    env = (ROOT / "migrations" / "env.py").read_text()
    assert "from app.config import settings" in env
    assert "settings.database_url" in env
    ini = (ROOT / "alembic.ini").read_text()
    assert "sqlalchemy.url" not in ini, "a hardcoded URL in alembic.ini would override it"


def test_alembic_is_a_production_dependency():
    """It runs at startup locally and from scripts/migrate.py in production."""
    assert "alembic" in (ROOT / "requirements.txt").read_text()
