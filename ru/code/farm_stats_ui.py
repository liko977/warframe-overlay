# -*- coding: utf-8 -*-
"""Вкладка «Дроп» единого окна: статистика фарма и пулы реликвий.

Содержит две подвкладки:
  1. «Статистика» — карточки агрегатов ``farm_stats.FarmStatsCollector``
     (открыто реликвий, суммарная стоимость, лучший дроп, средний доход в час,
     список взятого лута) и кнопка «Сбросить статистику».
  2. «Пулы реликвий» — выбор тира (Lith/Meso/Neo/Axi/Requiem) и состояния
     (Intact/Exceptional/Flawless/Radiant), список реликвий по EV с шансами,
     ценами и бейджем «стоит ли открывать», кнопка «Обновить».

Сеть не блокирует GUI: пулы грузятся в фоне и показываются по сигналу.
Без лога/сети панель работает на кэше и показывает понятные сообщения.

Модуль не импортирует warframe/market_client: ``collector`` и ``pool``
передаются извне (см. ``build_drops_panel``).
"""
import datetime
import os
import time
import webbrowser

import ui_theme as theme
import ui_safe

try:                                    # 1.0.0: иконки валют (fallback — текст)
    import currency_icons as _currency_icons
except Exception:                       # pragma: no cover
    _currency_icons = None


def _money_text(amount, currency="platinum", cfg=None):
    """Сумма + иконка валюты (или «N p»/«N дк», если иконок нет)."""
    suffix = "дк" if str(currency).lower() == "ducats" else "p"
    if _currency_icons is None:
        try:
            return f"{int(round(float(amount)))} {suffix}"
        except Exception:
            return f"{amount} {suffix}"
    try:
        return _currency_icons.format_money(amount, currency=currency,
                                            cfg=cfg)
    except Exception:
        return f"{amount} {suffix}"


def _rate_text(value, cfg=None):
    """«38 p/ч» или «38 <icon>/ч» для карточки среднего дохода."""
    try:
        number = f"{int(round(float(value)))}"
    except Exception:
        number = str(value)
    if _currency_icons is None:
        return f"{number} p/ч"
    try:
        icon = _currency_icons.html("platinum", cfg, px=10)
        if str(icon).startswith("<img"):
            return f"<span>{number}&#160;{icon}/ч</span>"
        return f"{number} p/ч"
    except Exception:
        return f"{number} p/ч"



def _ui_error(context, exc=None, text=None):
    """1.0.0: в UI — понятный текст, детали исключения — только в app.log."""
    return ui_safe.human_error(context, exc, fallback=text)


#: Цвета бейджей тира эры — как в панели событий (events_api/warframe.py).
TIER_COLORS = {
    "Lith": "#b08d57", "Meso": "#8fb8d6", "Neo": "#d9a441",
    "Axi": "#a468c9", "Requiem": "#c0392b",
}

_MOD_TITLES = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win"}
_MOD_ORDER = ("ctrl", "alt", "shift", "win")


def _hotkey_helpers():
    try:
        import settings_ui
        return settings_ui.canon_hotkey, settings_ui.hotkey_variants
    except Exception:
        return (lambda value: str(value or "").strip().lower()), \
               (lambda value: [str(value or "").strip().lower()])


def _pretty_combo(combo, canonical=True):
    if canonical:
        key = _hotkey_helpers()[0](combo)
        combo = key
    else:
        combo = str(combo or "").strip().lower()
    parts = [part for part in combo.split("+") if part]
    if not parts:
        return ""
    key = parts[-1]
    mods = [part for part in parts[:-1] if part in _MOD_ORDER]
    if key.startswith("f") and key[1:].isdigit():
        key_label = key.upper()
    elif len(key) == 1:
        key_label = key.upper()
    else:
        key_label = key.capitalize()
    return "+".join([_MOD_TITLES[mod] for mod in
                     sorted(mods, key=_MOD_ORDER.index)] + [key_label])


def _parse_pick_keys(keys):
    if isinstance(keys, dict):
        keys = keys.get("pick_hotkeys", [])
    if isinstance(keys, str):
        keys = [part.strip() for part in keys.split(",") if part.strip()]
    if not isinstance(keys, (list, tuple)):
        return []
    canon, _variants = _hotkey_helpers()
    return [canon(key) for key in keys if str(key or "").strip()]


def _entry_utc(entry, session=None):
    """UTC-момент записи: utc/picked_utc/at, иначе started_utc + rel_time."""
    entry = dict(entry or {})
    session = dict(session or {})
    for key in ("picked_utc", "utc", "at"):
        try:
            value = float(entry.get(key) or 0)
        except Exception:
            value = 0.0
        if value > 0:
            return value
    try:
        rel = float(entry.get("rel_time"))
    except Exception:
        rel = None
    try:
        started = float(session.get("started_utc"))
    except Exception:
        started = None
    if rel is not None and started:
        return started + rel
    return None


def format_pick_time(entry, session=None):
    """Локальное время отметки «HH:MM» (никогда rel_time как часы)."""
    stamp = _entry_utc(entry, session)
    if stamp is None:
        return "--:--"
    try:
        return datetime.datetime.fromtimestamp(stamp).strftime("%H:%M")
    except Exception:
        return "--:--"


def pick_time_tooltip(entry, session=None):
    if _entry_utc(entry, session) is None:
        return "время неизвестно"
    return ""


def group_label(value):
    """«Сегодня» / «Вчера» / «ДД.ММ.ГГГГ» по локальной дате."""
    dt = None
    if isinstance(value, datetime.datetime):
        dt = value
    elif isinstance(value, datetime.date):
        dt = datetime.datetime.combine(value, datetime.time.min)
    elif isinstance(value, (int, float)):
        try:
            dt = datetime.datetime.fromtimestamp(float(value))
        except Exception:
            dt = None
    if dt is None:
        return ""
    today = datetime.date.today()
    if dt.date() == today:
        return "Сегодня"
    if dt.date() == today - datetime.timedelta(days=1):
        return "Вчера"
    return dt.strftime("%d.%m.%Y")


def format_pick_hint(keys):
    """Человеческая подсказка по фактическим pick_hotkeys."""
    combos = _parse_pick_keys(keys)
    if not combos:
        return ("Отметьте награду (клавиши не заданы — "
                "настройте в Настройках)")
    parsed = []
    for combo in combos:
        parts = combo.split("+")
        parsed.append((tuple(parts[:-1]), parts[-1], combo))
    # одинаковые модификаторы + подряд цифры → компактный диапазон
    if (len(parsed) >= 2
            and all(item[0] == parsed[0][0] for item in parsed)
            and all(item[1].isdigit() for item in parsed)):
        nums = [int(item[1]) for item in parsed]
        if nums == list(range(nums[0], nums[0] + len(nums))):
            head = "+".join(_MOD_TITLES[mod] for mod in
                            sorted(parsed[0][0], key=_MOD_ORDER.index))
            if len(parsed) > 4:
                return ", ".join(_pretty_combo(item[2])
                                 for item in parsed[:4]) + ", …"
            return (head + "+" if head else "") + f"{nums[0]}…{nums[-1]}"
    shown = [_pretty_combo(item[2]) for item in parsed]
    if len(shown) > 4:
        shown = shown[:4] + ["…"]
    return ", ".join(shown)


def pick_hint_tooltip(keys):
    """Полный список биндов + кириллические варианты для регистрации."""
    try:
        combos = _parse_pick_keys(keys)
    except Exception:
        combos = []
    if not combos:
        return "Клавиши не заданы — откройте Настройки → Клавиши"
    _canon, variants_fn = _hotkey_helpers()
    lines = ["Клавиши отметки награды:"]
    for combo in combos:
        try:
            variants = variants_fn(combo) or []
        except Exception:
            # 1.0.0: сбой клавиатурного слоя — без текста ошибки в UI
            variants = []
        ru = [variant for variant in variants
              if variant != combo and any("а" <= ch <= "я"
                                          for ch in str(variant).lower())]
        line = _pretty_combo(combo)
        if ru:
            line += " / " + ", ".join(
                _pretty_combo(value, canonical=False) for value in ru)
        lines.append(line)
    return ui_safe.tooltip("\n".join(lines),
                           fallback="Клавиши отметки награды", limit=300)


def _item_icon_label(cfg, item, size=24):
    """QLabel с ленивой иконкой предмета (или placeholder); None без Qt."""
    try:
        from PyQt5.QtCore import Qt
        from PyQt5.QtWidgets import QLabel
        import item_icons
        base = (cfg or {}).get("data_dir") or os.environ.get("WF_DATA_DIR")
        provider = item_icons.get_provider(
            base, enabled=bool((cfg or {}).get("show_item_icons", True)))
        label = QLabel()
        label.setFixedSize(int(size), int(size))
        label.setStyleSheet("background: transparent;")
        if not (cfg or {}).get("show_item_icons", True):
            label.setVisible(False)
            return label
        name = item.get("en_name") or item.get("name") or ""
        slug = str(item.get("slug") or "")
        game_ref = str(item.get("gameRef") or "")
        if not slug and not item.get("en_name"):
            # в пулах бывает только подпись без slug — не создаём пустой слот
            label.deleteLater()
            return None
        blueprint = item_icons.is_blueprint(name, slug)
        pix = provider.placeholder(size)
        if pix is not None:
            label.setPixmap(pix)
        if item_icons.part_style(slug, name).get("is_part"):
            bw, bh = item_icons.part_badge_size(
                slug, name, mini=int(size) < 28)
            if int(size) < 28:
                bw = max(10, min(bw, int(size) - 2))
                bh = max(8, min(bh, int(size) - 6))
            pix_badge = item_icons.part_badge_pixmap(slug, name, bw, bh)
            if pix_badge is not None:
                badge = QLabel(label)
                badge.setPixmap(pix_badge)
                badge.setText(item_icons.part_badge(slug, name))
                badge.setFixedSize(bw, bh)
                badge.move(2, max(0, int(size) - bh - 2))
                badge.setToolTip(item_icons.part_style(slug, name).get('type', ''))
                badge.show()
            label.setToolTip(item_icons.part_style(slug, name).get('type', ''))
        if not slug:
            return label

        def done(_key, pixmap):
            try:
                if pixmap is not None and not pixmap.isNull():
                    label.setPixmap(pixmap.scaled(
                        int(size), int(size), Qt.KeepAspectRatio,
                        Qt.SmoothTransformation))
            except Exception:
                pass

        provider.request_item(slug, item, callback=done, size=size)
        return label
    except Exception:
        return None


def build_drops_panel():
    """Фабрика панели вкладки «Дроп» (PyQt5 импортируется лениво)."""
    from PyQt5.QtCore import Qt, QTimer, pyqtSignal
    from PyQt5.QtGui import QColor, QFont
    from PyQt5.QtWidgets import (QButtonGroup, QComboBox, QFrame, QGridLayout,
                                 QHBoxLayout, QLabel, QPushButton, QScrollArea,
                                 QSizePolicy, QStackedWidget, QVBoxLayout,
                                 QWidget)
    import market_browser

    def make_font(size, bold=False):
        font = QFont(theme.FONT)
        font.setPixelSize(int(size))
        font.setBold(bool(bold))
        return font

    def _fmt_rel(rel):
        try:
            sec = float(rel)
        except Exception:
            return "—"
        if sec < 0:
            sec = 0.0
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = int(sec % 60)
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    def _fmt_price(p):
        if p is None:
            return "—"
        try:
            return f"{int(p)}p"
        except Exception:
            return "—"

    def _color_for_price(p):
        if p is None:
            return theme.PALETTE["muted"]
        return theme.PALETTE["platinum"]

    class _LootEmptyHint(QWidget):
        """Пустое состояние «Лута»: блок по центру, подпись в 2 строки."""

        def __init__(self, title_text, subtitle_text, cfg=None, parent=None):
            super().__init__(parent)
            from PyQt5.QtWidgets import QLabel, QSizePolicy
            self.setObjectName("lootEmpty")
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            self.title = QLabel(title_text, self)
            self.title.setAlignment(Qt.AlignHCenter)
            self.title.setWordWrap(True)
            self.title.setMaximumWidth(520)
            self.title.setFont(make_font(12, True))
            self.title.setStyleSheet(
                f"color:{theme.PALETTE['muted']}; background:transparent;")
            self.subtitle = QLabel(subtitle_text, self)
            self.subtitle.setAlignment(Qt.AlignHCenter)
            self.subtitle.setWordWrap(True)
            self.subtitle.setMaximumWidth(520)
            self.subtitle.setFont(make_font(10))
            self.subtitle.setStyleSheet(theme.muted_qss(10))
            self.subtitle.setToolTip(pick_hint_tooltip(
                (cfg or {}).get("pick_hotkeys", [])))
            self._relayout()

        def _label_height(self, label, width):
            try:
                needed = int(label.heightForWidth(max(1, width)) or 0)
            except Exception:
                needed = 0
            if needed <= 0:
                needed = label.sizeHint().height()
            return max(needed, label.fontMetrics().lineSpacing() + 2)

        def _relayout(self):
            width = min(520, max(80, self.width() - 20))
            title_h = self._label_height(self.title, width)
            sub_h = self._label_height(self.subtitle, width)
            # подпись — минимум две строки (бинды не обрезаются)
            sub_h = max(sub_h, 2 * self.subtitle.fontMetrics().lineSpacing() + 4)
            self.title.setFixedWidth(width)
            self.subtitle.setFixedWidth(width)
            self.title.setFixedHeight(title_h)
            self.subtitle.setFixedHeight(sub_h)
            gap = 4
            # заголовок — по центру свободной области (verify t42: ±10 px),
            # подпись — сразу под ним
            top = max(0, (self.height() - title_h) // 2)
            x = max(0, (self.width() - width) // 2)
            self.title.setGeometry(x, top, width, title_h)
            self.subtitle.setGeometry(x, top + title_h + gap, width, sub_h)

        def resizeEvent(self, event):       # noqa: N802 (Qt API)
            super().resizeEvent(event)
            self._relayout()

        def showEvent(self, event):         # noqa: N802 (Qt API)
            super().showEvent(event)
            self._relayout()

    class _DropsPanel(QWidget):
        pools_ready = pyqtSignal()
        prices_ready = pyqtSignal()
        stats_ready = pyqtSignal()

        def __init__(self, cfg, collector, pool, sell_handler=None):
            super().__init__()
            # 1.0.0 (t46): cfg по ссылке — переключатели применяются на лету
            self.cfg = cfg if isinstance(cfg, dict) else {}
            self.collector = collector
            self.pool = pool
            self.sell_handler = sell_handler
            self._listed_slugs = set()
            self._active = 0
            self._session_view = "current"
            self._loot_signature = None
            self._loading_pools = False
            self._pools_loaded = False      # ленивая загрузка данных (F6)
            self._pools_gen = 0             # поколение рендера: отмена старых цен
            self._stats_timer = None        # 1.0.0: одноразовый дебаунс обновления

            base = QFont(theme.FONT)
            base.setPixelSize(11)
            self._f_title = QFont(base)
            self._f_title.setPixelSize(13)
            self._f_title.setBold(True)
            self._f_big = QFont(base)
            self._f_big.setPixelSize(22)
            self._f_big.setBold(True)
            self._f_text = QFont(base)
            self._f_detail = QFont(base)
            self._f_detail.setPixelSize(10)
            self._f_badge = QFont(base)
            self._f_badge.setPixelSize(9)
            self._f_badge.setBold(True)

            outer = QVBoxLayout(self)
            outer.setContentsMargins(0, 0, 0, 0)
            outer.setSpacing(4)

            # ── подвкладки ──────────────────────────────────────────────
            self.tabs = QButtonGroup(self)
            self.tabs.setExclusive(True)
            tabs_host = QWidget()
            tabs_lay = QHBoxLayout(tabs_host)
            tabs_lay.setContentsMargins(0, 2, 0, 0)
            tabs_lay.setSpacing(4)
            self.tab_buttons = []
            for index, title in enumerate(("Статистика", "Пулы реликвий")):
                button = QPushButton(title)
                button.setCheckable(True)
                button.setFont(make_font(12, True))
                button.setStyleSheet(theme.tab_qss())
                button.setCursor(Qt.PointingHandCursor)
                button.clicked.connect(
                    lambda _=False, i=index: self.switch_tab(i))
                self.tabs.addButton(button, index)
                self.tab_buttons.append(button)
                tabs_lay.addWidget(button)
            tabs_lay.addStretch(1)
            outer.addWidget(tabs_host)

            self.stack = QStackedWidget()
            self.stack.setStyleSheet("background: transparent;")
            self.stack.addWidget(self._build_stats_page())
            self.stack.addWidget(self._build_pools_page())
            outer.addWidget(self.stack, 1)

            self.pools_ready.connect(self._render_pools)
            self.prices_ready.connect(self._render_pools)
            self.stats_ready.connect(self._render_stats)

            self.switch_tab(self._active)
            self._render_stats()
            # данные пулов НЕ грузим здесь — лениво при первом показе вкладки (F6)

        # ── страница «Статистика» (только выбранное) ────────────────────
        def _build_stats_page(self):
            page = QWidget()
            lay = QVBoxLayout(page)
            lay.setContentsMargins(0, 6, 0, 0)
            lay.setSpacing(4)

            head = QHBoxLayout()
            head.setSpacing(8)
            self.lbl_total = QLabel("Выбрано: 0 предметов · Итого: 0 p")
            self.lbl_total.setFont(self._f_title)
            self.lbl_total.setStyleSheet(
                f"color:{theme.PALETTE['platinum']}; background: transparent;")
            self.btn_reset = QPushButton("Сбросить")
            self.btn_reset.setStyleSheet(
                theme.button_qss(danger=True, small=True))
            self.btn_reset.setCursor(Qt.PointingHandCursor)
            self.btn_reset.clicked.connect(self._reset_stats)
            head.addWidget(self.lbl_total)
            head.addStretch(1)
            head.addWidget(self.btn_reset)
            lay.addLayout(head)

            # 1.0.0 (t35): Текущий · Прошлый · Позапрошлый
            self.session_tabs_host = QWidget()
            tabs_lay = QHBoxLayout(self.session_tabs_host)
            tabs_lay.setContentsMargins(0, 0, 0, 0)
            tabs_lay.setSpacing(4)
            self.session_tabs = {}
            for view, text in (("current", "Текущий"),
                               ("prev", "Прошлый"),
                               ("prev2", "Позапрошлый")):
                button = QPushButton(text)
                button.setCheckable(True)
                button.setFont(make_font(10, True))
                button.setCursor(Qt.PointingHandCursor)
                button.setStyleSheet(
                    "QPushButton { background: transparent; border: none;"
                    " border-bottom: 2px solid transparent;"
                    " color:#8FA3BA; padding: 2px 8px; }"
                    "QPushButton:checked { color:#E7F1FF;"
                    " border-bottom: 2px solid #4A7CA6; }"
                    "QPushButton:disabled { color:#4E5C6E; }")
                button.clicked.connect(
                    lambda _=False, v=view: self._switch_session_view(v))
                tabs_lay.addWidget(button)
                self.session_tabs[view] = button
            tabs_lay.addStretch(1)
            self.session_tabs["current"].setChecked(True)
            lay.addWidget(self.session_tabs_host)

            # 1.0.0: компактные показатели из EE.log/выбранного (≤72 px)
            self.stats_block = self._build_stats_block()
            lay.addWidget(self.stats_block)

            loot_head = self._make_section("Лут")
            loot_head.setContentsMargins(0, 2, 0, 0)
            lay.addWidget(loot_head)
            self.loot_host = QWidget()
            self.loot_host.setSizePolicy(QSizePolicy.Expanding,
                                         QSizePolicy.Preferred)
            self.loot_lay = QVBoxLayout(self.loot_host)
            self.loot_lay.setContentsMargins(0, 0, 0, 0)
            self.loot_lay.setSpacing(3)
            self.loot_scroll = QScrollArea()
            self.loot_scroll.setWidgetResizable(True)
            self.loot_scroll.setFrameShape(QFrame.NoFrame)
            self.loot_scroll.setMinimumHeight(140)
            self.loot_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
            self.loot_scroll.setStyleSheet(
                "QScrollArea { background: transparent; }")
            self.loot_scroll.setWidget(self.loot_host)
            lay.addWidget(self.loot_scroll, 1)

            return page

        def _make_section(self, text):
            lbl = QLabel(text.upper())
            lbl.setFont(self._f_detail)
            lbl.setStyleSheet(theme.section_qss())
            return lbl

        # ── 1.0.0: карточки показателей «Дропа» (≤72 px) ────────────────
        #: уже этой ширины панель переключает 4 карточки в 2×2 (окно ≥900)
        NARROW_WIDTH = 960

        def _build_stats_block(self):
            block = QWidget()
            block.setMaximumHeight(72)
            block.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self._stats_grid = QGridLayout(block)
            self._stats_grid.setContentsMargins(0, 0, 0, 0)
            self._stats_grid.setSpacing(4)
            self.stats_cards = {}
            self._stats_rows = 0
            specs = (("relics", "Открыто реликвий"),
                     ("last_drop", "Последний дроп"),
                     ("selected", "Стоимость выбранного"),
                     ("avg", "Средний доход"))
            for key, title in specs:
                card = QFrame()
                card.setObjectName(f"statCard_{key}")
                card.setStyleSheet(theme.row_qss(name=f"statCard_{key}",
                                                 radius=4))
                card.setMaximumHeight(32)
                line = QHBoxLayout(card)
                line.setContentsMargins(8, 1, 8, 1)
                line.setSpacing(6)
                caption = QLabel(title)
                caption.setFont(make_font(9))
                caption.setStyleSheet(theme.muted_qss(9))
                caption.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                icon_host = QWidget()
                icon_host.setFixedSize(22, 22)
                icon_lay = QHBoxLayout(icon_host)
                icon_lay.setContentsMargins(0, 0, 0, 0)
                icon_lay.setSpacing(0)
                value = QLabel("—")
                value.setFont(make_font(12, True))
                value.setStyleSheet(
                    f"color:{theme.PALETTE['text']}; background: transparent;")
                line.addWidget(caption)
                line.addWidget(icon_host)
                line.addStretch(1)
                line.addWidget(value)
                self.stats_cards[key] = {"card": card, "caption": caption,
                                         "value": value, "icon_host": icon_host,
                                         "icon_lay": icon_lay}
            self._layout_stats_cards()
            return block

        def _layout_stats_cards(self, width=None):
            """1×4 на широком окне, 2×2 на узком (высота блока ≤72 px).

            ``width`` можно передать явно (тесты/предпросмотр): обычный путь —
            текущая ширина панели.
            """
            try:
                grid = self._stats_grid
            except AttributeError:
                return
            if width is None:
                width = self.width()
            narrow = bool(width) and width < self.NARROW_WIDTH
            columns = 2 if narrow else 4
            if getattr(self, "_stats_columns", None) == columns:
                return
            for card in (self.stats_cards or {}).values():
                grid.removeWidget(card["card"])
            for index in range(4):
                grid.setColumnStretch(index, 0)
                grid.setRowStretch(index, 0)
            keys = ("relics", "last_drop", "selected", "avg")
            for index, key in enumerate(keys):
                row, column = divmod(index, columns)
                grid.addWidget(self.stats_cards[key]["card"], row, column)
            for column in range(columns):
                grid.setColumnStretch(column, 1)
            self._stats_columns = columns
            self._stats_rows = 1 if columns == 4 else 2

        def resizeEvent(self, event):
            super().resizeEvent(event)
            self._layout_stats_cards()
            hint = getattr(self, "empty_hint_wrap", None)
            if hint is not None:
                try:
                    hint._relayout()
                except Exception:
                    pass

        def _drop_stats(self):
            """Показатели выбранного сеанса (1.0.0, t35)."""
            if self.collector is None:
                return {}
            try:
                getter = getattr(self.collector, "session_view", None)
                if callable(getter):
                    info = getter(self._session_view) or {}
                    try:
                        has_session = bool(self.collector.current_session())
                    except Exception:
                        has_session = False
                    info["log_available"] = bool(
                        getattr(self.collector, "path", None)) or has_session
                    return info
                getter = getattr(self.collector, "drop_stats", None)
                if callable(getter):
                    return getter() or {}
            except Exception:
                return {}
            return {}

        def _switch_session_view(self, view):
            view = str(view or "current")
            if view not in ("current", "prev", "prev2"):
                view = "current"
            self._session_view = view
            for name, button in (self.session_tabs or {}).items():
                button.setChecked(name == view)
            self._render_stats()

        def _render_session_tabs(self):
            """Доступность сеансов: прошлые — только если они есть."""
            tabs = self.session_tabs or {}
            count = 0
            if self.collector is not None:
                try:
                    completed = self.collector.completed_sessions(5)
                    count = len(completed)
                except Exception:
                    count = 0
            if self._session_view == "prev2" and count < 2:
                self._session_view = "current"
            elif self._session_view == "prev" and count < 1:
                self._session_view = "current"
            for name, button in tabs.items():
                enabled = (name == "current"
                           or (name == "prev" and count >= 1)
                           or (name == "prev2" and count >= 2))
                button.setEnabled(enabled)
                button.setChecked(name == self._session_view)
                if not enabled:
                    button.setToolTip("сеансов ещё не было")
            reset = getattr(self, "btn_reset", None)
            if reset is not None:
                current = self._session_view == "current"
                reset.setEnabled(current)
                reset.setToolTip("сброс только для текущего сеанса"
                                 if not current else "Сбросить текущий сеанс")

        @staticmethod
        def _short(text, limit=30):
            text = str(text or "").strip()
            if len(text) <= limit:
                return text
            return text[:max(1, limit - 1)].rstrip() + "…"

        def _render_stats_cards(self):
            cards = self.stats_cards
            info = self._drop_stats()
            log_ok = bool(info.get("log_available")) and bool(self.collector)
            # 1) открыто реликвий: сессия / всего, время последней
            relics = cards.get("relics")
            if relics is not None:
                if info and log_ok:
                    session = int(info.get("relics") or 0)
                    relics["value"].setText(str(session))
                    when = info.get("last_relic_at")
                    try:
                        when_text = datetime.datetime.fromtimestamp(
                            float(when)).strftime("%H:%M") if when else "—"
                    except Exception:
                        when_text = "—"
                    relics["value"].setStyleSheet(
                        f"color:{theme.PALETTE['text']};"
                        " background: transparent;")
                    relics["card"].setToolTip(
                        f"Открыто реликвий за сеанс: {session}; "
                        f"последняя — {when_text}\n(из лога игры EE.log)")
                else:
                    relics["value"].setText("—")
                    relics["value"].setStyleSheet(
                        f"color:{theme.PALETTE['muted']};"
                        " background: transparent;")
                    relics["card"].setToolTip("лог игры не найден")
            # 2) последний дроп: из ПОСЛЕДНЕЙ отметки Alt+1…4 (не из EE.log)
            last = cards.get("last_drop")
            if last is not None:
                icon_lay = last["icon_lay"]
                while icon_lay.count():
                    item = icon_lay.takeAt(0)
                    widget = item.widget()
                    if widget is not None:
                        widget.deleteLater()
                pick = (info or {}).get("last_pick")
                if pick:
                    name = str(pick.get("picked_name")
                               or pick.get("name") or "")
                    try:
                        import display_name as _dn
                        name = _dn.canonical_name(
                            str(pick.get("slug") or ""), name,
                            pick.get("path") or "")
                    except Exception:
                        pass
                    price = pick.get("picked_price")
                    if price is None:
                        price = pick.get("price")
                    price_text = (_money_text(price, cfg=self.cfg)
                                  if isinstance(price, (int, float))
                                  and price else "—")
                    when = format_pick_time(
                        pick, (info or {}).get("session"))
                    import item_icons
                    chip = item_icons.part_chip_html(
                        str(pick.get("slug") or ""), name)
                    last["value"].setText(
                        f"{self._short(name, 20)} {chip} · {price_text} · {when}")
                    last["value"].setStyleSheet(
                        f"color:{theme.PALETTE['text']};"
                        " background: transparent;")
                    tip = (f"Последняя отметка (Alt+1…4): {name} — "
                           f"{price_text}, {when}")
                    extra = pick_time_tooltip(
                        pick, (info or {}).get("session"))
                    if extra:
                        tip += "\n" + extra
                    last["card"].setToolTip(tip)
                    try:
                        icon = _item_icon_label(self.cfg, pick, 22)
                    except Exception:
                        icon = None
                    if icon is not None:
                        icon_lay.addWidget(icon)
                else:
                    last["value"].setText("—")
                    last["value"].setAlignment(Qt.AlignCenter)
                    last["value"].setStyleSheet(
                        f"color:{theme.PALETTE['muted']};"
                        " background: transparent;")
                    last["card"].setToolTip(
                        "нет отметок — отметьте награду Alt+1…4")
            # 3) сумма выбранного (Alt+1…4)
            selected = cards.get("selected")
            if selected is not None:
                count = int((info or {}).get("selected_count") or 0)
                total = int((info or {}).get("selected_sum") or 0)
                selected["value"].setText(
                    _money_text(total, cfg=self.cfg) if count else "—")
                selected["value"].setStyleSheet(
                    f"color:{theme.PALETTE['platinum'] if count else theme.PALETTE['muted']};"
                    " background: transparent;")
                selected["card"].setToolTip(
                    f"Выбрано наград: {count} · сумма {total}p")
            # 4) средний доход (hours < 0.05 → «нет данных»)
            avg = cards.get("avg")
            if avg is not None:
                hours = float((info or {}).get("hours") or 0.0)
                if hours < 0.05:
                    avg["value"].setText("—")
                    avg["value"].setStyleSheet(
                        f"color:{theme.PALETTE['muted']};"
                        " background: transparent;")
                    avg["card"].setToolTip("нет данных о времени фарма")
                else:
                    value = float((info or {}).get("avg_per_hour") or 0.0)
                    avg["value"].setText(_rate_text(value, self.cfg))
                    avg["value"].setStyleSheet(
                        f"color:{theme.PALETTE['success']};"
                        " background: transparent;")
                    avg["card"].setToolTip(
                        f"Средний доход по выбранному: {value:.0f} p/ч "
                        f"за {hours:.2f} ч")

        def schedule_refresh(self):
            """Обновить показатели после события (не таймер каждую секунду)."""
            if getattr(self, "_stats_timer", None) is not None:
                return False
            try:
                timer = QTimer(self)
            except Exception:
                self._render_stats()
                return False
            timer.setSingleShot(True)
            timer.setInterval(150)
            timer.timeout.connect(self._on_stats_timer)
            self._stats_timer = timer
            timer.start()
            return True

        def _on_stats_timer(self):
            self._stats_timer = None
            self._render_stats()

        def _update_stats_labels(self):
            info = self._drop_stats()
            count = int(info.get("selected_count") or 0)
            total = int(info.get("selected_sum") or 0)
            money = _money_text(total, cfg=self.cfg)
            prefix = ""
            if self._session_view != "current":
                try:
                    prefix = self.collector.session_label(
                        self._session_view) + " · "
                except Exception:
                    prefix = ""
            self.lbl_total.setText(
                f"{prefix}Выбрано: {count} предметов · Итого: {money}")
            self.lbl_total.setToolTip(theme.currency_tooltip("platinum"))

        def _render_stats(self):
            self._render_session_tabs()
            self._update_stats_labels()
            self._render_stats_cards()
            self._render_loot()

        def _pick_keys(self):
            raw = (self.cfg or {}).get("pick_hotkeys", [])
            if isinstance(raw, str):
                raw = [part.strip() for part in raw.split(",")
                       if part.strip()]
            if not isinstance(raw, (list, tuple)):
                raw = []
            return [key for key in raw if str(key or "").strip()]

        def _pick_hint(self):
            hint = format_pick_hint(self._pick_keys())
            if hint.startswith("Отметьте награду"):
                return hint
            return "Лут появится, когда отметите награду: " + hint

        def _pick_tooltip(self):
            return pick_hint_tooltip(self._pick_keys())

        def update_pick_hint(self):
            """Смена биндов в настройках — перечитать подсказку сразу."""
            self._render_loot()

        @staticmethod
        def _day_label(ts):
            return group_label(ts)

        def _render_loot(self):
            try:
                self.loot_host.setMinimumHeight(0)
            except Exception:
                pass
            while self.loot_lay.count():
                item = self.loot_lay.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()
            loot = []
            session = None
            history = self._session_view != "current"
            if self.collector is not None:
                try:
                    if history:
                        view = self.collector.session_view(self._session_view)
                        session = (view or {}).get("session")
                        for mark in (view or {}).get("marks") or []:
                            loot.append({
                                "picked_name": mark.get("name"),
                                "picked_price": mark.get("price"),
                                "picked_utc": mark.get("at"),
                                "slug": mark.get("slug"),
                                "history": True,
                            })
                    else:
                        view = self.collector.session_view("current")
                        session = (view or {}).get("session")
                        for mark in (view or {}).get("marks") or []:
                            loot.append({
                                "picked_name": mark.get("name"),
                                "picked_price": mark.get("price"),
                                "picked_utc": mark.get("at"),
                                "slug": mark.get("slug"),
                            })
                except Exception:
                    loot = []
            # 1.0.0 (t37): канонические русские имена, дубли → ×N
            try:
                import display_name as _dn
                grouped = {}
                for drop in loot:
                    full = _dn.canonical_name(
                        str(drop.get("slug") or ""),
                        drop.get("picked_name") or "", drop.get("path") or "")
                    drop["picked_name"] = full
                    key = full.casefold()
                    at = _entry_utc(drop, session) or 0.0
                    entry = grouped.get(key)
                    if entry is None:
                        entry = {"drop": drop, "count": 0, "at": at}
                        grouped[key] = entry
                    entry["count"] += 1
                    if at >= entry["at"]:
                        entry["drop"] = drop
                        entry["at"] = at
                loot = sorted((entry["drop"] for entry in grouped.values()),
                              key=lambda d: (_entry_utc(d, session) or 0.0),
                              reverse=True)
                counts = {str(entry["drop"].get("picked_name")): entry["count"]
                          for entry in grouped.values()}
            except Exception:
                counts = {}
            signature = (self._session_view,
                         tuple((d.get("picked_name"),
                                d.get("picked_price"),
                                d.get("picked_utc"),
                                counts.get(str(d.get("picked_name")), 1))
                               for d in loot))
            if signature == getattr(self, "_loot_signature", None) \
                    and self.loot_lay.count():
                return
            self._loot_signature = signature
            self.empty_hint_wrap = None
            self.empty_hint = None
            self.empty_hint_sub = None
            if not loot:
                if history:
                    title_text = "Нет отметок"
                    subtitle_text = "в этом сеансе не было выбранного лута"
                else:
                    title_text = "Лут пуст"
                    hint = self._pick_hint()
                    if ": " in hint:
                        head, binds = hint.rsplit(": ", 1)
                        subtitle_text = head + "\n" + binds
                    else:
                        subtitle_text = hint
                wrap = _LootEmptyHint(title_text, subtitle_text,
                                      cfg=self.cfg)
                self.loot_lay.addWidget(wrap, 1)
                self.empty_hint_wrap = wrap
                self.empty_hint = wrap.title
                self.empty_hint_sub = wrap.subtitle
                return
            current_day = None
            for drop in loot:
                day = group_label(_entry_utc(drop, session))
                if history and day != current_day:
                    current_day = day
                    head = QLabel(day)
                    head.setFont(self._f_detail)
                    head.setStyleSheet(theme.section_qss())
                    self.loot_lay.addWidget(head)
                row = QFrame()
                row.setObjectName("lootRow")
                row.setStyleSheet(theme.row_qss(name="lootRow", radius=4))
                line = QHBoxLayout(row)
                line.setContentsMargins(8, 3, 8, 3)
                line.setSpacing(7)
                when = format_pick_time(drop, session)
                t = QLabel(when)
                t.setFont(self._f_detail)
                t.setFixedWidth(42)
                t.setStyleSheet(theme.muted_qss(10))
                t.setToolTip(pick_time_tooltip(drop, session) or when)
                icon = _item_icon_label(self.cfg, drop, 24)
                full_name = str(drop.get("picked_name") or "")
                if not full_name:
                    try:
                        import display_name as _dn
                        full_name = _dn.UNKNOWN
                    except Exception:
                        full_name = "Неизвестный предмет"
                name = QLabel(full_name)
                name.setFont(self._f_text)
                name.setStyleSheet(
                    f"color:{theme.PALETTE['text']}; background: transparent;")
                try:
                    from PyQt5.QtGui import QFontMetrics
                    metrics = QFontMetrics(name.font())
                    elided = metrics.elidedText(full_name, Qt.ElideRight, 360)
                    name.setText(elided)
                    name.setMaximumWidth(370)
                    price_tip = drop.get("picked_price")
                    name.setToolTip(
                        f"{full_name}\nslug: {drop.get('slug') or '—'}"
                        f" · {price_tip if price_tip is not None else '—'} p")
                except Exception:
                    name.setToolTip(full_name)
                drop["_full_name"] = full_name
                price_value = drop.get("picked_price")
                price_text = (_money_text(price_value, cfg=self.cfg)
                              if isinstance(price_value, (int, float))
                              and price_value else "—")
                price = QLabel(price_text)
                price.setFont(make_font(11, True))
                price.setStyleSheet(
                    f"color:{theme.PALETTE['platinum'] if price_text != '—' else theme.PALETTE['muted']};"
                    " background: transparent;")
                if drop.get("picked_tradable") is False or \
                        drop.get("tradable") is False:
                    price.setToolTip("не торгуется на warframe.market")
                else:
                    price.setToolTip(theme.currency_tooltip("platinum"))
                qty = QLabel("×%d" % int(counts.get(
                    str(drop.get("_full_name") or drop.get("picked_name")),
                    1)))
                qty.setFont(self._f_detail)
                qty.setStyleSheet(theme.muted_qss(10))
                line.addWidget(t)
                if icon is not None:
                    line.addWidget(icon, 0, Qt.AlignVCenter)
                line.addWidget(name, 1)
                import item_icons
                drop_chip = item_icons.part_chip_label(
                    str(drop.get("slug") or ""), full_name, row)
                if drop_chip is not None:
                    line.addWidget(drop_chip)
                line.addWidget(price)
                line.addWidget(qty)
                market = QPushButton("↗")
                market.setFont(make_font(9, True))
                market.setStyleSheet(theme.button_qss(small=True))
                market.setCursor(Qt.PointingHandCursor)
                market.setFixedSize(22, 22)
                slug = str((drop or {}).get("slug") or "")
                market.setEnabled(bool(slug))
                market.setToolTip("Открыть на маркете")
                market.clicked.connect(
                    lambda _=False, s=slug: self._open_market(s))
                line.addWidget(market)
                if history:
                    sell_button = QPushButton("Продать")
                    sell_button.setFont(make_font(9, True))
                    sell_button.setStyleSheet(theme.button_qss(small=True))
                    sell_button.setFixedHeight(22)
                    sell_button.setEnabled(False)
                    sell_button.setToolTip(
                        "доступно только для текущего сеанса")
                    line.addWidget(sell_button)
                else:
                    line.addWidget(self._sell_button(drop))
                self.loot_lay.addWidget(row)
            self._fit_loot_height()
            try:
                QTimer.singleShot(0, self._fit_loot_height)
            except Exception:
                pass
            try:
                import item_icons
                provider = item_icons.get_provider(
                    self.cfg.get("data_dir"),
                    enabled=bool(self.cfg.get("show_item_icons", True)),
                    auto_download=False)
                keys = [str((d or {}).get("slug") or "") for d in loot]
                self._icon_preloader = item_icons.IconPreloader(
                    provider, logger=lambda text: None)
                self._icon_preloader.start(keys, 0, min(9, len(keys) - 1))
            except Exception:
                pass

        def _fit_loot_height(self):
            """Минимальная высота содержимого = суммарный sizeHint строк."""
            try:
                self.loot_lay.invalidate()
                self.loot_lay.activate()
                hint = self.loot_lay.totalSizeHint().height()
                if hint <= 0:
                    hint = self.loot_lay.sizeHint().height()
                self.loot_host.setMinimumHeight(max(0, int(hint)))
            except Exception:
                pass

        def _sell_status(self, drop):
            # 1.0.0: неторгуемый предмет продать нельзя
            if (drop or {}).get("picked_tradable") is False or \
                    (drop or {}).get("tradable") is False:
                return {"enabled": False, "label": "Продать",
                        "tooltip": "предмет не продаётся"}
            slug = str((drop or {}).get("slug") or "")
            if slug and slug in self._listed_slugs:
                return {"enabled": True, "label": "Изменить цену",
                        "tooltip": "Изменить цену своего ордера",
                        "listed": True}
            if not callable(self.sell_handler):
                return {"enabled": False, "label": "Продать",
                        "tooltip": "продажа недоступна"}
            try:
                result = self.sell_handler(drop, "status")
                if isinstance(result, dict):
                    # 1.0.0: подсказка кнопки — только человеческий текст
                    return self._safe_sell_info(result)
                return {}
            except Exception as exc:
                _ui_error("продажа (статус)", exc)
                return {"enabled": False, "label": "Продать",
                        "tooltip": "продажа недоступна — подробности в app.log"}

        @staticmethod
        def _safe_sell_info(info):
            try:
                result = dict(info or {})
            except Exception:
                return {}
            tooltip = ui_safe.tooltip(result.get("tooltip"),
                                      fallback="Продать", limit=200)
            result["tooltip"] = tooltip or "Продать"
            result["label"] = ui_safe.clean(result.get("label"),
                                            fallback="Продать", limit=40)
            return result

        def _sell_button(self, drop):
            info = self._sell_status(drop)
            button = QPushButton(str(info.get("label") or "Продать"))
            button.setFont(make_font(9, True))
            button.setStyleSheet(theme.button_qss(small=True))
            button.setCursor(Qt.PointingHandCursor)
            button.setToolTip(str(info.get("tooltip") or "Продать"))
            button.setEnabled(bool(info.get("enabled", True)))
            button.setFixedHeight(22)
            button.clicked.connect(lambda _=False, d=drop:
                                   self._on_loot_sell(d))
            return button

        def _open_market(self, slug):
            url = f"https://warframe.market/items/{slug}" if slug else ""
            if url:
                try:
                    webbrowser.open(url)
                except Exception:
                    pass

        def _on_loot_sell(self, drop):
            if not callable(self.sell_handler):
                return
            try:
                result = self.sell_handler(drop, "open")
            except Exception as exc:
                _ui_error("продажа (окно)", exc)
                self.lbl_total.setToolTip(
                    "продажа недоступна — подробности в app.log")
                return
            if isinstance(result, dict) and result.get("ok"):
                slug = str((drop or {}).get("slug") or "")
                if slug:
                    self._listed_slugs.add(slug)
                self._render_loot()

        def _reset_confirm_dialog(self):
            Confirm = market_browser.build_market_browser().ConfirmDialog
            return Confirm(
                self, "Очистить выбранный лут и историю взятых наград?",
                confirm_text="Сбросить", cancel_text="Отмена",
                danger=True, title="Сбросить лут")

        def _reset_stats(self):
            if self.collector is None:
                return
            try:
                accepted = self._reset_confirm_dialog().exec_() == 1
            except Exception:
                return
            if not accepted:
                return
            try:
                self.collector.reset()
            except Exception:
                pass
            self._listed_slugs.clear()
            self._session_view = "current"
            self._loot_signature = None
            self._render_stats()

        # ── страница «Пулы реликвий» ────────────────────────────────────
        def _build_pools_page(self):
            page = QWidget()
            lay = QVBoxLayout(page)
            lay.setContentsMargins(0, 6, 0, 0)
            lay.setSpacing(8)

            head = QHBoxLayout()
            head.setSpacing(8)
            title = QLabel("Пулы реликвий")
            title.setFont(self._f_title)
            title.setStyleSheet(
                f"color:{theme.PALETTE['text']}; background: transparent;")
            self.combo_tier = QComboBox()
            self.combo_state = QComboBox()
            for combo in (self.combo_tier, self.combo_state):
                combo.setStyleSheet(theme.input_qss())
                combo.setCursor(Qt.PointingHandCursor)
            self.combo_tier.currentTextChanged.connect(self._on_tier_changed)
            self.combo_state.currentTextChanged.connect(self._on_state_changed)
            self.btn_pool_refresh = QPushButton("⟳ Обновить")
            self.btn_pool_refresh.setStyleSheet(theme.button_qss(small=True))
            self.btn_pool_refresh.setCursor(Qt.PointingHandCursor)
            self.btn_pool_refresh.clicked.connect(
                lambda: self._start_pool_load(force=True))
            self.lbl_pool_status = QLabel("")
            self.lbl_pool_status.setFont(self._f_detail)
            self.lbl_pool_status.setStyleSheet(theme.status_qss())
            head.addWidget(title)
            head.addWidget(self.combo_tier)
            head.addWidget(self.combo_state)
            head.addStretch(1)
            head.addWidget(self.lbl_pool_status)
            head.addWidget(self.btn_pool_refresh)
            lay.addLayout(head)

            self.pool_host = QWidget()
            self.pool_lay = QVBoxLayout(self.pool_host)
            self.pool_lay.setContentsMargins(0, 0, 0, 0)
            self.pool_lay.setSpacing(6)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setStyleSheet("QScrollArea { background: transparent; }")
            scroll.setWidget(self.pool_host)
            lay.addWidget(scroll, 1)
            return page

        def _populate_controls(self):
            tiers = []
            states = []
            if self.pool is not None:
                try:
                    tiers = self.pool.tiers()
                except Exception:
                    tiers = []
                try:
                    states = list(self.pool.states())
                except Exception:
                    states = []
            cur_tier = self.combo_tier.currentText()
            cur_state = self.combo_state.currentText()
            self.combo_tier.blockSignals(True)
            self.combo_state.blockSignals(True)
            self.combo_tier.clear()
            self.combo_state.clear()
            self.combo_tier.addItems(tiers or ["Lith"])
            self.combo_state.addItems(states or ["Intact"])
            if cur_tier and cur_tier in tiers:
                self.combo_tier.setCurrentText(cur_tier)
            if cur_state and cur_state in states:
                self.combo_state.setCurrentText(cur_state)
            self.combo_tier.blockSignals(False)
            self.combo_state.blockSignals(False)

        def _on_tier_changed(self, _text):
            self._render_pools()

        def _on_state_changed(self, _text):
            self._render_pools()

        def _start_pool_load(self, force=False):
            if self._loading_pools or self.pool is None:
                if self.pool is None:
                    self.lbl_pool_status.setText("пулы недоступны (нет модуля)")
                return
            if force:
                # ⟳ перезагружает и цены: сброс кэша → фоновая дозагрузка
                try:
                    self.pool.clear_price_cache()
                except Exception:
                    pass
            self._loading_pools = True
            self.lbl_pool_status.setText("загружаю пулы…")

            def worker():
                try:
                    self.pool.load(force=force)
                except Exception:
                    pass
                finally:
                    self._loading_pools = False
                    self.pools_ready.emit()

            import threading
            threading.Thread(target=worker, daemon=True,
                             name="wf-relic-pool-load").start()

        def _render_pools(self):
            self._pools_gen += 1
            self._populate_controls()
            while self.pool_lay.count():
                item = self.pool_lay.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.deleteLater()
            if self.pool is None:
                self.lbl_pool_status.setText("пулы недоступны (нет модуля)")
                self.pool_lay.addStretch(1)
                return
            tier = self.combo_tier.currentText()
            state = self.combo_state.currentText()
            try:
                # только кэш цен: сеть в GUI-потоке не трогаем (F2)
                evals = self.pool.evaluate(tier, state, resolve=False)
            except Exception:
                evals = []
            try:
                status = self.pool.status_text()
            except Exception:
                status = ""
            self.lbl_pool_status.setText(status or "нет данных")
            if not evals:
                empty = QLabel("Нет данных по этому тиру/состоянию. "
                               "Нажмите «Обновить», если нет сети — кэш.")
                empty.setFont(self._f_detail)
                empty.setStyleSheet(theme.muted_qss(11))
                empty.setWordWrap(True)
                self.pool_lay.addWidget(empty)
                self.pool_lay.addStretch(1)
                return
            for e in evals:
                self.pool_lay.addWidget(self._make_relic_card(e))
            self.pool_lay.addStretch(1)
            # недостающие цены резолвим в фоне и обновляем по сигналу
            slugs = self._pending_slugs(evals)
            if slugs:
                self._resolve_prices_background(slugs, self._pools_gen)

        @staticmethod
        def _pending_slugs(evals):
            """Слаги без свежей цены (нужно догрузить в фоне)."""
            slugs = []
            for e in evals or []:
                for rw in e.get("rewards") or []:
                    if rw.get("slug") and not rw.get("price_cached"):
                        if rw["slug"] not in slugs:
                            slugs.append(rw["slug"])
            return slugs

        def _resolve_prices_background(self, slugs, gen):
            if self.pool is None or not slugs:
                return

            def worker():
                try:
                    self.pool.resolve_slugs(slugs)
                except Exception:
                    pass
                if gen == self._pools_gen:
                    self.prices_ready.emit()

            import threading
            threading.Thread(target=worker, daemon=True,
                             name="wf-relic-pool-price").start()

        def _make_relic_card(self, e):
            card = QFrame()
            card.setObjectName("relicCard")
            card.setStyleSheet(theme.card_qss("relicCard", 6))
            lay = QVBoxLayout(card)
            lay.setContentsMargins(12, 8, 12, 8)
            lay.setSpacing(4)

            top = QHBoxLayout()
            top.setSpacing(8)
            tier_badge = QLabel(e.get("tier") or "")
            tier_badge.setFont(self._f_badge)
            tier_badge.setStyleSheet(
                theme.badge_qss(TIER_COLORS.get(e.get("tier"), theme.PALETTE["border"])))
            name = QLabel(e.get("relic_name") or "?")
            name.setFont(make_font(12, True))
            name.setStyleSheet(
                f"color:{theme.PALETTE['text']}; background: transparent;")
            badge = e.get("badge") or ""
            badge_text = {"best": "🔥 лучшее", "good": "выгодно", "": ""}.get(badge, "")
            badge_color = theme.PALETTE["warning"] if badge == "best" else theme.PALETTE["success"]
            b_lbl = QLabel(badge_text)
            b_lbl.setFont(self._f_badge)
            b_lbl.setStyleSheet(theme.badge_qss(badge_color))
            ev_lbl = QLabel(f"{e.get('ev', 0):.1f} p")
            ev_lbl.setFont(make_font(13, True))
            ev_lbl.setStyleSheet(
                f"color:{theme.PALETTE['platinum']}; background: transparent;")
            ev_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            top.addWidget(tier_badge)
            top.addWidget(name, 1)
            if badge:
                top.addWidget(b_lbl)
            top.addWidget(ev_lbl)
            lay.addLayout(top)

            cov = QLabel(f"покрытие цен: {e.get('coverage', 0) * 100:.0f}%")
            cov.setFont(self._f_detail)
            cov.setStyleSheet(theme.muted_qss(10))
            lay.addWidget(cov)

            for rw in e.get("rewards") or []:
                row = QHBoxLayout()
                row.setSpacing(8)
                rname = QLabel(str(rw.get("item_name") or "?"))
                rname.setFont(self._f_detail)
                rname.setStyleSheet(
                    f"color:{theme.PALETTE['text']}; background: transparent;")
                chance = QLabel(f"{rw.get('chance', 0):.1f}%")
                chance.setFont(self._f_detail)
                chance.setFixedWidth(56)
                chance.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                chance.setStyleSheet(theme.muted_qss(10))
                price_text = _fmt_price(rw.get("price"))
                price_color = _color_for_price(rw.get("price"))
                if rw.get("slug") and not rw.get("price_cached"):
                    price_text = "загрузка…"
                    price_color = theme.PALETTE["muted"]
                price = QLabel(price_text)
                price.setFont(self._f_detail)
                price.setFixedWidth(76)
                price.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                price.setStyleSheet(
                    f"color:{price_color}; background: transparent;")
                rev = QLabel(f"EV {rw.get('ev', 0):.2f} p")
                rev.setFont(self._f_detail)
                rev.setFixedWidth(64)
                rev.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                rev.setStyleSheet(
                    f"color:{theme.PALETTE['platinum']}; "
                    "background: transparent;")
                icon = _item_icon_label(self.cfg, rw, 24)
                if icon is not None:
                    row.addWidget(icon)
                row.addWidget(rname, 1)
                row.addWidget(chance)
                row.addWidget(price)
                row.addWidget(rev)
                lay.addLayout(row)
            return card

        # ── API для окна и тестов ───────────────────────────────────────
        def switch_tab(self, index):
            index = max(0, min(1, int(index)))
            self._active = index
            for i, button in enumerate(self.tab_buttons):
                button.setChecked(i == index)
            self.stack.setCurrentIndex(index)
            if index == 1:
                if self._pools_loaded:
                    # пулы уже загружены — перерисовать из модели (кэш цен),
                    # чтобы при возврате не остались вечные «загрузка…» (F2)
                    self._render_pools()
                else:
                    self._ensure_pools_loaded()

        def _ensure_pools_loaded(self):
            """Ленивая загрузка пулов — только при первом показе подвкладки."""
            if self._pools_loaded or self.pool is None:
                return
            self._pools_loaded = True
            self._start_pool_load(force=False)

        def status_text(self):
            if self._active == 1:
                if self.pool is not None:
                    try:
                        return ui_safe.clean(self.pool.status_text(),
                                             fallback="пулы недоступны",
                                             limit=120)
                    except Exception:
                        pass
                return "пулы недоступны"
            try:
                stats = self._stats()
            except Exception as exc:
                _ui_error("дроп (статус)", exc)
                return "дроп: — рел · 0 p"
            if not stats:
                return "дроп: — рел · 0 p"
            # 1.0.0: N — ОТКРЫТЫЕ реликвии из EE.log (не число отметок)
            info = self._drop_stats() or {}
            total = int(stats.get("total_value") or 0)
            has_log = bool(getattr(self.collector, "path", None))
            if info.get("log_available") and has_log:
                relics = int(info.get("relics",
                                      info.get("relics_session")) or 0)
                text = f"дроп: {relics} рел · {total} p"
            else:
                text = f"дроп: — рел · {total} p"
            return ui_safe.clean(text, fallback="дроп: нет данных", limit=120)

        def apply_cfg(self, cfg):
            """1.0.0 (t46): актуальные настройки (иконки валют) без копий."""
            merged = dict(self.cfg or {})
            merged.update(dict(cfg or {}))
            self.cfg = merged
            self._loot_signature = None
            try:
                self.update_pick_hint()
            except Exception:
                pass
            try:
                self.refresh()
            except Exception:
                self._render_stats()
            return self

        def refresh(self):
            """⟳ из шапки окна: обновляет активную подвкладку."""
            if self._active == 1:
                self._start_pool_load(force=True)
            else:
                if self.collector is not None:
                    try:
                        self.collector.resolve_pending()
                    except Exception:
                        pass
                self._render_stats()

        def _stats(self):
            if self.collector is None:
                return {}
            try:
                picked = getattr(self.collector, "picked_stats", None)
                if callable(picked):
                    return picked()
                return self.collector.stats()
            except Exception:
                return {}

    return _DropsPanel


# Совместимое имя.
DropsPanel = None  # заполняется фабрикой; наружу отдают build_drops_panel().
__all__ = ["build_drops_panel", "TIER_COLORS"]
