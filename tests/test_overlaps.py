"""🔗 Пересекающиеся цели: проверка ответа ИИ, группы «объединить» / «главная и шаги» / «это разное»,
ручной выбор, права, субботнее предложение."""

import asyncio
import json
from datetime import datetime, timezone

import harness as h

from bot import ai, main, notion, scheduler

goals = [
    {"id": f"{i:032d}", "title": t, "url": f"https://n/{i}", "type": k, "ai_type": None, "project": p,
     "project_id": None, "when": w, "author_id": 42, "done": False}
    for i, (t, k, p, w) in enumerate([
        ("Выучить английский", "📋 Задача", "Курсы", None),
        ("Начать английский", "💡 Идея", None, "2026-11-01"),
        ("Найти репетитора по английскому", "📋 Задача", "Курсы", "2026-10-20"),
        ("Сайт-портфолио", "💡 Идея", None, None),
        ("Сделать портфолио на сайте", "📋 Задача", None, None),
        ("Купить кроссовки", "📋 Задача", None, None),
    ], 1)
]
merged: list[str] = []
sections: list[tuple] = []
created: list[dict] = []
filed: list[tuple] = []
types: list[tuple] = []
AI_GROUPS = {"groups": [
    {"n": [1, 2], "kind": "same", "main": 1, "why": "одно и то же"},
    {"n": [1, 3], "kind": "parent", "main": 1, "why": "репетитор — шаг"},     # 1 уже занят → останется только 3 → отбросится
    {"n": [4, 5, 99], "kind": "parent", "main": 5, "why": "сайт и портфолио"},
    {"n": [6], "kind": "same"}, "мусор",
]}


async def complete(prompt, max_tokens):
    if "Объедини эти записи" in prompt:
        assert "«Выучить английский»" in prompt and "«Начать английский»" in prompt
        return "===НАЗВАНИЕ===\nАнглийский язык\n===ТЕКСТ===\n- [ ] выбрать курс"
    if "Выучить английский" in prompt:  # у записи — тип, проект и сфера
        assert "Выучить английский [📋 Задача] {Курсы / 📚 Образование}" in prompt, prompt
    return json.dumps(AI_GROUPS, ensure_ascii=False)


async def open_goals(uid, limit=120):
    return [dict(g) for g in goals if g["id"] not in merged and g["author_id"] == uid][:limit]


async def page_info(pid):
    return dict(next(g for g in goals if g["id"] == pid))


async def read_note(pid):
    return {"summary": f"текст {pid[-1]}", "details": "", "images": []}


async def create_idea(title, source, tags, blocks, details, author_id=0, author="", when=None, ai_type=None):
    created.append({"title": title, "source": source, "when": when, "ai_type": ai_type, "author": author_id})
    return "f" * 32, "https://n/new"


async def add_section(pid, title, blocks):
    sections.append((pid, title))


async def mark_merged(pid):
    merged.append(pid)


async def set_type(pid, name):
    types.append((pid, name))


async def file_to_project(pid, name, skip_review=False):
    filed.append((pid, name))
    return {"id": "p" * 32, "name": name, "kind": notion.KIND_SHARED}


async def project_list(uid=None, include_closed=False):
    return [{"id": "p" * 32, "name": "Курсы", "sphere_id": "a" * 32, "kind": notion.KIND_SHARED, "creator": 42, "members": [],
             "status": notion.STATUS_ACTIVE}]


async def spheres():
    return [{"id": "a" * 32, "name": "📚 Образование", "description": ""}]


def text():
    return h.texts("editMessageText")[-1]


def buttons():
    d = next(d for e, d in reversed(h.sent) if e == "editMessageText")
    return [b.text for row in (d.get("reply_markup").inline_keyboard if d.get("reply_markup") else []) for b in row]


async def run():
    ai._complete = complete
    notion.open_goals, notion.page_info, notion.read_note, notion.create_idea = open_goals, page_info, read_note, create_idea
    notion.add_section, notion.mark_merged, notion.set_type, notion.file_to_project = add_section, mark_merged, set_type, file_to_project
    notion.project_list, notion.spheres = project_list, spheres
    app = await h.make_app()

    # 1. проверка ответа ИИ: номера вне списка, повторы и группы из одной записи отбрасываются
    groups = await ai.find_overlaps(["a"] * 6)
    assert [g["n"] for g in groups] == [[1, 2], [4, 5]] and groups[1]["main"] == 5 and groups[1]["kind"] == "parent", groups

    # 2. найти → группа 1 (повторяют) → объединить: новая запись, старые помечены, срок самый ранний, проект сохранён
    await app.process_update(h.text("/overlaps"))
    await app.process_update(h.callback("og:find"))
    assert "Группа 1 из 2" in text() and "повторяют друг друга" in text() and buttons()[0] == "🧩 Объединить в одну", (text(), buttons())
    await app.process_update(h.callback("og:m"))
    assert created[-1] == {"title": "Английский язык", "source": "Объединение", "when": "2026-11-01", "ai_type": "📋 Задача", "author": 42}
    assert types == [("f" * 32, "📋 Задача")] and filed == [("f" * 32, "Курсы")]
    assert merged == [goals[0]["id"], goals[1]["id"]] and ("🔗 Объединено") in [t for _, t in sections]
    assert "Объединено в" in text() and "Группа 2 из 2" in text() and "цель и её шаги" in text()

    # 3. группа 2 (цель и шаги): главная — «Сделать портфолио на сайте», ссылки в обе стороны, больше не предлагается
    assert buttons()[0].startswith("🌳 Главная — «Сделать портфолио")
    await app.process_update(h.callback("og:p"))
    assert (goals[4]["id"], "🌳 Шаги этой цели") in sections and (goals[3]["id"], "🌳 Шаг цели") in sections
    assert "групп больше нет" in text()
    assert any(k.startswith("distinct:") and v == "parent" for k, v in h.service.items())

    # 4. повторный поиск: объединённые и уже решённые группы не предлагаются
    AI_GROUPS["groups"] = [{"n": [2, 3], "kind": "parent", "main": 2}]  # теперь в списке 3, 4, 5, 6 → это 4 и 5
    await app.process_update(h.callback("og:find"))
    assert "Пересечений не нашла" in text(), text()

    # 5. «это разное» — запоминается
    AI_GROUPS["groups"] = [{"n": [1, 4], "kind": "same", "main": 1}]  # «Найти репетитора» и «Купить кроссовки»
    await app.process_update(h.callback("og:find"))
    await app.process_update(h.callback("og:d"))
    await app.process_update(h.callback("og:find"))
    assert "Пересечений не нашла" in text()

    # 6. вручную: отметить две и объединить; меньше двух — нельзя
    await app.process_update(h.callback("og:man"))
    assert "▫️ Найти репетитора по английскому" in buttons()
    await app.process_update(h.callback("og:t:0"))
    await app.process_update(h.callback("og:mm"))
    assert "хотя бы две" in next(d["text"] for e, d in reversed(h.sent) if e == "answerCallbackQuery")
    await app.process_update(h.callback("og:t:3"))
    assert "Отмечено: 2" in text()
    await app.process_update(h.callback("og:mp"))
    assert "главная цель" in text()

    # 7. чужие заметки не трогаются
    goals[5]["author_id"] = 77
    main.member.add_user_ids(77)
    anya = h.person(77, "Аня")
    main._overlaps[77] = {"items": [goals[5], goals[2]], "groups": [], "i": 0, "picked": {0, 1}}
    before = len(sections)
    await app.process_update(h.callback("og:mp", user=anya))
    assert len(sections) == before, "заметку владелицы участница не меняет"

    # 8. субботнее предложение — если открытых целей 6 и больше
    scheduler.JOBS = [h.OVERLAPS_JOB]

    async def nothing(*a, **k):
        return []
    scheduler._reminders, notion.forget_old_marks = nothing, nothing
    for i in range(7, 13):
        goals.append(dict(goals[0], id=f"{i:032d}", title=f"Цель {i}"))
    await scheduler.tick(app.bot, [42], now=datetime(2026, 10, 9, 9, 30, tzinfo=timezone.utc))  # пятница
    assert not any("Субботняя" in t for t in h.texts(chat=42))
    await scheduler.tick(app.bot, [42], now=datetime(2026, 10, 10, 9, 30, tzinfo=timezone.utc))  # суббота 12:30
    assert "Субботняя проверка" in h.texts(chat=42)[-1]
    print("overlaps OK")


asyncio.run(run())
