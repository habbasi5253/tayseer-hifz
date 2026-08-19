"""Tests for suggested times and the habit strip."""

from __future__ import annotations

from datetime import date, time

from app.domain.dayplan import (
    EVENING,
    FAJR,
    build_day_schedule,
    build_week_strip,
)
from app.domain.schedule import TimeBudget, build_daily_plan


def plan(**kw):
    kw.setdefault("budget", TimeBudget(120))
    kw.setdefault("murajaat", [(1, "full")])
    kw.setdefault("tasmee_juz", 3)
    return build_daily_plan(**kw)


def test_new_hifz_lands_in_the_freshest_slot():
    """The guide is explicit that memorizing while tired does not register."""
    day = build_day_schedule(plan(), wake_hour=6, evening_hour=20)
    hifz = next(b for b in day.blocks if b.key == "new_hifz")
    assert hifz.slot is FAJR
    assert hifz.start.hour == 6


def test_murajaat_and_tasmee_go_to_the_evening():
    day = build_day_schedule(plan(), wake_hour=6, evening_hour=20)
    for key in ("murajaat", "tasmee"):
        assert next(b for b in day.blocks if b.key == key).slot is EVENING


def test_blocks_in_one_slot_do_not_overlap():
    day = build_day_schedule(plan(), wake_hour=6, evening_hour=20)
    for _slot, items in day.by_slot:
        for earlier, later in zip(items, items[1:]):
            assert later.start >= earlier.end


def test_anchors_shift_the_whole_slot():
    early = build_day_schedule(plan(), wake_hour=5, evening_hour=19)
    late = build_day_schedule(plan(), wake_hour=8, evening_hour=22)
    assert next(b for b in early.blocks if b.key == "new_hifz").start.hour == 5
    assert next(b for b in late.blocks if b.key == "new_hifz").start.hour == 8
    assert next(b for b in early.blocks if b.key == "tasmee").start.hour < \
           next(b for b in late.blocks if b.key == "tasmee").start.hour


def test_blocks_are_returned_chronologically():
    day = build_day_schedule(plan(), wake_hour=6, evening_hour=20)
    starts = [(b.start.hour, b.start.minute) for b in day.blocks]
    assert starts == sorted(starts)


def test_time_range_collapses_a_shared_meridiem():
    day = build_day_schedule(plan(), wake_hour=6, evening_hour=20)
    hifz = next(b for b in day.blocks if b.key == "new_hifz")
    assert hifz.time_range.count("AM") == 1


def test_every_scheduled_block_counts_toward_the_total():
    day = build_day_schedule(plan(), wake_hour=6, evening_hour=20)
    assert day.total_minutes == sum(b.minutes for b in day.blocks)


def test_a_nonsense_anchor_hour_does_not_crash():
    day = build_day_schedule(plan(), wake_hour=99, evening_hour=-4)
    assert all(0 <= b.start.hour <= 23 for b in day.blocks)


# --- Week strip --------------------------------------------------------------

MONDAY = date(2026, 8, 17)


def test_today_is_open_not_missed():
    """A tracker that marks you failed at 9am is a tracker people delete."""
    strip = build_week_strip(set(), active_days="1111111", today=MONDAY)
    assert strip[-1].is_today
    assert strip[-1].state == "open"
    assert "missed" not in [d.state for d in strip if d.is_today]


def test_logged_days_read_as_done():
    from datetime import timedelta

    logged = {MONDAY - timedelta(days=i) for i in (0, 1, 3)}
    strip = build_week_strip(logged, active_days="1111111", today=MONDAY)
    assert [d.state for d in strip][-1] == "done"
    assert sum(1 for d in strip if d.state == "done") == 3


def test_days_off_are_marked_off_not_missed():
    strip = build_week_strip(set(), active_days="1111011", today=MONDAY)  # Friday off
    friday = next(d for d in strip if d.day.weekday() == 4)
    assert friday.state == "off"


def test_strip_covers_exactly_seven_days_ending_today():
    strip = build_week_strip(set(), active_days="1111111", today=MONDAY)
    assert len(strip) == 7
    assert strip[-1].day == MONDAY
    assert not any(d.is_future for d in strip)
