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
from .markdown import to_blocks

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
        url = await notion.create_idea(idea.title, source, idea.tags, blocks, to_blocks(idea.details))
    except Exception as e:
        log.exception("save failed")
        await status.edit_text(f"❌ Не получилось сохранить: {e}"[:4000])
        return
    await status.edit_text(
        f'✅ <a href="{url}">{escape(idea.title)}</a>{note}', parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    reply = update.message.reply_to_message
    if reply and reply.from_user and reply.from_user.id == ctx.bot.id and reply.text == ADD_PROMPT:
        await update.message.reply_text(await _add_projects_text(update.message.text))
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
# callback_data: r:<номер>  |  p:<page_id>:<проект>:<номер>  |  d:<page_id>:<номер>


async def _review_view(k: int) -> tuple[str, InlineKeyboardMarkup | None]:
    items = await notion.unsorted()
    if not items:
        return "🎉 Всё разобрано!", None
    if k >= len(items):
        return f"Остальное пропущено. Неразобранных: {len(items)}.", InlineKeyboardMarkup(
            [[Btn("↩️ Начать сначала", callback_data="r:0")]]
        )
    item = items[k]
    projects = await notion.projects()
    body = await notion.preview(item["id"])
    text = (
        f"<b>Идея {k + 1} из {len(items)}</b>\n\n"
        f'<a href="{item["url"]}">{escape(item["title"])}</a>\n\n{escape(body)}\n\n'
        "Куда отправить?"
    )
    buttons = [Btn(f"📁 {p}", callback_data=f"p:{item['id']}:{i}:{k}") for i, p in enumerate(projects)]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([Btn("🗑 Удалить", callback_data=f"d:{item['id']}:{k}"), Btn("⏭ Позже", callback_data=f"r:{k + 1}")])
    return text, InlineKeyboardMarkup(rows)


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
    try:
        if action == "p":
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
        else:
            k = int(args[0])
            await q.answer()
        text, kb = await _review_view(k)
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
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^[rpd]:"))
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
