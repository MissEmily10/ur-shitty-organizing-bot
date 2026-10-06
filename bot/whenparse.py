"""Сроки словами: «завтра в 15», «в пятницу 18:00», «15 июня», «через 2 часа», «25.12 10:30».
Частые случаи разбираем сами — быстро и бесплатно; что не поняли, отдаём ИИ (ai.parse_when)."""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}  # fmt: skip
WEEKDAYS = {"понедельник": 0, "вторник": 1, "сред": 2, "четверг": 3, "пятниц": 4, "суббот": 5, "воскресень": 6}


@dataclass
class When:
    at: datetime  # с часовым поясом человека
    has_time: bool  # False — указан только день

    def iso(self) -> str:
        return self.at.isoformat() if self.has_time else self.at.date().isoformat()


def _time(text: str) -> tuple[time, bool] | None:
    """Время из текста: «15:30», «в 15», «в 9 утра», «в 7 вечера», «в полдень»."""
    if "полдень" in text:
        return time(12, 0), True
    # «15:30» всегда время; «в 10.30» — время, а просто «25.12» — дата
    m = (
        re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", text)
        or re.search(r"\bв\s+(\d{1,2})\.(\d{2})(?![\d.])", text)
        or re.search(r"\bв\s+(\d{1,2})(?![\d.:])(?:\s*(?:час|ч\b))?", text)
    )
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2)) if m.re.groups > 1 and m.group(2) else 0
    if re.search(r"вечера|дня", text) and hh < 12:
        hh += 12
    if hh > 23 or mm > 59:
        return None
    return time(hh, mm), True


def _day(text: str, today: date) -> date | None:
    if "послезавтра" in text:
        return today + timedelta(days=2)
    if "завтра" in text:
        return today + timedelta(days=1)
    if "сегодня" in text:
        return today
    if m := re.search(r"через\s+(\d+)?\s*(дн|день|недел)", text):
        n = int(m.group(1) or 1)
        return today + timedelta(days=n * (7 if m.group(2).startswith("недел") else 1))
    if m := re.search(r"(?<![\d:])(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?(?![\d:])", text):
        dd, mo = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        year += 2000 if year < 100 else 0
        try:
            d = date(year, mo, dd)
        except ValueError:
            return None
        return d if m.group(3) or d >= today else date(year + 1, mo, dd)
    if m := re.search(r"(\d{1,2})\s+([а-я]+)", text):
        month = next((n for stem, n in MONTHS.items() if m.group(2).startswith(stem)), None)
        if month and (month != 5 or m.group(2) in ("мая", "май")):
            try:
                d = date(today.year, month, int(m.group(1)))
            except ValueError:
                return None
            return d if d >= today else date(today.year + 1, month, int(m.group(1)))
    for stem, wd in WEEKDAYS.items():
        if re.search(rf"\b{stem}", text):
            ahead = (wd - today.weekday()) % 7 or 7
            return today + timedelta(days=ahead)
    return None


def parse(text: str, now: datetime) -> When | None:
    """now — текущее время человека (с его часовым поясом). None — если срок не распознан."""
    text = text.lower().replace("ё", "е")
    if m := re.search(r"через\s+(\d+)?\s*(минут|мин\b|час)", text):
        n = int(m.group(1) or 1)
        delta = timedelta(minutes=n) if m.group(2).startswith("мин") else timedelta(hours=n)
        return When((now + delta).replace(second=0, microsecond=0), True)
    day = _day(text, now.date())
    found = _time(text)
    if not day and not found:
        return None
    if found and not day:
        # Только время: сегодня, а если уже прошло — завтра
        day = now.date() if datetime.combine(now.date(), found[0], now.tzinfo) > now else now.date() + timedelta(days=1)
    if found:
        return When(datetime.combine(day, found[0], now.tzinfo), True)
    return When(datetime.combine(day, time(0, 0), now.tzinfo), False)


def human(iso: str, now: datetime) -> str:
    """«сегодня 15:00», «завтра», «пт 18.10 09:00»."""
    has_time = "T" in iso
    at = datetime.fromisoformat(iso)
    if has_time and at.tzinfo:
        at = at.astimezone(now.tzinfo)
    days = (at.date() - now.date()).days
    day = {0: "сегодня", 1: "завтра", 2: "послезавтра", -1: "вчера"}.get(
        days, ["пн", "вт", "ср", "чт", "пт", "сб", "вс"][at.weekday()] + at.strftime(" %d.%m")
    )
    return f"{day} {at.strftime('%H:%M')}" if has_time else day
