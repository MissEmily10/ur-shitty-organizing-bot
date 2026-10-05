import base64
import json
import re
from dataclasses import dataclass, field

from huggingface_hub import AsyncInferenceClient

from . import config as c

PROMPT = """Ты личный ассистент, который разбирает хаотичные заметки (текст, расшифровку голосового или фото страницы блокнота).
Задача: аккуратно структурировать мысль, ничего не выдумывая и не теряя смысла. Пиши на языке оригинала.
Если это фото: сначала дословно распознай весь текст, включая рукописный, затем структурируй.

Ответь СТРОГО одним JSON-объектом без пояснений:
{"title": "короткое название, до 8 слов",
 "tags": ["1-3 тега строчными буквами, например: идея, задача, дизайн"],
 "markdown": "структурированный текст в Markdown: заголовки ##, списки -, чекбоксы - [ ] для задач"}"""


@dataclass
class Idea:
    title: str
    markdown: str
    tags: list[str] = field(default_factory=list)


def _client() -> AsyncInferenceClient:
    return AsyncInferenceClient(api_key=c.HF_TOKEN)


async def transcribe(audio: bytes) -> str:
    result = await _client().automatic_speech_recognition(audio, model=c.ASR_MODEL)
    return result.text.strip()


def _parse(raw: str, fallback: str) -> Idea:
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        data = {}
    markdown = str(data.get("markdown") or raw or fallback).strip()
    title = str(data.get("title") or "").strip() or markdown.splitlines()[0].lstrip("#- ")[:60]
    tags = [str(t).strip().lower()[:30] for t in data.get("tags") or [] if str(t).strip()][:3]
    return Idea(title=title, markdown=markdown, tags=tags)


async def structure(text: str = "", image: bytes | None = None) -> Idea:
    content: list[dict] = []
    if image:
        b64 = base64.b64encode(image).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    # Инструкцию кладём в сообщение пользователя: не все модели принимают роль system
    content.append({"type": "text", "text": f"{PROMPT}\n\nЗаметка:\n{text or '(на фото)'}"})

    response = await _client().chat_completion(
        model=c.VISION_MODEL,
        messages=[{"role": "user", "content": content}],
        max_tokens=1500,
        temperature=0.2,
    )
    return _parse(response.choices[0].message.content or "", fallback=text)
