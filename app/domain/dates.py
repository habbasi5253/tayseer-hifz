"""Timezone-aware date math.

This module exists because the 30-day rule cannot be allowed to drift, and
every realistic way of getting it wrong is a date-math bug. The rules it
enforces:

1. Every timestamp that hits the database is timezone-aware UTC. There are no
   naive datetimes anywhere past this boundary. `utcnow()` is the only clock.

2. "Days" always means *calendar days in the student's own timezone*, never
   elapsed 24-hour periods. If a student tasmee's a page at 11pm Monday and
   checks the app at 8am Tuesday, that is 1 day, not 0. Counting 86400-second
   blocks would say 0 and silently buy them an extra day on every page.

3. Because the unit is calendar days in a named IANA zone, DST transitions are
   free. A 23-hour or 25-hour day is still exactly one day. Subtracting UTC
   instants and dividing by 86400 would drift by a day twice a year.

4. The timezone used is always the *student's*, never the viewer's. A Muhaffiz
   in Karachi looking at a student in Chicago must see the deadline the student
   is actually living under. Every function here takes the zone explicitly, so
   there is no ambient "local time" to accidentally inherit.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

try:  # Python 3.9+
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - we require 3.9+
    raise RuntimeError("Python 3.9+ with zoneinfo is required")

UTC = timezone.utc
DEFAULT_TIMEZONE = "UTC"


class InvalidTimezone(ValueError):
    """Raised when a stored timezone string is not a valid IANA zone."""


def utcnow() -> datetime:
    """The single clock for the whole application.

    Everything that needs "now" calls this, which makes the entire system
    trivially freezable in tests by monkeypatching one symbol.
    """
    return datetime.now(tz=UTC)


def get_zone(tz_name: Optional[str]) -> ZoneInfo:
    """Resolve an IANA timezone name, falling back to UTC for empty input.

    An unknown zone raises rather than silently falling back: a student whose
    timezone got corrupted should surface loudly, not quietly start being
    measured against the wrong deadline.
    """
    if not tz_name:
        return ZoneInfo(DEFAULT_TIMEZONE)
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidTimezone(f"unknown timezone {tz_name!r}") from exc


def is_valid_timezone(tz_name: str) -> bool:
    try:
        get_zone(tz_name)
        return True
    except InvalidTimezone:
        return False


def ensure_utc(dt: datetime) -> datetime:
    """Coerce a datetime to aware UTC.

    Naive datetimes are treated as UTC rather than as local time. Anything
    reaching this function naive came from a legacy row or a bad caller; the
    alternative (guessing the server's local zone) is how drift starts.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def local_date(dt: datetime, tz_name: Optional[str]) -> date:
    """The calendar date an instant fell on, in the given timezone."""
    return ensure_utc(dt).astimezone(get_zone(tz_name)).date()


def today_local(tz_name: Optional[str], now: Optional[datetime] = None) -> date:
    """Today's calendar date in the given timezone."""
    return local_date(now or utcnow(), tz_name)


def start_of_local_day(day: date, tz_name: Optional[str]) -> datetime:
    """Midnight at the start of `day` in `tz_name`, returned as aware UTC."""
    zone = get_zone(tz_name)
    return datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)


def days_between(earlier: date, later: date) -> int:
    """Whole calendar days from `earlier` to `later`. Negative if reversed."""
    return (later - earlier).days


def days_since(dt: datetime, tz_name: Optional[str], now: Optional[datetime] = None) -> int:
    """Calendar days elapsed since `dt`, measured in `tz_name`.

    Same local day -> 0. Yesterday -> 1. This is the workhorse behind both the
    30-day rule and "days since last tasmee" in the Muhaffiz console.
    """
    return days_between(local_date(dt, tz_name), today_local(tz_name, now))


def add_days(day: date, n: int) -> date:
    return day + timedelta(days=n)


def format_local(dt: Optional[datetime], tz_name: Optional[str], fmt: str = "%d %b %Y") -> str:
    if dt is None:
        return "—"
    return ensure_utc(dt).astimezone(get_zone(tz_name)).strftime(fmt)


def humanize_days(n: Optional[int]) -> str:
    """Encouraging, plain-language day counts for the UI."""
    if n is None:
        return "not yet"
    if n <= 0:
        return "today"
    if n == 1:
        return "yesterday"
    if n < 7:
        return f"{n} days ago"
    if n < 14:
        return "last week"
    if n < 30:
        return f"{n // 7} weeks ago"
    if n < 60:
        return "last month"
    return f"{n // 30} months ago"


# Weekday helpers -------------------------------------------------------------
# Stored as a 7-character string of "0"/"1", index 0 = Monday, matching
# date.weekday(). The guide is emphatic that a day off must stay a day off and
# not silently become catch-up day, so off-days are first-class in scheduling.

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEKDAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
ALL_DAYS_ON = "1111111"


def normalize_active_days(mask: Optional[str]) -> str:
    if not mask or len(mask) != 7 or any(c not in "01" for c in mask):
        return ALL_DAYS_ON
    return mask


def is_active_day(mask: Optional[str], day: date) -> bool:
    return normalize_active_days(mask)[day.weekday()] == "1"


def active_days_per_week(mask: Optional[str]) -> int:
    return normalize_active_days(mask).count("1")


def describe_off_days(mask: Optional[str]) -> str:
    m = normalize_active_days(mask)
    off = [WEEKDAY_NAMES[i] for i, c in enumerate(m) if c == "0"]
    if not off:
        return "no days off"
    if len(off) == 1:
        return f"{off[0]}s off"
    return ", ".join(off[:-1]) + f" and {off[-1]} off"
