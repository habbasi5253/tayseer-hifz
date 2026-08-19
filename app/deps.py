"""Request dependencies: current user, role guards, template environment."""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.config import BASE_DIR, settings
from app.db import get_session
from app.domain import dates as dt
from app.domain.marhala import marhala_choices
from app.domain.quran import JUZ_COUNT, label_page, juz_name, page_index_in_juz
from app.domain.revision import method_choices
from app.models import MuhaffizProfile, StudentProfile, User
from app.security import make_csrf_token, read_session_token, verify_csrf_token

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

# Helpers every template can reach.
templates.env.globals.update(
    app_name=settings.app_name,
    juz_count=JUZ_COUNT,
    marhala_choices=marhala_choices,
    method_choices=method_choices,
    weekday_short=dt.WEEKDAY_SHORT,
)
templates.env.filters["local"] = dt.format_local
templates.env.filters["humandays"] = dt.humanize_days
templates.env.filters["pagelabel"] = label_page
templates.env.filters["juzname"] = juz_name
templates.env.filters["pageindex"] = page_index_in_juz


class Redirect(Exception):
    """Raised to bounce an unauthenticated request to the login page."""

    def __init__(self, url: str):
        self.url = url


def get_current_user(request: Request, db: Session = Depends(get_session)) -> Optional[User]:
    uid = read_session_token(request.cookies.get(settings.session_cookie))
    if uid is None:
        return None
    return db.execute(
        select(User)
        .options(joinedload(User.student), joinedload(User.muhaffiz))
        .where(User.id == uid)
    ).scalar_one_or_none()


def require_user(request: Request, db: Session = Depends(get_session)) -> User:
    user = get_current_user(request, db)
    if user is None:
        raise Redirect("/login")
    return user


def require_student(request: Request, db: Session = Depends(get_session)) -> StudentProfile:
    user = require_user(request, db)
    if not user.student:
        raise Redirect("/onboarding")
    return user.student


def require_muhaffiz(request: Request, db: Session = Depends(get_session)) -> MuhaffizProfile:
    user = require_user(request, db)
    if not user.muhaffiz:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This page is for a Muhaffiz account.")
    return user.muhaffiz


def csrf_for(request: Request) -> str:
    token = request.cookies.get(settings.session_cookie)
    return make_csrf_token(token) if token else ""


def check_csrf(request: Request, submitted: Optional[str]) -> None:
    if not verify_csrf_token(submitted, request.cookies.get(settings.session_cookie)):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Your session expired. Please try again.")


def render(request: Request, template: str, ctx: dict):
    """Render with the context every page needs."""
    from app import notifications as notif

    db: Optional[Session] = ctx.get("db")
    user: Optional[User] = ctx.get("user")

    base = {
        "request": request,
        "csrf_token": csrf_for(request),
        "unread": notif.unread_for(db, user.id) if (db is not None and user) else [],
        "now": dt.utcnow(),
    }
    base.update(ctx)
    base.pop("db", None)
    return templates.TemplateResponse(template, base)


def redirect(url: str, flash: Optional[str] = None, error: bool = False) -> RedirectResponse:
    """Post/Redirect/Get, carrying a one-shot message in a short-lived cookie."""
    resp = RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)
    if flash:
        resp.set_cookie(
            "flash_error" if error else "flash",
            flash,
            max_age=10,
            httponly=True,
            samesite="lax",
            secure=settings.secure_cookies,
        )
    return resp
