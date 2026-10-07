import asyncio
import base64
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from huggingface_hub import AsyncInferenceClient

from . import config as c

PROMPT = """Ты личный ассистент, который разбирает хаотичные заметки (текст, расшифровку голосового или фото страницы блокнота).
Ничего не выдумывай. Пиши на языке оригинала.

Сейчас у пользователя {now}.

Ответь строго в таком формате, с этими заголовками и без пояснений:

===НАЗВАНИЕ===
короткое название, до 8 слов
===ТЕГИ===
1-3 тега строчными буквами через запятую, например: идея, задача, дизайн
===ТИП===
какой это тип записи, ровно одно название из списка: {types}
===КОГДА===
если в заметке назван срок, дедлайн, время встречи или то, о чём надо напомнить, — дата в формате ГГГГ-ММ-ДД ЧЧ:ММ \
(или ГГГГ-ММ-ДД, если время не названо), считая от текущего момента; если срока нет — слово нет
===СУТЬ===
главная тема и ключевые мысли в Markdown: 2-6 пунктов списком "- ", задачи как "- [ ] ", при необходимости заголовки "## "
===ДЕТАЛИ===
{details}"""

DETAILS_PHOTO = """ВСЁ, что есть на фото, максимально полно и дословно, в Markdown. Ничего не сокращай и не обобщай:
каждая строка и пункт, все числа, даты, имена, цены, ссылки, пометки на полях, зачёркнутое, стрелки и связи между блоками.
Сохраняй порядок и структуру страницы. Рисунки, схемы, скетчи и цвета опиши словами: что изображено, где на странице, что подписано.
Неразборчивое слово помечай как [неразборчиво]."""

DETAILS_TEXT = "оставь пустым"

SECTIONS = ("НАЗВАНИЕ", "ТЕГИ", "ТИП", "КОГДА", "СУТЬ", "ДЕТАЛИ")
WEEKDAY_NAMES = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def _now_text(now: datetime) -> str:
    return f"{now.strftime('%Y-%m-%d %H:%M')}, {WEEKDAY_NAMES[now.weekday()]}"


def _when_from(value: str, now: datetime) -> str | None:
    """«2026-10-07 15:00» или «2026-10-07» от модели → ISO с часовым поясом человека. Явную чушь отбрасываем."""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{1,2}):(\d{2}))?", value or "")
    if not m:
        return None
    try:
        day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if m.group(4):
            at = datetime.combine(day, time(int(m.group(4)), int(m.group(5))), now.tzinfo)
            return at.isoformat() if timedelta(days=-1) < at - now < timedelta(days=5 * 366) else None
    except ValueError:
        return None
    return day.isoformat() if timedelta(days=-1) <= day - now.date() < timedelta(days=5 * 366) else None


@dataclass
class Idea:
    title: str
    summary: str
    details: str = ""
    tags: list[str] = field(default_factory=list)
    type_guess: str | None = None  # предложение ИИ; сам тип выбирает человек в разборе
    when: str | None = None  # срок, если он есть в заметке (ISO)


# Дольше ждать ответа модели нет смысла: лучше честно сказать об ошибке, чем висеть
AI_TIMEOUT = 120


def _client() -> AsyncInferenceClient:
    return AsyncInferenceClient(api_key=c.HF_TOKEN, timeout=AI_TIMEOUT)


async def transcribe(audio: bytes) -> str:
    result = await _client().automatic_speech_recognition(audio, model=c.ASR_MODEL)
    return result.text.strip()


def _parse(raw: str, fallback: str) -> Idea:
    parts = dict.fromkeys(SECTIONS, "")
    names = "|".join(SECTIONS)
    for m in re.finditer(rf"===\s*({names})\s*===(.*?)(?====\s*(?:{names})\s*===|\Z)", raw, re.S):
        parts[m.group(1)] = m.group(2).strip()
    if not parts["СУТЬ"] and not parts["ДЕТАЛИ"]:
        # Модель ответила не по формату: ничего не теряем, всё уходит в детали
        parts["ДЕТАЛИ"] = raw.strip() or fallback
    summary = parts["СУТЬ"]
    first_line = (summary or parts["ДЕТАЛИ"] or fallback or "Заметка").splitlines()[0]
    title = parts["НАЗВАНИЕ"].splitlines()[0].strip(" *#\"«»") if parts["НАЗВАНИЕ"] else first_line.lstrip("#-[] ")[:60]
    tags = [t.strip(" #.").lower()[:30] for t in parts["ТЕГИ"].split(",") if t.strip(" #.")][:3]
    return Idea(
        title=title or "Заметка",
        summary=summary,
        details=parts["ДЕТАЛИ"],
        tags=tags,
        type_guess=parts["ТИП"].splitlines()[0].strip(" *\"«»") if parts["ТИП"] else None,
        when=parts["КОГДА"].strip(),
    )


async def structure(
    text: str = "", images: list[bytes] | None = None, types: list[str] | None = None, now: datetime | None = None
) -> Idea:
    """images: одно фото или альбом, страницы по порядку. types — список типов, из которых ИИ предлагает один;
    now — текущее время человека (с его часовым поясом), от него считается срок."""
    images = images or []
    now = now or datetime.now(timezone.utc)
    content: list[dict] = []
    for image in images:
        b64 = base64.b64encode(image).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    if images:
        about = "Заметка на фото." if len(images) == 1 else f"Заметка на {len(images)} фото, это страницы по порядку. В деталях раздели их заголовками «## Фото 1», «## Фото 2» и т.д."
        note = f"{about}\nПодпись: {text}" if text else about
    else:
        note = f"Заметка:\n{text}"
    prompt = PROMPT.format(
        details=DETAILS_PHOTO if images else DETAILS_TEXT, now=_now_text(now), types=", ".join(types or []) or "—"
    )
    # Инструкцию кладём в сообщение пользователя: не все модели принимают роль system
    content.append({"type": "text", "text": f"{prompt}\n\n{note}"})

    response = await _client().chat_completion(
        model=c.VISION_MODEL,
        messages=[{"role": "user", "content": content}],
        max_tokens=6000,
        temperature=0.2,
    )
    idea = _parse(response.choices[0].message.content or "", fallback=text)
    idea.when = _when_from(idea.when or "", now)
    # Предложенный тип засчитываем, только если он из списка (сверяем без эмодзи и регистра)
    plain = lambda t: re.sub(r"[^\w\s]", "", t).strip().lower()  # noqa: E731
    idea.type_guess = next((t for t in types or [] if idea.type_guess and plain(t) == plain(idea.type_guess)), None)
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


# ---------- ✨ раскрытие заметки под её тип: вопрос за вопросом ----------

# Для каждого типа: цель, стороны, которые нужно раскрыть вопросами, и структура итогового описания.
# Тип узнаётся по ключевому слову в названии, поэтому эмодзи и мелкие правки названий в Notion не мешают.
GUIDES = {
    "идея": (
        "превратить сырую мысль в идею, которую можно оценить и начать делать",
        "в чём суть; какую проблему решает или зачем это мне; для кого; как это может выглядеть; "
        "что нужно для реализации; что может помешать; какой первый маленький шаг",
        "## Суть\n## Зачем и для кого\n## Как это может выглядеть\n## Что нужно\n## Риски и сомнения\n## Первый шаг",
    ),
    "задач": (
        "превратить задачу в понятный план действий",
        "что именно должно получиться (результат); из каких шагов состоит; когда срок и насколько срочно; "
        "что нужно и от кого зависит; как понять, что готово",
        "## Результат\n## Шаги (чек-лист - [ ])\n## Срок и приоритет\n## Что нужно, от кого зависит\n## Критерий готовности",
    ),
    "быстр": (
        "сохранить факт так, чтобы его легко найти и понять через полгода",
        "что это за факт точно; откуда он; в какой ситуации пригодится",
        "## Факт\n## Откуда\n## Когда пригодится",
    ),
    "напомин": (
        "сделать напоминание, которое невозможно понять неправильно",
        "о чём именно напомнить; когда (дата, время); где; кому или с кем; что подготовить или взять с собой",
        "## О чём\n## Когда\n## Где и с кем\n## Что подготовить",
    ),
    "референс": (
        "разобрать референс так, чтобы из него можно было взять конкретные решения в работу",
        "что на референсе и откуда он; что именно в нём цепляет; что берём: цвет, композиция, типографика, свет, "
        "фактуры, настроение, обработка; куда в проекте это пойдёт; чего брать не нужно",
        "## Что на референсе\n## Что цепляет\n## Что берём (цвет, композиция, типографика, свет, фактуры, обработка)\n"
        "## Палитра (HEX-коды, если есть изображение)\n## Куда применить в проекте\n## Чего не берём\n## Источник",
    ),
    "обсуд": (
        "подготовить тему к обсуждению так, чтобы разговор закончился решением",
        "какой именно вопрос нужно решить; почему он важен сейчас; что уже известно и что пробовали; какие есть "
        "варианты и их плюсы и минусы; с кем обсудить; к какому сроку нужно решение; что считается итогом разговора",
        "## Вопрос\n## Почему сейчас\n## Что уже известно\n## Варианты (плюсы и минусы)\n## С кем и когда\n## Нужный итог",
    ),
    "событ": (
        "собрать всё о событии, чтобы ничего не забыть",
        "что за событие; когда и где; кто участвует; зачем мне туда; что подготовить заранее; что после",
        "## Что\n## Когда и где\n## Кто\n## Зачем\n## Подготовка (чек-лист - [ ])\n## После",
    ),
}
GUIDE_DEFAULT = (
    "раскрыть заметку подробнее",
    "в чём суть; зачем это; какие детали важны; какой следующий шаг",
    "## Суть\n## Зачем\n## Детали\n## Следующий шаг",
)
MAX_QUESTIONS = 7

NEXT_QUESTION_PROMPT = """Ты помогаешь раскрыть заметку пользователя типа «{type}», задавая вопросы по одному.
Цель: {goal}.
Что нужно раскрыть для этого типа: {aspects}.

Проект: {project}.{project_notes}

=== ЗАМЕТКА{photos} ===
{note}
=== КОНЕЦ ЗАМЕТКИ ===

Уже заданные вопросы и ответы:
{qa}

Задай ОДИН следующий вопрос — о самой важной стороне из списка, которая ещё не раскрыта ни заметкой, ни ответами.
Вопрос короткий и конкретный, привязанный к содержанию заметки, на языке заметки; можно предложить варианты ответа в скобках.
Не повторяй уже заданное и не спрашивай то, что есть в заметке.
Если всё важное уже раскрыто, ответь одним словом: ГОТОВО"""

COMPOSE_PROMPT = """Собери подробное описание заметки типа «{type}».
Цель: {goal}.
Проект: {project}.{project_notes}

Структура:
{template}

=== ЗАМЕТКА{photos} ===
{note}
=== КОНЕЦ ЗАМЕТКИ ===

Ответы пользователя на вопросы:
{qa}{draft}

Правила: опирайся только на заметку{photos_rule} и ответы, ничего не выдумывай. Раздел, для которого нет данных, пропусти.
Пиши на языке заметки, Markdown: заголовки ##, списки -, задачи - [ ]. Без вступлений и пояснений, только описание."""


def guide(type_name: str) -> tuple[str, str, str]:
    name = type_name.lower()
    return next((g for key, g in GUIDES.items() if key in name), GUIDE_DEFAULT)


def _qa_text(qa: list[tuple[str, str]]) -> str:
    return "\n".join(f"— {q}\n  Ответ: {a}" for q, a in qa) or "(пока ничего)"


def _with_images(images: list[bytes], text: str) -> list[dict]:
    content = [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(i).decode()}"}} for i in images
    ]
    return content + [{"type": "text", "text": text}]


def _common(type_name: str, note: str, project: str | None, titles: list[str], images: list[bytes]) -> dict:
    goal, aspects, template = guide(type_name)
    return {
        "type": type_name,
        "goal": goal,
        "aspects": aspects,
        "template": template,
        "project": project or "не указан",
        "project_notes": ("\nДругие заметки проекта: " + "; ".join(titles[:15])) if titles else "",
        "photos": " (и оригиналы фото выше)" if images else "",
        "photos_rule": ", оригиналы фото" if images else "",
        "note": note[:ASK_MAX_CHARS],
    }


async def next_question(
    type_name: str, note: str, project: str | None, titles: list[str], images: list[bytes], qa: list[tuple[str, str]]
) -> str | None:
    """Следующий вопрос или None, если всё раскрыто."""
    if len(qa) >= MAX_QUESTIONS:
        return None
    text = NEXT_QUESTION_PROMPT.format(**_common(type_name, note, project, titles, images), qa=_qa_text(qa))
    response = await _client().chat_completion(
        model=c.VISION_MODEL, messages=[{"role": "user", "content": _with_images(images, text)}], max_tokens=300, temperature=0.4
    )
    answer = (response.choices[0].message.content or "").strip().strip('"«»')
    if not answer or answer.upper().startswith("ГОТОВО"):
        return None
    return answer.splitlines()[0].strip() if len(answer) > 400 else answer


async def compose(
    type_name: str,
    note: str,
    project: str | None,
    titles: list[str],
    images: list[bytes],
    qa: list[tuple[str, str]],
    draft: str = "",
) -> str:
    extra = f"\n\nПредыдущий вариант описания (обнови его с учётом ответов):\n{draft[:8000]}" if draft else ""
    text = COMPOSE_PROMPT.format(**_common(type_name, note, project, titles, images), qa=_qa_text(qa), draft=extra)
    response = await _client().chat_completion(
        model=c.VISION_MODEL, messages=[{"role": "user", "content": _with_images(images, text)}], max_tokens=2500, temperature=0.3
    )
    return (response.choices[0].message.content or "").strip() or "🤷 Модель вернула пустое описание."


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


WHEN_PROMPT = """Сейчас у пользователя {now}. Он написал срок: «{text}».
Переведи в дату. Ответь одной строкой: ГГГГ-ММ-ДД ЧЧ:ММ, или ГГГГ-ММ-ДД, если время не названо, или слово нет, если это не срок."""


async def parse_when(text: str, now: datetime) -> str | None:
    """Срок словами, который не разобрал простой разборщик (whenparse), — через ИИ."""
    response = await _client().chat_completion(
        model=c.VISION_MODEL,
        messages=[{"role": "user", "content": WHEN_PROMPT.format(now=_now_text(now), text=text[:200])}],
        max_tokens=30,
        temperature=0,
    )
    return _when_from(response.choices[0].message.content or "", now)


# ---------- 🗓 расписание словами ----------

SCHEDULE_PROMPT = """Сейчас у пользователя {now}. Его текущее базовое расписание (регулярные блоки):
{base}

Пользователь пишет: «{text}»

{task}

Ответь строго одним JSON-объектом без пояснений:
{{"replace_base": false,
 "base_add": [{{"title": "Работа", "days": ["пн","ср","пт"], "start": "10:00", "end": "14:00"}}],
 "base_remove": ["название регулярного блока"],
 "add": [{{"title": "Стоматолог", "date": "ГГГГ-ММ-ДД", "start": "14:00", "end": "15:00"}}],
 "cancel": [{{"title": "название регулярного блока", "date": "ГГГГ-ММ-ДД"}}]}}
Пустые списки — если таких изменений нет. Время в формате ЧЧ:ММ; если конец не назван, оставь "end" пустым.
Дни недели: пн, вт, ср, чт, пт, сб, вс. Даты считай от текущего момента («в эту среду», «завтра»)."""

ROUTINE_TASK = """Это описание базового распорядка, который повторяется каждую неделю. Разложи его в base_add и поставь
"replace_base": true. Остальные списки пустые."""
CHANGE_TASK = """Это изменение расписания: разовое событие (add), отмена регулярного блока в конкретный день (cancel),
или изменение базового распорядка (base_add / base_remove; чтобы поменять время блока — удали старый и добавь новый).
"replace_base" — false."""


def _json_object(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw or "", re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        data = {}
    return data if isinstance(data, dict) else {}


async def parse_schedule(text: str, base: str, now: datetime, routine: bool) -> dict:
    """Расписание или изменение словами → план изменений для schedule.apply."""
    prompt = SCHEDULE_PROMPT.format(now=_now_text(now), base=base, text=text[:2000], task=ROUTINE_TASK if routine else CHANGE_TASK)
    response = await _client().chat_completion(
        model=c.VISION_MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=1500, temperature=0
    )
    plan = _json_object(response.choices[0].message.content or "")
    clean = {"replace_base": bool(plan.get("replace_base")) and routine}
    for key in ("base_add", "base_remove", "add", "cancel"):
        value = plan.get(key) or []
        clean[key] = [v for v in value if isinstance(v, (dict, str))] if isinstance(value, list) else []
    for item in clean["add"] + clean["cancel"]:
        if isinstance(item, dict) and item.get("date"):
            item["date"] = (_when_from(item["date"], now) or "")[:10]
    clean["add"] = [a for a in clean["add"] if isinstance(a, dict) and a.get("date")]
    clean["cancel"] = [a for a in clean["cancel"] if isinstance(a, dict) and a.get("date")]
    for item in clean["base_add"] + clean["add"]:
        if isinstance(item, dict):
            for key in ("start", "end"):
                value = str(item.get(key) or "")
                m = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*", value)
                item[key] = f"{int(m.group(1)):02d}:{m.group(2) or '00'}" if m and int(m.group(1)) < 24 else ""
    return clean
