"""Turning the day's work into suggested times.

The schedule builder says *what* and *how long*. This says *when*. That gap is
where plans die: "35 minutes of work today" is a fact you can agree with and
still never act on, whereas "6:10 AM, new hifz, 10 minutes" is something you
either do or consciously skip.

The placement is not arbitrary. The guide's time-management section says to
categorise your free moments as Good / Medium / Bad and "plan to do hifz and
revising in the Good & Medium parts of the day. There is no point doing hifz
during the Bad times as nothing will register." It also points at the specific
pockets most people have: after fajr, after lunch, before sleep, and ten minutes
after each namaaz.

So new hifz lands in the freshest slot of the day, murajaat in the evening
wind-down, and the optional rotation juz in the midday pocket where listening or
a quick read actually fits. Nothing is scheduled into a Bad slot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional


@dataclass(frozen=True)
class Slot:
    key: str
    label: str
    hint: str
    quality: str  # "good" | "medium"


FAJR = Slot("fajr", "After fajr", "The freshest your mind will be all day", "good")
MIDDAY = Slot("midday", "Midday", "A short pocket — after lunch, or a commute", "medium")
EVENING = Slot("evening", "After isha", "Wind-down — best for murajaat", "good")

SLOTS = [FAJR, MIDDAY, EVENING]

# Which slot each kind of work belongs in.
#
# new_hifz gets the fajr slot on purpose: it is the hardest, least forgiving
# task, and the guide is explicit that memorizing while tired does not register.
# The rotation juz gets midday because it is the one block that survives being
# done as listening on a commute.
BLOCK_SLOT: Dict[str, Slot] = {
    "new_hifz": FAJR,
    "tasmee": EVENING,
    "murajaat": EVENING,
}

# Gap left between two blocks in the same slot, so the plan does not read as one
# unbroken 40-minute wall.
GAP_MINUTES = 5

DEFAULT_WAKE_HOUR = 6
DEFAULT_MIDDAY_HOUR = 13
DEFAULT_EVENING_HOUR = 20


@dataclass
class TimedBlock:
    """One plan block with a suggested clock time."""

    block: object  # schedule.PlanBlock
    slot: Slot
    start: time
    end: time

    @property
    def key(self) -> str:
        return self.block.key

    @property
    def title(self) -> str:
        return self.block.title

    @property
    def detail(self) -> str:
        return self.block.detail

    @property
    def minutes(self) -> int:
        return self.block.minutes

    @property
    def optional(self) -> bool:
        return self.block.optional

    @property
    def time_range(self) -> str:
        """'6:00 – 6:16 AM', collapsing the meridiem when both ends share it."""
        s, e = _fmt(self.start), _fmt(self.end)
        if s[-2:] == e[-2:]:
            return f"{s[:-3]} – {e}"
        return f"{s} – {e}"

    @property
    def start_text(self) -> str:
        return _fmt(self.start)


def _fmt(t: time) -> str:
    hour = t.hour % 12 or 12
    meridiem = "AM" if t.hour < 12 else "PM"
    return f"{hour}:{t.minute:02d} {meridiem}"


def _add(t: time, minutes: int) -> time:
    base = datetime(2000, 1, 1, t.hour, t.minute) + timedelta(minutes=minutes)
    return base.time()


@dataclass
class DaySchedule:
    """The day's blocks, placed in time and grouped by slot."""

    blocks: List[TimedBlock]

    @property
    def by_slot(self) -> List[tuple]:
        """[(Slot, [TimedBlock, ...]), ...] in chronological slot order."""
        out = []
        for slot in SLOTS:
            items = [b for b in self.blocks if b.slot is slot]
            if items:
                out.append((slot, items))
        return out

    @property
    def required(self) -> List[TimedBlock]:
        return [b for b in self.blocks if not b.optional]

    @property
    def first_start(self) -> Optional[str]:
        return self.blocks[0].start_text if self.blocks else None

    @property
    def total_minutes(self) -> int:
        return sum(b.minutes for b in self.blocks if not b.optional)


def build_day_schedule(
    plan,
    *,
    wake_hour: int = DEFAULT_WAKE_HOUR,
    midday_hour: int = DEFAULT_MIDDAY_HOUR,
    evening_hour: int = DEFAULT_EVENING_HOUR,
) -> DaySchedule:
    """Place each block of `plan` at a suggested time.

    Blocks stack inside their slot in the order the schedule builder produced
    them, separated by a short gap. The first block of a slot starts ten minutes
    after the anchor hour — nobody is memorizing at the exact second fajr ends.
    """
    anchors = {
        FAJR.key: time(hour=_clamp_hour(wake_hour), minute=10),
        MIDDAY.key: time(hour=_clamp_hour(midday_hour), minute=0),
        EVENING.key: time(hour=_clamp_hour(evening_hour), minute=0),
    }
    cursor = dict(anchors)
    timed: List[TimedBlock] = []

    for block in plan.blocks:
        slot = BLOCK_SLOT.get(block.key, EVENING)
        start = cursor[slot.key]
        end = _add(start, block.minutes)
        timed.append(TimedBlock(block=block, slot=slot, start=start, end=end))
        cursor[slot.key] = _add(end, GAP_MINUTES)

    # Chronological, so the card reads top-to-bottom as the day happens.
    timed.sort(key=lambda b: (b.start.hour, b.start.minute))
    return DaySchedule(blocks=timed)


def _clamp_hour(h: int) -> int:
    try:
        return max(0, min(23, int(h)))
    except (TypeError, ValueError):
        return DEFAULT_WAKE_HOUR


# --- Week strip --------------------------------------------------------------


@dataclass
class DayDot:
    """One day in the habit strip."""

    day: date
    logged: bool
    is_active: bool
    is_today: bool
    is_future: bool

    @property
    def letter(self) -> str:
        return "MTWTFSS"[self.day.weekday()]

    @property
    def state(self) -> str:
        if self.is_future:
            return "future"
        if self.logged:
            return "done"
        if not self.is_active:
            return "off"
        if self.is_today:
            return "open"
        return "missed"


def build_week_strip(logged_dates, *, active_days: str, today: date, days: int = 7) -> List[DayDot]:
    """The last `days` days ending today — the habit-tracker strip.

    Today is rendered as *open*, never as missed. The day is not over, and a
    tracker that marks you failed at 9am is a tracker people delete.
    """
    from app.domain import dates as dt

    logged = set(logged_dates)
    start = today - timedelta(days=days - 1)
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        out.append(
            DayDot(
                day=d,
                logged=d in logged,
                is_active=dt.is_active_day(active_days, d),
                is_today=(d == today),
                is_future=(d > today),
            )
        )
    return out
