import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from . import config as c
from .markdown import toggle

API = "https://api.notion.com/v1"
DB_TITLE = "Входящие идеи"
DETAILS_TITLE = "📝 Полный текст и детали"
# Загрузка файлов появилась в более новой версии API, остальные запросы остаются на 2022-06-28
FILES_VERSION = "2026-03-11"


class NotionError(RuntimeError):
    def __init__(self, status: int, text: str):
        super().__init__(f"Notion {status}: {text[:300]}")
        self.status = status


def _forget_db() -> None:
    """Сбрасывает запомненную таблицу: при следующем обращении бот заново проверит её колонки."""
    global _db_id
    _db_id = None


async def _call(method: str, path: str, json: dict | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {c.NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.request(method, f"{API}{path}", headers=headers, json=json)
    if r.is_error:
        if r.status_code == 400 and "property" in r.text.lower():
            _forget_db()  # колонку удалили или переименовали в Notion: в следующий раз бот её вернёт
        raise NotionError(r.status_code, r.text)
    return r.json()


# ---------- поиск или создание таблицы ----------

SCHEMA = {
    c.P_TITLE: {"title": {}},
    c.P_STATUS: {"select": {"options": [{"name": c.STATUS_NEW, "color": "red"}, {"name": c.STATUS_DONE, "color": "green"}]}},
    c.P_SOURCE: {
        "select": {
            "options": [{"name": "Текст", "color": "gray"}, {"name": "Фото", "color": "orange"}, {"name": "Голос", "color": "purple"}]
        }
    },
    c.P_TAGS: {
        "multi_select": {
            "options": [{"name": "идея", "color": "yellow"}, {"name": "задача", "color": "pink"}, {"name": "дизайн", "color": "blue"}]
        }
    },
    c.P_TYPE: {
        "select": {
            "options": [
                {"name": "💡 Идея", "color": "yellow"},
                {"name": "📋 Задача", "color": "pink"},
                {"name": "⚡ Быстрая заметка", "color": "gray"},
                {"name": "⏰ Напоминание", "color": "red"},
                {"name": "🎨 Референс", "color": "purple"},
                {"name": "❓ Обсудить", "color": "orange"},
                {"name": "📅 Событие", "color": "blue"},
            ]
        }
    },
    c.P_AUTHOR: {"select": {}},
    c.P_AUTHOR_ID: {"number": {}},
    c.P_WHEN: {"date": {}},  # срок, время напоминания или события
    c.P_DONE: {"checkbox": {}},
    c.P_AI_TYPE: {"select": {}},  # тип, который предложил ИИ; сам тип выбирает человек в разборе
    "Создано": {"created_time": {}},
}

MEMBERS_TITLE = "Участники бота"
# Одна строка — один человек. Пока Telegram ID пуст, а Код заполнен, это неиспользованное приглашение.
MEMBERS_SCHEMA = {
    "Имя": {"title": {}},
    "Telegram ID": {"number": {}},
    "Username": {"rich_text": {}},
    "Код": {"rich_text": {}},
    "Код до": {"date": {}},
    "Добавлен": {"created_time": {}},
}

_db_id: str | None = None
_members_db_id: str | None = None
_service_db_id: str | None = None
_schedule_db_id: str | None = None
_extra_dbs: dict[str, str] = {}
_db_lock = asyncio.Lock()


def _plain(rich: list[dict]) -> str:
    return "".join(r["plain_text"] for r in rich)


async def _search(kind: str, query: str = "") -> list[dict]:
    body = {"filter": {"property": "object", "value": kind}, "page_size": 50}
    if query:
        body["query"] = query
    return [r for r in (await _call("POST", "/search", body))["results"] if not r.get("archived")]


async def db_id() -> str:
    """ID таблицы: из NOTION_DATABASE_ID, иначе ищем «Входящие идеи» среди доступного интеграции,
    иначе создаём таблицу на первой странице, к которой подключена интеграция."""
    global _db_id
    async with _db_lock:
        if _db_id:
            return _db_id
        found = None
        if c.NOTION_DATABASE_ID:
            try:
                found = await _call("GET", f"/databases/{c.NOTION_DATABASE_ID}")
            except NotionError as e:
                if e.status != 404:
                    raise
        if not found:
            found = next((db for db in await _search("database", DB_TITLE) if _plain(db["title"]) == DB_TITLE), None)
        if found:
            # Колонки, которых нет (новые в этой версии бота или удалённые вручную), бот возвращает сам
            missing = {
                name: spec for name, spec in SCHEMA.items() if name not in found["properties"] and "title" not in spec
            }
            if missing:
                await _call("PATCH", f"/databases/{found['id']}", {"properties": missing})
            _db_id = found["id"]
            return _db_id
        pages = [p for p in await _search("page") if p["parent"]["type"] in ("workspace", "page_id")]
        if not pages:
            raise RuntimeError(
                "Интеграция Notion не видит ни одной страницы. "
                "Откройте нужную страницу → ••• → Connections → подключите интеграцию."
            )
        db = await _call(
            "POST",
            "/databases",
            {
                "parent": {"type": "page_id", "page_id": pages[0]["id"]},
                "title": [{"type": "text", "text": {"content": DB_TITLE}}],
                "properties": SCHEMA,
            },
        )
        _db_id = db["id"]
        return _db_id


def _title(page: dict) -> str:
    return _plain((page["properties"].get(c.P_TITLE) or {}).get("title") or []) or "Без названия"


def _select(page: dict, prop: str) -> str | None:
    """Значение колонки-выбора; None, если пусто или колонки нет."""
    return ((page["properties"].get(prop) or {}).get("select") or {}).get("name")


async def upload_file(data: bytes, filename: str, content_type: str) -> str:
    """Загружает файл в Notion, возвращает id для блока. На бесплатном Notion лимит 5 МБ на файл."""
    headers = {"Authorization": f"Bearer {c.NOTION_TOKEN}", "Notion-Version": FILES_VERSION}
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(f"{API}/file_uploads", headers=headers, json={"filename": filename, "content_type": content_type})
        if r.is_error:
            raise NotionError(r.status_code, r.text)
        upload_id = r.json()["id"]
        r = await client.post(
            f"{API}/file_uploads/{upload_id}/send", headers=headers, files={"file": (filename, data, content_type)}
        )
        if r.is_error:
            raise NotionError(r.status_code, r.text)
    return upload_id


async def upload_image(data: bytes, filename: str) -> str:
    return await upload_file(data, filename, "image/jpeg")


async def set_icon(page_id: str, upload_id: str | None) -> None:
    """Иконка страницы из загруженного файла; None — убрать иконку."""
    icon = {"type": "file_upload", "file_upload": {"id": upload_id}} if upload_id else None
    headers = {"Authorization": f"Bearer {c.NOTION_TOKEN}", "Notion-Version": FILES_VERSION}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.patch(f"{API}/pages/{page_id}", headers=headers, json={"icon": icon})
    if r.is_error:
        raise NotionError(r.status_code, r.text)


def file_block(upload_id: str, kind: str = "file") -> dict:
    """kind: image — картинка, pdf — PDF с просмотром прямо в Notion, file — любой другой файл."""
    return {"object": "block", "type": kind, kind: {"type": "file_upload", "file_upload": {"id": upload_id}}}


def image_block(upload_id: str) -> dict:
    return file_block(upload_id, "image")


async def create_idea(
    title: str,
    source: str,
    tags: list[str],
    blocks: list[dict],
    details: list[dict],
    author_id: int = 0,
    author: str = "",
    when: str | None = None,
    ai_type: str | None = None,
) -> tuple[str, str]:
    """blocks видны сразу (сюда же идут картинки-оригиналы), details прячутся в свёрнутый блок «Полный текст и детали»."""
    props = {
        c.P_TITLE: {"title": [{"text": {"content": title[:200]}}]},
        c.P_STATUS: {"select": {"name": c.STATUS_NEW}},
        c.P_SOURCE: {"select": {"name": source}},
        c.P_TAGS: {"multi_select": [{"name": t} for t in tags]},
    }
    if when:
        props[c.P_WHEN] = {"date": {"start": when}}
    if ai_type:
        props[c.P_AI_TYPE] = {"select": {"name": ai_type}}
    if author_id:
        props[c.P_AUTHOR_ID] = {"number": author_id}
        props[c.P_AUTHOR] = {"select": {"name": (author or str(author_id)).replace(",", " ")[:100]}}
    body = {"properties": props, "children": blocks[:100]}
    try:
        page = await _call("POST", "/pages", {"parent": {"database_id": await db_id()}, **body})
    except NotionError as e:
        if e.status != 400:
            raise
        # Скорее всего, колонку удалили вручную: бот уже сбросил таблицу, проверит колонки и попробует ещё раз
        page = await _call("POST", "/pages", {"parent": {"database_id": await db_id()}, **body})
    await _append(page["id"], blocks[100:])
    if details:
        added = await _call("PATCH", f"/blocks/{page['id']}/children", {"children": [toggle(DETAILS_TITLE, details)]})
        await _append(added["results"][-1]["id"], details[100:])
    return page["id"].replace("-", ""), page["url"]


async def _append(block_id: str, blocks: list[dict]) -> None:
    for i in range(0, len(blocks), 100):
        await _call("PATCH", f"/blocks/{block_id}/children", {"children": blocks[i : i + 100]})


def _item(p: dict) -> dict:
    return {
        "id": p["id"].replace("-", ""),
        "title": _title(p),
        "url": p["url"],
        "project_id": _project_id(p),
        "project": _project_names.get(_project_id(p) or ""),
        "type": _select(p, c.P_TYPE),
        "author_id": int((p["properties"].get(c.P_AUTHOR_ID) or {}).get("number") or 0),
        "when": ((p["properties"].get(c.P_WHEN) or {}).get("date") or {}).get("start"),
        "done": bool((p["properties"].get(c.P_DONE) or {}).get("checkbox")),
        "ai_type": _select(p, c.P_AI_TYPE),
        "author": _select(p, c.P_AUTHOR) or "",
    }


async def page_info(page_id: str) -> dict:
    await _load_projects()
    return _item(await _call("GET", f"/pages/{page_id}"))


async def page_project(page_id: str) -> str | None:
    return (await page_info(page_id))["project"]


def _by_author(user_id: int) -> dict:
    """Заметки человека. Заметки, сохранённые до командного режима (без автора), принадлежат владелице."""
    mine = {"property": c.P_AUTHOR_ID, "number": {"equals": user_id}}
    if user_id != c.OWNER_ID:
        return mine
    return {"or": [mine, {"property": c.P_AUTHOR_ID, "number": {"is_empty": True}}]}


async def review_items(user_id: int) -> list[dict]:
    """Для вечернего разбора: заметки человека, которым ещё не выбран тип (проект может уже стоять). Старые первыми.
    Заметки со статусом «Разобрано» из времён до типов тоже считаются разобранными."""
    await _load_projects()
    data = await _call(
        "POST",
        f"/databases/{await db_id()}/query",
        {
            "filter": {
                "and": [
                    {"property": c.P_TYPE, "select": {"is_empty": True}},
                    {"property": c.P_STATUS, "select": {"does_not_equal": c.STATUS_DONE}},
                    _by_author(user_id),
                ]
            },
            "sorts": [{"timestamp": "created_time", "direction": "ascending"}],
            "page_size": 100,
        },
    )
    return [_item(p) for p in data["results"]]


async def _children(block_id: str) -> list[dict]:
    blocks, cursor = [], None
    while True:
        query = f"?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
        data = await _call("GET", f"/blocks/{block_id}/children{query}")
        blocks += data["results"]
        if not data.get("has_more"):
            return blocks
        cursor = data["next_cursor"]


def _block_text(b: dict) -> str | None:
    rich = b.get(b["type"], {}).get("rich_text")
    if rich is None:
        return None
    text = _plain(rich)
    if b["type"] in ("bulleted_list_item", "numbered_list_item"):
        return f"• {text}"
    if b["type"] == "to_do":
        return ("☑ " if b["to_do"].get("checked") else "☐ ") + text
    if b["type"].startswith("heading_"):
        return f"\n{text}"
    return text


async def read_note(page_id: str) -> dict:
    """Заметка целиком: суть, подробности из свёрнутого блока, ссылки на оригиналы фото."""
    summary, details, images = [], [], []
    for b in await _children(page_id):
        if b["type"] == "image":
            image = b["image"]
            images.append(image.get(image["type"], {}).get("url"))
        elif b["type"] == "toggle" and _plain(b["toggle"]["rich_text"]) == DETAILS_TITLE:
            details = [t for t in map(_block_text, await _children(b["id"])) if t is not None]
        elif (text := _block_text(b)) is not None:
            summary.append(text)
    return {"summary": "\n".join(summary).strip(), "details": "\n".join(details).strip(), "images": [u for u in images if u]}


async def _options(prop: str) -> list[dict]:
    db = await _call("GET", f"/databases/{await db_id()}")
    return ((db["properties"].get(prop) or {}).get("select") or {}).get("options", [])


async def _set_options(prop: str, options: list[dict]) -> None:
    # Notion заменяет список вариантов целиком: существующие передаём с их id, иначе они удалятся
    await _call("PATCH", f"/databases/{await db_id()}", {"properties": {prop: {"select": {"options": options}}}})


async def _add_options(prop: str, names: list[str]) -> list[str]:
    options = await _options(prop)
    taken = {o["name"].lower() for o in options}
    new = []
    for name in names:
        if name.lower() not in taken:
            taken.add(name.lower())
            new.append(name)
    if new:
        keep = [{"id": o["id"], "name": o["name"], "color": o["color"]} for o in options]
        await _set_options(prop, keep + [{"name": n} for n in new])
    return new


async def _delete_option(prop: str, name: str) -> None:
    keep = [{"id": o["id"], "name": o["name"], "color": o["color"]} for o in await _options(prop) if o["name"] != name]
    await _set_options(prop, keep)


async def projects(user_id: int | None = None) -> list[str]:
    """Названия открытых проектов, которые видит человек (None — все открытые). По ним строятся кнопки «📁»."""
    return [p["name"] for p in await project_list(user_id)]


async def add_projects(names: list[str], user_id: int | None = None) -> list[str]:
    """Создаёт проекты, которых ещё нет (без учёта регистра). Проекты владелицы — общие, участников — личные."""
    existing = {p["name"].lower() for p in await project_list(None, include_closed=True)}
    added = []
    for name in names:
        if name.lower() in existing:
            continue
        existing.add(name.lower())
        kind = KIND_SHARED if not user_id or user_id == c.OWNER_ID else KIND_PERSONAL
        await create_project(name, kind, user_id or c.OWNER_ID)
        added.append(name)
    return added


async def delete_project(name: str) -> None:
    project = await get_project(name)
    if project:
        await trash(project["id"])
        _forget_projects()


async def preview(page_id: str, limit: int = 600) -> str:
    data = await _call("GET", f"/blocks/{page_id}/children?page_size=30")
    lines = []
    for b in data["results"]:
        rich = b["type"] != "toggle" and b.get(b["type"], {}).get("rich_text")
        if rich:
            text = "".join(r["plain_text"] for r in rich)
            lines.append(f"• {text}" if "list_item" in b["type"] or b["type"] == "to_do" else text)
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


async def file_to_project(page_id: str, project: str, skip_review: bool = False) -> dict:
    """Только проект: из разбора заметка уходит, когда ей выбран тип. skip_review — режим «📌 Писать в»:
    заметка сразу считается разобранной. Возвращает проект."""
    found = await get_project(project)
    if not found:
        raise RuntimeError(f"Проект «{project}» не найден")
    props = {c.P_PROJECT: {"relation": [{"id": found["id"]}]}}
    if skip_review:
        props[c.P_STATUS] = {"select": {"name": c.STATUS_DONE}}
    await _call("PATCH", f"/pages/{page_id}", {"properties": props})
    return found


async def types() -> list[str]:
    return [o["name"] for o in await _options(c.P_TYPE)]


async def add_types(names: list[str]) -> list[str]:
    return await _add_options(c.P_TYPE, names)


async def delete_type(name: str) -> None:
    await _delete_option(c.P_TYPE, name)


async def set_when(page_id: str, when: str | None) -> None:
    """Срок заметки (ISO-дата или дата со временем). None — убрать срок. Новый срок снова «не выполнен»."""
    props = {c.P_WHEN: {"date": {"start": when} if when else None}}
    if when:
        props[c.P_DONE] = {"checkbox": False}
    await _call("PATCH", f"/pages/{page_id}", {"properties": props})


async def set_done(page_id: str, done: bool = True) -> None:
    await _call("PATCH", f"/pages/{page_id}", {"properties": {c.P_DONE: {"checkbox": done}}})


async def dated_items(user_id: int, before: str, after: str | None = None) -> list[dict]:
    """Невыполненные заметки человека со сроком до before (и после after, если указано), ближайшие первыми."""
    await _load_projects()
    conditions = [
        {"property": c.P_WHEN, "date": {"on_or_before": before}},
        {"property": c.P_DONE, "checkbox": {"equals": False}},
        _by_author(user_id),
    ]
    if after:
        conditions.append({"property": c.P_WHEN, "date": {"on_or_after": after}})
    data = await _call(
        "POST",
        f"/databases/{await db_id()}/query",
        {"filter": {"and": conditions}, "sorts": [{"property": c.P_WHEN, "direction": "ascending"}], "page_size": 100},
    )
    return [_item(p) for p in data["results"]]


async def set_type(page_id: str, type_name: str) -> None:
    """Тип выбран — заметка разобрана."""
    await _call(
        "PATCH",
        f"/pages/{page_id}",
        {"properties": {c.P_TYPE: {"select": {"name": type_name}}, c.P_STATUS: {"select": {"name": c.STATUS_DONE}}}},
    )


async def trash(page_id: str) -> None:
    """В корзину Notion: оттуда можно восстановить в течение 30 дней."""
    await _call("PATCH", f"/pages/{page_id}", {"archived": True})


async def add_answer(page_id: str, question: str, blocks: list[dict]) -> None:
    """Ответ ИИ дописывается в заметку свёрнутым блоком «🤖 <запрос>»."""
    added = await _call("PATCH", f"/blocks/{page_id}/children", {"children": [toggle(f"🤖 {question[:150]}", blocks)]})
    await _append(added["results"][-1]["id"], blocks[100:])


async def download(url: str) -> bytes:
    """Оригиналы фото лежат по временным ссылкам Notion, авторизация не нужна."""
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.content


async def project_titles(project: str, exclude: str, user_id: int, limit: int = 15) -> list[str]:
    """Названия других заметок проекта — контекст для ИИ. В командном проекте — всех участников, иначе свои."""
    found = await get_project(project)
    if not found:
        return []
    scope = {"property": c.P_PROJECT, "relation": {"contains": found["id"]}}
    data = await _call(
        "POST",
        f"/databases/{await db_id()}/query",
        {
            "filter": scope if found["kind"] == KIND_TEAM else {"and": [scope, _by_author(user_id)]},
            "sorts": [{"timestamp": "created_time", "direction": "descending"}],
            "page_size": limit + 1,
        },
    )
    return [_title(p) for p in data["results"] if p["id"].replace("-", "") != exclude][:limit]


async def add_section(page_id: str, title: str, blocks: list[dict]) -> None:
    """Дописывает в заметку раздел с заголовком (например, разбор Арт-директора)."""
    heading = {"object": "block", "type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": title}}]}}
    await _append(page_id, [heading] + blocks)


async def add_expansion(page_id: str, type_name: str, blocks: list[dict]) -> None:
    """Расширенное описание дописывается в заметку открыто, под заголовком «✨ <тип>: подробно»."""
    heading = {"object": "block", "type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": f"✨ {type_name}: подробно"}}]}}
    await _append(page_id, [heading] + blocks)


async def notes_in_scope(user_id: int, project: str | None = None, days: int | None = None, limit: int = 150) -> list[dict]:
    """Заметки проекта и/или за последние N дней, новые первыми (не больше limit). Свои — а в командном проекте
    заметки всех его участников."""
    await _load_projects()
    team = False
    if project:
        found = await get_project(project)
        if not found:
            return []
        if not can_see(found, user_id):
            return []
        scope = {"property": c.P_PROJECT, "relation": {"contains": found["id"]}}
        team = found["kind"] == KIND_TEAM
    else:
        since = (datetime.now(timezone.utc) - timedelta(days=days or 7)).isoformat()
        scope = {"timestamp": "created_time", "created_time": {"on_or_after": since}}
    parts = [scope]
    if project and days:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        parts.append({"timestamp": "created_time", "created_time": {"on_or_after": since}})
    if not team:
        parts.append(_by_author(user_id))
    flt = parts[0] if len(parts) == 1 else {"and": parts}
    notes, cursor = [], None
    while len(notes) < limit:
        body = {"filter": flt, "sorts": [{"timestamp": "created_time", "direction": "descending"}], "page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = await _call("POST", f"/databases/{await db_id()}/query", body)
        for p in data["results"]:
            notes.append({**_item(p), "created": p["created_time"][:10]})
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    return notes[:limit]


async def created_today(user_id: int) -> int:
    """Сколько заметок человек сохранил за последние сутки: для дневного лимита участников."""
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    data = await _call(
        "POST",
        f"/databases/{await db_id()}/query",
        {
            "filter": {
                "and": [
                    {"property": c.P_AUTHOR_ID, "number": {"equals": user_id}},
                    {"timestamp": "created_time", "created_time": {"on_or_after": since}},
                ]
            },
            "page_size": 100,
        },
    )
    return len(data["results"])


# ---------- участники и приглашения ----------


async def members_db_id() -> str:
    """Таблица «Участники бота»: ищем, иначе создаём рядом с «Входящими идеями». Пропавшие колонки возвращаем."""
    global _members_db_id
    if _members_db_id:
        return _members_db_id
    ideas = await _call("GET", f"/databases/{await db_id()}")
    found = next((d for d in await _search("database", MEMBERS_TITLE) if _plain(d["title"]) == MEMBERS_TITLE), None)
    if found:
        missing = {n: spec for n, spec in MEMBERS_SCHEMA.items() if n not in found["properties"] and "title" not in spec}
        if missing:
            await _call("PATCH", f"/databases/{found['id']}", {"properties": missing})
        _members_db_id = found["id"]
    else:
        parent = ideas["parent"].get("page_id") or await _first_shared_page()
        db = await _call(
            "POST",
            "/databases",
            {
                "parent": {"type": "page_id", "page_id": parent},
                "title": [{"type": "text", "text": {"content": MEMBERS_TITLE}}],
                "properties": MEMBERS_SCHEMA,
            },
        )
        _members_db_id = db["id"]
    return _members_db_id


async def _first_shared_page() -> str:
    pages = [p for p in await _search("page") if p["parent"]["type"] in ("workspace", "page_id")]
    if not pages:
        raise RuntimeError("Интеграция Notion не видит ни одной страницы.")
    return pages[0]["id"]


def _text_prop(page: dict, name: str) -> str:
    return _plain((page["properties"].get(name) or {}).get("rich_text") or [])


def _person(p: dict) -> dict:
    props = p["properties"]
    until = ((props.get("Код до") or {}).get("date") or {}).get("start")
    return {
        "page": p["id"],
        "tg": int((props.get("Telegram ID") or {}).get("number") or 0),
        "name": _plain((props.get("Имя") or {}).get("title") or []) or "Без имени",
        "username": _text_prop(p, "Username"),
        "code": _text_prop(p, "Код"),
        "until": until,
    }


async def _people() -> list[dict]:
    data = await _call("POST", f"/databases/{await members_db_id()}/query", {"page_size": 100})
    return [_person(p) for p in data["results"]]


async def members() -> list[dict]:
    """Участники, которые уже вошли по коду."""
    return [p for p in await _people() if p["tg"]]


def _expired(until: str | None) -> bool:
    return bool(until) and datetime.fromisoformat(until.replace("Z", "+00:00")) < datetime.now(timezone.utc)


async def invites() -> list[dict]:
    """Неиспользованные и не просроченные приглашения."""
    return [p for p in await _people() if not p["tg"] and p["code"] and not _expired(p["until"])]


async def create_invite(label: str, code: str, days: int) -> str:
    until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    await _call(
        "POST",
        "/pages",
        {
            "parent": {"database_id": await members_db_id()},
            "properties": {
                "Имя": {"title": [{"text": {"content": label[:200]}}]},
                "Код": {"rich_text": [{"text": {"content": code}}]},
                "Код до": {"date": {"start": until}},
            },
        },
    )
    return until


async def redeem(code: str, tg: int, name: str, username: str | None) -> dict | None:
    """Код верный и не просрочен — привязываем человека к строке и сжигаем код. Возвращает участника или None."""
    data = await _call(
        "POST",
        f"/databases/{await members_db_id()}/query",
        {"filter": {"property": "Код", "rich_text": {"equals": code}}, "page_size": 5},
    )
    for p in map(_person, data["results"]):
        if p["tg"] or _expired(p["until"]):
            continue
        who = f"@{username} · {name}" if username else name
        await _call(
            "PATCH",
            f"/pages/{p['page']}",
            {
                "properties": {
                    "Telegram ID": {"number": tg},
                    "Username": {"rich_text": [{"text": {"content": who[:200]}}]},
                    "Код": {"rich_text": []},
                }
            },
        )
        return {**p, "tg": tg, "code": ""}
    return None


async def remove_person(page_id: str) -> None:
    """Убрать участника или отозвать приглашение. Заметки человека остаются в «Входящих идеях»."""
    await trash(page_id)


# ---------- служебная таблица: настройки людей и отметки планировщика ----------

SERVICE_TITLE = "Служебное бота"
SERVICE_SCHEMA = {"Ключ": {"title": {}}, "Значение": {"rich_text": {}}, "Обновлено": {"last_edited_time": {}}}


async def service_db_id() -> str:
    """«Служебное бота» рядом с «Входящими идеями». Руками её трогать не нужно."""
    global _service_db_id
    if _service_db_id:
        return _service_db_id
    found = next((d for d in await _search("database", SERVICE_TITLE) if _plain(d["title"]) == SERVICE_TITLE), None)
    if found:
        _service_db_id = found["id"]
    else:
        ideas = await _call("GET", f"/databases/{await db_id()}")
        parent = ideas["parent"].get("page_id") or await _first_shared_page()
        db = await _call(
            "POST",
            "/databases",
            {
                "parent": {"type": "page_id", "page_id": parent},
                "title": [{"type": "text", "text": {"content": SERVICE_TITLE}}],
                "properties": SERVICE_SCHEMA,
            },
        )
        _service_db_id = db["id"]
    return _service_db_id


async def _service_row(key: str) -> dict | None:
    data = await _call(
        "POST",
        f"/databases/{await service_db_id()}/query",
        {"filter": {"property": "Ключ", "title": {"equals": key}}, "page_size": 1},
    )
    return data["results"][0] if data["results"] else None


async def get_value(key: str) -> str | None:
    row = await _service_row(key)
    return _text_prop(row, "Значение") if row else None


async def set_value(key: str, value: str) -> None:
    row = await _service_row(key)
    # Длинные значения (наборы реплик, персонажи) — кусками по 2000 символов; при чтении страницы Notion
    # отдаёт не больше 25 кусков, поэтому всего до 50 000 символов
    parts = [value[i : i + 2000] for i in range(0, min(len(value), 50_000), 2000)]
    props = {"Значение": {"rich_text": [{"text": {"content": p}} for p in parts]}}
    if row:
        await _call("PATCH", f"/pages/{row['id']}", {"properties": props})
    else:
        await _call(
            "POST",
            "/pages",
            {"parent": {"database_id": await service_db_id()}, "properties": {"Ключ": {"title": [{"text": {"content": key}}]}, **props}},
        )


async def forget_old_marks(days: int = 14) -> int:
    """Отметки планировщика и дневные флаги старше двух недель больше не нужны: чистим, чтобы таблица не росла."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    removed = 0
    for prefix in ("done:", "checkin_off:", "quick:"):
        data = await _call(
            "POST",
            f"/databases/{await service_db_id()}/query",
            {
                "filter": {
                    "and": [
                        {"property": "Ключ", "title": {"starts_with": prefix}},
                        {"timestamp": "last_edited_time", "last_edited_time": {"before": since}},
                    ]
                },
                "page_size": 100,
            },
        )
        for row in data["results"]:
            await trash(row["id"])
        removed += len(data["results"])
    return removed


# ---------- 🗓 расписание ----------

SCHEDULE_TITLE = "Расписание"
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
KIND_REGULAR, KIND_ONCE, KIND_CANCEL = "🔁 Регулярно", "📌 Разово", "🚫 Отмена"
SCHEDULE_SCHEMA = {
    "Событие": {"title": {}},
    "Вид": {"select": {"options": [{"name": KIND_REGULAR, "color": "blue"}, {"name": KIND_ONCE, "color": "green"}, {"name": KIND_CANCEL, "color": "red"}]}},
    "Дни": {"multi_select": {"options": [{"name": d} for d in WEEKDAYS]}},  # для регулярных
    "Дата": {"date": {}},  # для разовых и отмен
    "Начало": {"rich_text": {}},
    "Конец": {"rich_text": {}},
    "Автор ID": {"number": {}},
}


async def schedule_db_id() -> str:
    """Таблица «Расписание» рядом с «Входящими идеями»: регулярные блоки, разовые события и отмены."""
    global _schedule_db_id
    if _schedule_db_id:
        return _schedule_db_id
    found = next((d for d in await _search("database", SCHEDULE_TITLE) if _plain(d["title"]) == SCHEDULE_TITLE), None)
    if found:
        missing = {n: spec for n, spec in SCHEDULE_SCHEMA.items() if n not in found["properties"] and "title" not in spec}
        if missing:
            await _call("PATCH", f"/databases/{found['id']}", {"properties": missing})
        _schedule_db_id = found["id"]
    else:
        ideas = await _call("GET", f"/databases/{await db_id()}")
        parent = ideas["parent"].get("page_id") or await _first_shared_page()
        db = await _call(
            "POST",
            "/databases",
            {
                "parent": {"type": "page_id", "page_id": parent},
                "title": [{"type": "text", "text": {"content": SCHEDULE_TITLE}}],
                "properties": SCHEDULE_SCHEMA,
            },
        )
        _schedule_db_id = db["id"]
    return _schedule_db_id


def _slot(p: dict) -> dict:
    props = p["properties"]
    return {
        "page": p["id"],
        "title": _plain((props.get("Событие") or {}).get("title") or []) or "Без названия",
        "kind": ((props.get("Вид") or {}).get("select") or {}).get("name"),
        "days": [o["name"] for o in (props.get("Дни") or {}).get("multi_select") or []],
        "date": ((props.get("Дата") or {}).get("date") or {}).get("start"),
        "start": _text_prop(p, "Начало"),
        "end": _text_prop(p, "Конец"),
    }


async def schedule_slots(user_id: int) -> list[dict]:
    """Все строки расписания человека: их немного, поэтому без фильтра по датам."""
    slots, cursor = [], None
    while True:
        body = {"filter": {"property": "Автор ID", "number": {"equals": user_id}}, "page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = await _call("POST", f"/databases/{await schedule_db_id()}/query", body)
        slots += [_slot(p) for p in data["results"]]
        if not data.get("has_more"):
            return slots
        cursor = data["next_cursor"]


async def add_slot(user_id: int, title: str, kind: str, start: str = "", end: str = "", days: list[str] | None = None, date: str | None = None) -> None:
    props = {
        "Событие": {"title": [{"text": {"content": title[:200]}}]},
        "Вид": {"select": {"name": kind}},
        "Начало": {"rich_text": [{"text": {"content": start}}] if start else []},
        "Конец": {"rich_text": [{"text": {"content": end}}] if end else []},
        "Автор ID": {"number": user_id},
    }
    if days:
        props["Дни"] = {"multi_select": [{"name": d} for d in days]}
    if date:
        props["Дата"] = {"date": {"start": date}}
    await _call("POST", "/pages", {"parent": {"database_id": await schedule_db_id()}, "properties": props})


async def remove_slot(page_id: str) -> None:
    await trash(page_id)


async def dated_events(user_id: int, start: str, end: str) -> list[dict]:
    """События из заметок (тип со словом «событ» и сроком) за период — они попадают в расписание сами."""
    data = await _call(
        "POST",
        f"/databases/{await db_id()}/query",
        {
            "filter": {
                "and": [
                    {"property": c.P_WHEN, "date": {"on_or_after": start}},
                    {"property": c.P_WHEN, "date": {"on_or_before": end}},
                    _by_author(user_id),
                ]
            },
            "page_size": 100,
        },
    )
    items = [_item(p) for p in data["results"]]
    return [i for i in items if "событ" in ((i.get("type") or i.get("ai_type") or "").lower())]


# ---------- 😊 настроение и недельные отчёты ----------

MOOD_TITLE = "Настроение"
MOOD_SCHEMA = {"День": {"title": {}}, "Оценка": {"number": {}}, "Комментарий": {"rich_text": {}}, "Автор ID": {"number": {}}}
REPORTS_TITLE = "Недельные отчёты"
REPORTS_SCHEMA = {"Неделя": {"title": {}}, "Среднее настроение": {"number": {}}, "Автор ID": {"number": {}}}


async def _db_next_to_ideas(title: str, schema: dict) -> str:
    """Найти таблицу по названию или создать её рядом с «Входящими идеями»; пропавшие колонки вернуть."""
    if title in _extra_dbs:
        return _extra_dbs[title]
    found = next((d for d in await _search("database", title) if _plain(d["title"]) == title), None)
    if found:
        missing = {n: spec for n, spec in schema.items() if n not in found["properties"] and "title" not in spec}
        if missing:
            await _call("PATCH", f"/databases/{found['id']}", {"properties": missing})
        _extra_dbs[title] = found["id"]
    else:
        ideas = await _call("GET", f"/databases/{await db_id()}")
        parent = ideas["parent"].get("page_id") or await _first_shared_page()
        db = await _call(
            "POST",
            "/databases",
            {"parent": {"type": "page_id", "page_id": parent}, "title": [{"type": "text", "text": {"content": title}}], "properties": schema},
        )
        _extra_dbs[title] = db["id"]
    return _extra_dbs[title]


async def _mood_row(user_id: int, day: str) -> dict | None:
    data = await _call(
        "POST",
        f"/databases/{await _db_next_to_ideas(MOOD_TITLE, MOOD_SCHEMA)}/query",
        {"filter": {"and": [{"property": "День", "title": {"equals": day}}, {"property": "Автор ID", "number": {"equals": user_id}}]}, "page_size": 1},
    )
    return data["results"][0] if data["results"] else None


async def set_mood(user_id: int, day: str, score: int | None = None, comment: str | None = None) -> None:
    """Одна строка на человека и день: повторный ответ обновляет оценку или комментарий."""
    props = {}
    if score is not None:
        props["Оценка"] = {"number": score}
    if comment is not None:
        props["Комментарий"] = {"rich_text": [{"text": {"content": comment[:1500]}}]}
    row = await _mood_row(user_id, day)
    if row:
        await _call("PATCH", f"/pages/{row['id']}", {"properties": props})
    else:
        props.update({"День": {"title": [{"text": {"content": day}}]}, "Автор ID": {"number": user_id}})
        await _call("POST", "/pages", {"parent": {"database_id": await _db_next_to_ideas(MOOD_TITLE, MOOD_SCHEMA)}, "properties": props})


async def moods(user_id: int) -> dict[str, tuple[int | None, str]]:
    """{«ГГГГ-ММ-ДД»: (оценка, комментарий)} за последние ~100 записей."""
    data = await _call(
        "POST",
        f"/databases/{await _db_next_to_ideas(MOOD_TITLE, MOOD_SCHEMA)}/query",
        {"filter": {"property": "Автор ID", "number": {"equals": user_id}}, "sorts": [{"timestamp": "created_time", "direction": "descending"}], "page_size": 100},
    )
    result = {}
    for p in data["results"]:
        day = _plain(p["properties"]["День"]["title"])
        result[day] = ((p["properties"].get("Оценка") or {}).get("number"), _text_prop(p, "Комментарий"))
    return result


async def notes_created(user_id: int, start: str, end: str) -> list[dict]:
    """Заметки человека, созданные между start и end (ISO), с моментом создания — для недельного отчёта."""
    notes, cursor = [], None
    while True:
        body = {
            "filter": {
                "and": [
                    {"timestamp": "created_time", "created_time": {"on_or_after": start}},
                    {"timestamp": "created_time", "created_time": {"before": end}},
                    _by_author(user_id),
                ]
            },
            "page_size": 100,
        }
        if cursor:
            body["start_cursor"] = cursor
        data = await _call("POST", f"/databases/{await db_id()}/query", body)
        notes += [{**_item(p), "created_at": p["created_time"]} for p in data["results"]]
        if not data.get("has_more"):
            return notes
        cursor = data["next_cursor"]


def _table(rows: list[list[str]]) -> dict:
    """Настоящая таблица Notion: первая строка — заголовок."""
    width = len(rows[0])
    return {
        "object": "block",
        "type": "table",
        "table": {
            "table_width": width,
            "has_column_header": True,
            "has_row_header": False,
            "children": [
                {"object": "block", "type": "table_row", "table_row": {"cells": [[{"type": "text", "text": {"content": cell[:1900]}}] for cell in row]}}
                for row in rows
            ],
        },
    }


async def create_report(user_id: int, title: str, average: float | None, rows: list[list[str]], conclusion: list[dict], image_id: str | None) -> str:
    """Страница недельного отчёта: картинка, таблица по дням и вывод."""
    children = ([image_block(image_id)] if image_id else []) + [_table(rows)] + conclusion
    props = {"Неделя": {"title": [{"text": {"content": title}}]}, "Автор ID": {"number": user_id}}
    if average is not None:
        props["Среднее настроение"] = {"number": round(average, 1)}
    page = await _call(
        "POST",
        "/pages",
        {"parent": {"database_id": await _db_next_to_ideas(REPORTS_TITLE, REPORTS_SCHEMA)}, "properties": props, "children": children[:100]},
    )
    return page["url"]


async def done_between(user_id: int, start: str, end: str) -> list[dict]:
    """Выполненные заметки со сроком в этом промежутке — для колонки «Выполнено» в отчёте."""
    data = await _call(
        "POST",
        f"/databases/{await db_id()}/query",
        {
            "filter": {
                "and": [
                    {"property": c.P_WHEN, "date": {"on_or_after": start}},
                    {"property": c.P_WHEN, "date": {"before": end}},
                    {"property": c.P_DONE, "checkbox": {"equals": True}},
                    _by_author(user_id),
                ]
            },
            "page_size": 100,
        },
    )
    return [_item(p) for p in data["results"]]


# ---------- 📁 проекты-страницы и сферы ----------

PROJECTS_TITLE = "Проекты"
SPHERES_TITLE = "Сферы"
STATUS_ACTIVE, STATUS_CLOSED = "🟢 Активный", "✅ Закрыт"
KIND_SHARED, KIND_PERSONAL, KIND_TEAM = "🌐 Общий", "👤 Личный", "👥 Командный"
OLD_PROJECT = "Проект (старое)"
SPHERES_SCHEMA = {"Название": {"title": {}}, "Описание": {"rich_text": {}}}

_project_names: dict[str, str] = {}  # id без дефисов → название
_projects_cache: tuple[float, list[dict]] | None = None
PROJECTS_TTL = 60  # секунд


def _project_id(p: dict) -> str | None:
    rel = (p["properties"].get(c.P_PROJECT) or {}).get("relation") or []
    return rel[0]["id"].replace("-", "") if rel else None


def _forget_projects() -> None:
    global _projects_cache
    _projects_cache = None


def _project(p: dict) -> dict:
    props = p["properties"]
    sphere = (props.get("Сфера") or {}).get("relation") or []
    members = _text_prop(p, "Участники ID")
    return {
        "id": p["id"].replace("-", ""),
        "name": _plain((props.get("Название") or {}).get("title") or []) or "Без названия",
        "status": ((props.get("Статус") or {}).get("select") or {}).get("name") or STATUS_ACTIVE,
        "kind": ((props.get("Вид") or {}).get("select") or {}).get("name") or KIND_SHARED,
        "sphere_id": sphere[0]["id"].replace("-", "") if sphere else None,
        "members": [int(x) for x in members.replace(" ", "").split(",") if x.isdigit()],
        "creator": int((props.get("Создатель ID") or {}).get("number") or 0),
        "description": _text_prop(p, "Описание"),
        "url": p.get("url", ""),
    }


async def spheres_db_id() -> str:
    return await _db_next_to_ideas(SPHERES_TITLE, SPHERES_SCHEMA)


async def projects_db_id() -> str:
    schema = {
        "Название": {"title": {}},
        "Статус": {"select": {"options": [{"name": STATUS_ACTIVE, "color": "green"}, {"name": STATUS_CLOSED, "color": "gray"}]}},
        "Вид": {"select": {"options": [{"name": KIND_SHARED, "color": "blue"}, {"name": KIND_PERSONAL, "color": "yellow"}, {"name": KIND_TEAM, "color": "purple"}]}},
        "Сфера": {"relation": {"database_id": await spheres_db_id(), "single_property": {}}},
        "Участники ID": {"rich_text": {}},
        "Создатель ID": {"number": {}},
        "Описание": {"rich_text": {}},
    }
    return await _db_next_to_ideas(PROJECTS_TITLE, schema)


async def _load_projects(force: bool = False) -> list[dict]:
    global _projects_cache
    loop = asyncio.get_running_loop()
    if not force and _projects_cache and loop.time() - _projects_cache[0] < PROJECTS_TTL:
        return _projects_cache[1]
    rows, cursor = [], None
    while True:
        body = {"page_size": 100, "sorts": [{"timestamp": "created_time", "direction": "ascending"}]}
        if cursor:
            body["start_cursor"] = cursor
        data = await _call("POST", f"/databases/{await projects_db_id()}/query", body)
        rows += [_project(p) for p in data["results"]]
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    _project_names.clear()
    _project_names.update({p["id"]: p["name"] for p in rows})
    _projects_cache = (loop.time(), rows)
    return rows


def is_team_member(project: dict, user_id: int) -> bool:
    """Состоит ли человек в командном проекте (автор проекта тоже участник)."""
    return project["kind"] == KIND_TEAM and (user_id == project["creator"] or user_id in project["members"])


def team_people(project: dict) -> list[int]:
    """Кому приходят новости командного проекта: автор и участники."""
    return list(dict.fromkeys([project["creator"], *project["members"]]))


def can_see(project: dict, user_id: int | None) -> bool:
    """Владелица видит всё; общий — все; личный — автор; командный — его участники и автор."""
    if user_id is None or user_id == c.OWNER_ID or project["kind"] == KIND_SHARED:
        return True
    return user_id == project["creator"] or user_id in project["members"]


async def project_list(user_id: int | None = None, include_closed: bool = False) -> list[dict]:
    return [
        p for p in await _load_projects()
        if can_see(p, user_id) and (include_closed or p["status"] != STATUS_CLOSED)
    ]  # fmt: skip


async def get_project(name_or_id: str) -> dict | None:
    key = name_or_id.replace("-", "")
    rows = await _load_projects()
    return next((p for p in rows if p["id"] == key), None) or next((p for p in rows if p["name"].lower() == name_or_id.lower()), None)


async def create_project(name: str, kind: str, creator: int, members: list[int] | None = None) -> dict:
    props = {
        "Название": {"title": [{"text": {"content": name[:200]}}]},
        "Статус": {"select": {"name": STATUS_ACTIVE}},
        "Вид": {"select": {"name": kind}},
        "Создатель ID": {"number": creator},
        "Участники ID": {"rich_text": [{"text": {"content": ",".join(map(str, members or []))}}]},
    }
    page = await _call("POST", "/pages", {"parent": {"database_id": await projects_db_id()}, "properties": props})
    _forget_projects()
    return _project(page)


async def update_project(project_id: str, *, status: str | None = None, kind: str | None = None, description: str | None = None,
                         sphere_id: str | None = None, members: list[int] | None = None, name: str | None = None) -> None:  # fmt: skip
    props = {}
    if status:
        props["Статус"] = {"select": {"name": status}}
    if kind:
        props["Вид"] = {"select": {"name": kind}}
    if description is not None:
        props["Описание"] = {"rich_text": [{"text": {"content": description[:2000]}}]}
    if sphere_id is not None:
        props["Сфера"] = {"relation": [{"id": sphere_id}] if sphere_id else []}
    if members is not None:
        props["Участники ID"] = {"rich_text": [{"text": {"content": ",".join(map(str, members))}}]}
    if name:
        props["Название"] = {"title": [{"text": {"content": name[:200]}}]}
    await _call("PATCH", f"/pages/{project_id}", {"properties": props})
    _forget_projects()


async def project_note_count(project_id: str) -> int:
    data = await _call(
        "POST", f"/databases/{await db_id()}/query",
        {"filter": {"property": c.P_PROJECT, "relation": {"contains": project_id}}, "page_size": 100},
    )  # fmt: skip
    return len(data["results"])


async def spheres() -> list[dict]:
    data = await _call("POST", f"/databases/{await spheres_db_id()}/query", {"page_size": 100})
    return [
        {"id": p["id"].replace("-", ""), "name": _plain(p["properties"]["Название"]["title"]) or "Без названия", "description": _text_prop(p, "Описание")}
        for p in data["results"]
    ]  # fmt: skip


async def add_sphere(name: str, description: str = "") -> dict:
    page = await _call(
        "POST", "/pages",
        {"parent": {"database_id": await spheres_db_id()}, "properties": {
            "Название": {"title": [{"text": {"content": name[:200]}}]},
            "Описание": {"rich_text": [{"text": {"content": description[:2000]}}] if description else []},
        }},
    )  # fmt: skip
    return {"id": page["id"].replace("-", ""), "name": name, "description": description}


async def update_sphere(sphere_id: str, description: str) -> None:
    await _call("PATCH", f"/pages/{sphere_id}", {"properties": {"Описание": {"rich_text": [{"text": {"content": description[:2000]}}]}}})


async def ensure_projects() -> str:
    """Проекты-страницы. Один раз переносит старые проекты-метки: создаёт страницы, переименовывает старую
    колонку в «Проект (старое)», добавляет связь «Проект» и привязывает к ней заметки. Повторный вызов
    только проверяет, что связь на месте."""
    ideas = await _call("GET", f"/databases/{await db_id()}")
    props = ideas["properties"]
    projects_id = await projects_db_id()
    current = props.get(c.P_PROJECT)
    if current and current.get("type") == "relation":
        return "ok"
    report = []
    if current and current.get("type") == "select":
        names = [o["name"] for o in current["select"].get("options", [])]
        existing = {p["name"].lower() for p in await _load_projects(force=True)}
        for name in names:
            if name.lower() not in existing:
                await create_project(name, KIND_SHARED, c.OWNER_ID)
        await _call("PATCH", f"/databases/{ideas['id']}", {"properties": {c.P_PROJECT: {"name": OLD_PROJECT}}})
        report.append(f"перенесено проектов: {len(names)}")
    await _call(
        "PATCH", f"/databases/{ideas['id']}",
        {"properties": {c.P_PROJECT: {"relation": {"database_id": projects_id, "single_property": {}}}}},
    )  # fmt: skip
    if current and current.get("type") == "select":
        by_name = {p["name"].lower(): p["id"] for p in await _load_projects(force=True)}
        moved, cursor = 0, None
        while True:
            body = {"filter": {"property": OLD_PROJECT, "select": {"is_not_empty": True}}, "page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            data = await _call("POST", f"/databases/{ideas['id']}/query", body)
            for p in data["results"]:
                old = _select(p, OLD_PROJECT)
                if old and old.lower() in by_name:
                    await _call("PATCH", f"/pages/{p['id']}", {"properties": {c.P_PROJECT: {"relation": [{"id": by_name[old.lower()]}]}}})
                    moved += 1
            if not data.get("has_more"):
                break
            cursor = data["next_cursor"]
        report.append(f"привязано заметок: {moved}")
    return "; ".join(report) or "связь добавлена"
