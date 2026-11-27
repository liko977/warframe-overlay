# -*- coding: utf-8 -*-
"""Вкладка «Перевод» (t200): история переводов выделенной области, стиль E.

Заголовки ``ВРЕМЯ | ОРИГИНАЛ | ПЕРЕВОД``, строки 28 px (длинные — кратно 14),
группировка по дате капсом, кнопки «Копировать»/«Удалить» в строке и
«Очистить всё»/«Копировать всё» в шапке.
"""
import datetime

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                             QPushButton, QScrollArea, QSizePolicy,
                             QVBoxLayout, QWidget)

try:
    import ui_theme as theme
except Exception:                        # pragma: no cover - тема всегда рядом
    theme = None

import translate_history as history_mod

GRID = (("time", 78), ("original", 0), ("translated", 0), ("actions", 150))
ROW_H = 28


def _color(name, default="#e6edf6"):
    if theme is None:
        return default
    return theme.PALETTE.get(name, default)


def _qss(kind, text):
    if theme is not None:
        return getattr(theme, kind)(text)
    return f"color:{_color('text')}; background:transparent;"


def _font(size, bold=False):
    from PyQt5.QtGui import QFont
    font = QFont(theme.FONT if theme is not None else "Segoe UI")
    font.setPixelSize(int(size))
    font.setBold(bool(bold))
    return font


def _caps(text):
    label = QLabel(str(text).upper())
    label.setFont(_font(9))
    if theme is not None:
        label.setStyleSheet(theme.caps_header_qss())
    return label


def _button(text, small=True):
    button = QPushButton(str(text))
    from PyQt5.QtGui import QFont
    font = QFont("Consolas")
    font.setPixelSize(9)
    font.setCapitalization(QFont.AllUppercase)
    button.setFont(font)
    if theme is not None:
        button.setStyleSheet(theme.button_qss(small=small)
                             + " border-radius:2px;")
    button.setFixedHeight(24)
    button.setCursor(Qt.PointingHandCursor)
    return button


def _date_label(utc):
    """СЕГОДНЯ / ВЧЕРА / ДД.ММ.ГГГГ для группировки."""
    day = datetime.datetime.fromtimestamp(float(utc or 0)).date()
    today = datetime.date.today()
    if day == today:
        return "СЕГОДНЯ"
    if day == today - datetime.timedelta(days=1):
        return "ВЧЕРА"
    return day.strftime("%d.%m.%Y")


def _time_text(utc):
    return datetime.datetime.fromtimestamp(float(utc or 0)).strftime("%H:%M:%S")


def build_translate_widget():
    """Фабрика класса вкладки (Qt импортируется лениво у вызывающего)."""

    class TranslateWidget(QWidget):
        """История переводов: таблица E + действия."""

        def __init__(self, cfg=None, parent=None):
            super().__init__(parent)
            self.cfg = dict(cfg or {})
            self.setObjectName("translatePanel")
            self.setAttribute(Qt.WA_StyledBackground, True)
            if theme is not None:
                self.setStyleSheet(
                    f"QWidget#translatePanel {{"
                    f" background-color:{theme.PALETTE['panel']}; }}"
                    + theme.scroll_qss())
            self.history = history_mod.TranslateHistory(self.cfg)
            self.rows = []
            self.group_labels = []
            outer = QVBoxLayout(self)
            outer.setContentsMargins(10, 8, 10, 8)
            outer.setSpacing(6)

            tools = QHBoxLayout()
            tools.setSpacing(6)
            self.lbl_count = QLabel("0 переводов")
            self.lbl_count.setFont(_font(10))
            self.lbl_count.setStyleSheet(_qss("muted_qss", 10))
            self.btn_copy_all = _button("Копировать всё")
            self.btn_copy_all.clicked.connect(self.copy_all)
            self.btn_clear = _button("Очистить всё")
            self.btn_clear.clicked.connect(self.clear_all)
            tools.addWidget(self.lbl_count, 1)
            tools.addWidget(self.btn_copy_all)
            tools.addWidget(self.btn_clear)
            outer.addLayout(tools)

            head = QFrame()
            head.setObjectName("translateHeader")
            head.setFixedHeight(24)
            line = QHBoxLayout(head)
            line.setContentsMargins(8, 0, 8, 0)
            line.setSpacing(6)
            for key, title in (("time", "ВРЕМЯ"), ("original", "ОРИГИНАЛ"),
                               ("translated", "ПЕРЕВОД"), ("actions", "")):
                label = _caps(title)
                width = dict(GRID)[key]
                if width:
                    label.setFixedWidth(width)
                label.setFixedHeight(24)
                line.addWidget(label, 0 if width else 1)
            outer.addWidget(head)

            self.scroll = QScrollArea()
            self.scroll.setWidgetResizable(True)
            self.scroll.setFrameShape(QFrame.NoFrame)
            if theme is not None:
                self.scroll.setStyleSheet(
                    "QScrollArea { background:transparent; border:none; }"
                    " QScrollArea > QWidget > QWidget { background:transparent; }"
                    + theme.scroll_qss())
            self.host = QWidget()
            self.body = QVBoxLayout(self.host)
            self.body.setContentsMargins(0, 0, 0, 0)
            self.body.setSpacing(0)
            self.body.setAlignment(Qt.AlignTop)
            self.scroll.setWidget(self.host)
            outer.addWidget(self.scroll, 1)
            self.empty = self._empty_state()
            outer.addWidget(self.empty)
            self.refresh()

        @staticmethod
        def _empty_state():
            box = QWidget()
            lay = QVBoxLayout(box)
            lay.setContentsMargins(0, 20, 0, 0)
            lay.setSpacing(6)
            title = _caps("Нет переводов")
            title.setAlignment(Qt.AlignCenter)
            hint = QLabel("Alt+R — выделить область с текстом")
            hint.setFont(_font(10))
            hint.setAlignment(Qt.AlignCenter)
            hint.setStyleSheet(_qss("muted_qss", 10))
            lay.addWidget(title)
            lay.addWidget(hint)
            box.title = title
            return box

        def set_cfg(self, cfg):
            self.cfg = dict(cfg or {})
            self.history = history_mod.TranslateHistory(self.cfg)
            self.refresh()
            return True

        def _cell(self, text, muted=False, wrap_limits=2):
            label = QLabel(str(text or ""))
            label.setFont(_font(10 if muted else 11))
            label.setStyleSheet(_qss("muted_qss", 10) if muted
                                else f"color:{_color('text')};"
                                     " background:transparent;")
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
            label.setMinimumWidth(0)
            label.setToolTip(str(text or ""))
            label.setMaximumHeight(14 * max(1, wrap_limits))
            return label

        def _row(self, record, zebra):
            row = QFrame()
            row.setObjectName("translateRow")
            row.setProperty("utc", float(record.get("utc") or 0))
            background = (_color("zebra") if (zebra and theme is not None)
                          else "transparent")
            row.setStyleSheet(
                f"QFrame#translateRow {{ background-color:{background};"
                " border:none; border-radius:0px; }")
            line = QHBoxLayout(row)
            line.setContentsMargins(8, 0, 8, 0)
            line.setSpacing(6)
            original = str(record.get("original") or "")
            translated = str(record.get("translated") or "")
            error = str(record.get("error") or "")
            when = _time_text(record.get("utc"))
            time_label = QLabel(when)
            time_label.setFixedWidth(dict(GRID)["time"])
            time_label.setFont(_font(10))
            time_label.setStyleSheet(_qss("muted_qss", 10)
                                     + " font-family:'Consolas', monospace;")
            orig_label = self._cell(original)
            if error:
                trans_text = f"ошибка: {error}"
            elif record.get("skipped"):
                trans_text = "уже русский"
            else:
                trans_text = translated or "…"
            trans_label = self._cell(trans_text, muted=True)
            line.addWidget(time_label, 0)
            line.addWidget(orig_label, 1)
            line.addWidget(trans_label, 1)
            actions = QWidget()
            actions.setFixedWidth(dict(GRID)["actions"])
            act_lay = QHBoxLayout(actions)
            act_lay.setContentsMargins(0, 0, 0, 0)
            act_lay.setSpacing(4)
            btn_copy = _button("Копировать")
            btn_copy.setFixedHeight(20)
            btn_copy.clicked.connect(
                lambda _checked=False, rec=dict(record): self.copy_record(rec))
            btn_del = _button("Удалить")
            btn_del.setFixedHeight(20)
            btn_del.clicked.connect(
                lambda _checked=False, rec=dict(record): self.remove_record(rec))
            act_lay.addWidget(btn_copy)
            act_lay.addWidget(btn_del)
            line.addWidget(actions, 0)
            rows = max(1, min(3, (max(len(original), len(trans_text)) // 42) + 1))
            row.setFixedHeight(max(ROW_H, 14 * (rows * 2)))
            row.copy_button = btn_copy
            row.delete_button = btn_del
            row.original_label = orig_label
            row.translated_label = trans_label
            return row

        def refresh(self):
            while self.body.count():
                item = self.body.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.setParent(None)
                    widget.deleteLater()
            self.rows = []
            self.group_labels = []
            records = self.history.records()
            self.lbl_count.setText(f"{len(records)} переводов")
            self.empty.setVisible(not records)
            self.scroll.setVisible(bool(records))
            last_date = None
            for index, record in enumerate(records):
                date_text = _date_label(record.get("utc"))
                if date_text != last_date:
                    group = _caps(date_text)
                    group.setContentsMargins(8, 6, 0, 0)
                    self.body.addWidget(group)
                    self.group_labels.append(group)
                    last_date = date_text
                row = self._row(record, zebra=index % 2 == 1)
                self.rows.append(row)
                self.body.addWidget(row)
            self.body.addStretch(1)
            return len(records)

        def texts(self):
            """Полные тексты строк (для тестов): пары оригинал/перевод."""
            out = []
            for row in self.rows:
                getter = getattr(row.original_label, "text", None)
                out.append((str(getter() or ""),
                            str(row.translated_label.text() or "")))
            return out

        def copy_record(self, record):
            original = str(record.get("original") or "")
            translated = str(record.get("translated") or "")
            error = str(record.get("error") or "")
            body = translated if translated and not error else (
                f"ошибка: {error}" if error else "уже русский")
            QApplication.clipboard().setText(f"{original}\n{body}")
            return True

        def copy_all(self):
            lines = []
            for record in self.history.records():
                lines.append(str(record.get("original") or ""))
                if record.get("translated"):
                    lines.append(str(record.get("translated")))
                elif record.get("error"):
                    lines.append(f"ошибка: {record['error']}")
            QApplication.clipboard().setText("\n".join(lines))
            return True

        def remove_record(self, record):
            removed = self.history.remove(record.get("utc"))
            self.refresh()
            return bool(removed)

        def clear_all(self):
            confirm = getattr(self, "confirm", None)
            if callable(confirm) and not confirm("Удалить все переводы?"):
                return False
            self.history.clear()
            self.refresh()
            return True

    return TranslateWidget
