# -*- coding: utf-8 -*-
"""Авто-калибровка геометрии карточек наград (1.0.0).

Данные/пропорции получены из WFInfo (WFCD/WFinfo, Apache-2.0):
https://github.com/WFCD/WFinfo (commit 161f371, v9.8.2) — пропорция карточки
235/242 и подход «ряд вплотную» для разбиения слипшихся рядов.
Изменено: перенос в Python, собственные допуски и проверки.
Лицензия — ``licenses/Apache-2.0.txt``, атрибуция — ``THIRD_PARTY_NOTICES.md``.

Зачем
-----
Координаты карточек были захардкожены под 1920×1080 при масштабе Windows
100% (`lefts=[470, 720, 970, 1220]`). На 2K/4K, на 125–150% DPI и в оконном
режиме с другим HUD карточки съезжают: цены уезжают не к тем наградам, а скан
пишет «не удалось разобрать раскладку».

Что здесь
---------
* :func:`detect_geometry` — находит прямоугольники карточек на кадре
  (OpenCV: Canny/OTSU → findContours, фильтр по пропорциям ≈ квадрат, площади
  и горизонтальному ряду), проверяет «все на одной высоте ±6 px» и
  «шаг между левыми краями стабилен ±4 px» и пересчитывает геометрию
  (`lefts`, `card_width`, `card_pitch`, `card_top`, `name_offset_y`,
  `name_height`) во внутренние координаты 1920×1080 (после downscale скана).
* :func:`resolve_geometry` — приоритет источников: сохранённая калибровка >
  авто-определение по кадру (кэш на сессию) > текущий конфиг 1080p.
* :func:`apply_geometry` — записать геометрию в конфиг (включая
  `cards_center_x` и `scan_band`, чтобы OCR-полоса шла за карточками).
* :func:`normalize_calibration` — проверка/миграция `scan_calibration` из
  config.json (старые конфиги без поля работают как раньше).
* :func:`build_calibration_dialog` — мастер «Калибровка скана» для настроек
  (превью с зелёными рамками и номерами 1..N).

Модуль не тянет Qt на импорте: OpenCV/numpy — только там, где нужно.
"""

import time

BASE_W, BASE_H = 1920, 1080

#: Все боксы на одной высоте (±6 px) — требование подтверждения.
CARD_ROW_TOL = 6.0
#: Шаг между левыми краями стабилен (±4 px).
PITCH_TOL = 4.0
#: Доли карточки, перенесённые из 1080p-конфига (140/250 и 100/250).
NAME_OFFSET_RATIO = 140.0 / 250.0
NAME_HEIGHT_RATIO = 100.0 / 250.0

#: Внутренние ключи геометрии (в координатах 1920×1080).
GEOMETRY_KEYS = ("lefts", "card_width", "card_pitch", "card_top",
                 "name_offset_y", "name_height")
#: Что сохраняем в config.json → scan_calibration.
CONFIG_KEYS = ("lefts", "card_width", "card_pitch", "card_top",
               "name_offset_y", "name_height", "ui_scale", "updated_at")

#: Фильтры кандидатов — только относительные (не зависят от разрешения).
_MIN_W_RATIO = 0.085
_MAX_W_RATIO = 0.175
_MIN_H_RATIO = 0.155
_MAX_H_RATIO = 0.310
_MIN_ASPECT = 0.72
_MAX_ASPECT = 1.40
_MIN_AREA_RATIO = 0.012
_MAX_AREA_RATIO = 0.060

#: Пропорция карточки WFInfo: CARD_H/CARD_W = 235/242 (для разбиения слипшихся рядов)
CARD_H_W_RATIO = 235.0 / 242.0


def _np():
    import numpy as np
    return np


def to_gray(img):
    """BGR/BGRA/gray → серый кадр (без падения на необычных форматах)."""
    import cv2
    if img is None:
        return None
    if img.ndim == 2:
        return img
    if img.shape[2] == 4:
        return cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def frame_to_base(frame, base_w=BASE_W, base_h=BASE_H):
    """Кадр любого разрешения → внутренние 1920×1080 (как ``to_base``)."""
    import cv2
    if frame is None:
        return None
    height, width = frame.shape[:2]
    if width <= 0 or height <= 0:
        return None
    if width == int(base_w) and height == int(base_h):
        return frame
    interp = (cv2.INTER_AREA if (width > base_w or height > base_h)
              else cv2.INTER_CUBIC)
    return cv2.resize(frame, (int(base_w), int(base_h)), interpolation=interp)


def _round_list(values):
    return [int(round(float(v))) for v in values]


def _scale_pairs(cfg):
    """Масштаб экрана из конфига (для лога): ``sx × sy``."""
    cfg = cfg or {}
    try:
        sx = float(cfg.get("_screen_sx") or cfg.get("ui_scale") or 1.0)
    except Exception:
        sx = 1.0
    try:
        sy = float(cfg.get("_screen_sy") or cfg.get("ui_scale") or 1.0)
    except Exception:
        sy = 1.0
    return sx, sy


def default_geometry(cfg=None, base_w=BASE_W, base_h=BASE_H):
    """Геометрия по текущему конфигу (1080p по умолчанию)."""
    cfg = cfg or {}
    try:
        width = float(cfg.get("card_width") or 240)
        pitch = float(cfg.get("card_pitch") or 250)
        top = float(cfg.get("card_top") or 220)
        name_off = float(cfg.get("name_offset_y") or 140)
        name_h = float(cfg.get("name_height") or 100)
        center = float(cfg.get("cards_center_x") or 965)
    except Exception:
        width, pitch, top = 240.0, 250.0, 220.0
        name_off, name_h, center = 140.0, 100.0, 965.0
    lefts = _grid_lefts(4, center, width, pitch)
    return {
        "lefts": lefts,
        "card_width": int(round(width)),
        "card_pitch": int(round(pitch)),
        "card_top": int(round(top)),
        "name_offset_y": int(round(name_off)),
        "name_height": int(round(name_h)),
        "card_height": int(round(name_off + name_h + 10)),
        "cards_center_x": int(round(center)),
        "source": "конфиг",
        "base_size": [int(base_w), int(base_h)],
    }


def _grid_lefts(count, center, width, pitch):
    """Ряд центрирован: n карточек вокруг ``center`` (как ``card_lefts``)."""
    count = max(1, int(count))
    total = count * width + (count - 1) * (pitch - width)
    start = float(center) - total / 2.0
    return [int(round(start + index * pitch)) for index in range(count)]


def geometry_from_config(cfg, base_w=BASE_W, base_h=BASE_H):
    """Текущая геометрия конфига (источник «конфиг»)."""
    return default_geometry(cfg, base_w, base_h)


# ─────────────────────────────────────────────────────────────────────────
#  Поиск карточек на кадре
# ─────────────────────────────────────────────────────────────────────────
def _box_ok(box, width, height):
    """Прямоугольник похож на карточку награды (только относительные пороги)."""
    bw, bh = float(box["width"]), float(box["height"])
    if bw <= 0 or bh <= 0:
        return False
    if not (_MIN_W_RATIO * width <= bw <= _MAX_W_RATIO * width):
        return False
    if not (_MIN_H_RATIO * height <= bh <= _MAX_H_RATIO * height):
        return False
    aspect = bw / bh
    if not (_MIN_ASPECT <= aspect <= _MAX_ASPECT):
        return False
    area_ratio = (bw * bh) / float(width * height)
    return _MIN_AREA_RATIO <= area_ratio <= _MAX_AREA_RATIO


def _masks(gray):
    """Маски кандидатов: контуры ярких рамок + низкопороговое заполнение."""
    import cv2
    import numpy as np
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    edges = cv2.Canny(blurred, 40, 130)
    edges = cv2.dilate(edges, cv2.getStructuringElement(
        cv2.MORPH_RECT, (3, 3)), iterations=1)
    median = float(np.median(blurred))
    high = float(blurred.max())
    # карточка чуть светлее фона (или заметно светлее в игре) — берём
    # низкий порог от медианы, чтобы не поймать только яркий текст
    threshold = median + max(6.0, 0.04 * (high - median))
    _, fill = cv2.threshold(blurred, threshold, 255, cv2.THRESH_BINARY)
    fill = cv2.morphologyEx(fill, cv2.MORPH_CLOSE,
                            cv2.getStructuringElement(cv2.MORPH_RECT,
                                                      (5, 5)))
    fill = cv2.morphologyEx(fill, cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_RECT,
                                                      (3, 3)))
    # OTSU — страховка для контрастных кадров (яркая рамка на тёмном фоне)
    _, otsu = cv2.threshold(blurred, 0, 255,
                            cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    otsu = cv2.morphologyEx(otsu, cv2.MORPH_CLOSE,
                            cv2.getStructuringElement(cv2.MORPH_RECT,
                                                      (9, 9)))
    otsu = cv2.morphologyEx(otsu, cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_RECT,
                                                      (5, 5)))
    return (edges, fill, otsu)


def _row_ok(box, width, height):
    """Прямоугольник похож на «слипшийся» ряд из 2–4 карточек."""
    bw, bh = float(box["width"]), float(box["height"])
    if bw <= 0 or bh <= 0:
        return False
    if not (_MIN_H_RATIO * height <= bh <= _MAX_H_RATIO * height):
        return False
    aspect = bw / bh
    if aspect < 1.45:
        return False
    if not (_MIN_W_RATIO * width <= bw / max(1.0, round(aspect * CARD_H_W_RATIO))
            <= _MAX_W_RATIO * width):
        return False
    area_ratio = (bw * bh) / float(width * height)
    return 0.010 <= area_ratio <= 0.120


def _boxes_from_mask(mask, width, height):
    """Все прямоугольники контуров маски (фильтры — на вызывающей стороне)."""
    import cv2
    boxes = []
    try:
        contours, _hierarchy = cv2.findContours(
            mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    except Exception:
        return boxes
    for contour in contours:
        x, y, bw, bh = cv2.boundingRect(contour)
        if bw < 8 or bh < 8:
            continue
        # маски расширяют фигуру на 1–2 px (dilate/close) — компенсируем
        inset = 2
        boxes.append({"left": int(x + inset), "top": int(y + inset),
                      "width": int(max(1, bw - 2 * inset)),
                      "height": int(max(1, bh - 2 * inset))})
    return boxes


def _same_box(a, b):
    """Один и тот же прямоугольник (соседние маски дают почти одинаковые)."""
    if abs(a["left"] - b["left"]) <= 6 and abs(a["top"] - b["top"]) <= 6 \
            and abs(a["width"] - b["width"]) <= 8 \
            and abs(a["height"] - b["height"]) <= 8:
        return True
    ax2, ay2 = a["left"] + a["width"], a["top"] + a["height"]
    bx2, by2 = b["left"] + b["width"], b["top"] + b["height"]
    inter = (max(0, min(ax2, bx2) - max(a["left"], b["left"]))
             * max(0, min(ay2, by2) - max(a["top"], b["top"])))
    if inter <= 0:
        return False
    union = (a["width"] * a["height"] + b["width"] * b["height"] - inter)
    return union > 0 and inter / float(union) >= 0.6


def _dedupe_boxes(boxes):
    out = []
    for box in sorted(boxes, key=lambda b: (b["top"], b["left"])):
        if any(_same_box(box, kept) for kept in out):
            continue
        out.append(box)
    return out


def _row_clusters(boxes, row_tol=CARD_ROW_TOL):
    """Группы боксов «на одной высоте» (по верхнему краю)."""
    clusters = []
    for box in sorted(boxes, key=lambda b: (b["top"], b["left"])):
        placed = False
        for cluster in clusters:
            tops = [b["top"] for b in cluster]
            if abs(box["top"] - sum(tops) / float(len(tops))) <= row_tol:
                cluster.append(box)
                placed = True
                break
        if not placed:
            clusters.append([box])
    return clusters


def _split_merged_rows(boxes, width, height):
    """Разбить «слипшийся» ряд карточек на колонки (1.0.0).

    В игре карточки стоят вплотную (242 px, без зазора), поэтому маски часто
    дают один широкий прямоугольник. Число колонок оцениваем по пропорции:
    ``k = round((w/h) * CARD_H/CARD_W)`` — только относительные величины,
    без абсолютных пикселей. Возвращает список боксов-колонок.
    """
    extra = []
    for box in boxes or []:
        if box["height"] <= 0:
            continue
        aspect = box["width"] / float(box["height"])
        if aspect < 1.45:
            continue
        count = int(round(aspect * CARD_H_W_RATIO))
        if not 2 <= count <= 4:
            continue
        col_width = box["width"] / float(count)
        for index in range(count):
            left = int(round(box["left"] + index * col_width))
            piece = {"left": left, "top": int(box["top"]),
                     "width": int(round(col_width)),
                     "height": int(box["height"])}
            if _box_ok(piece, width, height):
                extra.append(piece)
    return extra


def _stable_pitch_run(boxes, pitch_tol=PITCH_TOL):
    """Самая длинная цепочка слева-направо с одинаковым шагом (±pitch_tol)."""
    ordered = sorted(boxes, key=lambda b: b["left"])
    if len(ordered) <= 1:
        return ordered
    best = []
    for start in range(len(ordered)):
        run = [ordered[start]]
        for index in range(start + 1, len(ordered)):
            step = ordered[index]["left"] - run[-1]["left"]
            if step <= 0:
                continue
            if len(run) == 1:
                run.append(ordered[index])
                continue
            pitch = run[-1]["left"] - run[-2]["left"]
            if abs(step - pitch) <= pitch_tol:
                run.append(ordered[index])
        if len(run) > len(best):
            best = run
    return best


def _pick_row(boxes, expected=0, row_tol=CARD_ROW_TOL,
              pitch_tol=PITCH_TOL):
    """Выбрать ряд карточек: одна высота, стабильный шаг, 1–4 штуки."""
    if not boxes:
        return []
    candidates = []
    for cluster in _row_clusters(boxes, row_tol):
        run = _stable_pitch_run(cluster, pitch_tol)
        if not run:
            continue
        run = sorted(run, key=lambda b: b["left"])
        tops = [b["top"] for b in run]
        spread = (max(tops) - min(tops)) if tops else 0
        if len(run) > 4:
            run = run[:4]
        if not 1 <= len(run) <= 4:
            continue
        candidates.append((len(run), -spread, run))
    if not candidates:
        return []
    # приоритет: точное совпадение с ожидаемым, затем больше карточек,
    # затем ровнее ряд
    def rank(item):
        count, spread, _run = item
        return (1 if expected and count == expected else 0,
                count if not expected or count == expected else -abs(
                    count - expected),
                spread)
    candidates.sort(key=rank, reverse=True)
    count, spread, run = candidates[0]
    if expected and count != expected:
        return []
    tops = [b["top"] for b in run]
    if spread > row_tol:
        return []
    return run


def geometry_from_boxes(boxes, base_w=BASE_W, base_h=BASE_H):
    """Боксы карточек → геометрия во внутренних координатах 1920×1080."""
    if not boxes:
        return None
    ordered = sorted(boxes, key=lambda b: b["left"])
    lefts = [int(round(b["left"])) for b in ordered]
    widths = sorted(float(b["width"]) for b in ordered)
    heights = sorted(float(b["height"]) for b in ordered)
    tops = sorted(float(b["top"]) for b in ordered)
    median = lambda values: values[len(values) // 2]
    card_width = int(round(median(widths)))
    card_height = int(round(median(heights)))
    card_top = int(round(median(tops)))
    if len(lefts) >= 2:
        steps = sorted(ordered[i + 1]["left"] - ordered[i]["left"]
                       for i in range(len(ordered) - 1))
        card_pitch = int(round(median(steps)))
    else:
        card_pitch = int(round(card_width * 250.0 / 240.0))
    name_offset_y = int(round(card_height * NAME_OFFSET_RATIO))
    name_height = int(round(card_height * NAME_HEIGHT_RATIO))
    center = int(round((lefts[0] + lefts[-1] + card_width) / 2.0))
    geometry = {
        "lefts": lefts,
        "card_width": card_width,
        "card_pitch": card_pitch,
        "card_top": card_top,
        "name_offset_y": name_offset_y,
        "name_height": name_height,
        "card_height": card_height,
        "cards_center_x": center,
        "source": "авто",
        "base_size": [int(base_w), int(base_h)],
    }
    return geometry


def _filter_by_hint(boxes, hint, height):
    """Оставить кандидатов рядом с ожидаемой геометрией (WFInfo/конфиг).

    Без подсказки детектор может поймать посторонний «ряд» в HUD; hint
    (``card_top``/``card_height``/``card_width``) отсекает такие находки.
    """
    if not isinstance(hint, dict) or not boxes:
        return boxes
    try:
        top = float(hint.get("card_top") or 0)
        card_h = float(hint.get("card_height") or 0)
        card_w = float(hint.get("card_width") or 0)
    except Exception:
        return boxes
    if card_h <= 0 or card_w <= 0:
        return boxes
    top_tol = max(24.0, 0.07 * float(height))
    kept = []
    for box in boxes:
        if abs(box["top"] - top) > top_tol:
            continue
        if not (0.70 * card_h <= box["height"] <= 1.40 * card_h):
            continue
        width = box["width"]
        if 0.65 * card_w <= width <= 1.70 * card_w:
            kept.append(box)
            continue
        # «слипшийся» ряд: ширина кратна ширине карточки
        columns = max(1, int(round(width / card_w)))
        if 2 <= columns <= 4 and width / columns <= 1.25 * card_w:
            kept.append(box)
    return kept or boxes


def detect_geometry(frame, expected=0, base_w=BASE_W, base_h=BASE_H,
                    band=None, row_tol=CARD_ROW_TOL, pitch_tol=PITCH_TOL,
                    hint=None):
    """Найти геометрию карточек на кадре.

    ``frame`` — кадр любого разрешения (обычно полный снимок монитора).
    ``expected`` — ожидаемое число карточек (1–4) из ``_cards_from_words``;
    0 — определять по кадру. Если найдено не ``expected`` — возвращаем None
    (работаем по конфигу).
    """
    np = _np()
    if frame is None:
        return None
    try:
        base = frame_to_base(frame, base_w, base_h)
    except Exception:
        return None
    if base is None or base.size == 0:
        return None
    gray = to_gray(base)
    if gray is None or gray.size == 0:
        return None
    height, width = gray.shape[:2]
    raw = []
    for mask in _masks(gray):
        raw.extend(_boxes_from_mask(mask, width, height))
    boxes = [box for box in raw if _box_ok(box, width, height)]
    rows = [box for box in raw if _row_ok(box, width, height)]
    if hint:
        boxes = _filter_by_hint(boxes, hint, height)
        rows = _filter_by_hint(rows, hint, height)
    # карточки в игре стоят вплотную — «слипшийся» ряд разбиваем на колонки
    pieces = _split_merged_rows(rows, width, height)
    if pieces:
        boxes = _dedupe_boxes(boxes + pieces)
    else:
        boxes = _dedupe_boxes(boxes)
    if band:
        # отсекаем кандидатов вне вертикальной окрестности полосы наград
        try:
            top_limit = float(band.get("top") or 0)
            height_limit = float(band.get("height") or 0)
            nearby = [b for b in boxes
                      if top_limit - height_limit <= b["top"]
                      <= top_limit + 3 * height_limit]
            if nearby:
                boxes = nearby
        except Exception:
            pass
    row = _pick_row(boxes, expected=expected, row_tol=row_tol,
                    pitch_tol=pitch_tol)
    if not row:
        return None
    # ряд наград всегда в верхней половине экрана: посторонние «ряды» HUD
    # (кнопки/итоги внизу) отсекаем по вертикальному центру
    med_top = sorted(float(box["top"]) for box in row)[len(row) // 2]
    med_h = sorted(float(box["height"]) for box in row)[len(row) // 2]
    row_center = med_top + med_h / 2.0
    if not (0.08 * height <= row_center <= 0.45 * height):
        return None
    geometry = geometry_from_boxes(row, base_w, base_h)
    ok, reason = validate_geometry(geometry, base_w, base_h,
                                   expected=len(row))
    if not ok:
        return None
    geometry["reason"] = ""
    # 1.0.0 (t31): сколько рамок нашёл детектор — источник истины для
    # раскладки 1/2/3/4 (чтобы 2 карточки не превращались в 4 с «?»)
    geometry["frames_found"] = int(len(row))
    return geometry


def validate_geometry(geometry, base_w=BASE_W, base_h=BASE_H, expected=0):
    """Проверка геометрии на правдоподобие → ``(ok, причина)``."""
    if not isinstance(geometry, dict):
        return False, "нет геометрии"
    lefts = geometry.get("lefts")
    if not isinstance(lefts, (list, tuple)) or not lefts:
        return False, "нет lefts"
    lefts = [float(x) for x in lefts]
    if len(lefts) > 4:
        return False, "больше 4 карточек"
    if expected and len(lefts) != int(expected):
        return False, "число карточек не совпало"
    for index in range(1, len(lefts)):
        if lefts[index] <= lefts[index - 1]:
            return False, "lefts идут не по порядку"
    width = float(geometry.get("card_width") or 0)
    pitch = float(geometry.get("card_pitch") or 0)
    top = float(geometry.get("card_top") or 0)
    name_off = float(geometry.get("name_offset_y") or 0)
    name_h = float(geometry.get("name_height") or 0)
    if not (_MIN_W_RATIO * base_w <= width <= _MAX_W_RATIO * base_w):
        return False, "ширина карточки вне диапазона"
    if len(lefts) >= 2:
        steps = [lefts[i + 1] - lefts[i] for i in range(len(lefts) - 1)]
        if max(steps) - min(steps) > PITCH_TOL:
            return False, "шаг между карточками неровный"
        if abs(pitch - sum(steps) / float(len(steps))) > PITCH_TOL:
            return False, "card_pitch не совпадает с шагом"
    if not (0.95 * width <= pitch <= 1.45 * width):
        return False, "шаг несоразмерен ширине"
    if not (0.05 * base_h <= top <= 0.65 * base_h):
        return False, "верх карточек вне экрана"
    if not (0.25 * width <= name_off <= 2.0 * width):
        return False, "name_offset_y несоразмерен"
    if not (20 <= name_h <= 0.9 * width):
        return False, "name_height несоразмерен"
    if lefts[-1] + width > base_w + 0.02 * base_w:
        return False, "карточки выходят за кадр"
    if lefts[0] < -0.02 * base_w:
        return False, "карточки выходят за левый край"
    return True, ""


# ─────────────────────────────────────────────────────────────────────────
#  Миграция конфига, приоритет источников, применение
# ─────────────────────────────────────────────────────────────────────────
def normalize_calibration(raw, base_w=BASE_W, base_h=BASE_H):
    """``scan_calibration`` из config.json → геометрия (или None).

    Старые конфиги поля не знают — возвращаем None, приложение работает
    по прежним координатам 1080p (миграция без сброса настроек).
    """
    if not isinstance(raw, dict):
        return None
    try:
        lefts = [int(round(float(x))) for x in (raw.get("lefts") or [])]
    except Exception:
        return None
    if not lefts:
        return None
    geometry = {
        "lefts": lefts,
        "card_width": raw.get("card_width"),
        "card_pitch": raw.get("card_pitch"),
        "card_top": raw.get("card_top"),
        "name_offset_y": raw.get("name_offset_y"),
        "name_height": raw.get("name_height"),
        "source": "конфиг",
        "base_size": [int(base_w), int(base_h)],
    }
    for key in ("card_width", "card_pitch", "card_top", "name_offset_y",
                "name_height"):
        try:
            geometry[key] = int(round(float(geometry[key])))
        except Exception:
            return None
    geometry["card_height"] = int(round(
        geometry["name_offset_y"] + geometry["name_height"]))
    geometry["cards_center_x"] = int(round(
        (lefts[0] + lefts[-1] + geometry["card_width"]) / 2.0))
    ok, _reason = validate_geometry(geometry, base_w, base_h)
    if not ok:
        return None
    try:
        geometry["ui_scale"] = round(float(raw.get("ui_scale") or 1.0), 3)
    except Exception:
        geometry["ui_scale"] = 1.0
    try:
        geometry["updated_at"] = float(raw.get("updated_at") or 0)
    except Exception:
        geometry["updated_at"] = 0.0
    return geometry


def _cache_key(frame, expected, base_w, base_h):
    try:
        height, width = frame.shape[:2]
    except Exception:
        height, width = 0, 0
    return (int(expected or 0), int(width), int(height),
            int(base_w), int(base_h))


APP_UI_MARKERS = ("калибровка", "настройки", "прозрачность",
                   "масштаб шрифта")


def frame_has_app_ui(text):
    """True, если OCR кадра содержит подписи интерфейса приложения."""
    low = str(text or "").lower().replace("ё", "е")
    return any(marker in low for marker in APP_UI_MARKERS)


def capture_without_windows(capture_fn, delay=0.15, app=None):
    """Снимок с временно скрытыми окнами приложения.

    Возвращает ``(frame, hidden_count)``; окна восстанавливаются в finally.
    """
    import time
    from PyQt5.QtWidgets import QApplication
    app = app or QApplication.instance()
    hidden = []
    if app is not None:
        for widget in app.topLevelWidgets():
            try:
                if widget.isVisible():
                    widget.hide()
                    hidden.append(widget)
            except Exception:
                pass
        app.processEvents()
    try:
        time.sleep(max(0.0, float(delay)))
        frame = capture_fn() if callable(capture_fn) else None
    finally:
        for widget in hidden:
            try:
                widget.show()
            except Exception:
                pass
        if app is not None:
            app.processEvents()
    return frame, len(hidden)


def clear_cache(cache):
    """Сбросить кэш авто-определения (кнопка «Определить автоматически»)."""
    if isinstance(cache, dict):
        cache.clear()
    return cache


def resolve_geometry(cfg, frame=None, expected=0, cache=None,
                     base_w=BASE_W, base_h=BASE_H, allow_auto=True,
                     hint=None):
    """Приоритет: сохранённая калибровка > авто по кадру > конфиг 1080p.

    Возвращает ``(geometry, источник)``; источник — «конфиг» или «авто».
    Авто-определение кэшируется на сессию (``cache`` — обычный dict).
    ``hint`` — ожидаемая геометрия (WFInfo/конфиг), чтобы детектор не поймал
    посторонний «ряд» в HUD.
    """
    cfg = cfg or {}
    saved = normalize_calibration(cfg.get("scan_calibration"), base_w, base_h)
    if saved is not None:
        return saved, "конфиг"
    if allow_auto and frame is not None:
        key = _cache_key(frame, expected, base_w, base_h)
        if isinstance(cache, dict) and key in cache:
            cached = cache.get(key)
            if cached:
                return dict(cached), "авто"
            return geometry_from_config(cfg, base_w, base_h), "конфиг"
        detected = None
        try:
            detected = detect_geometry(frame, expected=expected, base_w=base_w,
                                       base_h=base_h,
                                       band=cfg.get("scan_band"),
                                       hint=hint)
        except Exception:
            detected = None
        if isinstance(cache, dict):
            cache[key] = dict(detected) if detected else None
        if detected:
            return detected, "авто"
    return geometry_from_config(cfg, base_w, base_h), "конфиг"


def scan_band_for(geometry, base_w=BASE_W, base_h=BASE_H):
    """OCR-полоса под найденную геометрию (с запасом по краям)."""
    geometry = geometry or {}
    lefts = [float(x) for x in (geometry.get("lefts") or [])]
    if not lefts:
        return None
    width = float(geometry.get("card_width") or 240)
    top = float(geometry.get("card_top") or 220)
    name_off = float(geometry.get("name_offset_y") or 140)
    name_h = float(geometry.get("name_height") or 100)
    left = max(0, int(round(min(lefts) - 60)))
    right = min(int(base_w), int(round(max(lefts) + width + 60)))
    band_top = max(0, int(round(top + name_off - 40)))
    band_bottom = min(int(base_h), int(round(top + name_off + name_h + 40)))
    if right - left < 40 or band_bottom - band_top < 20:
        return None
    return {"left": left, "top": band_top,
            "width": max(40, right - left),
            "height": max(20, band_bottom - band_top)}


def apply_geometry(cfg, geometry, base_w=BASE_W, base_h=BASE_H):
    """Записать геометрию в конфиг (in-memory или перед сохранением).

    Обновляет ``card_width``, ``card_pitch``, ``cards_center_x``,
    ``card_top``, ``name_offset_y``, ``name_height`` и ``scan_band`` —
    чтобы и скан, и плашки, и OCR-полоса шли за найденными карточками.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    geometry = geometry or {}
    lefts = [float(x) for x in (geometry.get("lefts") or [])]
    if not lefts:
        return {}
    ok, _reason = validate_geometry(geometry, base_w, base_h)
    if not ok:
        return {}
    width = int(round(float(geometry.get("card_width"))))
    pitch = int(round(float(geometry.get("card_pitch"))))
    top = int(round(float(geometry.get("card_top"))))
    name_off = int(round(float(geometry.get("name_offset_y"))))
    name_h = int(round(float(geometry.get("name_height"))))
    center = int(round((lefts[0] + lefts[-1] + width) / 2.0))
    cfg["card_width"] = width
    cfg["card_pitch"] = pitch
    cfg["cards_center_x"] = center
    cfg["card_top"] = top
    cfg["name_offset_y"] = name_off
    cfg["name_height"] = name_h
    band = scan_band_for(geometry, base_w, base_h)
    if band:
        cfg["scan_band"] = band
    return {"card_width": width, "card_pitch": pitch,
            "cards_center_x": center, "card_top": top,
            "name_offset_y": name_off, "name_height": name_h,
            "scan_band": band, "lefts": [int(round(x)) for x in lefts]}


def calibration_payload(geometry, ui_scale=1.0, updated_at=None):
    """Геометрия → поле ``scan_calibration`` для config.json."""
    geometry = geometry or {}
    ok, _reason = validate_geometry(geometry)
    if not ok:
        return {}
    payload = {key: geometry.get(key) for key in GEOMETRY_KEYS}
    payload["lefts"] = [int(round(float(x))) for x in geometry.get("lefts")]
    for key in ("card_width", "card_pitch", "card_top", "name_offset_y",
                "name_height"):
        payload[key] = int(round(float(geometry.get(key) or 0)))
    try:
        payload["ui_scale"] = round(float(ui_scale or 1.0), 3)
    except Exception:
        payload["ui_scale"] = 1.0
    payload["updated_at"] = (time.time() if updated_at is None
                             else float(updated_at))
    return {key: payload[key] for key in CONFIG_KEYS}


def log_line(geometry, source="конфиг", ui_scale=None, cfg=None):
    """Строка в app.log: «Калибровка: lefts=[…] pitch=… scale=…×… (источник: …)»."""
    geometry = geometry or {}
    lefts = geometry.get("lefts") or []
    pitch = geometry.get("card_pitch")
    if ui_scale is None:
        ui_scale = geometry.get("ui_scale")
    if not ui_scale:
        sx, sy = _scale_pairs(cfg)
    else:
        try:
            sx = float(ui_scale)
        except Exception:
            sx = 1.0
        sy = sx
    text = (f"Калибровка: lefts={[int(round(float(x))) for x in lefts]} "
            f"pitch={int(round(float(pitch))) if pitch else '—'} "
            f"scale={sx:.2f}×{sy:.2f} (источник: {source})")
    return text


def _crop_box(geometry, base_w=BASE_W, base_h=BASE_H, pad=40):
    """Границы превью вокруг карточек ``(x0, y0, x1, y1)``."""
    geometry = geometry or {}
    lefts = [float(x) for x in (geometry.get("lefts") or [])]
    if not lefts:
        return 0, 0, int(base_w), int(base_h)
    width = float(geometry.get("card_width") or 240)
    top = float(geometry.get("card_top") or 220)
    name_off = float(geometry.get("name_offset_y") or 140)
    name_h = float(geometry.get("name_height") or 100)
    x0 = max(0, int(min(lefts) - pad))
    x1 = min(int(base_w), int(max(lefts) + width + pad))
    y0 = max(0, int(top - pad))
    y1 = min(int(base_h), int(top + name_off + name_h + pad))
    if x1 - x0 < 40 or y1 - y0 < 20:
        return 0, 0, int(base_w), int(base_h)
    return x0, y0, x1, y1


def preview_crop(frame, geometry, base_w=BASE_W, base_h=BASE_H, pad=40):
    """Обрезать кадр вокруг карточек (для превью мастера)."""
    base = frame_to_base(frame, base_w, base_h)
    if base is None:
        return None
    x0, y0, x1, y1 = _crop_box(geometry, base_w, base_h, pad)
    crop = base[y0:y1, x0:x1]
    return crop if crop.size else base


def draw_preview(frame, geometry, base_w=BASE_W, base_h=BASE_H, crop=True):
    """Превью с зелёными рамками карточек и номерами 1..N (BGR)."""
    import cv2
    base = frame_to_base(frame, base_w, base_h)
    if base is None:
        return None
    work = base.copy()
    if work.ndim == 2:
        work = cv2.cvtColor(work, cv2.COLOR_GRAY2BGR)
    geometry = geometry or {}
    lefts = [float(x) for x in (geometry.get("lefts") or [])]
    if lefts:
        width = float(geometry.get("card_width") or 240)
        top = float(geometry.get("card_top") or 220)
        name_off = float(geometry.get("name_offset_y") or 140)
        name_h = float(geometry.get("name_height") or 100)
        box_height = max(40.0, name_off + name_h)
        for index, left in enumerate(lefts, 1):
            x0 = int(round(left))
            y0 = int(round(top))
            x1 = int(round(left + width))
            y1 = int(round(top + box_height))
            cv2.rectangle(work, (x0, y0), (x1, y1), (80, 220, 90), 2)
            cv2.putText(work, str(index), (x0 + 6, max(14, y0 - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (80, 220, 90), 2,
                        cv2.LINE_AA)
    if crop and lefts:
        x0, y0, x1, y1 = _crop_box(geometry, base_w, base_h)
        sub = work[y0:y1, x0:x1]
        if sub.size:
            return sub
    return work


# ─────────────────────────────────────────────────────────────────────────
#  Мастер калибровки (Qt импортируется лениво)
# ─────────────────────────────────────────────────────────────────────────
def _bgr_to_qpixmap(image):
    """BGR (OpenCV) → QPixmap для превью мастера."""
    import numpy as np
    from PyQt5.QtGui import QImage, QPixmap
    if image is None:
        return QPixmap()
    if image.ndim == 2:
        rgb = np.dstack([image, image, image])
    else:
        rgb = image[:, :, ::-1]
    rgb = np.ascontiguousarray(rgb)
    height, width = rgb.shape[:2]
    qimage = QImage(rgb.data, width, height, 3 * width,
                    QImage.Format_RGB888).copy()
    return QPixmap.fromImage(qimage)


def build_calibration_dialog():
    """Фабрика мастера «Калибровка скана» (Qt импортируется лениво).

    Параметры конструктора:
      * ``cfg`` — конфиг приложения (для «Сбросить к 1920×1080»);
      * ``capture_fn`` — ``() -> BGR-кадр`` (снимок монитора; None — нет кадра);
      * ``save_fn`` — ``(payload) -> dict`` — записать ``scan_calibration``;
      * ``check_fn`` — ``(geometry) -> dict`` — пробный скан («Проверить»);
      * ``ui_scale`` — масштаб экрана для записи в калибровку;
      * ``parent`` — родительское окно.
    """
    import numpy as np  # noqa: F401
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QFont
    from PyQt5.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton,
                                 QVBoxLayout)

    try:
        import ui_theme as theme
    except Exception:
        theme = None

    def color(name, default="#e6edf6"):
        palette = getattr(theme, "PALETTE", {}) if theme is not None else {}
        return palette.get(name, default)

    def font(size, bold=False):
        result = QFont(getattr(theme, "FONT", "Segoe UI"))
        result.setPixelSize(int(size))
        result.setBold(bool(bold))
        return result

    def button_qss(**kwargs):
        if theme is not None:
            try:
                return theme.button_qss(**kwargs)
            except Exception:
                pass
        return ""

    class CalibrationDialog(QDialog):
        """Скриншот полосы наград, авто-определение, сброс, сохранение."""

        def __init__(self, cfg=None, capture_fn=None, save_fn=None,
                     check_fn=None, ui_scale=1.0, parent=None,
                     reset_fn=None, ocr_text_fn=None):
            super().__init__(parent)
            self.cfg = dict(cfg or {})
            self.capture_fn = capture_fn
            self.save_fn = save_fn
            self.check_fn = check_fn
            self.reset_fn = reset_fn
            self.ocr_text_fn = ocr_text_fn
            self.last_capture_hidden = 0
            self.ui_scale = float(ui_scale or 1.0)
            self.frame = None
            self.geometry = None
            self.last_result = {}
            self.setWindowTitle("Калибровка скана")
            self.setModal(True)
            self.setMinimumSize(900, 640)
            if theme is not None:
                try:
                    self.setStyleSheet(theme.dialog_qss())
                except Exception:
                    pass
            layout = QVBoxLayout(self)
            layout.setContentsMargins(14, 12, 14, 12)
            layout.setSpacing(8)
            title = QLabel("<b>Калибровка скана наград</b>")
            title.setFont(font(14, True))
            title.setStyleSheet(f"color:{color('text')};")
            layout.addWidget(title)
            hint = QLabel(
                "Снимите экран наград в игре и нажмите «Определить "
                "автоматически»: приложение найдёт карточки и подгонит "
                "координаты под ваше разрешение и масштаб Windows.")
            hint.setWordWrap(True)
            hint.setFont(font(10))
            hint.setStyleSheet(f"color:{color('muted')};")
            layout.addWidget(hint)
            self.lbl_preview = QLabel("превью появится после снимка")
            self.lbl_preview.setAlignment(Qt.AlignCenter)
            self.lbl_preview.setMinimumHeight(340)
            self.lbl_preview.setStyleSheet(
                f"background:{color('panel')}; border:1px solid "
                f"{color('border')}; color:{color('muted')};")
            layout.addWidget(self.lbl_preview, 1)
            self.lbl_state = QLabel("калибровка: —")
            self.lbl_state.setFont(font(11, True))
            self.lbl_state.setWordWrap(True)
            self.lbl_state.setStyleSheet(f"color:{color('text')};")
            layout.addWidget(self.lbl_state)
            self.lbl_status = QLabel(
                "Кнопка «Определить автоматически» делает снимок и ищет "
                "карточки (1–4).")
            self.lbl_status.setFont(font(10))
            self.lbl_status.setWordWrap(True)
            self.lbl_status.setStyleSheet(f"color:{color('muted')};")
            layout.addWidget(self.lbl_status)
            row = QHBoxLayout()
            row.setSpacing(8)
            self.btn_detect = QPushButton("Определить автоматически")
            self.btn_reset = QPushButton("Сбросить калибровку")
            self.btn_check = QPushButton("Проверить")
            self.btn_save = QPushButton("Сохранить")
            self.btn_close = QPushButton("Закрыть")
            for button, slot, primary in (
                    (self.btn_detect, self.detect_auto, True),
                    (self.btn_reset, self.reset_defaults, False),
                    (self.btn_check, self.check, False),
                    (self.btn_save, self.save, True),
                    (self.btn_close, self.reject, False)):
                button.setStyleSheet(button_qss(primary=primary))
                button.setCursor(Qt.PointingHandCursor)
                button.clicked.connect(slot)
                row.addWidget(button)
            row.addStretch(1)
            layout.addLayout(row)
            self.refresh_state()

        # ── состояние ────────────────────────────────────────────────
        def refresh_state(self):
            geometry = self.geometry or {}
            lefts = geometry.get("lefts") or []
            if lefts:
                text = (f"калибровка: lefts={[int(x) for x in lefts]} "
                        f"pitch={geometry.get('card_pitch')} "
                        f"top={geometry.get('card_top')} "
                        f"(источник: {geometry.get('source', '—')})")
            else:
                saved = normalize_calibration(self.cfg.get("scan_calibration"))
                if saved:
                    text = (f"калибровка: сохранена lefts={saved['lefts']} "
                            f"pitch={saved['card_pitch']}")
                else:
                    geom = geometry_from_config(self.cfg)
                    text = (f"калибровка: нет (стандарт 1920×1080, "
                            f"lefts={geom['lefts']})")
            self.lbl_state.setText(text)
            return text

        def _set_status(self, text, warn=False):
            self.lbl_status.setText(str(text))
            self.lbl_status.setStyleSheet(
                f"color:{color('warning' if warn else 'success')};")

        def _capture(self):
            if not callable(self.capture_fn):
                self._set_status("Снимок экрана недоступен в этой сборке.",
                                 warn=True)
                return None
            try:
                frame = self.capture_fn()
            except Exception as exc:
                self._set_status(f"Не удалось снять экран: "
                                 f"{type(exc).__name__}.", warn=True)
                return None
            if frame is None or getattr(frame, "size", 0) == 0:
                self._set_status("Пустой кадр — откройте экран наград.",
                                 warn=True)
                return None
            self.frame = frame
            return frame

        def _show_preview(self, geometry=None):
            if self.frame is None:
                return False
            try:
                image = draw_preview(self.frame, geometry or self.geometry)
            except Exception:
                image = None
            if image is None:
                return False
            try:
                pixmap = _bgr_to_qpixmap(image)
                self.lbl_preview.setPixmap(pixmap.scaled(
                    self.lbl_preview.size(), Qt.KeepAspectRatio,
                    Qt.SmoothTransformation))
                self.lbl_preview.setText("")
                return True
            except Exception:
                return False

        # ── кнопки ───────────────────────────────────────────────────
        def detect_auto(self):
            """«Определить автоматически»: снимок + поиск карточек."""
            frame, hidden = capture_without_windows(self._capture)
            self.last_capture_hidden = hidden
            if frame is None:
                return None
            if callable(self.ocr_text_fn):
                try:
                    if frame_has_app_ui(self.ocr_text_fn(frame)):
                        self.geometry = None
                        self._show_preview(None)
                        self.refresh_state()
                        self._set_status(
                            "Калибровка отменена: в кадре интерфейс "
                            "приложения", warn=True)
                        return None
                except Exception:
                    pass
            geometry = detect_geometry(frame)
            if geometry is None:
                self.geometry = None
                self._show_preview(None)
                self.refresh_state()
                self._set_status(
                    "Не удалось определить карточки — работаю по стандартным "
                    "координатам.", warn=True)
                return None
            self.geometry = geometry
            self._show_preview(geometry)
            self.refresh_state()
            self._set_status(
                f"Найдено карточек: {len(geometry['lefts'])}. "
                "Проверьте рамки на превью и нажмите «Сохранить».")
            return geometry

        def reset_defaults(self):
            """«Сбросить к 1920×1080»: удалить калибровку и взять канонику."""
            self.cfg.pop("scan_calibration", None)
            if callable(self.reset_fn):
                try:
                    self.reset_fn()
                except Exception:
                    pass
            self.geometry = geometry_from_config(self.cfg)
            self.geometry["source"] = "конфиг"
            self._show_preview(self.geometry)
            self.refresh_state()
            self._set_status("Калибровка сброшена — используется "
                             "каноническая сетка.")
            return self.geometry

        def check(self):
            """«Проверить»: пробный скан с названиями (без сохранения)."""
            geometry = self.geometry or geometry_from_config(self.cfg)
            if not callable(self.check_fn):
                self._set_status("Пробный скан недоступен в этой сборке.",
                                 warn=True)
                return None
            try:
                result = self.check_fn(dict(geometry)) or {}
            except Exception as exc:
                result = {"ok": False,
                          "error": f"{type(exc).__name__}: {exc}"}
            self.last_result = dict(result)
            if result.get("ok"):
                names = [str(n) for n in (result.get("names") or [])]
                self._set_status(
                    "Пробный скан: " + (", ".join(names) if names
                                        else "названия не распознаны") + ".")
            else:
                self._set_status(
                    f"Пробный скан не удался: "
                    f"{result.get('error') or 'нет кадра'}.", warn=True)
            return result

        def save(self):
            """«Сохранить»: записать scan_calibration в config.json."""
            geometry = self.geometry or geometry_from_config(self.cfg)
            payload = calibration_payload(geometry, ui_scale=self.ui_scale)
            if not payload:
                self._set_status("Нечего сохранять: геометрия не определена.",
                                 warn=True)
                return {"ok": False, "error": "нет геометрии"}
            if not callable(self.save_fn):
                self._set_status("Сохранение недоступно в этой сборке.",
                                 warn=True)
                return {"ok": False, "error": "недоступно"}
            try:
                result = self.save_fn(payload) or {}
            except Exception as exc:
                result = {"ok": False,
                          "error": f"{type(exc).__name__}: {exc}"}
            self.last_result = dict(result)
            if result.get("ok"):
                self.cfg["scan_calibration"] = dict(payload)
                self.geometry = dict(geometry)
                self.refresh_state()
                self._set_status(
                    f"Сохранено: lefts={payload['lefts']} "
                    f"pitch={payload['card_pitch']} "
                    f"(scale={payload['ui_scale']}).")
            else:
                self._set_status(
                    f"Не сохранено: {result.get('error') or 'ошибка'}.",
                    warn=True)
            return result

    return CalibrationDialog


__all__ = ["detect_geometry", "resolve_geometry", "apply_geometry",
           "normalize_calibration", "calibration_payload", "log_line",
           "geometry_from_config", "default_geometry", "validate_geometry",
           "draw_preview", "preview_crop", "frame_to_base", "scan_band_for",
           "clear_cache", "build_calibration_dialog", "GEOMETRY_KEYS",
           "CONFIG_KEYS", "BASE_W", "BASE_H"]




