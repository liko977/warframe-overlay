# -*- coding: utf-8 -*-
"""Настройки оверлея: горячие клавиши и основные параметры (без сети).

Модуль состоит из двух частей:

* **Логика без Qt** — нормализация/проверка комбинаций, поиск конфликтов,
  :class:`HotkeyBinder` (перерегистрация хуков ``keyboard`` без перезапуска,
  с откатом при ошибке), атомарное сохранение ``config.json`` с бэкапом
  ``config.json.bak`` и чтение-мерж без потери неизвестных ключей;
* **UI** — :func:`build_settings_dialog`: окно «Настройки» в палитре
  ``ui_theme`` (вкладки: Клавиши / Уведомления / Оверлей / Обновления /
  Прочее), кнопки «Сбросить к умолчанию», «Сохранить», «Закрыть».

Все изменения настроек идут только через ``config.json`` (никаких скрытых
файлов), существующие значения не теряются: перед записью файл читается,
сливается с новыми ключами и атомарно заменяется, старый вариант остаётся
в ``config.json.bak``.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime

try:
    import ui_theme as theme
except Exception:                     # pragma: no cover - тема всегда рядом
    theme = None


# ─────────────────────────────────────────────────────────────────────────
#  Действия и значения по умолчанию
# ─────────────────────────────────────────────────────────────────────────
#: ``(action, label, default)``; ``pick1..4`` живут в ``pick_hotkeys``.
HOTKEY_ACTIONS = (
    ("scan", "Сканировать награды", "alt+q"),
    ("window", "Открыть/закрыть окно", "f8"),
    ("hide", "Скрыть всё", "f9"),
    ("events", "События", "alt+e"),
    ("market", "Маркет", "alt+m"),
    ("pick1", "Отметить награду 1", "alt+1"),
    ("pick2", "Отметить награду 2", "alt+2"),
    ("pick3", "Отметить награду 3", "alt+3"),
    ("pick4", "Отметить награду 4", "alt+4"),
)
PICK_ACTIONS = ("pick1", "pick2", "pick3", "pick4")

#: Какие события уведомлений показывать в настройках.
NOTIFY_EVENT_OPTIONS = (
    ("order_bought", "Куплен ордер"),
    ("price_drop", "Цена ниже моей"),
    ("baro_arrival", "Баро прилетел"),
    ("baro_leaving", "Баро улетает"),
    ("fissure_ending", "Фиссура заканчивается"),
    ("chat", "Сообщение в чате"),
)

#: Негорячие параметры по умолчанию (совпадают с warframe.DEFAULT_CONFIG).
DEFAULT_VALUES = {
    "notify_enabled": True,
    "notify_sound": True,
    "notify_skip_when_game_focused": False,
    "notify_events": ["order_bought", "price_drop", "baro_arrival",
                      "baro_leaving", "fissure_ending", "chat"],
    "notify_price_drop_pct": 10,
    "notify_fissure_tiers": ["Axi", "Neo"],
    "notify_mode": "overlay",
    "notify_toast_sec": 6,
    "notify_chat_color": "#4aa8ff",
    "notify_market_color": "#43d17c",
    "notify_price_color": "#f5c451",
    "notify_sound_choice": "auto",
    "notify_sound_file": "",
    "overlay_layout": "bottom",
    "font_scale": 1.0,
    "overlay_opacity": 1.0,
    "show_summary": True,
    "update_check": True,
    "update_url": "",
}

MODIFIERS = ("ctrl", "alt", "shift", "win")
#: Цвета-акценты центра уведомлений — только из палитры ui_theme.
COLOR_CHOICES = ("accent", "success", "warning", "danger", "platinum",
                 "ducats", "muted")
_MODIFIER_ALIASES = {
    "ctrl": "ctrl", "control": "ctrl",
    "alt": "alt",
    "shift": "shift",
    "win": "win", "windows": "win", "meta": "win",
}
_SPECIAL_KEYS = {
    "space", "tab", "enter", "insert", "delete", "home", "end",
    "pageup", "pagedown", "up", "down", "left", "right",
}


def default_hotkeys():
    return {action: default for action, _label, default in HOTKEY_ACTIONS}


def default_values():
    values = {key: (list(value) if isinstance(value, list) else value)
              for key, value in DEFAULT_VALUES.items()}
    values.update(default_hotkeys())
    values["pick_hotkeys"] = [default_hotkeys()[f"pick{i}"] for i in range(1, 5)]
    return values


# ─────────────────────────────────────────────────────────────────────────
#  Комбинации: нормализация и валидация (без Qt)
# ─────────────────────────────────────────────────────────────────────────
def normalize_hotkey(value):
    """``' Ctrl + Alt + Q '`` → ``'ctrl+alt+q'`` (порядок модификаторов)."""
    if isinstance(value, (list, tuple)):
        value = "+".join(str(part) for part in value)
    tokens = [part.strip().lower() for part in str(value or "").split("+")
              if part.strip()]
    if not tokens:
        return ""
    mods = []
    keys = []
    for token in tokens:
        if token in _MODIFIER_ALIASES:
            mod = _MODIFIER_ALIASES[token]
            if mod not in mods:
                mods.append(mod)
        else:
            keys.append(token)
    mods.sort(key=lambda name: MODIFIERS.index(name)
              if name in MODIFIERS else 99)
    return "+".join(mods + keys)


def valid_key(key):
    """Допустимая «обычная» клавиша: A-Z, 0-9, F1-F12, пробел/стрелки и т.п."""
    key = str(key or "").lower()
    if re.fullmatch(r"f([1-9]|1[0-2])", key):
        return True
    if len(key) == 1 and (key.isascii() and key.isalnum()):
        return True
    return key in _SPECIAL_KEYS


def validate_hotkey(value):
    """Проверить одну комбинацию → ``(ok, message)``."""
    norm = normalize_hotkey(value)
    if not norm:
        return False, "пустая комбинация"
    parts = norm.split("+")
    mods = [part for part in parts if part in MODIFIERS]
    keys = [part for part in parts if part not in MODIFIERS]
    if not keys:
        return False, "только модификатор — нужна ещё клавиша"
    if len(keys) > 1:
        return False, "можно только одну клавишу (например, ctrl+alt+1)"
    key = keys[0]
    if key in _MODIFIER_ALIASES:
        return False, "только модификатор — нужна ещё клавиша"
    if key in ("esc", "escape"):
        return False, "Esc занят отменой захвата — выберите другую клавишу"
    if not valid_key(key):
        return False, f"недопустимая клавиша: {key}"
    return True, ""


def make_hotkey(modifiers, key):
    """Собрать комбинацию из модификаторов и клавиши (для тестов/захвата)."""
    parts = list(modifiers or []) + [key]
    combo = normalize_hotkey("+".join(str(part) for part in parts))
    ok, _message = validate_hotkey(combo)
    return combo if ok else ""


def validate_hotkeys(mapping):
    """Проверить все комбинации и дубликаты.

    Возвращает ``{"ok", "errors": {action: сообщение}, "conflicts": [...],
    "mapping": нормализованные значения}``. Дубликат подсвечивает оба поля.
    """
    errors = {}
    conflicts = []
    normalized = {}
    seen = {}
    for action, _label, _default in HOTKEY_ACTIONS:
        value = (mapping or {}).get(action, "")
        ok, message = validate_hotkey(value)
        norm = normalize_hotkey(value)
        if not ok:
            errors[action] = message
            continue
        normalized[action] = norm
        if norm in seen:
            other = seen[norm]
            errors[action] = f"дубликат: {norm}"
            errors[other] = f"дубликат: {norm}"
            if norm not in conflicts:
                conflicts.append(norm)
        else:
            seen[norm] = action
    return {"ok": not errors, "errors": errors, "conflicts": conflicts,
            "mapping": normalized}


def hotkeys_from_cfg(cfg):
    """Текущие комбинации из config.json (с фолбэком на умолчания)."""
    cfg = cfg or {}
    mapping = {}
    for action, _label, default in HOTKEY_ACTIONS:
        if action.startswith("pick"):
            continue
        mapping[action] = normalize_hotkey(cfg.get(
            {"scan": "hotkey_scan", "window": "hotkey_window",
             "hide": "hotkey_hide", "events": "hotkey_events",
             "market": "hotkey_market"}[action], default)) or default
    raw_picks = cfg.get("pick_hotkeys")
    if isinstance(raw_picks, str):
        raw_picks = [part.strip() for part in raw_picks.split(",")
                     if part.strip()]
    if not isinstance(raw_picks, (list, tuple)):
        raw_picks = [default_hotkeys()[f"pick{i}"] for i in range(1, 5)]
    defaults = default_hotkeys()
    for index in range(4):
        action = f"pick{index + 1}"
        value = raw_picks[index] if index < len(raw_picks) else defaults[action]
        mapping[action] = normalize_hotkey(value) or defaults[action]
    return mapping


def settings_from_cfg(cfg):
    """Текущие негорячие настройки (для заполнения окна)."""
    cfg = cfg or {}
    values = {}
    for key, default in DEFAULT_VALUES.items():
        value = cfg.get(key, default)
        values[key] = list(value) if isinstance(value, list) else value
    return values


# ─────────────────────────────────────────────────────────────────────────
#  Перерегистрация хуков keyboard без перезапуска
# ─────────────────────────────────────────────────────────────────────────
class HotkeyBinder:
    """Владелец горячих клавиш: снимает старые и ставит новые с откатом.

    :param keyboard_module: настоящий модуль ``keyboard`` или заглушка с
        ``add_hotkey(key, callback, suppress=False) -> handle`` и
        ``remove_hotkey(handle_or_key)``.
    :param callbacks: ``{action: callable}``; ``None``/отсутствие — действие
        не регистрируется (компонент недоступен), но настройка сохраняется.
    """

    def __init__(self, keyboard_module, callbacks=None):
        self.keyboard = keyboard_module
        self.callbacks = dict(callbacks or {})
        self._keys = {}
        self._handles = {}
        self.history = []

    def adopt(self, mapping):
        """Запомнить УЖЕ зарегистрированные клавиши (стартовая регистрация)."""
        self._keys = {action: normalize_hotkey(key)
                      for action, key in (mapping or {}).items()
                      if normalize_hotkey(key)}
        self._handles = {}
        return dict(self._keys)

    def current(self):
        return dict(self._keys)

    def _remove(self, action):
        key = self._keys.get(action)
        handle = self._handles.pop(action, None)
        try:
            if handle is not None:
                self.keyboard.remove_hotkey(handle)
            elif key:
                self.keyboard.remove_hotkey(key)
            return True
        except Exception:
            return False

    def _add(self, action, key):
        callback = self.callbacks.get(action)
        if not callable(callback):
            return False
        handle = self.keyboard.add_hotkey(key, callback, suppress=False)
        self._handles[action] = handle
        return True

    def apply(self, mapping):
        """Снять старые клавиши и поставить новые. При ошибке — откат.

        Возвращает ``{"ok", "keys", "registered", "error", "restored"}``.
        """
        desired = {action: normalize_hotkey(value)
                   for action, value in (mapping or {}).items()}
        desired = {action: key for action, key in desired.items() if key}
        old_keys = dict(self._keys)
        for action in list(self._keys):
            self._remove(action)
        try:
            registered = []
            for action, key in desired.items():
                if self._add(action, key):
                    registered.append(action)
            self._keys = dict(desired)
            self.history.append({"ok": True, "old": old_keys,
                                 "new": dict(desired)})
            return {"ok": True, "keys": dict(self._keys),
                    "registered": registered}
        except Exception as exc:
            # убрать частично поставленные новые
            for action in list(self._handles):
                self._remove(action)
            self._keys = {}
            restore_errors = 0
            for action, key in old_keys.items():
                try:
                    self._add(action, key)
                except Exception:
                    restore_errors += 1
            self._keys = dict(old_keys)
            self.history.append({"ok": False, "old": old_keys,
                                 "new": dict(desired)})
            return {
                "ok": False,
                "error": (f"клавиша не зарегистрировалась "
                          f"({type(exc).__name__}: {exc}) — оставил прежнюю"),
                "restored": restore_errors == 0,
                "keys": dict(self._keys),
            }


# ─────────────────────────────────────────────────────────────────────────
#  config.json: чтение-мерж, атомарная запись, бэкап
# ─────────────────────────────────────────────────────────────────────────
def save_settings(cfg, values, path):
    """Сохранить ``values`` в config.json: мерж + бэкап ``.bak`` + атомарно."""
    path = str(path or "config.json")
    raw = {}
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig") as handle:
                raw = json.load(handle)
            if not isinstance(raw, dict):
                raw = {}
    except Exception:
        raw = {}
    backup = ""
    try:
        if os.path.exists(path):
            backup = path + ".bak"
            shutil.copy2(path, backup)
    except Exception as exc:
        return {"ok": False, "error": f"бэкап config.json не создан: {exc}"}
    raw.update(values or {})
    tmp = path + ".tmp"
    try:
        folder = os.path.dirname(os.path.abspath(path))
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(raw, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except Exception as exc:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return {"ok": False, "error": f"config.json не сохранён: {exc}"}
    cfg.update(values or {})
    return {"ok": True, "backup": backup, "values": dict(values or {}),
            "saved": raw}


def _hotkey_config_values(mapping):
    mapping = mapping or {}
    values = {
        "hotkey_scan": mapping.get("scan", ""),
        "hotkey_window": mapping.get("window", ""),
        "hotkey_hide": mapping.get("hide", ""),
        "hotkey_events": mapping.get("events", ""),
        "hotkey_market": mapping.get("market", ""),
        "pick_hotkeys": [mapping.get(f"pick{i}", "") for i in range(1, 5)],
    }
    return values


def apply_settings(cfg, values, binder=None, path=None, extra_apply=None):
    """Проверить, перерегистрировать хуки и сохранить настройки.

    Порядок: валидация → перерегистрация (с откатом внутри binder) → запись
    config.json (с бэкапом). Если запись не удалась, клавиши возвращаются
    к прежним. ``extra_apply(cfg, values)`` вызывается после успеха для
    применения настроек в уже работающем приложении.
    """
    values = dict(values or {})
    mapping = dict(values.pop("hotkeys", {}) or {})
    check = validate_hotkeys(mapping)
    if not check["ok"]:
        return {"ok": False, "error": "проверьте горячие клавиши",
                "errors": check["errors"], "conflicts": check["conflicts"]}
    mapping = check["mapping"]
    values.update(_hotkey_config_values(mapping))
    old_keys = binder.current() if binder is not None else {}
    if binder is not None:
        applied = binder.apply(mapping)
        if not applied.get("ok"):
            return {"ok": False, "error": applied.get("error",
                                                      "клавиши не применены"),
                    "keys": applied.get("keys", old_keys)}
    saved = save_settings(cfg, values, path)
    if not saved.get("ok"):
        if binder is not None:
            binder.apply(old_keys)
        return {"ok": False, "error": saved.get("error", "config.json")}
    result = {"ok": True, "values": dict(values), "hotkeys": mapping,
              "backup": saved.get("backup", "")}
    if callable(extra_apply):
        try:
            extra_apply(cfg, dict(values))
        except Exception as exc:
            result["warning"] = (f"сохранено, но применить на лету "
                                 f"не удалось: {type(exc).__name__}: {exc}")
    return result


def open_data_folder(path, opener=None):
    """Открыть папку данных в проводнике. ``opener`` — подмена для тестов."""
    path = os.path.abspath(str(path or "."))
    if not os.path.isdir(path):
        return False
    if callable(opener):
        try:
            opener(path)
            return True
        except Exception:
            return False
    try:
        if os.name == "nt":
            os.startfile(path)                    # noqa: S606 - Windows
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────
#  UI (Qt импортируется лениво)
# ─────────────────────────────────────────────────────────────────────────
def build_settings_dialog():
    """Фабрика окна «Настройки» (палитра ui_theme, без новых зависимостей)."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QFont
    from PyQt5.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog,
                                 QDoubleSpinBox, QFileDialog, QFormLayout,
                                 QHBoxLayout, QLabel, QLineEdit, QPushButton,
                                 QScrollArea, QSpinBox, QTabWidget,
                                 QVBoxLayout, QWidget)

    def color(name, default="#e6edf6"):
        return theme.PALETTE.get(name, default) if theme is not None else default

    def qss():
        return theme.dialog_qss() if theme is not None else ""

    def font(size, bold=False):
        result = QFont(theme.FONT if theme is not None else "Segoe UI")
        result.setPixelSize(int(size))
        result.setBold(bool(bold))
        return result

    def button_qss(**kwargs):
        return theme.button_qss(**kwargs) if theme is not None else ""

    def tab_qss():
        base = f"""
            QTabWidget::pane {{ border:1px solid {color('border')};
                                background:{color('panel')}; }}
            QTabBar::tab {{ background:{color('card')}; color:{color('text')};
                            padding:6px 14px; border:1px solid {color('border')};
                            border-bottom:none; }}
            QTabBar::tab:selected {{ background:{color('panel')};
                                     color:{color('accent')}; }}
        """
        return base

    def muted(text):
        label = QLabel(str(text))
        label.setFont(font(10))
        label.setStyleSheet(f"color:{color('muted')}; background:transparent;")
        label.setWordWrap(True)
        return label

    class HotkeyEdit(QPushButton):
        """Поле захвата комбинации: клик → «Нажмите клавиши…»."""

        def __init__(self, value="", parent=None):
            super().__init__(parent)
            self._value = normalize_hotkey(value)
            self._backup = self._value
            self._capturing = False
            self.setFont(font(11))
            self.setCursor(Qt.PointingHandCursor)
            self.setMinimumWidth(170)
            self.setFocusPolicy(Qt.StrongFocus)
            self._render()
            self.clicked.connect(self.start_capture)

        def _render(self):
            self.setText(self._value or "—")
            self.setToolTip("Нажмите, чтобы назначить комбинацию "
                            "(Esc — отмена, Backspace/Delete — очистить)")
            self.setStyleSheet(button_qss())

        def value(self):
            return self._value

        def set_value(self, value):
            self._value = normalize_hotkey(value)
            self._backup = self._value
            self._capturing = False
            self._render()

        def is_capturing(self):
            return bool(self._capturing)

        def start_capture(self):
            self._backup = self._value
            self._capturing = True
            self.setText("Нажмите клавиши…")
            self.setStyleSheet(button_qss(primary=True))
            self.setFocus(Qt.OtherFocusReason)

        def cancel_capture(self):
            if not self._capturing:
                return
            self._value = self._backup
            self._capturing = False
            self._render()

        def keyPressEvent(self, event):
            if not self._capturing:
                super().keyPressEvent(event)
                return
            key = event.key()
            if key == Qt.Key_Escape:
                self.cancel_capture()
                event.accept()
                return
            if key in (Qt.Key_Backspace, Qt.Key_Delete):
                self._value = ""
                self._capturing = False
                self._render()
                event.accept()
                return
            if key in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta,
                       Qt.Key_AltGr, Qt.Key_CapsLock):
                # только модификатор: ждём «настоящую» клавишу
                event.accept()
                return
            combo = _event_combo(event)
            if combo:
                self._value = combo
                self._capturing = False
                self._render()
            event.accept()

        def focusOutEvent(self, event):
            if self._capturing:
                self.cancel_capture()
            super().focusOutEvent(event)

    def _qt_key_name(key):
        if Qt.Key_A <= key <= Qt.Key_Z:
            return chr(key).lower()
        if Qt.Key_0 <= key <= Qt.Key_9:
            return chr(key)
        if Qt.Key_F1 <= key <= Qt.Key_F12:
            return f"f{key - Qt.Key_F1 + 1}"
        special = {
            Qt.Key_Space: "space", Qt.Key_Tab: "tab",
            Qt.Key_Return: "enter", Qt.Key_Enter: "enter",
            Qt.Key_Insert: "insert", Qt.Key_Delete: "delete",
            Qt.Key_Home: "home", Qt.Key_End: "end",
            Qt.Key_PageUp: "pageup", Qt.Key_PageDown: "pagedown",
            Qt.Key_Up: "up", Qt.Key_Down: "down",
            Qt.Key_Left: "left", Qt.Key_Right: "right",
        }
        return special.get(key, "")

    def _event_combo(event):
        name = _qt_key_name(event.key())
        if not name:
            return ""
        modifiers = []
        state = event.modifiers()
        if state & Qt.ControlModifier:
            modifiers.append("ctrl")
        if state & Qt.AltModifier:
            modifiers.append("alt")
        if state & Qt.ShiftModifier:
            modifiers.append("shift")
        if state & Qt.MetaModifier:
            modifiers.append("win")
        return make_hotkey(modifiers, name)

    from PyQt5.QtCore import QTimer, pyqtSignal
    from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog,
                                 QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                                 QPushButton, QTabWidget, QVBoxLayout, QWidget,
                                 QDoubleSpinBox, QSpinBox)

    class SettingsDialog(QDialog):
        """Окно настроек: клавиши + уведомления + оверлей + обновления."""

        update_checked = pyqtSignal(dict)
        update_applied = pyqtSignal(dict)

        def __init__(self, cfg=None, apply_fn=None, on_open_data_dir=None,
                     on_test_notification=None, check_update_fn=None,
                     apply_update_fn=None, restart_fn=None, auto_check=False,
                     data_dir="", version="", parent=None):
            super().__init__(parent)
            self.cfg = dict(cfg or {})
            self.apply_fn = apply_fn
            self.check_update_fn = check_update_fn
            self.apply_update_fn = apply_update_fn
            self.restart_fn = restart_fn
            self.update_info = {}
            self.last_checked = ""
            self._update_busy = False
            self.on_open_data_dir = on_open_data_dir
            self.on_test_notification = on_test_notification
            self.data_dir = data_dir
            self.version = version
            self.last_result = {}
            self.edits = {}
            self.event_boxes = {}
            self._defaults = default_values()
            self.setWindowTitle("Настройки")
            self.setModal(True)
            self.setMinimumSize(760, 640)
            self.setStyleSheet(qss())
            try:
                import login_mode
                login_mode.register_dialog(self)      # B2: F9 закроет окно
            except Exception:
                pass

            layout = QVBoxLayout(self)
            layout.setContentsMargins(16, 14, 16, 12)
            layout.setSpacing(10)
            head = QHBoxLayout()
            title = QLabel("<b>Настройки Warframe Price Overlay</b>")
            title.setFont(font(14, True))
            title.setStyleSheet(f"color:{color('text')};")
            self.lbl_version = QLabel(f"версия {version or wf_version_fallback()}")
            self.lbl_version.setFont(font(10))
            self.lbl_version.setStyleSheet(
                f"color:{color('muted')}; background:transparent;")
            head.addWidget(title)
            head.addStretch(1)
            head.addWidget(self.lbl_version)
            layout.addLayout(head)

            self.tabs = QTabWidget()
            self.tabs.setStyleSheet(tab_qss())
            self.tabs.addTab(self._build_hotkeys_tab(), "Клавиши")
            self.tabs.addTab(self._build_notify_tab(), "Уведомления")
            self.tabs.addTab(self._build_overlay_tab(), "Оверлей")
            self.tabs.addTab(self._build_updates_tab(), "Обновления")
            self.tabs.addTab(self._build_other_tab(), "Прочее")
            layout.addWidget(self.tabs, 1)

            self.update_checked.connect(self._on_update_checked)
            self.update_applied.connect(self._on_update_applied)

            self.lbl_status = QLabel("")
            self.lbl_status.setFont(font(10))
            self.lbl_status.setStyleSheet(
                f"color:{color('muted')}; background:transparent;")
            self.lbl_status.setWordWrap(True)
            layout.addWidget(self.lbl_status)

            buttons = QHBoxLayout()
            buttons.setSpacing(8)
            self.btn_defaults = QPushButton("Сбросить к умолчанию")
            self.btn_defaults.setStyleSheet(button_qss())
            self.btn_defaults.setCursor(Qt.PointingHandCursor)
            self.btn_defaults.clicked.connect(self.reset_defaults)
            self.btn_save = QPushButton("Сохранить")
            self.btn_save.setStyleSheet(button_qss(primary=True))
            self.btn_save.setCursor(Qt.PointingHandCursor)
            self.btn_save.setDefault(True)
            self.btn_save.clicked.connect(self.save)
            self.btn_close = QPushButton("Закрыть")
            self.btn_close.setStyleSheet(button_qss())
            self.btn_close.setCursor(Qt.PointingHandCursor)
            self.btn_close.clicked.connect(self.reject)
            buttons.addWidget(self.btn_defaults)
            buttons.addStretch(1)
            buttons.addWidget(self.btn_save)
            buttons.addWidget(self.btn_close)
            layout.addLayout(buttons)

            self.load_values(self.cfg)
            self.finished.connect(self._unregister)
            if auto_check:
                QTimer.singleShot(0, self.check_updates)

        def _unregister(self, _result=0):
            try:
                import login_mode
                login_mode.unregister_dialog(self)
            except Exception:
                pass

        # ── вкладки ──────────────────────────────────────────────────
        def _build_hotkeys_tab(self):
            page = QWidget()
            outer = QVBoxLayout(page)
            outer.setContentsMargins(12, 10, 12, 10)
            outer.setSpacing(8)
            outer.addWidget(muted(
                "Клик по полю — нажмите комбинацию (Ctrl/Alt/Shift + клавиша). "
                "Esc отменяет захват, Backspace/Delete очищает. "
                "Пустые поля и дубликаты не сохраняются."))
            form = QFormLayout()
            form.setSpacing(8)
            for action, label, default in HOTKEY_ACTIONS:
                edit = HotkeyEdit(default)
                self.edits[action] = edit
                caption = QLabel(label)
                caption.setFont(font(11))
                caption.setStyleSheet(f"color:{color('text')};")
                form.addRow(caption, edit)
            outer.addLayout(form)
            outer.addStretch(1)
            return page

        def _build_notify_tab(self):
            page = QWidget()
            outer = QVBoxLayout(page)
            outer.setContentsMargins(12, 10, 12, 10)
            outer.setSpacing(8)
            self.chk_notify_enabled = QCheckBox("Показывать уведомления")
            self.chk_notify_sound = QCheckBox("Звук вместе с уведомлением")
            self.chk_notify_skip_focus = QCheckBox(
                "Только когда игра НЕ в фокусе")
            for box in (self.chk_notify_enabled, self.chk_notify_sound,
                        self.chk_notify_skip_focus):
                box.setFont(font(11))
                outer.addWidget(box)
            outer.addWidget(muted("Типы событий:"))
            for kind, label in NOTIFY_EVENT_OPTIONS:
                box = QCheckBox(label)
                box.setFont(font(11))
                self.event_boxes[kind] = box
                outer.addWidget(box)
            form = QFormLayout()
            form.setSpacing(8)
            self.spin_price_pct = QSpinBox()
            self.spin_price_pct.setRange(0, 90)
            self.spin_price_pct.setSuffix(" %")
            self.spin_price_pct.setFont(font(11))
            form.addRow(QLabel("Цена ниже моей на"), self.spin_price_pct)
            self.edit_fissure_tiers = QLineEdit()
            self.edit_fissure_tiers.setPlaceholderText("Axi, Neo")
            self.edit_fissure_tiers.setFont(font(11))
            form.addRow(QLabel("Эры фиссур (через запятую)"),
                        self.edit_fissure_tiers)
            # центр уведомлений (t15): режим, длительность тоста, цвета
            self.cmb_notify_mode = QComboBox()
            self.cmb_notify_mode.addItem("Свои тосты в оверлее", "overlay")
            self.cmb_notify_mode.addItem("Системные (трей)", "system")
            self.cmb_notify_mode.setFont(font(11))
            form.addRow(QLabel("Показ уведомлений"), self.cmb_notify_mode)
            self.spin_toast_sec = QSpinBox()
            self.spin_toast_sec.setRange(2, 20)
            self.spin_toast_sec.setSuffix(" с")
            self.spin_toast_sec.setFont(font(11))
            form.addRow(QLabel("Свой тост на экране"), self.spin_toast_sec)

            def _color_combo(default):
                combo = QComboBox()
                palette = (theme.PALETTE if theme is not None else {})
                for key in COLOR_CHOICES:
                    value = palette.get(key) or default
                    combo.addItem(key, value)
                index = combo.findData(default)
                if index >= 0:
                    combo.setCurrentIndex(index)
                combo.setFont(font(11))
                return combo

            self.cmb_chat_color = _color_combo("#4aa8ff")
            self.cmb_market_color = _color_combo("#43d17c")
            self.cmb_price_color = _color_combo("#f5c451")
            form.addRow(QLabel("Цвет чата"), self.cmb_chat_color)
            form.addRow(QLabel("Цвет «куплен ордер»"), self.cmb_market_color)
            form.addRow(QLabel("Цвет «перебили цену»"), self.cmb_price_color)
            self.cmb_sound_choice = QComboBox()
            self.cmb_sound_choice.addItem("По типу (авто)", "auto")
            self.cmb_sound_choice.addItem("Маркет", "market")
            self.cmb_sound_choice.addItem("Чат", "chat")
            self.cmb_sound_choice.addItem("Система", "system")
            self.cmb_sound_choice.setFont(font(11))
            form.addRow(QLabel("Звук уведомления"), self.cmb_sound_choice)
            sound_row = QHBoxLayout()
            self.edit_sound_file = QLineEdit()
            self.edit_sound_file.setPlaceholderText(
                "свой .wav (необязательно)")
            self.edit_sound_file.setFont(font(11))
            self.btn_sound_browse = QPushButton("…")
            self.btn_sound_browse.setFixedWidth(30)
            self.btn_sound_browse.setStyleSheet(button_qss(small=True))
            self.btn_sound_browse.setCursor(Qt.PointingHandCursor)
            self.btn_sound_browse.clicked.connect(self.browse_sound_file)
            sound_row.addWidget(self.edit_sound_file, 1)
            sound_row.addWidget(self.btn_sound_browse)
            form.addRow(QLabel("Свой .wav"), sound_row)
            test_row = QHBoxLayout()
            self.cmb_test_kind = QComboBox()
            for key, title in (("market", "Маркет (куплен ордер)"),
                               ("price", "Цена ниже моей"),
                               ("chat", "Чат"),
                               ("system", "Система")):
                self.cmb_test_kind.addItem(title, key)
            self.cmb_test_kind.setFont(font(11))
            self.btn_test_notify = QPushButton("Проверить уведомление")
            self.btn_test_notify.setStyleSheet(button_qss(small=True))
            self.btn_test_notify.setCursor(Qt.PointingHandCursor)
            self.btn_test_notify.clicked.connect(self.test_notification)
            test_row.addWidget(self.cmb_test_kind, 1)
            test_row.addWidget(self.btn_test_notify)
            form.addRow(QLabel(""), test_row)
            outer.addLayout(form)
            outer.addStretch(1)
            return page

        def _build_overlay_tab(self):
            page = QWidget()
            outer = QVBoxLayout(page)
            outer.setContentsMargins(12, 10, 12, 10)
            outer.setSpacing(8)
            form = QFormLayout()
            form.setSpacing(8)
            self.cmb_layout = QComboBox()
            self.cmb_layout.addItem("Снизу экрана", "bottom")
            self.cmb_layout.addItem("Под карточками (сверху)", "under_cards")
            self.cmb_layout.setFont(font(11))
            form.addRow(QLabel("Положение плашек"), self.cmb_layout)
            self.spin_font_scale = QDoubleSpinBox()
            self.spin_font_scale.setRange(0.8, 1.6)
            self.spin_font_scale.setSingleStep(0.05)
            self.spin_font_scale.setDecimals(2)
            self.spin_font_scale.setFont(font(11))
            form.addRow(QLabel("Масштаб шрифта"), self.spin_font_scale)
            self.spin_opacity = QDoubleSpinBox()
            self.spin_opacity.setRange(0.4, 1.0)
            self.spin_opacity.setSingleStep(0.05)
            self.spin_opacity.setDecimals(2)
            self.spin_opacity.setFont(font(11))
            form.addRow(QLabel("Прозрачность фона"), self.spin_opacity)
            self.chk_show_summary = QCheckBox("Показывать блок «Итого»")
            self.chk_show_summary.setFont(font(11))
            form.addRow(QLabel(""), self.chk_show_summary)
            outer.addLayout(form)
            outer.addWidget(muted(
                "Масштаб и положение применятся к следующему скану "
                "наград; прозрачность — сразу."))
            outer.addStretch(1)
            return page

        def _build_updates_tab(self):
            page = QWidget()
            self.updates_page = page
            outer = QVBoxLayout(page)
            outer.setContentsMargins(12, 10, 12, 10)
            outer.setSpacing(8)
            self.lbl_update_current = QLabel(
                f"Текущая версия: {self.version or wf_version_fallback()}")
            self.lbl_update_current.setFont(font(11, True))
            self.lbl_update_current.setStyleSheet(
                f"color:{color('text')}; background:transparent;")
            outer.addWidget(self.lbl_update_current)
            self.chk_update_check = QCheckBox("Проверять обновления при старте")
            self.chk_update_check.setFont(font(11))
            outer.addWidget(self.chk_update_check)
            form = QFormLayout()
            form.setSpacing(8)
            self.edit_update_url = QLineEdit()
            self.edit_update_url.setFont(font(11))
            self.edit_update_url.setPlaceholderText(
                "https://raw.githubusercontent.com/.../ru/update.json")
            form.addRow(QLabel("URL манифеста обновлений"), self.edit_update_url)
            outer.addLayout(form)
            row = QHBoxLayout()
            self.btn_check_updates = QPushButton("Проверить обновление")
            self.btn_check_updates.setStyleSheet(button_qss(small=True))
            self.btn_check_updates.setCursor(Qt.PointingHandCursor)
            self.btn_check_updates.clicked.connect(self.check_updates)
            row.addWidget(self.btn_check_updates)
            self.btn_update_apply = QPushButton("Обновить и перезапустить")
            self.btn_update_apply.setStyleSheet(button_qss(small=True))
            self.btn_update_apply.setCursor(Qt.PointingHandCursor)
            self.btn_update_apply.setVisible(False)
            self.btn_update_apply.clicked.connect(self.apply_and_restart)
            row.addWidget(self.btn_update_apply)
            row.addStretch(1)
            outer.addLayout(row)
            self.lbl_update_status = QLabel(
                "Нажмите «Проверить обновление», чтобы узнать о новой версии.")
            self.lbl_update_status.setFont(font(11))
            self.lbl_update_status.setWordWrap(True)
            self.lbl_update_status.setStyleSheet(
                f"color:{color('text')}; background:transparent;")
            outer.addWidget(self.lbl_update_status)
            self.lbl_update_notes = QLabel("")
            self.lbl_update_notes.setFont(font(10))
            self.lbl_update_notes.setWordWrap(True)
            self.lbl_update_notes.setStyleSheet(
                f"color:{color('muted')}; background:transparent;")
            outer.addWidget(self.lbl_update_notes)
            self.lbl_update_checked = QLabel("Последняя проверка: —")
            self.lbl_update_checked.setFont(font(10))
            self.lbl_update_checked.setStyleSheet(
                f"color:{color('muted')}; background:transparent;")
            outer.addWidget(self.lbl_update_checked)
            outer.addWidget(muted(
                "Пустой URL — обновления не проверяются. Проверка выполняется "
                "в фоне и не блокирует окно; при доступной версии здесь же "
                "появится «Обновить и перезапустить»."))
            outer.addStretch(1)
            return page

        def _build_other_tab(self):
            page = QWidget()
            outer = QVBoxLayout(page)
            outer.setContentsMargins(12, 10, 12, 10)
            outer.setSpacing(8)
            self.btn_open_data = QPushButton("Открыть папку данных")
            self.btn_open_data.setStyleSheet(button_qss())
            self.btn_open_data.setCursor(Qt.PointingHandCursor)
            self.btn_open_data.clicked.connect(self.open_data)
            row = QHBoxLayout()
            row.addWidget(self.btn_open_data)
            row.addStretch(1)
            outer.addLayout(row)
            self.lbl_data_dir = QLabel(str(self.data_dir or "—"))
            self.lbl_data_dir.setFont(font(10))
            self.lbl_data_dir.setStyleSheet(
                f"color:{color('muted')}; background:transparent;")
            self.lbl_data_dir.setWordWrap(True)
            self.lbl_data_dir.setTextInteractionFlags(Qt.TextSelectableByMouse)
            outer.addWidget(self.lbl_data_dir)
            outer.addWidget(muted("Версия приложения показана в шапке окна."))
            outer.addStretch(1)
            return page

        # ── данные ───────────────────────────────────────────────────
        def load_values(self, cfg):
            cfg = cfg or {}
            hotkeys = hotkeys_from_cfg(cfg)
            for action, edit in self.edits.items():
                edit.set_value(hotkeys.get(action, ""))
            values = settings_from_cfg(cfg)
            self.chk_notify_enabled.setChecked(
                bool(values.get("notify_enabled", True)))
            self.chk_notify_sound.setChecked(
                bool(values.get("notify_sound", True)))
            self.chk_notify_skip_focus.setChecked(
                bool(values.get("notify_skip_when_game_focused", False)))
            events = values.get("notify_events")
            if isinstance(events, str):
                events = [part.strip() for part in events.split(",")]
            events = set(events or [])
            for kind, box in self.event_boxes.items():
                box.setChecked(kind in events)
            self.spin_price_pct.setValue(
                int(values.get("notify_price_drop_pct", 10) or 0))
            tiers = values.get("notify_fissure_tiers") or []
            if isinstance(tiers, str):
                tiers = [part.strip() for part in tiers.split(",")
                         if part.strip()]
            self.edit_fissure_tiers.setText(", ".join(str(x) for x in tiers))
            mode_index = self.cmb_notify_mode.findData(
                str(values.get("notify_mode") or "overlay"))
            self.cmb_notify_mode.setCurrentIndex(mode_index if mode_index >= 0 else 0)
            self.spin_toast_sec.setValue(
                int(values.get("notify_toast_sec", 6) or 6))
            for combo, key in ((self.cmb_chat_color, "notify_chat_color"),
                               (self.cmb_market_color, "notify_market_color"),
                               (self.cmb_price_color, "notify_price_color")):
                value = str(values.get(key) or "")
                index = combo.findData(value)
                if index < 0 and value:
                    # сохраняем неизвестный/старый цвет как «текущий»
                    combo.addItem("текущий", value)
                    index = combo.count() - 1
                if index >= 0:
                    combo.setCurrentIndex(index)
            sound_index = self.cmb_sound_choice.findData(
                str(values.get("notify_sound_choice") or "auto"))
            self.cmb_sound_choice.setCurrentIndex(
                sound_index if sound_index >= 0 else 0)
            self.edit_sound_file.setText(
                str(values.get("notify_sound_file") or ""))
            layout_value = str(values.get("overlay_layout") or "bottom")
            index = self.cmb_layout.findData(layout_value)
            self.cmb_layout.setCurrentIndex(index if index >= 0 else 0)
            self.spin_font_scale.setValue(
                float(values.get("font_scale", 1.0) or 1.0))
            self.spin_opacity.setValue(
                float(values.get("overlay_opacity", 1.0) or 1.0))
            self.chk_show_summary.setChecked(
                bool(values.get("show_summary", True)))
            self.chk_update_check.setChecked(
                bool(values.get("update_check", True)))
            self.edit_update_url.setText(str(values.get("update_url") or ""))
            self._clear_errors()

        def hotkey_values(self):
            return {action: edit.value() for action, edit in self.edits.items()}

        def values(self):
            mapping = self.hotkey_values()
            config = _hotkey_config_values(mapping)
            config.update({
                "notify_enabled": self.chk_notify_enabled.isChecked(),
                "notify_sound": self.chk_notify_sound.isChecked(),
                "notify_skip_when_game_focused":
                    self.chk_notify_skip_focus.isChecked(),
                "notify_events": [kind for kind, box in self.event_boxes.items()
                                  if box.isChecked()],
                "notify_price_drop_pct": int(self.spin_price_pct.value()),
                "notify_fissure_tiers": [
                    part.strip()
                    for part in self.edit_fissure_tiers.text().split(",")
                    if part.strip()],
                "notify_mode": str(self.cmb_notify_mode.currentData() or "overlay"),
                "notify_toast_sec": int(self.spin_toast_sec.value()),
                "notify_chat_color": str(self.cmb_chat_color.currentData()
                                          or "#4aa8ff"),
                "notify_market_color": str(self.cmb_market_color.currentData()
                                            or "#43d17c"),
                "notify_price_color": str(self.cmb_price_color.currentData()
                                           or "#f5c451"),
                "notify_sound_choice": str(self.cmb_sound_choice.currentData()
                                            or "auto"),
                "notify_sound_file": self.edit_sound_file.text().strip(),
                "overlay_layout": str(self.cmb_layout.currentData() or "bottom"),
                "font_scale": round(float(self.spin_font_scale.value()), 2),
                "overlay_opacity": round(float(self.spin_opacity.value()), 2),
                "show_summary": self.chk_show_summary.isChecked(),
                "update_check": self.chk_update_check.isChecked(),
                "update_url": self.edit_update_url.text().strip(),
                "hotkeys": mapping,
            })
            return config

        # ── действия ─────────────────────────────────────────────────
        def _clear_errors(self):
            for edit in self.edits.values():
                edit.setStyleSheet(button_qss())
                edit.setToolTip("Нажмите, чтобы назначить комбинацию "
                                "(Esc — отмена, Backspace/Delete — очистить)")

        def _mark_errors(self, errors):
            for action, message in (errors or {}).items():
                edit = self.edits.get(action)
                if edit is None:
                    continue
                edit.setStyleSheet(button_qss(danger=True))
                edit.setToolTip(f"Конфликт: {message}")

        def _set_status(self, text, warn=False):
            self.lbl_status.setText(str(text))
            self.lbl_status.setStyleSheet(
                f"color:{color('warning' if warn else 'success')};"
                " background:transparent;")

        def reset_defaults(self):
            self.load_values(default_values())
            self._set_status("Сброшено к умолчанию — нажмите «Сохранить», "
                             "чтобы записать в config.json.")
            return True

        def save(self):
            values = self.values()
            check = validate_hotkeys(values.get("hotkeys") or {})
            self._clear_errors()
            if not check["ok"]:
                self._mark_errors(check["errors"])
                conflicts = ", ".join(check["conflicts"]) or "пустые поля"
                self._set_status("Не сохранено: проверьте горячие клавиши "
                                 f"({conflicts}).", warn=True)
                return {"ok": False, "errors": check["errors"],
                        "conflicts": check["conflicts"]}
            if not callable(self.apply_fn):
                self._set_status("Сохранение недоступно в этой сборке.",
                                 warn=True)
                return {"ok": False, "error": "apply_fn не задан"}
            try:
                result = self.apply_fn(values) or {}
            except Exception as exc:
                result = {"ok": False,
                          "error": f"{type(exc).__name__}: {exc}"}
            self.last_result = dict(result)
            if result.get("ok"):
                self._set_status("Сохранено и применено без перезапуска.")
                if result.get("warning"):
                    self._set_status(str(result["warning"]), warn=True)
            else:
                self._mark_errors(result.get("errors") or {})
                self._set_status("Не сохранено: "
                                 f"{result.get('error') or 'ошибка'}.",
                                 warn=True)
            return result

        def browse_sound_file(self):
            """Выбрать свой .wav (файл не копируем — только путь)."""
            try:
                path, _filter = QFileDialog.getOpenFileName(
                    self, "Свой звук уведомления", "",
                    "Звуки (*.wav);;Все файлы (*)")
            except Exception:
                path = ""
            if path:
                self.edit_sound_file.setText(str(path))
                return True
            return False

        def check_updates(self):
            """«Проверить обновление»: фоновая проверка, окно не блокируется."""
            if self._update_busy:
                return False
            if not callable(self.check_update_fn):
                self._set_status("Обновления недоступны в этой сборке.",
                                 warn=True)
                self.lbl_update_status.setText(
                    "Проверка обновлений недоступна в этой сборке.")
                return False
            self._update_busy = True
            self.btn_check_updates.setEnabled(False)
            self.btn_check_updates.setText("Проверяю…")
            self.btn_update_apply.setVisible(False)
            self.lbl_update_notes.setText("")
            self.lbl_update_status.setText("Проверяю обновления…")
            self.lbl_update_checked.setText(
                "Последняя проверка: идёт сейчас…")

            def worker():
                try:
                    result = self.check_update_fn() or {}
                except Exception as exc:
                    result = {"checked": False, "available": False,
                              "error": f"{type(exc).__name__}: {exc}"}
                try:
                    self.update_checked.emit(dict(result))
                except RuntimeError:
                    pass

            threading.Thread(target=worker, daemon=True,
                             name="wf-settings-update").start()
            return True

        def _on_update_checked(self, result):
            self.update_info = dict(result or {})
            self._update_busy = False
            self.btn_check_updates.setEnabled(True)
            self.btn_check_updates.setText("Проверить обновление")
            self.last_checked = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
            self.lbl_update_checked.setText(
                f"Последняя проверка: {self.last_checked}")
            info = self.update_info
            if info.get("skipped"):
                self.lbl_update_status.setText(
                    f"Обновления не настроены: {info['skipped']}.")
                self.lbl_update_status.setStyleSheet(
                    f"color:{color('warning')};")
                self.btn_update_apply.setVisible(False)
                return
            if info.get("error"):
                self.lbl_update_status.setText(
                    f"Не удалось проверить обновления: {info['error']}.")
                self.lbl_update_status.setStyleSheet(
                    f"color:{color('warning')};")
                self.btn_update_apply.setVisible(False)
                return
            if info.get("available"):
                version = info.get("version") or "?"
                self.lbl_update_status.setText(
                    f"Доступна версия {version}. "
                    "Нажмите «Обновить и перезапустить».")
                self.lbl_update_status.setStyleSheet(f"color:{color('text')};")
                notes = str(info.get("notes") or "").strip()
                self.lbl_update_notes.setText(
                    f"Что нового: {notes}" if notes else "")
                self.btn_update_apply.setVisible(True)
                self.btn_update_apply.setEnabled(True)
                self._set_status(f"Доступна версия {version}.")
                return
            current = info.get("current") or self.version
            self.lbl_update_status.setText(
                f"Обновлений нет: у вас последняя версия"
                + (f" ({current})." if current else "."))
            self.lbl_update_status.setStyleSheet(f"color:{color('text')};")
            self.btn_update_apply.setVisible(False)
            self._set_status("Обновлений нет.")

        def apply_and_restart(self):
            """Установить доступное обновление и перезапустить приложение."""
            if self._update_busy:
                return False
            manifest = (self.update_info or {}).get("manifest")
            if not manifest:
                self._set_status("Сначала проверьте обновление.", warn=True)
                return False
            if not callable(self.apply_update_fn):
                self._set_status("Установка обновлений недоступна.",
                                 warn=True)
                return False
            self._update_busy = True
            self.btn_check_updates.setEnabled(False)
            self.btn_update_apply.setEnabled(False)
            self.btn_update_apply.setText("Обновляю…")
            self.lbl_update_status.setText(
                f"Скачиваю версию "
                f"{self.update_info.get('version') or '?'}…")

            def worker():
                try:
                    result = self.apply_update_fn(manifest) or {}
                except Exception as exc:
                    result = {"ok": False,
                              "error": f"{type(exc).__name__}: {exc}"}
                try:
                    self.update_applied.emit(dict(result))
                except RuntimeError:
                    pass

            threading.Thread(target=worker, daemon=True,
                             name="wf-settings-apply").start()
            return True

        def _on_update_applied(self, result):
            result = dict(result or {})
            self._update_busy = False
            self.btn_check_updates.setEnabled(True)
            self.btn_update_apply.setText("Обновить и перезапустить")
            if not result.get("ok") or result.get("error"):
                self.btn_update_apply.setEnabled(True)
                self.lbl_update_status.setText(
                    "Не удалось обновить: "
                    f"{result.get('error') or 'неизвестная ошибка'}.")
                self.lbl_update_status.setStyleSheet(
                    f"color:{color('warning')};")
                return
            version = (result.get("version")
                       or self.update_info.get("version") or "?")
            self.lbl_update_status.setText(
                f"Обновление {version} установлено — "
                "перезапускаю приложение…")
            self.btn_update_apply.setVisible(False)
            if callable(self.restart_fn):
                try:
                    self.restart_fn()
                except Exception as exc:
                    self.lbl_update_status.setText(
                        "Обновление установлено, но перезапуск не удался "
                        f"({type(exc).__name__}). Запустите приложение вручную.")

        def test_notification(self):
            """«Проверить уведомление»: ТЕСТ-запись/тост/трей без кулдаунов."""
            kind = str(self.cmb_test_kind.currentData() or "market")
            if not callable(self.on_test_notification):
                self._set_status("Тест уведомлений недоступен в этой сборке.",
                                 warn=True)
                return False
            try:
                result = self.on_test_notification(kind) or {}
            except Exception as exc:
                self._set_status(f"тест: {exc}", warn=True)
                return False
            if isinstance(result, dict) and result.get("ok") is False:
                self._set_status("Тест не отправлен: "
                                 f"{result.get('error') or 'ошибка'}",
                                 warn=True)
                return False
            self._set_status("ТЕСТ отправлен: история/тост/звук.")
            return True

        def open_data(self):
            opener = self.on_open_data_dir
            if open_data_folder(self.data_dir, opener=opener):
                self._set_status("Папка данных открыта.")
                return True
            self._set_status("Не удалось открыть папку данных.", warn=True)
            return False

    return SettingsDialog


def wf_version_fallback():
    """Версия приложения без импорта warframe (на случай ранних вызовов)."""
    try:
        import warframe as wf
        return str(getattr(wf, "APP_VERSION", "—"))
    except Exception:
        return "—"
