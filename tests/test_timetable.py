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
    # 8. разовая загрузка расписания владелицы из data/owner_timetable.txt
    real = timetable.parse(timetable.SEED_FILE.read_text(encoding="utf-8"))
    assert len(real) == 27 and timetable.alternatives({"items": real}) == []
    h.service.pop("timetable:42", None)
    h.service.pop("seed:timetable:1", None)
    slots.clear()
    result = await timetable.seed_owner(42)
    assert result.startswith("загружено: 27") and (await timetable.load(42))["anchor"] == timetable.SEED_ANCHOR
    assert await timetable.seed_owner(42) == "не нужно", "только один раз"
    seeded = await timetable.load(42)
    thu = [i["title"] for i in timetable.for_day(seeded, date(2026, 10, 22))]
    assert thu == ["Физическая культура (в записи)", "Информатика", "Литература", "Химия", "Математика", "Консультация по алгебре", "Танцы тодес олимпийский"], thu
    # 9. репетиторы: по часу, ссылка за 5 минут до начала (если есть), «сделала домашку?» после
    h.service.pop("seed:timetable:3", None)
    assert (await timetable.upgrade_owner(42)).startswith("репетиторы")
    assert await timetable.upgrade_owner(42) == "не нужно"
    data = await timetable.load(42)
    tutors = [i for i in data["items"] if timetable.is_tutor(i)]
    assert [(i["days"], i["start"], i["end"], i["title"], i["homework"]) for i in tutors] == [
        (["Пн"], "10:00", "11:00", "Китайский", True), (["Вт"], "15:00", "16:00", "Математика", True),
        (["Вт"], "17:30", "18:30", "Гетало", True), (["Ср"], "12:00", "13:00", "Китайский", True),
        (["Чт"], "15:00", "16:00", "Математика", True), (["Пт"], "09:00", "10:00", "Обществознание", True)]  # fmt: skip
    assert [i["title"] for i in data["items"] if i.get("link")] == ["Китайский", "Математика", "Китайский", "Математика", "Обществознание"], "у Гетало ссылки нет"
    assert not any(timetable.is_tutor(i) for i in data["items"] if i["room"]), "школьные уроки — не репетиторы"
    mon = lambda hh, mm: datetime(2026, 10, 19, hh, mm, tzinfo=timezone.utc)  # noqa: E731
    (key, text), = timetable.due_joins(data, mon(9, 55))
    assert "Китайский (10:00–11:00)" in text and "telemost.yandex.ru" in text and key == "join:2026-10-19:10:00"
    assert timetable.due_joins(data, mon(9, 50)) == [] and timetable.due_joins(data, mon(10, 0)) == []
    assert [i["title"] for _, i in timetable.due_homework(data, mon(11, 0))] == ["Китайский"]
    assert timetable.due_homework(data, mon(10, 55)) == []
    tue = lambda hh, mm: datetime(2026, 10, 20, hh, mm, tzinfo=timezone.utc)  # noqa: E731
    assert timetable.due_joins(data, tue(17, 25)) == [], "у Гетало нет ссылки — и напоминания со ссылкой нет"
    assert [i["title"] for _, i in timetable.due_homework(data, tue(18, 30))] == ["Гетало"]
    notes = []

    async def create_idea(title, source, tags, blocks, details, author_id=0, author="", when=None, ai_type=None):
        notes.append((title, when, ai_type))
        return "f" * 32, "u"

    async def set_type(pid, name):
        pass
    notion.create_idea, notion.set_type = create_idea, set_type
    await app.process_update(h.callback("hw:y:1000"))
    assert "Домашка по «Китайский» сделана" in h.texts("editMessageText")[-1]
    await app.process_update(h.callback("hw:n:1730"))
    await app.process_update(h.callback("hw:l:1730:2h"))
    assert notes and notes[-1][0] == "Домашка: Гетало" and notes[-1][2] == "⏰ Напоминание"
    await app.process_update(h.callback("hw:y:1000", user=h.person(77, "Аня")))
    assert len(notes) == 1
    # через планировщик: ссылка и вопрос приходят по одному разу
    timetable._cache.clear()
    for t in (datetime(2026, 10, 19, 6, 55, tzinfo=timezone.utc), datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc)):  # 9:55 и 11:00 МСК
        await scheduler.tick(app.bot, [42], now=t)
        await scheduler.tick(app.bot, [42], now=t)
    assert len([t for t in h.texts(chat=42) if t.startswith("🔗 Через 5 мин: Китайский")]) == 1
    assert len([t for t in h.texts(chat=42) if "Сделала домашку?" in t]) == 1
    # 10. новая версия расписания заменяет загруженную: уроки и репетиторы из файла, звонки — как были
    old = await timetable.load(42)
    old["bells"] = False
    await timetable.save(42, old)
    h.service.pop("seed:timetable:version", None)
    assert (await timetable.reload_owner(42)).startswith("загружено: 27")
    assert await timetable.reload_owner(42) == "не нужно"
    new = await timetable.load(42)
    assert new["bells"] is False
    assert [i["title"] for i in new["items"] if timetable.is_tutor(i)] == ["Китайский", "Математика", "Гетало", "Китайский", "Математика", "Обществознание"]
    assert not any(i["days"] == ["Пн"] and i["title"] == "Обществознание" and timetable.is_tutor(i) for i in new["items"]), "школьное обществознание — урок"
    assert not any(i["room"] not in ("", "онлайн") for i in new["items"]), "старых кабинетов не осталось"
    assert any(s["title"] == "🎒 Химия · онлайн" for s in slots) and not any("каб." in s["title"] for s in slots)
    new["bells"] = True
    # онлайн-урок по часу: 45 минут урок, с :45 перемена — звонки в :40 каждого часа
    assert timetable.due_bells(new, datetime(2026, 10, 19, 12, 55, tzinfo=timezone.utc)) == []
    (key, text), = timetable.due_bells(new, datetime(2026, 10, 19, 11, 40, tzinfo=timezone.utc))
    assert key == "bell:2026-10-19:11:00" and "Дальше: История (2-й час)" in text and "Начало в 12:00 · перемена 15 мин" in text, text
    (_, text), = timetable.due_bells(new, datetime(2026, 10, 19, 12, 40, tzinfo=timezone.utc))
    assert "Дальше: Английский язык" in text and "💻 Онлайн" in text and "перемена 15 мин" in text, text
    tue = timetable.for_day(new, date(2026, 10, 20))
    assert ("11:00", "12:00", "Встреча с наставником") in [(i["start"], i["end"], i["title"]) for i in tue]
    (_, text), = timetable.due_bells(new, datetime(2026, 10, 20, 13, 40, tzinfo=timezone.utc))
    assert "Математика" in text and "перерыв 1 ч 15 мин" in text, text
    print("timetable OK")


asyncio.run(run())
