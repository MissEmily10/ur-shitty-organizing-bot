"""🖥 Виджет на рабочем столе Mac: секретный адрес, папка виджета, день (расписание + общее), планы по важности,
«＋» (событие разово / каждую неделю, заметка), «☐» — закрыть, чужой адрес не работает."""

import asyncio
import io
import json
import zipfile
from datetime import date, timedelta

from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web

import harness as h

from bot import ai, changes, main, notion, schedule, scheduler, timetable, widget

AREA = "a" * 32
TODAY = scheduler.local_now(scheduler.DEFAULT_SETTINGS).date()
notes = [
    {"id": "1" * 32, "title": "Без срока", "when": None, "parent_id": None, "project_id": "p" * 32, "type": "💡 Идея", "done": False, "author_id": 42},
    {"id": "2" * 32, "title": "Просрочено", "when": (TODAY - timedelta(days=1)).isoformat(), "parent_id": None, "project_id": None, "type": "📋 Задача", "done": False, "author_id": 42},
    {"id": "3" * 32, "title": "Большая", "when": (TODAY + timedelta(days=3)).isoformat(), "parent_id": None, "project_id": None, "type": "", "done": False, "author_id": 42},
    {"id": "4" * 32, "title": "Шаг", "when": None, "parent_id": "3" * 32, "project_id": None, "type": "📋 Задача", "done": False, "author_id": 42},
]
closed: list[str] = []
sent_notes: list[str] = []


async def run():
    async def open_notes(uid, limit=500):
        return [dict(n) for n in notes if n["id"] not in closed]

    async def project_list(uid=None, include_closed=False):
        return [{"id": "p" * 32, "name": "Домашка", "sphere_id": AREA}]

    async def spheres():
        return [{"id": AREA, "name": "📚 Образование"}]

    async def page_info(pid):
        return next(dict(n) for n in notes if n["id"] == pid)

    async def set_done(pid, value=True):
        closed.append(pid)

    async def occurrences(uid, start, end):
        return [schedule.Occurrence(start, "18:00", "19:00", "🔖 Для себя", "regular"),
                schedule.Occurrence(start, "10:00", "11:00", "🎒 Китайский", "regular")]  # fmt: skip

    async def create_idea(title, source, tags, blocks, details, author_id=0, author="", when=None, ai_type=None, parent=None):
        sent_notes.append(title)
        return "9" * 32, "https://notion/9"

    async def structure(text="", images=None, types=None, now=None):
        return ai.Idea(title="Купить краски", summary="- купить")

    notion.open_notes, notion.project_list, notion.spheres, notion.page_info, notion.set_done = open_notes, project_list, spheres, page_info, set_done
    notion.create_idea, ai.structure, schedule.occurrences = create_idea, structure, occurrences
    weekday = notion.WEEKDAYS[TODAY.weekday()]
    await timetable.save(42, {"anchor": "2026-10-12", "bells": True, "items": [
        {"days": [weekday], "start": "10:00", "end": "11:00", "title": "Китайский", "room": "", "num": "", "teacher": "",
         "lesson": True, "week": None, "tutor": True, "link": "https://telemost.yandex.ru/j/1", "homework": True},
        {"days": [weekday], "start": "12:00", "end": "13:00", "title": "Физика", "room": "онлайн", "num": "", "teacher": "",
         "lesson": True, "week": None},
    ]})  # fmt: skip
    timetable._cache.clear()
    app = await h.make_app()

    # 1. секрет и папка виджета: адрес вписан в index.jsx, README рядом
    token = await widget.secret()
    assert token == await widget.secret() and len(token) > 20
    archive = zipfile.ZipFile(io.BytesIO(widget.package(token)))
    jsx = archive.read("organizer.widget/index.jsx").decode()
    assert f"/w/{token}" in jsx and "__BOT_URL__" not in jsx and "organizer.widget/README.txt" in archive.namelist()
    hotkey = archive.read("Горячая клавиша для заметки.txt").decode()
    iphone = archive.read("iPhone — Мой день.js").decode()
    assert f"/w/{token}/add" in hotkey and f'"{widget.url(token)}"' in iphone and "__BOT" not in iphone + hotkey
    await app.process_update(h.text("/widget"))
    doc = next(d for e, d in reversed(h.sent) if e == "sendDocument")
    assert getattr(doc["document"], "filename", "") == "organizer.widget.zip" and "Übersicht" in doc["caption"], doc
    await app.process_update(h.text("/widget", user=h.person(77, "Аня")))
    assert next(d for e, d in reversed(h.sent) if e == "sendDocument") is doc, "только владелице"

    server = web.Application()
    server.add_routes(widget.routes(app.bot))
    client = TestClient(TestServer(server))
    await client.start_server()
    try:
        # 2. чужой адрес — 404; CORS для виджета
        assert (await client.get("/w/wrong/day")).status == 404
        r = await client.options(f"/w/{token}/add")
        assert r.headers["Access-Control-Allow-Origin"] == "*"

        # 3. день: «Моё расписание» + общее (строки 🎒 не дублируются), по времени; планы — горит → скоро → потом
        r = await client.get(f"/w/{token}/day?d={TODAY.isoformat()}")
        data = await r.json()
        assert r.headers["Access-Control-Allow-Origin"] == "*" and data["today"] and data["relative"] == "сегодня"
        assert [(i["start"], i["title"], i["kind"]) for i in data["items"]] == [
            ("10:00", "Китайский", "tutor"), ("12:00", "Физика", "lesson"), ("18:00", "🔖 Для себя", "routine")], data["items"]
        assert data["items"][0]["link"].startswith("https://telemost") and data["items"][1]["online"]
        assert [(p["title"], p["urgency"]) for p in data["plans"]] == [("Просрочено", "fire"), ("Большая", "soon"), ("Без срока", "later")]
        assert data["plans"][1]["steps"] == [{"id": "4" * 32, "title": "Шаг"}] and data["plans"][2]["where"] == "📚 Образование › Домашка"
        other = await (await client.get(f"/w/{token}/day?d={(TODAY + timedelta(days=1)).isoformat()}")).json()
        assert other["relative"] == "завтра" and not other["today"]

        # 4. ☐ — закрыть
        r = await client.post(f"/w/{token}/done", data=json.dumps({"id": "2" * 32}))
        assert (await r.json())["ok"] and closed == ["2" * 32]
        data = await (await client.get(f"/w/{token}/day?d={TODAY.isoformat()}")).json()
        assert "Просрочено" not in [p["title"] for p in data["plans"]], "кэш планов сброшен"

        # 5. ＋ заметка и ＋ событие (разово — репетитор в «Моё расписание»); непонятное — ошибка в виджет
        r = await client.post(f"/w/{token}/add", data=json.dumps({"text": "купить краски", "kind": "note"}))
        assert (await r.json())["ok"] and sent_notes == ["Купить краски"] and "новая заметка" in h.texts(chat=42)[-1]
        prepared = []

        async def prepare(uid, text, category, once, now, owner):
            prepared.append((text, category, once, owner))
            return {"route": "tt", "items": [timetable.make_item({}, "китайский", "19:00", "", tutor=True, day=TODAY.isoformat())],
                    "plan": {}, "text": "📌 19:00–20:00 — 🧑‍🏫 Китайский 🔗", "no_link": []}  # fmt: skip
        changes.prepare = prepare

        async def add_items(uid, items, today):
            prepared.append(("added", items[0]["date"]))
        timetable.add_items = add_items
        r = await client.post(f"/w/{token}/add", data=json.dumps({"text": "в пятницу 19 китайский", "kind": "tutor", "once": True}))
        assert (await r.json())["message"].startswith("📌 19:00") and prepared == [("в пятницу 19 китайский", "tutor", True, True), ("added", TODAY.isoformat())]
        assert "Из виджета добавила" in h.texts(chat=42)[-1]

        async def nothing(*a, **k):
            return {"route": "schedule", "plan": {}, "text": "", "no_link": []}
        changes.prepare = nothing
        r = await client.post(f"/w/{token}/add", data=json.dumps({"text": "ммм", "kind": "task", "once": False}))
        assert r.status == 400 and "Не поняла" in (await r.json())["error"]
        assert (await client.post(f"/w/{token}/add", data=json.dumps({"text": "x", "kind": "hack"}))).status == 400
        r = await client.post(f"/w/{token}/add", data=json.dumps({"text": "Введенный текст", "kind": "note"}))
        assert r.status == 400 and "переменн" in h.texts(chat=42)[-1], "подсказка, если в быстрой команде слова вместо переменной"

        # 6. ⏰ на завтра (время срока сохраняется), перетащенный порядок, настроение
        moved = []

        async def set_when(pid, when):
            moved.append((pid, when))
        notion.set_when = set_when
        notes[1]["when"] = "2026-10-20T15:00:00+03:00"
        r = await client.post(f"/w/{token}/tomorrow", data=json.dumps({"id": "2" * 32}))
        assert (await r.json())["ok"] and moved == [("2" * 32, f"{TODAY + timedelta(days=1)}T15:00:00+03:00")], moved
        await client.post(f"/w/{token}/tomorrow", data=json.dumps({"id": "1" * 32}))
        assert moved[-1] == ("1" * 32, (TODAY + timedelta(days=1)).isoformat())
        r = await client.post(f"/w/{token}/order", data=json.dumps({"ids": ["1" * 32, "3" * 32]}))
        assert (await r.json())["ok"]
        data = await (await client.get(f"/w/{token}/day?d={TODAY.isoformat()}")).json()
        assert [p["title"] for p in data["plans"]] == ["Без срока", "Большая"], "порядок, перетащенный руками"
        assert [x["id"] for x in widget._manual_order([{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}], ["d", "b"])] == ["a", "d", "c", "b"]
        r = await client.post(f"/w/{token}/mood", data=json.dumps({"score": 4}))
        assert (await r.json())["ok"] and (await notion.moods(42))[TODAY.isoformat()][0] == 4
        data = await (await client.get(f"/w/{token}/day?d={TODAY.isoformat()}")).json()
        assert data["mood"]["score"] == 4
        assert (await client.post(f"/w/{token}/mood", data=json.dumps({"score": 9}))).status == 400

        # 6. новый адрес — старый перестаёт работать
        new = await widget.secret(new=True)
        assert new != token and (await client.get(f"/w/{token}/day")).status == 404
        assert (await client.get(f"/w/{new}/day?d={TODAY.isoformat()}")).status == 200
    finally:
        await client.close()
    print("widget OK")


asyncio.run(run())
