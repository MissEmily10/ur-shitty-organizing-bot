"""🎒 Моё расписание: разбор формата владелицы (без ИИ), уроки через неделю, звонок за 5 минут до конца урока,
строки в основном расписании, PDF, права. Данные — выдуманные (репозиторий публичный)."""

import asyncio
from datetime import date, datetime, timezone

import harness as h

from bot import main, notion, scheduler, timetable

SAMPLE = """понедельник, среда - 14:00 рисование, 16:00 французский

вторник:
9:00-9:45 • 101
Алгебра
Иванова Ирина Ивановна

9:55-10:40 • 202
2 Физика
Петров Пётр Петрович

10:55-11:40 • 303
З Химия
Сидорова Анна Павловна
ИЛИ
10:55-11:40 • 303
З Биология
Козлова Мария Андреевна

12:45-13:30 . 101
4 Алгебра
Иванова Ирина Ивановна

До 14:30
Еду в студию • Сокол

19:00 - 20:00 - бассейн

суббота:
16:20 - бассейн (выехать до 15:00)
"""

slots: list[dict] = []


async def schedule_slots(uid):
    return [dict(s) for s in slots]


async def add_slot(uid, title, kind, start="", end="", days=None, date=None):
    slots.append({"page": f"s{len(slots)}", "title": title, "kind": kind, "start": start, "end": end, "days": days or []})


async def remove_slot(page):
    slots[:] = [s for s in slots if s["page"] != page]


async def run():
    # 1. разбор
    items = timetable.parse(SAMPLE)
    by = {(tuple(i["days"]), i["start"], i["title"]): i for i in items}
    assert by[(("Пн", "Ср"), "14:00", "Рисование")]["lesson"]
    first = by[(("Вт",), "09:00", "Алгебра")]
    assert (first["end"], first["room"], first["teacher"]) == ("09:45", "101", "Иванова Ирина Ивановна")
    chem, bio = by[(("Вт",), "10:55", "Химия")], by[(("Вт",), "10:55", "Биология")]
    assert (chem["num"], chem["week"], bio["week"]) == ("3", 0, 1), "«З» вместо 3 и пара через неделю"
    assert by[(("Вт",), "12:45", "Алгебра")]["room"] == "101", "разделитель «.» тоже понимается"
    road = by[(("Вт",), "12:45" if False else "13:30", "Еду в студию • Сокол")]
    assert road["end"] == "14:30" and not road["lesson"], "«До 14:30» — с конца прошлого урока"
    assert by[(("Вт",), "19:00", "Бассейн")]["end"] == "20:00" and by[(("Сб",), "16:20", "Бассейн (выехать до 15:00)")]

    # 2. недели: отсчёт от недели сохранения, через неделю — другой урок
    data = {"anchor": "2026-10-12", "items": items, "bells": True}
    tue = date(2026, 10, 13)
    assert [i["title"] for i in timetable.for_day(data, tue)][2] == "Химия"
    assert [i["title"] for i in timetable.for_day(data, date(2026, 10, 20))][2] == "Биология"
    timetable.swap(data, 0)
    assert [i["title"] for i in timetable.for_day(data, tue)][2] == "Биология"
    timetable.swap(data, 0)

    # 3. звонки: за 5 минут до конца, следующий урок с кабинетом, учителем и переменой; после последнего — что дальше
    at = lambda hh, mm: datetime(2026, 10, 13, hh, mm, tzinfo=timezone.utc)  # noqa: E731
    assert timetable.due_bells(data, at(9, 39)) == []
    (key, text), = timetable.due_bells(data, at(9, 40))
    assert key == "bell:2026-10-13:09:00"
    assert "конец урока «Алгебра»" in text and "Дальше: 2 · Физика" in text and "Кабинет 202" in text
    assert "Петров Пётр Петрович" in text and "перемена 10 мин" in text, text
    (_, text), = timetable.due_bells(data, at(10, 37))
    assert "3 · Химия" in text and "перемена 15 мин" in text
    (_, last), = timetable.due_bells(data, at(13, 25))
    assert "последний урок" in last and "Еду в студию" in last and "до 14:30" in last, last
    assert timetable.due_bells(data, at(19, 55)) == [], "бассейн — не урок"
    assert timetable.due_bells(data, datetime(2026, 10, 12, 14, 55, tzinfo=timezone.utc)) == [], "без конца урока звонка нет"

    # 4. основное расписание: строки с меткой, пара — одной строкой; повторная загрузка заменяет старые
    notion.schedule_slots, notion.add_slot, notion.remove_slot = schedule_slots, add_slot, remove_slot
    slots.append({"page": "mine", "title": "Йога", "kind": notion.KIND_REGULAR, "start": "08:00", "end": "", "days": ["Пн"]})
    n = await timetable.sync_main_schedule(42, data)
    titles = [s["title"] for s in slots]
    assert "Йога" in titles and "🎒 Химия / Биология (чередуются через неделю) · каб. 303" in titles and len(slots) == n + 1
    await timetable.sync_main_schedule(42, data)
    assert len(slots) == n + 1, "без дублей"

    # 5. PDF
    pdf = timetable.week_pdf(data, tue)
    assert pdf[:4] == b"%PDF" and len(pdf) > 2000

    # 6. через бота: только владелица; черновик → поменять неделю → сохранить
    app = await h.make_app()
    await app.process_update(h.text("/timetable"))
    assert h.texts(chat=42)[-1].startswith(main.TIMETABLE_PROMPT)
    await app.process_update(h.text(SAMPLE, reply_to=h._message(main.TIMETABLE_PROMPT)))
    draft = h.texts(chat=42)[-1]
    assert "уроков 7" in draft and "на этой неделе — Химия" in draft, draft
    await app.process_update(h.callback("td2:sw:0"))
    assert "на этой неделе — Биология" in h.texts("editMessageText")[-1]
    await app.process_update(h.callback("td2:save"))
    saved = await timetable.load(42)
    assert saved and saved["anchor"] == timetable.monday(scheduler.local_now(await scheduler.get_settings(42)).date()).isoformat()
    assert "Моё расписание" in h.texts(chat=42)[-1]
    await app.process_update(h.callback("tt:pdf:0"))
    assert any(e == "sendDocument" for e, _ in h.sent[-3:])
    # участница — нет
    main.member.add_user_ids(77)
    count = len(h.sent)
    await app.process_update(h.text("/timetable", user=h.person(77, "Аня")))
    await app.process_update(h.callback("tt:bell", user=h.person(77, "Аня")))
    assert not any("Моё расписание" in (d.get("text") or "") for _, d in h.sent[count:])

    # 7. планировщик шлёт звонок один раз
    saved["anchor"], saved["items"] = "2026-10-12", items
    await timetable.save(42, saved)

    async def nothing(*a, **k):
        return []
    scheduler.JOBS, scheduler._reminders, notion.forget_old_marks = [], nothing, nothing
    moscow = datetime(2026, 10, 13, 6, 40, tzinfo=timezone.utc)  # 09:40 МСК
    await scheduler.tick(app.bot, [42, 77], now=moscow)
    await scheduler.tick(app.bot, [42, 77], now=moscow)
    bells = [t for t in h.texts(chat=42) if t.startswith("🔔 Через 5 минут")]
    assert len(bells) == 1 and not [t for t in h.texts(chat=77) if t.startswith("🔔")]
    print("timetable OK")


asyncio.run(run())
