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


async def _fake_post(self, endpoint, data=None, *args, **kwargs):
    data = {k: (json.loads(v) if isinstance(v, str) and v[:1] in "[{" else v) for k, v in (data or {}).items()}
    sent.append((endpoint, data))
    if endpoint == "getMe":
        return BOT_USER
    if endpoint in ("sendMessage", "editMessageText"):
        return _message(data.get("text", ""), **({"message_id": data["message_id"]} if "message_id" in data else {}))
    if endpoint == "editMessageReplyMarkup":
        return _message("", message_id=data.get("message_id", next(_ids)))
    return True


ExtBot._do_post = _fake_post


APP = None


async def make_app():
    global APP
    APP = main.build_app(webhook=True)
    await APP.initialize()
    return APP


def callback(data: str, message_text: str = "карточка") -> Update:
    msg = _message(message_text)
    return Update.de_json(
        {"update_id": next(_ids), "callback_query": {"id": str(next(_ids)), "from": OWNER, "chat_instance": "c", "data": data, "message": msg}},
        APP.bot,
    )


def text(value: str, reply_to: dict | None = None) -> Update:
    msg = {"message_id": next(_ids), "date": 0, "chat": CHAT, "from": OWNER, "text": value}
    if reply_to:
        msg["reply_to_message"] = reply_to
    return Update.de_json({"update_id": next(_ids), "message": msg}, APP.bot)


def texts(endpoint: str | None = None) -> list[str]:
    return [d.get("text", "") for e, d in sent if endpoint in (None, e) and "text" in d]
