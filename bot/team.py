"""👥 Командные проекты: ИИ-сводка по заметкам всех участников (по кнопке и раз в неделю)."""

import asyncio
import logging
from datetime import datetime

from telegram import Bot
from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup
from telegram.constants import ParseMode

from . import ai, notion
from . import config as c
from .markdown import chunks, tg_html, to_blocks, toggle

log = logging.getLogger("team")

SUMMARY_LIMIT = 60  # сколько последних заметок читает сводка по кнопке


def note_text(item: dict, note: dict) -> str:
    author = f", автор: {item['author']}" if item.get("author") else ""
    head = f"### «{item['title']}» ({item['created']}{author})"
    return "\n".join(p for p in (head, note["summary"], note["details"]) if p)


async def summary(project: dict, uid: int, days: int | None = None) -> tuple[str, int, bool]:
    """(сводка в Markdown, сколько заметок прочитано, обрезано ли). Пустая строка — заметок нет."""
    items = await notion.notes_in_scope(uid, project=project["name"], days=days, limit=SUMMARY_LIMIT)
    if not items:
        return "", 0, False
    limiter = asyncio.Semaphore(3)  # Notion разрешает около 3 запросов в секунду

    async def read(item: dict) -> str:
        async with limiter:
            return note_text(item, await notion.read_note(item["id"]))

    notes = await asyncio.gather(*(read(i) for i in items))
    text, truncated = await ai.project_summary(list(notes), project["name"], project.get("description", ""))
    return text, len(items), truncated


def summary_messages(project: dict, text: str, count: int, title: str) -> list[str]:
    header = f"🤖 <b>{title} · «{project['name']}»</b> ({count} заметок)\n\n"
    parts = chunks(tg_html(text))
    return [(header if i == 0 else "") + part for i, part in enumerate(parts)]


def feed_markup(project_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[Btn("📰 Лента проекта", callback_data=f"fd:{project_id}")]])


async def weekly(bot: Bot, uid: int, now: datetime, week: str, is_done, mark_done) -> str:
    """Понедельник: сводка за неделю по каждому командному проекту человека. Сводка проекта считается один раз
    (первым, у кого наступило утро понедельника) и уходит сразу всем его участникам, а ещё дописывается
    на страницу проекта в Notion."""
    sent = []
    allowed = {m["tg"] for m in await notion.members()} | {c.OWNER_ID}
    for project in await notion.project_list(uid):
        if not notion.is_team_member(project, uid):
            continue
        key = f"teamsum:{project['id']}:{week}"
        if await is_done(key):
            continue
        await mark_done(key)
        try:
            text, count, _ = await summary(project, uid, days=7)
        except Exception:
            log.exception("team summary failed")
            continue
        if not text:
            continue
        try:
            await notion._append(project["id"], [toggle(f"🤖 Сводка за неделю до {now:%d.%m}", to_blocks(text)[:100])])
        except Exception:
            log.exception("summary to notion failed")
        messages = summary_messages(project, text, count, "Сводка за неделю")
        for person in notion.team_people(project):
            if person not in allowed:
                continue
            try:
                for i, part in enumerate(messages):
                    last = i == len(messages) - 1
                    await bot.send_message(person, part, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                                           reply_markup=feed_markup(project["id"]) if last else None)  # fmt: skip
            except Exception:
                log.exception("send team summary to %s failed", person)
        sent.append(project["name"])
    return "сводки: " + ", ".join(sent) if sent else "нет новых заметок в командных проектах"
