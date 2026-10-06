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

from . import notion
from .whenparse import human

log = logging.getLogger("scheduler")

DEFAULT_SETTINGS = {"tz": "Europe/Moscow", "evening": "20:00"}
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
    run: Callable[[Bot, int], Awaitable[str]]  # отправить человеку, вернуть строку для отчёта


def _evening_due(now: datetime, settings: dict) -> str | None:
    """Пора, если местное время уже после времени разбора. Ключ — местная дата: раз в день."""
    hh, mm = map(int, settings["evening"].split(":"))
    return now.date().isoformat() if now.time() >= time(hh, mm) else None


async def overdue(uid: int, settings: dict) -> list[dict]:
    """Невыполненные заметки, срок которых уже прошёл (дата без времени — просрочена со следующего дня)."""
    now = local_now(settings)
    items = await notion.dated_items(uid, before=now.isoformat())
    return [i for i in items if _deadline(i["when"], now) < now]


def _deadline(iso: str, now: datetime) -> datetime:
    """Момент, после которого срок считается прошедшим: для даты без времени — конец того дня."""
    if "T" in iso:
        return datetime.fromisoformat(iso).astimezone(now.tzinfo)
    return datetime.combine(datetime.fromisoformat(iso).date() + timedelta(days=1), time(0, 0), now.tzinfo)


async def _evening_run(bot: Bot, uid: int) -> str:
    items = await notion.review_items(uid)
    late = await overdue(uid, await get_settings(uid))
    if not items and not late:
        return "разбирать нечего"
    lines, buttons = [], []
    if items:
        lines.append(f"🌙 Вечерний разбор: заметок без категории {len(items)}")
        buttons.append(Btn("Разобрать", callback_data="r:0"))
    if late:
        lines.append(f"🔥 Просрочено: {len(late)}")
        buttons.append(Btn("🔥 Показать", callback_data="ov"))
    await bot.send_message(uid, "\n".join(lines), reply_markup=InlineKeyboardMarkup([buttons]))
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
                f'{label}: <a href="{item["url"]}">{escape(item["title"])}</a> — {escape(human(item["when"], local))}',
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=reminder_markup(item["id"]),
            )
            sent.append(f"{kind} {item['title'][:20]}")
    return sent


JOBS = [Job("evening", _evening_due, _evening_run)]


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
                            report.append(f"{uid} {job.name}: {await job.run(bot, uid)} (проверка)")
                        continue
                    period = job.due(current, settings)
                    if not period:
                        continue
                    key = f"{job.name}:{uid}:{period}"
                    if await is_done(key):
                        continue
                    # Отметку ставим до отправки: лучше в редком сбое не отправить, чем отправить дважды
                    await mark_done(key)
                    report.append(f"{uid} {job.name}: {await job.run(bot, uid)}")
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
