# -*- coding: utf-8 -*-
"""Сборщик статистики фарма из EE.log — ТОЛЬКО ЧТЕНИЕ.

Модуль для ``wf_ru/warframe.py``. Полное описание — ``wf_ru/FARM_STATS_SPEC.md``.

Что делает
----------
1. Читает игровой лог ``EE.log`` своим «хвостом» (``log_parser.EeLogTailer``)
   с ``start_at_end=False``: при старте программы дочитывает текущую сессию
   с начала файла, дальше — только новые строки. Файл открывается только на
   чтение (см. ``log_parser.py``).
2. Ведёт историю сессий фарма в ``farm_stats.json`` рядом с ``config.json``:
   открыто реликвий (своих), суммарная стоимость, лучший дроп, средний доход
   в час. Файл переживает перезапуск программы.
3. Цены получаются лениво через инъектированную ``price_fn``; отсутствие цены
   (офлайн/нет слага) не роняет сборщик — дроп учитывается в счётчике реликвий,
   но не в сумме стоимости.

Модуль самодостаточен: не импортирует ``warframe.py``. Зависимость от
``log_parser.py`` — ленивая (тот же приём, что в ``warframe.log_parser_module``),
чтобы работать и из исходника, и из собранного exe.

Сессия — один запуск игры (файл ``EE.log`` усекается при каждом запуске).
База времени «среднего в час» — от старта клиента: ``started_utc`` берётся из
строки заголовка ``Current time: … [UTC: …]`` (факт из VERIFY_REPORT_FARMSTATS2.md),
а НЕ из ``st_ctime`` (на Windows CreationTime не меняется при усечении файла).
Идентичность сессии = ``(log_path, started_utc)``; новая игра распознаётся по
смене ``Current time``, уменьшению размера файла или откату ``rel_time`` назад.
"""
import calendar
import copy
import json
import os
import queue
import re
import threading
import time
import uuid

SCHEMA = 1

#: строка заголовка: ``… Current time: … [UTC: Fri Nov 20 17:22:42 2026]``
_RE_CURRENT_TIME = re.compile(r"Current time:.*\[UTC:\s*([^\]]+)\]")

_LOG_PARSER = {"mod": None, "tried": False}


def _parse_utc(text):
    """``'Fri Nov 20 17:22:42 2026'`` → UNIX-время (UTC) или ``None``."""
    try:
        t = time.strptime(str(text or "").strip(), "%a %b %d %H:%M:%S %Y")
        return float(calendar.timegm(t))
    except Exception:
        return None


def _read_log_start_utc(path):
    """Старт игровой сессии из заголовка EE.log (UTC) или ``None``.

    Читает только первые байты файла (заголовок), файл открывается только на
    чтение. ``st_ctime`` не используется — на Windows CreationTime не меняется
    при усечении файла на месте.
    """
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return None
    try:
        text = head.decode("utf-8", "replace")
    except Exception:
        return None
    for line in text.splitlines():
        m = _RE_CURRENT_TIME.search(line)
        if m:
            utc = _parse_utc(m.group(1))
            if utc is not None:
                return utc
    return None


def _load_log_parser():
    """Подключает ``log_parser.py`` рядом с этим модулем (кэшируя результат).

    ``log_parser`` не импортируется на уровне модуля: в собранном exe папка
    ``app`` не лежит в ``sys.path``, а ``warframe.py`` грузит его отдельным
    файлом и кладёт в ``sys.modules``. Здесь тот же запасной путь — грузим
    ``log_parser.py`` напрямую по ``__file__``.
    """
    if _LOG_PARSER["tried"]:
        return _LOG_PARSER["mod"]
    _LOG_PARSER["tried"] = True
    try:
        import log_parser as mod  # noqa: F401
        _LOG_PARSER["mod"] = mod
        return mod
    except Exception:
        pass
    try:
        import importlib.util
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(here, "log_parser.py")
        if os.path.isfile(path):
            spec = importlib.util.spec_from_file_location("wf_farm_log_parser", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _LOG_PARSER["mod"] = mod
            return mod
    except Exception:
        pass
    return None


def _to_int(value):
    """Число → int|None; 0 и не-числа считаем «нет цены»."""
    if value is None:
        return None
    try:
        value = int(float(value))
    except Exception:
        return None
    return value if value > 0 else None


def _to_float(value):
    try:
        return float(value)
    except Exception:
        return None


class FarmStatsCollector:
    """Собирает сессии фарма и агрегаты; пишет ``farm_stats.json`` атомарно.

    :param cfg: настройки (``farm_stats_file``, ``farm_stats_max_sessions``,
        ``log_poll_ms``, ``log_path`` …).
    :param price_fn: ``callable(slug) -> dict|None`` — ``{"price": int|None,
        "name": str, "ducats": int}`` (обёртка над ``MarketAPI.lookup_slug``).
    :param slug_fn: ``callable(path) -> str|None`` — маппинг пути EE.log в слаг
        warframe.market (``log_parser.slug_from_path(path, api)``).
    :param clock: подменяемое время (тесты).
    :param logger: куда писать служебные сообщения.
    :param async_prices: ``True`` — цены тянутся в фоновом потоке (прод);
        ``False`` — синхронно (детерминированные тесты).
    :param data_dir: папка для ``farm_stats.json`` (по умолчанию WF_DATA_DIR
        либо папка модуля).
    """

    def __init__(self, cfg, price_fn=None, slug_fn=None, clock=time.time,
                 logger=print, async_prices=True, data_dir=None,
                 on_change=None):
        self.cfg = dict(cfg or {})
        self.price_fn = price_fn
        self.slug_fn = slug_fn
        self.clock = clock or time.time
        self.logger = logger or (lambda *a, **k: None)
        self.async_prices = bool(async_prices)
        self.on_change = on_change
        self.data_dir = (data_dir or os.environ.get("WF_DATA_DIR")
                         or os.path.dirname(os.path.abspath(__file__)))
        self._lock = threading.RLock()
        self._queue = queue.Queue()
        self._worker = None
        self._stop = threading.Event()
        self._tailer = None
        self.path = None
        self.data = self._empty()
        self._cur_max_rel = None
        self._cur_keys = set()
        self._replaying = False     # идёт повторное чтение уже учтённой сессии
        self._last_file_size = None # для детекта in-place truncate
        self._app_session_started = False
        self._open_keys = set()      # дедуп повторных событий «open»
        self.load()

    # ── файл ────────────────────────────────────────────────────────────
    def stats_path(self):
        name = str(self.cfg.get("farm_stats_file") or "farm_stats.json").strip()
        if not name:
            name = "farm_stats.json"
        return os.path.join(self.data_dir, name)

    @staticmethod
    def _empty():
        return {"schema": SCHEMA, "updated_utc": 0.0, "sessions": [],
                # 1.0.0: счётчики реликвий (EE.log) — total не сбрасывается
                "relics_opened_total": 0, "relics_opened_session": 0,
                "last_relic_at": None}

    def load(self):
        """Читает ``farm_stats.json`` (или пустую структуру)."""
        path = self.stats_path()
        try:
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    with self._lock:
                        self.data = self._normalize(d)
        except Exception as e:
            self.logger(f"farm_stats: история не прочитана ({type(e).__name__}: {e})")
        return dict(self.data)

    @staticmethod
    def _normalize(d):
        """Толерантная нормализация прочитанного JSON."""
        out = {"schema": int(d.get("schema", SCHEMA) or SCHEMA),
               "updated_utc": _to_float(d.get("updated_utc")) or 0.0,
               "sessions": []}
        for s in (d.get("sessions") or []):
            if not isinstance(s, dict):
                continue
            drops = []
            for drop in (s.get("drops") or []):
                if not isinstance(drop, dict):
                    continue
                drops.append({
                    "rel_time": _to_float(drop.get("rel_time")),
                    "utc": _to_float(drop.get("utc")),
                    "path": str(drop.get("path") or ""),
                    "slug": drop.get("slug") or None,
                    "name": drop.get("name"),
                    "price": _to_int(drop.get("price")),
                    "priced": bool(drop.get("priced")),
                })
                if "ducats" in drop:
                    drops[-1]["ducats"] = drop.get("ducats")
                # «взято: …» — отметка Alt+1…4 (может отсутствовать)
                for key in ("picked_name", "picked_price", "picked_index",
                            "picked_utc"):
                    if key in drop:
                        drops[-1][key] = drop.get(key)
            bd = s.get("best_drop")
            best = None
            if isinstance(bd, dict) and bd.get("price") is not None:
                best = {
                    "rel_time": _to_float(bd.get("rel_time")),
                    "utc": _to_float(bd.get("utc")),
                    "path": str(bd.get("path") or ""),
                    "slug": bd.get("slug") or None,
                    "name": bd.get("name"),
                    "price": _to_int(bd.get("price")),
                }
            # старт сессии: валиден только из заголовка лога («log_header»)
            # или fallback clock («clock»); унаследованные сессии со st_ctime
            # (нет anchor_source) помечаются «unknown» и не входят в «доход в час».
            anchor_source = s.get("anchor_source")
            started_utc = _to_float(s.get("started_utc"))
            if anchor_source not in ("log_header", "clock"):
                anchor_source = "unknown"
                started_utc = None
            out["sessions"].append({
                "id": str(s.get("id") or ""),
                "log_path": str(s.get("log_path") or ""),
                "started_utc": started_utc,
                "anchor_source": anchor_source,
                "ended_utc": _to_float(s.get("ended_utc")),
                "last_rel_time": _to_float(s.get("last_rel_time")),
                "last_file_size": _to_int(s.get("last_file_size")),
                "relics_opened": int(s.get("relics_opened") or 0),
                "screens_opened": int(s.get("screens_opened") or 0),
                "total_value": int(s.get("total_value") or 0),
                "priced_drops": int(s.get("priced_drops") or 0),
                "best_drop": best,
                "drops": drops,
                "marks": [],
                "picks": [],
            })
            for mark in (s.get("marks") or []):
                if not isinstance(mark, dict):
                    continue
                name = str(mark.get("name") or mark.get("picked_name") or "")
                if not name:
                    continue
                out["sessions"][-1]["marks"].append({
                    "name": name,
                    "price": _to_int(mark.get("price")
                                     or mark.get("picked_price")),
                    "slug": str(mark.get("slug") or ""),
                    "at": _to_float(mark.get("at")
                                    or mark.get("picked_utc")),
                })
            sess = out["sessions"][-1]
            # t77: новые picks; старые marks/picked_* мигрируем один раз
            for pick in (s.get("picks") or []):
                if not isinstance(pick, dict):
                    continue
                pname = str(pick.get("name") or "").strip()
                if not pname:
                    continue
                sess["picks"].append({
                    "name": pname,
                    "price": _to_int(pick.get("price")),
                    "slug": str(pick.get("slug") or ""),
                    "at": _to_float(pick.get("at")) or 0.0,
                    "index": _to_int(pick.get("index")),
                    "tradable": pick.get("tradable"),
                })
            if not sess["picks"]:
                for mark in sess["marks"]:
                    sess["picks"].append({
                        "name": mark["name"], "price": mark["price"],
                        "slug": mark["slug"], "at": mark["at"],
                        "index": _to_int(mark.get("index")),
                        "tradable": mark.get("tradable"),
                    })
                for drop in drops:
                    pname = str(drop.get("picked_name") or "").strip()
                    if not pname:
                        continue
                    entry = {
                        "name": pname,
                        "price": _to_int(drop.get("picked_price")),
                        "slug": str(drop.get("slug") or ""),
                        "at": (_to_float(drop.get("picked_utc"))
                               or _to_float(drop.get("utc"))
                               or 0.0),
                        "index": _to_int(drop.get("picked_index")),
                        "tradable": drop.get("picked_tradable"),
                    }
                    if not any(
                            str(x.get("name")) == entry["name"]
                            and _to_int(x.get("price")) == entry["price"]
                            and _to_int(x.get("index")) == entry["index"]
                            and abs((_to_float(x.get("at")) or 0.0)
                                    - entry["at"]) <= 10.0
                            for x in sess["picks"]):
                        sess["picks"].append(entry)
        # 1.0.0: счётчики открытий реликвий (EE.log).
        # У старых файлов этих полей нет — total восстанавливаем из сессий,
        # чтобы накопительный счётчик не обнулился при обновлении.
        total = _to_int(d.get("relics_opened_total"))
        if total is None:
            total = 0
        if not total:
            opens = sum(int(s.get("screens_opened") or 0)
                        for s in out["sessions"])
            if opens:
                total = opens
            else:
                total = sum(int(s.get("relics_opened") or 0)
                            for s in out["sessions"])
        out["relics_opened_total"] = max(0, int(total))
        session = _to_int(d.get("relics_opened_session"))
        if session is None:
            last = out["sessions"][-1] if out["sessions"] else None
            session = (int(last.get("screens_opened") or 0)
                       if last and last.get("ended_utc") is None else 0)
        out["relics_opened_session"] = max(0, int(session))
        out["last_relic_at"] = _to_float(d.get("last_relic_at")) or None
        return out

    def save(self):
        """Атомарная запись ``farm_stats.json`` (tmp + os.replace)."""
        path = self.stats_path()
        with self._lock:
            payload = self._payload_locked()
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            return True
        except Exception as e:
            self.logger(f"farm_stats: не сохранён ({type(e).__name__}: {e})")
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            return False

    def _notify(self):
        """Сообщить UI, что данные изменились (только по событиям, не таймер)."""
        if not callable(self.on_change):
            return False
        try:
            self.on_change()
            return True
        except Exception:
            return False

    # ── счётчики открытий реликвий (1.0.0) ────────────────────────────
    def _event_utc_locked(self, sess, rel):
        started = sess.get("started_utc") if isinstance(sess, dict) else None
        if isinstance(started, (int, float)) and started and rel is not None:
            return float(started) + float(rel)
        try:
            return float(self.clock())
        except Exception:
            return time.time()

    def _payload_locked(self):
        now = self.clock()
        sessions = []
        for s in self.data["sessions"]:
            self._recompute_session_locked(s)
            s["marks"] = self._marks_of_session(s)
            sessions.append(copy.deepcopy(s))
        sessions = self._rotate_locked(sessions)
        payload = {"schema": SCHEMA, "updated_utc": now, "sessions": sessions}
        # 1.0.0: накопительные счётчики открытий реликвий
        payload["relics_opened_total"] = int(
            self.data.get("relics_opened_total") or 0)
        payload["relics_opened_session"] = int(
            self.data.get("relics_opened_session") or 0)
        payload["last_relic_at"] = self.data.get("last_relic_at")
        return payload

    def _rotate_locked(self, sessions):
        try:
            max_n = int(self.cfg.get("farm_stats_max_sessions", 200) or 200)
        except Exception:
            max_n = 200
        if max_n < 1:
            max_n = 200
        if len(sessions) <= max_n:
            return sessions
        return sessions[-max_n:]

    # ── поток/хвост ─────────────────────────────────────────────────────
    def attach(self, path=None):
        """Привязывает сборщик к конкретному ``EE.log`` без запуска хвоста.

        Идентичность игровой сессии = ``(log_path, started_utc)``, где
        ``started_utc`` — из заголовка ``Current time: … [UTC: …]``. Повторное
        чтение того же лога возобновляет ту же сессию; новая игра (смена
        ``Current time`` / усечение файла) открывает новую сессию.
        """
        logp = _load_log_parser()
        if logp is None:
            self.logger("farm_stats: модуль log_parser не найден — статистика выключена")
            return False
        if not path:
            try:
                path = logp.resolve_log_path(self.cfg)
            except Exception as e:
                self.logger(f"farm_stats: путь к логу не определён ({e})")
                return False
        if not path:
            self.logger("farm_stats: путь к EE.log не задан — статистика выключена")
            return False
        self.path = path
        self._last_file_size = self._file_size()
        with self._lock:
            self._resume_active_locked()
        self._requeue_unpriced()
        return True

    def start(self):
        """Запускает свой ``EeLogTailer`` (читает файл с начала). Возвращает
        поток или ``None`` (нет лога/модуля — статистика просто выключена)."""
        if not self.attach():
            return None
        # 1.0.0 (t35): каждый запуск приложения — новый сеанс; предыдущий
        # закрывается (ended_utc) и остаётся в истории.
        if bool(self.cfg.get("farm_stats_new_session_on_start", True)):
            with self._lock:
                self._begin_app_session_locked()
        logp = _load_log_parser()
        try:
            poll_ms = int(self.cfg.get("log_poll_ms", 500) or 500)
        except Exception:
            poll_ms = 500
        try:
            # 1.0.0 (t35): новый сеанс на запуск — не перечитываем старый лог
            start_at_end = bool(self.cfg.get(
                "farm_stats_new_session_on_start", True))
            tailer = logp.EeLogTailer(self.path, on_event=self.on_event,
                                      poll_ms=poll_ms,
                                      start_at_end=start_at_end,
                                      log_func=self.logger)
            tailer.start()
            self._tailer = tailer
            return tailer
        except Exception as e:
            self.logger(f"farm_stats: хвост лога не запущен ({type(e).__name__}: {e})")
            return None

    def stop(self):
        """Закрывает активную сессию и сохраняет историю.

        Цены в фоновом режиме дочитываются без блокировки: ``stop()`` не ждёт
        сеть (см. FARM_STATS_SPEC.md, риск 8) — недочитанные дропы останутся с
        ``price=None``, как и при отсутствии сети.
        """
        self._stop.set()
        tailer = self._tailer
        if tailer is not None:
            try:
                tailer.stop()
            except Exception:
                pass
        if not self.async_prices:
            # синхронный режим (тесты): очередь резолвится детерминированно
            self.resolve_pending()
        try:
            self._queue.put(None)
        except Exception:
            pass
        with self._lock:
            cur = self._current_session_locked()
            if cur is not None:
                cur["ended_utc"] = float(self.clock())
        self.save()

    def reset(self):
        """Сбросить ТОЛЬКО текущий сеанс (лут и счётчик).

        1.0.0 (t35): история прошлых сеансов и ``relics_opened_total``
        сохраняются — «Сбросить» не удаляет прошлое.
        """
        with self._lock:
            cur = self._current_session_locked()
            if cur is not None:
                cur["drops"] = []
                cur["screens_opened"] = 0
                cur["relics_opened"] = 0
                cur["total_value"] = 0
                cur["priced_drops"] = 0
                cur["best_drop"] = None
                cur["last_rel_time"] = None
                cur["marks"] = []
                cur["picks"] = []
            self.data["relics_opened_session"] = 0
            self.data["last_relic_at"] = None
            self._cur_keys = set()
            self._cur_max_rel = None
            self._open_keys = set()
        self.save()
        self._notify()
        return True

    # ── события ─────────────────────────────────────────────────────────
    def on_event(self, ev):
        """Обработчик событий ``EeLogTailer`` (вызывается в потоке хвоста)."""
        try:
            kind = (ev or {}).get("kind")
            if kind == "drop":
                self._handle_drop(ev)
            elif kind == "open":
                self._handle_open(ev)
            # rewards/close для агрегатов не нужны — игнорируем
        except Exception as e:
            self.logger(f"farm_stats: событие не обработано ({type(e).__name__}: {e})")

    def _handle_drop(self, ev):
        path = str((ev or {}).get("path") or "").strip()
        if not path:
            return
        rel = _to_float(ev.get("time"))
        with self._lock:
            sess = self._ensure_session_locked(rel)
            if self._replay_skip_locked(rel):
                return
            key = (rel, path)
            if key in self._cur_keys:
                return
            slug = None
            if self.slug_fn is not None:
                try:
                    slug = self.slug_fn(path)
                except Exception:
                    slug = None
            name = None
            logp = _load_log_parser()
            if logp is not None:
                try:
                    name = logp.display_name_from_path(path)
                except Exception:
                    name = None
            drop = {
                "rel_time": rel,
                "utc": (sess["started_utc"] + rel) if (sess.get("started_utc")
                                                       and rel is not None) else None,
                "path": path,
                "slug": slug or None,
                "name": name or (slug or path.rsplit("/", 1)[-1]),
                "price": None,
                "priced": False,
            }
            sess["drops"].append(drop)
            self._cur_keys.add(key)
            self._recompute_session_locked(sess)
        self._request_price(drop)
        self.save()
        self._notify()

    def _handle_open(self, ev):
        rel = _to_float(ev.get("time"))
        with self._lock:
            # 1.0.0 (t35): один и тот же экран не должен давать +2
            key = (rel, str(ev.get("path") or ev.get("screen") or ""))
            if key in self._open_keys:
                return
            self._open_keys.add(key)
            if len(self._open_keys) > 5000:
                self._open_keys = set(list(self._open_keys)[-2000:])
            sess = self._ensure_session_locked(rel)
            if self._replay_skip_locked(rel):
                return
            sess["screens_opened"] = int(sess.get("screens_opened") or 0) + 1
            utc = self._event_utc_locked(sess, rel)
            sess["last_relic_at"] = utc
            # 1.0.0: «Открыто реликвий» — сессия и накопительный total
            self.data["relics_opened_total"] = int(
                self.data.get("relics_opened_total") or 0) + 1
            self.data["relics_opened_session"] = int(
                self.data.get("relics_opened_session") or 0) + 1
            self.data["last_relic_at"] = utc
        self.save()
        self._notify()

    def _add_pick_locked(self, sess, name, price=None, index=None,
                         slug="", at=None, tradable=None):
        """Добавить отметку в ``session['picks']`` (дедуп 10 с)."""
        name = str(name or "").strip()
        if not name:
            return None
        now = float(at if at is not None else self.clock())
        price = _to_int(price)
        index = _to_int(index)
        picks = sess.setdefault("picks", [])
        for old in reversed(picks):
            if (str(old.get("name") or "") == name
                    and _to_int(old.get("price")) == price
                    and _to_int(old.get("index")) == index
                    and abs(now - (_to_float(old.get("at")) or 0.0)) <= 10.0):
                return old
        pick = {
            "name": name,
            "price": price,
            "slug": str(slug or ""),
            "at": now,
            "index": index,
            "tradable": (None if tradable is None else bool(tradable)),
        }
        picks.append(pick)
        self.logger(f"Дроп: отметка #{index if index is not None else 0} "
                    f"«{name}» — {price if price is not None else '—'}p "
                    f"(записей в лут: {len(picks)})")
        return pick

    def mark_picked(self, name, price=None, index=None, tradable=None):
        """Alt+1…4: записать отметку ТОЛЬКО в ``session['picks']``."""
        name = str(name or "").strip()
        if not name:
            return False
        name = self._canonical(name)
        with self._lock:
            sess = self._current_session_locked()
            if sess is None:
                sessions = self.data.get("sessions") or []
                sess = sessions[-1] if sessions else None
            if sess is None:
                return False
            self._add_pick_locked(sess, name, price=price, index=index,
                                  at=self.clock(), tradable=tradable)
            self._recompute_session_locked(sess)
        self.save()
        self._notify()
        return True

    def record_pick(self, name, price=None, index=None, slug=None,
                    ts=None, tradable=None):
        """Записать отметку Alt+1…4 (только picks, без логового дропа)."""
        name = str(name or "").strip()
        if not name:
            return False
        name = self._canonical(name, str(slug or ""))
        now = float(ts if ts is not None else self.clock())
        with self._lock:
            sess = self._current_session_locked()
            if sess is None:
                sess = self._new_session_locked(now)
            self._add_pick_locked(sess, name, price=price, index=index,
                                  slug=slug, at=now, tradable=tradable)
            self._recompute_session_locked(sess)
        self.save()
        self._notify()
        return True

    def picked_loot(self, limit=200):
        """Взятые предметы (Alt+1…4) из picks, новые сверху."""
        out = []
        order = 0
        with self._lock:
            for sess in self.data.get("sessions") or []:
                started = sess.get("started_utc")
                for pick in sess.get("picks") or []:
                    name = str(pick.get("name") or "").strip()
                    if not name:
                        continue
                    order += 1
                    at = (_to_float(pick.get("at")) or started or 0.0)
                    out.append({
                        "name": name,
                        "picked_name": name,
                        "picked_price": _to_int(pick.get("price")),
                        "picked_utc": at,
                        "picked_index": _to_int(pick.get("index")),
                        "slug": str(pick.get("slug") or ""),
                        "tradable": pick.get("tradable"),
                        "picked_tradable": pick.get("tradable"),
                        "session_started_utc": started,
                        "_order": order,
                    })
        out.sort(key=lambda d: (d.get("picked_utc") or 0.0,
                                d.get("_order") or 0), reverse=True)
        for item in out:
            item.pop("_order", None)
        return out[:max(1, int(limit))]

    def picked_stats(self):
        """Агрегаты «Дропа» по взятому луту (не по всем EE.log дропам)."""
        loot = self.picked_loot(100000)
        total_value = sum(d.get("picked_price") or 0 for d in loot)
        best = None
        for d in loot:
            price = d.get("picked_price")
            if price is None:
                continue
            if best is None or price > (best.get("price") or 0):
                best = {"name": d.get("picked_name"), "price": price,
                        "utc": d.get("picked_utc"),
                        "picked_utc": d.get("picked_utc"),
                        "slug": d.get("slug")}
        base = self.stats()
        with self._lock:
            sessions = self.data.get("sessions") or []
            current_id = (sessions[-1].get("id") if sessions
                          and sessions[-1].get("ended_utc") is None else None)
        current = [d for d in loot
                   if self._session_id_for_started(
                       d.get("session_started_utc")) == current_id]
        return {
            "relics_opened": len(loot), "total_value": int(total_value),
            "best_drop": best,
            "avg_per_hour": (total_value / base.get("hours", 0.0)
                             if base.get("hours", 0.0) >= 0.05 else 0.0),
            "hours": base.get("hours", 0.0),
            "sessions": base.get("sessions", 0),
            "current_relics": len(current),
            "current_active": base.get("current_active", False),
            "priced_drops": sum(1 for d in loot
                                if d.get("picked_price") is not None),
            "loot": loot,
        }

    def drop_stats(self):
        """Компактные показатели «Дропа» для 4 карточек (1.0.0).

        Возвращает:
          * ``relics_total``/``relics_session`` — открыто реликвий (EE.log);
          * ``last_relic_at`` — время последней реликвии (UTC) или ``None``;
          * ``last_pick`` — предмет из ПОСЛЕДНЕЙ отметки Alt+1…4
            (``picked_name``/``picked_price``/``picked_utc``) или ``None``;
          * ``selected_count``/``selected_sum`` — выбранное (Alt+1…4);
          * ``avg_per_hour``/``hours`` — средний доход (hours<0.05 → 0);
          * ``log_available``/``log_path`` — доступен ли EE.log.

        Наград из EE.log в UI не показываем: карточка «Последний дроп»
        строится только по отметкам Alt+1…4 (записи ``record_pick``/сессии).
        """
        base = self.picked_stats()
        last_pick = self.picked_loot(1)
        with self._lock:
            total = int(self.data.get("relics_opened_total") or 0)
            session = int(self.data.get("relics_opened_session") or 0)
            last_relic_at = _to_float(self.data.get("last_relic_at"))
        return {
            "relics_total": total,
            "relics_session": session,
            "last_relic_at": last_relic_at,
            "last_pick": last_pick[0] if last_pick else None,
            "selected_count": int(base.get("relics_opened", 0) or 0),
            "selected_sum": int(base.get("total_value", 0) or 0),
            "avg_per_hour": float(base.get("avg_per_hour", 0.0) or 0.0),
            "hours": float(base.get("hours", 0.0) or 0.0),
            "log_available": bool(self.path),
            "log_path": str(self.path or ""),
        }

    def _session_id_for_started(self, started):
        try:
            for sess in self.data.get("sessions") or []:
                if sess.get("started_utc") == started:
                    return sess.get("id")
        except Exception:
            pass
        return None

    # ── сессии ──────────────────────────────────────────────────────────
    def _current_session_locked(self):
        sessions = self.data.get("sessions") or []
        if not sessions:
            return None
        last = sessions[-1]
        if last.get("ended_utc") is None:
            return last
        return None

    def _resume_active_locked(self):
        """После привязки к логу: продолжить ту же игровую сессию или начать новую.

        Идентичность игровой сессии = ``log_path`` + ``started_utc`` (UTC из
        заголовка ``Current time: … [UTC: …]``). Если в истории есть сессия с тем
        же путём и тем же якорем (даже закрытая предыдущим ``stop()``), она
        открывается заново и дропы из неё восстанавливаются в ключи
        дедупликации — повторный ``replay`` того же файла не дублирует их.
        Если якорь сменился (игра запущена заново), старая активная сессия
        закрывается и начинается новая.
        """
        anchor = self._session_start_anchor()
        sessions = self.data.get("sessions") or []
        # последняя сессия того же лога (если есть)
        idx = None
        for i in range(len(sessions) - 1, -1, -1):
            if str(sessions[i].get("log_path") or "") == str(self.path or ""):
                idx = i
                break
        cur = sessions[idx] if idx is not None else None
        if cur is not None and self._same_anchor(cur.get("started_utc"), anchor):
            # Тот же заголовок, но файл стал меньше сохранённого размера → это
            # новая игра после in-place truncate (F2 в VERIFY_REPORT_FARMSTATS3.md).
            # Не возобновляем: ниже закроем старую и начнём новую сессию.
            saved_size = cur.get("last_file_size")
            cur_size = self._file_size()
            if saved_size is None or cur_size is None or cur_size < saved_size:
                active = self._current_session_locked()
                if active is not None:
                    active["ended_utc"] = float(self.clock())
                self._cur_max_rel = None
                self._cur_keys = set()
                self._replaying = False
                return
            # та же игровая сессия — продолжаем её (закрытую stop() открываем)
            cur["ended_utc"] = None
            self._restore_session_state(cur)
            self._replaying = True      # дальше хвост прочитает файл с начала
            return
        # иная игровая сессия: закрываем старую активную, начнём новую
        active = self._current_session_locked()
        if active is not None:
            active["ended_utc"] = float(self.clock())
        self._cur_max_rel = None
        self._cur_keys = set()
        self._replaying = False

    @staticmethod
    def _canonical(name, slug=""):
        try:
            import display_name
            return display_name.canonical_name(slug or "", name or "")
        except Exception:
            return str(name or "")

    # ── 1.0.0 (t35): сеансы «Дропа» ────────────────────────────────────
    HISTORY_SESSIONS = 5

    def _trim_history_locked(self):
        """Оставить не более 5 завершённых сеансов + текущий."""
        sessions = self.data.get("sessions") or []
        completed = [s for s in sessions if s.get("ended_utc") is not None]
        if len(completed) <= self.HISTORY_SESSIONS:
            return
        keep = set(id(s) for s in completed[-self.HISTORY_SESSIONS:])
        self.data["sessions"] = [
            s for s in sessions
            if s.get("ended_utc") is None or id(s) in keep]
        self._trim_history_locked()

    def _begin_app_session_locked(self):
        """Закрыть предыдущий сеанс и начать новый (запуск приложения)."""
        if self._app_session_started:
            return self._current_session_locked()
        self._app_session_started = True
        sessions = self.data.setdefault("sessions", [])
        active = self._current_session_locked()
        if active is not None:
            active["ended_utc"] = float(self.clock())
        self._trim_history_locked()
        now = float(self.clock())
        sess = {
            "id": self._new_id(now),
            "log_path": self.path or "",
            "started_utc": now,
            "anchor_source": "clock",
            "ended_utc": None,
            "last_rel_time": None,
            "last_file_size": self._file_size(),
            "relics_opened": 0,
            "screens_opened": 0,
            "total_value": 0,
            "priced_drops": 0,
            "best_drop": None,
            "drops": [],
            "marks": [],
            "picks": [],
        }
        sessions.append(sess)
        self.data["relics_opened_session"] = 0
        self.data["last_relic_at"] = None
        self._cur_max_rel = None
        self._cur_keys = set()
        self._open_keys = set()
        self._replaying = False
        return sess

    def _marks_of_session(self, sess):
        """Отметки Alt+1…4: только ``session['picks']`` (миграция старых)."""
        picks = []
        started = _to_float(sess.get("started_utc")) or 0.0
        for pick in sess.get("picks") or []:
            name = str(pick.get("name") or "").strip()
            if not name:
                continue
            picks.append({
                "name": name,
                "price": _to_int(pick.get("price")),
                "slug": str(pick.get("slug") or ""),
                "at": _to_float(pick.get("at")) or started,
                "index": _to_int(pick.get("index")),
                "tradable": pick.get("tradable"),
            })
        if picks:
            picks.sort(key=lambda m: m.get("at") or 0.0)
            return picks
        # старые данные ещё не мигрированы: только picked_* / marks
        for drop in sess.get("drops") or []:
            name = str(drop.get("picked_name") or "").strip()
            if not name:
                continue
            at = (_to_float(drop.get("picked_utc"))
                  or _to_float(drop.get("utc")))
            if not at:
                rel = _to_float(drop.get("rel_time"))
                at = (started + rel) if (rel is not None and started) else 0.0
            picks.append({"name": name,
                          "price": _to_int(drop.get("picked_price")),
                          "slug": str(drop.get("slug") or ""),
                          "at": at,
                          "index": _to_int(drop.get("picked_index")),
                          "tradable": drop.get("picked_tradable")})
        for mark in sess.get("marks") or []:
            name = str(mark.get("name") or "").strip()
            if not name:
                continue
            picks.append({"name": name,
                          "price": _to_int(mark.get("price")),
                          "slug": str(mark.get("slug") or ""),
                          "at": _to_float(mark.get("at")) or started,
                          "index": _to_int(mark.get("index")),
                          "tradable": mark.get("tradable")})
        unique = []
        for item in picks:
            if any(str(x.get("name")) == str(item.get("name"))
                   and _to_int(x.get("price")) == _to_int(item.get("price"))
                   and _to_int(x.get("index")) == _to_int(item.get("index"))
                   and abs((_to_float(x.get("at")) or 0.0)
                           - (_to_float(item.get("at")) or 0.0)) <= 10.0
                   for x in unique):
                continue
            unique.append(item)
        unique.sort(key=lambda m: m.get("at") or 0.0)
        return unique

    def completed_sessions(self, limit=None):
        """Завершённые сеансы, новые сверху (по ``ended_utc``)."""
        limit = self.HISTORY_SESSIONS if limit is None else int(limit)
        with self._lock:
            items = [copy.deepcopy(s) for s in self.data.get("sessions") or []
                     if s.get("ended_utc") is not None]
        items.sort(key=lambda s: _to_float(s.get("ended_utc")) or 0.0,
                   reverse=True)
        return items[:max(1, limit)]

    def current_session(self):
        with self._lock:
            cur = self._current_session_locked()
            return copy.deepcopy(cur) if cur is not None else None

    def session_view(self, view="current"):
        """Данные выбранного сеанса для карточек и списка «Лут».

        ``view``: ``current``, ``prev``, ``prev2``. Возвращает словарь с
        ``marks``, ``relics``, ``selected_count``/``sum``, ``avg_per_hour``,
        ``hours``, ``last_pick``, ``session`` (или ``None``).
        """
        view = str(view or "current")
        sess = None
        if view == "current":
            sess = self.current_session()
        else:
            index = 0 if view == "prev" else 1
            completed = self.completed_sessions(self.HISTORY_SESSIONS)
            if len(completed) > index:
                sess = completed[index]
        if sess is None:
            return {"view": view, "session": None, "marks": [],
                    "relics": 0, "selected_count": 0, "selected_sum": 0,
                    "avg_per_hour": 0.0, "hours": 0.0, "last_pick": None,
                    "empty": True}
        marks = self._marks_of_session(sess)
        total = sum(m.get("price") or 0 for m in marks)
        started = _to_float(sess.get("started_utc")) or 0.0
        ended = _to_float(sess.get("ended_utc"))
        now = ended if ended else float(self.clock())
        hours = ((now - started) / 3600.0
                 if started and now >= started else 0.0)
        best = None
        for mark in marks:
            if mark.get("price") is None:
                continue
            if best is None or mark["price"] > (best.get("price") or 0):
                best = mark
        last = marks[-1] if marks else None
        return {
            "view": view,
            "session": sess,
            "marks": marks,
            "relics": int(sess.get("screens_opened") or 0),
            "selected_count": len(marks),
            "selected_sum": int(total),
            "avg_per_hour": (total / hours if hours >= 0.05 else 0.0),
            "hours": hours,
            "last_pick": last,
            "best_drop": best,
            "empty": not marks,
        }

    def session_label(self, view="current"):
        """Заголовок сеанса: «Прошлый · 22.11 21:40» (локальное время)."""
        data = self.session_view(view)
        sess = data.get("session") or {}
        titles = {"current": "Текущий", "prev": "Прошлый",
                  "prev2": "Позапрошлый"}
        title = titles.get(str(view), "Текущий")
        stamp = _to_float(sess.get("started_utc"))
        if not stamp:
            stamp = _to_float(sess.get("ended_utc"))
        if not stamp:
            return title
        try:
            text = time.strftime("%d.%m %H:%M", time.localtime(stamp))
        except Exception:
            return title
        return f"{title} · {text}"

    def _requeue_unpriced(self):
        """F4: дропы с ``priced=false`` — в очередь на повторный запрос цены.

        Нужно, чтобы после перезапуска оверлея недооценённые ранее дропы
        (офлайн при прошлом запуске) получили цену, а не оставались
        ``price=None``. Без слага или без ``price_fn`` запрос невозможен.
        """
        if self.price_fn is None:
            return
        with self._lock:
            unpriced = [d for s in self.data.get("sessions") or []
                        for d in s.get("drops") or []
                        if not d.get("priced") and d.get("slug")]
        for drop in unpriced:
            self._request_price(drop)

    @staticmethod
    def _same_anchor(a, b):
        """Совпадает ли якорь сессии (UTC из заголовка лога)."""
        if a is None or b is None:
            return False
        try:
            return abs(float(a) - float(b)) < 1.0
        except Exception:
            return False

    def _restore_session_state(self, cur):
        """Восстанавливает ключи дедупликации и максимум rel_time из сессии."""
        self._cur_keys = set()
        self._cur_max_rel = _to_float(cur.get("last_rel_time"))
        # fallback: по дропам (старые файлы истории без last_rel_time)
        if self._cur_max_rel is None:
            for d in cur.get("drops") or []:
                rel = d.get("rel_time")
                if rel is not None and (self._cur_max_rel is None
                                        or rel > self._cur_max_rel):
                    self._cur_max_rel = rel
        for d in cur.get("drops") or []:
            rel = d.get("rel_time")
            if rel is not None and d.get("path"):
                self._cur_keys.add((rel, d.get("path")))

    def _replay_skip_locked(self, rel_time):
        """Пропустить уже прочитанные события при повторном replay сессии.

        Возвращает ``True``, если событие относится к уже учтённой части лога
        (``rel_time`` меньше сохранённого максимума). Как только достигли конца
        прочитанного, режим replay снимается и дальше обрабатываются новые
        события.
        """
        if not self._replaying:
            return False
        if rel_time is None:
            return False
        if self._cur_max_rel is None or rel_time < self._cur_max_rel:
            return True
        self._replaying = False
        return False

    def _ensure_session_locked(self, rel_time):
        cur = self._current_session_locked()
        if cur is not None and self._new_game_detected_locked(cur, rel_time):
            self._close_active_locked()
            self._cur_max_rel = None
            self._cur_keys = set()
            self._replaying = False
            cur = None
        if cur is None:
            cur = self._new_session_locked(self.clock())
            self._cur_max_rel = None
            self._cur_keys = set()
            self._replaying = False
            # базовая линия размера для новой сессии (нельзя считать прошлый
            # размер усечением — см. F1 в VERIFY_REPORT_FARMSTATS3.md)
            self._last_file_size = self._file_size()
        if rel_time is not None:
            if self._cur_max_rel is None or rel_time > self._cur_max_rel:
                self._cur_max_rel = rel_time
                cur["last_rel_time"] = rel_time
        cur["last_file_size"] = self._file_size()
        return cur

    def _new_session_locked(self, now):
        started, source = self._session_anchor()
        sess = {
            "id": self._new_id(started),
            "log_path": self.path or "",
            "started_utc": started,
            "anchor_source": source,
            "ended_utc": None,
            "last_rel_time": None,
            "last_file_size": None,
            "relics_opened": 0,
            "screens_opened": 0,
            "total_value": 0,
            "priced_drops": 0,
            "best_drop": None,
            "drops": [],
        }
        self.data.setdefault("sessions", []).append(sess)
        try:
            max_n = int(self.cfg.get("farm_stats_max_sessions", 200) or 200)
        except Exception:
            max_n = 200
        if max_n < 1:
            max_n = 200
        sessions = self.data["sessions"]
        if len(sessions) > max_n:
            self.data["sessions"] = sessions[-max_n:]
        self._trim_history_locked()
        return sess

    def _close_active_locked(self):
        """Граница сессии: ``ended_utc = started_utc + last_rel_time``.

        Полностью пустая сессия (нет ни дропов, ни открытий экрана) не
        сохраняется — она возникает только как артефакт границы и засоряла бы
        счётчик сессий и базу часов (см. F1 в VERIFY_REPORT_FARMSTATS3.md).
        """
        cur = self._current_session_locked()
        if cur is None:
            return
        if not (cur.get("drops") or cur.get("screens_opened")):
            self.data["sessions"] = [s for s in self.data.get("sessions") or []
                                     if s is not cur]
            return
        last = self._cur_max_rel or 0.0
        started = cur.get("started_utc")
        if isinstance(started, (int, float)) and started:
            cur["ended_utc"] = started + last
        else:
            cur["ended_utc"] = float(self.clock())

    def _session_anchor(self):
        """Возвращает ``(started_utc, anchor_source)`` для ТЕКУЩЕГО файла лога.

        ``started_utc`` — UTC из строки заголовка ``Current time: … [UTC: …]``
        (``anchor_source="log_header"``). ``st_ctime`` НЕ используется. Если
        пути/заголовка нет — fallback на момент первого контакта ``clock()``
        (``anchor_source="clock"``).
        """
        if self.path:
            utc = _read_log_start_utc(self.path)
            if utc is not None:
                return utc, "log_header"
        return float(self.clock()), "clock"

    def _session_start_anchor(self):
        """Только ``started_utc`` (float) — скаляр для вызовов идентичности."""
        utc, _src = self._session_anchor()
        return utc

    def _file_size(self):
        try:
            if not self.path:
                return None
            return int(os.path.getsize(self.path))
        except OSError:
            return None

    def _file_shrank_locked(self):
        """Файл стал меньше сохранённого размера → in-place truncate (новая игра)."""
        size = self._file_size()
        shrunk = (self._last_file_size is not None and size is not None
                  and size < self._last_file_size)
        if size is not None:
            self._last_file_size = size
        return shrunk

    def _new_game_detected_locked(self, cur, rel_time):
        """Новая игровая сессия в том же файле: смена ``Current time``, усечение
        файла или откат относительного времени назад."""
        utc, src = self._session_anchor()
        stored = cur.get("started_utc")
        # Якорь из заголовка меняется только между разными игровыми сессиями.
        # «clock»-fallback (заголовок не прочитан) по якорю не сравниваем — там
        # границу ловят усечение файла и откат rel_time.
        if (src == "log_header" and cur.get("anchor_source") == "log_header"
                and stored is not None and not self._same_anchor(stored, utc)):
            # сразу выравниваем _last_file_size, чтобы следующее событие новой
            # игры не сочло старый размер за in-place truncate (F1).
            self._last_file_size = self._file_size()
            return True
        if self._file_shrank_locked():
            return True
        if (not self._replaying and rel_time is not None
                and self._cur_max_rel is not None
                and rel_time < self._cur_max_rel - 60.0):
            return True
        return False

    @staticmethod
    def _new_id(started):
        try:
            ts = (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(started))
                  if started else "unknown")
        except Exception:
            ts = "unknown"
        return f"{ts}-{uuid.uuid4().hex[:8]}"

    # ── цены ────────────────────────────────────────────────────────────
    def _request_price(self, drop):
        if self.price_fn is None:
            return
        if self.async_prices:
            self._queue.put(drop)
            self._ensure_worker()
        else:
            self._resolve_drop_price(drop)

    def _ensure_worker(self):
        worker = self._worker
        if worker is not None and worker.is_alive():
            return
        worker = threading.Thread(target=self._price_loop, daemon=True,
                                  name="wf-farm-price")
        self._worker = worker
        worker.start()

    def _price_loop(self):
        while not self._stop.is_set():
            try:
                drop = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if drop is None:
                break
            try:
                self._resolve_drop_price(drop)
            except Exception:
                pass

    def resolve_pending(self):
        """Синхронно дочитывает очередь цен (тесты и ``stop()``)."""
        while True:
            try:
                drop = self._queue.get_nowait()
            except queue.Empty:
                break
            if drop is None:
                continue
            try:
                self._resolve_drop_price(drop)
            except Exception:
                pass

    def _resolve_drop_price(self, drop):
        slug = drop.get("slug")
        if not slug or self.price_fn is None:
            return
        try:
            info = self.price_fn(slug)
        except Exception:
            info = None
        price = None
        name = None
        ducats = None
        tradable = None
        if isinstance(info, dict):
            price = info.get("price")
            name = info.get("name") or info.get("market_name")
            ducats = info.get("ducats")
            tradable = info.get("tradable")
        price = _to_int(price)
        with self._lock:
            drop["price"] = price
            drop["priced"] = price is not None
            if name:
                drop["name"] = name
            if ducats is not None:
                drop["ducats"] = ducats
            if tradable is not None:
                drop["tradable"] = bool(tradable)
                if drop.get("picked_name"):
                    drop["picked_tradable"] = bool(tradable)
            sess = self._find_session_locked(drop)
            if sess is not None:
                self._recompute_session_locked(sess)
        self.save()
        self._notify()

    def _find_session_locked(self, drop):
        for s in self.data.get("sessions") or []:
            for d in s.get("drops") or []:
                if d is drop:
                    return s
        return None

    # ── агрегаты ────────────────────────────────────────────────────────
    @staticmethod
    def _recompute_session_locked(sess):
        drops = sess.get("drops") or []
        total = 0
        priced = 0
        best = None
        for d in drops:
            if d.get("priced") and d.get("price") is not None:
                total += int(d["price"])
                priced += 1
                if (best is None or d["price"] > best["price"]
                        or (d["price"] == best["price"]
                            and (d.get("rel_time") or 0) < (best.get("rel_time") or 0))):
                    best = d
        sess["relics_opened"] = len(drops)
        sess["total_value"] = total
        sess["priced_drops"] = priced
        sess["best_drop"] = None
        if best is not None:
            sess["best_drop"] = {
                "rel_time": best.get("rel_time"),
                "utc": best.get("utc"),
                "path": best.get("path"),
                "slug": best.get("slug"),
                "name": best.get("name"),
                "price": best.get("price"),
            }

    @staticmethod
    def _hours(sess, now):
        started = sess.get("started_utc")
        if not isinstance(started, (int, float)) or not started:
            return 0.0
        ended = sess.get("ended_utc")
        if not isinstance(ended, (int, float)) or not ended:
            ended = now
        if ended < started:
            ended = started
        return max(0.0, (ended - started) / 3600.0)

    def stats(self):
        """Агрегаты §4.4 (+ текущая сессия) — вычисляются из JSON на лету."""
        now = float(self.clock())
        with self._lock:
            sessions = [copy.deepcopy(s) for s in self.data.get("sessions") or []]
        total_relics = sum(s.get("relics_opened", 0) for s in sessions)
        total_value = sum(s.get("total_value", 0) for s in sessions)
        best = None
        hours = 0.0
        for s in sessions:
            bd = s.get("best_drop")
            if bd and bd.get("price") is not None:
                if best is None or bd["price"] > best["price"]:
                    best = bd
            hours += self._hours(s, now)
        avg_per_hour = total_value / hours if hours >= 0.05 else 0.0
        current = sessions[-1] if sessions else None
        return {
            "relics_opened": total_relics,
            "total_value": total_value,
            "best_drop": best,
            "avg_per_hour": avg_per_hour,
            "hours": hours,
            "sessions": len(sessions),
            "current_relics": current.get("relics_opened", 0) if current else 0,
            "current_screens": current.get("screens_opened", 0) if current else 0,
            "current_active": bool(current and current.get("ended_utc") is None),
            "priced_drops": sum(s.get("priced_drops", 0) for s in sessions),
        }

    def recent_drops(self, limit=20):
        """Последние дропы (сначала свежие) для компактного списка в UI."""
        with self._lock:
            out = []
            for s in reversed(self.data.get("sessions") or []):
                for d in reversed(s.get("drops") or []):
                    out.append(copy.deepcopy(d))
                    if len(out) >= limit:
                        return out
            return out


# Совместимое имя для краткости (как в спеке).
__all__ = ["FarmStatsCollector", "SCHEMA"]
