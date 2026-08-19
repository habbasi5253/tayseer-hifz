"""Shareable PDF progress report.

Built to be handed to a Muhaffiz or programme coordinator, so it reads as a
report rather than a table dump: a summary line in plain language, then the
numbers behind it. Everything on the page is a fact the app actually holds — no
placeholder sections, and nothing is rounded in a flattering direction.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta
from typing import List, Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import services
from app.domain import dates as dt
from app.domain import revalidation as rv
from app.domain.marhala import get_marhala
from app.domain.quran import TOTAL_PAGES
from app.domain.revision import METHODS
from app.models import Stage, StudentProfile, TasmeePageResult, TasmeeSession

# Matches the web palette so a printed report and the screen feel like one product.
GREEN = colors.HexColor("#1f5c46")
GREEN_SOFT = colors.HexColor("#e8f1ec")
AMBER = colors.HexColor("#b4762a")
AMBER_SOFT = colors.HexColor("#fbf0dd")
CLAY = colors.HexColor("#a4432f")
CLAY_SOFT = colors.HexColor("#fbe9e4")
INK = colors.HexColor("#1e2a24")
INK_SOFT = colors.HexColor("#5b6b62")
INK_FAINT = colors.HexColor("#8b998f")
LINE = colors.HexColor("#e6e0d6")
PAPER = colors.HexColor("#fbf8f3")

TONE_FILL = {
    "good": GREEN_SOFT,
    "soon": AMBER_SOFT,
    "attention": CLAY_SOFT,
    "neutral": colors.HexColor("#f0ede7"),
}
TONE_TEXT = {"good": GREEN, "soon": AMBER, "attention": CLAY, "neutral": INK_FAINT}


def _hex(color) -> str:
    """reportlab paragraph markup needs '#rrggbb'; Color.hexval() yields '0xrrggbb'."""
    return "#" + color.hexval()[2:]


def _styles():
    ss = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=ss["Title"], fontName="Helvetica-Bold",
                                fontSize=20, leading=24, textColor=INK, alignment=TA_LEFT,
                                spaceAfter=2),
        "sub": ParagraphStyle("s", parent=ss["Normal"], fontName="Helvetica",
                              fontSize=9.5, leading=13, textColor=INK_SOFT),
        "h": ParagraphStyle("h", parent=ss["Normal"], fontName="Helvetica-Bold",
                            fontSize=8, leading=11, textColor=INK_FAINT,
                            spaceBefore=14, spaceAfter=5),
        "body": ParagraphStyle("b", parent=ss["Normal"], fontName="Helvetica",
                               fontSize=9.5, leading=13.5, textColor=INK),
        "lead": ParagraphStyle("l", parent=ss["Normal"], fontName="Helvetica",
                               fontSize=11, leading=16, textColor=INK, spaceAfter=4),
        "cell": ParagraphStyle("c", parent=ss["Normal"], fontName="Helvetica",
                               fontSize=8.5, leading=11.5, textColor=INK),
        "small": ParagraphStyle("sm", parent=ss["Normal"], fontName="Helvetica",
                                fontSize=8, leading=10.5, textColor=INK_FAINT),
    }


class Rule(Flowable):
    def __init__(self, width, color=LINE, thickness=0.6):
        super().__init__()
        self.width, self.color, self.thickness = width, color, thickness
        self.height = 0

    def draw(self):
        self.canv.setStrokeColor(self.color)
        self.canv.setLineWidth(self.thickness)
        self.canv.line(0, 0, self.width, 0)


class JuzStrip(Flowable):
    """A 30-cell map of the whole Quran, one cell per juz.

    Far more legible at a glance than a table, and it is the first thing a
    teacher looks for: how far along is this student, and where is the trouble.
    """

    def __init__(self, rows: List[dict], width: float):
        super().__init__()
        self.rows = rows
        self.width = width
        self.cell = width / 15.0
        self.height = self.cell * 2 + 14

    def draw(self):
        c = self.canv
        gap = 1.6
        for i, row in enumerate(self.rows):
            col, line = i % 15, i // 15
            x = col * self.cell
            y = self.height - 14 - (line + 1) * self.cell + gap

            fill, text = colors.HexColor("#f3f0ea"), INK_FAINT
            if row.is_certified:
                # Lapsed certifications stay certified — they are shown as
                # needing renewal, not as lost work.
                fill, text = (GREEN, colors.white) if row.health != "lapsed" else (CLAY_SOFT, CLAY)
            elif row.stage == "evaluation":
                fill, text = GREEN_SOFT, GREEN
            elif row.stage == "memorizing":
                fill, text = AMBER_SOFT, AMBER
            if row.health in ("lapsed", "at_risk") and not row.is_certified:
                fill, text = CLAY_SOFT, CLAY

            c.setFillColor(fill)
            c.setStrokeColor(LINE)
            c.setLineWidth(0.5)
            c.roundRect(x, y, self.cell - gap, self.cell - gap, 2.2, fill=1, stroke=1)
            c.setFillColor(text)
            c.setFont("Helvetica-Bold", 6.6)
            c.drawCentredString(x + (self.cell - gap) / 2, y + (self.cell - gap) / 2 - 2.2,
                                str(row.juz))

        legend = [("Certified", GREEN), ("Evaluation", GREEN_SOFT), ("Memorizing", AMBER_SOFT),
                  ("Needs renewal", CLAY_SOFT), ("Not started", colors.HexColor("#f3f0ea"))]
        x = 0
        c.setFont("Helvetica", 6.6)
        for label, col in legend:
            c.setFillColor(col)
            c.setStrokeColor(LINE)
            c.setLineWidth(0.4)
            c.roundRect(x, 2, 7, 7, 1.6, fill=1, stroke=1)
            c.setFillColor(INK_FAINT)
            c.drawString(x + 9.5, 4.2, label)
            x += 12 + c.stringWidth(label, "Helvetica", 6.6) + 12


class MixBars(Flowable):
    """Horizontal bars for the five revision methods."""

    def __init__(self, mix, width: float):
        super().__init__()
        self.mix = mix
        self.width = width
        self.rowh = 15
        self.height = self.rowh * len(METHODS) + 4

    def draw(self):
        c = self.canv
        label_w, pct_w = 150, 34
        bar_w = self.width - label_w - pct_w
        for i, m in enumerate(METHODS):
            y = self.height - (i + 1) * self.rowh
            share = self.mix.share(m.rank)
            count = self.mix.count(m.rank)

            c.setFillColor(GREEN if m.is_strong else (AMBER if m.rank == 3 else INK_FAINT))
            c.setFont("Helvetica-Bold", 7)
            c.drawString(0, y + 3.5, str(m.rank))
            c.setFillColor(INK)
            c.setFont("Helvetica", 8)
            c.drawString(10, y + 3.5, m.short)

            c.setFillColor(colors.HexColor("#eeeae3"))
            c.roundRect(label_w, y + 1, bar_w, 8, 2, fill=1, stroke=0)
            if share > 0:
                c.setFillColor(GREEN if m.is_strong else (AMBER if m.rank == 3 else INK_FAINT))
                c.roundRect(label_w, y + 1, max(3.0, bar_w * share), 8, 2, fill=1, stroke=0)

            c.setFillColor(INK_SOFT)
            c.setFont("Helvetica", 7.5)
            c.drawRightString(self.width, y + 3.5, f"{round(share * 100)}%  ({count})")


def _stat_row(items, width, st):
    """A row of headline numbers."""
    data = [
        [Paragraph(f'<font size="15"><b>{v}</b></font>', st["body"]) for _, v, _ in items],
        [Paragraph(f'<font color="#8b998f" size="7.5">{k.upper()}</font>', st["small"])
         for k, _, _ in items],
        [Paragraph(f'<font color="#5b6b62" size="7.5">{s}</font>', st["small"])
         for _, _, s in items],
    ]
    t = Table(data, colWidths=[width / len(items)] * len(items))
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 1),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 0),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    return t


def build_report(db: Session, student: StudentProfile, now: Optional[datetime] = None) -> bytes:
    now = now or dt.utcnow()
    user = student.user
    tz = user.timezone if user else "UTC"
    today = dt.today_local(tz, now)
    st = _styles()

    summary = services.progress_summary(db, student, now=now)
    board: rv.RevalidationBoard = summary["board"]
    mix = services.method_mix_for(db, student, window_days=30, now=now)
    streak = summary["streak"]
    marhala = get_marhala(student.marhala)
    certs = services.certification_board(db, student, now=now)
    track = services.tracking_summary(db, student, now=now)

    stage1_m = services.muhaffiz_for(db, student.id, Stage.ONE)
    stage2_m = services.muhaffiz_for(db, student.id, Stage.TWO)

    buf = io.BytesIO()
    page_w, page_h = A4
    margin = 17 * mm
    content_w = page_w - 2 * margin

    doc = BaseDocTemplate(
        buf, pagesize=A4,
        leftMargin=margin, rightMargin=margin, topMargin=margin, bottomMargin=18 * mm,
        title=f"Hifz progress — {user.name if user else 'Student'}",
        author="Tayseer",
    )

    def decorate(canvas, _doc):
        canvas.saveState()
        canvas.setFillColor(INK_FAINT)
        canvas.setFont("Helvetica", 7.5)
        canvas.drawString(margin, 11 * mm, f"Tayseer · Barnamaj Tayseer progress report")
        canvas.drawRightString(page_w - margin, 11 * mm, f"Page {canvas.getPageNumber()}")
        canvas.setStrokeColor(LINE)
        canvas.setLineWidth(0.6)
        canvas.line(margin, 14 * mm, page_w - margin, 14 * mm)
        canvas.restoreState()

    doc.addPageTemplates([
        PageTemplate(
            id="main",
            frames=[Frame(margin, 18 * mm, content_w, page_h - margin - 18 * mm, id="f")],
            onPage=decorate,
        )
    ])

    flow: List = []

    # --- Header --------------------------------------------------------------
    flow.append(Paragraph(user.name if user else "Student", st["title"]))
    flow.append(Paragraph(
        f"Barnamaj Tayseer · {marhala.label} · "
        f"Report generated {today.strftime('%d %B %Y')} ({tz})",
        st["sub"],
    ))
    flow.append(Spacer(1, 5))
    flow.append(Rule(content_w, GREEN, 1.6))
    flow.append(Spacer(1, 12))

    # --- Where they are ------------------------------------------------------
    pages_mem = summary["pages_memorized"]
    pages_rev = summary["pages_revalidated"]
    flow.append(_stat_row([
        ("Passed", f"{len(certs.certified)}/30", "juz passed by Muhaffiz 2"),
        ("In progress", str(len(certs.in_progress)),
         f"juz {', '.join(str(c.juz) for c in certs.in_progress[:3]) or '—'}"),
        ("Memorized", f"{pages_mem}", f"of {TOTAL_PAGES} pages ({summary['percent_memorized']}%)"),
        ("Marhala", marhala.label.replace("Marhala ", ""), f"{marhala.tasmee_pages} pages tasmee"),
    ], content_w, st))
    flow.append(Spacer(1, 12))

    # The Stage-2-lags-Stage-1 gap, said out loud rather than left to inference.
    gap = max(0, pages_mem - pages_rev)
    flow.append(Paragraph(
        (f"<b>{certs.headline}.</b> {pages_mem} pages memorized, of which {pages_rev} have passed "
         f"Stage 2 evaluation — evaluation trails memorization by {gap} page"
         f"{'s' if gap != 1 else ''}, which is how the programme is meant to run. "
         "A juz is certified once all 20 of its pages have passed hifz, makharij and tajweed, "
         "and its full tasmee is passed by the second Muhaffiz inside 30 days.")
        if pages_mem else
        "No pages memorized yet. This report will fill out as work is logged.",
        st["body"],
    ))

    flow.append(Paragraph("MUHAFFIZ", st["h"]))
    m_rows = [
        [Paragraph("<b>Stage 1</b> — Tilawat &amp; memorization", st["cell"]),
         Paragraph(stage1_m.name if stage1_m else "<i>Not assigned</i>", st["cell"])],
        [Paragraph("<b>Stage 2</b> — Hifz evaluation", st["cell"]),
         Paragraph(stage2_m.name if stage2_m else "<i>Not assigned</i>", st["cell"])],
    ]
    t = Table(m_rows, colWidths=[content_w * 0.5, content_w * 0.5])
    t.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    flow.append(t)

    # --- 30-day rule ---------------------------------------------------------
    flow.append(Paragraph("30-DAY TASMEE RULE", st["h"]))

    active = board.active
    tone = "good"
    if active is not None:
        tone = {"expired": "attention", "due_soon": "soon", "due_today": "soon"}.get(
            active.status, "good"
        )

    banner = Table(
        [[Paragraph(f'<font color="{_hex(TONE_TEXT[tone])}"><b>{board.headline}</b></font>',
                    st["body"])]],
        colWidths=[content_w],
    )
    banner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), TONE_FILL[tone]),
        ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("ROUNDEDCORNERS", [4, 4, 4, 4]),
    ]))
    flow.append(banner)
    flow.append(Spacer(1, 9))

    flow.append(Paragraph(
        "Each juz gets one 30-day window, opened when the Stage 1 Muhaffiz signs off all "
        "20 pages. The whole juz must be recited to the Stage 2 Muhaffiz inside it; if the "
        "window closes first, tasmee for that juz restarts from page 1.",
        st["small"]))
    flow.append(Spacer(1, 8))

    if board.windows:
        rows = [[Paragraph(f"<b>{h}</b>", st["small"]) for h in
                 ("Juz", "Window opened", "Deadline", "Recited", "Attempt", "Status")]]
        for w in board.windows:
            rows.append([
                Paragraph(f"Juz {w.juz}", st["cell"]),
                Paragraph(w.started_on.strftime("%d %b %Y") if w.started_on else "—", st["cell"]),
                Paragraph(w.deadline.strftime("%d %b %Y") if w.deadline else "—", st["cell"]),
                Paragraph(f"{w.passed_count}/20", st["cell"]),
                Paragraph(str(w.attempt), st["cell"]),
                Paragraph(
                    f'<font color="{_hex(TONE_TEXT[w.tone])}"><b>{w.label}</b></font>'
                    f' <font color="#8b998f">· {w.countdown_text}</font>', st["cell"]),
            ])
        t = Table(rows, colWidths=[content_w * x for x in (.10, .19, .19, .12, .12, .28)],
                  repeatRows=1)
        t.setStyle(TableStyle([
            ("LINEBELOW", (0, 0), (-1, 0), 0.7, LINE),
            ("LINEBELOW", (0, 1), (-1, -2), 0.4, LINE),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        flow.append(t)
        restarts = [w for w in board.windows if w.attempt > 1]
        if restarts:
            flow.append(Spacer(1, 5))
            flow.append(Paragraph(
                "Juz " + ", ".join(str(w.juz) for w in restarts) +
                " ran past a window and restarted. Earlier attempts remain in the record.",
                st["small"]))
    else:
        flow.append(Paragraph("No juz has entered Stage 2 tasmee yet.", st["body"]))

    # --- Juz map -------------------------------------------------------------
    flow.append(Paragraph("CERTIFICATIONS", st["h"]))
    flow.append(JuzStrip(certs.items, content_w))

    # --- Tasmee history ------------------------------------------------------
    # --- Per-juz progress ---------------------------------------------------
    flow.append(Paragraph("PROGRESS PER JUZ", st["h"]))
    if track["juz_rows"]:
        rows = [[Paragraph(f"<b>{h}</b>", st["small"]) for h in
                 ("Juz", "Signed off", "Recited", "Status", "Days")]]
        for r in track["juz_rows"]:
            rows.append([
                Paragraph(f"Juz {r.juz}", st["cell"]),
                Paragraph(f"{r.signed_off}/20 · {r.signed_off_percent}%", st["cell"]),
                Paragraph(f"{r.recited}/20 · {r.recited_percent}%", st["cell"]),
                Paragraph(r.status, st["cell"]),
                Paragraph(f"{r.days_taken}" if r.days_taken is not None else "—", st["cell"]),
            ])
        t = Table(rows, colWidths=[content_w * x for x in (.14, .22, .22, .28, .14)],
                  repeatRows=1)
        t.setStyle(TableStyle([
            ("LINEBELOW", (0, 0), (-1, 0), 0.7, LINE),
            ("LINEBELOW", (0, 1), (-1, -2), 0.4, LINE),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        flow.append(t)
        flow.append(Spacer(1, 4))
        flow.append(Paragraph(
            "Signed off = pages heard and passed by the Stage 1 Muhaffiz. "
            "Recited = pages taken to the Stage 2 Muhaffiz in the current tasmee attempt. "
            "Days = from starting the juz to passing it, or to today if still in progress.",
            st["small"]))
    else:
        flow.append(Paragraph("No juz started yet.", st["body"]))

    # --- Pace ---------------------------------------------------------------
    pace = track["pace"]
    flow.append(Paragraph("TIME PER PAGE", st["h"]))
    flow.append(Paragraph(pace.summary, st["lead"]))
    if pace.has_pace:
        flow.append(Paragraph(
            f"Measured from {pace.pages_counted} pages signed off over "
            f"{pace.span_days} days. Quickest gap between pages {pace.fastest_gap} days, "
            f"longest {pace.slowest_gap}. "
            + (
                f"At this rate juz {student.current_juz} finishes around "
                f"{track['projected_juz_finish'].strftime('%d %B %Y')} "
                f"({track['current_juz_signed_off']} of 20 pages signed off so far)."
                if track["projected_juz_finish"] else
                "The current juz is fully signed off."
            ),
            st["small"]))

    # --- Attendance ---------------------------------------------------------
    att = track["attendance"]
    flow.append(Paragraph("MURAJAAT CLASSES", st["h"]))
    flow.append(Paragraph(att.summary, st["lead"]))
    if att.expected:
        rows = [[Paragraph(f"<b>{h}</b>", st["small"]) for h in
                 ("Week of", "Scheduled", "Attended", "Missed", "")]]
        for w in att.weeks:
            tone = "good" if w.missed == 0 else ("soon" if w.missed <= 1 else "attention")
            rows.append([
                Paragraph(w.label, st["cell"]),
                Paragraph(str(w.expected), st["cell"]),
                Paragraph(str(w.attended), st["cell"]),
                Paragraph(str(w.missed), st["cell"]),
                Paragraph(f'<font color="{_hex(TONE_TEXT[tone])}"><b>{w.percent}%</b></font>',
                          st["cell"]),
            ])
        t = Table(rows, colWidths=[content_w * x for x in (.24, .19, .19, .19, .19)],
                  repeatRows=1)
        t.setStyle(TableStyle([
            ("LINEBELOW", (0, 0), (-1, 0), 0.7, LINE),
            ("LINEBELOW", (0, 1), (-1, -2), 0.4, LINE),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ]))
        flow.append(t)
        flow.append(Spacer(1, 4))
        flow.append(Paragraph(
            "Scheduled comes from the student's own murajaat plan, counting only days "
            "that have already passed. Murajaat is recited to the Stage 1 Muhaffiz for "
            "feedback, but missing a class does not gate progress through the programme.",
            st["small"]))

    # --- Method mix ---------------------------------------------------------
    flow.append(Paragraph("REVISION METHOD MIX (LAST 30 DAYS)", st["h"]))
    if mix.total:
        flow.append(MixBars(mix, content_w))
        flow.append(Spacer(1, 5))
        flow.append(Paragraph(
            f"{round(mix.strong_share * 100)}% of {mix.total} logged revisions used methods "
            "1–2 (tasmee to someone, or from memory). "
            + ("This is a healthy mix for retention."
               if mix.strong_share >= 0.3
               else "Shifting more sessions to methods 1–2 would strengthen retention."),
            st["small"]))
    else:
        flow.append(Paragraph("No revision sessions logged in this window.", st["body"]))

    doc.build(flow)
    return buf.getvalue()
