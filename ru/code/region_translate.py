# -*- coding: utf-8 -*-
"""Alt+R (t200): выделение области → OCR (RU+EN) → перевод → карточка + история.

Шторка выделения переиспользуется из ``region_price`` (тот же компонент, что
Alt+D). Перевод идёт в фоновом потоке: UI не блокируется.
"""
import threading
import time

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

try:
    import ui_theme as theme
except Exception:                        # pragma: no cover
    theme = None

import region_price
import translate_history as history_mod

TEXT_LIMIT = 500
MIN_BOX = (80, 60)


class RegionTranslateCard(region_price.DragResizeMixin, QWidget):
    """Короткая карточка у выделения: оригинал (≤3 строки) + перевод."""

    translation_ready = pyqtSignal(str, str)   # (текст, статус) из потока

    def __init__(self, cfg=None, on_save=None, log=print):
        super().__init__()
        self.cfg = dict(cfg or {})
        self._log = log
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._init_drag(on_save=on_save)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.frame = QFrame()
        self.frame.setObjectName("translateCard")
        self.frame.setStyleSheet(
            "QFrame#translateCard{background:rgba(13,17,23,245);"
            "border:1px solid #24344a;border-radius:6px;}"
            "QLabel{border:none;background:transparent;color:#e8eef7;"
            "font-family:'Segoe UI',Arial,sans-serif;}")
        lay = QVBoxLayout(self.frame)
        lay.setContentsMargins(9, 6, 9, 6)
        lay.setSpacing(2)
        self.lbl_original = QLabel("")
        self.lbl_original.setStyleSheet("font-size:11px;color:#93A1B5;")
        self.lbl_original.setWordWrap(True)
        self.lbl_translation = QLabel("…")
        self.lbl_translation.setStyleSheet("font-size:13px;font-weight:600;")
        self.lbl_translation.setWordWrap(True)
        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("font-size:9px;color:#93A1B5;")
        lay.addWidget(self.lbl_original)
        lay.addWidget(self.lbl_translation)
        lay.addWidget(self.lbl_status)
        outer.addWidget(self.frame)
        self.translation_ready.connect(self.set_translation)
        self.resize(360, 96)

    def set_original(self, text):
        lines = str(text or "").splitlines()[:3]
        self.lbl_original.setText("\n".join(lines))
        return True

    def set_translation(self, text, status=""):
        self.lbl_translation.setText(str(text or "—"))
        self.lbl_status.setText(str(status or ""))
        self.adjustSize()
        return True

    def restart_ttl(self):
        ttl = float(self.cfg.get("region_translate_ttl", 6.0) or 0)
        if ttl <= 0:
            return False
        from PyQt5.QtCore import QTimer
        QTimer.singleShot(int(ttl * 1000), self.close)
        return True


class RegionTranslate:
    """Контроллер Alt+R: шторка → захват → OCR → перевод → карточка/история."""

    def __init__(self, cfg, ocr=None, ocr_getter=None, log=print,
                 capture_fn=None, translate_fn=None, history=None,
                 on_saved=None, toast=None):
        self.cfg = dict(cfg or {})
        self.ocr = ocr
        self.ocr_getter = ocr_getter
        self.log = log
        self.capture_fn = capture_fn
        self.translate_fn = translate_fn
        self.history = history if history is not None else (
            history_mod.TranslateHistory(self.cfg, logger=log))
        self.on_saved = on_saved
        self.toast = toast
        self.card = None
        self.selector = None
        self.last_box = None
        self.last_error = ""

    # ── шторка и захват ───────────────────────────────────────────────
    def open(self):
        if self.card is not None:
            self.card.close()
            self.card = None
        self.selector = region_price.RegionSelector()
        self.selector.selected.connect(self._on_selected)
        self.selector.canceled.connect(self._canceled)
        self.selector.showFullScreen()
        self.selector.activateWindow()
        self.selector.raise_()
        return self.selector

    def _canceled(self):
        self.log("Alt+R: отменено")

    def _on_selected(self, rect):
        return self.process(rect.x(), rect.y(), rect.width(), rect.height())

    def capture(self, x, y, w, h):
        if self.capture_fn is not None:
            return self.capture_fn(x, y, w, h)
        import mss
        import numpy as np
        import cv2
        with mss.mss() as sct:
            raw = np.array(sct.grab({"left": x, "top": y, "width": w,
                                     "height": h}))
        return cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)

    def recognize(self, frame):
        """OCR области → текст построчно (RU+EN), ≤500 символов."""
        if frame is None or getattr(frame, "size", 0) == 0:
            return ""
        engine = (self.ocr_getter() if self.ocr_getter is not None
                  else self.ocr)
        if engine is None:
            self.log("Alt+R: OCR недоступен (None)")
            return ""
        try:
            import cv2
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            proc = cv2.resize(gray, None, fx=2.0, fy=2.0,
                              interpolation=cv2.INTER_CUBIC)
            result = engine.read_proc(proc) or []
        except Exception as exc:
            self.log(f"Alt+R: OCR недоступен ({type(exc).__name__})")
            return ""
        lines = {}
        for item in result:
            try:
                bbox, text = item[0], str(item[1])
            except (TypeError, IndexError):
                continue
            key = int(sum(point[1] for point in bbox) / max(1, len(bbox)) / 12)
            lines.setdefault(key, []).append(text)
        text = "\n".join(" ".join(parts) for _key, parts in sorted(lines.items()))
        return text.strip()[:TEXT_LIMIT]

    # ── перевод и показ ──────────────────────────────────────────────
    def process(self, x, y, w, h):
        """Область → карточка (сразу) → перевод в фоне → история."""
        if w < MIN_BOX[0] or h < MIN_BOX[1]:
            self.last_error = "слишком маленькая область"
            self.log(f"Alt+R: {self.last_error} ({w}x{h})")
            if callable(self.toast):
                try:
                    self.toast(self.last_error)
                except Exception:
                    pass
            return None
        self.last_box = (x, y, w, h)
        frame = self.capture(x, y, w, h)
        text = self.recognize(frame)
        self.card = RegionTranslateCard(self.cfg, log=self.log)
        self.card.set_original(text or "(текст не распознан)")
        self._place_card(x, y, w, h)
        self.card.set_translation("…", "перевожу…")
        self.card.show()
        if text:
            threading.Thread(target=self._translate_and_show,
                             args=(text,), daemon=True,
                             name="wf-region-translate").start()
        else:
            self.card.set_translation("—", "текст не распознан")
        return self.card

    def _translate_and_show(self, text):
        translated, info = self.translate_text(text)
        error = str((info or {}).get("error") or "")
        skipped = "ru" in str((info or {}).get("skipped") or "")
        if skipped:
            stored = "уже русский"
        elif error:
            stored = ""
        else:
            stored = translated
        record = self.history.add(text, stored, source="region",
                                  error=error, utc=time.time())
        if record is not None and skipped:
            record["skipped"] = True
        if error:
            ui_text, status = f"ошибка: {error}", "перевод недоступен"
        elif skipped:
            ui_text, status = text, "уже русский"
        else:
            ui_text, status = (translated or "—"), ""
        self.emit_result(ui_text, status)
        if callable(self.on_saved):
            try:
                self.on_saved(record)
            except Exception:
                pass
        return ui_text

    def emit_result(self, text, status=""):
        """Отдать перевод в GUI-поток сигналом (карточка может быть уже закрыта)."""
        card = self.card
        if card is None:
            return False
        try:
            card.translation_ready.emit(str(text or ""), str(status or ""))
            return True
        except RuntimeError:
            return False

    def translate_text(self, text):
        """``translator.translate`` (или мок) → (перевод, info)."""
        if callable(self.translate_fn):
            result = self.translate_fn(text)
            if isinstance(result, tuple):
                return result
            return str(result or ""), {"ok": True}
        import translator
        return translator.translate(text, target="ru", cfg=self.cfg)

    def _place_card(self, x, y, w, h):
        if self.card is None:
            return
        width = max(280, min(460, w))
        self.card.resize(width, self.card.height())
        screen = self.card.screen().geometry() if self.card.screen() else None
        left = int(x)
        top = int(y + h + 8)
        if screen is not None:
            left = max(screen.left() + 4,
                       min(left, screen.right() - width - 4))
            if top + self.card.height() > screen.bottom():
                top = int(y - self.card.height() - 8)
        self.card.move(left, top)
        self.card.restart_ttl()
        return self.card
