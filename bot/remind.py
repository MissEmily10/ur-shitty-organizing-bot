"""Вечерний пуш с приглашением в разбор. Его дёргает cron-job.org по секретной ссылке /remind/<секрет>."""

from datetime import datetime, timezone

from telegram import Bot
from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup

from . import config as c
from . import notion

_last_sent = None


async def send(bot: Bot, force: bool = False) -> str:
    """Шлёт пуш не чаще раза в день: cron стучится дважды, чтобы первый запрос разбудил уснувший Render."""
    global _last_sent
    today = datetime.now(timezone.utc).date()
    if _last_sent == today and not force:
        return "Сегодня уже проверяли"
    items = await notion.review_items()
    if not force:  # ручная проверка не должна отменять вечерний пуш
        _last_sent = today
    if not items:
        return "Разбирать нечего"
    text = f"🌙 Вечерний разбор: заметок без категории {len(items)}"
    await bot.send_message(
        c.OWNER_ID, text, reply_markup=InlineKeyboardMarkup([[Btn("Разобрать", callback_data="r:0")]])
    )
    return f"Пуш отправлен: {text}"
