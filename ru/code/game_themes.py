# -*- coding: utf-8 -*-
"""Темы интерфейса Warframe: таблица и определение по пробе (1.0.0).

Данные/формулы получены из WFInfo (WFCD/WFinfo, Apache-2.0):
https://github.com/WFCD/WFinfo (commit 161f371, v9.8.2), файл ``WFInfo/Ocr.cs``
(``enum WFtheme``, ``AllThemes``, ``GetThemeWeighted``, ``GetProbeColors``).
Изменено: перенос в Python, добавлены русскоязычные сообщения, кэш и
собственные проверки. Лицензия — ``licenses/Apache-2.0.txt``,
атрибуция — ``THIRD_PARTY_NOTICES.md``.

Таблица — 19 тем в порядке ``enum WFtheme`` (индексы 0..18). Для каждой:
``primary``/``secondary`` — цвета акцента UI, ``probe_top``/``probe_bot`` —
усреднённые цвета полосы профиля (верх/низ), по которым и определяется тема.

Определение темы (актуальный путь WFInfo — probe):

* одна вертикальная линия пикселей ``x = 150·sc``, ``y = 85·sc .. 93·sc``,
  где ``sc = ScreenScaling · max(uiScaling, 0.5)``; на нашем кадре,
  уже приведённом к 1920×1080, остаётся ``max(ui, 0.5)``;
* ``avgTop`` — средний RGB по строкам выше середины пробы, ``avgBot`` — по
  строкам от середины и ниже;
* ``dist = L1(avgTop, probe_top) + L1(avgBot, probe_bot)`` (0..765);
* ``weight = 1 / (dist + 1)``; активная тема — максимальный вес.

HSL-фильтры (``ThemeThresholdFilter``) и редактор ``ThemeAdjuster`` из WFInfo
**не переносились**: тема используется только для диагностики/валидации кадра,
геометрия от неё не зависит.
"""


AUTO = "AUTO"
#: ниже этого веса считаем, что тема не определена (мусор/нестандартный скин)
MIN_WEIGHT = 0.01

#: порядок = enum WFtheme (0..18); цвета — RGB из AllThemes
THEMES = (
    {"name": "VITRUVIAN", "primary": (190, 169, 102),
     "secondary": (245, 227, 173),
     "probe_top": (189, 168, 101), "probe_bot": (26, 22, 24)},
    {"name": "STALKER", "primary": (153, 31, 35),
     "secondary": (255, 61, 51),
     "probe_top": (152, 31, 35), "probe_bot": (17, 4, 4)},
    {"name": "BARUUK", "primary": (238, 193, 105),
     "secondary": (236, 211, 162),
     "probe_top": (237, 192, 104), "probe_bot": (60, 55, 43)},
    {"name": "CORPUS", "primary": (35, 201, 245),
     "secondary": (111, 229, 253),
     "probe_top": (35, 200, 244), "probe_bot": (7, 39, 63)},
    {"name": "FORTUNA", "primary": (57, 105, 192),
     "secondary": (255, 115, 230),
     "probe_top": (57, 105, 191), "probe_bot": (7, 9, 34)},
    {"name": "GRINEER", "primary": (255, 189, 102),
     "secondary": (255, 224, 153),
     "probe_top": (254, 188, 101), "probe_bot": (18, 27, 16)},
    {"name": "LOTUS", "primary": (36, 184, 242),
     "secondary": (255, 241, 191),
     "probe_top": (36, 183, 241), "probe_bot": (39, 53, 96)},
    {"name": "NIDUS", "primary": (140, 38, 92),
     "secondary": (245, 73, 93),
     "probe_top": (139, 38, 91), "probe_bot": (220, 211, 197)},
    {"name": "OROKIN", "primary": (20, 41, 29),
     "secondary": (178, 125, 5),
     "probe_top": (20, 41, 29), "probe_bot": (203, 209, 208)},
    {"name": "TENNO", "primary": (9, 78, 106),
     "secondary": (6, 106, 74),
     "probe_top": (9, 78, 105), "probe_bot": (183, 204, 207)},
    {"name": "HIGH_CONTRAST", "primary": (102, 176, 255),
     "secondary": (255, 255, 0),
     "probe_top": (101, 175, 254), "probe_bot": (15, 31, 61)},
    {"name": "LEGACY", "primary": (255, 255, 255),
     "secondary": (232, 213, 93),
     "probe_top": (254, 254, 254), "probe_bot": (35, 60, 70)},
    {"name": "EQUINOX", "primary": (158, 159, 167),
     "secondary": (232, 227, 227),
     "probe_top": (157, 159, 166), "probe_bot": (19, 12, 21)},
    {"name": "DARK_LOTUS", "primary": (140, 119, 147),
     "secondary": (200, 169, 237),
     "probe_top": (139, 119, 146), "probe_bot": (41, 11, 85)},
    {"name": "ZEPHYR", "primary": (253, 132, 2),
     "secondary": (255, 53, 0),
     "probe_top": (252, 132, 2), "probe_bot": (27, 26, 27)},
    {"name": "CONQUERA", "primary": (200, 100, 200),
     "secondary": (255, 215, 0),
     "probe_top": (254, 254, 254), "probe_bot": (177, 66, 182)},
    {"name": "DEADLOCK", "primary": (25, 35, 60),
     "secondary": (255, 255, 255),
     "probe_top": (254, 254, 254), "probe_bot": (30, 40, 62)},
    {"name": "LUNAR_RENEWAL", "primary": (160, 40, 40),
     "secondary": (255, 200, 100),
     "probe_top": (254, 254, 254), "probe_bot": (101, 28, 29)},
    {"name": "POM_2", "primary": (105, 185, 140),
     "secondary": (100, 255, 100),
     "probe_top": (129, 223, 150), "probe_bot": (11, 47, 31)},
)

PROBE_X = 150
PROBE_Y1 = 85
PROBE_Y2 = 93


def theme_names():
    """Имена тем в порядке enum WFtheme."""
    return [theme["name"] for theme in THEMES]


def get_theme(name):
    """Тема по имени (регистр/подчёркивания не важны) или ``None``."""
    text = str(name or "").strip().upper().replace(" ", "_")
    for theme in THEMES:
        if theme["name"] == text:
            return dict(theme)
    return None


def color_difference(first, second):
    """``ColorDifference`` WFInfo: L1-расстояние по RGB (0..765)."""
    try:
        return (abs(int(first[0]) - int(second[0]))
                + abs(int(first[1]) - int(second[1]))
                + abs(int(first[2]) - int(second[2])))
    except Exception:
        return 765


def probe_scale(ui=1.0):
    """``sc = ScreenScaling · max(uiScaling, 0.5)`` — для кадра-базы."""
    try:
        ui = float(ui or 1.0)
    except Exception:
        ui = 1.0
    return max(ui, 0.5)


def probe_coords(ui=1.0):
    """Координаты пробы: ``(x, y1, y2, mid)``."""
    sc = probe_scale(ui)
    x = int(round(PROBE_X * sc))
    y1 = int(round(PROBE_Y1 * sc))
    y2 = int(round(PROBE_Y2 * sc))
    return x, y1, y2, (y1 + y2) / 2.0


def probe_colors(image, ui=1.0):
    """Средние цвета пробы ``(avg_top, avg_bot)`` или ``None``.

    ``image`` — кадр уже во внутреннем пространстве 1920×1080 (BGR).
    """
    if image is None:
        return None
    try:
        height, width = image.shape[:2]
    except Exception:
        return None
    x, y1, y2, mid = probe_coords(ui)
    if width <= x or height <= y2 or y2 <= y1:
        return None
    top_rows = image[y1:int(mid), x]
    bot_rows = image[int(mid):y2 + 1, x]
    if len(top_rows) == 0 or len(bot_rows) == 0:
        return None
    try:
        top = top_rows.reshape(-1, top_rows.shape[-1]).mean(axis=0)
        bot = bot_rows.reshape(-1, bot_rows.shape[-1]).mean(axis=0)
    except Exception:
        return None
    # OpenCV даёт BGR, таблица WFInfo — RGB
    top_rgb = (int(round(top[2])), int(round(top[1])), int(round(top[0])))
    bot_rgb = (int(round(bot[2])), int(round(bot[1])), int(round(bot[0])))
    return top_rgb, bot_rgb


def theme_weights(avg_top, avg_bot):
    """Веса тем: ``[(name, dist, weight)]``, максимум первым."""
    rows = []
    for theme in THEMES:
        dist = (color_difference(avg_top, theme["probe_top"])
                + color_difference(avg_bot, theme["probe_bot"]))
        rows.append({"name": theme["name"], "dist": dist,
                     "weight": 1.0 / (dist + 1.0)})
    rows.sort(key=lambda row: row["weight"], reverse=True)
    return rows


def detect_theme(image, ui=1.0, manual=None, min_weight=MIN_WEIGHT):
    """Определить тему по кадру (или взять ручной выбор).

    Возвращает ``dict``: ``theme`` (``AUTO``/имя), ``manual``, ``dist``,
    ``weight``, ``top``/``bottom`` (RGB пробы), ``candidates`` (топ-5).
    Мусор/нестандартный скин → ``AUTO`` (вес ниже ``min_weight``).
    """
    manual_name = str(manual or "").strip()
    if manual_name and manual_name.upper() != AUTO:
        theme = get_theme(manual_name)
        if theme is not None:
            return {"theme": theme["name"], "manual": True, "dist": None,
                    "weight": None, "top": None, "bottom": None,
                    "candidates": [], "ui_scale": float(ui or 1.0)}
    colors = probe_colors(image, ui=ui)
    if colors is None:
        return {"theme": AUTO, "manual": False, "dist": None,
                "weight": 0.0, "top": None, "bottom": None,
                "candidates": [], "ui_scale": float(ui or 1.0)}
    top, bottom = colors
    rows = theme_weights(top, bottom)
    best = rows[0] if rows else {"name": AUTO, "dist": 765, "weight": 0.0}
    if best["weight"] < float(min_weight):
        return {"theme": AUTO, "manual": False, "dist": best["dist"],
                "weight": best["weight"], "top": top, "bottom": bottom,
                "candidates": rows[:5], "ui_scale": float(ui or 1.0)}
    return {"theme": best["name"], "manual": False, "dist": best["dist"],
            "weight": best["weight"], "top": top, "bottom": bottom,
            "candidates": rows[:5], "ui_scale": float(ui or 1.0)}


def log_line(result):
    """Строка для app.log: «Тема: OROKIN (dist=70)»."""
    result = dict(result or {})
    name = str(result.get("theme") or AUTO)
    if result.get("manual"):
        return f"Тема: {name} (вручную)"
    if name == AUTO:
        dist = result.get("dist")
        tail = f" (dist={dist})" if isinstance(dist, int) else ""
        return f"Тема: AUTO (не определена){tail}"
    dist = result.get("dist")
    if isinstance(dist, int):
        return f"Тема: {name} (dist={dist})"
    return f"Тема: {name}"


def manual_names():
    """Варианты для выпадающего списка: «Авто» + 19 тем."""
    return [AUTO] + theme_names()


__all__ = ["THEMES", "AUTO", "MIN_WEIGHT", "PROBE_X", "PROBE_Y1", "PROBE_Y2",
           "theme_names", "get_theme", "color_difference", "probe_scale",
           "probe_coords", "probe_colors", "theme_weights", "detect_theme",
           "log_line", "manual_names"]
