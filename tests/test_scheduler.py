"""Планировщик: часовые пояса, время разбора, отсутствие дублей (в том числе после перезапуска), настройки."""

import asyncio
from datetime import datetime, timezone

import harness as h

from bot import notion, scheduler

store: dict[str, str] = {}
pushes: list[int] = []


async def get_value(key):
    return store.get(key)


async def set_value(key, value):
    store[key] = value


async def review_items(uid):
    return [{"id": "x"}] * 3


async def forget_old_marks(days=14):
    return 0


async def dated_items(uid, before, after=None):
    return []


class FakeBot:
    async def send_message(self, uid, text, **kw):
        if text.startswith("🌙"):  # здесь проверяем только вечерний разбор; вопрос о настроении — в test_mood
            pushes.append(uid)


def utc(hh, mm=0, day=6):
    return datetime(2026, 10, day, hh, mm, tzinfo=timezone.utc)


def restart():
    scheduler._settings_cache.clear()
    scheduler._done_cache.clear()


async def run():
    notion.get_value, notion.set_value, notion.review_items, notion.forget_old_marks = get_value, set_value, review_items, forget_old_marks
    notion.dated_items = dated_items
    bot = FakeBot()
    store["settings:77"] = '{"tz": "Asia/Vladivostok", "evening": "21:30"}'

    # Москва по умолчанию, 20:00 МСК = 17:00 UTC
    await scheduler.tick(bot, [42, 77], now=utc(16, 55))
    assert pushes == [], pushes
    await scheduler.tick(bot, [42, 77], now=utc(17, 0))
    assert pushes == [42], pushes
    await scheduler.tick(bot, [42, 77], now=utc(17, 5))
    assert pushes == [42], "второй тик не дублирует"
    restart()
    await scheduler.tick(bot, [42, 77], now=utc(17, 10))
    assert pushes == [42], "после перезапуска тоже без дублей"

    # Владивосток (UTC+10), разбор в 21:30 → 11:30 UTC следующего дня по UTC-дате 7-го
    await scheduler.tick(bot, [77], now=utc(11, 25, day=7))
    assert pushes == [42]
    await scheduler.tick(bot, [77], now=utc(11, 30, day=7))
    assert pushes == [42, 77], pushes

    # на следующий день снова
    await scheduler.tick(bot, [42], now=utc(17, 0, day=7))
    assert pushes == [42, 77, 42]

    # проверка вручную не ставит отметку
    await scheduler.tick(bot, [42], now=utc(10, 0, day=8), force="evening")
    await scheduler.tick(bot, [42], now=utc(17, 0, day=8))
    assert pushes[-2:] == [42, 42], pushes

    # разбор формата
    assert scheduler.parse_time("21") == "21:00" and scheduler.parse_time("9.15") == "09:15" and scheduler.parse_time("25:00") is None
    assert scheduler.parse_tz("Europe/Berlin") and scheduler.parse_tz("UTC+5") and scheduler.parse_tz("+5:30") and not scheduler.parse_tz("Марс")

    # интерфейс /settings через бота
    restart()
    app = await h.make_app()
    await app.process_update(h.text("/settings"))
    assert "Москва (UTC+03)" in h.texts(chat=42)[-1] and "20:00" in h.texts(chat=42)[-1], h.texts(chat=42)[-1]
    await app.process_update(h.callback("st:tz:7"))
    assert "Владивосток" in h.texts(chat=42)[-1]
    await app.process_update(h.callback("st:ev:2130"))
    assert "21:30" in h.texts(chat=42)[-1]
    await app.process_update(h.text("в девять", reply_to=h._message(__import__("bot.main", fromlist=["x"]).TIME_PROMPT)))
    assert "Не понял время" in h.texts(chat=42)[-1]
    await app.process_update(h.text("22:15", reply_to=h._message(__import__("bot.main", fromlist=["x"]).TIME_PROMPT)))
    assert "22:15" in h.texts(chat=42)[-1]
    await app.process_update(h.text("UTC+5", reply_to=h._message(__import__("bot.main", fromlist=["x"]).TZ_PROMPT)))
    assert "UTC+05" in h.texts(chat=42)[-1], h.texts(chat=42)[-1]
    print("scheduler OK")


asyncio.run(run())
