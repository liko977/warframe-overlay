# -*- coding: utf-8 -*-
"""Единое окно «VoidFrameRU» с вкладками (стиль AlecaFrame).

Одна кнопка (F8) открывает одно окно; внутри — вкладки «Маркет»,
«Мои ордера», «События», «Дроп», «Чат». Редактор «Выбрано»
из окна убран (плашки Alt+Q и Alt+1…4 остаются отдельными). Содержимое — уже
существующие панели:
редактор списка (F8-редактор), маркет-браузер (t6) и панель событий (t5);
они создаются один раз и не пересоздаются ни при переключении вкладок,
ни при смене режима окна.

Особенности:
* кастомная шапка: тянуть можно за любое свободное место шапки
  (openHand/closedHand), кнопки и поля внутри шапки перетаскивание не
  начинают; двойной клик по шапке больше не разворачивает окно;
  окно не выходит за пределы рабочего стола текущего монитора;
* режимы окна: normal и fullscreen (кнопка «⛶», F11). Esc в полноэкранном
  режиме выходит из него, в обычном — скрывает окно. Кнопки «—» (скрыть, как
  F9) и «×» (закрыть и вернуть фокус игре) — справа, в стиле ui_theme;
* ресайз за невидимый грип 12×12 в правом нижнем углу, минимум 900×600,
  максимум — availableGeometry; в fullscreen ресайз заблокирован;
* позиция/размер/последняя вкладка сохраняются в config.json
  (``window_geometry``, ``window_size``, ``last_tab``) — только для
  нормального состояния; при старте окно открывается нормальным с
  сохранённой геометрией, невалидная позиция центрируется;
* Ctrl+1..5 и клик переключают вкладки; статус маркета (чип токена) и
  «обновлено N с назад» + ⟳ для активной вкладки;
* чип «🔑» открывает окно входа на warframe.market (встроенный браузер,
  см. AUTH_SPEC.md и market_auth.py); если окно входа недоступно — прежний
  диалог ручной вставки токена. После успешного входа чип зелёный, а вкладка
  «Мои ордера» перезагружается в фоне.
* BUG-1: после любого завершения входа (успех/ошибка/отмена/таймаут) окно
  гарантированно кликабельно: ensure_interactive() снимает disabled/прозрачность
  для мыши/остаточный grab и нативные WS_EX_TRANSPARENT/WS_EX_NOACTIVATE;
  закрытие любого диалога тоже возвращает кликабельность.
* BUG-2: чип токена проверяет его фоном через GET /v2/me (Bearer) и различает
  состояния: нет токена (серый), проверяю…, токен есть (зелёный, только 200),
  ⚠ токен просрочен (жёлтый, 401; клик открывает окно входа).

Модуль не импортирует warframe/events/market: все зависимости передаются
извне (см. ``build_overlay_window``), поэтому его легко тестировать.
"""

import time

try:                      # модуль всегда лежит рядом, в сборке — hiddenimports
    import login_mode
except Exception:         # pragma: no cover - без него прячем только своё окно
    login_mode = None

import ui_theme as theme
try:
    import branding
except Exception:                       # pragma: no cover — старая сборка
    branding = None



def _ui_error(context, exc=None, text="не удалось — подробности в app.log"):
    """1.0.0: в статус — только человеческий текст, детали — в app.log."""
    try:
        import ui_safe
        return ui_safe.human_error(context, exc, fallback=text)
    except Exception:
        return str(text)


TAB_TITLES = ("Маркет", "Мои ордера", "События")
DEFAULT_WIDTH = 1180
DEFAULT_HEIGHT = 780
MIN_WIDTH = 900          # минимальный размер окна (требование пользователя)
MIN_HEIGHT = 600
RESIZE_GRIP = 12         # невидимый грип ресайза в правом нижнем углу

# ── чип токена (BUG-2): зелёный «токен есть» — только при 200 от GET /v2/me ──
TOKEN_STATE_NONE = "none"          # токена нет — серый
TOKEN_STATE_CHECKING = "checking"  # идёт фоновая проверка — серый
TOKEN_STATE_OK = "ok"              # 200 — зелёный
TOKEN_STATE_EXPIRED = "expired"    # 401 — жёлтый «просрочен»
TOKEN_STATE_UNKNOWN = "unknown"    # сеть/прочее — жёлтый «не проверен»

TOKEN_CHIP_TEXT = {
    TOKEN_STATE_NONE: "🔑 нет токена",
    TOKEN_STATE_CHECKING: "🔑 проверяю…",
    TOKEN_STATE_OK: "🔑 токен есть",
    TOKEN_STATE_EXPIRED: "⚠ токен просрочен",
    TOKEN_STATE_UNKNOWN: "⚠ токен не проверен",
}
TOKEN_CHIP_COLOR = {
    TOKEN_STATE_NONE: "muted",
    TOKEN_STATE_CHECKING: "muted",
    TOKEN_STATE_OK: "success",
    TOKEN_STATE_EXPIRED: "warning",
    TOKEN_STATE_UNKNOWN: "warning",
}
TOKEN_CHIP_TOOLTIP = {
    TOKEN_STATE_NONE: "Токен warframe.market не задан — нажмите, чтобы войти",
    TOKEN_STATE_CHECKING: "Проверяю токен через GET /v2/me…",
    TOKEN_STATE_OK: "Токен действителен (GET /v2/me → 200)",
    TOKEN_STATE_EXPIRED: ("Токен просрочен или не принят (401). "
                          "Нажмите, чтобы войти заново"),
    TOKEN_STATE_UNKNOWN: ("Не удалось проверить токен (нет связи?) — "
                          "нажмите, чтобы войти или проверить снова"),
}


def build_overlay_window():
    """Фабрика класса единого окна (PyQt5 импортируется лениво)."""
    from PyQt5.QtCore import QPoint, QRect, QSize, Qt, QTimer, pyqtSignal
    from PyQt5.QtGui import (QColor, QFont, QGuiApplication, QKeySequence,
                             QPainter)
    from PyQt5.QtWidgets import (QAbstractButton, QAbstractSlider,
                                 QAbstractSpinBox, QButtonGroup, QComboBox,
                                 QDialog, QFrame, QHBoxLayout, QLabel,
                                 QLineEdit, QMenu, QProgressBar, QPushButton,
                                 QShortcut, QSizePolicy, QStackedWidget,
                                 QVBoxLayout, QWidget)

    #: Виджеты в шапке, которые сами обрабатывают мышь: с них drag не стартует.
    INTERACTIVE = (QAbstractButton, QLineEdit, QComboBox, QAbstractSpinBox,
                   QAbstractSlider, QProgressBar)

    def make_font(size, bold=False):
        font = QFont(theme.FONT)
        font.setPixelSize(int(size))
        font.setBold(bool(bold))
        return font

    def _has_topmost(widget):
        """Есть ли у окна флаг «всегда сверху» (через login_mode или напрямую)."""
        if login_mode is not None:
            return login_mode.has_topmost(widget)
        try:
            return bool(widget.windowFlags() & Qt.WindowStaysOnTopHint)
        except Exception:
            return False

    def _as_pair(value):
        try:
            if isinstance(value, (list, tuple)) and len(value) == 2:
                return int(value[0]), int(value[1])
        except Exception:
            pass
        return None

    class _ElidedTitle(QLabel):
        """Заголовок, который сокращается с «…», а не обрезается краем."""

        def __init__(self, text="", parent=None):
            super().__init__(text, parent)
            self._full_text = str(text)
            self.setMinimumWidth(120)

        def set_title(self, text):
            self._full_text = str(text)
            self._update_elide()

        def _update_elide(self):
            width = max(0, self.contentsRect().width())
            metrics = self.fontMetrics()
            text = metrics.elidedText(self._full_text, Qt.ElideRight, width)
            if text != self.text():
                super().setText(text)
            self.setToolTip(self._full_text)

        def resizeEvent(self, event):
            super().resizeEvent(event)
            self._update_elide()

        def showEvent(self, event):
            super().showEvent(event)
            self._update_elide()

        def text(self):
            return self._full_text

    class _DragHeader(QFrame):
        """Шапка окна: только перетаскивание (без двойного клика).

        Тянуть можно за любое свободное место шапки. Кнопки и поля ввода
        внутри шапки перетаскивание не начинают — они обрабатывают мышь сами.
        """

        def __init__(self, window):
            super().__init__()
            self.window = window
            self.setObjectName("ovHeader")
            self.setCursor(Qt.OpenHandCursor)
            self._offset = None
            self._moved = False

        def _interactive_at(self, pos):
            """Под курсором кнопка/поле ввода → перетаскивание не начинаем."""
            child = self.childAt(pos)
            while child is not None and child is not self:
                if isinstance(child, INTERACTIVE):
                    return True
                child = child.parentWidget()
            return False

        def mousePressEvent(self, event):
            if event.button() == Qt.LeftButton:
                if self._interactive_at(event.pos()):
                    # клик по кнопке/полю — пусть обработает сам виджет
                    event.ignore()
                    return
                offset = self.window.begin_drag(event.globalPos())
                if offset is None:
                    event.ignore()
                    return
                self._offset = offset
                self._moved = False
                self.setCursor(Qt.ClosedHandCursor)
                event.accept()
                return
            super().mousePressEvent(event)

        def mouseMoveEvent(self, event):
            # Qt5 event.globalPos() уже в логических (DPI-масштабированных)
            # координатах, как и availableGeometry() — пересчёт не нужен.
            if self._offset is not None and event.buttons() & Qt.LeftButton:
                self.window.drag_to(event.globalPos() - self._offset)
                self._moved = True
                event.accept()
                return
            if self._offset is None:
                self.setCursor(Qt.OpenHandCursor)
            super().mouseMoveEvent(event)

        def mouseReleaseEvent(self, event):
            if event.button() == Qt.LeftButton and self._offset is not None:
                self._offset = None
                self.setCursor(Qt.OpenHandCursor)
                if self._moved:
                    self.window.save_geometry()
                event.accept()
                return
            super().mouseReleaseEvent(event)

        def mouseDoubleClickEvent(self, event):
            # Скрытая функция «развернуть» убрана вместе с кнопкой.
            event.ignore()

    class _ResizeGrip(QWidget):
        """Невидимый грип 12×12 в правом нижнем углу: ресайз нормального окна."""

        def __init__(self, window):
            super().__init__(window)
            self.window = window
            self.setFixedSize(RESIZE_GRIP, RESIZE_GRIP)
            self.setCursor(Qt.SizeFDiagCursor)
            self.setToolTip("Потяните, чтобы изменить размер окна")
            self._start = None

        def mousePressEvent(self, event):
            if event.button() == Qt.LeftButton:
                self._start = (QPoint(event.globalPos()), self.window.size())
                event.accept()
                return
            super().mousePressEvent(event)

        def mouseMoveEvent(self, event):
            if self._start is not None and event.buttons() & Qt.LeftButton:
                start_pos, start_size = self._start
                self.window.resize_by_delta(event.globalPos() - start_pos,
                                            start_size)
                event.accept()
                return
            super().mouseMoveEvent(event)

        def mouseReleaseEvent(self, event):
            if event.button() == Qt.LeftButton and self._start is not None:
                self._start = None
                self.window.save_geometry()
                event.accept()
                return
            super().mouseReleaseEvent(event)

    class OverlayWindow(QWidget):
        """Одно окно с четырьмя вкладками и кастомной шапкой."""

        #: (состояние, сообщение) проверки токена из фонового потока (BUG-2)
        token_checked = pyqtSignal(str, str)

        def __init__(self, cfg, editor, market, events,
                     status_providers=None, refresh_actions=None,
                     token_factory=None, save_values=None,
                     market_client=None, on_open=None, on_close=None,
                     hotkey_aliases=True, auth_factory=None,
                     drops_widget=None, on_settings=None,
                     notifications_widget=None, chat_widget=None,
                     on_status_set=None, on_status_auto=None,
                     on_status_profile=None, on_icons_cancel=None):
            super().__init__(None, Qt.Window | Qt.FramelessWindowHint |
                             Qt.WindowStaysOnTopHint)
            self.cfg = cfg
            # editor оставлен параметром для старого API, но вкладки/стека
            # «Выбрано» больше нет.
            self.market_widget = market
            self.events_widget = events
            self.drops_widget = drops_widget
            self.notifications_widget = notifications_widget
            self.chat_widget = chat_widget
            #: названия вкладок: Маркет · Мои ордера · События + опциональные
            self.titles = list(TAB_TITLES)
            if drops_widget is not None:
                self.titles.append("Дроп")
            # «Уведомления» больше не вкладка: центр открывается колокольчиком
            self._notifications_index = None
            self._notifications_dialog = None
            self._chat_index = None
            if chat_widget is not None:
                self._chat_index = len(self.titles)
                self.titles.append("Чат")

            self.market_client = market_client
            self.token_factory = token_factory
            # auth_factory(parent) -> QDialog — окно входа через браузер
            # (market_auth.build_auth_dialog); None — прежняя ручная вставка.
            self.auth_factory = auth_factory
            self.save_values = save_values or (lambda mapping: True)
            self.on_open = on_open
            self.on_close = on_close
            self.on_settings = on_settings
            self.on_status_set = on_status_set
            self.on_status_auto = on_status_auto
            self.on_status_profile = on_status_profile
            # 1.0.0: кнопка «Отмена» у прогресса загрузки пакета иконок
            self.on_icons_cancel = on_icons_cancel
            self._icons_progress = {"done": 0, "total": 0, "text": ""}
            self._status_auto = False
            self.hotkey_aliases = bool(hotkey_aliases)
            self.status_providers = list(status_providers or [])
            self.refresh_actions = list(refresh_actions or [])
            if notifications_widget is not None:
                try:
                    notifications_widget.unread_changed.connect(
                        self.set_unread_badge)
                except Exception:
                    pass
            if chat_widget is not None:
                self.status_providers.append(
                    lambda: str(getattr(self.chat_widget, "lbl_status").text()
                                or "чат"))
                self.refresh_actions.append(None)
            self._active_tab = self._initial_tab()
            # ── режим окна: normal | maximized | fullscreen ─────────────
            self._mode = "normal"
            self._normal_geom = None     # геометрия нормального состояния
            self._grip = None
            self._geometry_restored = False
            # ── режим входа на warframe.market (браузер не перекрываем) ──
            self._login_mode = False
            self._login_snapshot = None
            self._login_was_visible = False
            self._login_tab = 0
            self._login_geometry = None
            self._login_topmost = True
            #: открытое окно входа (браузерный вход не использует exec_)
            self._login_dialog = None
            # ── чип токена: реальная проверка GET /v2/me (BUG-2) ────────
            self._token_state = TOKEN_STATE_NONE
            self._token_checking = False
            # (нужен ли сброс modality окна входа — ставит сам диалог)
            self.setAttribute(Qt.WA_TranslucentBackground)
            self.app_version = str(getattr(branding, "APP_VERSION", "")
                                   if branding is not None else "")
            self.app_name = str(getattr(branding, "APP_NAME", "VoidFrameRU")
                                if branding is not None else "VoidFrameRU")
            self.setWindowTitle(f"{self.app_name} {self.app_version}".strip())
            self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)
            self.resize(DEFAULT_WIDTH, DEFAULT_HEIGHT)

            outer = QVBoxLayout(self)
            outer.setContentsMargins(0, 0, 0, 0)
            self.frame = QFrame()
            self.frame.setObjectName("win")
            self.frame.setStyleSheet(theme.window_qss("win", 2) + """
                QFrame#ovHeader {
                    background-color: #121a25;
                    border: none;
                    border-bottom: 1px solid #24344a;
                    border-top-left-radius: 10px;
                    border-top-right-radius: 10px;
                }
            """)
            lay = QVBoxLayout(self.frame)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(0)

            self.header = _DragHeader(self)
            self.header.setFixedHeight(44)
            header_lay = QHBoxLayout(self.header)
            header_lay.setContentsMargins(12, 7, 10, 7)
            header_lay.setSpacing(7)
            self.lbl_title = _ElidedTitle(self.app_name)
            self.lbl_title.setFont(make_font(theme.size("window"), True))
            self.lbl_title.setStyleSheet(f"color:{theme.PALETTE['text']};")
            self.lbl_title.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            self.lbl_title.setMinimumHeight(24)
            self.lbl_title.setSizePolicy(QSizePolicy.Preferred,
                                         QSizePolicy.Fixed)
            self.btn_token = QPushButton("🔑 нет токена")
            self.btn_token.setFont(make_font(10, True))
            self.btn_token.setFixedHeight(24)
            self.btn_token.setCursor(Qt.PointingHandCursor)
            self.btn_token.setStyleSheet(theme.button_qss(small=True))
            self.btn_token.clicked.connect(self.open_token)
            # чип статуса warframe.market (t20): ● Онлайн / В игре / …
            self.btn_status = QPushButton("● —")
            self.btn_status.setFont(make_font(10, True))
            self.btn_status.setFixedHeight(24)
            self.btn_status.setCursor(Qt.PointingHandCursor)
            self.btn_status.setStyleSheet(theme.button_qss(small=True))
            self.btn_status.setToolTip("Статус warframe.market")
            self.btn_status.clicked.connect(self.open_status_menu)
            self._status = ""
            self.lbl_status = QLabel("—")
            self.lbl_status.setFont(make_font(10))
            self.lbl_status.setFixedHeight(24)
            self.lbl_status.setStyleSheet(theme.status_qss())
            self.lbl_status.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            self.lbl_status.setMinimumWidth(24)
            self.lbl_status.setMaximumWidth(220)
            self.lbl_status.setSizePolicy(QSizePolicy.Preferred,
                                          QSizePolicy.Fixed)
            self.btn_refresh = QPushButton("⟳")
            self.btn_refresh.setFont(QFont("Segoe UI Symbol", 13))
            self.btn_refresh.setFixedSize(24, 24)
            self.btn_refresh.setStyleSheet(theme.button_qss(small=True))
            self.btn_refresh.setCursor(Qt.PointingHandCursor)
            self.btn_refresh.setToolTip("Обновить активную вкладку")
            self.btn_refresh.clicked.connect(self.refresh_active)
            # 🔔 центр уведомлений: бейдж непрочитанных в шапке
            self.btn_bell = QPushButton("🔔")
            self.btn_bell.setFont(make_font(12))
            self.btn_bell.setFixedSize(28, 28)
            self.btn_bell.setStyleSheet(theme.button_qss(small=True))
            self.btn_bell.setCursor(Qt.PointingHandCursor)
            self.btn_bell.setToolTip("Уведомления")
            self.btn_bell.clicked.connect(self.open_notifications)
            self._unread = 0
            self.badge_bell = QLabel("", self.btn_bell)
            self.badge_bell.setObjectName("bellBadge")
            self.badge_bell.setAlignment(Qt.AlignCenter)
            self.badge_bell.setFont(make_font(9, True))
            self.badge_bell.setFixedHeight(14)
            self.badge_bell.setStyleSheet(
                "QLabel#bellBadge { background-color:#f5c451; color:#0d131c;"
                " border-radius:7px; padding:0 3px; }")
            self.badge_bell.hide()
            self.btn_minimize = QPushButton("—")
            self.btn_minimize.setFont(make_font(12))
            self.btn_minimize.setFixedSize(24, 24)
            self.btn_minimize.setStyleSheet(theme.button_qss(small=True))
            self.btn_minimize.setCursor(Qt.PointingHandCursor)
            self.btn_minimize.setToolTip("Скрыть (F9)")
            self.btn_minimize.clicked.connect(self.hide_window)
            self.btn_fullscreen = QPushButton("⛶")
            self.btn_fullscreen.setFont(QFont("Segoe UI Symbol", 12))
            self.btn_fullscreen.setFixedSize(24, 24)
            self.btn_fullscreen.setStyleSheet(theme.button_qss(small=True))
            self.btn_fullscreen.setCursor(Qt.PointingHandCursor)
            self.btn_fullscreen.clicked.connect(self.toggle_fullscreen)
            self.btn_close = QPushButton("×")
            self.btn_close.setFont(make_font(13))
            self.btn_close.setFixedSize(24, 24)
            self.btn_close.setStyleSheet(theme.button_qss(small=True))
            self.btn_close.setCursor(Qt.PointingHandCursor)
            self.btn_close.setToolTip("Закрыть и вернуть фокус игре")
            self.btn_close.clicked.connect(self.hide_window)
            # Настройки — в шапке, чтобы не занимать место в строке вкладок.
            self.btn_settings = QPushButton("")
            self.btn_settings.setFont(make_font(12))
            self.btn_settings.setFixedSize(26, 24)
            try:
                import ui_icons
                self.btn_settings.setIcon(ui_icons.gear_icon(22))
                self.btn_settings.setIconSize(QSize(22, 22))
            except Exception:
                self.btn_settings.setText("⋯")
            self.btn_settings.setStyleSheet(theme.button_qss(small=True))
            self.btn_settings.setCursor(Qt.PointingHandCursor)
            self.btn_settings.setToolTip("Настройки (Ctrl+,)")
            self.btn_settings.clicked.connect(self.open_settings)
            self._settings_shortcut = QShortcut(QKeySequence("Ctrl+,"), self)
            self._settings_shortcut.activated.connect(self.open_settings)

            # ── 1.0.0: прогресс загрузки пакета иконок в шапке ──────────
            self.icons_progress = QWidget()
            icons_lay = QHBoxLayout(self.icons_progress)
            icons_lay.setContentsMargins(0, 0, 0, 0)
            icons_lay.setSpacing(4)
            self.lbl_icons_progress = QLabel("")
            self.lbl_icons_progress.setFont(make_font(10))
            self.lbl_icons_progress.setFixedHeight(24)
            self.lbl_icons_progress.setStyleSheet(
                f"color:{theme.PALETTE['muted']}; background:transparent;")
            self.lbl_icons_progress.setToolTip("Скачивание пакета иконок")
            self.bar_icons = QProgressBar()
            self.bar_icons.setRange(0, 100)
            self.bar_icons.setValue(0)
            self.bar_icons.setTextVisible(False)
            self.bar_icons.setFixedHeight(8)
            self.bar_icons.setFixedWidth(90)
            self.bar_icons.setStyleSheet(
                "QProgressBar { background-color:%s; border:1px solid %s;"
                " border-radius:4px; }"
                " QProgressBar::chunk { background-color:%s;"
                " border-radius:3px; }"
                % (theme.PALETTE["card"], theme.PALETTE["border"],
                   theme.PALETTE["accent"]))
            self.btn_icons_cancel = QPushButton("×")
            self.btn_icons_cancel.setFont(make_font(11))
            self.btn_icons_cancel.setFixedSize(22, 22)
            self.btn_icons_cancel.setStyleSheet(theme.button_qss(small=True))
            self.btn_icons_cancel.setCursor(Qt.PointingHandCursor)
            self.btn_icons_cancel.setToolTip(
                "Отменить загрузку иконок (продолжится при следующем запуске)")
            self.btn_icons_cancel.clicked.connect(self.cancel_icons)
            icons_lay.addWidget(self.lbl_icons_progress)
            icons_lay.addWidget(self.bar_icons)
            icons_lay.addWidget(self.btn_icons_cancel)
            self.icons_progress.hide()

            def add_header(widget):
                header_lay.addWidget(widget, 0, Qt.AlignVCenter)

            add_header(self.lbl_title)
            add_header(self.btn_token)
            add_header(self.btn_status)
            add_header(self.icons_progress)
            header_lay.addStretch(1)
            for widget in (self.lbl_status, self.btn_bell, self.btn_settings,
                           self.btn_refresh, self.btn_minimize,
                           self.btn_fullscreen, self.btn_close):
                add_header(widget)

            self.tabs = QButtonGroup(self)
            self.tabs.setExclusive(True)
            tabs_host = QWidget()
            tabs_lay = QHBoxLayout(tabs_host)
            tabs_lay.setContentsMargins(12, 4, 12, 0)
            tabs_lay.setSpacing(2)
            self.tab_buttons = []
            for index, title in enumerate(self.titles):
                button = QPushButton(title)
                button.setCheckable(True)
                button.setFont(make_font(12, True))
                button.setStyleSheet(theme.tab_qss())
                button.setCursor(Qt.PointingHandCursor)
                button.clicked.connect(lambda _=False, i=index: self.select_tab(i))
                self.tabs.addButton(button, index)
                self.tab_buttons.append(button)
                tabs_lay.addWidget(button)
            tabs_lay.addStretch(1)

            self.stack = QStackedWidget()
            self.market_widget.setObjectName("marketPage")
            self.stack.addWidget(self.market_widget)
            # 1.1.0: отдельная страница «Мои ордера» (реальный список ордеров
            # рисует market_widget, страница нужна для соответствия вкладок)
            self.orders_page = QWidget()
            self.orders_page.setObjectName("ordersPage")
            self.stack.addWidget(self.orders_page)
            self.events_widget.setObjectName("eventsPage")
            self.stack.addWidget(self.events_widget)
            if self.drops_widget is not None:
                self.stack.addWidget(self.drops_widget)
            if self.chat_widget is not None:
                self.stack.addWidget(self.chat_widget)

            self.stack.setStyleSheet("background: transparent;")

            lay.addWidget(self.header)
            lay.addWidget(tabs_host)
            lay.addWidget(self.stack, 1)
            outer.addWidget(self.frame)

            # невидимый грип ресайза — поверх правого нижнего угла
            self._grip = _ResizeGrip(self)
            self._update_resize_grip()

            self._shortcuts = []
            for index in range(len(self.titles)):
                shortcut = QShortcut(QKeySequence(f"Ctrl+{index + 1}"), self)
                shortcut.activated.connect(
                    lambda i=index: self.select_tab(i))
                self._shortcuts.append(shortcut)
            # F11 — полный экран (Esc в fullscreen только выходит из него)
            self._fs_shortcut = QShortcut(QKeySequence("F11"), self)
            self._fs_shortcut.activated.connect(self.toggle_fullscreen)

            self._update_window_buttons()

            self._status_timer = QTimer(self)
            self._status_timer.setInterval(1000)
            self._status_timer.timeout.connect(self._tick_status)
            self._status_timer.start()

            self.select_tab(self._active_tab, save=False)
            self.token_checked.connect(self._on_token_checked)
            if login_mode is not None:
                try:
                    # BUG-1: после входа/диалогов окну возвращаем кликабельность
                    login_mode.register_interactive_window(self)
                except Exception:
                    pass
            self._update_token_chip()
            if self._has_token():
                # после перезапуска проверяем сохранённый токен фоном (BUG-2)
                QTimer.singleShot(0, self.refresh_token_status)

        # ── состояние ──────────────────────────────────────────────────
        def _initial_tab(self):
            """last_tab с миграцией на 5 вкладок (без «Уведомлений»)."""
            try:
                raw = int(self.cfg.get("last_tab", 0) or 0)
            except Exception:
                raw = 0
            try:
                schema = int(self.cfg.get("tabs_schema", 0) or 0)
            except Exception:
                schema = 0
            if schema >= 3:
                index = raw if 0 <= raw < len(self.titles) else 0
            elif schema == 2:
                # schema2: 0..3 те же; 4/5 были Уведомления/Чат — на Маркет
                index = raw if 0 <= raw <= 3 else 0
            else:
                # legacy 0..6: Выбрано/Маркет/Ордера/События/Дроп/Увед./Чат
                if raw == 5:
                    index = 0
                elif raw == 6:
                    index = len(self.titles) - 1
                elif raw <= 0:
                    index = 0
                else:
                    index = min(raw - 1, len(self.titles) - 1)
            if schema != 3 or raw != index:
                self.cfg["tabs_schema"] = 3
                self.cfg["last_tab"] = index
                try:
                    self.save_values({"tabs_schema": 3, "last_tab": index})
                except Exception:
                    pass
            return index

        def current_tab(self):
            return int(self._active_tab)

        def tab_titles(self):
            return [b.text() for b in self.tab_buttons]

        def tab_count(self):
            return len(self.tab_buttons)

        def mode(self):
            """Режим окна: ``normal`` | ``maximized`` | ``fullscreen``."""
            return str(self._mode)

        def is_maximized(self):
            """Развёрнуто ли окно в пределах рабочего стола.

            Окно без рамки, поэтому «развёрнуто» — это наша геометрия
            availableGeometry, а не Qt-windowState (см. :meth:`toggle_maximized`).
            """
            return self._mode == "maximized"

        def is_fullscreen_mode(self):
            return self._mode == "fullscreen"

        def is_normal_mode(self):
            return self._mode == "normal"

        def _has_token(self):
            """Есть ли токен рынка (для чипа и перезагрузки ордеров)."""
            client = self.market_client
            checker = getattr(client, "has_token", None)
            if not callable(checker):
                return False
            try:
                return bool(checker())
            except Exception:
                return False

        def _update_token_chip(self):
            """Отрисовать чип в текущем состоянии; без токена — серый (BUG-2)."""
            if not self._has_token():
                self._token_state = TOKEN_STATE_NONE
                self._token_checking = False
            elif self._token_state == TOKEN_STATE_NONE:
                # токен появился в обход окна входа — проверяем фоном
                self.refresh_token_status()
            self._render_token_chip(self._token_state)

        def _render_token_chip(self, state):
            state = str(state or TOKEN_STATE_NONE)
            if state not in TOKEN_CHIP_TEXT:
                state = TOKEN_STATE_UNKNOWN
            self._token_state = state
            color = theme.PALETTE[TOKEN_CHIP_COLOR.get(state, "muted")]
            self.btn_token.setText(TOKEN_CHIP_TEXT[state])
            self.btn_token.setToolTip(TOKEN_CHIP_TOOLTIP[state])
            self.btn_token.setStyleSheet(
                f"color:{color}; background-color:{theme.PALETTE['card']};"
                f" border:1px solid {theme.PALETTE['border']}; border-radius:4px;"
                " padding: 2px 8px; font-size: 10px;")

        def token_state(self):
            """Текущее состояние чипа: none/checking/ok/expired/unknown."""
            return str(self._token_state)

        def refresh_token_status(self, force=False):
            """Фоновая проверка токена через GET /v2/me (BUG-2).

            Зелёный «токен есть» показывается только при 200; 401 — жёлтый
            «⚠ токен просрочен»; пустой токен — серый «нет токена». Вызов
            безопасен из GUI-потока: сеть уходит в фоновый поток, результат
            возвращается сигналом ``token_checked``.
            """
            if not self._has_token():
                self._token_checking = False
                self._render_token_chip(TOKEN_STATE_NONE)
                return False
            if self._token_checking and not force:
                return False
            self._token_checking = True
            self._render_token_chip(TOKEN_STATE_CHECKING)

            def worker():
                state = TOKEN_STATE_UNKNOWN
                try:
                    state = self._verify_token_state()
                except Exception:
                    state = TOKEN_STATE_UNKNOWN
                try:
                    self.token_checked.emit(str(state), "")
                except RuntimeError:      # окно уже закрыли
                    pass

            import threading
            threading.Thread(target=worker, daemon=True,
                             name="wf-market-token-check").start()
            return True

        def _verify_token_state(self):
            """Состояние токена по данным клиента (Bearer — внутри клиента)."""
            client = self.market_client
            checker = getattr(client, "token_state", None)
            if callable(checker):
                raw = str(checker() or "").lower()
                return {
                    "ok": TOKEN_STATE_OK,
                    "expired": TOKEN_STATE_EXPIRED,
                    "missing": TOKEN_STATE_NONE,
                    "error": TOKEN_STATE_UNKNOWN,
                }.get(raw, TOKEN_STATE_UNKNOWN)
            ok_fn = getattr(client, "token_ok", None)
            if callable(ok_fn):
                return (TOKEN_STATE_OK if ok_fn()
                        else TOKEN_STATE_EXPIRED)
            # клиент-заглушка без проверки: не показываем ложный 401
            return TOKEN_STATE_OK if self._has_token() else TOKEN_STATE_NONE

        def _on_token_checked(self, state, message=""):
            self._token_checking = False
            if not self._has_token():
                state = TOKEN_STATE_NONE
            self._render_token_chip(state)

        def _provider(self, index, default="—"):
            try:
                provider = self.status_providers[index]
            except Exception:
                return default
            if not callable(provider):
                return default
            try:
                text = provider()
                return str(text) if text else default
            except Exception:
                return default

        def _tick_status(self):
            self.lbl_status.setText(self._provider(self._active_tab))
            self.lbl_status.setStyleSheet(theme.status_qss())
            self._update_token_chip()

        def refresh_active(self):
            """⟳: обновляет активную вкладку (сеть — в фоне)."""
            try:
                action = self.refresh_actions[self._active_tab]
            except Exception:
                action = None
            if not callable(action):
                self.lbl_status.setText("обновление недоступно")
                return
            self.lbl_status.setText("обновляю…")
            try:
                action()
            except Exception:
                self.lbl_status.setText("не удалось обновить")

        # ── вкладки ────────────────────────────────────────────────────
        def select_tab(self, index, save=True):
            index = max(0, min(len(self.titles) - 1, int(index)))
            self._active_tab = index
            for i, button in enumerate(self.tab_buttons):
                button.setChecked(i == index)
            if index in (0, 1):
                self.stack.setCurrentWidget(self.market_widget)
                try:
                    self.market_widget.switch_tab(index)
                except Exception:
                    pass
            elif index == 2:
                self.stack.setCurrentWidget(self.events_widget)
            elif self.drops_widget is not None and index == 3:
                self.stack.setCurrentWidget(self.drops_widget)
            elif self.chat_widget is not None and index == self._chat_index:
                self.stack.setCurrentWidget(self.chat_widget)
            else:
                self.stack.setCurrentWidget(self.market_widget)
            try:
                import warframe
                warframe.log("Вкладка: " + self.titles[index])
            except Exception:
                pass
            self.lbl_status.setText(self._provider(index))
            if save:
                self.save_values({"last_tab": index})
                self.cfg["last_tab"] = index

        def open_settings(self):
            """Настройки: клавиши/уведомления/обновления/прочее."""
            if not callable(self.on_settings):
                self.lbl_status.setText("настройки недоступны в этой сборке")
                return False
            try:
                self.on_settings()
                return True
            except Exception as exc:
                self.lbl_status.setText(_ui_error(
                    "настройки", exc, "настройки не открылись — "
                    "подробности в app.log"))
                return False

        # ── 1.0.0: прогресс пакета иконок ──────────────────────────────
        def set_icons_progress(self, done=0, total=0, text=""):
            """Показать/обновить прогресс «Иконки: 1240 / 2500 (49%)»."""
            try:
                done = max(0, int(done))
                total = max(0, int(total))
            except Exception:
                done, total = 0, 0
            if not text:
                percent = int(done * 100.0 / total) if total else 0
                text = f"Иконки: {done} / {total} ({percent}%)"
            self._icons_progress = {"done": done, "total": total,
                                    "text": str(text)}
            try:
                self.lbl_icons_progress.setText(str(text))
                percent = int(done * 100.0 / total) if total else 0
                self.bar_icons.setRange(0, 100)
                self.bar_icons.setValue(max(0, min(100, percent)))
                self.icons_progress.show()
            except Exception:
                pass
            return self.icons_progress

        def icons_progress_text(self):
            return str(self._icons_progress.get("text") or "")

        def hide_icons_progress(self):
            try:
                self.icons_progress.hide()
            except Exception:
                pass
            self._icons_progress = {"done": 0, "total": 0, "text": ""}
            return True

        def cancel_icons(self):
            """Кнопка «×» у прогресса: прервать загрузку пакета иконок."""
            try:
                self.lbl_icons_progress.setText("Иконки: отмена…")
            except Exception:
                pass
            if callable(self.on_icons_cancel):
                try:
                    self.on_icons_cancel()
                    return True
                except Exception as exc:
                    try:
                        self.lbl_status.setText(_ui_error(
                            "иконки", exc, "иконки: не удалось отменить — "
                            "подробности в app.log"))
                    except Exception:
                        pass
                    return False
            return False

        def open_notifications(self, record_id=None):
            """Колокольчик/клик по тосту: отдельное окно центра уведомлений."""
            if self.notifications_widget is None:
                self.lbl_status.setText("центр уведомлений недоступен")
                return False
            try:
                if self._notifications_dialog is None:
                    dialog = QDialog(self)
                    dialog.setObjectName("notifDialog")
                    dialog.setWindowTitle("Уведомления")
                    dialog.setModal(False)
                    dialog.resize(620, 520)
                    try:
                        dialog.setStyleSheet(theme.notifications_qss())
                        dialog.setAutoFillBackground(True)
                        palette = dialog.palette()
                        palette.setColor(palette.Window,
                                         QColor(theme.PALETTE["panel"]))
                        palette.setColor(palette.Base,
                                         QColor(theme.PALETTE["panel"]))
                        dialog.setPalette(palette)
                    except Exception:
                        pass
                    lay = QVBoxLayout(dialog)
                    lay.setContentsMargins(0, 0, 0, 0)
                    lay.addWidget(self.notifications_widget)
                    dialog.finished.connect(self._notifications_closed)
                    self._notifications_dialog = dialog
                try:
                    self.notifications_widget.refresh()
                except Exception:
                    pass
                self._notifications_dialog.show()
                self._notifications_dialog.raise_()
                self._notifications_dialog.activateWindow()
                if record_id:
                    try:
                        self.notifications_widget.select_record(record_id)
                    except Exception:
                        pass
                return True
            except Exception as exc:
                self.lbl_status.setText(_ui_error(
                    "уведомления", exc, "уведомления не открылись — "
                    "подробности в app.log"))
                return False

        def _notifications_closed(self, _result=None):
            """Закрыли центр — возвращаем фокус главному окну."""
            try:
                if self.isVisible():
                    self.raise_()
                    self.activateWindow()
            except Exception:
                pass

        def set_unread_badge(self, count, color=None):
            """Бейдж непрочитанных: отдельная накладка, не расширяет кнопку."""
            try:
                count = max(0, int(count or 0))
            except Exception:
                count = 0
            self._unread = count
            self.btn_bell.setText("🔔")
            self.btn_bell.setToolTip(
                f"Уведомления: {count} непрочитанных" if count
                else "Уведомления")
            badge = getattr(self, "badge_bell", None)
            if badge is None:
                return count
            if count <= 0:
                badge.clear()
                badge.hide()
                self.btn_bell.setStyleSheet(theme.button_qss(small=True))
                return count
            text = "99+" if count > 99 else str(count)
            badge.setText(text)
            metrics = badge.fontMetrics()
            width = max(16, min(26, metrics.horizontalAdvance(text) + 8))
            badge.setFixedSize(width, 14)
            # Внутри header: на 2 px правее кнопки, у верхнего края.
            badge.move(max(0, self.btn_bell.width() - width + 2), 0)
            badge.raise_()
            badge.show()
            color = str(color or theme.PALETTE["warning"])
            badge.setStyleSheet(
                "QLabel#bellBadge { background-color:%s; color:#0d131c;"
                " border-radius:7px; padding:0 3px; }" % color)
            self.btn_bell.setStyleSheet(theme.button_qss(small=True))
            return count

        def open_chat(self):
            """Открыть вкладку «Чат»."""
            if self.chat_widget is None:
                self.lbl_status.setText("чат недоступен")
                return False
            self.show_tab(self._chat_index)
            return True

        def set_chat_badge(self, count):
            """Бейдж непрочитанных на вкладке «Чат»."""
            if self.chat_widget is None:
                return False
            try:
                count = max(0, int(count or 0))
            except Exception:
                count = 0
            try:
                button = self.tab_buttons[self._chat_index]
            except Exception:
                return False
            button.setText("Чат" if not count else f"Чат · {count}")
            button.setToolTip(
                f"Чат: {count} непрочитанных" if count else "Чат")
            return count

        def set_market_status(self, status, duration=None):
            """t20: чип статуса warframe.market (точка + подпись)."""
            labels = {"online": "Онлайн", "ingame": "В игре",
                      "invisible": "Невидимка", "offline": "Оффлайн"}
            colors = {"online": theme.PALETTE["success"],
                      "ingame": theme.PALETTE["accent"],
                      "invisible": theme.PALETTE["muted"],
                      "offline": theme.PALETTE["muted"]}
            status = str(status or "").lower()
            self._status = status
            text = labels.get(status, "—")
            color = colors.get(status, theme.PALETTE["muted"])
            self.btn_status.setText(f"● {text}")
            self.btn_status.setStyleSheet(
                theme.button_qss(small=True)
                + f"QPushButton {{ color:{color}; border:1px solid {color}; }}")
            self.btn_status.setToolTip(
                f"Статус warframe.market: {text}"
                + (f" · до {duration} с" if duration else ""))
            return status

        def set_status_auto(self, enabled):
            """1.0.0: авто-режим статуса убран — только явные статусы."""
            self._status_auto = False
            self.btn_status.setToolTip("Статус warframe.market")
            if self._status:
                self.set_market_status(self._status)
            return self._status_auto

        def request_status(self, status, duration=None):
            """Явный выбор в меню → callback сервиса статуса."""
            if not callable(self.on_status_set):
                return False
            try:
                self.on_status_set(str(status), duration)
                return True
            except Exception as exc:
                self.lbl_status.setText(_ui_error(
                    "статус", exc, "статус не изменился — "
                    "подробности в app.log"))
                return False

        def request_status_auto(self, enabled):
            self._status_auto = bool(enabled)
            if not callable(self.on_status_auto):
                return False
            try:
                self.on_status_auto(self._status_auto)
                return True
            except Exception as exc:
                self.lbl_status.setText(_ui_error(
                    "статус", exc, "статус не изменился — "
                    "подробности в app.log"))
                return False

        def request_status_profile(self):
            """Fallback: открыть профиль на сайте (если WS-смена не видна)."""
            if not callable(self.on_status_profile):
                return False
            try:
                self.on_status_profile()
                return True
            except Exception as exc:
                self.lbl_status.setText(_ui_error(
                    "профиль", exc, "профиль не открылся — "
                    "подробности в app.log"))
                return False

        def status_menu_labels(self):
            """1.0.0: только явные статусы — пункта «Авто» больше нет."""
            return ["Онлайн", "В игре", "Невидимка"]

        def open_status_menu(self):
            menu = QMenu(self)
            for label, status in (("Онлайн", "online"), ("В игре", "ingame"),
                                  ("Невидимка", "invisible")):
                action = menu.addAction(label)
                action.triggered.connect(
                    lambda _=False, s=status: self.request_status(s))
            duration_menu = menu.addMenu("На время")
            for label, status in (("Онлайн", "online"), ("В игре", "ingame")):
                sub = duration_menu.addMenu(label)
                for seconds, title in ((1800, "30 минут"), (3600, "1 час"),
                                       (7200, "2 ч"), (14400, "4 ч")):
                    act = sub.addAction(title)
                    act.triggered.connect(
                        lambda _=False, s=status, d=seconds:
                        self.request_status(s, d))
            menu.addSeparator()
            profile = menu.addAction("Открыть профиль на сайте")
            profile.triggered.connect(self.request_status_profile)
            menu.exec_(self.btn_status.mapToGlobal(
                self.btn_status.rect().bottomLeft()))
            return True

        def update_hotkey_hints(self, mapping=None):
            """Обновить подписи/подсказки после смены горячих клавиш."""
            mapping = mapping if isinstance(mapping, dict) else {}
            hide_key = str(mapping.get("hide") or "").upper()
            window_key = str(mapping.get("window") or "").upper()
            scan_key = str(mapping.get("scan") or "").upper()
            if hide_key:
                self.btn_close.setToolTip(
                    f"Закрыть и вернуть фокус игре ({hide_key})")
                self.btn_minimize.setToolTip(f"Скрыть ({hide_key})")
            if window_key:
                self.setToolTip(f"Окно: {window_key}")
            hints = " · ".join(
                f"{name}: {str(mapping.get(key) or '').upper()}"
                for key, name in (("scan", "скан"), ("window", "окно"),
                                  ("hide", "скрыть"), ("events", "события"),
                                  ("market", "маркет"))
                if mapping.get(key))
            self.lbl_title.setToolTip(
                ("Горячие клавиши — " + hints) if hints else "VoidFrameRU")
            return True

        def show_tab(self, index):
            self.select_tab(index)
            self.show_window()

        # ── показ/скрытие ─────────────────────────────────────────────
        def show_window(self, focus=True):
            """Показать окно.

            :param focus: ``True`` — как F8: активировать окно, отдать ему мышь
                и вызвать ``on_open`` (``focus_editor``). ``False`` — вернуть
                окно после входа в браузере: показать, но **не** выхватывать
                фокус у игры/браузера и не трогать ``focus_editor``.
            """
            if not self._geometry_restored:
                self.restore_geometry()
                self._geometry_restored = True
            self.clamp_to_screen()
            # BUG-1: перед показом снимаем возможный disabled/прозрачность/grab
            self.ensure_interactive(activate=False)
            self.show()
            self.raise_()
            if focus:
                self.activateWindow()
            self._status_timer.start()
            self._tick_status()
            if focus and callable(self.on_open):
                try:
                    self.on_open()
                except Exception:
                    pass

        # ── режим входа на warframe.market ────────────────────────────
        def enter_login_mode(self, keep=()):
            """Спрятать окно (и все окна приложения) на время входа.

            Пользователь входит на сайт в настоящем браузере, поэтому **ни
            одно** окно оверлея не должно его перекрывать. Прячем все окна
            приложения (включая диалог входа, плашки, «Итого», старые окна) и
            снимаем с них ``WindowStaysOnTopHint``; порядок для каждого окна:
            ``hide()`` → флаг → (при возврате) флаг → ``show()``.

            Диалог входа и пилюля «Отменить» передаются в ``keep`` — их
            трогать нельзя: диалог прячет себя сам, а пилюля остаётся
            единственным видимым элементом приложения (она topmost намеренно,
            чтобы «Отменить» была доступна даже в полноэкранном браузере).

            :return: ``True`` — режим включён (повторный вызов безопасен).
            """
            if self._login_mode:
                return False
            self._login_was_visible = bool(self.isVisible())
            self._login_tab = int(self._active_tab)
            self._login_topmost = _has_topmost(self)
            self._login_mode = True
            try:
                self._status_timer.stop()
            except Exception:
                pass
            if self._mode != "normal":
                # из max/fullscreen возвращаемся в normal, иначе после входа
                # окно вернулось бы развёрнутым «поверх всего»
                try:
                    self._leave_special_mode()
                except Exception:
                    pass
            try:
                self._login_geometry = QRect(self._normal_geometry())
            except Exception:
                self._login_geometry = None
            snapshot = []
            if login_mode is not None:
                try:
                    snapshot = login_mode.hide_app_windows(
                        keep=(self,) + tuple(w for w in (keep or ())
                                             if w is not None))
                except Exception:
                    snapshot = []
            self._login_snapshot = snapshot
            # страховка: своё окно точно скрыто и без topmost
            try:
                self.hide()
                if _has_topmost(self):
                    self.setWindowFlag(Qt.WindowStaysOnTopHint, False)
            except Exception:
                pass
            return True

        def exit_login_mode(self, restore=True):
            """Вернуть окно после входа (успех/ошибка/отмена/таймаут).

            Возвращает topmost, видимость, геометрию и активную вкладку.
            ``on_open``/``focus_editor`` НЕ вызываются и окно не активируется —
            иначе после входа приложение отобрало бы мышь у игры.
            """
            was_login = self._login_mode
            self._login_mode = False
            snapshot, self._login_snapshot = self._login_snapshot, None
            if login_mode is not None:
                try:
                    login_mode.restore_app_windows(snapshot)
                except Exception:
                    pass
            try:
                if self._login_topmost and not _has_topmost(self):
                    self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            except Exception:
                pass
            if restore and self._login_was_visible:
                if self._login_geometry is not None:
                    try:
                        self.setGeometry(self._login_geometry)
                    except Exception:
                        pass
                try:
                    # show() без активации: браузер/игра остаются на переднем
                    # плане, окно возвращается видимым и снова topmost
                    self.setAttribute(Qt.WA_ShowWithoutActivating, True)
                    try:
                        self.show_window(focus=False)
                    finally:
                        self.setAttribute(Qt.WA_ShowWithoutActivating, False)
                except Exception:
                    pass
                try:
                    self.select_tab(self._login_tab, save=False)
                except Exception:
                    pass
            else:
                try:
                    self._status_timer.start()
                except Exception:
                    pass
            # BUG-1: после любого исхода входа окно обязано принимать клики
            self.ensure_interactive(activate=True)
            return was_login

        def is_login_mode(self):
            """Идёт ли вход в браузере (окно спрятано)?"""
            return bool(self._login_mode)

        def hide_window(self, save=True):
            # B2: скрытие оверлея не должно оставлять на экране ни одного окна
            # приложения — закрываем подтверждения, формы, окно входа.
            if self._notifications_dialog is not None:
                try:
                    self._notifications_dialog.hide()
                except Exception:
                    pass
            if login_mode is not None:
                try:
                    if not login_mode.in_progress():
                        login_mode.close_active_dialogs()
                except Exception:
                    pass
            # из fullscreen/maximized всегда возвращаемся в normal, иначе
            # следующее открытие было бы снова развёрнутым
            if self._mode != "normal":
                self._leave_special_mode()
            if save:
                self.save_state()
            self._status_timer.stop()
            self._update_resize_grip()
            self.hide()
            if callable(self.on_close):
                try:
                    self.on_close()
                except Exception:
                    pass

        def toggle(self):
            if self.isVisible():
                self.hide_window()
            else:
                self.show_window()

        def closeEvent(self, event):
            event.ignore()
            self.hide_window()

        def keyPressEvent(self, event):
            if event.key() == Qt.Key_Escape:
                if self._mode == "fullscreen":
                    # Esc в полноэкранном режиме только выходит из него
                    self._leave_special_mode()
                    event.accept()
                    return
                self.hide_window()
                return
            if event.key() == Qt.Key_F11:
                self.toggle_fullscreen()
                event.accept()
                return
            super().keyPressEvent(event)

        # ── геометрия ─────────────────────────────────────────────────
        def _available_rect(self, point=None):
            screen = None
            if point is not None:
                try:
                    screen = QGuiApplication.screenAt(point)
                except Exception:
                    screen = None
            if screen is None:
                screen = QGuiApplication.primaryScreen()
            return screen.availableGeometry()

        def _fit_size(self, width, height, area):
            """Размер в пределах рабочего стола, но не меньше 900×600."""
            width = max(MIN_WIDTH, min(int(width), area.width()))
            height = max(MIN_HEIGHT, min(int(height), area.height()))
            return width, height

        def _normal_geometry(self):
            """Последняя геометрия нормального окна (никогда не max/fullscreen)."""
            geom = self._normal_geom
            if geom is not None and geom.isValid():
                return QRect(geom)
            return QRect(self.x(), self.y(),
                         max(MIN_WIDTH, self.width()),
                         max(MIN_HEIGHT, self.height()))

        def clamp_to_screen(self):
            """Окно целиком в пределах рабочего стола (и не больше него)."""
            if self._mode != "normal":
                return
            area = self._available_rect(self.frameGeometry().center())
            width, height = self._fit_size(self.width(), self.height(), area)
            if width != self.width() or height != self.height():
                self.resize(width, height)
            x = max(area.left(), min(self.x(), area.right() - width + 1))
            y = max(area.top(), min(self.y(), area.bottom() - height + 1))
            if (x, y) != (self.x(), self.y()):
                self.move(x, y)

        def begin_drag(self, global_pos):
            """Начинает перетаскивание; из max/fullscreen — сначала normal.

            Возвращает смещение курсора от левого верхнего угла окна (QPoint)
            либо ``None``, если тащить нельзя. Смещение клампится по шапке,
            чтобы курсор оставался над ней (после восстановления из max окно
            «прыгает» под курсор, а не улетает).
            """
            if self._mode != "normal":
                self._leave_special_mode()
            geometry = self.frameGeometry()
            offset = QPoint(global_pos) - geometry.topLeft()
            offset.setX(max(0, min(int(offset.x()), max(0, geometry.width() - 1))))
            header_h = max(1, self.header.height())
            offset.setY(max(0, min(int(offset.y()), header_h - 1)))
            return offset

        def drag_to(self, position):
            """Передвигает окно с клампингом по рабочему столу."""
            if self._mode != "normal":
                return
            area = self._available_rect(position)
            width, height = self.width(), self.height()
            max_x = area.right() - width + 1
            max_y = area.bottom() - height + 1
            x = max(area.left(), min(int(position.x()), max_x))
            y = max(area.top(), min(int(position.y()), max_y))
            self.move(x, y)

        def resize_by_delta(self, delta, start_size):
            """Ресайз за грип: только normal, минимум 900×600, максимум экран."""
            if self._mode != "normal" or start_size is None:
                return
            area = self._available_rect(self.frameGeometry().center())
            max_w = max(MIN_WIDTH, min(area.width(),
                                       area.right() - self.x() + 1))
            max_h = max(MIN_HEIGHT, min(area.height(),
                                        area.bottom() - self.y() + 1))
            width = max(MIN_WIDTH, min(start_size.width() + int(delta.x()), max_w))
            height = max(MIN_HEIGHT,
                         min(start_size.height() + int(delta.y()), max_h))
            self.resize(width, height)

        def _leave_special_mode(self):
            """Возвращает окно в normal из maximized/fullscreen."""
            if self._mode == "normal":
                return
            geometry = self._normal_geometry()
            if self._mode == "fullscreen":
                self.showNormal()
            self._mode = "normal"
            self.setGeometry(geometry)
            self._normal_geom = QRect(geometry)
            self._update_window_buttons()
            self._update_resize_grip()

        def toggle_maximized(self):
            """Совместимость: развернуть/восстановить без кнопки в шапке."""
            if self._mode != "normal":
                # из fullscreen/max — вернуть нормальный размер
                self._leave_special_mode()
                return
            self._normal_geom = QRect(self.geometry())
            area = self._available_rect(self.frameGeometry().center())
            self._mode = "maximized"
            self.setGeometry(area)
            self._update_window_buttons()
            self._update_resize_grip()

        def toggle_fullscreen(self):
            """«⛶»/F11: полноэкранный режим на текущем мониторе и обратно."""
            if self._mode == "fullscreen":
                self._leave_special_mode()
                return
            if self._mode == "normal":
                self._normal_geom = QRect(self.geometry())
            self._mode = "fullscreen"
            self.showFullScreen()
            self._update_window_buttons()
            self._update_resize_grip()

        def _update_resize_grip(self):
            grip = self._grip
            if grip is None:
                return
            grip.setVisible(self._mode == "normal")
            if self._mode == "normal":
                grip.move(max(0, self.width() - RESIZE_GRIP),
                          max(0, self.height() - RESIZE_GRIP))
                grip.raise_()

        def _update_window_buttons(self):
            if self._mode == "fullscreen":
                self.btn_fullscreen.setToolTip(
                    "Выйти из полноэкранного режима (F11, Esc)")
            else:
                self.btn_fullscreen.setToolTip("Полный экран (F11)")

        def restore_geometry(self):
            size = _as_pair(self.cfg.get("window_size")) or (DEFAULT_WIDTH,
                                                             DEFAULT_HEIGHT)
            pos = _as_pair(self.cfg.get("window_geometry"))
            area = self._available_rect()
            width, height = self._fit_size(size[0], size[1], area)
            self.resize(width, height)
            if pos is None:
                self._center(area, width, height)
            else:
                x, y = pos
                # сохранённая позиция вне экрана (сменили монитор) → центрируем
                probe = QPoint(x + width // 2, y + height // 2)
                if area.contains(probe) or area.contains(QPoint(x, y)):
                    self.move(max(area.left(), min(x, area.right() - width + 1)),
                              max(area.top(), min(y, area.bottom() - height + 1)))
                else:
                    self._center(area, width, height)
            self._mode = "normal"
            self._normal_geom = QRect(self.geometry())

        def _center(self, area, width, height):
            self.move(area.left() + max(0, (area.width() - width) // 2),
                      area.top() + max(0, (area.height() - height) // 2))

        def save_geometry(self):
            """Сохраняет геометрию только нормального окна (не max/fullscreen)."""
            geom = self._normal_geometry()
            values = {"window_geometry": [geom.x(), geom.y()],
                      "window_size": [geom.width(), geom.height()]}
            self.save_values(values)
            self.cfg.update(values)

        def save_state(self):
            self.save_geometry()
            values = {"last_tab": int(self._active_tab)}
            self.save_values(values)
            self.cfg.update(values)

        def resizeEvent(self, event):
            super().resizeEvent(event)
            self._update_resize_grip()
            if self._mode == "normal":
                self._normal_geom = QRect(self.geometry())

        def moveEvent(self, event):
            super().moveEvent(event)
            if self._mode == "normal":
                self._normal_geom = QRect(self.geometry())

        # ── токен и вход ──────────────────────────────────────────────
        def _reload_orders(self):
            """После входа: перезагрузить «Мои ордера» (сеть — в фоне панели).

            Панель маркета не обязана иметь публичный ``reload_orders``,
            поэтому пробуем по очереди: публичный метод, затем её внутреннюю
            загрузку с ``force=True``, затем общий ``refresh``.
            """
            widget = self.market_widget
            for name in ("reload_orders", "refresh_orders"):
                method = getattr(widget, name, None)
                if callable(method):
                    try:
                        method()
                        return True
                    except Exception:
                        return False
            loader = getattr(widget, "_load_orders", None)
            if callable(loader):
                try:
                    loader(force=True)
                    return True
                except Exception:
                    return False
            refresher = getattr(widget, "refresh", None)
            if callable(refresher):
                try:
                    refresher()
                    return True
                except Exception:
                    pass
            return False

        def open_token(self):
            """Чип «🔑»: окно входа (браузер) или прежняя ручная вставка.

            Окно входа само перехватывает cookie JWT, проверяет токен через
            GET /v2/me и сохраняет его в config.json; при недоступном
            PyQtWebEngine оно честно объясняет это и предлагает ручную
            вставку. Закрытие окна не роняет приложение.

            Окно входа с режимом «вход в браузере» показывается **без**
            ``exec_()``: на время входа оно прячется (``hide()``), а
            ``QDialog`` при скрытии завершает свой модальный цикл — окно
            осталось бы без обработчика и оверлей не вернулся бы. Старые
            окна (ручная вставка токена) работают как раньше — через
            ``exec_()``.
            """
            if self.auth_factory is None and self.token_factory is None:
                return
            # BUG-2: по клику на чип обновляем статус токена (GET /v2/me)
            self.refresh_token_status(force=True)
            if self._login_dialog is not None:
                # окно входа уже открыто — второе не создаём
                try:
                    self._login_dialog.show()
                    self._login_dialog.raise_()
                except Exception:
                    pass
                return
            try:
                dialog = None
                if self.auth_factory is not None:
                    dialog = self.auth_factory(self)
                else:
                    _sell_cls, token_cls, save_fn = self.token_factory()
                    dialog = token_cls(self.cfg, self.market_client, self,
                                       save_fn=save_fn)
                if dialog is None:
                    return
                if self._show_login_dialog(dialog):
                    return
                dialog.exec_()
            except Exception as exc:
                self.lbl_status.setText(_ui_error(
                    "окно входа", exc, "окно входа не открылось — "
                    "подробности в app.log"))
                return
            self._after_login_dialog()

        def _show_login_dialog(self, dialog):
            """Показать окно входа неблокирующе (без ``exec_()``).

            Возвращает ``True``, если окно показано так. Так делается для
            окон с ``finished`` (браузерный вход): их прячут на время входа.
            Окна без ``finished``/``show`` (ручная вставка) — ``False``,
            вызывающий код использует ``exec_()``.
            """
            if not (callable(getattr(dialog, "show", None))
                    and hasattr(dialog, "finished")):
                return False
            self._login_dialog = dialog
            try:
                dialog.finished.connect(self._on_login_dialog_finished)
            except Exception:
                pass
            try:
                # t30: токен из личного профиля браузера — зелёный чип сразу,
                # не дожидаясь закрытия окна входа.
                signal = getattr(dialog, "auth_changed", None)
                if signal is not None:
                    signal.connect(self._on_auth_changed_live)
            except Exception:
                pass
            try:
                dialog.show()
                dialog.raise_()
                dialog.activateWindow()
            except Exception:
                self._login_dialog = None
                try:
                    dialog.deleteLater()
                except Exception:
                    pass
                return False
            return True

        def _on_login_dialog_finished(self, _result=0):
            """Окно входа закрыто: чип токена, «Мои ордера»/маркет."""
            dialog, self._login_dialog = self._login_dialog, None
            if dialog is not None:
                try:
                    dialog.deleteLater()
                except Exception:
                    pass
            self._after_login_dialog()

        def _on_auth_changed_live(self):
            """Токен изменился, окно входа ещё открыто: обновить чип/ордера."""
            try:
                self.refresh_token_status(force=True)
            except Exception:
                pass
            if self._has_token():
                try:
                    self._reload_orders()
                except Exception:
                    pass

        def _after_login_dialog(self):
            """Обновляет чип токена и данные после закрытия окна входа."""
            # BUG-1: после закрытия окна входа главное окно кликабельно
            self.ensure_interactive(activate=True)
            # BUG-2: зелёный чип — только после настоящей проверки GET /v2/me
            self.refresh_token_status(force=True)
            if self._has_token():
                # вошли — подтягиваем «Мои ордера» сразу (в фоне)
                self._reload_orders()
            else:
                try:
                    self.market_widget.refresh()
                except Exception:
                    pass

        # ── кликабельность окна (BUG-1) ───────────────────────────────
        def ensure_interactive(self, activate=False):
            """Гарантировать, что окно снова принимает мышь и клавиатуру.

            После режима входа и модальных диалогов окно могло остаться
            выключенным (``isEnabled() == False``), с атрибутом «прозрачно для
            мыши», с нативным ``WS_EX_TRANSPARENT``/``WS_EX_NOACTIVATE`` или с
            чужим mouse grab. Снимаем всё это перед показом и после закрытия
            диалогов, чтобы шапка и вкладки работали без перезапуска.
            """
            try:
                self.setEnabled(True)
            except Exception:
                pass
            try:
                self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
            except Exception:
                pass
            try:
                if self.windowFlags() & Qt.WindowTransparentForInput:
                    self.setWindowFlag(Qt.WindowTransparentForInput, False)
                    if self.isVisible():
                        self.show()
            except Exception:
                pass
            # остаточные grab'ы (пилюля/диалог/меню) не должны держать мышь
            for release in ("releaseMouse", "releaseKeyboard"):
                try:
                    getattr(self, release)()
                except Exception:
                    pass
            try:
                grabbed = QWidget.mouseGrabber()
                if grabbed is not None and grabbed is not self:
                    grabbed.releaseMouse()
                grabbed = QWidget.keyboardGrabber()
                if grabbed is not None and grabbed is not self:
                    grabbed.releaseKeyboard()
            except Exception:
                pass
            # спрятанный модальный диалог не должен блокировать главное окно
            self._release_stale_modality()
            # нативные стили «клик сквозь» / «не активировать» (Windows)
            if login_mode is not None:
                try:
                    login_mode.clear_clickthrough_style(self)
                except Exception:
                    pass
            if activate:
                try:
                    self.setAttribute(Qt.WA_ShowWithoutActivating, False)
                except Exception:
                    pass
                try:
                    self.raise_()
                    self.activateWindow()
                except Exception:
                    pass
            return True

        def _release_stale_modality(self):
            """Снять модальность со СКРЫТЫХ диалогов приложения (BUG-1).

            Модальный диалог, который спрятали, а не закрыли, мог оставить
            главное окно заблокированным. Видимые диалоги не трогаем — с ними
            пользователь работает; скрытые делаем немодальными.
            """
            from PyQt5.QtWidgets import QApplication, QDialog

            app = QApplication.instance()
            if app is None:
                return 0
            released = 0
            seen = set()
            for widget in list(app.topLevelWidgets()) + list(app.allWidgets()):
                if widget is None or id(widget) in seen:
                    continue
                seen.add(id(widget))
                try:
                    if widget is self or not widget.isWindow():
                        continue
                    if not isinstance(widget, QDialog):
                        continue
                    if widget.isVisible():
                        continue
                    # диалог, который ещё ни разу не показывали, — не «залипший»:
                    # setModal(True) до первого show() это норма, не трогаем
                    if not widget.testAttribute(Qt.WA_WState_ExplicitShowHide):
                        continue
                    if widget.windowModality() == Qt.NonModal:
                        continue
                    widget.setModal(False)
                    widget.setWindowModality(Qt.NonModal)
                    released += 1
                except Exception:
                    continue
            return released

        def interactive_report(self):
            """Диагностика кликабельности окна (тесты/лог, BUG-1)."""
            try:
                grabber = QWidget.mouseGrabber()
                grabber_name = (type(grabber).__name__
                                if grabber is not None else None)
            except Exception:
                grabber_name = None
            return {
                "visible": bool(self.isVisible()),
                "enabled": bool(self.isEnabled()),
                "transparent_for_mouse": bool(
                    self.testAttribute(Qt.WA_TransparentForMouseEvents)),
                "transparent_for_input": bool(
                    self.windowFlags() & Qt.WindowTransparentForInput),
                "show_without_activating": bool(
                    self.testAttribute(Qt.WA_ShowWithoutActivating)),
                "topmost": bool(
                    self.windowFlags() & Qt.WindowStaysOnTopHint),
                "mouse_grabber": grabber_name,
            }

        def is_interactive(self):
            """Короткая проверка: окно включено и не «прозрачно» для мыши."""
            report = self.interactive_report()
            return bool(report["enabled"]
                        and not report["transparent_for_mouse"]
                        and not report["transparent_for_input"])

    return OverlayWindow
