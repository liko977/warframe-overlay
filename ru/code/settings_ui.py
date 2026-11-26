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

try:
    import ui_safe as _ui_safe
except Exception:                       # pragma: no cover
    _ui_safe = None


def _human(context, exc=None, text="не удалось — подробности в app.log"):
    """1.0.0: человеческий текст для UI, исключение — одной строкой в app.log."""
    if _ui_safe is not None:
        return _ui_safe.human_error(context, exc, fallback=text)
    return str(text)


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
    ("pick", "Выбор награды Alt+1…4"),
)

#: Негорячие параметры по умолчанию (совпадают с warframe.DEFAULT_CONFIG).
DEFAULT_VALUES = {
    "notify_enabled": True,
    "notify_sound": True,
    "notify_pick_toast": True,
    "notify_skip_when_game_focused": False,
    "notify_events": ["order_bought", "price_drop", "baro_arrival",
                      "baro_leaving", "fissure_ending", "chat", "pick"],
    "notify_price_drop_pct": 10,
    "notify_fissure_tiers": ["Axi", "Neo"],
    "notify_mode": "overlay",
    "notify_toast_sec": 6,
    "notify_toast_opacity": 0.95,
    "notify_chat_color": "#4aa8ff",
    "notify_market_color": "#43d17c",
    "notify_price_color": "#f5c451",
    "notify_sound_choice": "auto",
    "notify_sound_file": "",
    "overlay_layout": "bottom",
    "plates_click_through": False,
    "plate_boxes": {},
    "region_card_ttl": 5.0,
    "overlay_ttl": 5.0,
    "auto_detect": True,
    "font_scale": 1.0,
    "overlay_opacity": 1.0,
    "currency_platinum_color": "#E7F1FF",
    "currency_ducats_color": "#C9A66B",
    "show_summary": True,
    "show_item_icons": True,
    "icons_allow_manual_download": True,
    "overlay_show_set_price": True,
    "show_set_progress": True,
    "currency_icons": True,
    # 1.0.0: пакет иконок — автообновление раз в N дней (0 = выключено)
    "icons_update_days": 7,
    # 1.0.0: тема интерфейса Warframe («auto» или имя из 19 тем WFInfo)
    "game_theme": "auto",
    "update_check": True,
    "update_url": ("https://raw.githubusercontent.com/liko977/"
                   "warframe-overlay/main/ru/update.json"),
    "update_disabled": False,
    "config_version": 2,
    "update_check_interval_min": 180,
}

DEFAULT_UPDATE_URL = ("https://raw.githubusercontent.com/liko977/"
                      "warframe-overlay/main/ru/update.json")

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

#: Русская раскладка → латинские имена клавиш (ЙЦУКЕН).
RU_TO_EN = {
    "й": "q", "ц": "w", "у": "e", "к": "r", "е": "t", "н": "y",
    "г": "u", "ш": "i", "щ": "o", "з": "p", "х": "[", "ъ": "]",
    "ф": "a", "ы": "s", "в": "d", "а": "f", "п": "g", "р": "h",
    "о": "j", "л": "k", "д": "l", "ж": ";", "э": "'", "я": "z",
    "ч": "x", "с": "c", "м": "v", "и": "b", "т": "n", "ь": "m",
    "б": ",", "ю": ".",
}
EN_TO_RU = {value: key for key, value in RU_TO_EN.items()
            if len(value) == 1}
_KEY_PUNCT = set("[];',./")

#: Имена клавиш, которые библиотека ``keyboard`` понимает на всех версиях
#: (латиница/цифры/F-клавиши + безопасная пунктуация). Кириллические имена
#: не поддерживаются: RU-раскладка работает по физической клавише/скан-коду.
_KEYBOARD_PUNCT = set(";.,/-='`")


def keyboard_hotkey_variants(value, keyboard_module=None):
    """Варианты хоткея ДЛЯ БИБЛИОТЕКИ keyboard: только поддерживаемые имена.

    Кириллические варианты (``alt+й``) не отдаём — они приводят к
    ``ValueError: Key 'й' is not mapped``. На русской раскладке хоткей
    работает по физической клавише: ``alt+й`` канонизируется в ``alt+q``,
    а ``keyboard`` регистрирует scan-code именно этой клавиши.

    Если передан ``keyboard_module`` и у него есть ``parse_hotkey``, имя
    дополнительно проверяется этой функцией (точная поддержка версии).
    """
    canon = normalize_hotkey(value)
    if not canon:
        return []
    base = canon.split("+")[-1]
    supported = (base.isascii()
                 and (base.isalnum() or base in _KEYBOARD_PUNCT))
    if not supported:
        return []
    if keyboard_module is not None:
        parse = getattr(keyboard_module, "parse_hotkey", None)
        if callable(parse):
            try:
                parse(canon)
            except Exception:
                return []
    return [canon]


def short_keyboard_error(key, keyboard_module=None):
    """Понятная строка для лога/настроек вместо repr исключения keyboard."""
    return ("Хоткей " + str(key or "?") +
            " не поддерживается keyboard — использую скан-код")


def log_skipped_hotkeys(skipped, logger=None):
    """Записать пропущенные (кириллические) варианты ОДНОЙ строкой на клавишу."""
    target = logger
    if not callable(target):
        try:
            import warframe as _wf
            target = getattr(_wf, "log", None)
        except Exception:
            target = None
    if not callable(target):
        return 0
    seen = set()
    count = 0
    for key in skipped or []:
        text = str(key or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        try:
            target(short_keyboard_error(text))
            count += 1
        except Exception:
            pass
    return count



def canon_key(token):
    """Канонизировать одну клавишу/модификатор (RU → EN)."""
    token = str(token or "").strip().lower()
    return RU_TO_EN.get(token, token)


def canon_hotkey(value):
    """Каноническая комбинация: ``' Alt + Ц '`` → ``'alt+w'``.

    Русские буквы переводятся в латинские имена клавиш; цифры/F-клавиши и
    специальные имена не меняются. Это единая точка для config, захвата,
    валидации и поиска дубликатов.
    """
    return normalize_hotkey(value)


def hotkey_variants(value):
    """[канонический вариант, русский вариант] для регистрации хуков."""
    key = canon_hotkey(value)
    if not key:
        return []
    parts = key.split("+")
    last = parts[-1]
    ru = EN_TO_RU.get(last)
    out = [key]
    if ru:
        ru_key = "+".join(parts[:-1] + [ru])
        if ru_key != key:
            out.append(ru_key)
    return out



def default_hotkeys():
    return {action: default for action, _label, default in HOTKEY_ACTIONS}


def default_values():
    values = {key: (list(value) if isinstance(value, list) else value)
              for key, value in DEFAULT_VALUES.items()}
    values.update(default_hotkeys())
    values["pick_hotkeys"] = [default_hotkeys()[f"pick{i}"] for i in range(1, 5)]
    return values


def _game_theme_choices():
    """«Авто» + 19 тем WFInfo для выпадающего списка (game_themes.py)."""
    try:
        import game_themes
        names = game_themes.manual_names()
        if names:
            return names
    except Exception:
        pass
    return ["AUTO"] + [
        "VITRUVIAN", "STALKER", "BARUUK", "CORPUS", "FORTUNA", "GRINEER",
        "LOTUS", "NIDUS", "OROKIN", "TENNO", "HIGH_CONTRAST", "LEGACY",
        "EQUINOX", "DARK_LOTUS", "ZEPHYR", "CONQUERA", "DEADLOCK",
        "LUNAR_RENEWAL", "POM_2"]


def _fmt_bytes(value):
    """Байты → «27.1 МБ» (подпись размера пака иконок)."""
    try:
        size = float(value or 0)
    except Exception:
        size = 0.0
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024 or unit == "ГБ":
            return f"{int(size)} Б" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} ГБ"


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
        token = canon_key(token)
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
    if key in _KEY_PUNCT:
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
        #: 1.0.0: кириллические варианты и ошибки — только для лога/сводки
        self.skipped = []
        self.errors = []

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
        handled = self._handles.pop(action, None)
        if handled is not None:
            try:
                for handle in (handled if isinstance(handled, list)
                               else [handled]):
                    self.keyboard.remove_hotkey(handle)
                return True
            except Exception:
                return False
        if not key:
            return False
        ok = False
        for variant in keyboard_hotkey_variants(key, self.keyboard):
            try:
                self.keyboard.remove_hotkey(variant)
                ok = True
            except Exception:
                pass
        return ok

    def _add(self, action, key):
        callback = self.callbacks.get(action)
        if not callable(callback):
            return False
        # 1.0.0: только имена, которые keyboard реально понимает; кириллица
        # регистрируется по физической клавише (scan-code), без ValueError.
        variants = keyboard_hotkey_variants(key, self.keyboard)
        if not variants:
            self.skipped.append(str(key or ""))
            return False
        handles = []
        errors = 0
        for variant in variants:
            try:
                handles.append(self.keyboard.add_hotkey(
                    variant, callback, suppress=False))
            except Exception as exc:
                errors += 1
                self.errors.append(f"{type(exc).__name__}")
        if not handles:
            if errors:
                raise RuntimeError("клавиша не зарегистрировалась")
            return False
        self._handles[action] = handles
        return True

    def apply(self, mapping):
        """Снять старые клавиши и поставить новые. При ошибке — откат.

        Возвращает ``{"ok", "keys", "registered", "error", "restored"}``.
        """
        desired = {action: normalize_hotkey(value)
                   for action, value in (mapping or {}).items()}
        desired = {action: key for action, key in desired.items() if key}
        old_keys = dict(self._keys)
        self.skipped = []
        self.errors = []
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
            if self.skipped:
                log_skipped_hotkeys(self.skipped)
            return {"ok": True, "keys": dict(self._keys),
                    "registered": registered,
                    "skipped": list(self.skipped)}
        except Exception as exc:
            # убрать частично поставленные новые
            for action in list(self._handles):
                self._remove(action)
            self._keys = {}
            log_skipped_hotkeys(self.skipped)
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
                "error": ("клавиша не зарегистрировалась — оставил прежнюю "
                          "(подробности в app.log)"),
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
        return {"ok": False, "error": _human(
            "настройки (бэкап)", exc,
            "бэкап config.json не создан — подробности в app.log")}
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
        return {"ok": False, "error": _human(
            "настройки (сохранение)", exc,
            "config.json не сохранён — подробности в app.log")}
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
            result["warning"] = _human(
                "настройки (применение на лету)", exc,
                "сохранено, но применить на лету не удалось — "
                "подробности в app.log")
    return result


def open_data_folder(path, opener=None):
    """Открыть папку данных в проводнике. ``opener`` — подмена для тестов."""
    path = os.path.abspath(str(path or "."))
    if not os.path.isdir(path):
        return False
    return _open_path(path, opener)


def open_log_file(path, opener=None):
    """Открыть app.log; без файла — False."""
    path = os.path.abspath(str(path or ""))
    if not os.path.isfile(path):
        return False
    return _open_path(path, opener)


def _open_path(path, opener=None):
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
                                 QHBoxLayout, QLabel, QLineEdit, QProgressBar,
                                 QPushButton, QScrollArea, QSpinBox, QTabWidget,
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
            self._ru_hint = ""
            self.setFont(font(11))
            self.setCursor(Qt.PointingHandCursor)
            self.setMinimumWidth(170)
            self.setFocusPolicy(Qt.StrongFocus)
            self._render()
            self.clicked.connect(self.start_capture)

        def _render(self):
            if self._value and self._ru_hint:
                head, _sep, last = self._value.rpartition("+")
                shown = last.upper() + f" ({self._ru_hint})"
                self.setText((head + "+" if head else "") + shown)
            else:
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
            self._ru_hint = ""
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
                ru_char = str(event.text() or "").strip().lower()
                self._ru_hint = (ru_char if ru_char in RU_TO_EN else "")
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
            Qt.Key_Semicolon: ";", Qt.Key_Comma: ",", Qt.Key_Period: ".",
            Qt.Key_BracketLeft: "[", Qt.Key_BracketRight: "]",
            Qt.Key_Apostrophe: "'", Qt.Key_Slash: "/",
        }
        return special.get(key, "")

    def _event_combo(event):
        name = _qt_key_name(event.key())
        if not name:
            text = str(event.text() or "").strip().lower()
            if text in RU_TO_EN:
                name = RU_TO_EN[text]
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
                                 QProgressBar, QPushButton, QTabWidget,
                                 QVBoxLayout, QWidget, QDoubleSpinBox,
                                 QSpinBox)

    class SettingsDialog(QDialog):
        """Окно настроек: клавиши + уведомления + оверлей + обновления."""

        update_checked = pyqtSignal(dict)
        update_applied = pyqtSignal(dict)

        def __init__(self, cfg=None, apply_fn=None, on_open_data_dir=None,
                     on_test_notification=None, check_update_fn=None,
                     apply_update_fn=None, restart_fn=None, auto_check=False,
                     data_dir="", version="", parent=None, on_open_log=None,
                     icons_status_fn=None, icons_download_fn=None,
                     icons_update_fn=None, icons_clear_fn=None,
                     icons_cancel_fn=None, confirm_fn=None,
                     on_scan_calibrate=None):
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
            self.on_open_log = on_open_log
            self.on_test_notification = on_test_notification
            # 1.0.0: пакет иконок — состояние, кнопки, прогресс в «Прочем»
            self.icons_status_fn = icons_status_fn
            self.icons_download_fn = icons_download_fn
            self.icons_update_fn = icons_update_fn
            self.icons_clear_fn = icons_clear_fn
            self.icons_cancel_fn = icons_cancel_fn
            self.confirm_fn = confirm_fn
            self.on_scan_calibrate = on_scan_calibrate
            self._icons_busy = False
            self.last_icons_result = {}
            self.data_dir = data_dir
            self.version = version
            self.last_result = {}
            self.edits = {}
            self.event_boxes = {}
            self._defaults = default_values()
            self.setWindowTitle("Настройки")
            self.setModal(True)
            self.setMinimumSize(760, 640)
            self.setStyleSheet(
                qss() + " QLineEdit, QSpinBox, QDoubleSpinBox {"
                " font-family:'Consolas', monospace; }")
            try:
                import login_mode
                login_mode.register_dialog(self)      # B2: F9 закроет окно
            except Exception:
                pass

            layout = QVBoxLayout(self)
            layout.setContentsMargins(16, 14, 16, 12)
            layout.setSpacing(10)
            head = QHBoxLayout()
            title = QLabel("<b>Настройки VoidFrameRU</b>")
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
            # 1.0.0: два места для «Показывать иконки» — держим в синхроне
            try:
                self.chk_show_item_icons.toggled.connect(
                    self._sync_icons_checkboxes)
                self.chk_icons_show.toggled.connect(
                    self._sync_icons_checkboxes)
            except Exception:
                pass

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
            self.chk_notify_sound = QCheckBox("Звук уведомлений")
            self.chk_notify_sound.setToolTip(
                "Выключено — полная тишина (тосты, чат, лут, трей)")
            self.chk_notify_skip_focus = QCheckBox(
                "Только когда игра НЕ в фокусе")
            self.chk_notify_pick_toast = QCheckBox(
                "Тост при выборе награды Alt+1…4")
            for box in (self.chk_notify_enabled, self.chk_notify_sound,
                        self.chk_notify_skip_focus, self.chk_notify_pick_toast):
                box.setFont(font(11))
                outer.addWidget(box)
            outer.addWidget(muted(
                "Звук уведомлений — мастер: выключено = полная тишина "
                "(тосты, чат, лут, трей)."))
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
            self.spin_toast_opacity = QDoubleSpinBox()
            self.spin_toast_opacity.setRange(0.6, 1.0)
            self.spin_toast_opacity.setSingleStep(0.05)
            self.spin_toast_opacity.setDecimals(2)
            self.spin_toast_opacity.setFont(font(11))
            form.addRow(QLabel("Непрозрачность подложки тоста"),
                        self.spin_toast_opacity)

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
            self.chk_notify_sound.toggled.connect(
                self._update_sound_controls)
            self._update_sound_controls()
            outer.addLayout(form)
            outer.addStretch(1)
            return page

        def _update_sound_controls(self, *_args):
            enabled = bool(self.chk_notify_sound.isChecked())
            for widget in (self.cmb_sound_choice, self.edit_sound_file,
                           self.btn_sound_browse):
                widget.setEnabled(enabled)

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
            self.chk_plates_click = QCheckBox(
                "Плашки не мешают мыши (click-through)")
            self.chk_plates_click.setFont(font(11))
            form.addRow(QLabel(""), self.chk_plates_click)
            self.btn_reset_boxes = QPushButton("Сбросить расположение")
            self.btn_reset_boxes.setStyleSheet(button_qss())
            self.btn_reset_boxes.clicked.connect(self.reset_plate_boxes)
            form.addRow(QLabel(""), self.btn_reset_boxes)
            self.spin_region_ttl = QDoubleSpinBox()
            self.spin_region_ttl.setRange(0.0, 30.0)
            self.spin_region_ttl.setSingleStep(1.0)
            self.spin_region_ttl.setDecimals(1)
            self.spin_region_ttl.setSuffix(" с")
            self.spin_region_ttl.setSpecialValueText("не скрывать")
            form.addRow(QLabel("Карточка Alt+D: скрывать через"),
                        self.spin_region_ttl)
            self.spin_overlay_ttl = QDoubleSpinBox()
            self.spin_overlay_ttl.setRange(0.0, 30.0)
            self.spin_overlay_ttl.setSingleStep(1.0)
            self.spin_overlay_ttl.setDecimals(1)
            self.spin_overlay_ttl.setSuffix(" с")
            self.spin_overlay_ttl.setSpecialValueText("не скрывать")
            self.spin_overlay_ttl.setToolTip("0 — не скрывать")
            form.addRow(QLabel("Плашки наград: скрывать через"),
                        self.spin_overlay_ttl)
            self.chk_auto_detect = QCheckBox(
                "Автоматически показывать цены при открытии реликвии")
            self.chk_auto_detect.setToolTip(
                "Alt+Q всегда пересканирует вручную")
            self.chk_auto_detect.setFont(font(11))
            form.addRow(QLabel(""), self.chk_auto_detect)
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
            self.chk_show_item_icons = QCheckBox("Иконки предметов")
            self.chk_show_item_icons.setFont(font(11))
            self.chk_show_item_icons.setToolTip(
                "Иконки WFCD в плашках, «Дропе» и «Маркете»; загрузка в фоне")
            form.addRow(QLabel(""), self.chk_show_item_icons)
            self.chk_show_set_price = QCheckBox("Показывать цену комплекта")
            self.chk_show_set_price.setFont(font(11))
            self.chk_show_set_price.setToolTip(
                "На плашках частей — мелкая строка «компл. N p»")
            form.addRow(QLabel(""), self.chk_show_set_price)
            # 1.0.0: цены картинками платины/дукатов вместо «p»/«дк»
            self.chk_currency_icons = QCheckBox("Показывать иконки валют")
            self.chk_currency_icons.setFont(font(11))
            self.chk_currency_icons.setToolTip(
                "Платина и дукаты — картинкой в плашках, «Дропе» и "
                "«Маркете». Выключено — старые «p» / «дк»")
            form.addRow(QLabel(""), self.chk_currency_icons)

            def currency_combo(current, choices):
                combo = QComboBox()
                for value in choices:
                    combo.addItem(value, value)
                index = combo.findData(current)
                if index < 0 and current:
                    combo.addItem(f"{current} (текущий)", current)
                    index = combo.count() - 1
                combo.setCurrentIndex(max(0, index))
                combo.setFont(font(11))
                return combo

            palette = (theme.PALETTE if theme is not None else {})
            self.cmb_currency_platinum = currency_combo(
                str(self.cfg.get("currency_platinum_color")
                    or palette.get("currency_platinum") or "#E7F1FF"),
                getattr(theme, "PLATINUM_CHOICES", ("#E7F1FF",)))
            self.cmb_currency_ducats = currency_combo(
                str(self.cfg.get("currency_ducats_color")
                    or palette.get("currency_ducats") or "#C9A66B"),
                getattr(theme, "DUCATS_CHOICES", ("#C9A66B",)))
            form.addRow(QLabel("Цвет платины (p)"),
                        self.cmb_currency_platinum)
            form.addRow(QLabel("Цвет дукатов (дк)"),
                        self.cmb_currency_ducats)
            # 1.0.0: тема интерфейса Warframe — данные WFInfo, ручной выбор
            self.cmb_game_theme = QComboBox()
            self.cmb_game_theme.setFont(font(11))
            self.cmb_game_theme.setToolTip(
                "Тема интерфейса игры: «Авто» — определять по кадру "
                "(проба цвета), либо выберите вручную из 19 тем WFInfo")
            for theme_name in _game_theme_choices():
                label = ("Авто (по кадру)" if theme_name == "AUTO"
                         else theme_name)
                self.cmb_game_theme.addItem(
                    label, "auto" if theme_name == "AUTO" else theme_name)
            form.addRow(QLabel("Тема интерфейса"), self.cmb_game_theme)
            # 1.0.0: мастер калибровки скана под разрешение/DPI
            scan_row = QHBoxLayout()
            self.btn_scan_calibrate = QPushButton("Калибровка скана")
            self.btn_scan_calibrate.setStyleSheet(button_qss(small=True))
            self.btn_scan_calibrate.setCursor(Qt.PointingHandCursor)
            self.btn_scan_calibrate.setToolTip(
                "Снимок экрана наград, авто-определение карточек под ваше "
                "разрешение и масштаб Windows, пробный скан")
            self.btn_scan_calibrate.clicked.connect(self.calibrate_scan)
            self.btn_scan_calibrate.setVisible(False)
            self.btn_scan_calibrate.setToolTip(
                "Устарело (1.0.3): используйте готовый пресет раскладки")
            scan_row.addWidget(self.btn_scan_calibrate)
            try:
                import scan_presets as _sp
                self.cmb_scan_preset = QComboBox()
                width = int((self.cfg or {}).get("base_width") or 1920)
                height = int((self.cfg or {}).get("base_height") or 1080)
                for pid, label in _sp.PRESETS:
                    self.cmb_scan_preset.addItem(
                        f"{label} · экран {width}×{height}", pid)
                self.cmb_scan_preset.setFont(font(11))
                current = str((self.cfg or {}).get("scan_preset") or "auto")
                index = self.cmb_scan_preset.findData(current)
                self.cmb_scan_preset.setCurrentIndex(index if index >= 0 else 0)
                scan_row.addWidget(self.cmb_scan_preset, 1)
            except Exception:
                self.cmb_scan_preset = None
            scan_row.addStretch(1)
            form.addRow(QLabel("Координаты карточек"), scan_row)
            outer.addLayout(form)
            self.lbl_scan_calibration = QLabel("калибровка: —")
            self.lbl_scan_calibration.setFont(font(10))
            self.lbl_scan_calibration.setWordWrap(True)
            self.lbl_scan_calibration.setStyleSheet(
                f"color:{color('muted')}; background:transparent;")
            self.lbl_scan_preview = QLabel("")
            self.lbl_scan_preview.setFixedSize(192, 108)
            self.lbl_scan_preview.setStyleSheet(
                "background:#0d131c; border:1px solid #4A7CA6;")
            outer.addWidget(self.lbl_scan_preview)
            self.lbl_scan_preview_hint = QLabel(
                "карточки должны попасть в рамки")
            self.lbl_scan_preview_hint.setFont(font(9))
            self.lbl_scan_preview_hint.setStyleSheet(
                f"color:{color('muted')};")
            outer.addWidget(self.lbl_scan_preview_hint)
            try:
                self.cmb_scan_preset.currentIndexChanged.connect(
                    self._scan_preset_changed)
            except Exception:
                pass
            self._draw_scan_preset_preview()
            outer.addWidget(self.lbl_scan_calibration)
            self.refresh_scan_calibration()
            outer.addWidget(muted(
                "Без калибровки координаты берутся из стандарта 1920×1080; "
                "авто-определение подхватывает раскладку на 2K/4K и при "
                "масштабе Windows 125–150%."))
            outer.addWidget(muted(
                "Платина подписывается «p», дукаты — «дк»; цвета валют "
                "применяются сразу и не могут совпадать."))
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
            self.lbl_update_active = QLabel("")
            self.lbl_update_active.setFont(font(10))
            self.lbl_update_active.setStyleSheet(
                f"color:{color('muted')}; background:transparent;")
            self.lbl_update_active.setVisible(False)
            outer.addWidget(self.lbl_update_active)
            self.chk_update_check = QCheckBox(
                "Проверять автоматически (в фоне)")
            self.chk_update_check.setFont(font(11))
            outer.addWidget(self.chk_update_check)
            self.chk_update_disabled = QCheckBox("Не проверять обновления")
            self.chk_update_disabled.setFont(font(11))
            outer.addWidget(self.chk_update_disabled)
            form = QFormLayout()
            form.setSpacing(8)
            self.edit_update_url = QLineEdit()
            self.edit_update_url.setFont(font(11))
            self.edit_update_url.setPlaceholderText(
                "https://raw.githubusercontent.com/.../ru/update.json")
            form.addRow(QLabel("URL манифеста обновлений"), self.edit_update_url)
            self.spin_update_interval = QSpinBox()
            self.spin_update_interval.setRange(15, 1440)
            self.spin_update_interval.setSuffix(" минут")
            self.spin_update_interval.setFont(font(11))
            self.spin_update_interval.setToolTip(
                "Как часто проверять обновления, пока приложение запущено")
            form.addRow(QLabel("Интервал автопроверки"),
                        self.spin_update_interval)
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
            self.btn_restart_now = QPushButton("Перезапустить сейчас")
            self.btn_restart_now.setStyleSheet(button_qss(small=True))
            self.btn_restart_now.setCursor(Qt.PointingHandCursor)
            self.btn_restart_now.setVisible(False)
            self.btn_restart_now.clicked.connect(self.restart_now)
            row.addWidget(self.btn_restart_now)
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
            # ── 1.0.0: пакет иконок предметов ──────────────────────────
            icons_title = QLabel("<b>Иконки предметов</b>")
            icons_title.setFont(font(12, True))
            icons_title.setStyleSheet(
                f"color:{color('text')}; background:transparent;")
            outer.addWidget(icons_title)
            self.lbl_icons_state = QLabel("состояние: —")
            self.lbl_icons_state.setFont(font(10))
            self.lbl_icons_state.setStyleSheet(
                f"color:{color('muted')}; background:transparent;")
            self.lbl_icons_state.setWordWrap(True)
            self.lbl_icons_state.setTextInteractionFlags(Qt.TextSelectableByMouse)
            outer.addWidget(self.lbl_icons_state)
            self.lbl_icons_progress = QLabel("")
            self.lbl_icons_progress.setFont(font(10))
            self.lbl_icons_progress.setStyleSheet(
                f"color:{color('accent')}; background:transparent;")
            self.lbl_icons_progress.setWordWrap(True)
            outer.addWidget(self.lbl_icons_progress)
            self.bar_icons = QProgressBar()
            self.bar_icons.setRange(0, 100)
            self.bar_icons.setValue(0)
            self.bar_icons.setTextVisible(False)
            self.bar_icons.setFixedHeight(8)
            self.bar_icons.setStyleSheet(
                "QProgressBar { background-color:%s; border:1px solid %s;"
                " border-radius:4px; }"
                " QProgressBar::chunk { background-color:%s;"
                " border-radius:3px; }"
                % (color("card"), color("border"), color("accent")))
            self.bar_icons.setVisible(False)
            outer.addWidget(self.bar_icons)
            icons_row = QHBoxLayout()
            icons_row.setSpacing(8)
            self.btn_icons_download = QPushButton("Обновить библиотеку вручную")
            self.btn_icons_update = QPushButton("Обновить сейчас")
            self.btn_icons_clear = QPushButton("Очистить кэш иконок")
            self.btn_icons_cancel = QPushButton("Отмена")
            for button, slot, tip in (
                    (self.btn_icons_download, self.download_icons,
                     "Скачать весь пакет иконок (можно отменить; продолжится "
                     "при следующем запуске)"),
                    (self.btn_icons_update, self.update_icons,
                     "Сверить каталог и докачать только новые/изменённые"),
                    (self.btn_icons_clear, self.clear_icons,
                     "Удалить скачанные иконки (с подтверждением)"),
                    (self.btn_icons_cancel, self.cancel_icons,
                     "Прервать загрузку иконок (скачанное не теряется)")):
                button.setStyleSheet(button_qss(small=True))
                button.setCursor(Qt.PointingHandCursor)
                button.setToolTip(tip)
                button.clicked.connect(slot)
                icons_row.addWidget(button)
            icons_row.addStretch(1)
            outer.addLayout(icons_row)
            icons_form = QFormLayout()
            icons_form.setSpacing(8)
            self.chk_icons_show = QCheckBox("Показывать иконки предметов")
            self.chk_icons_show.setFont(font(11))
            self.chk_icons_show.setToolTip(
                "То же, что «Иконки предметов» на вкладке «Оверлей»")
            icons_form.addRow(QLabel(""), self.chk_icons_show)
            self.spin_icons_update_days = QSpinBox()
            self.spin_icons_update_days.setRange(0, 60)
            self.spin_icons_update_days.setSuffix(" дн")
            self.spin_icons_update_days.setSpecialValueText("выключено")
            self.spin_icons_update_days.setFont(font(11))
            self.spin_icons_update_days.setToolTip(
                "Раз в N дней сверять каталог и докачивать новые иконки "
                "(0 — автообновление выключено)")
            icons_form.addRow(QLabel("Автообновление иконок"),
                              self.spin_icons_update_days)
            outer.addLayout(icons_form)
            outer.addWidget(muted(
                "Пакет скачивается один раз (~27 МБ, 5–10 минут при 3 "
                "запросах/с) и дальше иконки берутся только с диска — без "
                "сети. Отмена не теряет скачанное: загрузка продолжится при "
                "следующем запуске. Устаревшие файлы удаляются автоматически."))
            self.refresh_icons_status()
            self.btn_open_data = QPushButton("Открыть папку данных")
            self.btn_open_data.setStyleSheet(button_qss())
            self.btn_open_data.setCursor(Qt.PointingHandCursor)
            self.btn_open_data.clicked.connect(self.open_data)
            row = QHBoxLayout()
            row.addWidget(self.btn_open_data)
            self.btn_open_log = QPushButton("Открыть app.log")
            self.btn_open_log.setStyleSheet(button_qss())
            self.btn_open_log.setCursor(Qt.PointingHandCursor)
            self.btn_open_log.clicked.connect(self.open_log)
            row.addWidget(self.btn_open_log)
            self.btn_open_diag = QPushButton("Открыть папку диагностики")
            self.btn_open_diag.setStyleSheet(button_qss())
            self.btn_open_diag.setCursor(Qt.PointingHandCursor)
            self.btn_open_diag.clicked.connect(self.open_diagnostics)
            row.addWidget(self.btn_open_diag)
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
            self.chk_notify_pick_toast.setChecked(
                bool(values.get("notify_pick_toast", True)))
            self.chk_notify_sound.setChecked(
                bool(values.get("notify_sound", True)))
            self._update_sound_controls()
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
            try:
                self.spin_toast_opacity.setValue(
                    float(values.get("notify_toast_opacity", 0.95) or 0.95))
            except Exception:
                self.spin_toast_opacity.setValue(0.95)
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
            self.chk_plates_click.setChecked(
                bool(values.get("plates_click_through", False)))
            self.spin_region_ttl.setValue(
                float(values.get("region_card_ttl", 5.0) or 0))
            self.spin_overlay_ttl.setValue(
                float(values.get("overlay_ttl", 5.0) or 0))
            self.chk_auto_detect.setChecked(
                bool(values.get("auto_detect", True)))
            layout_value = str(values.get("overlay_layout") or "bottom")
            index = self.cmb_layout.findData(layout_value)
            self.cmb_layout.setCurrentIndex(index if index >= 0 else 0)
            self.spin_font_scale.setValue(
                float(values.get("font_scale", 1.0) or 1.0))
            self.spin_opacity.setValue(
                float(values.get("overlay_opacity", 1.0) or 1.0))
            self.chk_show_summary.setChecked(
                bool(values.get("show_summary", True)))
            self.chk_show_item_icons.setChecked(
                bool(values.get("show_item_icons", True)))
            self.chk_show_set_price.setChecked(
                bool(values.get("overlay_show_set_price", True)))
            self.chk_currency_icons.setChecked(
                bool(values.get("currency_icons", True)))
            # 1.0.0: ручной выбор темы интерфейса (иначе «Авто»)
            try:
                theme_value = str(values.get("game_theme") or "auto")
                index = self.cmb_game_theme.findData(theme_value)
                if index < 0:
                    index = self.cmb_game_theme.findData(theme_value.upper())
                self.cmb_game_theme.setCurrentIndex(max(0, index))
            except Exception:
                pass
            # 1.0.0: блок иконок в «Прочем»
            try:
                self.chk_icons_show.blockSignals(True)
                self.chk_icons_show.setChecked(
                    bool(values.get("show_item_icons", True)))
                self.chk_icons_show.blockSignals(False)
                self.spin_icons_update_days.setValue(
                    int(values.get("icons_update_days", 7) or 0))
                self.refresh_icons_status()
            except Exception:
                pass
            for combo, key in (
                    (self.cmb_currency_platinum, "currency_platinum_color"),
                    (self.cmb_currency_ducats, "currency_ducats_color")):
                value = str(values.get(key) or "")
                index = combo.findData(value)
                if index < 0 and value:
                    combo.addItem(f"{value} (текущий)", value)
                    index = combo.count() - 1
                if index >= 0:
                    combo.setCurrentIndex(index)
            self.chk_update_check.setChecked(
                bool(values.get("update_check", True)))
            self.chk_update_disabled.setChecked(
                bool(values.get("update_disabled", False)))
            self.edit_update_url.setText(str(
                values.get("update_url") or DEFAULT_UPDATE_URL))
            self.spin_update_interval.setValue(
                int(values.get("update_check_interval_min", 180) or 180))
            self._clear_errors()

        def hotkey_values(self):
            return {action: edit.value() for action, edit in self.edits.items()}

        def values(self):
            mapping = self.hotkey_values()
            config = _hotkey_config_values(mapping)
            config.update({
                "notify_enabled": self.chk_notify_enabled.isChecked(),
                "notify_pick_toast": self.chk_notify_pick_toast.isChecked(),
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
                "scan_preset": str(
                    self.cmb_scan_preset.currentData()
                    if getattr(self, "cmb_scan_preset", None) is not None
                    else "auto"),
                "notify_toast_opacity": float(
                    self.spin_toast_opacity.value()),
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
                "plates_click_through": bool(
                    self.chk_plates_click.isChecked()),
                "region_card_ttl": round(float(
                    self.spin_region_ttl.value()), 1),
                "overlay_ttl": round(float(
                    self.spin_overlay_ttl.value()), 1),
                "auto_detect": bool(self.chk_auto_detect.isChecked()),
                "font_scale": round(float(self.spin_font_scale.value()), 2),
                "overlay_opacity": round(float(self.spin_opacity.value()), 2),
                "currency_platinum_color": str(
                    self.cmb_currency_platinum.currentData() or "#E7F1FF"),
                "currency_ducats_color": str(
                    self.cmb_currency_ducats.currentData() or "#C9A66B"),
                "show_summary": self.chk_show_summary.isChecked(),
                "show_item_icons": self.chk_show_item_icons.isChecked(),
                "icons_update_days": int(self.spin_icons_update_days.value()),
                "game_theme": str(self.cmb_game_theme.currentData() or "auto"),
                "overlay_show_set_price": self.chk_show_set_price.isChecked(),
                "currency_icons": self.chk_currency_icons.isChecked(),
                "update_check": self.chk_update_check.isChecked(),
                "update_url": (self.edit_update_url.text().strip()
                               or DEFAULT_UPDATE_URL),
                "update_disabled": self.chk_update_disabled.isChecked(),
                "update_check_interval_min":
                    int(self.spin_update_interval.value()),
                "hotkeys": mapping,
            })
            if not self.edit_update_url.text().strip():
                self.edit_update_url.setText(DEFAULT_UPDATE_URL)
                self._set_status("URL восстановлен по умолчанию", warn=True)
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
            try:
                theme.validate_currency_colors(
                    values.get("currency_platinum_color"),
                    values.get("currency_ducats_color"))
            except Exception as exc:
                text = _human("настройки (цвета валют)", exc)
                self._set_status(f"Не сохранено: {text}", warn=True)
                return {"ok": False, "error": text}
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
                          "error": _human("настройки", exc)}
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

        @staticmethod
        def _ver_tuple(value):
            try:
                return tuple(int(part) for part in str(value).split(".")
                             if part.isdigit())
            except Exception:
                return ()

        def _update_versions(self, info):
            running = str((info or {}).get("running") or self.version or "")
            active = str((info or {}).get("active")
                         or (info or {}).get("current") or running)
            if active and running and active != running:
                self.lbl_update_active.setText(
                    f"Активная после перезапуска: {active}")
                self.lbl_update_active.setVisible(True)
            else:
                self.lbl_update_active.setVisible(False)
            return running, active

        def restart_now(self):
            if callable(self.restart_fn):
                try:
                    self.restart_fn()
                    return True
                except Exception as exc:
                    self._set_status(f"Перезапуск не удался: {exc}", warn=True)
            else:
                self._set_status("Перезапуск недоступен в этой сборке.",
                                 warn=True)
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
                              "error": _human("обновления (проверка)", exc)}
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
                self.btn_restart_now.setVisible(False)
                return
            if info.get("error"):
                self.lbl_update_status.setText(
                    f"Не удалось проверить обновления: {info['error']}.")
                self.lbl_update_status.setStyleSheet(
                    f"color:{color('warning')};")
                self.btn_update_apply.setVisible(False)
                return
            running, active = self._update_versions(info)
            needs_restart = bool(info.get("needs_restart")) or bool(
                active and running and self._ver_tuple(active)
                > self._ver_tuple(running))
            if needs_restart:
                self.lbl_update_status.setText(
                    f"Обновление {active} скачано — перезапустите приложение.")
                self.lbl_update_status.setStyleSheet(f"color:{color('text')};")
                self.btn_update_apply.setVisible(False)
                self.btn_restart_now.setVisible(callable(self.restart_fn))
                self.btn_restart_now.setEnabled(True)
                self._set_status(f"Обновление {active} скачано — перезапустите.")
                return
            self.btn_restart_now.setVisible(False)
            if info.get("available"):
                version = info.get("version") or info.get("server") or "?"
                self.lbl_update_status.setText(
                    f"Доступно обновление {version} — скачиваю…")
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
            self.btn_restart_now.setVisible(False)
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
                              "error": _human("обновления (установка)", exc)}
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
                        "Обновление установлено, но перезапуск не удался — "
                        "запустите приложение вручную "
                        "(подробности в app.log).")

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
                self._set_status(_human("уведомления (тест)", exc,
                                        "тест не отправлен — "
                                        "подробности в app.log"),
                                 warn=True)
                return False
            if isinstance(result, dict) and result.get("ok") is False:
                self._set_status("Тест не отправлен: "
                                 f"{result.get('error') or 'ошибка'}",
                                 warn=True)
                return False
            self._set_status("ТЕСТ отправлен: " + (
                "без звука" if not self.chk_notify_sound.isChecked()
                else "история/тост/звук."))
            return True

        def open_data(self):
            opener = self.on_open_data_dir
            if open_data_folder(self.data_dir, opener=opener):
                self._set_status("Папка данных открыта.")
                return True
            self._set_status("Не удалось открыть папку данных.", warn=True)
            return False

        def reset_plate_boxes(self):
            self.cfg["plate_boxes"] = {}
            try:
                import warframe
                warframe._write_config(self.cfg, warframe.CONFIG_PATH)
            except Exception:
                pass
            self._set_status("Расположение плашек сброшено.")
            return True

        def open_diagnostics(self):
            path = os.path.join(self.data_dir, "ocr_debug")
            if open_data_folder(path, opener=self.on_open_data_dir):
                self._set_status("Папка диагностики открыта.")
                return True
            self._set_status("Дампов пока нет.", warn=True)
            return False

        def open_log(self):
            path = os.path.join(self.data_dir, "app.log")
            if open_log_file(path, opener=self.on_open_log):
                self._set_status("app.log открыт.")
                return True
            self._set_status("app.log пока нет.", warn=True)
            return False

        # ── 1.0.0: калибровка скана ──────────────────────────────────
        def _scan_preset_changed(self, *_args):
            try:
                import scan_presets as _sp
                pid = (self.cmb_scan_preset.currentData()
                       if self.cmb_scan_preset is not None else "auto")
                self.cfg["scan_preset"] = str(pid or "auto")
                width = int((self.cfg or {}).get("base_width") or 1920)
                height = int((self.cfg or {}).get("base_height") or 1080)
                geometry = _sp.scan_geometry(self.cfg, width, height)
                _sp.apply_geometry(self.cfg, geometry)
                self._draw_scan_preset_preview(geometry)
                self.lbl_scan_calibration.setText(
                    _sp.preset_log_line(geometry))
            except Exception:
                pass

        def _draw_scan_preset_preview(self, geometry=None):
            try:
                import scan_presets as _sp
                from PyQt5.QtCore import QRect
                from PyQt5.QtGui import QColor, QPainter, QPixmap
                width = int((self.cfg or {}).get("base_width") or 1920)
                height = int((self.cfg or {}).get("base_height") or 1080)
                geometry = geometry or _sp.scan_geometry(
                    self.cfg, width, height)
                pix = QPixmap(192, 108)
                pix.fill(QColor("#0d131c"))
                painter = QPainter(pix)
                try:
                    painter.setPen(QColor("#4A7CA6"))
                    sx = 192.0 / max(1, width)
                    sy = 108.0 / max(1, height)
                    for left in geometry.get("lefts") or []:
                        painter.drawRect(QRect(
                            int(float(left) * sx),
                            int(float(geometry.get("card_top") or 0) * sy),
                            max(8, int(float(geometry.get("card_width")
                                           or 242) * sx)),
                            max(6, int(float(geometry.get("card_height")
                                           or 235) * sy))))
                finally:
                    painter.end()
                self.lbl_scan_preview.setPixmap(pix)
            except Exception:
                pass

        def scan_calibration_text(self):
            """Подпись состояния калибровки (сохранена/нет)."""
            raw = (self.cfg or {}).get("scan_calibration")
            if isinstance(raw, dict) and raw.get("lefts"):
                lefts = [int(x) for x in (raw.get("lefts") or [])]
                pitch = raw.get("card_pitch")
                scale = raw.get("ui_scale")
                text = f"калибровка: сохранена lefts={lefts}"
                if pitch:
                    text += f" pitch={pitch}"
                if scale:
                    text += f" scale={scale}"
                return text
            return "калибровка: нет (стандарт 1920×1080)"

        def refresh_scan_calibration(self):
            try:
                self.lbl_scan_calibration.setText(self.scan_calibration_text())
            except Exception:
                pass
            return self.scan_calibration_text()

        def calibrate_scan(self):
            """«Калибровка скана»: открыть мастер (снимок/авто/сохранить)."""
            if not callable(self.on_scan_calibrate):
                self._set_status("Калибровка недоступна в этой сборке.",
                                 warn=True)
                return {"ok": False, "error": "недоступно"}
            try:
                result = self.on_scan_calibrate(self) or {}
            except TypeError:
                try:
                    result = self.on_scan_calibrate() or {}
                except Exception as exc:
                    result = {"ok": False,
                              "error": _human("обновления (установка)", exc)}
            except Exception as exc:
                result = {"ok": False,
                          "error": _human("настройки", exc)}
            if not isinstance(result, dict):
                result = {"ok": True}
            self.last_scan_calibration = dict(result)
            self.refresh_scan_calibration()
            if result.get("ok") is False:
                self._set_status(
                    f"Калибровка: {result.get('error') or 'не сохранена'}.",
                    warn=True)
            elif result.get("saved") or result.get("scan_calibration"):
                payload = (result.get("scan_calibration")
                           or (self.cfg or {}).get("scan_calibration") or {})
                self.cfg["scan_calibration"] = dict(payload)
                self.refresh_scan_calibration()
                self._set_status("Калибровка сохранена и применена.")
            return result

        # ── 1.0.0: пакет иконок предметов ────────────────────────────
        def _sync_icons_checkboxes(self, checked=False):
            """«Показывать иконки» есть в двух вкладках — значения равны."""
            value = bool(checked)
            for box in (self.chk_show_item_icons, self.chk_icons_show):
                if box.isChecked() != value:
                    box.blockSignals(True)
                    box.setChecked(value)
                    box.blockSignals(False)
            return value

        def _icons_info(self):
            """Словарь состояния пака от приложения (или ``{}``)."""
            if not callable(self.icons_status_fn):
                return {}
            try:
                info = self.icons_status_fn()
                return dict(info or {}) if isinstance(info, dict) else {}
            except Exception:
                return {}

        def icons_status_text(self):
            info = self._icons_info()
            if not info and not callable(self.icons_status_fn):
                return "состояние: недоступно в этой сборке"
            files = int(info.get("files") or 0)
            size = int(info.get("bytes") or 0)
            pack = "есть" if info.get("have_pack") else "нет"
            text = (f"Библиотека: {files} файлов, комплект: {pack}, "
                    f"размер {_fmt_bytes(size)}")
            if info.get("updated_text"):
                text += f" · обновлён {info['updated_text']}"
            if info.get("running"):
                text += " · идёт загрузка"
            return text

        def refresh_icons_status(self):
            """Перечитать состояние пака и обновить подпись/кнопки."""
            text = self.icons_status_text()
            try:
                self.lbl_icons_state.setText(text)
            except Exception:
                pass
            info = self._icons_info()
            busy = bool(self._icons_busy) or bool(info.get("running"))
            for button in (getattr(self, "btn_icons_download", None),
                           getattr(self, "btn_icons_update", None),
                           getattr(self, "btn_icons_clear", None)):
                if button is not None:
                    button.setEnabled(not busy)
            cancel = getattr(self, "btn_icons_cancel", None)
            if cancel is not None:
                cancel.setEnabled(bool(busy))
            return text

        def set_icons_progress(self, payload):
            """Прогресс от загрузчика: «Иконки: 1240 / 2500 (49%)»."""
            payload = dict(payload or {})
            try:
                done = max(0, int(payload.get("done") or 0))
                total = max(0, int(payload.get("total") or 0))
            except Exception:
                done, total = 0, 0
            percent = int(done * 100.0 / total) if total else 0
            text = str(payload.get("text")
                       or (f"Иконки: {done} / {total} ({percent}%)"))
            running = bool(payload.get("running", True))
            try:
                self.lbl_icons_progress.setText(text)
                self.bar_icons.setVisible(bool(total) and running)
                if total:
                    self.bar_icons.setValue(max(0, min(100, percent)))
            except Exception:
                pass
            self._icons_busy = running
            if not running:
                self.refresh_icons_status()
                if payload.get("phase") == "done":
                    self._set_status(text)
            return text

        def _run_icons_action(self, fn, start_text, done_text):
            """Общий путь кнопок иконок: фон/синхронно + статус + прогресс."""
            if self._icons_busy:
                self._set_status("Иконки: дождитесь окончания операции.",
                                 warn=True)
                return {"ok": False, "error": "уже идёт"}
            if not callable(fn):
                self._set_status("Иконки: действие недоступно в этой сборке.",
                                 warn=True)
                return {"ok": False, "error": "недоступно"}
            self._icons_busy = True
            self.refresh_icons_status()
            try:
                self.lbl_icons_progress.setText(start_text)
                self.bar_icons.setVisible(True)
                self.bar_icons.setValue(0)
            except Exception:
                pass
            try:
                result = fn() or {}
            except Exception as exc:
                result = {"ok": False,
                          "error": _human("настройки", exc)}
            if not isinstance(result, dict):
                result = {"ok": True, "text": str(result)}
            self.last_icons_result = dict(result)
            failed = (result.get("ok") is False
                      and not result.get("started")
                      and not result.get("cancelled")
                      and not result.get("busy"))
            if failed:
                self._icons_busy = False
                try:
                    self.bar_icons.setVisible(False)
                except Exception:
                    pass
                self.refresh_icons_status()
                self._set_status(f"Иконки: {result.get('error') or 'ошибка'}.",
                                 warn=True)
                return result
            if not result.get("started"):
                # действие завершилось синхронно (тесты/офлайн)
                self._icons_busy = False
                try:
                    self.bar_icons.setVisible(False)
                except Exception:
                    pass
                self.refresh_icons_status()
                self._set_status(str(result.get("text") or done_text))
            else:
                self._set_status(start_text)
            return result

        def download_icons(self):
            """«Скачать все иконки»: фоновая загрузка пака."""
            return self._run_icons_action(
                self.icons_download_fn, "Иконки: начинаю скачивание…",
                "Иконки: скачивание запущено в фоне.")

        def update_icons(self):
            """«Обновить сейчас»: сверка каталога и докачка нового."""
            return self._run_icons_action(
                self.icons_update_fn, "Иконки: сверяю каталог…",
                "Иконки: обновление запущено в фоне.")

        def cancel_icons(self):
            """«Отмена»: прервать фоновую загрузку пака (скачанное остаётся)."""
            if not callable(self.icons_cancel_fn):
                self._set_status("Иконки: отмена недоступна в этой сборке.",
                                 warn=True)
                return {"ok": False, "error": "недоступно"}
            try:
                result = self.icons_cancel_fn()
            except Exception as exc:
                result = {"ok": False,
                          "error": _human("настройки", exc)}
            if result is None:
                result = {"ok": True}
            if not isinstance(result, dict):
                result = {"ok": bool(result)}
            self.last_icons_result = dict(result)
            if result.get("ok") is False:
                self._set_status("Иконки: останавливать нечего.", warn=True)
            else:
                self._set_status("Иконки: загрузка останавливается — "
                                 "скачанное сохранится.")
                try:
                    self.lbl_icons_progress.setText("Иконки: отмена…")
                except Exception:
                    pass
            return result

        def _confirm_icons(self, text):
            if callable(self.confirm_fn):
                try:
                    return bool(self.confirm_fn(text))
                except Exception:
                    return False
            try:
                import warframe as wf_module
                return bool(wf_module.themed_confirm(
                    self, text, title="Иконки предметов",
                    confirm_text="Удалить", danger=True))
            except Exception:
                return False

        def clear_icons(self):
            """«Очистить кэш иконок» — только после подтверждения."""
            if self._icons_busy:
                self._set_status("Иконки: дождитесь окончания загрузки.",
                                 warn=True)
                return {"ok": False, "error": "идёт загрузка"}
            if not callable(self.icons_clear_fn):
                self._set_status("Иконки: действие недоступно в этой сборке.",
                                 warn=True)
                return {"ok": False, "error": "недоступно"}
            if not self._confirm_icons(
                    "Удалить все скачанные иконки предметов?\n"
                    "Их придётся скачивать заново."):
                self._set_status("Очистка кэша иконок отменена.")
                return {"ok": False, "cancelled": True}
            try:
                result = self.icons_clear_fn() or {}
            except Exception as exc:
                result = {"ok": False,
                          "error": _human("настройки", exc)}
            if not isinstance(result, dict):
                result = {"ok": True, "removed": result}
            self.last_icons_result = dict(result)
            if result.get("ok") is False:
                self._set_status(
                    f"Кэш иконок не очищен: "
                    f"{result.get('error') or 'ошибка'}.", warn=True)
            else:
                removed = int(result.get("removed") or 0)
                self._set_status(f"Кэш иконок очищен "
                                 f"(удалено файлов: {removed}).")
            self.refresh_icons_status()
            return result

    return SettingsDialog


def wf_version_fallback():
    """Версия приложения без импорта warframe (на случай ранних вызовов)."""
    try:
        import warframe as wf
        return str(getattr(wf, "APP_VERSION", "—"))
    except Exception:
        return "—"
