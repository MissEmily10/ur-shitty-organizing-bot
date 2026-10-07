"""Этап 7: расписание — сборка недели, отмены, разовые события, события из заметок, словами через ИИ, PDF, пересмотр."""

import asyncio
import re
from datetime import date, datetime, timezone
from types import SimpleNamespace as NS

import harness as h

from bot import ai, main, notion, schedule, scheduler

rows: list[dict] = []


async def schedule_slots(uid):
    return [dict(r) for r in rows if r["uid"] == uid]


async def add_slot(uid, title, kind, start="", end="", days=None, date=None):
    rows.append({"page": f"p{len(rows)}", "uid": uid, "title": title, "kind": kind, "days": days or [], "date": date, "start": start, "end": end})


async def remove_slot(page):
    rows[:] = [r for r in rows if r["page"] != page]


async def dated_events(uid, start, end):
    return [{"title": "ДР Ани", "when": "2026-10-09", "type": "📅 Событие"},
            {"title": "Съёмка", "when": "2026-10-10T11:00:00+03:00", "type": "📅 Событие"}]


async def run():
    notion.schedule_slots, notion.add_slot, notion.remove_slot, notion.dated_events = schedule_slots, add_slot, remove_slot, dated_events

    # --- ИИ: чистка ответа модели
    raw = '''Вот: {"replace_base": true, "base_add": [{"title": "Работа", "days": ["пн","ср","пт"], "start": "10", "end": "14:00"},
              {"title": "Зал", "days": ["вт","чт"], "start": "19:00", "end": ""}],
              "base_remove": [], "add": [{"title": "Стоматолог", "date": "2026-10-15", "start": "14:00", "end": "15:00"}, {"title": "x"}],
              "cancel": [{"title": "Зал", "date": "2026-10-08"}]}'''

    class C:
        async def chat_completion(self, **k):
            return NS(choices=[NS(message=NS(content=raw))])

    real_client = ai._client
    ai._client = lambda: C()
    now = datetime(2026, 10, 7, 12, 0, tzinfo=scheduler.parse_tz("Europe/Moscow"))
    plan = await ai.parse_schedule("пн ср пт работа…", "нет", now, routine=True)
    assert plan["replace_base"] and plan["base_add"][0]["start"] == "10:00" and len(plan["add"]) == 1, plan
    change = await ai.parse_schedule("в четверг зала не будет", "…", now, routine=False)
    assert change["replace_base"] is False
    ai._client = real_client

    # --- применение и сборка недели
    done = await schedule.apply(42, plan)
    assert len(done) == 4, done
    week = await schedule.occurrences(42, date(2026, 10, 5), date(2026, 10, 11))
    by_day = {}
    for o in week:
        by_day.setdefault(o.day.day, []).append(f"{o.start} {o.title}")
    assert by_day[5] == ["10:00 Работа"] and by_day[6] == ["19:00 Зал"], by_day
    assert 8 not in by_day, "в четверг 8-го зал отменён"
    assert by_day[9] == [" ДР Ани", "10:00 Работа"] and by_day[10] == ["11:00 Съёмка"], by_day
    assert "Пн 10:00–14:00 — Работа" in schedule.base_text(await schedule_slots(42)).replace(", Ср, Пт", "")

    # повторное «задать базовое» заменяет старое, а разовые и отмены остаются
    await schedule.apply(42, {"replace_base": True, "base_add": [{"title": "Йога", "days": ["сб"], "start": "09:00", "end": "10:00"}]})
    titles = sorted(r["title"] for r in rows)
    assert titles == ["Зал", "Стоматолог", "Йога"] or sorted(titles) == sorted(["Зал", "Стоматолог", "Йога"]), titles

    # --- через бота
    async def parse_schedule(text, base, now, routine):
        return {"replace_base": routine, "base_add": [{"title": "Работа", "days": ["пн"], "start": "10:00", "end": "14:00"}] if routine else [],
                "base_remove": [], "add": [] if routine else [{"title": "Врач", "date": "2026-10-12", "start": "09:00", "end": ""}], "cancel": []}
    ai.parse_schedule = parse_schedule
    app = await h.make_app()
    await app.process_update(h.text("/schedule"))
    assert "Базовое" in h.texts(chat=42)[-1]
    await app.process_update(h.text("пн 10-14 работа", reply_to=h._message(main.ROUTINE_PROMPT)))
    assert "Вот что я понял" in h.texts(chat=42)[-1] and "🔁 пн 10:00–14:00 — Работа" in h.texts(chat=42)[-1], h.texts(chat=42)[-1]
    key = list(main._schedule_plans)[-1]
    press = h.callback("sca")
    press.callback_query.message._unfreeze()
    press.callback_query.message.message_id = key
    await app.process_update(press)
    assert "Расписание обновлено" in h.texts(chat=42)[-1]
    assert [r["title"] for r in rows if r["kind"] == notion.KIND_REGULAR] == ["Работа"]

    await app.process_update(h.text("врач в пн в 9", reply_to=h._message(main.CHANGE_PROMPT)))
    assert "📌 2026-10-12 09:00 — Врач" in h.texts(chat=42)[-1], h.texts(chat=42)[-1]

    await app.process_update(h.callback("sc:week:0"))
    assert re.search(r"Пн \d\d\.\d\d", h.texts(chat=42)[-1])
    for data in ("sc:pdfw:0", "sc:pdfm:0", "sc:pdfw:1"):
        before = len(h.sent)
        await app.process_update(h.callback(data))
        docs = [d for e, d in h.sent[before:] if e == "sendDocument"]
        assert docs, data
        payload = docs[0]["document"]
        content = payload.input_file_content if hasattr(payload, "input_file_content") else payload
        assert bytes(content)[:4] == b"%PDF", data

    await app.process_update(h.callback("sc:rev"))
    await app.process_update(h.callback("sc:rev:month"))
    assert "Пересмотр: раз в месяц" in h.texts(chat=42)[-1]

    # --- напоминание пересмотреть: воскресенье 11:00 / 1-е число
    h.service.pop("settings:42", None)
    scheduler._settings_cache.clear()
    bot = app.bot

    async def no(*a, **k):
        return []
    notion.review_items, notion.dated_items, notion.forget_old_marks = no, no, (lambda *a: asyncio.sleep(0, 0))

    def reviews():
        return [t for t in h.texts(chat=42) if t.startswith("🗓 Пора")]

    sun = lambda hh: datetime(2026, 10, 11, hh, 0, tzinfo=timezone.utc)  # noqa: E731
    await scheduler.tick(bot, [42], now=sun(7))   # 10:00 МСК — рано
    assert not reviews()
    await scheduler.tick(bot, [42], now=sun(8))   # 11:00 МСК воскресенья
    await scheduler.tick(bot, [42], now=sun(9))
    assert len(reviews()) == 1 and "на неделю" in reviews()[-1]
    await scheduler.update_settings(42, schedule_review="month")
    await scheduler.tick(bot, [42], now=datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc))
    assert len(reviews()) == 2 and "на месяц" in reviews()[-1]
    await scheduler.update_settings(42, schedule_review="off")
    await scheduler.tick(bot, [42], now=datetime(2026, 11, 8, 8, 0, tzinfo=timezone.utc))
    assert len(reviews()) == 2
    # без базового расписания не напоминаем
    await scheduler.update_settings(42, schedule_review="week")
    rows.clear()
    await scheduler.tick(bot, [42], now=datetime(2026, 11, 15, 8, 0, tzinfo=timezone.utc))
    assert len(reviews()) == 2
    print("schedule OK")


asyncio.run(run())
