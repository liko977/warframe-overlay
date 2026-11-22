# -*- coding: utf-8 -*-
"""Мировые события Warframe для русского оверлея (api.warframestat.us).

Официальный источник — ``api.warframe.com/cdn/worldState.php`` (fallback —
warframestat); циклы Цетуса/Валлиса/Камбиона официальный API не отдаёт, поэтому
секция «Циклы» для него скрыта (:data:`OFFICIAL_CYCLES_NOTE`).
Что делает
----------
Загружает ``GET https://api.warframestat.us/pc?language=ru`` (см. EVENTS_MARKET_SPEC.md,
часть I), кэширует снимок на диск и превращает его в плоский список строк
для панели событий:

    РАЗЛОМЫ
      Лит · Захват · Гефит (Бездна) · Корпус          до 1ч 23м
    СОРТИРОВКА
      Councilor Vay Hek · Гринир                      до 14ч 10м
      • Оборона · Psamathe (Neptune)                  ...
    ЦИКЛЫ
      Цетус: ночь                                     до 50м

Правила (важные для оверлея)
----------------------------
1. Модуль **только читает** чужие данные: сеть — GET без авторизации,
   на диск пишется единственный файл кэша ``events_cache.json`` в папке данных
   (``DATA_DIR`` оверлея). Никаких других файлов модуль не трогает.
2. Отсутствие сети — не ошибка: показываем последний снимок с диска и
   понятную пометку («нет сети», «данные устарели»). Исключения наружу не
   выпускаются (кроме совсем неожиданных — их ловит вызывающий код).
3. Обратные отсчёты считаются локально по ``expiry``/``activation``: запрос
   в сеть раз в ``events_poll_ms`` (рекомендуется 60000 — источник обновляется
   раз в 5 минут), а таймеры перерисовываются хоть каждую секунду.
4. Русские названия берём из ответа API (``missionType``, ``enemy``, ``desc``);
   имена узлов API не переводит — их подставляет необязательный словарь
   ``events_translation`` (``{"Hepit (Void)": "Гефит (Бездна)"}``).

Модуль можно использовать и без GUI (тесты, консоль):

    import events_api
    client = events_api.WarframeStatusClient(cfg)
    client.fetch()
    print(events_api.format_text(client.sections(), client.status_text()))
"""

import datetime
import json
import os
import re
import sys
import threading
import time

OFFICIAL_URL = "https://api.warframe.com/cdn/worldState.php"
EVENTS_URL = "https://api.warframestat.us/pc"      # запасной источник
CACHE_NAME = "events_cache.json"
NODE_DICT_NAME = "node_names_ru.json"
NODE_DICT_CACHE = "events_nodes.json"
#: Откуда один раз скачать словарь узлов WFCD (кэшируется в папке данных).
NODE_DICT_URLS = (
    # рабочий путь (273 узла), затем фолбэки WFCD/worldstate-data
    "https://raw.githubusercontent.com/WFCD/warframe-items/master/data/json/"
    "Node.json",
    "https://raw.githubusercontent.com/WFCD/warframe-worldstate-data/master/"
    "data/ExportRegions.json",
    "https://raw.githubusercontent.com/WFCD/warframe-worldstate-data/master/"
    "data/json/ExportRegions.json",
    "https://raw.githubusercontent.com/WFCD/warframe-worldstate-data/main/"
    "data/ExportRegions.json",
    "https://raw.githubusercontent.com/WFCD/warframe-worldstate-data/main/"
    "data/json/ExportRegions.json",
    "https://raw.githubusercontent.com/WFCD/warframe-items/master/data/json/"
    "ExportRegions.json",
)
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


class EventsHTTPError(RuntimeError):
    """Сервер вернул не-200: хранит код для понятного сообщения."""

    def __init__(self, status):
        self.status = int(status)
        super().__init__(f"HTTP {self.status}")

#: Внутренние ключи официального worldState.php → секции warframestat.
OFFICIAL_KEYS = ("ActiveMissions", "Alerts", "Invasions", "Sorties",
                 "VoidTraders", "Events", "DailyDeals", "NodeOverrides",
                 "SyndicateMissions", "Goals")

#: Эры реликвий в официальном API: ``VoidT1`` … ``VoidT4``.
VOID_TIER_RU = {
    "VoidT1": "Лит", "VoidT2": "Мезо", "VoidT3": "Нео", "VoidT4": "Акси",
    "VoidT5": "Реквием", "VoidT6": "Реквием",
}
VOID_TIER_RAW = {
    "VoidT1": "Lith", "VoidT2": "Meso", "VoidT3": "Neo", "VoidT4": "Axi",
    "VoidT5": "Requiem", "VoidT6": "Requiem",
}

#: Внутренние ключи типов миссий → английские имена (их переводит
#: :data:`MISSION_TYPE_RU`); неизвестный ключ показываем как есть.
MISSION_TYPE_KEYS = {
    "MT_EXCAVATE": "Excavation",
    "MT_DEFENSE": "Defense",
    "MT_SURVIVAL": "Survival",
    "MT_CAPTURE": "Capture",
    "MT_EXTERMINATION": "Extermination",
    "MT_SABOTAGE": "Sabotage",
    "MT_RESCUE": "Rescue",
    "MT_SPY": "Spy",
    "MT_INTEL": "Spy",                 # официальный API: MT_INTEL
    "MT_INTERCEPTION": "Interception",
    "MT_MOBILE_DEFENSE": "Mobile Defense",
    "MT_HIJACK": "Hijack",
    "MT_ASSASSINATION": "Assassination",
    "MT_ASSASSINATE": "Assassination",
    "MT_ARTIFACT": "Artifact",
    "Artifact": "Artifact",            # встречается без префикса MT_
    "MT_RETRIEVAL": "Retrieval",
    "MT_TERRITORY": "Interception",
    "MT_ARENA": "Arena",
    "MT_ALCHEMY": "Alchemy",
    "MT_ORPHIX": "Orphix",
    "MT_DEFECTION": "Defection",
    "MT_ASSAULT": "Assault",
    "MT_RACE": "Race",
    "MT_CORRUPTION": "Corruption",
    "MT_VOID_CASCADE": "Void Cascade",
    "MT_DARK_ESCAPE": "Dark Escape",
    "MT_ESCORT": "Escort",
    "MT_ENDLESS": "Endless",
}

#: Боссы сортировки: официальный API отдаёт SORTIE_BOSS_*.
SORTIE_BOSS_RU = {
    "SORTIE_BOSS_RAPTOR": "Раптор",
    "SORTIE_BOSS_AMBULAS": "Амбулас",
    "SORTIE_BOSS_VAY_HEK": "Вей Хек",
    "SORTIE_BOSS_KELA_DE_THAYM": "Кела Де Тейм",
    "SORTIE_BOSS_SARGAS_RUK": "Саргас Рук",
    "SORTIE_BOSS_ALAD_V": "Алад V",
    "SORTIE_BOSS_TYL_REGOR": "Тайл Регор",
    "SORTIE_BOSS_LECH_KRIL": "Лех Крил",
    "SORTIE_BOSS_LEPHANTIS": "Лефантис",
    "SORTIE_BOSS_HYENA": "Гиена",
    "SORTIE_BOSS_CORRUPTED_VOR": "Порченый Вор",
    "SORTIE_BOSS_VOR": "Вор",
    "SORTIE_BOSS_INFESTED_ALAD": "Заражённый Алад V",
    "SORTIE_BOSS_JACKAL": "Джекал",
    "SORTIE_BOSS_AMBULAS_PROXY": "Амбулас",
}

#: Фракции официального API (FC_*).
OFFICIAL_FACTIONS = {
    "FC_GRINEER": "Grineer",
    "FC_CORPUS": "Corpus",
    "FC_INFESTATION": "Infested",
    "FC_INFESTED": "Infested",
    "FC_OROKIN": "Orokin",
    "FC_CORRUPTED": "Corrupted",
    "FC_MURMUR": "The Murmur",
    "FC_SENTIENT": "Sentient",
    "FC_NARMER": "Narmer",
    "FC_TENNO": "Tenno",
    "FC_STALKER": "Stalker",
}

#: Эры реликвий: в API они всегда английские (Lith/Meso/Neo/Axi/Requiem).
TIER_RU = {
    "Lith": "Лит",
    "Meso": "Мезо",
    "Neo": "Нео",
    "Axi": "Акси",
    "Requiem": "Реквием",
}

#: Типы миссий: живой API с ``language=ru`` отдаёт их по-английски,
#: поэтому переводим сами (незнакомые типы показываем как в API).
MISSION_TYPE_RU = {
    "Defense": "Оборона",
    "Survival": "Выживание",
    "Capture": "Захват",
    "Extermination": "Истребление",
    "Sabotage": "Диверсия",
    "Rescue": "Спасение",
    "Spy": "Шпионаж",
    "Interception": "Перехват",
    "Mobile Defense": "Мобильная оборона",
    "Excavation": "Раскопки",
    "Hijack": "Угон",
    "Assault": "Штурм",
    "Defection": "Дезертирство",
    "Arena": "Арена",
    "Alchemy": "Алхимия",
    "Orphix": "Орфикс",
    "Assassination": "Убийство",
    "Artifact": "Захват артефакта",
    "Retrieval": "Возврат",
    "Corruption": "Порча",
    "Void Cascade": "Каскад Бездны",
    "Race": "Гонка",
    "Dark Escape": "Тёмный побег",
    "Escort": "Сопровождение",
    "Endless": "Бесконечность",
}

#: Фракции: тот же случай — API отдаёт английские ключи.
FACTION_RU = {
    "Grineer": "Гринир",
    "Corpus": "Корпус",
    "Infested": "Заражённые",
    "Orokin": "Орокин",
    "Corrupted": "Порченые",
    "The Murmur": "Шёпот",
    "Sentient": "Сентиенты",
    "Crossfire": "Перекрёстный огонь",
    "Tenno": "Тенно",
    "Stalker": "Сталкер",
    "Narmer": "Нармер",
}

#: Состояния циклов открытых миров.
CYCLE_STATE_RU = {
    "day": "день",
    "night": "ночь",
    "warm": "тёпло",
    "cold": "холодно",
    "vome": "Воме",
    "fass": "Фасс",
}

#: Заголовки секций в порядке показа (пустые секции не показываем).
SECTION_FISSURES = "Разломы"
SECTION_ALERTS = "Тревоги"
SECTION_EVENTS = "События"
SECTION_INVASIONS = "Вторжения"
SECTION_SORTIE = "Сортировка"
SECTION_BARO = "Баро Ки'Тиир"
SECTION_STEEL_PATH = "Стальной путь"
SECTION_NIGHTWAVE = "Ночная волна"
SECTION_CYCLES = "Циклы"

SECTION_ORDER = [
    SECTION_FISSURES,
    SECTION_ALERTS,
    SECTION_EVENTS,
    SECTION_INVASIONS,
    SECTION_SORTIE,
    SECTION_BARO,
    SECTION_STEEL_PATH,
    SECTION_NIGHTWAVE,
    SECTION_CYCLES,
]


# ─────────────────────────────────────────────────────────────────────────
#  Мелкие утилиты: время, формат, безопасный доступ
# ─────────────────────────────────────────────────────────────────────────
def log(msg=""):
    """Печать в консоль оверлея; никогда не падает."""
    try:
        print(f"События: {msg}", flush=True)
    except Exception:
        pass


def _default_data_dir():
    """Папка данных, если вызывающий не передал свою (как в warframe.py)."""
    env = os.environ.get("WF_DATA_DIR")
    if env:
        return env
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "VoidFrameRU")


def parse_iso(value):
    """ISO-8601 строка из API → секунды epoch (UTC). ``None``, если не разобрать."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z") or text.endswith("z"):
            text = text[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.datetime.strptime(str(value).strip(), fmt)
            return dt.replace(tzinfo=datetime.timezone.utc).timestamp()
        except Exception:
            continue
    return None


def format_duration(seconds):
    """Секунды → короткая русская строка: ``1ч 23м``, ``50м``, ``12с``."""
    if seconds is None:
        return ""
    try:
        sec = int(seconds)
    except Exception:
        return ""
    if sec <= 0:
        return "0м"
    days, rem = divmod(sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}д {hours}ч"
    if hours:
        return f"{hours}ч {minutes:02d}м"
    if minutes:
        return f"{minutes}м"
    return f"{secs}с"


def format_timer(seconds):
    """Таймер для панели в стиле AlecaFrame: ``14ч 22м``, ``59м 12с``, ``<1м``.

    Дни показываются как ``3д 7ч`` (для Баро/ночной волны); всё, что меньше
    минуты, — ``<1м``.
    """
    if seconds is None:
        return ""
    try:
        sec = int(seconds)
    except Exception:
        return ""
    if sec < 60:
        return "<1м"
    if sec < 3600:
        minutes, secs = divmod(sec, 60)
        return f"{minutes}м {secs:02d}с"
    if sec < 86400:
        hours, rem = divmod(sec, 3600)
        return f"{hours}ч {rem // 60:02d}м"
    days, rem = divmod(sec, 86400)
    return f"{days}д {rem // 3600}ч"


def format_age(seconds):
    """Возраст данных для шапки панели: ``12 с``, ``5 мин``, ``2 ч 07 мин``."""
    if seconds is None:
        return ""
    try:
        sec = max(0, int(seconds))
    except Exception:
        return ""
    if sec < 60:
        return f"{sec} с"
    if sec < 3600:
        return f"{sec // 60} мин"
    if sec < 86400:
        hours, rem = divmod(sec, 3600)
        minutes = rem // 60
        return f"{hours} ч {minutes:02d} мин" if minutes else f"{hours} ч"
    days, rem = divmod(sec, 86400)
    hours = rem // 3600
    return f"{days} д {hours} ч" if hours else f"{days} д"


def ends_in(row, now=None):
    """Сколько секунд осталось до конца события (``None``, если срока нет)."""
    if not isinstance(row, dict):
        return None
    ends_at = row.get("ends_at")
    if not isinstance(ends_at, (int, float)):
        return None
    now = time.time() if now is None else now
    return max(0.0, float(ends_at) - float(now))


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


def _row(kind, section, title, detail="", ends_at=None, started_at=None,
         ends_prefix=None, headline=None, subtext=None, badges=None,
         tier=None, glyph=None, progress=None, faction=None, rewards=None,
         state=None, pinned=False):
    """Одна строка панели событий.

    ``title``/``detail`` — плоский текст (совместимость и консольный вывод).
    Дополнительные поля нужны панели в стиле AlecaFrame:

    * ``headline``/``subtext`` — основной текст и серые детали;
    * ``badges`` — список ``{"kind": "tier"|"mission"|"steel"|"storm"|"faction",
      "text": "Лит"}``;
    * ``tier`` — сырая эра (Lith/Meso/Neo/Axi/Requiem) для цвета бейджа;
    * ``glyph`` — глиф цикла (☀/☾/❄/◐/◑);
    * ``progress`` — процент вторжения (0..100);
    * ``faction`` — ключ фракции для цвета полосы прогресса;
    * ``rewards`` — награды списком («3 × Fieldron»);
    * ``state`` — состояние цикла («ночь», «день», …);
    * ``pinned`` — заголовок группы (сортируется первым, см. :func:`sections`).
    """
    return {
        "kind": kind,
        "section": section,
        "title": str(title or ""),
        "detail": str(detail or ""),
        "ends_at": ends_at,
        "started_at": started_at,
        "ends_prefix": str(ends_prefix or "до"),
        "headline": str(title or "") if headline is None else str(headline or ""),
        "subtext": str(detail or "") if subtext is None else str(subtext or ""),
        "badges": [dict(b) for b in (badges or []) if b],
        "tier": str(tier or ""),
        "glyph": str(glyph or ""),
        "progress": (None if progress is None
                     else max(0.0, min(100.0, float(progress)))),
        "faction": str(faction or ""),
        "rewards": [str(x) for x in (rewards or []) if x],
        "state": str(state or ""),
        "pinned": bool(pinned),
    }


def _reward_lines(reward):
    """Награды списком: ``["3 × Fieldron", "10 000 кр"]`` (для карточки)."""
    reward = _dict(reward)
    lines = []
    for item in reward.get("items") or []:
        if isinstance(item, dict):
            name = item.get("item") or item.get("name") or item.get("type")
            count = item.get("count") or item.get("quantity")
        else:
            name, count = item, None
        if not name:
            continue
        try:
            count = int(count) if count is not None else 1
        except Exception:
            count = 1
        lines.append(f"{max(1, count)} × {name}")
    for item in reward.get("countedItems") or []:
        item = _dict(item)
        name = item.get("item") or item.get("type") or item.get("name")
        if not name:
            continue
        try:
            count = max(1, int(item.get("count")))
        except Exception:
            count = 1
        lines.append(f"{count} × {name}")
    credits = _number(reward.get("credits"))
    if credits:
        lines.append(f"{int(credits):,} кр".replace(",", " "))
    return lines


def _reward_text(reward):
    """Награда из схемы API (items/countedItems/credits) → короткая строка."""
    reward = _dict(reward)
    parts = []
    for item in reward.get("items") or []:
        if isinstance(item, dict):
            name = item.get("item") or item.get("name") or item.get("type")
            count = item.get("count") or item.get("quantity")
        else:
            name, count = item, None
        if not name:
            continue
        try:
            count = int(count) if count is not None else None
        except Exception:
            count = None
        parts.append(f"{name} ×{count}" if count and count != 1 else str(name))
    for item in reward.get("countedItems") or []:
        item = _dict(item)
        name = item.get("item") or item.get("type") or item.get("name")
        if not name:
            continue
        try:
            count = int(item.get("count"))
        except Exception:
            count = None
        parts.append(f"{name} ×{count}" if count and count != 1 else str(name))
    credits = _number(reward.get("credits"))
    if credits:
        parts.append(f"{int(credits):,} кр".replace(",", " "))
    return ", ".join(parts)


def _node_name(node_dict, *candidates):
    """Русское имя узла из словаря, иначе значение из API как есть."""
    for cand in candidates:
        if not cand:
            continue
        text = str(cand)
        name = (node_dict or {}).get(text)
        return name or text
    return ""


def parse_node_dictionary(raw):
    """Словарь узлов WFCD/парсера → ``{"SolNode101": "Паимон (Европа)"}``.

    Понимает mapping ``{key: name}``, ``{key: {name/value: ...}}`` и списки
    записей с ``uniqueName``/``name``. Неизвестный формат → пустой словарь,
    панель продолжает показывать исходные ключи.
    """
    out = {}

    def short_key(value):
        text = str(value or "").strip()
        if not text:
            return ""
        if "/" in text:
            text = text.rstrip("/").rsplit("/", 1)[-1]
        return text

    def add(key, value):
        key = short_key(key)
        value = str(value or "").strip()
        if not key or not value:
            return
        if key.startswith("SolNode") or key.startswith("SettlementNode") \
                or key.startswith("CrewBattleNode") or key.startswith("/"):
            out[key] = value
        elif key and value and key not in ("name", "value", "uniqueName"):
            # плоский mapping вида {"Hepit (Void)": "Гефит (Бездна)"}
            out[key] = value

    def walk(obj):
        if isinstance(obj, dict):
            name = (obj.get("name") or obj.get("value")
                    or obj.get("localizedName") or obj.get("localized")
                    or obj.get("ru"))
            unique = (obj.get("uniqueName") or obj.get("key")
                      or obj.get("nodeKey") or obj.get("id"))
            if name and unique:
                add(unique, name)
            for key, value in obj.items():
                if isinstance(value, str):
                    if key not in ("name", "value", "uniqueName", "description",
                                   "type", "enemy", "system", "missionType"):
                        add(key, value)
                elif isinstance(value, (dict, list)):
                    if isinstance(value, dict):
                        inner_name = (value.get("name") or value.get("value")
                                      or value.get("localized"))
                        if inner_name:
                            add(key, inner_name)
                    walk(value)
        elif isinstance(obj, list):
            for entry in obj:
                walk(entry)

    walk(raw)
    return out


def _translate(value, table):
    """Перевод по словарю; неизвестное значение возвращается как есть."""
    if not value:
        return ""
    text = str(value)
    return (table or {}).get(text, text)


def _mission_type(*candidates):
    """Тип миссии по-русски (``missionType``/``missionTypeKey``/``MT_*``)."""
    for cand in candidates:
        if cand:
            text = str(cand)
            text = MISSION_TYPE_KEYS.get(text, text)
            return MISSION_TYPE_RU.get(text, text)
    return ""


def sortie_boss_name(value):
    """``SORTIE_BOSS_RAPTOR`` → «Раптор»; неизвестный ключ — как есть."""
    text = str(value or "").strip()
    if not text:
        return ""
    key = text.upper().replace(" ", "_")
    if not key.startswith("SORTIE_BOSS_"):
        key = "SORTIE_BOSS_" + key
    return SORTIE_BOSS_RU.get(key, text)


def _faction(value):
    """Фракция по-русски."""
    return _translate(value, FACTION_RU)


#: Рекламный шум в официальном Events (скидки/соцсети/мерч) — не игровые события.
PROMO_TOKENS = (
    "joindiscord", "discord", "movember", "merch", "sale", "twitch",
    "prime access", "giveaway", "promo", "black friday", "support",
    "creator", "companion app", "app store", "google play", "bundle",
    "discount", "fundraiser", "donate", "steam",
)

#: Официальный API не отдаёт циклы Цетуса/Валлиса/Камбиона; секция остаётся
#: пустой (сознательно, чтобы не показывать выдуманные состояния).
OFFICIAL_CYCLES_NOTE = (
    "циклы открытых миров официальный worldState.php не отдаёт — "
    "секция «Циклы» скрыта")


def clean_event_text(text, limit=120):
    """Один язык и короткая строка из многоязычной «простыни» Events.

    Предпочитает сегмент с кириллицей (ru), иначе первый до ``·``/``|``;
    убирает повторы и обрезает до ``limit`` символов с многоточием.
    """
    text = " ".join(str(text or "").split())
    if not text:
        return ""
    parts = []
    for chunk in re.split(r"\s*[·|]\s*", text):
        chunk = chunk.strip(" .;")
        if chunk and chunk.casefold() not in {p.casefold() for p in parts}:
            parts.append(chunk)
    chosen = next((part for part in parts
                   if re.search(r"[А-Яа-яЁё]", part)),
                  parts[0] if parts else "")
    if len(chosen) > limit:
        chosen = chosen[:max(1, limit - 1)].rstrip() + "…"
    return chosen


def _event_is_promo(*texts):
    blob = " ".join(str(text or "") for text in texts).lower()
    return any(token in blob for token in PROMO_TOKENS)


def _event_reward(event, info):
    return official_reward(_first(info, "missionReward", "MissionReward",
                                  "reward") or
                           _first(event, "MissionReward", "Reward") or {})


def _cycle_glyph(state, is_day=None):
    """Глиф цикла: ☀/☾ для дня и ночи, ❄/☀ для Долины Сфер, ◐/◑ для Деймоса."""
    state = str(state or "").lower()
    if state in ("day", "warm") or (not state and is_day is True):
        return "☀"
    if state == "night" or (not state and is_day is False):
        return "☾"
    if state == "cold":
        return "❄"
    if state == "vome":
        return "◐"
    if state == "fass":
        return "◑"
    return ""


# ─────────────────────────────────────────────────────────────────────────
#  Официальный worldState.php → схема warframestat
# ─────────────────────────────────────────────────────────────────────────
def official_date(value):
    """Дата официального API (``{"$date": {"$numberLong": "..."}}``) → epoch."""
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("$date", "$numberLong", "date", "Date", "sec", "seconds"):
            if key in value:
                return official_date(value.get(key))
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        # Mongo Date в JSON иногда в миллисекундах
        if number > 100_000_000_000:
            number /= 1000.0
        return number
    text = str(value).strip()
    try:
        number = float(text)
        if number > 100_000_000_000:
            number /= 1000.0
        return number
    except Exception:
        pass
    return parse_iso(value)


def _official_id(value):
    if isinstance(value, dict):
        return str(value.get("$oid") or value.get("id") or "")
    return str(value or "")


def _first(mapping, *keys):
    mapping = _dict(mapping)
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _official_item_label(value):
    """Внутренний путь предмета → короткая подпись (иначе как есть)."""
    text = str(value or "").strip()
    if not text:
        return ""
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    return text


def official_reward(value):
    """Награда официального API → формат warframestat (items/countedItems)."""
    value = _dict(value)
    out = {}
    credits = _number(_first(value, "credits", "Credits"))
    if credits:
        out["credits"] = credits
    items = []
    for entry in value.get("items") or value.get("Items") or []:
        label = _official_item_label(entry)
        if label:
            items.append(label)
    counted = []
    for entry in (value.get("countedItems") or value.get("CountedItems")
                  or value.get("counted_items") or []):
        entry = _dict(entry)
        label = _official_item_label(_first(entry, "item", "type", "ItemType",
                                            "name"))
        if not label:
            continue
        counted.append({"item": label,
                        "count": _number(_first(entry, "count", "Count",
                                                "ItemCount")) or 1})
    if items:
        out["items"] = items
    if counted:
        out["countedItems"] = counted
    return out


def _official_faction(value):
    """``FC_GRINEER``/dict → английское имя для FACTION_RU."""
    if isinstance(value, dict):
        value = _first(value, "faction", "Faction", "factionKey", "FactionKey")
    text = str(value or "").strip()
    return OFFICIAL_FACTIONS.get(text, text)


def _official_mission_type(value):
    text = str(value or "").strip()
    return MISSION_TYPE_KEYS.get(text, text)


def official_to_warframestat(data, now=None):
    """Конвертирует официальный ``worldState.php`` в схему warframestat.

    Неизвестные ключи не роняют панель: они показываются как есть. Циклы
    открытых миров официальный API не отдаёт — секция «Циклы» будет пустой
    (это допустимо по контракту t11).
    """
    data = _dict(data)
    out = {}

    # ── ActiveMissions → fissures ─────────────────────────────────────
    fissures = []
    for mission in (data.get("ActiveMissions")
                    or data.get("ActiveMission") or []):
        mission = _dict(mission)
        if not mission:
            continue
        node = str(_first(mission, "Node", "node") or "")
        mission_type = str(_first(mission, "MissionType", "missionType") or "")
        modifier = str(_first(mission, "Modifier", "modifier") or "")
        if modifier.startswith("VoidT") and modifier in VOID_TIER_RAW:
            tier = VOID_TIER_RAW[modifier]
        else:
            tier = modifier
        enemy = _official_faction(_first(mission, "Enemy", "enemy", "Faction",
                                         "faction"))
        fissures.append({
            "id": _official_id(_first(mission, "_id", "id")),
            "expiry": official_date(_first(mission, "Expiry", "expiry")),
            "activation": official_date(_first(mission, "Activation",
                                               "activation")),
            "tier": tier or modifier,
            "tierKey": modifier,
            "missionType": _official_mission_type(mission_type),
            "missionTypeKey": mission_type,
            "node": node,
            "nodeKey": node,
            "enemy": enemy,
            "isHard": bool(_first(mission, "Hard", "hard")),
            "isStorm": bool(_first(mission, "VoidStorm", "isStorm", "storm")),
        })
    if fissures:
        out["fissures"] = fissures

    # ── Alerts / Events / DailyDeals → alerts + events ────────────────
    alerts = []
    for alert in data.get("Alerts") or []:
        alert = _dict(alert)
        if not alert:
            continue
        info = _dict(_first(alert, "MissionInfo", "mission", "Mission") or {})
        mission_type = _official_mission_type(
            _first(info, "missionType", "MissionType"))
        node = str(_first(info, "location", "Location", "node", "Node") or "")
        faction = _official_faction(_first(info, "faction", "Faction"))
        alerts.append({
            "id": _official_id(_first(alert, "_id", "id")),
            "expiry": official_date(_first(alert, "Expiry", "expiry")),
            "activation": official_date(_first(alert, "Activation",
                                               "activation")),
            "mission": {
                "type": mission_type,
                "node": node,
                "nodeKey": node,
                "faction": faction,
                "reward": official_reward(
                    _first(info, "missionReward", "MissionReward",
                           "reward") or {}),
                "description": (_first(info, "description", "Description",
                                       "missionDescription") or ""),
            },
        })
    if alerts:
        out["alerts"] = alerts

    events = []
    for event in data.get("Events") or []:
        event = _dict(event)
        if not event:
            continue
        info = _dict(_first(event, "MissionInfo", "mission") or {})
        messages = []
        for message in event.get("Messages") or []:
            message = _dict(message)
            text = _first(message, "Message", "message")
            if text:
                messages.append(str(text))
        tag = str(_first(event, "Tag", "tag") or "")
        raw_description = (_first(event, "Description", "description")
                           or " · ".join(messages))
        expiry = official_date(_first(event, "Expiry", "expiry"))
        reward = _event_reward(event, info)
        # t17: только игровые события с окончанием; рекламу/соцсети не показываем
        if expiry is None or _event_is_promo(tag, raw_description, *messages):
            continue
        description = clean_event_text(raw_description)
        if not description and not reward:
            continue
        mission_type = _official_mission_type(
            _first(info, "missionType", "MissionType", "MissionTypeKey")
            or _first(event, "MissionType", "missionType"))
        node = str(_first(event, "Node", "node")
                   or _first(info, "location", "Location") or "")
        events.append({
            "id": _official_id(_first(event, "_id", "id")),
            "expiry": expiry,
            "activation": official_date(_first(event, "Activation",
                                               "activation")),
            "tag": tag,
            "mission": {"type": mission_type, "node": node, "nodeKey": node,
                        "faction": _official_faction(
                            _first(info, "faction", "Faction")),
                        "description": description,
                        "reward": reward},
        })
    if events:
        out["events"] = events

    deals = []
    for deal in data.get("DailyDeals") or []:
        deal = _dict(deal)
        if not deal:
            continue
        store_item = _official_item_label(
            _first(deal, "StoreItem", "ItemType", "item"))
        discount = _number(_first(deal, "Discount", "discount"))
        deals.append({
            "id": _official_id(_first(deal, "_id", "id")),
            "expiry": official_date(_first(deal, "Expiry", "expiry")),
            "activation": official_date(_first(deal, "Activation",
                                               "activation")),
            "mission": {"type": "Ежедневная сделка",
                        "node": "", "nodeKey": "",
                        "description": store_item
                        + (f" · скидка {int(discount)}%" if discount else ""),
                        "reward": {}},
        })
    if deals:
        out["alerts"] = list(out.get("alerts") or []) + deals

    # ── Invasions ─────────────────────────────────────────────────────
    invasions = []
    for invasion in data.get("Invasions") or []:
        invasion = _dict(invasion)
        if not invasion:
            continue
        node = str(_first(invasion, "Node", "node") or "")
        attacker = _dict(_first(invasion, "Attacker", "attacker") or {})
        defender = _dict(_first(invasion, "Defender", "defender") or {})
        attacker_faction = _official_faction(
            _first(invasion, "AttackerFaction", "attackerFaction")
            or _first(attacker, "faction", "Faction")
            or _first(invasion, "Faction", "faction"))
        defender_faction = _official_faction(
            _first(invasion, "DefenderFaction", "defenderFaction")
            or _first(defender, "faction", "Faction"))
        completion = _number(_first(invasion, "Completion", "completion"))
        if completion is not None and 0 <= completion <= 1.0:
            completion *= 100.0       # официальный API отдаёт долю 0..1
        invasions.append({
            "id": _official_id(_first(invasion, "_id", "id")),
            "expiry": official_date(_first(invasion, "Expiry", "expiry")),
            "activation": official_date(_first(invasion, "Activation",
                                               "activation")),
            "node": node,
            "nodeKey": node,
            "desc": str(_first(invasion, "Description", "description",
                               "Tag") or ""),
            "completed": bool(_first(invasion, "Completed", "completed")),
            "completion": completion,
            "attacker": {
                "faction": attacker_faction,
                "factionKey": str(_first(attacker, "faction", "Faction")
                                  or attacker_faction),
                "reward": official_reward(
                    _first(invasion, "AttackerReward", "attackerReward",
                           "AttackerRewards") or {}),
            },
            "defender": {
                "faction": defender_faction,
                "factionKey": str(_first(defender, "faction", "Faction")
                                  or defender_faction),
                "reward": official_reward(
                    _first(invasion, "DefenderReward", "defenderReward",
                           "DefenderRewards") or {}),
            },
        })
    if invasions:
        out["invasions"] = invasions

    # ── Sorties ───────────────────────────────────────────────────────
    sorties = data.get("Sorties") or data.get("Sortie")
    if isinstance(sorties, dict):
        sorties = [sorties]
    for sortie in (sorties or []):
        sortie = _dict(sortie)
        if not sortie:
            continue
        variants = []
        for variant in (sortie.get("Variants") or sortie.get("variants") or []):
            variant = _dict(variant)
            if not variant:
                continue
            modifiers = []
            raw_modifiers = (_first(variant, "Modifier", "modifier",
                                    "Modifiers") or [])
            if isinstance(raw_modifiers, str):
                raw_modifiers = [raw_modifiers]
            for modifier in raw_modifiers:
                modifier = _dict(modifier)
                text = _first(modifier, "modifierDescription",
                              "ModifierDescription", "description",
                              "Description", "value", "Value")
                if text:
                    modifiers.append(str(text))
            variants.append({
                "missionType": _official_mission_type(
                    _first(variant, "MissionType", "missionType")),
                "missionTypeKey": str(_first(variant, "MissionType",
                                             "missionType") or ""),
                "node": str(_first(variant, "Node", "node") or ""),
                "nodeKey": str(_first(variant, "Node", "node") or ""),
                "modifierDescription": " · ".join(modifiers),
            })
        out["sortie"] = {
            "id": _official_id(_first(sortie, "_id", "id")),
            "expiry": official_date(_first(sortie, "Expiry", "expiry")),
            "activation": official_date(_first(sortie, "Activation",
                                               "activation")),
            "boss": sortie_boss_name(_first(sortie, "Boss", "boss")),
            "factionKey": _official_faction(_first(sortie, "Faction",
                                                   "faction")),
            "variants": variants,
        }
        break

    # ── VoidTraders → voidTrader ──────────────────────────────────────
    traders = data.get("VoidTraders") or data.get("VoidTrader")
    if isinstance(traders, dict):
        traders = [traders]
    for trader in (traders or []):
        trader = _dict(trader)
        if not trader:
            continue
        inventory = []
        for entry in (trader.get("Manifest") or trader.get("Inventory")
                      or trader.get("inventory") or []):
            entry = _dict(entry)
            if not entry:
                continue
            inventory.append({
                "item": _official_item_label(
                    _first(entry, "ItemType", "item", "name", "Type")),
                "ducats": _number(_first(entry, "PrimePrice", "ducats",
                                         "Ducats")) or 0,
                "credits": _number(_first(entry, "RegularPrice", "credits",
                                          "Credits")) or 0,
            })
        out["voidTrader"] = {
            "id": _official_id(_first(trader, "_id", "id")),
            "character": "Баро Ки'Тиир",
            "location": str(_first(trader, "Node", "node", "Location") or ""),
            "activation": official_date(_first(trader, "Activation",
                                               "activation")),
            "expiry": official_date(_first(trader, "Expiry", "expiry")),
            "inventory": inventory,
        }
        break

    return out


# ─────────────────────────────────────────────────────────────────────────
#  Нормализация ответа API → список событий
# ─────────────────────────────────────────────────────────────────────────
def normalize(data, now=None, node_dict=None):
    """Превращает ответ warframestat в плоский список строк для панели.

    :param data: словарь ответа API (как есть).
    :param now: «текущее» время (epoch, сек) — для тестов; по умолчанию ``time.time()``.
    :param node_dict: словарь русских имён узлов ``{"Hepit (Void)": "Гефит (Бездна)"}``.
    :return: список dict'ов: ``kind, section, title, detail, ends_at, started_at``.
    """
    now = time.time() if now is None else float(now)
    node_dict = node_dict or {}
    data = _dict(data)
    if any(key in data for key in OFFICIAL_KEYS):
        # официальный worldState.php: приводим к схеме warframestat
        data = official_to_warframestat(data, now=now)
    rows = []

    # ── разломы Бездны ─────────────────────────────────────────────────
    for fissure in data.get("fissures") or []:
        fissure = _dict(fissure)
        if not fissure:
            continue
        ends_at = parse_iso(fissure.get("expiry"))
        if ends_at is not None and ends_at <= now:
            continue
        tier_raw = str(fissure.get("tier") or "")
        tier = TIER_RU.get(tier_raw, tier_raw)
        mission = _mission_type(fissure.get("missionType"),
                                fissure.get("missionTypeKey"))
        node = _node_name(node_dict, fissure.get("node"), fissure.get("nodeKey"))
        enemy = _faction(fissure.get("enemy") or fissure.get("enemyKey"))
        title = " · ".join(x for x in (tier, mission, node, enemy) if x) or "Разлом"
        tags = []
        if fissure.get("isStorm"):
            tags.append("шторм Бездны")
        if fissure.get("isHard"):
            tags.append("стальной путь")
        badges = []
        if tier:
            badges.append({"kind": "tier", "text": tier})
        if mission:
            badges.append({"kind": "mission", "text": mission})
        if fissure.get("isHard"):
            badges.append({"kind": "steel", "text": "СП"})
        if fissure.get("isStorm"):
            badges.append({"kind": "storm", "text": "Шторм"})
        rows.append(_row("fissure", SECTION_FISSURES, title, " · ".join(tags),
                         ends_at, parse_iso(fissure.get("activation")),
                         headline=node, subtext=enemy, badges=badges,
                         tier=tier_raw))
    rows.sort(key=lambda r: (r["ends_at"] or float("inf")))

    # ── тревоги ────────────────────────────────────────────────────────
    for alert in data.get("alerts") or []:
        alert = _dict(alert)
        if not alert:
            continue
        ends_at = parse_iso(alert.get("expiry"))
        if ends_at is not None and ends_at <= now:
            continue
        mission = _dict(alert.get("mission"))
        mission_type = _mission_type(mission.get("type"), mission.get("typeKey"))
        node = _node_name(node_dict, mission.get("node"), mission.get("nodeKey"))
        faction = _faction(mission.get("faction") or mission.get("factionKey"))
        title = " · ".join(x for x in (mission_type, node, faction) if x) or "Тревога"
        detail = ""
        reward = _reward_text(mission.get("reward"))
        if reward:
            detail = f"награда: {reward}"
        elif mission.get("description"):
            detail = str(mission.get("description"))
        rows.append(_row("alert", SECTION_ALERTS, title, detail, ends_at,
                         parse_iso(alert.get("activation")),
                         headline=node, subtext=faction,
                         badges=[{"kind": "mission", "text": mission_type}]
                         if mission_type else []))

    # ── мировые события (Events официального API) ──────────────────────
    for event in data.get("events") or []:
        event = _dict(event)
        if not event:
            continue
        ends_at = parse_iso(event.get("expiry"))
        # t17: только события с окончанием; без срока — реклама/мусор
        if ends_at is None or ends_at <= now:
            continue
        mission = _dict(event.get("mission"))
        raw_description = (mission.get("description")
                           or event.get("tag") or "")
        if _event_is_promo(event.get("tag"), raw_description):
            continue
        mission_type = _mission_type(mission.get("type"),
                                     mission.get("typeKey"))
        node = _node_name(node_dict, mission.get("node"),
                          mission.get("nodeKey"))
        reward = _dict(mission.get("reward"))
        description = clean_event_text(raw_description)
        if not description and reward:
            description = _reward_text(reward)
        if not description:
            continue
        title = " · ".join(x for x in (mission_type, node) if x) or "Событие"
        rows.append(_row("event", SECTION_EVENTS, title, description,
                         ends_at, parse_iso(event.get("activation")),
                         headline=node or title,
                         subtext=description,
                         badges=[{"kind": "mission", "text": mission_type}]
                         if mission_type else []))

    # ── вторжения ──────────────────────────────────────────────────────
    for invasion in data.get("invasions") or []:
        invasion = _dict(invasion)
        if not invasion:
            continue
        ends_at = parse_iso(invasion.get("expiry"))
        if ends_at is not None and ends_at <= now:
            continue
        node = _node_name(node_dict, invasion.get("node"), invasion.get("nodeKey"))
        attacker = _dict(invasion.get("attacker"))
        defender = _dict(invasion.get("defender"))
        attacker_name = (_faction(attacker.get("faction")
                                  or attacker.get("factionKey"))
                         or "Неизвестно")
        defender_name = _faction(defender.get("faction")
                                 or defender.get("factionKey"))
        if node and defender_name:
            title = f"{node} · {attacker_name} vs {defender_name}"
        elif node:
            title = f"{node} · {attacker_name}"
        elif defender_name:
            title = f"{attacker_name} vs {defender_name}"
        else:
            title = attacker_name
        bits = []
        if invasion.get("desc"):
            bits.append(str(invasion.get("desc")))
        completion = _number(invasion.get("completion"))
        if invasion.get("completed"):
            progress = 100.0
            bits.append("завершено")
        elif completion is not None:
            # живой API иногда отдаёт -0.26 или 100.02 — показываем 0..100%
            progress = max(0.0, min(100.0, completion))
            bits.append(f"{progress:.0f}%")
        else:
            progress = None
        reward = (_reward_text(attacker.get("reward")) or
                  _reward_text(defender.get("reward")))
        if reward:
            bits.append(f"награда: {reward}")
        reward_lines = (_reward_lines(attacker.get("reward")) or
                        _reward_lines(defender.get("reward")))
        faction_raw = (attacker.get("factionKey") or attacker.get("faction")
                       or defender.get("factionKey")
                       or defender.get("faction") or "")
        rows.append(_row("invasion", SECTION_INVASIONS, title, " · ".join(bits),
                         ends_at, parse_iso(invasion.get("activation")),
                         headline=node,
                         subtext=(f"{attacker_name} vs {defender_name}"
                                  if defender_name else attacker_name),
                         badges=[{"kind": "faction", "text": attacker_name}]
                         if attacker_name else [],
                         progress=progress, faction=str(faction_raw),
                         rewards=reward_lines))

    # ── сортировка ─────────────────────────────────────────────────────
    sortie = _dict(data.get("sortie"))
    if sortie:
        ends_at = parse_iso(sortie.get("expiry"))
        boss = sortie.get("boss") or ""
        faction_raw = sortie.get("factionKey") or sortie.get("faction") or ""
        faction = _faction(faction_raw)
        title = " · ".join(x for x in (boss, faction) if x) or "Сортировка"
        rows.append(_row("sortie", SECTION_SORTIE, title, "", ends_at,
                         parse_iso(sortie.get("activation")),
                         headline=boss or "Сортировка", subtext="",
                         badges=[{"kind": "faction", "text": faction}] if faction else [],
                         faction=str(faction_raw), pinned=True))
        for variant in sortie.get("variants") or []:
            variant = _dict(variant)
            if not variant:
                continue
            mission_type = _mission_type(variant.get("missionType"),
                                         variant.get("missionTypeKey")) or "?"
            node = _node_name(node_dict, variant.get("node"), variant.get("nodeKey"))
            modifier = (variant.get("modifierDescription") or
                        variant.get("modifier") or "")
            rows.append(_row("sortie_variant", SECTION_SORTIE,
                             f"• {mission_type} · {node}" if node else f"• {mission_type}",
                             modifier, headline=node, subtext=modifier,
                             badges=[{"kind": "mission", "text": mission_type}]))

    # ── Баро Ки'Тиир ───────────────────────────────────────────────────
    trader = _dict(data.get("voidTrader"))
    if trader:
        activation = parse_iso(trader.get("activation"))
        expiry = parse_iso(trader.get("expiry"))
        character = trader.get("character") or "Баро Ки'Тиир"
        location = trader.get("location") or ""
        inventory = [x for x in (trader.get("inventory") or []) if isinstance(x, dict)]
        if activation and activation > now:
            # ещё не прилетел: живой API не всегда отдаёт поле active
            rows.append(_row("void_trader", SECTION_BARO, f"{character} — ещё не прибыл",
                             location, activation, ends_prefix="через",
                             headline=f"{character} — ещё не прибыл",
                             subtext=location, pinned=True))
        elif expiry and expiry <= now:
            pass                      # уже улетел — не показываем
        else:
            title = f"{character} — {location}" if location else character
            rows.append(_row("void_trader", SECTION_BARO, title,
                             f"ассортимент: {len(inventory)}" if inventory else "",
                             expiry, activation,
                             headline=location or character,
                             subtext=(f"ассортимент: {len(inventory)}"
                                      if inventory else ""),
                             pinned=True))
            for entry in inventory[:40]:
                item = entry.get("item") or entry.get("name") or "?"
                bits = []
                ducats = _number(entry.get("ducats"))
                credits = _number(entry.get("credits"))
                if ducats:
                    bits.append(f"{int(ducats)} ◆")
                if credits:
                    bits.append(f"{int(credits):,} кр".replace(",", " "))
                rows.append(_row("void_trader_item", SECTION_BARO, f"• {item}",
                                 " / ".join(bits), headline=item,
                                 subtext=" / ".join(bits)))

    # ── стальной путь ──────────────────────────────────────────────────
    steel = _dict(data.get("steelPath"))
    if steel:
        reward = _dict(steel.get("currentReward"))
        name = reward.get("name") or ""
        cost = _number(reward.get("cost"))
        detail = ""
        if name:
            detail = f"награда: {name}" + (f" ({int(cost)} ◆)" if cost else "")
        rows.append(_row("steel_path", SECTION_STEEL_PATH, "Стальной путь", detail,
                         parse_iso(steel.get("expiry")), parse_iso(steel.get("activation")),
                         headline="Стальной путь", subtext=detail,
                         badges=[{"kind": "steel", "text": "СП"}]))

    # ── ночная волна ───────────────────────────────────────────────────
    nightwave = _dict(data.get("nightwave"))
    if nightwave:
        title = "Ночная волна"
        season = nightwave.get("season")
        phase = nightwave.get("phase")
        if season:
            title += f" · сезон {season}"
        if phase:
            title += f" · фаза {phase}"
        active_count = len([c for c in (nightwave.get("activeChallenges") or [])
                            if isinstance(c, dict)])
        rows.append(_row("nightwave", SECTION_NIGHTWAVE, title, "",
                         parse_iso(nightwave.get("expiry")),
                         parse_iso(nightwave.get("activation")),
                         headline=title,
                         subtext=f"испытаний: {active_count}" if active_count else "",
                         pinned=True))
        for challenge in nightwave.get("activeChallenges") or []:
            challenge = _dict(challenge)
            if not challenge:
                continue
            bits = []
            if challenge.get("desc"):
                bits.append(str(challenge.get("desc")))
            reputation = _number(challenge.get("reputation"))
            if reputation:
                bits.append(f"+{int(reputation)} репутации")
            challenge_title = (challenge.get("title") or challenge.get("name") or "?")
            rows.append(_row("nightwave_challenge", SECTION_NIGHTWAVE,
                             f"• {challenge_title}", " · ".join(bits),
                             parse_iso(challenge.get("expiry")),
                             headline=str(challenge_title),
                             subtext=" · ".join(bits)))

    # ── циклы открытых миров ───────────────────────────────────────────
    cetus = _dict(data.get("cetusCycle"))
    if cetus:
        state = str(cetus.get("state") or "").lower()
        state_ru = CYCLE_STATE_RU.get(state) or ("день" if cetus.get("isDay") else "ночь")
        rows.append(_row("cycle", SECTION_CYCLES, f"Цетус: {state_ru}", "",
                         parse_iso(cetus.get("expiry")), headline="Цетус",
                         subtext=state_ru, state=state_ru,
                         glyph=_cycle_glyph(state, cetus.get("isDay"))))
    earth = _dict(data.get("earthCycle"))
    if earth:
        state_ru = "день" if earth.get("isDay") else "ночь"
        rows.append(_row("cycle", SECTION_CYCLES, f"Земля: {state_ru}", "",
                         parse_iso(earth.get("expiry")), headline="Земля",
                         subtext=state_ru, state=state_ru,
                         glyph=_cycle_glyph("", earth.get("isDay"))))
    vallis = _dict(data.get("vallisCycle"))
    if vallis:
        state = str(vallis.get("state") or "").lower()
        state_ru = CYCLE_STATE_RU.get(state) or ("тёпло" if vallis.get("isWarm")
                                                else "холодно")
        rows.append(_row("cycle", SECTION_CYCLES, f"Долина Сфер: {state_ru}", "",
                         parse_iso(vallis.get("expiry")), headline="Долина Сфер",
                         subtext=state_ru, state=state_ru,
                         glyph=_cycle_glyph(state, vallis.get("isWarm"))))
    cambion = _dict(data.get("cambionCycle"))
    if cambion:
        state = str(cambion.get("state") or "").lower()
        state_ru = CYCLE_STATE_RU.get(state) or str(cambion.get("state") or "?")
        rows.append(_row("cycle", SECTION_CYCLES, f"Деймос: {state_ru}", "",
                         parse_iso(cambion.get("expiry")), headline="Деймос",
                         subtext=state_ru, state=state_ru,
                         glyph=_cycle_glyph(state)))

    return rows


def sections(rows):
    """Группирует строки по секциям в фиксированном порядке (пустые — пропускает).

    Внутри секции — по времени окончания (ближайшие сверху); строки без срока
    (варианты сортиса, товары Баро) идут после, сохраняя исходный порядок.
    Заголовки групп (``pinned``) остаются первыми, чтобы подпись не терялась.
    """
    grouped = {}
    for row in rows or []:
        name = (row or {}).get("section") or "Прочее"
        grouped.setdefault(name, []).append(row)
    order = list(SECTION_ORDER) + [n for n in grouped if n not in SECTION_ORDER]
    result = []
    for name in order:
        items = grouped.get(name)
        if not items:
            continue
        items = sorted(items, key=lambda r: (0 if r.get("pinned") else 1,
                                             r.get("ends_at") is None,
                                             r.get("ends_at") or 0.0))
        result.append((name, items))
    return result


def format_text(groups, status="", now=None):
    """Текстовая версия панели (как в спецификации §3.4) — для консоли и тестов."""
    now = time.time() if now is None else now
    lines = []
    if status:
        lines.append(status)
    for name, rows in groups or []:
        lines.append(name.upper())
        for row in rows:
            tail = ""
            left = ends_in(row, now)
            if left is not None:
                prefix = row.get("ends_prefix") or "до"
                tail = f"  {prefix} {format_duration(left)}"
            lines.append(f"  {row.get('title') or ''}{tail}")
            if row.get("detail"):
                lines.append(f"    {row['detail']}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────
#  Клиент warframestat: сеть + кэш на диске + нормализация
# ─────────────────────────────────────────────────────────────────────────
class WarframeStatusClient:
    """Читает мировые события с api.warframestat.us и хранит последний снимок.

    Объект потокобезопасен: фоновый опрос пишет снимок под замком, GUI читает
    ``rows()``/``sections()`` без сети и без блокировок.

    :param cfg: словарь настроек оверлея (ключи ``events_*``, ``request_timeout``).
    :param data_dir: папка для кэша (по умолчанию — папка данных оверлея).
    :param http_get: подмена HTTP-вызова для тестов: ``callable(url, params) -> dict``.
    """

    URL = OFFICIAL_URL
    fmt_duration = staticmethod(format_duration)

    def __init__(self, cfg=None, data_dir=None, http_get=None):
        self.cfg = dict(cfg or {})
        self.language = str(self.cfg.get("events_language") or "ru").strip() or "ru"
        try:
            self.poll_ms = max(30000, int(self.cfg.get("events_poll_ms") or 60000))
        except Exception:
            self.poll_ms = 60000
        try:
            timeout_value = int(self.cfg.get("events_timeout") or 6)
        except Exception:
            timeout_value = 6
        # t11: быстрый таймаут 5–6 с вместо 20; общий бюджет запроса — 6 с
        self.timeout = max(4, min(6, timeout_value))
        try:
            self._attempts = max(1, min(3, int(
                self.cfg.get("events_attempts") or 2)))
        except Exception:
            self._attempts = 2
        self._retries = self._attempts - 1
        self.data_dir = data_dir or _default_data_dir()
        self.cache_path = os.path.join(self.data_dir, CACHE_NAME)
        self._http = http_get
        self._lock = threading.RLock()
        self._data = None
        self._fetched_at = 0.0
        self._error = ""
        self._node_dict = {}
        self._source = "кэш"
        self._last_latency = None
        self._last_http_status = None
        self._last_attempts = 0
        self._request_deadline = None
        self.load_node_dict()
        self.load_cache()

    # ── сеть ───────────────────────────────────────────────────────────
    def _http_get(self, url, params):
        """Один GET с таймаутом. Кидает исключение при любой проблеме."""
        import requests
        timeout = self.timeout
        if self._request_deadline is not None:
            left = self._request_deadline - time.monotonic()
            if left <= 0.2:
                raise TimeoutError("бюджет запроса исчерпан")
            timeout = max(1.0, min(timeout, left))
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        started = time.monotonic()
        response = requests.get(url, params=params, headers=headers,
                                timeout=timeout)
        self._last_latency = time.monotonic() - started
        self._last_http_status = int(response.status_code)
        if response.status_code != 200:
            raise EventsHTTPError(response.status_code)
        return response.json()

    def _describe_error(self, exc):
        """Понятная причина для панели (без стектрейсов)."""
        if isinstance(exc, EventsHTTPError):
            return f"сервер вернул {exc.status}"
        text = str(exc or "").strip()
        low = text.lower()
        if "http 5" in low or "http 504" in low or "bad gateway" in low:
            status = self._last_http_status
            return f"сервер вернул {status}" if status else "сервер вернул 5xx"
        if ("timed out" in low or "timeout" in low or "таймаут" in low
                or isinstance(exc, TimeoutError)):
            return "нет сети (таймаут)"
        if isinstance(exc, (OSError, ConnectionError)) or any(
                token in low for token in ("connection", "network", "resolve",
                                           "name or service", "unreachable",
                                           "сеть недоступна")):
            return f"нет сети ({text})" if text else "нет сети"
        if "json" in low or "пустой ответ" in low or "empty" in low:
            return "сервер вернул некорректный ответ"
        return f"{type(exc).__name__}: {text}" if text else type(exc).__name__

    def _combined_error(self, errors):
        reasons = []
        for _label, reason, _latency in errors:
            if reason and reason not in reasons:
                reasons.append(reason)
        return "; ".join(reasons[:2]) or "нет сети"

    def fetch(self, force=False):
        """Обновляет снимок: официальный API → warframestat, быстро и с кэшем.

        Таймаут одного запроса — ``events_timeout`` (5–6 с), попыток —
        ``events_attempts`` (по умолчанию 2); общий бюджет попытки — 6 с.
        При любой ошибке прежний кэш остаётся и показывается с пометкой.
        """
        if not self.cfg.get("events_enabled", True):
            with self._lock:
                self._error = "опрос выключен (events_enabled=false)"
            return False
        now = time.time()
        with self._lock:
            fresh = (self._data is not None and
                     (now - self._fetched_at) < self.poll_ms / 1000.0)
        if fresh and not force:
            return True
        params = {"language": self.language}
        sources = ((OFFICIAL_URL, {}, "официальный API"),
                   (EVENTS_URL, params, "warframestat"))
        errors = []
        attempts = 0
        deadline = time.monotonic() + 6.0
        self._request_deadline = deadline
        try:
            for attempt in range(self._attempts):
                for url, request_params, label in sources:
                    if time.monotonic() >= deadline:
                        break
                    attempts += 1
                    started = time.monotonic()
                    try:
                        getter = self._http or self._http_get
                        data = getter(url, request_params)
                        latency = time.monotonic() - started
                        if not isinstance(data, dict) or not data:
                            raise ValueError("пустой ответ")
                        with self._lock:
                            self._data = data
                            self._fetched_at = time.time()
                            self._error = ""
                            self._source = label
                            self._last_latency = latency
                            self._last_attempts = attempts
                        self.save_cache()
                        log(f"{label}: HTTP {self._last_http_status or 'ok'} "
                            f"за {latency:.2f} с")
                        # словарь узлов WFCD — один раз, только при реальной
                        # сети (в тестах http_get подменён, сеть не трогаем)
                        if label == "официальный API" and self._http is None \
                                and not self._node_dict:
                            self._download_node_dict()
                        return True
                    except Exception as exc:
                        latency = time.monotonic() - started
                        reason = self._describe_error(exc)
                        errors.append((label, reason, latency))
                        log(f"{label}: ошибка ({reason}) за {latency:.2f} с")
                if attempt < self._attempts - 1 and time.monotonic() < deadline:
                    time.sleep(0.3)
        finally:
            self._request_deadline = None
        with self._lock:
            self._error = self._combined_error(errors)
            self._last_attempts = attempts
            if self._data:
                self._source = "кэш"
        log(f"обновление не удалось ({self._error})")
        return False

    # ── кэш на диске ───────────────────────────────────────────────────
    def save_cache(self):
        """Пишет снимок в ``events_cache.json`` (единственный файл модуля)."""
        try:
            with self._lock:
                payload = {"ts": self._fetched_at, "language": self.language,
                           "source": self._source,
                           "latency": self._last_latency,
                           "http_status": self._last_http_status,
                           "data": self._data}
            os.makedirs(self.data_dir, exist_ok=True)
            tmp = self.cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, self.cache_path)
            return True
        except Exception as exc:
            log(f"кэш не сохранён ({exc})")
            return False

    def load_cache(self):
        """Поднимает последний снимок с диска (даже устаревший — лучше, чем ничего)."""
        try:
            if not os.path.isfile(self.cache_path):
                return False
            with open(self.cache_path, "r", encoding="utf-8-sig") as f:
                payload = json.load(f)
            data = (payload or {}).get("data")
            if not isinstance(data, dict) or not data:
                return False
            with self._lock:
                self._data = data
                try:
                    self._fetched_at = float((payload or {}).get("ts") or 0)
                except Exception:
                    self._fetched_at = 0.0
                self._source = "кэш"
                try:
                    self._last_latency = float((payload or {}).get("latency"))
                except Exception:
                    self._last_latency = None
            return True
        except Exception as exc:
            log(f"кэш не прочитан ({exc})")
            return False

    # ── словарь русских имён узлов ─────────────────────────────────────
    def load_node_dict(self, spec=None):
        """Читает словарь ``nodeKey → русское имя`` из ``events_translation``.

        ``"auto"`` — искать ``node_names_ru.json`` рядом с программой/в папке
        данных; путь — читать этот файл; пусто — показывать имена из API.
        """
        spec = self.cfg.get("events_translation", "auto") if spec is None else spec
        self._node_dict = {}
        if spec is None or spec is False:
            return {}
        if isinstance(spec, dict):
            self._node_dict = {str(k): str(v) for k, v in spec.items() if k and v}
            return self._node_dict
        text = str(spec).strip()
        if not text:
            return {}
        if text.lower() == "auto":
            candidate_paths = []
            env_path = os.environ.get("WF_NODE_DICT")
            if env_path:
                candidate_paths.append(env_path)
            meipass = getattr(sys, "_MEIPASS", "")
            if meipass:
                candidate_paths.append(os.path.join(meipass, NODE_DICT_NAME))
            candidate_paths.append(os.path.join(self.data_dir, NODE_DICT_NAME))
            candidate_paths.append(os.path.join(
                os.path.dirname(os.path.abspath(__file__)), NODE_DICT_NAME))
        else:
            candidate_paths = [text]
            if not os.path.isabs(text):
                candidate_paths = [
                    os.path.join(os.path.dirname(os.path.abspath(__file__)), text),
                    os.path.join(self.data_dir, text),
                    text,
                ]
        for path in candidate_paths:
            try:
                if not path or not os.path.isfile(path):
                    continue
                with open(path, "r", encoding="utf-8-sig") as f:
                    raw = json.load(f)
                if isinstance(raw, dict) and raw:
                    parsed = parse_node_dictionary(raw)
                    if parsed:
                        self._node_dict = parsed
                    else:
                        self._node_dict = {str(k): str(v)
                                           for k, v in raw.items()
                                           if k and isinstance(v, str)}
                if self._node_dict:
                    log(f"словарь узлов: {len(self._node_dict)} записей ({path})")
                    return self._node_dict
            except Exception as exc:
                log(f"словарь узлов не прочитан ({path}: {exc})")
        # кэш словаря, скачанного из WFCD (t11)
        cached = os.path.join(self.data_dir, NODE_DICT_CACHE)
        try:
            if os.path.isfile(cached):
                with open(cached, "r", encoding="utf-8-sig") as f:
                    parsed = parse_node_dictionary(json.load(f))
                if parsed:
                    self._node_dict = parsed
                    log(f"словарь узлов WFCD: {len(parsed)} записей ({cached})")
        except Exception as exc:
            log(f"словарь узлов WFCD не прочитан ({exc})")
        return self._node_dict

    def download_node_dict(self, urls=None, fetcher=None):
        """Скачать словарь WFCD один раз и закэшировать в папке данных."""
        urls = list(urls or self.cfg.get("events_dict_urls")
                    or NODE_DICT_URLS)
        if isinstance(urls, str):
            urls = [urls]
        for url in urls:
            try:
                if callable(fetcher):
                    raw = fetcher(url)
                else:
                    import requests
                    response = requests.get(
                        url, headers={"User-Agent": USER_AGENT},
                        timeout=self.timeout)
                    if response.status_code != 200:
                        log(f"словарь WFCD: HTTP {response.status_code} ({url})")
                        continue
                    raw = response.json()
                parsed = parse_node_dictionary(raw)
                if not parsed:
                    continue
                os.makedirs(self.data_dir, exist_ok=True)
                path = os.path.join(self.data_dir, NODE_DICT_CACHE)
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(parsed, f, ensure_ascii=False, indent=2)
                os.replace(tmp, path)
                with self._lock:
                    self._node_dict = parsed
                log(f"словарь WFCD: {len(parsed)} узлов закэшировано ({url})")
                return parsed
            except Exception as exc:
                log(f"словарь WFCD: ошибка ({type(exc).__name__}: {exc})")
        return {}

    def _download_node_dict(self):
        return self.download_node_dict()

    # ── чтение снимка (без сети) ───────────────────────────────────────
    def snapshot(self):
        """Последний снимок API (словарь; пустой, если данных ещё нет)."""
        with self._lock:
            return self._data or {}

    def status_text(self, now=None):
        """Строка состояния: источник и задержка, либо «кэш · N мин назад»."""
        now = time.time() if now is None else now
        with self._lock:
            data = self._data
            fetched_at = self._fetched_at
            error = self._error
            source = self._source
            latency = self._last_latency
        if not data:
            try:
                import ui_safe
                error_text = ui_safe.clean(error, fallback="", limit=80)
            except Exception:
                error_text = ""
            return f"нет данных · {error_text}" if error_text else "нет данных"
        age = max(0, int(now - fetched_at))
        if source in ("официальный API", "warframestat") and not error \
                and latency is not None:
            parts = [f"{source} · {latency:.1f} с", "обновлено"]
        else:
            parts = [f"кэш · {format_age(age)} назад"]
        if self.is_stale(now):
            parts.append("данные устарели")
        if error:
            parts.append(error)
        return " · ".join(parts)

    def source_label(self):
        with self._lock:
            return self._source or "—"

    def last_latency(self):
        with self._lock:
            return self._last_latency

    def last_http_status(self):
        with self._lock:
            return self._last_http_status

    def last_attempts(self):
        with self._lock:
            return self._last_attempts

    def is_stale(self, now=None):
        """``True``, если снимок старше трёх периодов опроса или его нет."""
        now = time.time() if now is None else now
        with self._lock:
            data, fetched_at = self._data, self._fetched_at
        if not data:
            return True
        return (now - fetched_at) > (3 * self.poll_ms / 1000.0)

    def last_error(self):
        with self._lock:
            return self._error

    def rows(self, now=None):
        """Список событий (с таймерами, посчитанными на момент ``now``)."""
        return normalize(self.snapshot(), now=now, node_dict=self._node_dict)

    def sections(self, now=None):
        """Список событий, сгруппированный по секциям."""
        return sections(self.rows(now=now))

    def text(self, now=None):
        """Готовая текстовая панель (консоль/тесты)."""
        return format_text(self.sections(now=now), self.status_text(now),
                           now=now)


# ─────────────────────────────────────────────────────────────────────────
#  Фоновый опрос: GUI не ждёт сеть
# ─────────────────────────────────────────────────────────────────────────
class EventsPoller(threading.Thread):
    """Раз в ``events_poll_ms`` обновляет снимок в отдельном потоке.

    ``on_update`` вызывается **в потоке опроса** сразу после попытки обновления
    (успешной или нет). GUI-код обязан передать данные в свой поток через
    сигнал/очередь — напрямую виджеты здесь трогать нельзя.
    """

    def __init__(self, client, poll_ms=None, on_update=None):
        super().__init__(daemon=True, name="wf-events")
        self.client = client
        try:
            interval = int(poll_ms if poll_ms is not None else client.poll_ms)
        except Exception:
            interval = 60000
        self.interval = max(5.0, interval / 1000.0)
        self.on_update = on_update
        self._stop_event = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self.last_ok = None       # результат последней попытки (True/False)

    def stop(self):
        self._stop_event.set()
        self._wake.set()

    def poll_now(self):
        """Просит обновить снимок не дожидаясь конца периода (открытие панели)."""
        self._wake.set()

    def run(self):
        while not self._stop_event.is_set():
            ok = False
            try:
                ok = bool(self.client.fetch(force=True))
            except Exception as exc:  # сеть не должна ронять поток
                log(f"поток опроса: {type(exc).__name__}: {exc}")
            with self._lock:
                self.last_ok = ok
            if self.on_update is not None:
                try:
                    self.on_update()
                except Exception as exc:
                    log(f"поток опроса: обработчик упал ({type(exc).__name__}: {exc})")
            if self._stop_event.is_set():
                break
            self._wake.wait(self.interval)
            self._wake.clear()
