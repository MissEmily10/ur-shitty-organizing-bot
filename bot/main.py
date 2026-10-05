import hashlib
import logging
from html import escape

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from . import ai, notion
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
        "/razbor — разобрать входящие прямо сейчас."
    )


async def _save(update: Update, source: str, *, text: str = "", image: bytes | None = None) -> None:
    status = await update.message.reply_text("⏳ Обрабатываю…")
    try:
        try:
            idea = await ai.structure(text=text, image=image)
        except Exception:
            if image:
                raise
            # ИИ недоступен: текст всё равно не теряем, кладём как есть
            log.exception("AI failed, saving raw text")
            idea = ai.Idea(title=text.splitlines()[0][:60], markdown=text)
        url = await notion.create_idea(idea.title, source, idea.tags, to_blocks(idea.markdown))
    except Exception as e:
        log.exception("save failed")
        await status.edit_text(f"❌ Не получилось сохранить: {e}"[:4000])
        return
    await status.edit_text(
        f'✅ <a href="{url}">{escape(idea.title)}</a>', parse_mode=ParseMode.HTML, disable_web_page_preview=True
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


def main() -> None:
    c.require("TELEGRAM_TOKEN", "HF_TOKEN", "NOTION_TOKEN")
    app = Application.builder().token(c.TELEGRAM_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("razbor", razbor, filters=owner))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^[rpd]:"))
    app.add_handler(MessageHandler(owner & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(owner & (filters.PHOTO | filters.Document.IMAGE), on_photo))
    app.add_handler(MessageHandler(owner & (filters.VOICE | filters.AUDIO), on_voice))

    if c.WEBHOOK_BASE:
        secret = hashlib.sha256(c.TELEGRAM_TOKEN.encode()).hexdigest()[:32]
        app.run_webhook(
            listen="0.0.0.0",
            port=c.PORT,
            url_path="telegram",
            webhook_url=f"{c.WEBHOOK_BASE.rstrip('/')}/telegram",
            secret_token=secret,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
