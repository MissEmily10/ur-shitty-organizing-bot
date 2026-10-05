import asyncio
from datetime import datetime, timezone

import httpx

from . import config as c
from .markdown import toggle

API = "https://api.notion.com/v1"
DB_TITLE = "Входящие идеи"
MEMBERS_TITLE = "Участники бота"
DETAILS_TITLE = "📝 Полный текст и детали"


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
    c.P_AUTHOR: {"select": {}},
    c.P_AUTHOR_ID: {"number": {}},
    "Создано": {"created_time": {}},
}
# Колонки, которых не было в первой версии таблицы: бот дописывает их в старую таблицу сам
ADDED_LATER = (c.P_AUTHOR, c.P_AUTHOR_ID)

MEMBERS_SCHEMA = {
    "Имя": {"title": {}},
    "Telegram ID": {"number": {}},
    "Username": {"rich_text": {}},
    "Добавлен": {"created_time": {}},
}

_db_id: str | None = None
_members_db_id: str | None = None
_db_lock = asyncio.Lock()


def _plain(rich: list[dict]) -> str:
    return "".join(r["plain_text"] for r in rich)


async def _search(kind: str, query: str = "") -> list[dict]:
    body = {"filter": {"property": "object", "value": kind}, "page_size": 50}
    if query:
        body["query"] = query
    return [r for r in (await _call("POST", "/search", body))["results"] if not r.get("archived")]


async def _find_db(title: str) -> dict | None:
    for db in await _search("database", title):
        if _plain(db["title"]) == title:
            return db
    return None


async def _first_shared_page() -> str:
    pages = [p for p in await _search("page") if p["parent"]["type"] in ("workspace", "page_id")]
    if not pages:
        raise RuntimeError(
            "Интеграция Notion не видит ни одной страницы. "
            "Откройте нужную страницу → ••• → Connections → подключите интеграцию."
        )
    return pages[0]["id"]


async def _create_db(title: str, schema: dict, parent_page: str) -> str:
    db = await _call(
        "POST",
        "/databases",
        {
            "parent": {"type": "page_id", "page_id": parent_page},
            "title": [{"type": "text", "text": {"content": title}}],
            "properties": schema,
        },
    )
    return db["id"]


async def _ensure_columns(db: dict) -> None:
    missing = {name: SCHEMA[name] for name in ADDED_LATER if name not in db["properties"]}
    if missing:
        await _call("PATCH", f"/databases/{db['id']}", {"properties": missing})


async def db_id() -> str:
    """ID таблицы: из NOTION_DATABASE_ID, иначе ищем «Входящие идеи» среди доступного интеграции,
    иначе создаём таблицу на первой странице, к которой подключена интеграция."""
    global _db_id
    async with _db_lock:
        if _db_id:
            return _db_id
        db = None
        if c.NOTION_DATABASE_ID:
            try:
                db = await _call("GET", f"/databases/{c.NOTION_DATABASE_ID}")
            except NotionError as e:
                if e.status != 404:
                    raise
        db = db or await _find_db(DB_TITLE)
        if db:
            await _ensure_columns(db)
            _db_id = db["id"]
        else:
            _db_id = await _create_db(DB_TITLE, SCHEMA, await _first_shared_page())
        return _db_id


async def members_db_id() -> str:
    """Таблица «Участники бота»: ищем, иначе создаём рядом с «Входящими идеями»."""
    global _members_db_id
    if _members_db_id:
        return _members_db_id
    ideas = await _call("GET", f"/databases/{await db_id()}")
    async with _db_lock:
        if not _members_db_id:
            db = await _find_db(MEMBERS_TITLE)
            if db:
                _members_db_id = db["id"]
            else:
                parent = ideas["parent"].get("page_id") or await _first_shared_page()
                _members_db_id = await _create_db(MEMBERS_TITLE, MEMBERS_SCHEMA, parent)
    return _members_db_id


def _title(page: dict) -> str:
    return _plain(page["properties"][c.P_TITLE]["title"]) or "Без названия"


async def create_idea(
    title: str, source: str, tags: list[str], blocks: list[dict], details: list[dict], author_id: int, author: str
) -> str:
    """blocks видны сразу, details прячутся в свёрнутый блок «Полный текст и детали»."""
    props = {
        c.P_TITLE: {"title": [{"text": {"content": title[:200]}}]},
        c.P_STATUS: {"select": {"name": c.STATUS_NEW}},
        c.P_SOURCE: {"select": {"name": source}},
        c.P_TAGS: {"multi_select": [{"name": t} for t in tags]},
        c.P_AUTHOR: {"select": {"name": author.replace(",", " ")[:100]}},
        c.P_AUTHOR_ID: {"number": author_id},
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
    return page["url"]


async def _append(block_id: str, blocks: list[dict]) -> None:
    for i in range(0, len(blocks), 100):
        await _call("PATCH", f"/blocks/{block_id}/children", {"children": blocks[i : i + 100]})


def _by_author(user_id: int) -> dict:
    mine = {"property": c.P_AUTHOR_ID, "number": {"equals": user_id}}
    if user_id != c.OWNER_ID:
        return mine
    # Заметки, сохранённые до появления авторов, принадлежат владельцу
    return {"or": [mine, {"property": c.P_AUTHOR_ID, "number": {"is_empty": True}}]}


async def unsorted(user_id: int) -> list[dict]:
    """Неразобранные идеи этого человека, старые первыми."""
    data = await _call(
        "POST",
        f"/databases/{await db_id()}/query",
        {
            "filter": {"and": [{"property": c.P_STATUS, "select": {"equals": c.STATUS_NEW}}, _by_author(user_id)]},
            "sorts": [{"timestamp": "created_time", "direction": "ascending"}],
            "page_size": 100,
        },
    )
    return [{"id": p["id"].replace("-", ""), "title": _title(p), "url": p["url"]} for p in data["results"]]


async def created_today(user_id: int) -> int:
    """Сколько заметок человек сохранил сегодня (по UTC): для дневного лимита участников."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data = await _call(
        "POST",
        f"/databases/{await db_id()}/query",
        {
            "filter": {
                "and": [
                    {"property": c.P_AUTHOR_ID, "number": {"equals": user_id}},
                    {"timestamp": "created_time", "created_time": {"on_or_after": today}},
                ]
            },
            "page_size": 100,
        },
    )
    return len(data["results"])


# ---------- участники ----------


async def members() -> list[dict]:
    data = await _call("POST", f"/databases/{await members_db_id()}/query", {"page_size": 100})
    result = []
    for p in data["results"]:
        tg = p["properties"]["Telegram ID"]["number"]
        if tg:
            result.append({"page": p["id"], "tg": int(tg), "name": _plain(p["properties"]["Имя"]["title"]) or str(tg)})
    return result


async def add_member(tg: int, name: str, username: str | None) -> None:
    if any(m["tg"] == tg for m in await members()):
        return
    await _call(
        "POST",
        "/pages",
        {
            "parent": {"database_id": await members_db_id()},
            "properties": {
                "Имя": {"title": [{"text": {"content": name[:200]}}]},
                "Telegram ID": {"number": tg},
                "Username": {"rich_text": [{"text": {"content": f"@{username}"}}] if username else []},
            },
        },
    )


async def remove_member(tg: int) -> None:
    for m in await members():
        if m["tg"] == tg:
            await trash(m["page"])


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
    await _call(
        "PATCH",
        f"/pages/{page_id}",
        {"properties": {c.P_PROJECT: {"select": {"name": project}}, c.P_STATUS: {"select": {"name": c.STATUS_DONE}}}},
    )


async def trash(page_id: str) -> None:
    """В корзину Notion: оттуда можно восстановить в течение 30 дней."""
    await _call("PATCH", f"/pages/{page_id}", {"archived": True})
