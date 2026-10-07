"""🎨 ИИ-студия дизайна, этап 13. Первым — «Арт-директор»: палитра референса в HEX (считается по пикселям, без ИИ),
карточка палитры картинкой и разбор света, цвета и ретуши (ИИ по фото). Боты «Логотипы», «Бэкграунды», «Текстуры»
появятся вместе с моделью в её стиле (LoRA) — той же, что для иконок проектов."""

import io

from PIL import Image, ImageDraw

from .banners import TOKENS, _font

CARD_SIZE = (1200, 420)


def _rgb(hex_color: str) -> tuple[int, int, int]:
    return tuple(int(hex_color[i : i + 2], 16) for i in (1, 3, 5))


def palette(data: bytes, colors: int = 6) -> list[tuple[str, float]]:
    """Основные цвета картинки: [(HEX, доля площади)], от самого частого. Почти одинаковые оттенки склеиваются."""
    image = Image.open(io.BytesIO(data)).convert("RGB")
    image.thumbnail((256, 256))
    quantized = image.quantize(colors=colors * 2, method=Image.Quantize.MEDIANCUT)
    rgb = quantized.getpalette()
    counts = sorted(quantized.getcolors() or [], reverse=True)
    total = sum(n for n, _ in counts) or 1
    merged: list[list] = []  # [HEX, доля]
    for n, index in counts:
        r, g, b = rgb[index * 3 : index * 3 + 3]
        near = next((m for m in merged if sum((a - b2) ** 2 for a, b2 in zip(_rgb(m[0]), (r, g, b))) < 30**2), None)
        if near:
            near[1] += n / total
        else:
            merged.append([f"#{r:02X}{g:02X}{b:02X}", n / total])
    merged.sort(key=lambda m: -m[1])
    return [(h, share) for h, share in merged[:colors]]


def _ink(hex_color: str) -> str:
    """Тёмный или светлый текст поверх цвета — что читается лучше."""
    r, g, b = _rgb(hex_color)
    return "#111111" if 0.299 * r + 0.587 * g + 0.114 * b > 150 else "#FFFFFF"


def palette_card(colors: list[tuple[str, float]]) -> bytes:
    """Карточка палитры: полосы цветов с HEX и долей, в цветах и шрифте дизайн-системы."""
    tokens = TOKENS["colors"]
    image = Image.new("RGB", CARD_SIZE, tokens["background"])
    draw = ImageDraw.Draw(image)
    draw.text((40, 28), "Палитра референса", font=_font("bold", 40), fill=tokens["text"])
    x, top, bottom, width = 40, 100, 380, CARD_SIZE[0] - 80
    step = width / max(len(colors), 1)
    for i, (hex_color, share) in enumerate(colors):
        left = x + i * step
        draw.rectangle((left, top, left + step - 8, bottom), fill=hex_color)
        ink = _ink(hex_color)
        draw.text((left + 16, bottom - 70), hex_color, font=_font("bold", 30), fill=ink)
        draw.text((left + 16, bottom - 34), f"{share:.0%}", font=_font("regular", 24), fill=ink)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def palette_markdown(colors: list[tuple[str, float]]) -> str:
    return "\n".join(f"- `{h}` — {share:.0%}" for h, share in colors)
