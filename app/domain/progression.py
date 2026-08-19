"""Stage 1 sign-off gates, and the juz-to-juz gate.

The Barnamaj Tayseer loop, as the program actually runs it:

    memorize a batch of pages  ->  recite them to Muhaffiz 1  ->  signed off
        -> next batch  ->  ... whole juz done ...
        -> tasmee the entire juz to Muhaffiz 2  ->  passed  ->  next juz

Two gates fall out of that, and both are enforced here rather than in the UI so
they cannot be bypassed by posting a form directly:

**The sign-off gate.** A student may memorize as many pages as they like before
reciting — one at a time or a batch. What they may not do is run ahead while a
submitted batch is still waiting on their Muhaffiz. So the block is on *pending
submission*, not on page count: build a batch freely, submit it, then wait.

**The juz gate.** Juz N+1 does not open until Muhaffiz 2 has passed the full-juz
tasmee for juz N. This is the rule that makes the second Muhaffiz load-bearing
rather than advisory.

These are pure functions over plain data so the rules can be tested without a
database, and so the same check can answer both "may I?" and "why not?" — the UI
needs the reason, not just a boolean.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from app.domain.quran import PAGES_PER_JUZ, page_index_in_juz


class PageState:
    """Where a single page sits in the Stage 1 loop."""

    LOCKED = "locked"          # earlier pages are still pending
    READY = "ready"            # next up to memorize
    MEMORIZED = "memorized"    # student has learned it, not yet submitted
    SUBMITTED = "submitted"    # recited/awaiting Muhaffiz 1
    RETURNED = "returned"      # heard and not passed — recite again
    SIGNED_OFF = "signed_off"  # passed by Muhaffiz 1

    LABEL = {
        LOCKED: "Locked",
        READY: "Next up",
        MEMORIZED: "Memorized",
        SUBMITTED: "With your Muhaffiz",
        RETURNED: "Recite again",
        SIGNED_OFF: "Signed off",
    }
    TONE = {
        LOCKED: "neutral",
        READY: "neutral",
        MEMORIZED: "soon",
        SUBMITTED: "soon",
        RETURNED: "attention",
        SIGNED_OFF: "good",
    }


@dataclass(frozen=True)
class Gate:
    """The answer to 'may I?', carrying the reason when the answer is no."""

    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


def can_memorize_more(
    *,
    juz: int,
    submitted_pages: Sequence[int],
    returned_pages: Sequence[int],
) -> Gate:
    """May the student mark another page memorized in this juz?

    Blocked while a batch is with the Muhaffiz, or while any page has come back
    needing another recitation. Running ahead of an unresolved page is exactly
    what the sign-off gate exists to prevent.
    """
    if returned_pages:
        pages = ", ".join(f"p.{page_index_in_juz(p)}" for p in sorted(returned_pages))
        return Gate(
            False,
            f"Recite {pages} again before moving on — your Muhaffiz has sent "
            f"{'them' if len(returned_pages) > 1 else 'it'} back.",
        )
    if submitted_pages:
        pages = ", ".join(f"p.{page_index_in_juz(p)}" for p in sorted(submitted_pages))
        return Gate(
            False,
            f"{pages} {'are' if len(submitted_pages) > 1 else 'is'} with your Muhaffiz "
            "for sign-off. You can carry on once they have heard it.",
        )
    return Gate(True)


def can_submit(
    *, memorized_unsubmitted: Sequence[int], returned: Sequence[int] = ()
) -> Gate:
    """May the student send a batch to Muhaffiz 1?

    Returned pages count. They have already been memorized and heard once, and
    the whole point of returning them is that they get recited again — omitting
    them here deadlocks the student: blocked from new pages by the returned one,
    and blocked from re-submitting it because nothing is "unsubmitted".
    """
    if not memorized_unsubmitted and not returned:
        return Gate(False, "Mark at least one page memorized before sending it to be heard.")
    return Gate(True)


DEFAULT_JUZ_ORDER = tuple(range(1, 31))


def parse_juz_order(raw: Optional[str]) -> tuple:
    """A student's juz sequence, falling back to 1..30.

    Stored as a comma-separated string. Anything malformed, incomplete or
    duplicated falls back to the default rather than half-applying a broken
    order — a wrong sequence would gate the wrong juz, which is worse than
    ignoring the setting.
    """
    if not raw:
        return DEFAULT_JUZ_ORDER
    try:
        parsed = tuple(int(x) for x in raw.replace(" ", "").split(",") if x)
    except ValueError:
        return DEFAULT_JUZ_ORDER
    if sorted(parsed) != list(DEFAULT_JUZ_ORDER):
        return DEFAULT_JUZ_ORDER
    return parsed


def previous_juz_in(order: Sequence[int], juz: int) -> Optional[int]:
    """The juz that must be passed before `juz` opens, per this student's order."""
    try:
        idx = list(order).index(juz)
    except ValueError:
        return None
    return None if idx == 0 else order[idx - 1]


def can_start_juz(
    *,
    juz: int,
    previous_juz: Optional[int],
    previous_juz_passed: bool,
    is_entry_point: bool = False,
) -> Gate:
    """May the student begin `juz`?

    The predecessor comes from the student's own sequence, not from `juz - 1`.
    Most people go 1..30, but someone working backwards from 30 needs juz 30
    passed before 29 opens — arithmetic would demand juz 28 instead.

    `is_entry_point` covers joining mid-programme: whichever juz they start on
    has nothing before it to wait on.
    """
    if is_entry_point or previous_juz is None:
        return Gate(True)
    if not previous_juz_passed:
        return Gate(
            False,
            f"Juz {previous_juz} has not been passed by your Stage 2 Muhaffiz yet. "
            "The full juz is recited to them before the next one opens.",
        )
    return Gate(True)


@dataclass
class JuzWorkface:
    """The Stage 1 picture for one juz: what is done, pending, and next."""

    juz: int
    signed_off: Sequence[int]
    submitted: Sequence[int]
    returned: Sequence[int]
    memorized_unsubmitted: Sequence[int]

    @property
    def next_page(self) -> Optional[int]:
        """The next page to memorize, or None if the juz is fully accounted for."""
        used = set(self.signed_off) | set(self.submitted) | set(self.returned) | set(
            self.memorized_unsubmitted
        )
        start = (self.juz - 1) * PAGES_PER_JUZ + 1
        for p in range(start, start + PAGES_PER_JUZ):
            if p not in used:
                return p
        return None

    @property
    def is_awaiting_signoff(self) -> bool:
        return bool(self.submitted)

    @property
    def needs_recital(self) -> bool:
        return bool(self.returned)

    @property
    def batch_size(self) -> int:
        return len(self.memorized_unsubmitted)

    @property
    def memorize_gate(self) -> Gate:
        return can_memorize_more(
            juz=self.juz,
            submitted_pages=self.submitted,
            returned_pages=self.returned,
        )

    @property
    def submit_gate(self) -> Gate:
        return can_submit(
            memorized_unsubmitted=self.memorized_unsubmitted, returned=self.returned
        )

    @property
    def all_signed_off(self) -> bool:
        return len(self.signed_off) >= PAGES_PER_JUZ

    @property
    def headline(self) -> str:
        """One line describing where this juz stands in the Stage 1 loop."""
        if self.returned:
            n = len(self.returned)
            return f"{n} page{'s' if n != 1 else ''} to recite again"
        if self.submitted:
            n = len(self.submitted)
            return f"{n} page{'s' if n != 1 else ''} with your Muhaffiz"
        if self.all_signed_off:
            return "All 20 pages signed off — ready for Stage 2"
        if self.memorized_unsubmitted:
            n = len(self.memorized_unsubmitted)
            return f"{n} page{'s' if n != 1 else ''} ready to recite"
        return f"{len(self.signed_off)} of {PAGES_PER_JUZ} pages signed off"
