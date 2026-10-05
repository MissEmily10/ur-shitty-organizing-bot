import asyncio
import logging
import re
from html import escape

from aiohttp import web
from telegram import BotCommandScopeChat, ForceReply, InlineKeyboardMarkup, Message, Update
from telegram import InlineKeyboardButton as Btn
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from . import ai, notion, remind
from . import config as c
from .markdown import to_blocks

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

owner = filters.User(user_id=c.OWNER_ID)
# Владелец + участники из таблицы Notion «Участники бота». Список обновляется на лету при добавлении и удалении.
member = filters.User(user_id=c.OWNER_ID)


def is_owner(uid: int) -> bool:
    return uid == c.OWNER_ID


def is_member(uid: int) -> bool:
    return uid in member.user_ids


# ---------- меню ----------

MENU = (
    "Я складываю мысли в Notion.\n\n"
    "✍️ Просто пиши, присылай фото блокнота или голосовые: всё, что не начинается с «/», становится заметкой.\n\n"
    "<b>Команды</b>\n"
    "/razbor — разобрать свои входящие\n"
    "/projects — список проектов\n"
    "/addproject — добавить проект (бот спросит название)\n"
    "{owner_commands}"
    "/start — это меню\n\n"
    "Или жми кнопку 👇"
)
OWNER_COMMANDS = "/members — участники бота\n/remindlink — ссылка для вечернего напоминания\n"
# Ответ на это сообщение бота — названия проектов, а не заметка
ADD_PROMPT = "Напишите названия проектов ответом на это сообщение: через запятую или каждый с новой строки."


def menu(uid: int) -> tuple[str, InlineKeyboardMarkup]:
    rows = [
        [Btn("🗂 Разобрать входящие", callback_data="m:razbor")],
        [Btn("📁 Проекты", callback_data="m:projects"), Btn("➕ Добавить проект", callback_data="m:add")],
    ]
    if is_owner(uid):
        rows.append([Btn("👥 Участники", callback_data="m:members"), Btn("🔔 Ссылка напоминания", callback_data="m:remind")])
    text = MENU.format(owner_commands=OWNER_COMMANDS if is_owner(uid) else "")
    return text, InlineKeyboardMarkup(rows)


async def start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not c.OWNER_ID:
        await update.message.reply_text(f"Ваш Telegram ID: {uid}\nВпишите его в переменную OWNER_ID и перезапустите бота.")
        return
    if not is_member(uid):
        await stranger(update, _)
        return
    text, kb = menu(uid)
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def on_menu(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if not is_member(uid):
        return
    action = q.data.removeprefix("m:")
    try:
        if action == "razbor":
            text, kb = await _review_view(uid, 0)
            await q.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        elif action == "projects":
            text, kb = await _projects_view(uid)
            await q.message.reply_text(text, reply_markup=kb)
        elif action == "add":
            await _ask_project_names(q.message)
        elif action == "members" and is_owner(uid):
            text, kb = await _members_view()
            await q.message.reply_text(text, reply_markup=kb)
        elif action == "remind" and is_owner(uid):
            await q.message.reply_text(_remind_text())
    except Exception as e:
        log.exception("menu failed")
        await q.message.reply_text(f"❌ Ошибка: {e}"[:4000])


async def _ask_project_names(message: Message) -> None:
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


# ---------- доступ для новых людей ----------
# callback_data: j:req (попросить)  |  ja:<id> (пустить)  |  jd:<id> (отклонить)

_requested: set[int] = set()


async def stranger(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "Это закрытый бот для заметок команды. Попросить доступ у владельца?",
        reply_markup=InlineKeyboardMarkup([[Btn("🙋 Попросить доступ", callback_data="j:req")]]),
    )


async def on_join_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    user = q.from_user
    action, _, arg = q.data.partition(":")
    if action == "j":
        if is_member(user.id):
            await q.answer("У вас уже есть доступ, напишите /start")
        elif user.id in _requested:
            await q.answer("Запрос уже отправлен, ждите ответа")
        else:
            _requested.add(user.id)
            who = escape(user.full_name) + (f" (@{escape(user.username)})" if user.username else "")
            await ctx.bot.send_message(
                c.OWNER_ID,
                f"🙋 {who} просит доступ к боту.\nTelegram ID: {user.id}",
                reply_markup=InlineKeyboardMarkup(
                    [[Btn("✅ Пустить", callback_data=f"ja:{user.id}"), Btn("❌ Отклонить", callback_data=f"jd:{user.id}")]]
                ),
                parse_mode=ParseMode.HTML,
            )
            await q.answer()
            await q.edit_message_text("Запрос отправлен. Я напишу, когда владелец ответит.")
        return

    if not is_owner(user.id):
        await q.answer()
        return
    uid = int(arg)
    try:
        if action == "ja":
            chat = await ctx.bot.get_chat(uid)
            name = chat.full_name or str(uid)
            await notion.add_member(uid, name, chat.username)
            member.add_user_ids(uid)
            await q.answer()
            await q.edit_message_text(f"✅ {name} теперь участник. Убрать: /members")
            text, kb = menu(uid)
            await ctx.bot.send_message(uid, "✅ Доступ открыт!\n\n" + text, reply_markup=kb, parse_mode=ParseMode.HTML)
        else:
            await q.answer()
            await q.edit_message_text("❌ Запрос отклонён")
            await ctx.bot.send_message(uid, "Владелец отклонил запрос на доступ.")
        _requested.discard(uid)
    except Exception as e:
        log.exception("join decision failed")
        await q.answer(f"Ошибка: {e}"[:200], show_alert=True)


# ---------- участники (только владелец) ----------
# callback_data: ml (список)  |  mr:<id> (спросить про удаление)  |  mk:<id> (удалить)


async def _members_view() -> tuple[str, InlineKeyboardMarkup | None]:
    people = await notion.members()
    hint = "Новые люди пишут боту и нажимают «Попросить доступ», вам придёт запрос."
    if not people:
        return f"Участников пока нет, только вы.\n\n{hint}", None
    rows = [[Btn(f"🗑 {m['name']}", callback_data=f"mr:{m['tg']}")] for m in people]
    return "Участники:\n" + "\n".join(f"👤 {m['name']}" for m in people) + f"\n\n{hint}", InlineKeyboardMarkup(rows)


async def members_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = await _members_view()
    await update.message.reply_text(text, reply_markup=kb)


async def on_member_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer()
        return
    action, _, arg = q.data.partition(":")
    try:
        if action == "mr":
            name = next((m["name"] for m in await notion.members() if m["tg"] == int(arg)), arg)
            await q.answer()
            await q.edit_message_text(
                f"Убрать {name} из бота?\nЕго заметки останутся в Notion.",
                reply_markup=InlineKeyboardMarkup(
                    [[Btn("Да, убрать", callback_data=f"mk:{arg}"), Btn("Отмена", callback_data="ml")]]
                ),
            )
            return
        if action == "mk":
            await notion.remove_member(int(arg))
            member.remove_user_ids(int(arg))
            await q.answer("Участник удалён")
        else:
            await q.answer()
        text, kb = await _members_view()
    except Exception as e:
        log.exception("member button failed")
        await q.answer(f"Ошибка: {e}"[:200], show_alert=True)
        return
    await q.edit_message_text(text, reply_markup=kb)


# ---------- приём заметок ----------


async def _save(update: Update, source: str, *, text: str = "", image: bytes | None = None) -> None:
    user = update.effective_user
    status = await update.message.reply_text("⏳ Обрабатываю…")
    note = ""
    try:
        if not is_owner(user.id) and await notion.created_today(user.id) >= c.MEMBER_DAILY_LIMIT:
            await status.edit_text(f"⛔ Лимит {c.MEMBER_DAILY_LIMIT} заметок в день исчерпан, продолжим завтра.")
            return
        try:
            idea = await ai.structure(text=text, image=image)
        except Exception as e:
            if image:
                raise
            # ИИ недоступен: текст всё равно не теряем, кладём как есть
            log.exception("AI failed, saving raw text")
            idea = ai.Idea(title=text.splitlines()[0][:60], summary=text)
            note = f"\n\n⚠️ Сохранено без обработки ИИ: {escape(str(e)[:500])}"
        url = await notion.create_idea(
            idea.title, source, idea.tags, to_blocks(idea.summary), to_blocks(idea.details), user.id, user.full_name
        )
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


async def _review_view(uid: int, k: int) -> tuple[str, InlineKeyboardMarkup | None]:
    items = await notion.unsorted(uid)
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
    text, kb = await _review_view(update.effective_user.id, 0)
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def on_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    uid = q.from_user.id
    if not is_member(uid):
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
        text, kb = await _review_view(uid, k)
    except Exception as e:
        log.exception("button failed")
        await q.answer(f"Ошибка: {e}"[:200], show_alert=True)
        return
    await q.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# ---------- проекты (общие: добавлять могут все, удалять только владелец) ----------
# callback_data: pl  |  pa:<номер> (спросить про удаление)  |  pk:<номер> (удалить)


async def _projects_view(uid: int) -> tuple[str, InlineKeyboardMarkup | None]:
    names = await notion.projects()
    hint = "Добавить: /addproject"
    if not names:
        return f"Проектов пока нет.\n\n{hint}", None
    text = "Проекты:\n" + "\n".join(f"📁 {n}" for n in names) + f"\n\n{hint}"
    if not is_owner(uid):
        return text, None
    return text, InlineKeyboardMarkup([[Btn(f"🗑 {n}", callback_data=f"pa:{i}")] for i, n in enumerate(names)])


async def projects_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = await _projects_view(update.effective_user.id)
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
    uid = q.from_user.id
    if not is_owner(uid):
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
        text, kb = await _projects_view(uid)
    except Exception as e:
        log.exception("project button failed")
        await q.answer(f"Ошибка: {e}"[:200], show_alert=True)
        return
    await q.edit_message_text(text, reply_markup=kb)


# ---------- запуск ----------

COMMANDS = [
    ("start", "Меню всех команд"),
    ("razbor", "Разобрать входящие"),
    ("projects", "Список проектов"),
    ("addproject", "Добавить проект"),
]
OWNER_EXTRA = [("members", "Участники бота"), ("remindlink", "Ссылка для вечернего напоминания")]


async def on_startup(app: Application) -> None:
    """Меню «/» в Telegram (у владельца в нём больше команд) и список участников из Notion."""
    await app.bot.set_my_commands(COMMANDS)
    if c.OWNER_ID:
        await app.bot.set_my_commands(COMMANDS + OWNER_EXTRA, scope=BotCommandScopeChat(c.OWNER_ID))
    try:
        member.add_user_ids([m["tg"] for m in await notion.members()])
    except Exception:
        # Без списка участников бот всё равно работает для владельца
        log.exception("could not load members")


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
            result = await remind.send(app.bot, sorted(member.user_ids), force="force" in request.query)
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
        await on_startup(app)
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
    builder = Application.builder().token(c.TELEGRAM_TOKEN).concurrent_updates(True).post_init(on_startup)
    if c.WEBHOOK_BASE:
        builder = builder.updater(None)  # обновления приходят в наш сервер, встроенный не нужен
    app = builder.build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CallbackQueryHandler(on_join_button, pattern=r"^j[ad]?:"))
    app.add_handler(MessageHandler(~member, stranger))
    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^m:"))
    app.add_handler(CommandHandler("razbor", razbor, filters=member))
    app.add_handler(CommandHandler("projects", projects_cmd, filters=member))
    app.add_handler(CommandHandler("addproject", addproject, filters=member))
    app.add_handler(CommandHandler("members", members_cmd, filters=owner))
    app.add_handler(CommandHandler("remindlink", remindlink, filters=owner))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^[rpd]:"))
    app.add_handler(CallbackQueryHandler(on_project_button, pattern=r"^p[akl]"))
    app.add_handler(CallbackQueryHandler(on_member_button, pattern=r"^m[rkl]"))
    app.add_handler(MessageHandler(member & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(member & (filters.PHOTO | filters.Document.IMAGE), on_photo))
    app.add_handler(MessageHandler(member & (filters.VOICE | filters.AUDIO), on_voice))

    if c.WEBHOOK_BASE:
        asyncio.run(serve(app))
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
