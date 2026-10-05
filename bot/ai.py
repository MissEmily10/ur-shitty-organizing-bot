import base64
import re
from dataclasses import dataclass, field

from huggingface_hub import AsyncInferenceClient

from . import config as c

PROMPT = """Ты личный ассистент, который разбирает хаотичные заметки (текст, расшифровку голосового или фото страницы блокнота).
Ничего не выдумывай. Пиши на языке оригинала.

Ответь строго в таком формате, с этими четырьмя заголовками и без пояснений:

===НАЗВАНИЕ===
короткое название, до 8 слов
===ТЕГИ===
1-3 тега строчными буквами через запятую, например: идея, задача, дизайн
===СУТЬ===
главная тема и ключевые мысли в Markdown: 2-6 пунктов списком "- ", задачи как "- [ ] ", при необходимости заголовки "## "
===ДЕТАЛИ===
{details}"""

DETAILS_PHOTO = """ВСЁ, что есть на фото, максимально полно и дословно, в Markdown. Ничего не сокращай и не обобщай:
каждая строка и пункт, все числа, даты, имена, цены, ссылки, пометки на полях, зачёркнутое, стрелки и связи между блоками.
Сохраняй порядок и структуру страницы. Рисунки, схемы, скетчи и цвета опиши словами: что изображено, где на странице, что подписано.
Неразборчивое слово помечай как [неразборчиво]."""

DETAILS_TEXT = "оставь пустым"

SECTIONS = ("НАЗВАНИЕ", "ТЕГИ", "СУТЬ", "ДЕТАЛИ")


@dataclass
class Idea:
    title: str
    summary: str
    details: str = ""
    tags: list[str] = field(default_factory=list)


def _client() -> AsyncInferenceClient:
    return AsyncInferenceClient(api_key=c.HF_TOKEN)


async def transcribe(audio: bytes) -> str:
    result = await _client().automatic_speech_recognition(audio, model=c.ASR_MODEL)
    return result.text.strip()


def _parse(raw: str, fallback: str) -> Idea:
    parts = dict.fromkeys(SECTIONS, "")
    for m in re.finditer(r"===\s*(НАЗВАНИЕ|ТЕГИ|СУТЬ|ДЕТАЛИ)\s*===(.*?)(?====\s*(?:НАЗВАНИЕ|ТЕГИ|СУТЬ|ДЕТАЛИ)\s*===|\Z)", raw, re.S):
        parts[m.group(1)] = m.group(2).strip()
    if not parts["СУТЬ"] and not parts["ДЕТАЛИ"]:
        # Модель ответила не по формату: ничего не теряем, всё уходит в детали
        parts["ДЕТАЛИ"] = raw.strip() or fallback
    summary = parts["СУТЬ"]
    first_line = (summary or parts["ДЕТАЛИ"] or fallback or "Заметка").splitlines()[0]
    title = parts["НАЗВАНИЕ"].splitlines()[0].strip(" *#\"«»") if parts["НАЗВАНИЕ"] else first_line.lstrip("#-[] ")[:60]
    tags = [t.strip(" #.").lower()[:30] for t in parts["ТЕГИ"].split(",") if t.strip(" #.")][:3]
    return Idea(title=title or "Заметка", summary=summary, details=parts["ДЕТАЛИ"], tags=tags)


async def structure(text: str = "", images: list[bytes] | None = None) -> Idea:
    """images: одно фото или альбом, страницы по порядку."""
    images = images or []
    content: list[dict] = []
    for image in images:
        b64 = base64.b64encode(image).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    if images:
        about = "Заметка на фото." if len(images) == 1 else f"Заметка на {len(images)} фото, это страницы по порядку. В деталях раздели их заголовками «## Фото 1», «## Фото 2» и т.д."
        note = f"{about}\nПодпись: {text}" if text else about
    else:
        note = f"Заметка:\n{text}"
    prompt = PROMPT.format(details=DETAILS_PHOTO if images else DETAILS_TEXT)
    # Инструкцию кладём в сообщение пользователя: не все модели принимают роль system
    content.append({"type": "text", "text": f"{prompt}\n\n{note}"})

    response = await _client().chat_completion(
        model=c.VISION_MODEL,
        messages=[{"role": "user", "content": content}],
        max_tokens=6000,
        temperature=0.2,
    )
    idea = _parse(response.choices[0].message.content or "", fallback=text)
    if not images:
        # Для текста и голоса оригинал у нас уже есть: кладём его в детали целиком, а не пересказ модели
        idea.details = text
    return idea
