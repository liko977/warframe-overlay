# -*- coding: utf-8 -*-
"""Канонические русские названия предметов для «Дропа» (1.0.0, t37).

Единый источник: ``canonical_name(slug, raw_name, path=None)``.
Порядок:
  1. каталог warframe.market по slug (кэш ``items_cache_ru.json``) — русское
     имя как есть («Седо Прайм: Приёмник»);
  2. по английскому имени/пути: CamelCase/подчёркивания → слова → слаг →
     каталожное русское имя; иначе локальные правила
     (Blueprint→«(Чертеж)», Prime→«Прайм», части → «Приёмник» и т.д.);
  3. уже русское имя возвращается идемпотентно (без изменений);
  4. совсем неизвестно → «Неизвестный предмет» (никогда не «?»).
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
UNKNOWN = "Неизвестный предмет"

PART_WORDS = {
    "systems": "Система", "system": "Система",
    "chassis": "Каркас", "neuroptics": "Нейрооптика",
    "helmet": "Шлем", "barrel": "Ствол", "receiver": "Приёмник",
    "stock": "Приклад", "string": "Тетива", "grip": "Рукоять",
    "handle": "Рукоять", "blade": "Клинок", "link": "Связь",
    "ornament": "Украшение", "upper limb": "Верхнее Плечо",
    "lower limb": "Нижнее Плечо", "blueprint": "(Чертеж)",
    "prime": "Прайм", "set": "Комплект",
}
#: Частые названия, которых нет в кэше каталога (Forma и т.п.).
NAME_FIXES = {
    "forma": "Форма", "forma blueprint": "Форма (Чертеж)",
    "orokin catalyst": "Катализатор Орокин",
    "orokin reactor": "Реактор Орокин",
    "exilus adapter": "Адаптер Экзилус",
}

_INDEX = {"tried": False, "slug": {}, "en": {}}


def _load_index():
    """Индекс каталога: slug → ru/en, english name → ru."""
    if _INDEX["tried"]:
        return _INDEX
    _INDEX["tried"] = True
    candidates = []
    data_dir = os.environ.get("WF_DATA_DIR")
    if data_dir:
        candidates.append(os.path.join(data_dir, "items_cache_ru.json"))
    candidates.append(os.path.join(HERE, "items_cache_ru.json"))
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except Exception:
            continue
        items = (data or {}).get("items") if isinstance(data, dict) else None
        for value in (items or {}).values():
            if not isinstance(value, dict):
                continue
            slug = str(value.get("slug") or "")
            name = str(value.get("name") or "").strip()
            lang = str(value.get("lang") or "").lower()
            if not slug or not name:
                continue
            entry = _INDEX["slug"].setdefault(slug, {})
            entry[lang or "ru"] = name
            if lang == "en":
                _INDEX["en"][name.casefold()] = slug
        if _INDEX["slug"]:
            break
    return _INDEX


def _split_words(text):
    """CamelCase/подчёркивания/путь → слова."""
    text = str(text or "")
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    text = re.sub(r"^(?:Types|Recipes|Weapons|StoreItems|Lotus)+", " ", text,
                  flags=re.IGNORECASE)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    text = text.replace("_", " ").replace("-", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _slugify(text):
    words = _split_words(text).casefold()
    words = re.sub(r"[^a-z0-9 ]+", " ", words)
    return re.sub(r"\s+", "_", words.strip())


def _catalog(slug, en_name):
    index = _load_index()
    if slug:
        entry = index["slug"].get(str(slug))
        if entry and entry.get("ru"):
            return entry["ru"]
    if en_name:
        key = en_name.casefold()
        found = index["en"].get(key)
        if found:
            entry = index["slug"].get(found) or {}
            if entry.get("ru"):
                return entry["ru"]
    return ""


def _local_english(en_name):
    """Локализация английского имени по правилам (без каталога)."""
    lower = en_name.casefold()
    if lower in NAME_FIXES:
        return NAME_FIXES[lower]
    words = en_name.split()
    is_blueprint = "blueprint" in lower
    is_set = "set" in lower
    if is_blueprint:
        words = [w for w in words if w.casefold() != "blueprint"]
    if is_set:
        words = [w for w in words if w.casefold() != "set"]
    parent = []
    part = ""
    index = 0
    while index < len(words):
        pair = " ".join(words[index:index + 2]).casefold()
        single = words[index].casefold()
        if pair in ("upper limb", "lower limb"):
            part = PART_WORDS[pair]
            index += 2
            continue
        if single in PART_WORDS and single not in ("prime", "blueprint",
                                                   "set", "system"):
            part = PART_WORDS[single]
            index += 1
            continue
        if single == "prime":
            parent.append("Прайм")
        else:
            parent.append(NAME_FIXES.get(single, words[index]))
        index += 1
    base = " ".join(parent).strip()
    base = re.sub(r"\bПрайм\s+Прайм\b", "Прайм", base)
    if is_set:
        return f"Комплект {base}".strip()
    if part:
        text = f"{base}: {part}" if base else part
    else:
        text = base
    if is_blueprint:
        text += " (Чертеж)"
    return text.strip()


def canonical_name(slug="", raw_name="", path=None):
    """Каноническое русское имя предмета (см. модульный docstring)."""
    raw = str(raw_name or "").strip()
    path = str(path or "").strip()
    words = _split_words(raw or path)
    en_name = words
    derived = _slugify(raw or path)
    key_slug = str(slug or "").strip() or derived
    # 1) каталог по slug/английскому имени
    found = _catalog(key_slug, en_name)
    if found:
        return found
    # 2) уже русское — не портим (идемпотентность)
    if re.search(r"[А-Яа-яЁё]", raw):
        return re.sub(r"\s+", " ", raw).strip()
    if re.search(r"[А-Яа-яЁё]", en_name):
        return re.sub(r"\s+", " ", en_name).strip()
    # 3) локальные правила
    if en_name:
        localized = _local_english(en_name)
        if localized and re.search(r"[А-Яа-яЁё]", localized):
            return localized
    return UNKNOWN


def display_name(item):
    """Удобная обёртка для записи ``record_pick``/``mark_picked``."""
    item = item or {}
    return canonical_name(item.get("slug") or "",
                          item.get("name") or item.get("picked_name") or "",
                          item.get("path") or "")


__all__ = ["UNKNOWN", "canonical_name", "display_name", "PART_WORDS",
           "NAME_FIXES"]
