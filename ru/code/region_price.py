# -*- coding: utf-8 -*-
"""Alt+D: цена по выделенной области + перетаскиваемая карточка (t158)."""
import os

from PyQt5.QtCore import Qt, QPoint, QRect, QSize, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen
from PyQt5.QtWidgets import (QApplication, QCheckBox, QFrame, QHBoxLayout,
                             QLabel, QPushButton, QSpinBox, QVBoxLayout,
                             QWidget)

CARD_FONT = "font-family:'Segoe UI', Arial, sans-serif;"
MIN_BOX = QSize(180, 60)
MAX_BOX = QSize(520, 160)


class DragResizeMixin:
    """Тащить за тело, тянуть за бок/угол (hit-test 6 px)."""
    EDGE = 6

    def _init_drag(self, on_save=None, min_size=MIN_BOX, max_size=MAX_BOX):
        self._on_save = on_save
        self._min_size = QSize(min_size)
        self._max_size = QSize(max_size)
        self._drag_mode = ""
        self._drag_origin = QPoint()
        self._drag_geom = None
        self.setMouseTracking(True)

    def _hit(self, pos):
        x, y = pos.x(), pos.y()
        w, h = self.width(), self.height()
        left, right = x <= self.EDGE, x >= w - self.EDGE
        top, bottom = y <= self.EDGE, y >= h - self.EDGE
        if left and top:
            return "tl"
        if right and top:
            return "tr"
        if left and bottom:
            return "bl"
        if right and bottom:
            return "br"
        if left:
            return "l"
        if right:
            return "r"
        if top:
            return "t"
        if bottom:
            return "b"
        return "move"

    def _cursor_for(self, mode):
        if mode in ("l", "r"):
            return Qt.SizeHorCursor
        if mode in ("t", "b"):
            return Qt.SizeVerCursor
        if mode in ("tl", "br"):
            return Qt.SizeFDiagCursor
        if mode in ("tr", "bl"):
            return Qt.SizeBDiagCursor
        return Qt.SizeAllCursor

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_mode = self._hit(event.pos())
            self._drag_origin = event.globalPos()
            self._drag_geom = self.geometry()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not self._drag_mode:
            self.setCursor(self._cursor_for(self._hit(event.pos())))
            super().mouseMoveEvent(event)
            return
        delta = event.globalPos() - self._drag_origin
        geo = QRect(self._drag_geom)
        mode = self._drag_mode
        if mode == "move":
            geo.moveTopLeft(self._drag_geom.topLeft() + delta)
        else:
            if "l" in mode:
                geo.setLeft(geo.left() + delta.x())
            if "r" in mode:
                geo.setRight(geo.right() + delta.x())
            if "t" in mode:
                geo.setTop(geo.top() + delta.y())
            if "b" in mode:
                geo.setBottom(geo.bottom() + delta.y())
        geo.setWidth(max(self._min_size.width(),
                         min(self._max_size.width(), geo.width())))
        geo.setHeight(max(self._min_size.height(),
                          min(self._max_size.height(), geo.height())))
        self.setGeometry(geo)
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._drag_mode:
            self._drag_mode = ""
            if self._on_save:
                g = self.geometry()
                self._on_save([g.x(), g.y(), g.width(), g.height()])
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def set_click_through(self, enabled):
        visible = self.isVisible()
        self.setAttribute(Qt.WA_TransparentForMouseEvents, bool(enabled))
        self.setWindowFlag(Qt.WindowTransparentForInput, bool(enabled))
        if visible:
            self.show()


class RegionSelector(QWidget):
    """Полноэкранная шторка: протяжка мышью → QRect."""
    selected = pyqtSignal(QRect)
    canceled = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.CrossCursor)
        self.setGeometry(QApplication.desktop().geometry())
        self._start = None
        self._current = None

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 90))
        if self._start and self._current:
            box = QRect(self._start, self._current).normalized()
            p.setPen(QPen(QColor("#4aa8ff"), 1))
            p.drawRect(box)
            p.drawText(box.topLeft() + QPoint(4, -4),
                       f"{box.width()}×{box.height()}")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._start = self._current = event.pos()
            self.update()

    def mouseMoveEvent(self, event):
        if self._start:
            self._current = event.pos()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._start:
            box = QRect(self._start, event.pos()).normalized()
            self.hide()
            self.selected.emit(box)
            self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.hide()
            self.canceled.emit()
            self.close()


class PriceCard(DragResizeMixin, QWidget):
    closed = pyqtSignal()

    def __init__(self, cfg=None, on_save=None, log=print, client=None,
                 on_open_orders=None, toast=None):
        super().__init__()
        self.cfg = dict(cfg or {})
        self._log = log
        self._client = client
        self._on_open_orders = on_open_orders
        self._toast = toast
        self._item_id = ""
        self._slug = ""
        self._name = ""
        self._price = None
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._init_drag(on_save=on_save)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.frame = QFrame()
        self.frame.setObjectName("card")
        self.frame.setStyleSheet(
            "QFrame#card{background:rgba(13,17,23,245);"
            "border:1px solid #24344a;border-radius:6px;}"
            f"QLabel{{border:none;background:transparent;color:#e8eef7;"
            f"{CARD_FONT}}}")
        lay = QVBoxLayout(self.frame)
        lay.setContentsMargins(9, 6, 9, 6)
        self.lbl_name = QLabel("")
        self.lbl_name.setStyleSheet("font-size:13px;font-weight:600;")
        self.lbl_price = QLabel("—")
        self.lbl_price.setStyleSheet("font-size:18px;font-weight:700;")
        self.lbl_info = QLabel("")
        self.lbl_info.setStyleSheet("color:#93A1B5;font-size:10px;")
        lay.addWidget(self.lbl_name)
        lay.addWidget(self.lbl_price)
        lay.addWidget(self.lbl_info)
        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color:#93A1B5;font-size:10px;")
        lay.addWidget(self.lbl_status)
        actions = QHBoxLayout()
        self.btn_sell = QPushButton("Выставить на маркет")
        self.btn_open = QPushButton("Открыть на маркете")
        for button in (self.btn_sell, self.btn_open):
            button.setFixedHeight(24)
            button.setStyleSheet(
                "QPushButton{background:#17212e;border:1px solid #24344a;"
                "border-radius:0;color:#e6edf6;font-size:10px;}")
            button.setCursor(Qt.PointingHandCursor)
        self.btn_open.clicked.connect(self._open_market)
        self.btn_sell.clicked.connect(self._show_confirm)
        actions.addWidget(self.btn_sell)
        actions.addWidget(self.btn_open)
        lay.addLayout(actions)
        self.confirm = QWidget()
        confirm_lay = QHBoxLayout(self.confirm)
        confirm_lay.setContentsMargins(0, 2, 0, 0)
        self.spin_price = QSpinBox()
        self.spin_price.setRange(1, 100000)
        self.spin_qty = QSpinBox()
        self.spin_qty.setRange(1, 999)
        self.chk_visible = QCheckBox("видим")
        self.chk_visible.setChecked(True)
        self.btn_submit = QPushButton("Выставить")
        self.btn_cancel = QPushButton("Отмена")
        self.btn_submit.clicked.connect(self._submit_order)
        self.btn_cancel.clicked.connect(self._hide_confirm)
        for widget in (QLabel("цена"), self.spin_price, QLabel("количество"),
                       self.spin_qty, self.chk_visible, self.btn_submit,
                       self.btn_cancel):
            confirm_lay.addWidget(widget)
        self.confirm.setVisible(False)
        lay.addWidget(self.confirm)
        outer.addWidget(self.frame)
        self.resize(260, 86)
        self.set_click_through(bool(self.cfg.get("plates_click_through")))
        self._ttl = max(0.0, float(self.cfg.get("region_card_ttl", 5.0) or 0))
        self._ttl_timer = QTimer(self)
        self._ttl_timer.setSingleShot(True)
        self._ttl_timer.timeout.connect(self._hide_by_ttl)

    def showEvent(self, event):
        super().showEvent(event)
        self.restart_ttl()

    def restart_ttl(self):
        self._ttl_timer.stop()
        if self._ttl > 0:
            self._ttl_timer.start(int(self._ttl * 1000))

    def _hide_by_ttl(self):
        if not self.isVisible():
            return
        self.lbl_name.clear()
        self.lbl_price.clear()
        self.lbl_info.clear()
        self.hide()
        try:
            self._log(f"Alt+D: карточка скрыта по таймауту "
                      f"({self._ttl:.1f} с)")
        except Exception:
            pass

    def set_data(self, name, price, info="", score=None, item_id="",
                 slug=""):
        self._name = str(name or "")
        self._price = price
        self._item_id = str(item_id or "")
        self._slug = str(slug or "")
        title = name or "НЕ РАСПОЗНАНО"
        if score is not None and score < 0.6:
            title = f"НЕ РАСПОЗНАНО: {name or '—'}"
        self.lbl_name.setText(title)
        self.lbl_price.setText(
            "—" if price is None else f"{int(price)} платина")
        self.lbl_info.setText(info)
        self._refresh_sell()
        self.adjustSize()
        self.resize(max(180, min(520, self.width())),
                    max(60, min(160, self.height())))

    def _refresh_sell(self):
        reason = ""
        if not self._name or "НЕ РАСПОЗНАНО" in self._name:
            reason = "сначала распознайте предмет"
        elif self._price is None:
            reason = "цена неизвестна"
        elif not self._item_id:
            reason = "нет item_id предмета"
        elif self._client is None or not getattr(
                self._client, "has_token", lambda: False)():
            reason = "нужен токен warframe.market"
        self.btn_sell.setEnabled(not reason)
        if reason:
            self.btn_sell.setToolTip(reason)
            self.btn_sell.setText("Выставить на маркет")
        else:
            self.btn_sell.setToolTip("Выставить SELL-ордер")
            self.btn_sell.setText(
                f"Выставить на маркет — {int(self._price)} платина")

    def _show_confirm(self):
        if not self.btn_sell.isEnabled():
            return
        self.spin_price.setValue(max(1, int(self._price)))
        self.spin_qty.setValue(max(
            1, int(self.cfg.get("market_default_quantity", 1) or 1)))
        self.chk_visible.setChecked(True)
        self.confirm.setVisible(True)
        self.adjustSize()

    def _hide_confirm(self):
        self.confirm.setVisible(False)
        self.adjustSize()

    def _open_market(self):
        if self._on_open_orders is not None:
            self._on_open_orders()
            return
        import warframe
        warframe.open_market_page(self._slug or self._name)

    def _submit_order(self):
        client = self._client
        if client is None:
            return False
        price = self.spin_price.value()
        qty = self.spin_qty.value()
        try:
            valid = client.validate_order(price, qty)
        except Exception as exc:
            valid = {"ok": False, "error": type(exc).__name__}
        if isinstance(valid, dict) and valid.get("ok") is False:
            self.lbl_status.setText(str(valid.get("error") or "ошибка"))
            return False
        try:
            order = client.create_order(
                self._item_id, price, qty, visible=self.chk_visible.isChecked())
        except Exception as exc:
            self.lbl_status.setText(f"ошибка: {type(exc).__name__}")
            self._log(f"Alt+D: выставить — ошибка {exc}")
            return False
        order = order if isinstance(order, dict) else {}
        order_id = order.get("id") or order.get("order_id") or "?"
        self.lbl_status.setText("выставлено")
        self.btn_sell.setText("Открыть ордера")
        try:
            self.btn_sell.clicked.disconnect()
        except Exception:
            pass
        self.btn_sell.clicked.connect(self._open_market)
        self.btn_sell.setEnabled(True)
        self.confirm.setVisible(False)
        if self._toast:
            self._toast(f"Ордер выставлен: {self._name} — "
                        f"{price} платина ×{qty}")
        self._log(f"Alt+D: выставить {self._slug} — {price} p ×{qty} "
                  f"→ ордер {order_id}")
        return True

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)


class RegionPrice:
    """Контроллер Alt+D: выделение → OCR → цена → PriceCard."""
    def __init__(self, cfg, api, ocr=None, log=print, capture_fn=None,
                 parent=None, ocr_getter=None, client=None,
                 on_open_orders=None, toast=None):
        self.cfg = dict(cfg or {})
        self.api = api
        self.ocr = ocr
        self.ocr_getter = ocr_getter
        self.client = client
        self.on_open_orders = on_open_orders
        self.toast = toast
        self.log = log
        self.capture_fn = capture_fn
        self.card = None
        self.selector = None
        self.last_box = None

    def open(self):
        if self.card is not None:
            self.card.close()
            self.card = None
        self.selector = RegionSelector()
        self.selector.selected.connect(self._on_selected)
        self.selector.canceled.connect(self._canceled)
        self.selector.showFullScreen()
        self.selector.activateWindow()
        self.selector.raise_()

    def _canceled(self):
        self.log("Alt+D: отменено")

    def _on_selected(self, rect):
        self.process(rect.x(), rect.y(), rect.width(), rect.height())

    def process(self, x, y, w, h):
        if w < 80 or h < 60:
            self.log("Alt+D: слишком маленькая область")
            return None
        self.last_box = (x, y, w, h)
        self.log(f"Alt+D: область={w}x{h}")
        frame = self.capture(x, y, w, h)
        name, score, ready = self.recognize(frame)
        self.log(f"Alt+D: ocr={'ready' if ready else 'warming'}, "
                 f"область={w}x{h}, имя={name!r}, score={score:.2f}")
        self.card = PriceCard(self.cfg, on_save=self._save_box,
                              log=self.log, client=self.client,
                              on_open_orders=self.on_open_orders,
                              toast=self.toast)
        if not ready:
            self.card.lbl_name.setText("OCR готовится…")
            self.card.lbl_price.setText("—")
            self.card.lbl_info.setText("попробуйте через пару секунд")
            self._place_card(x, y, w, h)
            self.card.show()
            return self.card
        info = self.api.lookup(name) if name else {}
        slug = str(info.get("slug") or "")
        item_id = str(info.get("id") or info.get("item_id") or "")
        if not item_id and slug:
            item_id = self._cache_item_id(slug)
        price = info.get("price")
        parts = []
        if info.get("ducats"):
            parts.append(f"дукаты {info['ducats']}")
        if info.get("sell_online"):
            parts.append(f"онлайн {info['sell_online']}")
        if info.get("set_price"):
            parts.append(f"комплект {info['set_price']} платина")
        self.card.set_data(name, price, " · ".join(parts), score,
                           item_id=item_id, slug=slug)
        self._place_card(x, y, w, h)
        self.card.show()
        self.log(f"Alt+D: область={w}x{h}, имя={name!r}, score={score:.2f}, "
                 f"цена={price}")
        return self.card

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

    def _read(self, gray):
        import cv2
        proc = cv2.resize(gray, None, fx=2.0, fy=2.0,
                          interpolation=cv2.INTER_CUBIC)
        return self._ocr.read_proc(proc)

    def recognize(self, frame):
        import cv2
        import card_text
        self._ocr = (self.ocr_getter() if self.ocr_getter is not None
                     else self.ocr)
        if self._ocr is None:
            self.log("Alt+D: OCR недоступен (None)")
            return "", 0.0, False
        if frame is None or getattr(frame, "size", 0) == 0:
            return "", 0.0, True
        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            res = self._read(gray) or []
            if not res:
                top = gray[:max(1, gray.shape[0] // 2), :]
                res = self._read(top) or []
        except Exception as exc:
            self.log(f"Alt+D: OCR недоступен ({type(exc).__name__})")
            return "", 0.0, True
        words = []
        for bbox, text, _conf in res:
            ys = [p[1] for p in bbox]
            xs = [p[0] for p in bbox]
            words.append((sum(ys) / len(ys), sum(xs) / len(xs), str(text)))
        words.sort()
        name = card_text.clean_card_name(" ".join(w[2] for w in words))
        score = 0.0
        try:
            score = float((self.api.match(name) or {}).get("score") or 0.0)
        except Exception:
            pass
        return name, score, True

    def _cache_item_id(self, slug):
        try:
            import json
            path = os.path.join(self.cfg.get("data_dir") or "", 
                                "items_cache_ru.json")
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            for item in (data.get("items") or {}).values():
                if isinstance(item, dict) and item.get("slug") == slug:
                    return str(item.get("id") or "")
        except Exception:
            pass
        return ""

    def _place_card(self, x, y, w, h):
        screen = QApplication.desktop().availableGeometry()
        cw, ch = self.card.width(), self.card.height()
        cx, cy = x + w + 8, y
        if cx + cw > screen.right():
            cx = max(screen.left(), x - cw - 8)
        if cy + ch > screen.bottom():
            cy = max(screen.top(), y + h - ch)
        self.card.move(cx, cy)

    def _save_box(self, box):
        boxes = dict(self.cfg.get("plate_boxes") or {})
        boxes["price_card"] = box
        self.cfg["plate_boxes"] = boxes
        try:
            import warframe
            warframe._write_config(self.cfg, warframe.CONFIG_PATH)
        except Exception:
            pass
