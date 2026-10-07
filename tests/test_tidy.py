"""🧹 Навести порядок: разовое утреннее предложение, план ИИ (с проверкой ответа), «разложить всё» и «по шагам»."""

import asyncio
import json
from datetime import datetime, timezone

import harness as h

from bot import ai, main, notion, scheduler

areas = [{"id": "a" * 32, "name": "📚 Образование", "description": ""}, {"id": "b" * 32, "name": "📷 Фотография", "description": ""}]
rows = [
    {"id": "1" * 32, "name": "Домашка", "sphere_id": "a" * 32},
    {"id": "2" * 32, "name": "Аня", "sphere_id": None},
]
for r in rows:
    r.update(status=notion.STATUS_ACTIVE, kind=notion.KIND_SHARED, members=[], creator=42, description="", url="")
notes = [{"id": f"{i:032d}", "title": t, "url": "u", "type": None, "ai_type": None, "author_id": 42, "project": None, "when": None}
         for i, t in enumerate(["Решить алгебру", "Сочинение", "Ретушь Ани", "Купить хлеб", "Урок английского", "Слова на английском"], 1)]
filed: list[tuple] = []
AI_REPLY = {
    "notes": [{"n": 1, "project": "Домашка"}, {"n": 2, "project": "домашка"}, {"n": 3, "project": "Аня"},
              {"n": 5, "project": "Английский"}, {"n": 6, "project": "Английский"},
              {"n": 4, "project": "Несуществующий"}, {"n": 99, "project": "Домашка"}, {"bad": 1}],
    "new_projects": [{"name": "Английский", "sphere": "📚 Образование"}, {"name": "Лишний", "sphere": "Космос"}],
    "spheres": [{"project": "Аня", "sphere": "📷 Фотография"}, {"project": "Домашка", "sphere": "📷 Фотография"}],
}


TARGET = {"Решить алгебру": "Домашка", "Сочинение": "домашка", "Ретушь Ани": "Аня", "Урок английского": "Английский",
          "Слова на английском": "Английский", "Купить хлеб": "Несуществующий"}


async def complete(prompt, max_tokens):
    """Как модель: номера заметок — по списку в запросе. Плюс мусор, который должен отброситься."""
    assert "Заметки без проекта" in prompt
    listed = [line.split(". ", 1)[1] for line in prompt.split("\n") if line[:1].isdigit() and ". " in line and line.split(". ", 1)[1] in TARGET]
    reply = dict(AI_REPLY, notes=[{"n": i, "project": TARGET[t]} for i, t in enumerate(listed, 1)] + [{"n": 99, "project": "Домашка"}, {"bad": 1}])
    return json.dumps(reply, ensure_ascii=False)


async def without_project(uid, limit=80):
    return [dict(n) for n in notes if not n["project"]]


async def load(force=False):
    return [dict(r) for r in rows]


async def spheres():
    return [dict(a) for a in areas]


async def add_projects(names, uid=None):
    for n in names:
        rows.append({"id": f"{len(rows) + 1:032d}", "name": n, "sphere_id": None, "status": notion.STATUS_ACTIVE,
                     "kind": notion.KIND_SHARED, "members": [], "creator": 42, "description": "", "url": ""})
    return names


async def update_project(pid, **kw):
    r = next(r for r in rows if r["id"] == pid)
    if kw.get("sphere_id") is not None:
        r["sphere_id"] = kw["sphere_id"] or None


async def file_to_project(pid, name, skip_review=False):
    filed.append((pid, name))
    next(n for n in notes if n["id"] == pid)["project"] = name
    return next(r for r in rows if r["name"] == name)


async def page_info(pid):
    return dict(next(n for n in notes if n["id"] == pid))


async def run():
    ai._complete = complete
    notion.notes_without_project, notion._load_projects, notion.spheres = without_project, load, spheres
    notion.add_projects, notion.update_project, notion.file_to_project, notion.page_info = add_projects, update_project, file_to_project, page_info
    app = await h.make_app()

    # 1. разовое предложение утром 8–12, только владелице и один раз
    scheduler.JOBS = [h.TIDY_JOB]

    async def nothing(*a, **k):
        return []
    scheduler._reminders, notion.forget_old_marks = nothing, nothing
    main.member.add_user_ids(77)
    await scheduler.tick(app.bot, [42, 77], now=datetime(2026, 10, 7, 19, 0, tzinfo=timezone.utc))  # 22:00 МСК
    await scheduler.tick(app.bot, [42, 77], now=datetime(2026, 10, 8, 4, 55, tzinfo=timezone.utc))  # 07:55
    assert not h.texts(chat=42)
    await scheduler.tick(app.bot, [42, 77], now=datetime(2026, 10, 8, 5, 5, tzinfo=timezone.utc))  # 08:05
    assert "навести порядок" in h.texts(chat=42)[-1] and not h.texts(chat=77)
    await scheduler.tick(app.bot, [42, 77], now=datetime(2026, 10, 9, 5, 5, tzinfo=timezone.utc))
    assert len(h.texts(chat=42)) == 1, "только один раз"

    # 2. проверка ответа ИИ: выдуманные проекты, сферы и номера отбрасываются
    plan = await ai.tidy_plan(["📚 Образование", "📷 Фотография"], [("Домашка", "📚 Образование"), ("Аня", "")],
                              [(n["title"], "") for n in notes])
    assert plan["notes"] == [(1, "Домашка"), (2, "Домашка"), (3, "Аня"), (5, "Английский"), (6, "Английский")], plan
    assert plan["new_projects"] == [("Английский", "📚 Образование")] and plan["spheres"] == [("Аня", "📷 Фотография")]

    # 3. план → по шагам (сферы по алфавиту, без сферы — в конце): Английский ⏭, Домашка ✅, Аня ✅, сферы ✅
    await app.process_update(h.callback("td:plan"))
    overview = h.texts("editMessageText")[-1]
    assert "Домашка ← 2" in overview and "🆕 Английский ← 2" in overview and "«Аня» → 📷 Фотография" in overview, overview
    assert "останутся во входящих" in overview  # «Купить хлеб»
    await app.process_update(h.callback("td:step"))
    assert "Шаг 1 из 4" in h.texts("editMessageText")[-1]
    steps = [h.texts("editMessageText")[-1]]
    await app.process_update(h.callback("td:s"))
    steps.append(h.texts("editMessageText")[-1])
    await app.process_update(h.callback("td:y"))
    steps.append(h.texts("editMessageText")[-1])
    await app.process_update(h.callback("td:y"))
    steps.append(h.texts("editMessageText")[-1])
    await app.process_update(h.callback("td:y"))
    assert "🆕 новый проект Английский" in steps[0] and "Домашка" in steps[1] and "Без сферы → 📁 Аня" in steps[2] and "Поставить сферы" in steps[3], steps
    assert filed == [(notes[0]["id"], "Домашка"), (notes[1]["id"], "Домашка"), (notes[2]["id"], "Аня")], filed
    assert "Английский" not in [r["name"] for r in rows], "пропущенный новый проект не создаётся"
    assert next(r for r in rows if r["name"] == "Аня")["sphere_id"] == "b" * 32
    summary = h.texts("editMessageText")[-1]
    assert "заметок разложено: 3" in summary and "сфер проставлено: 1" in summary, summary

    # 4. «разложить всё»: оставшееся — новый проект «Английский» в «Образовании»
    await app.process_update(h.callback("td:plan"))
    await app.process_update(h.callback("td:all"))
    english = next(r for r in rows if r["name"] == "Английский")
    assert english["sphere_id"] == "a" * 32 and filed[-1] == (notes[5]["id"], "Английский")
    assert "новых проектов: 1" in h.texts("editMessageText")[-1]

    # 5. устаревший план и «не сейчас»
    await app.process_update(h.callback("td:y"))
    assert "устарел" in next(d["text"] for e, d in reversed(h.sent) if e == "answerCallbackQuery")
    await app.process_update(h.callback("td:no"))
    assert "/tidy" in h.texts("editMessageText")[-1]
    print("tidy OK")


asyncio.run(run())
