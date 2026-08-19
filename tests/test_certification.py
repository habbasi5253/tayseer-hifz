"""Tests for juz certification — the app's primary unit."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.domain.certification import Health, Stage, build_board, classify
from app.domain.quran import juz_pages
from app.domain.revalidation import build_window

UTC = timezone.utc
NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


def window_for(juz, *, passed_pages=0, started_days_ago=5, passed=False):
    """A tasmee window opened `started_days_ago` with `passed_pages` recited."""
    return build_window(
        juz=juz,
        tz_name="UTC",
        window_started_at=NOW - timedelta(days=started_days_ago),
        passed_at=NOW - timedelta(days=1) if passed else None,
        passed_pages=juz_pages(juz)[:passed_pages],
        now=NOW,
    )


def cert(juz=1, **kw):
    kw.setdefault("pages_memorized", 0)
    kw.setdefault("pages_evaluated", 0)
    kw.setdefault("window", None)
    return classify(juz=juz, **kw)


# --- Pipeline stage is derived, never stored ---------------------------------


def test_stage_progression():
    assert cert().stage == Stage.NOT_STARTED
    assert cert(pages_memorized=1).stage == Stage.MEMORIZING
    assert cert(pages_memorized=1).stage == Stage.MEMORIZING
    assert cert(pages_memorized=19).stage == Stage.MEMORIZING
    assert cert(pages_memorized=20).stage == Stage.EVALUATION
    # Certified means Muhaffiz 2 passed the juz, not merely that 20 pages were recited.
    assert cert(pages_memorized=20, pages_evaluated=20,
                window=window_for(1, passed_pages=20)).stage == Stage.EVALUATION
    assert cert(pages_memorized=20, pages_evaluated=20,
                window=window_for(1, passed_pages=20, passed=True)).stage == Stage.CERTIFIED


def test_certification_requires_muhaffiz_two_to_pass_the_juz():
    """Reciting all 20 pages is not the same as being passed."""
    c = cert(pages_memorized=20, pages_evaluated=20,
             window=window_for(1, passed_pages=20))
    assert c.stage == Stage.EVALUATION
    assert c.is_certified is False


def test_progress_is_weighted_not_step_counted():
    """Memorization is the long middle; the bar should reflect effort."""
    half_memorized = cert(pages_memorized=10)
    assert 25 <= half_memorized.percent <= 40

    assert cert(pages_memorized=20, pages_evaluated=20,
                window=window_for(1, passed_pages=20, passed=True)).percent == 100


# --- Health follows the single 30-day deadline ------------------------------


def test_a_passed_juz_is_certified_and_carries_no_clock():
    c = cert(pages_memorized=20,
             window=window_for(1, passed_pages=20, passed=True))
    assert c.is_certified
    assert c.health == Health.NOT_APPLICABLE  # the clock is finished
    assert "murajaat rotation" in c.next_action


def test_a_running_clock_reads_as_current():
    c = cert(pages_memorized=20,
             window=window_for(1, passed_pages=6, started_days_ago=3))
    assert c.stage == Stage.EVALUATION
    assert c.health == Health.CURRENT
    assert "14 pages left" in c.next_action


def test_the_last_week_reads_as_at_risk():
    c = cert(pages_memorized=20,
             window=window_for(1, passed_pages=12, started_days_ago=26))
    assert c.health == Health.AT_RISK
    assert c.tone == "soon"


def test_running_out_of_time_reads_as_a_restart_not_a_failure():
    """Expiry means starting the juz's tasmee again, and the copy says so."""
    c = cert(pages_memorized=20,
             window=window_for(1, passed_pages=17, started_days_ago=35))
    assert c.health == Health.LAPSED
    assert "starts again from page 1" in c.next_action
    # Memorization is untouched — only the tasmee attempt resets.
    assert c.pages_memorized == 20


def test_health_is_not_applicable_before_stage_two():
    c = cert(pages_memorized=8)
    assert c.health == Health.NOT_APPLICABLE
    assert c.health_label == ""
    assert "12 pages left to memorize" in c.next_action


# --- Board roll-ups ----------------------------------------------------------


def test_board_separates_passed_juz_from_the_one_under_a_clock():
    passed = cert(1, pages_memorized=20,
                  window=window_for(1, passed_pages=20, passed=True))
    expired = cert(2, pages_memorized=20,
                   window=window_for(2, passed_pages=17, started_days_ago=35))
    working = cert(3, pages_memorized=11)
    board = build_board([passed, expired, working])

    assert [c.juz for c in board.certified] == [1]
    assert [c.juz for c in board.lapsed] == [2]
    assert [c.juz for c in board.in_progress] == [2, 3]
    assert "renewing" in board.headline


def test_board_headline_prefers_the_restart_over_a_celebration():
    passed = cert(1, pages_memorized=20,
                  window=window_for(1, passed_pages=20, passed=True))
    expired = cert(2, pages_memorized=20,
                   window=window_for(2, passed_pages=9, started_days_ago=40))
    assert "certified" in build_board([passed]).headline
    assert "renewing" in build_board([passed, expired]).headline


def test_empty_board_invites_a_start():
    board = build_board([cert(j) for j in range(1, 31)])
    assert board.total_percent == 0
    assert board.certified == []
    assert "Ready to begin" in board.headline


def test_the_pipeline_stepper_matches_the_actual_stage():
    """Regression: the UI once carried its own step list and drifted.

    When the tilawat stage was removed the hardcoded indices shifted underneath
    the template, and a juz being memorized rendered "Tilawat" as its active
    step. The steps are now derived from Stage.ORDER so the two cannot disagree.
    """
    memorizing = cert(pages_memorized=5)
    labels = [s["label"] for s in memorizing.pipeline_steps]
    assert "Tilawat" not in labels
    assert len(labels) == len(Stage.ORDER) - 1  # NOT_STARTED is not a step

    active = [s["label"] for s in memorizing.pipeline_steps if s["state"] == "now"]
    assert active == [Stage.LABEL[Stage.MEMORIZING]]

    evaluating = cert(pages_memorized=20, window=window_for(1, passed_pages=3))
    assert [s["label"] for s in evaluating.pipeline_steps if s["state"] == "now"] == [
        Stage.LABEL[Stage.EVALUATION]
    ]
    assert [s["label"] for s in evaluating.pipeline_steps if s["state"] == "done"] == [
        Stage.LABEL[Stage.MEMORIZING]
    ]
