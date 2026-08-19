"""SQLAlchemy models.

Multi-tenant from the start: nothing is scoped to a single student implicitly.
Every log row carries `student_id`, and every query in `app/services.py` filters
on it. There is no "current student" global.

Timestamp convention: every datetime column is `DateTime(timezone=True)` and
every value written through the services layer is aware UTC. Local dates are
stored *alongside* the timestamp (see `local_date` columns) rather than derived
at query time, because "which day did this count for" is a fact about the
student's timezone at the moment of logging — if they later move from Chicago to
Dubai, yesterday's revision should not silently hop to a different day.
"""

from datetime import date, datetime
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.domain.dates import utcnow


class Base(DeclarativeBase):
    pass


# --- Enum-ish string constants ----------------------------------------------
# Kept as plain strings rather than native DB enums so that adding a value is a
# code change, not a migration, and so SQLite and Postgres behave identically.


class Stage:
    ONE = "stage1"
    TWO = "stage2"
    ALL = (ONE, TWO)
    LABEL = {ONE: "Stage 1 — Memorization", TWO: "Stage 2 — Tasmee"}
    SHORT = {ONE: "Stage 1", TWO: "Stage 2"}


class AssignmentStatus:
    ACTIVE = "active"
    ENDED = "ended"


class JuzStage:
    NOT_STARTED = "not_started"
    MEMORIZING = "memorizing"
    STAGE2 = "stage2"  # fully memorized, under evaluation by the second Muhaffiz
    COMPLETE = "complete"

    LABEL = {
        NOT_STARTED: "Not started",
        MEMORIZING: "Memorizing",
        STAGE2: "Tasmee with Muhaffiz 2",
        COMPLETE: "Complete",
    }


class RevisionKind:
    HALI = "hali"  # juz al hali — the juz currently being memorized
    ROTATION = "rotation"  # a previously completed juz, on rotation


class NotificationKind:
    DAILY_REMINDER = "daily_reminder"
    REVALIDATION_WARNING = "revalidation_warning"
    REVALIDATION_OVERDUE = "revalidation_overdue"
    METHOD_NUDGE = "method_nudge"
    CHECKPOINT_DECIDED = "checkpoint_decided"
    CHECKPOINT_REQUESTED = "checkpoint_requested"
    TASMEE_FEEDBACK = "tasmee_feedback"
    MILESTONE = "milestone"


# --- Identity ----------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(120))
    # IANA zone. Drives every date calculation for this user's own records.
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    is_student: Mapped[bool] = mapped_column(Boolean, default=False)
    is_muhaffiz: Mapped[bool] = mapped_column(Boolean, default=False)
    email_notifications: Mapped[bool] = mapped_column(Boolean, default=True)
    reminder_hour: Mapped[int] = mapped_column(Integer, default=19)  # local hour
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    student: Mapped[Optional["StudentProfile"]] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    muhaffiz: Mapped[Optional["MuhaffizProfile"]] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.id} {self.email}>"


class StudentProfile(Base):
    __tablename__ = "student_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)

    marhala: Mapped[int] = mapped_column(Integer, default=1)
    current_juz: Mapped[int] = mapped_column(Integer, default=1)

    # Time budget for the schedule builder.
    daily_minutes: Mapped[int] = mapped_column(Integer, default=30)
    # 7 chars of "0"/"1", index 0 = Monday. See app/domain/dates.py.
    active_days: Mapped[str] = mapped_column(String(7), default="1111111")
    preferred_method: Mapped[int] = mapped_column(Integer, default=2)

    # Anchors for the suggested times on the daily plan. The guide's advice is
    # to start counting your free time from fajr, so that is the anchor for the
    # freshest slot; evening is the wind-down slot for murajaat.
    wake_hour: Mapped[int] = mapped_column(Integer, default=6)
    evening_hour: Mapped[int] = mapped_column(Integer, default=20)

    # Optional custom juz sequence, comma-separated. NULL means 1..30, which is
    # what almost everyone does; the reorder control is deliberately tucked away
    # in settings rather than put in front of every new student.
    juz_order: Mapped[Optional[str]] = mapped_column(String(120))

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped["User"] = relationship(back_populates="student")

    @property
    def timezone(self) -> str:
        return self.user.timezone if self.user else "UTC"

    @property
    def name(self) -> str:
        return self.user.name if self.user else ""


class MuhaffizProfile(Base):
    __tablename__ = "muhaffiz_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    bio: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped["User"] = relationship(back_populates="muhaffiz")

    @property
    def name(self) -> str:
        return self.user.name if self.user else ""


class Assignment(Base):
    """A student <-> Muhaffiz relationship, scoped to a stage.

    Stage 1 and Stage 2 are mutually exclusive *per juz* and must be held by
    different people — the program mandates a second, different Muhaffiz for
    evaluation. That rule is enforced in `app/services.py::assign_muhaffiz`
    rather than by a DB constraint, because the check spans rows.

    `juz` is nullable: NULL means the assignment covers the student's whole
    programme, which is the common case. A per-juz row lets a coordinator hand a
    single juz to a different Muhaffiz without disturbing the rest.
    """

    __tablename__ = "assignments"
    __table_args__ = (
        Index("ix_assignment_student_stage", "student_id", "stage", "status"),
        Index("ix_assignment_muhaffiz", "muhaffiz_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("student_profiles.id", ondelete="CASCADE"))
    muhaffiz_id: Mapped[int] = mapped_column(ForeignKey("muhaffiz_profiles.id", ondelete="CASCADE"))
    stage: Mapped[str] = mapped_column(String(16))
    juz: Mapped[Optional[int]] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default=AssignmentStatus.ACTIVE)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    student: Mapped["StudentProfile"] = relationship()
    muhaffiz: Mapped["MuhaffizProfile"] = relationship()


# --- Progress ----------------------------------------------------------------


class StudentJuz(Base):
    """Per-juz progress through the two stages."""

    __tablename__ = "student_juz"
    __table_args__ = (UniqueConstraint("student_id", "juz", name="uq_student_juz"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(
        ForeignKey("student_profiles.id", ondelete="CASCADE"), index=True
    )
    juz: Mapped[int] = mapped_column(Integer)
    stage: Mapped[str] = mapped_column(String(16), default=JuzStage.NOT_STARTED)

    memorization_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    memorization_completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    stage2_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    stage2_completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Muhaffiz 2's pass on the full-juz tasmee. This is the gate that opens the
    # next juz, so it is stored separately from stage2_completed_at rather than
    # inferred from page counts.
    juz_tasmee_passed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    juz_tasmee_passed_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("muhaffiz_profiles.id", ondelete="SET NULL")
    )
    # The 30-day clock. Starts when Muhaffiz 1 signs off the whole juz. On
    # expiry it moves forward and `tasmee_attempt` increments, so only pages
    # recited on or after this instant count — the restart-from-page-1 rule,
    # expressed without deleting any history.
    tasmee_window_started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    tasmee_attempt: Mapped[int] = mapped_column(Integer, default=1)

    @property
    def juz_passed(self) -> bool:
        return self.juz_tasmee_passed_at is not None


class PageProgress(Base):
    """One row per page the student has actually memorized.

    This table is what defines Stage 2 *scope*: a page is judged by the 30-day
    rule only once `stage2_entered_at` is set. Without that distinction the
    dashboard would open on day one showing 600 overdue pages.
    """

    __tablename__ = "page_progress"
    __table_args__ = (
        UniqueConstraint("student_id", "page", name="uq_student_page"),
        Index("ix_page_progress_student_juz", "student_id", "juz"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(
        ForeignKey("student_profiles.id", ondelete="CASCADE"), index=True
    )
    page: Mapped[int] = mapped_column(Integer)
    juz: Mapped[int] = mapped_column(Integer, index=True)
    memorized_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    memorized_on: Mapped[Optional[date]] = mapped_column(Date)

    # The Stage 1 loop: the student memorizes, submits a batch to Muhaffiz 1,
    # and the page is either signed off or returned to be recited again. A page
    # that has been returned clears `submitted_at` so it rejoins the batch.
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    signed_off_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    signed_off_by_muhaffiz_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("muhaffiz_profiles.id", ondelete="SET NULL")
    )
    returned_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    stage2_entered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    @property
    def in_stage2(self) -> bool:
        return self.stage2_entered_at is not None

    @property
    def is_signed_off(self) -> bool:
        return self.signed_off_at is not None

    @property
    def is_pending_signoff(self) -> bool:
        return self.submitted_at is not None and self.signed_off_at is None

    @property
    def is_returned(self) -> bool:
        return self.returned_at is not None and self.signed_off_at is None


# --- Stage 1 checkpoints -----------------------------------------------------


class DailyRevisionLog(Base):
    """One murajaat session. The one-tap log."""

    __tablename__ = "daily_revision_logs"
    __table_args__ = (
        Index("ix_revision_student_date", "student_id", "local_date"),
        Index("ix_revision_student_juz_date", "student_id", "juz", "local_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(
        ForeignKey("student_profiles.id", ondelete="CASCADE"), index=True
    )
    juz: Mapped[int] = mapped_column(Integer)
    method: Mapped[int] = mapped_column(Integer)  # 1..5, see domain/revision.py
    kind: Mapped[str] = mapped_column(String(16), default=RevisionKind.HALI)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[Optional[str]] = mapped_column(Text)
    logged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # The student's local calendar date at the moment of logging. Frozen here
    # deliberately — see the module docstring.
    local_date: Mapped[date] = mapped_column(Date, index=True)


class TasmeeSession(Base):
    """A sitting in which the student recited to a Muhaffiz.

    Covers both stages. In Stage 1 this is explicitly *not* a formal ikhtibar,
    so per-criterion results are optional; in Stage 2 all three criteria are
    required and drive revalidation.
    """

    __tablename__ = "tasmee_sessions"
    __table_args__ = (
        Index("ix_tasmee_student_stage_date", "student_id", "stage", "local_date"),
        Index("ix_tasmee_muhaffiz", "muhaffiz_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(
        ForeignKey("student_profiles.id", ondelete="CASCADE"), index=True
    )
    muhaffiz_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("muhaffiz_profiles.id", ondelete="SET NULL")
    )
    stage: Mapped[str] = mapped_column(String(16))
    juz: Mapped[int] = mapped_column(Integer)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    local_date: Mapped[date] = mapped_column(Date, index=True)

    results: Mapped[List["TasmeePageResult"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="TasmeePageResult.page"
    )
    muhaffiz: Mapped[Optional["MuhaffizProfile"]] = relationship()

    @property
    def page_count(self) -> int:
        return len(self.results)

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.results if r.passed)


class TasmeePageResult(Base):
    """Per-page outcome inside a sitting.

    `passed` is stored rather than computed on read because it is the column the
    30-day rule query filters and sorts on — deriving it in SQL from three
    booleans on every dashboard load is the one place where denormalizing
    genuinely pays. It is written in exactly one place
    (`services.record_tasmee`) and is immutable thereafter.

    `student_id` is denormalized off the session for the same reason: the
    revalidation query is "latest passing result per page for this student" and
    should not need a join.
    """

    __tablename__ = "tasmee_page_results"
    __table_args__ = (
        Index("ix_result_student_page_passed", "student_id", "page", "passed", "recorded_at"),
        Index("ix_result_session", "session_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("tasmee_sessions.id", ondelete="CASCADE"))
    student_id: Mapped[int] = mapped_column(
        ForeignKey("student_profiles.id", ondelete="CASCADE"), index=True
    )
    page: Mapped[int] = mapped_column(Integer, index=True)
    juz: Mapped[int] = mapped_column(Integer, index=True)
    stage: Mapped[str] = mapped_column(String(16))

    # The three Stage 2 criteria, evaluated independently. NULL in Stage 1.
    hifz_pass: Mapped[Optional[bool]] = mapped_column(Boolean)
    makharij_pass: Mapped[Optional[bool]] = mapped_column(Boolean)
    tajweed_pass: Mapped[Optional[bool]] = mapped_column(Boolean)

    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    session: Mapped["TasmeeSession"] = relationship(back_populates="results")

    @property
    def failed_criteria(self) -> List[str]:
        out = []
        if self.hifz_pass is False:
            out.append("hifz")
        if self.makharij_pass is False:
            out.append("makharij")
        if self.tajweed_pass is False:
            out.append("tajweed")
        return out


class MurajaatClass(Base):
    """A murajaat sitting that Muhaffiz 1 heard and graded.

    The program does not treat murajaat as private homework: the student revises
    a juz from an agreed schedule, recites it in class, and the Muhaffiz judges
    whether that class went well. So this carries an outcome, unlike
    `DailyRevisionLog`, which is the student's own record of revising alone.

    Both exist deliberately. Solo revision is the daily habit the streak and the
    method-mix nudge are built on; this is the graded checkpoint on top of it.
    """

    __tablename__ = "murajaat_classes"
    __table_args__ = (Index("ix_murajaat_class_student_date", "student_id", "local_date"),)

    PENDING = "pending"
    GOOD = "good"
    NEEDS_WORK = "needs_work"

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(
        ForeignKey("student_profiles.id", ondelete="CASCADE"), index=True
    )
    muhaffiz_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("muhaffiz_profiles.id", ondelete="SET NULL")
    )
    juz: Mapped[int] = mapped_column(Integer)
    # "full", "first_half" or "second_half" — see domain/revision.Portion.
    portion: Mapped[str] = mapped_column(String(16), default="full")
    outcome: Mapped[str] = mapped_column(String(16), default=PENDING)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    local_date: Mapped[date] = mapped_column(Date, index=True)
    graded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    @property
    def is_graded(self) -> bool:
        return self.outcome != MurajaatClass.PENDING

    @property
    def went_well(self) -> bool:
        return self.outcome == MurajaatClass.GOOD


class MurajaatPlan(Base):
    """The student's own murajaat schedule: what to revise on which day.

    Owned by the student, not the Muhaffiz — they are the one who knows which
    juz has gone soft and how much this week actually holds. The Muhaffiz sees
    it read-only so they know what is coming to class.

    A juz can be scheduled whole or in halves. The guide notes the second half
    of a juz is almost always the weaker one, memorized while tired and revised
    last, so being able to schedule that half on its own is exactly the fix it
    recommends — and it turns a juz someone is avoiding into something that
    fits in a real evening.
    """

    __tablename__ = "murajaat_plans"
    __table_args__ = (
        UniqueConstraint(
            "student_id", "weekday", "juz", "portion", name="uq_murajaat_plan_slot"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(
        ForeignKey("student_profiles.id", ondelete="CASCADE"), index=True
    )
    juz: Mapped[int] = mapped_column(Integer)
    # "full", "first_half" or "second_half" — see domain/revision.Portion.
    portion: Mapped[str] = mapped_column(String(16), default="full")
    weekday: Mapped[int] = mapped_column(Integer)  # 0 = Monday, matches date.weekday()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WeakSpot(Base):
    """A noted trouble spot.

    Straight from the guide: "If you feel some parts are weak, make a note of it
    and keep repeating it until it is perfect", and the recommendation to keep a
    pocket notepad of mistakes per siparah.
    """

    __tablename__ = "weak_spots"
    __table_args__ = (Index("ix_weakspot_student_open", "student_id", "resolved_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(
        ForeignKey("student_profiles.id", ondelete="CASCADE"), index=True
    )
    juz: Mapped[int] = mapped_column(Integer)
    page: Mapped[Optional[int]] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(Text)
    created_by_muhaffiz_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("muhaffiz_profiles.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    @property
    def is_open(self) -> bool:
        return self.resolved_at is None


class PasswordResetToken(Base):
    """A one-time link a Muhaffiz hands to a student who is locked out.

    There is no reset email in this deployment, so recovery is a person-to-person
    act: the Muhaffiz generates a link and passes it over in person or on
    WhatsApp. That is a reasonable trust model for a programme where the two
    already know each other, and it removes the whole mail-deliverability
    problem from the critical path of getting back into an account.

    Only the *hash* of the token is stored. A leaked database should not hand
    an attacker working reset links, and nothing in the app ever needs to read
    the original value back — it is shown once at generation and then gone.
    """

    __tablename__ = "password_reset_tokens"
    __table_args__ = (Index("ix_reset_user", "user_id", "used_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    issued_by_muhaffiz_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("muhaffiz_profiles.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Notification(Base):
    """In-app notification, optionally mirrored to email.

    `dedupe_key` is what stops the 30-day warnings becoming a daily drumbeat:
    one row per (user, kind, subject, threshold, local date). The unique index
    makes double-sending a database error rather than a judgement call.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("user_id", "dedupe_key", name="uq_notification_dedupe"),
        Index("ix_notification_user_unread", "user_id", "read_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    url: Mapped[Optional[str]] = mapped_column(String(255))
    severity: Mapped[str] = mapped_column(String(16), default="info")
    dedupe_key: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    emailed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    @property
    def is_read(self) -> bool:
        return self.read_at is not None
