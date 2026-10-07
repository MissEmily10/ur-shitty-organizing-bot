"""Этап 5: срок при сохранении, ⏰ в разборе, своя дата, напоминания, просроченное, «готово/перенести», свои типы."""

import asyncio
from datetime import datetime, timezone

import harness as h

from bot import ai, main, notion, scheduler

PID = "c" * 32
pages: dict[str, dict] = {}
created: list[dict] = []
added_types: list[list[str]] = []


def page(pid=PID, **kw):
    base = {"id": pid, "title": "Позвонить Ане", "url": f"https://www.notion.so/x-{pid}", "project": None, "type": None,
            "author_id": 42, "when": None, "done": False, "ai_type": None}
    base.update(kw)
    pages[pid] = base
    return base


async def page_info(pid):
    return dict(pages[pid])


async def set_when(pid, when):
    pages[pid]["when"] = when
    if when:
        pages[pid]["done"] = False


async def set_done(pid, done=True):
    pages[pid]["done"] = done


async def dated_items(uid, before, after=None):
    return [dict(p) for p in pages.values() if p["when"] and not p["done"]]


async def review_items(uid):
    return [dict(p) for p in pages.values() if not p["type"]]


async def preview(pid):
    return "суть"


async def create_idea(title, source, tags, blocks, details, author_id=0, author="", when=None, ai_type=None):
    created.append({"when": when, "ai_type": ai_type})
    page(when=when, ai_type=ai_type)
    return PID, pages[PID]["url"]


async def structure(text="", images=None, types=None, now=None):
    assert types == h.TYPES and now.tzinfo is not None
    return ai.Idea(title="Позвонить Ане", summary="- позвонить", type_guess="⏰ Напоминание", when="2026-10-07T15:00:00+03:00")


async def parse_when(text, now):
    return None


async def add_types(names):
    added_types.append(names)
    return names


async def created_today(uid):
    return 0


def last(chat=42):
    import re
    return re.sub("<[^>]+>", "", h.texts(chat=chat)[-1])


def markup_texts():
    for e, d in reversed(h.sent):
        markup = d.get("reply_markup")
        if hasattr(markup, "inline_keyboard"):
            return [b.text for row in markup.inline_keyboard for b in row]
        if isinstance(markup, dict) and "inline_keyboard" in markup:
            return [b["text"] for row in markup["inline_keyboard"] for b in row]
    return []


async def run():
    notion.page_info, notion.set_when, notion.set_done, notion.dated_items = page_info, set_when, set_done, dated_items
    notion.review_items, notion.preview, notion.create_idea, notion.add_types = review_items, preview, create_idea, add_types
    notion.created_today = created_today
    ai.structure, ai.parse_when = structure, parse_when
    app = await h.make_app()
    real_now = datetime.now(timezone.utc)

    # 1. Сохранение: срок от ИИ сразу ставится, в ответе «⏰ Напомню», тип — только предложение
    await app.process_update(h.text("завтра в 15 позвонить Ане"))
    assert created[-1] == {"when": "2026-10-07T15:00:00+03:00", "ai_type": "⏰ Напоминание"}, created
    assert "⏰ Напомню:" in last() and "15:00" in last(), last()
    assert "⏰ Изменить срок" in markup_texts() and "✖️ Без срока" in markup_texts()

    # 2. Разбор: срок в шапке, предложенный тип первым со звёздочкой, кнопка «⏰ Срок»
    await app.process_update(h.text("/razbor"))
    assert "⏰ Срок:" in last() and "ИИ думает: ⏰ Напоминание" in last(), last()
    kb = markup_texts()
    assert kb[0] == "✨ ⏰ Напоминание" and "⏰ Срок" in kb, kb

    # 3. Быстрый срок «Завтра» и убрать срок
    await app.process_update(h.callback(f"wv:{PID}:0:d1"))
    # У срока было время 15:00 — «Завтра» сохраняет его
    tomorrow = (scheduler.local_now(scheduler.DEFAULT_SETTINGS).date() + __import__("datetime").timedelta(days=1)).isoformat()
    assert pages[PID]["when"].startswith(tomorrow + "T15:00"), pages[PID]["when"]
    # без времени — просто дата
    pages[PID]["when"] = None
    await app.process_update(h.callback(f"wv:{PID}:0:d1"))
    assert pages[PID]["when"] == tomorrow, pages[PID]["when"]
    await app.process_update(h.callback(f"wx:{PID}:0"))
    assert pages[PID]["when"] is None

    # 4. Своя дата ответом: сначала понятная, потом непонятная
    await app.process_update(h.callback(f"wc:{PID}:-"))
    prompt = [d for e, d in h.sent if e == "sendMessage" and d.get("text", "").startswith("⏰ Срок для")][-1]
    reply_to = h._message(prompt["text"].replace("<a href=", "").split(">")[0], entities=[{"type": "text_link", "offset": 0, "length": 5, "url": pages[PID]["url"]}])
    reply_to["text"] = "⏰ Срок для Позвонить Ане: напишите…"
    await app.process_update(h.text("пятница 18:00", reply_to=reply_to))
    assert pages[PID]["when"] and pages[PID]["when"].endswith("18:00:00+03:00"), pages[PID]
    assert "✓" in last()
    await app.process_update(h.text("когда-нибудь потом", reply_to=reply_to))
    assert "Не понял срок" in last()

    # 5. Напоминания по планировщику (время двигаем): срок 2026-10-08 15:00 МСК (12:00 UTC)
    pages.clear()
    page(PID, when="2026-10-08T15:00:00+03:00", type="📋 Задача")
    page("d" * 32, title="Сдать макет", when="2026-10-08", type="📋 Задача")
    page("e" * 32, title="Позвонить", when="2026-10-07T18:00:00+03:00", type="⏰ Напоминание")
    bot = app.bot
    h.sent.clear()
    def utc(d, hh, mm=0):
        return datetime(2026, 10, d, hh, mm, tzinfo=timezone.utc)

    await scheduler.tick(bot, [42], now=utc(7, 6, 55))   # 09:55 МСК 7-го — ещё ничего
    assert not h.texts(chat=42), h.texts(chat=42)
    await scheduler.tick(bot, [42], now=utc(7, 7, 0))    # 10:00 МСК: «завтра срок» для даты без времени
    await scheduler.tick(bot, [42], now=utc(7, 12, 0))   # 15:00 МСК: «завтра срок» для 15:00 8-го
    await scheduler.tick(bot, [42], now=utc(7, 12, 5))   # без дублей
    await scheduler.tick(bot, [42], now=utc(7, 15, 0))   # 18:00 МСК: само напоминание
    await scheduler.tick(bot, [42], now=utc(8, 6, 0))    # 09:00 МСК 8-го: «сегодня срок»
    await scheduler.tick(bot, [42], now=utc(8, 11, 0))   # 14:00 МСК: «через час»
    msgs = [t for t in h.texts(chat=42) if t.startswith(("📌", "⏰"))]
    import re
    msgs = [re.sub("<[^>]+>", "", m) for m in msgs]
    assert [m.split(":")[0] for m in msgs] == ["📌 Завтра срок", "📌 Завтра срок", "⏰ Напоминание", "📌 Сегодня срок", "📌 Через час срок"], msgs

    # Перенос срока — напоминания по новому сроку приходят снова
    await set_when(PID, "2026-10-09T15:00:00+03:00")
    await scheduler.tick(bot, [42], now=utc(8, 12, 0))
    assert re.sub("<[^>]+>", "", h.texts(chat=42)[-1]).startswith("📌 Завтра срок")

    # 6. Вечером — просроченное
    pages["d" * 32]["when"] = "2026-10-05"
    await scheduler.tick(bot, [42], now=utc(8, 17, 0))
    # просрочены «Сдать макет» (5-го) и не отмеченное «Позвонить» (7-го в 18:00)
    evening = [t for t in h.texts(chat=42) if "Просрочено" in t][-1]  # следом приходит вопрос о настроении
    assert "🔥 Просрочено: 2" in evening, evening
    await app.process_update(h.callback("ov"))
    shown = h.texts(chat=42)[-2:]
    assert all(t.startswith("🔥") for t in shown) and any("Сдать макет" in t for t in shown), shown

    # 7. Готово и перенести
    await app.process_update(h.callback(f"dl:ok:{'d' * 32}"))
    assert pages["d" * 32]["done"]
    await app.process_update(h.callback(f"dl:sn:{PID}"))
    assert "+1 час" in markup_texts()
    await app.process_update(h.callback(f"dl:p1d:{PID}"))
    assert pages[PID]["when"].endswith("15:00:00+03:00"), pages[PID]

    # 8. Чужая заметка: участница не может отметить или перенести
    main.member.add_user_ids(77)
    anya = h.person(77, "Аня")
    pages[PID]["done"] = False
    await app.process_update(h.callback(f"dl:ok:{PID}", user=anya))
    assert not pages[PID]["done"]

    # 9. Свои типы
    await app.process_update(h.text("/addtype 🎵 Трек, 📷 Съёмка"))
    assert added_types[-1] == ["🎵 Трек", "📷 Съёмка"]
    await app.process_update(h.text("/types", user=anya))
    assert "🗑" not in str(markup_texts()) or h.sent[-1][1].get("reply_markup") is None
    print("deadlines OK")


asyncio.run(run())
