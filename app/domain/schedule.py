"""The honest schedule builder.

There is no daily page target in this programme — a student memorizes one page
or several, at whatever rate they and their Muhaffiz settle on. So this module
does not prescribe an amount. It lays down the day's fixed commitments (the
murajaat the Muhaffiz scheduled, and tasmee if a juz is under its 30-day clock)
and reports how much of the student's stated time is genuinely left to memorize
in.

That is the honest version of the same service: a plan that quietly assumes 55
minutes from someone with 30 is how students conclude they are failing when in
fact they were mis-scheduled.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import List, Optional, Sequence

from app.domain import dates as dt
from app.domain.quran import JUZ_COUNT, PAGES_PER_JUZ
from app.domain.revision import get_method

# A memorization slot below this is not worth scheduling.
MIN_HIFZ_MINUTES = 10
# A tasmee sitting with Muhaffiz 2, however many pages it covers.
MINUTES_PER_TASMEE_SITTING = 20

# The guide puts the brain's attention span on one task at roughly 25 minutes
# and recommends breaking work into short blocks rather than one long sitting.
ATTENTION_BLOCK_MINUTES = 25


# What a student picks in the UI. Stored as minutes because every duration in
# the app is minutes, but nobody thinks about their day in units of 45.
HOUR_CHOICES = [0.5, 0.75, 1, 1.5, 2, 2.5, 3, 4]


def hours_to_minutes(hours: float) -> int:
    return max(15, int(round(float(hours) * 60)))


def minutes_to_hours(minutes: int) -> float:
    """Snapped to the nearest offered choice so the select always has a match."""
    hours = (minutes or 60) / 60
    return min(HOUR_CHOICES, key=lambda h: abs(h - hours))


def format_hours(hours: float) -> str:
    if hours == 0.5:
        return "30 minutes"
    if hours == 0.75:
        return "45 minutes"
    if hours == 1:
        return "1 hour"
    return f"{hours:g} hours"


@dataclass
class TimeBudget:
    daily_minutes: int
    active_days: str = dt.ALL_DAYS_ON

    @property
    def daily_hours(self) -> float:
        return minutes_to_hours(self.daily_minutes)

    @property
    def daily_text(self) -> str:
        return format_hours(self.daily_hours)

    @property
    def days_per_week(self) -> int:
        return dt.active_days_per_week(self.active_days)

    @property
    def weekly_minutes(self) -> int:
        return self.daily_minutes * self.days_per_week

    @property
    def off_days_text(self) -> str:
        return dt.describe_off_days(self.active_days)


@dataclass
class PlanBlock:
    key: str
    title: str
    detail: str
    minutes: int
    optional: bool = False


@dataclass
class DailyPlan:
    """One day's work, costed in minutes.

    There is no daily page target. The programme lets a student memorize one
    page or several — that is between them and their Muhaffiz — so the plan
    reserves *time* for new hifz rather than prescribing an amount. What the app
    can honestly say is how much of the day the fixed commitments take, and
    therefore how much is genuinely left to memorize in.
    """

    blocks: List[PlanBlock] = field(default_factory=list)
    budget_minutes: int = 0

    @property
    def total_minutes(self) -> int:
        return sum(b.minutes for b in self.blocks)

    @property
    def required_minutes(self) -> int:
        return sum(b.minutes for b in self.blocks if not b.optional)

    @property
    def committed_minutes(self) -> int:
        """Murajaat and tasmee — the parts that are not the student's to shrink."""
        return sum(b.minutes for b in self.blocks if b.key != "new_hifz" and not b.optional)

    @property
    def hifz_minutes(self) -> int:
        return sum(b.minutes for b in self.blocks if b.key == "new_hifz")

    @property
    def fits(self) -> bool:
        return self.required_minutes <= self.budget_minutes

    @property
    def overrun(self) -> int:
        return max(0, self.required_minutes - self.budget_minutes)

    @property
    def suggested_sittings(self) -> int:
        """The guide advises short blocks over one long sitting."""
        return max(1, math.ceil(self.required_minutes / ATTENTION_BLOCK_MINUTES))


def build_daily_plan(
    *,
    budget: TimeBudget,
    murajaat: Sequence[tuple] = (),
    tasmee_juz: Optional[int] = None,
    revision_method: int = 2,
    memorizing: bool = True,
) -> DailyPlan:
    """Cost out one active day.

    Commitments are laid down first — the murajaat the student scheduled, and
    tasmee if a juz is under its 30-day clock — because those have a fixed shape.
    Whatever remains of the budget goes to new hifz.

    `murajaat` is a sequence of (juz, portion). A half juz costs half the time,
    which is the point of allowing halves at all: a hard juz becomes something
    that fits in a real evening.
    """
    from app.domain.revision import portion_label, portion_minutes

    method = get_method(revision_method)
    blocks: List[PlanBlock] = []

    for juz, portion in murajaat:
        blocks.append(
            PlanBlock(
                "murajaat",
                f"Murajaat — {portion_label(juz, portion).replace('Juz ', 'juz ')}",
                f"On your murajaat schedule ({method.short})",
                minutes=portion_minutes(portion, revision_method),
            )
        )

    if tasmee_juz is not None:
        blocks.append(
            PlanBlock(
                "tasmee",
                f"Tasmee — juz {tasmee_juz}",
                "Recite to your Stage 2 Muhaffiz, working through the juz in order",
                minutes=MINUTES_PER_TASMEE_SITTING,
            )
        )

    if memorizing:
        committed = sum(b.minutes for b in blocks)
        remaining = budget.daily_minutes - committed
        blocks.insert(
            0,
            PlanBlock(
                "new_hifz",
                "New hifz",
                (
                    f"About {remaining} minutes to memorize in — as many pages as that gets you"
                    if remaining >= MIN_HIFZ_MINUTES
                    else "Whatever time you can find today"
                ),
                minutes=max(MIN_HIFZ_MINUTES, remaining),
            ),
        )

    return DailyPlan(blocks=blocks, budget_minutes=budget.daily_minutes)


@dataclass
class ScheduleRecommendation:
    """What the app tells the student about their time budget."""

    plan: DailyPlan
    verdict: str  # fits | tight
    headline: str
    detail: str

    @property
    def recommended(self) -> DailyPlan:
        return self.plan


def recommend_schedule(
    *,
    budget: TimeBudget,
    murajaat: Sequence[tuple] = (),
    tasmee_juz: Optional[int] = None,
    revision_method: int = 2,
    memorizing: bool = True,
) -> ScheduleRecommendation:
    """Report honestly on the day rather than negotiating a page target."""
    plan = build_daily_plan(
        budget=budget,
        murajaat=murajaat,
        tasmee_juz=tasmee_juz,
        revision_method=revision_method,
        memorizing=memorizing,
    )

    if plan.fits:
        return ScheduleRecommendation(
            plan=plan,
            verdict="fits",
            headline=f"About {plan.required_minutes} minutes today.",
            detail=(
                f"{plan.committed_minutes} of that is murajaat and tasmee, leaving roughly "
                f"{plan.hifz_minutes} minutes for new hifz."
                if plan.hifz_minutes
                else f"All of it is murajaat and tasmee, with {budget.off_days_text}."
            ),
        )

    return ScheduleRecommendation(
        plan=plan,
        verdict="tight",
        headline=(
            f"Your commitments alone come to about {plan.committed_minutes} minutes, "
            f"more than the {budget.daily_minutes} you have."
        ),
        detail=(
            f"The realistic options are splitting the day across {plan.suggested_sittings} "
            "short sittings, or editing your murajaat schedule — a juz can be taken in "
            "halves rather than whole. The guide's own advice is to hunt for the 5- and "
            "10-minute pockets: after fajr, after lunch, before sleep."
        ),
    )


# Projection ------------------------------------------------------------------


@dataclass
class PaceProjection:
    """Completion forecast built on logged reality, not the theoretical max."""

    observed_pages_per_active_day: Optional[float]
    active_days_per_week: int
    pages_remaining: int
    projected_completion: Optional[date]
    weeks_remaining: Optional[int]
    basis: str

    @property
    def has_projection(self) -> bool:
        return self.projected_completion is not None

    @property
    def summary(self) -> str:
        if not self.has_projection:
            return "Not enough logged history yet to project a completion date."
        assert self.projected_completion is not None
        return (
            f"At your logged pace of {self.observed_pages_per_active_day:.2f} pages per active day, "
            f"on track for {self.projected_completion.strftime('%B %Y')}."
        )


def project_completion(
    *,
    pages_memorized: int,
    pages_logged_in_window: int,
    active_days_in_window: int,
    active_days: str,
    today: date,
    target_pages: int = JUZ_COUNT * PAGES_PER_JUZ,
    min_active_days: int = 5,
) -> PaceProjection:
    """Project completion from actual logged pages.

    Deliberately refuses to guess below `min_active_days` of history. A
    projection from two days of data is not encouraging, it is noise — and a
    wildly optimistic date that slips every week is corrosive.
    """
    per_week = dt.active_days_per_week(active_days)
    remaining = max(0, target_pages - pages_memorized)

    if active_days_in_window < min_active_days or pages_logged_in_window <= 0:
        return PaceProjection(
            observed_pages_per_active_day=None,
            active_days_per_week=per_week,
            pages_remaining=remaining,
            projected_completion=None,
            weeks_remaining=None,
            basis=f"{active_days_in_window} active days logged — need at least {min_active_days}",
        )

    rate = pages_logged_in_window / active_days_in_window
    if rate <= 0 or per_week <= 0:
        return PaceProjection(
            observed_pages_per_active_day=rate,
            active_days_per_week=per_week,
            pages_remaining=remaining,
            projected_completion=None,
            weeks_remaining=None,
            basis="logged pace is zero",
        )

    pages_per_week = rate * per_week
    weeks = math.ceil(remaining / pages_per_week) if pages_per_week else None
    completion = today + timedelta(weeks=weeks) if weeks is not None else None

    return PaceProjection(
        observed_pages_per_active_day=rate,
        active_days_per_week=per_week,
        pages_remaining=remaining,
        projected_completion=completion,
        weeks_remaining=weeks,
        basis=f"{pages_logged_in_window} pages over {active_days_in_window} active days",
    )
