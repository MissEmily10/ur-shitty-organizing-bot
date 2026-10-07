"""Расписание: регулярные блоки по дням недели, разовые события, отмены и события из заметок.
Хранится в таблице Notion «Расписание», здесь — сборка по дням и применение изменений."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import notion
from .notion import KIND_CANCEL, KIND_ONCE, KIND_REGULAR, WEEKDAYS


@dataclass
class Occurrence:
    day: date
    start: str  # «10:00» или «» — весь день
    end: str
    title: str
    kind: str  # regular / once / note


def _norm_day(name: str) -> str | None:
    """«пн», «Пн», «понедельник» → «Пн»."""
    name = name.strip().lower()[:2]
    return next((d for d in WEEKDAYS if d.lower() == name), None)


async def occurrences(uid: int, start: date, end: date) -> list[Occurrence]:
    """Все события человека с start по end включительно, по порядку."""
    slots = await notion.schedule_slots(uid)
    cancelled = {(s["title"].lower(), s["date"][:10]) for s in slots if s["kind"] == KIND_CANCEL and s["date"]}
    result = []
    day = start
    while day <= end:
        weekday = WEEKDAYS[day.weekday()]
        for s in slots:
            if s["kind"] == KIND_REGULAR and weekday in s["days"] and (s["title"].lower(), day.isoformat()) not in cancelled:
                result.append(Occurrence(day, s["start"], s["end"], s["title"], "regular"))
            elif s["kind"] == KIND_ONCE and s["date"] and s["date"][:10] == day.isoformat():
                result.append(Occurrence(day, s["start"], s["end"], s["title"], "once"))
        day += timedelta(days=1)
    for note in await notion.dated_events(uid, start.isoformat(), end.isoformat()):
        when = note["when"]
        at = datetime.fromisoformat(when) if "T" in when else None
        if at:
            finish = (at + timedelta(hours=1)).strftime("%H:%M")
            result.append(Occurrence(at.date(), at.strftime("%H:%M"), finish, note["title"], "note"))
        else:
            result.append(Occurrence(date.fromisoformat(when[:10]), "", "", note["title"], "note"))
    return sorted(result, key=lambda o: (o.day, o.start or "00:00", o.title))


def base_text(slots: list[dict]) -> str:
    regular = [s for s in slots if s["kind"] == KIND_REGULAR]
    if not regular:
        return "Базовое расписание пока не задано."
    lines = []
    for s in sorted(regular, key=lambda s: (min(WEEKDAYS.index(d) for d in s["days"]) if s["days"] else 9, s["start"])):
        time = f"{s['start']}–{s['end']}" if s["end"] else s["start"] or "весь день"
        lines.append(f"• {', '.join(s['days'])} {time} — {s['title']}")
    return "\n".join(lines)


def week_text(items: list[Occurrence], start: date) -> str:
    lines = []
    for i in range(7):
        day = start + timedelta(days=i)
        today = [o for o in items if o.day == day]
        lines.append(f"\n<b>{WEEKDAYS[day.weekday()]} {day.strftime('%d.%m')}</b>")
        if not today:
            lines.append("  —")
        for o in today:
            time = f"{o.start}–{o.end}" if o.end else o.start or "весь день"
            mark = {"once": "📌 ", "note": "📅 "}.get(o.kind, "")
            lines.append(f"  {time} {mark}{o.title}")
    return "\n".join(lines).strip()


async def apply(uid: int, plan: dict) -> list[str]:
    """Применяет изменения от ИИ (ai.parse_schedule_change / ai.parse_routine). Возвращает список сделанного."""
    done = []
    slots = await notion.schedule_slots(uid)
    if plan.get("replace_base"):
        for s in slots:
            if s["kind"] == KIND_REGULAR:
                await notion.remove_slot(s["page"])
        slots = [s for s in slots if s["kind"] != KIND_REGULAR]
    for name in plan.get("base_remove", []):
        for s in slots:
            if s["kind"] == KIND_REGULAR and s["title"].lower() == name.lower():
                await notion.remove_slot(s["page"])
                done.append(f"🗑 убрано из базового: {s['title']}")
    for b in plan.get("base_add", []):
        days = [d for d in (_norm_day(x) for x in b.get("days", [])) if d]
        if days and b.get("title"):
            await notion.add_slot(uid, b["title"], KIND_REGULAR, b.get("start", ""), b.get("end", ""), days=days)
            done.append(f"🔁 {', '.join(days)} {b.get('start', '')}–{b.get('end', '')} {b['title']}".replace(" –", " "))
    for e in plan.get("add", []):
        if e.get("title") and e.get("date"):
            await notion.add_slot(uid, e["title"], KIND_ONCE, e.get("start", ""), e.get("end", ""), date=e["date"])
            done.append(f"📌 {e['date']} {e.get('start', '')} {e['title']}")
    for x in plan.get("cancel", []):
        if x.get("title") and x.get("date"):
            await notion.add_slot(uid, x["title"], KIND_CANCEL, date=x["date"])
            done.append(f"🚫 {x['date']}: без «{x['title']}»")
    return done
