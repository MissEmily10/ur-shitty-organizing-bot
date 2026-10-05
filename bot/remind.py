"""Вечерний пуш «У тебя есть неразобранное». Запускается по расписанию из GitHub Actions."""

import asyncio

import httpx

from . import config as c
from . import notion


async def main() -> None:
    c.require("TELEGRAM_TOKEN", "OWNER_ID", "NOTION_TOKEN", "NOTION_DATABASE_ID")
    count = len(await notion.unsorted())
    if not count:
        print("Неразобранного нет, пуш не нужен")
        return
    r = httpx.post(
        f"https://api.telegram.org/bot{c.TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id": c.OWNER_ID,
            "text": f"🌙 У тебя есть неразобранное: {count}",
            "reply_markup": {"inline_keyboard": [[{"text": "Разобрать", "callback_data": "r:0"}]]},
        },
        timeout=30,
    )
    r.raise_for_status()
    print(f"Пуш отправлен, неразобранных: {count}")


if __name__ == "__main__":
    asyncio.run(main())
