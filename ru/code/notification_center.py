# -*- coding: utf-8 -*-
"""Центр уведомлений оверлея: история, цвета, тосты и свой звук.

Без новых зависимостей и без сети. Модуль состоит из трёх слоёв:

* **Модель/хранилище** — :class:`HistoryStore` (``notifications.json``:
  последние 200 записей, атомарная запись, ротация, прочитанность и бейдж
  сохраняются между запусками), :func:`group_records` (Сегодня/Вчера/дата),
  :func:`kind_style`/:func:`badge_state` (цвета по категориям, приоритет
  «самого важного» непрочитанного), :func:`action_for_kind`;
* **Звук** — :class:`SoundPlayer`: программно сгенерированные WAV-звуки
  (маркет/чат/система) в папке данных + свой ``notify_sound_file``;
  системные звуки Windows не используются;
* **UI** — :func:`build_notification_center` (вкладка «Уведомления» в F8) и
  :func:`build_toast_stack` (свои всплывающие тосты в правом верхнем углу,
  стек до 3, без фокуса, авто-скрытие ``notify_toast_sec``).

Qt импортируется лениво: модель, цвета и звук тестируются без GUI.
"""

import datetime
import json
import math
import os
import struct
import time
import uuid
import wave

try:
    import ui_theme as theme
except Exception:                     # pragma: no cover
    theme = None


HISTORY_LIMIT = 200
HISTORY_FILE = "notifications.json"
SCHEMA = 1

# ─────────────────────────────────────────────────────────────────────────
#  Категории/цвета/приоритеты
# ─────────────────────────────────────────────────────────────────────────
CATEGORY_MARKET = "market"
CATEGORY_CHAT = "chat"
CATEGORY_SYSTEM = "system"

_KIND_CATEGORY = {
    "order_bought": CATEGORY_MARKET,
    "price_drop": CATEGORY_MARKET,
    "chat": CATEGORY_CHAT,
    "baro_arrival": CATEGORY_SYSTEM,
    "baro_leaving": CATEGORY_SYSTEM,
    "fissure_ending": CATEGORY_SYSTEM,
    "updates": CATEGORY_SYSTEM,
    "pick": CATEGORY_SYSTEM,
}

#: Алиасы чата: внешние модули/чаты могут присылать разные имена kind.
CHAT_KIND_ALIASES = ("chat_message", "message", "im", "im_message",
                     "new_message")
_KIND_ALIASES = {alias: "chat" for alias in CHAT_KIND_ALIASES}
_KIND_ALIASES.update({"update": "updates", "system_update": "updates",
                      "system-update": "updates"})

_KIND_BADGE = {
    "order_bought": "продажа",
    "price_drop": "цена",
    "chat": "чат",
    "baro_arrival": "баро",
    "baro_leaving": "баро",
    "fissure_ending": "фиссура",
    "updates": "обновление",
    "pick": "награда",
}

#: Приоритет бейджа: цена > продажа > чат > система (t16).
_KIND_PRIORITY = {
    "price_drop": 30,
    "order_bought": 25,
    "chat": 20,
    "fissure_ending": 12,
    "baro_arrival": 11,
    "baro_leaving": 10,
    "updates": 5,
    "pick": 1,
    "system": 10,
}

DEFAULT_CHAT_COLOR = "#4aa8ff"
DEFAULT_MARKET_COLOR = "#43d17c"
DEFAULT_PRICE_COLOR = "#f5c451"
DEFAULT_SYSTEM_COLOR = "#93a1b5"

#: Варианты кнопки «Проверить уведомление» в настройках.
TEST_NOTIFICATION_TYPES = (
    ("market", "Маркет (куплен ордер)"),
    ("price", "Цена ниже моей"),
    ("chat", "Чат"),
    ("system", "Система"),
)


def test_notification_record(kind="market"):
    """Демо-уведомление для настроек: «ТЕСТ», без реального события."""
    kind = str(kind or "market").lower()
    mapping = {
        "market": ("order_bought", "ТЕСТ: куплен ордер",
                   "Проверка уведомления — маркет", "market"),
        "price": ("price_drop", "ТЕСТ: цена ниже моей",
                  "Проверка уведомления — цена", "market"),
        "chat": ("chat", "ТЕСТ: сообщение в чате",
                 "Проверка уведомления — чат", "chat"),
        "system": ("updates", "ТЕСТ: системное уведомление",
                   "Проверка уведомления — система", "updates"),
    }
    event_kind, title, text, action = mapping.get(kind, mapping["market"])
    return {
        "kind": event_kind,
        "title": title,
        "text": text,
        "ts": time.time(),
        "sound": True,
        "action": action,
        "payload": {"test": True},
    }


def dispatch_test_notification(kind="market", **kwargs):
    """Показать «ТЕСТ»-уведомление (история/тост/трей/звук).

    Не проходит через NotificationManager: не зависит от notify_enabled,
    не трогает кулдауны и не меняет флаги настроек.
    """
    return dispatch_notification(test_notification_record(kind), **kwargs)

#: Палитра для выбора акцента в настройках (имя → ключ ui_theme.PALETTE).
COLOR_CHOICES = (
    ("accent", "accent"),
    ("success", "success"),
    ("warning", "warning"),
    ("danger", "danger"),
    ("platinum", "platinum"),
    ("ducats", "ducats"),
    ("muted", "muted"),
)


def palette_color(key, fallback):
    if theme is not None:
        try:
            return str(theme.PALETTE.get(key) or fallback)
        except Exception:
            pass
    return fallback


def canonical_kind(kind):
    """Нормализовать kind: ``chat_message``/``message``/``im`` → ``chat``."""
    key = str(kind or "").strip().lower()
    return _KIND_ALIASES.get(key, key)


def kind_category(kind):
    return _KIND_CATEGORY.get(canonical_kind(kind), CATEGORY_SYSTEM)


def _cfg_bool(value, default=False):
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off", "нет")
    if value is None:
        return bool(default)
    return bool(value)


def kind_style(cfg, kind):
    """Стиль записи: ``{"category","badge","color","priority"}``.

    Чат-типы (``chat``, ``chat_message``, ``message``, ``im``) дают
    category=chat, бейдж «чат» и синий акцент/``notify_chat_color``.
    """
    cfg = cfg or {}
    kind = canonical_kind(kind)
    category = kind_category(kind)
    if kind == "order_bought":
        color = str(cfg.get("notify_market_color")
                    or palette_color("success", DEFAULT_MARKET_COLOR))
    elif kind == "price_drop":
        color = str(cfg.get("notify_price_color")
                    or palette_color("warning", DEFAULT_PRICE_COLOR))
    elif category == CATEGORY_CHAT:
        color = str(cfg.get("notify_chat_color")
                    or palette_color("accent", DEFAULT_CHAT_COLOR))
    else:
        color = palette_color("muted", DEFAULT_SYSTEM_COLOR)
    return {"category": category,
            "badge": _KIND_BADGE.get(kind, "система"),
            "color": color,
            "priority": _KIND_PRIORITY.get(kind, 0)}


def action_for_kind(kind):
    """Куда ведёт клик по записи: chat/market/events/updates/center."""
    kind = canonical_kind(kind)
    if kind == "chat":
        return "chat"
    if kind in ("order_bought", "price_drop"):
        return "market"
    if kind in ("baro_arrival", "baro_leaving", "fissure_ending"):
        return "events"
    if kind == "updates":
        return "updates"
    return "center"


def badge_state(cfg, records):
    """``(count, color)`` для колокольчика: цвет самого важного непрочитанного."""
    unread = [record for record in (records or [])
              if isinstance(record, dict) and not record.get("read")]
    if not unread:
        return 0, palette_color("muted", DEFAULT_SYSTEM_COLOR)
    best = max(unread, key=lambda record: kind_style(
        cfg, record.get("kind")).get("priority", 0))
    return len(unread), kind_style(cfg, best.get("kind"))["color"]


# ─────────────────────────────────────────────────────────────────────────
#  Даты и группировка
# ─────────────────────────────────────────────────────────────────────────
def _local_date(ts):
    return datetime.datetime.fromtimestamp(float(ts)).date()


def group_records(records, now=None):
    """Сгруппировать записи по датам: [("Сегодня", [...]), ("Вчера", [...]), …].

    Внутри групп — новые сверху; пустые группы не возвращаются.
    """
    records = [record for record in (records or []) if isinstance(record, dict)]
    records.sort(key=lambda record: float(record.get("ts") or 0), reverse=True)
    now_date = _local_date(now if now is not None else time.time())
    yesterday = now_date - datetime.timedelta(days=1)
    groups = []
    index = {}
    for record in records:
        try:
            day = _local_date(record.get("ts"))
        except Exception:
            day = now_date
        if day == now_date:
            label = "Сегодня"
        elif day == yesterday:
            label = "Вчера"
        else:
            label = day.strftime("%d.%m.%Y")
        key = label
        if key not in index:
            index[key] = []
            groups.append((label, index[key]))
        index[key].append(record)
    return groups


def format_clock(ts):
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%H:%M")
    except Exception:
        return "—"


# ─────────────────────────────────────────────────────────────────────────
#  История: notifications.json (атомарно, ротация, прочитанность)
# ─────────────────────────────────────────────────────────────────────────
class HistoryStore:
    """Последние ``limit`` записей центра уведомлений.

    Файл: ``{"schema": 1, "records": [...]}``; запись:
    ``{id, ts, kind, title, text, action, payload, read}``.
    Запись атомарная (tmp + ``os.replace``), старые записи ротируются.
    """

    def __init__(self, path=None, limit=HISTORY_LIMIT, clock=None,
                 logger=None):
        self.path = str(path or HISTORY_FILE)
        self.limit = max(1, int(limit or HISTORY_LIMIT))
        self.clock = clock or time.time
        self.logger = logger or (lambda message: None)
        self._records = []
        self.load()

    # ── файл ─────────────────────────────────────────────────────────
    def load(self):
        try:
            if not os.path.isfile(self.path):
                self._records = []
                return []
            with open(self.path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
            raw = data.get("records") if isinstance(data, dict) else data
            if not isinstance(raw, list):
                raw = []
            records = []
            seen = set()
            for item in raw:
                record = _normalize_record(item)
                if record is None or record["id"] in seen:
                    continue
                seen.add(record["id"])
                records.append(record)
            records.sort(key=lambda record: record["ts"], reverse=True)
            self._records = records[:self.limit]
        except Exception as exc:
            self.logger(f"Центр уведомлений: история не прочитана "
                        f"({type(exc).__name__}: {exc})")
            self._records = []
        return self.records()

    def save(self):
        try:
            folder = os.path.dirname(os.path.abspath(self.path))
            if folder:
                os.makedirs(folder, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                json.dump({"schema": SCHEMA, "records": self._records},
                          handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(tmp, self.path)
            return True
        except Exception as exc:
            self.logger(f"Центр уведомлений: история не сохранена "
                        f"({type(exc).__name__}: {exc})")
            try:
                if os.path.exists(self.path + ".tmp"):
                    os.remove(self.path + ".tmp")
            except Exception:
                pass
            return False

    # ── данные ───────────────────────────────────────────────────────
    def add(self, kind, title, text="", action="", payload=None, ts=None,
            read=False):
        """Добавить запись (новые сверху, кап ``limit``) и сохранить."""
        record = _normalize_record({
            "id": uuid.uuid4().hex[:12],
            "ts": self.clock() if ts is None else ts,
            "kind": kind,
            "title": title,
            "text": text,
            "action": action or action_for_kind(kind),
            "payload": dict(payload or {}) if isinstance(payload, dict) else {},
            "read": bool(read),
        })
        if record is None:
            raise ValueError("пустая запись уведомления")
        self._records.insert(0, record)
        if len(self._records) > self.limit:
            self._records = self._records[:self.limit]
        self.save()
        return dict(record)

    def add_record(self, record):
        """Добавить готовый dict (id/ts сохраняются, если заданы)."""
        normalized = _normalize_record(dict(record or {}))
        if normalized is None:
            raise ValueError("пустая запись уведомления")
        existing = {item["id"] for item in self._records}
        if normalized["id"] in existing:
            return dict(normalized)
        self._records.insert(0, normalized)
        self._records.sort(key=lambda item: item["ts"], reverse=True)
        self._records = self._records[:self.limit]
        self.save()
        return dict(normalized)

    def records(self):
        return [dict(record) for record in self._records]

    def grouped(self, now=None):
        return group_records(self.records(), now=now)

    def find(self, record_id):
        for record in self._records:
            if str(record.get("id")) == str(record_id):
                return dict(record)
        return None

    def mark_read(self, record_id):
        for record in self._records:
            if str(record.get("id")) == str(record_id):
                if not record.get("read"):
                    record["read"] = True
                    self.save()
                return True
        return False

    def mark_all_read(self):
        changed = 0
        for record in self._records:
            if not record.get("read"):
                record["read"] = True
                changed += 1
        if changed:
            self.save()
        return changed

    def clear(self):
        had = bool(self._records)
        self._records = []
        if had:
            self.save()
        return had

    def unread_count(self):
        return sum(1 for record in self._records if not record.get("read"))

    def unread_records(self):
        return [dict(record) for record in self._records
                if not record.get("read")]

    def badge(self, cfg=None):
        return badge_state(cfg or {}, self._records)


#: Поля предмета/ордера, которые храним в записи (t15: правка в один клик).
ITEM_FIELDS = ("item_slug", "item_name", "my_order_id", "my_price",
               "new_price", "seller_name")


def _normalize_record(item):
    if not isinstance(item, dict):
        return None
    kind = str(item.get("kind") or "").strip() or "system"
    title = str(item.get("title") or "").strip()
    text = str(item.get("text") or "").strip()
    if not title and not text:
        return None
    try:
        ts = float(item.get("ts") or 0)
    except Exception:
        ts = 0.0
    if ts <= 0:
        ts = time.time()
    payload = (dict(item.get("payload"))
               if isinstance(item.get("payload"), dict) else {})
    record = {
        "id": str(item.get("id") or uuid.uuid4().hex[:12]),
        "ts": ts,
        "kind": kind,
        "title": title or kind,
        "text": text,
        "action": str(item.get("action") or action_for_kind(kind)),
        "payload": payload,
        "read": bool(item.get("read")),
    }
    # поля предмета: сначала верхний уровень, затем payload (slug/item/order_id)
    aliases = {
        "item_slug": ("item_slug", "slug"),
        "item_name": ("item_name", "item", "name"),
        "my_order_id": ("my_order_id", "order_id"),
        "my_price": ("my_price", "mine", "price"),
        "new_price": ("new_price", "lowest", "lowest_price"),
        "seller_name": ("seller_name", "seller", "author", "user"),
    }
    for field in ITEM_FIELDS:
        value = item.get(field)
        if value in (None, ""):
            for alias in aliases.get(field, (field,)):
                if payload.get(alias) not in (None, ""):
                    value = payload.get(alias)
                    break
        record[field] = value
    return record


# ─────────────────────────────────────────────────────────────────────────
#  Свой звук: программно сгенерированные WAV (без системных сигналов)
# ─────────────────────────────────────────────────────────────────────────
#: (частота, длительность) — короткие мягкие тона, без внешних файлов.
SOUND_DEFS = {
    "market": ((880.0, 0.10, 0.55), (1174.7, 0.16, 0.40)),
    "chat": ((659.3, 0.10, 0.50), (880.0, 0.15, 0.35)),
    "system": ((440.0, 0.10, 0.25), (523.3, 0.12, 0.18)),
}
SOUND_NAMES = tuple(SOUND_DEFS.keys())


class SoundPlayer:
    """Генерирует и проигрывает встроенные звуки; системные не использует."""

    def __init__(self, data_dir="", play_fn=None, logger=None):
        self.data_dir = str(data_dir or "")
        self.play_fn = play_fn
        self.logger = logger or (lambda message: None)
        self._paths = {}

    def sound_path(self, name):
        name = str(name or "system").lower()
        if name not in SOUND_DEFS:
            name = "system"
        if name in self._paths:
            return self._paths[name]
        if not self.data_dir:
            return ""
        folder = os.path.join(self.data_dir, "sounds")
        path = os.path.join(folder, f"{name}.wav")
        if not os.path.isfile(path):
            try:
                os.makedirs(folder, exist_ok=True)
                self._write_wave(path, SOUND_DEFS[name])
            except Exception as exc:
                self.logger(f"Звук: не сгенерирован {name} "
                            f"({type(exc).__name__}: {exc})")
                return ""
        self._paths[name] = path
        return path

    @staticmethod
    def _write_wave(path, notes, rate=22050):
        frames = bytearray()
        for frequency, duration, volume in notes:
            count = max(1, int(rate * duration))
            for index in range(count):
                # мягкая огибающая: плавное появление/затухание
                envelope = math.sin(math.pi * index / count) ** 2
                sample = (math.sin(2.0 * math.pi * frequency * index / rate)
                          * volume * envelope)
                frames += struct.pack("<h", int(max(-1.0, min(1.0, sample))
                                                * 32767))
        tmp = path + ".tmp"
        with wave.open(tmp, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(bytes(frames))
        os.replace(tmp, path)

    def ensure_sounds(self):
        return {name: self.sound_path(name) for name in SOUND_NAMES}

    @staticmethod
    def _sound_name_for_kind(kind):
        category = kind_category(kind)
        if category == CATEGORY_CHAT:
            return "chat"
        if category == CATEGORY_MARKET:
            return "market"
        return "system"

    def resolve_path(self, kind, cfg=None):
        """Какой файл играть (учитывает notify_sound_choice/file)."""
        cfg = cfg or {}
        choice = str(cfg.get("notify_sound_choice") or "auto").strip().lower()
        custom = str(cfg.get("notify_sound_file") or "").strip()
        if custom and os.path.isfile(custom):
            return custom
        if choice in SOUND_DEFS:
            name = choice
        else:
            name = self._sound_name_for_kind(kind)
        return self.sound_path(name)

    def play(self, kind, cfg=None):
        """Проиграть звук. ``False`` — выключено/не удалось/нет файла."""
        cfg = cfg or {}
        if not _cfg_bool(cfg.get("notify_sound", True), True):
            return False
        path = self.resolve_path(kind, cfg)
        if not path or not os.path.isfile(path):
            return False
        if callable(self.play_fn):
            try:
                return bool(self.play_fn(path))
            except Exception as exc:
                self.logger(f"Звук: ошибка воспроизведения ({exc})")
                return False
        return self._play_default(path)

    def _play_default(self, path):
        try:
            if os.name == "nt":
                import winsound
                winsound.PlaySound(
                    path,
                    winsound.SND_FILENAME | winsound.SND_ASYNC
                    | winsound.SND_NODEFAULT)
                return True
        except Exception as exc:
            self.logger(f"Звук: winsound не сработал ({exc})")
        try:
            from PyQt5.QtCore import QUrl
            from PyQt5.QtMultimedia import QSoundEffect
            effect = QSoundEffect()
            effect.setSource(QUrl.fromLocalFile(path))
            effect.setVolume(0.6)
            effect.play()
            self._effect = effect       # держим ссылку, иначе звук оборвётся
            return True
        except Exception as exc:
            self.logger(f"Звук: Qt-плеер недоступен ({exc})")
            return False


def open_url(url, opener=None):
    """Открыть ссылку в браузере (``opener`` — мок для тестов)."""
    url = str(url or "").strip()
    if not url:
        return False
    if callable(opener):
        try:
            opener(url)
            return True
        except Exception:
            return False
    try:
        import webbrowser
        return bool(webbrowser.open(url))
    except Exception:
        return False


def perform_action(record, unified=None, browser=None, events_panel=None,
                   open_updates=None, cfg=None, open_url_fn=None):
    """Выполнить действие клика по записи центра (используется run_gui).

    Возвращает ``{"action", "ok", "opened", "reason"}``. Никаких сетевых
    запросов; если окно/браузер недоступны — просто переключает вкладку.
    """
    record = dict(record or {})
    cfg = cfg or {}
    kind = str(record.get("kind") or "")
    action = str(record.get("action") or action_for_kind(kind))
    payload = (record.get("payload")
               if isinstance(record.get("payload"), dict) else {})
    item_slug = record.get("item_slug") or payload.get("slug") or ""
    item_name = (record.get("item_name") or payload.get("item")
                 or payload.get("name") or "")
    order_id = record.get("my_order_id") or payload.get("order_id") or ""
    my_price = record.get("my_price")
    new_price = record.get("new_price")
    note = ""
    if isinstance(my_price, (int, float)) and isinstance(new_price, (int, float)):
        note = (f"ваша цена {int(my_price)}p → "
                f"сейчас минимум {int(new_price)}p")

    if action == "market":
        if kind == "order_bought":
            if unified is not None:
                try:
                    unified.show_tab(1)       # «Мои ордера»
                except Exception:
                    pass
            ok = False
            if browser is not None and order_id:
                try:
                    ok = bool(browser.select_my_order(order_id))
                except Exception:
                    ok = False
            return {"action": action, "ok": ok, "opened": "orders",
                    "reason": "" if ok else "order_not_found"}
        # «перебили цену»: карточка предмета + правка МОЕГО ордера
        if unified is not None:
            try:
                unified.show_tab(0)           # «Маркет»
            except Exception:
                pass
        opened = False
        if browser is not None and (item_slug or item_name):
            try:
                browser.open_item(item_slug, item_name)
            except Exception:
                pass
        if browser is not None and (order_id or new_price):
            try:
                result = browser.edit_my_order(
                    order_id, new_price=new_price, item_slug=item_slug,
                    item_name=item_name, my_price=my_price, price_note=note)
            except Exception:
                result = {"ok": False, "error": "edit_failed"}
            result = result if isinstance(result, dict) else {}
            if result.get("ok"):
                opened = True
            elif result.get("error") == "order_not_found":
                if unified is not None:
                    try:
                        unified.lbl_status.setText(
                            "ордер не найден — возможно, уже продан")
                    except Exception:
                        pass
        elif not order_id:
            if unified is not None:
                try:
                    unified.lbl_status.setText(
                        "в уведомлении нет ордера — открыта карточка предмета")
                except Exception:
                    pass
        return {"action": action, "ok": opened, "opened": "market",
                "reason": "" if opened else "edit_not_opened"}

    if action == "events":
        if unified is not None:
            try:
                unified.show_tab(2)           # «События»
            except Exception:
                pass
        tier = record.get("tier") or payload.get("tier")
        focused = False
        if events_panel is not None and tier:
            try:
                focus = getattr(events_panel, "focus_tier", None)
                if callable(focus):
                    focus(tier)
                    focused = True
            except Exception:
                focused = False
        return {"action": action, "ok": True, "tier": tier,
                "filtered": focused}

    if action == "updates":
        called = False
        if callable(open_updates):
            try:
                open_updates()
                called = True
            except Exception:
                called = False
        return {"action": action, "ok": called, "opened": "updates"}

    if action == "chat":
        url = str(cfg.get("chat_url") or "https://warframe.market/")
        opened = bool(open_url_fn(url)) if callable(open_url_fn) else \
            open_url(url)
        if unified is not None:
            try:
                unified.lbl_status.setText("Открыть чат на сайте: " + url)
            except Exception:
                pass
        return {"action": action, "ok": opened, "url": url}

    if unified is not None:
        try:
            unified.open_notifications(record.get("id"))
        except Exception:
            pass
    return {"action": "center", "ok": True}


def dispatch_notification(record, cfg=None, store=None, center=None,
                          sound_player=None, toast_stack=None, tray=None,
                          login_check=None, badge_cb=None):
    """Показать уведомление: история → звук → свой тост/системный тост.

    Используется run_gui (сигнал из фонового потока). ``tray`` — объект с
    ``available()``/``show_message(...)``; ``toast_stack`` — из
    :func:`build_toast_stack`; ``center`` — виджет центра.
    Возвращает ``{"ok", "record", "display", "sound"}``.
    """
    record = dict(record or {})
    cfg = cfg or {}
    if store is None:
        return {"ok": False, "record": record, "display": "none",
                "sound": False, "error": "no_store"}
    action = record.get("action") or action_for_kind(record.get("kind"))
    full = dict(record)
    full["action"] = action
    stored = store.add_record(full)
    if center is not None:
        try:
            center.add_record(stored)
        except Exception:
            pass
    if callable(badge_cb):
        try:
            badge_cb(*store.badge(cfg))
        except Exception:
            pass
    login = False
    if callable(login_check):
        try:
            login = bool(login_check())
        except Exception:
            login = False
    sound_played = False
    if bool(record.get("sound", True)) and sound_player is not None and not login:
        try:
            sound_played = bool(sound_player.play(record.get("kind"), cfg))
        except Exception:
            sound_played = False
    if login:
        return {"ok": True, "record": stored, "display": "login",
                "sound": sound_played}
    mode = str(cfg.get("notify_mode") or "overlay").lower()
    display = "none"
    if mode == "system":
        available = True
        if tray is not None and hasattr(tray, "available"):
            try:
                available = bool(tray.available())
            except Exception:
                available = False
        if tray is not None and available:
            try:
                tray.show_message(stored.get("title", ""),
                                  stored.get("text", ""),
                                  kind=stored.get("kind", ""), sound=False)
                display = "system"
            except Exception:
                display = "none"
    elif toast_stack is not None:
        try:
            if toast_stack.show_toast(stored) is not None:
                display = "toast"
        except Exception:
            display = "none"
    if display == "none" and tray is not None:
        # свой тост недоступен — запасной системный тост трея
        try:
            if not hasattr(tray, "available") or tray.available():
                tray.show_message(stored.get("title", ""),
                                  stored.get("text", ""),
                                  kind=stored.get("kind", ""), sound=False)
                display = "system_fallback"
        except Exception:
            pass
    return {"ok": True, "record": stored, "display": display,
            "sound": sound_played}


# ─────────────────────────────────────────────────────────────────────────
#  UI: вкладка «Уведомления» в F8
# ─────────────────────────────────────────────────────────────────────────
def build_notification_center():
    """Фабрика виджета-вкладки «Уведомления» (палитра ui_theme)."""
    from PyQt5.QtCore import Qt, pyqtSignal
    from PyQt5.QtGui import QFont
    from PyQt5.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                                 QScrollArea, QSizePolicy, QVBoxLayout,
                                 QWidget)

    def color(name, fallback="#e6edf6"):
        return palette_color(name, fallback)

    def font(size, bold=False):
        result = QFont(theme.FONT if theme is not None else "Segoe UI")
        result.setPixelSize(int(size))
        result.setBold(bool(bold))
        return result

    def button_qss(**kwargs):
        return theme.button_qss(**kwargs) if theme is not None else ""

    class _NotificationRow(QFrame):
        activated = pyqtSignal(dict)

        def __init__(self, cfg, record, parent=None):
            super().__init__(parent)
            self.record = dict(record)
            self._cfg = dict(cfg or {})
            style = kind_style(self._cfg, record.get("kind"))
            self.color = style["color"]
            self.badge_text = style["badge"]
            self.unread = not bool(record.get("read"))
            self.setObjectName("notifRow")
            self.setStyleSheet(
                f"QFrame#notifRow {{ background:"
                f"{color('card') if self.unread else 'transparent'};"
                f" border:1px solid {color('border')};"
                " border-radius:6px; }")
            self.setCursor(Qt.PointingHandCursor)
            layout = QHBoxLayout(self)
            layout.setContentsMargins(0, 0, 10, 0)
            layout.setSpacing(8)
            stripe = QFrame()
            stripe.setFixedWidth(4)
            stripe.setStyleSheet(
                f"background:{self.color}; border:none;"
                " border-top-left-radius:6px; border-bottom-left-radius:6px;")
            layout.addWidget(stripe)
            body = QVBoxLayout()
            body.setContentsMargins(8, 6, 0, 6)
            body.setSpacing(2)
            head = QHBoxLayout()
            head.setSpacing(6)
            badge = QLabel(self.badge_text)
            badge.setFont(font(9, True))
            badge.setStyleSheet(
                f"color:#0d131c; background:{self.color};"
                " border-radius:5px; padding:1px 6px;")
            self.lbl_badge = badge
            title = QLabel(str(record.get("title") or "Уведомление"))
            title.setFont(font(11, True))
            title.setStyleSheet(f"color:{color('text')}; background:transparent;")
            self.lbl_title = title
            head.addWidget(badge)
            head.addWidget(title, 1)
            self.lbl_time = QLabel(format_clock(record.get("ts")))
            self.lbl_time.setFont(font(9))
            self.lbl_time.setStyleSheet(
                f"color:{color('muted')}; background:transparent;")
            head.addWidget(self.lbl_time)
            body.addLayout(head)
            text = QLabel(str(record.get("text") or ""))
            text.setFont(font(10))
            text.setStyleSheet(f"color:{color('text')}; background:transparent;")
            text.setWordWrap(True)
            if theme is not None:
                try:
                    import re as _re
                    platinum = theme.currency_platinum()
                    raw = str(record.get("text") or "")
                    html = _re.sub(
                        r"(\d+)\s*p\b",
                        lambda m: (
                            f"<span style='color:{platinum}; "
                            f"font-weight:700;'>{m.group(1)}p</span>"),
                        raw)
                    if html != raw:
                        text.setText(html)
                        text.setToolTip(theme.currency_tooltip("platinum"))
                except Exception:
                    pass
            self.lbl_text = text
            body.addWidget(text)
            layout.addLayout(body, 1)
            if self.unread:
                dot = QLabel("●")
                dot.setFont(font(8))
                dot.setStyleSheet(f"color:{self.color}; background:transparent;")
                layout.addWidget(dot)

        def mousePressEvent(self, event):
            if event.button() == Qt.LeftButton:
                self.activated.emit(dict(self.record))
            super().mousePressEvent(event)

    class NotificationCenter(QWidget):
        """Список истории с группировкой по дате и действиями."""

        record_activated = pyqtSignal(dict)
        unread_changed = pyqtSignal(int, str)

        def __init__(self, cfg, store, parent=None):
            super().__init__(parent)
            self.cfg = dict(cfg or {})
            self.store = store
            self.rows = {}
            self.setObjectName("notifCenter")
            outer = QVBoxLayout(self)
            outer.setContentsMargins(10, 8, 10, 8)
            outer.setSpacing(6)
            head = QHBoxLayout()
            title = QLabel("Центр уведомлений")
            title.setFont(font(13, True))
            title.setStyleSheet(f"color:{color('text')};")
            self.lbl_count = QLabel("")
            self.lbl_count.setFont(font(10))
            self.lbl_count.setStyleSheet(
                f"color:{color('muted')}; background:transparent;")
            head.addWidget(title)
            head.addWidget(self.lbl_count)
            head.addStretch(1)
            self.btn_mark_all = QPushButton("Отметить все прочитанными")
            self.btn_mark_all.setFont(font(10))
            self.btn_mark_all.setStyleSheet(button_qss(small=True))
            self.btn_mark_all.setCursor(Qt.PointingHandCursor)
            self.btn_mark_all.clicked.connect(self.mark_all_read)
            self.btn_clear = QPushButton("Очистить историю")
            self.btn_clear.setFont(font(10))
            self.btn_clear.setStyleSheet(button_qss(small=True))
            self.btn_clear.setCursor(Qt.PointingHandCursor)
            self.btn_clear.clicked.connect(self.clear_history)
            head.addWidget(self.btn_mark_all)
            head.addWidget(self.btn_clear)
            outer.addLayout(head)
            self.scroll = QScrollArea()
            self.scroll.setWidgetResizable(True)
            self.scroll.setStyleSheet("QScrollArea { border:none; }")
            self.body_host = QWidget()
            self.body = QVBoxLayout(self.body_host)
            self.body.setContentsMargins(0, 0, 0, 0)
            self.body.setSpacing(6)
            self.scroll.setWidget(self.body_host)
            outer.addWidget(self.scroll, 1)
            self.lbl_empty = QLabel(
                "Пока тихо. Здесь появятся уведомления о продажах, ценах, "
                "событиях и чате.")
            self.lbl_empty.setFont(font(10))
            self.lbl_empty.setStyleSheet(
                f"color:{color('muted')}; background:transparent;")
            self.lbl_empty.setWordWrap(True)
            self.refresh()

        # ── данные ───────────────────────────────────────────────────
        def set_cfg(self, cfg):
            self.cfg = dict(cfg or {})
            self.refresh()

        def refresh(self):
            while self.body.count():
                item = self.body.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.setParent(None)
                    widget.deleteLater()
            self.rows = {}
            groups = self.store.grouped()
            for label, records in groups:
                header = QLabel(label)
                header.setFont(font(10, True))
                header.setStyleSheet(
                    f"color:{color('muted')}; background:transparent;"
                    " padding-top:4px;")
                self.body.addWidget(header)
                for record in records:
                    row = _NotificationRow(self.cfg, record)
                    row.activated.connect(self._row_activated)
                    self.rows[str(record.get("id"))] = row
                    self.body.addWidget(row)
            if not groups:
                empty = QLabel(
                    "Пока тихо. Здесь появятся уведомления о продажах, "
                    "ценах, событиях и чате.")
                empty.setFont(font(10))
                empty.setStyleSheet(
                    f"color:{color('muted')}; background:transparent;")
                empty.setWordWrap(True)
                self.body.addWidget(empty)
            self.body.addStretch(1)
            unread = self.store.unread_count()
            self.lbl_count.setText(
                f"непрочитанных: {unread}" if unread else "всё прочитано")
            self.unread_changed.emit(*self.store.badge(self.cfg))
            return groups

        def add_record(self, record):
            record = dict(record or {})
            existing = self.store.find(record.get("id"))
            if existing is None:
                if record.get("id") and all(k in record for k in
                                            ("kind", "title")):
                    self.store.add_record(record)
                else:
                    self.store.add(
                        record.get("kind", "system"),
                        record.get("title", ""),
                        record.get("text", ""),
                        action=record.get("action", ""),
                        payload=record.get("payload"),
                        ts=record.get("ts"),
                        read=bool(record.get("read")))
            self.refresh()
            return self.store.find(record.get("id"))

        def activate(self, record_id):
            record = self.store.find(record_id)
            if record is None:
                return None
            self.store.mark_read(record_id)
            self.refresh()
            self.record_activated.emit(record)
            return record

        def select_record(self, record_id):
            self.refresh()
            row = self.rows.get(str(record_id))
            if row is not None:
                row.setStyleSheet(
                    row.styleSheet().replace(
                        f"border:1px solid {color('border')};",
                        f"border:1px solid {color('accent')};"))
                self.scroll.ensureWidgetVisible(row, 0, 8)
            return row

        def _row_activated(self, record):
            self.activate(record.get("id"))

        def mark_all_read(self):
            changed = self.store.mark_all_read()
            self.refresh()
            return changed

        def clear_history(self):
            had = self.store.clear()
            self.refresh()
            return had

        def unread_count(self):
            return self.store.unread_count()

        def badge(self):
            return self.store.badge(self.cfg)

    return NotificationCenter


# ─────────────────────────────────────────────────────────────────────────
#  UI: свои тосты (правый верхний угол, стек ≤3, без фокуса)
# ─────────────────────────────────────────────────────────────────────────
def build_toast_stack():
    """Фабрика стека тостов. ``login_check()`` — не показывать при входе."""
    from PyQt5.QtCore import QObject, QPropertyAnimation, QTimer, Qt, pyqtSignal
    from PyQt5.QtGui import QColor, QFont
    from PyQt5.QtWidgets import (QApplication, QFrame, QGraphicsDropShadowEffect,
                                 QHBoxLayout, QLabel, QVBoxLayout)

    def color(name, fallback="#e6edf6"):
        return palette_color(name, fallback)

    def font(size, bold=False):
        result = QFont(theme.FONT if theme is not None else "Segoe UI")
        result.setPixelSize(int(size))
        result.setBold(bool(bold))
        return result

    class ToastWidget(QFrame):
        clicked = pyqtSignal(dict)
        finished = pyqtSignal(object)

        def __init__(self, cfg, record, parent=None):
            super().__init__(parent)
            self.cfg = dict(cfg or {})
            self.record = dict(record)
            style = kind_style(self.cfg, record.get("kind"))
            self.color = style["color"]
            self.setObjectName("toast")
            self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint |
                                Qt.WindowStaysOnTopHint)
            self.setAttribute(Qt.WA_ShowWithoutActivating, True)
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setAttribute(Qt.WA_DeleteOnClose, False)
            self.setFixedWidth(320)
            self.setStyleSheet(
                f"QFrame#toast {{ background:{color('panel')};"
                f" border:1px solid {self.color}; border-radius:8px; }}")
            layout = QHBoxLayout(self)
            layout.setContentsMargins(0, 0, 10, 0)
            layout.setSpacing(8)
            stripe = QFrame()
            stripe.setFixedWidth(4)
            stripe.setStyleSheet(
                f"background:{self.color}; border:none;"
                " border-top-left-radius:8px; border-bottom-left-radius:8px;")
            layout.addWidget(stripe)
            body = QVBoxLayout()
            body.setContentsMargins(8, 7, 0, 7)
            body.setSpacing(2)
            head = QHBoxLayout()
            head.setSpacing(6)
            badge = QLabel(style["badge"])
            badge.setFont(font(9, True))
            badge.setStyleSheet(
                f"color:#0d131c; background:{self.color};"
                " border-radius:5px; padding:1px 6px;")
            title = QLabel(str(record.get("title") or "Уведомление"))
            title.setFont(font(11, True))
            title.setStyleSheet(f"color:{color('text')}; background:transparent;")
            head.addWidget(badge)
            head.addWidget(title, 1)
            time_lbl = QLabel(format_clock(record.get("ts")))
            time_lbl.setFont(font(9))
            time_lbl.setStyleSheet(
                f"color:{color('muted')}; background:transparent;")
            head.addWidget(time_lbl)
            body.addLayout(head)
            text = QLabel(str(record.get("text") or ""))
            text.setFont(font(10))
            text.setStyleSheet(f"color:{color('text')}; background:transparent;")
            text.setWordWrap(True)
            body.addWidget(text)
            layout.addLayout(body, 1)
            try:
                seconds = float(self.cfg.get("notify_toast_sec", 6) or 6)
            except Exception:
                seconds = 6.0
            self._timeout = max(500, int(seconds * 1000))
            self.timer = QTimer(self)
            self.timer.setSingleShot(True)
            self.timer.setInterval(self._timeout)
            self.timer.timeout.connect(self.fade_out)
            self._animation = None
            self.setCursor(Qt.PointingHandCursor)
            try:
                shadow = QGraphicsDropShadowEffect(self)
                shadow.setBlurRadius(18)
                shadow.setOffset(0, 4)
                shadow.setColor(QColor(0, 0, 0, 160))
                self.setGraphicsEffect(shadow)
            except Exception:
                pass
            self.adjustSize()

        def show_without_focus(self):
            self.show()
            try:
                self.raise_()
            except Exception:
                pass
            self.timer.start()

        def fade_out(self):
            if self._animation is not None:
                return
            self._animation = QPropertyAnimation(self, b"windowOpacity", self)
            self._animation.setDuration(300)
            self._animation.setStartValue(1.0)
            self._animation.setEndValue(0.0)
            self._animation.finished.connect(self._finish)
            self._animation.start()

        def _finish(self):
            self.hide()
            self.finished.emit(self)

        def close_now(self):
            self.timer.stop()
            self.hide()
            self.finished.emit(self)

        def mousePressEvent(self, event):
            if event.button() == Qt.LeftButton:
                self.clicked.emit(dict(self.record))
                self.close_now()
            super().mousePressEvent(event)

    class ToastStack(QObject):
        activated = pyqtSignal(dict)

        def __init__(self, cfg, on_click=None, login_check=None,
                     max_toasts=3, parent=None):
            super().__init__(parent)
            self.cfg = dict(cfg or {})
            self.on_click = on_click
            self.login_check = login_check
            self.max_toasts = max(1, int(max_toasts or 3))
            self.toasts = []
            self._default_login = None

        def _login_now(self):
            if callable(self.login_check):
                try:
                    return bool(self.login_check())
                except Exception:
                    return False
            if self._default_login is None:
                try:
                    import login_mode
                    self._default_login = login_mode.login_in_progress
                except Exception:
                    self._default_login = lambda: False
            try:
                return bool(self._default_login())
            except Exception:
                return False

        def _screen_geometry(self):
            try:
                screen = QApplication.primaryScreen()
                return screen.availableGeometry()
            except Exception:
                return None

        def _reposition(self):
            geometry = self._screen_geometry()
            if geometry is None:
                return
            y = geometry.top() + 16
            x = geometry.right() - 16 - 320
            for toast in list(self.toasts):
                toast.move(max(0, x), int(y))
                y += toast.height() + 8

        def show_toast(self, record):
            if self._login_now():
                return None
            if len(self.toasts) >= self.max_toasts:
                self._remove(self.toasts[0])
            toast = ToastWidget(self.cfg, record)
            toast.clicked.connect(self._on_clicked)
            toast.finished.connect(self._remove)
            self.toasts.append(toast)
            toast.show_without_focus()
            self._reposition()
            return toast

        def _on_clicked(self, record):
            try:
                if callable(self.on_click):
                    self.on_click(dict(record))
            except Exception:
                pass
            self.activated.emit(dict(record))

        def _remove(self, toast):
            if toast in self.toasts:
                self.toasts.remove(toast)
            try:
                toast.hide()
                toast.deleteLater()
            except Exception:
                pass
            self._reposition()
            return toast

        def count(self):
            return len(self.toasts)

        def hide_all(self):
            for toast in list(self.toasts):
                self._remove(toast)

        def set_cfg(self, cfg):
            self.cfg = dict(cfg or {})

    return ToastStack


def notification_center_defaults():
    """Ключи конфига, которые добавляет центр (для settings/тестов)."""
    return {
        "notify_mode": "overlay",
        "notify_toast_sec": 6,
        "notify_chat_color": DEFAULT_CHAT_COLOR,
        "notify_market_color": DEFAULT_MARKET_COLOR,
        "notify_price_color": DEFAULT_PRICE_COLOR,
        "notify_sound_choice": "auto",
        "notify_sound_file": "",
    }

