# -*- coding: utf-8 -*-
"""Кэши сканирования Alt+Q (0.6.4).

* OCR-кэш по sha1 пиксельного кропа — повторный скан того же экрана без OCR;
* LRU-кэш «распознанный текст → match/slug»;
* TTL-кэш цен (по умолчанию 10 минут).
"""
import hashlib
import threading
from collections import OrderedDict


def image_hash(image):
    """sha1 пикселей numpy-изображения (или bytes)."""
    try:
        import numpy as np
        data = np.ascontiguousarray(image).tobytes()
    except Exception:
        data = bytes(image or b"")
    return hashlib.sha1(data).hexdigest()


class LRUCache:
    def __init__(self, capacity=128):
        self.capacity = max(1, int(capacity))
        self._data = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key, default=None):
        with self._lock:
            if key not in self._data:
                self.misses += 1
                return default
            value = self._data.pop(key)
            self._data[key] = value
            self.hits += 1
            return value

    def set(self, key, value):
        with self._lock:
            if key in self._data:
                self._data.pop(key)
            self._data[key] = value
            while len(self._data) > self.capacity:
                self._data.popitem(last=False)
        return value

    def clear(self):
        with self._lock:
            self._data.clear()
            self.hits = 0
            self.misses = 0

    def __len__(self):
        return len(self._data)

    def __contains__(self, key):
        with self._lock:
            return key in self._data


class TTLCache:
    def __init__(self, ttl=600.0, capacity=512, clock=None):
        self.ttl = max(1.0, float(ttl))
        self.capacity = max(1, int(capacity))
        self.clock = clock or __import__("time").monotonic
        self._data = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0

    def get(self, key, default=None):
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return default
            ts, value = item
            if self.clock() - ts > self.ttl:
                self._data.pop(key, None)
                return default
            self._data.pop(key)
            self._data[key] = (ts, value)
            self.hits += 1
            return value

    def set(self, key, value):
        with self._lock:
            self._data[key] = (self.clock(), value)
            self._data.move_to_end(key)
            while len(self._data) > self.capacity:
                self._data.popitem(last=False)
        return value

    def clear(self):
        with self._lock:
            self._data.clear()
            self.hits = 0

    def __len__(self):
        return len(self._data)


class ScanCache:
    """Три кэша сканера: OCR-кроп, текст→match, цены с TTL."""

    def __init__(self, price_ttl=600.0, price_capacity=512,
                 ocr_capacity=64, match_capacity=512):
        self.ocr = LRUCache(ocr_capacity)
        self.match = LRUCache(match_capacity)
        self.price = TTLCache(price_ttl, price_capacity)
        self._lock = threading.Lock()

    @staticmethod
    def image_key(image):
        return image_hash(image)

    def ocr_get(self, key):
        return self.ocr.get(key)

    def ocr_set(self, key, value):
        return self.ocr.set(key, value)

    def match_get(self, text):
        return self.match.get(str(text or "").strip().lower())

    def match_set(self, text, value):
        return self.match.set(str(text or "").strip().lower(), value)

    def price_get(self, name):
        return self.price.get(str(name or "").strip().lower())

    def price_set(self, name, value):
        return self.price.set(str(name or "").strip().lower(), value)

    def clear(self):
        self.ocr.clear()
        self.match.clear()
        self.price.clear()


__all__ = ["ScanCache", "LRUCache", "TTLCache", "image_hash"]
