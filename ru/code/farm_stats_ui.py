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
    combos = _parse_pick_keys(keys)
    if not combos:
        return "Клавиши не заданы — откройте Настройки → Клавиши"
    _canon, variants_fn = _hotkey_helpers()
    lines = ["Клавиши отметки награды:"]
    for combo in combos:
        variants = variants_fn(combo)
        ru = [variant for variant in variants
              if variant != combo and any("а" <= ch <= "я"
                                          for ch in variant.lower())]
        line = _pretty_combo(combo)
        if ru:
            line += " / " + ", ".join(
                _pretty_combo(value, canonical=False) for value in ru)
        lines.append(line)
    return "\n".join(lines)


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
        image = provider.resolve(name=name, slug=slug,
                                 en_name=item.get("en_name") or "",
                                 game_ref=game_ref)
        blueprint = item_icons.is_blueprint(name, slug)
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


def build_drops_panel():
    """Фабрика панели вкладки «Дроп» (PyQt5 импортируется лениво)."""
    from PyQt5.QtCore import Qt, pyqtSignal
    from PyQt5.QtGui import QFont
    from PyQt5.QtWidgets import (QButtonGroup, QComboBox, QFrame, QHBoxLayout,
                                 QLabel, QPushButton, QScrollArea, QSizePolicy,
                                 QStackedWidget, QVBoxLayout, QWidget)
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

    class _DropsPanel(QWidget):
        pools_ready = pyqtSignal()
        prices_ready = pyqtSignal()
        stats_ready = pyqtSignal()

        def __init__(self, cfg, collector, pool, sell_handler=None):
            super().__init__()
            self.cfg = dict(cfg or {})
            self.collector = collector
            self.pool = pool
            self.sell_handler = sell_handler
            self._listed_slugs = set()
            self._active = 0
            self._loading_pools = False
            self._pools_loaded = False      # ленивая загрузка данных (F6)
            self._pools_gen = 0             # поколение рендера: отмена старых цен

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
            lay.setSpacing(8)

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

            lay.addWidget(self._make_section("Лут"))
            self.loot_host = QWidget()
            self.loot_lay = QVBoxLayout(self.loot_host)
            self.loot_lay.setContentsMargins(0, 0, 0, 0)
            self.loot_lay.setSpacing(3)
            self.loot_scroll = QScrollArea()
            self.loot_scroll.setWidgetResizable(True)
            self.loot_scroll.setFrameShape(QFrame.NoFrame)
            self.loot_scroll.setMaximumHeight(420)
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

        def _update_stats_labels(self):
            stats = self._stats()
            count = int(stats.get("relics_opened", 0) or 0)
            total = int(stats.get("total_value", 0) or 0)
            self.lbl_total.setText(
                f"Выбрано: {count} предметов · Итого: {total} p")
            self.lbl_total.setToolTip(theme.currency_tooltip("platinum"))

        def _render_stats(self):
            self._update_stats_labels()
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
            try:
                dt = datetime.datetime.fromtimestamp(float(ts)).date()
            except Exception:
                dt = datetime.date.today()
            today = datetime.date.today()
            if dt == today:
                return "Сегодня"
            if dt == today - datetime.timedelta(days=1):
                return "Вчера"
            return dt.strftime("%d.%m.%Y")

        def _render_loot(self):
            while self.loot_lay.count():
                item = self.loot_lay.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()
            loot = []
            if self.collector is not None:
                try:
                    loot = self.collector.picked_loot(60)
                except Exception:
                    loot = []
            if not loot:
                empty = QLabel(self._pick_hint())
                empty.setFont(self._f_detail)
                empty.setStyleSheet(theme.muted_qss(11))
                empty.setWordWrap(True)
                empty.setToolTip(self._pick_tooltip())
                self.loot_lay.addWidget(empty)
                return
            current_day = None
            for drop in loot:
                day = self._day_label(drop.get("picked_utc"))
                if day != current_day:
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
                try:
                    when = datetime.datetime.fromtimestamp(
                        float(drop.get("picked_utc"))).strftime("%H:%M")
                except Exception:
                    when = "--:--"
                t = QLabel(when)
                t.setFont(self._f_detail)
                t.setFixedWidth(42)
                t.setStyleSheet(theme.muted_qss(10))
                icon = _item_icon_label(self.cfg, drop, 24)
                name = QLabel(str(drop.get("picked_name") or "?"))
                name.setFont(self._f_text)
                name.setStyleSheet(
                    f"color:{theme.PALETTE['text']}; background: transparent;")
                price = QLabel(f"{int(drop.get('picked_price') or 0)} p")
                price.setFont(make_font(11, True))
                price.setStyleSheet(
                    f"color:{theme.PALETTE['platinum']};"
                    " background: transparent;")
                price.setToolTip(theme.currency_tooltip("platinum"))
                qty = QLabel("×1")
                qty.setFont(self._f_detail)
                qty.setStyleSheet(theme.muted_qss(10))
                line.addWidget(t)
                if icon is not None:
                    line.addWidget(icon, 0, Qt.AlignVCenter)
                line.addWidget(name, 1)
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
                line.addWidget(self._sell_button(drop))
                self.loot_lay.addWidget(row)

        def _sell_status(self, drop):
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
                return result if isinstance(result, dict) else {}
            except Exception as exc:
                return {"enabled": False, "label": "Продать",
                        "tooltip": f"продажа недоступна: {exc}"}

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
                self.lbl_total.setToolTip(f"продажа: {exc}")
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
                        return self.pool.status_text()
                    except Exception:
                        pass
                return "пулы недоступны"
            stats = self._stats()
            if not stats:
                return "дроп: нет данных"
            return (f"дроп: {stats['relics_opened']} предметов · "
                    f"{stats['total_value']}p")

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
