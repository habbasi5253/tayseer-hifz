"""In-app notifications with a pluggable delivery channel.

Email is the first channel because it needs no device permissions and reaches
adults with jobs reliably. `Channel` is an interface, so web push or SMS drops
in later without touching any of the logic that decides *what* to send.

Tone rule for everything in this file: these messages arrive on a phone at the
end of a long day. They report a fact and name the next action. They never
imply the student is failing.
"""

from __future__ import annotations

import logging
import smtplib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.domain import dates as dt
from app.domain import revalidation as rv
from app.models import Notification, NotificationKind, StudentProfile, User

log = logging.getLogger("tayseer.notify")


# --- Channels ----------------------------------------------------------------


@dataclass
class Message:
    to_email: str
    to_name: str
    subject: str
    body: str


class Channel(ABC):
    @abstractmethod
    def send(self, message: Message) -> bool:
        """Return True if delivered. Never raise on transient failure."""


class ConsoleChannel(Channel):
    """Development default. Logs instead of sending, so nobody gets mailed."""

    def send(self, message: Message) -> bool:
        log.info(
            "[email suppressed] to=%s subject=%s\n%s",
            message.to_email,
            message.subject,
            message.body,
        )
        return True


class SmtpChannel(Channel):
    def send(self, message: Message) -> bool:
        msg = EmailMessage()
        msg["From"] = settings.smtp_from
        msg["To"] = f"{message.to_name} <{message.to_email}>"
        msg["Subject"] = message.subject
        msg.set_content(message.body)
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as s:
                if settings.smtp_tls:
                    s.starttls()
                if settings.smtp_user:
                    s.login(settings.smtp_user, settings.smtp_password)
                s.send_message(msg)
            return True
        except Exception as exc:  # a failed reminder must never break a request
            log.warning("email delivery failed to %s: %s", message.to_email, exc)
            return False


def get_channel() -> Channel:
    return SmtpChannel() if settings.email_enabled else ConsoleChannel()


# --- Creation ----------------------------------------------------------------


def notify(
    db: Session,
    user: User,
    *,
    kind: str,
    title: str,
    body: str,
    dedupe_key: str,
    url: Optional[str] = None,
    severity: str = "info",
    send_email: bool = True,
) -> Optional[Notification]:
    """Create a notification, silently skipping exact duplicates.

    The unique index on (user_id, dedupe_key) is the real guard. Catching the
    IntegrityError rather than pre-checking makes this safe if two workers ever
    run the sweep at once.
    """
    row = Notification(
        user_id=user.id,
        kind=kind,
        title=title,
        body=body,
        url=url,
        severity=severity,
        dedupe_key=dedupe_key,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return None

    if send_email and user.email_notifications:
        ok = get_channel().send(
            Message(to_email=user.email, to_name=user.name, subject=title, body=body)
        )
        if ok:
            row.emailed_at = dt.utcnow()
            db.flush()
    return row


def unread_for(db: Session, user_id: int, limit: int = 20) -> List[Notification]:
    return list(
        db.execute(
            select(Notification)
            .where(Notification.user_id == user_id, Notification.read_at.is_(None))
            .order_by(Notification.created_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )


def recent_for(db: Session, user_id: int, limit: int = 50) -> List[Notification]:
    return list(
        db.execute(
            select(Notification)
            .where(Notification.user_id == user_id)
            .order_by(Notification.created_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )


def mark_all_read(db: Session, user_id: int) -> int:
    rows = db.execute(
        select(Notification).where(
            Notification.user_id == user_id, Notification.read_at.is_(None)
        )
    ).scalars()
    n = 0
    now = dt.utcnow()
    for r in rows:
        r.read_at = now
        n += 1
    db.flush()
    return n


# --- The daily sweep ---------------------------------------------------------


def sweep_student(
    db: Session, student: StudentProfile, now: Optional[datetime] = None
) -> List[Notification]:
    """Generate today's notifications for one student.

    Called by `scripts/send_reminders.py`, which is meant to run hourly; each
    student is only acted on when the local clock reaches their reminder hour,
    so a single cron covers every timezone.
    """
    from app import services

    user = student.user
    if user is None:
        return []

    now = now or dt.utcnow()
    tz = user.timezone
    today = dt.today_local(tz, now)
    out: List[Notification] = []

    board = services.build_board(db, student, now=now)

    # 1. A juz whose 30 days ran out. The most consequential thing in the app:
    #    tasmee for that juz starts again from page 1.
    for w in board.expired:
        n = notify(
            db,
            user,
            kind=NotificationKind.REVALIDATION_OVERDUE,
            title=f"Juz {w.juz} tasmee restarts from page 1",
            body=(
                f"The 30 days to tasmee juz {w.juz} to your Stage 2 Muhaffiz have passed, "
                f"so that attempt is closed and tasmee begins again at page 1.\n\n"
                "Everything you already recited stays in your record — your Muhaffiz can "
                "still see it. Booking the next sitting starts the new window."
            ),
            dedupe_key=f"expired:{w.juz}:{w.attempt}",
            url=f"/certificate/{w.juz}",
            severity="attention",
        )
        if n:
            out.append(n)

    # 2. Windows crossing a warning threshold today — 7, 3, 1 and 0 days out.
    #    Thresholds rather than a daily message, so this stays a signal.
    for warn in rv.deadline_warnings(board):
        w, lead = warn["window"], warn["lead"]
        when = {0: "today is the last day", 1: "1 day left", 3: "3 days left",
                7: "1 week left"}.get(lead, f"{lead} days left")
        n = notify(
            db,
            user,
            kind=NotificationKind.REVALIDATION_WARNING,
            title=f"Juz {w.juz} tasmee — {when}",
            body=(
                f"{w.pace_text}\n\n"
                "If the window closes, tasmee for this juz starts again from page 1."
            ),
            dedupe_key=f"warn:{w.juz}:{w.attempt}:{lead}",
            url=f"/certificate/{w.juz}",
            severity="soon" if lead > 0 else "attention",
        )
        if n:
            out.append(n)

    # 3. Retention-risk nudge on revision method.
    nudge = services.method_nudge_for(db, student, juz=student.current_juz, now=now)
    if nudge.should_nudge:
        n = notify(
            db,
            user,
            kind=NotificationKind.METHOD_NUDGE,
            title=nudge.headline,
            body=nudge.detail,
            # Weekly, not daily — this is advice, not an alarm.
            dedupe_key=f"method:{today.isocalendar()[0]}-{today.isocalendar()[1]}",
            url="/revise",
            severity=nudge.severity,
        )
        if n:
            out.append(n)

    # 4. The daily nudge to revise and log — only on active days, and only if
    #    they have not already logged. Nobody needs a reminder to do the thing
    #    they already did.
    if dt.is_active_day(student.active_days, today):
        streak = services.streak_for(db, student, now=now)
        if not streak.logged_today:
            n = notify(
                db,
                user,
                kind=NotificationKind.DAILY_REMINDER,
                title="Time for today's murajaat",
                body=(
                    f"{streak.headline}\n{streak.support_line}\n\n"
                    "Even fifteen minutes counts. Log it when you are done."
                ),
                dedupe_key=f"daily:{today.isoformat()}",
                url="/revise",
                severity="info",
            )
            if n:
                out.append(n)

    return out


def sweep_all(
    db: Session,
    now: Optional[datetime] = None,
    respect_reminder_hour: bool = True,
) -> int:
    """Sweep students and return the number of notifications created.

    With `respect_reminder_hour` (the default) only students whose local clock
    has reached their chosen hour are swept, so an hourly job delivers each
    reminder at the right local time and covers every timezone from one cron.

    Set it False when the scheduler can only fire once a day — Vercel's Hobby
    plan, for instance. Everyone is then swept on that single run, which means
    reminders land at a fixed UTC time rather than each student's preferred
    hour. That is safe to do because every notification carries a date-stamped
    `dedupe_key` and the unique index makes a repeat a no-op, so this stays
    idempotent however often it runs.
    """
    now = now or dt.utcnow()
    students = (
        db.execute(select(StudentProfile).join(User, StudentProfile.user_id == User.id))
        .scalars()
        .all()
    )
    sent = 0
    for s in students:
        user = s.user
        if user is None:
            continue
        try:
            local_hour = dt.ensure_utc(now).astimezone(dt.get_zone(user.timezone)).hour
        except dt.InvalidTimezone:
            log.warning("student %s has an invalid timezone %r; skipping", s.id, user.timezone)
            continue
        if respect_reminder_hour and local_hour != user.reminder_hour:
            continue
        sent += len(sweep_student(db, s, now=now))
    return sent
