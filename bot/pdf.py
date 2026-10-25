"""Календарь расписания в PDF A4 (альбомная ориентация): неделя по часам и месяц клетками.
Цвета и шрифты — из design/tokens.json, чтобы заменить их дизайн-системой владелицы без правки кода."""

import calendar
import io
import json
import re
from dataclasses import replace
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


def _optional_font(name: str, key: str) -> str | None:
    """Шрифт из дизайна владелицы, если его файл лежит в design/fonts (TTF); иначе None — рисуем PT Sans."""
    path = ROOT / TOKENS["fonts"].get(key, "")
    if not path.is_file():
        return None
    try:
        pdfmetrics.registerFont(TTFont(name, str(path)))
    except Exception:  # например, OTF с CFF-контурами reportlab не читает
        return None
    return name


DAY_FONT = _optional_font("Day", "day")  # Paytone One — заголовки дней
DISPLAY_FONT = _optional_font("Display", "display")  # Coolvetica — названия занятий
DISPLAY_ITALIC = _optional_font("DisplayItalic", "display_italic")  # Coolvetica Italic — заголовок
DAYS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

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


def _can(font: str | None, text: str, fallback: str) -> str:
    """Шрифт, если в нём есть все буквы текста (в латинских шрифтах может не быть кириллицы), иначе запасной."""
    if not font:
        return fallback
    glyphs = pdfmetrics.getFont(font).face.charToGlyph
    return font if all(ord(ch) in glyphs for ch in text if not ch.isspace()) else fallback


def _plain(items: list[Occurrence]) -> list[Occurrence]:
    """Без эмодзи в названиях (🎒, 🧑‍🏫…): в PT Sans их нет, в PDF вместо них были бы квадратики."""
    glyphs = pdfmetrics.getFont("Body").face.charToGlyph
    return [replace(o, title="".join(ch for ch in o.title if ord(ch) in glyphs).strip() or o.title) for o in items]


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
    items = _plain(items)
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
    items = _plain(items)
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


# ---------- 🎒 индивидуальное расписание (уроки по дням, отдельно от основного календаря) ----------
# Выкройка под дизайн: колонки дней недели, в каждой — карточки занятий сверху вниз. У урока: время, кабинет,
# номер и предмет, преподаватель; у урока «через неделю» — что будет на другой неделе. Занятия вне школы — другим цветом.


def _span(item: dict) -> tuple[int, int]:
    """Начало и конец в минутах; без конца — час."""
    start = _minutes(item["start"]) if item.get("start") else _minutes(item["end"]) - 60
    end = _minutes(item["end"]) if item.get("end") else start + 60
    return start, max(end, start + 15)


def _lanes(items: list[dict]) -> list[tuple[dict, int, int]]:
    """Пересекающиеся занятия — рядом: (занятие, номер дорожки, сколько дорожек в группе)."""
    spans = sorted(((i, *_span(i)) for i in items), key=lambda x: (x[1], x[2]))
    placed, group, group_end = [], [], -1
    out = []

    def flush():
        lanes = max((lane for _, lane in group), default=-1) + 1
        out.extend((item, lane, lanes) for item, lane in group)

    for item, start, end in spans:
        if group and start >= group_end:
            flush()
            group, placed = [], []
        free = next((n for n, last in enumerate(placed) if last <= start), None)
        if free is None:
            placed.append(end)
            free = len(placed) - 1
        else:
            placed[free] = end
        group.append((item, free))
        group_end = max(group_end, end)
    if group:
        flush()
    return out


def timetable_pdf(days: list[tuple[date, list[dict]]], title: str, subtitle: str, other: dict | None = None) -> bytes:
    """Сетка по часам: колонки дней, высота карточки — длительность занятия, окна остаются пустыми.
    days — [(дата, занятия этого дня)] в порядке недели; other — {(день недели, начало): название варианта
    другой недели} для уроков, которые чередуются."""
    other = other or {}
    buf = io.BytesIO()
    c = Canvas(buf, pagesize=(PAGE_W, PAGE_H))
    # шапка по Figma владелицы: заголовок Coolvetica Italic 26 с трекингом −3 %, подпись PT Sans 10
    head = _can(DISPLAY_ITALIC, title, "Bold")
    text = c.beginText(MARGIN, PAGE_H - MARGIN - 19)
    text.setFont(head, 26 if head != "Bold" else 22)
    text.setCharSpace(-0.03 * 26)
    text.setFillColor(COLORS["text"])
    text.textOut(title)
    c.drawText(text)
    c.setFont("Body", 10)
    c.setFillColor(COLORS["muted"])
    c.drawRightString(PAGE_W - MARGIN, PAGE_H - MARGIN - 14, subtitle)
    days = [d for d in days if d[1]] or days[:5]
    spans = [_span(i) for _, items in days for i in items]
    first = min([s for s, _ in spans] + [8 * 60]) // 60
    last = min(24, -(-max([e for _, e in spans] + [18 * 60]) // 60))
    hours = max(1, last - first)

    cols = max(1, len(days))
    top, bottom = PAGE_H - MARGIN - 40, MARGIN
    left = MARGIN + 30
    gap = 6
    col_w = (PAGE_W - MARGIN - left - gap * (cols - 1)) / cols
    grid_top = top - 26
    per_min = (grid_top - bottom) / (hours * 60)

    def y_of(minutes: int) -> float:
        return grid_top - (minutes - first * 60) * per_min

    # часы: подписи слева и тонкие линии через всю сетку
    c.setFont("Body", 8)
    for h in range(first, last + 1):
        y = y_of(h * 60)
        c.setStrokeColor(COLORS["grid"])
        c.setLineWidth(0.5)
        c.line(left - 4, y, PAGE_W - MARGIN, y)
        c.setFillColor(COLORS["muted"])
        c.drawRightString(left - 7, y - 3, f"{h}:00")

    for n, (day, items) in enumerate(days):
        x = left + n * (col_w + gap)
        c.setFillColor(COLORS["tt_day"])
        c.roundRect(x, top - 20, col_w, 20, 4, stroke=0, fill=1)
        c.setFillColor(COLORS["text"])
        if DAY_FONT:  # как в Figma: «Monday 19.10  †» шрифтом Paytone One
            c.setFont(DAY_FONT, 11)
            c.drawString(x + 6, top - 14, _fit(c, f"{DAYS_EN[day.weekday()]} {day:%d.%m}  †", DAY_FONT, 11, col_w - 10))
        else:
            c.setFont("Bold", 11)
            c.drawString(x + 6, top - 14, f"{WEEKDAYS[day.weekday()]} {day:%d.%m}")
        for item, lane, lanes in _lanes(items):
            start, end = _span(item)
            w = (col_w - 2 * (lanes - 1)) / lanes
            bx = x + lane * (w + 2)
            y_top, y_bottom = y_of(start) - 1, y_of(end) + 1
            h = y_top - y_bottom
            # цвета из Figma: уроки — сиреневые, репетиторы (у них ссылка на вход) — розовые, вне школы — салатовые
            kind = "tt_tutor" if item.get("tutor") or item.get("link") else ("tt_lesson" if item.get("lesson") else "tt_other")
            fill, border = _box_colors(kind)
            c.setFillColor(fill)
            c.setStrokeColor(border)
            c.setLineWidth(1)
            c.roundRect(bx, y_bottom, w, h, 3, stroke=1, fill=1)
            if item.get("until"):
                time = f"до {item['end']}"
            else:
                time = f"{item['start']}–{item['end']}" if item.get("start") and item.get("end") else item.get("start") or ""
            line = y_top - 9
            c.setFillColor(COLORS["text"])
            c.setFont("Bold", 7.5)
            c.drawString(bx + 4, line, _fit(c, time, "Bold", 7.5, w - 8))
            if item.get("room") and w > 70:
                room = "онлайн" if item["room"].lower() == "онлайн" else f"каб. {item['room']}"
                c.setFont("Body", 7.5)
                c.drawRightString(bx + w - 4, line, room)
            name = (f"{item['num']}. " if item.get("num") else "") + item["title"]
            line -= 10
            if line < y_bottom + 2:
                continue
            # название — Coolvetica 10 (если в шрифте есть буквы), в две строки, если хватает высоты
            font = _can(DISPLAY_FONT, name, "Bold")
            size = 10 if font != "Bold" else 9
            words, first_line = name.split(), ""
            while words and c.stringWidth((first_line + " " + words[0]).strip(), font, size) <= w - 8:
                first_line = (first_line + " " + words.pop(0)).strip()
            c.setFont(font, size)
            if words and line - 10 >= y_bottom + 2:
                c.drawString(bx + 4, line, first_line or _fit(c, name, font, size, w - 8))
                line -= 10
                c.drawString(bx + 4, line, _fit(c, " ".join(words), font, size, w - 8))
            else:  # в одну строку: сначала чуть уменьшаем шрифт, потом уже обрезаем
                while size > 7.5 and c.stringWidth(name, font, size) > w - 8:
                    size -= 0.5
                c.setFont(font, size)
                c.drawString(bx + 4, line, _fit(c, name, font, size, w - 8))
            line -= 9
            c.setFont("Body", 7)
            c.setFillColor(COLORS["muted"])
            if item.get("teacher") and line >= y_bottom + 2:
                c.drawString(bx + 4, line, _fit(c, item["teacher"], "Body", 7, w - 8))
                line -= 8.5
            alt = other.get((day.weekday(), item.get("start")))
            if alt and item.get("week") is not None and line >= y_bottom + 2:
                c.setFillColor(border)
                c.drawString(bx + 4, line, _fit(c, f"через неделю: {alt}", "Body", 7, w - 8))
    c.showPage()
    c.save()
    return buf.getvalue()


# ---------- 🖨 все заметки для печати: A4 книжная, несколько листов ----------

PORTRAIT_W, PORTRAIT_H = A4
NOTE_KINDS = (  # цвет карточки по типу — палитра «Моего расписания» из Figma владелицы
    ("tt_lesson", ("задач", "напомин", "план")),
    ("tt_tutor", ("иде", "референс")),
    ("tt_other", ("событ", "обсуд", "встреч")),
)
RULE_GAP = 17  # расстояние между линейками для пометок, pt (~6 мм — удобно писать ручкой)
NOTES_LEGEND = "сиреневые — задачи и напоминания · розовые — идеи и референсы · салатовые — события и «обсудить»"


def _clean(text: str, font: str = "Body") -> str:
    """Без символов, которых нет в шрифте (эмодзи и т. п.): иначе в PDF были бы квадратики."""
    glyphs = pdfmetrics.getFont(font).face.charToGlyph
    return re.sub(r"\s{2,}", " ", "".join(ch for ch in text if ord(ch) in glyphs or ch in "\n")).strip()


def _wrap(c: Canvas, text: str, font: str, size: float, width: float, max_lines: int) -> list[str]:
    """Перенос по словам; что не влезло в max_lines — многоточие в последней строке."""
    lines: list[str] = []
    for para in text.splitlines() or [""]:
        words, line = para.split(), ""
        for word in words:
            probe = f"{line} {word}".strip()
            if c.stringWidth(probe, font, size) <= width:
                line = probe
                continue
            if line:
                lines.append(line)
            line = word if c.stringWidth(word, font, size) <= width else _fit(c, word, font, size, width)
        if line:
            lines.append(line)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = _fit(c, lines[-1] + " …", font, size, width) if not lines[-1].endswith("…") else lines[-1]
    return lines


def _note_kind(note: dict) -> str:
    kind = (note.get("type") or note.get("ai_type") or "").lower()
    return next((name for name, keys in NOTE_KINDS if any(k in kind for k in keys)), "")


MARKER = 28  # метки по углам листа: по ним бот находит лист на фото и место каждой заметки (paper.py)
MARKER_INSET = 14  # от края листа: у большинства принтеров поля 4–5 мм


def marker_corners(page_w: float = PORTRAIT_W, page_h: float = PORTRAIT_H) -> list[tuple[float, float]]:
    """Левые нижние углы меток: левый верхний, правый верхний, правый нижний, левый нижний."""
    far_x, far_y = page_w - MARKER_INSET - MARKER, page_h - MARKER_INSET - MARKER
    return [(MARKER_INSET, far_y), (far_x, far_y), (far_x, MARKER_INSET), (MARKER_INSET, MARKER_INSET)]


def _markers(c: Canvas, page: int) -> None:
    """ArUco-метки (номер листа × 4 + угол) — как QR-код, только проще: их надёжно находит OpenCV даже на косом фото."""
    try:
        import cv2
    except ImportError:
        return
    if page * 4 + 3 >= 250:
        return
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_250)
    c.setFillColor(HexColor("#000000"))
    for corner, (x, y) in enumerate(marker_corners()):
        bits = cv2.aruco.generateImageMarker(dictionary, page * 4 + corner, 6)  # 4×4 клетки + чёрная рамка
        cell = MARKER / 6
        for row in range(6):
            for col in range(6):
                if bits[row][col] < 128:
                    c.rect(x + col * cell, y + MARKER - (row + 1) * cell, cell + 0.05, cell + 0.05, stroke=0, fill=1)


def notes_pdf(groups: list[tuple[str, list[tuple[str, list[dict]]]]], title: str, subtitle: str, layout: list | None = None) -> bytes:
    """groups — [(сфера, [(проект, [заметка])])]; заметка — {"num", "title", "type", "when", "summary", "children": [заметка]}.
    Каждая заметка — карточка с квадратиком и номером «#12», чтобы вычеркнуть ручкой и прислать фото боту.
    layout — сюда записывается, где на каком листе какая заметка (для поиска зачёркнутого по фото, paper.py)."""
    buf = io.BytesIO()
    c = Canvas(buf, pagesize=(PORTRAIT_W, PORTRAIT_H))
    m = 48
    width = PORTRAIT_W - 2 * m
    head_font = _can(DISPLAY_FONT, "", "Bold")

    def card_lines(note: dict, indent: float) -> dict:
        """Строки карточки и её высота (без отрисовки) — чтобы заранее разложить по страницам."""
        text_w = width - indent - 34 - 110
        name = _clean(note["title"]) or "Без названия"
        font = _can(DISPLAY_FONT, name, "Bold")
        title_lines = _wrap(c, name, font, 10.5, text_w, 2)
        summary = _wrap(c, _clean(note.get("summary") or ""), "Body", 9, text_w + 110, 3 if not indent else 2)
        rules = 1 if indent else 2  # линейки для пометок от руки: у заметки две, у подзадачи одна
        return {"note": note, "indent": indent, "font": font, "title": title_lines, "summary": summary, "rules": rules,
                "h": 8 + 12.5 * len(title_lines) + 11 * len(summary) + 4 + RULE_GAP * rules}  # fmt: skip

    # 1. раскладка: элементы (заголовок сферы, проекта, карточка с подзадачами) по страницам
    blocks: list[tuple[str, object, float]] = []
    for sphere, projects in groups:
        blocks.append(("sphere", sphere, 30))
        for project, notes in projects:
            if project:  # у заметок без проекта подзаголовка нет
                blocks.append(("project", project, 18))
            for note in notes:
                main = card_lines(note, 0)
                kids = [card_lines(k, 22) for k in note.get("children") or []]
                blocks.append(("card", (main, kids), main["h"] + sum(k["h"] for k in kids) + 6))
    top, bottom = PORTRAIT_H - m - 58, m + 22
    pages: list[list[tuple[str, object, float]]] = [[]]
    y = top
    for i, (kind, data, h) in enumerate(blocks):
        # заголовок не оставляем внизу листа без карточки под ним
        need = h + (sum(b[2] for b in blocks[i + 1 : i + 3] if b[0] != "sphere") if kind != "card" else 0)
        if y - min(need, top - bottom) < bottom and pages[-1]:
            pages.append([])
            y = top
        pages[-1].append((kind, data, y))
        y -= h

    # 2. отрисовка
    for n, page in enumerate(pages, 1):
        c.setFillColor(COLORS["text"])
        text = c.beginText(m, PORTRAIT_H - m - 22)
        text.setFont(DAY_FONT or "Bold", 24)
        text.setCharSpace(-0.03 * 24)
        text.textOut("My notes" if DAY_FONT else _clean(title, "Bold"))
        c.drawText(text)
        c.setFont("Body", 9.5)
        c.setFillColor(COLORS["muted"])
        c.drawRightString(PORTRAIT_W - m, PORTRAIT_H - m - 12, _clean(title))
        c.drawRightString(PORTRAIT_W - m, PORTRAIT_H - m - 24, _clean(subtitle))
        c.setFont("Body", 7.5)
        c.drawString(m, PORTRAIT_H - m - 40, NOTES_LEGEND)
        c.drawString(m, m, "Вычеркни ненужное, допиши пометки на линейках и пришли фото боту: /crossed")
        c.drawRightString(PORTRAIT_W - m, m, f"лист {n} из {len(pages)}")
        _markers(c, n - 1)
        for kind, data, y in page:
            if kind == "sphere":
                c.setFillColor(COLORS["tt_day"])
                c.roundRect(m, y - 22, width, 22, 4, stroke=0, fill=1)
                c.setFillColor(COLORS["text"])
                c.setFont(head_font, 12.5)
                c.drawString(m + 8, y - 15.5, _fit(c, _clean(data, head_font) or "Без сферы", head_font, 12.5, width - 16))
            elif kind == "project":
                c.setFillColor(COLORS["muted"])
                c.setFont("Bold", 9.5)
                c.drawString(m + 2, y - 11, _fit(c, _clean(data, "Bold"), "Bold", 9.5, width))
            else:
                main, kids = data
                total = main["h"] + sum(k["h"] for k in kids)
                kind_color = _note_kind(main["note"])
                fill, border = _box_colors(kind_color) if kind_color else (COLORS["background"], COLORS["grid"])
                if kids:  # большая заметка с подзадачами — тёплая, как шапки дней в расписании
                    fill, border = COLORS["tt_day"], COLORS["tt_other_border"]
                c.setFillColor(fill)
                c.setStrokeColor(border)
                c.setLineWidth(0.9)
                c.roundRect(m, y - total, width, total, 4, stroke=1, fill=1)
                line_y = y
                for card in [main] + kids:
                    place = _note_row(c, card, m, width, line_y)
                    if layout is not None:
                        layout.append({"num": card["note"]["num"], "page": n - 1, **place})
                    line_y -= card["h"]
        c.showPage()
    c.save()
    return buf.getvalue()


def _note_row(c: Canvas, card: dict, x0: float, width: float, y: float) -> dict:
    """Рисует строку заметки и возвращает, где она на листе (pt): band — область без линеек для пометок,
    ink — где напечатан текст (его не считаем следами ручки), check — внутренность квадратика."""
    note, indent = card["note"], card["indent"]
    x = x0 + 8 + indent
    ink = [[x - 1.5, y - 18.5, x + 10.5, y - 6.5]]

    def text_box(left: float, base: float, text: str, font: str, size: float, right: bool = False) -> None:
        w = c.stringWidth(text, font, size)
        x1, x2 = (left - w, left) if right else (left, left + w)
        ink.append([x1 - 1, base - 0.3 * size, x2 + 1, base + 0.85 * size])

    if indent:  # подзадача: тонкая линия-отступ слева
        c.setStrokeColor(COLORS["muted"])
        c.setLineWidth(0.5)
        c.line(x - 8, y - 2, x - 8, y - card["h"] + 4)
        ink.append([x - 9.5, y - card["h"] + 3, x - 6.5, y - 1])
    # квадратик для галочки и номер
    c.setFillColor(COLORS["background"])
    c.setStrokeColor(COLORS["text"])
    c.setLineWidth(0.8)
    c.rect(x, y - 17, 9, 9, stroke=1, fill=1)
    c.setFillColor(COLORS["text"])
    c.setFont("Bold", 8)
    c.drawString(x + 13, y - 15.5, f"#{note['num']}")
    text_box(x + 13, y - 15.5, f"#{note['num']}", "Bold", 8)
    tx = x + 34
    line = y - 16
    c.setFont(card["font"], 10.5)
    for t in card["title"]:
        c.drawString(tx, line, t)
        text_box(tx, line, t, card["font"], 10.5)
        line -= 12.5
    meta = " · ".join(b for b in (_clean(note.get("type") or "", "Body"), note.get("when_label") or "") if b)
    if meta:
        meta = _fit(c, meta, "Body", 7.5, 104)
        c.setFont("Body", 7.5)
        c.setFillColor(COLORS["muted"])
        c.drawRightString(x0 + width - 8, y - 15.5, meta)
        text_box(x0 + width - 8, y - 15.5, meta, "Body", 7.5, right=True)
    c.setFont("Body", 9)
    c.setFillColor(COLORS["muted"])
    line += 1.5
    for t in card["summary"]:
        c.drawString(tx, line, t)
        text_box(tx, line, t, "Body", 9)
        line -= 11
    # линейки для пометок: пунктир по всей ширине карточки, номер заметки рядом — ИИ поймёт, к чему пометка
    c.setStrokeColor(COLORS["muted"])
    c.setLineWidth(0.4)
    c.setDash(1, 2.5)
    bottom = y - card["h"] + 5
    for i in range(card["rules"]):
        ry = bottom + i * RULE_GAP
        c.line(tx, ry, x0 + width - 10, ry)
    c.setDash()
    band = [x0 + 4, y - card["h"] + card["rules"] * RULE_GAP + 3, x0 + width - 4, y - 3]
    return {"band": [round(v, 1) for v in band], "ink": [[round(v, 1) for v in b] for b in ink],
            "check": [round(x + 2.4, 1), round(y - 14.6, 1), round(x + 6.6, 1), round(y - 10.4, 1)]}  # fmt: skip


# ---------- ✂️ карточки для вырезания: одна задача — одна карточка ----------

CARD_COLS, CARD_ROWS = 2, 4  # 8 карточек на A4, каждая ~98 × 70 мм
CARD_PAD = 12
URGENCY_LABELS = {"fire": "горит", "soon": "скоро", "later": "потом"}


def cards_pdf(cards: list[dict], title: str) -> bytes:
    """A4 книжная, сетка карточек без зазоров: режется по пунктиру ножницами в два счёта.
    Карточка — {"order", "num", "title", "type", "where", "when_label", "urgency": fire|soon|later, "summary",
    "steps": [(название, готово ли)], "created"}; порядок карточек — по важности (как пришли)."""
    buf = io.BytesIO()
    c = Canvas(buf, pagesize=(PORTRAIT_W, PORTRAIT_H))
    top_margin, margin = 30, 18
    card_w = (PORTRAIT_W - 2 * margin) / CARD_COLS
    card_h = (PORTRAIT_H - top_margin - margin) / CARD_ROWS
    per_page = CARD_COLS * CARD_ROWS
    pages = max(1, -(-len(cards) // per_page))
    for page in range(pages):
        c.setFont("Body", 8)
        c.setFillColor(COLORS["muted"])
        c.drawString(margin, PORTRAIT_H - 19, _clean(title) + " · режь по пунктиру")
        c.drawRightString(PORTRAIT_W - margin, PORTRAIT_H - 19, f"лист {page + 1} из {pages}")
        chunk = cards[page * per_page : (page + 1) * per_page]
        for i, card in enumerate(chunk):
            col, row = i % CARD_COLS, i // CARD_COLS
            x = margin + col * card_w
            y = PORTRAIT_H - top_margin - (row + 1) * card_h
            _cut_card(c, card, x, y, card_w, card_h)
        # линии реза: сквозные пунктиры через весь лист, чтобы резать одним движением
        c.setStrokeColor(COLORS["muted"])
        c.setLineWidth(0.5)
        c.setDash(4, 3)
        rows = -(-len(chunk) // CARD_COLS)  # на последнем листе — только под заполненными рядами
        low = PORTRAIT_H - top_margin - rows * card_h
        for k in range(CARD_COLS + 1):
            gx = margin + k * card_w
            c.line(gx, low - (6 if rows < CARD_ROWS else low), gx, PORTRAIT_H - top_margin + 6)
        for k in range(rows + 1):
            gy = PORTRAIT_H - top_margin - k * card_h
            c.line(0, gy, PORTRAIT_W, gy)
        c.setDash()
        c.showPage()
    c.save()
    return buf.getvalue()


def _cut_card(c: Canvas, card: dict, x: float, y: float, w: float, h: float) -> None:
    kind = _note_kind(card)
    fill, border = _box_colors(kind) if kind else (COLORS["header"], COLORS["muted"])
    inner_w = w - 2 * CARD_PAD
    top = y + h
    # цветная шапка: тип слева, номер по важности справа
    c.setFillColor(fill)
    c.rect(x, top - 30, w, 30, stroke=0, fill=1)
    c.setFillColor(border)
    c.rect(x, top - 30, 4, 30, stroke=0, fill=1)
    c.setFillColor(COLORS["text"])
    c.setFont("Bold", 8.5)
    c.drawString(x + CARD_PAD, top - 19, _clean(card.get("type") or "Заметка", "Bold").upper())
    order = str(card.get("order", ""))
    label = URGENCY_LABELS.get(card.get("urgency") or "", "")
    big = DAY_FONT or "Bold"
    c.setFont(big, 17)
    c.drawRightString(x + w - CARD_PAD, top - 22, order)
    if label:
        c.setFont("Bold", 8)
        c.setFillColor(COLORS["mood_1"] if card.get("urgency") == "fire" else COLORS["muted"])
        c.drawRightString(x + w - CARD_PAD - c.stringWidth(order, big, 17) - 8, top - 19, label.upper())
    # название крупно
    line = top - 30 - 18
    name = _clean(card["title"])
    font = _can(DISPLAY_FONT, name, "Bold")
    c.setFillColor(COLORS["text"])
    c.setFont(font, 13.5)
    for t in _wrap(c, name, font, 13.5, inner_w, 2):
        c.drawString(x + CARD_PAD, line, t)
        line -= 15.5
    # где и когда
    c.setFont("Body", 8.5)
    c.setFillColor(COLORS["muted"])
    where = _clean(card.get("where") or "")
    if where:
        c.drawString(x + CARD_PAD, line + 2, _fit(c, where, "Body", 8.5, inner_w))
        line -= 11
    if card.get("when_label"):
        pill = f"до {card['when_label']}"
        pw = c.stringWidth(pill, "Bold", 8.5) + 12
        c.setFillColor(COLORS["mood_1"] if card.get("urgency") == "fire" else COLORS["text"])
        c.roundRect(x + CARD_PAD, line - 3, pw, 13, 6.5, stroke=0, fill=1)
        c.setFillColor(COLORS["background"])
        c.setFont("Bold", 8.5)
        c.drawString(x + CARD_PAD + 6, line + 0.5, pill)
        line -= 17
    line -= 2
    # суть и шаги — сколько поместится до подвала
    footer = y + 22
    steps = card.get("steps") or []
    step_lines = min(len(steps), 4)
    room = int((line - footer - step_lines * 12.5 - (4 if steps else 0)) // 11.5)
    c.setFillColor(COLORS["text"])
    c.setFont("Body", 9.5)
    for t in _wrap(c, _clean(card.get("summary") or ""), "Body", 9.5, inner_w, max(room, 0)):
        c.drawString(x + CARD_PAD, line, t)
        line -= 11.5
    if steps:
        line -= 4
        for k, (step, done) in enumerate(steps[:step_lines]):
            if k == step_lines - 1 and len(steps) > step_lines:
                step, done = f"и ещё {len(steps) - step_lines + 1}…", False
            c.setStrokeColor(COLORS["text"])
            c.setLineWidth(0.7)
            c.rect(x + CARD_PAD, line - 1, 7, 7, stroke=1, fill=0)
            if done:
                c.line(x + CARD_PAD + 1, line + 2.5, x + CARD_PAD + 3, line)
                c.line(x + CARD_PAD + 3, line, x + CARD_PAD + 6.5, line + 6)
            c.setFillColor(COLORS["muted"] if done else COLORS["text"])
            c.setFont("Body", 9)
            c.drawString(x + CARD_PAD + 12, line, _fit(c, _clean(step), "Body", 9, inner_w - 12))
            line -= 12.5
    # подвал: квадратик «готово», номер заметки (по нему бот поймёт фото) и дата
    c.setStrokeColor(COLORS["text"])
    c.setLineWidth(0.8)
    c.rect(x + CARD_PAD, y + 10, 9, 9, stroke=1, fill=0)
    c.setFillColor(COLORS["text"])
    c.setFont("Body", 8)
    c.drawString(x + CARD_PAD + 13, y + 12, "готово")
    c.setFillColor(COLORS["muted"])
    c.drawRightString(x + w - CARD_PAD, y + 12, f"#{card.get('num', '')} · от {card.get('created', '')}")
