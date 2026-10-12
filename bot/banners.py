"""🖼 Баннеры разделов для меню с картинками.

Откуда берётся картинка раздела, по порядку:
1. design/assets/styles/<стиль>/banner_<раздел>.png — баннер под стиль общения человека (кавай, пират…);
2. design/assets/banner_<раздел>.png — дизайн владелицы из Figma (1280 × 640);
3. баннер, нарисованный ИИ в её стиле по команде /banners (хранится как file_id Telegram в «Служебном»);
4. временная заглушка: Pillow рисует название раздела цветами из design/tokens.json, у стилей — своими акцентами.
Выкройка для рисования баннеров — template_png() → design/templates/banner_template.png."""

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
    "notes": "Все заметки",
    "spheres": "Сферы",
    "types": "Типы",
    "ai": "ИИ",
    "plan": "Планы",
    "help": "Навигация",
    "team": "Команда",
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
    "notes": "всё, что пришло в голову",
    "spheres": "сфера, проект, заметка",
    "types": "идея, задача, референс",
    "ai": "спроси, разбери, придумай",
    "plan": "расписание и отчёты",
    "help": "как здесь всё устроено",
    "team": "вместе с участниками",
}

BANNER_PROMPT = (
    "wide website banner, 2:1, abstract composition on the theme «{theme}», no text, no letters, "
    "lots of calm empty space on the left for a caption, {style}"
)

_cache: dict[str, str] = {}  # «стиль:раздел» → file_id уже отправленной картинки (повторно не загружаем)

# Акценты заглушек под стили общения (styles.BUILTIN): пока нет баннеров из Figma, стиль видно хотя бы по цвету
STYLE_LOOKS = {
    "formal": ("Формальный", ["#DDE3EC", "#C9D3E2"]),
    "kawaii": ("Кавай-тян", ["#FFD6E8", "#FBE3FF"]),
    "coach": ("Коуч", ["#FFE1C2", "#FFF0B8"]),
    "sarcastic": ("Саркастичный друг", ["#E6DDFB", "#D9F2EC"]),
    "butler": ("Дворецкий", ["#E4EADF", "#F3E8CF"]),
    "pirate": ("Пират", ["#D3E7F5", "#F1E2C6"]),
    "eminemovna": ("Эминемовна", ["#F2E0D6", "#E0E8F0"]),
    "custom": ("Свой персонаж", ["#E3F0E8", "#F0E3EC"]),
}


def _font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(ROOT / TOKENS["fonts"][kind]), size)


def placeholder(section: str, style: str | None = None) -> bytes:
    """Временная картинка: название и подпись раздела на фоне из цветов дизайн-системы (у стиля — свои акценты)."""
    colors = TOKENS["colors"]
    image = Image.new("RGB", SIZE, colors["background"])
    draw = ImageDraw.Draw(image)
    look = STYLE_LOOKS.get(style or "")
    accents = look[1] if look else [colors["regular"], colors["once"], colors["note"], colors["today"]]
    accent = accents[sum(map(ord, section)) % len(accents)]
    # Спокойная декоративная композиция справа: круги из акцентного цвета
    draw.ellipse((820, -120, 1400, 460), fill=accent)
    draw.ellipse((960, 320, 1260, 620), fill=accents[-1] if look else colors["header"])
    if look:
        draw.text((84, 120), f"стиль: {look[0]}", font=_font("regular", 30), fill=colors["muted"])
    for x in range(80, 760, 40):
        draw.line((x, 560, x + 20, 560), fill=colors["grid"], width=4)
    draw.text((80, 210), SECTIONS.get(section, section), font=_font("bold", 96), fill=colors["text"])
    draw.text((84, 340), SUBTITLES.get(section, ""), font=_font("regular", 44), fill=colors["muted"])
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _style_key(style: str | None) -> str:
    return style if style in STYLE_LOOKS else ""


async def get(section: str, style: str | None = None) -> str | bytes:
    """file_id или PNG баннера раздела для стиля общения человека."""
    style = _style_key(style)
    key = f"{style}:{section}"
    if style:
        path = ASSETS / "styles" / style / f"banner_{section}.png"
        if path.exists():
            return _cache.get(key) or path.read_bytes()
    path = ASSETS / f"banner_{section}.png"
    if path.exists():
        return _cache.get(key) or path.read_bytes()
    if not style:
        stored = await notion.get_value(f"banner:{section}")
        if stored:
            return stored
    return _cache.get(key) or placeholder(section, style)


def remember(section: str, message, style: str | None = None) -> None:
    """После отправки запоминаем file_id, чтобы не загружать ту же картинку снова."""
    photo = getattr(message, "photo", None)
    if photo:
        _cache[f"{_style_key(style)}:{section}"] = photo[-1].file_id


def template_png() -> bytes:
    """Выкройка баннера 1280 × 640: поля, зона названия, зона иллюстрации и пояснения — чтобы рисовать в Figma."""
    colors = TOKENS["colors"]
    image = Image.new("RGB", SIZE, "#FFFFFF")
    d = ImageDraw.Draw(image)
    margin = 64
    # безопасная зона: Telegram может подрезать края превью
    d.rectangle((0, 0, SIZE[0] - 1, SIZE[1] - 1), outline="#B0B0B8", width=2)
    d.rectangle((margin, margin, SIZE[0] - margin, SIZE[1] - margin), outline="#4C7DFF", width=3)
    # зона названия (слева) и иллюстрации/персонажа (справа)
    d.rectangle((margin, 180, 720, 460), fill="#EEF3FF", outline="#4C7DFF", width=2)
    d.rectangle((760, margin, SIZE[0] - margin, SIZE[1] - margin), fill="#FFF4D6", outline="#D4B22C", width=2)
    small, mid, big = _font("regular", 22), _font("bold", 30), _font("bold", 56)
    d.text((margin + 8, 20), "БАННЕР РАЗДЕЛА · 1280 × 640 px · PNG · поля 64 px (синяя рамка — безопасная зона)", font=small, fill=colors["muted"])
    d.text((margin + 24, 200), "Название раздела", font=big, fill=colors["text"])
    d.text((margin + 24, 280), "подпись (необязательно)", font=mid, fill=colors["muted"])
    d.text((margin + 24, 340), "Текст можно не писать: бот ставит\nподпись и кнопки под картинкой.", font=small, fill=colors["muted"])
    d.text((780, 90), "Иллюстрация / персонаж стиля", font=mid, fill="#8A6D00")
    d.text((780, 140), "кавай-тян, пират, дворецкий…\nили символ раздела", font=small, fill="#8A6D00")
    d.text((margin + 8, SIZE[1] - 50), "Файл: design/assets/styles/<стиль>/banner_<раздел>.png  ·  общий: design/assets/banner_<раздел>.png",
           font=small, fill=colors["muted"])
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


async def draw(section: str) -> bytes:
    """Баннер раздела, нарисованный ИИ в стиле иконок (design/icon_style.md)."""
    theme = f"{SECTIONS[section]} — {SUBTITLES[section]}"
    return await ai.draw(BANNER_PROMPT.format(theme=theme, style=ai.icon_style()), size=SIZE)


async def save(section: str, file_id: str | None) -> None:
    """Оставить нарисованный ИИ баннер (file_id из Telegram) или вернуть заглушку (None)."""
    await notion.set_value(f"banner:{section}", file_id or "")
    _cache.pop(section, None)
