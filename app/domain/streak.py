"""Forgiving streak and consistency tracking.

A brittle streak is actively harmful for the audience this app is for. A parent
with a sick child misses a Tuesday, watches "97 days" reset to zero, and the
psychological cost lands nowhere near proportional to one missed session. So:

* Days the student marked off are not misses. They are days off, and the guide
  is explicit that a day off must stay a day off.
* A limited number of misses are absorbed rather than resetting the count.
* The number shown largest is *consistency over the last 30 days*, not the
  streak. Consistency recovers; a streak only ever dies.
* Nothing here ever renders as a loss. The worst case is "let's start a new
  run today", which is true and is also the correct next action.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, List, Optional, Set

from app.domain import dates as dt

# Misses absorbed per 7 active days before a run is considered ended.
GRACE_PER_WEEK = 1
# How far back the consistency percentage looks.
CONSISTENCY_WINDOW_DAYS = 30
# Hard stop on the backward walk, so a long-dormant account cannot spin.
MAX_LOOKBACK_DAYS = 730


@dataclass
class StreakState:
    current_run: int
    longest_run: int
    grace_used: int
    active_days_in_window: int
    logged_days_in_window: int
    logged_today: bool
    last_logged: Optional[date]
    milestones: List[int] = field(default_factory=list)

    @property
    def consistency_percent(self) -> int:
        if not self.active_days_in_window:
            return 0
        return round(self.logged_days_in_window / self.active_days_in_window * 100)

    @property
    def next_milestone(self) -> Optional[int]:
        for m in (7, 14, 30, 60, 100, 200, 365):
            if self.current_run < m:
                return m
        return None

    @property
    def to_next_milestone(self) -> Optional[int]:
        nm = self.next_milestone
        return None if nm is None else nm - self.current_run

    @property
    def headline(self) -> str:
        """Deliberately never punitive."""
        if self.current_run == 0:
            if self.logged_days_in_window:
                return "Today is a good day to start a new run."
            return "Log your first revision to get started."
        if self.logged_today:
            unit = "day" if self.current_run == 1 else "days"
            return f"{self.current_run} {unit} running. Logged for today."
        unit = "day" if self.current_run == 1 else "days"
        return f"{self.current_run} {unit} running — today is still open."

    @property
    def support_line(self) -> str:
        if self.grace_used:
            n = self.grace_used
            return (
                f"{n} missed day{'s' if n != 1 else ''} {'are' if n != 1 else 'is'} absorbed into "
                "this run. Missing the occasional day is not a setback."
            )
        pct = self.consistency_percent
        if pct >= 90:
            return f"You have shown up on {pct}% of your days this month."
        if pct >= 60:
            return f"{pct}% of your days this month. Steady is what finishes a juz."
        if pct > 0:
            return f"{pct}% of your days this month. Every session counts — keep going."
        return "Small and consistent beats big and occasional."


def compute_streak(
    logged_dates: Iterable[date],
    *,
    active_days: str,
    today: date,
    grace_per_week: int = GRACE_PER_WEEK,
    window_days: int = CONSISTENCY_WINDOW_DAYS,
) -> StreakState:
    """Compute the forgiving streak from a set of local dates that have logs."""
    logged: Set[date] = set(logged_dates)
    mask = dt.normalize_active_days(active_days)

    logged_today = today in logged
    last_logged = max((d for d in logged if d <= today), default=None)

    # --- current run: walk backwards over active days only -------------------
    # If today is active but not yet logged we do not count it as a miss; the
    # day is not over. We simply start the walk at yesterday.
    cursor = today if logged_today else today - timedelta(days=1)
    run = 0
    grace_used = 0
    active_seen = 0
    last_forgiven_at = None  # active-day index of the most recent absorbed miss
    guard = 0

    # The walk stops at the first day the student ever logged. Days before that
    # are not misses — there was no programme yet — and letting the walk run
    # past it silently spent grace on prehistory, so an unbroken run reported a
    # forgiven miss it never made.
    earliest = min(logged) if logged else None

    while guard < MAX_LOOKBACK_DAYS:
        guard += 1
        if earliest is None or cursor < earliest:
            break
        if not dt.is_active_day(mask, cursor):
            cursor -= timedelta(days=1)
            continue

        active_seen += 1
        if cursor in logged:
            run += 1
        elif _can_forgive(run, active_seen, last_forgiven_at, grace_per_week):
            grace_used += 1
            last_forgiven_at = active_seen
        else:
            break
        cursor -= timedelta(days=1)

    # --- longest run: same rules, applied across all history -----------------
    longest = _longest_run(logged, mask, today, grace_per_week)

    # --- consistency window --------------------------------------------------
    window_start = today - timedelta(days=window_days - 1)
    active_in_window = 0
    logged_in_window = 0
    d = window_start
    while d <= today:
        if dt.is_active_day(mask, d):
            active_in_window += 1
            if d in logged:
                logged_in_window += 1
        d += timedelta(days=1)

    return StreakState(
        current_run=run,
        longest_run=max(longest, run),
        grace_used=grace_used,
        active_days_in_window=active_in_window,
        logged_days_in_window=logged_in_window,
        logged_today=logged_today,
        last_logged=last_logged,
        milestones=[m for m in (7, 14, 30, 60, 100, 200, 365) if run >= m],
    )


def _can_forgive(
    run: int, active_seen: int, last_forgiven_at: Optional[int], grace_per_week: int
) -> bool:
    """Whether this miss is absorbed rather than ending the run.

    The rule is genuinely rolling: at most `grace_per_week` misses in any window
    of 7 active days. An earlier cumulative allowance ("one per week earned over
    the whole run") let misses bunch — a student on a 40-day run could skip five
    days straight and keep it, which makes the number meaningless. Spacing is
    the thing being rewarded, so spacing is what the rule checks.

    A run must also have started: the first thing you ever do cannot be a miss.
    """
    if run <= 0:
        return False
    if grace_per_week <= 0:
        return False
    if last_forgiven_at is None:
        return True
    return (active_seen - last_forgiven_at) >= 7


def _longest_run(logged: Set[date], mask: str, today: date, grace_per_week: int) -> int:
    if not logged:
        return 0
    start = min(logged)
    best = 0
    run = 0
    active_seen = 0
    last_forgiven_at = None

    d = start
    while d <= today:
        if not dt.is_active_day(mask, d):
            d += timedelta(days=1)
            continue
        active_seen += 1
        if d in logged:
            run += 1
            best = max(best, run)
        elif _can_forgive(run, active_seen, last_forgiven_at, grace_per_week):
            last_forgiven_at = active_seen
        else:
            run = 0
            active_seen = 0
            last_forgiven_at = None
        d += timedelta(days=1)
    return best
