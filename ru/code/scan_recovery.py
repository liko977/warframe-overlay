# -*- coding: utf-8 -*-
"""1.1.0: диагностика/самовосстановление скана (t115)."""
import datetime
import os
import shutil
import threading
import time


def fail_dir(root):
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(str(root), f"fail_{stamp}")
    suffix = 1
    while os.path.isdir(path):
        path = os.path.join(str(root), f"fail_{stamp}_{suffix}")
        suffix += 1
    return path


def rotate(root, keep=10):
    try:
        dirs = [os.path.join(root, name) for name in os.listdir(root)
                if name.startswith("fail_")
                and os.path.isdir(os.path.join(root, name))]
        dirs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for old in dirs[int(keep):]:
            shutil.rmtree(old, ignore_errors=True)
    except Exception:
        pass


def _imwrite(path, image):
    """Запись изображения с поддержкой кириллицы (cv2.imwrite её не умеет).

    cv2.imwrite на не-ASCII пути молча возвращает False, поэтому пишем
    через ``cv2.imencode`` + ``numpy.ndarray.tofile``.
    """
    try:
        import cv2
    except Exception:
        return False
    if image is None:
        return False
    try:
        ext = os.path.splitext(path)[1] or ".png"
        ok, buf = cv2.imencode(ext, image)
        if not ok:
            return False
        buf.tofile(path)
        return True
    except Exception:
        try:
            return bool(cv2.imwrite(path, image))
        except Exception:
            return False


def dump_failure(root, frame=None, cards=None, meta=None, logger=None):
    """Сохранить screen.png/card*.png/cards.txt/meta.txt; вернуть путь."""
    path = fail_dir(root)
    try:
        os.makedirs(path, exist_ok=True)
        if frame is not None:
            _imwrite(os.path.join(path, "screen.png"), frame)
        for index, card in enumerate(cards or [], 1):
            crop = card.get("_crop")
            if crop is not None:
                _imwrite(os.path.join(path, f"card{index}.png"), crop)
        with open(os.path.join(path, "cards.txt"), "w",
                  encoding="utf-8") as handle:
            for index, card in enumerate(cards or [], 1):
                handle.write(
                    f"{index}: raw={card.get('_ocr_raw', '')!r} "
                    f"name={card.get('name', '')!r} "
                    f"score={float(card.get('score') or 0):.2f} "
                    f"bbox=({int(card.get('left') or 0)},"
                    f"{int(card.get('top') or 0)},"
                    f"{int(card.get('width') or 0)},"
                    f"{int(card.get('height') or 0)})\n")
        with open(os.path.join(path, "meta.txt"), "w",
                  encoding="utf-8") as handle:
            for key, value in (meta or {}).items():
                handle.write(f"{key}={value}\n")
        rotate(root, 10)
    except Exception as exc:
        if logger:
            logger(f"Скан: дамп не создан ({type(exc).__name__}: {exc})")
        return ""
    return path


class Recovery:
    """Счётчик нулей и однократный сброс кэшей с повтором."""

    def __init__(self):
        self.zero_streak = 0
        self.retry_done = False

    def note(self, matched, reset_fn=None, logger=None):
        if matched >= 2:
            self.zero_streak = 0
            return False
        self.zero_streak += 1
        if self.zero_streak >= 2 and not self.retry_done:
            self.retry_done = True
            try:
                if callable(reset_fn):
                    reset_fn()
            except Exception:
                pass
            if logger:
                logger("Скан: деградация — кэши сброшены, тема переопределена")
            return True
        return False


class ScanWatchdog:
    """Снимает busy, если скан идёт дольше timeout."""

    def __init__(self, timeout=30.0, on_timeout=None):
        self.timeout = float(timeout)
        self.on_timeout = on_timeout
        self._timer = None
        self.fired = False

    def start(self):
        self.cancel()
        self.fired = False
        self._timer = threading.Timer(self.timeout, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self):
        self.fired = True
        if callable(self.on_timeout):
            try:
                self.on_timeout()
            except Exception:
                pass

    def cancel(self):
        if self._timer is not None:
            try:
                self._timer.cancel()
            except Exception:
                pass
            self._timer = None


def hide_windows(app, logger=None):
    """Скрыть видимые top-level окна; вернуть список для restore()."""
    hidden = []
    if app is None:
        return hidden
    for widget in app.topLevelWidgets():
        try:
            if widget.isVisible():
                widget.hide()
                hidden.append(widget)
        except Exception:
            pass
    try:
        app.processEvents()
    except Exception:
        pass
    if hidden and logger:
        logger("Скан: окна скрыты на время снимка")
    return hidden


def restore_windows(hidden):
    for widget in hidden or []:
        try:
            widget.show()
        except Exception:
            pass


def scan_result_line(number, matched, total, dump_path=""):
    if matched >= 2:
        return f"Скан #{number}: ок, {matched}/{total}"
    suffix = f" (дамп: {dump_path})" if dump_path else ""
    return f"Скан #{number}: провал {matched}/{total}{suffix}"
