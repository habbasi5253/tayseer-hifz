"""Tests for the 30-day rule: one deadline per juz, restart on expiry.

The bugs this file exists to prevent are drift (a juz silently gaining or
losing a day through DST or timezone confusion) and a wrong reset (an expired
attempt still counting, or a passed juz being dragged back under a clock).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.domain import dates as dt
from app.domain.quran import global_page as g
from app.domain.quran import juz_pages
from app.domain.revalidation import (
    AT_RISK_DAYS,
    WINDOW_DAYS,
    Status,
    build_board,
    build_window,
    deadline_warnings,
)

UTC = timezone.utc


def at(y, m, d, hh=12, mm=0, tz_name="UTC"):
    return datetime(y, m, d, hh, mm, tzinfo=dt.get_zone(tz_name)).astimezone(UTC)


def win(**kw):
    kw.setdefault("juz", 3)
    kw.setdefault("tz_name", "UTC")
    kw.setdefault("window_started_at", at(2026, 1, 1))
    # Pin "now" inside the window by default. Letting it fall back to the real
    # clock made every unpinned window silently expired.
    kw.setdefault("now", at(2026, 1, 5))
    return build_window(**kw)


# --- The deadline ------------------------------------------------------------


@pytest.mark.parametrize(
    "elapsed,expected,remaining",
    [
        (0, Status.IN_PROGRESS, 30),
        (22, Status.IN_PROGRESS, 8),
        (23, Status.DUE_SOON, 7),      # AT_RISK_DAYS boundary
        (29, Status.DUE_SOON, 1),
        (30, Status.DUE_TODAY, 0),     # last day still counts
        (31, Status.EXPIRED, -1),      # first day outside
    ],
)
def test_window_boundaries(elapsed, expected, remaining):
    started = at(2026, 1, 1, 9)
    w = win(window_started_at=started, now=started + timedelta(days=elapsed))
    assert w.status == expected
    assert w.days_remaining == remaining
    assert w.deadline == dt.local_date(started, "UTC") + timedelta(days=WINDOW_DAYS)


def test_at_risk_boundary_is_inclusive():
    started = at(2026, 1, 1)
    w = win(window_started_at=started, now=started + timedelta(days=WINDOW_DAYS - AT_RISK_DAYS))
    assert w.days_remaining == AT_RISK_DAYS
    assert w.status == Status.DUE_SOON


# --- Calendar days, not 24-hour blocks --------------------------------------


def test_late_night_start_counts_a_full_day_next_morning():
    """11pm Monday -> 8am Tuesday is 1 day, not 0."""
    tz = "America/Chicago"
    started = at(2026, 3, 2, 23, 0, tz)
    w = win(tz_name=tz, window_started_at=started, now=at(2026, 3, 3, 8, 0, tz))
    assert w.days_elapsed == 1
    assert w.days_remaining == 29


def test_dst_spring_forward_does_not_shorten_the_window():
    """A 23-hour day is still one day. US DST begins 8 March 2026."""
    tz = "America/Chicago"
    started, now = at(2026, 3, 5, 12, 0, tz), at(2026, 4, 4, 12, 0, tz)
    w = win(tz_name=tz, window_started_at=started, now=now)
    assert w.days_elapsed == 30
    assert w.status == Status.DUE_TODAY
    # what a naive seconds-based implementation would have said
    assert (now - started).days == 29


def test_dst_fall_back_does_not_lengthen_the_window():
    """A 25-hour day is also one day. US DST ends 1 November 2026."""
    tz = "America/Chicago"
    w = win(tz_name=tz, window_started_at=at(2026, 10, 20, 12, 0, tz),
            now=at(2026, 11, 19, 12, 0, tz))
    assert w.days_elapsed == 30
    assert w.status == Status.DUE_TODAY


def test_the_students_timezone_decides_not_the_viewers():
    """A Muhaffiz abroad sees the deadline the student actually lives under."""
    started = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)   # 1 May in both zones
    now = datetime(2026, 5, 31, 22, 0, tzinfo=UTC)     # 31 May UTC, 1 Jun in NZ
    assert win(tz_name="UTC", window_started_at=started, now=now).status == Status.DUE_TODAY
    assert win(tz_name="Pacific/Auckland", window_started_at=started, now=now).status == Status.EXPIRED


# --- Progress through the juz ------------------------------------------------


def test_tasmee_runs_in_order_from_page_one():
    w = win(passed_pages=[g(3, 1), g(3, 2), g(3, 3)])
    assert w.passed_count == 3
    assert w.pages_remaining == 17
    assert w.next_page == g(3, 4)
    assert w.percent == 15


def test_a_finished_juz_awaits_only_the_muhaffiz():
    w = win(passed_pages=juz_pages(3))
    assert w.pages_remaining == 0
    assert w.next_page is None
    assert "waiting on your muhaffiz" in w.pace_text.lower()
    assert w.is_passed is False  # recited is not the same as passed


def test_pace_text_says_what_finishing_actually_takes():
    started = at(2026, 1, 1)
    w = win(window_started_at=started, now=started + timedelta(days=24),
            passed_pages=[g(3, i) for i in range(1, 13)])
    assert w.days_remaining == 6
    assert "8 pages in 6 days" in w.pace_text


# --- Expiry resets the attempt ----------------------------------------------


def test_expiry_restarts_tasmee_from_page_one():
    """The whole point of the rule: run out of time and you start again."""
    started = at(2026, 1, 1)
    w = win(window_started_at=started, now=started + timedelta(days=35),
            passed_pages=[g(3, i) for i in range(1, 18)])
    assert w.is_expired is True
    assert "restarts from page 1" in w.headline
    assert w.page_status(g(3, 5)) == "passed"  # history is still visible...
    assert w.page_status(g(3, 20)) == "reset"  # ...but the attempt is over


def test_a_new_attempt_only_counts_pages_from_its_own_window():
    """After a restart the earlier passes are excluded by the caller.

    The window itself is told only about pages in the current attempt, which is
    what `services.pages_passed_in_window` scopes by window start.
    """
    restarted = at(2026, 2, 5)
    w = win(window_started_at=restarted, attempt=2, passed_pages=[g(3, 1)],
            now=restarted + timedelta(days=2))
    assert w.attempt == 2
    assert w.passed_count == 1
    assert w.next_page == g(3, 2)
    assert w.status == Status.IN_PROGRESS


def test_a_passed_juz_is_never_expired():
    """Once Muhaffiz 2 passes it the clock stops for good."""
    started = at(2026, 1, 1)
    w = win(window_started_at=started, passed_at=at(2026, 1, 20),
            now=started + timedelta(days=400), passed_pages=juz_pages(3))
    assert w.status == Status.PASSED
    assert w.is_expired is False
    assert w.needs_attention is False


def test_a_juz_still_with_muhaffiz_one_has_no_clock():
    w = win(window_started_at=None, now=at(2026, 6, 1))
    assert w.status == Status.NOT_STARTED
    assert w.days_remaining is None
    assert w.needs_attention is False
    assert "Stage 1 Muhaffiz" in w.headline


# --- Board -------------------------------------------------------------------


def test_board_surfaces_the_one_running_clock():
    now = at(2026, 6, 15)
    passed = build_window(juz=1, tz_name="UTC", window_started_at=at(2026, 3, 1),
                          passed_at=at(2026, 3, 20), passed_pages=juz_pages(1), now=now)
    active = build_window(juz=2, tz_name="UTC", window_started_at=now - timedelta(days=26),
                          passed_pages=[g(2, i) for i in range(1, 15)], now=now)
    board = build_board(tz_name="UTC", windows=[passed, active], now=now)

    assert board.active is active
    assert [w.juz for w in board.passed] == [1]
    assert [w.juz for w in board.at_risk] == [2]
    assert "6 pages in 4 days" in board.headline


def test_board_reads_calmly_with_nothing_in_tasmee():
    board = build_board(tz_name="UTC", windows=[], now=at(2026, 1, 1))
    assert board.active is None
    assert "No juz in tasmee yet" in board.headline


def test_warnings_fire_on_thresholds_only():
    now = at(2026, 6, 15)
    windows = [
        build_window(juz=j, tz_name="UTC", window_started_at=now - timedelta(days=WINDOW_DAYS - i),
                     now=now)
        for j, i in enumerate(range(0, 10), start=1)
    ]
    leads = sorted(w["lead"] for w in deadline_warnings(build_board(
        tz_name="UTC", windows=windows, now=now)))
    assert leads == [0, 1, 3, 7]
