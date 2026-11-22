# -*- coding: utf-8 -*-
"""Пакет иконок предметов warframe.market — однократное скачивание и офлайн.

Иконки предметов раньше подгружались «по требованию» (`item_icons.py`):
пока предмет не встретился в «Маркете»/«Дропе», картинки на диске нет, а
без сети на её месте пустое место. Здесь — полноценный пакет:

* каталог берётся из ``GET https://api.warframe.market/v2/items`` и
  дедуплицируется по ``slug``;
* картинки (``i18n.en.thumb``, 128×128 webp, ~7 КБ) кладутся в
  ``DATA_DIR/icons/<slug>.<ext>`` — туда же, куда их писала ленивая загрузка;
* манифест ``DATA_DIR/icon_pack.json`` —
  ``{version, format, updated_at, count, source, hash, slugs, sources}``;
* не более 3 запросов/с (ToS маркета), конкурентность 3, ретраи с backoff,
  таймаут 15 с;
* возобновляемость: уже скачанные файлы пропускаются, поэтому повторный
  запуск продолжает с места обрыва;
* фолбэк картинки: ``thumb`` → ``icon`` → WFCD ``cdn.warframestat.us``;
  если нет ни одной — это не ошибка, UI покажет плейсхолдер;
* по завершении устаревшие файлы предметов удаляются (только из ``icons/``).

Модуль не знает про Qt: его можно гонять в тестах без GUI.
"""

import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─────────────────────────────────────────────────────────────────────────
#  Константы
# ─────────────────────────────────────────────────────────────────────────
API_ITEMS = "https://api.warframe.market/v2/items"
MARKET_ASSETS = "https://warframe.market/static/assets/"
CDN = "https://cdn.warframestat.us/img/{image}"

PACK_NAME = "icon_pack.json"
ICONS_SUBDIR = "icons"
PACK_FORMAT = 1
#: 1.0.0 (t54): файлы меньше этого размера считаем подозрительными
MIN_ICON_BYTES = 2048

PACK_SOURCE = "market"

#: ToS warframe.market: не чаще 3 запросов в секунду.
MAX_RATE = 3.0
#: Сколько картинок качаем одновременно (3–4 по спеке, 3 — безопасно).
WORKERS = 3
#: Таймаут одного HTTP-запроса, сек.
TIMEOUT = 15
#: Попыток на один URL (первая + ретраи с backoff).
RETRIES = 3
USER_AGENT = "VoidFrameRU/1.0.0 (+icon pack)"

#: суффиксы частей/чертежей — для фолбэка WFCD на базовый предмет.
_PART_SUFFIXES = (
    "chassis", "neuroptics", "systems", "barrel", "receiver", "stock",
    "blades", "blade", "handle", "gauntlet", "link", "upper_limb",
    "lower_limb", "string", "carapace", "cerebrum", "disc", "stars",
    "ornament", "pouch", "band", "buckle", "hilt",
)
_EXT_RE = re.compile(r"\.(png|jpe?g|webp)$", re.IGNORECASE)
_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


# ─────────────────────────────────────────────────────────────────────────
#  Пути и мелкие утилиты
# ─────────────────────────────────────────────────────────────────────────
def pack_path(base_dir):
    """Путь к манифесту пакета (``DATA_DIR/icon_pack.json``)."""
    return os.path.join(str(base_dir), PACK_NAME)


def icons_dir(base_dir):
    """Папка с иконками (``DATA_DIR/icons``)."""
    return os.path.join(str(base_dir), ICONS_SUBDIR)


def safe_stem(value):
    """slug → безопасное имя файла (как в ``item_icons._disk_path``)."""
    return _SAFE_RE.sub("_", str(value or "").strip())[:120]


def read_manifest(base_dir):
    """Прочитать манифест; нет файла/битый JSON — пустой словарь."""
    try:
        with open(pack_path(base_dir), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return dict(data) if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_manifest(base_dir, payload):
    """Атомарно записать манифест (tmp + replace)."""
    path = pack_path(base_dir)
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def ext_from_url(url):
    """Расширение файла по URL (по умолчанию ``.webp`` — так отдаёт маркет)."""
    path = str(url or "").split("?", 1)[0]
    name = path.rsplit("/", 1)[-1]
    match = _EXT_RE.search(name)
    return match.group(0).lower() if match else ""


def camel_name(text):
    """«Twin Keres Prime» → «TwinKeresPrime» (имена файлов WFCD)."""
    words = re.split(r"[^A-Za-z0-9]+", str(text or ""))
    return "".join(word[:1].upper() + word[1:] for word in words if word)


def wfcd_candidates(entry):
    """Имена файлов для WFCD CDN: полное имя → базовый предмет."""
    entry = dict(entry or {})
    slug = str(entry.get("slug") or "").strip()
    names = []
    for value in (entry.get("name_en"), entry.get("image_name"),
                  slug.replace("_", " ")):
        camel = camel_name(value)
        if camel and camel not in names:
            names.append(camel)
    # базовый предмет: убираем _blueprint/часть/_set и пробуем снова
    base = slug
    changed = True
    while changed:
        changed = False
        for suffix in ("_blueprint", "_set") + tuple(
                "_" + part for part in _PART_SUFFIXES):
            if base.endswith(suffix) and len(base) > len(suffix):
                base = base[: -len(suffix)]
                camel = camel_name(base)
                if camel and camel not in names:
                    names.append(camel)
                changed = True
                break
    return [CDN.format(image=name + ".png") for name in names]


def market_url(value):
    """Относительный путь маркета → абсолютный URL."""
    value = str(value or "").strip()
    if not value:
        return ""
    if value.lower().startswith(("http://", "https://")):
        return value
    return MARKET_ASSETS + value.lstrip("/")


def icon_urls(entry):
    """Порядок загрузки картинки: thumb → icon → WFCD CDN."""
    entry = dict(entry or {})
    urls = []
    for field in ("thumb", "icon"):
        url = market_url(entry.get(field))
        if url and url not in urls:
            urls.append(url)
    for url in wfcd_candidates(entry):
        if url not in urls:
            urls.append(url)
    return urls


def source_rel(entry):
    """Путь картинки в каталоге (thumb, иначе icon) — для поиска изменений."""
    entry = dict(entry or {})
    for field in ("thumb", "icon"):
        value = str(entry.get(field) or "").strip()
        if value:
            return value
    return ""


def normalize_entries(payload):
    """Ответ ``/v2/items`` → список ``{slug, thumb, icon, name_en, name_ru}``.

    Дедупликация строго по ``slug``: в ответе маркета один предмет на строку,
    но кэш/мок может отдать дубли — второй экземпляр отбрасываем.
    """
    if isinstance(payload, dict):
        rows = payload.get("data")
        if rows is None and isinstance(payload.get("payload"), dict):
            rows = payload["payload"].get("items")
        rows = rows or []
    elif isinstance(payload, (list, tuple)):
        rows = list(payload)
    else:
        return []
    entries = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        slug = str(row.get("slug") or "").strip()
        if not slug or slug in seen:
            continue
        i18n = row.get("i18n") if isinstance(row.get("i18n"), dict) else {}
        en = i18n.get("en") if isinstance(i18n.get("en"), dict) else {}
        ru = i18n.get("ru") if isinstance(i18n.get("ru"), dict) else {}
        seen.add(slug)
        entries.append({
            "slug": slug,
            "thumb": str(en.get("thumb") or ""),
            "icon": str(en.get("icon") or ""),
            "name_en": str(en.get("name") or ""),
            "name_ru": str(ru.get("name") or ""),
            "image_name": str(row.get("imageName") or ""),
        })
    return entries


def catalog_hash(entries):
    """Стабильный хеш каталога: slug + путь картинки, 16 hex-символов."""
    rows = sorted(
        f"{str(e.get('slug') or '')}|{source_rel(e)}"
        for e in (entries or []) if isinstance(e, dict))
    digest = hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()
    return digest[:16]


def format_progress(done, total):
    """«Иконки: 1240 / 2500 (49%)»."""
    try:
        done = max(0, int(done))
        total = max(0, int(total))
    except Exception:
        done, total = 0, 0
    percent = int(done * 100.0 / total) if total else 0
    return f"Иконки: {done} / {total} ({percent}%)"


def human_bytes(value):
    """Байты → «27.1 МБ» для настроек."""
    try:
        size = float(value or 0)
    except Exception:
        size = 0.0
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024 or unit == "ГБ":
            if unit == "Б":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} ГБ"


def _default_fetch(url, timeout=TIMEOUT):
    """Один GET → bytes (urllib, без внешних зависимостей)."""
    import urllib.request

    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


class RateLimiter:
    """Глобальный троттлинг: не более ``rate`` запросов в секунду.

    Все потоки загрузчика делят один лимитер, поэтому суммарная частота
    запросов к маркету не превышает лимит независимо от конкурентности.
    """

    def __init__(self, rate=MAX_RATE, clock=None, sleep=None):
        self.rate = max(0.1, float(rate or MAX_RATE))
        self.interval = 1.0 / self.rate
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._next = 0.0
        self._lock = threading.Lock()
        self.requests = 0

    def reset(self):
        with self._lock:
            self._next = 0.0
            self.requests = 0

    def acquire(self):
        """Дождаться слота; возвращает, сколько реально пришлось ждать."""
        with self._lock:
            now = self._clock()
            slot = self._next if self._next > now else now
            self._next = slot + self.interval
            self.requests += 1
        wait = slot - now
        if wait > 0:
            self._sleep(wait)
        return max(0.0, wait)


class IconPack:
    """Загрузчик/хранитель пакета иконок (сеть, диск, манифест)."""

    def __init__(self, base_dir, fetcher=None, catalog_fetcher=None,
                 rate=MAX_RATE, workers=WORKERS, timeout=TIMEOUT,
                 retries=RETRIES, logger=None, app_version="",
                 clock=None, sleep=None):
        self.base_dir = str(base_dir)
        self.icons_dir = icons_dir(base_dir)
        self.pack_path = pack_path(base_dir)
        self.fetcher = fetcher or _default_fetch
        self.catalog_fetcher = catalog_fetcher or self.fetcher
        self.timeout = int(timeout or TIMEOUT)
        self.retries = max(1, int(retries or RETRIES))
        self.workers = max(1, int(workers or WORKERS))
        self.logger = logger
        self.app_version = str(app_version or "")
        self.limiter = RateLimiter(rate, clock=clock, sleep=sleep)
        self._sleep = sleep or time.sleep
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._running = False
        self._manifest = None
        self._last_result = {}
        try:
            os.makedirs(self.icons_dir, exist_ok=True)
        except Exception:
            pass

    # ── состояние ─────────────────────────────────────────────────────
    def log(self, message):
        if callable(self.logger):
            try:
                self.logger(str(message))
            except Exception:
                pass

    @property
    def running(self):
        return bool(self._running)

    def cancelled(self):
        return self._cancel.is_set()

    def request_cancel(self):
        """Попросить загрузку остановиться (файлы остаются на диске)."""
        self._cancel.set()

    def manifest(self, refresh=False):
        if self._manifest is None or refresh:
            self._manifest = read_manifest(self.base_dir)
        return dict(self._manifest or {})

    def has_pack(self):
        """Есть ли готовый манифест (значит, качать нечего)."""
        manifest = self.manifest()
        return bool(manifest) and bool(manifest.get("updated_at"))

    def files(self):
        """``{stem: (name, size)}`` по папке icons (только картинки)."""
        found = {}
        try:
            names = os.listdir(self.icons_dir)
        except Exception:
            return found
        for name in names:
            if not _EXT_RE.search(name):
                continue
            stem = name.rsplit(".", 1)[0]
            try:
                size = os.path.getsize(os.path.join(self.icons_dir, name))
            except Exception:
                size = 0
            if size > 0:
                found[stem] = (name, size)
        return found

    def disk_size(self):
        """Суммарный объём картинок папки icons, байты."""
        return sum(size for _name, size in self.files().values())

    def status(self):
        """Состояние для настроек: есть пак/нет, файлов, объём, дата."""
        files = self.files()
        manifest = self.manifest()
        updated = float(manifest.get("updated_at") or 0)
        return {
            "have_pack": self.has_pack(),
            "count": int(manifest.get("count") or 0),
            "files": len(files),
            "bytes": sum(size for _name, size in files.values()),
            "source": str(manifest.get("source") or ""),
            "hash": str(manifest.get("hash") or ""),
            "updated_at": updated,
            "updated_text": (time.strftime("%d.%m.%Y %H:%M",
                                           time.localtime(updated))
                             if updated else ""),
            "running": self.running,
        }

    def needs_download(self):
        """Нужен ли первый запуск загрузки (манифеста нет)."""
        return not self.has_pack()

    def needs_update(self, days=7, now=None):
        """Пора ли обновлять пак (``days`` = 0 → автообновление выключено)."""
        try:
            days = int(days)
        except Exception:
            days = 0
        if days <= 0:
            return False
        manifest = self.manifest()
        if not manifest:
            return True
        stamp = float(manifest.get("updated_at") or 0)
        if not stamp:
            return True
        moment = time.time() if now is None else float(now)
        return (moment - stamp) >= days * 86400.0

    # ── каталог ───────────────────────────────────────────────────────
    def fetch_catalog(self):
        """Список предметов маркета; ошибка сети → ``([], текст)``."""
        try:
            try:
                raw = self.catalog_fetcher(API_ITEMS, timeout=self.timeout)
            except TypeError:
                raw = self.catalog_fetcher(API_ITEMS)
        except Exception as exc:
            return [], f"{type(exc).__name__}: {exc}"
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = raw.decode("utf-8", "replace")
            except Exception:
                return [], "каталог не декодируется"
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception as exc:
                return [], f"каталог не JSON: {type(exc).__name__}"
        entries = normalize_entries(raw)
        if not entries:
            return [], "каталог пуст"
        return entries, ""

    # ── загрузка ──────────────────────────────────────────────────────
    def _write(self, stem, url, data):
        ext = ext_from_url(url) or ".webp"
        path = os.path.join(self.icons_dir, stem + ext)
        tmp = path + ".tmp"
        try:
            os.makedirs(self.icons_dir, exist_ok=True)
            with open(tmp, "wb") as handle:
                handle.write(data)
            os.replace(tmp, path)
        except Exception:
            try:
                os.remove(tmp)
            except Exception:
                pass
            return ""
        self._drop_other_extensions(stem, stem + ext)
        return stem + ext

    def _drop_other_extensions(self, stem, keep_name):
        """У предмета один файл: после смены расширения старый убираем."""
        try:
            names = os.listdir(self.icons_dir)
        except Exception:
            return 0
        removed = 0
        for name in names:
            if name == keep_name or not _EXT_RE.search(name):
                continue
            if name.rsplit(".", 1)[0] != stem:
                continue
            try:
                os.remove(os.path.join(self.icons_dir, name))
                removed += 1
            except Exception:
                pass
        return removed

    @staticmethod
    def _is_not_found(exc):
        """404/битый ответ = «картинки нет»; прочее = временная ошибка сети."""
        try:
            code = getattr(exc, "code", None)
            if code in (404, 410):
                return True
        except Exception:
            pass
        text = str(exc or "")
        return ("404" in text or "410" in text
                or "Not Found" in text or "No such file" in text)

    def _fetch_one(self, entry):
        """Скачать одну иконку: thumb → icon → WFCD, с ретраями.

        ``status`` результата:
          * ``ok`` — файл записан;
          * ``missing`` — все варианты ответили «нет такой картинки»
            (это НЕ ошибка: UI покажет плейсхолдер);
          * ``error`` — были временные сбои сети (пакет не фиксируем,
            повторим при следующем запуске);
          * ``cancelled`` — загрузку отменили.
        """
        entry = dict(entry or {})
        stem = safe_stem(entry.get("slug"))
        if not stem:
            return {"status": "missing", "slug": "", "error": "пустой slug"}
        last_error = ""
        transient = False
        for url in icon_urls(entry):
            for attempt in range(self.retries):
                if self._cancel.is_set():
                    return {"status": "cancelled", "slug": stem}
                self.limiter.acquire()
                try:
                    data = self.fetcher(url, timeout=self.timeout)
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    if not self._is_not_found(exc):
                        transient = True
                    data = b""
                if isinstance(data, (bytes, bytearray)) and data:
                    name = self._write(stem, url, bytes(data))
                    if name:
                        return {"status": "ok", "slug": stem, "file": name,
                                "url": url, "bytes": len(data)}
                    last_error = "не удалось записать файл"
                    transient = True
                if attempt + 1 < self.retries:
                    self._sleep(min(2.0, 0.3 * (2 ** attempt)))
        return {"status": "error" if transient else "missing",
                "slug": stem, "error": last_error}

    def _report(self, progress, done, total, phase="", extra=None):
        if not callable(progress):
            return
        payload = {
            "done": int(done),
            "total": int(total),
            "percent": int(done * 100.0 / total) if total else 0,
            "text": format_progress(done, total),
            "phase": str(phase or ""),
            "running": True,
        }
        if extra:
            payload.update(extra)
        try:
            progress(payload)
        except Exception:
            pass

    @staticmethod
    def _dedupe(entries):
        """Дедупликация каталога по slug (порядок сохраняем)."""
        seen = set()
        unique = []
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            slug = str(entry.get("slug") or "")
            if not slug or slug in seen:
                continue
            seen.add(slug)
            unique.append(dict(entry))
        return unique

    def download(self, progress=None, force=False, prune=True, entries=None,
                 catalog_entries=None):
        """Скачать весь пакет. Возвращает сводку ``dict``.

        ``entries`` — что качать (по умолчанию весь каталог);
        ``catalog_entries`` — полный каталог для манифеста/очистки (нужен
        при докачке подмножества из :meth:`update`).

        ``force=True`` перекачивает всё, иначе существующие файлы
        пропускаются (возобновляемость). При отмене манифест не пишется —
        следующий запуск продолжит с места обрыва.
        """
        started = time.time()
        if self._running:
            return {"ok": False, "busy": True, "error": "загрузка уже идёт"}
        self._running = True
        self._cancel.clear()
        result = {"ok": False, "total": 0, "downloaded": 0, "skipped": 0,
                  "missing": 0, "failed": 0, "pruned": 0, "bytes": 0,
                  "new": 0, "updated": 0, "cancelled": False, "errors": [],
                  "redownloaded": 0, "broken": 0}
        pool = None
        done = 0
        total = 0
        try:
            if entries is None:
                entries, error = self.fetch_catalog()
                if not entries:
                    result["error"] = error or "нет каталога"
                    self.log(f"Иконки: каталог не получен ({result['error']})")
                    return result
            todo = self._dedupe(entries)
            catalog = self._dedupe(catalog_entries) if catalog_entries \
                else list(todo)
            total = len(todo)
            result["total"] = total
            os.makedirs(self.icons_dir, exist_ok=True)
            files = self.files()
            redownload = set()
            if not force:
                pending = []
                for entry in todo:
                    stem = safe_stem(entry.get("slug"))
                    got = files.get(stem)
                    if got:
                        size = int(got[1] or 0)
                        if size >= MIN_ICON_BYTES:
                            result["skipped"] += 1
                            continue
                        # мелкий/битый файл — перекачиваем, не пропускаем
                        redownload.add(stem)
                    pending.append(entry)
                todo = pending
            done = result["skipped"]
            self._report(progress, done, total, "download")
            if todo:
                pool = ThreadPoolExecutor(max_workers=self.workers)
                futures = {pool.submit(self._fetch_one, entry): entry
                           for entry in todo}
                for future in as_completed(futures):
                    if self._cancel.is_set():
                        break
                    try:
                        outcome = future.result()
                    except Exception as exc:
                        outcome = {"status": "missing", "slug": "",
                                   "error": f"{type(exc).__name__}: {exc}"}
                    status = outcome.get("status")
                    if status == "ok":
                        result["downloaded"] += 1
                        if outcome.get("slug") in redownload:
                            result["redownloaded"] += 1
                        result["bytes"] += int(outcome.get("bytes") or 0)
                    elif status == "error":
                        # временный сбой сети: пакет не фиксируем, повторим позже
                        result["failed"] += 1
                        result["errors"].append(
                            f"{outcome.get('slug')}: {outcome.get('error')}")
                    elif status != "cancelled":
                        # картинки нет ни на маркете, ни в WFCD — не ошибка
                        result["missing"] += 1
                    done += 1
                    self._report(progress, done, total, "download")
            if self._cancel.is_set():
                result["ok"] = False
                result["cancelled"] = True
                result["elapsed"] = round(time.time() - started, 2)
                self._report(progress, done, total, "cancelled",
                             {"running": False})
                self.log(f"Иконки: загрузка отменена ({done} / {total}); "
                         "продолжим при следующем запуске")
                return result
            if result["failed"]:
                # сеть оборвалась на части иконок — манифест не пишем,
                # следующий запуск продолжит (существующие файлы пропустятся)
                result["ok"] = False
                result["error"] = (f"не скачано иконок: {result['failed']} "
                                   "(ошибки сети)")
                result["elapsed"] = round(time.time() - started, 2)
                self.log(f"Иконки: не всё скачалось ({result['failed']} "
                         "ошибок сети) — продолжим при следующем запуске")
                self._report(progress, done, total, "error",
                             {"running": False})
                return result
            files = self.files()
            by_stem = {}
            for entry in catalog:
                by_stem[safe_stem(entry.get("slug"))] = entry
            small = [(stem, int(got[1] or 0))
                     for stem, got in files.items()
                     if int(got[1] or 0) < MIN_ICON_BYTES]
            if small:
                self.log("Иконки: подозрительно малы (перекачиваю): "
                         + ", ".join(f"{s} ({n} Б)"
                                     for s, n in small[:20]))
                for stem, _size in small:
                    entry = by_stem.get(stem)
                    if not entry:
                        continue
                    outcome = self._fetch_one(entry)
                    if outcome.get("status") == "ok":
                        result["redownloaded"] += 1
                        result["bytes"] += int(outcome.get("bytes") or 0)
                files = self.files()
                small = [(stem, int(got[1] or 0))
                         for stem, got in files.items()
                         if int(got[1] or 0) < MIN_ICON_BYTES]
            result["broken"] = len(small)
            for stem, size in small[:20]:
                self.log(f"Иконки: иконка подозрительно мала — {stem} "
                         f"({size} Б)")
            self.log(
                f"Иконки: скачано {result['downloaded']}, "
                f"пропущено {result['skipped']}, "
                f"перекачано {result['redownloaded']}, "
                f"битых {result['broken']}, кэш сброшен")
            current = []
            for entry in catalog:
                stem = safe_stem(entry.get("slug"))
                if stem in files:
                    current.append(stem)
            if prune:
                result["pruned"] = self.prune(current)
            manifest = self.build_manifest(catalog, files)
            if write_manifest(self.base_dir, manifest):
                self._manifest = manifest
            result.update({
                "ok": True,
                "count": int(manifest.get("count") or 0),
                "hash": manifest.get("hash") or "",
                "bytes_total": self.disk_size(),
                "elapsed": round(time.time() - started, 2),
            })
            self._report(progress, total, total, "done", {"running": False})
            self._last_result = dict(result)
            if not result.get("new"):
                self.log(f"Иконки: пакет готов — файлов {result['count']}, "
                         f"скачано {result['downloaded']}, "
                         f"пропущено {result['skipped']}")
            return result
        finally:
            if pool is not None:
                cancelled_now = self._cancel.is_set()
                pool.shutdown(wait=not cancelled_now, cancel_futures=True)
            self._running = False

    def build_manifest(self, entries, files=None):
        """Собрать манифест по каталогу и файлам на диске."""
        files = self.files() if files is None else files
        slugs = []
        sources = {}
        total_bytes = 0
        for entry in entries or []:
            stem = safe_stem(entry.get("slug"))
            got = files.get(stem)
            if not got:
                continue
            slugs.append(stem)
            sources[stem] = source_rel(entry)
            total_bytes += int(got[1] or 0)
        now = time.time()
        return {
            "version": self.app_version,
            "format": PACK_FORMAT,
            "updated_at": now,
            "updated_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S",
                                            time.localtime(now)),
            "count": len(slugs),
            "source": PACK_SOURCE,
            "hash": catalog_hash(entries or []),
            "bytes": total_bytes,
            "slugs": sorted(slugs),
            "sources": sources,
        }

    def prune(self, current_stems):
        """Удалить из ``icons/`` файлы предметов, которых больше нет в каталоге.

        Удаляются только те файлы, которые были в предыдущем манифесте
        (то есть заведомо наши иконки предметов) и не входят в текущий
        каталог. Посторонние файлы папки не трогаем.
        """
        previous = self.manifest()
        known = set(str(s) for s in (previous.get("slugs") or []))
        if not known:
            known = set(str(s) for s in (previous.get("sources") or {}).keys())
        if not known:
            return 0
        current = set(str(s) for s in (current_stems or []))
        removed = 0
        for stem in sorted(known - current):
            try:
                names = [n for n in os.listdir(self.icons_dir)
                         if n.rsplit(".", 1)[0] == stem]
            except Exception:
                names = []
            for name in names:
                try:
                    os.remove(os.path.join(self.icons_dir, name))
                    removed += 1
                except Exception:
                    pass
        if removed:
            self.log(f"Иконки: удалено устаревших файлов — {removed}")
        return removed

    def update(self, progress=None, entries=None, prune=True):
        """Сверка каталога и докачка только новых/изменённых иконок."""
        previous = self.manifest()
        old_slugs = set(str(s) for s in (previous.get("slugs") or []))
        old_sources = dict(previous.get("sources") or {})
        if entries is None:
            entries, error = self.fetch_catalog()
            if not entries:
                self.log(f"Иконки: обновление пропущено ({error})")
                return {"ok": False, "error": error, "total": 0,
                        "new": 0, "updated": 0}
        entries = self._dedupe(entries)
        files = self.files()
        todo = []
        new_count = 0
        changed_count = 0
        for entry in entries:
            stem = safe_stem(entry.get("slug"))
            got = files.get(stem)
            rel = source_rel(entry)
            if not got:
                todo.append(entry)
                if stem not in old_slugs:
                    new_count += 1
                else:
                    changed_count += 1
                continue
            if stem not in old_slugs:
                # файл уже есть (скачан «по требованию») — просто войдёт в пак
                new_count += 1
                continue
            if int(got[1] or 0) < MIN_ICON_BYTES:
                changed_count += 1
                todo.append(entry)
                continue
            known_rel = old_sources.get(stem) or ""
            if known_rel and rel and known_rel != rel:
                changed_count += 1
                todo.append(entry)
        sub = self.download(progress=progress, force=True, prune=prune,
                            entries=todo, catalog_entries=entries)
        if not sub.get("ok"):
            return sub
        result = {
            "ok": True,
            "total": len(entries),
            "new": int(new_count),
            "updated": int(changed_count),
            "downloaded": int(sub.get("downloaded") or 0),
            "pruned": int(sub.get("pruned") or 0),
            "count": int(sub.get("count") or 0),
            "redownloaded": int(sub.get("redownloaded") or 0),
            "broken": int(sub.get("broken") or 0),
            "text": f"Иконки: +{int(new_count)} новых, "
                    f"обновлено {int(changed_count)}",
        }
        self.log(result["text"])
        self._last_result = dict(result)
        if callable(progress):
            try:
                progress({"done": len(entries), "total": len(entries),
                          "percent": 100, "text": result["text"],
                          "phase": "done", "running": False,
                          "new": result["new"],
                          "updated": result["updated"]})
            except Exception:
                pass
        return result

    def clear(self):
        """Удалить пакет: все картинки из icons/ и манифест."""
        removed = 0
        try:
            names = os.listdir(self.icons_dir)
        except Exception:
            names = []
        for name in names:
            if not _EXT_RE.search(name) and not name.endswith(".tmp"):
                continue
            try:
                os.remove(os.path.join(self.icons_dir, name))
                removed += 1
            except Exception:
                pass
        try:
            os.remove(self.pack_path)
        except Exception:
            pass
        self._manifest = {}
        self.log(f"Иконки: кэш очищен (удалено файлов: {removed})")
        return {"ok": True, "removed": removed}


_PACK = None
_PACK_LOCK = threading.Lock()


def get_pack(base_dir=None, **kwargs):
    """Единственный экземпляр загрузчика (как ``item_icons.get_provider``)."""
    global _PACK
    with _PACK_LOCK:
        if _PACK is None:
            base = base_dir or os.environ.get("WF_DATA_DIR") or os.getcwd()
            _PACK = IconPack(base, **kwargs)
        return _PACK


def reset_pack():
    global _PACK
    with _PACK_LOCK:
        _PACK = None


__all__ = ["IconPack", "RateLimiter", "get_pack", "reset_pack",
           "read_manifest", "write_manifest", "pack_path", "icons_dir",
           "normalize_entries", "icon_urls", "wfcd_candidates", "catalog_hash",
           "format_progress", "human_bytes", "safe_stem", "source_rel",
           "API_ITEMS", "PACK_NAME", "MAX_RATE"]
