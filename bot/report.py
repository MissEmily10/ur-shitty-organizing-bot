"""Недельный отчёт: настроение и дела по дням. Картинка-таблица (Pillow) + страница в Notion + вывод ИИ.
Оформление — из design/tokens.json, под замену дизайном владелицы."""

import io
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import notion

ROOT = Path(__file__).resolve().parent.parent
TOKENS = json.loads((ROOT / "design" / "tokens.json").read_text(encoding="utf-8"))
COLORS = TOKENS["colors"]
MOOD_EMOJI = {1: "😞", 2: "😕", 3: "😐", 4: "🙂", 5: "🤩"}
DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


@dataclass
class Day:
    day: date
    mood: int | None
    comment: str
    notes: int
    sorted: int
    done: int


@dataclass
class Week:
    start: date
    days: list[Day]
    average: float | None
    previous: float | None

    @property
    def title(self) -> str:
        end = self.start + timedelta(days=6)
        return f"Неделя {self.start.strftime('%d.%m')} — {end.strftime('%d.%m.%Y')}"

    def totals(self) -> tuple[int, int, int]:
        return sum(d.notes for d in self.days), sum(d.sorted for d in self.days), sum(d.done for d in self.days)


def _num(value: float) -> str:
    """3.55 → «3,6»: по-русски, с запятой."""
    return f"{value:.1f}".replace(".", ",")


def _avg(values: list[int]) -> float | None:
    return sum(values) / len(values) if values else None


async def collect(uid: int, monday: date, tz) -> Week:
    start = datetime.combine(monday, time(0, 0), tz)
    end = start + timedelta(days=7)
    moods = await notion.moods(uid)
    notes = await notion.notes_created(uid, start.isoformat(), end.isoformat())
    done = await notion.done_between(uid, monday.isoformat(), (monday + timedelta(days=7)).isoformat())
    days = []
    for i in range(7):
        day = monday + timedelta(days=i)
        made = [n for n in notes if datetime.fromisoformat(n["created_at"].replace("Z", "+00:00")).astimezone(tz).date() == day]
        score, comment = moods.get(day.isoformat(), (None, ""))
        days.append(
            Day(
                day=day,
                mood=int(score) if score else None,
                comment=comment,
                notes=len(made),
                sorted=sum(1 for n in made if n["type"]),
                done=sum(1 for n in done if (n["when"] or "")[:10] == day.isoformat()),
            )
        )
    previous = [moods[(monday - timedelta(days=7 - i)).isoformat()][0] for i in range(7) if (monday - timedelta(days=7 - i)).isoformat() in moods]
    return Week(monday, days, _avg([d.mood for d in days if d.mood]), _avg([int(v) for v in previous if v]))


def table_rows(week: Week) -> list[list[str]]:
    rows = [["День", "Настроение", "Комментарий", "Заметок", "Разобрано", "Выполнено"]]
    for d in week.days:
        mood = f"{MOOD_EMOJI[d.mood]} {d.mood}" if d.mood else "—"
        rows.append([f"{DAYS[d.day.weekday()]} {d.day.strftime('%d.%m')}", mood, d.comment or "—", str(d.notes), str(d.sorted), str(d.done)])
    notes, sorted_, done = week.totals()
    avg = f"ср. {_num(week.average)}" if week.average else "—"
    rows.append(["Итого", avg, "", str(notes), str(sorted_), str(done)])
    return rows


def mood_line(week: Week) -> str:
    return "".join(MOOD_EMOJI.get(d.mood, "▫️") for d in week.days)


def render_png(week: Week) -> bytes:
    """Картинка-таблица. Эмодзи в шрифтах для картинок не рисуются, поэтому настроение — цветной кружок с числом."""
    regular = ImageFont.truetype(str(ROOT / TOKENS["fonts"]["regular"]), 22)
    bold = ImageFont.truetype(str(ROOT / TOKENS["fonts"]["bold"]), 22)
    big = ImageFont.truetype(str(ROOT / TOKENS["fonts"]["bold"]), 36)
    small = ImageFont.truetype(str(ROOT / TOKENS["fonts"]["regular"]), 18)
    widths = [130, 150, 440, 110, 140, 150]
    width = sum(widths) + 80
    row_h, head = 56, 130
    height = head + row_h * 9 + 40
    img = Image.new("RGB", (width, height), COLORS["background"])
    d = ImageDraw.Draw(img)
    d.text((40, 36), week.title, font=big, fill=COLORS["text"])
    sub = f"Среднее настроение: {_num(week.average)}" if week.average else "Настроение не отмечалось"
    if week.average and week.previous:
        sub += f" (неделей раньше {_num(week.previous)})"
    d.text((40, 84), sub, font=small, fill=COLORS["muted"])

    rows = table_rows(week)
    y = head
    for r, row in enumerate(rows):
        fill = COLORS["header"] if r in (0, len(rows) - 1) else (COLORS["weekend"] if r in (6, 7) else COLORS["background"])
        d.rectangle([40, y, width - 40, y + row_h], fill=fill)
        d.line([40, y + row_h, width - 40, y + row_h], fill=COLORS["grid"], width=1)
        x = 40
        for c, cell in enumerate(row):
            font = bold if r in (0, len(rows) - 1) or c == 0 else regular
            if c == 1 and 0 < r < len(rows) - 1 and week.days[r - 1].mood:
                score = week.days[r - 1].mood
                d.ellipse([x + 14, y + 16, x + 38, y + 40], fill=COLORS[f"mood_{score}"])
                d.text((x + 50, y + 15), f"{score} из 5", font=regular, fill=COLORS["text"])
            else:
                text = cell.replace("😞", "").replace("😕", "").replace("😐", "").replace("🙂", "").replace("🤩", "").strip()
                if d.textlength(text, font=font) > widths[c] - 24:
                    while text and d.textlength(text + "…", font=font) > widths[c] - 24:
                        text = text[:-1]
                    text = text.rstrip() + "…"
                d.text((x + 14, y + 15), text, font=font, fill=COLORS["text"] if text != "—" else COLORS["muted"])
            x += widths[c]
        y += row_h
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()
