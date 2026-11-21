# -*- coding: utf-8 -*-
"""Вкладка «Дроп» единого окна: статистика фарма и пулы реликвий.

Содержит две подвкладки:
  1. «Статистика» — карточки агрегатов ``farm_stats.FarmStatsCollector``
     (открыто реликвий, суммарная стоимость, лучший дроп, средний доход в час,
     список последних дропов) и кнопка «Сбросить статистику».
  2. «Пулы реликвий» — выбор тира (Lith/Meso/Neo/Axi/Requiem) и состояния
     (Intact/Exceptional/Flawless/Radiant), список реликвий по EV с шансами,
     ценами и бейджем «стоит ли открывать», кнопка «Обновить».

Сеть не блокирует GUI: пулы грузятся в фоне и показываются по сигналу.
Без лога/сети панель работает на кэше и показывает понятные сообщения.

Модуль не импортирует warframe/market_client: ``collector`` и ``pool``
передаются извне (см. ``build_drops_panel``).
"""
import os
import time

import ui_theme as theme

#: Цвета бейджей тира эры — как в панели событий (events_api/warframe.py).
TIER_COLORS = {
    "Lith": "#b08d57", "Meso": "#8fb8d6", "Neo": "#d9a441",
    "Axi": "#a468c9", "Requiem": "#c0392b",
}


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
    from PyQt5.QtCore import Qt, QTimer, pyqtSignal
    from PyQt5.QtGui import QFont
    from PyQt5.QtWidgets import (QButtonGroup, QComboBox, QFrame, QHBoxLayout,
                                 QLabel, QPushButton, QScrollArea, QSizePolicy,
                                 QStackedWidget, QVBoxLayout, QWidget)

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

        def __init__(self, cfg, collector, pool):
            super().__init__()
            self.cfg = dict(cfg or {})
            self.collector = collector
            self.pool = pool
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

            # живой пересчёт «доход в час» раз в секунду (без перестроения UI)
            self._tick_timer = QTimer(self)
            self._tick_timer.setInterval(1000)
            self._tick_timer.timeout.connect(self._update_stats_labels)
            self._tick_timer.start()

            self.switch_tab(self._active)
            self._render_stats()
            # данные пулов НЕ грузим здесь — лениво при первом показе вкладки (F6)

        # ── страница «Статистика» ───────────────────────────────────────
        def _build_stats_page(self):
            page = QWidget()
            lay = QVBoxLayout(page)
            lay.setContentsMargins(0, 6, 0, 0)
            lay.setSpacing(8)

            head = QHBoxLayout()
            head.setSpacing(8)
            title = QLabel("Дроп — статистика фарма")
            title.setFont(self._f_title)
            title.setStyleSheet(
                f"color:{theme.PALETTE['text']}; background: transparent;")
            self.lbl_stats_status = QLabel("")
            self.lbl_stats_status.setFont(self._f_detail)
            self.lbl_stats_status.setStyleSheet(theme.status_qss())
            self.btn_reset = QPushButton("Сбросить статистику")
            self.btn_reset.setStyleSheet(theme.button_qss(danger=True, small=True))
            self.btn_reset.setCursor(Qt.PointingHandCursor)
            self.btn_reset.clicked.connect(self._reset_stats)
            head.addWidget(title)
            head.addStretch(1)
            head.addWidget(self.lbl_stats_status)
            head.addWidget(self.btn_reset)
            lay.addLayout(head)

            cards = QHBoxLayout()
            cards.setSpacing(8)
            self.card_relics = self._make_card(
                "Открыто реликвий", "0", "за текущую сессию: 0")
            self.card_value = self._make_card(
                "Суммарная стоимость", "0p", "оценено дропов: 0")
            self.card_best = self._make_card(
                "Лучший дроп", "—", "цена: —")
            self.card_avg = self._make_card(
                "Средний доход", "0 p/ч", "по всем сессиям")
            for card in (self.card_relics, self.card_value,
                         self.card_best, self.card_avg):
                cards.addWidget(card["frame"], 1)
            lay.addLayout(cards)

            lay.addWidget(self._make_section("Последние дропы"))
            self.recent_host = QWidget()
            self.recent_lay = QVBoxLayout(self.recent_host)
            self.recent_lay.setContentsMargins(0, 0, 0, 0)
            self.recent_lay.setSpacing(4)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setStyleSheet("QScrollArea { background: transparent; }")
            scroll.setWidget(self.recent_host)
            lay.addWidget(scroll, 1)

            return page

        def _make_card(self, caption, value, sub):
            frame = QFrame()
            frame.setObjectName("dropsCard")
            frame.setStyleSheet(theme.card_qss("dropsCard", 6))
            lay = QVBoxLayout(frame)
            lay.setContentsMargins(12, 10, 12, 10)
            lay.setSpacing(4)
            cap = QLabel(caption)
            cap.setFont(self._f_detail)
            cap.setStyleSheet(theme.muted_qss(10))
            val = QLabel(value)
            val.setFont(self._f_big)
            val.setStyleSheet(
                f"color:{theme.PALETTE['text']}; background: transparent;")
            val.setWordWrap(True)
            sub_lbl = QLabel(sub)
            sub_lbl.setFont(self._f_detail)
            sub_lbl.setStyleSheet(theme.muted_qss(10))
            lay.addWidget(cap)
            lay.addWidget(val)
            lay.addWidget(sub_lbl)
            return {"frame": frame, "value": val, "sub": sub_lbl}

        def _make_section(self, text):
            lbl = QLabel(text.upper())
            lbl.setFont(self._f_detail)
            lbl.setStyleSheet(theme.section_qss())
            return lbl

        def _update_stats_labels(self):
            stats = self._stats()
            if not stats:
                return
            self.card_relics["value"].setText(str(stats["relics_opened"]))
            active = "идёт" if stats.get("current_active") else "завершена"
            self.card_relics["sub"].setText(
                f"текущая сессия: {stats['current_relics']} ({active})")
            self.card_value["value"].setText(_fmt_price(stats["total_value"]))
            self.card_value["value"].setStyleSheet(
                f"color:{theme.PALETTE['platinum']}; background: transparent;")
            self.card_value["value"].setToolTip(theme.currency_tooltip("platinum"))
            self.card_value["sub"].setText(
                f"оценено дропов: {stats['priced_drops']} · сессий: {stats['sessions']}")
            best = stats["best_drop"]
            if best:
                self.card_best["value"].setText(str(best.get("name") or "—"))
                self.card_best["value"].setStyleSheet(
                    f"color:{_color_for_price(best.get('price'))};"
                    " background: transparent;")
                self.card_best["sub"].setText(
                    f"цена: {_fmt_price(best.get('price'))} · {_fmt_rel(best.get('rel_time'))}")
            else:
                self.card_best["value"].setText("—")
                self.card_best["value"].setStyleSheet(
                    f"color:{theme.PALETTE['text']}; background: transparent;")
                self.card_best["sub"].setText("цена: —")
            self.card_avg["value"].setText(f"{stats['avg_per_hour']:.1f} p/ч")
            self.card_avg["sub"].setText(f"за {stats['hours']:.1f} ч игры")

        def _render_stats(self):
            self._update_stats_labels()
            # список последних дропов
            while self.recent_lay.count():
                item = self.recent_lay.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.deleteLater()
            drops = []
            if self.collector is not None:
                try:
                    drops = self.collector.recent_drops(25)
                except Exception:
                    drops = []
            if not drops:
                empty = QLabel("Дропов пока нет — откройте реликвию в игре.")
                empty.setFont(self._f_detail)
                empty.setStyleSheet(theme.muted_qss(11))
                self.recent_lay.addWidget(empty)
            for d in drops:
                self.recent_lay.addWidget(self._make_drop_row(d))
            self.recent_lay.addStretch(1)
            self.lbl_stats_status.setText(
                "обновлено" if drops else "нет данных")

        def _make_drop_row(self, d):
            row = QFrame()
            row.setObjectName("dropsRow")
            row.setStyleSheet(theme.row_qss(name="dropsRow", radius=4))
            lay = QHBoxLayout(row)
            lay.setContentsMargins(10, 5, 10, 5)
            lay.setSpacing(8)
            t = QLabel(_fmt_rel(d.get("rel_time")))
            t.setFont(self._f_detail)
            t.setFixedWidth(72)
            t.setStyleSheet(theme.muted_qss(10))
            name = QLabel(str(d.get("name") or d.get("path") or "—"))
            name.setFont(self._f_text)
            name.setStyleSheet(
                f"color:{theme.PALETTE['text']}; background: transparent;")
            price = QLabel(_fmt_price(d.get("price")))
            price.setFont(make_font(11, True))
            price.setStyleSheet(
                f"color:{_color_for_price(d.get('price'))};"
                " background: transparent;")
            price.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            lay.addWidget(t)
            icon = _item_icon_label(self.cfg, d, 24)
            if icon is not None:
                lay.addWidget(icon)
            lay.addWidget(name, 1)
            picked = str(d.get("picked_name") or "").strip()
            if picked:
                picked_lbl = QLabel(f"взято: {picked}")
                picked_lbl.setFont(self._f_detail)
                picked_lbl.setStyleSheet(theme.muted_qss(10))
                tip = "Взята награда из скана Alt+Q"
                if d.get("picked_price") is not None:
                    tip += f" · {d.get('picked_price')}p"
                picked_lbl.setToolTip(tip)
                lay.addWidget(picked_lbl)
            lay.addWidget(price)
            return row

        def _reset_stats(self):
            if self.collector is not None:
                try:
                    self.collector.reset()
                except Exception:
                    pass
            self.lbl_stats_status.setText("статистика сброшена")
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
            return (f"дроп: {stats['relics_opened']} рел. · "
                    f"{_fmt_price(stats['total_value'])} · "
                    f"{stats['avg_per_hour']:.1f} p/ч")

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
                return self.collector.stats()
            except Exception:
                return {}

    return _DropsPanel


# Совместимое имя.
DropsPanel = None  # заполняется фабрикой; наружу отдают build_drops_panel().
__all__ = ["build_drops_panel", "TIER_COLORS"]
