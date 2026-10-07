"""Разбор в два шага: сначала «к какому проекту относится?», потом «что это за запись?»."""

import asyncio

import harness as h

from bot import main, notion

A, B, C = "a" * 32, "b" * 32, "c" * 32
items = {
    A: {"id": A, "title": "Идея для главной", "url": "u", "project": None, "when": None, "ai_type": "💡 Идея", "author_id": 42},
    B: {"id": B, "title": "Купить бумагу", "url": "u", "project": None, "when": None, "ai_type": None, "author_id": 42},
    C: {"id": C, "title": "Логотип", "url": "u", "project": "Фоны", "when": None, "ai_type": None, "author_id": 42},
}
order = [A, B, C]
rows = [{"id": "s1", "name": "Сайт", "status": notion.STATUS_ACTIVE, "kind": notion.KIND_SHARED, "sphere_id": None,
         "members": [], "creator": 42, "description": "", "url": ""},
        {"id": "s2", "name": "Фоны", "status": notion.STATUS_ACTIVE, "kind": notion.KIND_SHARED, "sphere_id": None,
         "members": [], "creator": 42, "description": "", "url": ""}]
filed = []


async def review_items(uid):
    return [dict(items[i]) for i in order]


async def preview(pid):
    return "текст"


async def page_info(pid):
    return dict(items[pid])


async def load(force=False):
    return [dict(r) for r in rows]


async def file_to_project(pid, name, skip_review=False):
    filed.append((pid, name))
    items[pid]["project"] = name
    return next(r for r in rows if r["name"] == name)


def card():
    d = next(d for e, d in reversed(h.sent) if e in ("sendMessage", "editMessageText"))
    return d["text"], [b.text for row in d["reply_markup"].inline_keyboard for b in row]


async def run():
    notion.review_items, notion.preview, notion.page_info = review_items, preview, page_info
    notion._load_projects, notion.file_to_project = load, file_to_project
    app = await h.make_app()

    # 1. заметка без проекта — сначала проект, типов ещё нет
    await app.process_update(h.text("/razbor"))
    text, buttons = card()
    assert "Шаг 1 из 2" in text and "К какому проекту" in text, text
    assert "📁 Сайт" in buttons and "📭 Ни к какому" in buttons and "✨ 💡 Идея" not in buttons, buttons

    # 2. выбрали проект → шаг 2: что это за запись, проект отмечен
    await app.process_update(h.callback(f"p:{A}:0:0"))
    assert filed == [(A, "Сайт")]
    text, buttons = card()
    assert "Шаг 2 из 2" in text and "📁 Сайт ✓" in text and "✨ 💡 Идея" in buttons, (text, buttons)

    # 3. «📭 Ни к какому» → сразу к типу, проект не ставится
    await app.process_update(h.callback(f"pn:{B}:1"))
    text, buttons = card()
    assert "Купить бумагу" in text and "Шаг 2 из 2" in text and "📭 Без проекта" in text and len(filed) == 1

    # 4. у заметки уже есть проект — сразу шаг 2
    await app.process_update(h.callback("r:2"))
    text, buttons = card()
    assert "Логотип" in text and "Шаг 2 из 2" in text and "📁 Фоны ✓" in buttons

    # 5. проектов нет вообще — один шаг, сразу тип
    rows.clear()
    await app.process_update(h.callback("r:1"))
    text, buttons = card()
    assert "Шаг" not in text and "Что это за запись?" in text
    print("review steps OK")


asyncio.run(run())
