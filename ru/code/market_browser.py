# -*- coding: utf-8 -*-
"""Окно «Маркет» (Alt+M): поиск, детали, график цен и свои ордера.

Стиль — единая дизайн-система оверлея (``ui_theme.py``, AlecaFrame): тёмные
панели, акцент #4aa8ff, бейджи sell/buy, тонкий скроллбар, только Unicode-
глифы (⛁ ◆ ⌕ ⟳ ✎ ×). Никаких картинок и новых зависимостей.

Сеть — всегда в фоновом потоке; GUI получает данные сигналами. Клиент
(``market_client``) сам держит TTL: каталог и ордера 60 с, статистика 5 мин,
пауза 3 rps. Без сети окно показывает понятное сообщение и не падает.

Модуль можно импортировать без Qt: UI-фабрика ``build_market_browser()``
импортирует PyQt5 лениво. Отдельные функции (``age_text``, ``chart_points``,
``chart_stats``, ``matches``) тестируются без GUI.
"""

import os
import time

import market_client as mc
import ui_theme as theme

try:                       # B2: общий список открытых окон приложения
    import login_mode
except Exception:          # старая сборка без login_mode.py
    login_mode = None


# ─────────────────────────────────────────────────────────────────────────
#  Мелкие функции (тестируются без Qt)
# ─────────────────────────────────────────────────────────────────────────
def age_text(seconds):
    """«обновлено N с назад»: секунды/минуты/часы."""
    try:
        sec = max(0, int(seconds))
    except Exception:
        return "только что"
    if sec < 5:
        return "только что"
    if sec < 60:
        return f"{sec} с"
    if sec < 3600:
        return f"{sec // 60} мин"
    if sec < 86400:
        hours, rem = divmod(sec, 3600)
        return f"{hours} ч {rem // 60:02d} мин" if rem // 60 else f"{hours} ч"
    days, rem = divmod(sec, 86400)
    return f"{days} д {rem // 3600} ч" if rem // 3600 else f"{days} д"


def chart_points(series, mode="48h"):
    """Точки графика из ``statistics_series``: ``48h`` или ``90d``."""
    series = series if isinstance(series, dict) else {}
    key = "90d" if str(mode).lower() in ("90d", "90", "90days") else "48h"
    points = series.get(key) or []
    return [dict(p) for p in points
            if isinstance(p, dict) and p.get("price") is not None]


def chart_stats(points):
    """Мин/макс/средняя цена и суммарный объём по точкам графика."""
    points = [p for p in (points or []) if isinstance(p, dict)
              and p.get("price") is not None]
    if not points:
        return {"min": None, "max": None, "avg": None, "volume": 0, "count": 0}
    prices = [float(p["price"]) for p in points]
    lows = [float(p["min"]) for p in points if p.get("min") is not None]
    highs = [float(p["max"]) for p in points if p.get("max") is not None]
    return {
        "min": min(lows) if lows else min(prices),
        "max": max(highs) if highs else max(prices),
        "avg": sum(prices) / len(prices),
        "volume": sum(int(p.get("volume") or 0) for p in points),
        "count": len(points),
    }


def matches(item, query):
    """Простой матч названия (ru/en/slug) — для подсветки/отладки."""
    query = str(query or "").strip().lower()
    if not query:
        return True
    text = " ".join(str(item.get(k) or "") for k in ("name", "name_en", "slug"))
    return all(part in text.lower() for part in query.split())


def price_text(value):
    """Цена платины с суффиксом: ``12 p`` (p — платина warframe.market)."""
    try:
        return f"{int(value)} p"
    except Exception:
        return "—"


def item_icon_label(item, size=24, cfg=None):
    """QLabel с иконкой предмета (лениво); None, если Qt/модуль недоступны."""
    try:
        from PyQt5.QtCore import Qt
        from PyQt5.QtWidgets import QLabel
        import item_icons
        cfg = cfg or {}
        base = cfg.get("data_dir") or os.environ.get("WF_DATA_DIR")
        provider = item_icons.get_provider(
            base, enabled=bool(cfg.get("show_item_icons", True)))
        label = QLabel()
        label.setFixedSize(int(size), int(size))
        label.setStyleSheet("background: transparent;")
        if not cfg.get("show_item_icons", True):
            label.hide()
            return label
        name = item.get("en_name") or item.get("name") or ""
        slug = str(item.get("slug") or "")
        if not slug and not item.get("en_name"):
            return label
        if slug:
            image = str(item.get("thumb") or item.get("icon") or "")
        else:
            image = provider.resolve(name=name, slug=slug,
                                     en_name=item.get("en_name") or "",
                                     game_ref=str(item.get("gameRef") or ""))
        pix = provider.placeholder(size)
        if pix is not None:
            label.setPixmap(pix)
        badge_text = item_icons.part_badge(slug, name)
        if badge_text:
            badge = QLabel(badge_text, label)
            badge.setFixedSize(16, 11)
            badge.setAlignment(Qt.AlignCenter)
            badge.setStyleSheet(
                "background:#f5c451; color:#17212e; font-size:6px;"
                " font-weight:700; border-radius:2px;")
            badge.move(int(size) - 17, 0)
            badge.show()
        if not image and not slug:
            return label
        if not (item.get("thumb") or item.get("icon")) and not provider.has_index():
            return label

        def done(_key, pixmap):
            try:
                if pixmap is not None and not pixmap.isNull():
                    label.setPixmap(pixmap.scaled(
                        int(size), int(size), Qt.KeepAspectRatio,
                        Qt.SmoothTransformation))
            except Exception:
                pass

        provider.request_item(slug or name or image, item, callback=done,
                              size=size)
        return label
    except Exception:
        return None


def order_type_of(order):
    return str((order or {}).get("type") or "sell").lower()


# ─────────────────────────────────────────────────────────────────────────
#  UI (PyQt5 импортируется лениво)
# ─────────────────────────────────────────────────────────────────────────
def build_market_browser():
    """Фабрика окна «Маркет»; возвращает класс ``MarketBrowser``."""
    from PyQt5.QtCore import QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
    from PyQt5.QtGui import (QBrush, QColor, QFont, QLinearGradient, QPainter,
                             QPen, QPolygonF)
    from PyQt5.QtWidgets import (QApplication, QButtonGroup, QDialog, QFrame,
                                 QHBoxLayout, QLabel, QLineEdit,
                                 QListWidget, QListWidgetItem,
                                 QPushButton, QScrollArea, QStackedWidget,
                                 QToolTip, QVBoxLayout, QWidget)

    def make_font(size, bold=False):
        font = QFont(theme.FONT)
        font.setPixelSize(int(size))
        font.setBold(bool(bold))
        return font

    def badge_label(text, background, foreground="#ffffff"):
        label = QLabel(str(text))
        label.setFont(make_font(9, True))
        label.setFixedHeight(18)
        label.setStyleSheet(theme.badge_qss(background, foreground))
        return label

    def clear_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    # ── тематические диалоги (B1/B2) ───────────────────────────────────
    class _ThemedDialog(QDialog):
        """База окон в палитре ui_theme.

        ``QMessageBox`` под общим оформлением приложения оставался без кнопок
        (виден только значок/текст), поэтому подтверждения и сообщения рисуем
        сами: у каждого окна явные кнопки со своим QSS и предсказуемые клавиши
        (Enter — подтвердить/ОК, Esc — отмена).
        """

        def __init__(self, parent, title="Маркет"):
            super().__init__(parent)
            self.setWindowTitle(str(title))
            self.setModal(True)
            self.setStyleSheet(theme.dialog_qss())
            self.setMinimumWidth(400)
            # B2: любое новое окно само попадает в общий список открытых
            if login_mode is not None:
                try:
                    login_mode.register_dialog(self)
                    self.finished.connect(
                        lambda _result=0: login_mode.unregister_dialog(self))
                except Exception:
                    pass

        def keyPressEvent(self, event):
            if event.key() == Qt.Key_Escape:
                self.reject()
                return
            super().keyPressEvent(event)

    class _ConfirmDialog(_ThemedDialog):
        """Подтверждение с двумя ВИДИМЫМИ кнопками: Enter — да, Esc — нет."""

        def __init__(self, parent, text, confirm_text="Да", cancel_text="Нет",
                     danger=False, title="Маркет"):
            super().__init__(parent, title=title)
            layout = QVBoxLayout(self)
            layout.setContentsMargins(16, 14, 16, 12)
            layout.setSpacing(12)
            question = QLabel(str(text))
            question.setWordWrap(True)
            question.setFont(make_font(11))
            layout.addWidget(question)
            buttons = QHBoxLayout()
            buttons.setSpacing(8)
            buttons.addStretch(1)
            self.btn_cancel = QPushButton(str(cancel_text))
            self.btn_cancel.setStyleSheet(theme.button_qss())
            self.btn_cancel.setCursor(Qt.PointingHandCursor)
            self.btn_cancel.setAutoDefault(False)
            self.btn_cancel.clicked.connect(self.reject)
            self.btn_confirm = QPushButton(str(confirm_text))
            self.btn_confirm.setStyleSheet(
                theme.button_qss(danger=bool(danger),
                                 primary=not bool(danger)))
            self.btn_confirm.setCursor(Qt.PointingHandCursor)
            self.btn_confirm.setDefault(True)      # Enter подтверждает
            self.btn_confirm.setAutoDefault(True)
            self.btn_confirm.clicked.connect(self.accept)
            buttons.addWidget(self.btn_cancel)
            buttons.addWidget(self.btn_confirm)
            layout.addLayout(buttons)

    class _MessageDialog(_ThemedDialog):
        """Сообщение с видимой кнопкой «ОК» (замена QMessageBox в маркете)."""

        def __init__(self, parent, text, warning=False, title="Маркет"):
            super().__init__(parent, title=title)
            layout = QVBoxLayout(self)
            layout.setContentsMargins(16, 14, 16, 12)
            layout.setSpacing(12)
            message = QLabel(str(text))
            message.setWordWrap(True)
            message.setFont(make_font(11))
            if warning:
                message.setStyleSheet(
                    f"color:{theme.PALETTE['warning']};"
                    " background: transparent;")
            layout.addWidget(message)
            buttons = QHBoxLayout()
            buttons.addStretch(1)
            self.btn_ok = QPushButton("ОК")
            self.btn_ok.setStyleSheet(theme.button_qss(primary=True))
            self.btn_ok.setCursor(Qt.PointingHandCursor)
            self.btn_ok.setDefault(True)
            self.btn_ok.clicked.connect(self.accept)
            buttons.addWidget(self.btn_ok)
            layout.addLayout(buttons)

    # ── график истории цен ─────────────────────────────────────────────
    class _PriceChart(QWidget):
        """QPainter-график: линия, градиентная заливка, объём, тултип."""

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setMinimumHeight(190)
            self.setMouseTracking(True)
            self.setAttribute(Qt.WA_StyledBackground, True)
            self.setStyleSheet(theme.chart_qss())
            self.series = {"48h": [], "90d": []}
            self.mode = "48h"
            self._hover = None

        def set_series(self, series):
            series = series if isinstance(series, dict) else {}
            self.series = {"48h": list(series.get("48h") or []),
                           "90d": list(series.get("90d") or [])}
            self._hover = None
            self.update()

        def set_mode(self, mode):
            self.mode = "90d" if str(mode).lower().startswith("90") else "48h"
            self._hover = None
            self.update()

        def points(self):
            return chart_points(self.series, self.mode)

        def stats(self):
            return chart_stats(self.points())

        def _plot_rect(self):
            rect = self.rect().adjusted(10, 10, -10, -10)
            return QRectF(rect.left(), rect.top(), rect.width(),
                          max(10, rect.height() * 0.78))

        def _nearest_index(self, x):
            points = self.points()
            if not points:
                return None
            plot = self._plot_rect()
            if len(points) == 1 or plot.width() <= 0:
                return 0
            rel = (float(x) - plot.left()) / plot.width()
            index = int(round(rel * (len(points) - 1)))
            return max(0, min(len(points) - 1, index))

        def paintEvent(self, event):
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing)
            rect = self.rect().adjusted(10, 10, -10, -10)
            plot = self._plot_rect()
            vol_top = rect.top() + rect.height() * 0.84

            grid_pen = QPen(QColor(theme.CHART_GRID))
            grid_pen.setWidth(1)
            painter.setPen(grid_pen)
            for i in range(5):
                y = rect.top() + rect.height() * i / 4.0
                painter.drawLine(int(rect.left()), int(y), int(rect.right()), int(y))
                x = rect.left() + rect.width() * i / 4.0
                painter.drawLine(int(x), int(rect.top()), int(x), int(rect.bottom()))

            points = self.points()
            if not points:
                painter.setPen(QColor(theme.PALETTE["muted"]))
                painter.setFont(make_font(10))
                painter.drawText(rect, Qt.AlignCenter,
                                 "нет данных по ценам" if self.mode else "")
                return
            stats = self.stats()
            low = stats["min"] if stats["min"] is not None else 1.0
            high = stats["max"] if stats["max"] is not None else low + 1
            span = max(1.0, float(high) - float(low))
            max_volume = max(1, max(int(p.get("volume") or 0) for p in points))
            count = len(points)

            def px(index):
                if count <= 1:
                    return plot.left() + plot.width() / 2.0
                return plot.left() + plot.width() * index / (count - 1.0)

            def py(price):
                return plot.bottom() - plot.height() * (float(price) - low) / span

            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(74, 168, 255, 52))
            bar_width = max(1.0, plot.width() / max(1, count) / 2.0)
            vol_height = max(2.0, rect.bottom() - vol_top)
            for index, point in enumerate(points):
                height = vol_height * int(point.get("volume") or 0) / max_volume
                painter.drawRect(QRectF(px(index) - bar_width / 2.0,
                                        rect.bottom() - height, bar_width, height))

            line = QPolygonF([QPointF(px(i), py(p["price"]))
                              for i, p in enumerate(points)])
            area = QPolygonF(line)
            area.append(QPointF(plot.right(), plot.bottom()))
            area.append(QPointF(plot.left(), plot.bottom()))
            gradient = QLinearGradient(0, plot.top(), 0, plot.bottom())
            gradient.setColorAt(0, QColor(74, 168, 255, 46))
            gradient.setColorAt(1, QColor(74, 168, 255, 0))
            painter.setBrush(QBrush(gradient))
            painter.setPen(Qt.NoPen)
            painter.drawPolygon(area)

            pen = QPen(QColor(theme.CHART_LINE))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawPolyline(line)

            painter.setPen(QColor(theme.PALETTE["muted"]))
            painter.setFont(make_font(9))
            painter.drawText(QRectF(plot.left(), plot.top() - 4, 140, 14),
                             Qt.AlignLeft, f"мин {stats['min']:g}p")
            painter.drawText(QRectF(plot.right() - 140, plot.top() - 4, 140, 14),
                             Qt.AlignRight, f"макс {stats['max']:g}p")
            painter.drawText(QRectF(plot.left(), plot.top() + 10, 160, 14),
                             Qt.AlignLeft, f"сред {stats['avg']:g}p")

            if self._hover is not None and 0 <= self._hover < count:
                point = points[self._hover]
                x = px(self._hover)
                y = py(point["price"])
                dash = QPen(QColor(theme.PALETTE["accent"]), 1, Qt.DashLine)
                painter.setPen(dash)
                painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(theme.PALETTE["accent"]))
                painter.drawEllipse(QPointF(x, y), 3, 3)

        def mouseMoveEvent(self, event):
            points = self.points()
            if not points:
                return
            index = self._nearest_index(event.x())
            if index != self._hover:
                self._hover = index
                self.update()
            if index is None:
                return
            point = points[index]
            stamp = ""
            try:
                stamp = time.strftime("%d.%m %H:%M", time.localtime(point["ts"]))
            except Exception:
                pass
            QToolTip.showText(
                event.globalPos(),
                f"{stamp}\nцена {point['price']:g}p · объём {point.get('volume', 0)}",
                self)

        def leaveEvent(self, event):
            self._hover = None
            self.update()
            super().leaveEvent(event)

    # ── строка результата поиска ───────────────────────────────────────
    class _ResultRow(QFrame):
        def __init__(self, item, parent=None, cfg=None):
            super().__init__(parent)
            self.item_data = dict(item)
            self.setObjectName("mRow")
            self.setFixedHeight(48)
            self.set_style(selected=False, zebra=False)
            lay = QHBoxLayout(self)
            lay.setContentsMargins(8, 4, 8, 4)
            lay.setSpacing(8)
            self.lbl_name = QLabel(str(item.get("name") or item.get("slug") or "?"))
            self.lbl_name.setFont(make_font(12))
            self.lbl_name.setStyleSheet(f"color:{theme.PALETTE['text']};")
            self.icon = item_icon_label(item, 40, cfg)
            if self.icon is not None:
                lay.addWidget(self.icon, 0, Qt.AlignVCenter)
            self.lbl_sell = QLabel("")
            self.lbl_sell.setFont(make_font(11, True))
            self.lbl_sell.setStyleSheet(f"color:{theme.PALETTE['platinum']};")
            self.lbl_sell.setToolTip(theme.currency_tooltip("platinum"))
            self.lbl_buy = QLabel("")
            self.lbl_buy.setFont(make_font(11, True))
            self.lbl_buy.setStyleSheet(f"color:{theme.PALETTE['platinum']};")
            self.lbl_buy.setToolTip(theme.currency_tooltip("platinum"))
            self.lbl_online = QLabel("")
            self.lbl_online.setFont(make_font(10))
            self.lbl_online.setStyleSheet(f"color:{theme.PALETTE['muted']};")
            lay.addWidget(self.lbl_name, 1)
            lay.addWidget(self.lbl_sell)
            lay.addWidget(self.lbl_buy)
            lay.addWidget(self.lbl_online)

        def set_style(self, selected=False, zebra=False):
            self.setStyleSheet(theme.row_qss(zebra=zebra, selected=selected))

        def set_prices(self, hint):
            hint = hint or {}
            sell = hint.get("sell")
            buy = hint.get("buy")
            self.lbl_sell.setText(price_text(sell) if sell else "")
            self.lbl_buy.setText(price_text(buy) if buy else "")
            online = hint.get("sell_online") or 0
            self.lbl_online.setText(f"{online} онлайн" if online else "")

    # ── основной класс окна ────────────────────────────────────────────
    class MarketBrowser(QWidget):
        """Окно маркета: вкладки «Поиск» и «Мои ордера»."""

        catalog_ready = pyqtSignal(list)
        catalog_failed = pyqtSignal(str)
        details_ready = pyqtSignal(dict)
        details_failed = pyqtSignal(str)
        orders_ready = pyqtSignal(list)
        orders_failed = pyqtSignal(str)
        prices_ready = pyqtSignal(dict)
        action_done = pyqtSignal(str)
        action_failed = pyqtSignal(str)

        def __init__(self, cfg, client, dialog_factory=None, parent=None,
                     embedded=False):
            if embedded:
                super().__init__(parent)
            else:
                super().__init__(parent, Qt.Window | Qt.FramelessWindowHint |
                                 Qt.WindowStaysOnTopHint)
            self.cfg = cfg
            self.client = client
            self.dialog_factory = dialog_factory
            self.embedded = bool(embedded)
            if not self.embedded:
                self.setAttribute(Qt.WA_TranslucentBackground)
                self.setWindowTitle("Маркет — warframe.market")
                self.resize(1000, 700)
                self.setMinimumSize(820, 560)

            self._catalog = []
            self._items_by_slug = {}
            self._items_by_id = {}
            self._selected_slug = ""
            self._details = {}
            self._orders = []
            self._busy_catalog = False
            self._busy_details = False
            self._busy_orders = False
            self._last_ts = 0.0
            self._sticky_status = ""
            self._current_tab = 0
            self._result_items = []
            self._result_rows = []
            self._suppress_select = False

            outer = QVBoxLayout(self)
            outer.setContentsMargins(0, 0, 0, 0)
            self.frame = QFrame()
            self.frame.setObjectName("win")
            if self.embedded:
                self.frame.setStyleSheet(
                    "QFrame#win { background: transparent; border: none; }"
                    "QLabel { background: transparent; }")
            else:
                self.frame.setStyleSheet(theme.window_qss("win", 8))
            lay = QVBoxLayout(self.frame)
            lay.setContentsMargins(0 if self.embedded else 14,
                                   0 if self.embedded else 10,
                                   0 if self.embedded else 14,
                                   0 if self.embedded else 10)
            lay.setSpacing(8)

            # шапка: заголовок, табы, закрыть
            head = QHBoxLayout()
            head.setSpacing(10)
            self.lbl_title = QLabel("Маркет")
            self.lbl_title.setFont(make_font(theme.size("window"), True))
            self.lbl_title.setStyleSheet(f"color:{theme.PALETTE['text']};")
            self.btn_tab_search = QPushButton("Поиск")
            self.btn_tab_orders = QPushButton("Мои ордера")
            for button in (self.btn_tab_search, self.btn_tab_orders):
                button.setCheckable(True)
                button.setCursor(Qt.PointingHandCursor)
                button.setStyleSheet(theme.tab_qss())
                button.setFont(make_font(12, True))
            self.btn_tab_search.setChecked(True)
            self.tab_group = QButtonGroup(self)
            self.tab_group.setExclusive(True)
            self.tab_group.addButton(self.btn_tab_search, 0)
            self.tab_group.addButton(self.btn_tab_orders, 1)
            self.btn_tab_search.clicked.connect(lambda: self.switch_tab(0))
            self.btn_tab_orders.clicked.connect(lambda: self.switch_tab(1))
            self.btn_close = QPushButton("×")
            self.btn_close.setFont(make_font(13))
            self.btn_close.setFixedSize(24, 22)
            self.btn_close.setStyleSheet(theme.button_qss(small=True))
            self.btn_close.setCursor(Qt.PointingHandCursor)
            self.btn_close.clicked.connect(self.hide)
            head.addWidget(self.lbl_title)
            head.addSpacing(10)
            head.addWidget(self.btn_tab_search)
            head.addWidget(self.btn_tab_orders)
            head.addStretch(1)
            head.addWidget(self.btn_close)

            # страницы
            self.stack = QStackedWidget()
            self.stack.addWidget(self._build_search_page())
            self.stack.addWidget(self._build_orders_page())

            # статус-строка
            bottom = QHBoxLayout()
            self.lbl_status = QLabel("готово")
            self.lbl_status.setFont(make_font(10))
            self.lbl_status.setStyleSheet(theme.status_qss())
            self.btn_refresh = QPushButton("⟳")
            self.btn_refresh.setFont(make_font(13))
            self.btn_refresh.setFixedSize(24, 22)
            self.btn_refresh.setStyleSheet(theme.button_qss(small=True))
            self.btn_refresh.setToolTip("Обновить (⟳)")
            self.btn_refresh.setCursor(Qt.PointingHandCursor)
            self.btn_refresh.clicked.connect(self.refresh)
            bottom.addWidget(self.lbl_status, 1)
            bottom.addWidget(self.btn_refresh)

            if not self.embedded:
                lay.addLayout(head)
            lay.addWidget(self.stack, 1)
            if not self.embedded:
                lay.addLayout(bottom)
            outer.addWidget(self.frame)

            self.catalog_ready.connect(self._apply_catalog)
            self.catalog_failed.connect(self._on_catalog_failed)
            self.orders_ready.connect(self._apply_orders)
            self.details_ready.connect(self._apply_details)
            self.details_failed.connect(self._on_details_failed)
            self.orders_failed.connect(self._on_orders_failed)
            self.prices_ready.connect(self._apply_prices)
            self.action_done.connect(self._on_action_done)
            self.action_failed.connect(self._on_action_failed)

            self._search_timer = QTimer(self)
            self._search_timer.setSingleShot(True)
            self._search_timer.setInterval(220)
            self._search_timer.timeout.connect(self._apply_filter)
            self.edit_search.textChanged.connect(lambda _text: self._search_timer.start())
            self.edit_search.returnPressed.connect(self._on_search_enter)
            self.edit_search.installEventFilter(self)

            self._status_timer = QTimer(self)
            self._status_timer.setInterval(1000)
            self._status_timer.timeout.connect(self._tick_status)
            self._status_timer.start()

        # ── построение вкладок ─────────────────────────────────────────
        def _build_search_page(self):
            page = QWidget()
            lay = QHBoxLayout(page)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(12)

            left = QFrame()
            left.setObjectName("panel")
            left.setStyleSheet(theme.panel_qss("panel", 6) + theme.scroll_qss())
            left.setFixedWidth(360)
            left_lay = QVBoxLayout(left)
            left_lay.setContentsMargins(10, 10, 10, 10)
            left_lay.setSpacing(8)
            search_row = QHBoxLayout()
            search_row.setSpacing(6)
            icon = QLabel("⌕")
            icon.setFont(make_font(15))
            icon.setStyleSheet(f"color:{theme.PALETTE['muted']};")
            self.edit_search = QLineEdit()
            self.edit_search.setPlaceholderText("Поиск предмета…")
            self.edit_search.setFont(make_font(12))
            self.edit_search.setStyleSheet(theme.input_qss())
            search_row.addWidget(icon)
            search_row.addWidget(self.edit_search, 1)
            self.list_results = QListWidget()
            self.list_results.setFont(make_font(12))
            self.list_results.setFocusPolicy(Qt.StrongFocus)
            self.list_results.setStyleSheet(
                "QListWidget { background: transparent; border: none; }"
                "QListWidget::item { background: transparent; border: none; }"
                + theme.scroll_qss())
            self.list_results.currentRowChanged.connect(self._on_row_changed)
            self.list_results.itemActivated.connect(lambda _item: self._on_search_enter())
            left_lay.addLayout(search_row)
            left_lay.addWidget(self.list_results, 1)

            right = QFrame()
            right.setObjectName("panel")
            right.setStyleSheet(theme.panel_qss("panel", 6) + theme.scroll_qss())
            right_lay = QVBoxLayout(right)
            right_lay.setContentsMargins(14, 12, 14, 12)
            right_lay.setSpacing(8)
            self.lbl_detail_name = QLabel("Выберите предмет")
            self.lbl_detail_name.setFont(make_font(theme.size("title"), True))
            self.lbl_detail_name.setStyleSheet(
                f"color:{theme.PALETTE['text']}; background: transparent;")
            self.lbl_detail_name.setWordWrap(True)
            self.badges_host = QWidget()
            self.badges_box = QHBoxLayout(self.badges_host)
            self.badges_box.setContentsMargins(0, 0, 0, 0)
            self.badges_box.setSpacing(6)
            self.lbl_set_parts = QLabel("")
            self.lbl_set_parts.setFont(make_font(10))
            self.lbl_set_parts.setStyleSheet(
                f"color:{theme.PALETTE['muted']};")
            self.lbl_set_parts.setWordWrap(True)

            offers = QHBoxLayout()
            offers.setSpacing(14)
            sell_col = QVBoxLayout()
            sell_col.setSpacing(4)
            sell_head = QLabel("ПРОДАЖА")
            sell_head.setFont(make_font(theme.size("section"), True))
            sell_head.setStyleSheet(theme.section_qss(theme.PALETTE["success"]))
            self.sell_box = QVBoxLayout()
            self.sell_box.setSpacing(2)
            sell_col.addWidget(sell_head)
            sell_col.addLayout(self.sell_box)
            buy_col = QVBoxLayout()
            buy_col.setSpacing(4)
            buy_head = QLabel("ПОКУПКА")
            buy_head.setFont(make_font(theme.size("section"), True))
            buy_head.setStyleSheet(theme.section_qss(theme.PALETTE["danger"]))
            self.buy_box = QVBoxLayout()
            self.buy_box.setSpacing(2)
            buy_col.addWidget(buy_head)
            buy_col.addLayout(self.buy_box)
            offers.addLayout(sell_col, 1)
            offers.addLayout(buy_col, 1)

            actions = QHBoxLayout()
            actions.setSpacing(8)
            self.btn_sell = QPushButton("Продать")
            self.btn_buy = QPushButton("Купить")
            self.btn_site = QPushButton("Открыть на сайте")
            self.btn_refresh_details = QPushButton("Обновить")
            self.btn_sell.setStyleSheet(theme.button_qss(primary=True))
            self.btn_buy.setStyleSheet(theme.button_qss(danger=True))
            for button in (self.btn_site, self.btn_refresh_details):
                button.setStyleSheet(theme.button_qss())
            self.btn_sell.clicked.connect(lambda: self._open_order_dialog("sell"))
            self.btn_buy.clicked.connect(lambda: self._open_order_dialog("buy"))
            self.btn_site.clicked.connect(self._open_site)
            self.btn_refresh_details.clicked.connect(lambda: self._load_details(force=True))
            for button in (self.btn_sell, self.btn_buy, self.btn_site,
                           self.btn_refresh_details):
                button.setFont(make_font(theme.size("button")))
                button.setCursor(Qt.PointingHandCursor)
                actions.addWidget(button)
            actions.addStretch(1)

            chart_head = QHBoxLayout()
            chart_head.setSpacing(8)
            chart_title = QLabel("ИСТОРИЯ ЦЕН")
            chart_title.setFont(make_font(theme.size("section"), True))
            chart_title.setStyleSheet(theme.section_qss())
            self.btn_48h = QPushButton("48 ч")
            self.btn_90d = QPushButton("90 д")
            self.chart_group = QButtonGroup(self)
            self.chart_group.setExclusive(True)
            for index, button in enumerate((self.btn_48h, self.btn_90d)):
                button.setCheckable(True)
                button.setFont(make_font(10, True))
                button.setStyleSheet(theme.button_qss(small=True))
                button.setCursor(Qt.PointingHandCursor)
                self.chart_group.addButton(button, index)
            self.btn_48h.setChecked(True)
            self.btn_48h.clicked.connect(lambda: self._set_chart_mode("48h"))
            self.btn_90d.clicked.connect(lambda: self._set_chart_mode("90d"))
            chart_head.addWidget(chart_title)
            chart_head.addStretch(1)
            chart_head.addWidget(self.btn_48h)
            chart_head.addWidget(self.btn_90d)

            self.chart = _PriceChart(self)
            right_lay.addWidget(self.lbl_detail_name)
            right_lay.addWidget(self.badges_host)
            right_lay.addWidget(self.lbl_set_parts)
            right_lay.addLayout(offers)
            right_lay.addLayout(actions)
            right_lay.addLayout(chart_head)
            right_lay.addWidget(self.chart, 1)

            lay.addWidget(left)
            lay.addWidget(right, 1)
            return page

        def _build_orders_page(self):
            page = QWidget()
            lay = QVBoxLayout(page)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(8)
            head = QHBoxLayout()
            title = QLabel("МОИ ОРДЕРА")
            title.setFont(make_font(theme.size("section"), True))
            title.setStyleSheet(theme.section_qss())
            self.btn_token = QPushButton("🔑 Токен")
            self.btn_hide_all = QPushButton("Скрыть все мои")
            self.btn_orders_refresh = QPushButton("⟳ Обновить")
            for button in (self.btn_token, self.btn_hide_all, self.btn_orders_refresh):
                button.setStyleSheet(theme.button_qss(small=True))
                button.setFont(make_font(theme.size("button")))
                button.setCursor(Qt.PointingHandCursor)
            self.btn_token.clicked.connect(self._open_token)
            self.btn_hide_all.clicked.connect(lambda: self._order_action("hide_all", {}))
            self.btn_orders_refresh.clicked.connect(lambda: self._load_orders(force=True))
            head.addWidget(title)
            head.addStretch(1)
            head.addWidget(self.btn_token)
            head.addWidget(self.btn_hide_all)
            head.addWidget(self.btn_orders_refresh)

            self.orders_scroll = QScrollArea()
            self.orders_scroll.setWidgetResizable(True)
            self.orders_scroll.setFrameShape(QFrame.NoFrame)
            self.orders_scroll.setStyleSheet(theme.scroll_qss())
            self.orders_body = QWidget()
            self.orders_body.setStyleSheet("background: transparent;")
            self.orders_box = QVBoxLayout(self.orders_body)
            self.orders_box.setContentsMargins(0, 0, 4, 0)
            self.orders_box.setSpacing(2)
            self.orders_scroll.setWidget(self.orders_body)
            self.lbl_orders_empty = QLabel("")
            self.lbl_orders_empty.setFont(make_font(11))
            self.lbl_orders_empty.setStyleSheet(f"color:{theme.PALETTE['muted']};")
            self.lbl_orders_empty.setWordWrap(True)
            lay.addLayout(head)
            lay.addWidget(self.lbl_orders_empty)
            lay.addWidget(self.orders_scroll, 1)
            return page

        # ── табы и позиция ─────────────────────────────────────────────
        def switch_tab(self, index):
            index = 1 if int(index) else 0
            self._current_tab = index
            self.stack.setCurrentIndex(index)
            self.btn_tab_search.setChecked(index == 0)
            self.btn_tab_orders.setChecked(index == 1)
            if index == 1:
                if not self._orders and not self._busy_orders:
                    self._load_orders()
            else:
                if not self._catalog and not self._busy_catalog:
                    self._load_catalog()

        def show_browser(self):
            if not self.embedded:
                self._center_on_screen()
                self.show()
                self.raise_()
                self.activateWindow()
            if not self._catalog and not self._busy_catalog:
                self._load_catalog()
            if self._current_tab == 1 and not self._orders and not self._busy_orders:
                self._load_orders()

        def status_text(self):
            """Строка статуса для шапки единого окна."""
            return self.lbl_status.text()

        def current_tab(self):
            return int(getattr(self, "_current_tab", 0))

        def toggle(self):
            if self.isVisible():
                self.hide()
            else:
                self.show_browser()

        def _center_on_screen(self):
            try:
                screen = QApplication.primaryScreen()
                area = screen.availableGeometry()
                width = min(self.width() or 1000, area.width() - 40)
                height = min(self.height() or 700, area.height() - 40)
                self.resize(width, height)
                self.move(area.left() + (area.width() - width) // 2,
                          area.top() + (area.height() - height) // 2)
            except Exception:
                pass

        def closeEvent(self, event):
            event.ignore()
            self.hide()

        def keyPressEvent(self, event):
            if event.key() == Qt.Key_Escape:
                self.hide()
                return
            super().keyPressEvent(event)

        def eventFilter(self, obj, event):
            if obj is self.edit_search and event.type() == event.KeyPress:
                if event.key() == Qt.Key_Down:
                    if self.list_results.count():
                        self.list_results.setFocus()
                        self.list_results.setCurrentRow(0)
                    return True
            return super().eventFilter(obj, event)

        # ── статус ─────────────────────────────────────────────────────
        def _set_status(self, text, stale=False, sticky=False):
            self.lbl_status.setText(str(text))
            self.lbl_status.setStyleSheet(theme.status_qss(stale))
            self._sticky_status = str(text) if sticky else ""

        def _tick_status(self):
            if self._busy_catalog or self._busy_details or self._busy_orders:
                return
            if self._sticky_status:
                return
            if self._last_ts:
                self._set_status(f"обновлено {age_text(time.time() - self._last_ts)} назад")

        def refresh(self):
            """⟳: обновить текущую вкладку (сеть в фоне)."""
            self._sticky_status = ""
            if self._current_tab == 1:
                self._load_orders(force=True)
            else:
                self._load_catalog(force=True)

        # ── каталог и поиск ────────────────────────────────────────────
        def _load_catalog(self, force=False):
            if self._busy_catalog:
                return
            self._busy_catalog = True
            self._set_status("загружаю список предметов…")

            def worker():
                try:
                    catalog = self.client.items_catalog(force=force)
                except Exception as exc:
                    try:
                        self.catalog_failed.emit(str(exc) or "нет сети")
                    except RuntimeError:
                        pass
                    return
                try:
                    self.catalog_ready.emit(catalog)
                except RuntimeError:
                    pass

            import threading
            threading.Thread(target=worker, daemon=True,
                             name="wf-market-catalog").start()

        def _on_catalog_failed(self, message):
            self._busy_catalog = False
            self._set_status(f"каталог недоступен: {message}", stale=True,
                             sticky=True)
            if not self._catalog:
                self._clear_details("Нет сети: список предметов недоступен.\n"
                                    "Кэш прошлого запуска появится здесь, когда "
                                    "соединение восстановится.")

        def _apply_catalog(self, catalog):
            self._busy_catalog = False
            self._catalog = list(catalog or [])
            self._items_by_slug = {item.get("slug"): item for item in self._catalog}
            self._items_by_id = {item.get("id"): item for item in self._catalog}
            self._apply_filter()
            if self._orders:
                # каталог пришёл позже ордеров — обновим названия предметов
                self._render_orders()
            self._last_ts = time.time()
            self._set_status(f"загружено предметов: {len(self._catalog)}")

        def _apply_filter(self):
            query = self.edit_search.text().strip()
            try:
                results = mc.filter_catalog(self._catalog, query, limit=60)
            except Exception:
                results = []
            self._fill_results(results)

        def _fill_results(self, items):
            self._suppress_select = True
            self.list_results.clear()
            self._result_items = list(items or [])
            self._result_rows = []
            for index, item in enumerate(self._result_items):
                list_item = QListWidgetItem()
                list_item.setSizeHint(QSize(10, 48))
                self.list_results.addItem(list_item)
                row = _ResultRow(item, self, self.cfg)
                row.set_style(zebra=(index % 2 == 1))
                self.list_results.setItemWidget(list_item, row)
                self._result_rows.append(row)
            self._suppress_select = False
            if self._result_items:
                self.list_results.setCurrentRow(0)
                self._load_result_prices(self._result_items)
            else:
                self._clear_details("Ничего не найдено — измените запрос")

        def _load_result_prices(self, items):
            """Подсказки цен для списка результатов (фоном, кэш клиента 60 с)."""
            batch = [item for item in (items or []) if item.get("slug")][:12]
            if not batch:
                return

            def worker():
                mapping = {}
                for item in batch:
                    slug = item.get("slug")
                    try:
                        mapping[slug] = self.client.price_hint(slug)
                    except Exception:
                        continue
                if mapping:
                    try:
                        self.prices_ready.emit(mapping)
                    except RuntimeError:
                        pass

            import threading
            threading.Thread(target=worker, daemon=True,
                             name="wf-market-prices").start()

        def _apply_prices(self, mapping):
            mapping = dict(mapping or {})
            for row in self._result_rows:
                slug = (row.item_data or {}).get("slug")
                if slug in mapping:
                    row.set_prices(mapping[slug])

        def _on_row_changed(self, row):
            for index, widget in enumerate(self._result_rows):
                widget.set_style(selected=(index == row), zebra=(index % 2 == 1))
            if self._suppress_select:
                return
            if 0 <= row < len(self._result_items):
                self._select_slug(self._result_items[row].get("slug"))

        def _on_search_enter(self):
            if self._result_items:
                row = max(0, self.list_results.currentRow())
                self.list_results.setCurrentRow(row)
                self._select_slug(self._result_items[row].get("slug"))

        # ── детали выбранного предмета ─────────────────────────────────
        def _select_slug(self, slug):
            slug = str(slug or "")
            if not slug:
                return
            self._selected_slug = slug
            self._load_details()

        def _load_details(self, force=False):
            slug = self._selected_slug
            if not slug or self._busy_details:
                return
            self._busy_details = True
            self._set_status("загружаю детали…")

            def worker():
                try:
                    meta = self.client.item(slug)
                except Exception as exc:
                    self.details_failed.emit(str(exc) or "нет сети")
                    return
                top = {"sell": [], "buy": []}
                series = {"48h": [], "90d": []}
                notes = []
                try:
                    top = self.client.top_orders(slug, force=force)
                except Exception as exc:
                    notes.append(str(exc))
                try:
                    series = self.client.statistics(slug, force=force)
                except Exception as exc:
                    notes.append(str(exc))
                self.details_ready.emit({"slug": slug, "meta": meta, "top": top,
                                         "series": series, "notes": notes})

            import threading
            threading.Thread(target=worker, daemon=True,
                             name="wf-market-details").start()

        def _on_details_failed(self, message):
            self._busy_details = False
            self._set_status(f"нет данных: {message}", stale=True, sticky=True)

        def _apply_details(self, payload):
            payload = dict(payload or {})
            self._busy_details = False
            meta = dict(payload.get("meta") or {})
            top = dict(payload.get("top") or {})
            series = dict(payload.get("series") or {})
            notes = list(payload.get("notes") or [])
            self._details = {"meta": meta, "top": top, "series": series}
            self.lbl_detail_name.setText(str(meta.get("name") or payload.get("slug") or ""))
            self._fill_badges(meta)
            parts = meta.get("set_parts") or []
            self.lbl_set_parts.setText(
                "Части сета: " + ", ".join(str(x) for x in parts) if parts else "")
            self._fill_offers(self.sell_box, top.get("sell") or [], "sell")
            self._fill_offers(self.buy_box, top.get("buy") or [], "buy")
            self.chart.set_series(series)
            self._set_chart_mode(self.chart.mode)
            self._last_ts = time.time()
            if notes:
                self._set_status("часть данных недоступна: " + "; ".join(notes),
                                 stale=True, sticky=True)
            else:
                self._set_status("обновлено только что")

        def _clear_details(self, message=""):
            self.lbl_detail_name.setText("Выберите предмет")
            clear_layout(self.badges_box)
            self.lbl_set_parts.setText(message or "")
            clear_layout(self.sell_box)
            clear_layout(self.buy_box)
            self.chart.set_series({"48h": [], "90d": []})

        def _fill_badges(self, meta):
            clear_layout(self.badges_box)
            palette = theme.PALETTE
            ducats = meta.get("ducats")
            if ducats is not None:
                self.badges_box.addWidget(
                    badge_label(f"◆ {int(ducats)} дк", palette["ducats"],
                                "#1a1400"))
            tax = meta.get("trading_tax")
            if tax is not None:
                self.badges_box.addWidget(
                    badge_label(f"налог {int(tax):,}".replace(",", " "),
                                palette["card"], palette["muted"]))
            rank = meta.get("req_mastery_rank")
            if rank:
                self.badges_box.addWidget(
                    badge_label(f"ранг {int(rank)}", palette["card"], palette["muted"]))
            for tag in (meta.get("tags") or [])[:4]:
                self.badges_box.addWidget(
                    badge_label(str(tag), palette["card"], palette["muted"]))
            if meta.get("tradable") is False:
                self.badges_box.addWidget(
                    badge_label("не торгуется", palette["danger"]))
            self.badges_box.addStretch(1)

        def _fill_offers(self, layout, orders, kind):
            clear_layout(layout)
            color = (theme.PALETTE["success"] if kind == "sell"
                     else theme.PALETTE["danger"])
            if not orders:
                empty = QLabel("нет ордеров")
                empty.setFont(make_font(10))
                empty.setStyleSheet(f"color:{theme.PALETTE['muted']};")
                layout.addWidget(empty)
                return
            for order in orders[:5]:
                row = QWidget()
                line = QHBoxLayout(row)
                line.setContentsMargins(0, 0, 0, 0)
                line.setSpacing(6)
                price = QLabel(price_text(order.get("platinum")))
                price.setFont(make_font(12, True))
                price.setStyleSheet(f"color:{theme.PALETTE['platinum']};")
                price.setToolTip(theme.currency_tooltip("platinum"))
                price.setToolTip(theme.currency_tooltip("platinum"))
                quantity = QLabel(f"×{order.get('quantity', '?')}")
                quantity.setFont(make_font(10))
                quantity.setStyleSheet(f"color:{theme.PALETTE['muted']};")
                status = QLabel(str((order.get("user") or {}).get("status") or ""))
                status.setFont(make_font(9))
                status.setStyleSheet(f"color:{theme.PALETTE['muted']};")
                line.addWidget(price)
                line.addWidget(quantity)
                line.addWidget(status)
                line.addStretch(1)
                layout.addWidget(row)

        def _set_chart_mode(self, mode):
            mode = "90d" if str(mode).lower().startswith("90") else "48h"
            self.chart.set_mode(mode)
            self.btn_48h.setChecked(mode == "48h")
            self.btn_90d.setChecked(mode == "90d")

        def _open_site(self):
            import webbrowser
            meta = (self._details or {}).get("meta") or {}
            slug = meta.get("slug") or self._selected_slug
            if not slug:
                return
            url = meta.get("url") or f"{mc.SITE}/items/{slug}"
            try:
                webbrowser.open(url)
            except Exception:
                pass

        def _track_dialog(self, dialog):
            """Учесть окно, чтобы скрытие приложения его закрыло (B2)."""
            if login_mode is not None:
                try:
                    login_mode.register_dialog(dialog)
                except Exception:
                    pass
            return dialog

        def _untrack_dialog(self, dialog):
            """Убрать окно из общего списка открытых (закрыли)."""
            if dialog is None or login_mode is None:
                return
            try:
                login_mode.unregister_dialog(dialog)
            except Exception:
                pass

        def _make_confirm_dialog(self, text, confirm_text="Да",
                                 cancel_text="Нет", danger=False):
            """Тематическое подтверждение с видимыми кнопками (B1)."""
            return _ConfirmDialog(self, text, confirm_text=confirm_text,
                                  cancel_text=cancel_text, danger=danger)

        def _confirm(self, text, confirm_text="Да", cancel_text="Нет",
                     danger=False):
            """Спросить подтверждение. Enter — да, Esc — нет."""
            dialog = self._make_confirm_dialog(
                text, confirm_text=confirm_text, cancel_text=cancel_text,
                danger=danger)
            self._track_dialog(dialog)
            try:
                return dialog.exec_() == 1     # QDialog.Accepted
            finally:
                self._untrack_dialog(dialog)

        def _show_message(self, text, warning=False):
            """Сообщение в палитре темы; кнопка «ОК» точно видна (B1)."""
            dialog = _MessageDialog(self, text, warning=warning)
            self._track_dialog(dialog)
            try:
                dialog.exec_()
            finally:
                self._untrack_dialog(dialog)

        def hide(self):
            """Скрыть окно и все открытые диалоги приложения (B2).

            Срабатывает на Esc/крестик/переключение окна маркета: на экране не
            должно оставаться ни подтверждения, ни формы ордера, ни окна входа.
            """
            if login_mode is not None:
                try:
                    if not login_mode.in_progress():
                        login_mode.close_active_dialogs()
                except Exception:
                    pass
            super().hide()

        def _open_order_dialog(self, order_type="sell", order=None,
                               prefill_price=None, price_note=""):
            if order is not None:
                # правка существующего ордера: предмет — строго из ордера,
                # выбор в «Поиске» больше не влияет
                item = self._order_dialog_item(order)
                if not item.get("slug"):
                    self._set_status(
                        "не удалось определить предмет ордера — "
                        "обновите каталог (⟳) и повторите",
                        stale=True, sticky=True)
                    return
            else:
                # создание из «Поиска»: прежняя логика (регресс-контракт)
                if not self._details or not self._selected_slug:
                    return
                item = dict(self._items_by_slug.get(self._selected_slug) or {})
                meta = (self._details or {}).get("meta") or {}
                item.setdefault("slug", self._selected_slug)
                item.setdefault("market_name", meta.get("name"))
                item.setdefault("name", meta.get("name"))
            if self.dialog_factory is None:
                self._show_message(
                    "Форма ордера недоступна (нет модуля диалогов).")
                return
            accepted = False
            dialog = None
            try:
                sell_cls, _token_cls, _save_fn = self.dialog_factory()
                dialog_options = {"order_type": order_type, "order": order}
                if prefill_price is not None or price_note:
                    # только для «перебили цену»: старые фейковые диалоги
                    # в тестах не обязаны знать про предзаполнение
                    dialog_options.update(prefill_price=prefill_price,
                                          price_note=price_note)
                dialog = self._track_dialog(
                    sell_cls(self.cfg, self.client, item, self,
                             **dialog_options))
                accepted = (dialog.exec_() == 1)    # QDialog.Accepted
            except Exception as exc:
                self._set_status(f"форма ордера не открылась: {exc}",
                                 stale=True, sticky=True)
            finally:
                self._untrack_dialog(dialog)
            if self._current_tab == 1 or order is not None:
                self._load_orders(force=True)
                if order is not None and accepted:
                    self._set_status("ордер обновлён — список перезагружен")

        # ── мои ордера ─────────────────────────────────────────────────
        def _load_orders(self, force=False):
            if self._busy_orders:
                return
            if not self.client.has_token():
                self._apply_orders([])
                self.lbl_orders_empty.setText(
                    "Токен warframe.market не задан.\n"
                    "Нажмите «🔑 Токен», чтобы вставить JWT из браузера "
                    "(DevTools → Network → Authorization: Bearer …).")
                self._set_status("мои ордера: нужен токен", stale=True, sticky=True)
                return
            self._busy_orders = True
            self._set_status("загружаю мои ордера…")

            def worker():
                try:
                    orders = self.client.my_orders()
                except Exception as exc:
                    self.orders_failed.emit(str(exc) or "нет сети")
                    return
                self.orders_ready.emit(orders)

            import threading
            threading.Thread(target=worker, daemon=True,
                             name="wf-market-my-orders").start()

        def _on_orders_failed(self, message):
            self._busy_orders = False
            self.lbl_orders_empty.setText(f"Не удалось загрузить ордера: {message}")
            self._set_status(f"мои ордера: {message}", stale=True, sticky=True)

        def _apply_orders(self, orders):
            self._busy_orders = False
            self._orders = [dict(x) for x in (orders or []) if isinstance(x, dict)]
            self._render_orders()
            self._last_ts = time.time()
            if self._orders:
                self._set_status(f"мои ордера: {len(self._orders)}")
            pending = getattr(self, "_pending_edit", None)
            if pending:
                self._pending_edit = None
                self._open_my_order_edit(pending)
            pending_select = getattr(self, "_pending_select", None)
            if pending_select:
                self._pending_select = None
                self.select_my_order(pending_select)

        def _render_orders(self):
            clear_layout(self.orders_box)
            if not self._orders:
                if self.client.has_token():
                    self.lbl_orders_empty.setText("Ордеров нет — создайте через «Продать».")
                self.orders_box.addStretch(1)
                return
            self.lbl_orders_empty.setText("")
            for index, order in enumerate(self._orders):
                self.orders_box.addWidget(self._make_order_row(order, index))
            self.orders_box.addStretch(1)

        def _order_item_name(self, order):
            embedded = (order.get("item")
                        if isinstance(order.get("item"), dict) else {})
            item_id = order.get("itemId") or embedded.get("id")
            item = self._items_by_id.get(item_id) or {}
            if item.get("name"):
                return item["name"]
            i18n = (embedded.get("i18n")
                    if isinstance(embedded.get("i18n"), dict) else {})
            ru = i18n.get("ru") if isinstance(i18n.get("ru"), dict) else {}
            return (ru.get("name") or embedded.get("name")
                    or order.get("itemId") or "предмет")

        def _order_dialog_item(self, order):
            """Предмет для формы правки — строго из ордера, не из «Поиска».

            Источник: ``order["itemId"]`` → каталог ``_items_by_id``; фолбэк —
            вложенный ``order["item"]`` (dict с ``id``/``slug``/``i18n``).
            В item НЕ кладутся rank/price/maxRank/bulk: их даёт сам ордер и
            ``client.item(slug)``, а ``item["rank"]`` перебил бы ``order["rank"]``.
            """
            order = dict(order or {})
            embedded = (order.get("item")
                        if isinstance(order.get("item"), dict) else {})
            item_id = order.get("itemId") or embedded.get("id") or ""
            item = dict(self._items_by_id.get(item_id) or {}) if item_id else {}
            if not item and embedded:               # вложенный item из ордера
                item = dict(embedded)
            name = self._order_item_name(order)
            item.setdefault("id", item_id or embedded.get("id"))
            item.setdefault("slug", embedded.get("slug") or "")
            item.setdefault("name", name)
            item.setdefault("market_name", item.get("name") or name)
            return item

        def _make_order_row(self, order, index):
            kind = order_type_of(order)
            color = (theme.PALETTE["success"] if kind == "sell"
                     else theme.PALETTE["danger"])
            card = QFrame()
            card.setObjectName("mRow")
            card.setProperty("order_id", order.get("id"))
            card.setProperty("order_kind", kind)
            card.setFixedHeight(48)
            card.setStyleSheet(theme.row_qss(zebra=(index % 2 == 1)))
            lay = QHBoxLayout(card)
            lay.setContentsMargins(8, 0, 8, 0)
            lay.setSpacing(8)
            lay.addWidget(badge_label("SELL" if kind == "sell" else "BUY",
                                      color, "#0d141c"))
            icon_item = self._order_dialog_item(order)
            icon = item_icon_label(icon_item, 40, self.cfg)
            if icon is not None:
                lay.addWidget(icon, 0, Qt.AlignVCenter)
            name = QLabel(str(self._order_item_name(order)))
            name.setFont(make_font(11))
            name.setStyleSheet(f"color:{theme.PALETTE['text']};")
            name.setMinimumWidth(220)
            price = QLabel(price_text(order.get("platinum")))
            price.setFont(make_font(12, True))
            price.setStyleSheet(f"color:{theme.PALETTE['platinum']};")
            quantity = QLabel(f"×{order.get('quantity', '?')}")
            quantity.setFont(make_font(10))
            quantity.setStyleSheet(f"color:{theme.PALETTE['muted']};")
            extras = []
            if order.get("rank") is not None:
                extras.append(f"ранг {order.get('rank')}")
            if order.get("perTrade") is not None:
                extras.append(f"лот {order.get('perTrade')}")
            extras.append("виден" if order.get("visible", True) else "скрыт")
            info = QLabel(" · ".join(extras))
            info.setFont(make_font(10))
            info.setStyleSheet(f"color:{theme.PALETTE['muted']};")
            lay.addWidget(name, 1)
            lay.addWidget(price)
            lay.addWidget(quantity)
            lay.addWidget(info)
            buttons = [
                ("✎", "Изменить", lambda: self._open_order_dialog(kind, order)),
                ("показать" if not order.get("visible", True) else "скрыть",
                 "Показать" if not order.get("visible", True) else "Скрыть",
                 lambda: self._order_action("toggle", order)),
                ("×", "Удалить", lambda: self._order_action("delete", order)),
                ("закрыть", "Закрыть ордер", lambda: self._order_action("close", order)),
                ("⟳", "Выставить заново", lambda: self._order_action("repost", order)),
            ]
            for text, tooltip, callback in buttons:
                button = QPushButton(text)
                button.setToolTip(tooltip)
                button.setFont(make_font(10))
                button.setStyleSheet(theme.button_qss(small=True))
                button.setCursor(Qt.PointingHandCursor)
                button.clicked.connect(callback)
                lay.addWidget(button)
            return card

        def _order_action(self, kind, order):
            order = dict(order or {})
            order_id = order.get("id")
            if kind != "hide_all" and not order_id:
                return
            if kind == "close":
                if not self._confirm("Закрыть ордер полностью?",
                                     confirm_text="Закрыть",
                                     cancel_text="Отмена"):
                    return
            elif kind == "delete":
                if not self._confirm("Удалить ордер без возможности отмены?",
                                     confirm_text="Удалить",
                                     cancel_text="Отмена", danger=True):
                    return
            elif kind == "hide_all":
                if not self._confirm("Скрыть все мои ордера на warframe.market?",
                                     confirm_text="Скрыть",
                                     cancel_text="Отмена"):
                    return
            self._set_status("выполняю…")

            def worker():
                try:
                    if kind == "toggle":
                        self.client.set_visible(order_id,
                                                not order.get("visible", True))
                        message = "видимость ордера изменена"
                    elif kind == "close":
                        self.client.close_order(order_id)
                        message = "ордер закрыт"
                    elif kind == "delete":
                        self.client.delete_order(order_id)
                        message = "ордер удалён"
                    elif kind == "repost":
                        result = self.client.repost_order(order_id)
                        message = ("ордер выставлен заново"
                                   if not (isinstance(result, dict)
                                           and result.get("dry_run"))
                                   else "проверка dry_run: ордер не отправлен")
                    elif kind == "hide_all":
                        self.client.hide_all()
                        message = "все ордера скрыты"
                    else:
                        raise ValueError(f"неизвестное действие: {kind}")
                except Exception as exc:
                    self.action_failed.emit(str(exc) or "ошибка действия")
                    return
                self.action_done.emit(message)

            import threading
            threading.Thread(target=worker, daemon=True,
                             name="wf-market-action").start()

        def _on_action_done(self, message):
            self._set_status(message)
            self._load_orders(force=True)

        def _on_action_failed(self, message):
            self._set_status(f"ошибка: {message}", stale=True, sticky=True)
            self._show_message(str(message), warning=True)

        def _open_token(self):
            if self.dialog_factory is None:
                return
            dialog = None
            try:
                _sell_cls, token_cls, save_fn = self.dialog_factory()
                dialog = self._track_dialog(
                    token_cls(self.cfg, self.client, self, save_fn=save_fn))
                dialog.exec_()
            except Exception as exc:
                self._set_status(f"окно токена не открылось: {exc}", stale=True,
                                 sticky=True)
                return
            finally:
                self._untrack_dialog(dialog)
            self._load_orders(force=True)

        # ── публичные помощники для тестов ─────────────────────────────
        def _find_my_order(self, order_id):
            order_id = str(order_id or "")
            if not order_id:
                return None
            for order in (self._orders or getattr(self, "_orders_cache", None)
                          or []):
                if str(order.get("id")) == order_id:
                    return dict(order)
            return None

        def _open_my_order_edit(self, request):
            """Открыть форму правки МОЕГО ордера (с предзаполненной ценой)."""
            request = dict(request or {})
            order = self._find_my_order(request.get("order_id"))
            if order is None:
                # ордер продан/удалён: карточка предмета + честное сообщение
                if request.get("item_slug") or request.get("item_name"):
                    self.open_item(request.get("item_slug") or "",
                                   request.get("item_name") or "")
                self._set_status(
                    "ордер не найден — возможно, уже продан",
                    stale=True, sticky=True)
                try:
                    self._show_message(
                        "ордер не найден — возможно, уже продан",
                        warning=True)
                except Exception:
                    pass
                return {"ok": False, "error": "order_not_found",
                        "order_id": request.get("order_id")}
            note = str(request.get("price_note") or "")
            new_price = request.get("new_price")
            my_price = request.get("my_price")
            if not note and isinstance(new_price, (int, float)) \
                    and isinstance(my_price, (int, float)):
                note = (f"ваша цена {int(my_price)}p → "
                        f"сейчас минимум {int(new_price)}p")
            try:
                self.switch_tab(1)        # подвкладка «Мои ордера»
            except Exception:
                pass
            self._open_order_dialog(
                order_type_of(order), order,
                prefill_price=(new_price if isinstance(new_price, (int, float))
                               else None),
                price_note=note)
            return {"ok": True, "opened": True,
                    "order_id": order.get("id"),
                    "new_price": new_price}

        def open_item(self, slug="", name=""):
            """Показать карточку предмета на вкладке «Поиск» (без сети)."""
            slug = str(slug or "")
            query = str(name or slug)
            try:
                self.switch_tab(0)
            except Exception:
                pass
            if query:
                try:
                    self.set_search_text(query)
                except Exception:
                    pass
            if slug:
                for index, item in enumerate(getattr(self, "_result_items", []) or []):
                    if str(item.get("slug")) == slug:
                        try:
                            self.select_result(index)
                            self._select_slug(slug)
                        except Exception:
                            pass
                        break
            return bool(getattr(self, "_result_items", None))

        def edit_my_order(self, order_id="", new_price=None, item_slug="",
                          item_name="", my_price=None, price_note=""):
            """Клик по «перебили цену»: карточка + правка моего ордера.

            Если ордера ещё не загружены — запоминает запрос, грузит список и
            откроет форму после ответа; если ордера нет — сообщение.
            """
            request = {"order_id": str(order_id or ""),
                       "new_price": new_price,
                       "my_price": my_price,
                       "item_slug": str(item_slug or ""),
                       "item_name": str(item_name or ""),
                       "price_note": str(price_note or "")}
            if not request["order_id"]:
                self.open_item(request["item_slug"], request["item_name"])
                return {"ok": False, "error": "no_order_id"}
            if not self.client.has_token():
                self._set_status("мои ордера: нужен токен", stale=True,
                                 sticky=True)
                self.open_item(request["item_slug"], request["item_name"])
                return {"ok": False, "error": "no_token"}
            order = self._find_my_order(request["order_id"])
            if order is not None:
                return self._open_my_order_edit(request)
            if not self._orders:
                self._pending_edit = request
                self._load_orders(force=True)
                return {"ok": True, "pending": True,
                        "order_id": request["order_id"]}
            return self._open_my_order_edit(request)

        def select_my_order(self, order_id):
            """Подсветить/проскроллить строку «Мои ордера» (для «куплен»)."""
            try:
                self.switch_tab(1)
            except Exception:
                pass
            if not self._orders:
                cached = getattr(self, "_orders_cache", None) or []
                if cached:
                    self._apply_orders(list(cached))
                else:
                    self._pending_select = str(order_id or "")
                    self._load_orders(force=True)
                    return True
            for row in self.order_rows():
                if str(row.property("order_id")) == str(order_id):
                    try:
                        row.setStyleSheet(
                            row.styleSheet()
                            + f"QFrame#mRow {{ border:2px solid "
                              f"{theme.PALETTE['accent']}; }}")
                        self.orders_scroll.ensureWidgetVisible(row, 0, 8)
                    except Exception:
                        pass
                    return True
            return False

        def result_count(self):
            return len(self._result_items)

        def set_search_text(self, text):
            self.edit_search.setText(str(text))
            self._apply_filter()

        def select_result(self, index):
            self.list_results.setCurrentRow(int(index))

        def order_rows(self):
            return [w for w in self.orders_body.findChildren(QFrame)
                    if w.objectName() == "mRow"]

    return MarketBrowser
