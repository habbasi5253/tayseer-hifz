"""Tests for the on-track metrics: pace, attendance, per-juz progress."""

from __future__ import annotations

from datetime import date, timedelta

from app.domain.tracking import page_pace, projected_finish, weekly_attendance

MON = date(2026, 8, 17)  # a Monday


def days(*offsets, start=date(2026, 7, 1)):
    return [start + timedelta(days=o) for o in offsets]


# --- Pace --------------------------------------------------------------------


def test_pace_needs_a_few_pages_before_it_will_guess():
    assert page_pace(days(0, 3)).has_pace is False
    assert "Not enough pages" in page_pace(days(0, 3)).summary


def test_steady_pace_is_the_median_gap():
    p = page_pace(days(0, 2, 4, 6, 8))
    assert p.days_per_page == 2
    assert p.days_per_juz == 40
    assert "2.0 days a page" in p.summary


def test_a_long_break_does_not_wreck_the_measured_pace():
    """The median survives a holiday; a span average would not.

    Eight pages a day apart with one three-week gap in the middle is still a
    student doing a page a day — showing them 3.5 would be wrong and demoralising.
    """
    p = page_pace(days(0, 1, 2, 3, 24, 25, 26, 27))
    assert p.days_per_page == 1
    assert p.slowest_gap == 21


def test_same_day_batches_fall_back_to_the_mean():
    """A zero median means pages come in batches, so days-per-page is meaningless."""
    p = page_pace(days(0, 0, 0, 7, 7, 7, 14, 14, 14))
    assert p.days_per_page > 0
    assert p.days_per_juz is not None


def test_a_pace_of_zero_never_divides_by_zero():
    p = page_pace(days(0, 0, 0, 0))
    assert p.days_per_juz is None
    assert "too fast to measure" in p.summary
    assert projected_finish(pages_remaining=10, pace=p, today=MON) is None


def test_projection_uses_the_measured_pace():
    p = page_pace(days(0, 2, 4, 6))
    assert projected_finish(pages_remaining=5, pace=p, today=MON) == MON + timedelta(days=10)
    assert projected_finish(pages_remaining=0, pace=p, today=MON) is None


# --- Attendance --------------------------------------------------------------


def test_only_days_that_have_happened_count_as_expected():
    """Nobody should be marked as missing Thursday's class on Monday morning."""
    a = weekly_attendance(planned_weekdays=[0, 2, 4], class_dates=[MON], today=MON, weeks=1)
    week = a.weeks[-1]
    assert week.expected == 1     # only Monday has passed
    assert week.attended == 1
    assert week.missed == 0


def test_missed_classes_are_counted_per_week():
    a = weekly_attendance(
        planned_weekdays=[0, 2, 4],
        class_dates=[MON - timedelta(days=7), MON - timedelta(days=5)],
        today=MON,
        weeks=2,
    )
    last_week = a.weeks[0]
    assert last_week.expected == 3
    assert last_week.attended == 2
    assert last_week.missed == 1
    assert last_week.percent == 67


def test_no_schedule_means_nothing_to_miss():
    """An empty plan must not read as 100% missed."""
    a = weekly_attendance(planned_weekdays=[], class_dates=[], today=MON, weeks=4)
    assert a.expected == 0
    assert a.missed == 0
    assert a.percent == 0
    assert "nothing to attend against" in a.summary


def test_a_class_on_an_unscheduled_day_does_not_inflate_attendance():
    """Extra effort is welcome but cannot exceed what was scheduled."""
    a = weekly_attendance(
        planned_weekdays=[0],
        class_dates=[MON, MON - timedelta(days=1), MON - timedelta(days=2)],
        today=MON,
        weeks=1,
    )
    assert a.expected == 1
    assert a.attended == 1
    assert a.percent == 100


def test_weeks_run_most_recent_last():
    a = weekly_attendance(planned_weekdays=[0], class_dates=[], today=MON, weeks=3)
    assert [w.week_start for w in a.weeks] == sorted(w.week_start for w in a.weeks)
    assert a.weeks[-1].week_start == MON  # today's week is a Monday
    assert a.this_week is a.weeks[-1]
