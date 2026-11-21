# -*- coding: utf-8 -*-
"""Периодическая проверка обновлений (t34, 0.6.1).

Проверка идёт в фоне, не блокирует GUI. Первый запуск — через
``FIRST_DELAY`` секунд после старта, затем каждые
``update_check_interval_min`` минут (по умолчанию 180). Работает только при
``update_check=true`` и непустом ``update_url``.

Одно уведомление на версию: последняя показанная версия хранится в
``cfg["last_update_notified"]`` и сохраняется через ``save_fn``. Ошибки сети
только логируются, без тоста.
"""
import threading
import time

FIRST_DELAY = 150.0                     # 2.5 минуты после старта
DEFAULT_INTERVAL_MIN = 180
MIN_INTERVAL_MIN = 1
CHECK_TIMEOUT = 10.0


def _as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off",
                                             "нет")
    return bool(value)


def _as_float(value, default):
    try:
        return float(value)
    except Exception:
        return float(default)


class UpdateWatcher:
    """Фоновый таймер проверки обновлений с защитой от спама."""

    def __init__(self, cfg=None, check_fn=None, on_available=None,
                 save_fn=None, logger=None, first_delay=FIRST_DELAY,
                 timer_factory=None, check_timeout=CHECK_TIMEOUT,
                 clock=None):
        self.cfg = cfg if isinstance(cfg, dict) else {}
        self.check_fn = check_fn
        self.on_available = on_available
        self.save_fn = save_fn
        self.logger = logger
        self.first_delay = max(1.0, _as_float(first_delay, FIRST_DELAY))
        self.check_timeout = max(0.1, _as_float(check_timeout,
                                                CHECK_TIMEOUT))
        self.timer_factory = timer_factory or self._default_timer
        self.clock = clock or time.monotonic
        self._stopped = True
        self._timer = None
        self._lock = threading.Lock()
        self.check_count = 0

    # ── настройки ─────────────────────────────────────────────────────
    def enabled(self):
        return (_as_bool(self.cfg.get("update_check"), False)
                and bool(str(self.cfg.get("update_url") or "").strip()))

    def interval_seconds(self):
        minutes = _as_float(self.cfg.get("update_check_interval_min"),
                            DEFAULT_INTERVAL_MIN)
        minutes = max(MIN_INTERVAL_MIN, minutes)
        return minutes * 60.0

    def _log(self, message):
        if callable(self.logger):
            try:
                self.logger(str(message))
            except Exception:
                pass

    # ── таймер ────────────────────────────────────────────────────────
    def _default_timer(self, interval, callback):
        timer = threading.Timer(float(interval), callback)
        timer.daemon = True
        return timer

    def _schedule(self, seconds):
        with self._lock:
            if self._stopped:
                return False
            timer = self.timer_factory(max(1.0, float(seconds)),
                                       self._tick)
            self._timer = timer
        try:
            timer.start()
            return True
        except Exception as exc:
            self._log(f"обновления: таймер не запущен "
                      f"({type(exc).__name__})")
            return False

    def start(self):
        if not self.enabled():
            self._log("обновления: автопроверка выключена")
            return False
        with self._lock:
            if not self._stopped:
                return False
            self._stopped = False
        self._log(f"обновления: автопроверка через {int(self.first_delay)} с, "
                  f"интервал {int(self.interval_seconds() // 60)} мин")
        return self._schedule(self.first_delay)

    def stop(self):
        with self._lock:
            self._stopped = True
            timer, self._timer = self._timer, None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
        return True

    # ── проверка ──────────────────────────────────────────────────────
    def _run_check(self):
        if not callable(self.check_fn):
            return {}
        box = {"result": None, "error": None}

        def worker():
            try:
                box["result"] = self.check_fn()
            except Exception as exc:       # сеть/манифест — только лог
                box["error"] = exc

        thread = threading.Thread(target=worker, daemon=True,
                                  name="wf-update-check")
        thread.start()
        thread.join(self.check_timeout)
        if thread.is_alive():
            self._log("обновления: проверка не уложилась в "
                      f"{self.check_timeout:.0f} с — пропуск")
            return {}
        if box["error"] is not None:
            self._log("обновления: проверка не удалась "
                      f"({type(box['error']).__name__}) — тихо")
            return {}
        result = box["result"]
        return dict(result) if isinstance(result, dict) else {}

    def check_once(self):
        """Один проход (публичный — для тестов/ручной проверки)."""
        if not self.enabled():
            return {}
        self.check_count += 1
        result = self._run_check()
        if not result.get("available"):
            return result
        version = str(result.get("version") or "").strip()
        if not version:
            return result
        if str(self.cfg.get("last_update_notified") or "") == version:
            self._log(f"обновления: версия {version} уже показана")
            return result
        if callable(self.on_available):
            try:
                self.on_available(dict(result))
            except Exception as exc:
                self._log("обновления: уведомление не показано "
                          f"({type(exc).__name__})")
                return result
        self.cfg["last_update_notified"] = version
        if callable(self.save_fn):
            try:
                self.save_fn({"last_update_notified": version})
            except Exception:
                pass
        return result

    def _tick(self):
        with self._lock:
            if self._stopped:
                return
        try:
            self.check_once()
        except Exception as exc:
            self._log(f"обновления: тик упал ({type(exc).__name__})")
        finally:
            if not self._stopped:
                self._schedule(self.interval_seconds())


__all__ = ["UpdateWatcher", "FIRST_DELAY", "DEFAULT_INTERVAL_MIN",
           "MIN_INTERVAL_MIN", "CHECK_TIMEOUT"]
