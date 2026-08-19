"""Tests for the forgiving-streak design.

Forgiving must not mean meaningless. These pin down both halves: an occasional
miss is absorbed, and a cluster of misses is not.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.domain.dates import ALL_DAYS_ON
from app.domain.streak import compute_streak

TODAY = date(2026, 8, 17)  # a Monday


def days_back(n, skip=()):
    """The last `n` days ending today, minus any offsets in `skip`."""
    return {TODAY - timedelta(days=i) for i in range(n) if i not in skip}


def test_unbroken_run():
    s = compute_streak(days_back(20), active_days=ALL_DAYS_ON, today=TODAY)
    assert s.current_run == 20
    assert s.grace_used == 0
    assert s.logged_today is True


def test_today_still_open_does_not_break_the_run():
    """Not having logged *yet* today is not a miss — the day is not over."""
    logged = {TODAY - timedelta(days=i) for i in range(1, 11)}
    s = compute_streak(logged, active_days=ALL_DAYS_ON, today=TODAY)
    assert s.current_run == 10
    assert s.logged_today is False
    assert "today is still open" in s.headline


def test_single_miss_is_absorbed():
    s = compute_streak(days_back(20, skip={4}), active_days=ALL_DAYS_ON, today=TODAY)
    assert s.grace_used == 1
    assert s.current_run == 19
    assert "absorbed" in s.support_line


def test_off_days_are_not_misses():
    """Fridays off. Never logging on a Friday must not consume grace."""
    mask = "1111011"  # index 4 = Friday
    logged = {
        TODAY - timedelta(days=i)
        for i in range(30)
        if (TODAY - timedelta(days=i)).weekday() != 4
    }
    s = compute_streak(logged, active_days=mask, today=TODAY)
    assert s.grace_used == 0
    assert s.current_run == len(logged)


def test_clustered_misses_end_the_run():
    """Five consecutive misses is a break, however long the run before it.

    This is the case the earlier cumulative-allowance design got wrong: it let a
    long run bank enough grace to absorb a whole missed week.
    """
    logged = days_back(60, skip={5, 6, 7, 8, 9})
    s = compute_streak(logged, active_days=ALL_DAYS_ON, today=TODAY)
    assert s.current_run == 5  # the run ends at the cluster
    assert s.grace_used == 1  # only the first of the cluster was absorbed


def test_two_misses_inside_one_week_end_the_run():
    """The first miss is absorbed; the second one, 3 days later, is not.

    The run therefore covers days 0-2 and 4-5 — five logged days — and stops at
    the second miss rather than continuing back through the remaining 34.
    """
    logged = days_back(40, skip={3, 6})
    s = compute_streak(logged, active_days=ALL_DAYS_ON, today=TODAY)
    assert s.current_run == 5
    assert s.grace_used == 1


def test_well_spaced_misses_are_all_absorbed():
    """One miss a week keeps the run — that is the intended generosity."""
    logged = days_back(40, skip={7, 15, 23, 31})
    s = compute_streak(logged, active_days=ALL_DAYS_ON, today=TODAY)
    assert s.grace_used == 4
    assert s.current_run == 36


def test_no_history_is_never_phrased_as_failure():
    s = compute_streak(set(), active_days=ALL_DAYS_ON, today=TODAY)
    assert s.current_run == 0
    assert s.consistency_percent == 0
    assert "Log your first revision" in s.headline
    for text in (s.headline, s.support_line):
        for word in ("lost", "broken", "failed", "streak lost"):
            assert word not in text.lower()


def test_broken_run_invites_a_restart_rather_than_reporting_a_loss():
    logged = {TODAY - timedelta(days=i) for i in range(10, 25)}
    s = compute_streak(logged, active_days=ALL_DAYS_ON, today=TODAY)
    assert s.current_run == 0
    assert "start a new run" in s.headline
    assert s.longest_run >= 14  # the achievement is still on the record


def test_consistency_counts_only_active_days():
    """A 4-day week with all 4 logged is 100%, not 57%."""
    mask = "1111000"  # Mon-Thu
    logged = {
        TODAY - timedelta(days=i) for i in range(30) if (TODAY - timedelta(days=i)).weekday() < 4
    }
    s = compute_streak(logged, active_days=mask, today=TODAY)
    assert s.consistency_percent == 100
    assert s.active_days_in_window < 30


def test_milestones_and_next_target():
    s = compute_streak(days_back(9), active_days=ALL_DAYS_ON, today=TODAY)
    assert 7 in s.milestones
    assert s.next_milestone == 14
    assert s.to_next_milestone == 5
