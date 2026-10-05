import httpx

from . import config as c

API = "https://api.notion.com/v1"


async def _call(method: str, path: str, json: dict | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {c.NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.request(method, f"{API}{path}", headers=headers, json=json)
    if r.is_error:
        raise RuntimeError(f"Notion {r.status_code}: {r.text[:300]}")
    return r.json()


def _title(page: dict) -> str:
    parts = page["properties"][c.P_TITLE]["title"]
    return "".join(p["plain_text"] for p in parts) or "Без названия"


async def create_idea(title: str, source: str, tags: list[str], blocks: list[dict]) -> str:
    props = {
        c.P_TITLE: {"title": [{"text": {"content": title[:200]}}]},
        c.P_STATUS: {"select": {"name": c.STATUS_NEW}},
        c.P_SOURCE: {"select": {"name": source}},
        c.P_TAGS: {"multi_select": [{"name": t} for t in tags]},
    }
    page = await _call(
        "POST",
        "/pages",
        {"parent": {"database_id": c.NOTION_DATABASE_ID}, "properties": props, "children": blocks[:100]},
    )
    for i in range(100, len(blocks), 100):
        await _call("PATCH", f"/blocks/{page['id']}/children", {"children": blocks[i : i + 100]})
    return page["url"]


async def unsorted() -> list[dict]:
    """Неразобранные идеи, старые первыми."""
    data = await _call(
        "POST",
        f"/databases/{c.NOTION_DATABASE_ID}/query",
        {
            "filter": {"property": c.P_STATUS, "select": {"equals": c.STATUS_NEW}},
            "sorts": [{"timestamp": "created_time", "direction": "ascending"}],
            "page_size": 100,
        },
    )
    return [{"id": p["id"].replace("-", ""), "title": _title(p), "url": p["url"]} for p in data["results"]]


async def projects() -> list[str]:
    """Проекты = варианты поля «Проект». Добавили вариант в Notion — появилась кнопка в боте."""
    db = await _call("GET", f"/databases/{c.NOTION_DATABASE_ID}")
    return [o["name"] for o in db["properties"][c.P_PROJECT]["select"]["options"]]


async def preview(page_id: str, limit: int = 600) -> str:
    data = await _call("GET", f"/blocks/{page_id}/children?page_size=30")
    lines = []
    for b in data["results"]:
        rich = b.get(b["type"], {}).get("rich_text")
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
