# -*- coding: utf-8 -*-
"""История переводов (t200): ``DATA_DIR/translate_history.json``.

Запись ``{utc, original, translated, source, error}``, лимит 200, атомарная
запись (tmp+replace). Файл перечитывается при каждом обращении: историю пишут
разные объекты (карточка Alt+R и вкладка «Перевод»).
"""
import json
import os
import threading
import time

FILE_NAME = "translate_history.json"
LIMIT = 200

_lock = threading.Lock()


def data_dir(cfg=None):
    return (str((cfg or {}).get("data_dir") or "")
            or os.environ.get("WF_DATA_DIR") or os.getcwd())


def path(cfg=None):
    return os.path.join(data_dir(cfg), FILE_NAME)


class TranslateHistory:
    """История переводов: добавление, чтение, удаление, очистка."""

    def __init__(self, cfg=None, limit=LIMIT, logger=None):
        self.cfg = dict(cfg or {})
        try:
            self.limit = max(1, min(int((cfg or {}).get("translate_history_limit",
                                                        limit) or limit),
                                    LIMIT))
        except (TypeError, ValueError):
            self.limit = LIMIT
        self.logger = logger

    def enabled(self):
        return bool(self.cfg.get("translate_history_enabled", True))

    def _load(self):
        try:
            with open(path(self.cfg), encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        data = [dict(item) for item in raw
                if isinstance(item, dict) and item.get("original")]
        return data[-self.limit:]

    def _save(self, data):
        target = path(self.cfg)
        try:
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            temp = target + ".tmp"
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(list(data), handle, ensure_ascii=False)
            os.replace(temp, target)
            return True
        except OSError as exc:
            if callable(self.logger):
                self.logger(f"История переводов: не записана "
                            f"({type(exc).__name__})")
            return False

    def add(self, original, translated="", source="region", error="",
            utc=None):
        """Добавить запись (при ``translate_history_enabled=False`` — нет)."""
        original = str(original or "").strip()
        if not original or not self.enabled():
            return None
        record = {"utc": float(utc or time.time()),
                  "original": original,
                  "translated": str(translated or "").strip(),
                  "source": str(source or "region"),
                  "error": str(error or "")}
        with _lock:
            data = self._load()
            data.append(record)
            self._save(data[-self.limit:])
        return dict(record)

    def records(self):
        """Записи от новых к старым."""
        with _lock:
            return [dict(item) for item in reversed(self._load())]

    def remove(self, utc):
        """Удалить запись по времени (float)."""
        with _lock:
            data = self._load()
            before = len(data)
            keep = [item for item in data
                    if abs(float(item.get("utc") or 0) - float(utc)) > 1e-6]
            if len(keep) == before:
                return False
            self._save(keep)
            return True

    def clear(self):
        with _lock:
            return self._save([])
