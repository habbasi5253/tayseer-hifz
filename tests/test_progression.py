"""Tests for the Stage 1 sign-off gate and the juz-to-juz gate."""

from __future__ import annotations

from app.domain.progression import (
    DEFAULT_JUZ_ORDER,
    JuzWorkface,
    can_memorize_more,
    can_start_juz,
    can_submit,
    parse_juz_order,
    previous_juz_in,
)
from app.domain.quran import global_page as g


def face(**kw):
    kw.setdefault("juz", 3)
    kw.setdefault("signed_off", [])
    kw.setdefault("submitted", [])
    kw.setdefault("returned", [])
    kw.setdefault("memorized_unsubmitted", [])
    return JuzWorkface(**kw)


# --- The sign-off gate -------------------------------------------------------


def test_a_batch_may_be_any_size_before_submitting():
    """'One page at a time, or as many pages as you want.'"""
    f = face(memorized_unsubmitted=[g(3, 1), g(3, 2), g(3, 3)])
    assert f.memorize_gate.allowed is True
    assert f.batch_size == 3
    assert f.next_page == g(3, 4)


def test_submitting_blocks_further_memorization():
    """The gate is on a pending submission, not on page count."""
    f = face(submitted=[g(3, 1), g(3, 2)])
    gate = f.memorize_gate
    assert gate.allowed is False
    assert "with your Muhaffiz" in gate.reason
    assert f.is_awaiting_signoff is True


def test_sign_off_reopens_memorization():
    f = face(signed_off=[g(3, 1), g(3, 2)])
    assert f.memorize_gate.allowed is True
    assert f.next_page == g(3, 3)


def test_a_returned_page_blocks_progress_until_it_passes():
    f = face(signed_off=[g(3, 1)], returned=[g(3, 2)])
    gate = f.memorize_gate
    assert gate.allowed is False
    assert "again" in gate.reason
    assert f.needs_recital is True


def test_cannot_submit_an_empty_batch():
    assert can_submit(memorized_unsubmitted=[]).allowed is False
    assert can_submit(memorized_unsubmitted=[g(3, 1)]).allowed is True


def test_gate_reason_names_the_actual_pages():
    gate = can_memorize_more(
        juz=3, submitted_pages=[g(3, 4), g(3, 5)], returned_pages=[]
    )
    assert "p.4" in gate.reason and "p.5" in gate.reason


# --- The juz gate ------------------------------------------------------------


def test_next_juz_needs_the_previous_one_passed_by_muhaffiz_two():
    blocked = can_start_juz(juz=4, previous_juz=3, previous_juz_passed=False)
    assert blocked.allowed is False
    assert "Juz 3" in blocked.reason and "Stage 2 Muhaffiz" in blocked.reason
    assert can_start_juz(juz=4, previous_juz=3, previous_juz_passed=True).allowed is True


def test_a_student_joining_midway_is_not_blocked_by_juz_they_never_did():
    assert can_start_juz(juz=12, previous_juz=11, previous_juz_passed=False,
                         is_entry_point=True).allowed is True


def test_the_first_juz_in_the_order_never_waits():
    assert can_start_juz(juz=1, previous_juz=None, previous_juz_passed=False).allowed is True


# --- Custom juz order --------------------------------------------------------


def test_a_reversed_order_gates_on_the_right_juz():
    """Working backwards from 30, juz 29 waits on juz 30 — not juz 28."""
    order = parse_juz_order(",".join(str(i) for i in range(30, 0, -1)))
    assert previous_juz_in(order, 29) == 30
    assert previous_juz_in(order, 30) is None


def test_default_order_is_one_to_thirty():
    order = parse_juz_order(None)
    assert order[0] == 1 and order[-1] == 30
    assert previous_juz_in(order, 4) == 3


def test_a_malformed_order_falls_back_rather_than_half_applying():
    """A broken sequence would gate the wrong juz, which is worse than ignoring it."""
    assert parse_juz_order("1,2,oops") == DEFAULT_JUZ_ORDER
    assert parse_juz_order("1,2,3") == DEFAULT_JUZ_ORDER          # incomplete
    assert parse_juz_order("1,1," + ",".join(str(i) for i in range(2, 30))) == DEFAULT_JUZ_ORDER


# --- Workface headline -------------------------------------------------------


def test_headline_reports_the_blocking_state_first():
    assert "recite again" in face(returned=[g(3, 2)]).headline.lower()
    assert "with your Muhaffiz" in face(submitted=[g(3, 2)]).headline
    assert "ready to recite" in face(memorized_unsubmitted=[g(3, 2)]).headline


def test_full_juz_reports_ready_for_stage_two():
    f = face(signed_off=[g(3, i) for i in range(1, 21)])
    assert f.all_signed_off is True
    assert "Stage 2" in f.headline
    assert f.next_page is None


def test_a_returned_page_can_be_resubmitted_on_its_own():
    """Regression: the student must not deadlock.

    A returned page blocks new memorization, so if it also failed to count as
    submittable the student would be stuck with no legal move at all.
    """
    f = face(signed_off=[g(3, 1)], returned=[g(3, 2)])
    assert f.memorize_gate.allowed is False   # cannot go forward
    assert f.submit_gate.allowed is True      # but can recite it again
    assert can_submit(memorized_unsubmitted=[], returned=[g(3, 2)]).allowed is True


def test_juz_order_setting_survives_a_round_trip():
    """What Settings stores must parse back to the same sequence."""
    reversed_order = ",".join(str(i) for i in range(30, 0, -1))
    assert parse_juz_order(reversed_order)[0] == 30
    assert parse_juz_order(reversed_order) != DEFAULT_JUZ_ORDER
    assert parse_juz_order(" 30, 29 ,28," + ",".join(str(i) for i in range(27, 0, -1)))[1] == 29


# --- Murajaat portions -------------------------------------------------------


def test_portion_pages_split_the_juz_in_half():
    from app.domain.quran import page_index_in_juz
    from app.domain.revision import Portion, portion_pages

    first = [page_index_in_juz(p) for p in portion_pages(4, Portion.FIRST_HALF)]
    second = [page_index_in_juz(p) for p in portion_pages(4, Portion.SECOND_HALF)]
    assert first == list(range(1, 11))
    assert second == list(range(11, 21))
    assert len(portion_pages(4, Portion.FULL)) == 20


def test_an_unknown_portion_falls_back_to_the_whole_juz():
    """A bad value must never silently shrink what gets revised."""
    from app.domain.revision import Portion, normalize_portion, portion_pages

    assert normalize_portion("nonsense") == Portion.FULL
    assert normalize_portion(None) == Portion.FULL
    assert len(portion_pages(4, "nonsense")) == 20
