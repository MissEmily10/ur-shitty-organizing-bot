"""Этап 13: ИИ-студия — Арт-директор: палитра в HEX по пикселям, карточка палитры, разбор ИИ,
вход через /artdirector, фото с подписью /ad и кнопку у заметки-референса (разбор дописывается в заметку)."""

import asyncio
import io

from PIL import Image, ImageDraw

import harness as h

from bot import ai, main, notion, studio


def reference() -> bytes:
    img = Image.new("RGB", (400, 300), "#2B4C7E")
    d = ImageDraw.Draw(img)
    d.rectangle((0, 150, 400, 300), fill="#E8B04A")
    d.rectangle((0, 0, 80, 300), fill="#F4EDE1")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


JPEG = reference()
asked: list[str] = []
sections: list[tuple] = []
notes = {
    "a" * 32: {"author_id": 42, "images": ["https://img/1"], "project": None},
    "b" * 32: {"author_id": 42, "images": [], "project": None},
    "c" * 32: {"author_id": 77, "images": ["https://img/2"], "project": None},
}


async def art_director(image, palette, context=""):
    asked.append(context)
    assert image == JPEG and palette[0][0].startswith("#")
    return "## Свет\n- мягкий, слева"


async def page_info(pid):
    n = notes[pid]
    return {"id": pid, "title": "Референс", "url": "u", "author_id": n["author_id"], "project": n["project"],
            "project_id": None, "when": None, "ai_type": None}


async def read_note(pid):
    return {"summary": "", "details": "", "images": notes[pid]["images"]}


async def download(url):
    return JPEG


async def add_section(pid, title, blocks):
    sections.append((pid, title, len(blocks)))


async def download_image(msg):
    return JPEG


def photo_update(caption: str = "", reply_to: dict | None = None):
    msg = {"message_id": 5000, "date": 0, "chat": h.CHAT, "from": h.OWNER,
           "photo": [{"file_id": "F", "file_unique_id": "U", "width": 400, "height": 300}]}
    if caption:
        msg["caption"] = caption
    if reply_to:
        msg["reply_to_message"] = reply_to
    return h.Update.de_json({"update_id": 5001, "message": msg}, h.APP.bot)


async def run():
    # 1. палитра по пикселям: три цвета, почти одинаковые оттенки JPEG склеены
    colors = studio.palette(JPEG)
    assert len(colors) == 3 and abs(sum(s for _, s in colors) - 1) < 0.01, colors
    near = lambda a, b: sum((x - y) ** 2 for x, y in zip(studio._rgb(a), studio._rgb(b))) < 20**2  # noqa: E731
    assert near(colors[0][0], "#2B4C7E") and 0.35 < colors[0][1] < 0.45, colors  # синий: 3/8 площади
    assert any(near(hx, "#E8B04A") for hx, _ in colors) and any(near(hx, "#F4EDE1") for hx, _ in colors), colors
    assert Image.open(io.BytesIO(studio.palette_card(colors))).size == studio.CARD_SIZE
    assert ai.craft_for("Сфера деятельности: 🎨 Дизайнер — айдентика и веб") == "design"
    assert ai.craft_for("📷 Фотограф — портреты") == "photo"

    ai.art_director = art_director
    notion.page_info, notion.read_note, notion.download, notion.add_section = page_info, read_note, download, add_section
    main._download_image = download_image
    saved = []

    async def no_save(*a, **k):
        saved.append(a)
    main._save = no_save
    app = await h.make_app()

    # 2. /artdirector → фото ответом → карточка палитры + разбор, заметка не создаётся
    await app.process_update(h.text("/artdirector"))
    assert h.texts(chat=42)[-1] == main.AD_PROMPT
    await app.process_update(photo_update(reply_to=h._message(main.AD_PROMPT)))
    card = next(d for e, d in reversed(h.sent) if e == "sendPhoto")
    assert card["caption"].startswith("🎨 Палитра: #")
    assert "Арт-директор" in h.texts(chat=42)[-1] and "мягкий, слева" in h.texts(chat=42)[-1]
    assert not saved and not sections

    # 3. фото с подписью /ad — то же самое; обычное фото — заметка
    await app.process_update(photo_update(caption="/ad"))
    assert len(asked) == 2 and not saved
    await app.process_update(photo_update(caption="закат на море"))
    assert len(saved) == 1

    # 4. кнопка у заметки-референса: разбор дописывается в заметку
    await app.process_update(h.callback(f"ad:{'a' * 32}"))
    assert sections == [("a" * 32, "🎨 Арт-директор", sections[0][2])] and "дописан в заметку" in h.texts(chat=42)[-1]
    # без фото — подсказка; чужая заметка — нельзя
    await app.process_update(h.callback(f"ad:{'b' * 32}"))
    assert "нет фото" in next(d["text"] for e, d in reversed(h.sent) if e == "answerCallbackQuery")
    main.member.add_user_ids(77)
    await app.process_update(h.callback(f"ad:{'a' * 32}", user=h.person(77, "Аня")))
    assert len(sections) == 1

    # 5. кнопка появляется, когда в разборе выбран тип «Референс»
    async def set_type(pid, name):
        pass
    notion.set_type = set_type
    idx = h.TYPES.index("🎨 Референс")
    await app.process_update(h.callback(f"t:{'a' * 32}:{idx}:0"))
    markup = next(d for e, d in reversed(h.sent) if e == "editMessageText")["reply_markup"]
    assert "🎨 Арт-директор" in [b.text for row in markup.inline_keyboard for b in row]
    print("studio OK")


asyncio.run(run())
