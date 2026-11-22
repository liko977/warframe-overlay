# -*- coding: utf-8 -*-
"""История версий VoidFrameRU для карточки «Что нового».

Формат записи::

    {"version": "1.0.0", "date": "2026-11-22", "notes": ["...", "..."]}

Новые записи — сверху (в ``CHANGELOG``). Версия приложения НЕ дублируется:
:func:`app_version` берёт ``warframe.APP_VERSION`` (ленивый импорт, чтобы не
было циклической зависимости).

Использование::

    import changelog
    changelog.recent_patches(2)     # текущая версия + 2 предыдущих патча
    changelog.notes_for("1.0.0")    # ["...", ...]
    changelog.latest()              # самая новая запись

Автозаполнение при релизе (publish_update.py / prepare_build_ru.py):

    changelog.insert_entry_source("changelog.py", "1.0.1",
                                  "исправлен баг; ускорен скан")

Модуль без PyQt5: его читают центр уведомлений, настройки и тесты.
"""

import json
import os
import re
import time

#: Новые версии — сверху. ``notes`` — короткие пункты без markdown.
CHANGELOG = [
    {
        "version": "1.0.1",
        "date": "2026-11-22",
        "notes": [
            "Исправлен сдвиг плашек цен при устаревшей калибровке скана.",
            "Калибровка больше не захватывает окна приложения "
            "(настройки/оверлей).",
            "Кнопка «Сбросить калибровку» применяет каноническую сетку сразу.",
        ],
    },
    {
        "version": "1.0.0",
        "date": "2026-11-22",
        "notes": [
            "Приложение переименовано в VoidFrameRU (exe: VoidFrameRU.exe).",
            "Данные автоматически переносятся из старой папки — ничего не "
            "теряется.",
            "Пакет иконок предметов: однократное скачивание, офлайн-работа "
            "и недельное автообновление.",
            "«Дроп»: счётчик открытых реликвий, стоимость выбранного и "
            "средний доход.",
            "Исправлены русские хоткеи и утечка ошибок в интерфейс.",
        ],
    },
    {
        "version": "0.8.0",
        "date": "2026-11-21",
        "notes": [
            "Калибровка скана наград под разрешение и масштаб интерфейса.",
            "Геометрия WFInfo и темы оформления.",
            "Иконки предметов с прогрессом в шапке и настройках.",
        ],
    },
    {
        "version": "0.7.3",
        "date": "2026-11-19",
        "notes": [
            "Карточки наград не теряются, цена сета и кнопки в диалогах.",
            "Чат и центр уведомлений: тосты, история, звук.",
        ],
    },
]

#: Ссылка «Все изменения» (GitHub-репозиторий с README и историей версий).
CHANGELOG_URL = "https://github.com/liko977/warframe-overlay"


# ── версия приложения (без дублирования константы) ─────────────────────
def app_version():
    """``warframe.APP_VERSION`` (ленивый импорт) или ``""``."""
    try:
        import warframe
        return str(getattr(warframe, "APP_VERSION", "") or "")
    except Exception:
        return ""


def _normalize(items):
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        version = str(item.get("version") or "").strip()
        if not version:
            continue
        out.append({
            "version": version,
            "date": str(item.get("date") or "").strip(),
            "notes": [str(note) for note in (item.get("notes") or [])
                      if str(note).strip()],
        })
    return out


def entries():
    """Весь список записей (копия, новые сверху)."""
    return _normalize(CHANGELOG)


def latest():
    """Самая новая запись или ``{"version": "", "date": "", "notes": []}``."""
    items = entries()
    return items[0] if items else {"version": "", "date": "", "notes": []}


def notes_for(version):
    """Пункты для конкретной версии (без учёта регистра)."""
    wanted = str(version or "").strip().lower()
    for item in entries():
        if item["version"].lower() == wanted:
            return item["notes"]
    return []


def latest_notes(limit=5):
    """Последние (новые) пункты для карточки «Что нового»."""
    notes = list(latest().get("notes") or [])
    if limit:
        notes = notes[:max(1, int(limit))]
    return notes


def recent_patches(n=2, current_version=None):
    """Текущая версия + ``n`` предыдущих патчей (без дублей).

    Если текущей версии нет в ``CHANGELOG`` — она добавляется пустой записью
    (карточка покажет хотя бы заголовок), затем идут последние записи.
    Если записей меньше — возвращается сколько есть.
    """
    items = entries()
    if not items:
        return []
    try:
        count = max(0, int(n))
    except Exception:
        count = 2
    version = str(current_version or app_version() or "").strip()
    index = None
    for position, item in enumerate(items):
        if version and item["version"].lower() == version.lower():
            index = position
            break
    out = []
    if index is not None:
        out.append(items[index])
        tail = items[index + 1: index + 1 + count]
    else:
        out.append({"version": version, "date": "", "notes": []})
        tail = items[:count]
    seen = set()
    result = []
    for item in out + tail:
        key = item["version"].lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


# ── автозаполнение при релизе ───────────────────────────────────────────
def default_date(clock=None):
    """Дата релиза ``ГГГГ-ММ-ДД`` (UTC)."""
    try:
        stamp = clock() if callable(clock) else time.time()
    except Exception:
        stamp = time.time()
    return time.strftime("%Y-%m-%d", time.gmtime(stamp))


def split_notes(text, limit=3, max_len=140):
    """Разбить ``--notes`` на 1–3 короткие строки (по . ; , и переводам)."""
    text = str(text or "").strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?;])\s+|\s*[•·]\s*|\s*\|\s*", text)
    lines = []
    for part in parts:
        part = re.sub(r"\s+", " ", part).strip(" .;,-—")
        if not part:
            continue
        if len(part) > max_len:
            part = part[:max_len - 1].rstrip() + "…"
        lines.append(part)
        if len(lines) >= max(1, int(limit)):
            break
    return lines


def make_entry(version, notes="", date=None, clock=None):
    """Собрать запись из текста ``--notes`` (1–3 строки)."""
    version = str(version or "").strip()
    if not version:
        return {}
    if isinstance(notes, (list, tuple)):
        lines = [str(note).strip() for note in notes if str(note).strip()]
        lines = lines[:3]
    else:
        lines = split_notes(notes, limit=3)
    if not lines:
        lines = ["Обновление приложения."]
    return {"version": version, "date": str(date or default_date(clock)),
            "notes": lines}


def insert_entry_source(path, version, notes="", date=None, clock=None):
    """Дописать запись в ``changelog.py`` (для publish/prepare).

    Возвращает ``{"ok", "version", "reason"}``. Существующую версию не
    трогаем (``reason="exists"``), чтобы не затереть ручные заметки.
    """
    path = str(path or "")
    version = str(version or "").strip()
    result = {"ok": False, "version": version, "reason": ""}
    if not path or not os.path.isfile(path) or not version:
        result["reason"] = "bad_args"
        return result
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except Exception:
        result["reason"] = "read_error"
        return result
    if re.search(r'"version"\s*:\s*"' + re.escape(version) + r'"', text):
        result["reason"] = "exists"
        return result
    entry = make_entry(version, notes, date=date, clock=clock)
    if not entry:
        result["reason"] = "bad_entry"
        return result
    literal = "    " + json.dumps(entry, ensure_ascii=False) + ",\n"
    marker = "CHANGELOG = [\n"
    if marker not in text:
        # другой стиль форматирования — вставляем после первой строки списка
        match = re.search(r"CHANGELOG\s*=\s*\[[ \t]*\n", text)
        if not match:
            result["reason"] = "no_marker"
            return result
        insert_at = match.end()
        text = text[:insert_at] + literal + text[insert_at:]
    else:
        text = text.replace(marker, marker + literal, 1)
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except Exception:
        result["reason"] = "write_error"
        return result
    result["ok"] = True
    return result


__all__ = ["CHANGELOG", "CHANGELOG_URL", "app_version", "entries", "latest",
           "notes_for", "latest_notes", "recent_patches", "default_date",
           "split_notes", "make_entry", "insert_entry_source"]
