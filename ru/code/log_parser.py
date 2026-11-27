# -*- coding: utf-8 -*-
"""
Чтение игрового лога Warframe (EE.log) — ТОЛЬКО ЧТЕНИЕ
=====================================================

Модуль для ``wf_ru/warframe.py``. Полное описание — ``wf_ru/LOG_SPEC.md``.

Что делает
----------
1. Находит лог игры: путь из конфига (``log_path``) либо стандартный
   ``%LOCALAPPDATA%\\Warframe\\EE.log`` (``log_auto_detect``).
2. Читает его «хвостом»: помнит байтовый offset, забирает только новые строки,
   держит буфер неполной последней строки, корректно переживает усечение файла
   (игра пересоздаёт лог при каждом запуске) и отсутствие файла.
3. Разбирает маркеры реликвии:
     * ``VoidProjections: <id> gets reward /Lotus/StoreItems/...``  → ``drop``
       (единственный источник пути предмета — собственный дроп игрока);
     * ``OpenVoidProjectionRewardScreenRMI`` / ``ProjectionRewardChoice.swf`` /
       ``Relic rewards initialized``                              → ``open``
     * ``ProjectionRewardChoice.lua: Got rewards``                → ``rewards``
     * ``Relic reward screen shut down`` / ``Reward choice force closed`` → ``close``
4. Переводит внутриигровой путь в слаг warframe.market:
   ``gameRef = path.replace('/Lotus/StoreItems/', '/Lotus/')`` и поиск по
   обратному индексу ``{gameRef: slug}``, который строит ``MarketAPI``.

Чего в логе НЕТ (см. LOG_SPEC.md, раздел 3)
-------------------------------------------
Лог клиента содержит только СВОЙ дроп: наград остальных игроков отряда и
события выбора карточки в нём нет. Поэтому лог — дополнение к OCR-плашкам
(Alt+Q), а не замена.

Файл лога никогда не открывается на запись: только ``open(path, "rb")``.
"""
import collections
import os
import re
import threading

# ── константы ───────────────────────────────────────────────────────────
EE_LOG_MARKER = "gets reward /Lotus/StoreItems/"
GAMEREF_FROM = "/Lotus/StoreItems/"
GAMEREF_TO = "/Lotus/"
DEFAULT_POLL_MS = 500
LOG_SUBDIR = ("Warframe", "EE.log")

#: префикс сообщения о собственном дропе (основной маркер)
_RE_DROP = re.compile(r"VoidProjections:\s*([0-9a-fA-F]{24})\s+gets reward\s+(/Lotus/StoreItems/\S+)")
#: страховка на случай смены формата: путь есть, id не распознан
_RE_DROP_PATH = re.compile(r"gets reward\s+(/Lotus/StoreItems/\S+)")
#: время от старта сессии — первое число в строке
_RE_TIME = re.compile(r"^\s*(\d+(?:\.\d+)?)")
#: CamelCase → слова (только для читаемого имени, когда рынок не ответил)
_RE_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

#: началось открытие экрана реликвии (суффикса ``RMI`` у игры больше нет —
#: подстрока покрывает и ``...RewardScreen``, и старый ``...RewardScreenRMI``).
#: ``ProjectionRewardChoice.swf`` и ``Relic rewards initialized`` сюда НЕ входят:
#: в логе они повторяются (55 и 28 раз на 15 реликвий) и после close давали
#: вторую пару open/close — счётчик реликвий удваивался.
_OPEN_MARKERS = ("OpenVoidProjectionRewardScreen",)
#: экран заполнен карточками
_REWARDS_MARKERS = ("Got rewards",)
#: экран закрылся
_CLOSE_MARKERS = (
    "CloseVoidProjectionRewardScreen",
    "Relic reward screen shut down",
    "Reward choice force closed",
)


def markers_line():
    """Строка для старт-лога: какие маркеры EE.log ждёт парсер (t189)."""
    return (f"EE.log: маркеры open={_OPEN_MARKERS[0]} "
            f"close={_CLOSE_MARKERS[0]} rewards={_REWARDS_MARKERS[0]}")


# ─────────────────────────────────────────────────────────────────────────
#  Пути и маппинг путь → gameRef → slug
# ─────────────────────────────────────────────────────────────────────────
def default_log_path():
    """Стандартный путь к логу игры: ``%LOCALAPPDATA%\\Warframe\\EE.log``."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if not base:
        base = os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, *LOG_SUBDIR)


def resolve_log_path(cfg=None):
    """Путь к EE.log для чтения.

    Приоритет: ``log_path`` (или старое имя ``ee_log_path``) из конфига,
    иначе автопоиск в папке игры при ``log_auto_detect``.
    Возвращает путь даже если файла ещё нет (игра не запущена) — вызывающий
    код сам решает, предупреждать или ждать. Пустая строка = «лог не нужен».
    """
    cfg = cfg or {}
    raw = cfg.get("log_path")
    if raw is None:
        raw = cfg.get("ee_log_path")
    raw = str(raw or "").strip()
    if raw:
        try:
            return os.path.expanduser(os.path.expandvars(raw))
        except Exception:
            return raw
    if cfg.get("log_auto_detect", True) is False:
        return ""
    return default_log_path()


def path_to_gameref(path):
    """``/Lotus/StoreItems/X`` → ``/Lotus/X`` — так путь выглядит в API (gameRef)."""
    p = (path or "").strip()
    if not p:
        return ""
    return p.replace(GAMEREF_FROM, GAMEREF_TO)


def slug_from_path(path, api=None):
    """Внутриигровой путь → слаг warframe.market (или ``None``).

    Основной путь — точное совпадение gameRef в обратном индексе ``api``.
    Запасной — сравнение последнего сегмента пути (редкие переименования).
    """
    game_ref = path_to_gameref(path)
    if not game_ref:
        return None
    index = {}
    if api is not None:
        try:
            if hasattr(api, "ensure_ref_index"):
                api.ensure_ref_index()
        except Exception:
            index = {}
        try:
            index = getattr(api, "ref_to_slug", None) or {}
        except Exception:
            index = {}
    if not isinstance(index, dict):
        index = {}
    slug = index.get(game_ref)
    if slug:
        return slug
    # запасной вариант: тот же последний сегмент пути
    tail = game_ref.rsplit("/", 1)[-1]
    if not tail:
        return None
    matches = sorted({s for ref, s in index.items()
                      if isinstance(ref, str) and ref.rsplit("/", 1)[-1] == tail})
    if len(matches) == 1:
        return matches[0]
    return None


def display_name_from_path(path):
    """``.../BratonPrimeBarrel`` → ``Braton Prime Barrel`` (для журнала/консоли)."""
    last = (path or "").rstrip("/").rsplit("/", 1)[-1]
    if not last:
        return "?"
    return _RE_CAMEL.sub(" ", last).strip() or last


# ─────────────────────────────────────────────────────────────────────────
#  Разбор строк
# ─────────────────────────────────────────────────────────────────────────
def line_time(text):
    """Время строки в секундах от старта сессии (float) или ``None``."""
    m = _RE_TIME.match(text or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


class LogParser:
    """Разбирает строки EE.log в события реликвии.

    События (``dict``):
        ``{"kind": "open",    "time": 1344.142, "raw": <строка>}``
        ``{"kind": "rewards", "time": 1349.719, "raw": <строка>}``
        ``{"kind": "drop",    "time": 1349.389, "player_id": "...",
           "path": "/Lotus/StoreItems/...", "game_ref": "/Lotus/...", "raw": ...}``
        ``{"kind": "close",   "time": 1364.723, "raw": <строка>}``

    Парсер помнит состояние экрана (открыт/закрыт), поэтому повторные маркеры
    открытия и закрытия не дублируются. Повторно прочитанные после усечения
    файла строки с дропом отбрасываются по паре (время, путь).
    """

    def __init__(self, dedupe=True):
        self.screen_open = False
        self.dedupe = bool(dedupe)
        self._seen = set()
        self._recent = collections.deque(maxlen=512)

    # ── дедупликация ───────────────────────────────────────────────────
    def _duplicate(self, key):
        if not self.dedupe:
            return False
        if key in self._seen:
            return True
        if len(self._seen) >= 4000:      # не растём бесконечно
            self._seen = set(self._recent)
        self._seen.add(key)
        self._recent.append(key)
        return False

    # ── одна строка ────────────────────────────────────────────────────
    def parse_line(self, line):
        if not line:
            return None
        text = line.rstrip("\r\n")
        if not text.strip():
            return None
        text = text.lstrip("\ufeff")
        ev = {"time": line_time(text), "raw": text}

        # 1) главный маркер: путь награды собственного игрока
        m = _RE_DROP.search(text)
        player_id, path = (m.group(1), m.group(2)) if m else (None, None)
        if not path:
            m2 = _RE_DROP_PATH.search(text)
            if m2:
                path = m2.group(1)
        if path:
            path = path.strip().rstrip(",;")
            if not path:
                return None
            if self._duplicate((ev["time"], path)):
                return None
            ev.update({"kind": "drop", "player_id": player_id,
                       "path": path, "game_ref": path_to_gameref(path)})
            self.screen_open = True
            return ev

        # 2) открытие экрана реликвии (несколько маркеров об одном и том же)
        if any(mark in text for mark in _OPEN_MARKERS):
            if self.screen_open:
                return None
            self.screen_open = True
            ev["kind"] = "open"
            return ev

        # 3) экран заполнен карточками
        if any(mark in text for mark in _REWARDS_MARKERS):
            ev["kind"] = "rewards"
            return ev

        # 4) закрытие экрана
        if any(mark in text for mark in _CLOSE_MARKERS):
            if not self.screen_open:
                return None
            self.screen_open = False
            ev["kind"] = "close"
            return ev

        return None

    # ── пачка строк ────────────────────────────────────────────────────
    def parse_lines(self, lines):
        out = []
        for line in lines or []:
            ev = self.parse_line(line)
            if ev:
                out.append(ev)
        return out


def parse_line(line):
    """Разбор одной строки без сохранения состояния (удобно в тестах)."""
    return LogParser().parse_line(line)


def parse_lines(lines):
    """Разбор списка строк: события реликвии по порядку."""
    return LogParser().parse_lines(lines)


# ─────────────────────────────────────────────────────────────────────────
#  Фоновое чтение хвоста лога
# ─────────────────────────────────────────────────────────────────────────
class EeLogTailer(threading.Thread):
    """Фоновый «хвост» EE.log. Файл открывается только на чтение.

    Параметры
    ---------
    path : str
        Путь к EE.log (может ещё не существовать).
    on_event : callable(event) | None
        Вызывается в потоке хвоста для каждого распознанного события.
    poll_ms : int
        Интервал опроса файла, мс.
    start_at_end : bool
        ``True`` (по умолчанию) — начать с конца файла: в журнал попадут только
        реликвии, открытые после запуска программы. ``False`` — прочитать файл
        с начала (используется тестами).
    log_func : callable(str) | None
        Куда писать служебные сообщения (по умолчанию ``print``).
    """

    def __init__(self, path, on_event=None, poll_ms=DEFAULT_POLL_MS,
                 start_at_end=True, log_func=None):
        super().__init__(daemon=True, name="wf-eelog")
        self.path = path or ""
        self.on_event = on_event
        try:
            self.poll_ms = max(100, int(poll_ms or DEFAULT_POLL_MS))
        except Exception:
            self.poll_ms = DEFAULT_POLL_MS
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._start_pending = bool(start_at_end)
        self._offset = 0          # сколько байт файла уже прочитано
        self._buf = b""           # недописанная (неполная) последняя строка
        self.parser = LogParser()
        self.drops = 0
        self.errors = 0
        self._log = log_func or (lambda msg="": print(msg, flush=True))

    # ── управление ─────────────────────────────────────────────────────
    def stop(self):
        self._stop_event.set()

    def status(self):
        """Диагностика: offset, буфер, счётчики (для консоли и тестов)."""
        with self._lock:
            offset, buf = self._offset, self._buf
        try:
            exists = os.path.isfile(self.path)
        except Exception:
            exists = False
        return {"path": self.path, "exists": exists, "offset": offset,
                "buffer_bytes": len(buf), "drops": self.drops,
                "errors": self.errors, "running": self.is_alive()}

    # ── чтение ─────────────────────────────────────────────────────────
    def read_new_events(self):
        """Читает новые байты и возвращает список событий (без вызова callback)."""
        events = []
        if not self.path:
            return events
        try:
            size = os.path.getsize(self.path)
        except OSError:
            # лога нет (игра не запущена) — это нормально, не падаем
            return events
        with self._lock:
            if self._start_pending:
                self._start_pending = False
                self._offset = size          # старые строки не переигрываем
                self._buf = b""
                return events
            if size < self._offset:
                # файл усечён (новый запуск игры) — читаем заново с начала
                self._offset = 0
                self._buf = b""
                self.parser.screen_open = False
                self._log("EE.log: файл пересоздан (новый запуск игры) — читаю с начала")
            chunk = b""
            if size > self._offset:
                try:
                    # ВАЖНО: только чтение, лог игры не меняем
                    with open(self.path, "rb") as f:
                        f.seek(self._offset)
                        chunk = f.read()
                except OSError as e:
                    self.errors += 1
                    if self.errors <= 3:
                        self._log(f"EE.log: не читается ({e}) — попробую позже")
                    return events
                self._offset = min(size, self._offset + len(chunk))
            data = self._buf + chunk
            parts = data.split(b"\n")
            self._buf = parts.pop()          # неполный хвост — до следующего раза
        for raw in parts:
            if not raw:
                continue
            try:
                line = raw.decode("utf-8", "replace")
            except Exception:
                continue
            try:
                ev = self.parser.parse_line(line)
            except Exception as e:          # строка любого вида не должна ронять поток
                self.errors += 1
                if self.errors <= 3:
                    self._log(f"EE.log: строка не разобрана ({e})")
                ev = None
            if ev:
                events.append(ev)
        return events

    def poll_once(self):
        """Один цикл: прочитать новые события и раздать их обработчику."""
        events = self.read_new_events()
        for ev in events:
            self._dispatch(ev)
        return events

    def _dispatch(self, ev):
        if ev.get("kind") == "drop":
            self.drops += 1
        cb = self.on_event
        if cb is None:
            return
        try:
            cb(ev)
        except Exception as e:
            self.errors += 1
            if self.errors <= 3:
                self._log(f"EE.log: обработчик события упал ({e})")

    # ── поток ──────────────────────────────────────────────────────────
    def run(self):
        interval = self.poll_ms / 1000.0
        missing = False
        while not self._stop_event.is_set():
            try:
                if not self.path or not os.path.isfile(self.path):
                    if not missing:
                        missing = True
                        self._log(f"EE.log не найден: {self.path or '—'} — жду запуск игры")
                else:
                    if missing:
                        missing = False
                        self._log("EE.log найден — читаю новые строки")
                    self.poll_once()
            except Exception as e:          # поток не должен умирать
                self.errors += 1
                if self.errors <= 3:
                    self._log(f"EE.log: сбой чтения ({type(e).__name__}: {e})")
            self._stop_event.wait(interval)
