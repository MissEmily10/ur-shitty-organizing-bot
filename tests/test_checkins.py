"""Этап 6: дневные чек-ины — когда приходят, ⚡ быстро, 📦 пачкой, 🔕, настройки."""

import asyncio
import re
from datetime import datetime, timezone

import harness as h

from bot import main, notion, scheduler

notes: dict[str, dict] = {}
log: list[tuple] = []


def note(i, title, project=None):
    pid = f"{i:032d}"
    notes[pid] = {"id": pid, "title": title, "url": f"https://n/{pid}", "project": project, "type": None,
                  "author_id": 42, "when": None, "done": False, "ai_type": None}


async def review_items(uid):
    return [dict(n) for n in notes.values() if not n["type"]]


async def page_info(pid):
    return dict(notes[pid])


async def trash(pid):
    log.append(("trash", pid))
    notes.pop(pid, None)


async def file_to_project(pid, name):
    log.append(("project", pid, name))
    notes[pid]["project"] = name


async def set_type(pid, name):
    log.append(("type", pid, name))
    notes[pid]["type"] = name


async def projects(*a):
    return ["Сайт", "Фоны"]


async def dated_items(uid, before, after=None):
    return []


async def forget_old_marks(days=14):
    return 0


def plain(t):
    return re.sub("<[^>]+>", "", t)


def buttons():
    for e, d in reversed(h.sent):
        m = d.get("reply_markup")
        if hasattr(m, "inline_keyboard"):
            return [b.text for row in m.inline_keyboard for b in row]
    return []


def utc(hh, mm=0, day=7):
    return datetime(2026, 10, day, hh, mm, tzinfo=timezone.utc)


async def run():
    notion.review_items, notion.page_info, notion.trash, notion.file_to_project = review_items, page_info, trash, file_to_project
    notion.set_type, notion.projects, notion.dated_items, notion.forget_old_marks = set_type, projects, dated_items, forget_old_marks
    app = await h.make_app()
    bot = app.bot
    for i, t in enumerate(["Купить краски", "Идея фона", "Позвонить Ане", "Шрифты", "Референс света"], 1):
        note(i, t)

    def checkins():
        return [t for t in h.texts(chat=42) if t.startswith("☀️")]

    # Когда приходят: 12:00 МСК = 09:00 UTC
    await scheduler.tick(bot, [42], now=utc(8, 55))
    assert not checkins()
    await scheduler.tick(bot, [42], now=utc(9, 0))
    assert len(checkins()) == 1 and "Неразобранных заметок: 5" in checkins()[-1]
    await scheduler.tick(bot, [42], now=utc(9, 5))
    assert len(checkins()) == 1, "без дублей"
    await scheduler.tick(bot, [42], now=utc(11, 30))  # 14:30
    assert len(checkins()) == 2
    await scheduler.tick(bot, [42], now=utc(14, 20))  # 17:20 — слот 16:00 пропущен больше часа назад
    assert len(checkins()) == 2

    # 🔕 сегодня хватит
    await app.process_update(h.callback("ci:off"))
    assert any(k.startswith("checkin_off:42:") for k in h.service)
    h.service["checkin_off:42:2026-10-07"] = "1"
    await scheduler.tick(bot, [42], now=utc(15, 0))  # 18:00
    assert len(checkins()) == 2
    # на следующий день снова, а слот рядом с вечерним разбором не шлётся
    h.service["settings:42"] = '{"tz": "Europe/Moscow", "evening": "18:15", "checkins": ["12:00", "18:00"]}'
    scheduler._settings_cache.clear()
    await scheduler.tick(bot, [42], now=utc(9, 0, day=8))
    assert len(checkins()) == 3
    await scheduler.tick(bot, [42], now=utc(15, 0, day=8))
    assert len(checkins()) == 3, "18:00 рядом с разбором в 18:15 — пропускаем"

    # 🌙 На вечер
    await app.process_update(h.callback("ci:later"))
    assert "Отложено на вечер" in buttons()[0]

    # ⚡ Быстро
    await app.process_update(h.callback("ci:quick"))
    assert "Ещё 5" in plain(h.texts(chat=42)[-1]) and "Купить краски" in h.texts(chat=42)[-1]
    await app.process_update(h.callback(f"ka:{1:032d}"))
    assert "Ещё 4" in plain(h.texts(chat=42)[-1]) and "Идея фона" in h.texts(chat=42)[-1]
    await app.process_update(h.callback(f"kn:{2:032d}"))
    assert ("trash", f"{2:032d}") in log and "Ещё 3" in plain(h.texts(chat=42)[-1])
    # актуальное не показывается повторно в тот же день, но остаётся в разборе
    await app.process_update(h.callback("ci:quick"))
    assert "Позвонить Ане" in h.texts(chat=42)[-1] and f"{1:032d}" in notes

    # 📦 Пачкой
    await app.process_update(h.callback("ci:batch"))
    batch_msg = [d for e, d in h.sent if e == "sendMessage" and d.get("text", "").startswith("📦")][-1]
    mid = batch_msg["_mid"] if "_mid" in batch_msg else None
    # id сообщения со списком — последний выданный фейковым Telegram
    key = [k for k in main._batches][-1]
    cb = lambda data: h.callback(data)  # noqa: E731

    def press(data):
        u = h.callback(data)
        u.callback_query.message._unfreeze()
        u.callback_query.message.message_id = key[1]
        return u

    await app.process_update(press("bt:0"))
    await app.process_update(press("bt:1"))
    assert "Отмечено: 2" in h.texts(chat=42)[-1]
    assert "📁 В проект" in buttons()
    await app.process_update(press("bp"))
    await app.process_update(press("bpp:1"))
    assert sorted(x for x in log if x[0] == "project") == [("project", f"{1:032d}", "Фоны"), ("project", f"{3:032d}", "Фоны")], log
    await app.process_update(press("ba"))
    await app.process_update(press("by"))
    await app.process_update(press("byy:0"))
    # после «В проект» список обновился: 4 заметки (1, 3, 4, 5), «Все» → тип всем четырём
    assert len([x for x in log if x[0] == "type"]) == 4 and "Всё разобрано" in h.texts(chat=42)[-1], (log, h.texts(chat=42)[-1])

    # «пачка» после перезапуска бота — свежий список, а не ошибка
    note(9, "Новая")
    main._batches.clear()
    await app.process_update(press("bt:0"))
    assert "Новая" in str(buttons())

    # Настройки чек-инов
    await app.process_update(h.callback("st:ci"))
    await app.process_update(h.callback("st:ci:1317"))
    assert "13:00, 17:00" in h.texts(chat=42)[-1]
    prompt = h._message(main.CHECKIN_PROMPT)
    await app.process_update(h.text("19, 11, 15:30", reply_to=prompt))
    assert "11:00, 15:30, 19:00" in h.texts(chat=42)[-1], h.texts(chat=42)[-1]
    await app.process_update(h.text("в обед", reply_to=prompt))
    assert "Не понял" in h.texts(chat=42)[-1]
    await app.process_update(h.callback("st:ci:off"))
    assert "выключены" in h.texts(chat=42)[-1]
    print("checkins OK")


asyncio.run(run())
