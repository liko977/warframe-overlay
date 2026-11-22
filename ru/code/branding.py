# -*- coding: utf-8 -*-
"""Брендинг VoidFrameRU: единые имя/версия и поиск ассетов (1.0.0).

Модуль намеренно без зависимостей (кроме stdlib): его импортируют warframe,
overlay_window, settings_ui, notifications, launcher и тесты, поэтому он не
должен тянуть PyQt5/torch.

Пути к ассетам (см. ``assets/brand/BRAND.md`` §7):

* из исходников — ``wf_ru/assets/brand/<name>``;
* из one-folder сборки — ``<exe_dir>/_internal/assets/brand/<name>``;
* из one-file — ``sys._MEIPASS/assets/brand/<name>``.
"""

import os
import sys

#: Отображаемое имя приложения (заголовок окна, трей, «О программе», логи).
APP_NAME = "VoidFrameRU"

#: Версия релиза. Должна совпадать с ``warframe.APP_VERSION``.
APP_VERSION = "1.0.3"

#: Ассеты, которые обязаны попасть в сборку (prepare_build_ru/spec).
BRAND_ASSETS = ("icon.ico", "tray.png", "tray@2x.png", "logo_256.png",
                "splash.png")

#: Старые каталоги данных (для миграции; не использовать для новых данных).
OLD_APP_DIR_NAMES = ("WarframePriceOverlayRU", "WarframePriceOverlay")


def app_title(version=None):
    """Заголовок окна/трея: «VoidFrameRU 1.0.0»."""
    version = str(version or APP_VERSION).strip()
    return f"{APP_NAME} {version}" if version else APP_NAME


def brand_roots():
    """Корни поиска каталога ``assets/brand`` (сначала сборка, потом исходники)."""
    roots = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            roots.append(meipass)
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        roots.append(os.path.join(exe_dir, "_internal"))
    here = os.path.dirname(os.path.abspath(__file__))
    app_dir = os.environ.get("WF_APP_DIR") or ""
    if app_dir:
        roots.append(app_dir)
    roots += [here, os.path.dirname(here)]
    return [root for root in roots if root]


def brand_path(name):
    """Путь к ассету брендинга или ``""`` (ассет не найден)."""
    name = str(name or "").strip()
    if not name:
        return ""
    for root in brand_roots():
        path = os.path.join(root, "assets", "brand", name)
        if os.path.isfile(path):
            return path
    return ""


def data_dir_name():
    """Имя каталога данных в %LOCALAPPDATA% (VoidFrameRU)."""
    return APP_NAME


__all__ = ["APP_NAME", "APP_VERSION", "BRAND_ASSETS", "OLD_APP_DIR_NAMES",
           "app_title", "brand_roots", "brand_path", "data_dir_name"]
