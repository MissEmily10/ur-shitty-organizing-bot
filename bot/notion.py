import asyncio

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


async def _call(method: str, path: str, json: dict | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {c.NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.request(method, f"{API}{path}", headers=headers, json=json)
    if r.is_error:
        raise NotionError(r.status_code, r.text)
    return r.json()


# ---------- поиск или создание таблицы ----------

SCHEMA = {
    c.P_TITLE: {"title": {}},
    c.P_STATUS: {"select": {"options": [{"name": c.STATUS_NEW, "color": "red"}, {"name": c.STATUS_DONE, "color": "green"}]}},
    c.P_PROJECT: {"select": {"options": [{"name": "Пример проекта", "color": "blue"}]}},
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
    "Создано": {"created_time": {}},
}
# Колонки, которых не было в первой версии таблицы: бот дописывает их в существующую таблицу сам
ADDED_LATER = (c.P_TYPE,)

_db_id: str | None = None
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
            missing = {name: SCHEMA[name] for name in ADDED_LATER if name not in found["properties"]}
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
    return _plain(page["properties"][c.P_TITLE]["title"]) or "Без названия"


async def upload_image(data: bytes, filename: str) -> str:
    """Загружает картинку в Notion, возвращает id для блока image. На бесплатном Notion лимит 5 МБ на файл."""
    headers = {"Authorization": f"Bearer {c.NOTION_TOKEN}", "Notion-Version": FILES_VERSION}
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{API}/file_uploads", headers=headers, json={"filename": filename, "content_type": "image/jpeg"})
        if r.is_error:
            raise NotionError(r.status_code, r.text)
        upload_id = r.json()["id"]
        r = await client.post(
            f"{API}/file_uploads/{upload_id}/send", headers=headers, files={"file": (filename, data, "image/jpeg")}
        )
        if r.is_error:
            raise NotionError(r.status_code, r.text)
    return upload_id


def image_block(upload_id: str) -> dict:
    return {"object": "block", "type": "image", "image": {"type": "file_upload", "file_upload": {"id": upload_id}}}


async def create_idea(title: str, source: str, tags: list[str], blocks: list[dict], details: list[dict]) -> tuple[str, str]:
    """blocks видны сразу (сюда же идут картинки-оригиналы), details прячутся в свёрнутый блок «Полный текст и детали»."""
    props = {
        c.P_TITLE: {"title": [{"text": {"content": title[:200]}}]},
        c.P_STATUS: {"select": {"name": c.STATUS_NEW}},
        c.P_SOURCE: {"select": {"name": source}},
        c.P_TAGS: {"multi_select": [{"name": t} for t in tags]},
    }
    page = await _call(
        "POST",
        "/pages",
        {"parent": {"database_id": await db_id()}, "properties": props, "children": blocks[:100]},
    )
    await _append(page["id"], blocks[100:])
    if details:
        added = await _call("PATCH", f"/blocks/{page['id']}/children", {"children": [toggle(DETAILS_TITLE, details)]})
        await _append(added["results"][-1]["id"], details[100:])
    return page["id"].replace("-", ""), page["url"]


async def _append(block_id: str, blocks: list[dict]) -> None:
    for i in range(0, len(blocks), 100):
        await _call("PATCH", f"/blocks/{block_id}/children", {"children": blocks[i : i + 100]})


def _item(p: dict) -> dict:
    project = p["properties"][c.P_PROJECT]["select"]
    return {
        "id": p["id"].replace("-", ""),
        "title": _title(p),
        "url": p["url"],
        "project": project["name"] if project else None,
        "type": ((p["properties"].get(c.P_TYPE) or {}).get("select") or {}).get("name"),
    }


async def page_info(page_id: str) -> dict:
    return _item(await _call("GET", f"/pages/{page_id}"))


async def page_project(page_id: str) -> str | None:
    page = await _call("GET", f"/pages/{page_id}")
    project = page["properties"][c.P_PROJECT]["select"]
    return project["name"] if project else None


async def review_items() -> list[dict]:
    """Для вечернего разбора: все заметки, которым ещё не выбран тип (проект может уже стоять). Старые первыми.
    Заметки со статусом «Разобрано» из времён до типов тоже считаются разобранными."""
    data = await _call(
        "POST",
        f"/databases/{await db_id()}/query",
        {
            "filter": {
                "and": [
                    {"property": c.P_TYPE, "select": {"is_empty": True}},
                    {"property": c.P_STATUS, "select": {"does_not_equal": c.STATUS_DONE}},
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


async def _project_options() -> list[dict]:
    db = await _call("GET", f"/databases/{await db_id()}")
    return db["properties"][c.P_PROJECT]["select"]["options"]


async def _set_project_options(options: list[dict]) -> None:
    # Notion заменяет список вариантов целиком: существующие передаём с их id, иначе они удалятся
    await _call("PATCH", f"/databases/{await db_id()}", {"properties": {c.P_PROJECT: {"select": {"options": options}}}})


async def projects() -> list[str]:
    """Проекты = варианты поля «Проект». Добавили вариант в Notion или через /addproject — появилась кнопка в боте."""
    return [o["name"] for o in await _project_options()]


async def add_projects(names: list[str]) -> list[str]:
    """Добавляет новые проекты, возвращает те, которых ещё не было."""
    options = await _project_options()
    taken = {o["name"].lower() for o in options}
    new = []
    for name in names:
        if name.lower() not in taken:
            taken.add(name.lower())
            new.append(name)
    if new:
        keep = [{"id": o["id"], "name": o["name"], "color": o["color"]} for o in options]
        await _set_project_options(keep + [{"name": n} for n in new])
    return new


async def delete_project(name: str) -> None:
    options = await _project_options()
    keep = [{"id": o["id"], "name": o["name"], "color": o["color"]} for o in options if o["name"] != name]
    await _set_project_options(keep)


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


async def file_to_project(page_id: str, project: str) -> None:
    """Только проект: из разбора заметка уходит, когда ей выбран тип."""
    await _call("PATCH", f"/pages/{page_id}", {"properties": {c.P_PROJECT: {"select": {"name": project}}}})


async def types() -> list[str]:
    db = await _call("GET", f"/databases/{await db_id()}")
    return [o["name"] for o in db["properties"][c.P_TYPE]["select"]["options"]]


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
