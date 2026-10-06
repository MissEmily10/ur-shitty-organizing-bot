"""Раскрытие заметки вопросами: полный путь через обработчики бота."""

import asyncio
import re
import sys

sys.path.insert(0, "tests")
import harness as h  # noqa: E402

from bot import ai, notion  # noqa: E402

PID = "0123456789abcdef0123456789abcdef"
saved, asked, composed, notes_saved = [], [], [], []


async def page_info(pid):
    return {"id": pid, "title": "Созвон по бюджету", "url": "https://www.notion.so/x-" + PID, "project": "Сайт", "type": "❓ Обсудить"}


async def read_note(pid):
    return {"summary": "обсудить бюджет сайта", "details": "", "images": []}


async def project_titles(project, exclude, uid=0):
    return ["Hero", "Шрифты"]


async def next_question(type_name, note, project, titles, images, qa):
    asked.append((type_name, len(qa)))
    return None if len(qa) >= 2 else ["Какой вопрос нужно решить?", "С кем обсудить?"][len(qa)]


async def compose(type_name, note, project, titles, images, qa, draft=""):
    composed.append(list(qa))
    return "## Вопрос\n- " + qa[0][1] + "\n## С кем и когда\n- " + (qa[1][1] if len(qa) > 1 else "—")


async def add_expansion(pid, type_name, blocks):
    saved.append((type_name, [b["type"] for b in blocks]))


async def save_note(update, source, **kw):
    notes_saved.append(kw.get("text"))


async def run():
    notion.page_info, notion.read_note, notion.project_titles, notion.add_expansion = page_info, read_note, project_titles, add_expansion
    ai.next_question, ai.compose = next_question, compose
    from bot import main
    main._save = save_note
    app = await h.make_app()

    def last(n=1):
        return [re.sub("<[^>]+>", "", t) for t in h.texts()][-n:]

    await app.process_update(h.callback(f"x:{PID}:0"))
    q1 = last()[0]
    assert "Вопрос 1: Какой вопрос нужно решить?" in q1 and q1.startswith("✨ ❓ Обсудить"), q1
    assert asked[0][0] == "❓ Обсудить"

    await app.process_update(h.text("согласовать бюджет 1200"))
    assert "Вопрос 2: С кем обсудить?" in last()[0], last()

    await app.process_update(h.text("с Аней до пятницы"))
    draft = last()[0]
    assert "согласовать бюджет 1200" in draft and "с Аней до пятницы" in draft, draft
    assert composed[-1] == [("Какой вопрос нужно решить?", "согласовать бюджет 1200"), ("С кем обсудить?", "с Аней до пятницы")]

    # правка после сборки — тоже обычным сообщением
    await app.process_update(h.text("добавь, что дедлайн пятница"))
    assert composed[-1][-1] == ("Правка к описанию", "добавь, что дедлайн пятница")

    draft_msg = [d for e, d in h.sent if e in ("editMessageText", "sendMessage") and "reply_markup" in d and "xs:" in str(d["reply_markup"])][-1]
    await app.process_update(h.callback(f"xs:{PID}", draft_msg["text"]))
    assert saved and saved[-1][0] == "❓ Обсудить", saved

    # разговор закончен: обычный текст снова становится заметкой
    await app.process_update(h.text("купить краски"))
    assert notes_saved == ["купить краски"], notes_saved

    # «Пропустить» и «Стоп»
    await app.process_update(h.callback(f"x:{PID}:0"))
    await app.process_update(h.callback("xi:skip"))
    assert "Вопрос 2" in last()[0]
    await app.process_update(h.callback("xi:stop"))
    assert "остановились" in last()[0]
    await app.process_update(h.text("ещё заметка"))
    assert notes_saved[-1] == "ещё заметка"

    # ИИ упал — человек видит ошибку и кнопку «Ещё раз», а не тишину
    async def broken(*a, **k):
        raise TimeoutError("модель не ответила за 120 с")
    ai.next_question = broken
    await app.process_update(h.callback(f"x:{PID}:0"))
    assert "❌ ИИ не ответил" in last()[0], last()

    # необработанная ошибка в любой кнопке доходит до владелицы
    async def boom(*a):
        raise RuntimeError("Notion недоступен")
    notion.review_items = boom
    await app.process_update(h.callback("r:0"))
    assert any("Notion недоступен" in t for t in h.texts()[-3:]) or any(
        "Notion недоступен" in str(d.get("text", "")) for e, d in h.sent[-3:] if e == "answerCallbackQuery"
    ), h.sent[-3:]
    print("interview OK")


asyncio.run(run())
