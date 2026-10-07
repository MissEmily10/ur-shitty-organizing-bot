"""Горящие дедлайны: утренняя сводка (просрочено, сегодня, завтра) с вопросом «что закрыто?», вопрос
«задача закрыта?» после срока, кнопки закрыть / перенести / неактуально, настройка времени, права."""

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import harness as h

from bot import main, notion, scheduler

MSK = ZoneInfo("Europe/Moscow")
notes: dict[str, dict] = {}
done: list[str] = []


def note(letter, title, when, type_="📋 Задача", author=42):
    pid = letter * 32
    notes[pid] = {"id": pid, "title": title, "url": f"https://n/{letter}", "when": when, "type": type_, "ai_type": None,
                  "done": False, "author_id": author, "project": None, "project_id": None}


async def dated_items(uid, before, after=None):
    rows = [n for n in notes.values() if not n["done"] and n["when"] and n["author_id"] == uid
            and n["when"] <= before + "T99" and (not after or n["when"] >= after[:10])]
    return sorted((dict(r) for r in rows), key=lambda r: r["when"])


async def set_done(pid, value=True):
    notes[pid]["done"] = value
    done.append(pid)


async def set_when(pid, when):
    notes[pid]["when"] = when


async def page_info(pid):
    return dict(notes[pid])


def last(endpoint):
    return next(d for e, d in reversed(h.sent) if e == endpoint)


def keys(data):
    return [(b.text, b.callback_data) for row in data["reply_markup"].inline_keyboard for b in row]


async def run():
    notion.dated_items, notion.set_done, notion.set_when, notion.page_info = dated_items, set_done, set_when, page_info
    note("a", "Сдать макет", "2026-10-05")                       # просрочено
    note("b", "Отправить смету", "2026-10-07T18:00:00+03:00")    # сегодня
    note("c", "Позвонить в типографию", "2026-10-08")            # завтра
    note("d", "Через три дня", "2026-10-10")                     # ещё не горит
    note("e", "Встреча с Аней", "2026-10-08T12:00:00+03:00", "📅 Событие")  # встречу не «закрывают»
    note("f", "Выпить таблетку", "2026-10-07T21:00:00+03:00", "⏰ Напоминание")
    note("g", "Чужая задача", "2026-10-07", author=77)
    app = await h.make_app()
    scheduler.JOBS = [j for j in scheduler.JOBS if j.name == "deadlines"]

    async def nothing(*a, **k):
        return 0
    notion.forget_old_marks = nothing

    # 1. утром (10:00 по умолчанию) — сводка горящих с вопросом, что закрыто
    await scheduler.tick(app.bot, [42], now=datetime(2026, 10, 7, 6, 55, tzinfo=timezone.utc))  # 09:55 МСК
    assert not any("Горящие" in t for t in h.texts(chat=42))
    await scheduler.tick(app.bot, [42], now=datetime(2026, 10, 7, 7, 5, tzinfo=timezone.utc))  # 10:05 МСК
    # (тот же тик шлёт и обычное «📌 Завтра срок» — это отдельное напоминание)
    digest = next(d for e, d in reversed(h.sent) if e == "sendMessage" and "Горящие" in d.get("text", ""))
    text = digest["text"]
    assert "Горящие дедлайны" in text and "уже закрыты?" in text, text
    assert "🔥 <a href=\"https://n/a\">Сдать макет</a> — было" in text
    assert "Отправить смету" in text and "Позвонить в типографию" in text
    for absent in ("Через три дня", "Встреча с Аней", "Выпить таблетку", "Чужая задача"):
        assert absent not in text, absent
    assert ("⏰", f"hd:sn:{'b' * 32}") in keys(digest) and keys(digest)[0][0].startswith("✅ 1. Сдать макет")
    # раз в день
    count = len(h.sent)
    await scheduler.tick(app.bot, [42], now=datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc))
    assert not any("Горящие" in d.get("text", "") for _, d in h.sent[count:])

    # 2. ✅ — задача закрыта, сводка обновилась без неё
    await app.process_update(h.callback(f"hd:ok:{'a' * 32}"))
    assert notes["a" * 32]["done"]
    assert "Сдать макет" not in last("editMessageText")["text"]
    # ⏰ → перенести на неделю
    await app.process_update(h.callback(f"hd:sn:{'c' * 32}"))
    assert "Через неделю" in [t for t, _ in keys(last("editMessageReplyMarkup"))]
    await app.process_update(h.callback(f"hd:p7d:{'c' * 32}"))
    assert notes["c" * 32]["when"] > "2026-10-10" and "Позвонить" not in last("editMessageText")["text"]
    # чужую задачу закрыть нельзя
    main.member.add_user_ids(77)
    await app.process_update(h.callback(f"hd:ok:{'b' * 32}", user=h.person(77, "Аня")))
    assert not notes["b" * 32]["done"]

    # 3. после срока — вопрос «задача закрыта?» (через час после времени)
    async def none(*a, **k):
        return []
    after = datetime(2026, 10, 7, 16, 5, tzinfo=timezone.utc)  # 19:05 МСК, срок был в 18:00
    settings = await scheduler.get_settings(42)
    sent = await scheduler._reminders(app.bot, 42, after, settings)
    assert any(s.startswith("after Отправить смету") for s in sent), sent
    question = last("sendMessage")
    assert "Срок прошёл" in question["text"] and "Задача закрыта?" in question["text"]
    assert [t for t, _ in keys(question)] == ["✅ Да, закрыта", "⏳ Ещё нет", "🗑 Уже неактуально"]
    # про задачу без времени наутро спрашивает сводка, отдельного вопроса нет; без сводки — есть
    note("h", "Купить бумагу", "2026-10-06")
    morning = datetime(2026, 10, 7, 6, 40, tzinfo=timezone.utc)  # 09:40 МСК
    assert not await scheduler._reminders(app.bot, 42, morning, settings)
    assert await scheduler._reminders(app.bot, 42, morning, {**settings, "deadlines": ""}) == ["after Купить бумагу"]
    notes["h" * 32]["done"] = True
    # у напоминания и встречи такого вопроса нет
    points = scheduler.reminder_points(notes["f" * 32], after.astimezone(MSK))
    assert "after" not in [k for k, _, _ in points]
    # «🗑 Уже неактуально» — срок убран
    await app.process_update(h.callback(f"dl:x:{'b' * 32}"))
    assert notes["b" * 32]["when"] is None

    # 4. настройка: время и выключение
    await app.process_update(h.callback("st:dd"))
    await app.process_update(h.callback("st:dd:0900"))
    assert (await scheduler.get_settings(42))["deadlines"] == "09:00"
    await app.process_update(h.callback("st:dd:off"))
    assert scheduler._hot_due(datetime(2026, 10, 8, 12, 0, tzinfo=MSK), await scheduler.get_settings(42)) is None

    # 5. кнопка в разделе «Заметки»; когда гореть нечему — так и говорит
    notes["c" * 32]["done"] = notes["d" * 32]["done"] = True
    await app.process_update(h.callback("hd:show"))
    assert "Горящих дедлайнов нет" in h.texts(chat=42)[-1]
    print("hot deadlines OK")


asyncio.run(run())
