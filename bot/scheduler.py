"""Планировщик. cron-job.org раз в 5 минут открывает /tick/<секрет>, и на каждом «тике» бот сам решает,
кому и что пора отправить, по часовому поясу и настройкам каждого человека.

Каждая задача отправляется один раз: отметка «сделано» хранится в Notion (таблица «Служебное бота»),
поэтому повторный тик или перезапуск бота не приводят к дублям.
Новые задачи по времени (напоминания, чек-ины, настроение) добавляются в JOBS."""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from html import escape
from datetime import datetime, time, timedelta, timezone, tzinfo
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from telegram import Bot
from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup

from . import notion, styles, team, weekly
from .whenparse import human

log = logging.getLogger("scheduler")

DEFAULT_SETTINGS = {
    "tz": "Europe/Moscow",
    "evening": "20:00",
    "checkins": ["12:00", "14:00", "16:00", "18:00"],
    "mood": True,
}
SETTINGS_TTL = 3600  # секунд держим настройки в памяти, чтобы не спрашивать Notion на каждом тике

_settings_cache: dict[int, tuple[float, dict]] = {}
_done_cache: set[str] = set()
_tick_lock = asyncio.Lock()


# ---------- настройки человека ----------


def parse_tz(value: str) -> tzinfo | None:
    """«Europe/Moscow», «UTC+3», «+5:30», «-4» → часовой пояс; None, если не понятно."""
    value = value.strip()
    try:
        return ZoneInfo(value)
    except Exception:
        pass
    m = re.fullmatch(r"(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?", value, re.I)
    if not m:
        return None
    hours, minutes = int(m.group(2)), int(m.group(3) or 0)
    if hours > 14 or minutes >= 60:
        return None
    delta = timedelta(hours=hours, minutes=minutes)
    return timezone(delta if m.group(1) == "+" else -delta)


def parse_time(value: str) -> str | None:
    """«21», «21:30», «9.15» → «21:00», «21:30», «09:15»."""
    m = re.fullmatch(r"\s*(\d{1,2})(?:[:.](\d{2}))?\s*", value)
    if not m or int(m.group(1)) > 23 or int(m.group(2) or 0) > 59:
        return None
    return f"{int(m.group(1)):02d}:{int(m.group(2) or 0):02d}"


async def get_settings(uid: int) -> dict:
    loop = asyncio.get_running_loop()
    cached = _settings_cache.get(uid)
    if cached and loop.time() - cached[0] < SETTINGS_TTL:
        return cached[1]
    raw = await notion.get_value(f"settings:{uid}")
    settings = {**DEFAULT_SETTINGS, **(json.loads(raw) if raw else {})}
    _settings_cache[uid] = (loop.time(), settings)
    return settings


async def update_settings(uid: int, **changes) -> dict:
    settings = {**await get_settings(uid), **changes}
    await notion.set_value(f"settings:{uid}", json.dumps(settings, ensure_ascii=False))
    _settings_cache[uid] = (asyncio.get_running_loop().time(), settings)
    return settings


def local_now(settings: dict, now: datetime | None = None) -> datetime:
    tz = parse_tz(settings["tz"]) or ZoneInfo(DEFAULT_SETTINGS["tz"])
    return (now or datetime.now(timezone.utc)).astimezone(tz)


# ---------- отметки «уже сделано» ----------


async def is_done(key: str) -> bool:
    if key in _done_cache:
        return True
    if await notion.get_value(f"done:{key}") is not None:
        _done_cache.add(key)
        return True
    return False


async def mark_done(key: str) -> None:
    _done_cache.add(key)
    await notion.set_value(f"done:{key}", datetime.now(timezone.utc).isoformat())


# ---------- задачи ----------


@dataclass
class Job:
    name: str
    due: Callable[[datetime, dict], str | None]  # (местное время, настройки) → ключ периода, если пора, иначе None
    run: Callable[[Bot, int, datetime], Awaitable[str]]  # (бот, человек, его местное время) → строка для отчёта


def _evening_due(now: datetime, settings: dict) -> str | None:
    """Пора, если местное время уже после времени разбора. Ключ — местная дата: раз в день."""
    hh, mm = map(int, settings["evening"].split(":"))
    return now.date().isoformat() if now.time() >= time(hh, mm) else None


async def overdue(uid: int, settings: dict, now: datetime | None = None) -> list[dict]:
    """Невыполненные заметки, срок которых уже прошёл (дата без времени — просрочена со следующего дня)."""
    now = now or local_now(settings)
    items = await notion.dated_items(uid, before=now.isoformat())
    return [i for i in items if _deadline(i["when"], now) < now]


def _deadline(iso: str, now: datetime) -> datetime:
    """Момент, после которого срок считается прошедшим: для даты без времени — конец того дня."""
    if "T" in iso:
        return datetime.fromisoformat(iso).astimezone(now.tzinfo)
    return datetime.combine(datetime.fromisoformat(iso).date() + timedelta(days=1), time(0, 0), now.tzinfo)


async def _evening_run(bot: Bot, uid: int, now: datetime) -> str:
    items = await notion.review_items(uid)
    late = await overdue(uid, await get_settings(uid), now)
    if not items and not late:
        return "разбирать нечего"
    lines, buttons = [], []
    if items:
        lines.append(f"🌙 Вечерний разбор: заметок без категории {len(items)}")
        buttons.append(Btn("Разобрать", callback_data="r:0"))
    if late:
        lines.append(f"🔥 Просрочено: {len(late)}")
        buttons.append(Btn("🔥 Показать", callback_data="ov"))
    text = await styles.wrap(uid, "evening", "\n".join(lines))
    await bot.send_message(uid, text, reply_markup=InlineKeyboardMarkup([buttons]))
    return f"пуш: разбор {len(items)}, просрочено {len(late)}"


# ---------- напоминания о сроках ----------


def reminder_markup(page_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[Btn("✅ Готово", callback_data=f"dl:ok:{page_id}"), Btn("⏰ Перенести", callback_data=f"dl:sn:{page_id}")]]
    )

REMINDER_WINDOW = timedelta(hours=3)  # напоминание, пропущенное дольше этого (бот лежал), уже не шлём


def reminder_points(item: dict, now: datetime) -> list[tuple[str, datetime, str]]:
    """Когда и что напомнить: (вид, момент, текст). У «Напоминания» — ровно в срок; у остального — за день и в день."""
    iso = item["when"]
    kind = (item.get("type") or item.get("ai_type") or "").lower()
    if "T" in iso:
        at = datetime.fromisoformat(iso).astimezone(now.tzinfo)
        if "напомин" in kind:
            return [("now", at, "⏰ Напоминание")]
        return [("day", at - timedelta(days=1), "📌 Завтра срок"), ("hour", at - timedelta(hours=1), "📌 Через час срок")]
    day = datetime.fromisoformat(iso).date()
    return [
        ("day", datetime.combine(day - timedelta(days=1), time(10, 0), now.tzinfo), "📌 Завтра срок"),
        ("morning", datetime.combine(day, time(9, 0), now.tzinfo), "📌 Сегодня срок"),
    ]


async def _reminders(bot: Bot, uid: int, now: datetime, settings: dict) -> list[str]:
    local = local_now(settings, now)
    items = await notion.dated_items(
        uid, before=(local + timedelta(days=1, hours=1)).isoformat(), after=(local - timedelta(days=2)).isoformat()
    )
    sent = []
    for item in items:
        for kind, at, label in reminder_points(item, local):
            if not (at <= local < at + REMINDER_WINDOW):
                continue
            # В ключе есть сам срок: если срок перенесли, напоминания по новому сроку придут заново
            key = f"rem:{item['id']}:{kind}:{item['when']}"
            if await is_done(key):
                continue
            await mark_done(key)
            await bot.send_message(
                uid,
                await styles.wrap(uid, "reminder", f'{label}: <a href="{item["url"]}">{escape(item["title"])}</a> — {escape(human(item["when"], local))}', html=True),
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=reminder_markup(item["id"]),
            )
            sent.append(f"{kind} {item['title'][:20]}")
    return sent


# ---------- ☀️ дневные чек-ины ----------

CHECKIN_WINDOW = timedelta(hours=1)  # чек-ин, пропущенный дольше часа (бот лежал), уже не шлём


def _minutes(hhmm: str) -> int:
    hh, mm = map(int, hhmm.split(":"))
    return hh * 60 + mm


def _checkin_due(now: datetime, settings: dict) -> str | None:
    """Последний наступивший слот чек-ина за последний час. Слот рядом с вечерним разбором пропускаем:
    вечером и так придёт разбор."""
    minute = now.hour * 60 + now.minute
    evening = _minutes(settings["evening"])
    passed = [
        t for t in settings.get("checkins", [])
        if 0 <= minute - _minutes(t) < CHECKIN_WINDOW.seconds // 60 and abs(_minutes(t) - evening) > 30
    ]  # fmt: skip
    return f"{now.date().isoformat()}T{max(passed)}" if passed else None


def checkin_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [Btn("⚡ Быстро", callback_data="ci:quick"), Btn("📦 Пачкой", callback_data="ci:batch")],
            [Btn("🌙 На вечер", callback_data="ci:later"), Btn("🔕 Сегодня хватит", callback_data="ci:off")],
        ]
    )


async def _checkin_run(bot: Bot, uid: int, now: datetime) -> str:
    today = now.date().isoformat()
    if await notion.get_value(f"checkin_off:{uid}:{today}") is not None:
        return "выключены на сегодня"
    items = await notion.review_items(uid)
    if not items:
        return "разбирать нечего"
    await bot.send_message(
        uid,
        await styles.wrap(uid, "checkin",
            f"☀️ Неразобранных заметок: {len(items)}. Сколько у тебя сейчас времени?\n"
            "⚡ пара минут — пробежимся по актуальности\n📦 побольше — разберём пачкой\n🌙 нет — всё подождёт вечера"),
        reply_markup=checkin_markup(),
    )
    return f"чек-ин, заметок {len(items)}"


async def checkins_off_today(uid: int) -> None:
    today = local_now(await get_settings(uid)).date().isoformat()
    await notion.set_value(f"checkin_off:{uid}:{today}", "1")


# ---------- 🗓 пересмотр расписания ----------

REVIEW_AT = time(11, 0)  # во сколько по местному времени предлагать пересмотр


def _review_due(now: datetime, settings: dict) -> str | None:
    """Неделя и две недели — в воскресенье, месяц — 1-го числа; всё в 11:00 местного времени."""
    mode = settings.get("schedule_review", "week")
    if mode == "off" or now.time() < REVIEW_AT:
        return None
    if mode == "month":
        return now.strftime("%Y-%m") if now.day == 1 else None
    if now.weekday() != 6:
        return None
    week = now.isocalendar().week
    if mode == "2weeks" and week % 2:
        return None
    return f"{now.isocalendar().year}-W{week}"


async def _review_run(bot: Bot, uid: int, now: datetime) -> str:
    slots = await notion.schedule_slots(uid)
    if not any(s["kind"] == notion.KIND_REGULAR for s in slots):
        return "базового расписания нет"
    monthly = (await get_settings(uid)).get("schedule_review") == "month"
    period = "месяц" if monthly else "неделю"
    await bot.send_message(
        uid,
        f"🗓 Пора пересмотреть расписание на {period}. Всё как обычно или есть изменения?",
        reply_markup=InlineKeyboardMarkup(
            [
                [Btn("✅ Всё как есть", callback_data="sc:keep"), Btn("✏️ Изменить", callback_data="sc:edit")],
                # в воскресенье — следующая неделя; 1-го числа — уже наступивший месяц
                [Btn("📄 PDF на " + period, callback_data="sc:pdfm:0" if monthly else "sc:pdfw:1")],
            ]
        ),
    )
    return "предложен пересмотр"


# ---------- 😊 настроение и 📊 недельный отчёт ----------

MOOD_BUTTONS = [("😞", 1), ("😕", 2), ("😐", 3), ("🙂", 4), ("🤩", 5)]
REPORT_AT = time(10, 0)  # отчёт за прошлую неделю — в понедельник утром, когда воскресенье уже отмечено


def mood_markup(day: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[Btn(e, callback_data=f"md:{day}:{n}") for e, n in MOOD_BUTTONS]])


def _mood_due(now: datetime, settings: dict) -> str | None:
    """Вместе с вечерним разбором, раз в день, если не выключено в настройках."""
    return _evening_due(now, settings) if settings.get("mood", True) else None


async def _mood_run(bot: Bot, uid: int, now: datetime) -> str:
    day = now.date().isoformat()
    if (await notion.moods(uid)).get(day, (None, ""))[0]:
        return "уже отмечено"
    await bot.send_message(uid, await styles.wrap(uid, "mood", "😊 Как настроение сегодня?"), reply_markup=mood_markup(day))
    return "спросили настроение"


def _report_due(now: datetime, settings: dict) -> str | None:
    if now.weekday() != 0 or now.time() < REPORT_AT:
        return None
    return f"{now.isocalendar().year}-W{now.isocalendar().week}"


async def _report_run(bot: Bot, uid: int, now: datetime) -> str:
    monday = now.date() - timedelta(days=7)
    return await weekly.send_report(bot, uid, monday, now.tzinfo, automatic=True)


async def _team_run(bot: Bot, uid: int, now: datetime) -> str:
    """👥 Сводки командных проектов за неделю — в то же утро понедельника, что и отчёт."""
    return await team.weekly(bot, uid, now, _report_due(now, {}) or now.date().isoformat(), is_done, mark_done)


JOBS = [
    Job("evening", _evening_due, _evening_run),
    Job("mood", _mood_due, _mood_run),
    Job("checkin", _checkin_due, _checkin_run),
    Job("schedule_review", _review_due, _review_run),
    Job("weekly_report", _report_due, _report_run),
    Job("team_summary", _report_due, _team_run),
]


async def tick(bot: Bot, user_ids: list[int], now: datetime | None = None, force: str | None = None) -> str:
    """Один проход планировщика по всем людям и задачам. force=<имя задачи> — выполнить её сейчас без отметки
    (для проверки владелицей)."""
    async with _tick_lock:
        report = []
        for uid in user_ids:
            try:
                settings = await get_settings(uid)
                current = local_now(settings, now)
                for job in JOBS:
                    if force:
                        if job.name == force:
                            report.append(f"{uid} {job.name}: {await job.run(bot, uid, current)} (проверка)")
                        continue
                    period = job.due(current, settings)
                    if not period:
                        continue
                    key = f"{job.name}:{uid}:{period}"
                    if await is_done(key):
                        continue
                    # Отметку ставим до отправки: лучше в редком сбое не отправить, чем отправить дважды
                    await mark_done(key)
                    report.append(f"{uid} {job.name}: {await job.run(bot, uid, current)}")
                if not force:
                    for item in await _reminders(bot, uid, now or datetime.now(timezone.utc), settings):
                        report.append(f"{uid} напоминание: {item}")
            except Exception as e:
                # Один заблокировавший бота человек или сбой Notion не должен оставить остальных без уведомлений
                log.exception("tick failed for %s", uid)
                report.append(f"{uid}: ошибка {e}")
        if not force:
            day = (now or datetime.now(timezone.utc)).date().isoformat()
            if not await is_done(f"cleanup:{day}"):
                await mark_done(f"cleanup:{day}")
                try:
                    report.append(f"очистка: {await notion.forget_old_marks()}")
                except Exception:
                    log.exception("cleanup failed")
        return "; ".join(report) or "ничего не пора"
