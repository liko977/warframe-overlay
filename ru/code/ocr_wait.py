# -*- coding: utf-8 -*-
"""t204: ожидание OCR, если движок ещё грузится (Alt+D / Alt+F).

Кадр уже захвачен — повторять выделение не нужно: раз в 300 мс проверяем
движок, как только он появился — отдаём его в ``on_ready``. По таймауту
(45 с) вызывается ``on_timeout``; ``cancel()`` останавливает ожидание.
"""
from PyQt5.QtCore import QTimer

INTERVAL_MS = 300
TIMEOUT_SEC = 45.0


class OcrWait:
    """Таймер ожидания готового OCR-движка."""

    def __init__(self, getter, on_ready, on_timeout=None, log=print,
                 interval_ms=INTERVAL_MS, timeout=TIMEOUT_SEC, on_tick=None):
        self.getter = getter
        self.on_ready = on_ready
        self.on_timeout = on_timeout
        self.log = log
        self.interval_ms = int(interval_ms)
        self.timeout = float(timeout)
        self.timer = None
        self.elapsed = 0
        self._done = False
        self.on_tick = on_tick

    def seconds_left(self):
        return max(0, int(self.timeout - self.elapsed / 1000.0))

    def start(self):
        if self.timer is not None:
            return False
        self.timer = QTimer()
        self.timer.setInterval(self.interval_ms)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        return True

    def _tick(self):
        if self._done:
            return False
        self.elapsed += self.interval_ms
        engine = None
        try:
            engine = self.getter()
        except Exception as exc:
            self.log(f"OCR: ожидание — ошибка ({type(exc).__name__})")
        if engine is not None:
            self._done = True
            self.stop()
            self.log(f"OCR: дождались за {self.elapsed / 1000.0:.1f} с")
            try:
                self.on_ready(engine)
            except Exception as exc:
                self.log(f"OCR: ожидание — сбой ({type(exc).__name__})")
            return True
        if callable(self.on_tick):
            try:
                self.on_tick(self.seconds_left())
            except Exception:
                pass
        if self.elapsed >= self.timeout * 1000:
            self._done = True
            self.stop()
            self.log(f"OCR: не готов за {int(self.timeout)} с — "
                     "распознавание отменено")
            if callable(self.on_timeout):
                try:
                    self.on_timeout()
                except Exception:
                    pass
            return False
        return False

    def stop(self):
        if self.timer is not None:
            try:
                self.timer.stop()
            finally:
                self.timer = None
        return True

    def cancel(self):
        self._done = True
        return self.stop()
