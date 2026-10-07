"""Сферы: посев сфер владелицы, «К какой сфере?» при создании проекта (иконка — после ответа), разбор и кнопка
«📁 В проект» — сначала сфера, потом проект. Идентификаторы настоящей длины: кнопки не длиннее 64 байт."""

import asyncio

import harness as h

from bot import main, notion

areas: list[dict] = []
rows: list[dict] = []
filed: list[tuple] = []
icons: list[str] = []
NOTE = "e" * 32
note = {"id": NOTE, "title": "Решить задачи по алгебре", "url": "u", "project": None, "when": None, "ai_type": None, "author_id": 42}


def sid(n):
    return f"{n:032x}"


async def spheres():
    return [dict(a) for a in areas]


async def add_sphere(name, description=""):
    areas.append({"id": sid(100 + len(areas)), "name": name, "description": description})


def proj(name, sphere=None):
    rows.append({"id": sid(len(rows) + 1), "name": name, "status": notion.STATUS_ACTIVE, "kind": notion.KIND_SHARED,
                 "sphere_id": sphere, "members": [], "creator": 42, "description": "", "url": ""})


async def load(force=False):
    return [dict(r) for r in rows]


async def add_projects(names, uid=None):
    for n in names:
        proj(n)
    return names


async def update_project(pid, **kw):
    r = next(r for r in rows if r["id"] == pid)
    if kw.get("sphere_id") is not None:
        r["sphere_id"] = kw["sphere_id"] or None


async def file_to_project(pid, name, skip_review=False):
    filed.append((pid, name))
    note["project"] = name
    return next(r for r in rows if r["name"] == name)


async def review_items(uid):
    return [dict(note)]


async def page_info(pid):
    return dict(note)


async def preview(pid):
    return "текст"


async def fake_icon(message, project):
    icons.append(project["name"])


def last_buttons():
    d = next(d for e, d in reversed(h.sent) if hasattr(d.get("reply_markup"), "inline_keyboard"))
    return [(b.text, b.callback_data) for row in d["reply_markup"].inline_keyboard for b in row]


async def run():
    notion.spheres, notion.add_sphere, notion._load_projects = spheres, add_sphere, load
    notion.add_projects, notion.update_project, notion.file_to_project = add_projects, update_project, file_to_project
    notion.review_items, notion.page_info, notion.preview = review_items, page_info, preview
    main.auto_icon = fake_icon

    # 1. посев: четыре сферы владелицы, без дублей, один раз
    areas.append({"id": sid(99), "name": "фотография", "description": ""})
    assert await notion.seed_spheres() == "📚 Образование, 💼 Работа, 🏠 Бытовое"
    assert [a["name"] for a in areas] == ["фотография", "📚 Образование", "💼 Работа", "🏠 Бытовое"]
    areas.pop(1)
    assert await notion.seed_spheres() == "уже", "удалённая сфера не возвращается"
    areas.insert(1, {"id": sid(150), "name": "📚 Образование", "description": ""})

    app = await h.make_app()

    # 2. новый проект: «К какой сфере?», иконка — после ответа
    await app.process_update(h.text("/addproject Домашка"))
    assert "К какой сфере относится «Домашка»" in h.texts(chat=42)[-1] and not icons
    buttons = dict(last_buttons())
    assert buttons["📚 Образование"] == f"ps:{sid(1)}:1:n", buttons
    await app.process_update(h.callback(buttons["📚 Образование"]))
    assert rows[0]["sphere_id"] == sid(150) and icons == ["Домашка"]
    assert "Домашка» → 📚 Образование ✓" in h.texts("editMessageText")[-1]
    # в карточке проекта сфера меняется кнопками с номерами
    await app.process_update(h.callback(f"pr:{sid(1)}:sph"))
    assert ("💼 Работа", f"ps:{sid(1)}:2") in last_buttons()

    # 3. разбор: сначала сфера, потом проект в ней
    proj("Репетиторы", sid(150))
    proj("Аня", sid(99))
    proj("Разное")
    await app.process_update(h.text("/razbor"))
    text = h.texts(chat=42)[-1]
    labels = [t for t, _ in last_buttons()]
    assert "К какой сфере относится запись?" in text, text
    assert labels[:3] == ["фотография (1)", "📚 Образование (2)", "📁 Без сферы (1)"], labels
    group = dict(last_buttons())["📚 Образование (2)"]
    await app.process_update(h.callback(group))
    text = h.texts("editMessageText")[-1]
    labels = [t for t, _ in last_buttons()]
    assert "📚 Образование → какой проект?" in text and labels[:2] == ["📁 Домашка", "📁 Репетиторы"], (text, labels)
    assert "↩️ Другая сфера" in labels
    await app.process_update(h.callback(dict(last_buttons())["📁 Репетиторы"]))
    assert filed[-1] == (NOTE, "Репетиторы") and "Шаг 2 из 2" in h.texts("editMessageText")[-1]

    # 4. «📁 В проект» под заметкой — тоже сначала сфера
    note["project"] = None
    await app.process_update(h.callback(f"n:{NOTE}"))
    assert "фотография (1)" in [t for t, _ in last_buttons()]
    await app.process_update(h.callback(f"ns:{NOTE}:0"))
    assert [t for t, _ in last_buttons()][:2] == ["📁 Аня", "↩️ Сферы"]
    await app.process_update(h.callback(dict(last_buttons())["📁 Аня"]))
    assert filed[-1] == (NOTE, "Аня")

    # 5. все проекты в одной группе — сразу проекты
    for r in rows:
        r["sphere_id"] = None
    note["project"] = None
    await app.process_update(h.text("/razbor"))
    assert "К какому проекту относится запись?" in h.texts(chat=42)[-1]
    print("spheres OK")


asyncio.run(run())
