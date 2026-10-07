"""🖼 Баннеры разделов для меню с картинками.

Откуда берётся картинка раздела, по порядку:
1. design/assets/banner_<раздел>.png — дизайн владелицы из Figma (1280 × 640);
2. баннер, нарисованный ИИ в её стиле по команде /banners (хранится как file_id Telegram в «Служебном»);
3. временная заглушка: Pillow рисует название раздела цветами из design/tokens.json."""

import io
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import ai, notion

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "design" / "assets"
TOKENS = json.loads((ROOT / "design" / "tokens.json").read_text(encoding="utf-8"))
SIZE = (1280, 640)

SECTIONS = {
    "menu": "Меню",
    "razbor": "Разбор",
    "projects": "Проекты",
    "ask": "Спросить ИИ",
    "schedule": "Расписание",
    "settings": "Настройки",
    "report": "Отчёт",
    "checkin": "Чек-ин",
    "evening": "Вечерний разбор",
}
SUBTITLES = {
    "menu": "твои мысли — в Notion",
    "razbor": "что это и куда положить",
    "projects": "всё по полочкам",
    "ask": "ответы по твоим заметкам",
    "schedule": "неделя и месяц",
    "settings": "под тебя",
    "report": "как прошла неделя",
    "checkin": "минутка на заметки",
    "evening": "закрываем день",
}

BANNER_PROMPT = (
    "wide website banner, 2:1, abstract composition on the theme «{theme}», no text, no letters, "
    "lots of calm empty space on the left for a caption, {style}"
)

_cache: dict[str, str] = {}  # раздел → file_id уже отправленной картинки (повторно не загружаем)


def _font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(ROOT / TOKENS["fonts"][kind]), size)


def placeholder(section: str) -> bytes:
    """Временная картинка: название и подпись раздела на фоне из цветов дизайн-системы."""
    colors = TOKENS["colors"]
    image = Image.new("RGB", SIZE, colors["background"])
    draw = ImageDraw.Draw(image)
    accents = [colors["regular"], colors["once"], colors["note"], colors["today"]]
    accent = accents[sum(map(ord, section)) % len(accents)]
    # Спокойная декоративная композиция справа: круги из акцентного цвета
    draw.ellipse((820, -120, 1400, 460), fill=accent)
    draw.ellipse((960, 320, 1260, 620), fill=colors["header"])
    for x in range(80, 760, 40):
        draw.line((x, 560, x + 20, 560), fill=colors["grid"], width=4)
    draw.text((80, 210), SECTIONS.get(section, section), font=_font("bold", 96), fill=colors["text"])
    draw.text((84, 340), SUBTITLES.get(section, ""), font=_font("regular", 44), fill=colors["muted"])
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


async def get(section: str) -> str | bytes:
    """file_id или PNG баннера раздела."""
    path = ASSETS / f"banner_{section}.png"
    if path.exists():
        return _cache.get(section) or path.read_bytes()
    stored = await notion.get_value(f"banner:{section}")
    if stored:
        return stored
    return _cache.get(section) or placeholder(section)


def remember(section: str, message) -> None:
    """После отправки запоминаем file_id, чтобы не загружать ту же картинку снова."""
    photo = getattr(message, "photo", None)
    if photo:
        _cache[section] = photo[-1].file_id


async def draw(section: str) -> bytes:
    """Баннер раздела, нарисованный ИИ в стиле иконок (design/icon_style.md)."""
    theme = f"{SECTIONS[section]} — {SUBTITLES[section]}"
    return await ai.draw(BANNER_PROMPT.format(theme=theme, style=ai.icon_style()), size=SIZE)


async def save(section: str, file_id: str | None) -> None:
    """Оставить нарисованный ИИ баннер (file_id из Telegram) или вернуть заглушку (None)."""
    await notion.set_value(f"banner:{section}", file_id or "")
    _cache.pop(section, None)
