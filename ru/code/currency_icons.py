# -*- coding: utf-8 -*-
"""Иконки валют VoidFrameRU (1.0.0): платина и дукаты картинкой.

Ассеты лежат в ``assets/currency/`` (исходники, one-folder ``_internal`` и
``_MEIPASS``)::

    cur_platinum_16.png, cur_platinum_24.png, cur_platinum_64.png,
    cur_platinum_256.png
    cur_ducat_16.png, cur_ducat_24.png, cur_ducat_64.png, cur_ducat_256.png

Правила:
* ``currency_icons: true`` в config (по умолчанию ВКЛ) — показываем картинку;
  выключено — старые текстовые суффиксы «p» / «дк»;
* размер выбирается по DPI: 16 px (100 %), 24 px (125–150 %), 64 px для
  крупных плашек (мастер/превью). Если нужного размера нет — берём ближайший;
* если файлов нет (сборка без ассетов) — текстовый фолбэк, без исключений;
* HTML для rich-text: ``<img src="file:///…" width="10" height="10">`` —
  высота строки не растёт больше чем на 2 px.

Модуль не импортирует PyQt5 на уровне модуля: функции картинок подтягивают
Qt лениво, поэтому HTML-хелперы работают и в тестах без QApplication.
"""

import os
import sys

CURRENCIES = ("platinum", "ducats")

#: Текстовые суффиксы, если иконка недоступна/выключена.
SUFFIX = {"platinum": "p", "ducats": "дк"}

#: Имя файла ассета (в assets/currency: cur_platinum_*, cur_ducat_*).
ASSET_NAME = {"platinum": "platinum", "ducats": "ducat"}

#: Русские названия для tooltip/настроек.
TITLES = {"platinum": "платина", "ducats": "дукаты"}

#: Доступные размеры PNG (квадратные).
SIZES = (16, 24, 64, 256)

#: Размер по умолчанию для rich-text (чтобы строка не «толстела»).
INLINE_PX = 10

_pixmap_cache = {}
_path_cache = {}


# ── пути ───────────────────────────────────────────────────────────────
def _roots():
    roots = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            roots.append(meipass)
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        roots.append(os.path.join(exe_dir, "_internal"))
    app_dir = os.environ.get("WF_APP_DIR") or ""
    if app_dir:
        roots.append(app_dir)
    here = os.path.dirname(os.path.abspath(__file__))
    roots += [here, os.path.dirname(here)]
    return [root for root in roots if root]


def asset_dir():
    """Каталог ``assets/currency`` или ``""`` (ассетов нет)."""
    if "asset_dir" in _path_cache:
        return _path_cache["asset_dir"]
    found = ""
    for root in _roots():
        path = os.path.join(root, "assets", "currency")
        if os.path.isdir(path):
            found = path
            break
    _path_cache["asset_dir"] = found
    return found


def choose_size(size=None, scale=None, big=False):
    """Размер PNG: DPI (16/24) или 64 для крупных плашек; ближайший из SIZES."""
    if size:
        try:
            value = int(size)
        except Exception:
            value = 16
        return min(SIZES, key=lambda item: abs(item - value))
    if big:
        return 64
    try:
        scale_value = float(scale or 0.0)
    except Exception:
        scale_value = 0.0
    if scale_value >= 1.25:
        return 24
    return 16


def icon_path(currency, size=None, scale=None, big=False):
    """Путь к PNG нужного размера или ``""`` (файла нет)."""
    currency = str(currency or "").strip().lower()
    if currency not in CURRENCIES:
        return ""
    root = asset_dir()
    if not root:
        return ""
    wanted = choose_size(size=size, scale=scale, big=big)
    base = ASSET_NAME.get(currency, currency)
    for value in sorted(SIZES, key=lambda item: abs(item - wanted)):
        path = os.path.join(root, f"cur_{base}_{int(value)}.png")
        if os.path.isfile(path):
            return path
    return ""


# ── Qt: QPixmap и маленький QLabel ─────────────────────────────────────
def pixmap(currency, size=None, scale=None, big=False, exact=False):
    """``QPixmap`` с иконкой (кэш) или ``None``; ``exact`` — точный size."""
    path = icon_path(currency, size=size, scale=scale, big=big)
    if not path:
        return None
    key = (path, os.path.getsize(path))
    cached = _pixmap_cache.get(key)
    if cached is not None:
        return cached
    try:
        from PyQt5.QtGui import QPixmap
        pix = QPixmap(path)
        if pix.isNull():
            return None
        if exact and size:
            from PyQt5.QtCore import Qt
            side = max(1, int(size))
            pix = pix.scaled(side, side, Qt.KeepAspectRatio,
                             Qt.SmoothTransformation)
            if pix.isNull():
                return None
        _pixmap_cache[key] = pix
        return pix
    except Exception:
        return None


def currency_pixmap(currency, size, cfg=None):
    """Единый helper: non-null QPixmap ровно size×size или None (t97)."""
    if not enabled(cfg):
        return None
    return pixmap(currency, size=size, exact=True)


def icon_label(currency, cfg=None, size=None, scale=None, big=False,
               parent=None):
    """Маленький ``QLabel`` с иконкой валюты или ``None``.

    Возвращает ``None``, если иконки выключены/нет ассетов/нет Qt — вызывающий
    код в этом случае остаётся на текстовом суффиксе.
    """
    if not enabled(cfg):
        return None
    pix = currency_pixmap(currency, int(size or 16), cfg=cfg)
    if pix is None:
        return None
    try:
        from PyQt5.QtCore import Qt
        from PyQt5.QtWidgets import QLabel
        label = QLabel(parent)
        label.setPixmap(pix)
        label.setFixedSize(pix.width(), pix.height())
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet("background: transparent; border: none;")
        title = TITLES.get(str(currency or "").lower(), "")
        if title:
            label.setToolTip(title)
        return label
    except Exception:
        return None


# ── HTML / текст ───────────────────────────────────────────────────────
def enabled(cfg=None):
    """Включены ли иконки валют в конфиге (по умолчанию ДА)."""
    try:
        return bool((cfg or {}).get("currency_icons", True))
    except Exception:
        return True


def _file_url(path):
    try:
        from pathlib import Path
        return Path(path).as_uri()
    except Exception:
        return "file:///" + str(path).replace("\\", "/")


def html(currency, cfg=None, px=None, size=None, scale=None, big=False,
         style="vertical-align:middle;"):
    """HTML-иконка валюты или текстовый суффикс («p»/«дк»)."""
    currency = str(currency or "").strip().lower()
    suffix = SUFFIX.get(currency, "")
    if not enabled(cfg):
        return suffix
    path = icon_path(currency, size=size, scale=scale, big=big)
    if not path:
        return suffix
    side = int(px or INLINE_PX)
    style_attr = f' style="{style}"' if style else ""
    return (f'<img src="{_file_url(path)}" width="{side}" '
            f'height="{side}"{style_attr}/>')


def format_money(amount, currency="platinum", cfg=None, rich=True,
                 px=None, space="&#160;"):
    """Сумма + иконка: ``120`` + иконка платины (или «120 p» без иконок)."""
    try:
        number = f"{int(round(float(amount)))}"
    except Exception:
        number = str(amount if amount is not None else "—")
    suffix = SUFFIX.get(str(currency or "").lower(), "")
    if not rich:
        return f"{number} {suffix}".strip()
    icon = html(currency, cfg, px=px)
    if icon == suffix:
        return f"{number} {suffix}".strip()
    return f"{number}{space}{icon}"


def with_suffix_text(amount, currency="platinum"):
    """Текстовый вариант без иконок: «120 p» / «15 дк»."""
    return format_money(amount, currency=currency, cfg={"currency_icons": False},
                        rich=False)


def total_html(total, cfg=None, prefix="Итого: ", currency="platinum"):
    """«Итого: 120» + иконка (для rich-text QLabel)."""
    try:
        value = int(round(float(total)))
    except Exception:
        value = 0
    return f"{prefix}{value}&#160;" + html(currency, cfg)


def clear_cache():
    """Сбросить кэш путей/картинок (тесты, смена ассетов)."""
    _pixmap_cache.clear()
    _path_cache.clear()


__all__ = ["CURRENCIES", "SUFFIX", "TITLES", "SIZES", "INLINE_PX",
           "asset_dir", "choose_size", "icon_path", "pixmap",
           "currency_pixmap", "icon_label",
           "enabled", "html", "format_money", "with_suffix_text",
           "total_html", "clear_cache"]
