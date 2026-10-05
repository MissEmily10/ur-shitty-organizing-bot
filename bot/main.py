import asyncio
import logging
import re
from html import escape

from telegram import InlineKeyboardButton as Btn
from aiohttp import web
from telegram import ForceReply, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from . import ai, notion, remind
from . import config as c
from .markdown import tg_html, to_blocks

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

owner = filters.User(user_id=c.OWNER_ID)


# ---------- меню ----------

MENU = (
    "Я складываю твои мысли в Notion.\n\n"
    "✍️ Просто пиши, присылай фото блокнота или голосовые: всё, что не начинается с «/», становится заметкой.\n\n"
    "<b>Команды</b>\n"
    "/razbor — разобрать входящие\n"
    "/projects — список проектов и удаление\n"
    "/addproject — добавить проект (бот спросит название)\n"
    "/remindlink — ссылка для вечернего напоминания\n"
    "/start — это меню\n\n"
    "Или жми кнопку 👇"
)
MENU_KB = InlineKeyboardMarkup(
    [
        [Btn("🗂 Разобрать входящие", callback_data="m:razbor")],
        [Btn("📁 Проекты", callback_data="m:projects"), Btn("➕ Добавить проект", callback_data="m:add")],
        [Btn("🔔 Ссылка напоминания", callback_data="m:remind")],
    ]
)
# Ответ на это сообщение бота — названия проектов, а не заметка
ADD_PROMPT = "Напишите названия проектов ответом на это сообщение: через запятую или каждый с новой строки."


# ---------- приём заметок ----------


async def start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if uid != c.OWNER_ID:
        await update.message.reply_text(f"Ваш Telegram ID: {uid}\nВпишите его в переменную OWNER_ID и перезапустите бота.")
        return
    await update.message.reply_text(MENU, reply_markup=MENU_KB, parse_mode=ParseMode.HTML)


async def on_menu(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if q.from_user.id != c.OWNER_ID:
        return
    action = q.data.removeprefix("m:")
    try:
        if action == "razbor":
            text, kb = await _review_view(0)
            await q.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        elif action == "projects":
            text, kb = await _projects_view()
            await q.message.reply_text(text, reply_markup=kb)
        elif action == "add":
            await _ask_project_names(q.message)
        elif action == "remind":
            await q.message.reply_text(_remind_text())
    except Exception as e:
        log.exception("menu failed")
        await q.message.reply_text(f"❌ Ошибка: {e}"[:4000])


async def _ask_project_names(message) -> None:
    await message.reply_text(ADD_PROMPT, reply_markup=ForceReply(input_field_placeholder="Сайт, Логотипы, Фоны"))


def _remind_text() -> str:
    if not c.WEBHOOK_BASE:
        return "Ссылка появится, когда бот запущен на хостинге (Render)."
    url = f"{c.WEBHOOK_BASE.rstrip('/')}/remind/{c.secret('remind')}"
    return (
        f"Ссылка для cron-job.org:\n{url}\n\n"
        f"Проверить прямо сейчас (пуш придёт, даже если сегодня уже был):\n{url}?force=1\n\n"
        "Никому её не показывайте."
    )


async def remindlink(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_remind_text())


async def _save(update: Update, source: str, *, text: str = "", images: list[bytes] | None = None) -> None:
    images = images or []
    status = await update.message.reply_text("⏳ Обрабатываю…" if len(images) < 2 else f"⏳ Обрабатываю альбом из {len(images)} фото…")
    note = ""
    try:
        try:
            idea = await ai.structure(text=text, images=images)
        except Exception as e:
            if images:
                raise
            # ИИ недоступен: текст всё равно не теряем, кладём как есть
            log.exception("AI failed, saving raw text")
            idea = ai.Idea(title=text.splitlines()[0][:60], summary=text)
            note = f"\n\n⚠️ Сохранено без обработки ИИ: {escape(str(e)[:500])}"
        originals = []
        for i, image in enumerate(images, 1):
            try:
                originals.append(notion.image_block(await notion.upload_image(image, f"photo-{i}.jpg")))
            except Exception as e:
                # Заметку всё равно сохраняем, просто без оригинала
                log.exception("image upload failed")
                note = f"\n\n⚠️ Фото {i} не прикрепилось к Notion: {escape(str(e)[:300])}"
        blocks = to_blocks(idea.summary) + originals
        page_id, url = await notion.create_idea(idea.title, source, idea.tags, blocks, to_blocks(idea.details))
    except Exception as e:
        log.exception("save failed")
        await status.edit_text(f"❌ Не получилось сохранить: {e}"[:4000])
        return
    await status.edit_text(
        f'✅ <b>{escape(idea.title)}</b>\n📄 <a href="{url}">Заметка — тут</a>{note}',
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=_panel(page_id),
    )


# ---------- кнопка «В проект» под заметкой ----------
# callback_data: n:<page_id> (показать проекты)  |  np:<page_id>:<проект>  |  nx:<page_id> (свернуть)


def _panel(page_id: str, project: str | None = None) -> InlineKeyboardMarkup:
    label = f"📁 {project} ✓" if project else "📁 В проект"
    return InlineKeyboardMarkup([[Btn(label, callback_data=f"n:{page_id}")]])


async def on_panel(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q.from_user.id != c.OWNER_ID:
        await q.answer()
        return
    action, page_id, *rest = q.data.split(":")
    try:
        if action == "n":
            projects = await notion.projects()
            if not projects:
                await q.answer("Проектов пока нет: /addproject", show_alert=True)
                return
            buttons = [Btn(f"📁 {p}", callback_data=f"np:{page_id}:{i}") for i, p in enumerate(projects)]
            rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
            rows.append([Btn("✖️ Свернуть", callback_data=f"nx:{page_id}")])
            await q.answer()
            await q.edit_message_reply_markup(InlineKeyboardMarkup(rows))
        elif action == "np":
            projects = await notion.projects()
            idx = int(rest[0])
            if idx >= len(projects):
                await q.answer("Список проектов изменился, попробуйте ещё раз")
                return
            await notion.file_to_project(page_id, projects[idx])
            await q.answer(f"→ {projects[idx]}")
            await q.edit_message_reply_markup(_panel(page_id, projects[idx]))
        else:
            await q.answer()
            await q.edit_message_reply_markup(_panel(page_id, await notion.page_project(page_id)))
    except Exception as e:
        log.exception("panel failed")
        await q.answer(f"Ошибка: {e}"[:200], show_alert=True)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    reply = update.message.reply_to_message
    if reply and reply.from_user and reply.from_user.id == ctx.bot.id and reply.text == ADD_PROMPT:
        await update.message.reply_text(await _add_projects_text(update.message.text))
        return
    if reply and reply.from_user and reply.from_user.id == ctx.bot.id and (reply.text or "").startswith(EXPAND_MARK):
        if page_id := _ai_page(reply):
            await expand_note(update.message, page_id, reply=reply, answers=update.message.text)
            return
    if reply and reply.from_user and reply.from_user.id == ctx.bot.id and (page_id := _ai_page(reply)):
        # Ответ на вопрос «🤖 Что сделать с заметкой?» или уточнение к ответу ИИ
        history = reply.text if reply.text.startswith(AI_ANSWER_MARK) else ""
        await _ask_ai(update.message, page_id, update.message.text, history)
        return
    await _save(update, "Текст", text=update.message.text)


async def _download_image(msg: Message) -> bytes:
    file = await (msg.photo[-1] if msg.photo else msg.document).get_file()
    return bytes(await file.download_as_bytearray())


# Альбом приходит в Telegram пачкой отдельных сообщений с общим media_group_id: собираем их и сохраняем одной заметкой
ALBUM_WAIT = 2.0
_albums: dict[str, list[Update]] = {}


async def _flush_album(group_id: str) -> None:
    size = -1
    while size != len(_albums[group_id]):  # ждём, пока перестанут приходить новые фото
        size = len(_albums[group_id])
        await asyncio.sleep(ALBUM_WAIT)
    updates = sorted(_albums.pop(group_id), key=lambda u: u.message.message_id)
    images = [await _download_image(u.message) for u in updates]
    caption = "\n".join(u.message.caption for u in updates if u.message.caption)
    await _save(updates[0], "Фото", text=caption, images=images)


async def on_photo(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg.media_group_id:
        first = msg.media_group_id not in _albums
        _albums.setdefault(msg.media_group_id, []).append(update)
        if first:
            await _flush_album(msg.media_group_id)
        return
    await _save(update, "Фото", text=msg.caption or "", images=[await _download_image(msg)])


async def on_voice(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    file = await (msg.voice or msg.audio).get_file()
    try:
        text = await ai.transcribe(bytes(await file.download_as_bytearray()))
    except Exception as e:
        log.exception("transcribe failed")
        await msg.reply_text(f"❌ Не удалось расшифровать голосовое: {e}"[:4000])
        return
    if not text:
        await msg.reply_text("🤷 В голосовом не нашлось слов.")
        return
    await _save(update, "Голос", text=text)


# ---------- вечерний разбор ----------
# В разборе все заметки без типа. Тип выбран — заметка разобрана и больше в разбор не попадает.
# Проект можно поставить раньше (кнопкой под заметкой или здесь), он сам по себе из разбора не убирает.
# callback_data: r:<номер>  |  t:<page_id>:<тип>:<номер>  |  pj:<page_id>:<номер> (раскрыть проекты)
#                p:<page_id>:<проект>:<номер>  |  d:<page_id>:<номер>  |  v:<page_id>


async def _review_view(k: int, show_projects: bool = False) -> tuple[str, InlineKeyboardMarkup | None]:
    items = await notion.review_items()
    if not items:
        return "🎉 Всё разобрано!", None
    if k >= len(items):
        return f"Остальное пропущено. В разборе ещё: {len(items)}.", InlineKeyboardMarkup(
            [[Btn("↩️ Сначала", callback_data="r:0")]]
        )
    k = max(k, 0)
    item = items[k]
    body = await notion.preview(item["id"])
    where = f"📁 {escape(item['project'])} ✓" if item["project"] else "📭 Без проекта"
    text = (
        f"<b>Заметка {k + 1} из {len(items)}</b> · {where}\n\n"
        f'<a href="{item["url"]}">{escape(item["title"])}</a>\n\n{escape(body)}\n\n'
    )
    if show_projects:
        projects = await notion.projects()
        text += "В какой проект?"
        buttons = [
            Btn(f"📁 {p} ✓" if p == item["project"] else f"📁 {p}", callback_data=f"p:{item['id']}:{i}:{k}")
            for i, p in enumerate(projects)
        ]
        rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
        rows.append([Btn("↩️ Назад", callback_data=f"r:{k}")])
        return text, InlineKeyboardMarkup(rows)

    text += "Что это?"
    buttons = [Btn(t, callback_data=f"t:{item['id']}:{i}:{k}") for i, t in enumerate(await notion.types())]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    project_label = f"📁 {item['project']} ✓" if item["project"] else "📁 В проект"
    rows.append(
        [
            Btn(project_label, callback_data=f"pj:{item['id']}:{k}"),
            Btn("🤖 ИИ", callback_data=f"a:{item['id']}"),
            Btn("👁 Целиком", callback_data=f"v:{item['id']}"),
        ]
    )
    nav = [Btn("🗑", callback_data=f"d:{item['id']}:{k}")]
    if k > 0:
        nav.append(Btn("◀️", callback_data=f"r:{k - 1}"))
    nav.append(Btn("⏭ Позже", callback_data=f"r:{k + 1}"))
    rows.append(nav)
    return text, InlineKeyboardMarkup(rows)


async def show_note(message: Message, page_id: str) -> None:
    """👁 Заметка целиком прямо в чате, без захода в Notion."""
    note = await notion.read_note(page_id)
    parts = [p for p in (note["summary"], note["details"] and f"📝 Полный текст и детали\n\n{note['details']}") if p]
    text = "\n\n".join(parts) or "Заметка пустая."
    for i in range(0, len(text), 4000):  # лимит Telegram на одно сообщение
        await message.reply_text(text[i : i + 4000])
    for url in note["images"][:10]:
        try:
            await message.reply_photo(url)
        except Exception:
            log.exception("send photo failed")


# ---------- 🤖 Спросить ИИ по заметке ----------
# callback_data: a:<page_id> (начать)  |  aq:<page_id>:<номер> (быстрое действие)
# Какая заметка имеется в виду, бот узнаёт по ссылке на Notion внутри своего сообщения:
# так ответ и уточнения работают, даже если бот успел заснуть и проснуться.

AI_PROMPT_MARK = "🤖 Что сделать с заметкой"
AI_ANSWER_MARK = "🤖 «"
PRESETS = [
    ("📝 Кратко", "Кратко перескажи главное, 3–5 пунктов."),
    ("✅ Задачи", "Вытащи все задачи и действия чек-листом, со сроками, если они есть."),
    ("📊 Таблицей", "Сведи данные заметки в таблицу, если есть что сводить."),
    ("🌐 Перевести", "Переведи заметку на английский, а если она на английском — на русский."),
]


def _ai_page(message: Message) -> str | None:
    """Ищет в сообщении бота ссылку на заметку в Notion и достаёт из неё id страницы."""
    if not message.text or not message.text.startswith(("🤖", "✍️", EXPAND_MARK)):
        return None
    for entity in message.entities or []:
        if entity.url and "notion" in entity.url:
            if m := re.search(r"([0-9a-f]{32})(?:\?|$)", entity.url.replace("-", "")):
                return m.group(1)
    return None


def _chunks(html_text: str, limit: int = 3800) -> list[str]:
    """Режет HTML на сообщения по строкам, не разрывая блоки <pre>."""
    parts = re.split(r"(<pre>.*?</pre>)", html_text, flags=re.S)
    pieces = []
    for part in parts:
        pieces += [part] if part.startswith("<pre>") else part.split("\n")
    chunks, current = [], ""
    for piece in pieces:
        candidate = f"{current}\n{piece}" if current else piece
        if len(candidate) > limit and current:
            chunks.append(current)
            current = piece
        else:
            current = candidate
    return [c for c in chunks + [current] if c.strip()]


async def start_ai(message: Message, page_id: str) -> None:
    item = await notion.page_info(page_id)
    link = f'<a href="{item["url"]}">{escape(item["title"])}</a>'
    buttons = [Btn(label, callback_data=f"aq:{page_id}:{i}") for i, (label, _) in enumerate(PRESETS)]
    await message.reply_text(
        f"{AI_PROMPT_MARK} {link}?\nНажмите быстрое действие или напишите свой запрос ответом на это сообщение.",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([buttons[:2], buttons[2:]]),
    )
    await message.reply_text(
        f"✍️ Свой запрос к {link}:",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=ForceReply(input_field_placeholder="например: посчитай общую сумму"),
    )


async def _ask_ai(message: Message, page_id: str, question: str, history: str = "", label: str = "") -> None:
    label = label or question
    status = await message.reply_text("🤖 Думаю…")
    try:
        item = await notion.page_info(page_id)
        note = await notion.read_note(page_id)
        images = []
        for url in note["images"][:4]:  # оригиналы фото, чтобы ИИ сверил цифры
            try:
                images.append(await notion.download(url))
            except Exception:
                log.exception("image download failed")
        context = "\n\n".join(p for p in (f"# {item['title']}", note["summary"], note["details"]) if p)
        answer = await ai.ask(context, question, images, history)
        await notion.add_answer(page_id, label, to_blocks(answer))
    except Exception as e:
        log.exception("ask failed")
        await status.edit_text(f"❌ ИИ не ответил: {e}"[:4000])
        return
    header = f'{AI_ANSWER_MARK}{escape(label[:100])}» · <a href="{item["url"]}">{escape(item["title"])}</a>\n\n'
    footer = "\n\n↩️ Ответьте на это сообщение, чтобы уточнить. Ответ сохранён в заметке."
    chunks = _chunks(tg_html(answer))
    for i, chunk in enumerate(chunks):
        # Ссылка на заметку в каждом куске: уточнять можно ответом на любой из них
        text = header + chunk + (footer if i == len(chunks) - 1 else "")
        if i == 0:
            await status.edit_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        else:
            await message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def on_ai_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if q.from_user.id != c.OWNER_ID:
        return
    action, page_id, *rest = q.data.split(":")
    if action == "a":
        await start_ai(q.message, page_id)
    else:
        label, question = PRESETS[int(rest[0])]
        await _ask_ai(q.message, page_id, question, label=label)


# ---------- ✨ Расширение заметки под её тип ----------
# callback_data: x:<page_id>:<номер> (из разбора)  |  xs:<page_id> (сохранить)  |  xc:<page_id> (не надо)

EXPAND_MARK = "✨ "
QUESTIONS_MARK = "❓ Уточните:"
# Черновики по id сообщения: точный Markdown. Если бот перезапустился, берём текст из самого сообщения.
_drafts: dict[int, tuple[str, str]] = {}


def _draft_from_message(message: Message) -> tuple[str, str]:
    """(тип, черновик) из сообщения бота: первая строка «✨ <тип> · <заметка>», дальше черновик до вопросов."""
    if message.message_id in _drafts:
        return _drafts[message.message_id]
    first, _, rest = message.text.partition("\n")
    type_name = first.removeprefix(EXPAND_MARK).split(" · ")[0]
    return type_name, rest.split(QUESTIONS_MARK)[0].split("↩️")[0].strip()


async def expand_note(message: Message, page_id: str, reply: Message | None = None, answers: str = "") -> None:
    status = await message.reply_text("✨ Расширяю…" if not reply else "✨ Дополняю…")
    try:
        item = await notion.page_info(page_id)
        type_name, draft = _draft_from_message(reply) if reply else (item["type"] or "Заметка", "")
        note = await notion.read_note(page_id)
        images = []
        for url in note["images"][:4]:
            try:
                images.append(await notion.download(url))
            except Exception:
                log.exception("image download failed")
        titles = await notion.project_titles(item["project"], page_id) if item["project"] else []
        context = "\n\n".join(p for p in (f"# {item['title']}", note["summary"], note["details"]) if p)
        description, questions = await ai.expand(type_name, context, item["project"], titles, images, draft, answers)
    except Exception as e:
        log.exception("expand failed")
        await status.edit_text(f"❌ Не получилось расширить: {e}"[:4000])
        return
    header = f'{EXPAND_MARK}{escape(type_name)} · <a href="{item["url"]}">{escape(item["title"])}</a>\n\n'
    tail = ""
    if questions:
        tail = f"\n\n<b>{QUESTIONS_MARK}</b>\n" + "\n".join(f"{i}. {escape(q)}" for i, q in enumerate(questions, 1))
        tail += "\n\n↩️ Ответьте на это сообщение — дополню описание. Или сохраните как есть."
    body = tg_html(description)
    if len(header) + len(body) + len(tail) > 4000:
        body = body[: 4000 - len(header) - len(tail) - 40].rsplit("\n", 1)[0] + "\n… (полностью — после сохранения в Notion)"
    keyboard = InlineKeyboardMarkup(
        [[Btn("✅ Сохранить в заметку", callback_data=f"xs:{page_id}"), Btn("✖️ Не надо", callback_data=f"xc:{page_id}")]]
    )
    sent = await status.edit_text(
        header + body + tail, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=keyboard
    )
    _drafts[sent.message_id if hasattr(sent, "message_id") else status.message_id] = (type_name, description)


async def on_expand_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q.from_user.id != c.OWNER_ID:
        await q.answer()
        return
    action, page_id = q.data.split(":")[:2]
    if action == "xc":
        await q.answer()
        _drafts.pop(q.message.message_id, None)
        await q.edit_message_reply_markup(None)
        return
    try:
        type_name, draft = _draft_from_message(q.message)
        await notion.add_expansion(page_id, type_name, to_blocks(draft))
    except Exception as e:
        log.exception("save expansion failed")
        await q.answer(f"Ошибка: {e}"[:200], show_alert=True)
        return
    await q.answer("Сохранено ✓")
    _drafts.pop(q.message.message_id, None)
    await q.edit_message_reply_markup(InlineKeyboardMarkup([[Btn("✅ Сохранено в заметке", callback_data="noop")]]))


# ---------- проекты ----------
# callback_data: pl  |  pa:<номер> (спросить про удаление)  |  pk:<номер> (удалить)


async def _projects_view() -> tuple[str, InlineKeyboardMarkup | None]:
    names = await notion.projects()
    hint = "Добавить: /addproject Название (можно несколько, каждый с новой строки)."
    if not names:
        return f"Проектов пока нет.\n\n{hint}", None
    rows = [[Btn(f"🗑 {n}", callback_data=f"pa:{i}")] for i, n in enumerate(names)]
    return "Проекты:\n" + "\n".join(f"📁 {n}" for n in names) + f"\n\n{hint}", InlineKeyboardMarkup(rows)


async def projects_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = await _projects_view()
    await update.message.reply_text(text, reply_markup=kb)


async def _add_projects_text(raw: str) -> str:
    # Проекты разделяются переносом строки или запятой (запятую Notion в названии всё равно не разрешает)
    names = [n.strip()[:100] for n in re.split(r"[\n,]", raw) if n.strip()]
    if not names:
        return "Напишите название после команды, например:\n/addproject Сайт-портфолио"
    added = await notion.add_projects(names)
    skipped = [n for n in names if n not in added]
    text = ("✅ Добавлено: " + ", ".join(added)) if added else "Ничего нового не добавлено."
    if skipped:
        text += "\nУже были: " + ", ".join(skipped)
    return text + "\n\n/projects — список проектов"


async def addproject(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    raw = re.sub(r"^/addproject(@\w+)?", "", update.message.text, count=1)
    if not raw.strip():
        await _ask_project_names(update.message)
        return
    await update.message.reply_text(await _add_projects_text(raw))


async def on_project_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q.from_user.id != c.OWNER_ID:
        await q.answer()
        return
    action, _, arg = q.data.partition(":")
    try:
        names = await notion.projects()
        if action in ("pa", "pk") and int(arg) >= len(names):
            await q.answer("Список изменился")
        elif action == "pa":
            name = names[int(arg)]
            await q.answer()
            await q.edit_message_text(
                f"Удалить проект «{name}»?\nУ идей, отправленных в него, поле «Проект» станет пустым.",
                reply_markup=InlineKeyboardMarkup(
                    [[Btn("Да, удалить", callback_data=f"pk:{arg}"), Btn("Отмена", callback_data="pl")]]
                ),
            )
            return
        elif action == "pk":
            await notion.delete_project(names[int(arg)])
            await q.answer(f"Удалено: {names[int(arg)]}")
        else:
            await q.answer()
        text, kb = await _projects_view()
    except Exception as e:
        log.exception("project button failed")
        await q.answer(f"Ошибка: {e}"[:200], show_alert=True)
        return
    await q.edit_message_text(text, reply_markup=kb)


async def razbor(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = await _review_view(0)
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def on_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q.from_user.id != c.OWNER_ID:
        await q.answer()
        return
    action, *args = q.data.split(":")
    show_projects = False
    try:
        if action == "t":
            page_id, idx, k = args[0], int(args[1]), int(args[2])
            names = await notion.types()
            if idx >= len(names):
                await q.answer("Список типов изменился, попробуйте ещё раз")
            else:
                await notion.set_type(page_id, names[idx])
                await q.answer(f"{names[idx]} ✓")
                item = await notion.page_info(page_id)
                await q.edit_message_text(
                    f'<a href="{item["url"]}">{escape(item["title"])}</a> → {escape(names[idx])} ✓\n\n'
                    "Расширить описание под этот тип? ИИ дополнит заметку с учётом проекта и задаст пару уточняющих вопросов.",
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=InlineKeyboardMarkup(
                        [[Btn("✨ Расширить", callback_data=f"x:{page_id}:{k}"), Btn("⏭ Следующая", callback_data=f"r:{k}")]]
                    ),
                )
                return
        elif action == "x":
            # Карточка сразу переходит к следующей заметке, а черновик описания приходит отдельными сообщениями ниже
            page_id, k = args[0], int(args[1])
            await q.answer("✨ Расширяю…")
            text, kb = await _review_view(k)
            await q.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            await expand_note(q.message, page_id)
            return
        elif action == "pj":
            page_id, k = args[0], int(args[1])
            show_projects = True
            await q.answer()
        elif action == "p":
            page_id, idx, k = args[0], int(args[1]), int(args[2])
            projects = await notion.projects()
            if idx >= len(projects):
                await q.answer("Список проектов изменился, попробуйте ещё раз")
            else:
                await notion.file_to_project(page_id, projects[idx])
                await q.answer(f"→ {projects[idx]}")
        elif action == "d":
            page_id, k = args[0], int(args[1])
            await notion.trash(page_id)
            await q.answer("Удалено (можно восстановить из корзины Notion)")
        elif action == "v":
            await q.answer()
            await show_note(q.message, args[0])
            return
        else:
            k = int(args[0])
            await q.answer()
        text, kb = await _review_view(k, show_projects)
    except Exception as e:
        log.exception("button failed")
        await q.answer(f"Ошибка: {e}"[:200], show_alert=True)
        return
    await q.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# ---------- запуск ----------

COMMANDS = [
    ("start", "Меню всех команд"),
    ("razbor", "Разобрать входящие"),
    ("projects", "Список проектов"),
    ("addproject", "Добавить проект"),
    ("remindlink", "Ссылка для вечернего напоминания"),
]


async def set_commands(app: Application) -> None:
    """Меню «/» в Telegram."""
    await app.bot.set_my_commands(COMMANDS)


async def serve(app: Application) -> None:
    """Свой веб-сервер вместо run_webhook: кроме Telegram он принимает вечерний пинг от cron-job.org."""
    webhook_secret = c.secret("webhook")

    async def telegram(request: web.Request) -> web.Response:
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != webhook_secret:
            return web.Response(status=403)
        await app.update_queue.put(Update.de_json(await request.json(), app.bot))
        return web.Response()

    async def remind_hook(request: web.Request) -> web.Response:
        if request.match_info["secret"] != c.secret("remind"):
            return web.Response(status=404)
        try:
            result = await remind.send(app.bot, force="force" in request.query)
        except Exception as e:
            log.exception("remind failed")
            return web.Response(status=500, text=str(e))
        return web.Response(text=result)

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    server = web.Application()
    server.add_routes(
        [
            web.post("/telegram", telegram),
            web.get("/remind/{secret}", remind_hook),
            web.post("/remind/{secret}", remind_hook),
            web.get("/", health),
        ]
    )
    runner = web.AppRunner(server)
    await runner.setup()
    async with app:
        await app.bot.set_webhook(
            f"{c.WEBHOOK_BASE.rstrip('/')}/telegram", secret_token=webhook_secret, allowed_updates=Update.ALL_TYPES
        )
        await set_commands(app)
        await app.start()
        await web.TCPSite(runner, "0.0.0.0", c.PORT).start()
        log.info("Сервер слушает порт %s", c.PORT)
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
            await app.stop()


def main() -> None:
    c.require("TELEGRAM_TOKEN", "HF_TOKEN", "NOTION_TOKEN")
    builder = Application.builder().token(c.TELEGRAM_TOKEN).concurrent_updates(True).post_init(set_commands)
    if c.WEBHOOK_BASE:
        builder = builder.updater(None)  # обновления приходят в наш сервер, встроенный не нужен
    app = builder.build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^m:"))
    app.add_handler(CommandHandler("razbor", razbor, filters=owner))
    app.add_handler(CommandHandler("remindlink", remindlink, filters=owner))
    app.add_handler(CommandHandler("projects", projects_cmd, filters=owner))
    app.add_handler(CommandHandler("addproject", addproject, filters=owner))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(r|t|x|pj|p|d|v):"))
    app.add_handler(CallbackQueryHandler(on_expand_button, pattern=r"^x[sc]:"))
    app.add_handler(CallbackQueryHandler(lambda u, _: u.callback_query.answer(), pattern=r"^noop$"))
    app.add_handler(CallbackQueryHandler(on_panel, pattern=r"^n[px]?:"))
    app.add_handler(CallbackQueryHandler(on_ai_button, pattern=r"^aq?:"))
    app.add_handler(CallbackQueryHandler(on_project_button, pattern=r"^p[akl]"))
    app.add_handler(MessageHandler(owner & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(owner & (filters.PHOTO | filters.Document.IMAGE), on_photo))
    app.add_handler(MessageHandler(owner & (filters.VOICE | filters.AUDIO), on_voice))

    if c.WEBHOOK_BASE:
        asyncio.run(serve(app))
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
