# -*- coding: utf-8 -*-
"""Словарь названий предметов для автокоррекции OCR (1.0.0).

Источник — каталог warframe.market (ru+en), который приложение уже грузит в
``MarketAPI.items``. Словарь хранится компактно::

    assets/dict/ocr_dict.json.gz   # {"version":1, "tokens": {...},
                                  #  "names": [...], "parts": {...}}

* ``tokens`` — частотный словарь слов из названий (для fuzzy-коррекции);
  ключ — слово в нижнем регистре, значение — частота;
* ``names`` — полные названия (ru+en) для финальной проверки «стало лучше»;
* ``parts`` — карта «нормализованное имя → каноническое отображаемое имя».

Генерируется при сборке из снапшота каталога:
``python tools/build_ocr_dict.py --items items_cache_ru.json``.
Если файла нет — корректор просто не работает (фолбэк на текущую логику),
исключений не бывает.
"""

import gzip
import json
import os
import re
import sys

DICT_NAME = "ocr_dict.json.gz"
SCHEMA = 1

#: Частые слова частей — попадут в словарь даже на бедном каталоге.
CORE_TOKENS = (
    "прайм", "чертеж", "чертёж", "каркас", "нейрооптика", "система",
    "тетива", "плечо", "верхнее", "нижнее", "приёмник", "приемник",
    "ствол", "приклад", "клинок", "рукоять", "связь", "барабан", "цевьё",
    "цевье", "затвор", "набор", "реликвия", "часть", "чертёж:",
)

_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9'’\-]+")

_state = {"tried": False, "path": "", "tokens": {}, "display": {},
          "names": [], "parts": {}, "mtime": 0.0}


# ── пути (исходники / one-folder / _MEIPASS) ──────────────────────────
def _roots():
    roots = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            roots.append(meipass)
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        roots.append(os.path.join(exe_dir, "_internal"))
    app_dir = os.environ.get("WF_APP_DIR") or ""
    if app_dir:
        roots.append(app_dir)
    here = os.path.dirname(os.path.abspath(__file__))
    roots += [here, os.path.dirname(here)]
    return [root for root in roots if root]


def dict_path():
    """Первый найденный ``assets/dict/ocr_dict.json.gz`` или ``""``."""
    for root in _roots():
        path = os.path.join(root, "assets", "dict", DICT_NAME)
        if os.path.isfile(path):
            return path
    return ""


# ── сборка словаря из каталога ────────────────────────────────────────
def _tokens_of(text):
    return [word.casefold() for word in _WORD_RE.findall(str(text or ""))]


def build_bundle(items):
    """Собрать словарь из каталога ``items`` (dict norm_name → item)."""
    tokens = {}
    display = {}
    names = []
    parts = {}
    seen_names = set()
    for item in (items or {}).values():
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        for token in _WORD_RE.findall(name):
            key = token.casefold()
            if len(key) < 2:
                continue
            tokens[key] = tokens.get(key, 0) + 1
            # отображаемая форма — самая частая оригинальная запись слова
            bucket = display.setdefault(key, {})
            bucket[token] = bucket.get(token, 0) + 1
        key = name.casefold()
        if key not in seen_names:
            seen_names.add(key)
            names.append(name)
        slug = str(item.get("slug") or "")
        if slug:
            current = parts.get(name.casefold())
            if not current or len(name) < len(current):
                parts[name.casefold()] = name
    best = {}
    for key, bucket in display.items():
        best[key] = max(bucket.items(), key=lambda pair: pair[1])[0]
    for token in CORE_TOKENS:
        tokens.setdefault(token.casefold(), 1)
        best.setdefault(token.casefold(), token.capitalize())
    names.sort()
    return {"version": SCHEMA, "tokens": tokens, "display": best,
            "names": names, "parts": parts, "count": len(names)}


def save_bundle(bundle, path):
    """Записать ``dict`` в gzip-JSON (родительские папки создаются)."""
    path = str(path or "")
    if not path:
        return False
    folder = os.path.dirname(os.path.abspath(path))
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    raw = json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))
    with gzip.open(tmp, "wt", encoding="utf-8") as handle:
        handle.write(raw)
    os.replace(tmp, path)
    return True


# ── загрузка (кэш в памяти) ───────────────────────────────────────────
def load(force=False):
    """Загрузить словарь; ``False`` — файла нет/ошибка (фолбэк)."""
    path = dict_path()
    if not path:
        _state.update({"tried": True, "path": "", "tokens": {},
                       "display": {}, "names": [], "parts": {},
                       "mtime": 0.0})
        return False
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        mtime = 0.0
    if (not force and _state["tried"] and _state["path"] == path
            and _state["mtime"] == mtime):
        return bool(_state["tokens"])
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        _state.update({"tried": True, "path": path, "tokens": {},
                       "display": {}, "names": [], "parts": {},
                       "mtime": mtime})
        return False
    tokens = data.get("tokens") if isinstance(data, dict) else {}
    names = data.get("names") if isinstance(data, dict) else []
    parts = data.get("parts") if isinstance(data, dict) else {}
    display = data.get("display") if isinstance(data, dict) else {}
    _state.update({"tried": True, "path": path,
                   "tokens": {str(k).casefold(): int(v)
                              for k, v in (tokens or {}).items()},
                   "display": {str(k).casefold(): str(v)
                               for k, v in (display or {}).items()},
                   "names": [str(n) for n in (names or []) if str(n).strip()],
                   "parts": {str(k).casefold(): str(v)
                             for k, v in (parts or {}).items()},
                   "mtime": mtime})
    return bool(_state["tokens"])


def available():
    """Есть ли загруженный словарь."""
    return bool(load() and _state["tokens"])


def tokens():
    load()
    return dict(_state["tokens"])


def names():
    load()
    return list(_state["names"])


def parts():
    load()
    return dict(_state["parts"])


def display():
    """Отображаемая форма токенов (Прайм, Чертёж, …)."""
    load()
    return dict(_state["display"])


def state():
    """Диагностика: путь, размеры словаря."""
    load()
    return {"path": _state["path"], "tokens": len(_state["tokens"]),
            "names": len(_state["names"]), "parts": len(_state["parts"])}


def reset():
    """Сбросить кэш (тесты)."""
    _state.update({"tried": False, "path": "", "tokens": {},
                   "display": {}, "names": [], "parts": {},
                   "mtime": 0.0})


__all__ = ["DICT_NAME", "CORE_TOKENS", "dict_path", "build_bundle",
           "save_bundle", "load", "available", "tokens", "names", "parts",
           "state", "reset", "display"]
