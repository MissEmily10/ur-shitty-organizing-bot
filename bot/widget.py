"""🖥 Виджет на рабочем столе Mac (программа Übersicht): расписание на день и списки планов, «＋» и «☐ закрыть».

Виджет — папка widget/organizer.widget; бот присылает её владелице командой /widget уже со своим адресом внутри.
Адрес секретный (`widget:secret` в «Служебном», /widget new — новый, старый перестаёт работать), поэтому в чате
он виден только внутри файла. Всё работает для владелицы: виджет — её.

    GET  /w/<секрет>/day?d=ГГГГ-ММ-ДД     день (без d — сегодня, а после последнего занятия — завтра) и планы
    POST /w/<секрет>/add   {"text", "kind": tutor|me|lesson|task|note, "once": true|false}
    POST /w/<секрет>/done  {"id"}
    POST /w/<секрет>/tomorrow {"id"}     срок задачи — на завтра (время, если было, остаётся)
    POST /w/<секрет>/order {"ids": […]}  порядок планов, перетащенный руками (`plan_order:<uid>`)
    POST /w/<секрет>/mood  {"score": 1–5} настроение за сегодня (вместо вопроса в Telegram)
Тело POST — JSON строкой с типом text/plain: так браузер виджета не делает лишний предварительный запрос (CORS)."""

import io
import json
import logging
import secrets
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

from aiohttp import web
from telegram import Bot, ForceReply

from . import ai, changes, notion, schedule, scheduler, timetable, whenparse
from . import config as c
from .markdown import to_blocks

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
WIDGET_DIR = ROOT / "widget" / "organizer.widget"
DAYS_RU = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
DAYS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
PLANS_TTL = 60  # секунд: виджет спрашивает каждые несколько минут, Notion лишний раз не трогаем
CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST, OPTIONS", "Access-Control-Allow-Headers": "Content-Type"}

_secret: list[str] = []
_plans_cache: dict[int, tuple[float, list[dict]]] = {}


async def secret(new: bool = False) -> str:
    if new or not _secret:
        value = None if new else await notion.get_value("widget:secret")
        if not value:
            value = secrets.token_urlsafe(24)
            await notion.set_value("widget:secret", value)
        _secret[:] = [value]
    return _secret[0]


def url(token: str) -> str:
    return f"{(c.WEBHOOK_BASE or 'http://localhost:8080').rstrip('/')}/w/{token}"


EXTRAS = ROOT / "widget"  # рядом с папкой виджета: горячая клавиша и виджет для iPhone


def package(token: str, bot_username: str = "") -> bytes:
    """ZIP: папка виджета (адрес вписан в index.jsx), инструкция для горячей клавиши заметки (с адресом)
    и скрипт виджета для iPhone (Scriptable)."""
    fill = {b"__BOT_URL__": url(token).encode(), b"__BOT_USERNAME__": bot_username.encode()}

    def filled(data: bytes) -> bytes:
        for key, value in fill.items():
            data = data.replace(key, value)
        return data

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in WIDGET_DIR.rglob("*"):
            if path.is_file():
                z.writestr(f"organizer.widget/{path.relative_to(WIDGET_DIR)}", filled(path.read_bytes()))
        for name in ("Горячая клавиша для заметки.txt", "Karabiner — T+G+Enter.json", "iPhone — Мой день.js"):
            if (EXTRAS / name).is_file():
                z.writestr(name, filled((EXTRAS / name).read_bytes()))
    return buf.getvalue()


# ---------- что показывает виджет ----------


def _item(i: dict, kind: str) -> dict:
    return {"start": i.get("start") or "", "end": i.get("end") or "", "title": i["title"], "kind": kind,
            "online": (i.get("room") or "").lower() == "онлайн", "teacher": i.get("teacher") or "",
            "link": i.get("link") or "", "once": bool(i.get("date"))}  # fmt: skip


async def day_items(uid: int, day: date) -> list[dict]:
    """Занятия «Моего расписания» и события общего расписания за день, по времени."""
    items = []
    tt = await timetable.load_cached(uid)
    if tt:
        for i in timetable.for_day(tt, day):
            items.append(_item(i, "tutor" if timetable.is_tutor(i) else "lesson" if i["lesson"] else "other"))
    for o in await schedule.occurrences(uid, day, day):
        if o.title.startswith(timetable.SLOT_MARK):
            continue  # это строки из «Моего расписания» — уже показаны выше
        items.append({"start": o.start, "end": o.end, "title": o.title, "kind": {"regular": "routine", "once": "event"}.get(o.kind, "note"),
                      "online": False, "teacher": "", "link": "", "once": o.kind != "regular"})  # fmt: skip
    return sorted(items, key=lambda x: x["start"] or "00:00")


async def plans(uid: int, today: date) -> list[dict]:
    """Открытые заметки по порядку важности (как карточки): горит → скоро → потом; подзадачи — внутри."""
    now = datetime.now().timestamp()
    if uid in _plans_cache and now - _plans_cache[uid][0] < PLANS_TTL:
        return _plans_cache[uid][1]
    notes = await notion.open_notes(uid)
    by_id = {n["id"]: n for n in notes}
    projects = {p["id"]: p for p in await notion.project_list(uid, include_closed=True)}
    areas = {a["id"]: a["name"] for a in await notion.spheres()}
    out = []
    for n in notes:
        if n.get("parent_id") in by_id:
            continue
        project = projects.get(n.get("project_id") or "")
        sphere = areas.get((project or {}).get("sphere_id") or "")
        when, urgency = whenparse.urgency(n["when"], today) if n.get("when") else ("", "later")
        out.append({"id": n["id"], "title": n["title"], "type": n.get("type") or n.get("ai_type") or "", "when": when,
                    "urgency": urgency, "where": " › ".join(x for x in (sphere, project["name"] if project else "") if x),
                    "steps": [{"id": k["id"], "title": k["title"]} for k in notes if k.get("parent_id") == n["id"]],
                    "_sort": n.get("when") or "9999"})  # fmt: skip
    rank = {"fire": 0, "soon": 1, "later": 2}
    out.sort(key=lambda x: (rank[x["urgency"]], x.pop("_sort")))
    out = _manual_order(out, json.loads(await notion.get_value(f"plan_order:{uid}") or "[]"))
    _plans_cache[uid] = (now, out)
    return out


def _manual_order(items: list[dict], order: list[str]) -> list[dict]:
    """Порядок, перетащенный владелицей, поверх «горит → скоро → потом»: заметки, которые она двигала, стоят так,
    как она поставила, а новые остаются на своих местах по важности."""
    pos = {pid: k for k, pid in enumerate(order)}
    moved = sorted((x for x in items if x["id"] in pos), key=lambda x: pos[x["id"]])
    it = iter(moved)
    return [next(it) if x["id"] in pos else x for x in items]


async def view(uid: int, day: date | None) -> dict:
    now = scheduler.local_now(await scheduler.get_settings(uid))
    today = now.date()
    auto = day is None
    day = day or today
    items = await day_items(uid, day)
    if auto and items:
        # после последнего занятия дня — сразу завтрашний день
        last = max((x["end"] or x["start"] for x in items), default="")
        if last and last <= f"{now:%H:%M}" and now.hour >= 12:
            day = today + timedelta(days=1)
            items = await day_items(uid, day)
    settings = await scheduler.get_settings(uid)
    mood = (await notion.moods(uid)).get(today.isoformat(), (None, ""))[0]
    return {
        "mood": {"ask": settings.get("mood", True) and f"{now:%H:%M}" >= settings.get("evening", "20:00"), "score": mood},
        "date": day.isoformat(), "today": day == today, "now": f"{now:%H:%M}" if day == today else "",
        "title": f"{DAYS_EN[day.weekday()]} {day:%d.%m}", "title_ru": f"{DAYS_RU[day.weekday()]}, {day:%d.%m}",
        "relative": {0: "сегодня", 1: "завтра", -1: "вчера"}.get((day - today).days, ""),
        "items": items, "plans": await plans(uid, today),
    }  # fmt: skip


# ---------- что виджет меняет ----------


async def add(bot: Bot, uid: int, text: str, kind: str, once: bool) -> str:
    """«＋» из виджета: событие в расписание или новая заметка. Возвращает, что сделано (для подсказки в виджете)."""
    now = scheduler.local_now(await scheduler.get_settings(uid))
    if kind == "note":
        idea = await ai.structure(text=text, types=await notion.types(), now=now)
        _, page_url = await notion.create_idea(idea.title, "Текст", idea.tags, to_blocks(idea.summary), to_blocks(text), uid,
                                               "Владелица", when=idea.when, ai_type=idea.type_guess)  # fmt: skip
        _plans_cache.pop(uid, None)
        await bot.send_message(uid, f"🖥 Из виджета — новая заметка: ✅ {idea.title}\n{page_url}", disable_web_page_preview=True)
        return f"Заметка «{idea.title}» сохранена"
    change = await changes.prepare(uid, text, kind, once, now, uid == c.OWNER_ID)
    if not change["text"]:
        raise ValueError("Не поняла, что и когда. Например: «в пятницу 19:00 китайский».")
    await changes.apply(uid, change, now.date())
    timetable._cache.pop(uid, None)
    await bot.send_message(uid, "🖥 Из виджета добавила в расписание:\n\n" + change["text"])
    for title in change.get("no_link", []):
        await bot.send_message(uid, changes.link_question(title), reply_markup=ForceReply(input_field_placeholder="https://…"))
    return change["text"].split("\n\n")[0]


async def tomorrow(uid: int, page_id: str) -> str:
    """«⏰ на завтра»: срок — завтра; если у срока было время, оно сохраняется."""
    item = await notion.page_info(page_id)
    if uid != c.OWNER_ID and item.get("author_id") != uid:
        raise PermissionError("Это не ваша заметка.")
    now = scheduler.local_now(await scheduler.get_settings(uid))
    day = now.date() + timedelta(days=1)
    when = item.get("when") or ""
    new = datetime.combine(day, datetime.fromisoformat(when).timetz()).isoformat() if "T" in when else day.isoformat()
    await notion.set_when(page_id, new)
    _plans_cache.pop(uid, None)
    return new


async def done(uid: int, page_id: str) -> None:
    item = await notion.page_info(page_id)
    if uid != c.OWNER_ID and item.get("author_id") != uid:
        raise PermissionError("Это не ваша заметка.")
    await notion.set_done(page_id, True)
    _plans_cache.pop(uid, None)


def routes(bot: Bot) -> list:
    uid = c.OWNER_ID

    async def check(request: web.Request) -> None:
        if not secrets.compare_digest(request.match_info["secret"], await secret()):
            raise web.HTTPNotFound()

    async def body(request: web.Request) -> dict:
        try:
            data = json.loads(await request.text() or "{}")
        except json.JSONDecodeError:
            raise web.HTTPBadRequest(text="bad json")
        return data if isinstance(data, dict) else {}

    def reply(data: dict, status: int = 200) -> web.Response:
        return web.json_response(data, status=status, headers=CORS, dumps=lambda x: json.dumps(x, ensure_ascii=False))

    async def get_day(request: web.Request) -> web.Response:
        await check(request)
        try:
            day = date.fromisoformat(request.query["d"]) if request.query.get("d") else None
        except ValueError:
            day = None
        try:
            return reply(await view(uid, day))
        except Exception as e:
            log.exception("widget day failed")
            return reply({"error": str(e)[:300]}, 500)

    async def post_add(request: web.Request) -> web.Response:
        await check(request)
        data = await body(request)
        text, kind = str(data.get("text") or "").strip()[:2000], str(data.get("kind") or "note")
        if not text or kind not in (*schedule.CATEGORIES, "note"):
            return reply({"error": "Пусто или непонятный тип"}, 400)
        try:
            return reply({"ok": True, "message": await add(bot, uid, text, kind, bool(data.get("once", True)))})
        except ValueError as e:
            return reply({"error": str(e)}, 400)
        except Exception as e:
            log.exception("widget add failed")
            return reply({"error": f"Не получилось: {str(e)[:200]}"}, 500)

    async def post_done(request: web.Request) -> web.Response:
        await check(request)
        page_id = str((await body(request)).get("id") or "")
        if not page_id.isalnum():
            return reply({"error": "нет id"}, 400)
        try:
            await done(uid, page_id)
        except Exception as e:
            log.exception("widget done failed")
            return reply({"error": str(e)[:200]}, 500)
        return reply({"ok": True})

    async def post_tomorrow(request: web.Request) -> web.Response:
        await check(request)
        page_id = str((await body(request)).get("id") or "")
        if not page_id.isalnum():
            return reply({"error": "нет id"}, 400)
        try:
            return reply({"ok": True, "when": await tomorrow(uid, page_id)})
        except Exception as e:
            log.exception("widget tomorrow failed")
            return reply({"error": str(e)[:200]}, 500)

    async def post_order(request: web.Request) -> web.Response:
        await check(request)
        ids = [str(x) for x in (await body(request)).get("ids") or [] if str(x).isalnum()][:300]
        await notion.set_value(f"plan_order:{uid}", json.dumps(ids))
        _plans_cache.pop(uid, None)
        return reply({"ok": True})

    async def post_mood(request: web.Request) -> web.Response:
        await check(request)
        try:
            score = int((await body(request)).get("score"))
        except (TypeError, ValueError):
            score = 0
        if not 1 <= score <= 5:
            return reply({"error": "оценка 1–5"}, 400)
        today = scheduler.local_now(await scheduler.get_settings(uid)).date().isoformat()
        await notion.set_mood(uid, today, score)
        return reply({"ok": True})

    async def options(request: web.Request) -> web.Response:
        return web.Response(headers=CORS)

    return [
        web.get("/w/{secret}/day", get_day),
        web.post("/w/{secret}/add", post_add),
        web.post("/w/{secret}/done", post_done),
        web.post("/w/{secret}/tomorrow", post_tomorrow),
        web.post("/w/{secret}/order", post_order),
        web.post("/w/{secret}/mood", post_mood),
        web.options("/w/{secret}/{action}", options),
    ]
