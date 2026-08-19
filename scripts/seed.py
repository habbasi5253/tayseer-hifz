"""Seed a demo dataset.

Builds a student who looks like the real target user: mid-programme, a couple of
juz done, drifting toward passive revision, and with a handful of pages about to
fall out of their 30-day window — so the dashboard has something honest to show.

    python -m scripts.seed
"""

from __future__ import annotations

import random
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402

# Checked before importing app.db, which constructs the engine at import time —
# a Postgres URL would otherwise fail on the missing driver with a traceback
# instead of this explanation.
if not settings.is_sqlite:
    raise SystemExit(
        "scripts.seed creates demo accounts with a password committed to this repo "
        "in plain sight, so it refuses to run against anything but local SQLite.\n"
        "For a real deployment: run `python -m scripts.migrate`, then register the "
        "first account through the app."
    )

from app.db import SessionLocal, run_migrations  # noqa: E402
from app.domain import dates as dt  # noqa: E402
from app.domain.quran import global_page, juz_pages  # noqa: E402
from app import services as S  # noqa: E402
from app.models import (  # noqa: E402
    RevisionKind,
    Stage,
    StudentProfile,
    User,
)
from app.security import hash_password  # noqa: E402

PASSWORD = "tayseer2026"


def main() -> None:
    run_migrations()
    db = SessionLocal()
    random.seed(7)

    if db.query(User).filter(User.email == "student@example.com").first():
        print("Seed data already present. Delete tayseer.db to reseed.")
        return

    now = dt.utcnow()

    student_user = User(
        email="student@example.com",
        name="Hasan Abbasi",
        password_hash=hash_password(PASSWORD),
        timezone="America/Chicago",
        is_student=True,
        reminder_hour=20,
    )
    db.add(student_user)
    db.flush()

    student = StudentProfile(
        user_id=student_user.id,
        marhala=4,
        current_juz=3,
        daily_minutes=35,
        active_days="1111011",  # Fridays off
        preferred_method=2,
        started_at=now - timedelta(days=150),
    )
    db.add(student)
    db.flush()
    db.commit()

    def sign_off_juz(juz, first_day, _unused=None):
        """Stage 1 for a whole juz: batches of two, recited and signed off."""
        for b in range(0, 20, 2):
            when = now - timedelta(days=first_day - b)
            for i in (b + 1, b + 2):
                S.mark_page_memorized(db, student, global_page(juz, i), when=when)
            S.submit_batch(db, student, juz, when=when)
            S.sign_off_pages(
                db, student, muhaffiz_id=None,
                passed_pages=[global_page(juz, b + 1), global_page(juz, b + 2)], when=when,
            )

    def tasmee_juz(juz, pages, first_day):
        """Recite `pages` of a juz to Muhaffiz 2, one page a day."""
        for i, idx in enumerate(pages):
            S.record_tasmee(
                db, student, stage=Stage.TWO, juz=juz, muhaffiz_id=None,
                page_results=[{"page": global_page(juz, idx), "hifz": True,
                               "makharij": True, "tajweed": True}],
                when=now - timedelta(days=first_day - i),
            )

    # The programme runs strictly in sequence: a juz is signed off by Muhaffiz 1,
    # tasmee'd to Muhaffiz 2 inside 30 days, passed — and only then does the next
    # juz open. The seed follows that order because the gates now enforce it.

    # --- Juz 1: clean run ----------------------------------------------------
    sign_off_juz(1, 150)
    tasmee_juz(1, range(1, 21), 128)
    S.pass_juz_tasmee(db, student, 1, muhaffiz_id=None, when=now - timedelta(days=106))
    db.commit()

    # --- Juz 2: the rule bites -----------------------------------------------
    # Thirteen pages in, life happened, and the 30 days ran out. Picking it back
    # up restarts the attempt from page 1 — `record_tasmee` does that on the next
    # sitting, and the abandoned attempt stays in the record for both Muhaffiz.
    sign_off_juz(2, 100)
    tasmee_juz(2, range(1, 14), 76)
    tasmee_juz(2, range(1, 21), 25)
    S.pass_juz_tasmee(db, student, 2, muhaffiz_id=None, when=now - timedelta(days=4))
    db.commit()

    # --- Juz 3: in Stage 1, one batch sitting with Muhaffiz 1 ----------------
    for b in range(0, 10, 2):
        when = now - timedelta(days=20 - b)
        for i in (b + 1, b + 2):
            S.mark_page_memorized(db, student, global_page(3, i), when=when)
        S.submit_batch(db, student, 3, when=when)
        S.sign_off_pages(db, student, muhaffiz_id=None,
                         passed_pages=[global_page(3, b + 1), global_page(3, b + 2)], when=when)
    for i in (11, 12):
        S.mark_page_memorized(db, student, global_page(3, i), when=now - timedelta(days=1))
    S.submit_batch(db, student, 3, when=now - timedelta(hours=14))
    student.current_juz = 3
    db.commit()

    # --- Murajaat classes, graded by Muhaffiz 1 -----------------------------
    # The student owns this. Juz 2 is the harder one here, so it is taken in
    # halves rather than whole — which is the point of allowing portions.
    S.set_murajaat_plan(db, student.id, slots=[
        (0, 1, "full"), (1, 2, "first_half"), (2, 2, "second_half"),
        (3, 1, "full"), (5, 2, "first_half"), (6, 2, "second_half"),
    ])
    for d, juz, outcome in ((3, 1, "good"), (6, 2, "needs_work"), (10, 1, "good")):
        S.log_murajaat_class(
            db, student, juz=juz, portion="full", muhaffiz_id=None, outcome=outcome,
            notes="Order of ayats slipped in the second half." if outcome == "needs_work" else None,
            when=now - timedelta(days=d),
        )
    S.log_murajaat_class(db, student, juz=2, muhaffiz_id=None, when=now)  # awaiting a grade
    db.commit()

    # --- Revision logs: recently drifting toward passive methods -------------
    for d in range(0, 60):
        day = now - timedelta(days=d)
        if not dt.is_active_day(student.active_days, dt.local_date(day, student_user.timezone)):
            continue
        if d in (3, 11, 19, 28, 41):  # a few genuine misses
            continue
        method = random.choice([4, 4, 5, 3]) if d < 9 else random.choice([1, 2, 2, 3, 4])
        S.log_revision(
            db, student, juz=3, method=method,
            duration_minutes=random.choice([15, 20, 25, 30]),
            kind=RevisionKind.HALI, when=day,
        )
        if d % 3 == 0:
            S.log_revision(
                db, student, juz=random.choice([1, 2]), method=random.choice([2, 3, 4]),
                duration_minutes=random.choice([20, 30]),
                kind=RevisionKind.ROTATION, when=day,
            )
    db.commit()

    board = S.build_board(db, student)
    print("Seeded.\n")
    print(f"  student@example.com / {PASSWORD}\n")
    print(f"  Juz 3 Stage 1: {S.juz_workface(db, student, 3).headline}")
    print(f"  Tasmee board:  {board.headline}")
    for w in board.windows:
        print(f"    juz {w.juz}: {w.label} · {w.passed_count}/20 · attempt {w.attempt}")
    db.close()


if __name__ == "__main__":
    main()
