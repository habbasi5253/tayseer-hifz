"""Juz certification — the spine of the app.

One juz is one certification. A student does not memorize "pages"; they take a
juz through a pipeline and come out the other side holding a certification for
it. Every page-level fact in this app exists to answer a juz-level question.

The pipeline, in order:

    Tilawat  →  Memorization  →  Evaluation  →  Certified

  Tilawat        recite the whole juz to the Stage 1 Muhaffiz. A gate: the
                 Muhaffiz can send it back, and often does.
  Memorization   pages recited to Muhaffiz 1 in batches and signed off.
  Evaluation     Stage 2, with a different mandatory Muhaffiz. Every one of the
                 20 pages must pass hifz, makharij and tajweed independently.
  Certified      all 20 pages have passed.

Certification is **held, not banked.** The 30-day rule keeps applying after it
is earned: if pages fall outside their window the certification goes at-risk and
then lapses, and is renewed by re-reciting those pages. That is the honest
reading of the program — a certification that could never lapse would make the
30-day rule decorative.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import List, Optional

from app.domain.quran import PAGES_PER_JUZ, juz_name
from app.domain.revalidation import JuzTasmeeWindow
from app.domain.revalidation import Status as WindowStatus


class Stage:
    """Where a juz sits in the pipeline."""

    NOT_STARTED = "not_started"
    MEMORIZING = "memorizing"
    EVALUATION = "evaluation"
    CERTIFIED = "certified"

    ORDER = [NOT_STARTED, MEMORIZING, EVALUATION, CERTIFIED]
    LABEL = {
        NOT_STARTED: "Not started",
        MEMORIZING: "Memorizing",
        EVALUATION: "Tasmee",
        CERTIFIED: "Passed",
    }
    # What the student is actually doing at this step, in plain words.
    BLURB = {
        NOT_STARTED: "Not begun",
        MEMORIZING: "Memorizing and reciting to your Stage 1 Muhaffiz",
        EVALUATION: "Tasmee of the whole juz to your Stage 2 Muhaffiz",
        CERTIFIED: "Passed by your Stage 2 Muhaffiz",
    }


class Health:
    """Whether a certification is being kept current. Only meaningful once
    pages have entered Stage 2."""

    NOT_APPLICABLE = "n/a"
    CURRENT = "current"
    AT_RISK = "at_risk"
    LAPSED = "lapsed"

    LABEL = {
        NOT_APPLICABLE: "",
        CURRENT: "Current",
        AT_RISK: "Renew soon",
        LAPSED: "Needs renewal",
    }
    TONE = {
        NOT_APPLICABLE: "neutral",
        CURRENT: "good",
        AT_RISK: "soon",
        LAPSED: "attention",
    }


# The pipeline is weighted so the bar reflects effort, not step count.
# Memorization is the long middle; certification is not 75% done the moment
# memorization begins.
_MEMORIZE_WEIGHT = 0.60
_EVALUATE_WEIGHT = 0.40


@dataclass
class JuzCertification:
    """Everything about one juz, as one object the UI can render directly."""

    juz: int
    stage: str
    pages_memorized: int
    pages_evaluated: int          # pages that have ever passed a Stage 2 tasmee
    window: Optional[JuzTasmeeWindow]
    certified_on: Optional[date] = None

    # --- identity ---
    @property
    def name(self) -> str:
        return juz_name(self.juz)

    @property
    def label(self) -> str:
        return Stage.LABEL[self.stage]

    @property
    def blurb(self) -> str:
        return Stage.BLURB[self.stage]

    @property
    def step_index(self) -> int:
        return Stage.ORDER.index(self.stage)

    @property
    def pipeline_steps(self) -> List[dict]:
        """The stepper, derived from Stage.ORDER rather than hardcoded.

        The UI used to carry its own list of step labels and indices. When the
        tilawat stage was removed the indices shifted underneath it and a juz
        being memorized rendered "Tilawat" as its active step. Deriving it here
        means the two cannot disagree.
        """
        here = self.step_index
        return [
            {
                "label": Stage.LABEL[stage],
                "state": "done" if here > i else ("now" if here == i else ""),
            }
            for i, stage in enumerate(Stage.ORDER)
            if stage != Stage.NOT_STARTED
        ]

    @property
    def is_certified(self) -> bool:
        return self.stage == Stage.CERTIFIED

    @property
    def is_started(self) -> bool:
        return self.stage != Stage.NOT_STARTED

    @property
    def is_active(self) -> bool:
        """In flight — the student is working on it right now."""
        return self.stage in (Stage.MEMORIZING, Stage.EVALUATION)

    # --- progress ---
    @property
    def percent(self) -> int:
        """Weighted progress through the whole pipeline, 0-100."""
        if self.stage == Stage.NOT_STARTED:
            return 0
        pct = 0.0
        pct += _MEMORIZE_WEIGHT * min(1.0, self.pages_memorized / PAGES_PER_JUZ)
        pct += _EVALUATE_WEIGHT * min(1.0, self.pages_evaluated / PAGES_PER_JUZ)
        return int(round(pct * 100))

    @property
    def memorize_percent(self) -> int:
        return int(round(self.pages_memorized / PAGES_PER_JUZ * 100))

    @property
    def evaluate_percent(self) -> int:
        return int(round(self.pages_evaluated / PAGES_PER_JUZ * 100))

    @property
    def pages_remaining_to_memorize(self) -> int:
        return max(0, PAGES_PER_JUZ - self.pages_memorized)

    @property
    def pages_remaining_to_evaluate(self) -> int:
        return max(0, PAGES_PER_JUZ - self.pages_evaluated)

    # --- health (the 30-day rule, expressed at certification level) ---
    @property
    def health(self) -> str:
        """Where this juz stands against its single 30-day tasmee deadline."""
        w = self.window
        if w is None or not w.has_started or w.is_passed:
            return Health.NOT_APPLICABLE
        if w.status == WindowStatus.EXPIRED:
            return Health.LAPSED
        if w.status in (WindowStatus.DUE_SOON, WindowStatus.DUE_TODAY):
            return Health.AT_RISK
        return Health.CURRENT

    @property
    def health_label(self) -> str:
        return Health.LABEL[self.health]

    @property
    def tone(self) -> str:
        """The single colour this certification shows in the UI."""
        if self.health in (Health.LAPSED, Health.AT_RISK):
            return Health.TONE[self.health]
        if self.is_certified:
            return "good"
        if self.is_active:
            return "soon" if self.stage != Stage.EVALUATION else "good"
        return "neutral"

    @property
    def pages_to_recite(self) -> int:
        """Pages of the juz still to tasmee in the current attempt."""
        return self.window.pages_remaining if self.window else PAGES_PER_JUZ

    @property
    def days_until_lapse(self) -> Optional[int]:
        return self.window.days_remaining if self.window else None

    @property
    def tasmee_attempt(self) -> int:
        return self.window.attempt if self.window else 1

    # --- what to do next ---
    @property
    def next_action(self) -> str:
        """One sentence. What moves this certification forward today."""
        if self.stage == Stage.NOT_STARTED:
            return "Start memorizing this juz."
        if self.stage == Stage.MEMORIZING:
            n = self.pages_remaining_to_memorize
            return f"{n} page{'s' if n != 1 else ''} left to memorize."
        if self.stage == Stage.EVALUATION:
            w = self.window
            if w is None or not w.has_started:
                return "Waiting to start tasmee with your Stage 2 Muhaffiz."
            if w.is_expired:
                return (
                    "The 30 days ran out — tasmee starts again from page 1 of this juz."
                )
            return f"{w.pages_remaining} pages left, {w.countdown_text.lower()}."
        return "Passed. Keep it in your murajaat rotation."

    @property
    def status_line(self) -> str:
        """The one-line status shown on the certificate tile."""
        if self.stage == Stage.NOT_STARTED:
            return "Not started"
        if self.stage == Stage.MEMORIZING:
            return f"{self.pages_memorized} of {PAGES_PER_JUZ} pages memorized"
        if self.stage == Stage.EVALUATION:
            w = self.window
            if w and w.has_started:
                return f"{w.passed_count} of {PAGES_PER_JUZ} recited · {w.countdown_text}"
            return "Ready for tasmee"
        return self.health_label


def classify(
    *,
    juz: int,
    pages_memorized: int,
    pages_evaluated: int,
    window: Optional[JuzTasmeeWindow],
    certified_on: Optional[date] = None,
) -> JuzCertification:
    """Derive the pipeline stage from the underlying facts.

    Stage is computed rather than stored so it can never disagree with the page
    records underneath it — the classic bug where a status column says
    "certified" while a page sits un-evaluated.
    """
    if window is not None and window.is_passed:
        stage = Stage.CERTIFIED
    elif pages_memorized >= PAGES_PER_JUZ:
        stage = Stage.EVALUATION
    elif pages_memorized > 0:
        stage = Stage.MEMORIZING
    else:
        stage = Stage.NOT_STARTED

    return JuzCertification(
        juz=juz,
        stage=stage,
        pages_memorized=pages_memorized,
        pages_evaluated=pages_evaluated,
        window=window,
        certified_on=certified_on,
    )


@dataclass
class CertificationBoard:
    """All 30 certifications, plus the roll-ups the dashboard needs."""

    items: List[JuzCertification]

    def get(self, juz: int) -> Optional[JuzCertification]:
        return next((c for c in self.items if c.juz == juz), None)

    @property
    def certified(self) -> List[JuzCertification]:
        return [c for c in self.items if c.is_certified]

    @property
    def held_current(self) -> List[JuzCertification]:
        return [c for c in self.certified if c.health != Health.LAPSED]

    @property
    def lapsed(self) -> List[JuzCertification]:
        return [c for c in self.items if c.health == Health.LAPSED]

    @property
    def at_risk(self) -> List[JuzCertification]:
        return [c for c in self.items if c.health == Health.AT_RISK]

    @property
    def in_progress(self) -> List[JuzCertification]:
        return [c for c in self.items if c.is_active]

    @property
    def not_started(self) -> List[JuzCertification]:
        return [c for c in self.items if c.stage == Stage.NOT_STARTED]

    @property
    def needs_attention(self) -> List[JuzCertification]:
        """Lapsed first, then at-risk, then whatever is in flight."""
        return self.lapsed + self.at_risk

    @property
    def total_percent(self) -> int:
        """Progress toward all 30 certifications."""
        if not self.items:
            return 0
        return int(round(sum(c.percent for c in self.items) / (len(self.items) * 100) * 100))

    @property
    def headline(self) -> str:
        n_cert = len(self.certified)
        n_lapsed = len(self.lapsed)
        if n_lapsed:
            return (
                f"{n_lapsed} certificate{'s' if n_lapsed != 1 else ''} "
                f"need{'' if n_lapsed != 1 else 's'} renewing"
            )
        if n_cert:
            return f"{n_cert} of 30 certified"
        active = self.in_progress
        if active:
            return f"Juz {active[0].juz} in progress"
        return "Ready to begin your first juz"


def build_board(items: List[JuzCertification]) -> CertificationBoard:
    return CertificationBoard(items=sorted(items, key=lambda c: c.juz))
