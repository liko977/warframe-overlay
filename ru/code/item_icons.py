# -*- coding: utf-8 -*-
"""Иконки предметов Warframe (WFCD/CDN) — ленивая загрузка и кэш.

Сеть никогда не блокирует GUI: индекс WFCD обновляется в фоне, картинки
качаются в пуле из 3 потоков и подставляются через Qt-сигнал. Без сети
используются дисковой кэш и аккуратный плейсхолдер.

1.0.0: если рядом лежит манифест пакета ``icon_pack.json`` (его пишет
``icon_pack.py`` после однократного скачивания всех иконок), провайдер
работает полностью офлайн: картинки берутся только с диска, а для
отсутствующего файла сразу отдаётся плейсхолдер — ни одного запроса в сеть.
"""
import json
import os
import re
import threading
import time
import urllib.request
from collections import OrderedDict

WFCD = ("https://raw.githubusercontent.com/WFCD/warframe-items/master/"
        "data/json/{name}.json")
CDN = "https://cdn.warframestat.us/img/{image}"
MARKET_ASSETS = "https://warframe.market/static/assets/"
CATEGORIES = ("Warframes", "Primary", "Secondary", "Melee", "Mods", "Misc",
              "Resources", "Gear", "Relics", "Pets", "Archwing", "Arch-Gun",
              "Arch-Melee", "Sentinels", "SentinelWeapons", "Skins")
INDEX_TTL = 7 * 24 * 3600
MAX_MEMORY = 200
MAX_WORKERS = 3
PACK_NAME = "icon_pack.json"          # манифест пакета иконок (icon_pack.py)
BUNDLE_NAME = "icons_pack.zip"        # архив иконок в комплекте сборки
_PARTS = ("chassis", "neuroptics", "systems", "barrel", "receiver", "stock",
          "blade", "blades", "handle", "gauntlet", "link", "upper limb",
          "lower limb", "string", "blueprint", "prime", "set", "carapace",
          "cerebrum", "harrow chassis")
_BLUEPRINT_RE = re.compile(r"(?:^|\b)(?:blueprint|чертеж|чертёж)(?:\b|$)",
                           re.IGNORECASE)
_ICON_RE = re.compile(r"\.(png|jpg|jpeg|webp)$", re.IGNORECASE)


def normalize_name(text):
    t = str(text or "").lower().replace("ё", "е")
    t = re.sub(r"[^\w]+", " ", t, flags=re.UNICODE)
    return re.sub(r"\s+", " ", t).strip()


def is_blueprint(name="", slug=""):
    return bool(_BLUEPRINT_RE.search(str(name or ""))
                or _BLUEPRINT_RE.search(str(slug or "").replace("_", " ")))


#: (суффикс slug, бейдж типа части); порядок важен: длинные раньше.
_PART_BADGES = (
    ("chassis", "К"), ("neuroptics", "Н"), ("systems", "С"),
    ("barrel", "Ст"), ("receiver", "Пр"), ("stock", "Пк"),
    ("blades", "Кл"), ("blade", "Кл"), ("handle", "Рк"),
    ("link", "Св"), ("gauntlet", "Зах"), ("string", "Тет"),
    ("upper_limb", "ВЧ"), ("lower_limb", "НЧ"), ("band", "Зас"),
    ("buckle", "Пряж"), ("set", "Км"),
)


#: (ключевые слова RU, тип, бейдж, цвет подложки) — t62.
PART_TYPES = (
    (("верхнее плечо",), "Верхнее плечо", "ВП", "#6FA8C8"),
    (("нижнее плечо",), "Нижнее плечо", "НП", "#6FA8C8"),
    (("нейрооптика", "шлем"), "Нейрооптика", "Н", "#6FA8C8"),
    (("чертеж", "чертёж"), "Чертёж", "Ч", "#E6E9EE"),
    (("каркас",), "Каркас", "К", "#4A7CA6"),
    (("система",), "Система", "С", "#4CA870"),
    (("ствол",), "Ствол", "Ст", "#7F93A8"),
    (("приемник", "приёмник"), "Приёмник", "Пр", "#B57F4E"),
    (("приклад",), "Приклад", "Пк", "#8B6A4E"),
    (("клинок", "лезвие"), "Клинок", "Кл", "#A0524E"),
    (("рукоять",), "Рукоять", "Рк", "#C8A660"),
    (("эфес",), "Эфес", "Эф", "#C8A660"),
    (("связь",), "Связь", "Св", "#5B9AA8"),
    (("тетива",), "Тетива", "Тт", "#5E82A8"),
    (("застежка", "застёжка", "пряжка"), "Застёжка", "Зас",
     "#8E7CB0"),
    (("ошейник",), "Ошейник", "Ош", "#8E7CB0"),
    (("упряжь",), "Упряжь", "Упр", "#7F93A8"),
    (("крылья",), "Крылья", "Кр", "#5E82A8"),
)
PART_UNKNOWN_COLOR = "#4A5462"
_SET_WORDS = ("комплект", "набор")
_PART_EN = {
    "chassis": ("Каркас", "К", "#4A7CA6"),
    "neuroptics": ("Нейрооптика", "Н", "#6FA8C8"),
    "systems": ("Система", "С", "#4CA870"),
    "barrel": ("Ствол", "Ст", "#7F93A8"),
    "receiver": ("Приёмник", "Пр", "#B57F4E"),
    "stock": ("Приклад", "Пк", "#8B6A4E"),
    "blade": ("Клинок", "Кл", "#A0524E"),
    "blades": ("Клинок", "Кл", "#A0524E"),
    "handle": ("Рукоять", "Рк", "#C8A660"),
    "link": ("Связь", "Св", "#5B9AA8"),
    "string": ("Тетива", "Тт", "#5E82A8"),
    "upper_limb": ("Верхнее плечо", "ВП", "#6FA8C8"),
    "lower_limb": ("Нижнее плечо", "НП", "#6FA8C8"),
    "band": ("Застёжка", "Зас", "#8E7CB0"),
    "buckle": ("Застёжка", "Зас", "#8E7CB0"),
    "guard": ("Эфес", "Эф", "#C8A660"),
}
_BADGE_CACHE = {}


def part_style(value="", name=""):
    """Тип части: dict(type, badge, color, caption, is_part, is_set)."""
    text = str(value or "").strip()
    title = str(name or "").strip()
    low_title = (title or text).lower().replace("ё", "е")
    low = text.lower().replace("ё", "е").replace("-", "_")
    if low.endswith("_kubrow_collar") or "кубрау" in low_title:
        return {"type": "", "badge": "", "color": "", "caption": "",
                "is_part": False, "is_set": False}
    if any(word in low_title for word in _SET_WORDS) or low.endswith("_set"):
        return {"type": "Комплект", "badge": "", "color": "#4A7CA6",
                "caption": "", "is_part": False, "is_set": True}
    # RU-имя (каталог) — приоритет
    for words, type_ru, badge, color in PART_TYPES:
        for word in words:
            if word.replace("ё", "е") in low_title:
                return {"type": type_ru, "badge": badge, "color": color,
                        "caption": type_ru, "is_part": True, "is_set": False}
    # EN-слово в названии (Handle/Chassis/Barrel…)
    name_tokens = [tok.strip(".,:;").lower() for tok in
                   str(title or "").replace("-", " ").split()]
    for token in reversed(name_tokens):
        if token in _PART_EN:
            type_ru, badge, color = _PART_EN[token]
            return {"type": type_ru, "badge": badge, "color": color,
                    "caption": type_ru, "is_part": True, "is_set": False}
    if "blueprint" in name_tokens:
        return {"type": "Чертёж", "badge": "Ч", "color": "#E6E9EE",
                "caption": "Чертёж", "is_part": True, "is_set": False}
    # прямой тип (для тестов/вызовов part_badge("Рукоять"))
    for words, type_ru, badge, color in PART_TYPES:
        if low and any(low == word.replace("ё", "е") for word in words):
            return {"type": type_ru, "badge": badge, "color": color,
                    "caption": type_ru, "is_part": True, "is_set": False}
    # EN-slug fallback (в т.ч. chassis_blueprint)
    base_low = low[:-len("_blueprint")] if low.endswith("_blueprint") else low
    allowed = (("chassis", "neuroptics", "systems")
               if low.endswith("_blueprint") else tuple(_PART_EN))
    for suffix in allowed:
        if suffix not in _PART_EN:
            continue
        type_ru, badge, color = _PART_EN[suffix]
        if base_low.endswith("_" + suffix) or base_low == suffix:
            return {"type": type_ru, "badge": badge, "color": color,
                    "caption": type_ru, "is_part": True, "is_set": False}
    if low.endswith("_blueprint"):
        return {"type": "Чертёж", "badge": "Ч", "color": "#E6E9EE",
                "caption": "Чертёж", "is_part": True, "is_set": False}
    if text and not title and "_" not in text and " " not in text:
        # неизвестный переданный тип: первые две буквы
        letters = "".join(ch for ch in text if ch.isalpha())[:2]
        return {"type": text, "badge": letters, "color": PART_UNKNOWN_COLOR,
                "caption": text, "is_part": True, "is_set": False}
    return {"type": "", "badge": "", "color": "", "caption": "",
            "is_part": False, "is_set": False}


def _contrast(color1, color2):
    def lum(hex_color):
        vals = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        vals = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
                for v in vals]
        return 0.2126 * vals[0] + 0.7152 * vals[1] + 0.0722 * vals[2]
    l1, l2 = sorted((lum(color1), lum(color2)), reverse=True)
    return (l1 + 0.05) / (l2 + 0.05)


def badge_text_color(background):
    dark = _contrast(background, "#000000")
    light = _contrast(background, "#FFFFFF")
    return "#000000" if dark >= light else "#FFFFFF"


def part_badge_pixmap(value="", name="", width=28, height=16, size=None):
    if size in ("small", "mini"):
        width, height = part_badge_size(value, name, mini=(size == "mini"))
    elif size is None:
        width, height = part_badge_size(value, name)
    """QPixmap бейджа: тип по slug/name/типу; кэш по (тип, размер)."""
    style = part_style(value, name)
    badge = part_badge_text(value, name)
    if not badge:
        return None
    color = style.get("color") or PART_UNKNOWN_COLOR
    key = (badge, color, int(width), int(height))
    cached = _BADGE_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        from PyQt5.QtCore import QRectF, Qt
        from PyQt5.QtGui import QColor, QFont, QPainter, QPixmap
        pix = QPixmap(int(width), int(height))
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            bg = QColor(color)
            bg.setAlpha(220)
            painter.setBrush(bg)
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(QRectF(0, 0, width, height), 4, 4)
            font = QFont("Segoe UI")
            font.setPixelSize(11 if width >= 34 else 10)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor(badge_text_color(color)))
            painter.drawText(QRectF(0, 0, width, height), Qt.AlignCenter,
                             badge)
        finally:
            painter.end()
        _BADGE_CACHE[key] = pix
        return pix
    except Exception:
        return None


def part_word(value="", name=""):
    """Слово типа из названия («Лезвие», «Рукоять»); иначе тип."""
    style = part_style(value, name)
    if not style.get("is_part"):
        return ""
    title = str(name or "").strip()
    if ":" in title:
        word = title.rsplit(":", 1)[1].strip()
        if word:
            return word
    tokens = [tok for tok in title.replace("-", " ").split() if tok]
    known = set()
    for words, _type, _badge, _color in PART_TYPES:
        known.update(w.replace("ё", "е") for w in words)
    if tokens:
        last = tokens[-1].strip(".,").lower().replace("ё", "е")
        if last in known:
            return tokens[-1].strip(".,")
    return style.get("type") or ""


def part_badge_text(value="", name=""):
    """t165: полное слово части, без сокращений."""
    return part_word(value, name)


def part_badge_size(value="", name="", mini=False):
    """Размер бейджа: 3 буквы 28×14 (мини 24×12), 2 — 22×13 (20×12)."""
    text = part_badge_text(value, name)
    width = max(28, min(96, 7 * len(text) + 8))
    return (max(24, int(width * 0.86)), 12) if mini else (width, 16)


def part_chip_html(value="", name=""):
    """HTML-чип типа (рядом с названием). None у целых/сетов."""
    style = part_style(value, name)
    if not style.get("is_part"):
        return ""
    return (f"<span style='background-color:#262F40;"
            f" color:{style['color']}; font-size:9px; font-weight:700;"
            f" border-radius:8px; padding:2px 6px;'>"
            f"{part_word(value, name)}</span>")


def part_chip_label(value="", name="", parent=None):
    """QLabel-чип: фон #262F40, текст цвета типа, radius 8, 9px bold."""
    try:
        from PyQt5.QtCore import Qt
        from PyQt5.QtWidgets import QLabel, QSizePolicy
        style = part_style(value, name)
        label = QLabel(parent)
        if not style.get("is_part"):
            label.hide()
            label.setParent(None)
            return label
        text = part_word(value, name) or style["type"]
        label.setText(text)
        label.setStyleSheet(
            f"background:#262F40; color:{style['color']};"
            " font-size:9px; font-weight:700; border-radius:8px;"
            " padding:2px 6px;")
        label.setToolTip(text)
        label.setAlignment(Qt.AlignCenter)
        label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        label.setFixedHeight(18)
        width = label.fontMetrics().horizontalAdvance(text) + 16
        label.setMaximumWidth(max(24, width))
        label.adjustSize()
        return label
    except Exception:
        return None


def part_caption(value="", name=""):
    style = part_style(value, name)
    return style.get("caption") if style.get("is_part") else ""


def part_info(slug="", name=""):
    """Разбирает slug на часть: ``(is_part, badge, parent_slug)``.

    Любой ``*_blueprint`` — «Ч», кроме chassis/neuroptics/systems (К/Н/С);
    целые предметы (в т.ч. kavasa_prime_kubrow_collar) — без бейджа.
    """
    text = str(slug or "").strip().lower().replace("-", "_").replace(" ", "_")
    # имя тоже может быть источником, если slug пуст
    if not text:
        text = normalize_name(name).replace(" ", "_")
    if text.endswith("_kubrow_collar"):
        return False, "", text
    if text.endswith("_chassis_blueprint"):
        return True, "К", text[:-len("chassis_blueprint")].rstrip("_")
    if text.endswith("_neuroptics_blueprint"):
        return True, "Н", text[:-len("neuroptics_blueprint")].rstrip("_")
    if text.endswith("_systems_blueprint"):
        return True, "С", text[:-len("systems_blueprint")].rstrip("_")
    if text.endswith("_blueprint"):
        return True, "Ч", text[:-len("blueprint")].rstrip("_")
    for suffix, badge in _PART_BADGES:
        if text.endswith("_" + suffix):
            parent = text[:-(len(suffix) + 1)].rstrip("_")
            return True, badge, parent
    return False, "", text


def part_badge(slug="", name="", type_text=None):
    """Бейдж части: по русскому типу/названию, иначе по slug."""
    if type_text is not None:
        return part_style(str(type_text)).get("badge") or ""
    style = part_style(slug, name)
    if style.get("is_part"):
        return style.get("badge") or ""
    return part_info(slug, name)[1]


def visual_config(item):
    """Конфигурация визуала: parent+badge; для тестов дедупликации частей."""
    item = dict(item or {})
    slug = str(item.get("slug") or "")
    name = str(item.get("name_en") or item.get("name") or "")
    is_part, badge, parent = part_info(slug, name)
    return {"parent": parent, "badge": badge, "part": is_part,
            "slug": slug}


def image_name(value, fallback=""):
    """imageName из WFCD → имя файла CDN."""
    value = str(value or "").strip()
    if not value:
        value = str(fallback or "").strip()
    if not value:
        return ""
    value = value.replace("\\", "/").rsplit("/", 1)[-1]
    if not _ICON_RE.search(value):
        value += ".png"
    return value


def base_candidates(name, slug="", game_ref=""):
    """Кандидаты en-имён: точное → без Blueprint → без части → база."""
    values = [str(name or ""), str(slug or "").replace("_", " ")]
    # gameRef: /Lotus/Powersuits/Lavos/LavosPrime → LavosPrime
    ref = str(game_ref or "").replace("\\", "/").rsplit("/", 1)[-1]
    if ref:
        ref = re.sub(r"(Blueprint|Chassis|Neuroptics|Systems|Barrel|"
                     r"Receiver|Stock|Blade|Handle|Gauntlet|Link)$", "",
                     ref, flags=re.IGNORECASE)
        ref = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", ref)
        values.append(ref)
    out = []
    for value in values:
        value = re.sub(r"\(([^)]*)\)", r"\1", str(value or ""))
        value = value.replace(":", " ")
        value = re.sub(r"\s+", " ", value).strip(" -_")
        if not value:
            continue
        norm = normalize_name(value)
        for candidate in (norm, compact_name(value)):
            if candidate and candidate not in out:
                out.append(candidate)
        # Blueprint / (Blueprint)
        no_bp = re.sub(r"\b(?:blueprint|чертеж|чертёж)\b", " ", value,
                       flags=re.IGNORECASE)
        no_bp = normalize_name(no_bp)
        if no_bp and no_bp not in out:
            out.append(no_bp)
        # части: Chassis/Neuroptics/... + Prime; после каждого снятия — кандидат
        part = no_bp
        for token in _PARTS:
            part = re.sub(rf"\b{re.escape(token)}\b", " ", part)
            candidate = normalize_name(part)
            if candidate and candidate not in out:
                out.append(candidate)
    return out


def build_index(records):
    """records: {category: [ {name,imageName}, ... ]} → {norm en: imageName}."""
    index = {}
    for items in (records or {}).values():
        for item in items or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or ""
            image = image_name(item.get("imageName"), name)
            if not image:
                continue
            keys = {normalize_name(name)}
            compact = compact_name(name)
            if compact:
                keys.add(compact)
            # имя файла тоже полезно как alias (TwinKeresPrime.png)
            keys.add(normalize_name(image.rsplit(".", 1)[0]))
            for key in keys:
                if key and key not in index:
                    index[key] = image
    return index


def compact_name(text):
    """«Twin Keres Prime» → «twinkeresprime» (для CamelCase-вариантов)."""
    return re.sub(r"[^a-z0-9]+", "", normalize_name(text).replace("ё", "е"))


def camel_name(text):
    words = re.split(r"[^A-Za-z0-9]+", str(text or ""))
    return "".join(word[:1].upper() + word[1:] for word in words if word)


class _Bridge:
    """Ленивый Qt-мостик: callback всегда в GUI-потоке."""

    def __init__(self, callback, provider=None, size=32):
        self.callback = callback
        self.provider = provider
        self.size = int(size)
        try:
            from PyQt5.QtCore import QObject, pyqtSignal

            class _Q(QObject):
                ready = pyqtSignal(str, object)
            self.q = _Q()
            self.q.ready.connect(self._deliver)
        except Exception:
            self.q = None

    def _deliver(self, key, value):
        pix = value
        try:
            from PyQt5.QtGui import QImage, QPixmap
            if value is None and self.provider is not None:
                pix = self.provider.placeholder(self.size)
            elif isinstance(value, QImage):
                pix = QPixmap.fromImage(value)
                if self.provider is not None:
                    self.provider._remember(key, pix)
        except Exception:
            pix = None
        try:
            self.callback(key, pix)
        except Exception:
            pass
        try:
            if self.provider is not None:
                self.provider._bridges.discard(self)
        except Exception:
            pass

    def emit(self, key, value):
        if self.q is not None:
            try:
                self.q.ready.emit(key, value)
                return
            except Exception:
                pass
        try:
            self.callback(key, value)
        except Exception:
            pass


_BUNDLE_CACHE = {"tried": False, "path": "", "names": {}, "pix": {}}
_SET_PIX_CACHE = {}
_LIB_MISSING = set()
_LIB_MISSING_LOGGED = 0

def log_missing_icon(slug):
    """Одна строка на slug, максимум 50 за сеанс."""
    global _LIB_MISSING_LOGGED
    slug = str(slug or "").strip()
    if not slug or slug in _LIB_MISSING or _LIB_MISSING_LOGGED >= 50:
        return False
    _LIB_MISSING.add(slug)
    _LIB_MISSING_LOGGED += 1
    try:
        import warframe as _wf
        _wf.log(f"Иконка не найдена в библиотеке: {slug}")
    except Exception:
        pass
    return True



def bundle_path():
    """Путь к assets/icons_pack.zip (исходники / _internal / _MEIPASS)."""
    roots = []
    try:
        import sys
        if getattr(sys, "frozen", False):
            roots.append(os.path.dirname(os.path.abspath(sys.executable)))
            roots.append(os.path.dirname(os.path.abspath(sys.executable)))
            roots.append(getattr(sys, "_MEIPASS", ""))
    except Exception:
        pass
    env = os.environ.get("WF_APP_DIR")
    if env:
        roots.append(env)
    here = os.path.dirname(os.path.abspath(__file__))
    roots += [here, os.path.dirname(here)]
    for root in roots:
        if not root:
            continue
        for rel in (("assets", BUNDLE_NAME), ("_internal", "assets", BUNDLE_NAME)):
            path = os.path.join(root, *rel)
            if os.path.isfile(path):
                return path
    return ""


def bundle_manifest():
    """Манифест из архива (icon_pack.json) или {}."""
    path = bundle_path()
    if not path:
        return {}
    try:
        import zipfile
        with zipfile.ZipFile(path) as archive:
            try:
                data = archive.read(PACK_NAME)
            except KeyError:
                return {}
        return dict(json.loads(data.decode("utf-8")))
    except Exception:
        return {}


def bundle_count():
    return int(bundle_manifest().get("count") or 0)


def ensure_bundle(base_dir, logger=None):
    """Распаковать bundle в ``base/icons`` при первом старте."""
    path = bundle_path()
    base_dir = str(base_dir or "")
    if not path or not base_dir:
        return {"ok": False, "extracted": False, "count": 0}
    icons_dir = os.path.join(base_dir, "icons")
    marker = os.path.join(base_dir, ".icons_bundle.json")
    manifest = bundle_manifest()
    expected = int(manifest.get("count") or 0)
    try:
        size = os.path.getsize(path)
        mtime = int(os.path.getmtime(path))
    except Exception:
        size = 0
        mtime = 0
    try:
        marker_data = json.load(open(marker, encoding="utf-8"))
    except Exception:
        marker_data = {}
    have = 0
    try:
        have = len([n for n in os.listdir(icons_dir)
                    if n.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))])
    except Exception:
        have = 0
    if (marker_data.get("zip_size") == size
            and marker_data.get("zip_mtime") == mtime
            and have >= expected and expected > 0):
        return {"ok": True, "extracted": False, "count": have}
    if expected > 0 and have >= expected:
        try:
            with open(marker, "w", encoding="utf-8") as handle:
                json.dump({"zip_size": size, "zip_mtime": mtime,
                           "count": have}, handle)
        except Exception:
            pass
        return {"ok": True, "extracted": False, "count": have}
    os.makedirs(icons_dir, exist_ok=True)
    written = 0
    try:
        import zipfile
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name == PACK_NAME or name.endswith("/"):
                    continue
                target = os.path.join(icons_dir, os.path.basename(name))
                with archive.open(name) as source, open(target, "wb") as dest:
                    dest.write(source.read())
                written += 1
        if manifest:
            with open(os.path.join(base_dir, PACK_NAME), "w",
                      encoding="utf-8") as handle:
                json.dump(manifest, handle, ensure_ascii=False)
        with open(marker, "w", encoding="utf-8") as handle:
            json.dump({"zip_size": size, "zip_mtime": mtime,
                       "count": written}, handle)
    except Exception as exc:
        return {"ok": False, "extracted": False, "count": have,
                "error": type(exc).__name__}
    if callable(logger):
        logger(f"Иконки: из комплекта {written} (архив), докачка выключена")
    return {"ok": True, "extracted": True, "count": written}


def bundle_pixmap(key):
    """QPixmap из bundle по slug (без сети); None, если нет."""
    path = bundle_path()
    if not path:
        return None
    slug = str(key or "")
    if not slug:
        return None
    if _BUNDLE_CACHE.get("path") != path:
        _BUNDLE_CACHE.update({"path": path, "names": {}, "pix": {}})
        try:
            import zipfile
            with zipfile.ZipFile(path) as archive:
                for name in archive.namelist():
                    stem = os.path.splitext(os.path.basename(name))[0]
                    _BUNDLE_CACHE["names"].setdefault(stem, name)
        except Exception:
            return None
    name = ""
    for candidate in library_candidates(slug):
        name = _BUNDLE_CACHE["names"].get(candidate)
        if name:
            slug = candidate
            break
    if not name:
        return None
    pix = _BUNDLE_CACHE["pix"].get(slug)
    if pix is not None:
        return pix
    try:
        import zipfile
        from PyQt5.QtGui import QPixmap
        with zipfile.ZipFile(path) as archive:
            data = archive.read(name)
        pix = QPixmap()
        if pix.loadFromData(data):
            _BUNDLE_CACHE["pix"][slug] = pix
            return pix
    except Exception:
        return None
    return None


class IconPreloader:
    """Фоновая предзагрузка иконок строк через кэш провайдера.

    ``start(keys, first, last)`` загружает ``keys[max(0,first-5):
    last+6]`` порциями по 20 с паузой 35 мс; повторный start отменяет
    прошлое поколение (быстрый скролл).
    """

    def __init__(self, provider, logger=None, parent=None):
        self.provider = provider
        self.logger = logger
        self.generation = 0
        self.loaded = 0
        self._queue = []
        self._timer = None
        self._logged = False
        self._parent = parent

    def _log(self, message):
        if callable(self.logger):
            try:
                self.logger(message)
            except Exception:
                pass

    def start(self, keys, first=0, last=9):
        self.cancel()
        keys = [str(key) for key in (keys or []) if str(key).strip()]
        first = max(0, int(first) - 5)
        last = min(len(keys) - 1, int(last) + 5)
        window = keys[first:last + 1]
        fresh = []
        for key in window:
            try:
                if self.provider.get_cached(key) is None:
                    fresh.append(key)
            except Exception:
                fresh.append(key)
        self._queue = fresh
        self.loaded = 0
        if not fresh:
            self._log(f"Иконки: предзагрузка 0 (строки {first}–{last})")
            return 0
        if not self._logged:
            self._logged = True
            self._log(f"Иконки: предзагрузка {len(fresh)} "
                      f"(строки {first}–{last})")
        self._start_timer()
        return len(fresh)

    def _start_timer(self):
        try:
            from PyQt5.QtCore import QTimer
        except Exception:
            return
        timer = QTimer()
        timer.setSingleShot(False)
        timer.setInterval(35)
        timer.timeout.connect(self._tick)
        self._timer = timer
        timer.start()

    def _tick(self):
        if not self._queue:
            self.cancel()
            return self.loaded
        generation = self.generation
        batch = self._queue[:20]
        self._queue = self._queue[20:]
        for key in batch:
            if generation != self.generation:
                return self.loaded
            if self.provider.get_cached(key) is not None:
                continue
            pix = None
            try:
                path = self.provider._find_disk(key)
                if path:
                    pix = self.provider._load_pixmap(path)
                if pix is None:
                    pix = bundle_pixmap(key)
            except Exception:
                pix = None
            if pix is not None:
                try:
                    self.provider._remember(key, pix)
                except Exception:
                    pass
                self.loaded += 1
        if not self._queue:
            self.cancel()
        return self.loaded

    def cancel(self):
        self.generation += 1
        self._queue = []
        if self._timer is not None:
            try:
                self._timer.stop()
                self._timer.deleteLater()
            except Exception:
                pass
            self._timer = None
        return self.generation


def library_status(base_dir=None):
    """Файлы библиотеки + bundle для статуса/лога."""
    base = str(base_dir or "")
    files = 0
    size = 0
    icons_dir = os.path.join(base, "icons") if base else ""
    if icons_dir and os.path.isdir(icons_dir):
        for name in os.listdir(icons_dir):
            path = os.path.join(icons_dir, name)
            try:
                if os.path.isfile(path) and _ICON_RE.search(name):
                    files += 1
                    size += os.path.getsize(path)
            except Exception:
                pass
    count = bundle_count()
    path = bundle_path()
    if not files and count:
        files = count
        try:
            size = os.path.getsize(path)
        except Exception:
            size = 0
    return {"files": files, "bytes": size, "have_pack": bool(count),
            "bundle": bool(path), "bundle_count": count}


def library_candidates(slug):
    """Кандидаты файла: точный → без *_blueprint → родитель/сет."""
    key = str(slug or "").strip()
    if not key:
        return []
    out = [key]
    if key.endswith("_blueprint"):
        out.append(key[:-len("_blueprint")])
    try:
        is_part, _badge, parent = part_info(key)
    except Exception:
        is_part, parent = False, ""
    if is_part and parent:
        out += [parent + "_set", parent]
    # без дублей
    seen = set()
    return [x for x in out if x and not (x in seen or seen.add(x))]


def is_set_item(slug="", name=""):
    text = str(slug or "").lower()
    title = str(name or "").lower()
    return (text.endswith("_set") or "комплект" in title
            or "набор" in title or text.endswith("_bundle"))


def decorate_set_pixmap(pixmap, size=None, label=True):
    """Дорисовать рамку #4A7CA6 и плашку «КОМПЛЕКТ» (кэш в памяти)."""
    if pixmap is None or pixmap.isNull():
        return pixmap
    key = (pixmap.cacheKey(), int(size or 0), bool(label))
    cached = _SET_PIX_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        from PyQt5.QtCore import Qt
        from PyQt5.QtGui import QColor, QFont, QPainter, QPixmap
        out = QPixmap(pixmap)
        painter = QPainter(out)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            pen = QColor("#4A7CA6")
            pen.setAlpha(220)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(1, 1, out.width() - 3, out.height() - 3)
            if label and out.width() >= 48:
                text = "КОМПЛЕКТ"
                font = QFont("Segoe UI")
                font.setPixelSize(max(7, min(10, out.width() // 12)))
                font.setBold(True)
                painter.setFont(font)
                metrics = painter.fontMetrics()
                tw = metrics.horizontalAdvance(text) + 6
                th = metrics.height() + 2
                rect_x = max(0, out.width() - tw - 2)
                rect_y = max(0, out.height() - th - 2)
                painter.fillRect(rect_x, rect_y, tw, th,
                                 QColor(13, 17, 23, 200))
                painter.setPen(QColor("#E6E9EE"))
                painter.drawText(rect_x + 3, rect_y + th - 4, text)
        finally:
            painter.end()
        _SET_PIX_CACHE[key] = out
        return out
    except Exception:
        return pixmap


class ItemIconProvider:
    def __init__(self, base_dir, fetcher=None, loader=None, enabled=True,
                 index_ttl=INDEX_TTL, memory_limit=MAX_MEMORY,
                 auto_download=False):
        self.base_dir = str(base_dir)
        self.icons_dir = os.path.join(self.base_dir, "icons")
        self.index_path = os.path.join(self.base_dir, "icon_index.json")
        self.pack_path = os.path.join(self.base_dir, PACK_NAME)
        self.fetcher = fetcher or self._fetch_url
        self.loader = loader or self._fetch_url
        self.enabled = bool(enabled)
        self.auto_download = bool(auto_download)
        self.index_ttl = int(index_ttl)
        self.memory_limit = max(1, int(memory_limit))
        self._index = {}
        self._missing = set()
        self._misses = 0
        self._refreshed_once = False
        self._lock = threading.Lock()
        self._pixmaps = OrderedDict()
        self._attempted = set()
        self._executor = None
        self._index_threads = []
        self._bridges = set()
        # 1.0.0: манифест пакета иконок → работаем только с диска, без сети
        self._pack = {}
        self.pack_ready = False
        self._disk_lock = threading.Lock()
        self._disk_map = None
        self._disk_stamp = None
        try:
            os.makedirs(self.icons_dir, exist_ok=True)
        except Exception:
            pass
        self._load_index_sync()
        self._load_pack_sync()

    # ── пакет иконок (1.0.0) ──────────────────────────────────────────
    def _load_pack_sync(self):
        """Прочитать ``icon_pack.json``: есть пак — сеть не нужна вообще."""
        try:
            with open(self.pack_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            self._pack = dict(data) if isinstance(data, dict) else {}
        except Exception:
            self._pack = {}
        self.pack_ready = bool(self._pack) and bool(
            self._pack.get("updated_at"))
        return self._pack

    def reload_pack(self):
        """Перечитать манифест (после скачивания/очистки пака) и кэш диска."""
        result = self._load_pack_sync()
        self._invalidate_disk()
        return result

    def has_pack(self):
        return bool(self.pack_ready)

    def pack_info(self):
        info = dict(self._pack or {})
        info["ready"] = bool(self.pack_ready)
        return info

    # ── список файлов на диске (кэш по mtime папки) ───────────────────
    def _disk_files(self, force=False):
        if not force:
            with self._disk_lock:
                cached, stamp = self._disk_map, self._disk_stamp
            if cached is not None:
                try:
                    now_stamp = os.stat(self.icons_dir).st_mtime_ns
                except Exception:
                    now_stamp = None
                if now_stamp is not None and now_stamp == stamp:
                    return cached
        # mtime снимаем ДО листинга: запись во время листинга оставит
        # штамп старым, и следующий вызов пересоберёт карту.
        try:
            stamp = os.stat(self.icons_dir).st_mtime_ns
        except Exception:
            stamp = None
        mapping = {}
        try:
            for name in os.listdir(self.icons_dir):
                if _ICON_RE.search(name):
                    mapping.setdefault(name.rsplit(".", 1)[0], name)
        except Exception:
            pass
        with self._disk_lock:
            self._disk_map = mapping
            self._disk_stamp = stamp
        return mapping

    def _invalidate_disk(self):
        with self._disk_lock:
            self._disk_map = None
            self._disk_stamp = None

    # ── индекс ────────────────────────────────────────────────────────
    @staticmethod
    def _fetch_url(url, timeout=8):
        request = urllib.request.Request(url, headers={
            "User-Agent": "VoidFrameRU/1.0.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()

    def _load_index_sync(self):
        try:
            with open(self.index_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            self._index = dict(data.get("index") or {})
            self._missing = set(data.get("missing") or [])
        except Exception:
            self._index = {}
            self._missing = set()
        return self._index

    def index_age(self):
        try:
            with open(self.index_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return time.time() - float(data.get("updated") or 0)
        except Exception:
            return self.index_ttl + 1

    def refresh_index(self, force=False):
        """Синхронно обновить индекс (обычно из фонового потока)."""
        if not self.enabled:
            return self._index
        if not force and self._index and self.index_age() <= self.index_ttl:
            return self._index
        records = {}
        for category in CATEGORIES:
            try:
                raw = self.fetcher(WFCD.format(name=category))
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", "replace")
                data = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(data, list):
                    records[category] = data
            except Exception:
                continue
        if not records:
            return self._index
        index = build_index(records)
        if index:
            self._index = index
            try:
                with open(self.index_path, "w", encoding="utf-8") as handle:
                    json.dump({"updated": time.time(), "index": index,
                               "missing": sorted(self._missing)},
                              handle, ensure_ascii=False)
            except Exception:
                pass
        return self._index

    def refresh_index_async(self):
        if not self.enabled:
            return False
        # пак иконок уже на диске: WFCD-индекс не нужен, сеть не трогаем
        if self.pack_ready:
            return False
        if self._index and self.index_age() <= self.index_ttl:
            return False
        thread = threading.Thread(target=self.refresh_index, daemon=True,
                                  name="wf-icon-index")
        thread.start()
        self._index_threads.append(thread)
        return True

    def resolve(self, name="", slug="", en_name="", game_ref=""):
        for candidate in base_candidates(en_name or name, slug, game_ref):
            if candidate in self._index:
                return self._index[candidate]
        # индекс не знает имя — пробуем CamelCase-вариант, CDN ответит 404
        # на отсутствующие, а provider запомнит отрицательный кэш.
        for value in (en_name or name, slug.replace("_", " "),
                      game_ref.rsplit("/", 1)[-1]):
            value = str(value or "").strip()
            if not value:
                continue
            camel = camel_name(value)
            if camel:
                return camel + ".png"
        return ""

    def _save_missing(self, image):
        try:
            with open(self.index_path, "w", encoding="utf-8") as handle:
                json.dump({"updated": time.time(), "index": self._index,
                           "missing": sorted(self._missing)},
                          handle, ensure_ascii=False)
        except Exception:
            pass

    def _mark_missing(self, key, image):
        with self._lock:
            self._missing.add(str(image or ""))
            self._missing.add(str(key or ""))
            self._misses += 1
            refresh = (self._misses >= 3 and not self._refreshed_once)
            if refresh:
                self._refreshed_once = True
        self._save_missing(image)
        if refresh:
            self.refresh_index_async()

    # ── картинки ──────────────────────────────────────────────────────
    def _disk_path(self, key, ext=".png"):
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(key))[:120]
        if not ext.startswith("."):
            ext = "." + ext
        return os.path.join(self.icons_dir, safe + ext)

    def _find_disk(self, key):
        for candidate in library_candidates(key):
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(candidate))[:120]
            name = self._disk_files().get(safe)
            if name:
                return os.path.join(self.icons_dir, name)
        return ""

    def _make_placeholder(self, size=32):
        try:
            from PyQt5.QtWidgets import QApplication
            if QApplication.instance() is None:
                return None
            from PyQt5.QtCore import Qt
            from PyQt5.QtGui import QColor, QFont, QPainter, QPen, QPixmap
            pix = QPixmap(int(size), int(size))
            pix.fill(QColor("#17212e"))
            painter = QPainter(pix)
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setBrush(QColor("#17212e"))
            painter.setPen(QPen(QColor("#24344a"), 1))
            painter.drawRoundedRect(1, 1, size - 2, size - 2, 3, 3)
            painter.setPen(QColor("#93a1b5"))
            font = QFont("Segoe UI")
            font.setPixelSize(max(9, int(size * 0.42)))
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(pix.rect(), Qt.AlignCenter, "?")
            painter.end()
            return pix
        except Exception:
            return None

    def placeholder(self, size=32):
        key = f"__placeholder_{int(size)}"
        cached = self._pixmaps.get(key)
        if cached is not None:
            return cached
        pix = self._make_placeholder(size)
        if pix is not None:
            self._remember(key, pix)
        return pix

    def _remember(self, key, pixmap):
        with self._lock:
            self._pixmaps.pop(key, None)
            self._pixmaps[key] = pixmap
            while len(self._pixmaps) > self.memory_limit:
                self._pixmaps.popitem(last=False)

    def memory_size(self):
        return len(self._pixmaps)

    def has_index(self):
        return bool(self._index)

    def get_cached(self, key):
        with self._lock:
            pix = self._pixmaps.get(key)
            if pix is not None:
                self._pixmaps.move_to_end(key)
            return pix

    def request(self, key, image_name_value="", callback=None, size=32):
        """Загрузить иконку; callback(key, QPixmap) в GUI-потоке."""
        if not self.enabled:
            return None
        key = str(key or image_name_value or "")
        cached = self.get_cached(key)
        if cached is not None:
            if callable(callback):
                callback(key, cached)
            return key
        image = image_name(image_name_value) if image_name_value else ""
        if not image or image in self._missing or key in self._missing:
            pix = self.placeholder(size)
            if callable(callback):
                callback(key, pix)
            return key
        path = self._find_disk(key)
        if path:
            pix = self._load_pixmap(path)
            if pix is not None:
                self._remember(key, pix)
                if callable(callback):
                    callback(key, pix)
                return key
        bundle_pix = bundle_pixmap(key)
        if bundle_pix is not None:
            self._remember(key, bundle_pix)
            if callable(callback):
                callback(key, bundle_pix)
            return key
        log_missing_icon(key)
        if callable(callback):
            callback(key, self.placeholder(size))
        return key

    def request_item(self, key, item, callback=None, size=32):
        """Главный путь: иконка из библиотеки по slug (DATA_DIR → bundle).

        1.0.0: при готовом пакете (``icon_pack.json``) файл берётся только с
        диска, а если его нет — сразу плейсхолдер. Сеть не используется.
        """
        if not self.enabled:
            return None
        item = dict(item or {})
        key = str(key or item.get("slug") or "")
        if not key:
            return None
        cached = self.get_cached(key)
        if cached is not None:
            if callable(callback):
                callback(key, cached)
            return key
        disk = self._find_disk(key)
        if disk:
            pix = self._load_pixmap(disk)
            if pix is not None:
                self._remember(key, pix)
                if callable(callback):
                    callback(key, pix)
                return key
        bundle_pix = bundle_pixmap(key)
        if bundle_pix is not None:
            self._remember(key, bundle_pix)
            if callable(callback):
                callback(key, bundle_pix)
            return key
        log_missing_icon(key)
        pix = self.placeholder(size)
        if callable(callback):
            callback(key, pix)
        return key

    def _load_pixmap(self, path):
        try:
            from PyQt5.QtWidgets import QApplication
            if QApplication.instance() is None:
                return None
            from PyQt5.QtGui import QPixmap
            pix = QPixmap(path)
            if pix.isNull():
                return None
            return pix
        except Exception:
            return None

    def _load_image(self, path):
        try:
            from PyQt5.QtGui import QImage
            image = QImage(path)
            return image if not image.isNull() else None
        except Exception:
            return None


_PROVIDER = None
_PROVIDER_LOCK = threading.Lock()


def get_provider(base_dir=None, **kwargs):
    global _PROVIDER
    with _PROVIDER_LOCK:
        if _PROVIDER is None:
            base = base_dir or os.environ.get("WF_DATA_DIR") or os.getcwd()
            _PROVIDER = ItemIconProvider(base, **kwargs)
        else:
            if "enabled" in kwargs:
                _PROVIDER.enabled = bool(kwargs["enabled"])
            if "auto_download" in kwargs:
                _PROVIDER.auto_download = bool(kwargs["auto_download"])
        return _PROVIDER


def reset_provider():
    global _PROVIDER
    with _PROVIDER_LOCK:
        _PROVIDER = None


__all__ = ["ItemIconProvider", "get_provider", "reset_provider",
           "normalize_name", "base_candidates", "build_index", "is_blueprint",
           "image_name", "CATEGORIES"]
