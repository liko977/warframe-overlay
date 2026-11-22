# -*- coding: utf-8 -*-
"""Безопасные тексты для UI (0.8.0): никакие исключения не попадают к людям.

Внутренние ошибки (traceback, repr исключения, сообщения Qt/keyboard) должны
уходить только в ``app.log``. Для подсказок, статусов и меток здесь есть:

* :func:`has_internal` — есть ли в строке след исключения/отладки;
* :func:`clean` — одна понятная строка (или ``fallback``), максимум ``limit``;
* :func:`tooltip` — многострочная подсказка без внутренних строк;
* :func:`log_internal` — ОДНА короткая строка в лог (тип + контекст, без repr);
* :func:`human_error` — залогировать и вернуть человеческий текст.

Модуль не импортирует PyQt5 и warframe на уровне модуля: при недоступном
модуле лог молча пропускается.
"""

import re

#: Признаки внутреннего/технического текста, который нельзя показывать.
INTERNAL_MARKERS = (
    "Traceback (most recent call last)",
    "File \"",
    "ValueError:",
    "RuntimeError:",
    "TypeError:",
    "KeyError:",
    "AttributeError:",
    "IndexError:",
    "OSError:",
    "Exception:",
    "not mapped",
    "is not mapped",
    "cannot be started from another thread",
    "Timers cannot be started",
    "QObject::",
    "QWidget::",
    "QMetaObject::",
    "key name",
    "no attribute",
)

_WHITESPACE_RE = re.compile(r"\s+")


def has_internal(text):
    """Похоже ли ``text`` на исключение/traceback/технический вывод."""
    value = str(text or "")
    if not value.strip():
        return False
    lowered = value.lower()
    for marker in INTERNAL_MARKERS:
        if marker.lower() in lowered:
            return True
    return False


def _one_line(text):
    return _WHITESPACE_RE.sub(" ", str(text or "")).strip()


def clean(text, fallback="—", limit=160):
    """Одна безопасная строка: без внутренних следов, без переводов строк."""
    value = _one_line(text)
    if not value or has_internal(value):
        return str(fallback or "—")
    if len(value) > int(limit):
        value = value[:max(1, int(limit) - 1)].rstrip() + "…"
    return value


def tooltip(text, fallback="", limit=400):
    """Многострочная подсказка: убираем только внутренние строки."""
    lines = []
    for line in str(text or "").splitlines():
        line = _one_line(line)
        if not line or has_internal(line):
            continue
        lines.append(line)
    if not lines:
        return str(fallback or "")
    value = "\n".join(lines)
    if len(value) > int(limit):
        value = value[:max(1, int(limit) - 1)].rstrip() + "…"
    return value


def log_internal(context, exc=None, logger=None):
    """Записать внутреннюю ошибку в app.log ОДНОЙ строкой (без repr/traceback).

    ``context`` — короткое описание места («продажа», «статус»). В лог идут
    только тип и безопасное имя, без текста исключения: он часто содержит
    repr вложенных ошибок (Qt/keyboard) и не должен светиться даже в логе
    рядом с пользовательскими действиями.
    """
    name = ""
    try:
        if exc is not None:
            name = type(exc).__name__
    except Exception:
        name = ""
    message = str(context or "ошибка")
    if name:
        message = f"{message}: {name}"
    target = logger
    if not callable(target):
        try:
            import warframe
            target = getattr(warframe, "log", None)
        except Exception:
            target = None
    if callable(target):
        try:
            target(str(message))
        except Exception:
            pass
    return str(message)


def human_error(context, exc=None, fallback=None, logger=None):
    """Залогировать внутреннюю ошибку и вернуть безопасный текст для UI."""
    log_internal(context, exc, logger=logger)
    if not fallback:
        fallback = "не удалось — подробности в app.log"
    return str(fallback)


__all__ = ["INTERNAL_MARKERS", "has_internal", "clean", "tooltip",
           "log_internal", "human_error"]
