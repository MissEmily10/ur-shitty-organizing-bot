"""Этап 12: меню с картинками — панель-баннер, разделы в том же сообщении, подписи вместо текста,
пуши с картинкой, дизайн из design/assets, ИИ-баннеры владелицы."""

import asyncio
import io
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image

import harness as h

from bot import ai, banners, main, notion, scheduler

items: list[dict] = []


async def review_items(uid):
    return list(items)


async def preview(pid):
    return "текст заметки"


async def nothing(*a, **k):
    return []


def last(endpoint):
    return next(d for e, d in reversed(h.sent) if e == endpoint)


def keys(data):
    return [b.text for row in data["reply_markup"].inline_keyboard for b in row]


async def run():
    notion.review_items, notion.preview, notion.dated_items = review_items, preview, nothing
    notion.project_list = nothing
    await scheduler.update_settings(42, banners=True)
    app = await h.make_app()

    # 1. /start — картинка с короткой подписью и кнопками, полный список команд — кнопкой
    await app.process_update(h.text("/start"))
    start = last("sendPhoto")
    assert start["caption"] == main.MENU_SHORT and "📜 Все команды" in keys(start)
    await app.process_update(h.callback("m:help", photo=True))
    assert "/razbor" in h.texts("sendMessage")[-1]

    # 2. раздел открывается в той же панели: меняется картинка и подпись, есть «↩️ В меню»
    await app.process_update(h.callback("m:projects", photo=True))
    media = last("editMessageMedia")
    assert "Проектов пока нет" in media["media"]["caption"] and "↩️ В меню" in keys(media), media
    await app.process_update(h.callback("m:home", photo=True))
    assert last("editMessageMedia")["media"]["caption"] == main.MENU_SHORT

    # 3. разбор в панели; дальше кнопки правят подпись картинки
    items.append({"id": "1" * 32, "title": "Идея", "url": "https://n/1", "project": None, "when": None, "ai_type": None})
    await app.process_update(h.callback("m:razbor", photo=True))
    assert "Заметка 1 из 1" in last("editMessageMedia")["media"]["caption"]
    async def page_info(pid):
        return {**items[0], "author_id": 42}
    notion.page_info = page_info
    await app.process_update(h.callback(f"pj:{'1' * 32}:0", photo=True))
    assert "В какой проект?" in last("editMessageCaption")["caption"]
    # длинный текст в подпись не влезает — уходит отдельным сообщением, у картинки убираются кнопки
    items[0]["title"] = "Очень длинная заметка " * 60
    count = len(h.sent)
    await app.process_update(h.callback("r:0", photo=True))
    tail = [e for e, _ in h.sent[count:]]
    assert "editMessageReplyMarkup" in tail and "sendMessage" in tail, tail
    items.clear()

    # 4. обычное текстовое сообщение правится как раньше
    await app.process_update(h.callback("r:0"))
    assert h.sent[-1][0] in ("editMessageText", "answerCallbackQuery") and "Всё разобрано" in h.texts("editMessageText")[-1]

    # 5. вечерний пуш — с картинкой
    items.append({"id": "2" * 32, "title": "Ещё", "url": "u", "project": None, "when": None, "ai_type": None})
    now = datetime(2026, 10, 7, 20, 1, tzinfo=ZoneInfo("Europe/Moscow"))
    await scheduler._evening_run(app.bot, 42, now)
    assert "Вечерний разбор" in last("sendPhoto")["caption"]

    # 6. картинка из Figma (design/assets) важнее заглушки
    folder = Path(tempfile.mkdtemp())
    (folder / "banner_menu.png").write_bytes(b"FIGMA")
    banners.ASSETS, banners._cache = folder, {}
    assert await banners.get("menu") == b"FIGMA"
    png = await banners.get("projects")
    assert Image.open(io.BytesIO(png)).size == (1280, 640)

    # 7. ИИ-баннер: нарисовать → поставить → используется вместо заглушки; вернуть заглушку
    async def draw(prompt, size=512):
        assert size == (1280, 640) and "no text" in prompt
        return b"AI"
    ai.draw = draw
    await app.process_update(h.callback("bn:schedule"))
    assert "✅ Поставить" in keys(last("sendPhoto"))
    await app.process_update(h.callback("bn:schedule:ok", photo=True))
    assert await banners.get("schedule") == "PHOTO"
    await app.process_update(h.callback("bn:schedule:reset", photo=True))
    assert isinstance(await banners.get("schedule"), bytes)
    # участнице это недоступно
    main.member.add_user_ids(77)
    await app.process_update(h.callback("bn:schedule:ok", photo=True, user=h.person(77, "Аня")))
    assert not h.service.get("banner:schedule")

    # 8. выключить картинки — снова текстовое меню
    await app.process_update(h.callback("st:bn"))
    await app.process_update(h.text("/start"))
    assert h.sent[-1][0] == "sendMessage" and "/razbor" in h.sent[-1][1]["text"]
    print("banners OK")


asyncio.run(run())
