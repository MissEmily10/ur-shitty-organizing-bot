"""Этап 11: характер бота — встроенные стили, свой персонаж из Character.AI, фильтр, лимит, сброс владелицей,
профиль «Эминемовна»."""

import asyncio
import json
import tempfile
from pathlib import Path

import harness as h

from bot import ai, main, notion, scheduler, styles

calls: list[str] = []
reply = [""]
PACK = {k: [f"Йо, {k}!"] for k in styles.KEYS}


async def complete(prompt, max_tokens):
    calls.append(prompt)
    return reply[0]


async def create_idea(title, source, tags, blocks, details, author_id=0, author="", **kw):
    return "0" * 32, "https://n/1"


async def structure(text="", images=None, **kw):
    return ai.Idea(title=text[:30], summary=text)


async def zero(*a, **k):
    return 0


async def empty(*a, **k):
    return []


async def members():
    return [{"tg": 77, "name": "Аня", "username": "", "page": "x"}]


def buttons():
    for e, d in reversed(h.sent):
        m = d.get("reply_markup")
        if hasattr(m, "inline_keyboard"):
            return [b.text for row in m.inline_keyboard for b in row]
    return []


async def run():
    ai._complete = complete
    ai.structure = structure
    notion.create_idea, notion.created_today, notion.review_items = create_idea, zero, empty
    notion.members, notion.invites = members, empty
    main.member.add_user_ids([77])
    anya = h.person(77, "Аня")
    app = await h.make_app()

    # 1. экран стилей; Эминемовна-черновик не предлагается, владелица видит подсказку
    await app.process_update(h.text("/style"))
    assert "✓ 🙂 Обычный" in buttons() and "🌸 Кавай-тян" in buttons() and styles.EMINEMOVNA_NAME not in buttons()
    assert "styles/eminemovna.md" in h.texts(chat=42)[-1]
    await app.process_update(h.text("/style", user=anya))
    assert "eminemovna" not in h.texts(chat=77)[-1]

    # 2. выбрать кавай: реплика над сообщением о сохранении и «всё разобрано»
    await app.process_update(h.callback("sy:kawaii"))
    assert (await scheduler.get_settings(42))["style"] == "kawaii"
    await app.process_update(h.text("купить молоко"))
    saved = h.texts(chat=42)[-1]
    assert saved.split("\n")[0] in [main.escape(x) for x in styles.BUILTIN["kawaii"]["pack"]["saved"]], saved
    await app.process_update(h.text("/razbor"))
    assert h.texts(chat=42)[-1].split("\n")[0] in [main.escape(x) for x in styles.BUILTIN["kawaii"]["pack"]["all_done"]]
    # обычный стиль — тексты как были
    await app.process_update(h.callback("sy:standard"))
    await app.process_update(h.text("/razbor"))
    assert h.texts(chat=42)[-1] == "🎉 Всё разобрано!"

    # 3. свой персонаж: 4 шага → один запрос к ИИ → пример реплик с «Оставить / Переделать»
    reply[0] = json.dumps(PACK, ensure_ascii=False)
    await app.process_update(h.callback("sy:new", user=anya))
    for step, answer in zip(main.PERSONA_STEPS, ["Гоку", "Весёлый воин", "Привет, я Гоку!", "Пойдём тренироваться!\nЯ голоден"]):
        assert h.texts(chat=77)[-1] == step[1]
        await app.process_update(h.text(answer, user=anya, reply_to=h._message(step[1])))
    assert len(calls) == 1 and "Весёлый воин" in calls[0]
    assert "в роли «Гоку»" in h.texts(chat=77)[-1] and "✅ Оставить" in buttons()
    assert (await scheduler.get_settings(77))["style"] == styles.CUSTOM
    assert await styles.line(77, "saved") == "Йо, saved!"
    # шаг не по порядку — бот просит начать заново
    await app.process_update(h.text("ответ", user=anya, reply_to=h._message(main.PERSONA_STEPS[2][1])))
    assert "заново" in h.texts(chat=77)[-1]

    # 4. фильтр: неприемлемого персонажа бот не играет, стиль не меняется
    reply[0] = '{"refused": true}'
    await app.process_update(h.callback("sy:redo", user=anya))
    assert h.texts(chat=77)[-1].startswith("🙅"), h.texts(chat=77)[-1]
    assert (await styles.persona(77))["pack"] == PACK

    # 5. лимит: 3 набора в день у участницы (два уже потрачены)
    reply[0] = json.dumps(PACK, ensure_ascii=False)
    await app.process_update(h.callback("sy:redo", user=anya))
    before = len(calls)
    await app.process_update(h.callback("sy:redo", user=anya))
    assert len(calls) == before and "3 раза в день" in h.texts(chat=77)[-1]

    # 6. владелица видит стиль участницы и сбрасывает его
    await app.process_update(h.text("/members"))
    assert "🎭 🧩 Гоку" in h.texts(chat=42)[-1] and "🎭 Сбросить стиль" in buttons()
    await app.process_update(h.callback("ms:77"))
    assert (await scheduler.get_settings(77))["style"] == "standard"
    assert "вернула обычный стиль" in h.texts(chat=77)[-1]
    # подделанный выбор недоступного стиля
    await app.process_update(h.callback("sy:eminemovna", user=anya))
    assert (await scheduler.get_settings(77))["style"] == "standard"

    # 7. Эминемовна: когда профиль заполнен, стиль появляется; набор пишется один раз; участникам — подпись
    profile = Path(tempfile.mkdtemp()) / "eminemovna.md"
    profile.write_text("# Стиль\nГоворю коротко и с иронией.\n", encoding="utf-8")
    styles.EMINEMOVNA_FILE = profile
    await app.process_update(h.text("/style"))
    assert styles.EMINEMOVNA_NAME in buttons()
    calls.clear()
    await app.process_update(h.callback("sy:eminemovna"))
    await app.process_update(h.callback("sy:eminemovna", user=anya))
    assert await styles.line(42, "praise") == "Йо, praise!"
    assert await styles.line(77, "praise") == "Йо, praise!" + styles.EMINEMOVNA_SIGN
    assert len(calls) == 1 and "иронией" in calls[0], len(calls)

    print("styles OK")


asyncio.run(run())
