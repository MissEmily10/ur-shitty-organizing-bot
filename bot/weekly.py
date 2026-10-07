"""Отправка недельного отчёта: картинка в Telegram, страница в Notion, вывод ИИ."""

import io
import logging
from datetime import date

from telegram import Bot

from . import ai, notion, report
from .markdown import to_blocks

log = logging.getLogger("weekly")


async def send_report(bot: Bot, uid: int, monday: date, tz, automatic: bool = False) -> str:
    week = await report.collect(uid, monday, tz)
    notes, sorted_, done = week.totals()
    if automatic and not week.average and not notes:
        # Пустую неделю не присылаем сами: нечего показывать
        return "пустая неделя"
    rows = report.table_rows(week)
    try:
        conclusion = await ai.week_conclusion(rows, week.previous)
    except Exception:
        log.exception("week conclusion failed")
        conclusion = ""
    image = report.render_png(week)
    url = None
    try:
        image_id = await notion.upload_image(image, f"week-{monday.isoformat()}.png")
        url = await notion.create_report(uid, week.title, week.average, rows, to_blocks(conclusion), image_id)
    except Exception:
        # Отчёт в Telegram важнее страницы в Notion
        log.exception("report page failed")
    avg = report._num(week.average) if week.average else "—"
    trend = ""
    if week.average and week.previous:
        trend = f" (было {report._num(week.previous)})"
    caption = (
        f"📊 {week.title}\n"
        f"Настроение: {avg}{trend}  {report.mood_line(week)}\n"
        f"Заметок {notes} · разобрано {sorted_} · выполнено {done}"
    )
    if conclusion:
        caption += f"\n\n{conclusion}"
    if url:
        caption += f"\n\n📄 Полный отчёт: {url}"
    await bot.send_photo(uid, io.BytesIO(image), caption=caption[:1024])
    return f"отчёт: настроение {avg}, заметок {notes}"
