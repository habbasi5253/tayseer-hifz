"""Registration, login, onboarding, settings."""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import notifications as notif
from app.config import settings
from app.db import get_session
from app.deps import check_csrf, get_current_user, redirect, render, require_user
from app.domain import dates as dt
from app.domain.marhala import marhala_choices
from app.domain.quran import JUZ_COUNT
from app.models import MuhaffizProfile, StudentProfile, User
from app.security import (
    check_password_strength,
    hash_password,
    make_session_token,
    needs_rehash,
    verify_password,
)

router = APIRouter()

COMMON_ZONES = [
    "UTC", "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
    "America/Toronto", "Europe/London", "Europe/Paris", "Europe/Berlin", "Africa/Cairo",
    "Africa/Nairobi", "Asia/Dubai", "Asia/Karachi", "Asia/Kolkata", "Asia/Colombo",
    "Asia/Dhaka", "Asia/Jakarta", "Asia/Kuala_Lumpur", "Asia/Singapore", "Australia/Sydney",
    "Pacific/Auckland",
]


def _set_session(resp, user_id: int):
    resp.set_cookie(
        settings.session_cookie,
        make_session_token(user_id),
        max_age=settings.session_max_age,
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
        path="/",
    )
    return resp


@router.get("/login")
def login_form(request: Request, db: Session = Depends(get_session)):
    if get_current_user(request, db):
        return redirect("/")
    return render(request, "auth/login.html", {"user": None, "db": db})


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_session),
):
    user = db.execute(
        select(User).where(User.email == email.strip().lower())
    ).scalar_one_or_none()

    if not user or not verify_password(password, user.password_hash):
        # One message for both cases, so this cannot be used to enumerate accounts.
        return render(
            request,
            "auth/login.html",
            {"user": None, "db": db, "error": "That email and password do not match.", "email": email},
        )

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        db.commit()

    return _set_session(redirect("/"), user.id)


@router.get("/logout")
@router.post("/logout")
def logout():
    resp = redirect("/login", "Signed out.")
    resp.delete_cookie(settings.session_cookie, path="/")
    return resp


@router.get("/register")
def register_form(request: Request, db: Session = Depends(get_session)):
    if get_current_user(request, db):
        return redirect("/")
    return render(request, "auth/register.html", {"user": None, "db": db, "zones": COMMON_ZONES})


@router.post("/register")
def register(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    timezone: str = Form("UTC"),
    role: str = Form("student"),
    db: Session = Depends(get_session),
):
    email = email.strip().lower()
    ctx = {"user": None, "db": db, "zones": COMMON_ZONES, "name": name, "email": email, "role": role}

    if db.execute(select(User).where(User.email == email)).scalar_one_or_none():
        return render(request, "auth/register.html", {**ctx, "error": "That email is already registered."})

    problem = check_password_strength(password)
    if problem:
        return render(request, "auth/register.html", {**ctx, "error": problem})

    if not dt.is_valid_timezone(timezone):
        timezone = "UTC"

    user = User(
        email=email,
        name=name.strip() or email,
        password_hash=hash_password(password),
        timezone=timezone,
        is_student=role in ("student", "both"),
        is_muhaffiz=role in ("muhaffiz", "both"),
    )
    db.add(user)
    db.flush()

    if user.is_student:
        db.add(StudentProfile(user_id=user.id))
    if user.is_muhaffiz:
        db.add(MuhaffizProfile(user_id=user.id))
    db.commit()

    dest = "/onboarding" if user.is_student else "/muhaffiz"
    return _set_session(redirect(dest, f"Welcome, {user.name}."), user.id)


# --- Onboarding --------------------------------------------------------------


@router.get("/onboarding")
def onboarding(request: Request, db: Session = Depends(get_session)):
    user = require_user(request, db)
    if not user.student:
        return redirect("/muhaffiz")

    muhaffizin = list(
        db.execute(
            select(MuhaffizProfile).join(User, MuhaffizProfile.user_id == User.id).order_by(User.name)
        )
        .scalars()
        .all()
    )
    from app import services

    return render(
        request,
        "auth/onboarding.html",
        {
            "user": user,
            "db": db,
            "student": user.student,
            "muhaffizin": muhaffizin,
            "zones": COMMON_ZONES,
            "juz_range": range(1, JUZ_COUNT + 1),
            "assignments": services.active_assignments(db, user.student.id),
            "marahil": marhala_choices(),
        },
    )


@router.post("/onboarding")
def save_onboarding(
    request: Request,
    csrf_token: str = Form(...),
    marhala: int = Form(1),
    current_juz: int = Form(1),
    daily_minutes: int = Form(30),
    preferred_method: int = Form(2),
    timezone: str = Form("UTC"),
    day: Optional[List[str]] = Form(None),
    stage1_muhaffiz: Optional[str] = Form(None),
    stage2_muhaffiz: Optional[str] = Form(None),
    db: Session = Depends(get_session),
):
    from app import services

    user = require_user(request, db)
    check_csrf(request, csrf_token)
    student = user.student
    if not student:
        return redirect("/")

    if dt.is_valid_timezone(timezone):
        user.timezone = timezone

    student.marhala = int(marhala)
    student.current_juz = max(1, min(JUZ_COUNT, int(current_juz)))
    student.daily_minutes = max(5, min(600, int(daily_minutes)))
    student.preferred_method = int(preferred_method)

    # Checkbox group -> 7-char mask. Absent means every day is active.
    selected = set(day or [])
    student.active_days = (
        "".join("1" if str(i) in selected else "0" for i in range(7)) if day is not None
        else dt.ALL_DAYS_ON
    )
    if "1" not in student.active_days:
        student.active_days = dt.ALL_DAYS_ON

    error = None
    for raw, stage in ((stage1_muhaffiz, "stage1"), (stage2_muhaffiz, "stage2")):
        if raw:
            try:
                services.assign_muhaffiz(
                    db, student_id=student.id, muhaffiz_id=int(raw), stage=stage
                )
            except services.ProgramRuleViolation as exc:
                error = str(exc)

    db.commit()
    if error:
        return redirect("/onboarding", error, error=True)
    return redirect("/", "Setup saved.")


# --- Settings ----------------------------------------------------------------


@router.get("/settings")
def settings_page(request: Request, db: Session = Depends(get_session)):
    from app import services

    user = require_user(request, db)
    assignments = services.active_assignments(db, user.student.id) if user.student else []
    return render(
        request,
        "auth/settings.html",
        {
            "user": user,
            "db": db,
            "student": user.student,
            "zones": COMMON_ZONES,
            "assignments": assignments,
        },
    )


@router.post("/settings")
def save_settings(
    request: Request,
    csrf_token: str = Form(...),
    name: str = Form(...),
    timezone: str = Form("UTC"),
    reminder_hour: int = Form(19),
    email_notifications: Optional[str] = Form(None),
    daily_minutes: Optional[int] = Form(None),
    wake_hour: Optional[int] = Form(None),
    juz_order: Optional[str] = Form(None),
    evening_hour: Optional[int] = Form(None),
    day: Optional[List[str]] = Form(None),
    db: Session = Depends(get_session),
):
    user = require_user(request, db)
    check_csrf(request, csrf_token)

    user.name = name.strip() or user.name
    if dt.is_valid_timezone(timezone):
        user.timezone = timezone
    user.reminder_hour = max(0, min(23, int(reminder_hour)))
    user.email_notifications = email_notifications is not None

    if user.student:
        if daily_minutes is not None:
            user.student.daily_minutes = max(5, min(600, int(daily_minutes)))
        if juz_order is not None:
            # Blank clears it back to the default 1..30. `parse_juz_order`
            # rejects anything that is not a complete permutation, so a typo
            # cannot half-apply and gate the wrong juz.
            from app.domain.progression import DEFAULT_JUZ_ORDER, parse_juz_order

            cleaned = juz_order.strip()
            if not cleaned:
                user.student.juz_order = None
            else:
                parsed = parse_juz_order(cleaned)
                user.student.juz_order = (
                    None if parsed == DEFAULT_JUZ_ORDER else ",".join(str(j) for j in parsed)
                )
        if wake_hour is not None:
            user.student.wake_hour = max(0, min(23, int(wake_hour)))
        if evening_hour is not None:
            user.student.evening_hour = max(0, min(23, int(evening_hour)))
        if day is not None:
            selected = set(day)
            mask = "".join("1" if str(i) in selected else "0" for i in range(7))
            user.student.active_days = mask if "1" in mask else dt.ALL_DAYS_ON

    db.commit()
    return redirect("/settings", "Settings saved.")


# --- Notifications -----------------------------------------------------------


@router.get("/notifications")
def notifications_page(request: Request, db: Session = Depends(get_session)):
    user = require_user(request, db)
    items = notif.recent_for(db, user.id)
    return render(request, "notifications.html", {"user": user, "db": db, "items": items})


@router.post("/notifications/read")
def mark_read(request: Request, csrf_token: str = Form(...), db: Session = Depends(get_session)):
    user = require_user(request, db)
    check_csrf(request, csrf_token)
    notif.mark_all_read(db, user.id)
    db.commit()
    return redirect("/notifications")


# --- Password recovery -------------------------------------------------------


@router.get("/reset/{token}")
def reset_form(token: str, request: Request, db: Session = Depends(get_session)):
    from app import services

    user = services.consume_reset_token(db, token)
    return render(
        request,
        "auth/reset.html",
        {"user": None, "db": db, "token": token, "valid": user is not None,
         "name": user.name if user else None},
    )


@router.post("/reset/{token}")
def do_reset(
    token: str,
    request: Request,
    password: str = Form(...),
    db: Session = Depends(get_session),
):
    from app import services

    problem = check_password_strength(password)
    if problem:
        user = services.consume_reset_token(db, token)
        return render(
            request,
            "auth/reset.html",
            {"user": None, "db": db, "token": token, "valid": user is not None,
             "name": user.name if user else None, "error": problem},
        )

    user = services.complete_reset(db, token, password)
    if user is None:
        return render(
            request,
            "auth/reset.html",
            {"user": None, "db": db, "token": token, "valid": False},
        )
    db.commit()
    return _set_session(redirect("/", "Password updated."), user.id)
