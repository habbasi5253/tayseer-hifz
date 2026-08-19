"""Query and command layer.

Everything that touches the database lives here, so the domain package stays
pure functions over plain data and remains trivially testable.

One storage note that matters for correctness: SQLAlchemy's SQLite dialect
stores `DateTime(timezone=True)` as a naive wall-clock string and hands back
naive datetimes on read. Because this layer only ever *writes* aware UTC, the
stored wall clock is UTC, and `dates.ensure_utc` re-attaches UTC on the way
back. Postgres returns aware datetimes directly. Both paths converge on aware
UTC before any date math happens — which is why `ensure_utc` treats naive input
as UTC rather than guessing the server's zone.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.domain import certification as cert
from app.domain import dates as dt
from app.domain import dayplan
from app.domain import progression as prog
from app.domain import revalidation as rv
from app.domain import tracking
from app.domain.marhala import get_marhala, tasmee_page_target
from app.domain.quran import (
    JUZ_COUNT,
    PAGES_PER_JUZ,
    TOTAL_PAGES,
    juz_of_page,
    juz_pages,
    page_index_in_juz,
)
from app.domain.revision import (
    JuzRotationEntry,
    normalize_portion,
    MethodMix,
    build_method_mix,
    evaluate_method_mix,
    rank_neglected_juz,
)
from app.domain.schedule import (
    PaceProjection,
    TimeBudget,
    project_completion,
    recommend_schedule,
)
from app.domain.streak import compute_streak
from app.models import (
    Assignment,
    AssignmentStatus,
    MurajaatClass,
    MurajaatPlan,
    DailyRevisionLog,
    JuzStage,
    MuhaffizProfile,
    PasswordResetToken,
    PageProgress,
    RevisionKind,
    Stage,
    StudentJuz,
    StudentProfile,
    TasmeePageResult,
    TasmeeSession,
    User,
    WeakSpot,
)


class ProgramRuleViolation(Exception):
    """Raised when an action would break a Barnamaj Tayseer rule."""


# --- Students & assignments --------------------------------------------------


def get_student(db: Session, student_id: int) -> Optional[StudentProfile]:
    return db.execute(
        select(StudentProfile)
        .options(joinedload(StudentProfile.user))
        .where(StudentProfile.id == student_id)
    ).scalar_one_or_none()


def student_timezone(student: StudentProfile) -> str:
    return student.user.timezone if student.user else "UTC"


def active_assignments(
    db: Session, student_id: int, stage: Optional[str] = None
) -> List[Assignment]:
    stmt = (
        select(Assignment)
        .options(joinedload(Assignment.muhaffiz).joinedload(MuhaffizProfile.user))
        .where(
            Assignment.student_id == student_id,
            Assignment.status == AssignmentStatus.ACTIVE,
        )
    )
    if stage:
        stmt = stmt.where(Assignment.stage == stage)
    return list(db.execute(stmt).scalars().all())


def muhaffiz_for(
    db: Session, student_id: int, stage: str, juz: Optional[int] = None
) -> Optional[MuhaffizProfile]:
    """The Muhaffiz covering this stage, preferring a juz-specific assignment."""
    rows = active_assignments(db, student_id, stage)
    if juz is not None:
        for a in rows:
            if a.juz == juz:
                return a.muhaffiz
    for a in rows:
        if a.juz is None:
            return a.muhaffiz
    return rows[0].muhaffiz if rows else None


def assign_muhaffiz(
    db: Session,
    *,
    student_id: int,
    muhaffiz_id: int,
    stage: str,
    juz: Optional[int] = None,
) -> Assignment:
    """Create an assignment, enforcing the program's separation rule.

    The mandatory-second-Muhaffiz rule is the whole point of Stage 2: the person
    who taught you the juz cannot be the person who certifies you know it. This
    is enforced here rather than in the database because the check spans rows
    and needs to understand juz-level vs programme-level scope.
    """
    if stage not in Stage.ALL:
        raise ProgramRuleViolation(f"unknown stage {stage!r}")

    other = Stage.ONE if stage == Stage.TWO else Stage.TWO
    for a in active_assignments(db, student_id, other):
        overlaps = a.juz is None or juz is None or a.juz == juz
        if a.muhaffiz_id == muhaffiz_id and overlaps:
            raise ProgramRuleViolation(
                "Stage 1 and Stage 2 must be different people. "
                "This Muhaffiz already holds the other stage for this student."
            )

    # Supersede any existing active assignment for the same stage and scope.
    for a in active_assignments(db, student_id, stage):
        if a.juz == juz:
            a.status = AssignmentStatus.ENDED
            a.ended_at = dt.utcnow()

    assignment = Assignment(
        student_id=student_id, muhaffiz_id=muhaffiz_id, stage=stage, juz=juz
    )
    db.add(assignment)
    db.flush()
    return assignment


def students_of_muhaffiz(db: Session, muhaffiz_id: int, stage: Optional[str] = None):
    stmt = (
        select(Assignment)
        .options(
            joinedload(Assignment.student).joinedload(StudentProfile.user),
        )
        .where(
            Assignment.muhaffiz_id == muhaffiz_id,
            Assignment.status == AssignmentStatus.ACTIVE,
        )
    )
    if stage:
        stmt = stmt.where(Assignment.stage == stage)
    return list(db.execute(stmt).scalars().all())


# --- Page progress -----------------------------------------------------------


def get_juz_record(db: Session, student_id: int, juz: int) -> StudentJuz:
    row = db.execute(
        select(StudentJuz).where(StudentJuz.student_id == student_id, StudentJuz.juz == juz)
    ).scalar_one_or_none()
    if row is None:
        row = StudentJuz(student_id=student_id, juz=juz, stage=JuzStage.NOT_STARTED)
        db.add(row)
        db.flush()
    return row


def all_juz_records(db: Session, student_id: int) -> Dict[int, StudentJuz]:
    rows = db.execute(select(StudentJuz).where(StudentJuz.student_id == student_id)).scalars()
    return {r.juz: r for r in rows}


def memorized_pages(db: Session, student_id: int) -> List[PageProgress]:
    return list(
        db.execute(
            select(PageProgress)
            .where(PageProgress.student_id == student_id, PageProgress.memorized_at.is_not(None))
            .order_by(PageProgress.page)
        )
        .scalars()
        .all()
    )


def memorized_page_count(db: Session, student_id: int) -> int:
    return int(
        db.execute(
            select(func.count(PageProgress.id)).where(
                PageProgress.student_id == student_id,
                PageProgress.memorized_at.is_not(None),
            )
        ).scalar_one()
    )


def pages_memorized_in_juz(db: Session, student_id: int, juz: int) -> int:
    return int(
        db.execute(
            select(func.count(PageProgress.id)).where(
                PageProgress.student_id == student_id,
                PageProgress.juz == juz,
                PageProgress.memorized_at.is_not(None),
            )
        ).scalar_one()
    )


def juz_workface(db: Session, student: StudentProfile, juz: int) -> prog.JuzWorkface:
    """The Stage 1 picture for one juz: signed off, pending, returned, next up."""
    rows = list(
        db.execute(
            select(PageProgress).where(
                PageProgress.student_id == student.id, PageProgress.juz == juz
            )
        ).scalars()
    )
    return prog.JuzWorkface(
        juz=juz,
        signed_off=[r.page for r in rows if r.is_signed_off],
        submitted=[r.page for r in rows if r.is_pending_signoff],
        returned=[r.page for r in rows if r.is_returned],
        memorized_unsubmitted=[
            r.page
            for r in rows
            if r.memorized_at and not r.submitted_at and not r.signed_off_at and not r.returned_at
        ],
    )


def juz_order_for(student: StudentProfile) -> tuple:
    """The student's juz sequence. Defaults to 1..30."""
    return prog.parse_juz_order(student.juz_order)


def can_start_juz(db: Session, student: StudentProfile, juz: int) -> prog.Gate:
    """A juz stays shut until Muhaffiz 2 has passed the one before it.

    "Before it" means the student's own sequence, not juz - 1.
    """
    order = juz_order_for(student)
    previous = prog.previous_juz_in(order, juz)
    records = all_juz_records(db, student.id)
    started = [j for j, r in records.items() if r.stage != JuzStage.NOT_STARTED]
    # Whichever juz they actually began on is their entry point.
    entry = min(started, key=lambda j: list(order).index(j)) if started else None
    prev_rec = records.get(previous) if previous else None
    return prog.can_start_juz(
        juz=juz,
        previous_juz=previous,
        previous_juz_passed=bool(prev_rec and prev_rec.juz_passed),
        is_entry_point=(entry is None or juz == entry),
    )


def next_juz_after(student: StudentProfile, juz: int) -> Optional[int]:
    order = list(juz_order_for(student))
    try:
        idx = order.index(juz)
    except ValueError:
        return None
    return order[idx + 1] if idx + 1 < len(order) else None


def mark_page_memorized(
    db: Session, student: StudentProfile, page: int, when: Optional[datetime] = None
) -> PageProgress:
    """Record a page as memorized, enforcing both program gates.

    The student may build a batch of any size, but may not run ahead of pages
    that are still with their Muhaffiz or that have been sent back — and may not
    open a juz whose predecessor Muhaffiz 2 has not yet passed.
    """
    when = when or dt.utcnow()
    juz = juz_of_page(page)
    tz = student_timezone(student)

    juz_gate = can_start_juz(db, student, juz)
    if not juz_gate:
        raise ProgramRuleViolation(juz_gate.reason)

    face = juz_workface(db, student, juz)
    gate = face.memorize_gate
    if not gate:
        raise ProgramRuleViolation(gate.reason)

    record = get_juz_record(db, student.id, juz)
    row = db.execute(
        select(PageProgress).where(
            PageProgress.student_id == student.id, PageProgress.page == page
        )
    ).scalar_one_or_none()
    if row is None:
        row = PageProgress(student_id=student.id, page=page, juz=juz)
        db.add(row)

    if row.memorized_at is None:
        row.memorized_at = when
        row.memorized_on = dt.local_date(when, tz)

    if record.stage == JuzStage.NOT_STARTED:
        record.stage = JuzStage.MEMORIZING
        record.memorization_started_at = record.memorization_started_at or when

    db.flush()
    return row


def submit_batch(
    db: Session, student: StudentProfile, juz: int, when: Optional[datetime] = None
) -> List[PageProgress]:
    """Send the memorized batch to Muhaffiz 1 to be heard."""
    when = when or dt.utcnow()
    face = juz_workface(db, student, juz)
    gate = face.submit_gate
    if not gate:
        raise ProgramRuleViolation(gate.reason)

    rows = list(
        db.execute(
            select(PageProgress).where(
                PageProgress.student_id == student.id,
                PageProgress.page.in_(list(face.memorized_unsubmitted) + list(face.returned)),
            )
        ).scalars()
    )
    for r in rows:
        r.submitted_at = when
        r.returned_at = None
    db.flush()
    return rows


def sign_off_pages(
    db: Session,
    student: StudentProfile,
    *,
    muhaffiz_id: Optional[int],
    passed_pages: Sequence[int],
    returned_pages: Sequence[int] = (),
    notes: Optional[str] = None,
    when: Optional[datetime] = None,
) -> Dict[str, List[int]]:
    """Muhaffiz 1 hears a batch: each page is signed off or sent back.

    A returned page clears its submission so it rejoins the batch to be recited
    again, and the student stays blocked from new pages until it passes. That is
    the gate doing its job rather than a punishment.
    """
    when = when or dt.utcnow()
    juz = juz_of_page(passed_pages[0]) if passed_pages else (
        juz_of_page(returned_pages[0]) if returned_pages else None
    )
    if juz is None:
        raise ProgramRuleViolation("Select at least one page to sign off.")

    rows = {
        r.page: r
        for r in db.execute(
            select(PageProgress).where(
                PageProgress.student_id == student.id,
                PageProgress.page.in_(list(passed_pages) + list(returned_pages)),
            )
        ).scalars()
    }

    for page in passed_pages:
        r = rows.get(page)
        if r is None:
            continue
        r.signed_off_at = when
        r.signed_off_by_muhaffiz_id = muhaffiz_id
        r.returned_at = None

    for page in returned_pages:
        r = rows.get(page)
        if r is None:
            continue
        r.returned_at = when
        r.submitted_at = None
        r.signed_off_at = None

    # Also record it as a Stage 1 recitation so the history and the per-page
    # "days since last heard" view stay complete.
    results = [{"page": p, "hifz": True} for p in passed_pages]
    results += [{"page": p, "hifz": False} for p in returned_pages]
    if results:
        record_tasmee(
            db, student, stage=Stage.ONE, juz=juz, muhaffiz_id=muhaffiz_id,
            page_results=results, notes=notes, when=when,
        )

    db.flush()

    # A juz whose 20 pages are all signed off is ready for Muhaffiz 2.
    face = juz_workface(db, student, juz)
    if face.all_signed_off:
        complete_juz_memorization(db, student, juz, when=when)

    return {"passed": list(passed_pages), "returned": list(returned_pages)}


def complete_juz_memorization(
    db: Session, student: StudentProfile, juz: int, when: Optional[datetime] = None
) -> StudentJuz:
    """Stage 1 done for this juz — hand it to Muhaffiz 2 for the full tasmee."""
    when = when or dt.utcnow()
    record = get_juz_record(db, student.id, juz)
    record.memorization_completed_at = record.memorization_completed_at or when
    record.stage = JuzStage.STAGE2
    record.stage2_started_at = record.stage2_started_at or when
    # Muhaffiz 1's full sign-off is what starts the 30-day tasmee clock.
    record.tasmee_window_started_at = record.tasmee_window_started_at or when

    rows = db.execute(
        select(PageProgress).where(
            PageProgress.student_id == student.id, PageProgress.juz == juz
        )
    ).scalars()
    for r in rows:
        if r.stage2_entered_at is None:
            r.stage2_entered_at = when
    db.flush()
    return record


def pass_juz_tasmee(
    db: Session,
    student: StudentProfile,
    juz: int,
    *,
    muhaffiz_id: Optional[int],
    when: Optional[datetime] = None,
) -> StudentJuz:
    """Muhaffiz 2 passes the full-juz tasmee. This is what opens the next juz."""
    when = when or dt.utcnow()
    record = get_juz_record(db, student.id, juz)
    record.juz_tasmee_passed_at = when
    record.juz_tasmee_passed_by_id = muhaffiz_id
    record.stage2_completed_at = record.stage2_completed_at or when
    record.stage = JuzStage.COMPLETE

    # Advance the student's working juz so the next one opens on their dashboard.
    if student.current_juz == juz:
        nxt = next_juz_after(student, juz)
        if nxt is not None:
            student.current_juz = nxt
    db.flush()
    return record


# --- The 30-day rule ---------------------------------------------------------


def pages_passed_in_window(
    db: Session, student_id: int, juz: int, since: Optional[datetime]
) -> List[int]:
    """Pages of `juz` passed with Muhaffiz 2 on or after `since`.

    Scoping by the window start is how the restart-from-page-1 rule is applied:
    an expired attempt's passes stay in the table and remain visible in history,
    but stop counting toward the current attempt.
    """
    if since is None:
        return []
    rows = db.execute(
        select(TasmeePageResult.page)
        .where(
            TasmeePageResult.student_id == student_id,
            TasmeePageResult.juz == juz,
            TasmeePageResult.stage == Stage.TWO,
            TasmeePageResult.passed.is_(True),
            TasmeePageResult.recorded_at >= since,
        )
        .distinct()
    ).all()
    return sorted({p for (p,) in rows})


def tasmee_window(
    db: Session, student: StudentProfile, juz: int, now: Optional[datetime] = None
) -> rv.JuzTasmeeWindow:
    """The 30-day window for one juz."""
    record = get_juz_record(db, student.id, juz)
    started = (
        dt.ensure_utc(record.tasmee_window_started_at)
        if record.tasmee_window_started_at
        else None
    )
    return rv.build_window(
        juz=juz,
        tz_name=student_timezone(student),
        window_started_at=started,
        passed_at=(
            dt.ensure_utc(record.juz_tasmee_passed_at)
            if record.juz_tasmee_passed_at
            else None
        ),
        attempt=record.tasmee_attempt or 1,
        passed_pages=pages_passed_in_window(db, student.id, juz, started),
        now=now,
    )


def build_board(
    db: Session, student: StudentProfile, now: Optional[datetime] = None
) -> rv.TasmeeBoard:
    """Every juz that has entered Stage 2, with its clock. Computed on read."""
    records = all_juz_records(db, student.id)
    windows = [
        tasmee_window(db, student, juz, now=now)
        for juz, r in sorted(records.items())
        if r.tasmee_window_started_at is not None
    ]
    return rv.build_board(tz_name=student_timezone(student), windows=windows, now=now)


def restart_tasmee_window(
    db: Session, student: StudentProfile, juz: int, when: Optional[datetime] = None
) -> StudentJuz:
    """Begin a fresh attempt after the 30 days ran out.

    Nothing is deleted — the window start moves forward, so the previous
    attempt's passes stop counting but stay in the record for both Muhaffiz.
    """
    when = when or dt.utcnow()
    record = get_juz_record(db, student.id, juz)
    record.tasmee_window_started_at = when
    record.tasmee_attempt = (record.tasmee_attempt or 1) + 1
    db.flush()
    return record


def last_attempt_map(db: Session, student_id: int, stage: str = Stage.TWO) -> Dict[int, datetime]:
    """page -> most recent tasmee of any outcome. Context, never a countdown."""
    rows = db.execute(
        select(TasmeePageResult.page, func.max(TasmeePageResult.recorded_at))
        .where(
            TasmeePageResult.student_id == student_id,
            TasmeePageResult.stage == stage,
        )
        .group_by(TasmeePageResult.page)
    ).all()
    return {page: dt.ensure_utc(ts) for page, ts in rows if ts is not None}


# --- Stage 1 tasmee view -----------------------------------------------------


def stage1_page_view(
    db: Session, student: StudentProfile, juz: int, now: Optional[datetime] = None
) -> List[Dict]:
    """Per-page 'days since last tasmee' for the Stage 1 Muhaffiz.

    Deliberately returns data, not a decision. The program puts the choice of
    which pages to hear with the Muhaffiz, based on days elapsed and retention
    health, so the app's job is to make that judgment easy — surfacing the
    dates, the gaps, and the current revision method — and then get out of the
    way. There is no "recommended pages" field here on purpose.
    """
    tz = student_timezone(student)
    now = now or dt.utcnow()
    attempts = last_attempt_map(db, student.id, Stage.ONE)
    stage2_passed = set(last_attempt_map(db, student.id, Stage.TWO))

    memorized = {
        p.page
        for p in db.execute(
            select(PageProgress).where(
                PageProgress.student_id == student.id,
                PageProgress.juz == juz,
                PageProgress.memorized_at.is_not(None),
            )
        ).scalars()
    }

    open_spots = {
        w.page
        for w in db.execute(
            select(WeakSpot).where(
                WeakSpot.student_id == student.id,
                WeakSpot.juz == juz,
                WeakSpot.resolved_at.is_(None),
            )
        ).scalars()
        if w.page
    }

    out = []
    for page in juz_pages(juz):
        last = attempts.get(page)
        out.append(
            {
                "page": page,
                "index": page_index_in_juz(page),
                "half": 1 if page_index_in_juz(page) <= PAGES_PER_JUZ // 2 else 2,
                "memorized": page in memorized,
                "last_tasmee_at": last,
                "last_tasmee_on": dt.local_date(last, tz) if last else None,
                "days_since": dt.days_since(last, tz, now) if last else None,
                "days_since_text": dt.humanize_days(dt.days_since(last, tz, now) if last else None),
                "seen_in_stage2": page in stage2_passed,
                "has_weak_spot": page in open_spots,
            }
        )
    return out


def stage1_tasmee_target(student: StudentProfile) -> int:
    return tasmee_page_target(student.marhala)


# --- Logging -----------------------------------------------------------------


def log_revision(
    db: Session,
    student: StudentProfile,
    *,
    juz: int,
    method: int,
    duration_minutes: int = 0,
    kind: str = RevisionKind.HALI,
    note: Optional[str] = None,
    portion: str = "full",
    when: Optional[datetime] = None,
) -> DailyRevisionLog:
    when = when or dt.utcnow()
    tz = student_timezone(student)
    row = DailyRevisionLog(
        student_id=student.id,
        juz=juz,
        portion=normalize_portion(portion),
        method=int(method),
        kind=kind,
        duration_minutes=max(0, int(duration_minutes or 0)),
        note=note or None,
        logged_at=when,
        local_date=dt.local_date(when, tz),
    )
    db.add(row)
    db.flush()
    return row


def record_tasmee(
    db: Session,
    student: StudentProfile,
    *,
    stage: str,
    juz: int,
    muhaffiz_id: Optional[int],
    page_results: Sequence[Dict],
    notes: Optional[str] = None,
    when: Optional[datetime] = None,
) -> TasmeeSession:
    """Record a tasmee sitting.

    `page_results` items: {page, hifz, makharij, tajweed, notes}. In Stage 2 all
    three criteria are required and *any* failure means the page is not
    revalidated. In Stage 1 the criteria are optional, because Stage 1 tasmee is
    explicitly not a formal ikhtibar.
    """
    if stage not in Stage.ALL:
        raise ProgramRuleViolation(f"unknown stage {stage!r}")
    if not page_results:
        raise ProgramRuleViolation("A tasmee session must cover at least one page.")

    when = when or dt.utcnow()
    tz = student_timezone(student)

    if stage == Stage.TWO:
        window = tasmee_window(db, student, juz, now=when)
        if not window.has_started:
            # Without this the results are written but count toward nothing,
            # because the window that scopes them does not exist yet — a silent
            # no-op that would read to a Muhaffiz as "I recorded it and it
            # vanished".
            raise ProgramRuleViolation(
                f"Juz {juz} has not reached Stage 2 yet. All 20 pages must be signed "
                "off by the Stage 1 Muhaffiz before tasmee begins."
            )
        # Reciting after the deadline is the start of a fresh attempt, so the
        # restart happens the moment the student picks it back up rather than
        # waiting on a job to notice.
        if window.is_expired:
            restart_tasmee_window(db, student, juz, when=when)

    session = TasmeeSession(
        student_id=student.id,
        muhaffiz_id=muhaffiz_id,
        stage=stage,
        juz=juz,
        notes=notes or None,
        occurred_at=when,
        local_date=dt.local_date(when, tz),
    )
    db.add(session)
    db.flush()

    for item in page_results:
        page = int(item["page"])
        hifz = item.get("hifz")
        makharij = item.get("makharij")
        tajweed = item.get("tajweed")

        if stage == Stage.TWO:
            if hifz is None or makharij is None or tajweed is None:
                raise ProgramRuleViolation(
                    "Stage 2 requires an outcome for hifz, makharij and tajweed."
                )
            # The rule: any one failing means the page is not revalidated.
            passed = bool(hifz) and bool(makharij) and bool(tajweed)
        else:
            # Stage 1 is not a test. Absent an explicit judgement, the page was
            # heard and counts as heard.
            passed = True if hifz is None else bool(hifz)

        db.add(
            TasmeePageResult(
                session_id=session.id,
                student_id=student.id,
                page=page,
                juz=juz_of_page(page),
                stage=stage,
                hifz_pass=None if hifz is None else bool(hifz),
                makharij_pass=None if makharij is None else bool(makharij),
                tajweed_pass=None if tajweed is None else bool(tajweed),
                passed=passed,
                notes=(item.get("notes") or None),
                recorded_at=when,
            )
        )

        if stage == Stage.TWO and not passed and item.get("notes"):
            db.add(
                WeakSpot(
                    student_id=student.id,
                    juz=juz_of_page(page),
                    page=page,
                    note=item["notes"],
                    created_by_muhaffiz_id=muhaffiz_id,
                )
            )

    db.flush()
    return session


# No daily cap in Stage 2: the whole juz must fit inside 30 days, so a limit
# only ever penalises someone catching up after a slow start.
STAGE2_MIN_PAGES_PER_SITTING = 1


def stage2_pages_today(db: Session, student: StudentProfile, when: Optional[datetime] = None) -> int:
    today = dt.today_local(student_timezone(student), when)
    return int(
        db.execute(
            select(func.count(TasmeePageResult.id))
            .join(TasmeeSession, TasmeePageResult.session_id == TasmeeSession.id)
            .where(
                TasmeePageResult.student_id == student.id,
                TasmeePageResult.stage == Stage.TWO,
                TasmeeSession.local_date == today,
            )
        ).scalar_one()
    )


# --- Checkpoints -------------------------------------------------------------


def recent_revision_logs(
    db: Session,
    student_id: int,
    *,
    juz: Optional[int] = None,
    since: Optional[date] = None,
    limit: int = 200,
) -> List[DailyRevisionLog]:
    stmt = select(DailyRevisionLog).where(DailyRevisionLog.student_id == student_id)
    if juz is not None:
        stmt = stmt.where(DailyRevisionLog.juz == juz)
    if since is not None:
        stmt = stmt.where(DailyRevisionLog.local_date >= since)
    stmt = stmt.order_by(DailyRevisionLog.logged_at.desc()).limit(limit)
    return list(db.execute(stmt).scalars().all())


def method_mix_for(
    db: Session,
    student: StudentProfile,
    *,
    juz: Optional[int] = None,
    window_days: int = 14,
    now: Optional[datetime] = None,
) -> MethodMix:
    tz = student_timezone(student)
    since = dt.today_local(tz, now) - timedelta(days=window_days - 1)
    logs = recent_revision_logs(db, student.id, juz=juz, since=since)
    return build_method_mix(logs, window_days=window_days)


def method_nudge_for(
    db: Session, student: StudentProfile, *, juz: Optional[int] = None, now: Optional[datetime] = None
):
    mix = method_mix_for(db, student, juz=juz, now=now)
    label = f"juz {juz}" if juz else "your juz"
    return evaluate_method_mix(mix, label)


def rotation_entries(
    db: Session, student: StudentProfile, now: Optional[datetime] = None
) -> List[JuzRotationEntry]:
    """Revision recency for every juz the student has finished memorizing.

    This is what surfaces the juz being avoided — the guide's central warning
    about murajaat.
    """
    tz = student_timezone(student)
    now = now or dt.utcnow()
    records = all_juz_records(db, student.id)
    done = [
        j
        for j, r in records.items()
        if r.memorization_completed_at is not None
    ]
    if not done:
        return []

    rows = db.execute(
        select(
            DailyRevisionLog.juz,
            func.max(DailyRevisionLog.local_date),
            func.count(DailyRevisionLog.id),
        )
        .where(DailyRevisionLog.student_id == student.id, DailyRevisionLog.juz.in_(done))
        .group_by(DailyRevisionLog.juz)
    ).all()
    by_juz = {juz: (last, count) for juz, last, count in rows}

    today = dt.today_local(tz, now)
    out = []
    for juz in sorted(done):
        last, count = by_juz.get(juz, (None, 0))
        if isinstance(last, str):  # SQLite returns dates from max() as strings
            last = date.fromisoformat(last)
        out.append(
            JuzRotationEntry(
                juz=juz,
                last_revised=last,
                days_since=dt.days_between(last, today) if last else None,
                revision_count=int(count or 0),
            )
        )
    return rank_neglected_juz(out)


def streak_for(db: Session, student: StudentProfile, now: Optional[datetime] = None):
    tz = student_timezone(student)
    rows = db.execute(
        select(DailyRevisionLog.local_date)
        .where(DailyRevisionLog.student_id == student.id)
        .distinct()
    ).all()
    logged = set()
    for (d,) in rows:
        logged.add(date.fromisoformat(d) if isinstance(d, str) else d)
    return compute_streak(
        logged, active_days=student.active_days, today=dt.today_local(tz, now)
    )


# --- Progress & projection ---------------------------------------------------


def pace_projection(
    db: Session, student: StudentProfile, *, window_days: int = 28, now: Optional[datetime] = None
) -> PaceProjection:
    """Projection from logged reality, not the theoretical one-page-a-day max."""
    tz = student_timezone(student)
    today = dt.today_local(tz, now)
    since = today - timedelta(days=window_days - 1)

    pages_logged = int(
        db.execute(
            select(func.count(PageProgress.id)).where(
                PageProgress.student_id == student.id,
                PageProgress.memorized_on.is_not(None),
                PageProgress.memorized_on >= since,
            )
        ).scalar_one()
    )

    # Active days actually available in the window, per the student's schedule.
    active_days_in_window = sum(
        1
        for i in range(window_days)
        if dt.is_active_day(student.active_days, since + timedelta(days=i))
    )

    return project_completion(
        pages_memorized=memorized_page_count(db, student.id),
        pages_logged_in_window=pages_logged,
        active_days_in_window=active_days_in_window,
        active_days=student.active_days,
        today=today,
    )


def schedule_for(db: Session, student: StudentProfile, now: Optional[datetime] = None):
    """Today's plan: the Muhaffiz's murajaat schedule, tasmee, then hifz."""
    tz = student_timezone(student)
    today = dt.today_local(tz, now)
    board = build_board(db, student, now=now)
    active = board.active
    face = juz_workface(db, student, student.current_juz)

    return recommend_schedule(
        budget=TimeBudget(daily_minutes=student.daily_minutes, active_days=student.active_days),
        murajaat=[(p.juz, p.portion) for p in planned_murajaat_for(db, student, today)],
        tasmee_juz=active.juz if (active and not active.is_passed) else None,
        revision_method=student.preferred_method,
        # No point reserving memorization time while a batch is with the Muhaffiz.
        memorizing=bool(face.memorize_gate),
    )


def day_schedule_for(db: Session, student: StudentProfile, now: Optional[datetime] = None):
    """Today's plan with a suggested clock time against every block."""
    rec = schedule_for(db, student, now=now)
    return dayplan.build_day_schedule(
        rec.recommended,
        wake_hour=student.wake_hour,
        evening_hour=student.evening_hour,
    )


def week_strip(db: Session, student: StudentProfile, now: Optional[datetime] = None):
    """The last seven days of revision logging, for the habit strip."""
    tz = student_timezone(student)
    today = dt.today_local(tz, now)
    rows = db.execute(
        select(DailyRevisionLog.local_date)
        .where(
            DailyRevisionLog.student_id == student.id,
            DailyRevisionLog.local_date >= today - timedelta(days=6),
        )
        .distinct()
    ).all()
    logged = {date.fromisoformat(d) if isinstance(d, str) else d for (d,) in rows}
    return dayplan.build_week_strip(logged, active_days=student.active_days, today=today)


def log_murajaat_class(
    db: Session,
    student: StudentProfile,
    *,
    juz: int,
    portion: str = "full",
    muhaffiz_id: Optional[int] = None,
    outcome: str = MurajaatClass.PENDING,
    notes: Optional[str] = None,
    when: Optional[datetime] = None,
) -> MurajaatClass:
    """Record a murajaat class. The outcome is Muhaffiz 1's to give."""
    when = when or dt.utcnow()
    row = MurajaatClass(
        student_id=student.id,
        muhaffiz_id=muhaffiz_id,
        juz=int(juz),
        portion=normalize_portion(portion),
        outcome=outcome,
        notes=(notes or None),
        occurred_at=when,
        local_date=dt.local_date(when, student_timezone(student)),
        graded_at=when if outcome != MurajaatClass.PENDING else None,
    )
    db.add(row)
    db.flush()
    return row


def grade_murajaat_class(
    db: Session, cls: MurajaatClass, *, outcome: str, notes: Optional[str] = None
) -> MurajaatClass:
    cls.outcome = outcome
    cls.graded_at = dt.utcnow()
    if notes:
        cls.notes = notes
    db.flush()
    return cls


def murajaat_classes_today(
    db: Session, student: StudentProfile, now: Optional[datetime] = None
) -> List[MurajaatClass]:
    today = dt.today_local(student_timezone(student), now)
    return list(
        db.execute(
            select(MurajaatClass)
            .where(MurajaatClass.student_id == student.id, MurajaatClass.local_date == today)
            .order_by(MurajaatClass.occurred_at.desc())
        ).scalars().all()
    )


def recent_murajaat_classes(
    db: Session, student_id: int, limit: int = 10
) -> List[MurajaatClass]:
    return list(
        db.execute(
            select(MurajaatClass)
            .where(MurajaatClass.student_id == student_id)
            .order_by(MurajaatClass.occurred_at.desc())
            .limit(limit)
        ).scalars().all()
    )


def murajaat_plan(db: Session, student_id: int) -> List[MurajaatPlan]:
    return list(
        db.execute(
            select(MurajaatPlan)
            .where(MurajaatPlan.student_id == student_id)
            .order_by(MurajaatPlan.weekday, MurajaatPlan.juz)
        ).scalars().all()
    )


def planned_murajaat_for(
    db: Session, student: StudentProfile, day: date
) -> List[MurajaatPlan]:
    """What the student's own schedule puts on this weekday."""
    return [p for p in murajaat_plan(db, student.id) if p.weekday == day.weekday()]


def set_murajaat_plan(
    db: Session, student_id: int, *, slots: Sequence[Tuple[int, int, str]]
) -> List[MurajaatPlan]:
    """Replace the schedule with `slots` of (weekday, juz, portion).

    Owned by the student: they know which juz has gone soft and how much this
    week actually holds. Duplicate slots are collapsed rather than rejected, so
    a double-submit cannot trip the unique constraint.
    """
    for row in murajaat_plan(db, student_id):
        db.delete(row)
    db.flush()

    seen = set()
    out = []
    for weekday, juz, portion in slots:
        key = (int(weekday), int(juz), normalize_portion(portion))
        if key in seen:
            continue
        seen.add(key)
        row = MurajaatPlan(
            student_id=student_id, weekday=key[0], juz=key[1], portion=key[2]
        )
        db.add(row)
        out.append(row)
    db.flush()
    return out


def revisions_today(
    db: Session, student: StudentProfile, now: Optional[datetime] = None
) -> List[DailyRevisionLog]:
    today = dt.today_local(student_timezone(student), now)
    return list(
        db.execute(
            select(DailyRevisionLog)
            .where(DailyRevisionLog.student_id == student.id, DailyRevisionLog.local_date == today)
            .order_by(DailyRevisionLog.logged_at.desc())
        )
        .scalars()
        .all()
    )


def progress_summary(db: Session, student: StudentProfile, now: Optional[datetime] = None) -> Dict:
    """The headline numbers. Stage 2 lags Stage 1 and both are shown."""
    board = build_board(db, student, now=now)
    certs = certification_board(db, student, now=now)
    memorized = memorized_page_count(db, student.id)
    # "Revalidated" now means pages recited to Muhaffiz 2 in a live attempt,
    # plus every page of a juz that has been passed outright.
    revalidated = sum(
        PAGES_PER_JUZ if w.is_passed else w.passed_count for w in board.windows
    )
    records = all_juz_records(db, student.id)

    return {
        "board": board,
        "certs": certs,
        "marhala": get_marhala(student.marhala),
        "pages_memorized": memorized,
        "pages_revalidated": revalidated,
        "percent_memorized": round(memorized / TOTAL_PAGES * 100, 1),
        "percent_revalidated": round(revalidated / TOTAL_PAGES * 100, 1),
        "juz_complete": sum(1 for r in records.values() if r.stage == JuzStage.COMPLETE),
        "juz_memorized": sum(1 for r in records.values() if r.memorization_completed_at),
        "juz_in_progress": sum(1 for r in records.values() if r.stage == JuzStage.MEMORIZING),
        "records": records,
        "streak": streak_for(db, student, now=now),
        "projection": pace_projection(db, student, now=now),
        "tasmee_target": stage1_tasmee_target(student),
    }


def certification_board(
    db: Session, student: StudentProfile, now: Optional[datetime] = None
) -> cert.CertificationBoard:
    """All 30 juz certifications for a student.

    One query pass feeds all thirty: page counts, evaluated-page counts, juz
    records and the 30-day board. The pipeline stage itself is derived in
    `domain/certification.classify` rather than read from a column, so it cannot
    drift out of step with the page records underneath it.
    """
    tz = student_timezone(student)
    records = all_juz_records(db, student.id)
    memorized_counts = dict(
        db.execute(
            select(PageProgress.juz, func.count(PageProgress.id))
            .where(
                PageProgress.student_id == student.id,
                PageProgress.memorized_at.is_not(None),
            )
            .group_by(PageProgress.juz)
        ).all()
    )

    items = []
    for juz in range(1, JUZ_COUNT + 1):
        rec = records.get(juz)
        window = (
            tasmee_window(db, student, juz, now=now)
            if rec and rec.tasmee_window_started_at
            else None
        )
        items.append(
            cert.classify(
                juz=juz,
                pages_memorized=int(memorized_counts.get(juz, 0)),
                pages_evaluated=window.passed_count if window else 0,
                window=window,
                certified_on=(
                    dt.local_date(rec.stage2_completed_at, tz)
                    if rec and rec.stage2_completed_at
                    else None
                ),
            )
        )
    return cert.build_board(items)


# --- Weak spots --------------------------------------------------------------


def open_weak_spots(db: Session, student_id: int, juz: Optional[int] = None) -> List[WeakSpot]:
    stmt = select(WeakSpot).where(
        WeakSpot.student_id == student_id, WeakSpot.resolved_at.is_(None)
    )
    if juz is not None:
        stmt = stmt.where(WeakSpot.juz == juz)
    return list(db.execute(stmt.order_by(WeakSpot.created_at.desc())).scalars().all())


def add_weak_spot(
    db: Session,
    student_id: int,
    *,
    juz: int,
    note: str,
    page: Optional[int] = None,
    muhaffiz_id: Optional[int] = None,
) -> WeakSpot:
    row = WeakSpot(
        student_id=student_id,
        juz=juz,
        page=page,
        note=note,
        created_by_muhaffiz_id=muhaffiz_id,
    )
    db.add(row)
    db.flush()
    return row


def resolve_weak_spot(db: Session, spot: WeakSpot) -> WeakSpot:
    spot.resolved_at = dt.utcnow()
    db.flush()
    return spot


# --- Password recovery -------------------------------------------------------


def issue_reset_token(
    db: Session,
    user: User,
    *,
    muhaffiz_id: Optional[int] = None,
    hours: int = 72,
    now: Optional[datetime] = None,
) -> str:
    """Create a one-time reset link and return the raw token.

    The raw value is returned once and never stored; only its hash goes to the
    database. Any earlier unused token for this user is burned at the same time,
    so an old link handed out and forgotten cannot still be live.
    """
    from app.security import hash_reset_token, make_reset_token

    now = now or dt.utcnow()
    for old in db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None)
        )
    ).scalars():
        old.used_at = now

    token = make_reset_token()
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=hash_reset_token(token),
            issued_by_muhaffiz_id=muhaffiz_id,
            expires_at=now + timedelta(hours=hours),
        )
    )
    db.flush()
    return token


def consume_reset_token(
    db: Session, token: str, now: Optional[datetime] = None
) -> Optional[User]:
    """Resolve a reset token to its user, or None if it is unusable.

    Deliberately returns None for every failure mode — unknown, expired, already
    used — so the reset page cannot be used to tell a real token from a stale one.
    """
    from app.security import hash_reset_token

    now = now or dt.utcnow()
    row = db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == hash_reset_token(token)
        )
    ).scalar_one_or_none()
    if row is None or row.used_at is not None:
        return None
    if dt.ensure_utc(row.expires_at) < now:
        return None
    return db.get(User, row.user_id)


def complete_reset(
    db: Session, token: str, new_password: str, now: Optional[datetime] = None
) -> Optional[User]:
    """Set the new password and burn the token in one step."""
    from app.security import hash_password, hash_reset_token

    now = now or dt.utcnow()
    user = consume_reset_token(db, token, now=now)
    if user is None:
        return None
    row = db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == hash_reset_token(token)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    user.password_hash = hash_password(new_password)
    row.used_at = now
    db.flush()
    return user


# --- Tracking: am I on track? ------------------------------------------------


def page_pace_for(
    db: Session, student: StudentProfile, *, window_days: int = 120
) -> tracking.PagePace:
    """Days per page, from Muhaffiz 1's sign-offs.

    Sign-off rather than the student's own "memorized" tick: the tick is a
    self-report of readiness, the sign-off is the page actually landing. Only
    the second one is a fact about progress.
    """
    tz = student_timezone(student)
    since = dt.today_local(tz) - timedelta(days=window_days)
    rows = db.execute(
        select(PageProgress.signed_off_at).where(
            PageProgress.student_id == student.id,
            PageProgress.signed_off_at.is_not(None),
        )
    ).all()
    days = [dt.local_date(ts, tz) for (ts,) in rows if ts is not None]
    return tracking.page_pace([d for d in days if d >= since])


def attendance_for(
    db: Session, student: StudentProfile, *, weeks: int = 4, now: Optional[datetime] = None
) -> tracking.Attendance:
    """Murajaat classes attended against the student's own schedule."""
    tz = student_timezone(student)
    today = dt.today_local(tz, now)
    since = today - timedelta(weeks=weeks)
    rows = db.execute(
        select(MurajaatClass.local_date).where(
            MurajaatClass.student_id == student.id,
            MurajaatClass.local_date >= since,
        )
    ).all()
    class_dates = {date.fromisoformat(d) if isinstance(d, str) else d for (d,) in rows}
    return tracking.weekly_attendance(
        planned_weekdays={p.weekday for p in murajaat_plan(db, student.id)},
        class_dates=class_dates,
        today=today,
        weeks=weeks,
    )


def juz_progress_rows(
    db: Session, student: StudentProfile, now: Optional[datetime] = None
) -> List[tracking.JuzProgressRow]:
    """Per-juz progress: pages signed off, pages recited, and how long it took."""
    tz = student_timezone(student)
    records = all_juz_records(db, student.id)
    board = build_board(db, student, now=now)
    windows = {w.juz: w for w in board.windows}

    signed = dict(
        db.execute(
            select(PageProgress.juz, func.count(PageProgress.id))
            .where(
                PageProgress.student_id == student.id,
                PageProgress.signed_off_at.is_not(None),
            )
            .group_by(PageProgress.juz)
        ).all()
    )

    out = []
    for juz in sorted(records):
        rec = records[juz]
        if rec.stage == JuzStage.NOT_STARTED and not signed.get(juz):
            continue
        w = windows.get(juz)
        out.append(
            tracking.JuzProgressRow(
                juz=juz,
                signed_off=int(signed.get(juz, 0)),
                recited=(PAGES_PER_JUZ if (w and w.is_passed) else (w.passed_count if w else 0)),
                status=(
                    "Passed" if rec.juz_passed
                    else (w.label if w and w.has_started else "Memorizing")
                ),
                started_on=(
                    dt.local_date(rec.memorization_started_at, tz)
                    if rec.memorization_started_at else None
                ),
                finished_on=(
                    dt.local_date(rec.juz_tasmee_passed_at, tz)
                    if rec.juz_tasmee_passed_at else None
                ),
            )
        )
    return out


def tracking_summary(
    db: Session, student: StudentProfile, now: Optional[datetime] = None
) -> Dict:
    """Everything the report's on-track section needs."""
    tz = student_timezone(student)
    today = dt.today_local(tz, now)
    pace = page_pace_for(db, student)
    face = juz_workface(db, student, student.current_juz)
    return {
        "pace": pace,
        "attendance": attendance_for(db, student, now=now),
        "juz_rows": juz_progress_rows(db, student, now=now),
        "projected_juz_finish": tracking.projected_finish(
            pages_remaining=PAGES_PER_JUZ - len(face.signed_off),
            pace=pace,
            today=today,
        ),
        "current_juz_signed_off": len(face.signed_off),
    }


# --- Self-recorded progress --------------------------------------------------
# This is a student's own log. There are no Muhaffiz accounts: the student
# records what happened in class — which pages were signed off, which were
# recited in tasmee, when a juz was passed. The programme's gates still apply
# (a batch must be signed off before new pages, a juz must be passed before the
# next opens, the 30-day window still runs), they are simply driven by the
# student's own record rather than a second login.


def record_signoff(
    db: Session,
    student: StudentProfile,
    *,
    passed_pages: Sequence[int],
    returned_pages: Sequence[int] = (),
    notes: Optional[str] = None,
    when: Optional[datetime] = None,
) -> Dict[str, List[int]]:
    """The student records what their Muhaffiz signed off."""
    return sign_off_pages(
        db, student, muhaffiz_id=None,
        passed_pages=passed_pages, returned_pages=returned_pages,
        notes=notes, when=when,
    )


def record_tasmee_pages(
    db: Session,
    student: StudentProfile,
    juz: int,
    *,
    pages: Sequence[int],
    when: Optional[datetime] = None,
) -> TasmeeSession:
    """The student records pages recited to their Stage 2 Muhaffiz."""
    return record_tasmee(
        db, student, stage=Stage.TWO, juz=juz, muhaffiz_id=None,
        page_results=[
            {"page": p, "hifz": True, "makharij": True, "tajweed": True} for p in pages
        ],
        when=when,
    )


def backfill_prior_juz(
    db: Session,
    student: StudentProfile,
    up_to_juz: int,
    *,
    when: Optional[datetime] = None,
) -> List[int]:
    """Mark every juz before `up_to_juz` in the student's order as complete.

    Somebody joining at juz 10 has already memorized the nine before it; making
    them tick 180 pages to say so would be absurd. Their sequence decides what
    "before" means, so a student working backwards from 30 gets 30..11 filled in
    rather than 1..9.

    Only juz with no record at all are touched, so re-running this can never
    overwrite real progress.
    """
    when = when or dt.utcnow()
    tz = student_timezone(student)
    order = list(juz_order_for(student))
    try:
        cutoff = order.index(up_to_juz)
    except ValueError:
        return []

    filled = []
    for juz in order[:cutoff]:
        record = get_juz_record(db, student.id, juz)
        if record.juz_passed or record.memorization_completed_at:
            continue

        for page in juz_pages(juz):
            row = db.execute(
                select(PageProgress).where(
                    PageProgress.student_id == student.id, PageProgress.page == page
                )
            ).scalar_one_or_none()
            if row is None:
                row = PageProgress(student_id=student.id, page=page, juz=juz)
                db.add(row)
            row.memorized_at = row.memorized_at or when
            row.memorized_on = row.memorized_on or dt.local_date(when, tz)
            row.submitted_at = None
            row.returned_at = None
            row.signed_off_at = row.signed_off_at or when
            row.stage2_entered_at = row.stage2_entered_at or when

        record.memorization_started_at = record.memorization_started_at or when
        record.memorization_completed_at = record.memorization_completed_at or when
        record.stage2_started_at = record.stage2_started_at or when
        record.stage2_completed_at = record.stage2_completed_at or when
        record.tasmee_window_started_at = record.tasmee_window_started_at or when
        record.juz_tasmee_passed_at = when
        record.stage = JuzStage.COMPLETE
        filled.append(juz)

    db.flush()
    return filled


def revisable_juz(db: Session, student: StudentProfile) -> List[int]:
    """Juz the student can put on a murajaat schedule.

    Anything they have memorized, not only what has been passed. Waiting for a
    juz to be passed before it could be revised left new students staring at an
    empty schedule screen with nothing to choose.
    """
    records = all_juz_records(db, student.id)
    out = {
        j for j, r in records.items()
        if r.juz_passed or r.memorization_completed_at or r.stage == JuzStage.COMPLETE
    }
    counts = db.execute(
        select(PageProgress.juz)
        .where(PageProgress.student_id == student.id, PageProgress.memorized_at.is_not(None))
        .distinct()
    ).all()
    out.update(j for (j,) in counts)
    return sorted(out)
