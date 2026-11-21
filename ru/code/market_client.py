# -*- coding: utf-8 -*-
"""Продажа предметов на warframe.market прямо из оверлея (API v2).

См. EVENTS_MARKET_SPEC.md, часть II. Модуль самостоятельный: не импортирует
``warframe.py`` (его можно тестировать и использовать отдельно).

Правила безопасности
--------------------
1. **Токен только из config.json (``market_token``) или переменной окружения
   ``WF_MARKET_TOKEN``.** Пароль нигде не спрашивается и не хранится.
2. **Токен никогда не печатается и не логируется.** Он уходит только в
   заголовке ``Authorization: Bearer …`` на ``api.warframe.market``; в
   текстах ошибок и ``repr`` его нет (маскируется).
3. **Никаких изменяющих запросов без явного действия пользователя.**
   ``create_order``/``update_order``/``close_order``/``delete_order``
   вызываются только из диалога по кнопке; ``dry_run`` (настройка
   ``market_dry_run``) вообще не отправляет запрос.
4. Rate limit warframe.market — 3 запроса/сек: между запросами пауза
   ``market_min_interval`` (по умолчанию 0.35 с), ``429``/``509`` —
   пауза и повтор, потом понятная русская ошибка.

Пример (без UI)::

    from market_client import MarketOrderClient
    client = MarketOrderClient(cfg)
    client.has_token()                       # False/True
    meta = client.item("paris_prime_blueprint")
    hint = client.price_hint("paris_prime_blueprint")
    client.create_order(meta["id"], platinum=10, quantity=1, meta=meta)
"""

import json
import os
import threading
import time

BASE = "https://api.warframe.market"
API = BASE + "/v2"
SITE = "https://warframe.market"
TOKEN_ENV = "WF_MARKET_TOKEN"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

MAX_PLATINUM = 900000
MAX_QUANTITY = 9999
MIN_INTERVAL = 0.35          # сек: 3 запроса/сек — предел warframe.market

#: Русские подсказки к кодам валидации warframe.market.
FIELD_HINTS = {
    "app.field.tooSmall": "слишком маленькое значение",
    "app.field.tooBig": "слишком большое значение",
    "app.field.required": "обязательное поле",
    "app.field.forbidden": "поле здесь запрещено",
    "app.field.notFound": "не найдено",
    "app.field.invalid": "недопустимое значение",
    "app.field.divisibleBy": "должно делиться нацело",
    "app.field.invalidValue": "недопустимое значение",
}
FIELD_NAMES = {
    "itemId": "предмет",
    "item_id": "предмет",
    "type": "тип ордера",
    "platinum": "цена",
    "quantity": "количество",
    "visible": "видимость",
    "perTrade": "лот",
    "per_trade": "лот",
    "rank": "ранг",
    "charges": "заряды",
    "subtype": "подтип",
    "amberStars": "звёзды Айя",
    "cyanStars": "звёзды Айя",
}
REQUEST_HINTS = {
    "app.errors.unauthorized": "нужна авторизация (токен не принят)",
    "app.errors.forbidden": "нет прав на это действие",
    "app.errors.notFound": "объект не найден",
}


# ─────────────────────────────────────────────────────────────────────────
#  Ошибки: наружу всегда понятный русский текст
# ─────────────────────────────────────────────────────────────────────────
class MarketError(RuntimeError):
    """Ошибка warframe.market с русским сообщением (без токена внутри)."""


class MarketAuthError(MarketError):
    """Токен отсутствует, недействителен или просрочен (401)."""


class MarketValidationError(MarketError):
    """Сервер (400) или локальная проверка отклонили поля ордера."""

    def __init__(self, message, inputs=None):
        super().__init__(message)
        self.inputs = dict(inputs or {})


class MarketRateLimitError(MarketError):
    """Слишком много запросов (429/509)."""


class MarketNetworkError(MarketError):
    """Нет связи с warframe.market (таймаут/сеть)."""


# ─────────────────────────────────────────────────────────────────────────
#  Токен и мелкие утилиты
# ─────────────────────────────────────────────────────────────────────────
def normalize_token(value):
    """Приводит вставленный токен к чистому JWT (без ``Bearer``/``JWT``/кавычек)."""
    text = str(value or "").strip().strip('"').strip("'").strip()
    for prefix in ("Bearer ", "bearer ", "JWT ", "jwt "):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    return text


def token_from_config(cfg):
    """Токен только из config (``market_token``) или env ``WF_MARKET_TOKEN``."""
    cfg = cfg or {}
    token = normalize_token(cfg.get("market_token") or "")
    if not token:
        token = normalize_token(os.environ.get(TOKEN_ENV) or "")
    return token


def mask_token(token):
    """Короткое безопасное представление токена для UI/логов (без утечки)."""
    token = normalize_token(token)
    if not token:
        return "нет токена"
    tail = token[-4:] if len(token) >= 8 else ""
    return f"••••{tail}" if tail else "••••"


def _dig(payload, *keys):
    """Достаёт первое найденное значение по цепочке ключей."""
    for key in keys:
        if isinstance(payload, dict) and key in payload:
            return payload[key]
    return None


def data_of(payload):
    """Распаковывает конверт API ``{apiVersion, data, error}`` (или legacy)."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if data is not None:
        return data
    inner = payload.get("payload")
    if isinstance(inner, dict):
        for key in ("item", "data", "order", "orders", "profile"):
            if key in inner:
                return inner[key]
        return inner
    return payload


def orders_of(payload):
    """Достаёт список ордеров из ответа v2 или legacy-конверта."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "payload", "orders"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for inner_key in ("orders", "data", "items"):
                inner = value.get(inner_key)
                if isinstance(inner, list):
                    return inner
    return []


def _integer(value):
    """Мягко приводит к int (иначе ``None``)."""
    try:
        return int(value)
    except Exception:
        return None


def _dict(value):
    """Безопасно приводит значение к словарю."""
    return value if isinstance(value, dict) else {}


def _number(value):
    """Безопасно приводит значение к числу (иначе ``None``)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except Exception:
        return None


def _online(order):
    """Онлайн ли продавец в ордере (v2: ``user.status``)."""
    user = order.get("user") or {}
    status = str(user.get("status") or order.get("status") or "").lower()
    return status in ("online", "ingame")


def summarize_orders(orders):
    """Низшая sell / высшая buy по ордерам (онлайн в приоритете)."""
    sells_online, sells_other, buys_online, buys_other = [], [], [], []
    total = 0
    for order in orders or []:
        if not isinstance(order, dict):
            continue
        if order.get("visible") is False:
            continue
        platinum = _integer(order.get("platinum"))
        if not platinum or platinum < 1 or platinum > MAX_PLATINUM:
            continue
        quantity = _integer(order.get("quantity"))
        if quantity is not None and quantity < 1:
            continue
        total += 1
        kind = str(order.get("type") or order.get("order_type") or "").lower()
        online = _online(order)
        if kind == "sell":
            (sells_online if online else sells_other).append(platinum)
        elif kind == "buy":
            (buys_online if online else buys_other).append(platinum)
    sell_pool = sells_online or sells_other
    buy_pool = buys_online or buys_other
    return {
        "sell": min(sell_pool) if sell_pool else None,
        "buy": max(buy_pool) if buy_pool else None,
        "sell_online": len(sells_online),
        "buy_online": len(buys_online),
        "count": total,
        "sell_offline": bool(sells_other) and not sells_online,
    }


def item_meta_of(payload, slug):
    """Данные предмета из ответа ``/v2/items/{slug}`` (id, ранги, лоты, подтипы)."""
    raw = data_of(payload)
    if isinstance(raw, list):
        raw = raw[0] if raw else {}
    if not isinstance(raw, dict):
        raw = {}
    i18n = raw.get("i18n") if isinstance(raw.get("i18n"), dict) else {}
    names = []
    for lang in ("ru", "en"):
        name = ((i18n.get(lang) or {}).get("name") or "").strip()
        if name:
            names.append(name)
    names_en = [((i18n.get("en") or {}).get("name") or "").strip(),
                ((i18n.get("ru") or {}).get("name") or "").strip()]
    tags = [str(x) for x in (raw.get("tags") or []) if x]
    set_parts = [str(x) for x in (raw.get("setParts") or []) if x]
    return {
        "id": raw.get("id") or raw.get("_id") or raw.get("item_id"),
        "slug": raw.get("slug") or slug,
        "name": names[0] if names else (raw.get("item_name") or slug),
        "name_en": names_en[0] if names_en[0] else (names[0] if names else slug),
        "max_rank": max(0, _integer(raw.get("maxRank") or raw.get("max_rank")) or 0),
        "bulk": bool(raw.get("bulkTradable") or raw.get("bulk_tradable")),
        "subtypes": [str(x) for x in (raw.get("subtypes") or []) if x],
        "trading_tax": _integer(raw.get("tradingTax") or raw.get("trading_tax")),
        "ducats": _integer(raw.get("ducats")),
        "req_mastery_rank": _integer(raw.get("reqMasteryRank")
                                     or raw.get("req_mastery_rank")),
        "quantity_in_set": _integer(raw.get("quantityInSet")
                                    or raw.get("quantity_in_set")),
        "tags": tags,
        "set_parts": set_parts,
        "tradable": raw.get("tradable"),
        "url": f"{SITE}/items/{slug}",
    }


def catalog_item(row):
    """Строка ``GET /v2/items`` → компактная запись для поиска/кэша."""
    row = _dict(row)
    i18n = row.get("i18n") if isinstance(row.get("i18n"), dict) else {}
    name_ru = ((i18n.get("ru") or {}).get("name") or "").strip()
    name_en = ((i18n.get("en") or {}).get("name") or "").strip()
    en_meta = i18n.get("en") if isinstance(i18n.get("en"), dict) else {}
    slug = row.get("slug") or ""
    return {
        "id": row.get("id") or row.get("_id") or "",
        "slug": slug,
        "name": name_ru or name_en or slug,
        "name_en": name_en or name_ru,
        "icon": str(en_meta.get("icon") or ""),
        "thumb": str(en_meta.get("thumb") or ""),
        "tags": [str(x) for x in (row.get("tags") or []) if x],
        "game_ref": row.get("gameRef") or "",
        "url": f"{SITE}/items/{slug}" if slug else SITE,
    }


def _stat_number(value):
    number = _number(value)
    return float(number) if number is not None else None


def _parse_datetime(value):
    """ISO-8601 (``Z``/``+00:00``) → epoch секунд; ``None``, если не разобрать."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        import datetime
        if text.endswith("Z") or text.endswith("z"):
            text = text[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def statistic_point(point):
    """Точка v1 statistics → ``{ts, price, min, max, avg, volume}``."""
    point = _dict(point)
    ts = _parse_datetime(point.get("datetime"))
    price = (_stat_number(point.get("avg_price"))
             or _stat_number(point.get("wa_price"))
             or _stat_number(point.get("closed_price"))
             or _stat_number(point.get("median"))
             or _stat_number(point.get("open_price")))
    return {
        "ts": ts,
        "price": price,
        "min": _stat_number(point.get("min_price")),
        "max": _stat_number(point.get("max_price")),
        "avg": _stat_number(point.get("avg_price")),
        "volume": _integer(point.get("volume")) or 0,
        "datetime": str(point.get("datetime") or ""),
        "raw": point,
    }


def statistics_series(payload):
    """Ответ ``/v1/items/{slug}/statistics`` → ``{"48h": [...], "90d": [...]}``.

    Источник — официальная структура v1:
    ``payload.statistics_closed.{48hours, 90days}`` и
    ``payload.statistics_live.48hours``. Битые точки пропускаются.
    """
    root = payload
    if isinstance(root, dict):
        for key in ("payload", "data"):
            inner = root.get(key)
            if isinstance(inner, dict) and ("statistics_closed" in inner
                                            or "statistics_live" in inner):
                root = inner
                break
    root = _dict(root)
    closed = _dict(root.get("statistics_closed"))
    live = _dict(root.get("statistics_live"))

    def points(raw_list):
        result = []
        for item in raw_list or []:
            point = statistic_point(item)
            if point["ts"] is None or point["price"] is None:
                continue
            result.append(point)
        result.sort(key=lambda x: x["ts"])
        return result

    result_48h = points(closed.get("48hours"))
    live_48h = points(live.get("48hours"))
    # для 48 часов полезнее «живые» точки, если они есть
    return {
        "48h": live_48h or result_48h,
        "48h_closed": result_48h,
        "90d": points(closed.get("90days")),
    }


def filter_catalog(items, query, limit=60):
    """Фильтр каталога по ru/en названиям (rapidfuzz, запасной — difflib)."""
    items = [dict(x) for x in (items or []) if isinstance(x, dict)]
    query = str(query or "").strip().lower()
    if not query:
        result = sorted(items, key=lambda x: str(x.get("name") or "").lower())
        return result[:max(1, int(limit))]

    # слова запроса должны «покрываться» названием: сначала подстрочные совпадения
    tokens = [t for t in query.replace("(", " ").replace(")", " ").split() if t]
    scored = []
    for item in items:
        name = str(item.get("name") or "").lower()
        name_en = str(item.get("name_en") or "").lower()
        slug = str(item.get("slug") or "").lower().replace("_", " ")
        haystack = f"{name} {name_en} {slug}"
        if all(t in haystack for t in tokens):
            scored.append((0, item))
            continue
        try:
            from rapidfuzz import fuzz
            score = max(fuzz.WRatio(query, name), fuzz.WRatio(query, name_en),
                        fuzz.WRatio(query, slug))
        except Exception:
            import difflib
            score = 100.0 * max(
                difflib.SequenceMatcher(None, query, name).ratio(),
                difflib.SequenceMatcher(None, query, name_en).ratio(),
                difflib.SequenceMatcher(None, query, slug).ratio())
        if score >= 62:
            scored.append((100 - float(score), item))
    scored.sort(key=lambda pair: (pair[0], str(pair[1].get("name") or "").lower()))
    return [item for _score, item in scored[:max(1, int(limit))]]


def describe_error_inputs(inputs):
    """``{"platinum": "app.field.tooSmall"}`` → «цена: слишком маленькое значение»."""
    parts = []
    for field, code in (inputs or {}).items():
        name = FIELD_NAMES.get(field, field)
        hint = FIELD_HINTS.get(str(code), str(code))
        parts.append(f"{name}: {hint}")
    return "; ".join(parts)


# ─────────────────────────────────────────────────────────────────────────
#  Клиент API
# ─────────────────────────────────────────────────────────────────────────
class MarketOrderClient:
    """Минимальный клиент warframe.market v2: чтение цен и свои sell-ордера.

    :param cfg: настройки оверлея (``market_token``, ``request_timeout``,
        ``market_min_interval``, ``market_dry_run`` ...).
    :param session: готовая ``requests.Session`` (тесты/повторное использование).
    :param sleep: подмена ``time.sleep`` (тесты не ждут пауз).
    :param retries: сколько раз повторять запрос при 429/509.
    :param min_interval: пауза между запросами, сек (по умолчанию 0.35).
    """

    def __init__(self, cfg=None, session=None, sleep=None, retries=2,
                 min_interval=None, catalog_ttl=60.0, orders_ttl=60.0,
                 stats_ttl=300.0):
        self.cfg = dict(cfg or {})
        try:
            self.timeout = max(3, int(self.cfg.get("request_timeout") or 8))
        except Exception:
            self.timeout = 8
        if min_interval is None:
            try:
                min_interval = float(self.cfg.get("market_min_interval") or MIN_INTERVAL)
            except Exception:
                min_interval = MIN_INTERVAL
        self.min_interval = max(0.0, float(min_interval))
        self.retries = max(0, int(retries))
        self.language = str(self.cfg.get("market_language") or "ru")
        self.dry_run = bool(self.cfg.get("market_dry_run", False))
        self._session = session
        self._sleep = sleep or time.sleep
        self._lock = threading.RLock()
        self._last_request = 0.0
        self._token = token_from_config(self.cfg)
        self._item_cache = {}
        # кэши браузера: каталог/ордера 60 с, статистика 5 мин
        self.catalog_ttl = max(0.0, float(catalog_ttl))
        self.orders_ttl = max(0.0, float(orders_ttl))
        self.stats_ttl = max(0.0, float(stats_ttl))
        self._catalog = None
        self._catalog_ts = 0.0
        self._orders_cache = {}      # slug -> (ts, orders)
        self._top_cache = {}         # (slug, rank) -> (ts, dict)
        self._stats_cache = {}       # slug -> (ts, series)

    # ── токен ──────────────────────────────────────────────────────────
    def __repr__(self):
        return f"MarketOrderClient({mask_token(self._token)})"

    def token(self):
        """Текущий JWT (только для заголовка; наружу не печатать)."""
        with self._lock:
            return self._token

    def has_token(self):
        return bool(self.token())

    def set_token(self, token):
        """Задаёт токен в памяти (сохранение в config — забота UI)."""
        with self._lock:
            self._token = normalize_token(token)
        return self.has_token()

    def token_status(self):
        """Короткая строка статуса для UI (без токена)."""
        return mask_token(self.token()) if self.has_token() else "нет токена"

    def headers(self, auth=False):
        """Заголовки запроса; ``Authorization`` добавляется только по требованию."""
        result = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Platform": "pc",
            "Language": self.language,
            "Crossplay": "true",
        }
        if auth:
            token = self.token()
            if not token:
                raise MarketAuthError(
                    "Нет токена warframe.market: нажмите «🔑» в шапке окна — "
                    "вход через встроенный браузер (или вставьте JWT вручную: "
                    "Network → Authorization: Bearer …).")
            result["Authorization"] = f"Bearer {token}"
        return result

    # ── HTTP ───────────────────────────────────────────────────────────
    def _get_session(self):
        if self._session is not None:
            return self._session
        import requests
        with self._lock:
            if self._session is None:
                self._session = requests.Session()
            return self._session

    def _throttle(self):
        """Держит паузу между запросами (warframe.market: 3 запроса/сек)."""
        if self.min_interval <= 0:
            return
        with self._lock:
            wait = self._last_request + self.min_interval - time.monotonic()
            if wait > 0:
                self._sleep(wait)
            self._last_request = time.monotonic()

    @staticmethod
    def _json_of(response):
        try:
            payload = response.json()
        except Exception:
            return {}
        return payload if isinstance(payload, (dict, list)) else {}

    @staticmethod
    def _inputs_of(payload):
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                inputs = error.get("inputs")
                if isinstance(inputs, dict):
                    return inputs
        return {}

    @staticmethod
    def _request_hint(payload):
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                request = error.get("request")
                if isinstance(request, list) and request:
                    return REQUEST_HINTS.get(str(request[0]), str(request[0]))
                if isinstance(request, str) and request:
                    return REQUEST_HINTS.get(request, request)
        return ""

    def _request(self, method, path, params=None, body=None, auth=False):
        """Один запрос к API с обработкой 401/400/429/509 и русскими ошибками."""
        url = path if str(path).startswith("http") else API + str(path)
        headers = self.headers(auth=auth)
        attempt = 0
        while True:
            self._throttle()
            try:
                session = self._get_session()
                response = session.request(method, url, params=params, json=body,
                                           headers=headers, timeout=self.timeout)
            except MarketError:
                raise
            except Exception as exc:
                raise MarketNetworkError(
                    "Нет связи с warframe.market: проверьте интернет и повторите "
                    f"({type(exc).__name__}).") from exc
            status = int(getattr(response, "status_code", 0) or 0)
            payload = self._json_of(response)
            if status in (200, 201, 202, 204):
                return payload
            if status == 401:
                raise MarketAuthError(
                    "Токен warframe.market недействителен или просрочен. "
                    "Проще всего нажать «🔑» в шапке окна и войти через "
                    "встроенный браузер — токен заберётся сам. Запасной путь: "
                    "сайт → DevTools → Network → любой запрос к "
                    "api.warframe.market → заголовок Authorization: Bearer …")
            if status == 400:
                inputs = self._inputs_of(payload)
                message = "warframe.market отклонил данные"
                details = describe_error_inputs(inputs)
                if details:
                    message += f": {details}"
                else:
                    hint = self._request_hint(payload)
                    message += f": {hint}" if hint else " (проверьте поля ордера)."
                raise MarketValidationError(message, inputs)
            if status in (429, 509):
                attempt += 1
                if attempt > self.retries:
                    raise MarketRateLimitError(
                        "Лимит запросов warframe.market (3 в секунду): "
                        "подождите несколько секунд и повторите.")
                self._sleep(min(4.0, 0.5 * (2 ** (attempt - 1))))
                continue
            if status == 404:
                raise MarketError(
                    "warframe.market: объект не найден (HTTP 404) — "
                    "возможно, предмет снят с продажи или API изменился.")
            hint = self._request_hint(payload)
            extra = f" ({hint})" if hint else ""
            raise MarketError(f"warframe.market ответил HTTP {status}{extra}.")

    # ── чтение ─────────────────────────────────────────────────────────
    def item(self, slug, use_cache=True):
        """Данные предмета для создания ордера: id, maxRank, лоты, подтипы."""
        slug = str(slug or "").strip()
        if not slug:
            raise MarketError("Не задан слаг предмета — продажа невозможна.")
        if use_cache:
            with self._lock:
                cached = self._item_cache.get(slug)
            if cached:
                return dict(cached)
        payload = self._request("GET", f"/items/{slug}")
        meta = item_meta_of(payload, slug)
        if not meta.get("id"):
            raise MarketError(
                "warframe.market не вернул ID предмета — создать ордер нельзя.")
        with self._lock:
            self._item_cache[slug] = dict(meta)
        return dict(meta)

    def orders(self, slug, force=False):
        """Все видимые ордера предмета (для подсказки цены), TTL 60 с.

        Основной путь по спецификации — ``/v2/items/{slug}/orders`` (проверен),
        запасной — ``/v2/orders/item/{slug}``.
        """
        slug = str(slug or "").strip()
        if not slug:
            return []
        now = time.time()
        if not force and self.orders_ttl > 0:
            with self._lock:
                cached = self._orders_cache.get(slug)
            if cached and now - cached[0] < self.orders_ttl:
                return [dict(x) for x in cached[1]]
        errors = []
        for path in (f"/items/{slug}/orders", f"/orders/item/{slug}"):
            try:
                payload = self._request("GET", path)
            except MarketError as exc:
                errors.append(str(exc))
                continue
            found = orders_of(payload)
            with self._lock:
                self._orders_cache[slug] = (now, [dict(x) for x in found])
            return found
        if errors:
            raise MarketError(errors[-1])
        return []

    def top_orders(self, slug, rank=None, force=False):
        """Топ-5 sell и buy от онлайн-игроков: ``/v2/orders/item/{slug}/top``."""
        slug = str(slug or "").strip()
        if not slug:
            return {"sell": [], "buy": []}
        key = (slug, None if rank is None else int(rank))
        now = time.time()
        if not force and self.orders_ttl > 0:
            with self._lock:
                cached = self._top_cache.get(key)
            if cached and now - cached[0] < self.orders_ttl:
                return dict(cached[1])
        params = {"rank": int(rank)} if rank is not None else None
        payload = self._request("GET", f"/orders/item/{slug}/top", params=params)
        data = data_of(payload)
        result = {"sell": [], "buy": []}
        if isinstance(data, dict):
            for kind in ("sell", "buy"):
                orders = data.get(kind)
                if isinstance(orders, list):
                    result[kind] = [dict(x) for x in orders if isinstance(x, dict)]
            if not result["sell"] and not result["buy"]:
                # запасной разбор: data — список ордеров
                for order in orders_of(payload):
                    kind = str(order.get("type") or "").lower()
                    if kind in result:
                        result[kind].append(dict(order))
        elif isinstance(data, list):
            for order in data:
                if not isinstance(order, dict):
                    continue
                kind = str(order.get("type") or "").lower()
                if kind in result:
                    result[kind].append(dict(order))
        for kind in ("sell", "buy"):
            result[kind].sort(key=lambda o: _integer(o.get("platinum")) or 0,
                              reverse=(kind == "buy"))
            result[kind] = result[kind][:5]
        with self._lock:
            self._top_cache[key] = (now, result)
        return result

    # ── каталог предметов и поиск ──────────────────────────────────────
    def items_catalog(self, force=False):
        """Список предметов ``GET /v2/items?language=ru`` с TTL 60 с."""
        now = time.time()
        if not force and self.catalog_ttl > 0:
            with self._lock:
                if self._catalog is not None and now - self._catalog_ts < self.catalog_ttl:
                    return [dict(x) for x in self._catalog]
        payload = self._request("GET", "/items", params={"language": self.language})
        rows = data_of(payload)
        if not isinstance(rows, list):
            rows = orders_of(payload) if isinstance(payload, dict) else []
        catalog = []
        for row in rows or []:
            item = catalog_item(row)
            if item.get("slug"):
                catalog.append(item)
        if not catalog:
            raise MarketError(
                "warframe.market вернул пустой список предметов — "
                "возможно, API изменился.")
        with self._lock:
            self._catalog = catalog
            self._catalog_ts = now
        return [dict(x) for x in catalog]

    def search_items(self, query, limit=60):
        """Поиск по каталогу (ru/en, rapidfuzz) — ``[(слаг, название), …]``."""
        return filter_catalog(self.items_catalog(), query, limit=limit)

    def item_by_slug(self, slug):
        """Название/слаг из каталога без лишнего запроса (для «Моих ордеров»)."""
        slug = str(slug or "")
        if not slug:
            return None
        try:
            for item in self.items_catalog():
                if item.get("slug") == slug:
                    return dict(item)
        except MarketError:
            pass
        return None

    # ── статистика цен (v1) ────────────────────────────────────────────
    def statistics(self, slug, force=False):
        """История цен ``GET /v1/items/{slug}/statistics`` (TTL 5 мин).

        Возвращает ``{"48h": [...], "90d": [...]}``; точки — ``ts/price/min/max/avg/volume``.
        """
        slug = str(slug or "").strip()
        if not slug:
            raise MarketError("Не задан слаг предмета — статистики нет.")
        now = time.time()
        if not force and self.stats_ttl > 0:
            with self._lock:
                cached = self._stats_cache.get(slug)
            if cached and now - cached[0] < self.stats_ttl:
                return cached[1]
        payload = self._request("GET", f"{BASE}/v1/items/{slug}/statistics")
        series = statistics_series(payload)
        with self._lock:
            self._stats_cache[slug] = (now, series)
        return series

    def price_hint(self, slug, rank=None):
        """Подсказка цены: низшая sell, высшая buy, число ордеров, ссылка на сайт.

        Сетевые проблемы не роняют диалог: возвращается пустая подсказка
        с текстом ошибки в ``error`` (цену можно ввести руками).
        """
        hint = {"sell": None, "buy": None, "sell_online": 0, "buy_online": 0,
                "count": 0, "sell_offline": False, "error": None,
                "url": f"{SITE}/items/{slug}"}
        try:
            orders = self.orders(slug)
            if rank is not None:
                rank = int(rank)
                filtered = [o for o in orders
                            if _integer(o.get("rank")) in (rank, None)]
                orders = filtered or orders
            hint.update(summarize_orders(orders))
        except MarketError as exc:
            hint["error"] = str(exc)
        except Exception as exc:
            hint["error"] = f"подсказка цены недоступна ({type(exc).__name__})"
        return hint

    def token_state(self):
        """Состояние токена по ``GET /v2/me`` (Bearer): для чипа «🔑».

        Возвращает ``"ok"`` (200), ``"expired"`` (401/токен не принят),
        ``"missing"`` (токена нет — запрос не уходит) или ``"error"``
        (сеть/прочие ошибки). Заголовок всегда ``Authorization: Bearer …``:
        ``set_token``/``token_from_config`` нормализуют ``JWT``/``Bearer``.
        """
        if not self.has_token():
            return "missing"
        try:
            self._request("GET", "/me", auth=True)
            return "ok"
        except MarketAuthError:
            return "expired"
        except MarketError:
            return "error"

    def token_ok(self):
        """Проверка токена через ``GET /v2/me`` (401 → ``False``)."""
        return self.token_state() == "ok"

    def my_orders(self):
        """Свои ордера (``GET /v2/orders/my``); без токена — понятная ошибка."""
        payload = self._request("GET", "/orders/my", auth=True)
        data = data_of(payload)
        return data if isinstance(data, list) else orders_of(payload)

    def get_order(self, order_id):
        """Один ордер по id (нужен для полного закрытия)."""
        payload = self._request("GET", f"/order/{order_id}", auth=True)
        data = data_of(payload)
        return data if isinstance(data, dict) else {}

    # ── создание ───────────────────────────────────────────────────────
    def validate_order(self, platinum, quantity, rank=None, per_trade=None,
                       meta=None, subtype=None):
        """Локальная проверка полей до отправки (русские сообщения)."""
        meta = dict(meta or {})
        platinum_i = _integer(platinum)
        if platinum_i is None:
            raise MarketValidationError("Цена должна быть целым числом платины.")
        if not 1 <= platinum_i <= MAX_PLATINUM:
            raise MarketValidationError(
                f"Цена должна быть от 1 до {MAX_PLATINUM} платины.")
        quantity_i = _integer(quantity)
        if quantity_i is None:
            raise MarketValidationError("Количество должно быть целым числом.")
        if not 1 <= quantity_i <= MAX_QUANTITY:
            raise MarketValidationError(
                f"Количество должно быть от 1 до {MAX_QUANTITY}.")
        max_rank = _integer(meta.get("max_rank")) or 0
        bulk = bool(meta.get("bulk"))
        subtypes = [str(x) for x in (meta.get("subtypes") or []) if x]
        if rank is not None:
            rank_i = _integer(rank)
            if rank_i is None or rank_i < 0:
                raise MarketValidationError("Ранг мода должен быть числом от 0.")
            if max_rank and rank_i > max_rank:
                raise MarketValidationError(
                    f"Ранг мода должен быть от 0 до {max_rank}.")
        elif max_rank:
            raise MarketValidationError(
                f"Для этого мода обязателен ранг (0..{max_rank}).")
        if per_trade is not None:
            per_trade_i = _integer(per_trade)
            if per_trade_i is None or not 1 <= per_trade_i <= 6:
                raise MarketValidationError("Лот должен быть от 1 до 6.")
            if quantity_i % per_trade_i:
                raise MarketValidationError(
                    "Количество должно делиться на лот нацело.")
        elif bulk:
            raise MarketValidationError(
                "Для этого предмета обязателен лот (1..6), который делит количество.")
        if subtype is not None and subtypes and str(subtype) not in subtypes:
            raise MarketValidationError(
                "Подтип не из списка предмета: " + ", ".join(subtypes))
        if not subtype and subtypes:
            raise MarketValidationError(
                "Для этого предмета обязателен подтип: " + ", ".join(subtypes))

    def create_order(self, item_id, platinum, quantity, visible=True, rank=None,
                     per_trade=None, subtype=None, charges=None, meta=None,
                     dry_run=None, order_type="sell"):
        """Создаёт ордер (``POST /v2/order``) — только по кнопке пользователя.

        :param order_type: ``"sell"`` (по умолчанию) или ``"buy"``.
        :param item_id: ID предмета из :meth:`item`.
        :param meta: необязательные данные предмета — включает проверку
            ранга/лота/подтипа.
        :param dry_run: ``True`` — показать тело, но не отправлять запрос
            (по умолчанию — настройка ``market_dry_run``).
        """
        item_id = str(item_id or "").strip()
        if not item_id:
            raise MarketValidationError("Не задан ID предмета — создать ордер нельзя.")
        order_type = str(order_type or "sell").lower()
        if order_type not in ("sell", "buy"):
            raise MarketValidationError("Тип ордера должен быть sell или buy.")
        if not self.has_token() and not (self.dry_run if dry_run is None else dry_run):
            raise MarketAuthError(
                "Нет токена warframe.market: вставьте JWT в диалоге «продать».")
        self.validate_order(platinum, quantity, rank=rank, per_trade=per_trade,
                            meta=meta, subtype=subtype)
        body = {
            "itemId": item_id,
            "type": order_type,
            "platinum": int(platinum),
            "quantity": int(quantity),
            "visible": bool(visible),
        }
        if rank is not None:
            body["rank"] = int(rank)
        if per_trade is not None:
            body["perTrade"] = int(per_trade)
        if subtype:
            body["subtype"] = str(subtype)
        if charges is not None:
            body["charges"] = int(charges)
        run_dry = self.dry_run if dry_run is None else bool(dry_run)
        if run_dry:
            return {"dry_run": True, "body": dict(body),
                    "url": f"{API}/order"}
        payload = self._request("POST", "/order", body=body, auth=True)
        data = data_of(payload)
        return data if isinstance(data, dict) else {"raw": payload}

    # ── обновление / закрытие / удаление ───────────────────────────────
    def update_order(self, order_id, **fields):
        """``PATCH /v2/order/{id}``: цена, количество, видимость, ранг и т.п."""
        allowed = {"platinum": "platinum", "quantity": "quantity",
                   "visible": "visible", "rank": "rank", "charges": "charges",
                   "subtype": "subtype", "per_trade": "perTrade",
                   "perTrade": "perTrade", "amber_stars": "amberStars",
                   "cyan_stars": "cyanStars", "amberStars": "amberStars",
                   "cyanStars": "cyanStars"}
        body = {}
        for key, value in fields.items():
            if value is None:
                continue
            api_key = allowed.get(key)
            if not api_key:
                raise MarketValidationError(f"Поле «{key}» нельзя менять.")
            body[api_key] = value
        if not body:
            raise MarketValidationError("Нечего обновлять: не задано ни одно поле.")
        if "platinum" in body:
            platinum = _integer(body["platinum"])
            if platinum is None or not 1 <= platinum <= MAX_PLATINUM:
                raise MarketValidationError(
                    f"Цена должна быть от 1 до {MAX_PLATINUM} платины.")
            body["platinum"] = platinum
        if "quantity" in body:
            quantity = _integer(body["quantity"])
            if quantity is None or not 1 <= quantity <= MAX_QUANTITY:
                raise MarketValidationError(
                    f"Количество должно быть от 1 до {MAX_QUANTITY}.")
            body["quantity"] = quantity
        payload = self._request("PATCH", f"/order/{order_id}", body=body, auth=True)
        data = data_of(payload)
        return data if isinstance(data, dict) else {"raw": payload}

    def close_order(self, order_id, quantity=None):
        """``POST /v2/order/{id}/close``: частичное/полное закрытие ордера.

        Без ``quantity`` закрывает весь остаток (нужно прочитать ордер, чтобы
        узнать количество: API требует точное число).
        """
        if quantity is None:
            order = self.get_order(order_id) or {}
            quantity = _integer(order.get("quantity"))
            if not quantity:
                raise MarketValidationError(
                    "Не удалось узнать количество в ордере — закройте его на сайте.")
        quantity = _integer(quantity)
        if quantity is None or not 1 <= quantity <= MAX_QUANTITY:
            raise MarketValidationError(
                f"Количество для закрытия должно быть от 1 до {MAX_QUANTITY}.")
        payload = self._request("POST", f"/order/{order_id}/close",
                                body={"quantity": quantity}, auth=True)
        data = data_of(payload)
        return data if isinstance(data, dict) else {"raw": payload}

    def delete_order(self, order_id):
        """``DELETE /v2/order/{id}``: полностью удалить свой ордер."""
        payload = self._request("DELETE", f"/order/{order_id}", auth=True)
        data = data_of(payload)
        return data if isinstance(data, dict) else {"raw": payload}

    def group_visibility(self, visible, type=None, group="all"):
        """``PATCH /v2/orders/group/{group}``: скрыть/показать все ордера."""
        body = {"visible": bool(visible)}
        if type:
            body["type"] = str(type)
        payload = self._request("PATCH", f"/orders/group/{group}", body=body,
                                auth=True)
        data = data_of(payload)
        return data if isinstance(data, dict) else {"raw": payload}

    def hide_all(self, type=None):
        """Скрыть все свои ордера (кнопка «скрыть все мои» на сайте)."""
        return self.group_visibility(False, type=type, group="all")

    def set_visible(self, order_id, visible):
        """Показать/скрыть один ордер (``PATCH /v2/order/{id}``)."""
        return self.update_order(order_id, visible=bool(visible))

    def hide_order(self, order_id):
        return self.set_visible(order_id, False)

    def show_order(self, order_id):
        return self.set_visible(order_id, True)

    def repost_order(self, order_id, visible=None):
        """«Выставить заново»: создать копию ордера и закрыть старый.

        Сначала читаем ордер (``GET /v2/order/{id}``), затем ``POST /v2/order``
        с теми же полями (тип sell/buy тоже сохраняется), потом закрываем
        старый остаток (``POST /v2/order/{id}/close``). В ``dry_run`` старый
        ордер не закрывается.
        """
        order = self.get_order(order_id) or {}
        item_id = order.get("itemId") or order.get("item_id")
        if not item_id:
            raise MarketValidationError(
                "В ордере нет ID предмета — «выставить заново» невозможно.")
        order_type = str(order.get("type") or "sell").lower()
        platinum = _integer(order.get("platinum"))
        quantity = _integer(order.get("quantity"))
        if not platinum or not quantity:
            raise MarketValidationError(
                "В ордере нет цены/количества — «выставить заново» невозможно.")
        rank = order.get("rank")
        per_trade = order.get("perTrade", order.get("per_trade"))
        subtype = order.get("subtype")
        charges = order.get("charges")
        created = self.create_order(
            item_id, platinum, quantity,
            visible=bool(order.get("visible", True) if visible is None else visible),
            rank=rank, per_trade=per_trade, subtype=subtype, charges=charges,
            order_type=order_type)
        if isinstance(created, dict) and created.get("dry_run"):
            return {"dry_run": True, "created": created, "closed": None}
        closed = self.close_order(order_id, quantity=quantity)
        return {"dry_run": False, "created": created, "closed": closed}
