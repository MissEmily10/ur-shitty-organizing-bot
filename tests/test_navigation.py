"""Этап 14: навигация — знакомство при первом входе, меню по разделам, «↩️ В меню», подсказки «❔»
первые разы и по кнопке, справка через ИИ (/help вопрос), справка знает все команды."""

import asyncio
import re

import harness as h

from bot import ai, main, notion, scheduler

prompts: list[str] = []
items = [{"id": "1" * 32, "title": "Идея", "url": "https://n/1", "project": None, "when": None, "ai_type": None}]


async def complete(prompt, max_tokens):
    prompts.append(prompt)
    return "Нажми /addproject и напиши название."


async def review_items(uid):
    return list(items)


async def preview(pid):
    return "текст"


def buttons(data):
    return [b.text for row in data["reply_markup"].inline_keyboard for b in row]


def last(endpoint):
    return next(d for e, d in reversed(h.sent) if e == endpoint)


async def run():
    ai._complete = complete
    notion.review_items, notion.preview = review_items, preview
    scheduler.DEFAULT_SETTINGS["toured"] = False
    main.HINT_TIMES = 3
    app = await h.make_app()

    # 1. /start — всегда знакомство; в конце «📋 Открыть меню»
    await app.process_update(h.text("/start"))
    assert "1 из 5" in h.texts(chat=42)[-1] and "Дальше ▶️" in buttons(last("sendMessage"))
    await app.process_update(h.callback("tour:1"))
    assert "2 из 5" in last("editMessageText")["text"]
    await app.process_update(h.callback("tour:4"))
    assert buttons(last("editMessageText")) == ["📋 Открыть меню"]
    await app.process_update(h.callback("tour:end"))
    assert "🗂 Сферы" in buttons(last("sendMessage"))
    await app.process_update(h.text("/start"))
    assert "1 из 5" in h.texts(chat=42)[-1], "/start — снова знакомство, не меню"

    # 2. /menu — главные разделы в нужном порядке
    await app.process_update(h.text("/menu"))
    menu_msg = last("sendMessage")
    assert buttons(menu_msg) == ["🗂 Сферы", "📁 Проекты", "🏷 Типы", "📒 Все заметки", "🤖 ИИ", "🗓 Планы",
                                 "⚙️ Настройки", "🧭 Навигация", "👥 Команда"], buttons(menu_msg)
    assert "/notes" in menu_msg["text"] and "/feed" in menu_msg["text"]

    # 3. разделы открываются в том же сообщении, у каждой постоянной кнопки строка пояснения, «↩️ В меню» возвращает
    async def spheres():
        return [{"id": "a" * 32, "name": "📚 Образование", "description": "учёба"}]
    notion.spheres = spheres
    dynamic = {"spheres": ["📚 Образование"], "types": h.TYPES}
    for name in ("spheres", "projects", "types", "notes", "ai", "plan", "help", "team"):
        await app.process_update(h.callback(f"m:s:{name}", message_text="меню"))
        section = last("editMessageText")
        text = re.sub(r"<[^>]+>", "", section["text"])
        assert buttons(section)[-1] == "↩️ В меню", (name, buttons(section))
        for label in buttons(section)[:-1]:
            if label not in dynamic.get(name, []):
                assert label.split(" ", 1)[1] in text, (name, label)
    await app.process_update(h.callback("m:home", message_text="раздел"))
    assert "/razbor" in last("editMessageText")["text"]
    # сфера → её проекты, лента и заметки
    await app.process_update(h.callback("m:sp:0", message_text="меню"))
    assert "📚 Образование" in last("editMessageText")["text"] and "📰 Лента" in buttons(last("editMessageText"))

    # 4. у участницы нет раздела «Команда»
    main.member.add_user_ids(77)
    anya = h.person(77, "Аня")
    await app.process_update(h.text("/menu", user=anya))
    assert "👥 Команда" not in buttons(last("sendMessage"))
    count = len(h.sent)
    await app.process_update(h.callback("m:s:team", message_text="меню", user=anya))
    assert not any(e == "editMessageText" for e, _ in h.sent[count:])

    # 5. подсказки: первые 3 раза сами, потом только по кнопке «❔»
    for i in range(4):
        await app.process_update(h.text("/razbor"))
        card = h.texts(chat=42)[-1]
        assert ("❔ Выбери" in card) == (i < 3), (i, card)
    assert "❔ Что делать" in buttons(last("sendMessage")) and "↩️ В меню" in buttons(last("sendMessage"))
    assert "🤖 Спросить ИИ" in buttons(last("sendMessage"))
    await app.process_update(h.callback("hp:review"))
    alert = last("answerCallbackQuery")
    assert alert["show_alert"] and alert["text"].startswith("Выбери"), alert

    # 5. справка через ИИ: /help вопрос и ответом на подсказку
    await app.process_update(h.text("/help как добавить проект?"))
    assert "/addproject" in h.texts(chat=42)[-1] and "как добавить проект?" in prompts[-1] and "## Проекты" in prompts[-1]
    await app.process_update(h.text("где настройки?", reply_to=h._message(main.HELP_PROMPT)))
    assert "где настройки?" in prompts[-1]
    await app.process_update(h.text("/help"))
    assert "Знакомство" in h.texts(chat=42)[-1]

    # 6. справка описывает каждую команду бота
    guide = ai.GUIDE_FILE.read_text(encoding="utf-8")
    missing = [cmd for cmd, _ in main.COMMANDS + main.OWNER_EXTRA if f"/{cmd}" not in guide]
    assert not missing, f"в docs/GUIDE.md нет команд: {missing}"
    print("navigation OK")


asyncio.run(run())
