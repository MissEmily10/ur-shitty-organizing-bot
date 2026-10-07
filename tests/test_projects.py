"""Этап 9а: проекты-страницы — перенос старых меток, видимость, карточка, закрытие, сферы, права, контекст для ИИ."""

import asyncio
import json

import httpx

import harness as h

from bot import main, notion

# ---------- 1. перенос старых проектов-меток (на заглушке Notion API) ----------


async def migration():
    state = {"ideas_props": {"Название": {"type": "title"}, "Проект": {"type": "select", "select": {"options": [{"name": "Сайт"}, {"name": "Фоны"}]}}},
             "projects": [], "notes": [{"id": "n1", "old": "Сайт"}, {"id": "n2", "old": "Фоны"}, {"id": "n3", "old": None}], "patched": {}}

    def handler(req):
        body = json.loads(req.content or b"{}")
        path = req.url.path.replace("/v1", "")
        if path == "/databases/IDEAS" and req.method == "GET":
            return httpx.Response(200, json={"id": "IDEAS", "parent": {"page_id": "PARENT"}, "properties": state["ideas_props"]})
        if path == "/databases/IDEAS" and req.method == "PATCH":
            for name, spec in body["properties"].items():
                if "name" in spec:  # переименование
                    state["ideas_props"][spec["name"]] = state["ideas_props"].pop(name)
                else:
                    state["ideas_props"][name] = {"type": "relation"}
            return httpx.Response(200, json={})
        if path == "/search":
            return httpx.Response(200, json={"results": []})
        if path == "/databases" and req.method == "POST":
            title = body["title"][0]["text"]["content"]
            return httpx.Response(200, json={"id": {"Проекты": "PROJ", "Сферы": "SPH"}[title]})
        if path == "/pages" and req.method == "POST":
            name = body["properties"]["Название"]["title"][0]["text"]["content"]
            p = {"id": f"proj-{len(state['projects'])}", "url": "u", "properties": {"Название": {"title": [{"plain_text": name}]}}}
            state["projects"].append(p)
            return httpx.Response(200, json=p)
        if path == "/databases/PROJ/query":
            return httpx.Response(200, json={"results": state["projects"], "has_more": False})
        if path == "/databases/IDEAS/query":
            rows = [{"id": n["id"], "properties": {"Проект (старое)": {"select": {"name": n["old"]}}}} for n in state["notes"] if n["old"]]
            return httpx.Response(200, json={"results": rows, "has_more": False})
        if path.startswith("/pages/") and req.method == "PATCH":
            state["patched"][path.split("/")[-1]] = body["properties"]["Проект"]["relation"][0]["id"]
            return httpx.Response(200, json={})
        return httpx.Response(200, json={})

    orig = httpx.AsyncClient
    httpx.AsyncClient = lambda **k: orig(transport=httpx.MockTransport(handler), **k)
    notion._load_projects = h.REAL["_load_projects"]
    notion.c.NOTION_DATABASE_ID = "IDEAS"
    notion._db_id, notion._projects_cache = None, None
    notion._extra_dbs.clear()
    report = await notion.ensure_projects()
    assert "перенесено проектов: 2" in report and "привязано заметок: 2" in report, report
    props = state["ideas_props"]
    assert props["Проект (старое)"]["type"] == "select" and props["Проект"]["type"] == "relation", props
    assert state["patched"] == {"n1": "proj0", "n2": "proj1"}, state["patched"]
    assert await notion.ensure_projects() == "ok"
    httpx.AsyncClient = orig


# ---------- 2–4. видимость, карточка, права (через бота) ----------

rows: list[dict] = []
areas: list[dict] = [{"id": "a1", "name": "📷 Фотограф", "description": "портреты, Lightroom"}]
trashed: list[str] = []


def proj(pid, name, kind=notion.KIND_SHARED, creator=42, members=(), status=notion.STATUS_ACTIVE, sphere=None, desc=""):
    rows.append({"id": pid, "name": name, "status": status, "kind": kind, "sphere_id": sphere, "members": list(members),
                 "creator": creator, "description": desc, "url": f"https://n/{pid}"})


async def load(force=False):
    return [dict(r) for r in rows if r["id"] not in trashed]


async def update_project(pid, **kw):
    r = next(r for r in rows if r["id"] == pid)
    for key, value in kw.items():
        if value is not None:
            r[{"sphere_id": "sphere_id"}.get(key, key)] = value or None if key == "sphere_id" else value


async def create_project(name, kind, creator, members=None):
    proj(f"p{len(rows)}", name, kind, creator, members or [])
    return rows[-1]


async def spheres():
    return list(areas)


async def add_sphere(name, description=""):
    areas.append({"id": f"a{len(areas) + 1}", "name": name, "description": description})


async def update_sphere(sid, description):
    next(a for a in areas if a["id"] == sid)["description"] = description


async def note_count(pid):
    return 3


async def trash(pid):
    trashed.append(pid)


def btns():
    for e, d in reversed(h.sent):
        m = d.get("reply_markup")
        if hasattr(m, "inline_keyboard"):
            return [b.text for row in m.inline_keyboard for b in row]
    return []


async def bot_flows():
    notion._load_projects, notion.update_project, notion.create_project = load, update_project, create_project
    notion.spheres, notion.add_sphere, notion.update_sphere, notion.project_note_count, notion.trash = spheres, add_sphere, update_sphere, note_count, trash
    proj("s1", "Сайт", sphere="a1", desc="лендинг фотостудии")
    proj("s2", "Фоны")
    proj("x1", "Личное Ани", notion.KIND_PERSONAL, creator=77)
    proj("t1", "Командный", notion.KIND_TEAM, creator=42, members=[77])
    proj("c1", "Старый", status=notion.STATUS_CLOSED)
    anya = h.person(77, "Аня")
    main.member.add_user_ids(77)

    # видимость
    assert await notion.projects(42) == ["Сайт", "Фоны", "Личное Ани", "Командный"]
    assert await notion.projects(77) == ["Сайт", "Фоны", "Личное Ани", "Командный"]
    main.member.add_user_ids(88)
    assert await notion.projects(88) == ["Сайт", "Фоны"], "чужое личное и командный без участия не видно"

    app = await h.make_app()
    await app.process_update(h.text("/projects"))
    assert "— 📷 Фотограф —" in btns() and "✅ Закрытые" in btns(), btns()
    # карточка, описание, сфера
    await app.process_update(h.callback("pr:s1"))
    card = h.texts(chat=42)[-1]
    assert "Сайт" in card and "лендинг фотостудии" in card and "📷 Фотограф" in card and "Заметок: 3" in card, card
    # закрыть → пропал из кнопок, но виден в «Закрытых»
    await app.process_update(h.callback("pr:s1:close"))
    assert "Сайт" not in await notion.projects(42)
    await app.process_update(h.callback("plc"))
    assert "Сайт" in btns()
    await app.process_update(h.callback("pr:s1:open"))
    assert "Сайт" in await notion.projects(42)
    # описание ответом
    await app.process_update(h.text("сайт для студии, тёплые цвета", reply_to=h._message(f"{main.PROJECT_DESC_MARK}Фоны»: напишите…")))
    assert next(r for r in rows if r["id"] == "s2")["description"] == "сайт для студии, тёплые цвета"
    # сфера: новая через /addarea и привязка
    await app.process_update(h.text("/addarea 🎨 Дизайнер — айдентика и веб"))
    assert areas[-1] == {"id": "a2", "name": "🎨 Дизайнер", "description": "айдентика и веб"}, areas
    await app.process_update(h.callback("ps:s2:1"))  # сфера по номеру: 1 — вторая, «🎨 Дизайнер»
    assert next(r for r in rows if r["id"] == "s2")["sphere_id"] == "a2"
    # контекст для ИИ
    ctx = await main.project_context("Сайт")
    assert ctx == "Сайт — лендинг фотостудии. Сфера деятельности: 📷 Фотограф — портреты, Lightroom", ctx

    # права участницы
    before = dict(next(r for r in rows if r["id"] == "s2"))
    await app.process_update(h.callback("pr:s2:close", user=anya))
    assert next(r for r in rows if r["id"] == "s2")["status"] == before["status"], "чужой общий проект участница не закрывает"
    await app.process_update(h.callback("pr:s2:delok", user=anya))
    assert "s2" not in trashed
    await app.process_update(h.callback("pr:x1:close", user=anya))
    assert next(r for r in rows if r["id"] == "x1")["status"] == notion.STATUS_CLOSED, "своё личное — можно"
    await app.process_update(h.text("/addproject Аня-проект", user=anya))
    assert rows[-1]["name"] == "Аня-проект" and rows[-1]["kind"] == notion.KIND_PERSONAL and rows[-1]["creator"] == 77
    assert "личные" in h.texts(chat=77)[-2] and "К какой сфере" in h.texts(chat=77)[-1]
    # владелица делает проект общим/личным и удаляет
    await app.process_update(h.callback("pr:s2:kind"))
    assert next(r for r in rows if r["id"] == "s2")["kind"] == notion.KIND_PERSONAL
    await app.process_update(h.callback("pr:s2:delok"))
    assert "s2" in trashed
    print("projects OK")


async def run():
    await migration()
    await bot_flows()


asyncio.run(run())
