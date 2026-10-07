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

    # 1. первый /start — знакомство, «Дальше», «Пропустить»; потом меню
    await app.process_update(h.text("/start"))
    assert "1 из 5" in h.texts(chat=42)[-1] and "Дальше ▶️" in buttons(last("sendMessage"))
    await app.process_update(h.callback("tour:1"))
    assert "2 из 5" in last("editMessageText")["text"]
    await app.process_update(h.callback("tour:4"))
    assert buttons(last("editMessageText")) == ["🚀 Начать"]
    await app.process_update(h.callback("tour:end"))
    assert "📥 Заметки" in buttons(last("sendMessage"))
    # второй раз — сразу меню
    await app.process_update(h.text("/start"))
    menu_msg = last("sendMessage")
    assert "📥 Заметки" in buttons(menu_msg) and "👥 Команда" in buttons(menu_msg)
    await app.process_update(h.text("/tour"))
    assert "1 из 5" in h.texts(chat=42)[-1]

    # 2. разделы открываются в том же сообщении, у каждой кнопки строка пояснения, «↩️ В меню» возвращает
    await app.process_update(h.callback("m:s:notes", message_text="меню"))
    section = last("editMessageText")
    assert "Куда писать</b> — " in section["text"] and "↩️ В меню" in buttons(section)
    for name in ("projects", "ai", "plan", "help", "team"):
        await app.process_update(h.callback(f"m:s:{name}", message_text="меню"))
        text = last("editMessageText")["text"]
        # каждая кнопка раздела описана в тексте
        for label in buttons(last("editMessageText"))[:-1]:
            assert label.split(" ", 1)[1] in re.sub(r"<[^>]+>", "", text), (name, label)
    await app.process_update(h.callback("m:home", message_text="раздел"))
    assert "/razbor" in last("editMessageText")["text"]

    # 3. у участницы нет раздела «Команда»
    main.member.add_user_ids(77)
    anya = h.person(77, "Аня")
    scheduler._settings_cache[77] = (asyncio.get_running_loop().time(), {**scheduler.DEFAULT_SETTINGS, "toured": True})
    await app.process_update(h.text("/start", user=anya))
    assert "👥 Команда" not in buttons(last("sendMessage"))
    count = len(h.sent)
    await app.process_update(h.callback("m:s:team", message_text="меню", user=anya))
    assert not any(e == "editMessageText" for e, _ in h.sent[count:])

    # 4. подсказки: первые 3 раза сами, потом только по кнопке «❔»
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
