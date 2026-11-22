# -*- coding: utf-8 -*-
"""Геометрия экрана наград Warframe — единая точка правды (1.0.0).

Данные/формулы получены из WFInfo (WFCD/WFinfo, Apache-2.0):
https://github.com/WFCD/WFinfo (commit 161f371, v9.8.2)
Изменено: перенос в Python, добавлены собственные имена полей, допуски,
проверки диапазонов и русскоязычные сообщения. Полный текст лицензии —
``licenses/Apache-2.0.txt``, атрибуция — ``THIRD_PARTY_NOTICES.md``.

Все координаты — во «внутреннем» пространстве 1920×1080 при UI scale 100 %:
игра центрирует ряд карточек по X, а ``card_top`` считается от центра экрана,
поэтому формулы одинаково работают для любого разрешения (см.
:func:`to_base_uniform` — масштаб ``min(W/1920, H/1080)`` без растяжения).

Ключевые константы WFInfo (Ocr.cs, 1920×1080 / UI 100 %):

* ``ROW_W = 968`` — ширина ряда из 4 карточек (``4 × 242``, карточки вплотную);
* ``CARD_W = 242`` — ширина/шаг карточки;
* ``CARD_H = 235`` — высота карточки;
* ``Y_DISPLAY = 316`` — верх карточки = ``H/2 − 316``;
* ``NAME_H = 48`` — высота полосы названия (до двух строк).
"""

import os
import re

BASE_W, BASE_H = 1920, 1080

ROW_W = 968
CARD_W = 242
CARD_H = 235
Y_DISPLAY = 316
NAME_H = 48
#: запас OCR-полосы вокруг полосы названий (пиксели UI-пространства)
NAME_PAD = 15

EE_CFG_SUBPATH = os.path.join("Warframe", "EE.cfg")


# ─────────────────────────────────────────────────────────────────────────
#  Масштаб экрана: min(W/1920, H/1080) без растяжения
# ─────────────────────────────────────────────────────────────────────────
def screen_scaling(width, height):
    """``ScreenScaling`` WFInfo: по меньшему отношению (не растягивая UI)."""
    try:
        width = float(width)
        height = float(height)
    except Exception:
        return 1.0
    if width <= 0 or height <= 0:
        return 1.0
    return min(width / BASE_W, height / BASE_H)


def base_origin(width, height, base_w=BASE_W, base_h=BASE_H):
    """Смещение окна 1920×1080 внутри равномерно отмасштабированного кадра.

    Возвращает ``(sc, x0, y0)``: ``x0/y0`` — в пикселях внутреннего
    пространства (базы), экранные пиксели = ``x0 * sc``.
    """
    sc = screen_scaling(width, height)
    if sc <= 0:
        return 1.0, 0, 0
    scaled_w = int(round(float(width) / sc))
    scaled_h = int(round(float(height) / sc))
    return sc, max(0, (scaled_w - int(base_w)) // 2), \
        max(0, (scaled_h - int(base_h)) // 2)


def to_base_uniform(frame, base_w=BASE_W, base_h=BASE_H):
    """Кадр любого разрешения → внутренние 1920×1080 без растяжения.

    Игра масштабирует UI по меньшей оси, поэтому кадр сначала уменьшается
    равномерно (``sc = min(W/1920, H/1080)``), а затем из центра вырезается
    окно 1920×1080. Для 16:9 результат совпадает с прежним ``to_base``,
    для 16:10/21:9/4:3 — больше не «сплющивает» карточки.

    Возвращает ``(base_image, sc, x0, y0)``; ``x0/y0`` — смещение окна
    в пикселях базы (для перевода координат базы в экранные: ``x*sc + x0*sc``).
    """
    import cv2
    if frame is None:
        return None, 1.0, 0, 0
    height, width = frame.shape[:2]
    if width <= 0 or height <= 0:
        return None, 1.0, 0, 0
    sc, x0, y0 = base_origin(width, height, base_w, base_h)
    scaled_w = max(int(base_w), int(round(width / sc)))
    scaled_h = max(int(base_h), int(round(height / sc)))
    if (width, height) != (scaled_w, scaled_h):
        interp = (cv2.INTER_AREA if (scaled_w < width or scaled_h < height)
                  else cv2.INTER_CUBIC)
        frame = cv2.resize(frame, (scaled_w, scaled_h), interpolation=interp)
    base = frame[y0:y0 + int(base_h), x0:x0 + int(base_w)]
    if base.size == 0:
        return frame, sc, 0, 0
    return base, sc, x0, y0


# ─────────────────────────────────────────────────────────────────────────
#  UI scale из %LOCALAPPDATA%\Warframe\EE.cfg
# ─────────────────────────────────────────────────────────────────────────
def ee_cfg_path():
    """Путь к ``EE.cfg`` игры (или ``""``, если LOCALAPPDATA неизвестен)."""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return ""
    return os.path.join(base, EE_CFG_SUBPATH)


_UI_SCALE_RE = re.compile(
    r"(?:Flash\.)?FlashDrawScale\s*=\s*([0-9]+(?:[.,][0-9]+)?)", re.IGNORECASE)


def parse_ui_scale(text):
    """Найти ``Flash.FlashDrawScale`` в тексте EE.cfg → масштаб (0.5..2.0).

    Игра хранит значение в процентах (``100``), округление до 5 %; отсутствует
    ключ — возвращаем ``None`` (вызывающая сторона считает 100 %).
    """
    if not text:
        return None
    match = _UI_SCALE_RE.search(str(text))
    if not match:
        return None
    try:
        value = float(match.group(1).replace(",", "."))
    except Exception:
        return None
    scale = value / 100.0 if value > 3 else value
    percent = int(round(scale * 100.0 / 5.0)) * 5
    percent = max(50, min(200, percent))
    return round(percent / 100.0, 2)


def ui_scale_from_ee_cfg(path=None):
    """Прочитать UI scale игры. → ``(scale, found, path)``.

    ``found=False`` и ``scale=1.0``, если файла/ключа нет или он не читается
    (тогда приложение пишет в лог, что считает 100 %).
    """
    path = path or ee_cfg_path()
    if not path:
        return 1.0, False, ""
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
            text = handle.read()
    except Exception:
        return 1.0, False, path
    scale = parse_ui_scale(text)
    if scale is None:
        return 1.0, False, path
    return scale, True, path


# ─────────────────────────────────────────────────────────────────────────
#  Формулы геометрии (UI-пространство 1920×1080, умноженное на ui)
# ─────────────────────────────────────────────────────────────────────────
def row_width(ui=1.0):
    return ROW_W * float(ui)


def row_left(ui=1.0):
    return BASE_W / 2.0 - row_width(ui) / 2.0


def card_width(ui=1.0):
    return CARD_W * float(ui)


def card_top(ui=1.0):
    return BASE_H / 2.0 - Y_DISPLAY * float(ui)


def card_bottom(ui=1.0):
    return BASE_H / 2.0 - (Y_DISPLAY - CARD_H) * float(ui)


def name_band(ui=1.0):
    """Полоса названий: ``nameBandTop = H/2 − 129·ui``, высота ``48·ui``."""
    ui = float(ui)
    top = BASE_H / 2.0 - (Y_DISPLAY - CARD_H + NAME_H) * ui
    return {"left": row_left(ui), "top": top, "width": row_width(ui),
            "height": NAME_H * ui, "right": row_left(ui) + row_width(ui),
            "bottom": top + NAME_H * ui}


def scan_band(ui=1.0, pad=NAME_PAD):
    """OCR-полоса из геометрии (полоса имён + запас) — без иконок.

    При 1920×1080 / UI 100 %: ``left=461, top=396, width=998, height=78``.
    """
    ui = float(ui)
    band = name_band(ui)
    left = max(0.0, band["left"] - pad * ui)
    top = max(0.0, band["top"] - pad * ui)
    right = min(float(BASE_W), band["right"] + pad * ui)
    bottom = min(float(BASE_H), band["bottom"] + pad * ui)
    return {"left": int(round(left)), "top": int(round(top)),
            "width": int(round(right - left)),
            "height": int(round(bottom - top))}


def centers(count, ui=1.0):
    """Центры карточек: ряд всегда симметричен ``W/2``.

    ``n=4`` → 597/839/1081/1323, ``n=3`` → 718/960/1202,
    ``n=2`` → 839/1081, ``n=1`` → 960 (при UI 100 %).
    """
    count = max(1, min(4, int(count)))
    ui = float(ui)
    step = card_width(ui)
    middle = (count - 1) / 2.0
    return [BASE_W / 2.0 + (index - middle) * step for index in range(count)]


def card_lefts(count, ui=1.0):
    """Левые края карточек (вплотную, шаг = ширина)."""
    half = card_width(ui) / 2.0
    return [center - half for center in centers(count, ui)]


def card_rects(count, ui=1.0):
    """Прямоугольники карточек ``[{left, top, width, height, center_x}]``."""
    top = card_top(ui)
    width = card_width(ui)
    height = CARD_H * float(ui)
    return [{"left": left, "top": top, "width": width, "height": height,
             "center_x": left + width / 2.0}
            for left in card_lefts(count, ui)]


def click_zones(count, ui=1.0):
    """Зоны выбора (``GetSelectedReward``): прямоугольник + X-центры + Y клика."""
    count = max(1, min(4, int(count)))
    ui = float(ui)
    row = row_width(ui)
    left = row_left(ui)
    top = card_top(ui)
    bottom = BASE_H / 2.0 - (Y_DISPLAY - CARD_H) * ui * 0.5
    rect = {"left": left, "top": top, "width": row, "height": bottom / 2.0}
    if count == 3:
        rect["left"] = left + row / 8.0
        rect["width"] = row * 3.0 / 4.0
    return {"rect": rect,
            "centers_x": [left + factor * row / 8.0
                          for factor in (1, 3, 5, 7)][:count] if count == 4
            else [left + factor * row / 8.0 for factor in (2, 4, 6)][:count]
            if count == 3 else [center for center in centers(count, ui)],
            "click_y": top + bottom / 4.0}


def geometry(ui=1.0):
    """Полный набор геометрии для текущего UI scale (float)."""
    ui = float(ui)
    band = name_band(ui)
    lefts = card_lefts(4, ui)
    return {
        "lefts": lefts,
        "card_width": card_width(ui),
        "card_pitch": card_width(ui),
        "card_height": CARD_H * ui,
        "card_top": card_top(ui),
        "cards_center_x": BASE_W / 2.0,
        "name_offset_y": band["top"] - card_top(ui),
        "name_height": band["height"],
        "row_left": row_left(ui),
        "row_width": row_width(ui),
        "name_band": band,
        "scan_band": scan_band(ui),
        "ui_scale": ui,
        "source": "wfinfo",
    }


def config_values(ui=1.0):
    """Значения для ``DEFAULT_CONFIG`` (строки конфига) при UI scale."""
    values = geometry(ui)
    return {
        "card_width": values["card_width"],
        "card_pitch": values["card_pitch"],
        "cards_center_x": values["cards_center_x"],
        "card_top": values["card_top"],
        "card_height": values["card_height"],
        "name_offset_y": values["name_offset_y"],
        "name_height": values["name_height"],
        "scan_band": dict(values["scan_band"]),
    }


def fits_frame(image, count=4, ui=1.0):
    """Влезает ли ряд карточек в кадр (для валидации кадра)."""
    try:
        height, width = image.shape[:2]
    except Exception:
        return False
    rects = card_rects(count, ui)
    for rect in rects:
        if rect["left"] < 0 or rect["top"] < 0:
            return False
        if rect["left"] + rect["width"] > width + 1:
            return False
        if rect["top"] + rect["height"] > height + 1:
            return False
    return True


__all__ = ["BASE_W", "BASE_H", "ROW_W", "CARD_W", "CARD_H", "Y_DISPLAY",
           "NAME_H", "NAME_PAD", "screen_scaling", "base_origin",
           "to_base_uniform", "ee_cfg_path", "parse_ui_scale",
           "ui_scale_from_ee_cfg", "row_width", "row_left", "card_width",
           "card_top", "card_bottom", "name_band", "scan_band", "centers",
           "card_lefts", "card_rects", "click_zones", "geometry",
           "config_values", "fits_frame"]
