# -*- coding: utf-8 -*-
"""Уведомления Windows: трей-иконка, тосты и события (без сторонних библиотек).

Что здесь есть
--------------
* :class:`NotificationManager` — логика уведомлений без Qt: какие события
  включены (``notify_events``), анти-спам (``notify_cooldown_min``), фильтр
  фокуса, пороги цен/фиссур. Все методы можно гонять в тестах на заглушках.
* :class:`NotificationPoller` — фоновый поток: раз в ``notify_poll_sec``
  проверяет мои ордера, события мира и цены. GUI не блокируется.
* :func:`build_tray` — фабрика трея на ``QSystemTrayIcon``: иконка, меню
  «Открыть / Обновления / Выход», нативные тосты Windows через
  ``showMessage``. Qt импортируется лениво; без трея (headless) модуль
  импортируется и работает, ничего не падая.

События (ключи ``notify_events``)
---------------------------------
``order_bought``   — у sell-ордера уменьшилось/пропало quantity («куплен ордер»);
``price_drop``     — lowest sell стал ниже моей цены на ``notify_price_drop_pct``%;
``baro_arrival``   — Баро Ки'Тиир прилетел;
``baro_leaving``   — Баро улетает через ``notify_baro_leaving_min`` (по умолч. 1 ч);
``fissure_ending`` — фиссура нужной эры кончается через
                     ``notify_fissure_ending_min`` (по умолч. 5 мин);
``chat``           — заглушка-хук для будущего модуля чата
                     (:meth:`NotificationManager.chat_message`).

Модуль не импортирует PyQt5 на уровне модуля: логика тестируется без GUI,
а трей создаётся только через :func:`build_tray`.
"""

import threading
try:
    import branding
except Exception:                       # pragma: no cover — старая сборка
    branding = None
import time

# ─────────────────────────────────────────────────────────────────────────
#  Типы событий
# ─────────────────────────────────────────────────────────────────────────
KIND_ORDER_BOUGHT = "order_bought"
KIND_PRICE_DROP = "price_drop"
KIND_BARO_ARRIVAL = "baro_arrival"
KIND_BARO_LEAVING = "baro_leaving"
KIND_FISSURE_ENDING = "fissure_ending"
KIND_CHAT = "chat"

#: Служебные уведомления меню трея (в notify_events не входят; force=True).
KIND_UPDATES = "updates"

ALL_KINDS = (KIND_ORDER_BOUGHT, KIND_PRICE_DROP, KIND_BARO_ARRIVAL,
             KIND_BARO_LEAVING, KIND_FISSURE_ENDING, KIND_CHAT)

#: Настройки по умолчанию (добавляются в warframe.DEFAULT_CONFIG).
DEFAULT_CONFIG = {
    "notify_enabled": True,              # вообще показывать уведомления
    "notify_sound": True,                # звук Windows вместе с тостом
    "notify_only_when_game_focused": False,   # True — только когда игра в фокусе
    "notify_skip_when_game_focused": False,   # True — только когда игра НЕ в фокусе
    "notify_events": list(ALL_KINDS),    # какие события показывать
    "notify_cooldown_min": 10,           # не чаще одного на событие в N минут
    "notify_price_drop_pct": 10,         # «цена ниже моей» — на сколько % ниже
    "notify_fissure_tiers": ["Axi", "Neo"],   # эры фиссур для уведомления
    "notify_poll_sec": 60,               # период фонового опроса, сек
    "notify_baro_leaving_min": 60,       # «Баро улетает» за N минут до конца
    "notify_fissure_ending_min": 5,      # «фиссура кончается» за N минут
    # центр уведомлений в оверлее (t15): тосты/звук/цвета
    "notify_mode": "overlay",            # overlay | system (трей как запасной)
    "notify_toast_sec": 6,               # сколько висит свой тост
    "notify_chat_color": "#4aa8ff",      # акцент чата
    "notify_market_color": "#43d17c",    # акцент «куплен ордер»
    "notify_price_color": "#f5c451",     # акцент «цена ниже моей»
    "notify_sound_choice": "auto",       # auto | market | chat | system
    "notify_sound_file": "",             # свой .wav (не копируем)
}

#: Русские названия эр → сырые ключи API (для notify_fissure_tiers).
TIER_ALIASES = {
    "лит": "lith", "lith": "lith",
    "мезо": "meso", "meso": "meso",
    "нео": "neo", "neo": "neo",
    "акси": "axi", "axi": "axi",
    "реквием": "requiem", "requiem": "requiem",
}

#: Сколько последних «уже показанных» ключей помнить (анти-дубли для Баро/фиссур).
SEEN_LIMIT = 300


# ─────────────────────────────────────────────────────────────────────────
#  Мелкие утилиты (без Qt — тестируются отдельно)
# ─────────────────────────────────────────────────────────────────────────
def log_default(message):
    try:
        print(message, flush=True)
    except Exception:
        pass


def default_config():
    """Копия настроек уведомлений по умолчанию."""
    out = dict(DEFAULT_CONFIG)
    out["notify_events"] = list(DEFAULT_CONFIG["notify_events"])
    out["notify_fissure_tiers"] = list(DEFAULT_CONFIG["notify_fissure_tiers"])
    return out


def _as_bool(value, default=False):
    if value is None:
        return bool(default)
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off", "нет")
    return bool(value)


def _as_int(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _as_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _merged(cfg):
    """DEFAULT_CONFIG, поверх — только заданные ключи ``cfg``."""
    out = default_config()
    for key, value in (cfg or {}).items():
        if value is not None or key not in out:
            out[key] = value
    return out


def enabled_kinds(cfg):
    """Множество включённых типов событий из ``notify_events``.

    Принимает список/кортеж/множество или строку через запятую. Пустое
    значение = все события включены (совместимость со старой настройкой).
    """
    raw = (cfg or {}).get("notify_events", DEFAULT_CONFIG["notify_events"])
    if isinstance(raw, str):
        items = [part.strip() for part in raw.split(",") if part.strip()]
    elif isinstance(raw, dict):
        items = [str(k) for k, v in raw.items() if _as_bool(v, False)]
    elif isinstance(raw, (list, tuple, set)):
        items = [str(x).strip() for x in raw if str(x).strip()]
    else:
        items = []
    if not items:
        return set(ALL_KINDS)
    return {item.lower() for item in items}


def kind_enabled(cfg, kind):
    """Включён ли конкретный тип события."""
    return str(kind or "").lower() in enabled_kinds(cfg)


def cooldown_seconds(cfg):
    """Анти-спам: секунды между одинаковыми уведомлениями (0 — без кулдауна)."""
    return max(0.0, _as_float((cfg or {}).get("notify_cooldown_min", 10), 10.0) * 60.0)


def price_drop_pct(cfg):
    """Порог «цена ниже моей» в процентах (0 — любое снижение)."""
    return max(0.0, _as_float((cfg or {}).get("notify_price_drop_pct", 10), 10.0))


def fissure_tiers(cfg):
    """Множество эр фиссур (сырые ключи API, в нижнем регистре)."""
    raw = (cfg or {}).get("notify_fissure_tiers", DEFAULT_CONFIG["notify_fissure_tiers"])
    if isinstance(raw, str):
        items = [part.strip() for part in raw.split(",") if part.strip()]
    elif isinstance(raw, (list, tuple, set)):
        items = [str(x).strip() for x in raw if str(x).strip()]
    else:
        items = []
    tiers = set()
    for item in items:
        key = item.strip().lower()
        tiers.add(TIER_ALIASES.get(key, key))
    return tiers


def tier_allowed(tier, tiers):
    """Подходит ли эра фиссуры под фильтр (``Axi``/``Акси``/``axi``)."""
    key = str(tier or "").strip().lower()
    if not key:
        return False
    return TIER_ALIASES.get(key, key) in set(tiers or ())


def parse_ts(value):
    """ISO-8601/epoch → epoch-секунды (float) или ``None``.

    Своя копия (без импорта events_api): notifications.py должен работать,
    даже если модуль событий не подключён.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except Exception:
        pass
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


def order_type_of(order):
    """Тип ордера из ответа API: ``sell``/``buy``."""
    return str((order or {}).get("type") or "sell").lower()


def order_item_name(order, name_of=None, fallback="предмет"):
    """Имя предмета ордера: через ``name_of`` или вложенный ``order['item']``."""
    order = order or {}
    if callable(name_of):
        try:
            name = name_of(order)
            if name:
                return str(name)
        except Exception:
            pass
    embedded = order.get("item") if isinstance(order.get("item"), dict) else {}
    i18n = embedded.get("i18n") if isinstance(embedded.get("i18n"), dict) else {}
    ru = i18n.get("ru") if isinstance(i18n.get("ru"), dict) else {}
    return str(ru.get("name") or embedded.get("name")
               or order.get("itemId") or fallback)


def diff_sell_orders(previous, current):
    """Сравнить снимки sell-ордеров ``{id: quantity}``.

    Возвращает список словарей ``{"id", "bought", "left", "gone"}``:
    ``bought`` — сколько штук купили (previous - current); ``gone`` — ордер
    исчез целиком.
    """
    previous = previous or {}
    current = current or {}
    events = []
    for order_id, prev_qty in previous.items():
        try:
            prev_qty = max(0, int(prev_qty))
        except Exception:
            prev_qty = 0
        if order_id not in current:
            events.append({"id": order_id, "bought": prev_qty,
                           "left": 0, "gone": True})
            continue
        try:
            left = max(0, int(current[order_id]))
        except Exception:
            left = 0
        if left < prev_qty:
            events.append({"id": order_id, "bought": prev_qty - left,
                           "left": left, "gone": False})
    return events


# ─────────────────────────────────────────────────────────────────────────
#  Логика уведомлений (без Qt)
# ─────────────────────────────────────────────────────────────────────────
class NotificationManager:
    """Решает, что и когда показывать; отправка — через ``send``.

    :param cfg: настройки (config.json; недостающие ключи берутся из
        :data:`DEFAULT_CONFIG`).
    :param send: ``send(kind, title, text, sound) -> bool`` — отправитель
        (трей/тест). Вызывается из фонового потока, поэтому в продакшене это
        эмит Qt-сигнала, а не прямой вызов виджета.
    :param clock: источник времени (тесты подменяют).
    :param is_game_focused: ``callable() -> bool`` для фильтра фокуса игры.
    :param logger: ``callable(str)`` для лога.
    """

    def __init__(self, cfg=None, send=None, clock=None, is_game_focused=None,
                 logger=None, send_meta=None):
        self.cfg = _merged(cfg)
        self.send = send or (lambda kind, title, text, sound: False)
        #: расширенный отправитель (центр уведомлений): получает dict записи
        self.send_meta = send_meta
        self.clock = clock or time.time
        self.is_game_focused = is_game_focused
        self.logger = logger or log_default
        self._cooldowns = {}       # ключ события -> время последнего показа
        self._seen = {}            # анти-дубли Баро/фиссур (dict как OrderedDict)
        self._orders_snapshot = None   # {order_id: quantity} sell
        self.sent = []             # история отправленных (тесты/диагностика)

    # ── настройки ─────────────────────────────────────────────────────
    def notify_enabled(self):
        return _as_bool(self.cfg.get("notify_enabled", True), True)

    def kind_enabled(self, kind):
        return kind_enabled(self.cfg, kind)

    def cooldown_seconds(self):
        return cooldown_seconds(self.cfg)

    def price_pct(self):
        return price_drop_pct(self.cfg)

    def tiers(self):
        return fissure_tiers(self.cfg)

    def _focus_allows(self):
        checker = self.is_game_focused
        if not callable(checker):
            focused = False
        else:
            try:
                focused = bool(checker())
            except Exception:
                focused = False
        if _as_bool(self.cfg.get("notify_only_when_game_focused", False)):
            return focused
        if _as_bool(self.cfg.get("notify_skip_when_game_focused", False)):
            return not focused
        return True

    def reset(self):
        """Сбросить кулдауны и накопленные снимки (тесты/смена аккаунта)."""
        self._cooldowns.clear()
        self._seen.clear()
        self._orders_snapshot = None
        self.sent.clear()

    # ── отправка ──────────────────────────────────────────────────────
    def can_notify(self, kind, subject="", force=False, now=None):
        """Можно ли показать уведомление сейчас (без отправки)."""
        if not force:
            if not self.notify_enabled():
                return False
            if not self.kind_enabled(kind):
                return False
            if not self._focus_allows():
                return False
        cooldown = self.cooldown_seconds()
        if cooldown > 0 and not force:
            last = self._cooldowns.get(self._cooldown_key(kind, subject))
            if last is not None:
                now = self.clock() if now is None else float(now)
                if now - last < cooldown:
                    return False
        return True

    @staticmethod
    def _cooldown_key(kind, subject=""):
        kind = str(kind or "")
        subject = str(subject or "")
        return f"{kind}:{subject}" if subject else kind

    def notify(self, kind, title, text, subject="", force=False, sound=None,
               now=None, action=None, payload=None):
        """Показать уведомление. ``True`` — отправлено (или хотя бы попытались).

        ``force=True`` — служебное сообщение пользователю (меню трея):
        игнорирует notify_events/фокус/кулдаун, но не выключенный
        ``notify_enabled`` (если уведомления выключены совсем — молчим).

        ``action``/``payload`` — куда вести клик в центре уведомлений
        (market/chat/events/updates) и данные предмета/чата.
        """
        ts = self.clock() if now is None else float(now)
        if force and not self.notify_enabled():
            return False
        if not self.can_notify(kind, subject, force=force, now=ts):
            return False
        if sound is None:
            sound_on = _as_bool(self.cfg.get("notify_sound", True), True)
        else:
            sound_on = bool(sound)
        record = {"kind": str(kind), "title": str(title or ""),
                  "text": str(text or ""), "subject": str(subject or ""),
                  "sound": bool(sound_on), "ts": ts,
                  "action": str(action or ""),
                  "payload": dict(payload) if isinstance(payload, dict) else {}}
        try:
            if callable(self.send_meta):
                sent = self.send_meta(dict(record))
            else:
                sent = self.send(str(kind), str(title or ""), str(text or ""),
                                 bool(sound_on))
        except Exception as exc:
            self.logger(f"Уведомления: «{kind}» не показано "
                        f"({type(exc).__name__}: {exc})")
            return False
        if sent is False:          # трея нет / отправка невозможна
            return False
        self._cooldowns[self._cooldown_key(kind, subject)] = ts
        self.sent.append({"kind": str(kind), "title": str(title or ""),
                          "text": str(text or ""), "subject": str(subject or ""),
                          "sound": bool(sound_on), "ts": ts,
                          "action": record["action"],
                          "payload": dict(record["payload"])})
        self.logger(f"Уведомление: [{kind}] {title}")
        return True

    # ── «Куплен ордер» ────────────────────────────────────────────────
    def handle_orders(self, orders, name_of=None, now=None):
        """Сравнить мои ордера со снимком и показать «куплен ордер».

        Первый вызов только запоминает снимок (чтобы не спамить историей).
        Считаются только sell-ордера: quantity уменьшилось или ордер исчез.
        """
        current = {}
        for order in (orders or []):
            if not isinstance(order, dict):
                continue
            if order_type_of(order) != "sell":
                continue
            order_id = order.get("id")
            if not order_id:
                continue
            try:
                quantity = max(0, _as_int(order.get("quantity"), 1))
            except Exception:
                quantity = 1
            current[str(order_id)] = {"quantity": quantity, "order": dict(order)}

        previous = self._orders_snapshot
        snapshot = {order_id: item["quantity"]
                    for order_id, item in current.items()}
        self._orders_snapshot = current
        if previous is None:
            return []                   # первый опрос — без уведомлений

        prev_quantities = {order_id: item["quantity"]
                           for order_id, item in previous.items()}
        events = []
        for change in diff_sell_orders(prev_quantities, snapshot):
            info = previous.get(change["id"]) or {}
            order = info.get("order") or {}
            name = order_item_name(order, name_of)
            if change["gone"]:
                text = f"{name}: ордер куплен целиком ({change['bought']} шт.)"
            else:
                text = (f"{name}: куплено {change['bought']} шт., "
                        f"осталось {change['left']}")
            embedded = order.get("item") if isinstance(order.get("item"), dict) else {}
            if self.notify(KIND_ORDER_BOUGHT, "Куплен ордер", text,
                           subject=change["id"], now=now, action="market",
                           payload={"order_id": change["id"],
                                    "item": name,
                                    "item_slug": embedded.get("slug") or "",
                                    "item_name": name,
                                    "my_order_id": change["id"],
                                    "my_price": order.get("platinum")}):
                events.append({"kind": KIND_ORDER_BOUGHT, "id": change["id"],
                               "text": text, **change})
        return events

    # ── «Цена ниже моей» ──────────────────────────────────────────────
    def handle_prices(self, watch_items, lowest_price, now=None):
        """Для предметов из списка/ордеров: lowest sell ниже моей цены.

        ``watch_items`` — ``[{"slug", "name", "price"}]`` (моя цена);
        ``lowest_price`` — ``callable(slug) -> price|None`` (сеть/кэш).
        """
        pct = self.price_pct()
        events = []
        seen_slugs = set()
        for item in (watch_items or []):
            item = item if isinstance(item, dict) else {}
            slug = str(item.get("slug") or "").strip()
            mine = item.get("price")
            if not slug or slug in seen_slugs:
                continue
            if not isinstance(mine, (int, float)) or mine <= 0:
                continue
            seen_slugs.add(slug)
            try:
                raw_lowest = lowest_price(slug)
            except Exception:
                raw_lowest = None
            seller = ""
            if isinstance(raw_lowest, dict):
                seller = str(raw_lowest.get("seller_name")
                             or raw_lowest.get("seller")
                             or raw_lowest.get("user") or "")
                lowest = (raw_lowest.get("price")
                          or raw_lowest.get("platinum")
                          or raw_lowest.get("lowest"))
            else:
                lowest = raw_lowest
            if not isinstance(lowest, (int, float)) or lowest <= 0:
                continue
            mine = float(mine)
            lowest = float(lowest)
            threshold = mine * (1.0 - pct / 100.0) if pct > 0 else mine
            triggered = lowest < mine and lowest <= threshold
            if not triggered:
                continue
            name = str(item.get("name") or slug)
            drop = int(round((mine - lowest) / mine * 100.0))
            text = (f"{name}: lowest sell {int(lowest)}p ниже вашей "
                    f"{int(mine)}p (на {drop}%)")
            if self.notify(KIND_PRICE_DROP, "Цена ниже моей", text,
                           subject=slug, now=now, action="market",
                           payload={"slug": slug, "item": name,
                                    "item_slug": slug, "item_name": name,
                                    "my_price": mine, "new_price": lowest,
                                    "seller_name": seller,
                                    "my_order_id": item.get("order_id") or "",
                                    "order_id": item.get("order_id") or ""}):
                events.append({"kind": KIND_PRICE_DROP, "slug": slug,
                               "price": lowest, "mine": mine, "text": text})
        return events

    # ── события мира: Баро и фиссуры ──────────────────────────────────
    def _remember(self, key):
        """Запомнить ключ уже показанного события (лимит ``SEEN_LIMIT``)."""
        if key in self._seen:
            return False
        self._seen[key] = True
        if len(self._seen) > SEEN_LIMIT:
            for old in list(self._seen.keys())[:len(self._seen) - SEEN_LIMIT]:
                self._seen.pop(old, None)
        return True

    def handle_events(self, snapshot, now=None):
        """Баро прилетел/улетает и фиссуры нужной эры кончаются."""
        now = self.clock() if now is None else float(now)
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        events = []

        # ── Баро Ки'Тиир ──────────────────────────────────────────────
        trader = snapshot.get("voidTrader")
        trader = trader if isinstance(trader, dict) else {}
        if trader:
            activation = parse_ts(trader.get("activation"))
            expiry = parse_ts(trader.get("expiry"))
            location = str(trader.get("location") or "").strip()
            inventory = [x for x in (trader.get("inventory") or [])
                         if isinstance(x, dict)]
            # прилетел: время активации прошло, срок ещё не вышел
            if activation and activation <= now and (not expiry or now < expiry):
                key = f"baro-arrival:{int(activation)}"
                if key not in self._seen:
                    text = "Баро Ки'Тиир прилетел"
                    if location:
                        text += f": {location}"
                    if inventory:
                        text += f" (ассортимент: {len(inventory)})"
                    if self.notify(KIND_BARO_ARRIVAL, "Баро Ки'Тиир", text,
                                   subject=key, now=now, action="events"):
                        self._remember(key)
                        events.append({"kind": KIND_BARO_ARRIVAL, "text": text})
            # улетает: осталось не больше notify_baro_leaving_min
            leaving_min = max(1, _as_int(
                self.cfg.get("notify_baro_leaving_min", 60), 60))
            if expiry and 0 < expiry - now <= leaving_min * 60:
                key = f"baro-leaving:{int(expiry)}"
                if key not in self._seen:
                    minutes = max(1, int(round((expiry - now) / 60.0)))
                    text = f"Баро улетает через ~{minutes} мин"
                    if location:
                        text += f" ({location})"
                    if self.notify(KIND_BARO_LEAVING, "Баро улетает", text,
                                   subject=key, now=now, action="events"):
                        self._remember(key)
                        events.append({"kind": KIND_BARO_LEAVING,
                                       "text": text})

        # ── фиссуры Бездны ────────────────────────────────────────────
        ending_min = max(1, _as_int(
            self.cfg.get("notify_fissure_ending_min", 5), 5))
        ending_sec = ending_min * 60
        tiers = self.tiers()
        for fissure in (snapshot.get("fissures") or []):
            if not isinstance(fissure, dict):
                continue
            expiry = parse_ts(fissure.get("expiry"))
            if not expiry:
                continue
            left = expiry - now
            if not 0 < left <= ending_sec:
                continue
            tier_raw = str(fissure.get("tier") or "")
            if not tier_allowed(tier_raw, tiers):
                continue
            key = (f"fissure:{fissure.get('id') or fissure.get('node') or '?'}:"
                   f"{int(expiry)}:{tier_raw}")
            if key in self._seen:
                continue
            node = str(fissure.get("node") or "").strip()
            minutes = max(1, int(round(left / 60.0)))
            text = f"{tier_raw or 'фиссура'}"
            if node:
                text += f" · {node}"
            text += f": осталось ~{minutes} мин"
            if self.notify(KIND_FISSURE_ENDING,
                           "Фиссура заканчивается", text,
                           subject=key, now=now, action="events",
                           payload={"tier": tier_raw}):
                self._remember(key)
                events.append({"kind": KIND_FISSURE_ENDING, "tier": tier_raw,
                               "text": text})
        return events

    # ── чат (заглушка-хук для будущего модуля) ────────────────────────
    def chat_message(self, author, text, subject=None, now=None):
        """Хук «новое сообщение в чате» — вызовет модуль чата, когда появится."""
        author = str(author or "неизвестный")
        body = str(text or "").strip()
        if not body:
            return False
        return self.notify(KIND_CHAT, f"Сообщение от {author}",
                           body[:240], subject=subject or author, now=now,
                           action="chat",
                           payload={"author": author, "seller_name": author,
                                    "chat_id": str(subject or "")})

    # ── один цикл опроса (вызывается из фонового потока) ──────────────
    def poll_once(self, orders_provider=None, has_token=None, name_of=None,
                  events_provider=None, watch_provider=None,
                  lowest_price=None, now=None):
        """Один проход: ордера (если есть токен), события мира, цены."""
        result = {"orders": [], "prices": [], "events": [], "skipped": []}
        if not self.notify_enabled():
            result["skipped"].append("disabled")
            return result
        now = self.clock() if now is None else float(now)

        token_ok = True
        if callable(has_token):
            try:
                token_ok = bool(has_token())
            except Exception:
                token_ok = False
        if orders_provider is not None:
            if not token_ok:
                # без токена не копим старый снимок: после повторного входа
                # первый опрос снова станет базовым (без ложных «куплен»)
                self._orders_snapshot = None
                result["skipped"].append("orders:no_token")
            else:
                try:
                    orders = orders_provider()
                except Exception as exc:
                    result["skipped"].append(f"orders:error:{type(exc).__name__}")
                    orders = None
                if isinstance(orders, list):
                    result["orders"] = self.handle_orders(
                        orders, name_of=name_of, now=now)
                elif orders is not None:
                    result["skipped"].append("orders:bad_data")
        if events_provider is not None:
            try:
                snapshot = events_provider()
            except Exception as exc:
                result["skipped"].append(f"events:error:{type(exc).__name__}")
                snapshot = None
            if isinstance(snapshot, dict) and snapshot:
                result["events"] = self.handle_events(snapshot, now=now)
        if watch_provider is not None and callable(lowest_price):
            try:
                watch = watch_provider()
            except Exception as exc:
                result["skipped"].append(f"watch:error:{type(exc).__name__}")
                watch = None
            if isinstance(watch, list) and watch:
                result["prices"] = self.handle_prices(
                    watch, lowest_price, now=now)
        return result


# ─────────────────────────────────────────────────────────────────────────
#  Фоновый опрос (GUI не блокируется)
# ─────────────────────────────────────────────────────────────────────────
class NotificationPoller(threading.Thread):
    """Раз в ``notify_poll_sec`` дергает :meth:`NotificationManager.poll_once`."""

    def __init__(self, manager, interval=None, orders_provider=None,
                 has_token=None, name_of=None, events_provider=None,
                 watch_provider=None, lowest_price=None, logger=None,
                 initial_delay=2.0, min_interval=5.0):
        super().__init__(daemon=True, name="wf-notifications")
        self.manager = manager
        self.interval = max(_as_float(min_interval, 5.0), _as_float(
            interval if interval is not None
            else (manager.cfg or {}).get("notify_poll_sec", 60), 60.0))
        self.orders_provider = orders_provider
        self.has_token = has_token
        self.name_of = name_of
        self.events_provider = events_provider
        self.watch_provider = watch_provider
        self.lowest_price = lowest_price
        self.logger = logger or log_default
        self.polls = 0
        self.last_result = None
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._initial_delay = max(0.0, _as_float(initial_delay, 2.0))

    def poll_once_now(self):
        """Выполнить один проход прямо сейчас (в текущем потоке)."""
        result = self.manager.poll_once(
            orders_provider=self.orders_provider,
            has_token=self.has_token,
            name_of=self.name_of,
            events_provider=self.events_provider,
            watch_provider=self.watch_provider,
            lowest_price=self.lowest_price)
        self.last_result = result
        return result

    def poll_now(self):
        """Разбудить поток: следующий проход — сразу."""
        self._wake_event.set()

    def stop(self):
        self._stop_event.set()
        self._wake_event.set()

    def stopped(self):
        return self._stop_event.is_set()

    def run(self):
        # не тормозим старт приложения: первый проход чуть позже
        self._wake_event.wait(self._initial_delay)
        self._wake_event.clear()
        while not self._stop_event.is_set():
            try:
                self.poll_once_now()
                self.polls += 1
            except Exception as exc:
                self.logger(f"Уведомления: опрос не удался "
                            f"({type(exc).__name__}: {exc})")
            self._wake_event.wait(self.interval)
            self._wake_event.clear()


# ─────────────────────────────────────────────────────────────────────────
#  Трей Windows (Qt импортируется лениво)
# ─────────────────────────────────────────────────────────────────────────
def build_tray():
    """Фабрика трея: ``NotificationTray(cfg, on_open=, on_updates=, on_exit=)``.

    Иконка рисуется в памяти (без файлов и сторонних библиотек), меню —
    «Открыть / Проверить обновление / Настройки / Выход», тосты — нативные
    ``QSystemTrayIcon`` (Windows 10/11 показывает их как системные уведомления). Если системный
    трей недоступен (headless/offscreen), создание не падает, а
    ``available()`` возвращает ``False``.
    """
    from PyQt5.QtCore import QObject, Qt
    from PyQt5.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
    from PyQt5.QtWidgets import QApplication, QMenu, QSystemTrayIcon

    class NotificationTray(QObject):
        """Трей-иконка с меню и нативными тостами."""

        MESSAGE_MS = 8000

        def __init__(self, cfg=None, on_open=None, on_updates=None,
                     on_settings=None, on_exit=None, on_about=None,
                     parent=None):
            super().__init__(parent)
            self.cfg = dict(cfg or {})
            self.on_open = on_open
            self.on_updates = on_updates
            self.on_settings = on_settings
            self.on_exit = on_exit
            self.on_about = on_about
            self.app_name = str(getattr(branding, "APP_NAME", "VoidFrameRU")
                                if branding is not None else "VoidFrameRU")
            self._shown = False
            self._available = False
            self.tray = QSystemTrayIcon(self)
            self.tray.setIcon(self._make_icon())
            self.tray.setToolTip(self.app_name)
            self.menu = QMenu()
            self._actions = {}
            for key, title, callback in (
                    ("open", "Открыть", on_open),
                    ("updates", "Проверить обновление", on_updates),
                    ("settings", "Настройки", on_settings),
                    ("about", "О программе", on_about),
                    ("exit", "Выход", on_exit)):
                action = self.menu.addAction(title)
                if key == "settings":
                    try:
                        import ui_icons
                        action.setIcon(ui_icons.gear_icon(16))
                    except Exception:
                        pass
                if callable(callback):
                    action.triggered.connect(
                        lambda _checked=False, cb=callback: cb())
                self._actions[key] = action
            self.tray.setContextMenu(self.menu)
            try:
                self.tray.activated.connect(self._on_activated)
            except Exception:
                pass
            try:
                self._available = bool(QSystemTrayIcon.isSystemTrayAvailable())
            except Exception:
                self._available = False
            self.show()

        @staticmethod
        def _make_icon():
            """Иконка трея из assets/brand (tray.png/tray@2x.png) или запасная."""
            try:
                if branding is not None:
                    tray = branding.brand_path("tray.png")
                    tray2x = branding.brand_path("tray@2x.png")
                    if tray:
                        ico = QIcon(tray)
                        if tray2x:
                            from PyQt5.QtCore import QSize
                            ico.addFile(tray2x, QSize(64, 64))
                        if not ico.isNull():
                            return ico
            except Exception:
                pass
            pix = QPixmap(32, 32)
            pix.fill(Qt.transparent)
            painter = QPainter(pix)
            try:
                painter.setRenderHint(QPainter.Antialiasing)
                painter.setBrush(QColor("#0d131c"))
                painter.setPen(QPen(QColor("#4aa8ff"), 2))
                painter.drawRoundedRect(1, 1, 30, 30, 7, 7)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor("#ffd75e"))
                painter.drawEllipse(9, 9, 14, 14)
                painter.setBrush(QColor("#0d131c"))
                painter.drawEllipse(13, 13, 6, 6)
            finally:
                painter.end()
            return QIcon(pix)

        # ── состояние ─────────────────────────────────────────────────
        def available(self):
            """Есть ли системный трей (headless → False)."""
            return bool(self._available)

        def visible(self):
            return bool(self._shown)

        def menu_titles(self):
            return [action.text() for action in self.menu.actions()]

        def trigger(self, key):
            """Прожать пункт меню (для тестов): ``open``/``updates``/``exit``."""
            action = self._actions.get(str(key))
            if action is None:
                return False
            action.trigger()
            return True

        def _on_activated(self, reason):
            if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
                if callable(self.on_open):
                    try:
                        self.on_open()
                    except Exception:
                        pass

        # ── показ/тосты ───────────────────────────────────────────────
        def show(self):
            if self._shown or not self._available:
                return False
            try:
                self.tray.show()
                self._shown = True
            except Exception:
                self._shown = False
            return self._shown

        def show_message(self, title, text, kind="", sound=None):
            """Нативный тост Windows. ``False`` — трея нет/не удалось."""
            if not self._available:
                return False
            try:
                self.tray.showMessage(str(title or self.app_name),
                                      str(text or ""),
                                      QSystemTrayIcon.Information,
                                      int(self.MESSAGE_MS))
            except Exception:
                return False
            if sound is None:
                sound = _as_bool(self.cfg.get("notify_sound", True), True)
            if _as_bool(sound, True):
                try:
                    QApplication.beep()
                except Exception:
                    pass
            return True

        def hide(self):
            try:
                self.tray.hide()
            except Exception:
                pass
            self._shown = False
            return True

        def stop(self):
            """Убрать иконку и меню (выход из приложения)."""
            self.hide()
            try:
                self.tray.setContextMenu(None)
            except Exception:
                pass
            return True

    return NotificationTray
