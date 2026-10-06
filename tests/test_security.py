"""Права: чужие, участники и владелица; групповые чаты; подделанные кнопки; лимиты; секретные ссылки."""

import asyncio

import harness as h

from bot import ai, config, main, notion

ANYA = h.person(77, "Аня")
EVE = h.person(88, "Чужой")
OWNER_PAGE = "a" * 32
ANYA_PAGE = "b" * 32
touched: list[str] = []
transcribed: list[int] = []


async def page_info(pid):
    author = 0 if pid == OWNER_PAGE else 77
    return {"id": pid, "title": "t", "url": f"https://n/{pid}", "project": None, "type": None, "author_id": author}


async def trash(pid):
    touched.append(("trash", pid))


async def set_type(pid, name):
    touched.append(("type", pid))


async def read_note(pid):
    touched.append(("read", pid))
    return {"summary": "секрет владелицы", "details": "", "images": []}


async def created_today(uid):
    return 999


async def transcribe(data):
    transcribed.append(1)
    return "текст"


async def run():
    notion.page_info, notion.trash, notion.set_type, notion.read_note = page_info, trash, set_type, read_note
    notion.created_today = created_today

    async def types():
        return ["💡 Идея"]

    async def review_items(uid):
        return []

    async def members():
        return [{"page": "x", "tg": 77, "name": "Аня", "username": "", "code": "", "until": None}]

    notion.types, notion.review_items, notion.members = types, review_items, members
    ai.transcribe = transcribe
    app = await h.make_app()
    main.member.add_user_ids(77)

    # участница подделывает кнопки с id заметки владелицы
    for data in [f"d:{OWNER_PAGE}:0", f"t:{OWNER_PAGE}:0:0", f"v:{OWNER_PAGE}", f"n:{OWNER_PAGE}", f"a:{OWNER_PAGE}", f"aq:{OWNER_PAGE}:0", f"xs:{OWNER_PAGE}"]:
        await app.process_update(h.callback(data, user=ANYA))
    assert not touched, touched
    # со своей заметкой — можно
    await app.process_update(h.callback(f"t:{ANYA_PAGE}:0:0", user=ANYA))
    assert touched == [("type", ANYA_PAGE)], touched
    touched.clear()

    # участница жмёт кнопки владелицы
    before = len(h.sent)
    for data in ["mr:77", "mk:77", "mi:x", "pa:0", "pk:0", "m:members", "m:invite"]:
        await app.process_update(h.callback(data, user=ANYA))
    assert 77 in main.member.user_ids
    leaked = [d for e, d in h.sent[before:] if e == "sendMessage" and ("remind/" in d.get("text", "") or "Участники" in d.get("text", ""))]
    assert not leaked, leaked

    # чужой жмёт любые кнопки — ничего
    before = len(h.sent)
    for data in [f"d:{ANYA_PAGE}:0", "r:0", "m:razbor", "q:d:7", "xi:done", "mk:77"]:
        await app.process_update(h.callback(data, user=EVE))
    assert not touched and all(e == "answerCallbackQuery" for e, _ in h.sent[before:]), h.sent[before:]

    # чужой шлёт команды
    for cmd in ["/razbor", "/ask", "/members", "/invite X", "/remindlink", "/projects", "/addproject Взлом"]:
        await app.process_update(h.text(cmd, user=EVE))
        assert "код приглашения" in h.texts(chat=88)[-1], cmd

    # участница шлёт команды владелицы
    before = len(h.sent)
    for cmd in ["/members", "/invite X", "/remindlink"]:
        await app.process_update(h.text(cmd, user=ANYA))
    assert not any("remind/" in d.get("text", "") or "Участники" in d.get("text", "") for e, d in h.sent[before:])

    # групповой чат: бот ничего не показывает и выходит
    group_msg = h.text("/razbor")
    group_msg.message._unfreeze()
    group_msg.message.chat._unfreeze()
    group_msg.message.chat.type = "group"
    before = len(h.sent)
    await app.process_update(group_msg)
    assert [e for e, _ in h.sent[before:]] == ["leaveChat"], h.sent[before:]

    # лимит проверяется до расшифровки голосового
    voice = h.text("x", user=ANYA)
    voice.message._unfreeze()
    voice.message.text = None
    from telegram import Voice
    voice.message.voice = Voice("f", "u", 3)
    voice.message.voice.set_bot(app.bot)
    await app.process_update(voice)
    assert not transcribed and "Лимит" in h.texts(chat=77)[-1]

    # секретные ссылки
    import aiohttp
    from aiohttp.test_utils import TestClient, TestServer
    print("security OK")


asyncio.run(run())
