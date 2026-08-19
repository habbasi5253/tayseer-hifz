"""The 30-day rule.

One clock per juz, not a rolling window per page:

    Muhaffiz 1 signs off the whole juz
        -> the 30-day clock starts
        -> student has 30 days to tasmee the ENTIRE juz to Muhaffiz 2
        -> passed: the clock stops for good and the next juz opens
        -> not passed in time: tasmee restarts from page 1 of the juz

Design decisions that matter:

**The clock starts at Stage 2 entry, not at each page's last recitation.** It is
a deadline, not a maintenance window. Once Muhaffiz 2 passes the juz the clock is
finished — there is no perpetual re-validation, because the program moves the
student on.

**Expiry resets progress, it does not delete history.** A juz that runs out of
time starts a new *attempt*: `window_started_at` moves forward and only pages
recited on or after that instant count toward the new attempt. Every earlier
recitation stays in the record, visible to both Muhaffiz. Deleting the rows
would destroy exactly the history a teacher needs to see that a student is
struggling.

**Expiry is computed, never a scheduled job.** A juz is expired when the local
date passes its deadline; nothing has to run for that to become true. A cron
that failed would otherwise leave a student in a window that had silently ended.

**Calendar days in the student's timezone.** See `app/domain/dates.py` — a
tasmee at 11pm Monday read at 8am Tuesday is one day, and DST never adds or
removes one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence

from app.domain import dates as dt
from app.domain.quran import PAGES_PER_JUZ, juz_page_range

# The rule itself. Program-defined, not a tunable.
WINDOW_DAYS = 30

# How far out the app starts warning. A week is long enough to actually book
# sittings with a Muhaffiz rather than just feel bad about it.
AT_RISK_DAYS = 7


class Status:
    NOT_STARTED = "not_started"   # still with Muhaffiz 1
    IN_PROGRESS = "in_progress"   # clock running, comfortably
    DUE_SOON = "due_soon"         # within AT_RISK_DAYS
    DUE_TODAY = "due_today"       # last day
    EXPIRED = "expired"           # ran out — tasmee restarts from page 1
    PASSED = "passed"             # Muhaffiz 2 passed the juz

    LABEL = {
        NOT_STARTED: "Not in tasmee yet",
        IN_PROGRESS: "Tasmee in progress",
        DUE_SOON: "Due soon",
        DUE_TODAY: "Last day",
        EXPIRED: "Restart from page 1",
        PASSED: "Passed",
    }
    TONE = {
        NOT_STARTED: "neutral",
        IN_PROGRESS: "good",
        DUE_SOON: "soon",
        DUE_TODAY: "soon",
        EXPIRED: "attention",
        PASSED: "good",
    }


@dataclass
class JuzTasmeeWindow:
    """One juz's 30-day tasmee deadline with Muhaffiz 2."""

    juz: int
    tz_name: str
    window_started_at: Optional[datetime] = None
    passed_at: Optional[datetime] = None
    attempt: int = 1
    # Pages passed *in the current attempt*, i.e. recited on or after
    # `window_started_at`. Earlier attempts stay in history but do not count.
    pages_passed: Sequence[int] = field(default_factory=tuple)
    now: Optional[datetime] = None

    # --- dates ---
    @property
    def started_on(self) -> Optional[date]:
        if self.window_started_at is None:
            return None
        return dt.local_date(self.window_started_at, self.tz_name)

    @property
    def deadline(self) -> Optional[date]:
        started = self.started_on
        return None if started is None else dt.add_days(started, WINDOW_DAYS)

    @property
    def today(self) -> date:
        return dt.today_local(self.tz_name, self.now)

    @property
    def days_elapsed(self) -> Optional[int]:
        started = self.started_on
        return None if started is None else dt.days_between(started, self.today)

    @property
    def days_remaining(self) -> Optional[int]:
        elapsed = self.days_elapsed
        return None if elapsed is None else WINDOW_DAYS - elapsed

    # --- progress ---
    @property
    def passed_count(self) -> int:
        return len(self.pages_passed)

    @property
    def pages_remaining(self) -> int:
        return max(0, PAGES_PER_JUZ - self.passed_count)

    @property
    def percent(self) -> int:
        return round(self.passed_count / PAGES_PER_JUZ * 100)

    @property
    def next_page(self) -> Optional[int]:
        """Tasmee runs in order from page 1 of the juz."""
        done = set(self.pages_passed)
        for p in juz_page_range(self.juz):
            if p not in done:
                return p
        return None

    # --- state ---
    @property
    def is_passed(self) -> bool:
        return self.passed_at is not None

    @property
    def has_started(self) -> bool:
        return self.window_started_at is not None

    @property
    def status(self) -> str:
        if self.is_passed:
            return Status.PASSED
        if not self.has_started:
            return Status.NOT_STARTED
        remaining = self.days_remaining
        assert remaining is not None
        if remaining < 0:
            return Status.EXPIRED
        if remaining == 0:
            return Status.DUE_TODAY
        if remaining <= AT_RISK_DAYS:
            return Status.DUE_SOON
        return Status.IN_PROGRESS

    @property
    def is_expired(self) -> bool:
        return self.status == Status.EXPIRED

    @property
    def label(self) -> str:
        return Status.LABEL[self.status]

    @property
    def tone(self) -> str:
        return Status.TONE[self.status]

    @property
    def needs_attention(self) -> bool:
        return self.status in (Status.EXPIRED, Status.DUE_TODAY, Status.DUE_SOON)

    @property
    def countdown_text(self) -> str:
        if self.is_passed:
            return "Passed"
        if not self.has_started:
            return "Not started"
        remaining = self.days_remaining
        assert remaining is not None
        if remaining < 0:
            return f"{-remaining} day{'s' if remaining != -1 else ''} past the deadline"
        if remaining == 0:
            return "Last day"
        return f"{remaining} day{'s' if remaining != 1 else ''} left"

    @property
    def pace_text(self) -> str:
        """What finishing in time actually requires, from here."""
        if self.is_passed or not self.has_started:
            return ""
        remaining_days = self.days_remaining or 0
        if remaining_days <= 0:
            return "Tasmee restarts from page 1."
        if self.pages_remaining == 0:
            return "All 20 pages recited — waiting on your Muhaffiz to pass the juz."
        per_day = self.pages_remaining / remaining_days
        if per_day <= 1:
            return f"{self.pages_remaining} pages in {remaining_days} days — about a page a day."
        return (
            f"{self.pages_remaining} pages in {remaining_days} days — "
            f"about {per_day:.1f} a day to finish in time."
        )

    @property
    def headline(self) -> str:
        if self.is_passed:
            return f"Juz {self.juz} passed"
        if not self.has_started:
            return f"Juz {self.juz} is still with your Stage 1 Muhaffiz"
        if self.is_expired:
            return f"Juz {self.juz} tasmee restarts from page 1"
        return f"Juz {self.juz}: {self.passed_count} of {PAGES_PER_JUZ} pages, {self.countdown_text}"

    def page_status(self, page: int) -> str:
        """For the 20-page grid: passed this attempt, or still to do."""
        if page in set(self.pages_passed):
            return "passed"
        if self.is_expired:
            return "reset"
        return "todo"


@dataclass
class TasmeeBoard:
    """Every juz that has ever entered Stage 2, for one student."""

    windows: List[JuzTasmeeWindow]
    tz_name: str
    generated_at: datetime

    @property
    def active(self) -> Optional[JuzTasmeeWindow]:
        """The juz currently under a running clock — at most one at a time."""
        for w in self.windows:
            if w.has_started and not w.is_passed:
                return w
        return None

    @property
    def passed(self) -> List[JuzTasmeeWindow]:
        return [w for w in self.windows if w.is_passed]

    @property
    def expired(self) -> List[JuzTasmeeWindow]:
        return [w for w in self.windows if w.is_expired]

    @property
    def at_risk(self) -> List[JuzTasmeeWindow]:
        return [w for w in self.windows if w.status in (Status.DUE_SOON, Status.DUE_TODAY)]

    @property
    def headline(self) -> str:
        w = self.active
        if w is None:
            n = len(self.passed)
            if n:
                return f"{n} juz passed. Nothing in tasmee right now."
            return "No juz in tasmee yet — finish sign-off with your Stage 1 Muhaffiz first."
        if w.is_expired:
            return (
                f"Juz {w.juz} ran past its 30 days — tasmee starts again from page 1."
            )
        if w.status == Status.DUE_TODAY:
            return f"Last day to finish juz {w.juz} — {w.pages_remaining} pages left."
        if w.status == Status.DUE_SOON:
            return f"Juz {w.juz}: {w.pages_remaining} pages in {w.days_remaining} days."
        return f"Juz {w.juz}: {w.passed_count} of {PAGES_PER_JUZ} pages recited."


def build_window(
    *,
    juz: int,
    tz_name: str,
    window_started_at: Optional[datetime],
    passed_at: Optional[datetime] = None,
    attempt: int = 1,
    passed_pages: Sequence[int] = (),
    now: Optional[datetime] = None,
) -> JuzTasmeeWindow:
    return JuzTasmeeWindow(
        juz=juz,
        tz_name=tz_name or dt.DEFAULT_TIMEZONE,
        window_started_at=window_started_at,
        passed_at=passed_at,
        attempt=attempt,
        pages_passed=tuple(sorted(passed_pages)),
        now=now,
    )


def build_board(
    *, tz_name: str, windows: Sequence[JuzTasmeeWindow], now: Optional[datetime] = None
) -> TasmeeBoard:
    return TasmeeBoard(
        windows=sorted(windows, key=lambda w: w.juz),
        tz_name=tz_name or dt.DEFAULT_TIMEZONE,
        generated_at=now or dt.utcnow(),
    )


def deadline_warnings(
    board: TasmeeBoard, *, lead_days: Sequence[int] = (7, 3, 1, 0)
) -> List[Dict[str, object]]:
    """Windows crossing a warning threshold today, for the notifier.

    Thresholds rather than a daily drumbeat, so the message stays a signal.
    """
    out: List[Dict[str, object]] = []
    for w in board.windows:
        if w.is_passed or not w.has_started:
            continue
        if w.days_remaining is not None and w.days_remaining in lead_days:
            out.append({"window": w, "lead": w.days_remaining})
    return sorted(out, key=lambda r: r["lead"])
