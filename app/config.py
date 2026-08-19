"""Configuration.

Everything is environment-driven so the same image runs locally on SQLite and
in production on Postgres with only DATABASE_URL changing.
"""

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _str(name: str, default: str = "") -> str:
    """Read an environment variable, stripping surrounding whitespace.

    Pasting a value into a hosting dashboard picks up stray whitespace far more
    often than anyone expects — a leading tab on DATABASE_URL took this app down
    on its first deploy, because SQLAlchemy could not parse the scheme and the
    error named a URL that looked perfectly correct. A trailing newline on
    CRON_SECRET would be worse: no crash, just an endpoint that quietly rejects
    every cron invocation forever.

    Nothing here has meaningful leading or trailing whitespace, so stripping is
    always safe and never ambiguous.
    """
    raw = os.environ.get(name)
    return default if raw is None else raw.strip()


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(_str(name) or default)
    except ValueError:
        return default


def _normalize_db_url(raw: str) -> str:
    """Make a hosted Postgres URL usable by SQLAlchemy + psycopg 3.

    Neon, Supabase, Vercel Postgres and Heroku all hand out URLs beginning
    `postgres://` or `postgresql://`. SQLAlchemy rejects the first outright and
    resolves the second to psycopg2, which is not installed here. Rewriting the
    scheme is the single most common deployment footgun for this stack, so it is
    handled rather than documented.
    """
    raw = raw.strip()
    if raw.startswith("postgres://"):
        raw = "postgresql+psycopg://" + raw[len("postgres://"):]
    elif raw.startswith("postgresql://"):
        raw = "postgresql+psycopg://" + raw[len("postgresql://"):]
    return raw


@dataclass
class Settings:
    app_name: str = "Tayseer"
    tagline: str = "Hifz tracker"

    # sqlite:///./tayseer.db locally; postgresql+psycopg://... in production.
    database_url: str = _normalize_db_url(
        _str("DATABASE_URL", f"sqlite:///{BASE_DIR / 'tayseer.db'}")
    )

    # Set automatically by Vercel. Switches on serverless-safe behaviour:
    # no connection pooling, and no schema creation on cold start.
    serverless: bool = bool(os.environ.get("VERCEL"))
    # Secret Vercel sends as `Authorization: Bearer <value>` on cron invocations.
    cron_secret: str = _str("CRON_SECRET")
    # Create tables on startup. Safe for local SQLite; off in production, where
    # schema changes belong in a deliberate migration step.
    auto_migrate: bool = _bool("AUTO_MIGRATE", False)

    # Signs the session cookie. MUST be set in production.
    secret_key: str = _str("SECRET_KEY", "dev-secret-change-me")
    session_cookie: str = "tayseer_session"
    session_max_age: int = 60 * 60 * 24 * 30  # 30 days

    # Cookies are only marked Secure when actually served over HTTPS, otherwise
    # local development over http silently loses the session.
    secure_cookies: bool = _bool("SECURE_COOKIES", False)

    # Email. Unset SMTP -> the console adapter, which logs the message instead
    # of sending. That keeps development from quietly mailing real students.
    smtp_host: str = _str("SMTP_HOST")
    smtp_port: int = _int("SMTP_PORT", 587)
    smtp_user: str = _str("SMTP_USER")
    smtp_password: str = _str("SMTP_PASSWORD")
    smtp_from: str = _str("SMTP_FROM", "Tayseer <noreply@example.com>")
    smtp_tls: bool = _bool("SMTP_TLS", True)

    default_timezone: str = _str("DEFAULT_TIMEZONE", "UTC")
    debug: bool = _bool("DEBUG", True)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_host)


settings = Settings()
