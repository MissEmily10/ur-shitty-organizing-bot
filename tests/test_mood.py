"""Этап 8: вопрос о настроении, комментарий, недельный отчёт (картинка, Notion, вывод ИИ), /report, настройка."""

import asyncio
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import harness as h

from bot import ai, main, notion, report, scheduler

MSK = ZoneInfo("Europe/Moscow")
reports: list[dict] = []
uploads: list[bytes] = []


async def review_items(uid):
    return []


async def dated_items(uid, before, after=None):
    return []


async def forget_old_marks(days=14):
    return 0


async def notes_created(uid, start, end):
    # 23:30 UTC 5-го — это уже 6-е по Москве
    return [
        {"id": "a", "title": "x", "type": "💡 Идея", "created_at": "2026-10-05T08:00:00.000Z"},
        {"id": "b", "title": "y", "type": None, "created_at": "2026-10-05T23:30:00.000Z"},
        {"id": "c", "title": "z", "type": "📋 Задача", "created_at": "2026-10-06T10:00:00.000Z"},
    ]


async def done_between(uid, start, end):
    return [{"when": "2026-10-06T15:00:00+03:00"}]


async def upload_image(data, name):
    uploads.append(data)
    return "IMG"


async def create_report(uid, title, average, rows, conclusion, image_id):
    reports.append({"title": title, "avg": average, "rows": rows, "image": image_id})
    return "https://notion.so/report"


async def week_conclusion(rows, previous):
    return "Лучший день — понедельник."


def utc(d, hh, mm=0):
    return datetime(2026, 10, d, hh, mm, tzinfo=timezone.utc)


async def run():
    notion.review_items, notion.dated_items, notion.forget_old_marks = review_items, dated_items, forget_old_marks
    notion.notes_created, notion.done_between, notion.upload_image, notion.create_report = notes_created, done_between, upload_image, create_report
    ai.week_conclusion = week_conclusion
    app = await h.make_app()
    bot = app.bot

    def asks():
        return [t for t in h.texts(chat=42) if t.startswith("😊 Как настроение")]

    # Вопрос — вместе с вечерним разбором (20:00 МСК = 17:00 UTC), раз в день
    await scheduler.tick(bot, [42], now=utc(7, 16, 55))
    assert not asks()
    await scheduler.tick(bot, [42], now=utc(7, 17, 0))
    await scheduler.tick(bot, [42], now=utc(7, 17, 5))
    assert len(asks()) == 1

    # Ответ: оценка → просьба о комментарии → комментарий
    await app.process_update(h.callback("md:2026-10-07:4"))
    assert h.moods[(42, "2026-10-07")][0] == 4 and "🙂 4 из 5" in h.texts(chat=42)[-2]
    await app.process_update(h.text("продуктивно", reply_to=h._message(h.texts(chat=42)[-1])))
    assert h.moods[(42, "2026-10-07")] == [4, "продуктивно"] and "Записала" in h.texts(chat=42)[-1]

    # Уже отмечено — на следующий день сначала спросим, а если отметили заранее — нет
    h.moods[(42, "2026-10-08")] = [3, ""]
    await scheduler.tick(bot, [42], now=utc(8, 17, 0))
    assert len(asks()) == 1
    # Выключено в настройках
    await app.process_update(h.callback("st:mood"))
    assert "выключен" in h.texts(chat=42)[-1]
    await scheduler.tick(bot, [42], now=utc(9, 17, 0))
    assert len(asks()) == 1
    await app.process_update(h.callback("st:mood"))

    # Сборка недели: группировка по местной дате и средние
    h.moods.update({(42, "2026-10-05"): [5, "выспалась"], (42, "2026-10-06"): [3, ""], (42, "2026-09-29"): [2, ""]})
    week = await report.collect(42, date(2026, 10, 5), MSK)
    mon, tue = week.days[0], week.days[1]
    assert (mon.notes, mon.sorted, tue.notes, tue.sorted, tue.done) == (1, 1, 2, 1, 1), week.days[:2]
    assert round(week.average, 2) == round((5 + 3 + 4 + 3) / 4, 2) and week.previous == 2, (week.average, week.previous)
    assert report.render_png(week)[:4] == b"\x89PNG"

    # Отчёт в понедельник 10:00 МСК (07:00 UTC 12-го) за прошлую неделю
    before = len(h.sent)
    await scheduler.tick(bot, [42], now=datetime(2026, 10, 12, 6, 55, tzinfo=timezone.utc))
    assert not [e for e, _ in h.sent[before:] if e == "sendPhoto"]
    await scheduler.tick(bot, [42], now=datetime(2026, 10, 12, 7, 0, tzinfo=timezone.utc))
    photos = [d for e, d in h.sent[before:] if e == "sendPhoto"]
    assert len(photos) == 1, h.sent[before:]
    caption = photos[0]["caption"]
    assert "Неделя 05.10" in caption and "Настроение: 3,8 (было 2,0)" in caption and "Лучший день" in caption and "notion.so/report" in caption, caption
    assert reports[-1]["image"] == "IMG" and reports[-1]["rows"][0][0] == "День" and len(reports[-1]["rows"]) == 9

    # /report по запросу
    await app.process_update(h.text("/report"))
    before = len(h.sent)
    await app.process_update(h.callback("rp:prev"))
    assert [e for e, _ in h.sent[before:] if e == "sendPhoto"]
    print("mood OK")


asyncio.run(run())
