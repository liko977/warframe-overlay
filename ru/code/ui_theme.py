# -*- coding: utf-8 -*-
"""Единая дизайн-система оверлея (стиль AlecaFrame).

Только константы и строки QSS — никаких зависимостей и импорта Qt, поэтому
модуль можно использовать где угодно (маркет-браузер, панель событий, редактор
списка, диалоги ордеров) и тестировать строки стилей без запуска GUI.

Палитра и правила (по заданию t6):
    фон окна #0d131c (alpha 0.96), панель #121a25, карточка #17212e,
    границы #24344a, текст #e6edf6, вторичный #93a1b5, акцент #4aa8ff,
    успех #43d17c, предупреждение #f5c451, опасность #ff6b6b,
    платина #E7F1FF (светлая, не серая/не жёлтая), дукаты #C9A66B.
"""

PALETTE = {
    "window": "#0d131c",
    "window_alpha": 0.96,
    "panel": "#121a25",
    "card": "#17212e",
    "border": "#24344a",
    "text": "#e6edf6",
    "muted": "#93a1b5",
    "accent": "#4aa8ff",
    "success": "#43d17c",
    "warning": "#f5c451",
    "danger": "#ff6b6b",
    "platinum": "#E7F1FF",
    "ducats": "#C9A66B",
    "currency_platinum": "#E7F1FF",
    "currency_ducats": "#C9A66B",
    "currency_credits": "#9BC7A8",
    "hover": "#1e2a3a",
    "scroll": "#2a3a4e",
    "zebra": "rgba(255,255,255,0.03)",
    "grid": "#1f2b3a",
    "area": "rgba(74,168,255,0.18)",
}

FONT = "Segoe UI"
# 1.1.0 «E: Строгий терминал»
RADIUS = 2
ROW_H = 28
FONT_UI = "Segoe UI"
FONT_MONO = "Consolas"
#: Готовые варианты цветов валют (без свободного пикера).
PLATINUM_CHOICES = ("#E7F1FF", "#8FD8FF", "#7CC7FF", "#CFE8FF")
DUCATS_CHOICES = ("#C9A66B", "#F5C451", "#D4AF37", "#E0B45C")
CURRENCY_TOOLTIP = "Платина (warframe.market)"
DUCATS_TOOLTIP = "Дукаты (warframe.market)"
_CURRENCY_LISTENERS = []
FONT_SIZES = {
    "window": 14,      # заголовок окна, bold
    "section": 11,     # заголовки секций, bold uppercase
    "text": 12,        # основной текст
    "detail": 10,      # детали, muted
    "price": 13,       # цены, bold
    "button": 11,      # кнопки
    "title": 16,       # крупное название предмета в деталях
}

#: Бейджи ордеров: sell — зелёный, buy — красный.
BADGE_SELL = PALETTE["success"]
BADGE_BUY = PALETTE["danger"]

#: Цвета графика истории цен.
CHART_GRID = PALETTE["grid"]
CHART_AREA = PALETTE["area"]
CHART_LINE = PALETTE["accent"]
CHART_VOLUME = "rgba(74,168,255,0.35)"


def mono_qss(size=10):
    """QSS для моноширинных чисел/меток E."""
    return (f"font-family:'{FONT_MONO}', monospace; "
            f"font-size:{int(size)}px;")


def caps_header_qss():
    """Шапка таблицы/группы: капс, mono, muted."""
    return (f"font-family:'{FONT_MONO}', monospace; font-size:9px; "
            f"color:{PALETTE['muted']}; letter-spacing:1px; "
            "text-transform:uppercase;")


def chip_qss(part_type="", is_set=False):
    """Метка части: рамка 1 px, без заливки/скруглений."""
    color = currency_platinum() if is_set else PALETTE["muted"]
    return (f"font-family:'{FONT_MONO}', monospace; font-size:9px; "
            f"color:{color}; border:1px solid {PALETTE['border']}; "
            "background:transparent; border-radius:0px; padding:0px 4px;")


def _rgb(color):
    text = str(color or "").lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        raise ValueError("цвет должен быть #rrggbb")
    try:
        return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
    except Exception as exc:
        raise ValueError(f"неверный цвет: {color!r}") from exc


def luminance(color):
    """Относительная яркость sRGB (WCAG)."""
    channels = []
    for value in _rgb(color):
        value = value / 255.0
        channels.append(value / 12.92 if value <= 0.03928
                        else ((value + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast_ratio(fg, bg):
    """Контраст двух цветов по WCAG (1..21)."""
    a, b = luminance(fg), luminance(bg)
    light, dark = max(a, b), min(a, b)
    return (light + 0.05) / (dark + 0.05)


def validate_currency_colors(platinum, ducats):
    """Проверяет, что валюты различимы и не совпадают с текстом."""
    p, d = str(platinum or "").upper(), str(ducats or "").upper()
    _rgb(p)
    _rgb(d)
    if p == d:
        raise ValueError("цвет платины и дукатов не должен совпадать")
    if p == PALETTE["text"].upper():
        raise ValueError("цвет платины не должен совпадать с текстом")
    if d == PALETTE["text"].upper():
        raise ValueError("цвет дукатов не должен совпадать с текстом")
    return True


def currency_platinum():
    return PALETTE.get("currency_platinum") or PALETTE["platinum"]


def currency_ducats():
    return PALETTE.get("currency_ducats") or PALETTE["ducats"]


def currency_credits():
    return PALETTE.get("currency_credits", "#9BC7A8")


def on_currency_change(callback):
    """Подписка на смену цветов валют (для живого обновления виджетов)."""
    if callable(callback) and callback not in _CURRENCY_LISTENERS:
        _CURRENCY_LISTENERS.append(callback)
    return callback


def set_currency_colors(platinum=None, ducats=None, notify=True):
    """Применяет цвета валют без перезапуска; возвращает (p, d)."""
    p = str(platinum or currency_platinum())
    d = str(ducats or currency_ducats())
    validate_currency_colors(p, d)
    PALETTE["currency_platinum"] = p
    PALETTE["currency_ducats"] = d
    PALETTE["platinum"] = p
    PALETTE["ducats"] = d
    if notify:
        for callback in list(_CURRENCY_LISTENERS):
            try:
                callback(p, d)
            except Exception:
                pass
    return p, d


def configure_currency_colors(cfg):
    """Цвета из config (currency_*_color) с безопасными умолчаниями."""
    cfg = dict(cfg or {})
    p = str(cfg.get("currency_platinum_color") or currency_platinum())
    d = str(cfg.get("currency_ducats_color") or currency_ducats())
    try:
        return set_currency_colors(p, d)
    except ValueError:
        return set_currency_colors("#E7F1FF", "#C9A66B")


def currency_qss(kind="platinum", px=None):
    """Стиль цены/валюты: platinum по умолчанию, ducats по запросу."""
    color = currency_ducats() if str(kind) == "ducats" else currency_platinum()
    size = f" font-size:{int(px)}px;" if px else ""
    return f"color:{color};{size} font-weight:700; background: transparent;"


def currency_tooltip(kind="platinum"):
    return CURRENCY_TOOLTIP if str(kind) != "ducats" else DUCATS_TOOLTIP


def rgba(hex_color, alpha):
    """``#rrggbb`` + прозрачность → строка ``rgba(r, g, b, a)`` для QSS."""
    text = str(hex_color or "").lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    try:
        r, g, b = int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except Exception:
        r, g, b = 13, 19, 28
    try:
        alpha = max(0.0, min(1.0, float(alpha)))
    except Exception:
        alpha = 1.0
    return f"rgba({r}, {g}, {b}, {alpha:.2f})"


def size(kind, default=12):
    return FONT_SIZES.get(str(kind), default)


def window_qss(name="win", radius=8):
    """Окно/панель: фон #0d131c с alpha 0.96, рамка, скругление, скроллбар."""
    p = PALETTE
    return f"""
        QFrame#{name} {{
            background-color: {rgba(p['window'], p['window_alpha'])};
            border: 1px solid {p['border']};
            border-radius: {int(radius)}px;
        }}
        QLabel {{
            background: transparent;
            color: {p['text']};
            font-family: '{FONT}', Arial, sans-serif;
        }}
        QScrollArea {{ background: transparent; border: none; }}
        {scroll_qss()}
    """


def panel_qss(name="panel", radius=6):
    """Внутренняя панель/карточка: #121a25 с рамкой #24344a."""
    p = PALETTE
    return f"""
        QFrame#{name} {{
            background-color: {p['panel']};
            border: 1px solid {p['border']};
            border-radius: {int(radius)}px;
        }}
    """


def card_qss(name="card", radius=6):
    p = PALETTE
    return f"""
        QFrame#{name} {{
            background-color: {p['card']};
            border: 1px solid {p['border']};
            border-radius: {int(radius)}px;
        }}
    """


def scroll_qss():
    """Тонкий скроллбар 8 px цвета #2a3a4e."""
    p = PALETTE
    return f"""
        QScrollBar:vertical {{
            background: transparent; width: 8px; margin: 0px; border: none;
        }}
        QScrollBar::handle:vertical {{
            background: {p['scroll']}; border-radius: 4px; min-height: 24px;
        }}
        QScrollBar::handle:vertical:hover {{ background: {p['accent']}; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
            height: 0px;
        }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
            background: transparent;
        }}
        QScrollBar:horizontal {{
            background: transparent; height: 8px; margin: 0px; border: none;
        }}
        QScrollBar::handle:horizontal {{
            background: {p['scroll']}; border-radius: 4px; min-width: 24px;
        }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
            width: 0px;
        }}
    """


def section_qss(accent=None):
    """Заголовок секции: 11 px bold uppercase #93a1b5 (+ letter-spacing в QFont)."""
    p = PALETTE
    color = accent or p["muted"]
    return (f"color:{color}; font-size:{size('section')}px; font-weight:700;"
            " background: transparent;")


def divider_qss():
    return f"background-color: {rgba(PALETTE['muted'], 0.18)}; border: none;"


def row_qss(zebra=False, selected=False, radius=4, name="mRow"):
    """Zebra-строка 24 px: карточка #17212e, hover/selected #1e2a3a."""
    p = PALETTE
    background = p["hover"] if selected else (p["zebra"] if zebra else p["card"])
    return f"""
        QFrame#{name} {{
            background-color: {background};
            border-radius: {int(radius)}px;
            border: 1px solid transparent;
        }}
        QFrame#{name}:hover {{
            background-color: {p['hover']};
            border: 1px solid {rgba(p['accent'], 0.35)};
        }}
    """


def badge_qss(background, foreground=None, radius=3, small=True):
    """Бейдж-пилюля: sell/buy/ранг/тип. Цвет текста по умолчанию белый."""
    fg = foreground or "#ffffff"
    font = 9 if small else 10
    return (f"background-color: {background}; color: {fg};"
            f" border-radius: {int(radius)}px; padding: 0px 6px;"
            f" font-size: {font}px; font-weight: 600;")


def pill_qss(color, radius=8):
    """Пилюля таймера/цены: тёмная подложка, цветной текст."""
    p = PALETTE
    return (f"color: {color}; background-color: {rgba(p['window'], 0.75)};"
            f" border: 1px solid {p['border']}; border-radius: {int(radius)}px;"
            " padding: 0px 7px;")


def button_qss(primary=False, danger=False, small=False):
    """Кнопки 11 px; primary — акцентная, danger — красная."""
    p = PALETTE
    padding = "2px 8px" if small else "5px 12px"
    if primary:
        background, border, color = rgba(p["accent"], 0.18), p["accent"], "#dcefff"
    elif danger:
        background, border, color = rgba(p["danger"], 0.16), p["danger"], "#ffd9d9"
    else:
        background, border, color = p["card"], p["border"], p["text"]
    return f"""
        QPushButton {{
            color: {color}; background-color: {background};
            border: 1px solid {border}; border-radius: 4px;
            padding: {padding}; font-size: {size('button')}px;
            font-family: '{FONT}', Arial, sans-serif;
        }}
        QPushButton:hover {{
            background-color: {p['hover']};
            border-color: {p['accent'] if primary or not danger else p['danger']};
        }}
        QPushButton:pressed {{ background-color: {rgba(p['accent'], 0.3)}; }}
        QPushButton:checked {{
            background-color: {rgba(p['accent'], 0.22)};
            border-color: {p['accent']}; color: #eaf5ff;
        }}
        QPushButton:disabled {{ color: {p['muted']}; border-color: {p['border']}; }}
    """


def tab_qss():
    """Таб-бар: активная вкладка с подчёркиванием акцентом."""
    p = PALETTE
    return f"""
        QPushButton {{
            color: {p['muted']}; background: transparent; border: none;
            border-bottom: 2px solid transparent;
            padding: 6px 14px; font-size: 12px; font-weight: 600;
            font-family: '{FONT}', Arial, sans-serif;
        }}
        QPushButton:hover {{ color: {p['text']}; }}
        QPushButton:checked {{
            color: {p['text']}; border-bottom: 2px solid {p['accent']};
        }}
    """


def input_qss():
    p = PALETTE
    return f"""
        QLineEdit, QSpinBox, QComboBox {{
            background-color: {p['panel']}; color: {p['text']};
            border: 1px solid {p['border']}; border-radius: 6px;
            padding: 6px 8px; font-size: 12px;
            selection-background-color: {rgba(p['accent'], 0.45)};
        }}
        QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{
            border-color: {p['accent']};
        }}
        QComboBox QAbstractItemView {{
            background-color: {p['panel']}; color: {p['text']};
            selection-background-color: {p['hover']};
            border: 1px solid {p['border']};
        }}
    """


def progress_qss(color):
    p = PALETTE
    return (f"QProgressBar {{ background: {rgba(p['muted'], 0.12)}; border: none;"
            f" border-radius: 3px; }} QProgressBar::chunk {{ background: {color};"
            " border-radius: 3px; }")


def dialog_qss():
    """Диалог (ордер/токен) в палитре: окно, поля, кнопки, чекбоксы."""
    p = PALETTE
    return f"""
        QDialog {{
            background-color: {rgba(p['window'], p['window_alpha'])};
        }}
        QLabel {{
            color: {p['text']}; background: transparent;
            font-family: '{FONT}', Arial, sans-serif;
        }}
        {input_qss()}
        QCheckBox {{ color: {p['text']}; font-size: 11px; }}
        QCheckBox::indicator {{
            width: 14px; height: 14px; border-radius: 3px;
            border: 1px solid {p['border']}; background: {p['panel']};
        }}
        QCheckBox::indicator:checked {{
            background: {p['accent']}; border-color: {p['accent']};
        }}
        QPushButton {{
            color: {p['text']}; background-color: {p['card']};
            border: 1px solid {p['border']}; border-radius: 4px;
            padding: 5px 12px; font-size: 11px;
        }}
        QPushButton:hover {{
            background-color: {p['hover']}; border-color: {p['accent']};
        }}
        QPushButton:default, QPushButton:checked {{
            background-color: {rgba(p['accent'], 0.2)};
            border-color: {p['accent']}; color: #eaf5ff;
        }}
        QPushButton:disabled {{ color: {p['muted']}; }}
        {scroll_qss()}
    """


def safe_palette_color(key, fallback):
    """Цвет из PALETTE с валидацией (иначе литерал — без белого фолбэка)."""
    value = PALETTE.get(str(key))
    if not isinstance(value, str):
        return fallback
    text = value.strip()
    if not text:
        return fallback
    import re as _re
    if _re.fullmatch(r"#[0-9a-fA-F]{6}", text):
        return text
    if _re.fullmatch(r"#[0-9a-fA-F]{8}", text):
        return text
    if _re.fullmatch(r"rgba?\([^)]*\)", text):
        return text
    return fallback


def notifications_qss():
    """QSS центра уведомлений: диалог, скролл/viewport, список, кнопки.

    Все цвета — из PALETTE с валидацией и литеральным фолбэком
    (``#121a25`` и т.п.), поэтому строки всегда валидны и белого фона нет.
    """
    bg = safe_palette_color("panel", "#121a25")
    card = safe_palette_color("card", "#17212e")
    border = safe_palette_color("border", "#24344a")
    text = safe_palette_color("text", "#e6edf6")
    muted = safe_palette_color("muted", "#93a1b5")
    hover = safe_palette_color("hover", "#1e2a3a")
    accent = safe_palette_color("accent", "#4aa8ff")
    return f"""
        QDialog#notifDialog {{
            background-color: {bg};
        }}
        QDialog#notifDialog QLabel {{
            color: {text}; background: transparent;
        }}
        QWidget#notifCenter {{
            background-color: {bg};
        }}
        QWidget#notifBody {{
            background-color: transparent;
        }}
        QScrollArea#notifScroll {{
            background-color: {bg};
            border: 1px solid {border};
            border-radius: 6px;
        }}
        QWidget#notifViewport {{
            background-color: {bg};
        }}
        QScrollArea#notifScroll > QWidget > QWidget {{
            background-color: {bg};
        }}
        QLabel#notifHeader {{
            color: {muted}; background: transparent;
            font-weight: 600; padding-top: 4px;
        }}
        QPushButton {{
            color: {text}; background-color: {card};
            border: 1px solid {border}; border-radius: 4px;
            padding: 4px 10px;
        }}
        QPushButton:hover {{
            background-color: {hover}; border-color: {accent};
        }}
        QPushButton:disabled {{ color: {muted}; }}
        QFrame#notifRow {{
            background-color: {card};
            border: 1px solid {border};
            border-radius: 6px;
        }}
        QFrame#notifRow:hover {{
            background-color: {hover};
            border: 1px solid {accent};
        }}
        {scroll_qss()}
    """


def status_qss(stale=False):
    p = PALETTE
    color = p["warning"] if stale else p["muted"]
    return f"color:{color}; font-size:10px; background: transparent;"


def price_qss(color=None):
    p = PALETTE
    return (f"color:{color or p['platinum']}; font-size:{size('price')}px;"
            " font-weight:700; background: transparent;")


def muted_qss(px=None):
    p = PALETTE
    return (f"color:{p['muted']}; font-size:{px or size('detail')}px;"
            " background: transparent;")


def chart_qss():
    """График истории цен."""
    p = PALETTE
    return (f"background-color: {p['panel']}; border: 1px solid {p['border']};"
            " border-radius: 6px;")
