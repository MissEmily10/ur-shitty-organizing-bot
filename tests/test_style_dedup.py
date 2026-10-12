"""Реплика стиля не дублирует обычную фразу бота: где фраза — просто обёртка (настроение, всё разобрано, пусто,
сохранено, вечерний разбор, чек-ин, напоминание), стиль её заменяет; смысловые строки остаются."""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import harness as h

from bot import notion, scheduler, styles

KAWAII = styles.BUILTIN["kawaii"]["pack"]


async def run():
    app = await h.make_app()
    await scheduler.update_settings(42, style="kawaii")
    now = datetime(2026, 10, 12, 20, 5, tzinfo=ZoneInfo("Europe/Moscow"))

    # настроение: одна фраза вместо двух
    await scheduler._mood_run(app.bot, 42, now)
    mood = h.texts(chat=42)[-1]
    assert mood in KAWAII["mood"] and "Как настроение сегодня" not in mood, mood

    # вечерний разбор и чек-ин: обёртка заменена, цифры остались
    items = [{"id": "1" * 32, "title": "x", "url": "u", "project": None, "when": None, "ai_type": None}]

    async def review_items(uid):
        return items

    async def none(*a, **k):
        return []
    notion.review_items, notion.dated_items = review_items, none
    await scheduler._evening_run(app.bot, 42, now)
    evening = h.texts(chat=42)[-1].split("\n")
    assert evening[0] in KAWAII["evening"] and evening[1] == "Заметок без категории: 1" and "Вечерний разбор" not in evening[0]
    await scheduler._checkin_run(app.bot, 42, now)
    checkin = h.texts(chat=42)[-1].split("\n")
    assert checkin[0] in KAWAII["checkin"] and checkin[1].startswith("Неразобранных заметок: 1"), checkin

    # всплывашка «Сохранено» — одна реплика стиля
    await app.process_update(h.callback("st:tn"))
    toast = next(d["text"] for e, d in reversed(h.sent) if e == "answerCallbackQuery")
    assert toast in KAWAII["ok"], toast

    # «всё разобрано» — одна фраза
    items.clear()
    await app.process_update(h.text("/razbor"))
    assert h.texts(chat=42)[-1] in KAWAII["all_done"]

    # напоминание: реплика вместо «📌 Завтра срок», дата остаётся
    reminder = scheduler.reminder_points({"when": "2026-10-13T15:00:00+03:00", "type": "📋 Задача"}, now)
    assert reminder[0][2] == "📌 Завтра срок"
    text = await styles.wrap(42, "reminder", "📌 Завтра срок\nПозвонить — завтра 15:00", replace=True)
    first, second = text.split("\n")
    assert first in KAWAII["reminder"] and second == "Позвонить — завтра 15:00"

    # вопрос бота: реплика не повторяет «ответьте на это сообщение»
    for phrase in KAWAII["ask"]:
        assert "сообщени" not in phrase and "ответ" not in phrase.lower(), phrase

    # без стиля — всё как было
    await scheduler.update_settings(42, style="standard")
    await scheduler._mood_run(app.bot, 42, datetime(2026, 10, 13, 20, 5, tzinfo=ZoneInfo("Europe/Moscow")))
    assert h.texts(chat=42)[-1] == "😊 Как настроение сегодня?"
    print("style dedup OK")


asyncio.run(run())
