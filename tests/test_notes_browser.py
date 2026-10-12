"""📒 Все заметки (список, фильтры, карточка: закрыть, срок, проект, тип, удалить), 📰 лента по сферам,
стиль в новых местах (подтверждения, «пусто», вопросы бота) и баннеры под стиль."""

import asyncio
import io
import tempfile
from pathlib import Path

from PIL import Image

import harness as h

from bot import banners, main, notion, scheduler, styles

AREA, AREA2 = "a" * 32, "b" * 32
areas = [{"id": AREA, "name": "📚 Образование", "description": ""}, {"id": AREA2, "name": "📷 Фотография", "description": ""}]
projects = [
    {"id": "1" * 32, "name": "Домашка", "sphere_id": AREA, "kind": notion.KIND_SHARED, "creator": 42, "members": []},
    {"id": "2" * 32, "name": "Курсы", "sphere_id": AREA, "kind": notion.KIND_TEAM, "creator": 42, "members": [77]},
    {"id": "3" * 32, "name": "Аня", "sphere_id": AREA2, "kind": notion.KIND_SHARED, "creator": 42, "members": []},
]
for p in projects:
    p.update(status=notion.STATUS_ACTIVE, description="", url="")
notes = []
for i in range(1, 13):
    proj = projects[(i - 1) % 3]
    notes.append({"id": f"{i:032d}", "title": f"Заметка {i}", "url": f"https://n/{i}", "project": proj["name"], "project_id": proj["id"],
                  "type": "📋 Задача" if i % 2 else "💡 Идея", "ai_type": None, "when": None, "done": False, "author_id": 42,
                  "author": "Владелица", "created": f"2026-10-{i:02d}"})
notes[1].update(author_id=77, author="Аня")  # в командном «Курсы» — заметка участницы
queries: list[dict] = []
trashed: list[str] = []


async def browse(uid, project_ids=None, type_name=None, done=False, limit=100):
    queries.append({"projects": project_ids, "type": type_name, "done": done})
    out = [n for n in notes if n["id"] not in trashed and n["author_id"] == uid]
    if done is not None:
        out = [n for n in out if n["done"] == done]
    if type_name:
        out = [n for n in out if n["type"] == type_name]
    if project_ids is not None:
        out = [n for n in out if (n["project_id"] in project_ids) if project_ids] if project_ids else [n for n in out if not n["project_id"]]
    return [dict(n) for n in sorted(out, key=lambda n: n["created"], reverse=True)]


async def in_projects(ids, limit=40):
    return [dict(n) for n in sorted(notes, key=lambda n: n["created"], reverse=True) if n["project_id"] in ids]


async def page_info(pid):
    return dict(next(n for n in notes if n["id"] == pid))


async def preview(pid, limit=600):
    return "текст"


async def set_done(pid, value=True):
    next(n for n in notes if n["id"] == pid)["done"] = value


async def set_type(pid, name):
    next(n for n in notes if n["id"] == pid)["type"] = name


async def set_when(pid, when):
    next(n for n in notes if n["id"] == pid)["when"] = when


async def file_to_project(pid, name, skip_review=False):
    project = next(p for p in projects if p["name"] == name)
    next(n for n in notes if n["id"] == pid).update(project=name, project_id=project["id"])
    return project


async def trash(pid):
    trashed.append(pid)


async def load(force=False):
    return [dict(p) for p in projects]


async def spheres():
    return [dict(a) for a in areas]


def last(endpoint):
    return next(d for e, d in reversed(h.sent) if e == endpoint)


def kb(d):
    return [(b.text, b.callback_data) for row in d["reply_markup"].inline_keyboard for b in row]


async def run():
    notion.browse_notes, notion.notes_in_projects, notion.page_info, notion.preview = browse, in_projects, page_info, preview
    notion.set_done, notion.set_type, notion.set_when, notion.file_to_project, notion.trash = set_done, set_type, set_when, file_to_project, trash
    notion._load_projects, notion.spheres = load, spheres
    app = await h.make_app()

    # 1. /notes — открытые, новые сверху, по 8, с перелистыванием
    await app.process_update(h.text("/notes"))
    page1 = last("sendMessage")
    assert "Заметки: все</b> · открытые · 11" in page1["text"] and "1. ⏰" not in page1["text"], page1["text"]
    assert page1["text"].index("Заметка 12") < page1["text"].index("Заметка 11")
    assert ("▶️", "nb:pg:1") in kb(page1) and kb(page1)[0] == ("1", f"nc:{12:032d}")
    await app.process_update(h.callback("nb:pg:1"))
    assert "9." in last("editMessageText")["text"] and "стр. 2/2" in last("editMessageText")["text"]

    # 2. фильтры: сфера, тип, закрытые
    await app.process_update(h.callback("nb:f"))
    assert ("📚 Образование", "nb:s:0") in kb(last("editMessageText")) and ("💡 Идея", "nb:t:0") in kb(last("editMessageText"))
    await app.process_update(h.callback("nb:s:0"))
    assert queries[-1]["projects"] == ["1" * 32, "2" * 32] and "📚 Образование" in last("editMessageText")["text"]
    await app.process_update(h.callback("nb:t:1"))
    assert queries[-1]["type"] == h.TYPES[1]
    await app.process_update(h.callback("nb:cl"))
    assert queries[-1]["done"] is True and "закрытые" in last("editMessageText")["text"]
    assert "Здесь пока пусто" in last("editMessageText")["text"]
    await app.process_update(h.callback("nb:cl"))

    # 3. карточка: закрыть, тип, проект (через сферы), срок, удалить
    nid = f"{4:032d}"
    await app.process_update(h.callback(f"nc:{nid}"))
    card = last("editMessageText")
    assert "Заметка 4" in card["text"] and "▫️ открыта" in card["text"] and ("✅ Закрыть", f"nc:{nid}:dn") in kb(card)
    await app.process_update(h.callback(f"nc:{nid}:dn"))
    assert notes[3]["done"] and "✅ закрыта" in last("editMessageText")["text"]
    await app.process_update(h.callback(f"nc:{nid}:un"))
    assert not notes[3]["done"]
    await app.process_update(h.callback(f"nc:{nid}:tt:4"))
    assert notes[3]["type"] == h.TYPES[4]
    await app.process_update(h.callback(f"nc:{nid}:pj"))
    assert [t for t, _ in kb(last("editMessageReplyMarkup"))][:2] == ["📚 Образование (2)", "📷 Фотография (1)"]
    await app.process_update(h.callback(f"nc:{nid}:pg:1"))
    await app.process_update(h.callback(dict(kb(last("editMessageReplyMarkup")))["📁 Аня"]))
    assert notes[3]["project"] == "Аня"
    await app.process_update(h.callback(f"wv:{nid}:c:d1"))
    assert notes[3]["when"] and "⏰" in last("editMessageText")["text"], "срок из карточки возвращает в карточку"
    await app.process_update(h.callback(f"nc:{nid}:delok"))
    assert trashed == [nid] and "Заметки:" in last("editMessageText")["text"]
    # чужая заметка — нельзя
    main.member.add_user_ids(77)
    await app.process_update(h.callback(f"nc:{nid}:dn", user=h.person(77, "Аня")))
    assert not notes[3]["done"]

    # 4. /feed — по сферам; лента сферы: свои + заметки команды; номер — карточка или просмотр
    await app.process_update(h.text("/feed"))
    assert ("📚 Образование", "fd:s:0") in kb(last("sendMessage"))
    await app.process_update(h.callback("fd:s:0"))
    feed = last("editMessageText")
    assert "📚 Образование" in feed["text"] and "Заметка 2" in feed["text"] and "Заметка 3" not in feed["text"]
    assert ("📁 Домашка", "fd:" + "1" * 32) in kb(feed) and ("📒 Заметки сферы", "nb:s:0") in kb(feed)
    await app.process_update(h.text("/feed образование"))
    assert "📚 Образование" in last("sendMessage")["text"]
    # участница видит в ленте только командные заметки и свои
    await app.process_update(h.callback("fd:s:0", user=h.person(77, "Аня")))
    anya_feed = last("editMessageText")["text"]
    assert "Заметка 2" in anya_feed and "Заметка 1<" not in anya_feed and "Заметка 4" not in anya_feed, anya_feed

    # 5. стиль везде: «готово» на кнопке, «пусто», вопросы бота (и ответ на них всё равно узнаётся)
    await scheduler.update_settings(42, style="pirate")
    await app.process_update(h.callback("st:tn"))
    assert last("answerCallbackQuery")["text"].split(" Сохранено")[0] in styles.BUILTIN["pirate"]["pack"]["ok"]
    await app.process_update(h.callback("nb:s:1"))
    await app.process_update(h.callback("nb:cl"))
    assert any(p in last("editMessageText")["text"] for p in styles.BUILTIN["pirate"]["pack"]["empty"])
    await app.process_update(h.text("/addtype"))
    asked = h.texts(chat=42)[-1]
    assert asked.startswith(main.TYPE_PROMPT) and any(p in asked for p in styles.BUILTIN["pirate"]["pack"]["ask"])
    added = []

    async def add_types(names):
        added.extend(names)
        return names
    notion.add_types = add_types
    await app.process_update(h.text("🎬 Видео", reply_to=h._message(asked)))
    assert added == ["🎬 Видео"], "ответ на вопрос со стилевой репликой узнаётся"
    assert any(p in h.texts(chat=42)[-1] for p in styles.BUILTIN["pirate"]["pack"]["ok"])

    # 6. баннеры под стиль: файл стиля → общий → заглушка с акцентом стиля
    folder = Path(tempfile.mkdtemp())
    (folder / "styles" / "pirate").mkdir(parents=True)
    (folder / "styles" / "pirate" / "banner_menu.png").write_bytes(b"PIRATE")
    (folder / "banner_menu.png").write_bytes(b"COMMON")
    banners.ASSETS, banners._cache = folder, {}
    assert await banners.get("menu", "pirate") == b"PIRATE" and await banners.get("menu", "kawaii") == b"COMMON"
    assert await banners.get("menu") == b"COMMON"
    kawaii = Image.open(io.BytesIO(await banners.get("projects", "kawaii"))).convert("RGB")
    plain = Image.open(io.BytesIO(await banners.get("projects"))).convert("RGB")
    assert kawaii.size == (1280, 640) and kawaii.getpixel((1200, 100)) != plain.getpixel((1200, 100)), "у стиля свой акцент"
    assert Image.open(io.BytesIO(banners.template_png())).size == (1280, 640)
    print("notes browser OK")


asyncio.run(run())
