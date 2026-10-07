"""Этап 10: ИИ-иконки проектов — сами при создании, «Другой вариант», «Без иконки», права, лимит, сбой генератора."""

import asyncio

import harness as h

from bot import ai, main, notion

rows: list[dict] = []
icons: dict[str, str | None] = {}
prompts: list[tuple] = []
fail = [False]


def proj(pid, name, kind=notion.KIND_SHARED, creator=42):
    rows.append({"id": pid, "name": name, "status": notion.STATUS_ACTIVE, "kind": kind, "sphere_id": None,
                 "members": [], "creator": creator, "description": "", "url": ""})


async def load(force=False):
    return [dict(r) for r in rows]


async def add_projects(names, uid=None):
    for n in names:
        proj(f"p{len(rows)}", n, notion.KIND_SHARED if uid == 42 else notion.KIND_PERSONAL, uid)
    return names


async def icon_prompt(name, about, attempt=0):
    prompts.append((name, attempt))
    return f"icon of {name}"


async def draw(prompt, size=512):
    if fail[0]:
        raise RuntimeError("model is loading")
    return b"\x89PNG fake"


async def upload_file(data, filename, mime):
    assert mime == "image/png"
    return "up-1"


async def set_icon(pid, upload):
    icons[pid] = upload


def photos():
    return [d for e, d in h.sent if e == "sendPhoto"]


async def run():
    notion._load_projects, notion.add_projects, notion.upload_file, notion.set_icon = load, add_projects, upload_file, set_icon
    ai.icon_prompt, ai.draw = icon_prompt, draw
    main.c.AUTO_ICONS = True
    main.member.add_user_ids([77])
    anya = h.person(77, "Аня")
    app = await h.make_app()

    # 1. при создании проекта иконка рисуется сама и ставится в Notion
    await app.process_update(h.text("/addproject Сайт студии"))
    assert icons == {"p0": "up-1"} and prompts == [("Сайт студии", 0)]
    kb = photos()[-1]["reply_markup"].inline_keyboard
    assert [b.callback_data for row in kb for b in row] == ["ic:p0:ok", "ic:p0:new", "ic:p0:no"]

    # 2. другой вариант — новая попытка с другим образом; без иконки — убрать
    await app.process_update(h.callback("ic:p0:new"))
    assert prompts[-1] == ("Сайт студии", 1) and len(photos()) == 2
    await app.process_update(h.callback("ic:p0:no"))
    assert icons["p0"] is None

    # 3. чужой проект участница не трогает; свой личный — можно
    await app.process_update(h.callback("ic:p0:new", user=anya))
    assert len(photos()) == 2
    await app.process_update(h.text("/addproject Моё", user=anya))
    assert icons.get("p1") == "up-1" and photos()[-1]["chat_id"] == 77

    # 4. генератор недоступен — проект создан, бот объясняет, как повторить
    fail[0] = True
    await app.process_update(h.text("/addproject Фоны"))
    assert "p2" not in icons and "не получилось" in h.texts(chat=42)[-1], h.texts(chat=42)[-1]
    fail[0] = False

    # 5. можно выключить: AUTO_ICONS=0
    main.c.AUTO_ICONS = False
    await app.process_update(h.text("/addproject Тихий"))
    assert "p3" not in icons
    # но по кнопке в карточке — рисуется
    await app.process_update(h.callback("ic:p3:new"))
    assert icons.get("p3") == "up-1"

    # 6. стиль читается из design/icon_style.md, без шапки с пояснениями
    style = ai.icon_style()
    assert style.startswith("flat minimal") and "Бот читает" not in style, style
    print("icons OK")


asyncio.run(run())
