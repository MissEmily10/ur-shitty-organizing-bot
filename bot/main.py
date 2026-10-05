import asyncio
import logging
from html import escape

from telegram import InlineKeyboardButton as Btn
from aiohttp import web
from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from . import ai, notion, remind
from . import config as c
from .markdown import to_blocks

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

owner = filters.User(user_id=c.OWNER_ID)


# ---------- приём заметок ----------


async def start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if uid != c.OWNER_ID:
        await update.message.reply_text(f"Ваш Telegram ID: {uid}\nВпишите его в переменную OWNER_ID и перезапустите бота.")
        return
    await update.message.reply_text(
        "Присылайте текст, фото блокнота или голосовое — я разложу всё в Notion.\n"
        "/razbor — разобрать входящие прямо сейчас.\n"
        "/remindlink — ссылка для вечернего напоминания."
    )


async def remindlink(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not c.WEBHOOK_BASE:
        await update.message.reply_text("Ссылка появится, когда бот запущен на хостинге (Render).")
        return
    url = f"{c.WEBHOOK_BASE.rstrip('/')}/remind/{c.secret('remind')}"
    await update.message.reply_text(
        f"Ссылка для cron-job.org:\n{url}\n\n"
        f"Проверить прямо сейчас (пуш придёт, даже если сегодня уже был):\n{url}?force=1\n\n"
        "Никому её не показывайте."
    )


async def _save(update: Update, source: str, *, text: str = "", image: bytes | None = None) -> None:
    status = await update.message.reply_text("⏳ Обрабатываю…")
    note = ""
    try:
        try:
            idea = await ai.structure(text=text, image=image)
        except Exception as e:
            if image:
                raise
            # ИИ недоступен: текст всё равно не теряем, кладём как есть
            log.exception("AI failed, saving raw text")
            idea = ai.Idea(title=text.splitlines()[0][:60], summary=text)
            note = f"\n\n⚠️ Сохранено без обработки ИИ: {escape(str(e)[:500])}"
        url = await notion.create_idea(idea.title, source, idea.tags, to_blocks(idea.summary), to_blocks(idea.details))
    except Exception as e:
        log.exception("save failed")
        await status.edit_text(f"❌ Не получилось сохранить: {e}"[:4000])
        return
    await status.edit_text(
        f'✅ <a href="{url}">{escape(idea.title)}</a>{note}', parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def on_text(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await _save(update, "Текст", text=update.message.text)


async def on_photo(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    file = await (msg.photo[-1] if msg.photo else msg.document).get_file()
    image = bytes(await file.download_as_bytearray())
    await _save(update, "Фото", text=msg.caption or "", image=image)


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
    builder = Application.builder().token(c.TELEGRAM_TOKEN).concurrent_updates(True)
    if c.WEBHOOK_BASE:
        builder = builder.updater(None)  # обновления приходят в наш сервер, встроенный не нужен
    app = builder.build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("razbor", razbor, filters=owner))
    app.add_handler(CommandHandler("remindlink", remindlink, filters=owner))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^[rpd]:"))
    app.add_handler(MessageHandler(owner & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(owner & (filters.PHOTO | filters.Document.IMAGE), on_photo))
    app.add_handler(MessageHandler(owner & (filters.VOICE | filters.AUDIO), on_voice))

    if c.WEBHOOK_BASE:
        asyncio.run(serve(app))
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
