# -*- coding: utf-8 -*-
"""Единое окно «Warframe Overlay» с вкладками (стиль AlecaFrame).

Одна кнопка (F8) открывает одно окно; внутри — вкладки «Выбрано», «Маркет»,
«Мои ордера», «События». Содержимое — уже существующие панели:
редактор списка (F8-редактор), маркет-браузер (t6) и панель событий (t5);
они создаются один раз и не пересоздаются ни при переключении вкладок,
ни при смене режима окна.

Особенности:
* кастомная шапка: тянуть можно за любое свободное место шапки
  (openHand/closedHand), кнопки и поля внутри шапки перетаскивание не
  начинают; двойной клик — развернуть/восстановить; после разворота
  перетаскивание сначала возвращает нормальный размер, потом двигает окно;
  окно не выходит за пределы рабочего стола текущего монитора;
* режимы окна: normal ↔ maximized (кнопка «□», двойной клик по шапке) и
  fullscreen (кнопка «⛶», F11). Esc в полноэкранном режиме выходит из него,
  в обычном — скрывает окно. Кнопки «—» (скрыть, как F9) и «×» (закрыть и
  вернуть фокус игре) — справа, в стиле ui_theme, с подсказками;
* ресайз за невидимый грип 12×12 в правом нижнем углу, минимум 900×600,
  максимум — availableGeometry; в max/fullscreen ресайз заблокирован;
* позиция/размер/последняя вкладка сохраняются в config.json
  (``window_geometry``, ``window_size``, ``last_tab``) — только для
  нормального состояния; при старте окно открывается нормальным с
  сохранённой геометрией, невалидная позиция центрируется;
* Ctrl+1..4 и клик переключают вкладки; статус маркета (чип токена) и
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

TAB_TITLES = ("Выбрано", "Маркет", "Мои ордера", "События")
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
    from PyQt5.QtCore import QPoint, QRect, Qt, QTimer, pyqtSignal
    from PyQt5.QtGui import QFont, QGuiApplication, QKeySequence
    from PyQt5.QtWidgets import (QAbstractButton, QAbstractSlider,
                                 QAbstractSpinBox, QButtonGroup, QComboBox,
                                 QFrame, QHBoxLayout, QLabel, QLineEdit,
                                 QPushButton, QShortcut, QStackedWidget,
                                 QVBoxLayout, QWidget)

    #: Виджеты в шапке, которые сами обрабатывают мышь: с них drag не стартует.
    INTERACTIVE = (QAbstractButton, QLineEdit, QComboBox, QAbstractSpinBox,
                   QAbstractSlider)

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

    class _DragHeader(QFrame):
        """Шапка окна: перетаскивание и двойной клик (развернуть/восстановить).

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
            if event.button() == Qt.LeftButton:
                if self._interactive_at(event.pos()):
                    event.ignore()
                    return
                self.window.toggle_maximized()
                event.accept()
                return
            super().mouseDoubleClickEvent(event)

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
                     drops_widget=None, on_updates=None, on_settings=None,
                     notifications_widget=None):
            super().__init__(None, Qt.Window | Qt.FramelessWindowHint |
                             Qt.WindowStaysOnTopHint)
            self.cfg = cfg
            self.editor_widget = editor
            self.market_widget = market
            self.events_widget = events
            self.drops_widget = drops_widget
            self.notifications_widget = notifications_widget
            #: названия вкладок: 4 старые + «Дроп» + «Уведомления» (если есть)
            self.titles = list(TAB_TITLES)
            if drops_widget is not None:
                self.titles.append("Дроп")
            self._notifications_index = None
            if notifications_widget is not None:
                self._notifications_index = len(self.titles)
                self.titles.append("Уведомления")
            self.market_client = market_client
            self.token_factory = token_factory
            # auth_factory(parent) -> QDialog — окно входа через браузер
            # (market_auth.build_auth_dialog); None — прежняя ручная вставка.
            self.auth_factory = auth_factory
            self.save_values = save_values or (lambda mapping: True)
            self.on_open = on_open
            self.on_close = on_close
            self.on_updates = on_updates
            self.on_settings = on_settings
            self.hotkey_aliases = bool(hotkey_aliases)
            self.status_providers = list(status_providers or [])
            self.refresh_actions = list(refresh_actions or [])
            if notifications_widget is not None:
                # индекс провайдера/действия совпадает с индексом вкладки
                self.status_providers.append(
                    lambda: f"уведомления: "
                            f"{self.notifications_widget.unread_count()}")
                self.refresh_actions.append(self.notifications_widget.refresh)
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
            self.setWindowTitle("Warframe Overlay")
            self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)
            self.resize(DEFAULT_WIDTH, DEFAULT_HEIGHT)

            outer = QVBoxLayout(self)
            outer.setContentsMargins(0, 0, 0, 0)
            self.frame = QFrame()
            self.frame.setObjectName("win")
            self.frame.setStyleSheet(theme.window_qss("win", 10) + """
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
            header_lay = QHBoxLayout(self.header)
            header_lay.setContentsMargins(14, 8, 10, 6)
            header_lay.setSpacing(8)
            self.lbl_title = QLabel("Warframe Overlay")
            self.lbl_title.setFont(make_font(theme.size("window"), True))
            self.lbl_title.setStyleSheet(f"color:{theme.PALETTE['text']};")
            self.lbl_title.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            self.btn_token = QPushButton("🔑 нет токена")
            self.btn_token.setFont(make_font(10, True))
            self.btn_token.setCursor(Qt.PointingHandCursor)
            self.btn_token.setStyleSheet(theme.button_qss(small=True))
            self.btn_token.clicked.connect(self.open_token)
            self.lbl_status = QLabel("—")
            self.lbl_status.setFont(make_font(10))
            self.lbl_status.setStyleSheet(theme.status_qss())
            self.lbl_status.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            self.btn_refresh = QPushButton("⟳")
            self.btn_refresh.setFont(QFont("Segoe UI Symbol", 13))
            self.btn_refresh.setFixedSize(24, 22)
            self.btn_refresh.setStyleSheet(theme.button_qss(small=True))
            self.btn_refresh.setCursor(Qt.PointingHandCursor)
            self.btn_refresh.setToolTip("Обновить активную вкладку")
            self.btn_refresh.clicked.connect(self.refresh_active)
            # 🔔 центр уведомлений: бейдж непрочитанных в шапке
            self.btn_bell = QPushButton("🔔")
            self.btn_bell.setFont(make_font(12))
            self.btn_bell.setFixedSize(32, 22)
            self.btn_bell.setStyleSheet(theme.button_qss(small=True))
            self.btn_bell.setCursor(Qt.PointingHandCursor)
            self.btn_bell.setToolTip("Уведомления")
            self.btn_bell.clicked.connect(self.open_notifications)
            self._unread = 0
            self.btn_minimize = QPushButton("—")
            self.btn_minimize.setFont(make_font(12))
            self.btn_minimize.setFixedSize(24, 22)
            self.btn_minimize.setStyleSheet(theme.button_qss(small=True))
            self.btn_minimize.setCursor(Qt.PointingHandCursor)
            self.btn_minimize.setToolTip("Скрыть (F9)")
            self.btn_minimize.clicked.connect(self.hide_window)
            self.btn_maximize = QPushButton("□")
            self.btn_maximize.setFont(make_font(13))
            self.btn_maximize.setFixedSize(24, 22)
            self.btn_maximize.setStyleSheet(theme.button_qss(small=True))
            self.btn_maximize.setCursor(Qt.PointingHandCursor)
            self.btn_maximize.clicked.connect(self.toggle_maximized)
            self.btn_fullscreen = QPushButton("⛶")
            self.btn_fullscreen.setFont(QFont("Segoe UI Symbol", 12))
            self.btn_fullscreen.setFixedSize(24, 22)
            self.btn_fullscreen.setStyleSheet(theme.button_qss(small=True))
            self.btn_fullscreen.setCursor(Qt.PointingHandCursor)
            self.btn_fullscreen.clicked.connect(self.toggle_fullscreen)
            self.btn_close = QPushButton("×")
            self.btn_close.setFont(make_font(13))
            self.btn_close.setFixedSize(24, 22)
            self.btn_close.setStyleSheet(theme.button_qss(small=True))
            self.btn_close.setCursor(Qt.PointingHandCursor)
            self.btn_close.setToolTip("Закрыть и вернуть фокус игре")
            self.btn_close.clicked.connect(self.hide_window)
            header_lay.addWidget(self.lbl_title)
            header_lay.addWidget(self.btn_token)
            header_lay.addStretch(1)
            header_lay.addWidget(self.lbl_status)
            header_lay.addWidget(self.btn_bell)
            header_lay.addWidget(self.btn_refresh)
            header_lay.addWidget(self.btn_minimize)
            header_lay.addWidget(self.btn_maximize)
            header_lay.addWidget(self.btn_fullscreen)
            header_lay.addWidget(self.btn_close)

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
            # «пункт» обновлений прямо в едином окне (F8): текущая версия,
            # проверка, «Обновить и перезапустить» — открывает диалог.
            self.btn_updates = QPushButton("Обновления")
            self.btn_updates.setFont(make_font(10))
            self.btn_updates.setStyleSheet(theme.button_qss(small=True))
            self.btn_updates.setCursor(Qt.PointingHandCursor)
            self.btn_updates.setToolTip(
                "Обновления приложения: проверить и установить новую версию")
            self.btn_updates.clicked.connect(self.open_updates)
            tabs_lay.addWidget(self.btn_updates)
            # «⚙»: настройки (горячие клавиши, уведомления, оверлей…)
            self.btn_settings = QPushButton("⚙")
            self.btn_settings.setFont(make_font(12))
            self.btn_settings.setFixedSize(24, 22)
            self.btn_settings.setStyleSheet(theme.button_qss(small=True))
            self.btn_settings.setCursor(Qt.PointingHandCursor)
            self.btn_settings.setToolTip(
                "Настройки: горячие клавиши, уведомления, оверлей, обновления")
            self.btn_settings.clicked.connect(self.open_settings)
            tabs_lay.addWidget(self.btn_settings)

            self.stack = QStackedWidget()
            self.stack.addWidget(self.editor_widget)
            self.stack.addWidget(self.market_widget)
            self.stack.addWidget(self.events_widget)
            if self.drops_widget is not None:
                self.stack.addWidget(self.drops_widget)
            if self.notifications_widget is not None:
                self.stack.addWidget(self.notifications_widget)
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
            try:
                index = int(self.cfg.get("last_tab", 0) or 0)
            except Exception:
                index = 0
            return max(0, min(len(self.titles) - 1, index))

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
            if index in (1, 2):
                self.stack.setCurrentWidget(self.market_widget)
                try:
                    self.market_widget.switch_tab(index - 1)
                except Exception:
                    pass
            elif index == 3:
                self.stack.setCurrentWidget(self.events_widget)
            elif self.drops_widget is not None and index == 4:
                self.stack.setCurrentWidget(self.drops_widget)
            elif (self.notifications_widget is not None
                  and index == self._notifications_index):
                self.stack.setCurrentWidget(self.notifications_widget)
            else:
                self.stack.setCurrentWidget(self.editor_widget)
            self.lbl_status.setText(self._provider(index))
            if save:
                self.save_values({"last_tab": index})
                self.cfg["last_tab"] = index

        def open_updates(self):
            """Пункт «Обновления» в едином окне: открыть окно проверки версии."""
            if not callable(self.on_updates):
                self.lbl_status.setText("обновления недоступны в этой сборке")
                return False
            try:
                self.on_updates()
                return True
            except Exception as exc:
                self.lbl_status.setText(f"обновления: {exc}")
                return False

        def open_settings(self):
            """Кнопка «⚙»: открыть окно настроек (клавиши/уведомления/…)."""
            if not callable(self.on_settings):
                self.lbl_status.setText("настройки недоступны в этой сборке")
                return False
            try:
                self.on_settings()
                return True
            except Exception as exc:
                self.lbl_status.setText(f"настройки: {exc}")
                return False

        def open_notifications(self, record_id=None):
            """Колокольчик/клик по тосту: вкладка «Уведомления» (+ запись)."""
            if self.notifications_widget is None:
                self.lbl_status.setText("центр уведомлений недоступен")
                return False
            try:
                self.show_tab(self._notifications_index)
            except Exception:
                pass
            if record_id:
                try:
                    self.notifications_widget.select_record(record_id)
                except Exception:
                    pass
            return True

        def set_unread_badge(self, count, color=None):
            """Бейдж непрочитанных: счётчик и цвет важного типа."""
            try:
                count = max(0, int(count or 0))
            except Exception:
                count = 0
            self._unread = count
            self.btn_bell.setText("🔔" if not count else f"🔔 {count}")
            style = theme.button_qss(small=True)
            if count and color:
                style += (f"QPushButton {{ color:{color};"
                          f" border:1px solid {color}; }}")
            self.btn_bell.setStyleSheet(style)
            self.btn_bell.setToolTip(
                f"Уведомления: {count} непрочитанных" if count
                else "Уведомления")
            return count

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
                ("Горячие клавиши — " + hints) if hints else "Warframe Overlay")
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
            """«□»/двойной клик: normal ↔ maximized в пределах рабочего стола."""
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
            if self.btn_maximize is None:
                return
            if self._mode == "maximized":
                self.btn_maximize.setToolTip(
                    "Восстановить окно (двойной клик по шапке)")
            else:
                self.btn_maximize.setToolTip(
                    "Развернуть в пределах рабочего стола "
                    "(двойной клик по шапке)")
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
                self.lbl_status.setText(f"окно входа: {exc}")
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


def build_updates_dialog():
    """Фабрика окна «Обновления» (F8 → пункт «Обновления»).

    Окно показывает текущую версию, кнопку «Проверить обновления», статус и
    «Обновить и перезапустить». Сеть/установка — только через переданные
    колбэки и в фоновом потоке, поэтому GUI не блокируется:

    * ``check_fn() -> dict`` (как ``launcher.check_for_update(..., apply=False)``);
    * ``apply_fn(manifest) -> dict`` (как ``launcher.apply_manifest``);
    * ``restart_fn()`` — перезапуск приложения после установки.
    """
    import threading

    from PyQt5.QtCore import Qt, pyqtSignal
    from PyQt5.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton,
                                 QVBoxLayout)

    try:
        import ui_theme as theme
    except Exception:                 # pragma: no cover - тема всегда рядом
        theme = None

    def qss():
        return theme.dialog_qss() if theme is not None else ""

    def button_qss(primary=False, danger=False):
        if theme is None:
            return ""
        return theme.button_qss(primary=primary, danger=danger)

    def color(name, default="#e6edf6"):
        if theme is None:
            return default
        return theme.PALETTE.get(name, default)

    class UpdatesDialog(QDialog):
        """Окно обновлений: текущая версия, проверка, установка и перезапуск."""

        checked = pyqtSignal(dict)
        applied = pyqtSignal(dict)

        def __init__(self, cfg=None, current_version="", check_fn=None,
                     apply_fn=None, restart_fn=None, parent=None):
            super().__init__(parent)
            self.cfg = dict(cfg or {})
            self.current_version = str(current_version or "—")
            self.check_fn = check_fn
            self.apply_fn = apply_fn
            self.restart_fn = restart_fn
            self.info = {}
            self._busy = False
            self.setWindowTitle("Обновления")
            self.setModal(True)
            self.setMinimumWidth(520)
            self.setStyleSheet(qss())
            try:
                import login_mode
                login_mode.register_dialog(self)     # B2: закроется при F9
            except Exception:
                pass

            layout = QVBoxLayout(self)
            layout.setContentsMargins(16, 14, 16, 12)
            layout.setSpacing(10)
            title = QLabel("<b>Обновления Warframe Price Overlay</b>")
            title.setStyleSheet(f"color:{color('text')}; font-size:14px;")
            self.lbl_current = QLabel(f"Текущая версия: {self.current_version}")
            self.lbl_current.setStyleSheet(
                f"color:{color('muted')}; font-size:11px;")
            self.lbl_status = QLabel(
                "Нажмите «Проверить обновления», чтобы узнать о новой версии.")
            self.lbl_status.setWordWrap(True)
            self.lbl_status.setStyleSheet(
                f"color:{color('text')}; font-size:11px;")
            self.lbl_notes = QLabel("")
            self.lbl_notes.setWordWrap(True)
            self.lbl_notes.setStyleSheet(
                f"color:{color('muted')}; font-size:10px;")
            layout.addWidget(title)
            layout.addWidget(self.lbl_current)
            layout.addWidget(self.lbl_status)
            layout.addWidget(self.lbl_notes)

            buttons = QHBoxLayout()
            buttons.setSpacing(8)
            self.btn_check = QPushButton("Проверить обновления")
            self.btn_check.setStyleSheet(button_qss(primary=True))
            self.btn_check.setCursor(Qt.PointingHandCursor)
            self.btn_check.clicked.connect(self.start_check)
            self.btn_update = QPushButton("Обновить и перезапустить")
            self.btn_update.setStyleSheet(button_qss())
            self.btn_update.setCursor(Qt.PointingHandCursor)
            self.btn_update.setEnabled(False)
            self.btn_update.clicked.connect(self.start_update)
            self.btn_close = QPushButton("Закрыть")
            self.btn_close.setStyleSheet(button_qss())
            self.btn_close.setCursor(Qt.PointingHandCursor)
            self.btn_close.clicked.connect(self.reject)
            buttons.addWidget(self.btn_check)
            buttons.addWidget(self.btn_update)
            buttons.addStretch(1)
            buttons.addWidget(self.btn_close)
            layout.addLayout(buttons)

            self.checked.connect(self._on_checked)
            self.applied.connect(self._on_applied)
            self.finished.connect(
                lambda _result=0: self._release_dialog())

        # ── служебное ─────────────────────────────────────────────────
        def _release_dialog(self):
            try:
                import login_mode
                login_mode.unregister_dialog(self)
            except Exception:
                pass

        def _run(self, function):
            """Выполнить сетевую/дисковую операцию в фоне (GUI не блокируем)."""
            thread = threading.Thread(target=function, daemon=True,
                                      name="wf-updates")
            thread.start()
            return thread

        def _set_status(self, text, warn=False):
            self.lbl_status.setText(str(text))
            self.lbl_status.setStyleSheet(
                f"color:{color('warning' if warn else 'text')};"
                " font-size:11px;")

        def _set_busy(self, busy):
            self._busy = bool(busy)
            self.btn_check.setEnabled(not busy)
            self.btn_update.setEnabled(
                not busy and bool(self.info.get("available")))

        # ── проверка ──────────────────────────────────────────────────
        def start_check(self):
            if self._busy:
                return False
            if not callable(self.check_fn):
                self._set_status("Проверка обновлений недоступна в этой сборке.",
                                 warn=True)
                return False
            self._set_busy(True)
            self.lbl_notes.setText("")
            self._set_status("Проверяю обновления…")

            def worker():
                try:
                    result = self.check_fn() or {}
                except Exception as exc:
                    result = {"checked": False, "available": False,
                              "error": f"{type(exc).__name__}: {exc}"}
                try:
                    self.checked.emit(dict(result))
                except RuntimeError:
                    pass

            self._run(worker)
            return True

        def _on_checked(self, result):
            self.info = dict(result or {})
            self._set_busy(False)
            if self.info.get("skipped"):
                self._set_status(f"Обновления не настроены: "
                                 f"{self.info['skipped']}.", warn=True)
                return
            if self.info.get("error"):
                self._set_status(f"Не удалось проверить обновления: "
                                 f"{self.info['error']}.", warn=True)
                return
            if self.info.get("available"):
                version = self.info.get("version") or "?"
                self._set_status(f"Доступна версия {version}. Нажмите "
                                 "«Обновить и перезапустить».")
                notes = str(self.info.get("notes") or "").strip()
                self.lbl_notes.setText(f"Что нового: {notes}" if notes else "")
                return
            current = self.info.get("current") or self.current_version
            self._set_status(f"Обновлений нет: у вас последняя версия "
                             f"({current}).")

        # ── установка + перезапуск ────────────────────────────────────
        def start_update(self):
            if self._busy:
                return False
            manifest = self.info.get("manifest")
            if not manifest:
                self._set_status("Сначала проверьте обновления.", warn=True)
                return False
            if not callable(self.apply_fn):
                self._set_status("Установка обновлений недоступна.", warn=True)
                return False
            self._set_busy(True)
            self._set_status(f"Скачиваю версию {self.info.get('version') or '?'}…")

            def worker():
                try:
                    result = self.apply_fn(manifest) or {}
                except Exception as exc:
                    result = {"ok": False,
                              "error": f"{type(exc).__name__}: {exc}"}
                try:
                    self.applied.emit(dict(result))
                except RuntimeError:
                    pass

            self._run(worker)
            return True

        def _on_applied(self, result):
            result = dict(result or {})
            if not result.get("ok") or result.get("error"):
                self._set_busy(False)
                self._set_status(f"Не удалось обновить: "
                                 f"{result.get('error') or 'неизвестная ошибка'}.",
                                 warn=True)
                return
            version = result.get("version") or self.info.get("version") or "?"
            self._set_status(f"Обновление {version} установлено — "
                             "перезапускаю приложение…")
            if callable(self.restart_fn):
                try:
                    self.restart_fn()
                except Exception as exc:
                    self._set_status(f"Обновление установлено, но перезапуск "
                                     f"не удался ({type(exc).__name__}). "
                                     "Закройте и запустите приложение вручную.",
                                     warn=True)

    return UpdatesDialog
