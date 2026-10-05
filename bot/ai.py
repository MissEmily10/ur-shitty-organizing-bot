import asyncio
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


ASK_PROMPT = """Ты личный ассистент. Ниже заметка пользователя{photos}. Выполни его запрос, опираясь только на заметку.
Отвечай на языке запроса, по делу. Списки — через "- ", задачи — "- [ ] ", таблицы — в Markdown.
Если в заметке не хватает данных для ответа, так и скажи, ничего не выдумывай.

=== ЗАМЕТКА ===
{note}
=== КОНЕЦ ЗАМЕТКИ ==="""

# Сколько текста заметки отдаём модели за раз (большие объёмы — часть 2г)
ASK_MAX_CHARS = 30000


async def ask(note: str, question: str, images: list[bytes] | None = None, history: str = "") -> str:
    images = images or []
    content: list[dict] = []
    for image in images:
        b64 = base64.b64encode(image).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    photos = " и оригиналы фото: сверяй с ними цифры и детали" if images else ""
    text = ASK_PROMPT.format(photos=photos, note=note[:ASK_MAX_CHARS])
    if history:
        text += f"\n\nПредыдущий ответ, который пользователь уточняет:\n{history[:6000]}"
    content.append({"type": "text", "text": f"{text}\n\nЗапрос: {question}"})
    response = await _client().chat_completion(
        model=c.VISION_MODEL, messages=[{"role": "user", "content": content}], max_tokens=3000, temperature=0.3
    )
    return (response.choices[0].message.content or "").strip() or "🤷 Модель вернула пустой ответ."


# ---------- ✨ расширение заметки под её тип ----------

TEMPLATES = {
    "Идея": "## Суть\n## Зачем, в чём ценность\n## Как реализовать (шаги)\n## Что нужно: ресурсы, люди\n## Риски и сомнения\n## Следующий шаг",
    "Задача": "## Что сделать\n## Подзадачи (чек-лист - [ ])\n## Критерий готовности\n## Срок и приоритет\n## Что нужно, от кого зависит",
    "Быстрая": "## Факт, кратко и точно\n## Контекст: откуда и зачем пригодится",
    "Напомин": "## О чём напомнить\n## Когда\n## Где, кому, что взять с собой",
    "Референс": "## Что на референсе\n## Что берём: цвет, композиция, типографика, свет, фактуры, настроение\n## Палитра (HEX-коды, если есть изображение)\n## Как применить в проекте\n## Источник, ссылка",
    "Обсудить": "## Вопрос\n## Контекст\n## Варианты решения\n## С кем обсудить\n## Что нужно решить в итоге",
    "Событие": "## Что\n## Когда и где\n## Кто участвует\n## Подготовка (чек-лист - [ ])",
}
TEMPLATE_DEFAULT = "## Суть\n## Детали\n## Следующий шаг"

EXPAND_PROMPT = """Ты помогаешь развить заметку пользователя в полноценное описание типа «{type}».
Проект: {project}.{project_notes}

Структура описания для этого типа:
{template}

Правила:
- Опирайся на заметку{photos}. Ничего не выдумывай: где данных нет, оставь пункт с пометкой «уточнить» и задай об этом вопрос.
- Адаптируй содержание под тип и под проект.
- Пиши на языке заметки, Markdown: заголовки ##, списки -, задачи - [ ].

=== ЗАМЕТКА ===
{note}
=== КОНЕЦ ЗАМЕТКИ ==={refine}

Ответь строго в формате, без пояснений:
===ОПИСАНИЕ===
описание по структуре
===ВОПРОСЫ===
до 3 коротких уточняющих вопросов, каждый с новой строки; если всё ясно — оставь пустым"""


def _template(type_name: str) -> str:
    return next((t for key, t in TEMPLATES.items() if key.lower() in type_name.lower()), TEMPLATE_DEFAULT)


async def expand(
    type_name: str,
    note: str,
    project: str | None,
    project_titles: list[str],
    images: list[bytes] | None = None,
    draft: str = "",
    answers: str = "",
) -> tuple[str, list[str]]:
    """Возвращает (описание в Markdown, уточняющие вопросы)."""
    images = images or []
    content: list[dict] = []
    for image in images:
        b64 = base64.b64encode(image).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    project_notes = ("\nДругие заметки проекта: " + "; ".join(project_titles[:15])) if project_titles else ""
    refine = (
        f"\n\nТекущий черновик описания:\n{draft[:8000]}\n\nОтветы пользователя на уточняющие вопросы:\n{answers}\n"
        "Обнови черновик с учётом ответов и задай новые вопросы, только если что-то важное всё ещё неясно."
        if draft
        else ""
    )
    text = EXPAND_PROMPT.format(
        type=type_name,
        project=project or "не указан",
        project_notes=project_notes,
        template=_template(type_name),
        photos=" и оригиналы фото" if images else "",
        note=note[:ASK_MAX_CHARS],
        refine=refine,
    )
    content.append({"type": "text", "text": text})
    response = await _client().chat_completion(
        model=c.VISION_MODEL, messages=[{"role": "user", "content": content}], max_tokens=2500, temperature=0.3
    )
    raw = response.choices[0].message.content or ""
    m = re.search(r"===\s*ОПИСАНИЕ\s*===(.*?)(?:===\s*ВОПРОСЫ\s*===(.*))?\Z", raw, re.S)
    if not m:
        return raw.strip(), []
    questions = [q.strip(" -•\t") for q in (m.group(2) or "").splitlines()]
    questions = [re.sub(r"^\d+[.)]\s*", "", q) for q in questions if q.strip(" -•\t")]
    return m.group(1).strip(), questions[:3]


# ---------- 🔎 вопрос по многим заметкам ----------

NOTES_PROMPT = """Ты личный ассистент. Ниже заметки пользователя ({scope}), у каждой название, дата и тип.
Ответь на вопрос, опираясь только на эти заметки. Называй заметки, на которые опираешься, в «кавычках».
Пиши на языке вопроса, по делу: списки через "- ", таблицы в Markdown. Если ответа в заметках нет, так и скажи.

{notes}{history}

Вопрос: {question}"""

EXTRACT_PROMPT = """Ниже часть заметок пользователя. Выпиши из них всё, что относится к вопросу «{question}»:
факты, решения, даты, цифры, с названием заметки в «кавычках». Только то, что есть в заметках. Если ничего нет, ответь одним словом: нет.

{notes}"""

# Сколько текста заметок отдаём модели за один запрос
NOTES_CHUNK_CHARS = 25000
NOTES_MAX_CHUNKS = 8


async def _complete(text: str, max_tokens: int) -> str:
    response = await _client().chat_completion(
        model=c.VISION_MODEL, messages=[{"role": "user", "content": text}], max_tokens=max_tokens, temperature=0.3
    )
    return (response.choices[0].message.content or "").strip()


def pack(notes: list[str], limit: int = NOTES_CHUNK_CHARS) -> list[str]:
    """Складывает заметки в куски не длиннее limit; слишком длинная заметка обрезается."""
    chunks, current = [], ""
    for note in notes:
        note = note[:limit]
        if current and len(current) + len(note) + 2 > limit:
            chunks.append(current)
            current = ""
        current = f"{current}\n\n{note}" if current else note
    return chunks + [current] if current else chunks


async def ask_notes(question: str, notes: list[str], scope: str, history: str = "") -> tuple[str, bool]:
    """Ответ по многим заметкам. Если не влезают в один запрос: из каждой части выбираем относящееся
    к вопросу, потом отвечаем по выжимке. Второе значение — пришлось ли обрезать заметки."""
    chunks = pack(notes)
    truncated = len(chunks) > NOTES_MAX_CHUNKS
    chunks = chunks[:NOTES_MAX_CHUNKS]
    hist = f"\n\nПредыдущий ответ, который пользователь уточняет:\n{history[:6000]}" if history else ""
    if len(chunks) > 1:
        parts = await asyncio.gather(*(_complete(EXTRACT_PROMPT.format(question=question, notes=ch), 1500) for ch in chunks))
        relevant = [p for p in parts if p and p.strip(" .").lower() != "нет"]
        context = "\n\n".join(relevant) or "В заметках ничего не нашлось по этому вопросу."
        scope += ", выжимка по частям"
    else:
        context = chunks[0] if chunks else "Заметок нет."
    answer = await _complete(NOTES_PROMPT.format(scope=scope, notes=context, history=hist, question=question), 3000)
    return answer or "🤷 Модель вернула пустой ответ.", truncated
