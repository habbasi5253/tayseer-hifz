"""Application entrypoint."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from app.config import BASE_DIR, settings
from app.db import get_session, run_migrations
from app.deps import Redirect, require_muhaffiz, require_user
from app import notifications
from app.routes import auth, muhaffiz, student

logging.basicConfig(level=logging.INFO if settings.debug else logging.WARNING)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup work.

    Migrations run automatically for local SQLite so development stays a
    one-command affair. On serverless this hook fires on *every cold start*, and
    concurrent starts racing each other through the same migration is a good way
    to corrupt a schema — so production goes through `python -m scripts.migrate`
    as a deliberate, single step.
    """
    if settings.auto_migrate or (settings.is_sqlite and not settings.serverless):
        run_migrations()
    yield


app = FastAPI(title=settings.app_name, docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount(
    "/static",
    # check_dir=False: Vercel promotes these to its CDN at build time and the
    # directory may not survive into the function bundle. Raising at import over
    # a file the CDN already serves would take the whole app down.
    StaticFiles(directory=str(BASE_DIR / "app" / "static"), check_dir=False),
    name="static",
)


@app.exception_handler(Redirect)
async def _redirect_handler(request: Request, exc: Redirect):
    """Unauthenticated requests bounce to login rather than 500."""
    return RedirectResponse(exc.url, status_code=303)


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException):
    if exc.status_code == 404:
        return HTMLResponse(_error_page("Not found", "That page does not exist."), status_code=404)
    if exc.status_code in (401, 403):
        return HTMLResponse(_error_page("Not allowed", str(exc.detail)), status_code=exc.status_code)
    return HTMLResponse(_error_page("Something went wrong", str(exc.detail)), status_code=exc.status_code)


def _error_page(title: str, detail: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><link rel="stylesheet" href="/static/app.css"></head>
<body><main class="wrap"><div class="card" style="margin-top:40px">
<h2>{title}</h2><p class="muted">{detail}</p>
<a class="btn mt" href="/">Back to today</a></div></main></body></html>"""


app.include_router(auth.router)
app.include_router(student.router)
app.include_router(muhaffiz.router)


# --- Report ------------------------------------------------------------------


@app.get("/report.pdf")
def my_report(request: Request, db: Session = Depends(get_session)):
    from app.report import build_report

    user = require_user(request, db)
    if not user.student:
        raise HTTPException(403, "Only students have a progress report.")
    pdf = build_report(db, user.student)
    slug = "".join(c if c.isalnum() else "-" for c in user.name).strip("-").lower() or "student"
    return Response(
        pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="tayseer-{slug}.pdf"'},
    )


@app.get("/muhaffiz/student/{student_id}/report.pdf")
def student_report(student_id: int, request: Request, db: Session = Depends(get_session)):
    from app import services
    from app.report import build_report

    m = require_muhaffiz(request, db)
    target = services.get_student(db, student_id)
    if target is None:
        raise HTTPException(404, "Student not found.")
    if not any(a.muhaffiz_id == m.id for a in services.active_assignments(db, student_id)):
        raise HTTPException(403, "You are not assigned to this student.")

    pdf = build_report(db, target)
    slug = "".join(c if c.isalnum() else "-" for c in target.name).strip("-").lower() or "student"
    return Response(
        pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="tayseer-{slug}.pdf"'},
    )


@app.get("/healthz")
def healthz():
    return {"ok": True}


# --- Scheduled work ----------------------------------------------------------


@app.get("/api/cron/reminders")
def cron_reminders(request: Request, db: Session = Depends(get_session)):
    """Notification sweep, invoked by a scheduler.

    Vercel sends `Authorization: Bearer $CRON_SECRET` when that environment
    variable is set. Without it the endpoint is a public trigger, so an unset
    secret is refused outright rather than silently running open — failing
    closed is the right default for something that sends email.

    Set CRON_MODE=daily when the scheduler can only fire once a day (Vercel's
    Hobby plan). Reminders then land at a fixed UTC hour instead of each
    student's chosen local hour. The sweep is idempotent either way: every
    notification carries a date-stamped dedupe key behind a unique index, which
    is what Vercel's own cron guidance asks for, since delivery is best-effort
    and may double-fire.
    """
    secret = settings.cron_secret
    if not secret:
        raise HTTPException(503, "CRON_SECRET is not configured.")
    if request.headers.get("authorization") != f"Bearer {secret}":
        raise HTTPException(401, "Unauthorized.")

    hourly = os.environ.get("CRON_MODE", "hourly").lower() != "daily"
    created = notifications.sweep_all(db, respect_reminder_hour=hourly)
    db.commit()
    return {"ok": True, "created": created, "mode": "hourly" if hourly else "daily"}
