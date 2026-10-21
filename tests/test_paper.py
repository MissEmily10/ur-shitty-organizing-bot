"""✏️ Зачёркнутое без номеров: метки по углам листа → место заметки → следы ручки (bot/paper.py);
названия зачёркнутых → номера (ai.match_titles); утренняя просьба прислать фото распечатки (scheduler)."""

import asyncio
import io
import time
from datetime import datetime, timezone

import cv2
import numpy as np

import harness as h

from bot import ai, main, notion, paper, pdf, scheduler

S = paper.SCALE


def sheet_with_markers(page: int) -> np.ndarray:
    """Чистый выпрямленный лист с метками, как их печатает pdf._markers."""
    sheet = np.full((int(pdf.PORTRAIT_H * S), int(pdf.PORTRAIT_W * S)), 255, np.uint8)
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_250)
    for corner, (x, y) in enumerate(pdf.marker_corners()):
        size = int(pdf.MARKER * S)
        marker = cv2.aruco.generateImageMarker(dictionary, page * 4 + corner, size)
        left, top = int(x * S), int((pdf.PORTRAIT_H - y - pdf.MARKER) * S)
        sheet[top : top + size, left : left + size] = marker
    return sheet


def card(num: int, top: float) -> dict:
    """Карточка в раскладке: область 500 × 40 pt, напечатанный «текст» — одна строка слева."""
    band = [60, top - 40, 560, top]
    return {"num": num, "page": 1, "band": band, "ink": [[90, top - 16, 300, top - 6]], "check": [70, top - 15, 75, top - 10]}


def px(x: float, y: float) -> tuple[int, int]:
    return int(x * S), int((pdf.PORTRAIT_H - y) * S)


async def run():
    # 1. раскладка ужимается и разжимается без потерь
    layout = [card(5, 700), card(6, 640), card(7, 580)]
    assert paper.unpack(paper.pack(layout)) == layout

    # 2. лист 2 (метки 4–7): «текст» напечатан у всех, #6 зачёркнута крест-накрест, у #7 галочка; снимок под углом
    sheet = sheet_with_markers(1)
    for c in layout:
        x1, y1, x2, y2 = c["ink"][0]
        cv2.rectangle(sheet, px(x1, y2), px(x2, y1), 30, -1)
    x1, y1, x2, y2 = layout[1]["band"]
    cv2.line(sheet, px(x1, y2), px(x2, y1), 40, 5)
    cv2.line(sheet, px(x1, y1), px(x2, y2), 40, 5)
    cx1, cy1, cx2, cy2 = layout[2]["check"]
    cv2.line(sheet, px(cx1 - 1, cy2 + 1), px(cx2 + 1, cy1 - 1), 40, 4)
    h_, w_ = sheet.shape
    src = np.float32([[0, 0], [w_, 0], [w_, h_], [0, h_]])
    dst = np.float32([[80, 40], [w_ - 30, 90], [w_ - 60, h_ - 20], [30, h_ - 70]])
    photo = cv2.warpPerspective(sheet, cv2.getPerspectiveTransform(src, dst), (w_ + 40, h_ + 40), borderValue=120)
    ok, jpg = cv2.imencode(".jpg", photo, [cv2.IMWRITE_JPEG_QUALITY, 85])
    assert paper.find_crossed([jpg.tobytes()], layout) == {6, 7}
    # без меток (старая распечатка) — ничего не находит и не падает
    ok, plain = cv2.imencode(".jpg", np.full((400, 300), 255, np.uint8))
    assert paper.find_crossed([plain.tobytes(), b"not an image"], layout) == set()

    # 3. настоящий PDF: метки на каждом листе, раскладка заметок записана
    notes = [{"num": i, "title": f"Заметка {i}", "type": "📋 Задача", "summary": "суть", "children": []} for i in range(1, 4)]
    placed: list[dict] = []
    data = pdf.notes_pdf([("📚 Образование", [("Домашка", notes)])], "t", "s", placed)
    assert data[:4] == b"%PDF" and [p["num"] for p in placed] == [1, 2, 3] and all(p["page"] == 0 for p in placed)

    # 4. зачёркнуто целиком вместе с номером — ИИ узнаёт по названию
    titles = {1: "Разобрать дизайн-проекты", 2: "Позвонить в поликлинику", 3: "Купить объектив"}
    assert ai.match_titles(["разобрать дизайн проекты!", "Купить объектив 50мм", "что-то другое"], titles) == {1, 3}

    # 5. утренняя просьба: в назначенный день после 9:00 — сообщение, и фото весь день считаются листками распечатки
    app = await h.make_app()
    scheduler.JOBS[:] = [j for j in scheduler.JOBS if j.name == "printout_ask"]  # остальные задачи ходят в Notion

    async def nothing(*a, **k):
        return []
    scheduler._reminders, scheduler._bells = nothing, nothing
    notion.forget_old_marks = lambda days=14: asyncio.sleep(0, 0)
    await notion.set_value("print:42", '{"date": "20.10", "map": {"1": "' + "1" * 32 + '"}}')
    await scheduler.update_settings(42, printout_ask="2026-10-21")
    await scheduler.tick(app.bot, [42], now=datetime(2026, 10, 21, 5, 0, tzinfo=timezone.utc))  # 8:00 МСК — рано
    assert not [t for t in h.texts(chat=42) if "синхронизируемся" in t]
    await scheduler.tick(app.bot, [42], now=datetime(2026, 10, 21, 6, 5, tzinfo=timezone.utc))  # 9:05 МСК
    asked = [t for t in h.texts(chat=42) if "синхронизируемся" in t]
    assert len(asked) == 1 and (await scheduler.get_settings(42))["printout_ask"] == ""
    until = datetime.fromtimestamp(float(h.service["crossing:42"]), timezone.utc)
    assert until == datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc), "до 23:00 МСК"
    # переключатель, записанный планировщиком прямо в Notion, бот подхватывает (перечитывает не чаще раза в минуту)
    main._crossing.clear()
    main._crossing_checked.clear()
    h.service["crossing:42"] = str(int(time.time()) + 3600)
    assert await main._crossing_on(42)
    await scheduler.tick(app.bot, [42], now=datetime(2026, 10, 22, 6, 5, tzinfo=timezone.utc))
    assert len([t for t in h.texts(chat=42) if "синхронизируемся" in t]) == 1, "только один раз"
    print("paper OK")


asyncio.run(run())
