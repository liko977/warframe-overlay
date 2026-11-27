# -*- coding: utf-8 -*-
"""Режим входа на warframe.market: окна приложения не перекрывают браузер.

Пока пользователь входит на сайт в настоящем браузере, **ни одно** окно
оверлея не должно быть видимым и не должно нести флаг
``Qt.WindowStaysOnTopHint`` — иначе браузер (обычное окно) окажется под ним
(см. ``LOGIN_WINDOW_SPEC.md``). Исключение ровно одно: компактная
topmost-пилюля «⏳ Вход… [Отменить]» в правом верхнем углу экрана — она
маленькая, не закрывает центр страницы логина и нужна, чтобы прервать вход
даже в полноэкранном браузере (общая инфраструктура окон).

Здесь же лежит общий флаг ``in_progress``: его читают guard-ы горячих клавиш
(F8/F9/Alt+M/Alt+E), авто-скана и плашек наград (``warframe.py``), чтобы окна
не всплывали повторно во время входа.

Почему именно «спрятать», а не «снять topmost»: у приложения несколько
независимых top-level окон (главное окно, диалог входа, плашки наград,
«Итого», старые окна редактора/событий/маркета), и снятие флага у одного из
них ничего не меняет для остальных. Спрятанное окно не перекрывает браузер
гарантированно.

Qt импортируется лениво — модуль безопасно импортировать без PyQt5.
"""

import os
import time

#: Общий флаг «идёт вход в браузере» + время начала (для логов/диагностики).
IN_PROGRESS = {"active": False, "since": 0.0}


def in_progress():
    """Идёт ли сейчас вход на warframe.market (в браузере)?"""
    return bool(IN_PROGRESS["active"])


def set_in_progress(value):
    """Запомнить, что вход начался/закончился. Возвращает новое значение."""
    value = bool(value)
    IN_PROGRESS["active"] = value
    IN_PROGRESS["since"] = time.monotonic() if value else 0.0
    return value


def reset():
    """Сбросить флаг (страховка при закрытии приложения/в тестах)."""
    set_in_progress(False)


# ─────────────────────────────────────────────────────────────────────────
#  Окна приложения: скрыть на время входа и вернуть после
# ─────────────────────────────────────────────────────────────────────────
def _qt():
    """Ленивый импорт Qt: ``(Qt, QApplication)`` или ``(None, None)``."""
    try:
        from PyQt5.QtCore import Qt
        from PyQt5.QtWidgets import QApplication
    except Exception:              # pragma: no cover - Qt есть в продукте
        return None, None
    return Qt, QApplication


def window_flags(widget):
    """Флаги окна или ``None``, если виджет уже удалён."""
    try:
        return widget.windowFlags()
    except Exception:
        return None


def has_topmost(widget):
    """Есть ли у окна флаг «всегда сверху» (``WindowStaysOnTopHint``)."""
    Qt, _app = _qt()
    if Qt is None:
        return False
    flags = window_flags(widget)
    return bool(flags is not None and flags & Qt.WindowStaysOnTopHint)


def is_window(widget):
    """Является ли виджет окном (в т.ч. дочерним окном вроде QDialog)."""
    try:
        return bool(widget.isWindow())
    except Exception:
        return False


def app_windows(exclude=()):
    """Все окна приложения, включая дочерние окна (диалог с родителем).

    Почему ``QApplication.allWidgets()``, а не ``topLevelWidgets()``:
    ``topLevelWidgets()`` — не «список всех окон на экране», а список виджетов
    без родителя. ``QDialog(parent=OverlayWindow)`` в Qt — всё равно отдельное
    окно верхнего уровня (``isWindow()``), при этом оно является дочерним
    окном и может не попасть в ``topLevelWidgets()`` (зависит от состояния
    иерархии на момент вызова). ``allWidgets()`` — надмножество и стабильно
    возвращает и такие окна, поэтому обходим его и берём всё, что
    ``isWindow()``.
    """
    _Qt, QApplication = _qt()
    if QApplication is None:
        return []
    app = QApplication.instance()
    if app is None:
        return []
    excluded = [w for w in (exclude or ()) if w is not None]
    windows = []
    for widget in app.allWidgets():
        if widget is None:
            continue
        if any(widget is item for item in excluded):
            continue
        if is_window(widget):
            windows.append(widget)
    return windows


def visible_topmost_windows(exclude=()):
    """Окна, которые сейчас видны и «всегда сверху» (диагностика/тесты)."""
    return [widget for widget in app_windows(exclude=exclude)
            if _is_visible(widget) and has_topmost(widget)]


def _is_visible(widget):
    try:
        return bool(widget.isVisible())
    except Exception:
        return False


def hide_app_windows(keep=()):
    """Спрятать все окна приложения и снять с них «всегда сверху».

    Для каждого окна порядок строгий: сначала ``hide()``, потом флаг
    ``WindowStaysOnTopHint=False``. Спрятанное окно не перекрывает браузер в
    любом случае, а снятие флага у скрытого окна не зависит от того, отпустит
    ли Windows topmost-слой. Прежняя правка снимала флаг только у видимого
    главного окна и не трогала остальные topmost-окна (диалог входа, плашки,
    «Итого») — поэтому и не помогла.

    :param keep: окна, которые трогать нельзя (диалог входа и пилюля).
    :return: снимок для :func:`restore_app_windows`.
    """
    Qt, _app = _qt()
    if Qt is None:
        return []
    snapshot = []
    for widget in app_windows(exclude=keep):
        try:
            flags = widget.windowFlags()
        except Exception:
            continue
        topmost = bool(flags & Qt.WindowStaysOnTopHint)
        visible = _is_visible(widget)
        if not topmost and not visible:
            continue
        snapshot.append({"widget": widget, "visible": visible,
                         "topmost": topmost})
        try:
            if visible:
                widget.hide()
            if topmost:
                widget.setWindowFlag(Qt.WindowStaysOnTopHint, False)
        except Exception:
            continue
    return snapshot


def restore_app_windows(snapshot):
    """Вернуть окнам topmost и видимость (в обратном порядке снимка)."""
    Qt, _app = _qt()
    if Qt is None:
        return 0
    restored = 0
    for item in reversed(list(snapshot or [])):
        widget = item.get("widget")
        if widget is None:
            continue
        try:
            if item.get("topmost"):
                widget.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            if item.get("visible"):
                widget.show()
            restored += 1
        except Exception:
            # окно уже удалено (deleteLater) — пропускаем, приложение живо
            continue
    return restored


def windows_report(exclude=(), keep=None):
    """Короткая диагностика: что за окна есть у приложения и какие они.

    Используется в логах при входе (``[topmost]`` — «всегда сверху»), чтобы
    при следующей жалобе «окно перекрывает браузер» было видно все окна.

    ``keep`` — синоним ``exclude`` (совместимость с вызовом из
    ``warframe._on_login_state``: там передаётся ``keep=`` — диалог и пилюля,
    которые нельзя трогать).
    """
    excluded = list(exclude or ())
    if keep is not None:
        excluded.extend(list(keep or []))
    report = []
    for widget in app_windows(exclude=excluded):
        # только настоящие окна: дочерние QLabel/QPushButton/QFrame внутри
        # комбобоксов и шапок в отчёте — лишний шум
        try:
            if not widget.isWindow():
                continue
        except Exception:
            continue
        report.append({
            "class": type(widget).__name__,
            "title": _title(widget),
            "visible": _is_visible(widget),
            "topmost": has_topmost(widget),
        })
    return report


def _title(widget):
    try:
        return str(widget.windowTitle() or "")
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────
#  Открытые окна-диалоги: закрыть при любом скрытии приложения (B2)
# ─────────────────────────────────────────────────────────────────────────
#: Зарегистрированные диалоги: подтверждение, форма ордера, токен, вход.
#: Регистрация централизованная, чтобы новые окна не забывались.
DIALOGS = []


def register_dialog(dialog):
    """Учесть открытое окно-диалог, чтобы скрытие приложения его закрыло.

    Вызывается из конструкторов диалогов (форма ордера, окно токена, окно
    входа) и из мест создания подтверждений. Повторная регистрация одного и
    того же окна безопасна; при уничтожении окно само убирается из списка.
    Когда диалог закрывается, главному окну возвращается кликабельность
    (BUG-1: модальность/остаточный disabled не должны «залипать»).
    """
    if dialog is None:
        return None
    try:
        if any(existing is dialog for existing in DIALOGS):
            return dialog
        DIALOGS.append(dialog)
    except Exception:
        return dialog
    try:
        dialog.destroyed.connect(lambda *_args: unregister_dialog(dialog))
    except Exception:
        pass
    try:                           # QDialog: после закрытия окно снова активно
        dialog.finished.connect(
            lambda *_args: ensure_interactive_windows())
    except Exception:
        pass
    return dialog


def unregister_dialog(dialog):
    """Убрать окно из списка открытых (закрыли/удалили)."""
    try:
        DIALOGS[:] = [item for item in DIALOGS if item is not dialog]
    except Exception:
        pass


def reset_dialogs():
    """Очистить список (закрытие приложения/тесты); сами окна не трогает."""
    try:
        DIALOGS[:] = []
    except Exception:
        pass


def _alive(widget):
    """Не удалён ли C++-объект виджета (иначе обращение бросит RuntimeError)."""
    try:
        widget.isVisible()
        return True
    except Exception:
        return False


def active_dialogs():
    """Список открытых (видимых) зарегистрированных окон-диалогов."""
    return [dialog for dialog in list(DIALOGS)
            if _alive(dialog) and _is_visible(dialog)]


def _dialog_windows():
    """Все видимые окна-диалоги приложения, включая QMessageBox и QMenu.

    ``QMessageBox`` из статических методов (``QMessageBox.question`` и т.п.)
    нигде не регистрируется, поэтому дополнительно обходим
    ``QApplication.allWidgets()``: иначе скрытие оверлея оставило бы такое
    окно висеть поверх игры.
    """
    QApplication, QDialog, QMenu = _qt_dialog_classes()
    if QApplication is None:
        return []
    app = QApplication.instance()
    if app is None:
        return []
    windows = []
    for widget in app.allWidgets():
        if widget is None:
            continue
        try:
            if not widget.isWindow() or not widget.isVisible():
                continue
        except Exception:
            continue
        if isinstance(widget, (QDialog, QMenu)):
            windows.append(widget)
    return windows


def _qt_dialog_classes():
    """Ленивый импорт Qt: ``(QApplication, QDialog, QMenu)``."""
    try:
        from PyQt5.QtWidgets import QApplication, QDialog, QMenu
    except Exception:              # pragma: no cover - Qt есть в продукте
        return None, None, None
    return QApplication, QDialog, QMenu


def _close_window(widget):
    """Закрыть окно так, чтобы завершился и его modal-цикл (``exec_``)."""
    if not _alive(widget):
        return False
    handled = False
    try:
        reject = getattr(widget, "reject", None)
        if callable(reject):
            reject()               # QDialog/QMessageBox: exec_ вернёт Rejected
            handled = True
    except Exception:
        handled = False
    if not handled:
        try:
            widget.close()
            handled = True
        except Exception:
            handled = False
    return handled


def close_active_dialogs(keep=()):
    """Закрыть ВСЕ открытые окна-диалоги приложения.

    Вызывается из всех путей скрытия оверлея: F9 (``hide_all``), Esc и кнопки
    «—»/«×» (``hide_window``), скрытие по таймеру (``Manager.hide_all``),
    скрытие окна маркета (``MarketBrowser.hide``). ``keep`` — окна, которые
    трогать нельзя (диалог входа и пилюля во время режима входа).

    Возвращает число закрытых окон (для тестов и логов).
    """
    kept = [widget for widget in (keep or ()) if widget is not None]
    closed = 0
    seen = set()
    for dialog in list(DIALOGS):
        if not _alive(dialog) or any(dialog is item for item in kept):
            continue
        seen.add(id(dialog))
        if _close_window(dialog):
            closed += 1
    for widget in _dialog_windows():
        if any(widget is item for item in kept) or id(widget) in seen:
            continue
        if _close_window(widget):
            closed += 1
    DIALOGS[:] = [dialog for dialog in DIALOGS
                  if any(dialog is item for item in kept)]
    # BUG-1: после закрытия диалогов главное окно должно снова принимать клики
    ensure_interactive_windows()
    return closed


# ─────────────────────────────────────────────────────────────────────────
#  Кликабельность окон после логина/диалогов (BUG-1)
# ─────────────────────────────────────────────────────────────────────────
#: Окна приложения, которым возвращаем интерактивность (главное окно).
INTERACTIVE_WINDOWS = []

#: Стили расширенного окна Windows: «клик сквозь» и «не активировать».
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000


def register_interactive_window(window):
    """Запомнить окно, которому нужен :func:`ensure_interactive_windows`."""
    if window is None:
        return window
    try:
        if not any(existing is window for existing in INTERACTIVE_WINDOWS):
            INTERACTIVE_WINDOWS.append(window)
    except Exception:
        pass
    return window


def ensure_interactive_windows(activate=False):
    """Вернуть кликабельность зарегистрированным окнам приложения (BUG-1).

    Вызывается после завершения входа (успех/ошибка/отмена/таймаут) и после
    закрытия любого диалога: снимает disabled/прозрачность/остаточные grab'ы и
    нативные WS_EX_TRANSPARENT/WS_EX_NOACTIVATE. Возвращает число окон.
    """
    restored = 0
    alive = []
    for window in list(INTERACTIVE_WINDOWS):
        if not _alive(window):
            continue
        alive.append(window)
        helper = getattr(window, "ensure_interactive", None)
        if not callable(helper):
            continue
        try:
            helper(activate=bool(activate))
            restored += 1
        except Exception:
            continue
    INTERACTIVE_WINDOWS[:] = alive
    return restored


def _native_style(widget):
    """``GWL_EXSTYLE`` окна (только Windows) или ``None``."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = int(widget.winId())
        if not hwnd:
            return None
        return int(user32.GetWindowLongW(hwnd, -20))   # GWL_EXSTYLE
    except Exception:
        return None


def clickthrough_style(widget):
    """Есть ли у окна WS_EX_TRANSPARENT/WS_EX_NOACTIVATE (диагностика)."""
    style = _native_style(widget)
    if style is None:
        return 0
    return style & (WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)


def clear_clickthrough_style(widget):
    """Снять «клик сквозь»/«не активировать» с нативного окна (Windows).

    После ``hide → setWindowFlag → show`` (режим входа) окно могло остаться с
    ``WS_EX_TRANSPARENT``/``WS_EX_NOACTIVATE`` и не принимать клики — это одна
    из причин BUG-1. Возвращает ``True``, если стиль действительно менялся.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = int(widget.winId())
        if not hwnd:
            return False
        style = int(user32.GetWindowLongW(hwnd, -20))  # GWL_EXSTYLE
        cleaned = style & ~(WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
        if cleaned == style:
            return False
        user32.SetWindowLongW(hwnd, -20, cleaned)
        return True
    except Exception:
        return False
