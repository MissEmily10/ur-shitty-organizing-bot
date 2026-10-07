"""Прогон настоящих апдейтов Telegram через обработчики бота. Сеть подменена: запросы к Telegram записываются,
Notion и ИИ подменяются в самих тестах."""

import itertools
import json
import os

os.environ.setdefault("TELEGRAM_TOKEN", "1:TEST")
os.environ.setdefault("OWNER_ID", "42")
os.environ.setdefault("HF_TOKEN", "x")
os.environ.setdefault("NOTION_TOKEN", "x")

from telegram import Update  # noqa: E402
from telegram.ext import ExtBot  # noqa: E402

from bot import main  # noqa: E402

OWNER = {"id": 42, "is_bot": False, "first_name": "Owner"}
CHAT = {"id": 42, "type": "private"}
BOT_USER = {"id": 999, "is_bot": True, "first_name": "Bot", "username": "test_bot"}
_ids = itertools.count(1000)
sent: list[tuple[str, dict]] = []


def _message(text: str, **extra) -> dict:
    return {"message_id": next(_ids), "date": 0, "chat": CHAT, "from": BOT_USER, "text": text, **extra}


def _message_for(data: dict) -> dict:
    chat_id = int(data.get("chat_id") or CHAT["id"])
    return _message(data.get("text", ""), chat={"id": chat_id, "type": "private"})


async def _fake_post(self, endpoint, data=None, *args, **kwargs):
    data = {k: (json.loads(v) if isinstance(v, str) and v[:1] in "[{" else v) for k, v in (data or {}).items()}
    sent.append((endpoint, data))
    if endpoint == "getMe":
        return BOT_USER
    if endpoint in ("sendMessage", "editMessageText"):
        msg = _message_for(data)
        if "message_id" in data:
            msg["message_id"] = data["message_id"]
        return msg
    if endpoint in ("sendDocument", "sendPhoto"):
        return _message_for({**data, "text": ""})
    if endpoint == "editMessageReplyMarkup":
        return _message("", message_id=data.get("message_id", next(_ids)))
    return True


ExtBot._do_post = _fake_post

# Служебные значения (настройки людей, отметки планировщика) и типы — в памяти, без Notion
from bot import notion as _notion  # noqa: E402

service: dict[str, str] = {}
TYPES = ["💡 Идея", "📋 Задача", "⚡ Быстрая заметка", "⏰ Напоминание", "🎨 Референс", "❓ Обсудить", "📅 Событие"]


async def _get_value(key):
    return service.get(key)


async def _set_value(key, value):
    service[key] = value


async def _types():
    return list(TYPES)


moods: dict[tuple[int, str], list] = {}


async def _set_mood(uid, day, score=None, comment=None):
    row = moods.setdefault((uid, day), [None, ""])
    if score is not None:
        row[0] = score
    if comment is not None:
        row[1] = comment


async def _moods(uid):
    return {day: tuple(v) for (u, day), v in moods.items() if u == uid}


_notion.get_value, _notion.set_value, _notion.types = _get_value, _set_value, _types
_notion.set_mood, _notion.moods = _set_mood, _moods

# Проекты и сферы по умолчанию пустые: тесты, которым они нужны, подменяют _load_projects и spheres сами
async def _no_projects(force=False):
    return []


async def _no_spheres():
    return []


REAL = {"_load_projects": _notion._load_projects, "spheres": _notion.spheres}
_notion._load_projects, _notion.spheres = _no_projects, _no_spheres


APP = None


async def make_app():
    global APP
    APP = main.build_app(webhook=True)
    await APP.initialize()
    return APP


def callback(data: str, message_text: str = "карточка", user: dict | None = None) -> Update:
    user = user or OWNER
    msg = _message(message_text, chat={"id": user["id"], "type": "private"})
    return Update.de_json(
        {"update_id": next(_ids), "callback_query": {"id": str(next(_ids)), "from": user, "chat_instance": "c", "data": data, "message": msg}},
        APP.bot,
    )


def person(uid: int, name: str, username: str | None = None) -> dict:
    return {"id": uid, "is_bot": False, "first_name": name, **({"username": username} if username else {})}


def text(value: str, reply_to: dict | None = None, user: dict | None = None) -> Update:
    user = user or OWNER
    chat = {"id": user["id"], "type": "private"}
    entities = [{"type": "bot_command", "offset": 0, "length": len(value.split()[0])}] if value.startswith("/") else []
    msg = {"message_id": next(_ids), "date": 0, "chat": chat, "from": user, "text": value, "entities": entities}
    if reply_to:
        msg["reply_to_message"] = reply_to
    return Update.de_json({"update_id": next(_ids), "message": msg}, APP.bot)


def texts(endpoint: str | None = None, chat: int | None = None) -> list[str]:
    return [
        d.get("text", "")
        for e, d in sent
        if endpoint in (None, e) and "text" in d and (chat is None or int(d.get("chat_id", 0) or 0) == chat)
    ]
