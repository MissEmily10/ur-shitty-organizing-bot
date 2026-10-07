"""Календарь расписания в PDF A4 (альбомная ориентация): неделя по часам и месяц клетками.
Цвета и шрифты — из design/tokens.json, чтобы заменить их дизайн-системой владелицы без правки кода."""

import calendar
import io
import json
from datetime import date, timedelta
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

from .schedule import Occurrence

ROOT = Path(__file__).resolve().parent.parent
TOKENS = json.loads((ROOT / "design" / "tokens.json").read_text(encoding="utf-8"))
COLORS = {name: HexColor(value) for name, value in TOKENS["colors"].items()}
pdfmetrics.registerFont(TTFont("Body", str(ROOT / TOKENS["fonts"]["regular"])))
pdfmetrics.registerFont(TTFont("Bold", str(ROOT / TOKENS["fonts"]["bold"])))

WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
MONTHS = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
PAGE_W, PAGE_H = landscape(A4)
MARGIN = 28


def _minutes(hhmm: str) -> int:
    hh, mm = map(int, hhmm.split(":"))
    return hh * 60 + mm


def _fit(c: Canvas, text: str, font: str, size: float, width: float) -> str:
    """Обрезает текст с «…», чтобы влез в ширину."""
    if c.stringWidth(text, font, size) <= width:
        return text
    while text and c.stringWidth(text + "…", font, size) > width:
        text = text[:-1]
    return text + "…"


def _box_colors(kind: str) -> tuple:
    return COLORS.get(kind, COLORS["regular"]), COLORS.get(f"{kind}_border", COLORS["regular_border"])


def _title(c: Canvas, text: str, subtitle: str) -> None:
    c.setFillColor(COLORS["text"])
    c.setFont("Bold", 20)
    c.drawString(MARGIN, PAGE_H - MARGIN - 14, text)
    c.setFont("Body", 10)
    c.setFillColor(COLORS["muted"])
    c.drawRightString(PAGE_W - MARGIN, PAGE_H - MARGIN - 12, subtitle)


def week_pdf(items: list[Occurrence], start: date, today: date | None = None) -> bytes:
    """Неделя с понедельника start: колонки дней, строки часов; события — цветные плашки."""
    buf = io.BytesIO()
    c = Canvas(buf, pagesize=(PAGE_W, PAGE_H))
    end = start + timedelta(days=6)
    _title(c, f"Неделя {start.strftime('%d.%m')} — {end.strftime('%d.%m.%Y')}", "синие — регулярные, зелёные — разовые, фиолетовые — из заметок")

    timed = [o for o in items if o.start]
    first = min([_minutes(o.start) for o in timed] + [8 * 60]) // 60
    last = max([_minutes(o.end or o.start) + (0 if o.end else 60) for o in timed] + [21 * 60])
    last = min(24, -(-last // 60))
    hours = max(1, last - first)

    top = PAGE_H - MARGIN - 40
    allday_h = 18
    grid_top = top - 22 - allday_h
    grid_bottom = MARGIN
    left = MARGIN + 34
    col_w = (PAGE_W - MARGIN - left) / 7
    hour_h = (grid_top - grid_bottom) / hours

    for i in range(7):
        day = start + timedelta(days=i)
        x = left + i * col_w
        if i >= 5:
            c.setFillColor(COLORS["weekend"])
            c.rect(x, grid_bottom, col_w, top - 22 - grid_bottom, stroke=0, fill=1)
        if today and day == today:
            c.setFillColor(COLORS["today"])
            c.rect(x, grid_bottom, col_w, top - 22 - grid_bottom, stroke=0, fill=1)
        c.setFillColor(COLORS["text"])
        c.setFont("Bold", 10)
        c.drawString(x + 4, top - 14, f"{WEEKDAYS[i]}, {day.strftime('%d.%m')}")

    c.setStrokeColor(COLORS["grid"])
    c.setLineWidth(0.5)
    for h in range(hours + 1):
        y = grid_top - h * hour_h
        c.line(left, y, PAGE_W - MARGIN, y)
        if h < hours:
            c.setFillColor(COLORS["muted"])
            c.setFont("Body", 8)
            c.drawRightString(left - 4, y - 9, f"{first + h:02d}:00")
    for i in range(8):
        c.line(left + i * col_w, grid_bottom, left + i * col_w, top - 22)

    for o in items:
        i = (o.day - start).days
        if not 0 <= i < 7:
            continue
        x = left + i * col_w + 2
        fill, border = _box_colors(o.kind)
        c.setFillColor(fill)
        c.setStrokeColor(border)
        if not o.start:  # весь день — полоска над сеткой
            c.roundRect(x, grid_top + 2, col_w - 4, allday_h - 4, 3, stroke=1, fill=1)
            c.setFillColor(COLORS["text"])
            c.setFont("Body", 7.5)
            c.drawString(x + 3, grid_top + 7, _fit(c, o.title, "Body", 7.5, col_w - 10))
            continue
        s = max(_minutes(o.start), first * 60)
        e = min(_minutes(o.end) if o.end else s + 60, last * 60)
        y_top = grid_top - (s - first * 60) / 60 * hour_h
        height = max(12, (e - s) / 60 * hour_h - 2)
        c.roundRect(x, y_top - height, col_w - 4, height, 3, stroke=1, fill=1)
        c.setFillColor(COLORS["text"])
        c.setFont("Bold", 7.5)
        c.drawString(x + 3, y_top - 9, _fit(c, o.title, "Bold", 7.5, col_w - 10))
        if height > 20:
            c.setFont("Body", 7)
            c.setFillColor(COLORS["muted"])
            c.drawString(x + 3, y_top - 18, f"{o.start}–{o.end}" if o.end else o.start)
    c.showPage()
    c.save()
    return buf.getvalue()


def month_pdf(items: list[Occurrence], year: int, month: int, today: date | None = None) -> bytes:
    """Месяц клетками: в каждой клетке — события дня по времени."""
    buf = io.BytesIO()
    c = Canvas(buf, pagesize=(PAGE_W, PAGE_H))
    _title(c, f"{MONTHS[month]} {year}", "")
    weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(year, month)
    top = PAGE_H - MARGIN - 40
    head_h = 16
    col_w = (PAGE_W - 2 * MARGIN) / 7
    row_h = (top - head_h - MARGIN) / len(weeks)
    c.setFont("Bold", 9)
    for i, name in enumerate(WEEKDAYS):
        c.setFillColor(COLORS["muted"])
        c.drawString(MARGIN + i * col_w + 4, top - 11, name)
    by_day: dict[date, list[Occurrence]] = {}
    for o in items:
        by_day.setdefault(o.day, []).append(o)
    for r, week in enumerate(weeks):
        for i, day in enumerate(week):
            x = MARGIN + i * col_w
            y = top - head_h - (r + 1) * row_h
            bg = COLORS["today"] if today and day == today else COLORS["weekend"] if i >= 5 else COLORS["background"]
            c.setFillColor(bg)
            c.setStrokeColor(COLORS["grid"])
            c.rect(x, y, col_w, row_h, stroke=1, fill=1)
            c.setFillColor(COLORS["text"] if day.month == month else COLORS["muted"])
            c.setFont("Bold", 10)
            c.drawString(x + 4, y + row_h - 13, str(day.day))
            if day.month != month:
                continue
            line_y = y + row_h - 25
            events = by_day.get(day, [])
            for n, o in enumerate(events):
                if line_y < y + 4:
                    break
                if line_y < y + 14 and n < len(events) - 1:
                    c.setFillColor(COLORS["muted"])
                    c.setFont("Body", 7)
                    c.drawString(x + 4, line_y, f"ещё {len(events) - n}…")
                    break
                fill, border = _box_colors(o.kind)
                c.setFillColor(border)
                c.circle(x + 6, line_y + 2.5, 1.8, stroke=0, fill=1)
                c.setFillColor(COLORS["text"])
                c.setFont("Body", 7)
                label = f"{o.start} {o.title}" if o.start else o.title
                c.drawString(x + 10, line_y, _fit(c, label, "Body", 7, col_w - 14))
                line_y -= 9.5
    c.showPage()
    c.save()
    return buf.getvalue()
