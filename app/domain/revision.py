"""The five murajaat methods, the method-mix nudge, and juz rotation health.

Two distinct things the guide calls murajaat, both modelled here:

  * juz al hali  -- the juz currently being memorized, revised daily.
  * murajaat juz -- one previously-completed juz per day, on rotation.

The guide is blunt about the failure mode in the second one: a student who
knows six juz "will always do the 30th, 1st, 2nd regularly, but will try to
avoid 4th, 5th and 6th as much as possible... This fear will only make the weak
siparahs even weaker." So rotation coverage is tracked as a first-class signal,
not just a log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, Iterable, List, Optional, Sequence

# Revision method ranking -----------------------------------------------------
# Ordered most-recommended (1) to least (5). The numbers are stored in the DB,
# so they are a stable contract -- do not renumber.

TASMEE_TO_SOMEONE = 1
WITHOUT_MUSHAF_FULL = 2
WITHOUT_MUSHAF_PARTIAL = 3
LOOKING_AT_MUSHAF = 4
LISTENING_TO_RECORDING = 5

STRONG_METHODS = frozenset({TASMEE_TO_SOMEONE, WITHOUT_MUSHAF_FULL})
PASSIVE_METHODS = frozenset({LOOKING_AT_MUSHAF, LISTENING_TO_RECORDING})


@dataclass(frozen=True)
class RevisionMethod:
    rank: int
    key: str
    label: str
    short: str
    blurb: str
    # Rough minutes to revise one 20-page juz by this method. Used by the
    # schedule builder to be honest about time budgets.
    minutes_per_juz: int

    @property
    def is_strong(self) -> bool:
        return self.rank in STRONG_METHODS

    @property
    def is_passive(self) -> bool:
        return self.rank in PASSIVE_METHODS


METHODS: List[RevisionMethod] = [
    RevisionMethod(
        TASMEE_TO_SOMEONE,
        "tasmee_to_someone",
        "Tasmee of the entire juz to someone",
        "Tasmee to someone",
        "Strongest. Another person catches what you cannot hear yourself.",
        45,
    ),
    RevisionMethod(
        WITHOUT_MUSHAF_FULL,
        "without_mushaf_full",
        "Revise without looking at the mushaf",
        "From memory",
        "Closed mushaf, whole juz by heart. This is what actually builds retention.",
        35,
    ),
    RevisionMethod(
        WITHOUT_MUSHAF_PARTIAL,
        "without_mushaf_partial",
        "Revise parts without looking at the mushaf",
        "Partly from memory",
        "Closed mushaf for the parts you are sure of, open for the rest.",
        30,
    ),
    RevisionMethod(
        LOOKING_AT_MUSHAF,
        "looking_at_mushaf",
        "Recite while looking at the mushaf",
        "Reading aloud",
        "Useful on a tired day. Recite loudly enough to hear your own voice.",
        25,
    ),
    RevisionMethod(
        LISTENING_TO_RECORDING,
        "listening_to_recording",
        "Listen to a recording",
        "Listening",
        "Better than nothing, and good for a commute — but it is not recall.",
        20,
    ),
]

_BY_RANK: Dict[int, RevisionMethod] = {m.rank: m for m in METHODS}


def get_method(rank: int) -> RevisionMethod:
    m = _BY_RANK.get(int(rank))
    if m is None:
        raise ValueError(f"no revision method ranked {rank}")
    return m


def method_choices() -> List[RevisionMethod]:
    return list(METHODS)


# Method mix ------------------------------------------------------------------

# How many recent logs we look at when deciding whether to nudge.
MIX_WINDOW_DAYS = 14
# Below this share of strong (rank 1-2) revisions, we nudge.
STRONG_SHARE_FLOOR = 0.30
# We need at least this many logs before saying anything; nudging someone on
# their third ever log is obnoxious, not helpful.
MIN_LOGS_FOR_NUDGE = 4
# Consecutive passive-only revisions that trigger a nudge on their own.
PASSIVE_STREAK_TRIGGER = 5


@dataclass
class MethodMix:
    """Rolling method mix for one juz (or across all juz)."""

    counts: Dict[int, int] = field(default_factory=dict)
    window_days: int = MIX_WINDOW_DAYS
    passive_streak: int = 0

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def count(self, rank: int) -> int:
        return self.counts.get(rank, 0)

    def share(self, rank: int) -> float:
        return (self.count(rank) / self.total) if self.total else 0.0

    @property
    def strong_count(self) -> int:
        return sum(self.counts.get(r, 0) for r in STRONG_METHODS)

    @property
    def passive_count(self) -> int:
        return sum(self.counts.get(r, 0) for r in PASSIVE_METHODS)

    @property
    def strong_share(self) -> float:
        return (self.strong_count / self.total) if self.total else 0.0

    @property
    def passive_share(self) -> float:
        return (self.passive_count / self.total) if self.total else 0.0

    def breakdown(self) -> List[Dict]:
        """Ordered rows for the dashboard chart."""
        return [
            {
                "method": m,
                "count": self.count(m.rank),
                "share": self.share(m.rank),
                "percent": round(self.share(m.rank) * 100),
            }
            for m in METHODS
        ]


def build_method_mix(
    entries: Sequence,
    window_days: int = MIX_WINDOW_DAYS,
) -> MethodMix:
    """Build a mix from log rows ordered newest-first.

    `entries` need only expose a `.method` int attribute.
    """
    counts: Dict[int, int] = {}
    for e in entries:
        counts[e.method] = counts.get(e.method, 0) + 1

    passive_streak = 0
    for e in entries:  # newest first
        if e.method in PASSIVE_METHODS:
            passive_streak += 1
        else:
            break

    return MethodMix(counts=counts, window_days=window_days, passive_streak=passive_streak)


@dataclass
class MethodNudge:
    """A retention-risk nudge. Deliberately phrased as support, not a scolding."""

    should_nudge: bool
    headline: str = ""
    detail: str = ""
    severity: str = "info"  # info | warn

    def __bool__(self) -> bool:
        return self.should_nudge


def evaluate_method_mix(mix: MethodMix, juz_label: str = "this juz") -> MethodNudge:
    """Decide whether to nudge the student back toward methods 1-2.

    Two independent triggers, because they catch different students: the share
    test catches slow drift into passive revision, the streak test catches an
    abrupt switch that the share test would take another week to notice.
    """
    if mix.total < MIN_LOGS_FOR_NUDGE:
        return MethodNudge(False)

    if mix.passive_streak >= PASSIVE_STREAK_TRIGGER:
        return MethodNudge(
            True,
            headline=f"Your last {mix.passive_streak} revisions of {juz_label} were reading or listening.",
            detail=(
                "That is still showing up, which counts for a lot. When you have a spare "
                "15 minutes, try one round with the mushaf closed — it is the fastest way "
                "to find out what has quietly slipped."
            ),
            severity="warn",
        )

    if mix.strong_share < STRONG_SHARE_FLOOR:
        pct = round(mix.strong_share * 100)
        return MethodNudge(
            True,
            headline=f"Only {pct}% of your recent {juz_label} revisions were from memory.",
            detail=(
                "Reciting while looking, and listening, keep the juz familiar but they do "
                "not test recall. Aim for one closed-mushaf round, or a tasmee to someone, "
                "in the next few days."
            ),
            severity="info",
        )

    return MethodNudge(False)


# Rotation health -------------------------------------------------------------


@dataclass
class JuzRotationEntry:
    juz: int
    last_revised: Optional[date]
    days_since: Optional[int]
    revision_count: int

    @property
    def status(self) -> str:
        if self.days_since is None:
            return "never"
        if self.days_since >= 21:
            return "neglected"
        if self.days_since >= 10:
            return "cooling"
        return "warm"


def rank_neglected_juz(entries: Iterable[JuzRotationEntry]) -> List[JuzRotationEntry]:
    """Most-neglected first — the juz the student is avoiding.

    Never-revised juz sort ahead of everything else, then by days since.
    """
    return sorted(
        entries,
        key=lambda e: (0 if e.days_since is None else 1, -(e.days_since or 0), e.juz),
    )


# --- Murajaat portions -------------------------------------------------------
# A juz is not always revised whole. A student who finds one hard can take it in
# halves, and the guide gives a specific reason to care about *which* half: the
# second half of a juz is almost always the weaker one, because it is memorized
# while tired and revised last. Being able to schedule "juz 4, second half" on
# its own is exactly the fix it recommends.


class Portion:
    FULL = "full"
    FIRST_HALF = "first_half"
    SECOND_HALF = "second_half"

    ALL = (FULL, FIRST_HALF, SECOND_HALF)
    LABEL = {
        FULL: "Whole juz",
        FIRST_HALF: "First half",
        SECOND_HALF: "Second half",
    }
    # Toggle labels. Short enough for three side-by-side columns on a phone,
    # and shared by the revise and schedule pickers so the two cannot drift.
    SHORT = {FULL: "Whole", FIRST_HALF: "1st half", SECOND_HALF: "2nd half"}
    # Share of a juz, used to scale the time estimate.
    FRACTION = {FULL: 1.0, FIRST_HALF: 0.5, SECOND_HALF: 0.5}


def normalize_portion(value: Optional[str]) -> str:
    return value if value in Portion.ALL else Portion.FULL


def portion_pages(juz: int, portion: str) -> List[int]:
    """The pages a portion covers."""
    from app.domain.quran import PAGES_PER_JUZ, juz_pages

    pages = juz_pages(juz)
    portion = normalize_portion(portion)
    half = PAGES_PER_JUZ // 2
    if portion == Portion.FIRST_HALF:
        return pages[:half]
    if portion == Portion.SECOND_HALF:
        return pages[half:]
    return pages


def portion_label(juz: int, portion: str) -> str:
    """'Juz 4' or 'Juz 4 — second half', for anywhere it is named."""
    portion = normalize_portion(portion)
    if portion == Portion.FULL:
        return f"Juz {juz}"
    return f"Juz {juz} — {Portion.LABEL[portion].lower()}"


def portion_minutes(portion: str, method_rank: int) -> int:
    """Minutes to revise this portion by the given method."""
    full = get_method(method_rank).minutes_per_juz
    return max(5, round(full * Portion.FRACTION[normalize_portion(portion)]))
