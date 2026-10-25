"""✏️ Новое в расписании: «что это» (репетитор / засечка / доп. урок / доп. дело) и «разово или каждую неделю».
Общее для «✏️ Изменить» в /schedule и «＋» в виджете на рабочем столе.

Куда попадает: у владелицы репетиторы и доп. уроки — в «🎒 Моё расписание» (там звонки, ссылка на вход за 5 минут
и «сделала домашку?»; у репетитора ссылка постоянная, даже если время каждый раз разное), всё остальное — в общее
расписание (/schedule) со значком категории в названии."""

from datetime import date, datetime, timedelta

from . import ai, notion, schedule, timetable
from .notion import WEEKDAYS

TT_KINDS = ("tutor", "lesson")
TUTOR_LINK_PROMPT = "🔗 Ссылка на занятия «"  # ответ на этот вопрос бот узнаёт по началу текста (main._tutor_link_reply)


def link_question(title: str) -> str:
    return (f"{TUTOR_LINK_PROMPT}{title}» — пришли постоянную ссылку на вход ответом на это сообщение: буду присылать её "
            "за 5 минут до каждого занятия, даже если время каждый раз разное. Нет ссылки — просто пропусти.")


def _next_date(day_name: str, today: date) -> str | None:
    """«Пт» → ближайшая пятница начиная с сегодня."""
    d = schedule._norm_day(day_name)
    if not d:
        return None
    shift = (WEEKDAYS.index(d) - today.weekday()) % 7
    return (today + timedelta(days=shift)).isoformat()


def normalize(plan: dict, once: bool | None, today: date) -> dict:
    """Владелица уже сказала «разово» или «каждую неделю» — ИИ мог понять иначе, приводим к её ответу."""
    if once is None:
        return plan
    plan = {**plan, "base_add": list(plan.get("base_add") or []), "add": list(plan.get("add") or [])}
    if once:
        for b in plan["base_add"]:
            for d in b.get("days") or []:
                if day := _next_date(d, today):
                    plan["add"].append({"title": b.get("title"), "date": day, "start": b.get("start", ""), "end": b.get("end", "")})
        plan["base_add"] = []
    else:
        for a in plan["add"]:
            weekday = WEEKDAYS[date.fromisoformat(a["date"][:10]).weekday()]
            plan["base_add"].append({"title": a.get("title"), "days": [weekday], "start": a.get("start", ""), "end": a.get("end", "")})
        plan["add"] = []
    return plan


def _time(x: dict) -> str:
    return f"{x.get('start', '')}–{x['end']}" if x.get("end") else x.get("start", "") or "весь день"


async def prepare(uid: int, text: str, category: str | None, once: bool | None, now: datetime, owner: bool) -> dict:
    """Понять изменение: {"route": "tt" | "schedule", "plan": …, "items": […] (для tt), "text": что поняли, "no_link": [названия]}.
    Пустой "text" — не поняли."""
    known = schedule.CATEGORIES.get(category or "")
    hint = ""
    if known:
        hint = f"\n(Это: {known[1]}." + ({True: " Разово, в конкретный день.", False: " Каждую неделю.", None: ""}[once]) + ")"
    base = schedule.base_text(await notion.schedule_slots(uid))
    plan = normalize(await ai.parse_schedule(text + hint, base, now, False), once, now.date())
    if owner and category in TT_KINDS and (plan["base_add"] or plan["add"]):
        data = await timetable.load(uid) or {"items": []}
        tutor = category == "tutor"
        items = [timetable.make_item(data, b["title"], b.get("start", ""), b.get("end", ""), tutor=tutor,
                                     days=[d for d in (schedule._norm_day(x) for x in b.get("days", [])) if d])
                 for b in plan["base_add"] if b.get("title") and b.get("days")]  # fmt: skip
        items += [timetable.make_item(data, a["title"], a.get("start", ""), a.get("end", ""), tutor=tutor, day=a["date"][:10])
                  for a in plan["add"] if a.get("title")]  # fmt: skip
        icon = known[0] if known else "🎒"
        lines = [
            (f"📌 {date.fromisoformat(i['date']):%d.%m}" if i.get("date") else f"🔁 {', '.join(i['days'])}")
            + f" {_time(i)} — {icon} {i['title']}" + (" 🔗" if i.get("link") else "")
            for i in items
        ]
        no_link = sorted({i["title"] for i in items if i.get("tutor") and not i.get("link")})
        text_out = "\n".join(lines) + "\n\nКуда: «🎒 Моё расписание» — со звонками" + (
            ", ссылкой на вход и вопросом про домашку" if tutor else "") + "."
        return {"route": "tt", "items": items, "plan": plan, "text": text_out if items else "", "no_link": no_link}
    for item in plan.get("base_add", []) + plan.get("add", []):
        if isinstance(item, dict) and item.get("title"):
            item["title"] = schedule.mark(item["title"], category)
    return {"route": "schedule", "plan": plan, "text": plan_text(plan), "no_link": []}


def plan_text(plan: dict) -> str:
    lines = []
    if plan.get("replace_base"):
        lines.append("Новое базовое расписание (старое заменится):")
    for b in plan.get("base_add", []):
        lines.append(f"🔁 {', '.join(b.get('days', []))} {_time(b)} — {b.get('title')}")
    for name in plan.get("base_remove", []):
        lines.append(f"🗑 убрать из базового: {name}")
    for e in plan.get("add", []):
        lines.append(f"📌 {e['date']} {_time(e) if e.get('start') else ''} — {e.get('title')}".replace("  ", " "))
    for x in plan.get("cancel", []):
        lines.append(f"🚫 {x['date']}: без «{x.get('title')}»")
    return "\n".join(lines)


async def apply(uid: int, change: dict, today: date) -> list[str]:
    """Применить понятое. Возвращает, что сделано."""
    if change["route"] == "tt":
        await timetable.add_items(uid, change["items"], today)
        return [line for line in change["text"].split("\n\n")[0].splitlines()]
    return await schedule.apply(uid, change["plan"])
