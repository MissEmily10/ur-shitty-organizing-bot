"""Вечерний пуш с приглашением в разбор. Его дёргает cron-job.org по секретной ссылке /remind/<секрет>."""

from datetime import datetime, timezone

from telegram import Bot
from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup

from . import notion

_last_sent = None


async def send(bot: Bot, user_ids: list[int], force: bool = False) -> str:
    """Каждому участнику — пуш о его неразобранном. Не чаще раза в день: cron стучится дважды,
    чтобы первый запрос разбудил уснувший Render."""
    global _last_sent
    today = datetime.now(timezone.utc).date()
    if _last_sent == today and not force:
        return "Сегодня уже проверяли"
    if not force:  # ручная проверка не должна отменять вечерний пуш
        _last_sent = today
    report = []
    for uid in user_ids:
        try:
            items = await notion.review_items(uid)
            if items:
                await bot.send_message(
                    uid,
                    f"🌙 Вечерний разбор: заметок без категории {len(items)}",
                    reply_markup=InlineKeyboardMarkup([[Btn("Разобрать", callback_data="r:0")]]),
                )
            report.append(f"{uid}: {len(items)}")
        except Exception as e:
            # Один заблокировавший бота участник не должен оставить остальных без пуша
            report.append(f"{uid}: ошибка {e}")
    return "Пуш: " + ", ".join(report)
