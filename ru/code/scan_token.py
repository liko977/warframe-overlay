# -*- coding: utf-8 -*-
"""1.1.0: токен кооперативной отмены скана (t116)."""
import threading


class ScanToken:
    def __init__(self, generation=0, logger=None):
        self.generation = int(generation)
        self._event = threading.Event()
        self.stage = "start"
        self.logger = logger
        self.logged = False

    def cancel(self, reason="новый запрос"):
        self._event.set()
        return True

    def cancelled(self):
        return self._event.is_set()

    def check(self, stage):
        """Отметить стадию; True — скан нужно прервать."""
        self.stage = str(stage)
        if not self._event.is_set():
            return False
        if not self.logged:
            self.logged = True
            if callable(self.logger):
                try:
                    self.logger(
                        f"Скан #{self.generation}: отменён новым запросом "
                        f"(стадия={self.stage})")
                except Exception:
                    pass
        return True


def stage_check(token, stage):
    return bool(token is not None and token.check(stage))
