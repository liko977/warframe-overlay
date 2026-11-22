# -*- coding: utf-8 -*-
"""Иконки предметов Warframe (WFCD/CDN) — ленивая загрузка и кэш.

Сеть никогда не блокирует GUI: индекс WFCD обновляется в фоне, картинки
качаются в пуле из 3 потоков и подставляются через Qt-сигнал. Без сети
используются дисковой кэш и аккуратный плейсхолдер.
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


def part_badge(slug="", name=""):
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


def image_variants(image, name="", slug="", game_ref=""):
    """Возможные имена файлов CDN: базовое, без Prime/частей, CamelCase, Ak/Dual."""
    base = image_name(image)
    out = []

    def add(value):
        value = image_name(value)
        if value and value not in out:
            out.append(value)

    add(base)
    stem = base.rsplit(".", 1)[0] if base else ""
    original_stem = stem
    add(stem.replace("Prime", ""))
    for token in _PARTS:
        stem2 = re.sub(rf"{re.escape(token)}", "", stem, flags=re.IGNORECASE)
        if stem2 and stem2 != stem:
            add(stem2)
            stem = stem2
    for value in (name, slug.replace("_", " "), game_ref.rsplit("/", 1)[-1]):
        value = str(value or "")
        if not value:
            continue
        camel = camel_name(value)
        add(camel)
        add(camel.replace("Prime", ""))
        # оружие с префиксом Ak/Dual/Twin: BoltoPrime → AkboltoPrime
        for prefix in ("Ak", "Dual", "Twin"):
            if camel and not camel.lower().startswith(prefix.lower()):
                tail = camel[:1].lower() + camel[1:]
                add(prefix + tail)
                add(prefix + tail.replace("Prime", ""))
    if stem:
        add(stem + "Weapon")
        add(stem.replace("Prime", "") + "PrimeWeapon")
        add("Ak" + original_stem[:1].lower() + original_stem[1:])
        add("Ak" + (original_stem[:1].lower() + original_stem[1:]).replace(
            "Prime", ""))
        add("Ak" + stem[:1].lower() + stem[1:])
        add("Ak" + (stem[:1].lower() + stem[1:]).replace("Prime", ""))
        if "Kavasa" in stem:
            add(stem + "Collar")
            add(original_stem + "Collar")
            add(stem.replace("Prime", "") + "Collar")
    return out


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


class ItemIconProvider:
    def __init__(self, base_dir, fetcher=None, loader=None, enabled=True,
                 index_ttl=INDEX_TTL, memory_limit=MAX_MEMORY):
        self.base_dir = str(base_dir)
        self.icons_dir = os.path.join(self.base_dir, "icons")
        self.index_path = os.path.join(self.base_dir, "icon_index.json")
        self.fetcher = fetcher or self._fetch_url
        self.loader = loader or self._fetch_url
        self.enabled = bool(enabled)
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
        try:
            os.makedirs(self.icons_dir, exist_ok=True)
        except Exception:
            pass
        self._load_index_sync()

    # ── индекс ────────────────────────────────────────────────────────
    @staticmethod
    def _fetch_url(url, timeout=8):
        request = urllib.request.Request(url, headers={
            "User-Agent": "WarframePriceOverlayRU/0.7.3"})
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
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(key))[:120]
        try:
            for name in os.listdir(self.icons_dir):
                if name.startswith(safe + "."):
                    return os.path.join(self.icons_dir, name)
        except Exception:
            pass
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
        if key in self._attempted:
            if callable(callback):
                callback(key, self.placeholder(size))
            return key
        self._attempted.add(key)
        variants = image_variants(image)
        self._start_request(key, [CDN.format(image=name)
                                  for name in variants], callback, size)
        return key

    def request_item(self, key, item, callback=None, size=32):
        """Главный путь: иконка warframe.market по slug (thumb → icon), затем WFCD."""
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
        if key in self._attempted:
            if callable(callback):
                callback(key, self.placeholder(size))
            return key
        urls = []
        for field in ("thumb", "icon"):
            rel = str(item.get(field) or "").strip()
            if rel:
                if rel.startswith("http"):
                    urls.append(rel)
                else:
                    urls.append(MARKET_ASSETS + rel.lstrip("/"))
        # fallback WFCD только если рынок не дал путей или ответит 404
        name = item.get("name_en") or item.get("en_name") or item.get("name") or ""
        wfcd = self.resolve(name=name, slug=key,
                            en_name=item.get("name_en") or "",
                            game_ref=str(item.get("game_ref")
                                        or item.get("gameRef") or ""))
        if wfcd:
            urls.extend(CDN.format(image=name2)
                        for name2 in image_variants(wfcd))
        if not urls:
            pix = self.placeholder(size)
            if callable(callback):
                callback(key, pix)
            return key
        self._attempted.add(key)
        self._start_request(key, urls, callback, size)
        return key

    def _start_request(self, key, urls, callback, size):
        bridge = _Bridge(callback, self, size) if callable(callback) else None
        if bridge is not None:
            # держим мост до доставки queued-сигнала в GUI-поток
            self._bridges.add(bridge)

        def worker():
            image = None
            used_url = ""
            for url in urls:
                if url in self._missing:
                    continue
                try:
                    data = self.loader(url)
                    if isinstance(data, (bytes, bytearray)) and data:
                        ext = os.path.splitext(url.split("?", 1)[0])[1] or ".png"
                        path = self._disk_path(key, ext)
                        tmp = path + ".tmp"
                        with open(tmp, "wb") as handle:
                            handle.write(data)
                        os.replace(tmp, path)
                        image = self._load_image(path)
                        if image is not None:
                            used_url = url
                            break
                except Exception:
                    file_name = url.split("?", 1)[0].rsplit("/", 1)[-1]
                    self._mark_missing(file_name, url)
            if image is None:
                self._mark_missing(key, key)
            if bridge is not None:
                bridge.emit(key, image)

        if self._executor is None:
            from concurrent.futures import ThreadPoolExecutor
            self._executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        self._executor.submit(worker)
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
        return _PROVIDER


def reset_provider():
    global _PROVIDER
    with _PROVIDER_LOCK:
        _PROVIDER = None


__all__ = ["ItemIconProvider", "get_provider", "reset_provider",
           "normalize_name", "base_candidates", "build_index", "is_blueprint",
           "image_name", "CATEGORIES"]
