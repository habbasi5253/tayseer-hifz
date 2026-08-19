"""Student-facing pages: today, revise, the 30-day board, progress."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import notifications as notif
from app import services
from app.db import get_session
from app.deps import check_csrf, redirect, render, require_user
from app.domain import dates as dt
from app.domain.quran import JUZ_COUNT, global_page, page_index_in_juz
from app.domain.revision import Portion, get_method, method_choices, portion_label
from app.models import (
    JuzStage,
    NotificationKind,
    RevisionKind,
    Stage,
)

router = APIRouter()


@router.get("/")
def home(request: Request, db: Session = Depends(get_session)):
    user = require_user(request, db)
    if not user.student:
        return redirect("/muhaffiz")
    student = user.student

    now = dt.utcnow()
    tz = user.timezone
    today = dt.today_local(tz, now)

    certs = services.certification_board(db, student, now=now)
    board = services.build_board(db, student, now=now)
    streak = services.streak_for(db, student, now=now)
    sched = services.schedule_for(db, student, now=now)
    nudge = services.method_nudge_for(db, student, juz=student.current_juz, now=now)
    rotation = services.rotation_entries(db, student, now=now)
    records = services.all_juz_records(db, student.id)

    current_rec = records.get(student.current_juz)
    pages_done = services.pages_memorized_in_juz(db, student.id, student.current_juz)
    next_page = None
    if pages_done < 20:
        next_page = global_page(student.current_juz, pages_done + 1)

    # Everything the day needs: the timed plan, the habit strip, and what has
    # already been logged so the page can show it as done rather than re-ask.
    day = services.day_schedule_for(db, student, now=now)
    week = services.week_strip(db, student, now=now)
    revisions = services.revisions_today(db, student, now=now)
    classes = services.murajaat_classes_today(db, student, now=now)

    face = services.juz_workface(db, student, student.current_juz)
    memorized_today = any(
        p.memorized_on == today
        for p in services.memorized_pages(db, student.id)
        if p.memorized_on
    )
    hifz_time = next((b for b in day.blocks if b.key == "new_hifz"), None)

    return render(
        request,
        "student/today.html",
        {
            "user": user,
            "db": db,
            "student": student,
            "today": today,
            "certs": certs,
            "active": certs.in_progress[:2],
            "board": board,
            "streak": streak,
            "sched": sched,
            "nudge": nudge,
            "planned_today": services.planned_murajaat_for(db, student, today),
            "current_rec": current_rec,
            "pages_done": pages_done,
            "next_page": next_page,
            "day": day,
            "week": week,
            "revisions": revisions,
            "classes": classes,
            "memorized_today": memorized_today,
            "face": face,
            "hifz_time": hifz_time,
            "tasmee_target": services.stage1_tasmee_target(student),
            "is_active_day": dt.is_active_day(student.active_days, today),
            "open_spots": services.open_weak_spots(db, student.id)[:3],
        },
    )


# --- Revision logging --------------------------------------------------------


@router.get("/revise")
def revise_form(request: Request, juz: Optional[int] = None, db: Session = Depends(get_session)):
    user = require_user(request, db)
    student = user.student
    if not student:
        return redirect("/muhaffiz")

    now = dt.utcnow()
    records = services.all_juz_records(db, student.id)
    available = sorted(
        j for j, r in records.items() if r.stage in (JuzStage.MEMORIZING, JuzStage.STAGE2, JuzStage.COMPLETE)
    ) or [student.current_juz]

    selected = juz if juz in available else student.current_juz
    if selected not in available:
        selected = available[0]

    rotation = services.rotation_entries(db, student, now=now)

    return render(
        request,
        "student/revise.html",
        {
            "user": user,
            "db": db,
            "student": student,
            "methods": method_choices(),
            "available": available,
            "selected": selected,
            "mix": services.method_mix_for(db, student, juz=selected, now=now),
            "nudge": services.method_nudge_for(db, student, juz=selected, now=now),
            "rotation": rotation,
            "suggestion": rotation[0] if rotation else None,
            "recent": services.recent_revision_logs(db, student.id, limit=8),
            "streak": services.streak_for(db, student, now=now),
        },
    )


@router.post("/revise")
def log_revision(
    request: Request,
    csrf_token: str = Form(...),
    juz: int = Form(...),
    method: int = Form(...),
    duration_minutes: int = Form(0),
    kind: str = Form(RevisionKind.HALI),
    note: Optional[str] = Form(None),
    db: Session = Depends(get_session),
):
    user = require_user(request, db)
    check_csrf(request, csrf_token)
    student = user.student
    if not student:
        return redirect("/")

    services.log_revision(
        db,
        student,
        juz=int(juz),
        method=int(method),
        duration_minutes=int(duration_minutes or 0),
        kind=kind,
        note=note,
    )
    db.commit()

    m = get_method(int(method))
    streak = services.streak_for(db, student)
    msg = f"Juz {juz} logged — {m.short}."
    if streak.current_run > 1:
        msg += f" {streak.current_run} days running."
    return redirect("/", msg)


# --- The 30-day board --------------------------------------------------------


@router.get("/thirty-day")
def thirty_day(request: Request, db: Session = Depends(get_session)):
    """The 30-day rule now lives on the juz whose clock is running.

    Kept as a redirect rather than deleted: it is linked from older
    notifications, and a dead link in an email is a support question.
    """
    user = require_user(request, db)
    student = user.student
    if not student:
        return redirect("/muhaffiz")
    board = services.build_board(db, student)
    active = board.active
    return redirect(f"/certificate/{active.juz}" if active else "/certificates")


# --- Progress ----------------------------------------------------------------


@router.get("/progress")
def progress(request: Request, db: Session = Depends(get_session)):
    user = require_user(request, db)
    student = user.student
    if not student:
        return redirect("/muhaffiz")

    now = dt.utcnow()
    summary = services.progress_summary(db, student, now=now)

    return render(
        request,
        "student/progress.html",
        {
            "user": user,
            "db": db,
            "student": student,
            "s": summary,
            "certs": services.certification_board(db, student, now=now),
            "track": services.tracking_summary(db, student, now=now),
            "mix": services.method_mix_for(db, student, window_days=30, now=now),
            "rotation": services.rotation_entries(db, student, now=now),
            "sched": services.schedule_for(db, student, now=now),
            "spots": services.open_weak_spots(db, student.id),
        },
    )


@router.get("/certificates")
def certificates(request: Request, db: Session = Depends(get_session)):
    """The 30 juz certifications — the spine of the app."""
    user = require_user(request, db)
    student = user.student
    if not student:
        return redirect("/muhaffiz")

    now = dt.utcnow()
    return render(
        request,
        "student/certificates.html",
        {
            "user": user,
            "db": db,
            "student": student,
            "certs": services.certification_board(db, student, now=now),
            "track": services.tracking_summary(db, student, now=now),
        },
    )


@router.get("/certificate/{juz}")
def certificate_detail(juz: int, request: Request, db: Session = Depends(get_session)):
    user = require_user(request, db)
    student = user.student
    if not student:
        return redirect("/muhaffiz")

    juz = max(1, min(JUZ_COUNT, juz))
    now = dt.utcnow()
    certs = services.certification_board(db, student, now=now)
    c = certs.get(juz)

    return render(
        request,
        "student/certificate.html",
        {
            "user": user,
            "db": db,
            "student": student,
            "c": c,
            "window": c.window if c else None,
            "pages": services.stage1_page_view(db, student, juz, now=now),
            "mix": services.method_mix_for(db, student, juz=juz, now=now),
            "spots": services.open_weak_spots(db, student.id, juz=juz),
            "tz": services.student_timezone(student),
        },
    )


@router.get("/juz/{juz}")
def juz_redirect(juz: int):
    """Old page-centric URL. Certificates are the unit now."""
    return redirect(f"/certificate/{juz}")


@router.post("/submit-batch")
def submit_batch(
    request: Request,
    csrf_token: str = Form(...),
    juz: int = Form(...),
    db: Session = Depends(get_session),
):
    """Send the memorized pages to Muhaffiz 1 to be heard."""
    user = require_user(request, db)
    check_csrf(request, csrf_token)
    student = user.student
    if not student:
        return redirect("/")
    try:
        rows = services.submit_batch(db, student, int(juz))
    except services.ProgramRuleViolation as exc:
        db.rollback()
        return redirect("/", str(exc), error=True)

    m = services.muhaffiz_for(db, student.id, Stage.ONE, int(juz))
    if m and m.user:
        notif.notify(
            db,
            m.user,
            kind=NotificationKind.CHECKPOINT_REQUESTED,
            title=f"{student.name} has {len(rows)} page{'s' if len(rows) != 1 else ''} to recite",
            body=(
                f"Juz {juz}. They cannot move on to new pages until you have heard these, "
                "so a quick sign-off keeps them going."
            ),
            dedupe_key=f"batch:{student.id}:{juz}:{dt.utcnow().isoformat(timespec='minutes')}",
            url=f"/muhaffiz/student/{student.id}?juz={juz}",
            severity="info",
        )
    db.commit()
    return redirect("/", f"{len(rows)} page{'s' if len(rows) != 1 else ''} sent to your Muhaffiz.")


@router.get("/murajaat-plan")
def murajaat_plan_form(request: Request, db: Session = Depends(get_session)):
    """The student's own murajaat schedule."""
    user = require_user(request, db)
    student = user.student
    if not student:
        return redirect("/muhaffiz")

    records = services.all_juz_records(db, student.id)
    available = sorted(j for j, r in records.items() if r.juz_passed) or []
    plan = services.murajaat_plan(db, student.id)
    by_day = {}
    for row in plan:
        by_day.setdefault(row.weekday, []).append(row)

    return render(
        request,
        "student/murajaat_plan.html",
        {
            "user": user,
            "db": db,
            "student": student,
            "available": available,
            "by_day": by_day,
            "weekdays": dt.WEEKDAY_NAMES,
            "portions": Portion.ALL,
            "portion_label": Portion.LABEL,
        },
    )


@router.post("/murajaat-plan")
async def save_murajaat_plan(request: Request, db: Session = Depends(get_session)):
    user = require_user(request, db)
    form = await request.form()
    check_csrf(request, form.get("csrf_token"))
    student = user.student
    if not student:
        return redirect("/")

    # Rows arrive as "weekday:juz:portion" from the checkbox grid.
    slots = []
    for raw in form.getlist("slot"):
        try:
            weekday, juz, portion = raw.split(":")
            slots.append((int(weekday), int(juz), portion))
        except (ValueError, TypeError):
            continue
    services.set_murajaat_plan(db, student.id, slots=slots)
    db.commit()
    return redirect("/murajaat-plan", "Murajaat schedule saved.")


@router.get("/log-class")
def log_class_form(request: Request, db: Session = Depends(get_session)):
    """Record a murajaat class. The outcome is the Muhaffiz's to give."""
    user = require_user(request, db)
    student = user.student
    if not student:
        return redirect("/muhaffiz")

    now = dt.utcnow()
    records = services.all_juz_records(db, student.id)
    available = sorted(
        j for j, r in records.items()
        if r.stage in (JuzStage.MEMORIZING, JuzStage.STAGE2, JuzStage.COMPLETE)
    ) or [student.current_juz]
    today = dt.today_local(user.timezone, now)

    return render(
        request,
        "student/log_class.html",
        {
            "user": user,
            "db": db,
            "student": student,
            "available": available,
            "planned": services.planned_murajaat_for(db, student, today),
            "selected": student.current_juz,
            "classes": services.murajaat_classes_today(db, student, now=now),
            "recent": services.recent_murajaat_classes(db, student.id),
            "stage1": services.muhaffiz_for(db, student.id, Stage.ONE),
        },
    )


@router.post("/log-class")
def log_class(
    request: Request,
    csrf_token: str = Form(...),
    juz: int = Form(...),
    portion: str = Form("full"),
    note: Optional[str] = Form(None),
    db: Session = Depends(get_session),
):
    user = require_user(request, db)
    check_csrf(request, csrf_token)
    student = user.student
    if not student:
        return redirect("/")

    m = services.muhaffiz_for(db, student.id, Stage.ONE, int(juz))
    services.log_murajaat_class(
        db, student, juz=int(juz), portion=portion,
        muhaffiz_id=m.id if m else None, notes=note,
    )
    db.commit()
    return redirect("/", "Murajaat class logged — your Muhaffiz will mark how it went.")


# --- Student-initiated checkpoints# --- Student-initiated checkpoints & progress marking ------------------------


@router.post("/page/memorized")
def mark_memorized(
    request: Request,
    csrf_token: str = Form(...),
    page: int = Form(...),
    db: Session = Depends(get_session),
):
    user = require_user(request, db)
    check_csrf(request, csrf_token)
    student = user.student
    if not student:
        return redirect("/")
    try:
        services.mark_page_memorized(db, student, int(page))
    except services.ProgramRuleViolation as exc:
        db.rollback()
        return redirect("/", str(exc), error=True)
    db.commit()

    idx = page_index_in_juz(int(page))
    msg = f"Page {idx} marked memorized."
    if idx == 20:
        msg = "Juz complete — it now moves to your Stage 2 Muhaffiz for evaluation."
    return redirect("/", msg)


@router.post("/weak-spot")
def add_spot(
    request: Request,
    csrf_token: str = Form(...),
    juz: int = Form(...),
    note: str = Form(...),
    page: Optional[int] = Form(None),
    db: Session = Depends(get_session),
):
    user = require_user(request, db)
    check_csrf(request, csrf_token)
    if not user.student:
        return redirect("/")
    services.add_weak_spot(
        db, user.student.id, juz=int(juz), note=note.strip(), page=int(page) if page else None
    )
    db.commit()
    return redirect(f"/juz/{juz}", "Noted. Keep repeating it until it is perfect.")


@router.post("/weak-spot/{spot_id}/resolve")
def resolve_spot(
    spot_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_session),
):
    from app.models import WeakSpot

    user = require_user(request, db)
    check_csrf(request, csrf_token)
    spot = db.get(WeakSpot, spot_id)
    if spot and user.student and spot.student_id == user.student.id:
        services.resolve_weak_spot(db, spot)
        db.commit()
    return redirect(request.headers.get("referer", "/progress"), "Marked as sorted.")
