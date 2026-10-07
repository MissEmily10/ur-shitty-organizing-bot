import asyncio
import io
import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from html import escape

from telegram import InlineKeyboardButton as Btn
from aiohttp import web
from telegram import BotCommandScopeChat, ForceReply, InlineKeyboardMarkup, InputMediaPhoto, Message, Update
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

from . import ai, banners, documents, notion, pdf, schedule, scheduler, styles, team, weekly, whenparse
from . import config as c
from .markdown import chunks as _chunks, tg_html, to_blocks

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
    "/projects — проекты: карточки, описание, закрыть\n"
    "/addproject — добавить проект (бот спросит название)\n"
    "/feed — лента заметок проекта; в командном — от всех участников\n"
    "/types, /addtype — типы записей\n"
    "/areas, /addarea — сферы деятельности (фотограф, дизайнер…)\n"
    "/ask — спросить ИИ по проекту или по всем своим заметкам за период\n"
    "/schedule — расписание и календарь в PDF\n"
    "/report — недельный отчёт: настроение и дела\n"
    "/settings — часовой пояс, время разбора и дневных чек-инов\n"
    "/style — как бот с тобой разговаривает: стиль или свой персонаж\n"
    "{owner_commands}"
    "/start — это меню\n\n"
    "Или жми кнопку 👇"
)
OWNER_COMMANDS = (
    "/invite — пригласить участника\n/members — участники и приглашения\n"
    "/teamproject — командный проект: заметки вместе с участниками\n"
    "/banners — картинки разделов меню\n"
)


MENU_SHORT = (
    "Я складываю твои мысли в Notion.\n"
    "✍️ Пиши, присылай фото, голосовые и документы — всё станет заметкой.\n\n"
    "Выбери раздел 👇"
)


def menu(uid: int, short: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    """short — подпись к картинке-панели (у подписи лимит 1024 символа), полный список команд — кнопкой."""
    rows = [
        [Btn("🗂 Разобрать входящие", callback_data="m:razbor")],
        [Btn("📁 Проекты", callback_data="m:projects"), Btn("➕ Добавить проект", callback_data="m:add")],
        [Btn("🔎 Спросить по заметкам", callback_data="m:ask"), Btn("🗓 Расписание", callback_data="m:schedule")],
        [Btn("⚙️ Настройки", callback_data="m:settings")],
    ]
    if is_owner(uid):
        rows.append([Btn("👥 Участники", callback_data="m:members"), Btn("🎟 Пригласить", callback_data="m:invite")])
    if short:
        rows.append([Btn("📜 Все команды", callback_data="m:help")])
        return MENU_SHORT, InlineKeyboardMarkup(rows)
    return MENU.format(owner_commands=OWNER_COMMANDS if is_owner(uid) else ""), InlineKeyboardMarkup(rows)

# ---------- 🖼 панель с картинками ----------
# Меню — одно «живое» сообщение: картинка-баннер раздела, подпись и кнопки. Разделы открываются в нём же,
# «↩️ В меню» возвращает главное. Картинки — banners.py; выключается в /settings.

CAPTION_LIMIT = 1000  # у подписи к картинке лимит 1024 символа


def _fits_caption(text: str) -> bool:
    return len(re.sub(r"<[^>]+>", "", text)) <= CAPTION_LIMIT


async def _edit(q, text: str, reply_markup=None, **kwargs):
    """Правит сообщение с кнопкой. У панели-картинки правится подпись; не влезший в подпись текст уходит новым
    сообщением, а у картинки убираются кнопки."""
    if not (q.message and q.message.photo):
        return await q.edit_message_text(text, reply_markup=reply_markup, **kwargs)
    kwargs.pop("disable_web_page_preview", None)
    if _fits_caption(text):
        return await q.edit_message_caption(caption=text, reply_markup=reply_markup, **kwargs)
    await q.edit_message_reply_markup(None)
    return await q.message.reply_text(text, reply_markup=reply_markup, disable_web_page_preview=True, **kwargs)


async def _banners_on(uid: int) -> bool:
    return bool((await scheduler.get_settings(uid)).get("banners"))


def _with_home(kb: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup:
    rows = [list(r) for r in kb.inline_keyboard] if kb else []
    return InlineKeyboardMarkup(rows + [[Btn("↩️ В меню", callback_data="m:home")]])


async def _show(q, uid: int, section: str, text: str, kb: InlineKeyboardMarkup | None, parse_mode=None, home: bool = True) -> None:
    """Открывает раздел в панели: меняет картинку и подпись. Без картинок или с длинным текстом — обычным сообщением."""
    if not (await _banners_on(uid) and _fits_caption(text)):
        await q.message.reply_text(text, reply_markup=kb, parse_mode=parse_mode, disable_web_page_preview=True)
        return
    kb = _with_home(kb) if home else kb
    image = await banners.get(section)
    if q.message.photo:
        msg = await q.edit_message_media(InputMediaPhoto(image, caption=text, parse_mode=parse_mode), reply_markup=kb)
    else:
        msg = await q.message.reply_photo(image, caption=text, parse_mode=parse_mode, reply_markup=kb)
    banners.remember(section, msg)


async def send_menu(message: Message, uid: int) -> None:
    if await _banners_on(uid):
        text, kb = menu(uid, short=True)
        msg = await message.reply_photo(
            await banners.get("menu"), caption=await styles.wrap(uid, "greeting", text, html=True), reply_markup=kb, parse_mode=ParseMode.HTML
        )
        banners.remember("menu", msg)
        return
    text, kb = menu(uid)
    await message.reply_text(await styles.wrap(uid, "greeting", text, html=True), reply_markup=kb, parse_mode=ParseMode.HTML)


async def banners_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    rows = [[Btn(f"🎨 {name}", callback_data=f"bn:{key}")] for key, name in banners.SECTIONS.items()]
    await update.message.reply_text(
        "🖼 Баннеры разделов. Сейчас стоят временные картинки; когда пришлёте дизайн из Figma, они заменятся им.\n"
        "Можно нарисовать баннер ИИ в стиле иконок — выберите раздел:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def on_banner_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer()
        return
    _, section, *rest = q.data.split(":")
    action = rest[0] if rest else "new"
    try:
        if section not in banners.SECTIONS:
            raise ValueError("Нет такого раздела")
        if action == "ok":
            await banners.save(section, q.message.photo[-1].file_id)
            await q.answer("Баннер сохранён ✓")
            await q.edit_message_reply_markup(None)
            return
        if action == "reset":
            await banners.save(section, None)
            await q.answer("Вернула временную картинку")
            await q.edit_message_reply_markup(None)
            return
        await q.answer()
        status = await q.message.reply_text(f"🎨 Рисую баннер «{banners.SECTIONS[section]}»…")
        try:
            image = await banners.draw(section)
        except Exception as e:
            log.exception("banner failed")
            await status.edit_text(f"🎨 Не получилось нарисовать: {e}"[:4000])
            return
        await status.delete()
        await q.message.reply_photo(
            image,
            caption=f"🖼 Баннер «{banners.SECTIONS[section]}»",
            reply_markup=InlineKeyboardMarkup([
                [Btn("✅ Поставить", callback_data=f"bn:{section}:ok"), Btn("🔄 Другой", callback_data=f"bn:{section}:new")],
                [Btn("↩️ Временная картинка", callback_data=f"bn:{section}:reset")],
            ]),  # fmt: skip
        )
    except Exception as e:
        log.exception("banner button failed")
        await _fail(q, e)

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
            style = (await scheduler.get_settings(m["tg"])).get("style") or "standard"
            persona = await styles.persona(m["tg"]) if style == styles.CUSTOM else None
            lines.append(
                f"• {m['name']}" + (f" ({m['username']})" if m["username"] else "")
                + (f" · 🎭 {styles.style_name(style, persona and persona['name'])}" if style != "standard" else "")
            )
            row = [Btn(f"🗑 Убрать {m['name']}", callback_data=f"mr:{m['tg']}")]
            if style != "standard":
                row.append(Btn("🎭 Сбросить стиль", callback_data=f"ms:{m['tg']}"))
            rows.append(row)
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
                await _edit(q, "Этого участника уже нет.")
                return
            await _edit(q, 
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
        elif action == "ms":
            await scheduler.update_settings(int(arg), style="standard")
            await q.answer("Стиль сброшен на обычный")
            try:
                await ctx.bot.send_message(int(arg), "🎭 Владелица вернула обычный стиль общения. Выбрать другой: /style")
            except Exception:
                log.info("could not notify member about style reset")
        else:
            await q.answer()
        text, kb = await _members_view()
    except Exception as e:
        log.exception("member button failed")
        await _fail(q, e)
        return
    await _edit(q, text, reply_markup=kb)


# ---------- ⚙️ настройки ----------
# callback_data: st:tz (выбор пояса)  |  st:tz:<номер>  |  st:ev (выбор времени)  |  st:ev:<ЧЧ:ММ>  |  st:back

TIMEZONES = [
    ("Калининград", "Europe/Kaliningrad"),
    ("Москва", "Europe/Moscow"),
    ("Самара", "Europe/Samara"),
    ("Екатеринбург", "Asia/Yekaterinburg"),
    ("Омск", "Asia/Omsk"),
    ("Новосибирск", "Asia/Novosibirsk"),
    ("Иркутск", "Asia/Irkutsk"),
    ("Владивосток", "Asia/Vladivostok"),
]
EVENING_TIMES = ["18:00", "19:00", "20:00", "21:00", "22:00", "23:00"]
TZ_PROMPT = "🌍 Напишите часовой пояс ответом на это сообщение: например «Europe/Berlin», «Asia/Almaty» или «UTC+5»."
TIME_PROMPT = "🌙 Во сколько присылать вечерний разбор? Напишите время ответом, например «21:30»."
CHECKIN_PROMPT = "☀️ Во сколько присылать дневные чек-ины? Напишите время через запятую ответом, например «11, 15:30, 19»."
CHECKIN_PRESETS = {"12141618": ["12:00", "14:00", "16:00", "18:00"], "1317": ["13:00", "17:00"], "15": ["15:00"], "off": []}


def _tz_label(tz: str) -> str:
    name = next((n for n, z in TIMEZONES if z == tz), tz)
    offset = scheduler.local_now({"tz": tz}).strftime("%z")
    return f"{name} (UTC{offset[:3]}{':' + offset[3:] if offset[3:] != '00' else ''})"


async def _settings_view(uid: int) -> tuple[str, InlineKeyboardMarkup]:
    st = await scheduler.get_settings(uid)
    pinned = await _pinned_project(uid)
    now = scheduler.local_now(st).strftime("%H:%M")
    checkins = ", ".join(st.get("checkins") or []) or "выключены"
    text = (
        "⚙️ Настройки\n\n"
        f"🌍 Часовой пояс: {_tz_label(st['tz'])}, у вас сейчас {now}\n"
        f"🌙 Вечерний разбор: {st['evening']}\n"
        f"☀️ Дневные чек-ины: {checkins}\n"
        f"😊 Вопрос о настроении: {'вместе с вечерним разбором' if st.get('mood', True) else 'выключен'}\n"
        f"📌 Новые заметки: {'сразу в «' + pinned['name'] + '», без разбора' if pinned else 'во входящие, разбор вечером'}\n"
        f"👥 Новости командных проектов: {'присылать' if st.get('team_notify', True) else 'не присылать'}\n"
        f"🖼 Меню с картинками: {'да' if st.get('banners') else 'нет, только текст'}"
    )
    return text, InlineKeyboardMarkup(
        [
            [Btn("🌍 Часовой пояс", callback_data="st:tz"), Btn("🌙 Время разбора", callback_data="st:ev")],
            [Btn("☀️ Чек-ины", callback_data="st:ci"), Btn("😊 Настроение: вкл/выкл", callback_data="st:mood")],
            [Btn("📌 Куда писать", callback_data="st:ap"), Btn("👥 Новости команды: вкл/выкл", callback_data="st:tn")],
            [Btn("🖼 Картинки в меню: вкл/выкл", callback_data="st:bn")],
        ]
    )


async def settings_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = await _settings_view(update.effective_user.id)
    await update.message.reply_text(text, reply_markup=kb)


async def on_settings_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    uid = q.from_user.id
    if not is_member(uid):
        await q.answer()
        return
    parts = q.data.split(":")
    try:
        if parts[1] == "tz" and len(parts) == 2:
            buttons = [Btn(name, callback_data=f"st:tz:{i}") for i, (name, _) in enumerate(TIMEZONES)]
            rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
            rows.append([Btn("✍️ Другой", callback_data="st:tzx"), Btn("↩️ Назад", callback_data="st:back")])
            await q.answer()
            await _edit(q, "🌍 Выберите часовой пояс:", reply_markup=InlineKeyboardMarkup(rows))
            return
        if parts[1] == "tz":
            await scheduler.update_settings(uid, tz=TIMEZONES[int(parts[2])][1])
            await q.answer("Сохранено ✓")
        elif parts[1] == "tzx":
            await q.answer()
            await q.message.reply_text(TZ_PROMPT, reply_markup=ForceReply(input_field_placeholder="Europe/Berlin"))
            return
        elif parts[1] == "ev" and len(parts) == 2:
            buttons = [Btn(t, callback_data=f"st:ev:{t.replace(':', '')}") for t in EVENING_TIMES]
            rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
            rows.append([Btn("✍️ Своё время", callback_data="st:evx"), Btn("↩️ Назад", callback_data="st:back")])
            await q.answer()
            await _edit(q, "🌙 Во сколько присылать вечерний разбор?", reply_markup=InlineKeyboardMarkup(rows))
            return
        elif parts[1] == "ev":
            await scheduler.update_settings(uid, evening=f"{parts[2][:2]}:{parts[2][2:]}")
            await q.answer("Сохранено ✓")
        elif parts[1] == "evx":
            await q.answer()
            await q.message.reply_text(TIME_PROMPT, reply_markup=ForceReply(input_field_placeholder="21:30"))
            return
        elif parts[1] == "ci" and len(parts) == 2:
            rows = [
                [Btn("12, 14, 16, 18", callback_data="st:ci:12141618"), Btn("13 и 17", callback_data="st:ci:1317")],
                [Btn("Только 15:00", callback_data="st:ci:15"), Btn("✍️ Своё время", callback_data="st:cix")],
                [Btn("🔕 Выключить", callback_data="st:ci:off"), Btn("↩️ Назад", callback_data="st:back")],
            ]
            await q.answer()
            await _edit(q, 
                "☀️ Когда днём спрашивать о неразобранном? Пишу только если оно есть.", reply_markup=InlineKeyboardMarkup(rows)
            )
            return
        elif parts[1] == "ci":
            await scheduler.update_settings(uid, checkins=CHECKIN_PRESETS[parts[2]])
            await q.answer("Сохранено ✓")
        elif parts[1] == "ap" and len(parts) == 2:
            projects = await notion.projects(uid)
            buttons = [Btn(f"📌 {p}", callback_data=f"st:ap:{i}") for i, p in enumerate(projects)]
            rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
            rows.append([Btn("📥 Во входящие", callback_data="st:ap:off"), Btn("↩️ Назад", callback_data="st:back")])
            await q.answer()
            await _edit(q, 
                "📌 Куда класть новые заметки? Если выбрать проект, они сразу попадают в него и не ждут вечернего разбора.",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return
        elif parts[1] == "ap":
            if parts[2] == "off":
                await scheduler.update_settings(uid, active_project=None)
            else:
                projects = await notion.project_list(uid)
                if int(parts[2]) >= len(projects):
                    await q.answer("Список проектов изменился, попробуйте ещё раз")
                    return
                await scheduler.update_settings(uid, active_project=projects[int(parts[2])]["id"])
            await q.answer("Сохранено ✓")
        elif parts[1] == "bn":
            st = await scheduler.get_settings(uid)
            await scheduler.update_settings(uid, banners=not st.get("banners"))
            await q.answer("Сохранено ✓")
        elif parts[1] == "tn":
            st = await scheduler.get_settings(uid)
            await scheduler.update_settings(uid, team_notify=not st.get("team_notify", True))
            await q.answer("Сохранено ✓")
        elif parts[1] == "mood":
            st = await scheduler.get_settings(uid)
            await scheduler.update_settings(uid, mood=not st.get("mood", True))
            await q.answer("Сохранено ✓")
        elif parts[1] == "cix":
            await q.answer()
            await q.message.reply_text(CHECKIN_PROMPT, reply_markup=ForceReply(input_field_placeholder="11, 15:30, 19"))
            return
        else:
            await q.answer()
        text, kb = await _settings_view(uid)
    except Exception as e:
        log.exception("settings failed")
        await _fail(q, e)
        return
    await _edit(q, text, reply_markup=kb)


async def _settings_reply(message: Message, prompt: str) -> None:
    uid = message.chat_id
    if prompt == CHECKIN_PROMPT:
        times = [scheduler.parse_time(t) for t in re.split(r"[,;\s]+", message.text) if t.strip()]
        if not times or None in times:
            await message.reply_text("Не понял время. Пример: «11, 15:30, 19».")
            return
        await scheduler.update_settings(uid, checkins=sorted(set(times)))
    elif prompt == TZ_PROMPT:
        if not scheduler.parse_tz(message.text):
            await message.reply_text("Не понял часовой пояс. Пример: «Europe/Berlin» или «UTC+5».")
            return
        await scheduler.update_settings(uid, tz=message.text.strip())
    else:
        value = scheduler.parse_time(message.text)
        if not value:
            await message.reply_text("Не понял время. Пример: «21:30».")
            return
        await scheduler.update_settings(uid, evening=value)
    text, kb = await _settings_view(uid)
    await message.reply_text("Сохранено ✓\n\n" + text, reply_markup=kb)


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
    await send_menu(update.message, uid)


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
            await _show(q, uid, "razbor", text, kb, ParseMode.HTML, home=False)
        elif action == "projects":
            text, kb = await _projects_view(uid)
            await _show(q, uid, "projects", text, kb)
        elif action == "add":
            await _ask_project_names(q.message)
        elif action == "members" and is_owner(uid):
            text, kb = await _members_view()
            await q.message.reply_text(text, reply_markup=kb)
        elif action == "invite" and is_owner(uid):
            await _ask_invite_label(q.message)
        elif action == "ask":
            text, kb = await _ask_scope_view(uid)
            await _show(q, uid, "ask", text, kb)
        elif action == "settings":
            text, kb = await _settings_view(uid)
            await _show(q, uid, "settings", text, kb)
        elif action == "schedule":
            text, kb = await _schedule_view(uid)
            await _show(q, uid, "schedule", text, kb, ParseMode.HTML)
        elif action == "home":
            text, kb = menu(uid, short=True)
            await _show(q, uid, "menu", await styles.wrap(uid, "greeting", text, html=True), kb, ParseMode.HTML, home=False)
        elif action == "help":
            text, _kb = menu(uid)
            await q.message.reply_text(text, parse_mode=ParseMode.HTML)
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
            now = scheduler.local_now(await scheduler.get_settings(user.id))
            idea = await ai.structure(text=text, images=images, types=await notion.types(), now=now)
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
            idea.title,
            source,
            idea.tags,
            blocks,
            to_blocks(idea.details),
            user.id,
            user.full_name,
            when=idea.when,
            ai_type=idea.type_guess,
        )
    except Exception as e:
        log.exception("save failed")
        await status.edit_text(await styles.wrap(user.id, "error", f"❌ Не получилось сохранить: {e}"[:3800]))
        return
    pinned = await _pinned_project(user.id)
    if pinned:
        # Режим «📌 Писать в»: заметка сразу в проекте и без разбора
        try:
            await _file(update.get_bot(), user.id, page_id, pinned["name"], skip_review=True)
        except Exception as e:
            log.exception("pinned project failed")
            note += f"\n\n⚠️ Не получилось положить в «{escape(pinned['name'])}»: {escape(str(e)[:300])}"
            pinned = None
    when_line = f"\n⏰ Напомню: {escape(await _when_label(user.id, idea.when))}" if idea.when else ""
    pin_line = f"\n📌 Сразу в «{escape(pinned['name'])}», без разбора (выключить: /settings)" if pinned else ""
    await status.edit_text(
        await styles.wrap(user.id, "saved", f'✅ <b>{escape(idea.title)}</b>\n📄 <a href="{url}">Заметка — тут</a>{pin_line}{when_line}{note}', html=True),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=_panel(page_id, pinned["name"] if pinned else None, when=idea.when),
    )


# ---------- кнопка «В проект» под заметкой ----------
# callback_data: n:<page_id> (показать проекты)  |  np:<page_id>:<проект>  |  nx:<page_id> (свернуть)


def _panel(page_id: str, project: str | None = None, when: str | None = None) -> InlineKeyboardMarkup:
    """Под заметкой: «В проект», а если у заметки есть срок — ещё «изменить» и «без срока»."""
    label = f"📁 {project} ✓" if project else "📁 В проект"
    rows = [[Btn(label, callback_data=f"n:{page_id}")]]
    if when:
        rows.append([Btn("⏰ Изменить срок", callback_data=f"w:{page_id}:-"), Btn("✖️ Без срока", callback_data=f"wx:{page_id}:-")])
    return InlineKeyboardMarkup(rows)


async def _when_label(uid: int, iso: str | None) -> str:
    if not iso:
        return ""
    return whenparse.human(iso, scheduler.local_now(await scheduler.get_settings(uid)))


async def on_panel(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_member(q.from_user.id):
        await q.answer()
        return
    action, page_id, *rest = q.data.split(":")
    try:
        await own_page(q.from_user.id, page_id)
        if action == "n":
            projects = await notion.projects(q.from_user.id)
            if not projects:
                await q.answer("Проектов пока нет: /addproject", show_alert=True)
                return
            buttons = [Btn(f"📁 {p}", callback_data=f"np:{page_id}:{i}") for i, p in enumerate(projects)]
            rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
            rows.append([Btn("✖️ Свернуть", callback_data=f"nx:{page_id}")])
            await q.answer()
            await q.edit_message_reply_markup(InlineKeyboardMarkup(rows))
        elif action == "np":
            projects = await notion.projects(q.from_user.id)
            idx = int(rest[0])
            if idx >= len(projects):
                await q.answer("Список проектов изменился, попробуйте ещё раз")
                return
            await _file(q.get_bot(), q.from_user.id, page_id, projects[idx])
            await q.answer(f"→ {projects[idx]}")
            item = await notion.page_info(page_id)
            await q.edit_message_reply_markup(_panel(page_id, projects[idx], item["when"]))
        else:
            await q.answer()
            item = await notion.page_info(page_id)
            await q.edit_message_reply_markup(_panel(page_id, item["project"], item["when"]))
    except Exception as e:
        log.exception("panel failed")
        await _fail(q, e)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    reply = update.message.reply_to_message
    if reply and reply.from_user and reply.from_user.id == ctx.bot.id and reply.text == INVITE_PROMPT:
        if is_owner(update.effective_user.id):
            await _create_invite(update.message, ctx, update.message.text)
        return
    if reply and reply.from_user and reply.from_user.id == ctx.bot.id and reply.text in (TZ_PROMPT, TIME_PROMPT, CHECKIN_PROMPT):
        await _settings_reply(update.message, reply.text)
        return
    if reply and reply.from_user and reply.from_user.id == ctx.bot.id and reply.text == ADD_PROMPT:
        await _add_projects_reply(update.message, update.message.text)
        return
    if reply and reply.from_user and reply.from_user.id == ctx.bot.id and (reply.text or "").startswith(PROJECT_DESC_MARK):
        await _project_desc_reply(update.message, reply.text)
        return
    if reply and reply.from_user and reply.from_user.id == ctx.bot.id and reply.text in PERSONA_PROMPTS:
        await _persona_step(update.message, reply.text)
        return
    if reply and reply.from_user and reply.from_user.id == ctx.bot.id and reply.text == TEAM_PROMPT:
        if can_create_team(update.effective_user.id):
            await _create_team(update.message, update.message.text)
        return
    if reply and reply.from_user and reply.from_user.id == ctx.bot.id and reply.text == AREA_PROMPT:
        await update.message.reply_text(await _area_reply(update.message, update.message.text))
        return
    if reply and reply.from_user and reply.from_user.id == ctx.bot.id and (reply.text or "").startswith(MOOD_COMMENT_MARK):
        await _mood_comment(update.message, reply.text)
        return
    if reply and reply.from_user and reply.from_user.id == ctx.bot.id and reply.text in (ROUTINE_PROMPT, CHANGE_PROMPT):
        await _schedule_reply(update.message, routine=reply.text == ROUTINE_PROMPT)
        return
    if reply and reply.from_user and reply.from_user.id == ctx.bot.id and reply.text == TYPE_PROMPT:
        await update.message.reply_text(await _add_types_text(update.message.text))
        return
    if reply and reply.from_user and reply.from_user.id == ctx.bot.id and (reply.text or "").startswith(WHEN_PROMPT_MARK):
        if page_id := _ai_page(reply):
            await _when_reply(update.message, page_id)
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
        return await styles.wrap(uid, "all_done", "🎉 Всё разобрано!", html=True), None
    if k >= len(items):
        return f"Остальное пропущено. В разборе ещё: {len(items)}.", InlineKeyboardMarkup(
            [[Btn("↩️ Сначала", callback_data="r:0")]]
        )
    k = max(k, 0)
    item = items[k]
    body = await notion.preview(item["id"])
    where = f"📁 {escape(item['project'])} ✓" if item["project"] else "📭 Без проекта"
    when = f"\n⏰ Срок: {escape(await _when_label(uid, item['when']))}" if item["when"] else ""
    text = (
        f"<b>Заметка {k + 1} из {len(items)}</b> · {where}{when}\n\n"
        f'<a href="{item["url"]}">{escape(item["title"])}</a>\n\n{escape(body)}\n\n'
    )
    if show_projects:
        projects = await notion.projects(uid)
        text += "В какой проект?"
        buttons = [
            Btn(f"📁 {p} ✓" if p == item["project"] else f"📁 {p}", callback_data=f"p:{item['id']}:{i}:{k}")
            for i, p in enumerate(projects)
        ]
        rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
        rows.append([Btn("↩️ Назад", callback_data=f"r:{k}")])
        return text, InlineKeyboardMarkup(rows)

    types = await notion.types()
    text += "Что это?" + (f" ✨ ИИ думает: {escape(item['ai_type'])}" if item.get("ai_type") in types else "")
    buttons = [
        Btn(f"✨ {t}" if t == item.get("ai_type") else t, callback_data=f"t:{item['id']}:{i}:{k}") for i, t in enumerate(types)
    ]
    # Предложенный ИИ тип — первой кнопкой
    buttons.sort(key=lambda b: not b.text.startswith("✨"))
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    project_label = f"📁 {item['project']} ✓" if item["project"] else "📁 В проект"
    rows.append(
        [
            Btn(project_label, callback_data=f"pj:{item['id']}:{k}"),
            Btn("⏰ Срок", callback_data=f"w:{item['id']}:{k}"),
        ]
    )
    rows.append([Btn("🤖 ИИ", callback_data=f"a:{item['id']}"), Btn("👁 Целиком", callback_data=f"v:{item['id']}")])
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
    if not message.text or not message.text.startswith(("🤖", "✍️", "⏰", EXPAND_MARK)):
        return None
    for entity in message.entities or []:
        if entity.url and "notion" in entity.url:
            if m := re.search(r"([0-9a-f]{32})(?:\?|$)", entity.url.replace("-", "")):
                return m.group(1)
    return None


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
        project = await project_context(item["project"])
        header = f"# {item['title']}" + (f"\nПроект: {project}" if project else "")
        context = "\n\n".join(p for p in (header, note["summary"], note["details"]) if p)
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


async def _ask_scope_view(uid: int) -> tuple[str, InlineKeyboardMarkup]:
    projects = await notion.projects(uid)
    buttons = [Btn(f"📁 {p}", callback_data=f"q:p:{i}") for i, p in enumerate(projects)]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([Btn("🗂 Всё за неделю", callback_data="q:d:7"), Btn("🗂 Всё за месяц", callback_data="q:d:30")])
    return "🔎 По каким заметкам спросить ИИ?", InlineKeyboardMarkup(rows)


async def ask_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = await _ask_scope_view(update.effective_user.id)
    await update.message.reply_text(text, reply_markup=kb)


async def on_scope_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not is_member(q.from_user.id):
        return
    _, kind, value = q.data.split(":")
    if kind == "p":
        projects = await notion.projects(q.from_user.id)
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
        project_text = await project_context(item["project"])
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
        "project": project_text,
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
            state["type"], state["note"], state["project"], state["titles"], state["images"], state["qa"]
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
            state["type"], state["note"], state["project"], state["titles"], state["images"], state["qa"], state["draft"]
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


# ---------- ⏰ сроки и напоминания ----------
# callback_data: w:<page>:<номер|-> (меню срока)  |  wv:<page>:<номер|->:<t|d1|w1> (быстрый срок)
#                wc:<page>:<номер|-> (своя дата)  |  wx:<page>:<номер|-> (без срока)
#                dl:ok:<page> (готово)  |  dl:sn:<page> (перенести)  |  dl:p1h / dl:p1d / dl:p7d:<page>  |  ov (просроченные)
# «номер» — позиция в разборе, куда вернуться; «-» — сообщение после сохранения заметки.

WHEN_PROMPT_MARK = "⏰ Срок для "


def _quick_when(code: str, now: datetime, settings: dict, current: str | None = None) -> str:
    if code == "t":
        hh, mm = map(int, settings["evening"].split(":"))
        at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return (at if at > now else now + timedelta(hours=1)).replace(second=0, microsecond=0).isoformat()
    if code == "p1h":
        return (now + timedelta(hours=1)).replace(second=0, microsecond=0).isoformat()
    days = {"d1": 1, "p1d": 1, "w1": 7, "p7d": 7}[code]
    if current and "T" in current:
        # У срока было время: переносим на тот же час
        at = datetime.fromisoformat(current).astimezone(now.tzinfo)
        return datetime.combine(now.date() + timedelta(days=days), at.time(), now.tzinfo).isoformat()
    return (now.date() + timedelta(days=days)).isoformat()


async def _after_when(q, page_id: str, where: str, uid: int) -> None:
    """Вернуть экран, откуда пришли: карточку разбора или сообщение о сохранённой заметке."""
    if where != "-":
        text, kb = await _review_view(uid, int(where))
        await _edit(q, text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return
    item = await notion.page_info(page_id)
    lines = [ln for ln in (q.message.text_html or "").split("\n") if not ln.startswith("⏰")]
    if item["when"]:
        lines.insert(min(2, len(lines)), f"⏰ Напомню: {escape(await _when_label(uid, item['when']))}")
    await _edit(q, 
        "\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        reply_markup=_panel(page_id, item["project"], item["when"]),
    )


async def on_when_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    uid = q.from_user.id
    if not is_member(uid):
        await q.answer()
        return
    action, page_id, where, *rest = q.data.split(":") + [""]
    try:
        item = await own_page(uid, page_id)
        settings = await scheduler.get_settings(uid)
        now = scheduler.local_now(settings)
        if action == "w":
            await q.answer()
            rows = [
                [Btn("Сегодня вечером", callback_data=f"wv:{page_id}:{where}:t"), Btn("Завтра", callback_data=f"wv:{page_id}:{where}:d1")],
                [Btn("Через неделю", callback_data=f"wv:{page_id}:{where}:w1"), Btn("✍️ Своя дата", callback_data=f"wc:{page_id}:{where}")],
            ]
            tail = [Btn("↩️ Назад", callback_data=f"r:{where}" if where != "-" else f"nx:{page_id}")]
            if item["when"]:
                tail.insert(0, Btn("✖️ Без срока", callback_data=f"wx:{page_id}:{where}"))
            rows.append(tail)
            await q.edit_message_reply_markup(InlineKeyboardMarkup(rows))
            return
        if action == "wc":
            await q.answer()
            await q.message.reply_text(
                f'{WHEN_PROMPT_MARK}<a href="{item["url"]}">{escape(item["title"])}</a>: напишите ответом на это сообщение.\n'
                "Например: «завтра в 15», «пятница 18:00», «15.10», «через 2 часа».",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=ForceReply(input_field_placeholder="пятница 18:00"),
            )
            return
        when = None if action == "wx" else _quick_when(rest[0], now, settings, item["when"])
        await notion.set_when(page_id, when)
        await q.answer(f"⏰ {await _when_label(uid, when)}" if when else "Срок убран")
        await _after_when(q, page_id, where, uid)
    except Exception as e:
        log.exception("when button failed")
        await _fail(q, e)


async def _when_reply(message: Message, page_id: str) -> None:
    """Ответ на «⏰ Срок для …»: разбираем сами, что не поняли — отдаём ИИ."""
    uid = message.chat_id
    try:
        item = await own_page(uid, page_id)
        now = scheduler.local_now(await scheduler.get_settings(uid))
        parsed = whenparse.parse(message.text, now)
        when = parsed.iso() if parsed else await ai.parse_when(message.text, now)
        if not when:
            await message.reply_text("Не понял срок. Пример: «завтра в 15», «пятница 18:00», «15.10».")
            return
        await notion.set_when(page_id, when)
    except Exception as e:
        log.exception("when reply failed")
        await message.reply_text(f"❌ Не получилось поставить срок: {e}"[:4000])
        return
    await message.reply_text(
        f'⏰ Срок для «{escape(item["title"])}»: {escape(await _when_label(uid, when))} ✓',
        parse_mode=ParseMode.HTML,
        reply_markup=_panel(page_id, item["project"], when),
    )


async def on_deadline_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    uid = q.from_user.id
    if not is_member(uid):
        await q.answer()
        return
    parts = q.data.split(":")
    try:
        if parts[0] == "ov":
            await q.answer()
            await send_overdue(q.get_bot(), uid)
            return
        action, page_id = parts[1], parts[2]
        item = await own_page(uid, page_id)
        if action == "ok":
            await notion.set_done(page_id)
            await q.answer((await styles.line(uid, "praise")) or "Готово ✓")
            await q.edit_message_reply_markup(InlineKeyboardMarkup([[Btn("✅ Готово", callback_data="noop")]]))
            return
        if action == "sn":
            await q.answer()
            await q.edit_message_reply_markup(
                InlineKeyboardMarkup(
                    [
                        [Btn("+1 час", callback_data=f"dl:p1h:{page_id}"), Btn("Завтра", callback_data=f"dl:p1d:{page_id}")],
                        [Btn("Через неделю", callback_data=f"dl:p7d:{page_id}"), Btn("✍️ Своя дата", callback_data=f"wc:{page_id}:-")],
                    ]
                )
            )
            return
        settings = await scheduler.get_settings(uid)
        when = _quick_when(action, scheduler.local_now(settings), settings, item["when"])
        await notion.set_when(page_id, when)
        label = await _when_label(uid, when)
        await q.answer(f"Перенесено: {label}")
        await q.edit_message_reply_markup(InlineKeyboardMarkup([[Btn(f"⏰ Перенесено: {label}", callback_data="noop")]]))
    except Exception as e:
        log.exception("deadline button failed")
        await _fail(q, e)


async def send_overdue(bot, uid: int) -> None:
    """Просроченные невыполненные заметки — каждая отдельным сообщением с кнопками."""
    settings = await scheduler.get_settings(uid)
    items = await scheduler.overdue(uid, settings)
    if not items:
        await bot.send_message(uid, "🎉 Просроченного нет!")
        return
    for item in items[:10]:
        await bot.send_message(
            uid,
            f'🔥 <a href="{item["url"]}">{escape(item["title"])}</a> — срок был {escape(await _when_label(uid, item["when"]))}',
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=scheduler.reminder_markup(item["id"]),
        )


# ---------- ☀️ дневные чек-ины ----------
# callback_data: ci:quick / ci:batch / ci:later / ci:off — ответ на чек-ин
#   ⚡ быстро: kq (следующая карточка) | ka:<page> (актуально) | kn:<page> (неактуально → в корзину)
#   📦 пачкой: bt:<номер> (галочка) | ba (все/никто) | bp → bpp:<проект> | by → byy:<тип> | bd → bdd (удалить) | br | bx

BATCH_SIZE = 10
# Состояние «пачки» по сообщению: id заметок списка и отмеченные. Если бот перезапустился — просим открыть заново.
_batches: dict[tuple[int, int], dict] = {}


async def _quick_seen(uid: int) -> tuple[str, list[str]]:
    """Заметки, уже отмеченные «актуально» сегодня в быстром режиме: второй раз их не показываем."""
    today = scheduler.local_now(await scheduler.get_settings(uid)).date().isoformat()
    key = f"quick:{uid}:{today}"
    return key, json.loads(await notion.get_value(key) or "[]")


async def _quick_view(uid: int) -> tuple[str, InlineKeyboardMarkup | None]:
    _, seen = await _quick_seen(uid)
    items = [i for i in await notion.review_items(uid) if i["id"] not in seen]
    if not items:
        return "⚡ Готово! Всё актуальное ждёт вечернего разбора.", None
    item = items[0]
    return (
        f"⚡ Ещё {len(items)}. Актуально?\n\n<b>{escape(item['title'])}</b>",
        InlineKeyboardMarkup(
            [
                [Btn("✅ Актуально", callback_data=f"ka:{item['id']}"), Btn("🗑 Неактуально", callback_data=f"kn:{item['id']}")],
                [Btn("⏸ Хватит", callback_data="ci:later")],
            ]
        ),
    )


def _batch_view(state: dict) -> tuple[str, InlineKeyboardMarkup]:
    items, picked = state["items"], state["picked"]
    lines = [f"📦 Отметьте заметки и выберите, что с ними сделать. Отмечено: {len(picked)}"]
    rows = []
    for i, item in enumerate(items):
        mark = "☑️" if item["id"] in picked else "⬜"
        where = f" · 📁 {item['project']}" if item["project"] else ""
        rows.append([Btn(f"{mark} {item['title'][:40]}{where}", callback_data=f"bt:{i}")])
    rows.append([Btn("☑️ Все" if len(picked) < len(items) else "⬜ Никто", callback_data="ba")])
    if picked:
        rows.append([Btn("📁 В проект", callback_data="bp"), Btn("🏷 Тип", callback_data="by"), Btn("🗑 Удалить", callback_data="bd")])
    rows.append([Btn("🔄 Обновить", callback_data="br"), Btn("✖️ Закрыть", callback_data="bx")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def _new_batch(uid: int) -> dict:
    return {"items": (await notion.review_items(uid))[:BATCH_SIZE], "picked": set()}


async def on_checkin_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    uid = q.from_user.id
    if not is_member(uid):
        await q.answer()
        return
    action = q.data.split(":")[1]
    try:
        if action == "later":
            await q.answer("Ок, вечером разберём")
            await q.edit_message_reply_markup(InlineKeyboardMarkup([[Btn("🌙 Отложено на вечер", callback_data="noop")]]))
        elif action == "off":
            await scheduler.checkins_off_today(uid)
            await q.answer("Сегодня больше не спрошу")
            await q.edit_message_reply_markup(InlineKeyboardMarkup([[Btn("🔕 Сегодня больше не спрашиваю", callback_data="noop")]]))
        elif action == "quick":
            await q.answer()
            text, kb = await _quick_view(uid)
            await q.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        else:  # batch
            await q.answer()
            state = await _new_batch(uid)
            if not state["items"]:
                await q.message.reply_text("🎉 Разбирать нечего!")
                return
            text, kb = _batch_view(state)
            sent = await q.message.reply_text(text, reply_markup=kb)
            _batches[(uid, sent.message_id)] = state
    except Exception as e:
        log.exception("checkin button failed")
        await _fail(q, e)


async def on_quick_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    uid = q.from_user.id
    if not is_member(uid):
        await q.answer()
        return
    action, _, page_id = q.data.partition(":")
    try:
        if page_id:
            await own_page(uid, page_id)
        if action == "ka":
            key, seen = await _quick_seen(uid)
            await notion.set_value(key, json.dumps(seen + [page_id]))
            await q.answer("Ждёт вечера ✓")
        elif action == "kn":
            await notion.trash(page_id)
            await q.answer("В корзине (можно восстановить в Notion)")
        else:
            await q.answer()
        text, kb = await _quick_view(uid)
    except Exception as e:
        log.exception("quick button failed")
        await _fail(q, e)
        return
    await _edit(q, text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def on_batch_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    uid = q.from_user.id
    if not is_member(uid):
        await q.answer()
        return
    key = (uid, q.message.message_id)
    action, _, arg = q.data.partition(":")
    state = _batches.get(key)
    try:
        if action == "bx":
            _batches.pop(key, None)
            await q.answer()
            await _edit(q, "📦 Закрыто.")
            return
        if state is None or action == "br":
            # Бот перезапускался или просили обновить — собираем список заново
            state = _batches[key] = await _new_batch(uid)
            await q.answer("Список обновлён" if action == "br" else "Список устарел, вот свежий")
        elif action == "bt":
            item_id = state["items"][int(arg)]["id"]
            state["picked"] ^= {item_id}
            await q.answer()
        elif action == "ba":
            all_ids = {i["id"] for i in state["items"]}
            state["picked"] = set() if state["picked"] == all_ids else all_ids
            await q.answer()
        elif action in ("bp", "by"):
            names = await (notion.projects(uid) if action == "bp" else notion.types())
            code = "bpp" if action == "bp" else "byy"
            buttons = [Btn(f"📁 {n}" if action == "bp" else n, callback_data=f"{code}:{i}") for i, n in enumerate(names)]
            rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)] + [[Btn("↩️ Назад", callback_data="bb")]]
            await q.answer()
            title = "В какой проект отправить отмеченные?" if action == "bp" else "Какой тип у отмеченных? После этого они разобраны."
            await _edit(q, title, reply_markup=InlineKeyboardMarkup(rows))
            return
        elif action == "bd":
            await q.answer()
            await _edit(q, 
                f"Удалить отмеченные ({len(state['picked'])})? Их можно будет восстановить из корзины Notion.",
                reply_markup=InlineKeyboardMarkup([[Btn("Да, удалить", callback_data="bdd"), Btn("↩️ Назад", callback_data="bb")]]),
            )
            return
        elif action in ("bpp", "byy", "bdd"):
            picked = list(state["picked"])
            for page_id in picked:
                await own_page(uid, page_id)
            if action == "bpp":
                name = (await notion.projects(uid))[int(arg)]
                for page_id in picked:
                    await _file(q.get_bot(), uid, page_id, name)
                await q.answer(f"→ {name}: {len(picked)}")
            elif action == "byy":
                name = (await notion.types())[int(arg)]
                for page_id in picked:
                    await notion.set_type(page_id, name)
                await q.answer(f"{name}: {len(picked)} ✓")
            else:
                for page_id in picked:
                    await notion.trash(page_id)
                await q.answer(f"Удалено: {len(picked)}")
            state = _batches[key] = await _new_batch(uid)
        else:  # bb — назад к списку
            await q.answer()
        if not state["items"]:
            _batches.pop(key, None)
            await _edit(q, await styles.wrap(uid, "all_done", "🎉 Всё разобрано!"))
            return
        text, kb = _batch_view(state)
    except Exception as e:
        log.exception("batch button failed")
        await _fail(q, e)
        return
    await _edit(q, text, reply_markup=kb)


# ---------- 🗓 расписание ----------
# callback_data: sc:week:<0|1> (эта/следующая неделя текстом) | sc:pdfw:<0|1> | sc:pdfm:<0|1> (этот/следующий месяц)
#                sc:base (задать базовое) | sc:edit (изменить словами) | sc:rev → sc:rev:<week|2weeks|month|off>
#                sc:keep (пересмотр: всё как есть) | sca (применить понятое) | scx (отмена)

ROUTINE_PROMPT = (
    "🗓 Опишите свой обычный распорядок на неделю ответом на это сообщение, своими словами. Например:\n"
    "«пн, ср, пт 10–14 работа в студии; вт и чт 19:00–20:30 зал; каждый день 23:00 сон»"
)
CHANGE_PROMPT = (
    "✏️ Что изменить или добавить? Ответьте на это сообщение своими словами. Например:\n"
    "«в эту среду зала не будет», «стоматолог 15.10 в 14:00 на час», «работа теперь с 11 до 15»"
)
REVIEW_OPTIONS = {"week": "раз в неделю", "2weeks": "раз в две недели", "month": "раз в месяц", "off": "не напоминать"}
# Понятые ИИ планы изменений по id сообщения с предпросмотром
_schedule_plans: dict[int, tuple[dict, str]] = {}


def _monday(day, weeks: int = 0):
    return day - timedelta(days=day.weekday()) + timedelta(weeks=weeks)


async def _schedule_view(uid: int) -> tuple[str, InlineKeyboardMarkup]:
    st = await scheduler.get_settings(uid)
    base = schedule.base_text(await notion.schedule_slots(uid))
    review = REVIEW_OPTIONS.get(st.get("schedule_review", "week"), "раз в неделю")
    return (
        f"🗓 Расписание\n\n<b>Базовое (каждую неделю):</b>\n{escape(base)}\n\nПересмотр: {review}",
        InlineKeyboardMarkup(
            [
                [Btn("📅 Эта неделя", callback_data="sc:week:0"), Btn("📅 Следующая", callback_data="sc:week:1")],
                [Btn("📄 PDF недели", callback_data="sc:pdfw:0"), Btn("📄 PDF месяца", callback_data="sc:pdfm:0")],
                [Btn("✍️ Задать базовое", callback_data="sc:base"), Btn("✏️ Изменить / добавить", callback_data="sc:edit")],
                [Btn("🔁 Как часто пересматривать", callback_data="sc:rev")],
            ]
        ),
    )


async def schedule_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = await _schedule_view(update.effective_user.id)
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


def _plan_text(plan: dict) -> str:
    lines = []
    if plan.get("replace_base"):
        lines.append("Новое базовое расписание (старое заменится):")
    for b in plan.get("base_add", []):
        time_ = f"{b.get('start', '')}–{b['end']}" if b.get("end") else b.get("start", "") or "весь день"
        lines.append(f"🔁 {', '.join(b.get('days', []))} {time_} — {b.get('title')}")
    for name in plan.get("base_remove", []):
        lines.append(f"🗑 убрать из базового: {name}")
    for e in plan.get("add", []):
        time_ = f"{e.get('start', '')}–{e['end']}" if e.get("end") else e.get("start", "")
        lines.append(f"📌 {e['date']} {time_} — {e.get('title')}".replace("  ", " "))
    for x in plan.get("cancel", []):
        lines.append(f"🚫 {x['date']}: без «{x.get('title')}»")
    return "\n".join(lines)


async def _schedule_reply(message: Message, routine: bool) -> None:
    uid = message.chat_id
    if not await ai_allowed(message):
        return
    status = await message.reply_text("🗓 Разбираю…")
    try:
        now = scheduler.local_now(await scheduler.get_settings(uid))
        base = schedule.base_text(await notion.schedule_slots(uid))
        plan = await ai.parse_schedule(message.text, base, now, routine)
    except Exception as e:
        log.exception("schedule parse failed")
        await status.edit_text(f"❌ Не получилось разобрать: {e}"[:4000])
        return
    text = _plan_text(plan)
    if not text:
        await status.edit_text("🤷 Не понял, что поменять. Попробуйте сформулировать иначе.")
        return
    sent = await status.edit_text(
        f"Вот что я понял:\n\n{text}\n\nПрименить?",
        reply_markup=InlineKeyboardMarkup([[Btn("✅ Применить", callback_data="sca"), Btn("✖️ Отмена", callback_data="scx")]]),
    )
    _schedule_plans[getattr(sent, "message_id", status.message_id)] = (plan, text)


async def _send_schedule_pdf(message: Message, uid: int, kind: str, offset: int) -> None:
    today = scheduler.local_now(await scheduler.get_settings(uid)).date()
    if kind == "pdfw":
        start = _monday(today, offset)
        items = await schedule.occurrences(uid, start, start + timedelta(days=6))
        data, name = pdf.week_pdf(items, start, today), f"неделя-{start.strftime('%d.%m')}.pdf"
    else:
        first = (today.replace(day=1) + timedelta(days=32 * offset)).replace(day=1)
        last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        items = await schedule.occurrences(uid, first, last)
        data, name = pdf.month_pdf(items, first.year, first.month, today), f"месяц-{first.strftime('%m.%Y')}.pdf"
    await message.reply_document(io.BytesIO(data), filename=name, caption="🗓 Для печати на A4")


async def on_schedule_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    uid = q.from_user.id
    if not is_member(uid):
        await q.answer()
        return
    parts = q.data.split(":")
    try:
        if parts[0] == "scx":
            _schedule_plans.pop(q.message.message_id, None)
            await q.answer()
            await q.edit_message_reply_markup(None)
            return
        if parts[0] == "sca":
            stored = _schedule_plans.pop(q.message.message_id, None)
            if not stored:
                await q.answer("Устарело — опишите изменение ещё раз", show_alert=True)
                return
            await q.answer("Применяю…")
            done = await schedule.apply(uid, stored[0])
            await _edit(q, "✅ Расписание обновлено:\n\n" + (stored[1] if done else "ничего не поменялось"))
            return
        action = parts[1]
        if action == "week":
            await q.answer()
            today = scheduler.local_now(await scheduler.get_settings(uid)).date()
            start = _monday(today, int(parts[2]))
            items = await schedule.occurrences(uid, start, start + timedelta(days=6))
            await q.message.reply_text(
                f"🗓 Неделя {start.strftime('%d.%m')}–{(start + timedelta(days=6)).strftime('%d.%m')}\n\n"
                + schedule.week_text(items, start),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[Btn("📄 В PDF", callback_data=f"sc:pdfw:{parts[2]}")]]),
            )
        elif action in ("pdfw", "pdfm"):
            await q.answer("Собираю PDF…")
            await _send_schedule_pdf(q.message, uid, action, int(parts[2]))
        elif action in ("base", "edit"):
            await q.answer()
            prompt = ROUTINE_PROMPT if action == "base" else CHANGE_PROMPT
            await q.message.reply_text(prompt, reply_markup=ForceReply(input_field_placeholder="своими словами"))
        elif action == "keep":
            await q.answer("Оставляем как есть ✓")
            await q.edit_message_reply_markup(InlineKeyboardMarkup([[Btn("✅ Оставили как есть", callback_data="noop")]]))
        elif action == "rev" and len(parts) == 2:
            await q.answer()
            rows = [[Btn(label, callback_data=f"sc:rev:{key}")] for key, label in REVIEW_OPTIONS.items()]
            await q.edit_message_reply_markup(InlineKeyboardMarkup(rows))
        elif action == "rev":
            await scheduler.update_settings(uid, schedule_review=parts[2])
            await q.answer(f"Пересмотр: {REVIEW_OPTIONS[parts[2]]} ✓")
            text, kb = await _schedule_view(uid)
            await _edit(q, text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception as e:
        log.exception("schedule button failed")
        await _fail(q, e)


# ---------- 😊 настроение и 📊 отчёт ----------
# callback_data: md:<день>:<1-5> (оценка)  |  rp:cur / rp:prev (отчёт за эту / прошлую неделю)

MOOD_COMMENT_MARK = "💬 Пара слов о дне "


async def on_mood_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    uid = q.from_user.id
    if not is_member(uid):
        await q.answer()
        return
    _, day, score = q.data.split(":")
    try:
        await notion.set_mood(uid, day, score=int(score))
    except Exception as e:
        log.exception("mood failed")
        await _fail(q, e)
        return
    emoji = dict((n, e) for e, n in scheduler.MOOD_BUTTONS)[int(score)]
    await q.answer(f"Записала {emoji}")
    await _edit(q, f"😊 Настроение {day[8:10]}.{day[5:7]}: {emoji} {score} из 5")
    await q.message.reply_text(
        f"{MOOD_COMMENT_MARK}{day[8:10]}.{day[5:7]}? Ответьте на это сообщение — или просто ничего не пишите.",
        reply_markup=ForceReply(input_field_placeholder="как прошёл день"),
    )


async def _mood_comment(message: Message, prompt: str) -> None:
    m = re.search(r"(\d{2})\.(\d{2})", prompt)
    today = scheduler.local_now(await scheduler.get_settings(message.chat_id)).date()
    day = today.replace(month=int(m.group(2)), day=int(m.group(1))) if m else today
    if day > today:  # запись прошлогодняя — например, ответили 1 января на 31 декабря
        day = day.replace(year=day.year - 1)
    await notion.set_mood(message.chat_id, day.isoformat(), comment=message.text)
    await message.reply_text("💬 Записала ✓")


async def report_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📊 Отчёт за какую неделю? Сам он приходит по понедельникам в 10:00 за прошедшую.",
        reply_markup=InlineKeyboardMarkup([[Btn("Эта неделя", callback_data="rp:cur"), Btn("Прошлая", callback_data="rp:prev")]]),
    )


async def on_report_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    uid = q.from_user.id
    if not is_member(uid):
        await q.answer()
        return
    await q.answer("Собираю отчёт…")
    if not await ai_allowed(q.message):
        return
    now = scheduler.local_now(await scheduler.get_settings(uid))
    monday = now.date() - timedelta(days=now.weekday()) - timedelta(days=7 if q.data == "rp:prev" else 0)
    try:
        await weekly.send_report(ctx.bot, uid, monday, now.tzinfo)
    except Exception as e:
        log.exception("report failed")
        await q.message.reply_text(f"❌ Не получилось собрать отчёт: {e}"[:4000])


# ---------- 🏷 типы (общие, как проекты: добавлять могут все, удалять — только владелица) ----------
# callback_data: ta:<номер> (спросить про удаление)  |  tk:<номер> (удалить)  |  tl (список)

TYPE_PROMPT = "🏷 Напишите названия новых типов ответом на это сообщение: через запятую, можно с эмодзи, например «🎵 Трек»."


async def _types_view(uid: int) -> tuple[str, InlineKeyboardMarkup | None]:
    names = await notion.types()
    text = "Типы записей:\n" + "\n".join(f"• {n}" for n in names) + "\n\nДобавить: /addtype"
    if not is_owner(uid) or not names:
        return text, None
    return text, InlineKeyboardMarkup([[Btn(f"🗑 {n}", callback_data=f"ta:{i}")] for i, n in enumerate(names)])


async def types_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = await _types_view(update.effective_user.id)
    await update.message.reply_text(text, reply_markup=kb)


async def _add_types_text(raw: str) -> str:
    names = [n.strip()[:100] for n in re.split(r"[\n,]", raw) if n.strip()]
    if not names:
        return "Напишите название после команды, например: /addtype 🎵 Трек"
    added = await notion.add_types(names)
    skipped = [n for n in names if n not in added]
    text = ("✅ Добавлено: " + ", ".join(added)) if added else "Ничего нового не добавлено."
    return text + (("\nУже были: " + ", ".join(skipped)) if skipped else "") + "\n\n/types — все типы"


async def addtype(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    raw = re.sub(r"^/addtype(@\w+)?", "", update.message.text, count=1)
    if not raw.strip():
        await update.message.reply_text(TYPE_PROMPT, reply_markup=ForceReply(input_field_placeholder="🎵 Трек"))
        return
    await update.message.reply_text(await _add_types_text(raw))


async def on_type_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer()
        return
    action, _, arg = q.data.partition(":")
    try:
        names = await notion.types()
        if action in ("ta", "tk") and int(arg) >= len(names):
            await q.answer("Список изменился")
        elif action == "ta":
            await q.answer()
            await _edit(q, 
                f"Удалить тип «{names[int(arg)]}»?\nУ заметок с этим типом поле «Тип» станет пустым, и они вернутся в разбор.",
                reply_markup=InlineKeyboardMarkup([[Btn("Да, удалить", callback_data=f"tk:{arg}"), Btn("Отмена", callback_data="tl")]]),
            )
            return
        elif action == "tk":
            await notion.delete_type(names[int(arg)])
            await q.answer(f"Удалено: {names[int(arg)]}")
        else:
            await q.answer()
        text, kb = await _types_view(q.from_user.id)
    except Exception as e:
        log.exception("type button failed")
        await _fail(q, e)
        return
    await _edit(q, text, reply_markup=kb)


# ---------- 📁 проекты и сферы ----------
# callback_data: pl (список) | plc (закрытые) | pr:<id> (карточка) | pr:<id>:<действие> (close, open, desc, kind, sph, del, delok)
#                ps:<проект>:<сфера|new|none> (выбор сферы)

PROJECT_DESC_MARK = "✏️ Описание проекта «"
AREA_PROMPT = (
    "🗂 Напишите сферу деятельности и коротко о ней ответом на это сообщение, например:\n"
    "«📷 Фотограф — портреты и репортажи, обрабатываю в Lightroom, люблю тёплый плёночный цвет»"
)


async def project_context(name: str | None) -> str | None:
    """«Сайт студии — описание. Сфера: 📷 Фотограф — описание» — чтобы ИИ понимал, о чём проект и в какой он сфере."""
    if not name:
        return None
    project = await notion.get_project(name)
    if not project:
        return name
    text = project["name"] + (f" — {project['description']}" if project["description"] else "")
    if project["sphere_id"]:
        area = next((a for a in await notion.spheres() if a["id"] == project["sphere_id"]), None)
        if area:
            text += f". Сфера деятельности: {area['name']}" + (f" — {area['description']}" if area["description"] else "")
    return text


def _can_manage(uid: int, project: dict) -> bool:
    return is_owner(uid) or project["creator"] == uid


async def _projects_view(uid: int, closed: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    rows_all = await notion.project_list(uid, include_closed=True)
    shown = [p for p in rows_all if (p["status"] == notion.STATUS_CLOSED) == closed]
    areas = {a["id"]: a["name"] for a in await notion.spheres()}
    if not shown:
        text = "Закрытых проектов нет." if closed else "Проектов пока нет. Добавить: /addproject"
    else:
        text = ("✅ Закрытые проекты:" if closed else "📁 Проекты") + " — нажмите, чтобы открыть карточку."
    # Сгруппированы по сферам: сначала со сферой, потом без
    shown.sort(key=lambda p: (areas.get(p["sphere_id"] or "", "яяя"), p["name"].lower()))
    rows, current = [], None
    for p in shown:
        area = areas.get(p["sphere_id"] or "")
        if area != current and area:
            rows.append([Btn(f"— {area} —", callback_data="noop")])
        current = area
        mark = {notion.KIND_PERSONAL: "👤 ", notion.KIND_TEAM: "👥 "}.get(p["kind"], "")
        rows.append([Btn(f"{mark}{p['name']}", callback_data=f"pr:{p['id']}")])
    has_closed = any(p["status"] == notion.STATUS_CLOSED for p in rows_all)
    nav = [Btn("📁 Открытые", callback_data="pl")] if closed else ([Btn("✅ Закрытые", callback_data="plc")] if has_closed else [])
    nav.append(Btn("➕ Новый", callback_data="m:add"))
    rows.append(nav)
    return text, InlineKeyboardMarkup(rows)


async def _project_card(uid: int, project: dict) -> tuple[str, InlineKeyboardMarkup]:
    areas = {a["id"]: a for a in await notion.spheres()}
    area = areas.get(project["sphere_id"] or "")
    count = await notion.project_note_count(project["id"])
    team_line = ""
    if project["kind"] == notion.KIND_TEAM:
        names = {m["tg"]: m["name"] for m in await notion.members()}
        names.setdefault(c.OWNER_ID, "владелица")
        people = [names.get(t, str(t)) for t in notion.team_people(project)]
        team_line = "👥 В проекте: " + escape(", ".join(people)) + "\n"
    pinned = (await scheduler.get_settings(uid)).get("active_project") == project["id"]
    text = (
        f"📁 <b>{escape(project['name'])}</b>\n"
        f"{project['status']} · {project['kind']} · сфера: {escape(area['name']) if area else '—'}\n"
        f"Заметок: {count}\n{team_line}" + ("📌 Новые заметки сразу идут сюда\n" if pinned else "") + "\n"
        + (escape(project["description"]) if project["description"] else "<i>Описания пока нет. ИИ учитывает его при раскрытии заметок.</i>")
    )
    pid = project["id"]
    rows = []
    if _can_manage(uid, project):
        closed = project["status"] == notion.STATUS_CLOSED
        rows.append([
            Btn("🟢 Открыть заново" if closed else "✅ Закрыть", callback_data=f"pr:{pid}:{'open' if closed else 'close'}"),
            Btn("✏️ Описание", callback_data=f"pr:{pid}:desc"),
        ])  # fmt: skip
        row = [Btn("🗂 Сфера", callback_data=f"pr:{pid}:sph")]
        if is_owner(uid) and project["kind"] != notion.KIND_TEAM:
            to_shared = project["kind"] == notion.KIND_PERSONAL
            row.append(Btn("🌐 Сделать общим" if to_shared else "👤 Сделать личным", callback_data=f"pr:{pid}:kind"))
        rows.append(row)
        if project["kind"] == notion.KIND_TEAM:
            rows.append([Btn("👥 Участники", callback_data=f"tm:{pid}"), Btn("🎨 Иконка", callback_data=f"ic:{pid}:new")])
        else:
            rows.append(
                ([Btn("👥 Сделать командным", callback_data=f"pr:{pid}:team")] if can_create_team(uid) else [])
                + [Btn("🎨 Иконка", callback_data=f"ic:{pid}:new")]
            )
    if project["status"] != notion.STATUS_CLOSED:
        rows.append([Btn("📌 Пишу сюда ✓ — выключить" if pinned else "📌 Писать сюда", callback_data=f"pr:{pid}:pin")])
    rows.append([Btn("📰 Лента", callback_data=f"fd:{pid}"), Btn("🔎 Спросить по проекту", callback_data=f"pr:{pid}:ask")])
    if project["kind"] == notion.KIND_TEAM:
        rows.append([Btn("🤖 Сводка сейчас", callback_data=f"fs:{pid}")])
    if is_owner(uid) or (project["creator"] == uid and project["kind"] == notion.KIND_PERSONAL):
        rows.append([Btn("🗑 Удалить проект", callback_data=f"pr:{pid}:del")])
    rows.append([Btn("↩️ Все проекты", callback_data="pl")])
    return text, InlineKeyboardMarkup(rows)


async def projects_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = await _projects_view(update.effective_user.id)
    await update.message.reply_text(text, reply_markup=kb)


async def _add_projects_reply(message: Message, raw: str) -> None:
    """Добавляет проекты, отвечает списком и рисует новым проектам иконки."""
    text, added = await _add_projects_text(raw, message.chat_id)
    await message.reply_text(text)
    for name in added:
        if project := await notion.get_project(name):
            await auto_icon(message, project)


async def _add_projects_text(raw: str, uid: int) -> tuple[str, list[str]]:
    # Проекты разделяются переносом строки или запятой
    names = [n.strip()[:100] for n in re.split(r"[\n,]", raw) if n.strip()]
    if not names:
        return "Напишите название после команды, например:\n/addproject Сайт-портфолио", []
    added = await notion.add_projects(names, uid)
    skipped = [n for n in names if n not in added]
    text = ("✅ Добавлено: " + ", ".join(added)) if added else "Ничего нового не добавлено."
    if added and not is_owner(uid):
        text += "\n👤 Это ваши личные проекты: их видите только вы."
    if skipped:
        text += "\nУже были: " + ", ".join(skipped)
    return text + "\n\n/projects — список проектов", added


async def addproject(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    raw = re.sub(r"^/addproject(@\w+)?", "", update.message.text, count=1)
    if not raw.strip():
        await _ask_project_names(update.message)
        return
    await _add_projects_reply(update.message, raw)


async def on_project_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    uid = q.from_user.id
    if not is_member(uid):
        await q.answer()
        return
    parts = q.data.split(":")
    try:
        if parts[0] in ("pl", "plc"):
            await q.answer()
            text, kb = await _projects_view(uid, closed=parts[0] == "plc")
            await _edit(q, text, reply_markup=kb)
            return
        if parts[0] == "ps":
            project = await notion.get_project(parts[1])
            if not project or not _can_manage(uid, project):
                raise PermissionError("Этот проект менять нельзя.")
            if parts[2] == "new":
                await q.answer()
                await q.message.reply_text(AREA_PROMPT, reply_markup=ForceReply(input_field_placeholder="📷 Фотограф — …"))
                return
            await notion.update_project(project["id"], sphere_id="" if parts[2] == "none" else parts[2])
            await q.answer("Сохранено ✓")
            project = await notion.get_project(project["id"])
        else:
            project = await notion.get_project(parts[1])
            if not project or not notion.can_see(project, uid):
                raise PermissionError("Этот проект недоступен.")
            action = parts[2] if len(parts) > 2 else "show"
            manage = _can_manage(uid, project)
            if action in ("close", "open", "desc", "sph", "team") and not manage:
                raise PermissionError("Менять проект может только его автор или владелица.")
            if action == "close":
                await notion.update_project(project["id"], status=notion.STATUS_CLOSED)
                await q.answer("Проект закрыт: он пропал из кнопок, заметки остались")
            elif action == "open":
                await notion.update_project(project["id"], status=notion.STATUS_ACTIVE)
                await q.answer("Проект снова открыт")
            elif action == "kind" and is_owner(uid):
                new = notion.KIND_SHARED if project["kind"] == notion.KIND_PERSONAL else notion.KIND_PERSONAL
                await notion.update_project(project["id"], kind=new)
                await q.answer(new)
            elif action == "pin":
                pinned = (await scheduler.get_settings(uid)).get("active_project") == project["id"]
                if not pinned and project["status"] == notion.STATUS_CLOSED:
                    raise PermissionError("Проект закрыт: сначала откройте его.")
                await scheduler.update_settings(uid, active_project=None if pinned else project["id"])
                await q.answer("📌 Выключено: заметки снова во входящие" if pinned else f"📌 Новые заметки сразу в «{project['name']}»", show_alert=not pinned)
            elif action == "team":
                if not can_create_team(uid):
                    raise PermissionError("Командные проекты пока создаёт только владелица.")
                await notion.update_project(project["id"], kind=notion.KIND_TEAM)
                await q.answer("Теперь проект командный")
                text, kb = await _team_members_view(await notion.get_project(project["id"]))
                await _edit(q, text, reply_markup=kb)
                return
            elif action == "desc":
                await q.answer()
                await q.message.reply_text(
                    f"{PROJECT_DESC_MARK}{project['name']}»: напишите ответом на это сообщение — о чём проект, цель, стиль, для кого.",
                    reply_markup=ForceReply(input_field_placeholder="о чём проект"),
                )
                return
            elif action == "sph":
                areas = await notion.spheres()
                rows = [[Btn(a["name"], callback_data=f"ps:{project['id']}:{a['id']}")] for a in areas]
                rows.append([Btn("➕ Новая сфера", callback_data=f"ps:{project['id']}:new"), Btn("Без сферы", callback_data=f"ps:{project['id']}:none")])
                await q.answer()
                await q.edit_message_reply_markup(InlineKeyboardMarkup(rows))
                return
            elif action == "ask":
                await q.answer()
                await q.message.reply_text(
                    f"{NOTES_PROMPT_MARK}{_scope_label(('p', project['name']))}: напишите вопрос ответом на это сообщение.",
                    reply_markup=ForceReply(input_field_placeholder="ваш вопрос"),
                )
                return
            elif action == "del":
                if not (is_owner(uid) or (project["creator"] == uid and project["kind"] == notion.KIND_PERSONAL)):
                    raise PermissionError("Удалить проект может только владелица.")
                await q.answer()
                await _edit(q, 
                    f"Удалить проект «{project['name']}»? Заметки останутся, но без проекта.\n"
                    "Если проект просто закончился — лучше «✅ Закрыть».",
                    reply_markup=InlineKeyboardMarkup([[Btn("Да, удалить", callback_data=f"pr:{project['id']}:delok"), Btn("Отмена", callback_data=f"pr:{project['id']}")]]),
                )
                return
            elif action == "delok":
                if not (is_owner(uid) or (project["creator"] == uid and project["kind"] == notion.KIND_PERSONAL)):
                    raise PermissionError("Удалить проект может только владелица.")
                await notion.delete_project(project["id"])
                await q.answer("Проект удалён")
                text, kb = await _projects_view(uid)
                await _edit(q, text, reply_markup=kb)
                return
            else:
                await q.answer()
            project = await notion.get_project(project["id"])
        text, kb = await _project_card(uid, project)
    except Exception as e:
        log.exception("project button failed")
        await _fail(q, e)
        return
    await _edit(q, text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def _project_desc_reply(message: Message, prompt: str) -> None:
    name = prompt[len(PROJECT_DESC_MARK):].split("»")[0]
    project = await notion.get_project(name)
    if not project or not _can_manage(message.chat_id, project):
        await message.reply_text("Этот проект менять нельзя.")
        return
    await notion.update_project(project["id"], description=message.text)
    text, kb = await _project_card(message.chat_id, await notion.get_project(project["id"]))
    await message.reply_text("Описание сохранено ✓\n\n" + text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def _area_reply(message: Message, raw: str) -> str:
    # «Название — описание» (или через двоеточие); описание необязательно
    m = re.match(r"\s*(.+?)(?:\s+[—–-]\s+|:\s+)(.+)", raw, re.S)
    name, description = (m.group(1), m.group(2)) if m else (raw, "")
    name, description = name.strip()[:100], description.strip()
    if not name:
        return "Напишите название сферы, например: /addarea 📷 Фотограф — портреты и репортажи"
    existing = next((a for a in await notion.spheres() if a["name"].lower() == name.lower()), None)
    if existing:
        if description:
            await notion.update_sphere(existing["id"], description)
            return f"✏️ Описание сферы «{existing['name']}» обновлено."
        return f"Сфера «{existing['name']}» уже есть."
    await notion.add_sphere(name, description)
    return f"✅ Сфера «{name}» добавлена. Привязать к ней проект: /projects → проект → «🗂 Сфера»."


async def addarea(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    raw = re.sub(r"^/addarea(@\w+)?", "", update.message.text, count=1)
    if not raw.strip():
        await update.message.reply_text(AREA_PROMPT, reply_markup=ForceReply(input_field_placeholder="📷 Фотограф — …"))
        return
    await update.message.reply_text(await _area_reply(update.message, raw))


async def areas_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    items = await notion.spheres()
    if not items:
        await update.message.reply_text("Сфер пока нет. Добавить: /addarea 📷 Фотограф — чем занимаюсь")
        return
    lines = [f"🗂 <b>{escape(a['name'])}</b>" + (f" — {escape(a['description'])}" if a["description"] else "") for a in items]
    await update.message.reply_text("Сферы деятельности:\n\n" + "\n".join(lines) + "\n\nОбновить описание: /addarea Название — новое описание", parse_mode=ParseMode.HTML)


# ---------- 🎭 стиль бота ----------
# Тон реплик у каждого свой: встроенные стили, «Эминемовна» (когда заполним профиль) или свой персонаж из Character.AI.
# callback_data: sy:<стиль> (выбрать) | sy:new (перенести своего персонажа) | sy:redo (переписать реплики) | sy:ok

PERSONA_STEPS = [
    ("name", "🧩 Шаг 1 из 4. Как зовут персонажа? Напишите ответом на это сообщение."),
    ("description", "🧩 Шаг 2 из 4. Характер и описание: скопируйте со страницы персонажа в Character.AI или опишите своими словами."),
    ("greeting", "🧩 Шаг 3 из 4. Приветствие персонажа — его первая реплика."),
    ("examples", "🧩 Шаг 4 из 4. 2–3 примера его реплик, каждая с новой строки."),
]
PERSONA_PROMPTS = {prompt: i for i, (_, prompt) in enumerate(PERSONA_STEPS)}
PERSONA_LIMIT = 3  # новых наборов реплик своего персонажа в день у участника (у владелицы без лимита)
_persona_drafts: dict[int, dict] = {}


async def _style_view(uid: int) -> tuple[str, InlineKeyboardMarkup]:
    st = await scheduler.get_settings(uid)
    current = st.get("style") or "standard"
    mine = await styles.persona(uid)
    sample = await styles.line(uid, "saved")
    text = (
        "🎭 Как мне с тобой разговаривать?\nСтиль меняет только мои реплики: заметки и кнопки остаются как есть.\n\n"
        f"Сейчас: {styles.style_name(current, mine and mine['name'])}" + (f"\nНапример: «{sample}»" if sample else "")
    )
    if is_owner(uid) and not styles.eminemovna_profile():
        text += f"\n\n{styles.EMINEMOVNA_NAME} появится здесь, когда заполним профиль в styles/eminemovna.md."
    buttons = [Btn(("✓ " if key == current else "") + v["name"], callback_data=f"sy:{key}") for key, v in styles.BUILTIN.items()]
    if styles.eminemovna_profile():
        mark = "✓ " if current == styles.EMINEMOVNA else ""
        buttons.append(Btn(mark + styles.EMINEMOVNA_NAME, callback_data=f"sy:{styles.EMINEMOVNA}"))
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    if mine:
        rows.append([Btn(("✓ " if current == styles.CUSTOM else "") + f"🧩 {mine['name']}", callback_data=f"sy:{styles.CUSTOM}")])
    rows.append([Btn("🧩 Другой персонаж" if mine else "🧩 Свой персонаж из Character.AI", callback_data="sy:new")])
    return text, InlineKeyboardMarkup(rows)


async def style_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = await _style_view(update.effective_user.id)
    await update.message.reply_text(text, reply_markup=kb)


async def _persona_allowed(message: Message) -> bool:
    """Не больше PERSONA_LIMIT наборов реплик в день: каждый — запрос к ИИ на токене владелицы."""
    uid = message.chat_id
    if is_owner(uid):
        return True
    key = f"persona_gen:{uid}:{datetime.now(timezone.utc):%Y-%m-%d}"
    used = int(await notion.get_value(key) or 0)
    if used >= PERSONA_LIMIT:
        await message.reply_text(f"⛔ Персонажа можно менять {PERSONA_LIMIT} раза в день, продолжим завтра.")
        return False
    await notion.set_value(key, str(used + 1))
    return True


async def _persona_sample(message: Message, p: dict) -> None:
    pack = p.get("pack") or {}
    lines = [styles._pick(pack, k) for k in ("greeting", "saved", "all_done")]
    text = f"🧩 Вот как я буду с тобой говорить в роли «{p['name']}»:\n\n" + "\n".join(f"— {x}" for x in lines if x)
    await message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [Btn("✅ Оставить", callback_data="sy:ok"), Btn("🔄 Переделать", callback_data="sy:redo")],
            [Btn("↩️ Обычный стиль", callback_data="sy:standard")],
        ]),  # fmt: skip
    )


async def _build_persona(message: Message, p: dict) -> None:
    uid = message.chat_id
    if not await _persona_allowed(message) or not await ai_allowed(message):
        return
    status = await message.reply_text(f"🧩 Пишу реплики персонажа «{p['name']}»…")
    try:
        p["pack"] = await styles.generate_pack(styles.persona_card(p))
    except PermissionError as e:
        await status.edit_text(f"🙅 {e}")
        return
    except Exception as e:
        log.exception("persona failed")
        await status.edit_text(f"❌ Не получилось: {e}"[:4000])
        return
    await styles.save_persona(uid, p)
    await scheduler.update_settings(uid, style=styles.CUSTOM)
    try:
        await status.delete()
    except Exception:
        pass
    await _persona_sample(message, p)


async def _persona_step(message: Message, prompt: str) -> None:
    uid = message.chat_id
    step = PERSONA_PROMPTS[prompt]
    draft = _persona_drafts.get(uid)
    if draft is None or step != len(draft):
        await message.reply_text("Что-то сбилось. Начнём заново: /style → «🧩 Свой персонаж».")
        _persona_drafts.pop(uid, None)
        return
    draft[PERSONA_STEPS[step][0]] = message.text.strip()[:3000]
    if step + 1 < len(PERSONA_STEPS):
        await message.reply_text(PERSONA_STEPS[step + 1][1], reply_markup=ForceReply())
        return
    _persona_drafts.pop(uid, None)
    await _build_persona(message, draft)


async def on_style_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    uid = q.from_user.id
    if not is_member(uid):
        await q.answer()
        return
    choice = q.data.split(":", 1)[1]
    try:
        if choice == "ok":
            await q.answer("Оставили ✓")
            await q.edit_message_reply_markup(None)
            return
        if choice == "new":
            await q.answer()
            _persona_drafts[uid] = {}
            await q.message.reply_text(
                "🧩 Перенесём персонажа к нам: я спрошу 4 вещи по очереди. Скопируйте их со страницы персонажа "
                "в Character.AI. Персонаж меняет только тон моих ответов.",
            )
            await q.message.reply_text(PERSONA_STEPS[0][1], reply_markup=ForceReply())
            return
        if choice == "redo":
            p = await styles.persona(uid)
            await q.answer()
            if not p:
                await q.message.reply_text("Персонажа нет. Создать: /style → «🧩 Свой персонаж».")
                return
            await q.edit_message_reply_markup(None)
            await _build_persona(q.message, p)
            return
        if choice in styles.BUILTIN or (choice == styles.EMINEMOVNA and styles.eminemovna_profile()):
            await scheduler.update_settings(uid, style=choice)
        elif choice == styles.CUSTOM and await styles.persona(uid):
            await scheduler.update_settings(uid, style=choice)
        else:
            await q.answer("Этот стиль недоступен")
            return
        await q.answer("Сохранено ✓")
        text, kb = await _style_view(uid)
    except Exception as e:
        log.exception("style button failed")
        await _fail(q, e)
        return
    await _edit(q, text, reply_markup=kb)


# ---------- 🎨 ИИ-иконки проектов ----------
# При создании проекта ИИ придумывает образ, генератор картинок рисует иконку в стиле из design/icon_style.md,
# бот сразу ставит её на страницу проекта в Notion и показывает: оставить, другой вариант или без иконки.
# callback_data: ic:<проект>:ok | ic:<проект>:new | ic:<проект>:no

_icon_attempts: dict[str, int] = {}


def _icon_markup(pid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [Btn("✅ Оставить", callback_data=f"ic:{pid}:ok"), Btn("🔄 Другой вариант", callback_data=f"ic:{pid}:new")],
        [Btn("✖️ Без иконки", callback_data=f"ic:{pid}:no")],
    ])  # fmt: skip


async def make_icon(message: Message, project: dict) -> None:
    if not await ai_allowed(message):
        return
    attempt = _icon_attempts.get(project["id"], 0)
    _icon_attempts[project["id"]] = attempt + 1
    status = await message.reply_text(f"🎨 Рисую иконку для «{project['name']}»…")
    try:
        prompt = await ai.icon_prompt(project["name"], await project_context(project["name"]), attempt)
        image = await ai.draw(prompt)
        upload = await notion.upload_file(image, "icon.png", "image/png")
        await notion.set_icon(project["id"], upload)
    except Exception as e:
        log.exception("icon failed")
        await status.edit_text(
            f"🎨 Иконку нарисовать не получилось: {e}"[:3500]
            + "\nПопробовать ещё раз: /projects → проект → «🎨 Иконка»."
        )
        return
    try:
        await status.delete()
    except Exception:
        pass
    await message.reply_photo(
        image, caption=f"🎨 Иконка «{project['name']}» уже стоит в Notion.", reply_markup=_icon_markup(project["id"])
    )


async def auto_icon(message: Message, project: dict) -> None:
    """Иконка сама при создании проекта, если не выключено переменной AUTO_ICONS=0."""
    if c.AUTO_ICONS:
        await make_icon(message, project)


async def on_icon_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    uid = q.from_user.id
    if not is_member(uid):
        await q.answer()
        return
    _, pid, action = q.data.split(":")
    try:
        project = await notion.get_project(pid)
        if not project or not notion.can_see(project, uid) or not _can_manage(uid, project):
            raise PermissionError("Иконку меняет автор проекта или владелица.")
        if action == "ok":
            await q.answer("Оставили ✓")
            await q.edit_message_reply_markup(None)
        elif action == "no":
            await notion.set_icon(project["id"], None)
            await q.answer("Иконка убрана")
            await q.edit_message_caption(f"✖️ «{project['name']}» без иконки.", reply_markup=None)
        else:
            await q.answer()
            if q.message.photo:
                await q.edit_message_reply_markup(None)
            await make_icon(q.message, project)
    except Exception as e:
        log.exception("icon button failed")
        await _fail(q, e)


# ---------- 👥 командные проекты ----------
# Проект, в который несколько участников вместе вносят заметки. Создаёт владелица (или все, если TEAM_PROJECTS_BY_MEMBERS=1).
# Участники видят заметки друг друга в ленте и сводке, удалять могут только свои.
# callback_data: tm:<проект> (выбор участников) | tm:<проект>:<tg> (добавить/убрать) | fd:<проект> (лента)
#                fv:<page_id> (заметка из ленты целиком) | fs:<проект> (ИИ-сводка сейчас)

TEAM_PROMPT = "👥 Как назовём командный проект? Напишите название ответом на это сообщение."
FEED_SIZE = 15


def can_create_team(uid: int) -> bool:
    return is_owner(uid) or c.TEAM_PROJECTS_BY_MEMBERS


async def _pinned_project(uid: int) -> dict | None:
    """Проект режима «📌 Писать в», если он ещё открыт и человеку виден."""
    pid = (await scheduler.get_settings(uid)).get("active_project")
    if not pid:
        return None
    project = await notion.get_project(pid)
    if not project or not notion.can_see(project, uid) or project["status"] == notion.STATUS_CLOSED:
        return None
    return project


async def notify_team(bot, project: dict, page_id: str, author_id: int) -> None:
    """Остальным участникам командного проекта — короткое сообщение о новой заметке (если не выключили)."""
    item = await notion.page_info(page_id)
    who = item.get("author") or "Участник"
    text = (
        f"👥 «{escape(project['name'])}»: новая заметка\n"
        f'✍️ {escape(who)}: <a href="{item["url"]}">{escape(item["title"])}</a>'
    )
    kb = InlineKeyboardMarkup([[Btn("👁 Открыть", callback_data=f"fv:{page_id}"), Btn("📰 Лента", callback_data=f"fd:{project['id']}")]])
    for uid in notion.team_people(project):
        if uid == author_id or not is_member(uid):
            continue
        try:
            if not (await scheduler.get_settings(uid)).get("team_notify", True):
                continue
            await bot.send_message(uid, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=kb)
        except Exception:
            log.exception("team notify %s failed", uid)


async def _file(bot, uid: int, page_id: str, name: str, skip_review: bool = False) -> dict:
    """Кладёт заметку в проект; в командном проекте сообщает остальным участникам."""
    project = await notion.file_to_project(page_id, name, skip_review)
    if project["kind"] == notion.KIND_TEAM:
        try:
            await notify_team(bot, project, page_id, uid)
        except Exception:
            log.exception("team notify failed")
    return project


async def can_read(uid: int, page_id: str) -> dict:
    """Читать заметку можно свою, а в командном проекте — заметки всех его участников. Владелица — любые."""
    item = await notion.page_info(page_id)
    if is_owner(uid) or item.get("author_id") == uid:
        return item
    project = await notion.get_project(item["project_id"]) if item.get("project_id") else None
    if project and notion.is_team_member(project, uid):
        return item
    raise PermissionError("Эта заметка вам недоступна.")


async def _team_members_view(project: dict) -> tuple[str, InlineKeyboardMarkup]:
    people = [m for m in await notion.members() if m["tg"] != project["creator"]]
    pid = project["id"]
    if people:
        text = f"👥 Кто в проекте «{project['name']}»? Нажмите на человека, чтобы добавить или убрать."
    else:
        text = "Участников бота пока нет. Пригласите их: /invite — и потом добавьте в проект здесь."
    rows = [
        [Btn(("✅ " if m["tg"] in project["members"] else "▫️ ") + m["name"], callback_data=f"tm:{pid}:{m['tg']}")]
        for m in people
    ]
    rows.append([Btn("✔️ Готово", callback_data=f"pr:{pid}")])
    return text, InlineKeyboardMarkup(rows)


async def _create_team(message: Message, raw: str) -> None:
    uid = message.chat_id
    name = raw.strip().splitlines()[0][:100] if raw.strip() else ""
    if not name:
        await message.reply_text("Напишите название, например: /teamproject Сайт студии")
        return
    if await notion.get_project(name):
        await message.reply_text(f"Проект «{name}» уже есть. Сделать его командным: /projects → проект → «👥 Командный».")
        return
    project = await notion.create_project(name, notion.KIND_TEAM, uid, [])
    text, kb = await _team_members_view(project)
    await message.reply_text(f"✅ Командный проект «{name}» создан.\n\n" + text, reply_markup=kb)
    await auto_icon(message, project)


async def teamproject_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not can_create_team(update.effective_user.id):
        await update.message.reply_text("Командные проекты пока создаёт только владелица бота.")
        return
    raw = re.sub(r"^/teamproject(@\w+)?", "", update.message.text, count=1)
    if not raw.strip():
        await update.message.reply_text(TEAM_PROMPT, reply_markup=ForceReply(input_field_placeholder="Сайт студии"))
        return
    await _create_team(update.message, raw)


async def _feed_view(uid: int, project: dict) -> tuple[str, InlineKeyboardMarkup]:
    items = await notion.notes_in_scope(uid, project=project["name"], limit=FEED_SIZE)
    pid = project["id"]
    if not items:
        text = f"📰 «{escape(project['name'])}»: заметок пока нет."
    else:
        lines = [
            f'{i}. <a href="{it["url"]}">{escape(it["title"])}</a> — {escape(it.get("author") or "—")}, {it["created"][8:10]}.{it["created"][5:7]}'
            for i, it in enumerate(items, 1)
        ]
        text = f"📰 <b>«{escape(project['name'])}»</b> — последние заметки:\n\n" + "\n".join(lines) + "\n\n👁 Номер — открыть заметку здесь."
    buttons = [Btn(str(i), callback_data=f"fv:{it['id']}") for i, it in enumerate(items[:10], 1)]
    rows = [buttons[i : i + 5] for i in range(0, len(buttons), 5)]
    if project["kind"] == notion.KIND_TEAM:
        rows.append([Btn("🤖 Сводка сейчас", callback_data=f"fs:{pid}")])
    rows.append([Btn("↩️ Карточка проекта", callback_data=f"pr:{pid}")])
    return text, InlineKeyboardMarkup(rows)


async def feed_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    raw = re.sub(r"^/feed(@\w+)?", "", update.message.text, count=1).strip()
    if raw:
        project = await notion.get_project(raw)
        if not project or not notion.can_see(project, uid):
            await update.message.reply_text(f"Проекта «{raw}» нет. Список: /projects")
            return
        text, kb = await _feed_view(uid, project)
        await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return
    projects = await notion.project_list(uid)
    projects.sort(key=lambda p: p["kind"] != notion.KIND_TEAM)  # командные первыми
    if not projects:
        await update.message.reply_text("Проектов пока нет: /addproject")
        return
    rows = [[Btn(("👥 " if p["kind"] == notion.KIND_TEAM else "📁 ") + p["name"], callback_data=f"fd:{p['id']}")] for p in projects]
    await update.message.reply_text("📰 Ленту какого проекта показать?", reply_markup=InlineKeyboardMarkup(rows))


async def _send_summary(message: Message, uid: int, project: dict) -> None:
    if not await ai_allowed(message):
        return
    status = await message.reply_text(f"📚 Читаю заметки «{project['name']}»…")
    try:
        text, count, truncated = await team.summary(project, uid)
    except Exception as e:
        log.exception("summary failed")
        await status.edit_text(f"❌ ИИ не ответил: {e}"[:4000])
        return
    if not text:
        await status.edit_text("🤷 В проекте пока нет заметок.")
        return
    parts = team.summary_messages(project, text, count, "Сводка")
    if truncated:
        parts[-1] += "\n\n⚠️ Заметок очень много, ИИ прочитал не все."
    for i, part in enumerate(parts):
        if i == 0:
            await status.edit_text(part, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        else:
            await message.reply_text(part, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def on_team_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    uid = q.from_user.id
    if not is_member(uid):
        await q.answer()
        return
    action, key, *rest = q.data.split(":")
    try:
        if action == "fv":
            await can_read(uid, key)
            await q.answer()
            await show_note(q.message, key)
            return
        project = await notion.get_project(key)
        if not project or not notion.can_see(project, uid):
            raise PermissionError("Этот проект недоступен.")
        if action == "fd":
            await q.answer()
            text, kb = await _feed_view(uid, project)
            await q.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            return
        if action == "fs":
            await q.answer()
            await _send_summary(q.message, uid, project)
            return
        # tm — участники проекта
        if not _can_manage(uid, project) or project["kind"] != notion.KIND_TEAM:
            raise PermissionError("Состав проекта меняет его автор или владелица.")
        if rest:
            tg = int(rest[0])
            if tg not in {m["tg"] for m in await notion.members()}:
                raise PermissionError("Такого участника нет.")
            members = [m for m in project["members"] if m != tg] if tg in project["members"] else project["members"] + [tg]
            await notion.update_project(project["id"], members=members)
            added = tg in members
            await q.answer("Добавлен(а) ✓" if added else "Убран(а) из проекта")
            if added:
                try:
                    await q.get_bot().send_message(
                        tg,
                        f"👥 Вас добавили в командный проект «{project['name']}».\n"
                        "Заметки туда: кнопка «📁 В проект» под заметкой, или «📌 Писать сюда» в карточке проекта — "
                        "тогда все новые заметки сразу попадают в него.",
                        reply_markup=InlineKeyboardMarkup([[Btn("📁 Открыть проект", callback_data=f"pr:{project['id']}")]]),
                    )
                except Exception:
                    log.exception("could not tell %s about team", tg)
            project = await notion.get_project(project["id"])
        else:
            await q.answer()
        text, kb = await _team_members_view(project)
    except Exception as e:
        log.exception("team button failed")
        await _fail(q, e)
        return
    await _edit(q, text, reply_markup=kb)


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
                await _edit(q, 
                    f'<a href="{item["url"]}">{escape(item["title"])}</a> → {escape(names[idx])} ✓\n\n'
                    "Раскрыть заметку под этот тип? ИИ задаст несколько вопросов по одному и соберёт подробное описание.",
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=InlineKeyboardMarkup(
                        [[Btn("✨ Раскрыть", callback_data=f"x:{page_id}:{k}"), Btn("⏭ Следующая", callback_data=f"r:{k}")]]
                        + (
                            [[Btn("⏰ Поставить срок", callback_data=f"w:{page_id}:{k}")]]
                            if not item.get("when") and re.search(r"напомин|событ|задач", names[idx].lower())
                            else []
                        )
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
            projects = await notion.projects(q.from_user.id)
            if idx >= len(projects):
                await q.answer("Список проектов изменился, попробуйте ещё раз")
            else:
                await _file(q.get_bot(), q.from_user.id, page_id, projects[idx])
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
    await _edit(q, text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# ---------- запуск ----------

COMMANDS = [
    ("start", "Меню всех команд"),
    ("razbor", "Разобрать входящие"),
    ("projects", "Список проектов"),
    ("addproject", "Добавить проект"),
    ("feed", "Лента заметок проекта"),
    ("ask", "Спросить ИИ по заметкам"),
    ("settings", "Часовой пояс и время разбора"),
    ("style", "Стиль общения бота"),
    ("schedule", "Расписание и PDF"),
    ("report", "Недельный отчёт"),
    ("types", "Типы записей"),
    ("areas", "Сферы деятельности"),
    ("addtype", "Добавить тип"),
]
OWNER_EXTRA = [
    ("invite", "Пригласить участника"),
    ("members", "Участники и приглашения"),
    ("teamproject", "Командный проект"),
    ("banners", "Картинки разделов"),
]


async def set_commands(app: Application) -> None:
    """Меню «/» в Telegram (у владелицы в нём больше команд) и список участников из Notion."""
    await app.bot.set_my_commands(COMMANDS)
    if c.OWNER_ID:
        await app.bot.set_my_commands(COMMANDS + OWNER_EXTRA, scope=BotCommandScopeChat(c.OWNER_ID))
    try:
        log.info("projects: %s", await notion.ensure_projects())
    except Exception:
        log.exception("could not prepare projects")
    try:
        member.add_user_ids([m["tg"] for m in await notion.members()])
    except Exception:
        # Без списка участников бот всё равно работает для владелицы
        log.exception("could not load members")


async def _announce_tick_link(app: Application) -> None:
    """Один раз присылает владелице ссылку планировщика для cron-job.org: команды, которая её показывает, нет."""
    try:
        if await notion.get_value("announced:tick") is not None:
            return
        url = f"{c.WEBHOOK_BASE.rstrip('/')}/tick/{c.secret('tick')}"
        await app.bot.send_message(
            c.OWNER_ID,
            "⏱ Новый планировщик готов. Одна настройка в cron-job.org:\n\n"
            f"1. Создайте задачу с адресом:\n{url}\n"
            "2. Расписание: каждые 5 минут (Every 5 minutes).\n"
            "3. Две старые задачи на 20:00 и 20:03 удалите.\n\n"
            "Ссылку никому не показывайте, это сообщение можно удалить после настройки. "
            "Время разбора и часовой пояс теперь меняются в /settings.",
            disable_web_page_preview=True,
        )
        await notion.set_value("announced:tick", datetime.now(timezone.utc).isoformat())
    except Exception:
        log.exception("could not announce tick link")


async def serve(app: Application) -> None:
    """Свой веб-сервер вместо run_webhook: кроме Telegram он принимает вечерний пинг от cron-job.org."""
    webhook_secret = c.secret("webhook")
    last_force = [-1e9]

    async def telegram(request: web.Request) -> web.Response:
        if not secrets.compare_digest(request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), webhook_secret):
            return web.Response(status=403)
        await app.update_queue.put(Update.de_json(await request.json(), app.bot))
        return web.Response()

    async def tick_hook(request: web.Request) -> web.Response:
        # /tick/<секрет> — планировщик; старая ссылка /remind/<секрет> работает так же, пока её не заменят в cron-job.org
        purpose = "tick" if request.path.startswith("/tick/") else "remind"
        if not secrets.compare_digest(request.match_info["secret"], c.secret(purpose)):
            return web.Response(status=404)
        force = request.query.get("force")
        if force:
            # Ручная проверка — только для владелицы и не чаще раза в минуту
            now = asyncio.get_running_loop().time()
            if now - last_force[0] < 60:
                return web.Response(status=429, text="Не чаще раза в минуту")
            last_force[0] = now
        users = [c.OWNER_ID] if force else sorted(member.user_ids)
        try:
            result = await scheduler.tick(app.bot, users, force="evening" if force else None)
        except Exception as e:
            log.exception("tick failed")
            return web.Response(status=500, text=str(e))
        return web.Response(text=result)

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    server = web.Application()
    server.add_routes(
        [
            web.post("/telegram", telegram),
            web.get("/tick/{secret}", tick_hook),
            web.post("/tick/{secret}", tick_hook),
            web.get("/remind/{secret}", tick_hook),
            web.post("/remind/{secret}", tick_hook),
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
        await _announce_tick_link(app)
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
    app.add_handler(CommandHandler("teamproject", teamproject_cmd, filters=member))
    app.add_handler(CommandHandler("feed", feed_cmd, filters=member))
    app.add_handler(CallbackQueryHandler(on_team_button, pattern=r"^(tm|fd|fv|fs):"))
    app.add_handler(CallbackQueryHandler(on_icon_button, pattern=r"^ic:"))
    app.add_handler(CommandHandler("style", style_cmd, filters=member))
    app.add_handler(CommandHandler("banners", banners_cmd, filters=owner))
    app.add_handler(CallbackQueryHandler(on_banner_button, pattern=r"^bn:"))
    app.add_handler(CallbackQueryHandler(on_style_button, pattern=r"^sy:"))
    app.add_handler(CommandHandler("settings", settings_cmd, filters=member))
    app.add_handler(CommandHandler("types", types_cmd, filters=member))
    app.add_handler(CommandHandler("addtype", addtype, filters=member))
    app.add_handler(CallbackQueryHandler(on_type_button, pattern=r"^t[akl]"))
    app.add_handler(CallbackQueryHandler(on_when_button, pattern=r"^w[vcx]?:"))
    app.add_handler(CallbackQueryHandler(on_deadline_button, pattern=r"^(dl:|ov$)"))
    app.add_handler(CallbackQueryHandler(on_checkin_button, pattern=r"^ci:"))
    app.add_handler(CommandHandler("schedule", schedule_cmd, filters=member))
    app.add_handler(CommandHandler("report", report_cmd, filters=member))
    app.add_handler(CallbackQueryHandler(on_mood_button, pattern=r"^md:"))
    app.add_handler(CallbackQueryHandler(on_report_button, pattern=r"^rp:"))
    app.add_handler(CallbackQueryHandler(on_schedule_button, pattern=r"^(sc:|sca$|scx$)"))
    app.add_handler(CallbackQueryHandler(on_quick_button, pattern=r"^k[qan]"))
    app.add_handler(CallbackQueryHandler(on_batch_button, pattern=r"^b(t|a|p|pp|y|yy|d|dd|r|x|b)(:|$)"))
    app.add_handler(CallbackQueryHandler(on_settings_button, pattern=r"^st:"))
    app.add_handler(CommandHandler("invite", invite_cmd, filters=owner))
    app.add_handler(CommandHandler("members", members_cmd, filters=owner))
    app.add_handler(CallbackQueryHandler(on_member_button, pattern=r"^m[rkils]"))
    app.add_handler(CallbackQueryHandler(on_scope_button, pattern=r"^q:"))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(r|t|x|pj|p|d|v):"))
    app.add_handler(CallbackQueryHandler(on_expand_button, pattern=r"^x[sc]:"))
    app.add_handler(CallbackQueryHandler(on_interview_button, pattern=r"^xi:"))
    app.add_handler(CallbackQueryHandler(lambda u, _: u.callback_query.answer(), pattern=r"^noop$"))
    app.add_handler(CallbackQueryHandler(on_panel, pattern=r"^n[px]?:"))
    app.add_handler(CallbackQueryHandler(on_ai_button, pattern=r"^aq?:"))
    app.add_handler(CallbackQueryHandler(on_project_button, pattern=r"^(pr:|ps:|pl$|plc$)"))
    app.add_handler(CommandHandler("addarea", addarea, filters=member))
    app.add_handler(CommandHandler("areas", areas_cmd, filters=member))
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
