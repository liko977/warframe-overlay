# -*- coding: utf-8 -*-
"""Автокоррекция названий наград OCR (1.0.0).

Идея: даже если OCR прочитал «Парис Нижнее Праёчо» или «Коруфелл Прайм
риклад», восстановить настоящий предмет по словарю каталога
(:mod:`ocr_dictionary`) и отдать его существующему matcher'у.

Порядок коррекции:
1. ``aliases.json`` из DATA_DIR (ручные исправления) — самый высокий приоритет;
2. нормализация похожих символов в кириллическом контексте
   (``0→о``, ``P→Р``, ``C→С``, ``y→у`` и т.п.);
3. точное совпадение токена со словарём — быстрый путь (не меняем);
4. склейки/разъединения (``ПраймНейрооптика`` → ``Прайм Нейрооптика``);
5. fuzzy по словарю токенов (rapidfuzz) с порогом по длине и штрафом за
   разницу длины >2;
6. финальная проверка: название должно «похорошеть» (точное совпадение с
   каталогом или рост fuzzy-score) — иначе возвращаем исходное.

Лог: одна строка на изменённое название
``OCR-фикс: 'Праёчо'→'Плечо' (score 0.71→0.98)``.
"""

import json
import os
import re

import ocr_dictionary as od

#: Латинские/цифровые двойники кириллицы (в кириллическом контексте).
LOOKALIKE = {
    "a": "а", "b": "в", "c": "с", "e": "е", "h": "н", "k": "к",
    "m": "м", "o": "о", "p": "р", "r": "г", "t": "т", "x": "х",
    "y": "у", "i": "и", "l": "и",
    "0": "о", "1": "и", "3": "з", "6": "б", "8": "в",
}

#: Порог fuzzy по длине токена (символов).
THRESHOLDS = ((4, 90.0), (7, 85.0), (99, 80.0))

#: Известные ошибки OCR частей (как в fix_part_names), до fuzzy.
BUILTIN_FIXES = {
    "праёчо": "Плечо", "праечо": "Плечо", "праёчо:": "Плечо",
    "праертёней": "Плечо", "праертеней": "Плечо",
    # t47: искажения из app.log друга (OCR путает «г»/«э», «и»/«ы»)
    "чжуга": "Чжугэ", "чжуги": "Чжугэ", "чжугу": "Чжугэ",
    "чжуге": "Чжугэ", "чжуга:": "Чжугэ",
    "чжуга прайм": "Чжугэ Прайм",
    "чжуга прайм:": "Чжугэ Прайм:",
    "чжуга прайм чертеж": "Чжугэ Прайм (Чертеж)",
}

_CYR_RE = re.compile(r"[А-Яа-яЁё]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_CORE_RE = re.compile(r"^([^\wА-Яа-яЁё]*)(.*?)([^\wА-Яа-яЁё]*)$", re.S)
_QTY_RE = re.compile(r"^\s*(?:(\d+)\s*[xXхХ×]|[xXхХ×]\s*(\d+))\s+")
_SPACES_RE = re.compile(r"\s+")

_aliases_state = {"tried": False, "path": "", "mtime": 0.0, "map": {}}
_names_cache = {"state": None, "names": [], "lower": [], "lookup": {}}


# ── алиасы (DATA_DIR/aliases.json) ────────────────────────────────────
def _data_dir():
    env = os.environ.get("WF_DATA_DIR")
    if env:
        return env
    try:
        import warframe
        return str(getattr(warframe, "DATA_DIR", "") or "")
    except Exception:
        return os.path.dirname(os.path.abspath(__file__))


def aliases_path():
    return os.path.join(_data_dir(), "aliases.json")


def load_aliases(force=False):
    """Прочитать ``aliases.json`` (нижний регистр ключей) без исключений."""
    path = aliases_path()
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        mtime = 0.0
    if (not force and _aliases_state["tried"]
            and _aliases_state["path"] == path
            and _aliases_state["mtime"] == mtime):
        return dict(_aliases_state["map"])
    mapping = {}
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            raw = json.load(handle)
        if isinstance(raw, dict):
            for key, value in raw.items():
                key = _SPACES_RE.sub(" ", str(key or "")).strip().casefold()
                value = str(value or "").strip()
                if key and value:
                    mapping[key] = value
    except Exception:
        mapping = {}
    _aliases_state.update({"tried": True, "path": path, "mtime": mtime,
                           "map": mapping})
    return dict(mapping)


def save_aliases(mapping):
    """Записать ``aliases.json`` (для кнопки «исправить» в будущем)."""
    path = aliases_path()
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(dict(mapping or {}), handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    load_aliases(force=True)
    return path


# ── словарь: кэш отображаемых форм и имён ─────────────────────────────
def _dict_ready():
    if not od.available():
        return False
    state = od.state()
    if _names_cache["state"] != (state.get("path"), state.get("names")):
        names = od.names()
        _names_cache.update({
            "state": (state.get("path"), state.get("names")),
            "names": names,
            "lower": [name.casefold() for name in names],
            "lookup": {name.casefold(): name for name in names},
        })
    return True


def _threshold(token):
    length = len(token)
    for limit, value in THRESHOLDS:
        if length <= limit:
            return value
    return THRESHOLDS[-1][1]


def _display(token):
    """Каноническая форма слова (Прайм, Чертёж…)."""
    key = str(token or "").casefold()
    mapping = od.display()
    if key in mapping:
        return mapping[key]
    return str(token or "").capitalize()


def _lookalike(token):
    """Заменить латиницу/цифры на кириллицу, если слово кириллическое."""
    text = str(token or "")
    if not _CYR_RE.search(text):
        return text
    out = []
    for char in text:
        lower = char.lower()
        replacement = LOOKALIKE.get(lower)
        if replacement is None:
            out.append(char)
        elif char.isupper():
            out.append(replacement.upper())
        else:
            out.append(replacement)
    return "".join(out)


def _split_parts(token):
    """Разбить «ПраймНейрооптика» на два словарных слова (если выйдет)."""
    text = str(token or "")
    if len(text) < 8:
        return None
    tokens_map = od.tokens()
    best = None
    for cut in range(2, len(text) - 1):
        left, right = text[:cut], text[cut:]
        lkey, rkey = left.casefold(), right.casefold()
        if lkey not in tokens_map or rkey not in tokens_map:
            continue
        score = tokens_map[lkey] + tokens_map[rkey]
        if best is None or score > best[0]:
            best = (score, _display(lkey) + " " + _display(rkey))
    return best[1] if best else None


def _best_token(token):
    """Найти лучшее словарное слово для токена: (display, score 0..1)."""
    tokens_map = od.tokens()
    key = str(token or "").casefold()
    if not key:
        return "", 0.0
    if key in tokens_map:
        return token, 1.0
    normalized = _lookalike(key)
    if normalized in tokens_map:
        return _display(normalized), 1.0
    try:
        from rapidfuzz import fuzz, process
    except Exception:
        import difflib

        def _ratio(a, b):
            return difflib.SequenceMatcher(None, a, b).ratio() * 100.0

        best = None
        for candidate in tokens_map:
            score = _ratio(normalized, candidate)
            if best is None or score > best[0]:
                best = (score, candidate)
        ratio_value, hit = best if best else (0.0, "")
    else:
        result = process.extractOne(normalized, list(tokens_map.keys()),
                                    scorer=fuzz.ratio)
        if not result:
            return "", 0.0
        hit = result[0]
        ratio_value = float(result[1])
    if not hit:
        return "", 0.0
    penalty = 8.0 if abs(len(normalized) - len(hit)) > 2 else 0.0
    score = max(0.0, ratio_value - penalty)
    if score < _threshold(normalized):
        return "", score / 100.0
    return _display(hit), score / 100.0


def _token_core(token):
    match = _CORE_RE.match(token)
    if not match:
        return "", str(token), ""
    return match.group(1), match.group(2), match.group(3)


def _fix_token(token, aliases):
    """Исправить один токен: (new_token, old_core, new_core, score_before, after, alias)."""
    prefix, core, suffix = _token_core(token)
    if not core:
        return token, "", "", 0.0, 0.0, False
    key = core.casefold()
    if key in aliases:
        value = aliases[key]
        # ручное исправление может быть многословным
        return prefix + value + suffix, core, value, 0.0, 1.0, True
    if key in BUILTIN_FIXES:
        value = BUILTIN_FIXES[key]
        return prefix + value + suffix, core, value, 0.7, 1.0, False
    tokens_map = od.tokens()
    if key in tokens_map:
        display_form = _display(key)
        if token == display_form:
            return token, core, core, 1.0, 1.0, False
        return prefix + display_form + suffix, core, display_form, 1.0, 1.0,             False
    glued = _split_parts(core)
    if glued:
        return prefix + glued + suffix, core, glued, 0.0, 1.0, False
    display, score = _best_token(core)
    if not display:
        normalized = _lookalike(core)
        if (normalized != core
                and _LATIN_RE.search(core) and _CYR_RE.search(core)):
            # «Пpим» → «Прим»: латиница внутри кириллического слова
            return (prefix + _display(normalized) + suffix, core,
                    _display(normalized), round(score, 2), 1.0, False)
        return token, core, core, round(score, 2), round(score, 2), False
    if display.casefold() == key:
        return token, core, core, 1.0, 1.0, False
    return prefix + display + suffix, core, display, round(score, 2), 1.0, False


def _name_score(text):
    if not _dict_ready():
        return 0.0
    names_lower = _names_cache["lower"]
    if not text or not names_lower:
        return 0.0
    try:
        from rapidfuzz import fuzz, process
        result = process.extractOne(str(text).casefold(), names_lower,
                                    scorer=fuzz.WRatio)
        return float(result[1]) / 100.0 if result else 0.0
    except Exception:
        import difflib
        best = 0.0
        lowered = str(text).casefold()
        for name in names_lower:
            score = difflib.SequenceMatcher(None, lowered, name).ratio()
            if score > best:
                best = score
        return best


def normalize_quantity(text):
    """«2 X Чертёж» / «X2 Чертёж» → «×2 Чертёж»."""
    match = _QTY_RE.match(str(text or ""))
    if not match:
        return str(text or "")
    number = match.group(1) or match.group(2)
    return "×" + number + " " + str(text)[match.end():].lstrip()


def correct_name(raw, cfg=None, logger=None, log=True):
    """Скорректировать название; вернуть ``(name, info)``.

    ``info``: ``{"changed": [...], "score_before": ..., "score_after": ...,
    "corrected": bool}``. Без словаря — исходное имя и пустой info (фолбэк).
    """
    original = str(raw or "")
    info = {"changed": [], "score_before": 0.0, "score_after": 0.0,
            "corrected": False}
    if not original.strip():
        return original, info
    if not _dict_ready():
        return original, info
    aliases = load_aliases()
    text = _SPACES_RE.sub(" ", original).strip()
    if _dict_ready() and text.casefold() in _names_cache["lookup"]:
        return original, info
    # ручной алиас на всё название
    whole = aliases.get(text.casefold())
    if whole:
        info.update({"changed": [(text, whole, 0.0, 1.0)],
                     "score_before": _name_score(text),
                     "score_after": _name_score(whole),
                     "corrected": True})
        if log:
            _log_changes(info, logger)
        return whole, info
    prepared = normalize_quantity(text)
    quantity_changed = prepared != text
    tokens = prepared.split(" ")
    changed = [("2 X", "×2", 0.0, 1.0)] if quantity_changed else []
    alias_used = False
    rebuilt = []
    for token in tokens:
        if not token:
            continue
        new_token, old_core, new_core, before, after, alias = _fix_token(
            token, aliases)
        alias_used = alias_used or alias
        if new_token != token:
            changed.append((old_core, new_core, before, after))
        rebuilt.append(new_token)
    corrected = " ".join(rebuilt)
    if not changed:
        return original, info
    score_before = _name_score(text)
    if quantity_changed:
        # нормализация количества безопасна всегда
        info.update({"changed": changed,
                     "score_before": round(score_before, 2),
                     "score_after": round(_name_score(corrected), 2),
                     "corrected": True})
        if log:
            _log_changes(info, logger)
        return corrected, info
    score_after = _name_score(corrected)
    exact = corrected.casefold() in _names_cache["lookup"]
    improved = score_after > score_before + 0.02
    mixed = any(_LATIN_RE.search(old_core) and _CYR_RE.search(old_core)
                for old_core, _new, _b, _a in changed)
    strong = any(_b >= 1.0 and _a >= 1.0 for _old, _new, _b, _a in changed)
    # ручной алиас/mixed/точное слово — доверяем; иначе не портим
    if (not alias_used and not exact and not improved and not mixed
            and not strong):
        return original, info
    info.update({"changed": changed, "score_before": round(score_before, 2),
                 "score_after": round(score_after, 2), "corrected": True})
    if log:
        _log_changes(info, logger)
    return corrected, info


def _log_changes(info, logger=None):
    """Одна строка в app.log: ``OCR-фикс: 'X'→'Y' (score 0.71→0.98)``."""
    if not info.get("changed"):
        return ""
    parts = []
    for old, new, before, after in info["changed"][:4]:
        parts.append(f"'{old}'→'{new}' (score {before:.2f}→{after:.2f})")
    message = "OCR-фикс: " + ", ".join(parts)
    target = logger
    if not callable(target):
        try:
            import warframe
            target = getattr(warframe, "log", None)
        except Exception:
            target = None
    if callable(target):
        try:
            target(message)
        except Exception:
            pass
    return message


def correct_cards(cards, cfg=None, logger=None):
    """Скорректировать список карточек (dict name) на месте; вернуть счётчик."""
    corrected = 0
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        name = str(card.get("name") or "")
        if not name or card.get("unrecognized"):
            continue
        fixed, info = correct_name(name, cfg=cfg, logger=logger)
        if fixed and fixed != name:
            card["name"] = fixed
            card["ocr_corrected"] = True
            corrected += 1
    return corrected


def reset_caches():
    _aliases_state.update({"tried": False, "path": "", "mtime": 0.0, "map": {}})
    _names_cache.update({"state": None, "names": [], "lower": [], "lookup": {}})


__all__ = ["LOOKALIKE", "THRESHOLDS", "aliases_path", "load_aliases",
           "save_aliases", "normalize_quantity", "correct_name",
           "correct_cards", "reset_caches"]
