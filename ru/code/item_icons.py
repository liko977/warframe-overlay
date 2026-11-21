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
        for candidate in (norm,):
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
            key = normalize_name(name)
            if key and image and key not in index:
                index[key] = image
            # imageName иногда без расширения; имя файла важнее
    return index


class _Bridge:
    """Ленивый Qt-мостик: callback всегда в GUI-потоке."""

    def __init__(self, callback):
        self.callback = callback
        try:
            from PyQt5.QtCore import QObject, pyqtSignal

            class _Q(QObject):
                ready = pyqtSignal(str, object)
            self.q = _Q()
            self.q.ready.connect(lambda key, pix: self.callback(key, pix))
        except Exception:
            self.q = None

    def emit(self, key, pixmap):
        if self.q is not None:
            try:
                self.q.ready.emit(key, pixmap)
                return
            except Exception:
                pass
        try:
            self.callback(key, pixmap)
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
        self._lock = threading.Lock()
        self._pixmaps = OrderedDict()
        self._attempted = set()
        self._executor = None
        self._index_threads = []
        try:
            os.makedirs(self.icons_dir, exist_ok=True)
        except Exception:
            pass
        self._load_index_sync()

    # ── индекс ────────────────────────────────────────────────────────
    @staticmethod
    def _fetch_url(url, timeout=8):
        request = urllib.request.Request(url, headers={
            "User-Agent": "WarframePriceOverlayRU/0.6.4"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()

    def _load_index_sync(self):
        try:
            with open(self.index_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            self._index = dict(data.get("index") or {})
        except Exception:
            self._index = {}
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
                    json.dump({"updated": time.time(), "index": index},
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
        return ""

    # ── картинки ──────────────────────────────────────────────────────
    def _disk_path(self, key):
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(key))[:120]
        return os.path.join(self.icons_dir, safe + ".png")

    def _make_placeholder(self, size=32):
        try:
            from PyQt5.QtCore import Qt
            from PyQt5.QtGui import QColor, QPainter, QPixmap
            pix = QPixmap(int(size), int(size))
            pix.fill(QColor("#24344a"))
            painter = QPainter(pix)
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setBrush(QColor("#4aa8ff"))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(4, 4, size - 8, size - 8, 3, 3)
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
        if not image:
            pix = self.placeholder(size)
            if callable(callback):
                callback(key, pix)
            return key
        path = self._disk_path(key)
        if os.path.isfile(path):
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
        bridge = _Bridge(callback) if callable(callback) else None

        def worker():
            pix = None
            try:
                data = self.loader(CDN.format(image=image))
                if isinstance(data, (bytes, bytearray)):
                    path_tmp = path + ".tmp"
                    with open(path_tmp, "wb") as handle:
                        handle.write(data)
                    os.replace(path_tmp, path)
                    pix = self._load_pixmap(path)
            except Exception:
                pix = None
            if pix is None:
                pix = self.placeholder(size)
            else:
                self._remember(key, pix)
            if bridge is not None:
                bridge.emit(key, pix)

        if self._executor is None:
            from concurrent.futures import ThreadPoolExecutor
            self._executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        self._executor.submit(worker)
        return key

    def _load_pixmap(self, path):
        try:
            from PyQt5.QtGui import QPixmap
            pix = QPixmap(path)
            if pix.isNull():
                return None
            return pix
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
