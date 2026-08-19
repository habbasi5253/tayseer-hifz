"""The Muhaffiz console.

Serves both stages from one account type — a person can be Stage 1 for one
student and Stage 2 for another. What they may do on a given student is decided
by the assignment, never by the account.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app import notifications as notif
from app import services
from app.db import get_session
from app.deps import check_csrf, redirect, render, require_muhaffiz, require_user
from app.domain import dates as dt
from app.domain.quran import JUZ_COUNT, label_page
from app.models import (
    MuhaffizProfile,
    NotificationKind,
    Stage,
    TasmeeSession,
)

router = APIRouter(prefix="/muhaffiz")


def _stage_for(db: Session, muhaffiz_id: int, student_id: int) -> Optional[str]:
    """Which stage this Muhaffiz holds for this student, if any."""
    rows = services.active_assignments(db, student_id)
    for a in rows:
        if a.muhaffiz_id == muhaffiz_id:
            return a.stage
    return None


def _guard(db: Session, muhaffiz: MuhaffizProfile, student_id: int) -> tuple:
    student = services.get_student(db, student_id)
    if student is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Student not found.")
    stage = _stage_for(db, muhaffiz.id, student_id)
    if stage is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You are not assigned to this student.")
    return student, stage


@router.get("")
def console(request: Request, db: Session = Depends(get_session)):
    user = require_user(request, db)
    muhaffiz = require_muhaffiz(request, db)
    now = dt.utcnow()

    rows = []
    for a in services.students_of_muhaffiz(db, muhaffiz.id):
        student = a.student
        if student is None:
            continue
        board = services.build_board(db, student, now=now)
        streak = services.streak_for(db, student, now=now)
        certs = services.certification_board(db, student, now=now)
        rows.append(
            {
                "assignment": a,
                "student": student,
                "stage": a.stage,
                "board": board,
                "streak": streak,
                "certs": certs,
                "pages_memorized": services.memorized_page_count(db, student.id),
                "needs_attention": len(board.expired) + len(board.at_risk),
                "last_seen": streak.last_logged,
            }
        )

    # Students needing something from this Muhaffiz float to the top.
    rows.sort(key=lambda r: (-r["needs_attention"], r["student"].name))

    return render(
        request,
        "muhaffiz/console.html",
        {
            "user": user,
            "db": db,
            "muhaffiz": muhaffiz,
            "rows": rows,
            "stage1_count": sum(1 for r in rows if r["stage"] == Stage.ONE),
            "stage2_count": sum(1 for r in rows if r["stage"] == Stage.TWO),
        },
    )


@router.get("/student/{student_id}")
def student_detail(
    student_id: int,
    request: Request,
    juz: Optional[int] = None,
    db: Session = Depends(get_session),
):
    user = require_user(request, db)
    muhaffiz = require_muhaffiz(request, db)
    student, stage = _guard(db, muhaffiz, student_id)
    now = dt.utcnow()

    records = services.all_juz_records(db, student.id)
    if stage == Stage.ONE:
        default_juz = student.current_juz
    else:
        in_stage2 = sorted(j for j, r in records.items() if r.stage2_started_at)
        default_juz = in_stage2[0] if in_stage2 else student.current_juz
    selected = juz if (juz and 1 <= juz <= JUZ_COUNT) else default_juz

    board = services.build_board(db, student, now=now)
    window = services.tasmee_window(db, student, selected, now=now)

    history = list(
        db.execute(
            select(TasmeeSession)
            .options(joinedload(TasmeeSession.results))
            .where(TasmeeSession.student_id == student.id, TasmeeSession.stage == stage)
            .order_by(TasmeeSession.occurred_at.desc())
            .limit(10)
        )
        .unique()
        .scalars()
        .all()
    )

    return render(
        request,
        "muhaffiz/student.html",
        {
            "user": user,
            "db": db,
            "muhaffiz": muhaffiz,
            "student": student,
            "stage": stage,
            "juz": selected,
            "juz_options": sorted(records.keys()) or [student.current_juz],
            # The Stage 1 decision table: days since last tasmee, per page.
            "pages": services.stage1_page_view(db, student, selected, now=now),
            "window": window,
            "board": board,
            "records": records,
            "mix": services.method_mix_for(db, student, juz=selected, now=now),
            "tasmee_target": services.stage1_tasmee_target(student),
            "history": history,
            "spots": services.open_weak_spots(db, student.id, juz=selected),
            "pages_today": services.stage2_pages_today(db, student, when=now),
            "streak": services.streak_for(db, student, now=now),
            "face": services.juz_workface(db, student, selected),
            "record": records.get(selected) or services.get_juz_record(db, student.id, selected),
            "murajaat": services.recent_murajaat_classes(db, student.id),
            "plan": services.murajaat_plan(db, student.id),
            "weekdays": dt.WEEKDAY_SHORT,
        },
    )


# --- Checkpoint decisions ----------------------------------------------------


@router.post("/student/{student_id}/tasmee")
async def log_tasmee(
    student_id: int,
    request: Request,
    db: Session = Depends(get_session),
):
    """Record a tasmee sitting.

    Reads the form manually because the page count is dynamic: the Muhaffiz
    ticks whichever pages they chose to hear, and Stage 2 carries three
    independent criteria per page.
    """
    muhaffiz = require_muhaffiz(request, db)
    form = await request.form()
    check_csrf(request, form.get("csrf_token"))

    student, stage = _guard(db, muhaffiz, student_id)
    juz = int(form.get("juz") or student.current_juz)
    notes = (form.get("notes") or "").strip() or None

    selected = form.getlist("page")
    if not selected:
        return redirect(
            f"/muhaffiz/student/{student_id}?juz={juz}",
            "Select at least one page to record.",
            error=True,
        )

    results = []
    for raw in selected:
        page = int(raw)
        if stage == Stage.TWO:
            results.append(
                {
                    "page": page,
                    # Unticked means not passed. Silence is never a pass.
                    "hifz": form.get(f"hifz_{page}") is not None,
                    "makharij": form.get(f"makharij_{page}") is not None,
                    "tajweed": form.get(f"tajweed_{page}") is not None,
                    "notes": (form.get(f"notes_{page}") or "").strip() or None,
                }
            )
        else:
            results.append({"page": page, "notes": (form.get(f"notes_{page}") or "").strip() or None})

    try:
        session = services.record_tasmee(
            db,
            student,
            stage=stage,
            juz=juz,
            muhaffiz_id=muhaffiz.id,
            page_results=results,
            notes=notes,
        )
    except services.ProgramRuleViolation as exc:
        db.rollback()
        return redirect(f"/muhaffiz/student/{student_id}?juz={juz}", str(exc), error=True)

    passed = session.passed_count
    failed = session.page_count - passed

    if student.user:
        if stage == Stage.TWO:
            title = (
                f"{passed} page{'s' if passed != 1 else ''} revalidated"
                if not failed
                else f"{passed} revalidated, {failed} to work on"
            )
            lines = []
            for r in session.results:
                if r.passed:
                    continue
                crits = ", ".join(r.failed_criteria) or "review"
                lines.append(f"• {label_page(r.page)} — {crits}" + (f": {r.notes}" if r.notes else ""))
            body = (
                f"{muhaffiz.name} heard {session.page_count} page"
                f"{'s' if session.page_count != 1 else ''} of juz {juz}.\n\n"
                + ("\n".join(lines) + "\n\n" if lines else "")
                + (
                    "Their 30-day windows have reset."
                    if passed
                    else "These stay in the queue until they are re-recited."
                )
            )
        else:
            title = f"Tasmee recorded — juz {juz}"
            body = (
                f"{muhaffiz.name} heard {session.page_count} page"
                f"{'s' if session.page_count != 1 else ''} of juz {juz}."
                + (f"\n\n{notes}" if notes else "")
            )
        notif.notify(
            db,
            student.user,
            kind=NotificationKind.TASMEE_FEEDBACK,
            title=title,
            body=body,
            dedupe_key=f"tasmee:{session.id}",
            url="/thirty-day" if stage == Stage.TWO else f"/juz/{juz}",
            severity="attention" if failed else "info",
        )

    db.commit()
    msg = f"Recorded {session.page_count} page{'s' if session.page_count != 1 else ''}."
    if stage == Stage.TWO and failed:
        msg += f" {failed} not revalidated."
    return redirect(f"/muhaffiz/student/{student_id}?juz={juz}", msg)


@router.post("/student/{student_id}/signoff")
async def sign_off(student_id: int, request: Request, db: Session = Depends(get_session)):
    """Muhaffiz 1 hears the batch: each page is signed off or sent back."""
    muhaffiz = require_muhaffiz(request, db)
    form = await request.form()
    check_csrf(request, form.get("csrf_token"))
    student, stage = _guard(db, muhaffiz, student_id)
    if stage != Stage.ONE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Sign-off is the Stage 1 Muhaffiz's role.")

    juz = int(form.get("juz") or student.current_juz)
    passed, returned = [], []
    for raw in form.getlist("page"):
        page = int(raw)
        (passed if form.get(f"ok_{page}") is not None else returned).append(page)

    if not passed and not returned:
        return redirect(f"/muhaffiz/student/{student_id}?juz={juz}",
                        "Select at least one page.", error=True)
    try:
        services.sign_off_pages(
            db, student, muhaffiz_id=muhaffiz.id, passed_pages=passed,
            returned_pages=returned, notes=(form.get("notes") or "").strip() or None,
        )
    except services.ProgramRuleViolation as exc:
        db.rollback()
        return redirect(f"/muhaffiz/student/{student_id}?juz={juz}", str(exc), error=True)

    if student.user:
        face = services.juz_workface(db, student, juz)
        notif.notify(
            db, student.user,
            kind=NotificationKind.CHECKPOINT_DECIDED,
            title=(
                f"{len(passed)} page{'s' if len(passed) != 1 else ''} signed off"
                if not returned else
                f"{len(returned)} page{'s' if len(returned) != 1 else ''} to recite again"
            ),
            body=(
                f"Juz {juz}. " + (
                    "You can carry on to the next pages."
                    if not returned and not face.is_awaiting_signoff
                    else "Recite the returned pages again before moving on."
                )
            ),
            dedupe_key=f"signoff:{student.id}:{juz}:{dt.utcnow().isoformat(timespec='seconds')}",
            url="/",
            severity="attention" if returned else "info",
        )
    db.commit()
    msg = f"{len(passed)} signed off"
    if returned:
        msg += f", {len(returned)} returned"
    return redirect(f"/muhaffiz/student/{student_id}?juz={juz}", msg + ".")


@router.post("/student/{student_id}/pass-juz")
def pass_juz(
    student_id: int,
    request: Request,
    csrf_token: str = Form(...),
    juz: int = Form(...),
    db: Session = Depends(get_session),
):
    """Muhaffiz 2 passes the full-juz tasmee, which opens the next juz."""
    muhaffiz = require_muhaffiz(request, db)
    check_csrf(request, csrf_token)
    student, stage = _guard(db, muhaffiz, student_id)
    if stage != Stage.TWO:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only the Stage 2 Muhaffiz can pass a juz.",
        )

    services.pass_juz_tasmee(db, student, int(juz), muhaffiz_id=muhaffiz.id)
    if student.user:
        notif.notify(
            db, student.user,
            kind=NotificationKind.MILESTONE,
            title=f"Juz {juz} passed",
            body=(
                f"{muhaffiz.name} has passed your full tasmee of juz {juz}. "
                f"Juz {int(juz) + 1} is now open.\n\n"
                "Keep juz " + str(juz) + " in your murajaat rotation — the 30-day window "
                "keeps running."
            ),
            dedupe_key=f"juzpass:{student.id}:{juz}",
            url="/certificates",
            severity="info",
        )
    db.commit()
    return redirect(f"/muhaffiz/student/{student_id}", f"Juz {juz} passed.")


@router.post("/student/{student_id}/murajaat/{class_id}")
def grade_murajaat(
    student_id: int,
    class_id: int,
    request: Request,
    csrf_token: str = Form(...),
    outcome: str = Form(...),
    notes: Optional[str] = Form(None),
    db: Session = Depends(get_session),
):
    """Muhaffiz 1 marks how a murajaat class went."""
    from app.models import MurajaatClass

    muhaffiz = require_muhaffiz(request, db)
    check_csrf(request, csrf_token)
    student, _ = _guard(db, muhaffiz, student_id)

    cls = db.get(MurajaatClass, class_id)
    if cls is None or cls.student_id != student.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Class not found.")

    ok = outcome == MurajaatClass.GOOD
    services.grade_murajaat_class(
        db, cls, outcome=MurajaatClass.GOOD if ok else MurajaatClass.NEEDS_WORK, notes=notes
    )
    if student.user:
        notif.notify(
            db, student.user,
            kind=NotificationKind.TASMEE_FEEDBACK,
            title=f"Murajaat of juz {cls.juz} — {'went well' if ok else 'needs more work'}",
            body=(notes or ("Good session." if ok else "Give this juz another pass before next class.")),
            dedupe_key=f"murajaat:{cls.id}",
            url="/progress",
            severity="info" if ok else "attention",
        )
    db.commit()
    return redirect(f"/muhaffiz/student/{student_id}", "Murajaat class graded.")


@router.post("/student/{student_id}/reset-link")
def reset_link(
    student_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_session),
):
    """Generate a one-time reset link to hand to a locked-out student.

    Rendered directly rather than redirected: the raw token exists only in this
    response, and a redirect would either lose it or park it in the URL bar and
    browser history.
    """
    muhaffiz = require_muhaffiz(request, db)
    check_csrf(request, csrf_token)
    student, _ = _guard(db, muhaffiz, student_id)
    if student.user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No account for this student.")

    token = services.issue_reset_token(db, student.user, muhaffiz_id=muhaffiz.id)
    db.commit()
    return render(
        request,
        "muhaffiz/reset_link.html",
        {
            "user": require_user(request, db),
            "db": db,
            "student": student,
            "link": str(request.base_url).rstrip("/") + f"/reset/{token}",
            "hours": 72,
        },
    )


@router.post("/student/{student_id}/note")
def add_note(
    student_id: int,
    request: Request,
    csrf_token: str = Form(...),
    juz: int = Form(...),
    note: str = Form(...),
    page: Optional[int] = Form(None),
    db: Session = Depends(get_session),
):
    muhaffiz = require_muhaffiz(request, db)
    check_csrf(request, csrf_token)
    student, _ = _guard(db, muhaffiz, student_id)

    services.add_weak_spot(
        db,
        student.id,
        juz=int(juz),
        note=note.strip(),
        page=int(page) if page else None,
        muhaffiz_id=muhaffiz.id,
    )
    db.commit()
    return redirect(f"/muhaffiz/student/{student_id}?juz={juz}", "Note added for the student.")
