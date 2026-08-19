"""Guards on environment-variable handling.

Written after a deploy failure: a leading tab on DATABASE_URL, picked up when
pasting into a hosting dashboard, crashed the app at import with an error that
displayed a URL looking entirely correct.
"""

from __future__ import annotations

import importlib
import sys

import pytest
from sqlalchemy.engine.url import make_url


def load_settings(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for name in [m for m in sys.modules if m.startswith("app.")]:
        del sys.modules[name]
    return importlib.import_module("app.config").settings


@pytest.mark.parametrize(
    "raw",
    [
        "\tpostgresql://u:p@host/db",          # the tab that broke production
        "  postgresql://u:p@host/db",
        "postgresql://u:p@host/db\n",
        " \npostgresql://u:p@host/db \t",
    ],
)
def test_whitespace_around_the_database_url_is_survivable(monkeypatch, raw):
    s = load_settings(monkeypatch, DATABASE_URL=raw)
    assert make_url(s.database_url).drivername == "postgresql+psycopg"


def test_hosted_postgres_schemes_are_rewritten_for_psycopg3(monkeypatch):
    """Neon, Supabase and Heroku all hand out a scheme SQLAlchemy cannot use."""
    for raw in ("postgres://u:p@host/db", "postgresql://u:p@host/db"):
        s = load_settings(monkeypatch, DATABASE_URL=raw)
        assert s.database_url.startswith("postgresql+psycopg://")
        assert s.is_sqlite is False


def test_a_padded_cron_secret_still_matches(monkeypatch):
    """This one fails silently: every cron invocation rejected, no crash."""
    s = load_settings(monkeypatch, CRON_SECRET="  s3cret\n")
    assert s.cron_secret == "s3cret"


def test_padded_booleans_and_ints(monkeypatch):
    s = load_settings(monkeypatch, SECURE_COOKIES=" 1 ", DEBUG=" false ", SMTP_PORT=" 2525 ")
    assert s.secure_cookies is True
    assert s.debug is False
    assert s.smtp_port == 2525


def test_a_nonsense_port_falls_back_rather_than_crashing(monkeypatch):
    assert load_settings(monkeypatch, SMTP_PORT="not-a-number").smtp_port == 587


def test_sqlite_is_the_default_without_a_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for name in [m for m in sys.modules if m.startswith("app.")]:
        del sys.modules[name]
    s = importlib.import_module("app.config").settings
    assert s.is_sqlite is True
