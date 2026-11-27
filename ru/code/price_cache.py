# -*- coding: utf-8 -*-
"""Дисковый кэш цен (t199): ``DATA_DIR/price_cache.json``.

``{slug: {price, ducats, sell_online, ts}}`` — TTL из ``price_cache_ttl_min``
(по умолчанию 10 минут), лимит 2000 записей, атомарная запись (tmp+replace).
Ошибки диска не поднимаются в UI: кэш просто остаётся пустым.
"""
import json
import os
import threading
import time

FILE_NAME = "price_cache.json"
DEFAULT_TTL_MIN = 10
LIMIT = 2000

_lock = threading.Lock()


def data_dir(cfg=None):
    return (str((cfg or {}).get("data_dir") or "")
            or os.environ.get("WF_DATA_DIR") or os.getcwd())


def path(cfg=None):
    return os.path.join(data_dir(cfg), FILE_NAME)


def ttl_seconds(cfg=None):
    try:
        minutes = float((cfg or {}).get("price_cache_ttl_min", DEFAULT_TTL_MIN))
    except (TypeError, ValueError):
        minutes = DEFAULT_TTL_MIN
    return max(0.0, minutes) * 60.0


class PriceCache:
    """Цены по slug с TTL и лимитом записей."""

    def __init__(self, cfg=None, logger=None):
        self.cfg = dict(cfg or {})
        self.logger = logger
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
            for slug, item in raw.items():
                if isinstance(item, dict) and item.get("price") is not None:
                    data[str(slug)] = dict(item)
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
        except OSError as exc:
            if callable(self.logger):
                self.logger(f"Кэш цен: не записан ({type(exc).__name__})")
            return False

    def get(self, slug, now=None):
        """Свежая запись по slug или ``None`` (просроченные не отдаём)."""
        slug = str(slug or "").strip()
        if not slug:
            return None
        now = time.time() if now is None else float(now)
        with _lock:
            item = self._load().get(slug)
        if not item:
            return None
        try:
            age = now - float(item.get("ts") or 0)
        except (TypeError, ValueError):
            return None
        if age < 0 or age > ttl_seconds(self.cfg):
            return None
        return dict(item)

    def put(self, slug, info, now=None):
        """Сохранить цену: ``{price, ducats, sell_online, ts}``."""
        slug = str(slug or "").strip()
        info = dict(info or {})
        if not slug or info.get("price") is None:
            return False
        record = {"price": info.get("price"),
                  "ducats": info.get("ducats"),
                  "sell_online": info.get("sell_online"),
                  "ts": time.time() if now is None else float(now)}
        with _lock:
            data = self._load()
            data.pop(slug, None)
            data[slug] = record
            while len(data) > LIMIT:
                data.pop(next(iter(data)))
            return self._save()

    def clear(self):
        with _lock:
            self._data = {}
            return self._save()
