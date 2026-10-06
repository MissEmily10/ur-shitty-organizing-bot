import asyncio
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from html import escape

from telegram import InlineKeyboardButton as Btn
from aiohttp import web
from telegram import BotCommandScopeChat, ForceReply, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from . import ai, documents, notion, remind
from . import config as c
from .markdown import tg_html, to_blocks

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

owner = filters.User(user_id=c.OWNER_ID)
# Владелица + участники, вошедшие по коду приглашения. Список подгружается из Notion при запуске
# и меняется на лету, когда кто-то входит по коду или его убирают.
member = filters.User(user_id=c.OWNER_ID)


def is_owner(uid: int) -> bool:
    return uid == c.OWNER_ID


def is_member(uid: int) -> bool:
    return uid in member.user_ids


async def private_only(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Бот работает только в личных чатах: в группе заметки и разбор увидели бы посторонние.
    Если бота добавили в группу, он из неё выходит."""
    chat = update.effective_chat
    if chat and chat.type != ChatType.PRIVATE:
        if update.callback_query:
            await update.callback_query.answer()
        try:
            await ctx.bot.leave_chat(chat.id)
        except Exception:
            log.info("could not leave chat %s", chat.id)
        raise ApplicationHandlerStop


async def own_page(uid: int, page_id: str) -> dict:
    """Участник может трогать только свои заметки (данные кнопок может подделать модифицированный клиент).
    Владелица — любые: это её Notion."""
    item = await notion.page_info(page_id)
    if is_owner(uid) or item.get("author_id") == uid:
        return item
    raise PermissionError("Это не ваша заметка.")


# ---------- меню ----------

MENU = (
    "Я складываю твои мысли в Notion.\n\n"
    "✍️ Просто пиши, присылай фото блокнота, голосовые или документы: всё, что не начинается с «/», становится заметкой.\n\n"
    "<b>Команды</b>\n"
    "/razbor — разобрать свои входящие\n"
    "/projects — список проектов\n"
    "/addproject — добавить проект (бот спросит название)\n"
    "/ask — спросить ИИ по проекту или по всем своим заметкам за период\n"
    "{owner_commands}"
    "/start — это меню\n\n"
    "Или жми кнопку 👇"
)
OWNER_COMMANDS = (
    "/invite — пригласить участника\n/members — участники и приглашения\n"
)


def menu(uid: int) -> tuple[str, InlineKeyboardMarkup]:
    rows = [
        [Btn("🗂 Разобрать входящие", callback_data="m:razbor")],
        [Btn("📁 Проекты", callback_data="m:projects"), Btn("➕ Добавить проект", callback_data="m:add")],
        [Btn("🔎 Спросить по заметкам", callback_data="m:ask")],
    ]
    if is_owner(uid):
        rows.append([Btn("👥 Участники", callback_data="m:members"), Btn("🎟 Пригласить", callback_data="m:invite")])
    return MENU.format(owner_commands=OWNER_COMMANDS if is_owner(uid) else ""), InlineKeyboardMarkup(rows)
# Ответ на это сообщение бота — названия проектов, а не заметка
ADD_PROMPT = "Напишите названия проектов ответом на это сообщение: через запятую или каждый с новой строки."


# ---------- 🎟 приглашения и участники ----------
# Владелица создаёт приглашение (/invite) → бот выдаёт одноразовый код и ссылку. Новый человек открывает ссылку
# или присылает код → получает доступ. Чужие без кода видят только просьбу ввести код, владелице ничего не приходит.
# callback_data: mr:<tg> (спросить про удаление участника)  |  mk:<tg> (удалить)  |  mi:<page> (отозвать код)  |  ml

INVITE_PROMPT = "🎟 Для кого приглашение? Напишите имя или подпись ответом на это сообщение, например «Аня, дизайнер»."
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без похожих O/0 и I/1
CODE_RE = re.compile(r"^[A-Z0-9]{4}-?[A-Z0-9]{4}$")
# Защита от подбора: не больше 5 неверных кодов в час с одного аккаунта
_bad_codes: dict[int, list[datetime]] = {}


def _new_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def _normalize_code(text: str) -> str | None:
    code = text.strip().upper().replace(" ", "")
    if not CODE_RE.match(code):
        return None
    return code if "-" in code else f"{code[:4]}-{code[4:]}"


async def stranger(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Сообщение от человека без доступа: если похоже на код — пробуем его, иначе просим код."""
    if not update.message:
        return
    code = _normalize_code(update.message.text or "")
    if code:
        await _try_code(update, ctx, code)
        return
    await update.message.reply_text(
        "Это закрытый бот для заметок. Если у вас есть код приглашения, пришлите его сюда (вида ABCD-2345)."
    )


async def _try_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE, raw: str) -> None:
    user = update.effective_user
    now = datetime.now(timezone.utc)
    recent = [t for t in _bad_codes.get(user.id, []) if now - t < timedelta(hours=1)]
    if len(recent) >= 5:
        await update.message.reply_text("Слишком много неверных кодов. Попробуйте через час.")
        return
    code = _normalize_code(raw)
    person = await notion.redeem(code, user.id, user.full_name, user.username) if code else None
    if not person:
        _bad_codes[user.id] = recent + [now]
        await update.message.reply_text("Код не подошёл: он неверный, уже использован или просрочен. Попросите новый.")
        return
    member.add_user_ids(user.id)
    text, kb = menu(user.id)
    await update.message.reply_text("✅ Добро пожаловать!\n\n" + text, reply_markup=kb, parse_mode=ParseMode.HTML)
    who = f"@{user.username}" if user.username else user.full_name
    await ctx.bot.send_message(c.OWNER_ID, f"🎉 {person['name']} ({who}) вошёл(ла) по приглашению. Участники: /members")


async def _ask_invite_label(message: Message) -> None:
    await message.reply_text(INVITE_PROMPT, reply_markup=ForceReply(input_field_placeholder="Аня, дизайнер"))


async def _create_invite(message: Message, ctx: ContextTypes.DEFAULT_TYPE, label: str) -> None:
    label = label.strip()[:100] or "Без подписи"
    code = _new_code()
    until = await notion.create_invite(label, code, c.INVITE_DAYS)
    until_text = datetime.fromisoformat(until).strftime("%d.%m")
    link = f"https://t.me/{ctx.bot.username}?start={code}"
    await message.reply_text(
        f"🎟 Приглашение для «{escape(label)}»\n"
        f"Код: <code>{code}</code> · действует до {until_text}, одноразовый\n\n"
        "Перешлите человеку сообщение ниже 👇",
        parse_mode=ParseMode.HTML,
    )
    await message.reply_text(
        f"Привет! Приглашаю тебя в мой бот для заметок.\n\nОткрой ссылку: {link}\n"
        f"или напиши боту @{ctx.bot.username} код {code}\n\nКод одноразовый, действует до {until_text}.",
        disable_web_page_preview=True,
    )


async def invite_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    label = re.sub(r"^/invite(@\w+)?", "", update.message.text, count=1)
    if label.strip():
        await _create_invite(update.message, ctx, label)
    else:
        await _ask_invite_label(update.message)


async def _members_view() -> tuple[str, InlineKeyboardMarkup | None]:
    people, pending = await notion.members(), await notion.invites()
    lines, rows = [], []
    if people:
        lines.append("👥 Участники:")
        for m in people:
            lines.append(f"• {m['name']}" + (f" ({m['username']})" if m["username"] else ""))
            rows.append([Btn(f"🗑 Убрать {m['name']}", callback_data=f"mr:{m['tg']}")])
    if pending:
        lines.append("\n🎟 Неиспользованные приглашения:")
        for inv in pending:
            until = inv["until"][:10] if inv["until"] else ""
            lines.append(f"• {inv['name']}: {inv['code']} (до {until})")
            rows.append([Btn(f"❌ Отозвать код для {inv['name']}", callback_data=f"mi:{inv['page'].replace('-', '')}")])
    if not lines:
        lines.append("Пока только вы.")
    lines.append("\nПригласить: /invite")
    return "\n".join(lines), InlineKeyboardMarkup(rows) if rows else None


async def members_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = await _members_view()
    await update.message.reply_text(text, reply_markup=kb)


async def on_member_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer()
        return
    action, _, arg = q.data.partition(":")
    try:
        if action == "mr":
            person = next((m for m in await notion.members() if str(m["tg"]) == arg), None)
            await q.answer()
            if not person:
                await q.edit_message_text("Этого участника уже нет.")
                return
            await q.edit_message_text(
                f"Убрать {person['name']} из бота?\nДоступ закроется, его заметки останутся в Notion.",
                reply_markup=InlineKeyboardMarkup(
                    [[Btn("Да, убрать", callback_data=f"mk:{arg}"), Btn("Отмена", callback_data="ml")]]
                ),
            )
            return
        if action == "mk":
            person = next((m for m in await notion.members() if str(m["tg"]) == arg), None)
            if person:
                await notion.remove_person(person["page"])
            member.remove_user_ids(int(arg))
            _interviews.pop(int(arg), None)
            await q.answer("Участник убран")
            try:
                await ctx.bot.send_message(int(arg), "Ваш доступ к боту закрыт владелицей.")
            except Exception:
                log.info("could not notify removed member")
        elif action == "mi":
            await notion.remove_person(arg)
            await q.answer("Код отозван")
        else:
            await q.answer()
        text, kb = await _members_view()
    except Exception as e:
        log.exception("member button failed")
        await _fail(q, e)
        return
    await q.edit_message_text(text, reply_markup=kb)


# ---------- приём заметок ----------


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not c.OWNER_ID:
        await update.message.reply_text(f"Ваш Telegram ID: {uid}\nВпишите его в переменную OWNER_ID и перезапустите бота.")
        return
    if not is_member(uid):
        # Ссылка-приглашение t.me/<бот>?start=<код> приходит сюда с кодом в аргументе
        if ctx.args:
            await _try_code(update, ctx, ctx.args[0])
        else:
            await stranger(update, ctx)
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
        elif action == "invite" and is_owner(uid):
            await _ask_invite_label(q.message)
        elif action == "ask":
            text, kb = await _ask_scope_view()
            await q.message.reply_text(text, reply_markup=kb)
    except Exception as e:
        log.exception("menu failed")
        await q.message.reply_text(f"❌ Ошибка: {e}"[:4000])


async def _ask_project_names(message) -> None:
    await message.reply_text(ADD_PROMPT, reply_markup=ForceReply(input_field_placeholder="Сайт, Логотипы, Фоны"))


async def note_allowed(update: Update) -> bool:
    """Дневной лимит заметок участника. Проверяется до расшифровки и чтения файлов, чтобы не тратить на них кредиты."""
    uid = update.effective_user.id
    if is_owner(uid) or await notion.created_today(uid) < c.MEMBER_DAILY_LIMIT:
        return True
    await update.message.reply_text(f"⛔ Лимит {c.MEMBER_DAILY_LIMIT} заметок в сутки исчерпан, продолжим завтра.")
    return False


async def _save(
    update: Update,
    source: str,
    *,
    text: str = "",
    images: list[bytes] | None = None,
    attachment: tuple[bytes, str, str] | None = None,
    full_text: str | None = None,
    status: Message | None = None,
) -> None:
    """attachment — (данные, имя, MIME) оригинала документа; full_text — полный текст документа для деталей,
    когда в ИИ ушёл только фрагмент."""
    images = images or []
    if status is None:
        status = await update.message.reply_text("⏳ Обрабатываю…" if len(images) < 2 else f"⏳ Обрабатываю альбом из {len(images)} фото…")
    note = ""
    user = update.effective_user
    try:
        if not is_owner(user.id) and await notion.created_today(user.id) >= c.MEMBER_DAILY_LIMIT:
            await status.edit_text(f"⛔ Лимит {c.MEMBER_DAILY_LIMIT} заметок в сутки исчерпан, продолжим завтра.")
            return
        try:
            idea = await ai.structure(text=text, images=images)
            if full_text is not None:
                idea.details = full_text
        except Exception as e:
            if images:
                raise
            # ИИ недоступен: текст всё равно не теряем, кладём как есть
            log.exception("AI failed, saving raw text")
            idea = ai.Idea(title=(text.splitlines() or ["Заметка"])[0][:60], summary=text, details=full_text or "")
            note = f"\n\n⚠️ Сохранено без обработки ИИ: {escape(str(e)[:500])}"
        originals = []
        for i, image in enumerate(images, 1):
            try:
                originals.append(notion.image_block(await notion.upload_image(image, f"photo-{i}.jpg")))
            except Exception as e:
                # Заметку всё равно сохраняем, просто без оригинала
                log.exception("image upload failed")
                note = f"\n\n⚠️ Фото {i} не прикрепилось к Notion: {escape(str(e)[:300])}"
        if attachment:
            data, filename, mime = attachment
            try:
                kind = "pdf" if mime == "application/pdf" else "file"
                originals.append(notion.file_block(await notion.upload_file(data, filename, mime), kind))
            except Exception as e:
                log.exception("file upload failed")
                note = f"\n\n⚠️ Файл не прикрепился к Notion (на бесплатном Notion лимит 5 МБ): {escape(str(e)[:300])}"
        blocks = to_blocks(idea.summary) + originals
        page_id, url = await notion.create_idea(
            idea.title, source, idea.tags, blocks, to_blocks(idea.details), user.id, user.full_name
        )
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
    if not is_member(q.from_user.id):
        await q.answer()
        return
    action, page_id, *rest = q.data.split(":")
    try:
        await own_page(q.from_user.id, page_id)
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
        await _fail(q, e)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    reply = update.message.reply_to_message
    if reply and reply.from_user and reply.from_user.id == ctx.bot.id and reply.text == INVITE_PROMPT:
        if is_owner(update.effective_user.id):
            await _create_invite(update.message, ctx, update.message.text)
        return
    if reply and reply.from_user and reply.from_user.id == ctx.bot.id and reply.text == ADD_PROMPT:
        await update.message.reply_text(await _add_projects_text(update.message.text))
        return
    if (not reply or (reply.text or "").startswith(EXPAND_MARK)) and await interview_answer(update.message):
        return
    if reply and reply.from_user and reply.from_user.id == ctx.bot.id and (scope := _scope_from(reply.text or "")):
        # Вопрос по заметкам проекта или периода, либо уточнение к ответу
        history = reply.text if reply.text.startswith(NOTES_ANSWER_MARK) else ""
        await _ask_notes(update.message, scope, update.message.text, history)
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


# Сколько текста документа отдаём ИИ для краткой сути и сколько сохраняем в детали заметки
DOC_AI_CHARS = 30000
DOC_SAVE_CHARS = 150000


async def on_document(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    doc = msg.document
    if not await note_allowed(update):
        return
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await msg.reply_text("❌ Файл больше 20 МБ: Telegram не даёт ботам скачивать такие.")
        return
    status = await msg.reply_text("⏳ Читаю документ…")
    try:
        data = bytes(await (await doc.get_file()).download_as_bytearray())
        extracted = documents.extract(data, doc.file_name or "file", doc.mime_type)
    except documents.Unsupported as e:
        await status.edit_text(f"🤷 {e}")
        return
    except Exception as e:
        log.exception("document read failed")
        await status.edit_text(f"❌ Не получилось прочитать документ: {e}"[:4000])
        return
    filename = doc.file_name or "file"
    attachment = (data, filename, doc.mime_type or "application/octet-stream")
    caption = f"Подпись: {msg.caption}\n" if msg.caption else ""
    if extracted.scans:
        # Скан без текстового слоя: страницы идут в ИИ как фото
        await status.edit_text(f"⏳ Это скан, распознаю {len(extracted.scans)} стр…")
        await _save(update, "Документ", text=caption, images=extracted.scans, attachment=attachment, status=status)
        return
    if not extracted.text.strip():
        await status.edit_text("🤷 В документе не нашлось текста.")
        return
    full = extracted.text
    if len(full) > DOC_SAVE_CHARS:
        full = full[:DOC_SAVE_CHARS] + "\n\n… (дальше — в прикреплённом файле)"
    excerpt = extracted.text[:DOC_AI_CHARS]
    if len(extracted.text) > DOC_AI_CHARS:
        excerpt += "\n\n(Документ длинный, это его начало.)"
    size = f"{len(extracted.text):,}".replace(",", " ")
    await status.edit_text(f"⏳ Обрабатываю: {extracted.kind}, {size} символов…")
    text = f"{caption}{extracted.kind} «{filename}»:\n\n{excerpt}"
    await _save(update, "Документ", text=text, attachment=attachment, full_text=full, status=status)


async def on_voice(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not await note_allowed(update):
        return
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


async def _review_view(uid: int, k: int, show_projects: bool = False) -> tuple[str, InlineKeyboardMarkup | None]:
    items = await notion.review_items(uid)
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


# ---------- лимит запросов к ИИ для участников ----------
# Считается в памяти: после перезапуска бота счётчик обнуляется. Для защиты кредитов этого хватает.
_ai_used: dict[tuple[int, str], int] = {}


async def ai_allowed(message: Message) -> bool:
    """В личном чате id чата — это id человека. Владелица без лимита."""
    uid = message.chat_id
    if is_owner(uid):
        return True
    key = (uid, datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    if _ai_used.get(key, 0) >= c.MEMBER_AI_LIMIT:
        await message.reply_text(f"⛔ Лимит {c.MEMBER_AI_LIMIT} запросов к ИИ на сегодня исчерпан, продолжим завтра.")
        return False
    _ai_used[key] = _ai_used.get(key, 0) + 1
    return True


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
    if not await ai_allowed(message):
        return
    status = await message.reply_text("🤖 Думаю…")
    try:
        item = await own_page(message.chat_id, page_id)
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
    if not is_member(q.from_user.id):
        return
    action, page_id, *rest = q.data.split(":")
    try:
        await own_page(q.from_user.id, page_id)
    except Exception as e:
        await q.message.reply_text(f"❌ {e}")
        return
    if action == "a":
        await start_ai(q.message, page_id)
    else:
        label, question = PRESETS[int(rest[0])]
        await _ask_ai(q.message, page_id, question, label=label)


# ---------- 🔎 Вопрос по многим заметкам ----------
# callback_data: q:p:<номер проекта>  |  q:d:<дней>
# Область вопроса записана в первой строке сообщения бота, поэтому ответ и уточнения переживают перезапуск.

NOTES_PROMPT_MARK = "🔎 Вопрос по "
NOTES_ANSWER_MARK = "🔎 «"
PERIODS = {7: "заметкам за неделю", 30: "заметкам за месяц"}


def _scope_label(scope: tuple[str, str | int]) -> str:
    kind, value = scope
    return f"проекту «{value}»" if kind == "p" else PERIODS.get(int(value), f"заметкам за {value} дн.")


def _scope_from(text: str) -> tuple[str, str | int] | None:
    """Достаёт область из первой строки: «🔎 Вопрос по проекту «Сайт»…» или «🔎 «вопрос» · по проекту «Сайт»…»."""
    first = text.split("\n", 1)[0]
    if not first.startswith(("🔎",)):
        return None
    if m := re.search(r"проекту «(.+?)»", first):
        return ("p", m.group(1))
    for days, label in PERIODS.items():
        if label in first:
            return ("d", days)
    return None


async def _ask_scope_view() -> tuple[str, InlineKeyboardMarkup]:
    projects = await notion.projects()
    buttons = [Btn(f"📁 {p}", callback_data=f"q:p:{i}") for i, p in enumerate(projects)]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([Btn("🗂 Всё за неделю", callback_data="q:d:7"), Btn("🗂 Всё за месяц", callback_data="q:d:30")])
    return "🔎 По каким заметкам спросить ИИ?", InlineKeyboardMarkup(rows)


async def ask_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = await _ask_scope_view()
    await update.message.reply_text(text, reply_markup=kb)


async def on_scope_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not is_member(q.from_user.id):
        return
    _, kind, value = q.data.split(":")
    if kind == "p":
        projects = await notion.projects()
        if int(value) >= len(projects):
            await q.message.reply_text("Список проектов изменился, откройте /ask ещё раз.")
            return
        scope = ("p", projects[int(value)])
    else:
        scope = ("d", int(value))
    await q.message.reply_text(
        f"{NOTES_PROMPT_MARK}{_scope_label(scope)}: напишите вопрос ответом на это сообщение.\n"
        "Например: «что мы решили за это время?», «собери все идеи для логотипа», «какие задачи без срока?»",
        reply_markup=ForceReply(input_field_placeholder="ваш вопрос"),
    )


def _notes_word(n: int) -> str:
    """1 заметка, 3 заметки, 5 заметок."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} заметка"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} заметки"
    return f"{n} заметок"


def _note_text(item: dict, note: dict) -> str:
    head = f"### «{item['title']}» ({item['created']}" + (f", {item['type']}" if item.get("type") else "") + ")"
    return "\n".join(p for p in (head, note["summary"], note["details"]) if p)


async def _ask_notes(message: Message, scope: tuple[str, str | int], question: str, history: str = "") -> None:
    kind, value = scope
    label = _scope_label(scope)
    if not await ai_allowed(message):
        return
    status = await message.reply_text(f"📚 Собираю заметки по {label}…")
    try:
        items = await notion.notes_in_scope(
            message.chat_id, project=value if kind == "p" else None, days=int(value) if kind == "d" else None
        )
        if not items:
            await status.edit_text(f"🤷 По {label} заметок нет.")
            return
        await status.edit_text(f"📚 Читаю: {_notes_word(len(items))}…")
        limiter = asyncio.Semaphore(3)  # Notion разрешает около 3 запросов в секунду

        async def read(item: dict) -> str:
            async with limiter:
                return _note_text(item, await notion.read_note(item["id"]))

        notes = await asyncio.gather(*(read(i) for i in items))
        await status.edit_text("🤖 Думаю…")
        answer, truncated = await ai.ask_notes(question, list(notes), label, history)
    except Exception as e:
        log.exception("ask notes failed")
        await status.edit_text(f"❌ ИИ не ответил: {e}"[:4000])
        return
    header = f"{NOTES_ANSWER_MARK}{escape(question[:100])}» · по {escape(label)} ({_notes_word(len(items))})\n\n"
    footer = "\n\n↩️ Ответьте на это сообщение, чтобы уточнить."
    if truncated:
        footer = "\n\n⚠️ Заметок очень много, ИИ прочитал не все: сузьте вопрос до проекта или периода." + footer
    chunks = _chunks(tg_html(answer))
    for i, chunk in enumerate(chunks):
        text = header + chunk + (footer if i == len(chunks) - 1 else "")
        if i == 0:
            await status.edit_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        else:
            await message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# ---------- ✨ Раскрытие заметки под её тип: вопрос за вопросом ----------
# После выбора типа бот задаёт вопросы по одному. Ответ — просто следующее сообщение (или ответ на вопрос).
# Когда всё раскрыто или нажато «Хватит», ИИ собирает описание, его можно сохранить в заметку.
# callback_data: x:<page_id>:<номер> (начать из разбора)  |  xi:skip / xi:done / xi:stop / xi:more
#                xs:<page_id> (сохранить описание)  |  xc:<page_id> (не сохранять)

EXPAND_MARK = "✨ "
# Идущие интервью по id чата: заметка, тип, вопросы-ответы, оригиналы фото
_interviews: dict[int, dict] = {}
# Собранные описания по id сообщения: (тип, Markdown). Если бот перезапустился, берём текст из сообщения.
_drafts: dict[int, tuple[str, str]] = {}


def _draft_from_message(message: Message) -> tuple[str, str]:
    """(тип, описание) из сообщения бота: первая строка «✨ <тип> · <заметка>», дальше описание."""
    if message.message_id in _drafts:
        return _drafts[message.message_id]
    first, _, rest = (message.text or "").partition("\n")
    return first.removeprefix(EXPAND_MARK).split(" · ")[0], rest.strip()


async def _send_html(message: Message, text: str, status: Message | None = None, **kwargs) -> Message:
    """Отправляет или правит сообщение с HTML. Если Telegram не принял разметку — отправляет простым текстом,
    чтобы ответ не потерялся молча."""
    try:
        if status:
            return await status.edit_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, **kwargs)
        return await message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, **kwargs)
    except BadRequest as e:
        if "parse" not in str(e).lower() and "entit" not in str(e).lower():
            raise
        plain = re.sub(r"<[^>]+>", "", text).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        if status:
            return await status.edit_text(plain, disable_web_page_preview=True, **kwargs)
        return await message.reply_text(plain, disable_web_page_preview=True, **kwargs)


async def start_interview(message: Message, page_id: str) -> None:
    if not await ai_allowed(message):
        return
    status = await message.reply_text("✨ Читаю заметку…")
    try:
        item = await own_page(message.chat_id, page_id)
        note = await notion.read_note(page_id)
        images = []
        for url in note["images"][:4]:
            try:
                images.append(await notion.download(url))
            except Exception:
                log.exception("image download failed")
        titles = await notion.project_titles(item["project"], page_id, message.chat_id) if item["project"] else []
    except Exception as e:
        log.exception("interview start failed")
        await status.edit_text(f"❌ Не получилось прочитать заметку: {e}"[:4000])
        return
    _interviews[message.chat_id] = {
        "page": page_id,
        "item": item,
        "type": item["type"] or "Заметка",
        "note": "\n\n".join(p for p in (f"# {item['title']}", note["summary"], note["details"]) if p),
        "images": images,
        "titles": titles,
        "qa": [],
        "question": None,
        "draft": "",
    }
    await _next_step(message, status)


def _interview_header(state: dict) -> str:
    item = state["item"]
    return f'{EXPAND_MARK}{escape(state["type"])} · <a href="{item["url"]}">{escape(item["title"])}</a>\n\n'


async def _next_step(message: Message, status: Message | None = None) -> None:
    state = _interviews.get(message.chat_id)
    if not state:
        return
    if status is None:
        status = await message.reply_text("✨ Думаю над следующим вопросом…")
    else:
        await status.edit_text("✨ Думаю над вопросом…")
    try:
        question = await ai.next_question(
            state["type"], state["note"], state["item"]["project"], state["titles"], state["images"], state["qa"]
        )
    except Exception as e:
        log.exception("next question failed")
        await status.edit_text(
            f"❌ ИИ не ответил: {e}"[:3500],
            reply_markup=InlineKeyboardMarkup(
                [[Btn("🔄 Ещё раз", callback_data="xi:retry"), Btn("✅ Собрать по тому, что есть", callback_data="xi:done")]]
            ),
        )
        return
    if question is None:
        await _compose(message, status)
        return
    state["question"] = question
    n = len(state["qa"]) + 1
    await _send_html(
        message,
        _interview_header(state)
        + f"<b>Вопрос {n}:</b> {escape(question)}\n\n✍️ Просто напишите ответ следующим сообщением.",
        status=status,
        reply_markup=InlineKeyboardMarkup(
            [
                [Btn("⏭ Пропустить", callback_data="xi:skip"), Btn("✅ Хватит, собрать", callback_data="xi:done")],
                [Btn("✖️ Стоп", callback_data="xi:stop")],
            ]
        ),
    )


async def _compose(message: Message, status: Message | None = None) -> None:
    state = _interviews.get(message.chat_id)
    if not state:
        return
    state["question"] = None
    if status is None:
        status = await message.reply_text("✨ Собираю описание…")
    else:
        await status.edit_text("✨ Собираю описание…")
    try:
        description = await ai.compose(
            state["type"], state["note"], state["item"]["project"], state["titles"], state["images"], state["qa"], state["draft"]
        )
    except Exception as e:
        log.exception("compose failed")
        await status.edit_text(
            f"❌ ИИ не собрал описание: {e}"[:3500],
            reply_markup=InlineKeyboardMarkup([[Btn("🔄 Ещё раз", callback_data="xi:done")]]),
        )
        return
    state["draft"] = description
    body = tg_html(description)
    header = _interview_header(state)
    tail = "\n\n✍️ Хотите что-то поправить — напишите следующим сообщением."
    if len(header) + len(body) + len(tail) > 4000:
        body = body[: 4000 - len(header) - len(tail) - 60].rsplit("\n", 1)[0] + "\n… (полностью — после сохранения в Notion)"
        body = re.sub(r"<pre>(?![\s\S]*</pre>)[\s\S]*$", "", body)  # не оставляем незакрытый <pre>
    sent = await _send_html(
        message,
        header + body + tail,
        status=status,
        reply_markup=InlineKeyboardMarkup(
            [
                [Btn("✅ Сохранить в заметку", callback_data=f"xs:{state['page']}")],
                [Btn("❓ Ещё вопрос", callback_data="xi:more"), Btn("✖️ Не надо", callback_data=f"xc:{state['page']}")],
            ]
        ),
    )
    _drafts[getattr(sent, "message_id", status.message_id)] = (state["type"], description)


async def interview_answer(message: Message) -> bool:
    """Если идёт интервью, сообщение — это ответ на вопрос или правка собранного описания. True, если обработали."""
    state = _interviews.get(message.chat_id)
    if not state:
        return False
    if state["question"]:
        state["qa"].append((state["question"], message.text))
        state["question"] = None
        await _next_step(message)
    else:
        state["qa"].append(("Правка к описанию", message.text))
        await _compose(message)
    return True


async def on_interview_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not is_member(q.from_user.id):
        return
    action = q.data.removeprefix("xi:")
    state = _interviews.get(q.message.chat_id)
    if not state:
        await q.edit_message_reply_markup(None)
        await q.message.reply_text("Этот разговор уже закончился. Начните заново из разбора: тип → «✨ Раскрыть».")
        return
    await q.edit_message_reply_markup(None)
    if action == "stop":
        _interviews.pop(q.message.chat_id, None)
        await q.message.reply_text("Ок, остановились. Заметка осталась как была.")
    elif action == "skip":
        state["qa"].append((state["question"] or "вопрос", "(пропущено)"))
        state["question"] = None
        await _next_step(q.message)
    elif action in ("more", "retry"):
        await _next_step(q.message)
    else:  # done
        await _compose(q.message)


async def on_expand_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_member(q.from_user.id):
        await q.answer()
        return
    action, page_id = q.data.split(":")[:2]
    if action == "xc":
        await q.answer()
        _drafts.pop(q.message.message_id, None)
        _interviews.pop(q.message.chat_id, None)
        await q.edit_message_reply_markup(None)
        return
    try:
        await own_page(q.from_user.id, page_id)
        type_name, draft = _draft_from_message(q.message)
        await notion.add_expansion(page_id, type_name, to_blocks(draft))
    except Exception as e:
        log.exception("save expansion failed")
        await _fail(q, e)
        return
    await q.answer("Сохранено ✓")
    _drafts.pop(q.message.message_id, None)
    _interviews.pop(q.message.chat_id, None)
    await q.edit_message_reply_markup(InlineKeyboardMarkup([[Btn("✅ Сохранено в заметке", callback_data="noop")]]))


# ---------- проекты ----------
# callback_data: pl  |  pa:<номер> (спросить про удаление)  |  pk:<номер> (удалить)


async def _projects_view(uid: int) -> tuple[str, InlineKeyboardMarkup | None]:
    """Проекты общие: добавлять могут все, удалять — только владелица."""
    names = await notion.projects()
    hint = "Добавить: /addproject Название (можно несколько, каждый с новой строки)."
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
        text, kb = await _projects_view(q.from_user.id)
    except Exception as e:
        log.exception("project button failed")
        await _fail(q, e)
        return
    await q.edit_message_text(text, reply_markup=kb)


async def razbor(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = await _review_view(update.effective_user.id, 0)
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def on_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_member(q.from_user.id):
        await q.answer()
        return
    action, *args = q.data.split(":")
    show_projects = False
    try:
        if action != "r":
            await own_page(q.from_user.id, args[0])
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
                    "Раскрыть заметку под этот тип? ИИ задаст несколько вопросов по одному и соберёт подробное описание.",
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=InlineKeyboardMarkup(
                        [[Btn("✨ Раскрыть", callback_data=f"x:{page_id}:{k}"), Btn("⏭ Следующая", callback_data=f"r:{k}")]]
                    ),
                )
                return
        elif action == "x":
            # Карточка сразу переходит к следующей заметке, а черновик описания приходит отдельными сообщениями ниже
            page_id, k = args[0], int(args[1])
            # Карточка разбора ждёт, пока идёт разговор: после него вернётесь к ней кнопкой
            await q.answer()
            await q.edit_message_reply_markup(InlineKeyboardMarkup([[Btn("⏭ К следующей заметке", callback_data=f"r:{k}")]]))
            await start_interview(q.message, page_id)
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
        text, kb = await _review_view(q.from_user.id, k, show_projects)
    except Exception as e:
        log.exception("button failed")
        await _fail(q, e)
        return
    await q.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# ---------- запуск ----------

COMMANDS = [
    ("start", "Меню всех команд"),
    ("razbor", "Разобрать входящие"),
    ("projects", "Список проектов"),
    ("addproject", "Добавить проект"),
    ("ask", "Спросить ИИ по заметкам"),
]
OWNER_EXTRA = [
    ("invite", "Пригласить участника"),
    ("members", "Участники и приглашения"),
]


async def set_commands(app: Application) -> None:
    """Меню «/» в Telegram (у владелицы в нём больше команд) и список участников из Notion."""
    await app.bot.set_my_commands(COMMANDS)
    if c.OWNER_ID:
        await app.bot.set_my_commands(COMMANDS + OWNER_EXTRA, scope=BotCommandScopeChat(c.OWNER_ID))
    try:
        member.add_user_ids([m["tg"] for m in await notion.members()])
    except Exception:
        # Без списка участников бот всё равно работает для владелицы
        log.exception("could not load members")


async def serve(app: Application) -> None:
    """Свой веб-сервер вместо run_webhook: кроме Telegram он принимает вечерний пинг от cron-job.org."""
    webhook_secret = c.secret("webhook")
    last_force = [-1e9]

    async def telegram(request: web.Request) -> web.Response:
        if not secrets.compare_digest(request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), webhook_secret):
            return web.Response(status=403)
        await app.update_queue.put(Update.de_json(await request.json(), app.bot))
        return web.Response()

    async def remind_hook(request: web.Request) -> web.Response:
        if not secrets.compare_digest(request.match_info["secret"], c.secret("remind")):
            return web.Response(status=404)
        force = "force" in request.query
        if force:
            # Ручная проверка — не чаще раза в минуту, чтобы утёкшей ссылкой нельзя было заспамить участников
            now = asyncio.get_running_loop().time()
            if now - last_force[0] < 60:
                return web.Response(status=429, text="Не чаще раза в минуту")
            last_force[0] = now
        try:
            result = await remind.send(app.bot, sorted(member.user_ids), force=force)
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


async def _fail(q, error: Exception) -> None:
    """Показывает ошибку кнопки: всплывающим окном, а если на нажатие уже ответили — сообщением в чат."""
    try:
        await q.answer(f"Ошибка: {error}"[:200], show_alert=True)
    except Exception:
        await q.message.reply_text(f"❌ Ошибка: {error}"[:4000])


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Последняя линия: любая необработанная ошибка приходит владелице в чат, а не теряется молча."""
    log.error("unhandled error", exc_info=ctx.error)
    if not c.OWNER_ID:
        return
    where = ""
    if isinstance(update, Update):
        if update.callback_query:
            where = f" (кнопка {update.callback_query.data.split(':')[0]})"
        elif update.message:
            where = " (сообщение)"
    try:
        user = update.effective_user if isinstance(update, Update) else None
        who = f" у {user.full_name}" if user and not is_owner(user.id) else ""
        await ctx.bot.send_message(c.OWNER_ID, f"⚠️ Что-то пошло не так{who}{where}: {type(ctx.error).__name__}: {ctx.error}"[:4000])
        if user and not is_owner(user.id) and is_member(user.id):
            await ctx.bot.send_message(user.id, "⚠️ Что-то пошло не так. Владелица уже получила сообщение об ошибке.")
    except Exception:
        log.exception("could not report error")


def build_app(webhook: bool) -> Application:
    builder = Application.builder().token(c.TELEGRAM_TOKEN).concurrent_updates(True).post_init(set_commands)
    if webhook:
        builder = builder.updater(None)  # обновления приходят в наш сервер, встроенный не нужен
    app = builder.build()
    app.add_handler(TypeHandler(Update, private_only), group=-1)
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^m:"))
    # Люди без доступа: что бы ни прислали, бот просит код приглашения
    app.add_handler(MessageHandler(~member, stranger))
    app.add_handler(CommandHandler("razbor", razbor, filters=member))
    app.add_handler(CommandHandler("projects", projects_cmd, filters=member))
    app.add_handler(CommandHandler("ask", ask_cmd, filters=member))
    app.add_handler(CommandHandler("addproject", addproject, filters=member))
    app.add_handler(CommandHandler("invite", invite_cmd, filters=owner))
    app.add_handler(CommandHandler("members", members_cmd, filters=owner))
    app.add_handler(CallbackQueryHandler(on_member_button, pattern=r"^m[rkil]"))
    app.add_handler(CallbackQueryHandler(on_scope_button, pattern=r"^q:"))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(r|t|x|pj|p|d|v):"))
    app.add_handler(CallbackQueryHandler(on_expand_button, pattern=r"^x[sc]:"))
    app.add_handler(CallbackQueryHandler(on_interview_button, pattern=r"^xi:"))
    app.add_handler(CallbackQueryHandler(lambda u, _: u.callback_query.answer(), pattern=r"^noop$"))
    app.add_handler(CallbackQueryHandler(on_panel, pattern=r"^n[px]?:"))
    app.add_handler(CallbackQueryHandler(on_ai_button, pattern=r"^aq?:"))
    app.add_handler(CallbackQueryHandler(on_project_button, pattern=r"^p[akl]"))
    app.add_handler(MessageHandler(member & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(member & (filters.PHOTO | filters.Document.IMAGE), on_photo))
    app.add_handler(MessageHandler(member & (filters.VOICE | filters.AUDIO), on_voice))
    app.add_handler(MessageHandler(member & filters.Document.ALL & ~filters.Document.IMAGE, on_document))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    c.require("TELEGRAM_TOKEN", "HF_TOKEN", "NOTION_TOKEN")
    app = build_app(webhook=bool(c.WEBHOOK_BASE))
    if c.WEBHOOK_BASE:
        asyncio.run(serve(app))
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
