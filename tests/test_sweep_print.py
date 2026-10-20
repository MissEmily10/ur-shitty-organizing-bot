"""🧽 Перебрать все (от старых к новым: закрыть, удалить, оставить, продолжить с того же места),
🧩 большая заметка → подзадачи (главная закрывается сама), 🖨 PDF для печати и ✏️ зачёркнутое по фото и номерами."""

import asyncio
import json

import harness as h

from bot import ai, main, notion, pdf

AREA = "a" * 32
projects = [{"id": "1" * 32, "name": "Домашка", "sphere_id": AREA, "kind": notion.KIND_SHARED, "creator": 42, "members": [],
             "status": notion.STATUS_ACTIVE, "description": "", "url": ""}]  # fmt: skip
pages: dict[str, dict] = {}
trashed: list[str] = []


def add(title, day, **kw):
    pid = f"{len(pages) + 1:032d}"
    pages[pid] = {"id": pid, "title": title, "url": f"https://n/{pid}", "project": None, "project_id": None, "type": None,
                  "ai_type": None, "when": None, "done": False, "author_id": 42, "author": "Владелица", "parent_id": None,
                  "created": f"2026-10-{day:02d}", "created_at": f"2026-10-{day:02d}T10:00:00.000Z", **kw}  # fmt: skip
    return pid


async def open_notes(uid, limit=500):
    return [dict(p) for p in sorted(pages.values(), key=lambda p: p["created_at"]) if not p["done"] and p["id"] not in trashed]


async def page_info(pid):
    return dict(pages[pid])


async def preview(pid, limit=600):
    return f"суть {pages[pid]['title']}"


async def children(pid):
    return [dict(p) for p in sorted(pages.values(), key=lambda p: p["created_at"]) if p["parent_id"] == pid and p["id"] not in trashed]


async def close_parent(pid):
    parent = pages[pid]["parent_id"]
    if parent and all(k["done"] for k in await children(parent)):
        pages[parent]["done"] = True


async def set_done(pid, value=True):
    pages[pid]["done"] = value
    if value:
        await close_parent(pid)


async def trash(pid):
    trashed.append(pid)
    await close_parent(pid)


created: list[dict] = []


async def create_idea(title, source, tags, blocks, details, author_id=0, author="", when=None, ai_type=None, parent=None):
    created.append({"title": title, "parent": parent, "type": ai_type, "when": when})
    pid = add(title, 30, parent_id=parent, ai_type=ai_type, when=when)
    pages[pid]["created_at"] = f"2026-10-30T10:{len(created):02d}:00.000Z"
    return pid, pages[pid]["url"]


sections: list[tuple[str, str]] = []


async def add_section(pid, title, blocks):
    sections.append((pid, title))


async def mark_reviewed(pid):
    pages[pid]["reviewed"] = True


async def project_list(uid=None, include_closed=False):
    return [dict(p) for p in projects]


async def spheres():
    return [{"id": AREA, "name": "📚 Образование", "description": ""}]


def last(endpoint="sendMessage"):
    return next(d for e, d in reversed(h.sent) if e == endpoint)


def kb(d):
    return [(b.text, b.callback_data) for row in d["reply_markup"].inline_keyboard for b in row]


def photo_update():
    msg = {"message_id": 5000, "date": 0, "chat": h.CHAT, "from": h.OWNER,
           "photo": [{"file_id": "P", "file_unique_id": "u", "width": 100, "height": 100}]}  # fmt: skip
    return h.Update.de_json({"update_id": 5001, "message": msg}, h.APP.bot)


async def run():
    notion.open_notes, notion.page_info, notion.preview, notion.children = open_notes, page_info, preview, children
    notion.set_done, notion.trash, notion.create_idea, notion.add_section = set_done, trash, create_idea, add_section
    notion.mark_reviewed, notion.project_list, notion.spheres = mark_reviewed, project_list, spheres
    old, mid, new = add("Старая", 1), add("Средняя", 5), add("Новая", 9)
    app = await h.make_app()

    # 1. /sweep — от самой старой; закрыть, удалить, оставить; «хватит» и продолжение с того же места
    await app.process_update(h.text("/sweep"))
    card = last()
    assert "1 из 3" in card["text"] and "Старая" in card["text"] and ("✅ Сделано, закрыть", f"sw:dn:{old}") in kb(card), card["text"]
    await app.process_update(h.callback(f"sw:dn:{old}"))
    assert pages[old]["done"] and "Средняя" in last("editMessageText")["text"]
    await app.process_update(h.callback(f"sw:k:{mid}"))
    assert "Новая" in last("editMessageText")["text"] and not pages[mid]["done"]
    await app.process_update(h.callback("sw:x"))
    assert "Осталось посмотреть: 1" in last("editMessageText")["text"]
    await app.process_update(h.text("/sweep"))
    assert "Новая" in last()["text"] and ("🔁 Начать с самой старой", "sw:re") in kb(last()), "продолжаем с того же места"
    await app.process_update(h.callback(f"sw:del:{new}"))
    assert new in trashed and "Дошли до самой новой" in last("editMessageText")["text"]
    await app.process_update(h.callback("sw:re"))
    assert "Средняя" in last("editMessageText")["text"], "сначала — опять с самой старой открытой"
    # срок из прохода возвращает в проход
    notion.set_when = lambda pid, when: asyncio.sleep(0)
    await app.process_update(h.callback(f"wv:{mid}:sw:d1"))
    assert "Перебираем заметки" in last("editMessageText")["text"]

    # 2. длинная заметка с разными задачами → предложение разделить → подзадачи, главная закрывается сама
    async def structure(text="", images=None, types=None, now=None):
        return ai.Idea(title="Планы на неделю", summary="- всё подряд")

    async def split_tasks(text, now, types):
        return [{"title": "Реферат по истории", "text": "Сдать в пятницу", "type": h.TYPES[1], "when": None},
                {"title": "Позвонить Ане", "text": "Про съёмку", "type": None, "when": "2026-10-31"}]  # fmt: skip
    ai.structure, ai.split_tasks = structure, split_tasks
    notion.created_today = lambda uid: asyncio.sleep(0, 0)
    await app.process_update(h.text("так, " + "дел много: реферат, Аня, съёмка, учёба. " * 12))
    big = created[-1]["title"] == "Планы на неделю" and next(p for p in pages if pages[p]["title"] == "Планы на неделю")
    offer = last()
    assert "2 разные задачи" in offer["text"] and ("✂️ Разделить", f"sp:y:{big}") in kb(offer), offer["text"]
    await app.process_update(h.callback(f"sp:y:{big}"))
    kids = [c for c in created if c["parent"] == big]
    assert [k["title"] for k in kids] == ["Реферат по истории", "Позвонить Ане"] and kids[0]["type"] == h.TYPES[1]
    assert pages[big].get("reviewed") and (big, "🧩 Подзадачи") in sections
    assert "Разделила на 2 заметки" in last("editMessageText")["text"]
    kid_ids = [p for p in pages if pages[p]["parent_id"] == big]
    await notion.set_done(kid_ids[0])
    assert not pages[big]["done"]
    await notion.trash(kid_ids[1])
    assert pages[big]["done"], "все подзадачи закрыты или удалены — главная закрылась сама"
    pages[big]["done"] = False
    pages[kid_ids[0]]["done"] = False
    trashed.remove(kid_ids[1])

    # 3. PDF для печати: сфера → проект, большая заметка с подзадачами, номера сохраняются
    pages[mid].update(project="Домашка", project_id="1" * 32, type="📋 Задача")
    await app.process_update(h.text("/printnotes"))
    doc = last("sendDocument")
    payload = doc["document"]
    content = bytes(payload.input_file_content if hasattr(payload, "input_file_content") else payload)
    assert content[:4] == b"%PDF" and "/crossed" in doc["caption"]
    printed = json.loads(h.service[f"print:{h.OWNER['id']}"])
    numbers = {int(k): v for k, v in printed["map"].items()}
    assert numbers[1] == mid, "сначала сферы, потом «без проекта»"
    assert [numbers[n] for n in (2, 3, 4)] == [big] + kid_ids, numbers

    # 4. ✏️ зачёркнутое: фото → номера → подтверждение → удалено; большая — вместе с подзадачами
    await app.process_update(h.text("/crossed"))
    assert last()["text"].startswith(main.CROSSED_PROMPT)

    async def download(msg):
        return b"jpg"

    async def crossed(images):
        assert images == [b"jpg"]
        return [2, 77]
    main._download_image, ai.crossed_numbers = download, crossed
    await app.process_update(photo_update())
    shown = last()
    assert "#2 Планы на неделю" in shown["text"] and "77" in shown["text"] and ("🗑 Удалить (1)", "cr:del") in kb(shown), shown["text"]
    await app.process_update(h.callback("cr:pick"))
    await app.process_update(h.callback("cr:t:2"))
    assert "⬜ #2" in last("editMessageText")["text"]
    await app.process_update(h.callback("cr:t:2"))
    await app.process_update(h.callback("cr:show"))
    await app.process_update(h.callback("cr:del"))
    assert big in trashed and all(k in trashed for k in kid_ids) and "Удалила 1 заметку" in last("editMessageText")["text"]
    # после подтверждения фото — снова обычные заметки; номера текстом тоже работают
    await app.process_update(h.text("/crossed"))
    await app.process_update(h.text("1"))
    await app.process_update(h.callback("cr:dn"))
    assert pages[mid]["done"] and "Закрыла 1 заметку" in last("editMessageText")["text"]

    # 5. в меню «📒 Все заметки» — новые кнопки
    text, rows = await main._section(42, "notes")
    text = text.replace("<b>", "").replace("</b>", "")
    for label in ("🧽 Перебрать все", "🖨 Печать", "✏️ Зачёркнутое"):
        assert label in text and any(b[0] == label for row in rows for b in row), label
    assert pdf.notes_pdf([], "x", "y")[:4] == b"%PDF"
    print("sweep print OK")


asyncio.run(run())
