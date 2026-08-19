"""Password hashing and signed session cookies.

Uses PBKDF2-HMAC-SHA256 from the standard library rather than bcrypt/argon2 so
the app has no compiled dependencies. 260k iterations is above the OWASP floor
for PBKDF2-SHA256. The stored format carries its own parameters, so raising the
iteration count later re-hashes users transparently on next login.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 260_000
_SALT_BYTES = 16


def hash_password(password: str) -> str:
    salt = os.urandom(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: Optional[str]) -> bool:
    if not stored:
        return False
    try:
        algo, iterations, salt_hex, hash_hex = stored.split("$")
        if algo != _ALGO:
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    # Constant-time comparison: a plain == leaks timing information.
    return hmac.compare_digest(dk.hex(), hash_hex)


def needs_rehash(stored: Optional[str]) -> bool:
    if not stored:
        return True
    try:
        algo, iterations, _, _ = stored.split("$")
    except ValueError:
        return True
    return algo != _ALGO or int(iterations) < _ITERATIONS


def check_password_strength(password: str) -> Optional[str]:
    """Return an error message, or None if acceptable."""
    if len(password) < 8:
        return "Please use at least 8 characters."
    if password.lower() in ("password", "12345678", "qwertyui", "tayseer1"):
        return "That password is too easy to guess."
    return None


# --- Session cookie ----------------------------------------------------------

_serializer = URLSafeTimedSerializer(settings.secret_key, salt="tayseer-session")


def make_session_token(user_id: int) -> str:
    return _serializer.dumps({"uid": user_id})


def read_session_token(token: Optional[str]) -> Optional[int]:
    if not token:
        return None
    try:
        data = _serializer.loads(token, max_age=settings.session_max_age)
    except (BadSignature, SignatureExpired):
        return None
    uid = data.get("uid")
    return int(uid) if uid is not None else None


# --- CSRF --------------------------------------------------------------------
# Every mutating form carries a token tied to the session cookie. Combined with
# SameSite=Lax on the session cookie this covers cross-site form posts.

_csrf_serializer = URLSafeTimedSerializer(settings.secret_key, salt="tayseer-csrf")


def make_csrf_token(session_token: str) -> str:
    digest = hashlib.sha256(session_token.encode()).hexdigest()[:32]
    return _csrf_serializer.dumps(digest)


def verify_csrf_token(token: Optional[str], session_token: Optional[str]) -> bool:
    if not token or not session_token:
        return False
    try:
        digest = _csrf_serializer.loads(token, max_age=settings.session_max_age)
    except (BadSignature, SignatureExpired):
        return False
    expected = hashlib.sha256(session_token.encode()).hexdigest()[:32]
    return hmac.compare_digest(str(digest), expected)


# --- Password reset links ----------------------------------------------------
# No reset email in this deployment: a Muhaffiz generates a link and hands it
# over. The token is high-entropy and single-use, and only its hash is stored.

RESET_TOKEN_HOURS = 72


def make_reset_token() -> str:
    """A URL-safe token. 32 bytes is well beyond guessing range."""
    return secrets.token_urlsafe(32)


def hash_reset_token(token: str) -> str:
    """SHA-256 is right here, not PBKDF2.

    The token is already 256 bits of entropy from a CSPRNG, so there is no
    dictionary to slow an attacker down against — the stretching that protects
    a human-chosen password buys nothing, and a fast hash keeps lookup cheap.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
