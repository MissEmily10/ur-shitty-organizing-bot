"""Командный режим: приглашение по коду, доступ, авторство, лимиты, удаление участника."""

import asyncio

import harness as h

from bot import ai, config, notion

ANYA = h.person(77, "Аня", "anya")
EVE = h.person(88, "Чужой")
rows: list[dict] = []
created: list[tuple] = []
reviews: list[int] = []


async def create_invite(label, code, days):
    rows.append({"page": f"p{len(rows)}", "tg": 0, "name": label, "username": "", "code": code, "until": "2099-01-01T00:00:00+00:00"})
    return "2099-01-08T00:00:00+00:00"


async def redeem(code, tg, name, username):
    for r in rows:
        if r["code"] == code and not r["tg"]:
            r.update(tg=tg, code="", username=f"@{username} · {name}")
            return dict(r)
    return None


async def members():
    return [r for r in rows if r["tg"]]


async def invites():
    return [r for r in rows if not r["tg"] and r["code"]]


async def remove_person(page):
    rows[:] = [r for r in rows if r["page"].replace("-", "") != page.replace("-", "")]


async def create_idea(title, source, tags, blocks, details, author_id=0, author="", **kw):
    created.append((title, author_id, author))
    return ("P" * 32, "https://n/p")


async def structure(text="", images=None, **kw):
    return ai.Idea(title=text[:20], summary="- " + text)


async def created_today(uid):
    return sum(1 for c in created if c[1] == uid)


async def review_items(uid):
    reviews.append(uid)
    return []


async def run():
    notion.create_invite, notion.redeem, notion.members, notion.invites, notion.remove_person = (
        create_invite, redeem, members, invites, remove_person)
    notion.create_idea, notion.created_today, notion.review_items = create_idea, created_today, review_items
    ai.structure = structure
    config.MEMBER_DAILY_LIMIT = 2
    app = await h.make_app()

    # чужой без кода: только просьба о коде, владелице ничего
    await app.process_update(h.text("привет", user=EVE))
    assert "код приглашения" in h.texts(chat=88)[-1]
    await app.process_update(h.text("/razbor", user=EVE))
    assert "код приглашения" in h.texts(chat=88)[-1]
    assert not any("Чужой" in t for t in h.texts(chat=42))

    # владелица создаёт приглашение
    await app.process_update(h.text("/invite Аня, дизайнер"))
    code = rows[0]["code"]
    assert rows[0]["name"] == "Аня, дизайнер" and len(code) == 9 and code[4] == "-"
    assert any(f"start={code}" in t for t in h.texts(chat=42)[-2:])

    # неверный код и брутфорс
    await app.process_update(h.text("ZZZZ-ZZZZ", user=EVE))
    assert "не подошёл" in h.texts(chat=88)[-1]
    for _ in range(5):
        await app.process_update(h.text("ZZZZ-ZZZZ", user=EVE))
    assert "через час" in h.texts(chat=88)[-1]

    # Аня входит по ссылке /start <код> (код в нижнем регистре и без дефиса тоже подходит)
    await app.process_update(h.text(f"/start {code.replace('-', '').lower()}", user=ANYA))
    assert "Добро пожаловать" in h.texts(chat=77)[-1]
    assert "/invite" not in h.texts(chat=77)[-1], "у участницы нет команд владелицы"
    assert any("Аня, дизайнер" in t and "вошёл" in t for t in h.texts(chat=42))
    # код одноразовый
    await app.process_update(h.text(code, user=EVE))
    assert "не подошёл" in h.texts(chat=88)[-1] or "через час" in h.texts(chat=88)[-1]

    # заметки Ани подписаны ею, лимит 2 в сутки
    await app.process_update(h.text("идея для фона", user=ANYA))
    await app.process_update(h.text("ещё идея", user=ANYA))
    await app.process_update(h.text("третья", user=ANYA))
    assert [c[1:] for c in created] == [(77, "Аня"), (77, "Аня")], created
    assert "Лимит 2 заметок" in h.texts(chat=77)[-1]
    # у владелицы лимита нет
    for i in range(3):
        await app.process_update(h.text(f"моя {i}"))
    assert sum(1 for c in created if c[1] == 42) == 3

    # разбор у каждого свой
    await app.process_update(h.text("/razbor", user=ANYA))
    await app.process_update(h.text("/razbor"))
    assert reviews[-2:] == [77, 42], reviews

    # участница не может приглашать и удалять проекты
    await app.process_update(h.text("/invite Петя", user=ANYA))
    assert len(rows) == 1

    # /members и удаление
    await app.process_update(h.text("/members"))
    assert "Аня, дизайнер" in h.texts(chat=42)[-1]
    await app.process_update(h.callback("mr:77"))
    await app.process_update(h.callback("mk:77"))
    assert not rows and "доступ к боту закрыт" in h.texts(chat=77)[-1]
    await app.process_update(h.text("заметка после удаления", user=ANYA))
    assert "код приглашения" in h.texts(chat=77)[-1]

    # приглашение через вопрос бота + отзыв кода
    await app.process_update(h.text("/invite"))
    prompt = [d for e, d in h.sent if e == "sendMessage" and d.get("text", "").startswith("🎟 Для кого")][-1]
    reply_to = h._message(prompt["text"])
    await app.process_update(h.text("Петя", reply_to=reply_to))
    assert rows and rows[-1]["name"] == "Петя"
    await app.process_update(h.callback(f"mi:{rows[-1]['page']}"))
    assert not rows
    print("team OK")


asyncio.run(run())
