"""✏️ Зачёркнутое на распечатке «Все мои заметки» — без чтения номеров.

По углам каждого листа напечатаны ArUco-метки (pdf.marker_corners): номер метки = номер листа × 4 + угол.
По ним OpenCV находит лист на фото и выпрямляет его, а раскладка из pdf.notes_pdf говорит, где какая заметка.
Дальше — сколько следов ручки на месте заметки там, где ничего не напечатано (между строк, справа от текста),
и в квадратике. Заметку, зачёркнутую целиком вместе с номером, так видно по самим штрихам.
Пометки на линейках в счёт не идут: линейки в область заметки не входят."""

import base64
import json
import logging
import zlib

from .pdf import MARKER, PORTRAIT_H, PORTRAIT_W, marker_corners

log = logging.getLogger(__name__)

SCALE = 3  # точек выпрямленного листа на pt (A4 → 1786 × 2526)
BAND_INK = 0.025  # доля «чернильных» точек в свободной области заметки, с которой она считается зачёркнутой
CHECK_INK = 0.3  # то же для середины квадратика (галочка или крестик); рамку квадратика не задевает


def pack(layout: list[dict]) -> str:
    """Раскладка → короткая строка для «Служебного» (там лимит 50 000 символов)."""
    return base64.b64encode(zlib.compress(json.dumps(layout, separators=(",", ":")).encode(), 9)).decode()


def unpack(text: str) -> list[dict]:
    return json.loads(zlib.decompress(base64.b64decode(text)))


def _page_points() -> list[list[list[float]]]:
    """Углы каждой метки (как их отдаёт OpenCV: левый верхний, правый верхний, правый нижний, левый нижний)
    в координатах выпрямленного листа (пиксели, ось y вниз)."""
    out = []
    for x, y in marker_corners():
        left, top, right, bottom = x * SCALE, (PORTRAIT_H - y - MARKER) * SCALE, (x + MARKER) * SCALE, (PORTRAIT_H - y) * SCALE
        out.append([[left, top], [right, top], [right, bottom], [left, bottom]])
    return out


def _box(b: list[float]) -> tuple[int, int, int, int]:
    """Прямоугольник в pt (x1, y1 снизу, x2, y2) → пиксели выпрямленного листа (x1, y1 сверху, x2, y2)."""
    x1, y1, x2, y2 = b
    return int(x1 * SCALE), int((PORTRAIT_H - y2) * SCALE), int(x2 * SCALE), int((PORTRAIT_H - y1) * SCALE)


def _sheets(image: bytes) -> dict[int, "object"]:
    """Листы на фото: {номер листа: выпрямленное чёрно-белое изображение листа}."""
    import cv2
    import numpy as np

    photo = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_GRAYSCALE)
    if photo is None:
        return {}
    detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_250), cv2.aruco.DetectorParameters())
    corners, ids, _ = detector.detectMarkers(photo)
    if ids is None:
        return {}
    found: dict[int, dict[int, list[list[float]]]] = {}
    for quad, marker in zip(corners, ids.flatten()):
        found.setdefault(int(marker) // 4, {})[int(marker) % 4] = quad[0].tolist()
    target = _page_points()
    size = (int(PORTRAIT_W * SCALE), int(PORTRAIT_H * SCALE))
    sheets = {}
    for page, seen in found.items():
        # у каждой метки 4 угла: перспективу листа восстанавливаем даже по двум меткам (если угол листа не попал в кадр)
        if len(seen) < 2:
            continue
        src = np.float32([pt for k in sorted(seen) for pt in seen[k]])
        dst = np.float32([pt for k in sorted(seen) for pt in target[k]])
        matrix, _ = cv2.findHomography(src, dst, cv2.RANSAC, 3.0 * SCALE)
        if matrix is not None:
            sheets[page] = cv2.warpPerspective(photo, matrix, size, borderValue=255)
    return sheets


def _ink(sheet) -> "object":
    """Где на листе тёмные штрихи: адаптивный порог не путает их с цветной заливкой карточек."""
    import cv2

    blur = cv2.GaussianBlur(sheet, (3, 3), 0)
    return cv2.adaptiveThreshold(blur, 1, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 41, 18)


def crossed_on(sheet, cards: list[dict]) -> set[int]:
    """Номера зачёркнутых заметок на одном выпрямленном листе."""
    import numpy as np

    ink = _ink(sheet)
    pad = int(2.5 * SCALE)  # фото выпрямляется с погрешностью в пару pt: напечатанное берём с запасом
    out = set()
    for card in cards:
        x1, y1, x2, y2 = _box(card["band"])
        free = np.ones((y2 - y1, x2 - x1), np.uint8)
        for b in card["ink"]:
            bx1, by1, bx2, by2 = _box(b)
            free[max(by1 - pad - y1, 0) : max(by2 + pad - y1, 0), max(bx1 - pad - x1, 0) : max(bx2 + pad - x1, 0)] = 0
        region = ink[y1:y2, x1:x2]
        band = float((region * free).sum()) / max(int(free.sum()), 1)
        cx1, cy1, cx2, cy2 = _box(card["check"])
        check = float(ink[cy1:cy2, cx1:cx2].mean()) if cy2 > cy1 and cx2 > cx1 else 0.0
        if band > BAND_INK or check > CHECK_INK:
            out.add(card["num"])
    return out


def find_crossed(images: list[bytes], layout: list[dict]) -> set[int]:
    """Номера зачёркнутых заметок на всех фото (листы без меток или не найденные — пропускаются)."""
    by_page: dict[int, list[dict]] = {}
    for card in layout:
        by_page.setdefault(card["page"], []).append(card)
    found = set()
    for image in images:
        try:
            for page, sheet in _sheets(image).items():
                found |= crossed_on(sheet, by_page.get(page, []))
        except Exception:
            log.exception("paper: could not read a photo")
    return found
