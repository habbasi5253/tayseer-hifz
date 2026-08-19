# Tayseer — Hifz Tracker

A hifz tracker that encodes the **Barnamaj Tayseer** methodology, built for adults
doing this alongside a job and a family — 20–60 minutes a day, not a full-time talib's
schedule.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m scripts.seed
.venv/bin/uvicorn app.main:app --reload --port 8077
```

Then open http://localhost:8077 and sign in with a seeded account:

| Role | Email | Password |
|---|---|---|
| Student | `student@example.com` | `tayseer2026` |
| Stage 1 Muhaffiz | `muhaffiz1@example.com` | `tayseer2026` |
| Stage 2 Muhaffiz | `muhaffiz2@example.com` | `tayseer2026` |

```bash
.venv/bin/python -m pytest
```

## Mushaf geometry

The guide prescribes the **Misri (Othman Taha) mushaf** specifically because "each
siparah has a fixed number of 20 pages, each page ends on an ayat". So this app models
**30 juz × 20 pages = 600 pages**, not the 604-page Madani pagination.

That is load-bearing, not incidental: it is what makes the marhala tasmee counts land on
clean fractions of a juz (5 pages = ¼ juz, 10 pages = ½ juz), and it is why page ↔ juz
math is arithmetic rather than a lookup table. Changing it means revisiting
`app/domain/marhala.py` too.

## Juz certification is the unit

The app is organised around **one juz = one certification**, not around loose pages.
Every page-level fact exists to answer a juz-level question. Each juz walks a pipeline:

    Tilawat  →  Memorization  →  Evaluation  →  Certified

The stage is **derived** in `domain/certification.classify`, never stored, so a status can
never disagree with the page records underneath it — the classic bug where a column says
"certified" while a page sits un-evaluated.

Certification is **held, not banked.** The 30-day rule keeps applying after it is earned:
pages falling outside their window take a certification to *at risk* and then *needs
renewal*, and re-reciting them restores it. A certification that could never lapse would
make the 30-day rule decorative.

Losing currency is not the same as losing the evaluation. A lapsed juz still shows 20 of
20 pages passed and 100% pipeline progress — it is simply not current. Dropping a
student's visible progress off a cliff for being a week late is exactly the punitive
behaviour this app avoids, and `tests/test_certification.py` pins it.

## The 30-day rule

The highest-risk piece, and the one built first. Every page in a juz must have been
tasmee'd *and passed* within a rolling 30 days; if any page falls outside, the juz's
tasmee is no longer current.

Four decisions keep it from drifting:

**Computed on read, never cached.** There is no `days_since_last_tasmee` column anywhere.
A cached countdown is wrong the moment the clock advances without a write, and the failure
is silent — a dormant account would show "12 days left" forever. Recomputing is one dict
lookup per page; the most expensive question anyone can ask (all 30 juz) is 600 integer
subtractions.

**Calendar days in the student's zone, never elapsed seconds.** A tasmee at 11pm Monday
read at 8am Tuesday is 1 day, not 0. Counting 86400-second blocks would hand every page a
free day. It also makes DST free — a 23- or 25-hour day is still one day.
`tests/test_revalidation.py` pins both directions, and asserts that the naive
implementation would disagree.

**The student's timezone, never the viewer's.** A Muhaffiz in Karachi looking at a student
in Chicago sees the deadline the student actually lives under. Every function takes the
zone explicitly, so there is no ambient "local time" to inherit by accident.

**Only passing tasmee resets the clock.** A failure is recorded, is visible, and carries
the feedback — but it does not extend the window. Otherwise failing a page would buy 30
more days, inverting the rule.

Scope matters too: a page is judged only once it has entered Stage 2. A page not yet
memorized is not "overdue", it is not in the game — otherwise the dashboard opens on day
one as a wall of 600 failures.

## Program logic encoded

**Stage 1** — Tilawat is a repeatable gate: `mark_page_memorized` refuses until the juz's
tilawat is approved, and each rejection increments `attempt` rather than overwriting
history. Talaqqi covers *tomorrow's* portion. Daily revision logs one of five ranked
methods. Tasmee is a marhala-dependent page count (5/5/5/7/7/10/10/10) and is explicitly
**not** a formal ikhtibar, so pages are recorded as heard rather than graded.

**The Muhaffiz picks the pages, not the app.** `services.stage1_page_view` returns days
since last tasmee, last dates, memorization state and open notes per page — and no
"recommended pages" field. That judgment stays with the teacher by design.

**Stage 2** — A different, mandatory second Muhaffiz, enforced in `assign_muhaffiz`
(which spans rows, so it lives in code rather than a DB constraint). 1–5 pages a day
across any number of sittings. Hifz, makharij and tajweed are evaluated independently and
**any one failing means the page is not revalidated**; the failure notes become a tracked
weak spot.

**Two kinds of murajaat**, as the guide splits them: *juz al hali* (the juz in progress)
and a rotating older juz. The rotation view ranks completed juz by neglect, because the
guide's central warning is that students revise juz 30, 1 and 2 forever while avoiding 4,
5 and 6 — "this fear will only make the weak siparahs even weaker."

## Design notes

**There is no daily page target.** A student memorizes one page or several, at whatever
rate they and their Muhaffiz settle on, so the plan reserves *time* for new hifz rather
than prescribing an amount. The day is built commitments-first — the murajaat the Muhaffiz
scheduled, then tasmee if a juz is under its clock — and hifz takes what is left. When the
commitments alone exceed the budget the app says so and offers the two real fixes: shorter
sittings, or asking the Muhaffiz to thin the schedule. Never "try harder".

**The student owns the murajaat schedule.** `MurajaatPlan` holds juz-per-weekday and drives
the daily plan; the Muhaffiz sees it read-only so they know what is coming to class. The
student is the one who knows which juz has gone soft and what this week actually holds.

**A juz can be revised whole or in halves.** The guide notes the second half of a juz is
almost always the weaker one — memorized while tired, revised last — so scheduling that
half on its own is exactly the fix it recommends. It also turns a juz someone is avoiding
into something that fits in a real evening: half the pages, half the time, and the
overloaded-day message points at it as the fix.

**Password recovery is person-to-person.** There is no reset email: a Muhaffiz generates a
one-time link and hands it over. Only the token's SHA-256 is stored (the value is 256 bits
from a CSPRNG, so password-style stretching buys nothing), it is single-use, expires in 72
hours, and issuing a new one burns any outstanding link. A Muhaffiz locked out of their own
account is recovered with `python -m scripts.reset_password <email>` on the server — they
have nobody above them to ask.

**Streaks are forgiving but not meaningless.** Days off are not misses. Misses are
absorbed at a genuinely rolling rate of one per 7 active days — an earlier cumulative
design let a long run bank enough grace to swallow a whole missed week. The largest number
on screen is 30-day consistency, not the streak, because consistency recovers and a streak
only ever dies. Nothing in `streak.py` renders as a loss; the worst case is "today is a
good day to start a new run."

**The day is scheduled, not just totalled.** `domain/dayplan.py` places each block at a
clock time rather than reporting "35 minutes today" — a total you can agree with and still
never act on. Placement follows the guide's own time-management section: new hifz goes in
the freshest post-fajr slot because memorizing while tired does not register, murajaat
goes to the evening wind-down, and the optional rotation juz takes the midday pocket since
it is the one block that survives being done as listening on a commute.

**Hifz first, logging second.** The Today screen leads with the actual work of the
programme — the page to memorize, and talaqqi for tomorrow's — and only then asks what you
did. Renewals appear above it as a single line, never as a wall, because a time-critical
alert should be visible without displacing the thing the app is for.

**Students log attendance, Muhaffiz log outcomes.** `ClassLog` records that a student sat
with their Muhaffiz; it never passes a page. Only a Muhaffiz recording a `TasmeeSession`
resets a 30-day window. If a student could self-report a pass, the 30-day rule would
become an honour system and the second-Muhaffiz requirement would be decorative.

**Notifications warn at thresholds, not daily.** 7, 3, 1 and 0 days out, deduped by a
unique `(user_id, dedupe_key)` index so a double-run is a database error rather than a
judgement call. The daily reminder is skipped entirely if the student already logged.

## Layout

```
app/domain/      pure functions over plain data — no DB, no framework
  certification.py the juz certification pipeline — the app's spine
  quran.py         mushaf geometry
  dates.py         timezone-aware date math (the anti-drift boundary)
  revalidation.py  the 30-day rule
  marhala.py       marhala table and tasmee targets
  revision.py      the 5 methods, mix nudge, rotation health
  schedule.py      time-budget planner and pace projection
  streak.py        forgiving streak
  tracking.py      pace, class attendance, per-juz progress — "am I on track?"
  dayplan.py       suggested times for the day, and the habit strip
app/models.py    SQLAlchemy schema
app/services.py  every query and command; the only code that touches the DB
app/routes/      auth, student, muhaffiz
app/report.py    PDF progress report (reportlab)
tests/           101 tests, concentrated on the date math and certification
```

## Data model

`User` carries identity, timezone and reminder preferences, with optional
`StudentProfile` / `MuhaffizProfile` — one person can be both. `Assignment` holds the
student ↔ Muhaffiz relationship with a `stage` and an optional `juz` scope, so a Stage 1
relationship can end when the student moves to Stage 2 without deleting history.

`PageProgress` is the pivot: `memorized_at` puts a page in Stage 1, `stage2_entered_at`
puts it in the 30-day rule's universe. `TasmeeSession` → `TasmeePageResult` records
sittings; `passed` is stored (not derived) because it is what the revalidation query
filters on, written in exactly one place and immutable after. `PageRevalidationStatus` is
derived on read and never persisted.

Log rows freeze the student's `local_date` at write time rather than deriving it later, so
moving from Chicago to Dubai does not retroactively shift yesterday's revision to a
different day.

## Deploying to Vercel

Vercel detects `app/main.py` natively — a top-level `app` in `app/main.py` is a supported
Python entrypoint, so there is no adapter or `api/` shim. `vercel.json` only sets
`maxDuration`, trims the bundle, and registers the cron.

Two things genuinely change on serverless, and both are handled in code rather than left
as instructions:

- **SQLite cannot come with you.** The filesystem is ephemeral, so the database must be
  managed Postgres. `config._normalize_db_url` rewrites the `postgres://` scheme that
  Neon, Supabase and Vercel Postgres all hand out into the `postgresql+psycopg://` form
  SQLAlchemy needs — otherwise you get a driver error on first boot.
- **Connection pooling inverts.** Each invocation is its own short-lived process, so a
  pool is never reused but still consumes Postgres connections. `db.py` switches to
  `NullPool` when `VERCEL` is set. Use your provider's *pooled* connection string (Neon's
  `-pooler` host, Supabase port 6543); a direct connection will exhaust `max_connections`
  under load.

`tzdata` is in `requirements.txt` on purpose. `zoneinfo` reads the system IANA database,
which slim Linux images do not always carry, and the 30-day rule is entirely built on
named timezones. Do not drop it.

### Steps

1. **Create a Postgres database** — Vercel Postgres, [Neon](https://neon.tech) or
   [Supabase](https://supabase.com). Copy the **pooled** connection string.

2. **Run the migrations.** Deliberately not done on startup: on serverless that hook
   fires on every cold start, and concurrent starts racing through the same migration is
   a good way to corrupt a schema. Run it as a deliberate step, and again after **every**
   schema change — not just the first deploy:

   ```bash
   DATABASE_URL='postgresql://...' .venv/bin/python -m scripts.migrate
   ```

3. **Set environment variables** in Project Settings → Environment Variables:

   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | your pooled Postgres string |
   | `SECRET_KEY` | a long random string — rotating it signs everyone out |
   | `SECURE_COOKIES` | `1` |
   | `DEBUG` | `0` |
   | `CRON_SECRET` | a random 16+ character string |
   | `CRON_MODE` | `daily` on Hobby, `hourly` on Pro (see below) |
   | `SMTP_HOST` etc. | optional; unset means notifications stay in-app only |

4. **Deploy.**

   ```bash
   npx vercel deploy --prod
   ```

### The cron caveat

`vercel.json` ships `0 13 * * *` (once a day) with `CRON_MODE=daily`, because an hourly
expression **fails deployment outright on the Hobby plan** and a failed first deploy is a
miserable way to start. Everyone is swept on that single run, so reminders arrive at a
fixed UTC hour rather than each student's chosen local hour.

On Pro, change the schedule to `0 * * * *` and set `CRON_MODE=hourly`. That is what the
notification design actually wants: each student picks a local reminder hour, and an
hourly sweep reaches every timezone from one job.

That is safe to run either way: every notification carries a date-stamped `dedupe_key`
behind a unique index, so duplicate or missed runs cannot double-send — which is exactly
the idempotency Vercel's own cron guidance asks for, since delivery is best-effort.

`/api/cron/reminders` fails closed. With no `CRON_SECRET` set it returns 503 rather than
running as an open trigger, and a wrong bearer token gets 401.

### Is Vercel the right host for this?

It works, and the setup above is complete. Worth knowing the trade: this is a stateful,
session-based, server-rendered app with a background job — the shape serverless is least
suited to. You take on managed Postgres, Python cold starts on the first request after
idle, and the cron restriction above. A container host (Fly.io, Render, Railway) runs the
included `Dockerfile` as-is with a persistent process and unrestricted cron. If the rest
of your stack is already on Vercel, none of that is disqualifying.

## Deploying anywhere else

Portable by design: SQLite locally, Postgres via one `DATABASE_URL` change, plus a
Dockerfile. Copy `.env.example` to `.env` and set `SECRET_KEY` and `SECURE_COOKIES=1`
before going live.

Reminders run from cron — hourly, so one job covers every timezone:

```
0 * * * * cd /app && python -m scripts.send_reminders
```

## Schema changes

Migrations are Alembic, in `migrations/versions/`. After editing `app/models.py`:

```bash
.venv/bin/alembic revision --autogenerate -m "what changed"
```

Read the generated file before committing — autogenerate is a good first draft, not an
oracle; it does not always infer a rename, and it will happily suggest dropping a column
you meant to rename. Then apply it:

```bash
.venv/bin/alembic upgrade head    # or: python -m scripts.migrate
```

Locally the app runs pending migrations itself on startup, so development stays a single
command. `alembic downgrade -1` reverses the last one.

`env.py` sets `render_as_batch=True` because SQLite cannot ALTER or DROP a column in
place — batch mode rebuilds the table and copies the rows, which is what lets one
migration run on SQLite locally and Postgres in production.

**Never `Base.metadata.create_all()`.** It creates only tables that do not exist; a new
column on an existing table is silently skipped and reports success, then fails at query
time. That is the bug this replaced.

## Known gaps

- No password reset flow.
- `marhala` is stored but drives nothing and is hidden from onboarding, pending a
  definition of what it should mean.
- Juz run 1→30 by default; a custom order lives behind an Advanced disclosure in Settings.
- Email is SMTP-only. `notifications.Channel` is an interface — web push or SMS drops in
  without touching the logic that decides what to send.
