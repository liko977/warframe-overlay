# -*- coding: utf-8 -*-
"""Миграция данных WarframePriceOverlayRU → VoidFrameRU (1.0.0).

Новый каталог данных: ``%LOCALAPPDATA%\\VoidFrameRU``. При первом запуске,
если он ещё пуст, данные **копируются** (не перемещаются) из старого:

* ``%LOCALAPPDATA%\\WarframePriceOverlayRU``;
* ``D:\\WarframePriceOverlayRU``;
* ``%LOCALAPPDATA%\\WarframePriceOverlay`` (прежний неименованный вариант);
* каталог приложения рядом с exe — но только если в нём есть ``config.json``.

В ``config.json`` нового каталога дописываются ``migration_done``,
``migrated_from``, ``migrated_at``; остальные ключи сохраняются без потерь.
Ошибки копирования не мешают запуску: каждая — одна строка в ``app.log``.

Модуль без PyQt5: его можно вызывать из консольных режимов и тестов.
"""

import json
import os
import shutil
import time

#: Файлы, которые переносим как есть (если есть).
MIGRATED_FILES = (
    "config.json",
    "config.json.bak",
    "notifications.json",
    "farm_stats.json",
    "relics_log.txt",
    "last_rewards.txt",
    "selected.txt",
    "icon_index.json",
    "aliases.json",
    "update.log",
    "app.log",
)

#: Каталоги, которые копируем целиком.
MIGRATED_DIRS = ("icons", "code")

#: Признак «в каталоге уже есть данные приложения».
MARKER_FILES = ("config.json", "farm_stats.json", "notifications.json",
                "relics_log.txt", "icon_index.json")


def default_data_dir(environ=None, app_name="VoidFrameRU"):
    """``%LOCALAPPDATA%\\VoidFrameRU`` (или ``~/...`` без LOCALAPPDATA)."""
    env = os.environ if environ is None else environ
    base = env.get("LOCALAPPDATA") or os.path.expanduser("~")
    if app_name is None:
        try:
            import branding
            app_name = branding.APP_NAME
        except Exception:
            app_name = "VoidFrameRU"
    return os.path.join(base, str(app_name))


def old_data_dirs(app_dir="", environ=None):
    """Кандидаты старых каталогов данных (в порядке приоритета)."""
    env = os.environ if environ is None else environ
    base = env.get("LOCALAPPDATA") or os.path.expanduser("~")
    out = []
    for name in ("WarframePriceOverlayRU", "WarframePriceOverlay"):
        out.append(os.path.join(base, name))
    out.append(os.path.join("D:" + os.sep, "WarframePriceOverlayRU"))
    if app_dir:
        out.append(os.path.abspath(str(app_dir)))
    # убрать дубликаты и пустые, сохранив порядок
    seen = set()
    result = []
    for path in out:
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def has_data(path):
    """Есть ли в ``path`` признаки прежних данных приложения."""
    try:
        if not os.path.isdir(path):
            return False
        for name in MARKER_FILES:
            if os.path.isfile(os.path.join(path, name)):
                return True
        for name in MIGRATED_DIRS:
            if os.path.isdir(os.path.join(path, name)):
                return True
    except Exception:
        return False
    return False


def find_old_dir(app_dir="", environ=None, skip=(), candidates=None):
    """Первый старый каталог с данными (кроме ``skip``/нового каталога).

    ``candidates`` позволяет явно задать список (тесты/портативный режим);
    по умолчанию — :func:`old_data_dirs`.
    """
    skip_keys = {os.path.normcase(os.path.abspath(p)) for p in skip if p}
    paths = (list(candidates) if candidates is not None
             else old_data_dirs(app_dir, environ))
    for path in paths:
        try:
            if not has_data(path):
                continue
            if os.path.normcase(os.path.abspath(path)) in skip_keys:
                continue
        except Exception:
            continue
        return path
    return ""


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, path)


def _log(logger, message):
    if callable(logger):
        try:
            logger(message)
        except Exception:
            pass


def migrate_if_needed(data_dir="", app_dir="", logger=None, clock=None,
                      environ=None, old_dirs=None):
    """Скопировать данные из старого каталога, если новый пуст.

    ``old_dirs`` — явный список кандидатов (тесты/портативный режим);
    ``None`` — стандартный поиск :func:`old_data_dirs`.

    Возвращает ``{"migrated": bool, "from": str, "target": str,
    "copied": [...], "skipped": [...], "errors": [...]}``. Никогда не бросает
    исключение наружу: при ошибках приложение продолжает работу.
    """
    env = os.environ if environ is None else environ
    target = os.path.abspath(str(data_dir or default_data_dir(env)))
    result = {"migrated": False, "from": "", "target": target,
              "copied": [], "skipped": [], "errors": []}
    try:
        os.makedirs(target, exist_ok=True)
    except Exception as exc:
        result["errors"].append(f"каталог не создан: {type(exc).__name__}")
        _log(logger, "Данные: каталог не создан — работаю без миграции")
        return result
    # уже мигрировали (или новый каталог уже наполнен)
    existing_cfg = _read_json(os.path.join(target, "config.json"))
    if existing_cfg.get("migration_done"):
        result["skipped"].append("migration_done")
        return result
    if has_data(target):
        result["skipped"].append("target_not_empty")
        return result
    old = find_old_dir(app_dir, env, skip=(target,), candidates=old_dirs)
    if not old:
        result["skipped"].append("no_old_dir")
        return result
    result["from"] = old
    copied = []
    skipped = []
    errors = []
    for name in MIGRATED_FILES:
        src = os.path.join(old, name)
        dst = os.path.join(target, name)
        try:
            if not os.path.isfile(src):
                continue
            if os.path.exists(dst):
                skipped.append(name)
                continue
            shutil.copy2(src, dst)
            copied.append(name)
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}")
    for name in MIGRATED_DIRS:
        src = os.path.join(old, name)
        dst = os.path.join(target, name)
        try:
            if not os.path.isdir(src):
                continue
            if os.path.isdir(dst) and os.listdir(dst):
                skipped.append(name)
                continue
            shutil.copytree(src, dst, dirs_exist_ok=True)
            copied.append(name + os.sep)
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}")
    result["copied"] = copied
    result["skipped"] = skipped
    result["errors"] = errors
    result["migrated"] = bool(copied)
    # отметки в config нового каталога (ключи пользователя сохраняются)
    try:
        cfg_path = os.path.join(target, "config.json")
        cfg = _read_json(cfg_path)
        cfg["migration_done"] = True
        cfg["migrated_from"] = old
        cfg["migrated_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.gmtime(clock() if callable(clock)
                                             else time.time()))
        _write_json(cfg_path, cfg)
        result["config_updated"] = True
    except Exception as exc:
        result["config_updated"] = False
        errors.append(f"config.json: {type(exc).__name__}")
        result["errors"] = errors
    count = len(copied)
    _log(logger, f"Данные: миграция из {old} → {target} "
                 f"(скопировано: {count})")
    for error in errors:
        _log(logger, f"Данные: миграция, пропущено ({error})")
    return result


__all__ = ["MIGRATED_FILES", "MIGRATED_DIRS", "default_data_dir",
           "old_data_dirs", "has_data", "find_old_dir", "migrate_if_needed"]
