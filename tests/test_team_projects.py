"""Этап 9б: командные проекты — создание, участники, «📌 Писать в», уведомления, лента, права на чтение, сводка."""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import harness as h

from bot import ai, main, notion, scheduler, team

rows: list[dict] = []
people = [{"tg": 77, "name": "Аня"}, {"tg": 88, "name": "Петя"}]
notes: dict[str, dict] = {}
filed: list[tuple] = []
appended: list[str] = []
summaries: list[list[str]] = []


def alert():
    """Текст последнего всплывающего окна (ошибки кнопок показываются так)."""
    return next((d.get("text", "") for e, d in reversed(h.sent) if e == "answerCallbackQuery"), "")


def proj(pid, name, kind=notion.KIND_SHARED, creator=42, members=()):
    rows.append({"id": pid, "name": name, "status": notion.STATUS_ACTIVE, "kind": kind, "sphere_id": None,
                 "members": list(members), "creator": creator, "description": "", "url": f"https://n/{pid}"})


async def load(force=False):
    return [dict(r) for r in rows]


async def create_project(name, kind, creator, members=None):
    proj(f"p{len(rows)}", name, kind, creator, members or [])
    return dict(rows[-1])


async def update_project(pid, **kw):
    r = next(r for r in rows if r["id"] == pid)
    r.update({k: v for k, v in kw.items() if v is not None})


async def members():
    return list(people)


async def note_count(pid):
    return len([n for n in notes.values() if n["project_id"] == pid])


async def page_info(pid):
    n = notes[pid]
    return {**n, "project": next((r["name"] for r in rows if r["id"] == n["project_id"]), None)}


async def create_idea(title, source, tags, blocks, details, author_id=0, author="", **kw):
    pid = f"{len(notes) + 1:032d}"
    notes[pid] = {"id": pid, "title": title, "url": f"https://n/{pid}", "author_id": author_id, "author": author,
                  "project_id": None, "when": None, "type": None, "created": "2026-10-05"}
    return pid, notes[pid]["url"]


async def structure(text="", images=None, **kw):
    return ai.Idea(title=text[:30], summary=text)


async def created_today(uid):
    return 0


async def file_to_project(pid, name, skip_review=False):
    project = next(r for r in rows if r["name"] == name)
    notes[pid]["project_id"] = project["id"]
    filed.append((pid, name, skip_review))
    return dict(project)


async def notes_in_scope(uid, project=None, days=None, limit=150):
    found = next(r for r in rows if r["name"] == project)
    if not notion.can_see(found, uid):
        return []
    return [{**n, "created": n["created"]} for n in notes.values() if n["project_id"] == found["id"]][:limit]


async def read_note(pid):
    return {"summary": f"текст {notes[pid]['title']}", "details": "", "images": []}


async def project_summary(texts, name, about=""):
    summaries.append(texts)
    return "- главное: всё идёт по плану", False


async def append(block_id, blocks):
    appended.append(block_id)


async def run():
    notion._load_projects, notion.create_project, notion.update_project = load, create_project, update_project
    notion.members, notion.project_note_count, notion.page_info = members, note_count, page_info
    notion.create_idea, notion.created_today, notion.file_to_project = create_idea, created_today, file_to_project
    notion.notes_in_scope, notion.read_note, notion._append = notes_in_scope, read_note, append
    ai.structure, ai.project_summary = structure, project_summary
    main.member.add_user_ids([77, 88])
    anya, petya = h.person(77, "Аня"), h.person(88, "Петя")
    app = await h.make_app()

    # 1. создать может только владелица
    await app.process_update(h.text("/teamproject Чужой", user=anya))
    assert "только владелица" in h.texts(chat=77)[-1] and not rows
    await app.process_update(h.text("/teamproject"))
    assert h.texts(chat=42)[-1] == main.TEAM_PROMPT
    await app.process_update(h.text("Сайт студии", reply_to=h._message(main.TEAM_PROMPT)))
    team_p = rows[-1]
    assert team_p["name"] == "Сайт студии" and team_p["kind"] == notion.KIND_TEAM and team_p["creator"] == 42
    assert "▫️ Аня" in [b.text for row in h.sent[-1][1]["reply_markup"].inline_keyboard for b in row]
    pid = team_p["id"]

    # 2. участники: Аню добавили — ей пришло сообщение; подделанное нажатие участницы не меняет состав
    await app.process_update(h.callback(f"tm:{pid}:77"))
    assert rows[-1]["members"] == [77] and "Вас добавили" in h.texts(chat=77)[-1]
    await app.process_update(h.callback(f"tm:{pid}:88", user=anya))
    assert rows[-1]["members"] == [77], "добавлять людей участница не может"
    await app.process_update(h.callback(f"tm:{pid}:12345"))
    assert rows[-1]["members"] == [77], "в проект можно добавить только участника бота"
    assert await notion.projects(77) == ["Сайт студии"] and await notion.projects(88) == []

    # 3. «📌 Писать сюда»: заметка Ани сразу в проекте, без разбора, владелице пришло уведомление
    await app.process_update(h.callback(f"pr:{pid}:pin", user=anya))
    assert (await scheduler.get_settings(77))["active_project"] == pid
    await app.process_update(h.text("Идея для главной страницы", user=anya))
    note_id = filed[-1][0]
    assert filed[-1][1:] == ("Сайт студии", True), filed
    assert "📌 Сразу в «Сайт студии»" in h.texts(chat=77)[-1]
    notice = h.texts(chat=42)[-1]
    assert "«Сайт студии»: новая заметка" in notice and "Аня" in notice, notice
    # Петя не в проекте и уведомлений не получает
    assert not any("Сайт студии" in t for t in h.texts(chat=88))

    # 4. уведомления можно выключить
    await app.process_update(h.callback("st:tn"))
    before = len(h.texts(chat=42))
    await app.process_update(h.text("Вторая идея", user=anya))
    assert len(h.texts(chat=42)) == before, "владелица выключила новости команды"
    await app.process_update(h.callback("st:tn"))

    # 5. «📌 Входящие» в настройках возвращает обычный режим
    await app.process_update(h.callback("st:ap:off", user=anya))
    await app.process_update(h.text("Личная мысль", user=anya))
    assert filed[-1][0] != f"{len(notes):032d}", "без 📌 заметка не попадает в проект"

    # 6. чтение: участница видит заметки командного проекта, посторонний — нет
    own = next(n for n in notes.values() if n["project_id"] is None)
    async def show(message, page_id):
        await message.reply_text(f"показ {page_id}")
    main.show_note = show
    await app.process_update(h.callback(f"fv:{note_id}"))
    assert h.texts(chat=42)[-1] == f"показ {note_id}"
    await app.process_update(h.callback(f"fv:{note_id}", user=petya))
    assert "недоступ" in alert(), alert()
    await app.process_update(h.callback(f"fv:{own['id']}"))
    assert h.texts(chat=42)[-1] == f"показ {own['id']}", "владелица читает любые"
    await app.process_update(h.callback(f"fv:{own['id']}", user=petya))
    assert "недоступ" in alert()
    # удалять чужое по-прежнему нельзя, даже участнице проекта
    trashed = []
    async def trash(page_id):
        trashed.append(page_id)
    notion.trash = trash
    await app.process_update(h.callback(f"d:{note_id}:0", user=h.person(88, "Петя")))
    notes[note_id]["author_id"] = 42  # заметка владелицы в командном проекте
    await app.process_update(h.callback(f"d:{note_id}:0", user=anya))
    assert not trashed and "не ваша" in alert(), alert()
    notes[note_id]["author_id"] = 77

    # 7. лента и карточка
    await app.process_update(h.text("/feed Сайт студии", user=anya))
    feed = h.texts(chat=77)[-1]
    assert "Идея для главной" in feed and "Аня" in feed, feed
    await app.process_update(h.text("/feed Сайт студии", user=petya))
    assert "нет" in h.texts(chat=88)[-1]
    await app.process_update(h.callback(f"pr:{pid}"))
    card = h.texts(chat=42)[-1]
    assert "В проекте: владелица, Аня" in card, card

    # 8. сводка по кнопке: заметки с авторами
    await app.process_update(h.callback(f"fs:{pid}", user=anya))
    assert "автор: Аня" in summaries[-1][0] and "Сводка" in h.texts(chat=77)[-1]

    # 9. недельная сводка: одна на проект, всем участникам, плюс на страницу проекта
    now = datetime(2026, 10, 12, 10, 5, tzinfo=ZoneInfo("Europe/Moscow"))
    count = len(summaries)
    scheduler.JOBS = [j for j in scheduler.JOBS if j.name == "team_summary"]

    async def nothing(*a, **k):
        return []
    scheduler._reminders, notion.forget_old_marks = nothing, nothing
    report = await scheduler.tick(app.bot, [42, 77, 88], now=now)
    assert len(summaries) == count + 1, ("сводка считается один раз", report)
    assert "Сводка за неделю" in h.texts(chat=42)[-1] and "Сводка за неделю" in h.texts(chat=77)[-1]
    assert not any("Сводка за неделю" in t for t in h.texts(chat=88))
    assert appended == [pid]
    await scheduler.tick(app.bot, [42, 77, 88], now=now)
    assert len(summaries) == count + 1, "повторный тик не дублирует"

    # 10. сделать обычный проект командным
    proj("s1", "Фоны")
    await app.process_update(h.callback("pr:s1:team"))
    assert rows[-1]["kind"] == notion.KIND_TEAM
    await app.process_update(h.callback("pr:s1:team", user=anya))
    print("team projects OK")


asyncio.run(run())
