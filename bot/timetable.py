"""🎒 Индивидуальное расписание владелицы: уроки (время, кабинет, номер, предмет, преподаватель), занятия вне школы,
уроки, которые чередуются через неделю («ИЛИ»), звонок за 5 минут до конца урока с тем, куда идти дальше,
и выгрузка в основное расписание. Хранится в «Служебном» Notion (`timetable:<uid>`). Первый раз владелица попросила
внести её расписание за неё: оно лежит в data/owner_timetable.txt и загружается один раз при запуске (seed_owner).

Формат, который понимает разбор (как владелица пишет расписание):
    понедельник, среда - 14:00 китайский, 16:00 английский
    вторник:
    9:00-9:45 • 410
    2 Английский язык
    Фамилия Имя Отчество
    ИЛИ
    ...
    До 16:00 - добираюсь до серфа
    19:30 - 20:45 - танцы
"""

import asyncio
import json
import re
from pathlib import Path
from datetime import date, datetime, timedelta

from . import notion, pdf
from .notion import KIND_REGULAR, WEEKDAYS

DAY_NAMES = {
    "понедельник": "Пн", "вторник": "Вт", "среда": "Ср", "среду": "Ср", "четверг": "Чт",
    "пятница": "Пт", "пятницу": "Пт", "суббота": "Сб", "субботу": "Сб", "воскресенье": "Вс",
}  # fmt: skip
TIME = r"(\d{1,2})[:.](\d{2})"
LESSON_RE = re.compile(rf"^{TIME}\s*[-–—]\s*{TIME}\s*[•·.]\s*(\S+)\s*$")  # 9:00-9:45 • 410
RANGE_RE = re.compile(rf"^{TIME}\s*[-–—]\s*{TIME}\s*[-–—:]\s*(.+)$")  # 19:30 - 20:45 - танцы
UNTIL_RE = re.compile(rf"^до\s+{TIME}\s*(?:[-–—•:]\s*(.+))?$", re.I)  # До 16:00 - добираюсь
INLINE_RANGE_RE = re.compile(rf"^{TIME}\s*[-–—]\s*{TIME}\s+(.+)$")  # 14:00-15:00 китайский
AT_RE = re.compile(rf"^{TIME}\s*(?:[-–—:]\s*)?(.+)$")  # 16:20 - танцы, 14:00 китайский
NUMBERED_RE = re.compile(r"^([0-9ЗзOО])\s+(.+)$")  # «2 Английский», «З Химия» (З вместо 3)
SLOT_MARK = "🎒 "  # строки основного расписания, которые сделаны из этого (их бот обновляет сам)
BELL_BEFORE = timedelta(minutes=5)


def _hhmm(h: str, m: str) -> str:
    return f"{int(h):02d}:{m}"


def _minutes(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def _days_in(line: str) -> list[str] | None:
    """«понедельник, среда - …» → ["Пн", "Ср"]; None, если строка не начинается с дня недели."""
    words = re.findall(r"[а-яё]+", line.lower())
    if not words or words[0] not in DAY_NAMES:
        return None
    days = []
    for w in words:
        if w in DAY_NAMES:
            days.append(DAY_NAMES[w])
        elif w not in ("и",):
            break
    return days


def parse(text: str) -> list[dict]:
    """Текст расписания → список занятий:
    {"days", "start", "end", "title", "room", "num", "teacher", "lesson": урок ли, "week": None | 0 | 1}.
    week 0/1 — урок через неделю (первый вариант «ИЛИ» — 0, второй — 1)."""
    items: list[dict] = []
    days: list[str] = []
    current: dict | None = None  # урок, к которому дописываются предмет и преподаватель
    alternative = False
    for raw in text.splitlines():
        line = raw.strip().strip("•").strip()
        if not line:
            continue
        found_days = _days_in(line)
        if found_days:
            days, current = found_days, None
            rest = re.split(r"[:\-–—]", line, maxsplit=1)[1] if re.search(r"[:\-–—]", line) else ""
            for part in re.split(r",\s*(?=\d)", rest):
                if m := INLINE_RANGE_RE.match(part.strip()):
                    items.append({"days": days, "start": _hhmm(m[1], m[2]), "end": _hhmm(m[3], m[4]), "title": m[5].strip().capitalize(),
                                  "room": "", "num": "", "teacher": "", "lesson": True, "week": None})  # fmt: skip
                elif m := AT_RE.match(part.strip()):
                    items.append({"days": days, "start": _hhmm(m[1], m[2]), "end": "", "title": m[3].strip().capitalize(),
                                  "room": "", "num": "", "teacher": "", "lesson": True, "week": None})  # fmt: skip
            continue
        if line.upper() == "ИЛИ":
            alternative = True
            continue
        if m := LESSON_RE.match(line):
            current = {"days": days, "start": _hhmm(m[1], m[2]), "end": _hhmm(m[3], m[4]), "title": "", "room": m[5],
                       "num": "", "teacher": "", "lesson": True, "week": None}  # fmt: skip
            if alternative:
                prev = next((i for i in reversed(items) if i["lesson"] and i["start"] == current["start"] and i["days"] == days), None)
                if prev:
                    prev["week"], current["week"] = 0, 1
                alternative = False
            items.append(current)
            continue
        if m := UNTIL_RE.match(line):
            current = {"days": days, "start": "", "end": _hhmm(m[1], m[2]), "title": (m[3] or "").strip(), "room": "",
                       "num": "", "teacher": "", "lesson": False, "week": None, "until": True}  # fmt: skip
            items.append(current)
            if current["title"]:
                current = None
            continue
        if m := RANGE_RE.match(line):
            items.append({"days": days, "start": _hhmm(m[1], m[2]), "end": _hhmm(m[3], m[4]), "title": m[5].strip(),
                          "room": "", "num": "", "teacher": "", "lesson": False, "week": None})  # fmt: skip
            current = None
            continue
        if m := AT_RE.match(line):
            items.append({"days": days, "start": _hhmm(m[1], m[2]), "end": "", "title": m[3].strip(),
                          "room": "", "num": "", "teacher": "", "lesson": False, "week": None})  # fmt: skip
            current = None
            continue
        if current is None:
            continue
        if not current["title"]:
            if current["lesson"] and (m := NUMBERED_RE.match(line)):
                current["num"] = {"З": "3", "з": "3", "O": "0", "О": "0"}.get(m[1], m[1])
                current["title"] = m[2].strip()
            else:
                current["title"] = line
            if not current["lesson"]:
                current = None
        elif current["lesson"] and not current["teacher"]:
            current["teacher"] = line
    # «До 16:00»: начало — конец предыдущего занятия в тот же день
    for item in items:
        if not item["start"]:
            before = [i for i in items if i["days"] == item["days"] and i["end"] and i["end"] <= item["end"] and i is not item]
            item["start"] = max((i["end"] for i in before), default="")
    for item in items:
        item["title"] = item["title"][:1].upper() + item["title"][1:]
    return [i for i in items if i["title"]]


# ---------- хранение и недели ----------


def monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


_cache: dict[int, tuple[float, dict | None]] = {}
CACHE_TTL = 600  # секунд: планировщик заглядывает каждые 5 минут, Notion лишний раз не дёргаем


async def load_cached(uid: int) -> dict | None:
    now = asyncio.get_running_loop().time()
    if uid in _cache and now - _cache[uid][0] < CACHE_TTL:
        return _cache[uid][1]
    data = await load(uid)
    _cache[uid] = (now, data)
    return data


async def load(uid: int) -> dict | None:
    raw = await notion.get_value(f"timetable:{uid}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def save(uid: int, data: dict) -> None:
    await notion.set_value(f"timetable:{uid}", json.dumps(data, ensure_ascii=False))
    _cache.pop(uid, None)


def week_index(data: dict, day: date) -> int:
    """0 — неделя первого варианта «ИЛИ», 1 — второго; считается от недели, отмеченной при сохранении."""
    anchor = date.fromisoformat(data["anchor"])
    return ((monday(day) - anchor).days // 7) % 2


def for_day(data: dict, day: date) -> list[dict]:
    """Занятия этого дня с учётом чередования, по времени."""
    weekday, week = WEEKDAYS[day.weekday()], week_index(data, day)
    items = [i for i in data["items"] if weekday in i["days"] and i["week"] in (None, week)]
    return sorted(items, key=lambda i: i["start"] or "00:00")


def alternatives(data: dict) -> list[tuple[dict, dict]]:
    """Пары уроков «через неделю»: (вариант 0, вариант 1)."""
    pairs = []
    for a in data["items"]:
        if a["week"] == 0:
            b = next((i for i in data["items"] if i["week"] == 1 and i["days"] == a["days"] and i["start"] == a["start"]), None)
            if b:
                pairs.append((a, b))
    return pairs


def swap(data: dict, k: int) -> None:
    """Поменять, какой из пары k идёт на неделе отсчёта."""
    a, b = alternatives(data)[k]
    a["week"], b["week"] = 1, 0


# ---------- звонок: за 5 минут до конца урока ----------


LESSON_MINUTES = 45  # онлайн-урок идёт по часу: 45 минут урок, с :45 — перемена до начала следующего часа


def _online(item: dict) -> bool:
    return (item.get("room") or "").lower() == "онлайн"


def _hhmm_of(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def segments(item: dict) -> list[tuple[int, int]]:
    """Части урока (начало, конец) в минутах. Онлайн-урок на 2 часа — два урока по 45 минут, каждый с начала часа."""
    start, end = _minutes(item["start"]), _minutes(item["end"])
    if not _online(item):
        return [(start, end)]
    return [(s, min(s + LESSON_MINUTES, end)) for s in range(start, end, 60)]


def _where(item: dict) -> list[str]:
    lines = []
    if item.get("room"):
        lines.append("💻 Онлайн" if _online(item) else f"🚪 Кабинет {item['room']}")
    if item.get("teacher"):
        lines.append(f"👩‍🏫 {item['teacher']}")
    return lines


def bell_text(current: dict, nxt: dict | None, after: dict | None, gap: int = 0, part: str = "") -> str:
    """Сообщение за 5 минут до конца урока: что дальше, где, у кого, сколько перемена.
    nxt — следующий урок (у продолжения того же урока — он сам, part — «2-й час»)."""
    head = f"🔔 Через 5 минут конец урока «{current['title']}»."
    if nxt:
        name = f"{(nxt['num'] + ' · ') if nxt.get('num') else ''}{nxt['title']}" + (f" ({part})" if part else "")
        start = nxt["_start"] if "_start" in nxt else nxt["start"]
        lines = [head, "", f"Дальше: {name}", *_where(nxt)]
        if gap <= 0:
            pause = "сразу, без перемены"
        elif gap <= 30:
            pause = f"перемена {gap} мин"
        else:
            pause = f"перерыв {gap // 60} ч {gap % 60} мин" if gap % 60 else f"перерыв {gap // 60} ч"
        lines.append(f"⏰ Начало в {start} · {pause}")
        return "\n".join(lines)
    text = head + "\nЭто последний урок на сегодня 🎉"
    if after:
        when = f"до {after['end']}" if after.get("until") or not after["start"] else after["start"]
        text += f"\nДальше: {after['title']}" + (f" ({when})" if when else "")
    return text


def due_bells(data: dict, now: datetime) -> list[tuple[str, str]]:
    """Звонки, которые пора отправить сейчас: [(ключ отметки, текст)]. Окно — последние 5 минут урока
    (у онлайн-урока — каждого часа: звонок в :40, урок кончается в :45)."""
    today = for_day(data, now.date())
    out = []
    minute = now.hour * 60 + now.minute
    for k, item in enumerate(today):
        if not (item["lesson"] and item["end"] and item["start"]):
            continue
        segs = segments(item)
        for n, (seg_start, seg_end) in enumerate(segs):
            if not seg_end - BELL_BEFORE.seconds // 60 <= minute < seg_end:
                continue
            key = f"bell:{now.date().isoformat()}:{_hhmm_of(seg_start)}"
            if n + 1 < len(segs):
                # тот же урок, следующий час
                cont = {**item, "_start": _hhmm_of(segs[n + 1][0])}
                out.append((key, bell_text(item, cont, None, segs[n + 1][0] - seg_end, f"{n + 2}-й час")))
                continue
            end = _minutes(item["end"])
            later = today[k + 1 :]
            nxt = next((i for i in later if i["lesson"] and i["start"] and _minutes(i["start"]) >= end), None)
            # следующий урок — только если между ними нет «внешнего» занятия (иначе уроки кончились)
            if nxt and any(not i["lesson"] and i["start"] and _minutes(i["start"]) < _minutes(nxt["start"]) for i in later):
                nxt = None
            after = next((i for i in later if not i["lesson"]), None) if not nxt else None
            gap = _minutes(nxt["start"]) - seg_end if nxt else 0
            out.append((key, bell_text(item, nxt, after, gap)))
    return out


# ---------- ссылка на занятие и «сделала домашку?» (репетиторы) ----------

JOIN_BEFORE = 5  # минут до начала — прислать ссылку на вход


def due_joins(data: dict, now: datetime) -> list[tuple[str, str]]:
    """За 5 минут до занятия со ссылкой — ссылка на вход."""
    out = []
    minute = now.hour * 60 + now.minute
    for item in for_day(data, now.date()):
        if not (item.get("link") and item["start"]):
            continue
        start = _minutes(item["start"])
        if start - JOIN_BEFORE <= minute < start:
            time = f"{item['start']}–{item['end']}" if item["end"] else item["start"]
            out.append((f"join:{now.date().isoformat()}:{item['start']}",
                        f"🔗 Через {start - minute} мин: {item['title']} ({time})\nВход: {item['link']}"))  # fmt: skip
    return out


def due_homework(data: dict, now: datetime) -> list[tuple[str, dict]]:
    """Сразу после занятия с отметкой «домашка» — вопрос «сделала домашку?» (в первые 30 минут после конца)."""
    out = []
    minute = now.hour * 60 + now.minute
    for item in for_day(data, now.date()):
        if item.get("homework") and item["end"] and 0 <= minute - _minutes(item["end"]) < 30:
            out.append((f"hw:{now.date().isoformat()}:{item['start']}", item))
    return out


# ---------- текст и основное расписание ----------


def _room(i: dict) -> str:
    """«каб. 410» или «онлайн»."""
    if not i.get("room"):
        return ""
    return "онлайн" if i["room"].lower() == "онлайн" else f"каб. {i['room']}"


def lesson_line(i: dict) -> str:
    if i.get("until"):
        time = f"до {i['end']}"
    else:
        time = f"{i['start']}–{i['end']}" if i["start"] and i["end"] else i["start"]
    bits = [f"{i['num']} · " if i["num"] else "", i["title"]]
    extra = " · ".join(x for x in (_room(i), i["teacher"]) if x)
    return f"{time} — {''.join(bits)}" + (f" ({extra})" if extra else "")


def week_text(data: dict, day: date) -> str:
    start = monday(day)
    lines = []
    for d in range(7):
        current = start + timedelta(days=d)
        items = for_day(data, current)
        if not items:
            continue
        lines.append(f"\n{WEEKDAYS[d]} {current:%d.%m}")
        lines += [("🎒 " if i["lesson"] else "📍 ") + lesson_line(i) for i in items]
    return "\n".join(lines).strip()


def main_slots(data: dict) -> list[dict]:
    """Строки для основного расписания: уроки и занятия по дням; пара «через неделю» — одной строкой «А / Б (чередуются)»."""
    pairs = {id(a): b for a, b in alternatives(data)}
    second = {id(b) for b in pairs.values()}
    slots = []
    for i in data["items"]:
        if id(i) in second:
            continue
        title = i["title"]
        if id(i) in pairs:
            title = f"{i['title']} / {pairs[id(i)]['title']} (чередуются через неделю)"
        if i["room"]:
            title += f" · {_room(i)}"
        slots.append({"title": SLOT_MARK + title, "days": i["days"], "start": i["start"], "end": i["end"]})
    return slots


async def sync_main_schedule(uid: int, data: dict) -> int:
    """Обновляет в основном расписании строки, сделанные из этого (с меткой 🎒): старые убирает, новые добавляет."""
    for slot in await notion.schedule_slots(uid):
        if slot["title"].startswith(SLOT_MARK):
            await notion.remove_slot(slot["page"])
    slots = main_slots(data)
    for s in slots:
        await notion.add_slot(uid, s["title"], KIND_REGULAR, s["start"], s["end"], days=s["days"])
    return len(slots)


def week_pdf(data: dict, day: date) -> bytes:
    """PDF недели (с понедельника недели day): уроки по дням, у чередующихся — что будет через неделю."""
    start = monday(day)
    days = [(start + timedelta(days=i), for_day(data, start + timedelta(days=i))) for i in range(7)]
    week = week_index(data, start)
    other = {}
    for a, b in alternatives(data):
        hidden = b if week == 0 else a
        for d in a["days"]:
            other[(WEEKDAYS.index(d), a["start"])] = hidden["title"]
    end = start + timedelta(days=6)
    return pdf.timetable_pdf(
        days, f"Моё расписание · {start:%d.%m}–{end:%d.%m}",
        (f"неделя {'А' if week == 0 else 'Б'} · " if alternatives(data) else "") + ("сиреневые — уроки, розовые — репетиторы, салатовые — вне школы" if any(is_tutor(i) for i in data["items"])
         else "сиреневые — уроки, салатовые — вне школы"), other,
    )  # fmt: skip


# ---------- разовая загрузка расписания владелицы из файла (она попросила внести его за неё) ----------

SEED_FILE = Path(__file__).resolve().parent.parent / "data" / "owner_timetable.txt"
SEED_ANCHOR = "2026-10-12"  # неделя, на которой идут первые варианты «ИЛИ»: вторник — химия, пятница — информатика


async def seed_owner(uid: int) -> str:
    """Один раз: если у владелицы ещё нет расписания, берём его из data/owner_timetable.txt."""
    if not uid or not SEED_FILE.exists() or await notion.get_value("seed:timetable:1"):
        return "не нужно"
    if await load(uid):
        await notion.set_value("seed:timetable:1", "уже было")
        return "уже было"
    items = parse(SEED_FILE.read_text(encoding="utf-8"))
    data = {"anchor": SEED_ANCHOR, "items": items, "bells": True}
    await save(uid, data)
    slots = await sync_main_schedule(uid, data)
    await notion.set_value("seed:timetable:1", f"{len(items)} занятий")
    return f"загружено: {len(items)} занятий, строк в основном расписании: {slots}"


# Занятия с репетиторами (строки «день - ЧЧ:ММ-ЧЧ:ММ предмет» в начале data/owner_timetable.txt): по часу,
# ссылка на вход за 5 минут (если есть) и «сделала домашку?» после. Ключ — предмет строчными.
TUTORS = {
    "китайский": "https://telemost.yandex.ru/j/61862118303727",
    "математика": "https://chattern.ru/duo?rid=D-KzCkJaQW",
    "гетало": "",  # ссылки на вход пока нет
    "обществознание": "https://m1qa3quv.ktalk.ru/opvjrcj0okbg",
}


def _tutors(data: dict) -> None:
    for item in data["items"]:
        # репетиторы записаны строкой дня без кабинета; школьные уроки — с «• онлайн»
        if item["lesson"] and not item["room"] and item["title"].lower() in TUTORS:
            if not item["end"]:
                item["end"] = f"{_minutes(item['start']) // 60 + 1:02d}:{item['start'][3:]}"
            item["tutor"] = True
            if TUTORS[item["title"].lower()]:
                item["link"] = TUTORS[item["title"].lower()]
            item["homework"] = True


def is_tutor(item: dict) -> bool:
    return bool(item.get("tutor") or item.get("link"))


SEED_VERSION = 9  # растёт, когда владелица присылает новое расписание: тогда оно заменяет загруженное


async def reload_owner(uid: int) -> str:
    """Новая версия расписания из файла заменяет загруженную (репетиторы — из _tutors, звонки — как были)."""
    if not uid or not SEED_FILE.exists():
        return "не нужно"
    if int(await notion.get_value("seed:timetable:version") or 0) >= SEED_VERSION:
        return "не нужно"
    old = await load(uid) or {}
    data = {"anchor": SEED_ANCHOR, "items": parse(SEED_FILE.read_text(encoding="utf-8")), "bells": old.get("bells", True)}
    _tutors(data)
    await save(uid, data)
    slots = await sync_main_schedule(uid, data)
    for key in ("seed:timetable:1", "seed:timetable:3"):
        await notion.set_value(key, "заменено новой версией")
    await notion.set_value("seed:timetable:version", str(SEED_VERSION))
    return f"загружено: {len(data['items'])} занятий, строк в основном расписании: {slots}"


async def upgrade_owner(uid: int) -> str:
    """Один раз: занятия с репетиторами (пн, ср) — по часу, ссылки на вход, вопрос про домашку."""
    if not uid or await notion.get_value("seed:timetable:3"):
        return "не нужно"
    data = await load(uid)
    if not data:
        return "расписания нет"
    _tutors(data)
    await save(uid, data)
    await sync_main_schedule(uid, data)
    await notion.set_value("seed:timetable:3", "ok")
    return "репетиторы: по часу, ссылки, домашка"
