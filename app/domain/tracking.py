"""Are you on track? The numbers a report has to answer.

Four questions, and nothing else:

    How far into each juz am I?
    How long am I taking per page?
    How many classes have I attended?
    How many did I miss this week?

The first two say whether a juz will finish; the last two say whether the habit
underneath it is holding. Everything here is derived from records the app
already keeps — no self-assessment, no scores.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import List, Optional, Sequence

from app.domain.quran import PAGES_PER_JUZ


# --- Pace: how long a page is taking ----------------------------------------


@dataclass
class PagePace:
    """Days per page, measured from actual sign-offs."""

    pages_counted: int
    days_per_page: Optional[float]
    fastest_gap: Optional[int]
    slowest_gap: Optional[int]
    span_days: Optional[int]

    @property
    def has_pace(self) -> bool:
        return self.days_per_page is not None

    @property
    def days_per_juz(self) -> Optional[int]:
        if not self.has_pace or self.days_per_page <= 0:
            return None
        return round(self.days_per_page * PAGES_PER_JUZ)

    @property
    def summary(self) -> str:
        if not self.has_pace:
            return "Not enough pages signed off yet to measure a pace."
        d = self.days_per_page
        if d <= 0:
            # Every gap was zero: pages are signed off in same-day batches, so
            # "days per page" has no meaning. Rate over the span does.
            return "Several pages a sitting — too fast to measure in days per page."
        rate = f"{d:.1f} days a page" if d >= 1 else f"{1 / d:.1f} pages a day"
        return f"{rate} — about {self.days_per_juz} days for a full juz at this rate."


def page_pace(signoff_dates: Sequence[date], *, min_pages: int = 3) -> PagePace:
    """Days per page from the gaps between consecutive sign-offs.

    Gaps rather than (span / pages) because the median gap survives a holiday or
    a fortnight of illness, while an average over the whole span quietly folds
    every break into the pace and makes a steady student look slow.

    Same-day sign-offs are a batch of pages heard in one sitting, so they yield
    a zero gap and correctly pull the average down.
    """
    days = sorted(signoff_dates)
    if len(days) < min_pages:
        return PagePace(len(days), None, None, None, None)

    gaps = [(b - a).days for a, b in zip(days, days[1:])]
    # A zero median means pages come in same-day batches; the mean still carries
    # the real cadence across sittings, so fall back to it before giving up.
    median = statistics.median(gaps) if gaps else None
    if median == 0 and gaps:
        median = statistics.fmean(gaps)

    return PagePace(
        pages_counted=len(days),
        days_per_page=median,
        fastest_gap=min(gaps) if gaps else None,
        slowest_gap=max(gaps) if gaps else None,
        span_days=(days[-1] - days[0]).days,
    )


# --- Attendance: classes made and missed ------------------------------------


@dataclass
class WeekAttendance:
    week_start: date
    expected: int
    attended: int

    @property
    def missed(self) -> int:
        return max(0, self.expected - self.attended)

    @property
    def percent(self) -> int:
        return round(self.attended / self.expected * 100) if self.expected else 0

    @property
    def label(self) -> str:
        return self.week_start.strftime("%d %b")


@dataclass
class Attendance:
    weeks: List[WeekAttendance] = field(default_factory=list)

    @property
    def attended(self) -> int:
        return sum(w.attended for w in self.weeks)

    @property
    def expected(self) -> int:
        return sum(w.expected for w in self.weeks)

    @property
    def missed(self) -> int:
        return sum(w.missed for w in self.weeks)

    @property
    def percent(self) -> int:
        return round(self.attended / self.expected * 100) if self.expected else 0

    @property
    def this_week(self) -> Optional[WeekAttendance]:
        return self.weeks[-1] if self.weeks else None

    @property
    def summary(self) -> str:
        if not self.expected:
            return "No murajaat schedule set, so there is nothing to attend against."
        return (
            f"{self.attended} of {self.expected} scheduled classes attended "
            f"({self.percent}%), {self.missed} missed."
        )


def weekly_attendance(
    *,
    planned_weekdays: Sequence[int],
    class_dates: Sequence[date],
    today: date,
    weeks: int = 4,
) -> Attendance:
    """Classes attended vs scheduled, week by week, most recent last.

    Two deliberate choices:

    *Only days that have already happened count as expected.* Counting the rest
    of this week as missed would show a student failing on Monday morning for
    classes on Thursday.

    *The current schedule is applied to past weeks.* The app does not version the
    plan, so a student who recently added a day will see the earlier weeks look
    worse than they were. Over a four-week window that is a small distortion and
    the alternative — history nobody can edit — is worse for a plan meant to be
    adjusted freely.
    """
    attended_on = set(class_dates)
    planned = set(planned_weekdays)

    # Weeks run Monday-first, matching date.weekday().
    this_monday = today - timedelta(days=today.weekday())
    out: List[WeekAttendance] = []

    for w in range(weeks - 1, -1, -1):
        start = this_monday - timedelta(weeks=w)
        expected = 0
        attended = 0
        for i in range(7):
            day = start + timedelta(days=i)
            if day > today:
                break
            if day.weekday() in planned:
                expected += 1
                if day in attended_on:
                    attended += 1
        out.append(WeekAttendance(week_start=start, expected=expected, attended=attended))

    return Attendance(weeks=out)


# --- Per-juz progress --------------------------------------------------------


@dataclass
class JuzProgressRow:
    """One juz's line in the report."""

    juz: int
    signed_off: int
    recited: int
    status: str
    started_on: Optional[date] = None
    finished_on: Optional[date] = None

    @property
    def signed_off_percent(self) -> int:
        return round(self.signed_off / PAGES_PER_JUZ * 100)

    @property
    def recited_percent(self) -> int:
        return round(self.recited / PAGES_PER_JUZ * 100)

    @property
    def days_taken(self) -> Optional[int]:
        if self.started_on is None:
            return None
        end = self.finished_on or date.today()
        return (end - self.started_on).days

    @property
    def is_complete(self) -> bool:
        return self.finished_on is not None


def projected_finish(
    *, pages_remaining: int, pace: PagePace, today: date
) -> Optional[date]:
    """When the current juz finishes at the measured pace."""
    if not pace.has_pace or pages_remaining <= 0 or pace.days_per_page <= 0:
        return None
    return today + timedelta(days=round(pace.days_per_page * pages_remaining))
