"""Tests for the daily plan and the pace projection.

There is no daily page target in this programme, so the plan reserves *time*
for new hifz rather than prescribing an amount. What it must get right is the
order of priority: the Muhaffiz's murajaat schedule and tasmee are fixed, and
memorization takes what is left.
"""

from __future__ import annotations

from datetime import date

from app.domain.schedule import (
    MIN_HIFZ_MINUTES,
    TimeBudget,
    build_daily_plan,
    project_completion,
    recommend_schedule,
)


def test_hifz_gets_whatever_the_commitments_leave():
    plan = build_daily_plan(budget=TimeBudget(90), murajaat=[(1, "full")], tasmee_juz=3)
    assert plan.committed_minutes > 0
    assert plan.hifz_minutes == plan.budget_minutes - plan.committed_minutes
    assert plan.fits


def test_the_plan_follows_the_muhaffiz_schedule():
    """One block per juz the Muhaffiz put on today, and no invented extras."""
    plan = build_daily_plan(budget=TimeBudget(120), murajaat=[(2, "full"), (5, "full"), (9, "full")])
    murajaat = [b for b in plan.blocks if b.key == "murajaat"]
    assert len(murajaat) == 3
    assert "juz 5" in murajaat[1].title


def test_no_murajaat_scheduled_means_no_murajaat_block():
    plan = build_daily_plan(budget=TimeBudget(60), murajaat=[])
    assert not any(b.key == "murajaat" for b in plan.blocks)


def test_tasmee_appears_only_while_a_juz_is_under_its_clock():
    assert any(b.key == "tasmee" for b in build_daily_plan(
        budget=TimeBudget(60), tasmee_juz=4).blocks)
    assert not any(b.key == "tasmee" for b in build_daily_plan(
        budget=TimeBudget(60), tasmee_juz=None).blocks)


def test_no_hifz_block_while_a_batch_is_with_the_muhaffiz():
    """Nothing to memorize today, so the plan should not pretend otherwise."""
    plan = build_daily_plan(budget=TimeBudget(60), murajaat=[(1, "full")], memorizing=False)
    assert plan.hifz_minutes == 0
    assert any(b.key == "murajaat" for b in plan.blocks)


def test_a_squeezed_day_still_reserves_a_usable_hifz_slot():
    """Better to show a short honest slot than a zero-minute one."""
    plan = build_daily_plan(budget=TimeBudget(20), murajaat=[(1, "full")], tasmee_juz=3)
    assert plan.hifz_minutes == MIN_HIFZ_MINUTES


def test_a_day_that_fits_says_where_the_time_goes():
    r = recommend_schedule(budget=TimeBudget(90), murajaat=[(1, "full")], tasmee_juz=3)
    assert r.verdict == "fits"
    assert "minutes for new hifz" in r.detail


def test_an_overloaded_schedule_blames_the_commitments_not_the_student():
    """The fix offered is fewer juz or shorter sittings, never 'try harder'."""
    r = recommend_schedule(budget=TimeBudget(30), murajaat=[(1, "full"), (2, "full"), (3, "full")], tasmee_juz=4)
    assert r.verdict == "tight"
    assert "more than the 30 you have" in r.headline
    assert "editing your murajaat schedule" in r.detail
    assert "taken in halves" in r.detail or "halves rather than whole" in r.detail
    assert "short sittings" in r.detail


def test_off_days_shrink_the_week_not_the_day():
    budget = TimeBudget(daily_minutes=40, active_days="1111011")  # Friday off
    assert budget.days_per_week == 6
    assert budget.weekly_minutes == 240
    assert "Friday" in budget.off_days_text


# --- Projection --------------------------------------------------------------


def test_projection_refuses_to_guess_from_thin_data():
    p = project_completion(
        pages_memorized=10, pages_logged_in_window=2, active_days_in_window=2,
        active_days="1111111", today=date(2026, 8, 17),
    )
    assert p.has_projection is False
    assert "Not enough logged history" in p.summary


def test_projection_uses_logged_pace_not_a_target():
    """No target exists any more, so the forecast can only come from history."""
    p = project_completion(
        pages_memorized=100, pages_logged_in_window=20, active_days_in_window=40,
        active_days="1111111", today=date(2026, 8, 17), target_pages=600,
    )
    assert p.observed_pages_per_active_day == 0.5
    assert p.pages_remaining == 500
    assert p.projected_completion.year > 2028


def test_projection_respects_days_off():
    common = dict(pages_memorized=0, pages_logged_in_window=20,
                  active_days_in_window=20, today=date(2026, 8, 17), target_pages=600)
    assert project_completion(active_days="1111100", **common).projected_completion > \
           project_completion(active_days="1111111", **common).projected_completion


# --- Portions ----------------------------------------------------------------


def test_a_half_juz_costs_half_the_time():
    """The point of allowing halves: a hard juz fits in a real evening."""
    whole = build_daily_plan(budget=TimeBudget(120), murajaat=[(4, "full")])
    half = build_daily_plan(budget=TimeBudget(120), murajaat=[(4, "second_half")])
    w = next(b for b in whole.blocks if b.key == "murajaat")
    h = next(b for b in half.blocks if b.key == "murajaat")
    assert h.minutes < w.minutes
    assert abs(h.minutes - w.minutes / 2) <= 1


def test_a_portion_is_named_in_the_block():
    plan = build_daily_plan(budget=TimeBudget(120), murajaat=[(4, "second_half")])
    assert "second half" in next(b for b in plan.blocks if b.key == "murajaat").title


def test_halves_of_the_same_juz_are_separate_commitments():
    plan = build_daily_plan(
        budget=TimeBudget(120), murajaat=[(4, "first_half"), (4, "second_half")]
    )
    assert len([b for b in plan.blocks if b.key == "murajaat"]) == 2


def test_taking_a_juz_in_halves_can_rescue_an_overloaded_day():
    whole = recommend_schedule(budget=TimeBudget(45), murajaat=[(1, "full"), (2, "full")])
    split = recommend_schedule(
        budget=TimeBudget(45), murajaat=[(1, "full"), (2, "second_half")]
    )
    assert whole.verdict == "tight"
    assert split.plan.committed_minutes < whole.plan.committed_minutes
