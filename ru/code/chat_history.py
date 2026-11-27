# -*- coding: utf-8 -*-
"""История переписки по нику (t188): ``DATA_DIR/chat_history.json``.

Хранит до 50 ников по 100 последних сообщений. Запись атомарная (tmp +
replace), чтение — один раз на объект; ошибки диска не поднимаются в UI.
"""
import json
import os
import threading

FILE_NAME = "chat_history.json"
NICK_LIMIT = 50
MESSAGE_LIMIT = 100

_lock = threading.Lock()


def data_dir(cfg=None):
    """Папка данных: ``cfg['data_dir']`` → ``WF_DATA_DIR`` → текущая."""
    return (str((cfg or {}).get("data_dir") or "")
            or os.environ.get("WF_DATA_DIR") or os.getcwd())


def path(cfg=None):
    return os.path.join(data_dir(cfg), FILE_NAME)


class ChatHistory:
    """Записи ``{nick: [ {utc, text, label, outgoing} ]}`` с лимитами."""

    def __init__(self, cfg=None, limit=MESSAGE_LIMIT):
        self.cfg = dict(cfg or {})
        self.limit = max(1, min(int(limit or MESSAGE_LIMIT), MESSAGE_LIMIT))
        self._data = None

    def _load(self):
        if self._data is not None:
            return self._data
        data = {}
        try:
            with open(path(self.cfg), encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            raw = {}
        if isinstance(raw, dict):
            for nick, items in raw.items():
                if not isinstance(items, list):
                    continue
                clean = [dict(item) for item in items
                         if isinstance(item, dict) and item.get("text")]
                if clean:
                    data[str(nick)] = clean[-self.limit:]
        self._data = data
        return data

    def _save(self):
        target = path(self.cfg)
        try:
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            temp = target + ".tmp"
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(self._data or {}, handle, ensure_ascii=False)
            os.replace(temp, target)
            return True
        except OSError:
            return False

    def add(self, nick, text, label="", outgoing=False, utc=None):
        """Добавить сообщение; ``False`` — пусто/дубль/лимит ников."""
        nick = str(nick or "").strip()
        text = str(text or "").strip()
        if not nick or not text:
            return False
        with _lock:
            data = self._load()
            items = data.setdefault(nick, [])
            record = {"nick": nick, "utc": float(utc or 0) or _now(),
                      "text": text, "label": str(label or ""),
                      "outgoing": bool(outgoing)}
            if items and items[-1].get("text") == text \
                    and items[-1].get("utc") == record["utc"]:
                return False
            items.append(record)
            del items[:-self.limit]
            if len(data) > NICK_LIMIT:
                for old in list(data)[:-NICK_LIMIT]:
                    data.pop(old, None)
            self._save()
        return True

    def records(self, nick=None):
        """Записи по нику (или все, плоским списком) — копии словарей."""
        with _lock:
            data = self._load()
            if nick is None:
                out = []
                for items in data.values():
                    out.extend(dict(item) for item in items)
                return out
            return [dict(item) for item in data.get(str(nick), [])]

    def nicks(self):
        with _lock:
            return list(self._load().keys())

    def clear(self, nick=None):
        """Удалить историю ника (``nick=None`` — всю)."""
        with _lock:
            data = self._load()
            if nick is None:
                data.clear()
            else:
                data.pop(str(nick), None)
            self._save()
            return True


def _now():
    import time
    return time.time()
