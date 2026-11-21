# -*- coding: utf-8 -*-
"""Пулы реликвий: WFCD relics.json → шансы, цены, ожидаемая ценность (EV).

Модуль для ``wf_ru/warframe.py``. Полное описание — ``wf_ru/FARM_STATS_SPEC.md``.

Источник данных — публичный ``relics.json`` из репозитория
``WFCD/warframe-drop-data`` (GET, ~2.4 МБ). Ответ кэшируется на диске с TTL;
без сети используется просроченный кэш, без кэша — понятная русская ошибка.

Схема (факт F3):
    {"relics": [{"tier","relicName","state","rewards":[{"itemName","chance",…}]}]}

``chance`` приходит в ПРОЦЕНТАХ (11.11 = 11.11%), это нормализуется в долю
единицы функцией ``chance_01`` (защита и от смены формата источника).

Цены — инъектированный ``price_fn(slug) -> int|None`` (низшая sell). В проде это
``market.price_hint(slug)["sell"]``; в тестах — словарь. Цены кэшируются по слагу
с TTL ``relic_pool_price_ttl``.

Модуль самодостаточен: не импортирует warframe/market_client, сеть идёт только
через инъектированный ``fetch``.
"""
import json
import os
import re
import threading
import time

STATES = ("Intact", "Exceptional", "Flawless", "Radiant")
CANON_TIERS = ("Lith", "Meso", "Neo", "Axi", "Requiem")

DEFAULT_RELIC_URL = ("https://raw.githubusercontent.com/WFCD/"
                     "warframe-drop-data/main/data/relics.json")

_RE_SPACES = re.compile(r"\s+")


def chance_01(value):
    """``25.33`` (проценты) → ``0.2533``; ``0.2533`` (доля) → ``0.2533``.

    Источник (WFCD) отдаёт шансы в ПРОЦЕНТАХ, поэтому ``1.0`` трактуется как
    ``1 %`` (``0.01``), а не как ``100 %`` (см. VERIFY_REPORT_FARMSTATS.md F5).
    """
    try:
        v = float(value)
    except Exception:
        return 0.0
    if v >= 1.0:
        return v / 100.0
    return max(0.0, min(1.0, v))


def slugify(item_name):
    """Человекочитаемое имя → слаг warframe.market (эвристика).

    ``"Paris Prime Blueprint"`` → ``"paris_prime_blueprint"``. Нормализует
    ``&`` → ``and``, убирает апострофы/дефисы/прочие знаки.
    """
    s = str(item_name or "").strip().lower()
    if not s:
        return ""
    s = s.replace("&", " and ")
    s = s.replace("'", "").replace("\u2019", "").replace("`", "")
    s = s.replace("-", " ")
    # остальные не-буквенно-цифровые символы убираем
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = _RE_SPACES.sub("_", s.strip())
    return s.strip("_")


def _norm_name(text):
    """Сравнимое имя (как ``warframe.normalize_name``, без OCR-мусора).

    Нужен, чтобы искать в индексе ``{norm_name: slug}``, построенном из
    ``MarketAPI.items`` (ключи нормализованы той же логикой).
    """
    if not text:
        return ""
    t = str(text).lower().replace("ё", "е")
    t = t.replace("—", " ").replace("–", " ")
    t = re.sub(r"[^\w ]+", " ", t, flags=re.UNICODE)
    t = re.sub(r"_+", " ", t)
    return _RE_SPACES.sub(" ", t).strip()


def _to_int(value):
    if value is None:
        return None
    try:
        value = int(float(value))
    except Exception:
        return None
    return value if value > 0 else None


class RelicPool:
    """Пул реликвий с TTL-кэшем, ценами и EV.

    :param cfg: настройки (``relic_pool_url``, ``relic_pool_ttl_hours``,
        ``relic_pool_state``, ``relic_pool_min_ev``, ``relic_pool_price_ttl``,
        ``request_timeout`` …).
    :param price_fn: ``callable(slug) -> int|None`` — низшая sell (платина).
    :param fetch: ``callable(url) -> dict`` — загрузка relics.json (тесты
        подменяют); по умолчанию ``requests.get(...).json()``.
    :param clock: подменяемое время.
    :param logger: служебные сообщения.
    :param data_dir: папка для ``relics_cache.json``.
    :param name_index: ``{norm_name: slug}`` — авторитетный маппинг из
        ``MarketAPI.items`` / ``MarketOrderClient.items_catalog``.
    """

    def __init__(self, cfg, price_fn=None, fetch=None, clock=time.time,
                 logger=print, data_dir=None, name_index=None):
        self.cfg = dict(cfg or {})
        self.price_fn = price_fn
        self.fetch = fetch or self._default_fetch
        self.clock = clock or time.time
        self.logger = logger or (lambda *a, **k: None)
        self.data_dir = (data_dir or os.environ.get("WF_DATA_DIR")
                         or os.path.dirname(os.path.abspath(__file__)))
        self.name_index = dict(name_index or {})
        self._lock = threading.RLock()
        self._rows = []          # [{tier, relicName, state, rewards:[...]}]
        self._source_ts = 0.0    # ts данных (кэша или свежей загрузки)
        self._error = None
        self._loaded = False
        self._price_cache = {}   # slug -> (ts, price|None)

    # ── сеть и кэш ──────────────────────────────────────────────────────
    def _default_fetch(self, url):
        import requests
        try:
            timeout = float(self.cfg.get("request_timeout", 8) or 8)
        except Exception:
            timeout = 8.0
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def cache_path(self):
        return os.path.join(self.data_dir, "relics_cache.json")

    def _url(self):
        return str(self.cfg.get("relic_pool_url") or DEFAULT_RELIC_URL).strip()

    def _ttl_seconds(self):
        try:
            hours = float(self.cfg.get("relic_pool_ttl_hours", 24) or 24)
        except Exception:
            hours = 24.0
        return max(0.0, hours) * 3600.0

    def _price_ttl(self):
        try:
            return max(0.0, float(self.cfg.get("relic_pool_price_ttl", 3600) or 0))
        except Exception:
            return 3600.0

    def _read_cache(self):
        path = self.cache_path()
        try:
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    return d
        except Exception:
            pass
        return None

    def _write_cache(self, cache):
        path = self.cache_path()
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False)
            os.replace(tmp, path)
            return True
        except Exception as e:
            self.logger(f"relic_pool: кэш не записан ({type(e).__name__}: {e})")
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            return False

    @staticmethod
    def _fmt_ts(ts):
        try:
            return time.strftime("%d.%m.%Y %H:%M", time.localtime(ts))
        except Exception:
            return "?"

    def load(self, force=False):
        """Загружает пулы: свежий кэш → данные; иначе сеть → кэш → ошибка.

        :param force: игнорировать TTL (кнопка ⟳); при сетевой ошибке всё равно
            возвращает старый кэш, если он есть.
        :return: ``{'tier': [relic, ...]}``.
        """
        now = float(self.clock())
        ttl = self._ttl_seconds()
        cache = self._read_cache()
        payload = None
        source_ts = 0.0
        used_cache = False

        if cache and not force and (now - float(cache.get("ts") or 0) < ttl):
            payload = cache.get("payload")
            source_ts = float(cache.get("ts") or 0)
            used_cache = True
        else:
            try:
                payload = self.fetch(self._url())
                source_ts = now
                self._write_cache({"url": self._url(), "ts": now,
                                   "payload": payload})
            except Exception as e:
                if cache:
                    payload = cache.get("payload")
                    source_ts = float(cache.get("ts") or 0)
                    used_cache = True
                    self._error = (f"сеть недоступна — данные от "
                                   f"{self._fmt_ts(source_ts)}")
                else:
                    self._error = (f"пулы недоступны: {type(e).__name__}: {e}")
                    payload = None

        with self._lock:
            if payload is not None:
                self._rows = self._parse(payload)
                self._source_ts = source_ts
                if not used_cache:
                    self._error = None
                self._loaded = True
            return self._view_locked()

    def reload(self, force=True):
        """Синоним ``load(force=True)`` — удобен для кнопки «Обновить»."""
        return self.load(force=force)

    def is_loaded(self):
        return bool(self._loaded)

    def status_text(self):
        """Короткий статус для шапки/вкладки."""
        with self._lock:
            ts = self._source_ts
            err = self._error
            rows = len(self._rows)
        if err:
            return err
        if not rows:
            return "пулы: нет данных"
        return f"пулы: {self._fmt_ts(ts)} · реликвий: {rows}"

    # ── разбор ──────────────────────────────────────────────────────────
    @staticmethod
    def _parse(payload):
        rows = []
        relics = (payload or {}).get("relics")
        if not isinstance(relics, list):
            relics = []
        for r in relics:
            if not isinstance(r, dict):
                continue
            tier = str(r.get("tier") or "").strip()
            name = str(r.get("relicName") or "").strip()
            state = str(r.get("state") or "").strip()
            if not (tier and name and state):
                continue
            rewards = []
            for rw in (r.get("rewards") or []):
                if not isinstance(rw, dict):
                    continue
                item = str(rw.get("itemName") or "").strip()
                if not item:
                    continue
                try:
                    chance = float(rw.get("chance"))
                except Exception:
                    chance = 0.0
                rec = {"itemName": item, "chance": chance,
                       "chance_01": chance_01(chance)}
                for key in ("rarity", "_id", "ducats"):
                    if rw.get(key) is not None:
                        rec[key] = rw[key]
                rewards.append(rec)
            if not rewards:
                continue
            row = {"tier": tier, "relicName": name, "state": state,
                   "rewards": rewards}
            for key in ("_id",):
                if r.get(key) is not None:
                    row[key] = r[key]
            rows.append(row)
        return rows

    def _view_locked(self):
        out = {}
        for r in self._rows:
            out.setdefault(r["tier"], []).append(copy_row(r))
        return out

    def tiers(self):
        """Тиры в каноническом порядке (Lith/Meso/Neo/Axi/Requiem) + незнакомые."""
        with self._lock:
            present = []
            for r in self._rows:
                if r["tier"] not in present:
                    present.append(r["tier"])
            ordered = [t for t in CANON_TIERS if t in present]
            ordered += [t for t in present if t not in CANON_TIERS]
            return ordered

    def states(self):
        return list(STATES)

    # ── маппинг и цены ──────────────────────────────────────────────────
    def slug_for(self, item_name):
        """``itemName`` → слаг: авторитетный индекс имён, затем slugify."""
        key = _norm_name(item_name)
        if key and self.name_index and key in self.name_index:
            return self.name_index[key]
        return slugify(item_name) or None

    def _price_for(self, slug):
        if not slug or self.price_fn is None:
            return None
        now = float(self.clock())
        with self._lock:
            cached = self._price_cache.get(slug)
        if cached is not None and now - cached[0] < self._price_ttl():
            return cached[1]
        try:
            price = _to_int(self.price_fn(slug))
        except Exception:
            price = None
        with self._lock:
            self._price_cache[slug] = (now, price)
        return price

    def cached_price(self, slug):
        """Цена только из кэша (без сети). Возвращает ``(price, is_fresh)``.

        ``is_fresh`` — ``False``, если слага нет в кэше или кэш протух (тогда
        цена ещё не резолвилась и её можно запросить в фоне через
        :meth:`resolve_slugs`).
        """
        if not slug or self.price_fn is None:
            return None, False
        now = float(self.clock())
        with self._lock:
            cached = self._price_cache.get(slug)
        if cached is not None and now - cached[0] < self._price_ttl():
            return cached[1], True
        return None, False

    def resolve_slugs(self, slugs):
        """Резолвит цены для списка слагов (для фонового потока).

        Сеть вызывается только для слагов, которых нет в свежем кэше; результат
        кладётся в ``_price_cache`` и затем читается ``evaluate(resolve=False)``.
        Ошибки и отсутствие цены не роняют поток (кэшируется ``None``).
        """
        for slug in (slugs or []):
            if not slug:
                continue
            try:
                self._price_for(slug)
            except Exception:
                continue
        return None

    def clear_price_cache(self):
        with self._lock:
            self._price_cache = {}

    # ── EV ──────────────────────────────────────────────────────────────
    def _rows_for(self, tier, state):
        tier = str(tier or "").strip()
        state = str(state or "").strip()
        with self._lock:
            rows = [r for r in self._rows
                    if r["tier"] == tier and r["state"] == state]
            return [copy_row(r) for r in rows]

    def evaluate(self, tier, state=None, resolve=True):
        """Список реликвий тира/состояния, отсортированный по EV (убыв.).

        Каждая запись: ``{tier, relic_name, state, ev, coverage, rewards,
        badge}``. ``badge``: ``"best"`` (топ-3 по EV в списке), ``"good"``
        (EV >= relic_pool_min_ev), иначе ``""``.

        :param resolve: ``True`` (по умолчанию) — резолвить цены синхронно
            (сеть/price_fn вызывается прямо здесь, используется тестами и не-GUI
            кодом). ``False`` — только кэш цен, сеть не трогаем; недостающие
            цены остаются ``None`` с ``price_cached=False``, чтобы UI мог
            догрузить их в фоне через ``resolve_slugs``.
        """
        state = state or str(self.cfg.get("relic_pool_state") or "Intact")
        rows = self._rows_for(tier, state)
        evals = [self._eval_relic(r, resolve=resolve) for r in rows]
        evals.sort(key=lambda e: e["ev"], reverse=True)
        try:
            min_ev = float(self.cfg.get("relic_pool_min_ev", 10) or 0)
        except Exception:
            min_ev = 0.0
        top = min(3, len(evals))
        for i, e in enumerate(evals):
            if i < top and e["ev"] > 0:
                e["badge"] = "best"
            elif e["ev"] >= min_ev:
                e["badge"] = "good"
            else:
                e["badge"] = ""
        return evals

    def _eval_relic(self, r, resolve=True):
        ev = 0.0
        coverage = 0.0
        rewards = []
        for rw in r["rewards"]:
            item_name = rw["itemName"]
            c01 = rw["chance_01"]
            slug = self.slug_for(item_name)
            if slug:
                if resolve:
                    price = self._price_for(slug)
                    price_cached = True
                else:
                    price, price_cached = self.cached_price(slug)
            else:
                price = None
                price_cached = False
            rw_ev = c01 * price if price is not None else 0.0
            if price is not None:
                ev += rw_ev
                coverage += c01
            rewards.append({
                "item_name": item_name,
                "chance": rw["chance"],
                "chance_01": c01,
                "slug": slug,
                "price": price,
                "price_cached": price_cached,
                "ev": round(rw_ev, 2),
            })
        return {
            "tier": r["tier"],
            "relic_name": r["relicName"],
            "state": r["state"],
            "ev": round(ev, 2),
            "coverage": round(coverage, 4),
            "rewards": rewards,
            "badge": "",
        }


def copy_row(r):
    """Глубокая копия одной реликвии (без ``copy.deepcopy`` из цикла)."""
    return json.loads(json.dumps(r, ensure_ascii=False))


__all__ = ["RelicPool", "STATES", "CANON_TIERS", "chance_01", "slugify"]
