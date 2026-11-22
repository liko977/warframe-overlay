# -*- coding: utf-8 -*-
"""
Warframe Reward Price Overlay — аналог wfinfo
=============================================

Что делает
----------
Сканирует экран наград Warframe (F8), распознаёт предметы (EasyOCR),
берёт цены с Warframe Market (API v2) и показывает поверх игры:

    • по каждой карточке — цену в платине, лучшую продажу/покупку,
      число активных продавцов и дюкаты;
    • общий итог («Итого: Np») и самый дорогой предмет;
    • значок OWNED / CRAFTED, если предмет у вас уже есть.

Дополнительно (см. EVENTS_MARKET_SPEC.md)
-----------------------------------------
• Alt+E — панель игровых событий: разломы, тревоги, вторжения, сортис,
  Баро Ки'Тиир, стальной путь, ночная волна и циклы открытых миров
  (модуль events_api.py, только чтение, кэш в папке данных).
• Alt+M — маркет-браузер warframe.market: поиск, детали, график цен и свои
  ордера (модули market_browser.py/market_client.py/ui_theme.py).
• В редакторе F8 у каждой строки — кнопка «₽»: продажа предмета на
  warframe.market через диалог с подтверждением (токен из config.json или
  WF_MARKET_TOKEN; без токена — инструкция).
• Alt+Q / F8 / F9, OCR-плашки, журнал наград и EE.log работают как раньше.

Исправлено по сравнению с исходным warframe.py
----------------------------------------------
1. Поддержка 1–4 карточек. Раньше жёстко читались 4 области: при 1–3
   наградах «пустые» карточки давали мусор и ломали весь вывод.
   Теперь количество карточек определяется автоматически по геометрии
   текста, а сами карточки позиционируются по центру экрана (так их
   рисует игра). Есть принудительное значение `force_cards` в config.json.
2. Новый API. api.warframe.market/v1 отключён (403 Deprecated) —
   используется v2: /v2/items, /v2/items/{slug}, /v2/orders/item/{slug}.
   Дюкаты, торгуемость и цены берутся оттуда.
3. Любое разрешение экрана и DPI. Координаты заданы в базовых 1920x1080
   и масштабируются под ваш монитор; включён per-monitor DPI awareness.
4. Кэширование и параллельные запросы: 0.3–1.5 с на карточку вместо
   нескольких секунд, повторный скан мгновенный.
5. Скан больше не «залипает» (флаг is_scanning снимается в finally),
   ошибки не роняют приложение, последний скан пишется в last_rewards.txt.

Запуск
------
    python warframe.py               # обычный режим (Alt+Q — плашки, F8 — редактор,
                                     #  Alt+E — события, F9 — скрыть всё)
    python warframe.py --calibrate   # сохранить скриншот с разметкой карточек
    python warframe.py --test-api "Dual Zoren Prime Handle"
    python warframe.py --selftest shot.png   # прогнать OCR по картинке

Настройки — config.json рядом со скриптом (создаётся автоматически).
"""

import argparse
import ctypes
import datetime
import difflib
import json
import math
import os
import platform
import re
import sys
import threading
import time
_MODULE_TS = time.time()
import traceback
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─────────────────────────────────────────────────────────────────────────
#  Базовые константы
# ─────────────────────────────────────────────────────────────────────────
BASE_W, BASE_H = 1920.0, 1080.0
APP_NAME = "VoidFrameRU"
APP_VERSION = "1.0.0"
API_ROOT = "https://api.warframe.market/v2"
ITEMS_ENDPOINT = API_ROOT + "/items"


# ─────────────────────────────────────────────────────────────────────────
#  Пути (работают и из исходника, и из собранного .exe)
# ─────────────────────────────────────────────────────────────────────────
def _app_dir():
    # Код может быть запущен лаунчером из кэша обновлений: папку приложения
    # (модели/шаблоны/config.json) лаунчер передаёт через WF_APP_DIR.
    env = os.environ.get("WF_APP_DIR")
    if env:
        try:
            if os.path.isdir(env):
                return os.path.abspath(env)
        except Exception:
            pass
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _pick_data_dir(app_dir):
    """Папка данных: ``%LOCALAPPDATA%\\VoidFrameRU`` (1.0.0), затем запасные.

    Старые каталоги (``WarframePriceOverlayRU``) больше не используются для
    записи — оттуда данные копирует :mod:`data_migration` при первом запуске.
    """
    env = os.environ.get("WF_DATA_DIR")
    if env:
        try:
            os.makedirs(env, exist_ok=True)
            return env
        except Exception:
            pass
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    candidates = [os.path.join(local, APP_NAME),
                  app_dir,
                  os.path.join(os.environ.get("TEMP") or os.path.expanduser("~"),
                               APP_NAME)]
    for d in candidates:
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".wf_write_test")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(probe)
            return d
        except Exception:
            continue
    return app_dir


APP_DIR = _app_dir()
DATA_DIR = _pick_data_dir(APP_DIR)
MODELS_DIR = os.path.join(APP_DIR, "models")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
ITEMS_CACHE = os.path.join(DATA_DIR, "items_cache_ru.json")


def models_dir():
    """Папка с распознавалками EasyOCR, если она лежит рядом с программой."""
    cands = [MODELS_DIR, os.path.join(DATA_DIR, "models")]
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        cands.insert(0, os.path.join(meipass, "models"))
    for d in cands:
        try:
            if os.path.isdir(d) and any(f.endswith(".pth") for f in os.listdir(d)):
                return d
        except Exception:
            pass
    return None

DEFAULT_CONFIG = {
    # ── управление ──────────────────────────────────────────────────────
    "hotkey_scan": "alt+q",          # скан экрана наград (плашки)
    "hotkey_editor": "f8",           # список выбранного (редактор)
    "hotkey_events": "alt+e",        # панель игровых событий (сортис, разломы…)
    "hotkey_market": "alt+m",        # окно «Маркет» (поиск, график, ордера)
    "hotkey_window": "f8",           # единое окно с вкладками
    "hotkey_aliases": True,          # Alt+E/Alt+M — быстрые алиасы вкладок
    "last_tab": 0,                   # последняя активная вкладка окна
    "window_geometry": None,         # [x, y] единого окна (None — центр)
    "window_size": [1180, 780],      # [w, h] единого окна
    "hotkey_hide": "f9",             # скрыть всё
    # ── взятая награда: Alt+1…Alt+4 (по последнему скану Alt+Q) ─────────
    "pick_enabled": True,            # включить горячие клавиши «взято»
    "pick_hotkeys": ["alt+1", "alt+2", "alt+3", "alt+4"],
    "overlay_duration_ms": 12000,
    "alert_threshold": 100,          # платина: выше — звуковой сигнал
    "play_sound": True,
    "show_debug": True,              # сохранять картинки в ocr_debug/
    "debug_dir": "ocr_debug",

    # ── сеть ────────────────────────────────────────────────────────────
    "request_timeout": 8,
    "workers": 6,
    "price_ttl": 180,                # сек, кэш цен
    "scan_price_ttl": 600,           # 0.6.4: TTL кэша цен сканера, сек
    "items_cache_ttl": 86400,        # сек, кэш списка предметов
    "ocr_languages": ["ru", "en"],   # русская + английская игра

    # ── обновления ──────────────────────────────────────────────────────
    "update_url": "",                # загрузки обновлений нет
    "update_check": False,           # не обращаемся к GitHub
    "update_check_interval_min": 180,  # автопроверка раз в 3 часа
    "last_update_notified": "",      # одна нотификация на версию

    # ── экран / геометрия (координаты для 1920x1080) ────────────────────
    # 1.0.0: цифры экрана наград перенесены из WFInfo (v9.8.2) — см.
    # reward_geometry.py (единая точка правды) и WFINFO_RESEARCH.md §1.
    "monitor": 1,                    # 1 = основной монитор (mss)
    "base_width": 1920,
    "base_height": 1080,
    "card_width": 242,               # ширина карточки (968/4, вплотную)
    "card_pitch": 242,               # шаг между карточками (= ширине)
    "cards_center_x": 960,           # центр ряда карточек = W/2
    "card_top": 224,                 # верх карточки = H/2 - 316
    "card_height": 235,              # высота карточки
    "name_offset_y": 187,            # полоса названия = card_top + это (411)
    "name_height": 48,               # высота полосы названия (до 2 строк)
    "owned_strip_height": 30,        # шапка карточки: Crafted / N Owned
    # OCR-полоса выведена ИЗ геометрии: полоса имён 411..459 + запас 15 px
    "scan_band": {"left": 461, "top": 396, "width": 998, "height": 78},
    # 1.0.0: UI scale игры (Flash.FlashDrawScale из EE.cfg); 0 = читать из игры
    "ui_scaling": 0.0,
    # 1.0.0: тема интерфейса для диагностики: "auto" или имя темы (19 тем)
    "game_theme": "auto",
    "overlay_offset_y": 14,          # отступ плашки от низа карточки
    "overlay_layout": "bottom",      # "under_cards" | "bottom"
    "overlay_top": 830,              # для layout=bottom: верх плашек (y в базовых 1080)
    "summary_top": 84,               # верх блока «Итого»
    "selected_top": 700,             # панель «Выбрано из реликвий»: верх
    "selected_left": 20,             # ... и левый край
    "selected_duration_ms": 30000,   # сколько держать панель после выбора
    "force_cards": 0,                # 0 = авто, 1..4 = принудительно
    "scan_retries": 2,               # сколько раз переснять, если ничего не нашли
    "scan_retry_delay_ms": 350,

    # ── авто-режим: сам открывает и закрывает плашки ────────────────────
    "auto_detect": False,            # по умолчанию только ручной скан (Alt+Q)
    "auto_poll_ms": 400,             # как часто проверять экран
    "auto_confirm": 2,               # сколько проверок подряд для смены состояния
    "auto_scan_delay_ms": 450,       # пауза перед сканом (карточки доезжают)
    "auto_hide_on_close": True,      # скрывать плашки, когда экран закрылся
    "detect_threshold": 0.85,        # порог совпадения шаблона заголовка
    "template_dir": "templates",     # шаблоны экрана наград (учится сам)

    # ── журнал реликвий ─────────────────────────────────────────────────
    "relics_log": "relics_log.txt",  # история дропа с ценами
    "relics_log_reset": True,        # пересоздавать файл при каждом запуске
    "selected_log": "selected.txt",  # редактируемый список выбранного
    "focus_editor_on_f8": True,      # F8: забрать мышь/фокус у игры
    "restore_game_focus": True,      # при закрытии вернуть фокус в игру
    "font_scale": 1.0,
    "overlay_opacity": 1.0,          # прозрачность окна/фона (0.4..1.0)
    "currency_platinum_color": "#E7F1FF",  # цвет платины в UI
    "currency_ducats_color": "#C9A66B",    # цвет дукатов в UI
    "show_summary": True,            # показывать блок «Итого» над плашками
    "show_item_icons": True,         # иконки предметов WFCD в UI
    "overlay_show_set_price": True,   # «компл. N p» на карточке части
    "currency_icons": True,           # 1.0.0: иконки платины/дукатов вместо «p»/«дк»
    # ── пакет иконок предметов (1.0.0, см. icon_pack.py) ───────────────
    "icons_pack_enabled": True,      # пакет иконок включён (кнопки в настройках)
    "icons_allow_manual_download": True,  # 1.0.0: сеть только по кнопке
    "icons_update_days": 7,          # автообновление пака раз в N дней (0 = выкл.)
    "icons_rate_limit": 3.0,         # запросов/с к маркету (ToS: не более 3)

    # ── игровой лог EE.log (только чтение, см. LOG_SPEC.md) ─────────────
    "log_enabled": True,             # читать EE.log «хвостом» как доп. источник
    "log_path": "",                  # путь к EE.log; пусто → автопоиск папки игры
    "log_auto_detect": True,         # искать %LOCALAPPDATA%\Warframe\EE.log
    "log_poll_ms": 500,              # как часто проверять новые строки, мс
    "log_drop_to_relics_log": True,  # собственный дроп из лога — в relics_log.txt
    "log_auto_select_own": False,    # и в список «Выбрано» (по умолчанию только журнал)

    # ── статистика фарма и пулы реликвий (см. FARM_STATS_SPEC.md) ──────
    "farm_stats_enabled": True,      # собирать историю фарма из EE.log
    "farm_stats_file": "farm_stats.json",  # файл истории (в DATA_DIR)
    "farm_stats_max_sessions": 200,  # максимум хранимых сессий (ротация)
    "relic_pool_url": "https://raw.githubusercontent.com/WFCD/warframe-drop-data/main/data/relics.json",
    "relic_pool_ttl_hours": 24,      # TTL кэша пулов, часов
    "relic_pool_state": "Intact",    # состояние по умолчанию (Intact/Exceptional/Flawless/Radiant)
    "relic_pool_min_ev": 10,         # порог «выгодно» (платина)
    "relic_pool_price_ttl": 3600,    # TTL кэша цен пулов, сек

    # ── игровые события (только чтение, см. EVENTS_MARKET_SPEC.md) ──────
    "events_enabled": True,          # опрос официального worldState.php
    "events_poll_ms": 60000,         # период опроса; источник свеж раз в 5 мин
    "events_timeout": 6,             # t11: быстрый таймаут одного запроса
    "events_attempts": 2,            # t11: попыток с паузой (не 3×20 с)
    "events_language": "ru",         # ?language= (влияет на кэш и переводы)
    "events_translation": "auto",    # словарь узлов: "auto" | путь к json | ""

    # ── уведомления Windows: трей и нативные тосты ─────────────────────
    "notify_enabled": True,          # показывать уведомления вообще
    "notify_sound": True,            # звук Windows вместе с тостом
    "notify_only_when_game_focused": False,   # True — только когда игра в фокусе
    "notify_skip_when_game_focused": False,   # True — только когда игра НЕ в фокусе
    "notify_events": ["order_bought", "price_drop", "baro_arrival",
                      "baro_leaving", "fissure_ending", "chat"],
    "notify_cooldown_min": 10,       # анти-спам: не чаще раза в N минут
    "notify_price_drop_pct": 10,     # «цена ниже моей»: на сколько % ниже
    "notify_fissure_tiers": ["Axi", "Neo"],   # эры фиссур для уведомлений
    "notify_poll_sec": 60,           # период фоновой проверки ордеров/событий
    "notify_baro_leaving_min": 60,   # «Баро улетает» за N минут до конца
    "notify_fissure_ending_min": 5,  # «фиссура кончается» за N минут
    # ── центр уведомлений в оверлее (t15) ──────────────────────────────
    "notify_mode": "overlay",        # overlay (свои тосты) | system (трей)
    "notify_toast_sec": 6,           # сколько висит свой тост
    "notify_toast_opacity": 0.95,     # непрозрачность подложки тоста
    "notify_chat_color": "#4aa8ff",  # акцент чата
    "notify_market_color": "#43d17c",  # акцент «куплен ордер»
    "notify_price_color": "#f5c451",   # акцент «цена ниже моей»
    "notify_sound_choice": "auto",   # auto | market | chat | system
    "notify_sound_file": "",         # свой .wav (файл не копируем)
    "chat_url": "https://warframe.market/",  # пока чата в приложении нет
    "chat_enabled": True,             # вкладка «Чат» warframe.market
    "status_auto": False,             # авто ingame/online по фокусу игры
    "status_mode": "online",          # 1.0.0: явный статус (авто-режим убран)
    "status_restore_online": True,    # при выходе вернуть «Онлайн»
    "status_poll_ms": 5000,           # период проверки фокуса в авто-режиме

    # ── продажа на warframe.market (только по кнопке, см. спеку) ────────
    "market_token": "",              # JWT из браузера; пусто — продажа выключена
    "market_token_visible": False,   # показывать токен целиком (по умолч. маскируем)
    "market_store_token": True,      # сохранять JWT в config.json (false — только в памяти)
    "market_login_session_only": False,  # вход живёт до закрытия (cookie не на диск)
    # Вход через браузер пользователя (CDP) — основной способ: встроенный
    # Chromium 87 не проходит Cloudflare. market_browser_path — свой путь к
    # chrome.exe/msedge.exe, если автопоиск не нашёл.
    "market_browser_path": "",
    "market_login_timeout_sec": 600,   # сколько ждать входа в браузере, сек
    # Встроенный QtWebEngine — только запасной вариант (Cloudflare его блокирует),
    # по умолчанию выключен; требует установленного PyQtWebEngine.
    "market_web_login": False,
    "market_default_quantity": 1,    # количество по умолчанию в форме «продать»
    "market_dry_run": False,         # True — не отправлять ордер (только показать)
}


# ─────────────────────────────────────────────────────────────────────────
#  Мелкие утилиты
# ─────────────────────────────────────────────────────────────────────────
def app_log_path():
    return os.path.join(DATA_DIR, "app.log")


_LOG_LOCK = threading.Lock()
LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUPS = 3


def _rotate_log(path):
    try:
        if os.path.getsize(path) < LOG_MAX_BYTES:
            return
        for index in range(LOG_BACKUPS - 1, 0, -1):
            src = f"{path}.{index}"
            dst = f"{path}.{index + 1}"
            if os.path.exists(src):
                try:
                    os.replace(src, dst)
                except Exception:
                    pass
        try:
            os.replace(path, path + ".1")
        except Exception:
            try:
                os.remove(path)
            except Exception:
                pass
    except Exception:
        pass


def log(msg=""):
    """Безопасный лог: app.log (ротация) + stdout, если он есть."""
    text = str(msg)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        path = app_log_path()
        with _LOG_LOCK:
            _rotate_log(path)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(text + "\n")
    except Exception:
        pass
    try:
        stream = getattr(sys, "stdout", None)
        if stream is not None:
            print(text, flush=True)
    except Exception:
        pass


def log_exception(context=""):
    """Записать исключение с traceback в app.log (без падения)."""
    try:
        text = traceback.format_exc()
    except Exception:
        text = ""
    log((context + "\n" if context else "") + text.rstrip())


def startup_line():
    try:
        frozen = bool(getattr(sys, "frozen", False))
        lefts = [int(round(x)) for x in card_lefts(4, DEFAULT_CONFIG)]
        return (f"=== VoidFrameRU {APP_VERSION} | "
                f"{'exe' if frozen else 'source'} | "
                f"{platform.platform()} | python {platform.python_version()} | "
                f"data={DATA_DIR} | lefts={lefts} ===")
    except Exception:
        return f"=== VoidFrameRU {APP_VERSION} ==="


def themed_message(parent, text, title="VoidFrameRU", warning=False):
    """Тематический информационный диалог с видимой кнопкой «ОК»."""
    try:
        mod = market_browser_module()
        if mod is None:
            return None
        cls = mod.build_market_browser().MessageDialog
        dialog = cls(parent, str(text), warning=bool(warning), title=title)
        if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
            dialog.exec_()
        return dialog
    except Exception:
        return None


# ── 1.0.0: брендинг и миграция данных ──────────────────────────────────
_BRANDING_MODULE = {"tried": False, "mod": None}
_OCR_CORRECT_MODULE = {"tried": False, "mod": None}

_CURRENCY_ICONS_MODULE = {"tried": False, "mod": None}


def currency_icons_module():
    """Модуль ``currency_icons.py`` (иконки платины/дукатов, 1.0.0)."""
    return _load_local_module("currency_icons.py", "currency_icons",
                              _CURRENCY_ICONS_MODULE)


def currency_icon_html(currency, cfg=None, px=None):
    """HTML-иконка валюты или текстовый суффикс «p»/«дк» (фолбэк)."""
    suffix = "дк" if str(currency).lower() == "ducats" else "p"
    mod = currency_icons_module()
    if mod is None:
        return suffix
    try:
        return mod.html(currency, cfg, px=px)
    except Exception:
        return suffix


def currency_sep(icon):
    """Разделитель: неразрывный для картинки, обычный пробел для «p»/«дк»."""
    return "&#160;" if str(icon or "").startswith("<img") else " "


def money_html(amount, currency="platinum", cfg=None):
    """Сумма + иконка валюты (rich-text) с текстовым фолбэком."""
    mod = currency_icons_module()
    suffix = "дк" if str(currency).lower() == "ducats" else "p"
    if mod is None:
        try:
            return f"{int(round(float(amount)))} {suffix}"
        except Exception:
            return f"{amount} {suffix}"
    try:
        return mod.format_money(amount, currency=currency, cfg=cfg)
    except Exception:
        return f"{amount} {suffix}"


def currency_icon_label(currency, cfg=None, size=16, scale=None):
    """Маленький QLabel с иконкой валюты (для полей ввода) или None."""
    mod = currency_icons_module()
    if mod is None:
        return None
    try:
        return mod.icon_label(currency, cfg, size=size, scale=scale)
    except Exception:
        return None

_DATA_MIGRATION_MODULE = {"tried": False, "mod": None}


def branding_module():
    """Модуль ``branding.py`` (имя/версия/пути к ассетам)."""
    return _load_local_module("branding.py", "branding", _BRANDING_MODULE)


def data_migration_module():
    """Модуль ``data_migration.py`` (копирование данных старой версии)."""
    return _load_local_module("data_migration.py", "data_migration",
                              _DATA_MIGRATION_MODULE)


def run_data_migration(data_dir=None, app_dir=None):
    """Скопировать данные WarframePriceOverlayRU → VoidFrameRU при старте.

    Не запускается, если каталог данных задан явно через ``WF_DATA_DIR``
    (портативный режим/тесты): там данные уже лежат на месте.
    """
    if os.environ.get("WF_DATA_DIR"):
        return {"migrated": False, "skipped": ["WF_DATA_DIR"]}
    mod = data_migration_module()
    if mod is None:
        log("Данные: модуль миграции недоступен — работаю как есть")
        return {"migrated": False, "errors": ["модуль недоступен"]}
    try:
        return mod.migrate_if_needed(data_dir or DATA_DIR,
                                     app_dir or APP_DIR, logger=log) or {}
    except Exception as exc:
        log(f"Данные: миграция не выполнена ({type(exc).__name__})")
        return {"migrated": False, "errors": [type(exc).__name__]}


def app_icon():
    """QIcon приложения из ``assets/brand/icon.ico`` (или None)."""
    try:
        from PyQt5.QtGui import QIcon
    except Exception:
        return None
    mod = branding_module()
    path = ""
    if mod is not None:
        try:
            path = mod.brand_path("icon.ico")
        except Exception:
            path = ""
    if not path:
        return None
    try:
        icon = QIcon(path)
        return icon if not icon.isNull() else None
    except Exception:
        return None


def third_party_notices_path():
    """Путь к THIRD_PARTY_NOTICES.md (исходники/_internal/_MEIPASS)."""
    roots = []
    if getattr(sys, "frozen", False):
        roots.append(os.path.dirname(os.path.abspath(sys.executable)))
        roots.append(getattr(sys, "_MEIPASS", ""))
    roots += [os.path.dirname(os.path.abspath(__file__)), os.getcwd()]
    for root in roots:
        if not root:
            continue
        path = os.path.join(root, "THIRD_PARTY_NOTICES.md")
        if os.path.isfile(path):
            return path
    return ""


def show_about(parent=None):
    """Пункт «О программе»: имя, версия и подсказка про данные/логи."""
    text = (f"{APP_NAME} {APP_VERSION}\n\n"
            "Русский оверлей цен warframe.market: скан наград, «Дроп», "
            "маркет и чат.\n"
            f"Данные: {DATA_DIR}\n"
            f"Лог: {app_log_path()}\n\n"
            "Сторонние компоненты и лицензии — см. THIRD_PARTY_NOTICES.md"
            + (f"\n{third_party_notices_path()}"
               if third_party_notices_path() else ""))
    return themed_message(parent, text, title=f"О программе {APP_NAME}")


def themed_confirm(parent, text, title="VoidFrameRU",
                   confirm_text="Да", cancel_text="Отмена", danger=False):
    """Тематическое подтверждение; Enter — да, Esc/Отмена — нет."""
    try:
        mod = market_browser_module()
        if mod is None:
            return False
        cls = mod.build_market_browser().ConfirmDialog
        dialog = cls(parent, str(text), confirm_text=confirm_text,
                     cancel_text=cancel_text, danger=bool(danger),
                     title=title)
        if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
            return False
        return dialog.exec_() == 1
    except Exception:
        return False


def startup_error_text(exc):
    # 1.0.0: в диалоге — только тип ошибки; текст/трейс — в app.log
    reason = type(exc).__name__ if exc is not None else "ошибка"
    return (f"Не удалось запустить ({reason}).\n"
            f"Подробности в {app_log_path()}")


def show_startup_error(exc):
    """Тематический диалог для windowed-сборки; в offscreen — молча."""
    try:
        if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
            return False
        from PyQt5.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance()
        created = app is None
        if app is None:
            app = QApplication([])
        box = QMessageBox()
        try:
            box.setStyleSheet(ui_theme().dialog_qss())
        except Exception:
            pass
        box.setWindowTitle("VoidFrameRU")
        box.setIcon(QMessageBox.Critical)
        box.setText(startup_error_text(exc))
        box.setStandardButtons(QMessageBox.Ok)
        box.exec_()
        if created:
            app.quit()
        return True
    except Exception:
        return False


def enable_dpi_awareness():
    """Без этого Qt и mss видят разные координаты при масштабе Windows 125/150%."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor v2
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def deep_merge(base, extra):
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path=CONFIG_PATH):
    cfg = dict(DEFAULT_CONFIG)
    raw = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                raw = json.load(f)
            cfg = deep_merge(cfg, raw)
            log(f"Конфиг: {path}")
        except Exception as e:
            log(f"Конфиг не прочитан ({e}), беру настройки по умолчанию")
    else:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
            log(f"Создан конфиг: {path}")
        except Exception as e:
            log(f"Конфиг не создан: {e}")
    # миграция со старых конфигов: F8 был сканом, теперь это редактор
    if isinstance(raw, dict) and "hotkey_editor" not in raw:
        if str(raw.get("hotkey_scan", "")).lower() == "f8":
            cfg["hotkey_scan"] = "alt+q"
        cfg["hotkey_editor"] = "f8"
    if isinstance(raw, dict) and "auto_detect" not in raw:
        cfg["auto_detect"] = False
    # горячие клавиши: RU→EN уже при чтении конфига
    for key in _OTHER_HOTKEY_KEYS:
        if cfg.get(key):
            cfg[key] = _hotkey_canon(cfg.get(key))
    raw_picks = cfg.get("pick_hotkeys")
    if isinstance(raw_picks, (list, tuple)):
        cfg["pick_hotkeys"] = [_hotkey_canon(value) for value in raw_picks]
    elif isinstance(raw_picks, str):
        cfg["pick_hotkeys"] = [_hotkey_canon(part)
                               for part in raw_picks.split(",")
                               if part.strip()]
    cfg["debug_dir"] = os.path.join(DATA_DIR, cfg.get("debug_dir") or "ocr_debug")
    return cfg


def ensure_dirs(cfg):
    for d in (DATA_DIR, cfg["debug_dir"]):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass


def fmt_plat(p):
    if p is None:
        return "—"
    return f"{int(p)}p"


def imread_unicode(path, flags=None):
    """cv2.imread не умеет пути с кириллицей — читаем через numpy."""
    import cv2
    import numpy as np
    if flags is None:
        flags = cv2.IMREAD_UNCHANGED
    try:
        data = np.fromfile(path, dtype=np.uint8)
        if data.size == 0:
            return None
        return cv2.imdecode(data, flags)
    except Exception:
        return None


def imwrite_unicode(path, img):
    """cv2.imwrite не умеет пути с кириллицей — пишем через numpy."""
    import cv2
    try:
        ext = os.path.splitext(path)[1] or ".png"
        ok, buf = cv2.imencode(ext, img)
        if ok:
            buf.tofile(path)
            return True
    except Exception:
        pass
    return False


def price_color(p):
    """Цвет числа платины: единый семантический platinum (0.6.4)."""
    if not isinstance(p, (int, float)) or p <= 0:
        return "#8b95a5"
    try:
        return ui_theme().currency_platinum()
    except Exception:
        return "#E7F1FF"


# ─────────────────────────────────────────────────────────────────────────
#  Warframe Market API v2
# ─────────────────────────────────────────────────────────────────────────
def normalize_name(text):
    """Приводит распознанное название к сравнимому виду (русский и английский)."""
    if not text:
        return ""
    t = str(text).lower().replace("ё", "е")
    t = t.replace("—", " ").replace("–", " ")
    t = re.sub(r"[^\w ]+", " ", t, flags=re.UNICODE)
    t = re.sub(r"_+", " ", t)
    # мусор, который OCR цепляет с экрана наград
    t = re.sub(r"\b(owned|crafted|built|owneo|onned|onnd|ondde)\b", " ", t)
    t = re.sub(r"\b(изготовлено|скрафчено|создано|в наличии|есть|имеется)\b", " ", t)
    t = re.sub(r"\b(plat|platinum|ducats?|платина|платины|дукаты?)\b", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


_BLUEPRINT_RE = re.compile(r"\b(?:черт[её]ж|чертеж|blueprint)\b", re.IGNORECASE)
_QTY_PREFIX_RE = re.compile(
    r"^\s*(?:(?:\d+)\s*[xXхХ×]\s*|[xXхХ×]\s*(?:\d+)\s*)+")
_OWNED_STATUS_RE = re.compile(
    r"\s*(?:[-–—:]\s*)?(?:создано|изготовлено|скрафчено|в\s+наличии|"
    r"crafted|owned)\s*[-:]?\s*\d*\s*$", re.IGNORECASE)


#: Словарь частей для пост-правки OCR (fuzzy, rapidfuzz с фолбэком).
PART_WORDS = ("Тетива", "Плечо", "Верхнее Плечо", "Нижнее Плечо",
              "Приёмник", "Ствол", "Приклад", "Клинок", "Рукоять",
              "Связь", "Каркас", "Нейрооптика", "Система", "Застёжка",
              "Пряжка", "Ошейник")


def ocr_correct_module():
    """Модуль ``ocr_correct.py`` (автокоррекция OCR, 1.0.0)."""
    return _load_local_module("ocr_correct.py", "ocr_correct",
                              _OCR_CORRECT_MODULE)


def _ocr_fix_name(name, cfg=None):
    """Скорректировать распознанное название; вернуть ``(name, changed)``.

    Без словаря/модуля возвращает исходное имя (фолбэк без падений).
    """
    text = str(name or "")
    if not text:
        return text, False
    mod = ocr_correct_module()
    if mod is None:
        return text, False
    try:
        fixed, info = mod.correct_name(text, cfg=cfg, logger=log)
    except Exception as exc:
        log(f"OCR: корректор не сработал ({type(exc).__name__})")
        return text, False
    if fixed and fixed != text:
        return fixed, bool(info.get("corrected"))
    return text, False


def fix_part_names(text):
    """Исправить опечатки OCR в названиях частей по словарю."""
    raw = str(text or "")
    if not raw.strip():
        return raw
    known = {"праёчо": "Плечо", "праечо": "Плечо"}
    try:
        from rapidfuzz import fuzz, process
    except Exception:
        fuzz = None
        process = None
    tokens = raw.split(" ")
    flat = [word.casefold() for word in PART_WORDS]
    for index, token in enumerate(tokens):
        base = token.strip(" .,:;()[]").casefold()
        if len(base) < 4:
            continue
        if base in known:
            tokens[index] = known[base]
            continue
        best = None
        if process is not None:
            try:
                hit = process.extractOne(base, flat, scorer=fuzz.ratio)
                if hit:
                    best = (hit[1], PART_WORDS[flat.index(hit[0])])
            except Exception:
                best = None
        else:
            import difflib
            for word, flat_word in zip(PART_WORDS, flat):
                ratio = difflib.SequenceMatcher(None, base, flat_word).ratio() * 100
                if best is None or ratio > best[0]:
                    best = (ratio, word)
        if best and best[0] >= 82:
            canonical = best[1]
            # сохраняем «(Чертеж)» и прочую пунктуацию вокруг слова
            if token[:1].isupper() or base:
                tokens[index] = canonical
    return " ".join(tokens)


def normalize_card_name(text):
    """Нормализует строку карточки награды.

    Возвращает ``(display, is_blueprint, key)``:
    «2 X Чертёж: Форма» -> «Форма (Чертеж)», True, «форма чертеж»;
    «Форма» -> «Форма», False, «форма».
    """
    raw = str(text or "").strip()
    is_blueprint = bool(_BLUEPRINT_RE.search(raw))
    t = _QTY_PREFIX_RE.sub(" ", raw)
    # префикс «Чертёж:» / «Blueprint:» (иногда после количества)
    t = re.sub(r"^\s*(?:черт[её]ж|чертеж|blueprint)\s*[:\-]?\s*", " ",
               t, flags=re.IGNORECASE)
    # если OCR поставил количество после префикса
    t = _QTY_PREFIX_RE.sub(" ", t)
    # суффикс «(чертёж)» / «Blueprint» уже учтён флагом
    t = re.sub(r"[\(\[]\s*(?:черт[её]ж|чертеж|blueprint)\s*[\)\]]", " ",
               t, flags=re.IGNORECASE)
    t = re.sub(r"\s+[:\-]?\s*(?:черт[её]ж|чертеж|blueprint)\s*$", " ",
               t, flags=re.IGNORECASE)
    # «Создано - 7» / «в наличии» — статус, не часть названия
    t = _OWNED_STATUS_RE.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip(" :-")
    t = fix_part_names(t)
    t = re.sub(r"\s*:\s*", ": ", t)
    if not t:
        return "", is_blueprint, ""
    display = f"{t} (Чертеж)" if is_blueprint else t
    return display, is_blueprint, normalize_name(display)


def _is_blueprint_item(key, info):
    """Похож ли элемент каталога на blueprint-вариант."""
    if "чертеж" in key or "blueprint" in key:
        return True
    return str((info or {}).get("slug") or "").endswith("_blueprint")


def word_coverage(query, candidate):
    """Доля значимых слов запроса, которые нашлись в названии."""
    qw = [w for w in query.split() if len(w) > 2
          and w not in ("чертеж", "blueprint", "чертежи")]
    if not qw:
        return 0.0
    cw = candidate.split()
    hits = 0
    for w in qw:
        best = 0.0
        for c in cw:
            r = difflib.SequenceMatcher(None, w, c).ratio()
            if r > best:
                best = r
        threshold = 0.9 if len(w) <= 5 else 0.8
        if best >= threshold:
            hits += 1
    return hits / len(qw)


def _order_online(order):
    user = (order or {}).get("user")
    if isinstance(user, dict):
        status = str(user.get("status") or "").lower()
        if status in ("ingame", "online"):
            return True
        if status == "offline":
            return False
    status = str((order or {}).get("status") or "").lower()
    return status in ("ingame", "online")


def lowest_sell_order(client, slug):
    """Минимальный sell-ордер: сначала онлайн-игроки.

    Возвращает ``{"price", "seller", "online", "error", "count"}``; сеть/ошибки
    не бросает — цена остаётся ``None`` для ручного ввода.
    """
    out = {"price": None, "seller": "", "online": False,
           "error": None, "count": 0}
    try:
        orders = client.orders(slug) if client is not None else []
    except Exception as exc:
        out["error"] = f"не удалось узнать цену ({type(exc).__name__})"
        return out
    sells = []
    for order in orders or []:
        if not isinstance(order, dict) or order.get("visible") is False:
            continue
        kind = str(order.get("type") or order.get("order_type") or "").lower()
        try:
            platinum = int(order.get("platinum"))
        except Exception:
            continue
        if kind != "sell" or platinum < 1:
            continue
        sells.append((platinum, _order_online(order), order))
    out["count"] = len(sells)
    if not sells:
        out["error"] = "нет ордеров — введите цену вручную"
        return out
    online = [item for item in sells if item[1]]
    price, is_online, order = min(online or sells, key=lambda item: item[0])
    user = order.get("user") if isinstance(order.get("user"), dict) else {}
    out.update({"price": price, "online": is_online,
                "seller": user.get("ingameName") or user.get("name") or ""})
    return out


def loot_sell_status(record, client, browser=None):
    """Состояние кнопки «Продать» в строке лута (без сети)."""
    record = dict(record or {})
    slug = str(record.get("slug") or "")
    if client is None or not getattr(client, "has_token", lambda: False)():
        return {"enabled": False, "label": "Продать",
                "tooltip": "войдите на маркет"}
    order = None
    if browser is not None:
        try:
            order = browser.find_my_sell_order(slug)
        except Exception:
            order = None
    if order is not None:
        return {"enabled": True, "label": "Изменить цену",
                "tooltip": "Изменить цену своего ордера", "order": order}
    return {"enabled": True, "label": "Продать",
            "tooltip": "Выставить на продажу по цене рынка"}


def pick_toast_record(result, duplicate=False, cfg=None):
    """Запись эфемерного тоста лута (без HistoryStore).

    ``cfg`` — конфиг приложения: если передан, цены показываются с иконками
    валют (1.0.0); без него — прежний текстовый формат («12p»).
    """
    result = dict(result or {})
    item = result.get("item") if isinstance(result.get("item"), dict) else {}
    name = str(result.get("name") or item.get("market_name")
               or item.get("name") or "предмет")
    price = result.get("price", item.get("price"))
    ducats = item.get("ducats")
    text = name
    if isinstance(price, (int, float)) and price:
        if cfg is None:
            text += f" — {int(price)}p"
        else:
            text += " — " + money_html(price, cfg=cfg)
    if isinstance(ducats, (int, float)) and ducats:
        if cfg is None:
            text += f" · дукаты: {int(ducats)}"
        else:
            text += " · дукаты: " + money_html(ducats, "ducats", cfg=cfg)
    return {
        "kind": "pick",
        "title": ("Уже в дропах" if duplicate else "Добавлено в Дропы"),
        "text": text,
        "action": "drops",
        "item_slug": item.get("slug") or "",
        "item_name": name,
        "item_en_name": item.get("en_name") or "",
        "item_thumb": item.get("thumb") or "",
        "item_icon": item.get("icon") or "",
    }


def assess_set_price(part_price, set_price, parts_prices=None):
    """Оценить, не является ли цена комплекта подозрительно низкой."""
    try:
        set_price = int(set_price)
    except Exception:
        set_price = None
    values = []
    for value in (parts_prices or []):
        try:
            values.append(int(value))
        except Exception:
            pass
    try:
        part_price = int(part_price) if part_price is not None else None
    except Exception:
        part_price = None
    suspicious = False
    reason = ""
    total = sum(values)
    if set_price is not None:
        if set_price <= 1:
            suspicious = True
            reason = "цена ≤ 1p"
        elif len(values) >= 2 and total > 0 and set_price < 0.6 * total:
            suspicious = True
            reason = f"комплект {set_price}p дешевле 60% частей ({total}p)"
        elif values and set_price <= 1.5 * max(values):
            suspicious = True
            reason = f"комплект {set_price}p дешевле 1.5× части"
    return {"price": set_price, "parts_sum": total,
            "parts_count": len(values), "suspicious": suspicious,
            "reason": reason}


def format_set_price_tooltip(data):
    """Tooltip цены комплекта: цена/продавец/статус/время + предупреждение."""
    data = dict(data or {})
    price = data.get("set_price")
    if price is None:
        return "цена комплекта неизвестна"
    seller = str(data.get("set_seller") or "—")
    if data.get("set_online"):
        status = "онлайн"
    elif data.get("set_offline"):
        status = "offline"
    else:
        status = str(data.get("set_status") or "—")
    stamp = data.get("set_updated") or data.get("checked_at")
    try:
        when = datetime.datetime.fromtimestamp(float(stamp)).strftime("%H:%M")
    except Exception:
        when = "—"
    text = (f"мин. {int(price)}p · продавец {seller} · {status} · "
            f"обновлено {when}")
    if data.get("set_suspicious"):
        text += ("\nподозрительно низкая цена — возможна ошибка продавца, "
                 "проверьте на сайте")
    return text


def market_item_url(slug):
    slug = str(slug or "").strip()
    return f"https://warframe.market/items/{slug}" if slug else ""


def open_market_page(slug):
    """Открыть страницу предмета на warframe.market в браузере."""
    url = market_item_url(slug)
    if not url:
        return False
    try:
        import webbrowser
        return bool(webbrowser.open(url))
    except Exception:
        return False


def set_slug_for_part(slug, name=""):
    """slug части → slug полного комплекта (``*_prime_set``)."""
    try:
        mod = item_icons_module()
        if mod is None:
            return ""
        is_part, _badge, parent = mod.part_info(slug, name)
        parent = str(parent or "")
        if not is_part or not parent:
            return ""
        if str(slug or "").lower().endswith("_set"):
            return ""
        if parent.endswith("_set"):
            return parent
        return parent + "_set"
    except Exception:
        return ""


class MarketAPI:
    """Тонкая обёртка над API warframe.market (v2) с кэшами."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            "Accept": "application/json",
            "Platform": "pc",
            "Language": "ru",
            "Crossplay": "true",
        }
        self.timeout = cfg.get("request_timeout", 8)
        self._lock = threading.Lock()
        self._price_cache = {}      # slug -> (ts, data)
        self._detail_cache = {}     # slug -> (ts, data)
        self.items = {}             # norm_name -> {"slug", "name", "lang", "gameRef"}
        self.ref_to_slug = {}       # gameRef -> slug (для лог-пайплайна EE.log)
        self._loaded = False
        #: 1.0.0: уже залогированные неторгуемые предметы (по slug/имени)
        self._nontradable_logged = set()

    # ── HTTP ───────────────────────────────────────────────────────────
    def _get(self, url, retries=2):
        import requests
        last = None
        for attempt in range(retries + 1):
            try:
                r = requests.get(url, headers=self.headers, timeout=self.timeout)
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 404:
                    return None
                last = f"HTTP {r.status_code}"
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
            if attempt < retries:
                time.sleep(0.4 * (attempt + 1))
        raise RuntimeError(last or "network error")

    # ── список предметов ───────────────────────────────────────────────
    @staticmethod
    def _index_from_items(items):
        """Обратный индекс gameRef → slug из словаря предметов."""
        refs = {}
        for v in (items or {}).values():
            if not isinstance(v, dict):
                continue
            ref, slug = v.get("gameRef"), v.get("slug")
            if ref and slug and ref not in refs:
                refs[ref] = slug
        return refs

    def _restore_ref_index(self, payload):
        """Восстанавливает gameRef → slug из кэша на диске (если он там есть)."""
        refs = {}
        try:
            saved = (payload or {}).get("ref_to_slug")
            if isinstance(saved, dict):
                refs.update({str(k): str(v) for k, v in saved.items() if k and v})
        except Exception:
            pass
        for ref, slug in self._index_from_items(self.items).items():
            refs.setdefault(ref, slug)
        if refs:
            with self._lock:
                self.ref_to_slug = refs
            return True
        return False

    def _save_items_cache(self):
        """Пишет кэш предметов вместе с обратным индексом gameRef → slug."""
        try:
            with self._lock:
                payload = {"ts": time.time(), "items": self.items,
                           "ref_to_slug": dict(self.ref_to_slug)}
            with open(ITEMS_CACHE, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            return True
        except Exception:
            return False

    def ensure_ref_index(self):
        """Строит индекс gameRef → slug для лог-пайплайна (один запрос /v2/items).

        Нужен, чтобы путь из EE.log (`/Lotus/StoreItems/...`) превратить в слаг
        warframe.market. В старом кэше поля `gameRef` нет — тогда список
        предметов догружается один раз и кэш дополняется.
        """
        if self.ref_to_slug:
            return True
        self.load_items()
        refs = self._index_from_items(self.items)
        if refs:
            with self._lock:
                self.ref_to_slug = refs
            return True
        try:
            data = self._get(ITEMS_ENDPOINT)
        except Exception as e:
            log(f"Индекс путей предметов не получен: {e}")
            return False
        rows = (data or {}).get("data") or []
        slug_to_ref = {}
        for it in rows:
            slug, ref = it.get("slug"), it.get("gameRef")
            if slug and ref:
                slug_to_ref[slug] = ref
        if not slug_to_ref:
            log("API не отдал gameRef — сопоставление путей из лога недоступно")
            return False
        with self._lock:
            self.ref_to_slug = {ref: slug for slug, ref in slug_to_ref.items()}
            # заодно дополняем кэш предметов путями, чтобы не запрашивать снова
            for v in self.items.values():
                if not isinstance(v, dict) or v.get("gameRef"):
                    continue
                ref = slug_to_ref.get(v.get("slug"))
                if ref:
                    v["gameRef"] = ref
        self._save_items_cache()
        log(f"Индекс путей предметов: {len(self.ref_to_slug)} (gameRef → slug)")
        return True

    def load_items(self, force=False):
        if self._loaded and not force:
            return True
        # диск: кэш применяем сразу, даже если он старый (t60)
        try:
            if not force and os.path.exists(ITEMS_CACHE):
                with open(ITEMS_CACHE, "r", encoding="utf-8") as f:
                    d = json.load(f)
                self.items = d.get("items") or {}
                if self.items:
                    self._loaded = True
                    self._restore_ref_index(d)
                    log(f"База предметов: {len(self.items)} (кэш)")
                    return True
        except Exception:
            pass
        if not force:
            return False
        # сеть
        try:
            data = self._get(ITEMS_ENDPOINT)
            rows = (data or {}).get("data") or []
            items = {}
            refs = {}
            for it in rows:
                slug = it.get("slug")
                if not slug:
                    continue
                game_ref = it.get("gameRef") or ""
                if game_ref and game_ref not in refs:
                    refs[game_ref] = slug
                i18n = it.get("i18n") or {}
                en_meta = i18n.get("en") if isinstance(i18n.get("en"), dict) else {}
                icon = str(en_meta.get("icon") or "")
                thumb = str(en_meta.get("thumb") or "")
                # рынок отдаёт и русские, и английские названия — берём оба,
                # чтобы приложение понимало игру на любом языке
                for lang in ("ru", "en"):
                    name = ((i18n.get(lang) or {}).get("name") or "").strip()
                    if not name:
                        continue
                    key = normalize_name(name)
                    if key and key not in items:
                        items[key] = {"slug": slug, "name": name, "lang": lang,
                                      "gameRef": game_ref,
                                      "icon": icon, "thumb": thumb,
                                      # 1.0.0: флаг торговли из /v2/items
                                      "tradable": it.get("tradable")}
            if items:
                with self._lock:
                    self.items = items
                    self.ref_to_slug = refs
                self._loaded = True
                try:
                    with open(ITEMS_CACHE, "w", encoding="utf-8") as f:
                        json.dump({"ts": time.time(), "items": items,
                                   "ref_to_slug": refs}, f, ensure_ascii=False)
                except Exception:
                    pass
                log(f"База предметов: {len(items)} (загружено с site)")
                return True
            log("API вернул пустой список предметов")
        except Exception as e:
            log(f"Не удалось загрузить список предметов: {e}")
        # аварийно: старый кэш без проверки TTL
        try:
            if os.path.exists(ITEMS_CACHE):
                with open(ITEMS_CACHE, "r", encoding="utf-8") as f:
                    d = json.load(f)
                self.items = d.get("items") or {}
                if self.items:
                    self._loaded = True
                    self._restore_ref_index(d)
                    log(f"База предметов: {len(self.items)} (просроченный кэш)")
                    return True
        except Exception:
            pass
        return False

    # ── поиск предмета ─────────────────────────────────────────────────
    def match(self, ocr_name):
        ocr_name = (ocr_name or "").strip()
        display, is_blueprint, key = normalize_card_name(ocr_name)
        if not key or not self.items:
            return None
        exact_keys = [key]
        if is_blueprint:
            if "чертеж" in key:
                exact_keys.append(key.replace("чертеж", "blueprint"))
            elif "blueprint" in key:
                exact_keys.append(key.replace("blueprint", "чертеж"))
        # запрос из одного служебного «чертеж/blueprint» ничего не значит
        significant = [w for w in key.split()
                       if len(w) > 2 and w not in ("чертеж", "blueprint")]
        if not significant:
            return None
        for exact in exact_keys:
            hit = self.items.get(exact)
            if hit and (not is_blueprint or _is_blueprint_item(exact, hit)):
                return dict(hit, score=1.0, query=key)
        # вариант без «чертеж/blueprint/set» на конце — только для не-blueprint:
        # для чертежа крафченный предмет не подставляем.
        variants = [key]
        if not is_blueprint:
            for suffix in (" blueprint", " set", " prime blueprint",
                           " blueprint set", " чертеж", " набор"):
                if key.endswith(suffix):
                    variants.append(key[: -len(suffix)].strip())
        for v in variants:
            hit = self.items.get(v)
            if hit and (not is_blueprint or _is_blueprint_item(v, hit)):
                return dict(hit, score=0.95, query=key)
        # без слова «чертёж», но с именем, у которого есть только blueprint
        # (например «Парис Прайм»): крафченный exact уже проверен выше.
        if not is_blueprint:
            for suffix in (" чертеж", " blueprint"):
                candidate = f"{key}{suffix}"
                hit = self.items.get(candidate)
                if hit and _is_blueprint_item(candidate, hit):
                    return dict(hit, score=0.92, query=key)
        fuzz = None
        allow_set = (not is_blueprint and
                     ("комплект" in key or "набор" in key
                      or key.endswith(" set") or " set " in key))
        names = [k for k, v in self.items.items()
                 if (allow_set or not (v.get("slug") or "").endswith("_set"))
                 and (_is_blueprint_item(k, v) if is_blueprint
                      else not _is_blueprint_item(k, v))]
        if not names:
            return None
        try:
            from rapidfuzz import fuzz as _fuzz, process
            fuzz = _fuzz
            picked = process.extract(key, names, scorer=fuzz.token_set_ratio,
                                     limit=5, score_cutoff=45)
            cands = [p[0] for p in picked]
        except Exception:
            try:
                cands = difflib.get_close_matches(key, names, n=5, cutoff=0.62)
            except Exception:
                cands = []
        best, best_score = None, 0.0
        for c in cands:
            if fuzz is not None:
                ratio = fuzz.WRatio(key, c) / 100.0
            else:
                ratio = difflib.SequenceMatcher(None, key, c).ratio()
            cov = word_coverage(key, c)
            score = 0.55 * ratio + 0.45 * cov
            if score > best_score:
                best, best_score = c, score
        # не отдаём «похожий, но другой» предмет: слова запроса должны
        # совпасть (в русском названия склоняются — сравниваем нечётко)
        if best and best_score >= 0.72:
            if word_coverage(key, best) < 0.9 and best_score < 0.88:
                return None
            hit = self.items[best]
            return dict(hit, score=best_score, query=key)
        return None

    # ── детали и ордера ────────────────────────────────────────────────
    def _detail(self, slug):
        now = time.time()
        with self._lock:
            c = self._detail_cache.get(slug)
        if c and now - c[0] < self.cfg.get("items_cache_ttl", 86400):
            return c[1]
        try:
            data = self._get(f"{API_ROOT}/items/{slug}")
            detail = (data or {}).get("data") or {}
        except Exception:
            detail = {}
        with self._lock:
            self._detail_cache[slug] = (now, detail)
        return detail

    def _orders(self, slug):
        now = time.time()
        with self._lock:
            c = self._price_cache.get(slug)
        if c and now - c[0] < self.cfg.get("price_ttl", 180):
            return c[1]
        try:
            data = self._get(f"{API_ROOT}/orders/item/{slug}")
            orders = (data or {}).get("data") or []
        except Exception:
            orders = []
        info = summarize_orders(orders)
        with self._lock:
            self._price_cache[slug] = (now, info)
        return info

    def _empty_lookup(self, name):
        return {
            "name": name, "slug": None, "market_name": None,
            "price": None, "best_sell": None, "best_buy": None,
            "sell_online": 0, "buy_online": 0, "ducats": None,
            "tradable": None, "matched": False, "error": None,
        }

    def _match_with_fallback(self, name):
        """Матчинг имени; базовое имя подставляем только НЕ для чертежей.

        1.0.0: ``Forma Blueprint`` больше не даёт цену готовой ``Forma`` —
        чертёж неторгуемый, поэтому фолбэк для blueprint-имён запрещён.
        Для готовых предметов (без «чертёж/blueprint») фолбэк допустим.
        """
        match = self.match(name)
        if match:
            return match, ""
        _display, is_blueprint, _key = normalize_card_name(name)
        if is_blueprint:
            return None, ""
        raw = str(name or "")
        variants = []
        for variant in (
                re.sub(r"[\(\[]\s*(?:черт[её]ж|чертеж|blueprint)\s*[\)\]]",
                       " ", raw, flags=re.IGNORECASE),
                re.sub(r"\b(?:черт[её]ж|чертеж|blueprint|bp)\b", " ", raw,
                       flags=re.IGNORECASE),
                re.sub(r"^\s*(?:черт[её]ж|чертеж|blueprint)\s*[:\-]?\s*", " ",
                       raw, flags=re.IGNORECASE)):
            variant = re.sub(r"\s+", " ", variant).strip(" :-")
            if variant and variant.casefold() != raw.casefold():
                variants.append(variant)
        for variant in variants:
            match = self.match(variant)
            if match:
                return match, variant
        return None, ""

    def _catalog_meta(self, slug):
        """Метаданные предмета из каталога ``/v2/items`` (включая tradable)."""
        try:
            for item in (self.items or {}).values():
                if isinstance(item, dict) and item.get("slug") == slug:
                    return item
        except Exception:
            pass
        return {}

    def _is_tradable(self, slug):
        """``True/False/None`` — флаг торговли из каталога или деталей."""
        tradable = self._catalog_meta(slug).get("tradable")
        if tradable is None:
            tradable = (self._detail(slug) or {}).get("tradable")
        return tradable

    def _log_nontradable(self, name, slug=""):
        """Одна строка на предмет: цена не запрашивается (без спама)."""
        key = str(slug or name or "").strip().lower()
        if not key or key in self._nontradable_logged:
            return False
        self._nontradable_logged.add(key)
        log(f"{name or slug}: не торгуется, цена не запрашивается")
        return True

    def _fill_market(self, out, slug):
        """Дополняет запись ценой, дукатами и числом ордеров по готовому slug.

        1.0.0: для неторгуемых предметов (``tradable: false``) цена = None,
        ордера/дукаты не запрашиваются — в интерфейсе будет «—».
        """
        tradable = self._catalog_meta(slug).get("tradable")
        detail = self._detail(slug) if tradable is not False else {}
        if tradable is None:
            tradable = (detail or {}).get("tradable")
        out["tradable"] = tradable
        if tradable is False:
            self._log_nontradable(out.get("market_name") or out.get("name"),
                                  slug)
            out["ducats"] = None
            out["best_sell"] = None
            out["best_buy"] = None
            out["sell_online"] = 0
            out["buy_online"] = 0
            out["sell_offline"] = False
            out["price"] = None
            return out
        if detail:
            out["ducats"] = detail.get("ducats")
        info = self._orders(slug)
        out["best_sell"] = info.get("best_sell")
        out["best_buy"] = info.get("best_buy")
        out["sell_online"] = info.get("sell_online", 0)
        out["buy_online"] = info.get("buy_online", 0)
        out["sell_offline"] = info.get("sell_offline", False)
        out["price"] = (out["best_sell"] if out["best_sell"] is not None
                        else out["best_buy"])
        return out

    def refresh_items_async(self):
        """t60: сетевая сверка каталога в фоне, без перезапуска."""
        def worker():
            try:
                self.load_items(force=True)
                log(f"База предметов: обновлена в фоне "
                    f"({len(self.items)})")
            except Exception as e:
                log(f"База предметов: фоновая сверка не удалась "
                    f"({type(e).__name__}: {e})")
        threading.Thread(target=worker, daemon=True,
                         name="wf-catalog-refresh").start()
        return True

    def lookup(self, ocr_name):
        """Полная информация по распознанному названию."""
        out = self._empty_lookup(ocr_name)
        if not self.load_items():
            out["error"] = "нет списка предметов"
            return out
        m = self.match(ocr_name)
        if not m:
            fallback, _variant = self._match_with_fallback(ocr_name)
            m = fallback
        if not m:
            out["error"] = "не найден на рынке"
            return out
        out.update({"slug": m["slug"], "market_name": m["name"], "matched": True,
                    "tradable": m.get("tradable")})
        try:
            out["en_name"] = self.en_name_by_slug(m["slug"])
        except Exception:
            out["en_name"] = None
        for field in ("icon", "thumb", "gameRef"):
            out[field] = m.get(field) or ""
        return self._fill_market(out, m["slug"])

    def name_by_slug(self, slug):
        """Название предмета по слагу (первое найденное, обычно русское)."""
        try:
            for v in (self.items or {}).values():
                if isinstance(v, dict) and v.get("slug") == slug:
                    return v.get("name")
        except Exception:
            pass
        return None

    def en_name_by_slug(self, slug):
        """Английское имя по слагу (для иконок WFCD)."""
        try:
            fallback = None
            for v in (self.items or {}).values():
                if not isinstance(v, dict) or v.get("slug") != slug:
                    continue
                if v.get("lang") == "en":
                    return v.get("name")
                fallback = fallback or v.get("name")
            return fallback
        except Exception:
            return None

    def lookup_slug(self, slug, name=None):
        """То же, что lookup(), но по готовому slug.

        Нужен лог-пайплайну: EE.log даёт точный путь предмета, поэтому
        распознавание названия (и его ошибки) не участвует.
        """
        out = self._empty_lookup(name or slug)
        slug = (slug or "").strip()
        if not slug:
            out["error"] = "нет слага"
            return out
        self.load_items()
        # 1.0.0: slug не подменяем на базовый для чертежей — у чертежа своя
        # (неторгуемая) запись; базовое имя используем только для показа.
        market_name = self.name_by_slug(slug) or name or slug
        out.update({"slug": slug,
                    "market_name": market_name,
                    "matched": True,
                    "tradable": self._catalog_meta(slug).get("tradable")})
        return self._fill_market(out, slug)


def summarize_orders(orders):
    """Считает лучшую продажу/покупку и число активных ордеров.

    В приоритете онлайн-игроки (status online/ingame): оффлайн-ордера часто
    устаревшие. Если онлайн-продавцов нет, берём оффлайн-минимум и помечаем
    его флагом sell_offline, чтобы UI показал «~цену».
    """
    sells_on, sells_off, buys_on, buys_off = [], [], [], []
    for o in orders or []:
        try:
            if not o.get("visible", True):
                continue
            p = int(o.get("platinum") or 0)
            if p <= 0 or p > 100000:
                continue
            if int(o.get("quantity") or 1) < 1:
                continue
            typ = (o.get("type") or o.get("order_type") or "").lower()
            status = ((o.get("user") or {}).get("status") or "").lower()
            online = status in ("online", "ingame")
            if typ == "sell":
                (sells_on if online else sells_off).append(p)
            elif typ == "buy":
                (buys_on if online else buys_off).append(p)
        except Exception:
            continue

    info = {
        "best_sell": None, "best_buy": None,
        "sell_online": len(sells_on), "buy_online": len(buys_on),
        "sell_total": len(sells_on) + len(sells_off),
        "buy_total": len(buys_on) + len(buys_off),
        "sell_offline": False,
        "offline_sell_min": min(sells_off) if sells_off else None,
    }
    if sells_on:
        info["best_sell"] = min(sells_on)
    elif sells_off:
        # нет онлайн-продавцов: показываем минимум по оффлайну с пометкой
        info["best_sell"] = min(sells_off)
        info["sell_offline"] = True
    if buys_on:
        info["best_buy"] = max(buys_on)
    return info


# ─────────────────────────────────────────────────────────────────────────
#  OCR
# ─────────────────────────────────────────────────────────────────────────
_SHARPEN = None


def _to_gray(img):
    import cv2
    if img.ndim == 3 and img.shape[2] == 4:
        return cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
    if img.ndim == 3:
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return img


def prepare_ocr(img, variant="soft"):
    """Готовит картинку к распознаванию. Возвращает (картинка, коэффициент).

    soft     — лёгкий апскейл + шарпинг (лучше всего на реальных кадрах);
    contrast — CLAHE + 2x (страховка для тёмных/шумных кадров).
    """
    import cv2
    import numpy as np
    global _SHARPEN
    if _SHARPEN is None:
        _SHARPEN = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]], dtype=np.float32)
    gray = _to_gray(img)
    if variant == "contrast":
        gray = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(gray)
        scale = 2.0
    else:
        scale = 1.5
    up = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return cv2.filter2D(up, -1, _SHARPEN), scale


class OcrEngine:
    def __init__(self, cfg, use_gpu=False):
        import easyocr
        log("Загрузка EasyOCR (первый запуск может занять до минуты)...")
        t = time.time()
        if not use_gpu:
            try:
                import torch
                torch.set_num_threads(max(1, min(os.cpu_count() or 4, 6)))
                try:
                    torch.set_num_interop_threads(1)
                except Exception:
                    pass
            except Exception:
                pass
        self.reader = easyocr.Reader(
            list(cfg.get("ocr_languages") or ["ru", "en"]),
            gpu=use_gpu, model_storage_directory=models_dir())
        log(f"EasyOCR готов за {time.time()-t:.1f} с")
        self._warm_done = False

    def _warm(self):
        if self._warm_done:
            return
        try:
            import numpy as np
            warm = np.zeros((40, 160, 3), dtype=np.uint8)
            self.reader.readtext(warm, paragraph=False, canvas_size=400,
                                 mag_ratio=1.0)
        except Exception:
            pass
        self._warm_done = True

    def warmup_async(self):
        """dummy-OCR в отдельном потоке, чтобы не задерживать старт UI."""
        if self._warm_done:
            return False
        threading.Thread(target=self._warm, daemon=True,
                         name="wf-ocr-warmup").start()
        return True

    def read_proc(self, proc):
        try:
            return self.reader.readtext(proc, paragraph=False, mag_ratio=1.0,
                                        canvas_size=1280, width_ths=0.7,
                                        decoder="greedy")
        except Exception as e:
            log(f"  OCR ошибка: {e}")
            return []



OWNED_ALLOWLIST = "0123456789OWNEDCRAFTEDowedcrafted/ "


def parse_owned_label(text):
    """'CRAFTED' -> (1, 'CRAFTED'), '10 OWNED' -> (10, 'OWNED'), мусор -> (None, None).

    Метка в игре бывает двух видов: «Crafted» (с молотком) и «N Owned»
    (с галочкой). OCR на маленьком шрифте ошибается, поэтому слова
    сравниваются нечётко, а цифры берутся как есть.
    """
    t = (text or "").upper().replace("Ё", "Е")
    letters = re.sub(r"[^A-ZА-Я]", "", t)
    digits = re.findall(r"\d+", t)
    kind = None
    if len(letters) >= 3:
        words = {"CRAFTED": ("CRAFTED", "ИЗГОТОВЛЕНО", "СКРАФЧЕНО", "СОЗДАНО"),
                 "OWNED": ("OWNED", "ВНАЛИЧИИ", "ЕСТЬ", "ИМЕЕТСЯ")}
        ratios = {k: max(difflib.SequenceMatcher(None, letters, w).ratio()
                         for w in variants)
                  for k, variants in words.items()}
        best = max(ratios, key=ratios.get)
        if ratios[best] >= 0.55:
            kind = best
    count = int(digits[0]) if digits else None
    if kind is None and count is None:
        return None, None
    if kind == "CRAFTED":
        return (count if count is not None else 1), "CRAFTED"
    if kind == "OWNED":
        return (count if count is not None else 1), "OWNED"
    return count, None


def parse_owned(text):
    """'3OWNED' -> 3, 'CRAFTED' -> 1, '0 OWNED' -> 0, мусор -> 0."""
    if not text:
        return 0
    n, kind = parse_owned_label(text)
    if kind == "CRAFTED":
        return n or 1
    if n is not None:
        return min(n, 99)
    return 0


def card_lefts(n, cfg):
    """Левые края карточек: ряд всегда центрирован, шаг фиксированный."""
    w = float(cfg["card_width"])
    pitch = float(cfg["card_pitch"])
    cx = float(cfg["cards_center_x"])
    total = n * w + (n - 1) * (pitch - w)
    start = cx - total / 2.0
    return [start + i * pitch for i in range(n)]


def _word_boxes(res, band, scale):
    words = []
    for bbox, text, conf in res:
        text = (text or "").strip()
        if len(text) < 2 or conf < 0.25:
            continue
        xs = [p[0] for p in bbox]
        ys = [p[1] for p in bbox]
        words.append({
            "text": text,
            "x0": min(xs) / scale + band["left"],
            "x1": max(xs) / scale + band["left"],
            "y0": min(ys) / scale + band["top"],
            "y1": max(ys) / scale + band["top"],
        })
    for w in words:
        w["cx"] = (w["x0"] + w["x1"]) / 2.0
        w["cy"] = (w["y0"] + w["y1"]) / 2.0
    return words


def score_layout(n, words, cfg):
    """Оценивает гипотезу «на экране n карточек». Чем выше — тем правдоподобнее."""
    lefts = card_lefts(n, cfg)
    w_card = float(cfg["card_width"])
    pitch = float(cfg["card_pitch"])
    groups = {i: [] for i in range(n)}
    outside = 0
    for word in words:
        # ближайшая карточка по центру
        idx = min(range(n), key=lambda i: abs(word["cx"] - (lefts[i] + w_card / 2.0)))
        if word["x0"] < lefts[idx] - 30 or word["x1"] > lefts[idx] + w_card + 30:
            outside += 1
            continue
        groups[idx].append(word)

    score = 0.0
    for i in range(n):
        ws = groups[i]
        if not ws:
            # каноническая пустая позиция не должна «сжимать» ряд
            score -= 0.2
            continue
        x0 = min(w["x0"] for w in ws)
        x1 = max(w["x1"] for w in ws)
        span = x1 - x0
        score += 0.5 + min(span / w_card, 1.0)
        # текст вылезает за границы карточки
        over = max(0.0, lefts[i] - x0) + max(0.0, x1 - (lefts[i] + w_card))
        score -= 2.0 * over / w_card

    # слово, «разрезанное» границей между карточками, — почти наверняка ошибка
    for i in range(n - 1):
        b = lefts[i] + w_card + (pitch - w_card) / 2.0
        for word in words:
            if word["x0"] < b - 3 and word["x1"] > b + 3:
                score -= 4.0

    score -= 0.5 * outside
    return score, lefts, groups


def join_words(words):
    """Склеивает слова в строку в порядке чтения (строки сверху вниз)."""
    if not words:
        return ""
    rows = {}
    for w in words:
        key = int(round(w["cy"] / 22.0))
        rows.setdefault(key, []).append(w)
    parts = []
    for key in sorted(rows):
        line = sorted(rows[key], key=lambda w: w["x0"])
        parts.append(" ".join(x["text"] for x in line))
    text = " ".join(parts)
    # чистим мусор от рамок/скобок, который цепляет OCR
    text = re.sub(r"[^\w'\- ]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _empty_cards(cfg, count=4):
    slots = card_lefts(count, cfg)
    return [{"left": float(left), "top": float(cfg["card_top"]),
             "name": "", "unrecognized": True} for left in slots]


def _expand_canonical_grid(groups, lefts, cfg):
    """Если найденные lefts — слоты канонической сетки 4, вернуть её."""
    slots = card_lefts(4, cfg)
    mapping = {}
    for index, left in enumerate(lefts):
        slot = min(range(4), key=lambda i: abs(slots[i] - left))
        if abs(slots[slot] - left) <= 12:
            mapping[index] = slot
    if len(mapping) < 2:
        return None
    groups4 = {i: [] for i in range(4)}
    for index, words in groups.items():
        slot = mapping.get(index)
        if slot is None:
            continue
        groups4[slot].extend(words)
    return slots, groups4


def _cards_from_words(words, cfg, frames_found=None):
    """Превращает слова в карточки 1..4; пустые позиции сохраняются.

    ``frames_found`` — сколько рамок нашёл детектор ``scan_calibrate`` (1–4)
    для этого кадра. Если рамки найдены и образуют ровный ряд — раскладка
    берётся по ним (2 карточки остаются двумя, без фантомных «?»). Если
    детектор не смог (тени/анимация) — прежняя эвристика по словам.
    """
    frames = 0
    try:
        frames = int(frames_found or 0)
    except Exception:
        frames = 0
    forced = int(cfg.get("force_cards") or 0)
    if forced:
        n_best = max(1, min(4, forced))
        lefts = card_lefts(n_best, cfg)
        groups = {i: [] for i in range(n_best)}
        for w in words:
            idx = min(range(n_best),
                      key=lambda i: abs(w["cx"] - (lefts[i] + cfg["card_width"] / 2.0)))
            groups[idx].append(w)
        note = f"  Карточек (принудительно): {n_best}"
    elif frames in (1, 2, 3, 4):
        # источник истины — рамки детектора: 2 карточки = 2 позиции
        n_best = frames
        lefts = card_lefts(n_best, cfg)
        groups = {i: [] for i in range(n_best)}
        for w in words:
            idx = min(range(n_best),
                      key=lambda i: abs(w["cx"] - (lefts[i] + cfg["card_width"] / 2.0)))
            groups[idx].append(w)
        note = ""
    elif not words:
        return _empty_cards(cfg, 4), "  Нечитаемый кадр: 4 позиции «?»"
    else:
        best = None
        for n in (1, 2, 3, 4):
            s, lefts, groups = score_layout(n, words, cfg)
            if best is None or s > best[0]:
                best = (s, n, lefts, groups)
        s, n_best, lefts, groups = best
        expanded = _expand_canonical_grid(groups, lefts, cfg)
        if expanded is not None:
            lefts, groups = expanded
            n_best = 4
            note = (f"  Карточек: 4 (каноническая сетка, "
                    f"lefts={[int(l) for l in lefts]})")
        else:
            if s < 0.4 and not words:
                return _empty_cards(cfg, 4), "  Нечитаемый кадр: 4 позиции «?»"
            note = (f"  Карточек: {n_best}  (score={s:.2f}, "
                    f"lefts={[int(l) for l in lefts]})")

    cards = []
    for i in range(n_best):
        name = join_words(groups.get(i, []))
        corrected = False
        if name:
            # 1.0.0: автокоррекция OCR по словарю ДО матчинга
            name, corrected = _ocr_fix_name(name, cfg)
            name, _bp, _key = normalize_card_name(name)
        cards.append({"left": float(lefts[i]),
                      "top": float(cfg["card_top"]),
                      "name": name,
                      "ocr_corrected": bool(corrected),
                      "unrecognized": not bool(name)})
    if not note:
        texts = sum(1 for i in range(n_best) if groups.get(i))
        note = (f"  Карточек: {n_best} (рамок: {frames}, текстов: {texts}, "
                f"lefts={[int(l) for l in lefts]})")
    return cards, note


def _retry_unrecognized_card(base_img, card, cfg, ocr):
    """Точечный повтор OCR только кропа названия (апскейл 1.8 + контраст)."""
    try:
        import cv2
        left = int(round(float(card.get("left") or 0)))
        top = int(round(float(cfg["card_top"] + cfg["name_offset_y"])))
        width = int(cfg["card_width"])
        height = int(cfg["name_height"])
        x0 = max(0, left - 4)
        y0 = max(0, top - 8)
        x1 = min(base_img.shape[1], left + width + 4)
        y1 = min(base_img.shape[0], top + height + 8)
        crop = base_img[y0:y1, x0:x1]
        if crop.size == 0:
            return ""
        gray = _to_gray(crop)
        gray = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(gray)
        scale = 1.8
        proc = cv2.resize(gray, None, fx=scale, fy=scale,
                          interpolation=cv2.INTER_CUBIC)
        res = ocr.read_proc(proc)
        words = _word_boxes(res, {"left": x0, "top": y0}, scale)
        return join_words(words)
    except Exception:
        return ""


def retry_unrecognized_cards(base_img, cards, cfg, ocr):
    """Повторный OCR только для карточек без имени (не весь кадр)."""
    retried = 0
    for card in cards:
        if card.get("name"):
            continue
        retried += 1
        text = _retry_unrecognized_card(base_img, card, cfg, ocr)
        if text:
            name, _bp, _key = normalize_card_name(text)
            if name:
                card["name"] = name
                card["unrecognized"] = False
    return retried


# ─────────────────────────────────────────────────────────────────────────
#  Калибровка скана (1.0.0, scan_calibrate.py): приоритет источников
#  сохранённая калибровка > авто по кадру > встроенные цифры WFInfo > конфиг.
# ─────────────────────────────────────────────────────────────────────────
#: UI scale из EE.cfg читаем один раз на процесс (файл игры может меняться)
_UI_SCALE_CACHE = {"value": None, "source": ""}


def warframe_ui_scale(cfg=None, refresh=False):
    """Масштаб интерфейса игры: ``(scale, источник)``.

    Приоритет: ручной ``ui_scaling`` в config → ``Flash.FlashDrawScale``
    из ``%LOCALAPPDATA%\\Warframe\\EE.cfg`` → 1.0 (100 %). Если EE.cfg нет
    или ключа нет — пишем одну строку в app.log и считаем 100 %.
    """
    cfg = cfg or {}
    try:
        manual = float(cfg.get("ui_scaling") or 0)
    except Exception:
        manual = 0.0
    if manual > 0:
        return manual, "конфиг"
    if not refresh and _UI_SCALE_CACHE["value"] is not None:
        return _UI_SCALE_CACHE["value"], _UI_SCALE_CACHE["source"]
    mod = reward_geometry_module()
    scale, source = 1.0, "EE.cfg не найден"
    if mod is not None:
        try:
            scale, found, path = mod.ui_scale_from_ee_cfg()
            source = "EE.cfg" if found else "EE.cfg не найден"
            if not found and path:
                source = f"EE.cfg: ключа нет ({os.path.basename(path)})"
        except Exception as e:
            log(f"Масштаб интерфейса: EE.cfg не прочитан "
                f"({type(e).__name__}: {e})")
    _UI_SCALE_CACHE["value"] = scale
    _UI_SCALE_CACHE["source"] = source
    return scale, source


def ui_scale_percent(scale):
    try:
        return f"{int(round(float(scale) * 100))}%"
    except Exception:
        return "100%"


def log_ui_scale(cfg=None):
    """Строка в app.log про масштаб интерфейса (один раз на запуск)."""
    scale, source = warframe_ui_scale(cfg)
    log(f"Масштаб интерфейса: {ui_scale_percent(scale)} ({source})")
    return scale, source


def wfinfo_geometry(ui_scale=None, cfg=None):
    """Встроенная геометрия WFInfo для текущего UI scale."""
    mod = reward_geometry_module()
    if mod is None:
        return None
    ui = ui_scale if ui_scale else warframe_ui_scale(cfg)[0]
    try:
        return mod.geometry(ui)
    except Exception as e:
        log(f"Геометрия WFInfo: не собрана ({type(e).__name__}: {e})")
        return None


def apply_wfinfo_geometry(cfg, ui_scale=None):
    """Применить встроенные цифры WFInfo (×UI scale) к конфигу на сессию."""
    mod = reward_geometry_module()
    if mod is None or not isinstance(cfg, dict):
        return {}
    ui = ui_scale if ui_scale else warframe_ui_scale(cfg)[0]
    try:
        values = mod.config_values(ui)
    except Exception:
        return {}
    if not values:
        return {}
    cfg.update(values)
    return values


def detect_game_theme(cfg=None, image=None, ui_scale=None):
    """Тема интерфейса по пробе кадра (или ручной выбор из настроек)."""
    mod = game_themes_module()
    if mod is None:
        return {}
    cfg = cfg or {}
    ui = ui_scale if ui_scale else warframe_ui_scale(cfg)[0]
    manual = cfg.get("game_theme") or "auto"
    try:
        result = mod.detect_theme(image, ui=ui, manual=manual)
    except Exception as e:
        log(f"Тема: не определена ({type(e).__name__}: {e})")
        return {"theme": "AUTO", "manual": False}
    try:
        log(mod.log_line(result))
    except Exception:
        pass
    return result


def scan_calibration_line(geometry, source="конфиг", geom=None):
    """Строка для app.log: «Калибровка: lefts=[…] pitch=… scale=…×…»."""
    mod = scan_calibrate_module()
    if mod is None:
        return f"Калибровка: (источник: {source})"
    geometry = dict(geometry or {})
    try:
        sx = float((geom or {}).get("sx") or geometry.get("ui_scale") or 1.0)
        sy = float((geom or {}).get("sy") or geometry.get("ui_scale") or 1.0)
        geometry.setdefault("ui_scale", round(sx, 3))
    except Exception:
        sx = sy = 1.0
    try:
        return mod.log_line(geometry, source=source, ui_scale=sx)
    except Exception as e:
        return f"Калибровка: строка лога не собрана ({type(e).__name__}: {e})"


def init_scan_calibration(cfg, geom=None):
    """Старт: применить сохранённую калибровку (если она есть в config)."""
    mod = scan_calibrate_module()
    if mod is None:
        return None
    try:
        saved = mod.normalize_calibration(cfg.get("scan_calibration"))
    except Exception as e:
        log(f"Калибровка: сохранённая запись не читается ({type(e).__name__})")
        return None
    if not saved:
        return None
    try:
        applied = mod.apply_geometry(cfg, saved)
    except Exception as e:
        log(f"Калибровка: не применилась ({type(e).__name__}: {e})")
        return None
    if applied:
        log(scan_calibration_line(saved, "конфиг", geom))
    return saved


def scan_geometry_effective(cfg, frame=None, calib_cache=None,
                            ui_scale=None):
    """Геометрия для текущего кадра: ``(cfg_eff, geometry|None, источник)``.

    Приоритет (1.0.0): сохранённая калибровка (t4) → авто-определение по
    кадру (t4, кэш на сессию) → встроенные цифры WFInfo × UI scale →
    ручной конфиг. Сохранённую калибровку ставит ``init_scan_calibration``.
    """
    mod = scan_calibrate_module()
    saved = None
    if mod is not None:
        try:
            saved = mod.normalize_calibration(cfg.get("scan_calibration"))
        except Exception:
            saved = None
    if saved is not None:
        return cfg, None, "конфиг"
    if mod is not None and frame is not None:
        hint = wfinfo_geometry(ui_scale, cfg) if reward_geometry_module() else None
        try:
            geometry, source = mod.resolve_geometry(
                cfg, frame=frame, expected=0, cache=calib_cache, hint=hint)
        except Exception as e:
            log(f"Калибровка: авто-определение не удалось "
                f"({type(e).__name__}: {e})")
            geometry, source = None, "конфиг"
        if geometry and source == "авто":
            try:
                applied = mod.apply_geometry(dict(cfg), geometry)
            except Exception:
                applied = {}
            if applied:
                cfg_eff = dict(cfg)
                cfg_eff.update(applied)
                return cfg_eff, geometry, "авто"
    # встроенные цифры WFInfo (под текущий UI scale) — точнее старых констант
    rg = reward_geometry_module()
    if rg is not None:
        ui = ui_scale if ui_scale else warframe_ui_scale(cfg)[0]
        try:
            values = rg.config_values(ui)
        except Exception:
            values = {}
        if values:
            cfg_eff = dict(cfg)
            cfg_eff.update(values)
            try:
                geometry = rg.geometry(ui)
            except Exception:
                geometry = None
            return cfg_eff, geometry, "wfinfo"
    return cfg, None, "конфиг"


def save_scan_calibration(cfg, payload):
    """Сохранить ``scan_calibration`` в config.json и применить на лету."""
    payload = dict(payload or {})
    if not payload:
        return {"ok": False, "error": "пустая калибровка"}
    cfg["scan_calibration"] = dict(payload)
    mod = scan_calibrate_module()
    if mod is not None:
        try:
            mod.apply_geometry(cfg, mod.normalize_calibration(payload) or payload)
        except Exception as e:
            log(f"Калибровка: не применилась на лету "
                f"({type(e).__name__}: {e})")
    saved = save_config_keys(cfg, {"scan_calibration": dict(payload)})
    log(scan_calibration_line(payload, "конфиг"))
    if not saved:
        return {"ok": False, "error": "config.json не записан"}
    return {"ok": True, "scan_calibration": dict(payload)}


def _calibration_check_scan(cfg, geometry):
    """«Проверить» в мастере: снимок + пробный скан названий (без цен)."""
    mod = scan_calibrate_module()
    if mod is None:
        return {"ok": False, "error": "модуль сканирования недоступен"}
    try:
        full, _mon = grab(cfg)
        base = to_base(full, cfg)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    cfg_eff = dict(cfg)
    try:
        mod.apply_geometry(cfg_eff, geometry)
    except Exception:
        pass
    try:
        ocr = OcrEngine(cfg)
    except Exception as exc:
        return {"ok": False, "error": f"OCR: {type(exc).__name__}"}
    try:
        cards = detect_cards(base, cfg_eff, ocr)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    names = [c.get("name") or "?" for c in cards]
    return {"ok": True, "names": names, "cards": len(cards)}


def detect_cards(base_img, cfg, ocr, debug=False, matcher=None, cache=None,
                 calib_cache=None, calib_out=None, info_out=None):
    """
    Возвращает список карточек [{'left', 'top', 'name'}] для 1–4 наград.

    Делает один проход OCR, а если названия плохо узнаются — второй,
    с другим контрастом; берёт тот вариант, где больше совпадений с
    базой предметов (matcher — функция name -> score 0..1).

    1.0.0: если сохранённой калибровки нет, геометрия карточек определяется
    по кадру (``scan_calibrate.detect_geometry``) с кэшем на сессию
    (``calib_cache``). Если найденное число карточек не совпало с
    распознанным — авто не применяется, работаем по конфигу. Итог решения —
    в ``calib_out`` (``geometry``/``source``/``confirmed``).
    """
    import cv2

    ui_scale = warframe_ui_scale(cfg)[0]
    cfg_eff, geometry, source = scan_geometry_effective(
        cfg, base_img, calib_cache, ui_scale=ui_scale)
    # t31: число найденных рамок — источник истины для раскладки 1/2/3/4
    frames_found = 0
    if source == "авто" and isinstance(geometry, dict):
        try:
            frames_found = int(geometry.get("frames_found") or 0)
        except Exception:
            frames_found = 0
    band = dict(cfg_eff["scan_band"])
    x0 = max(0, int(band["left"]))
    y0 = max(0, int(band["top"]))
    x1 = min(base_img.shape[1], int(band["left"] + band["width"]))
    y1 = min(base_img.shape[0], int(band["top"] + band["height"]))
    crop = base_img[y0:y1, x0:x1]
    if crop.size == 0:
        if calib_out is not None:
            calib_out.clear()
            calib_out.update({"geometry": None, "source": "конфиг",
                              "confirmed": False})
        return _empty_cards(cfg, 4)
    cache_key = ""
    if cache is not None:
        try:
            cache_key = cache.image_key(crop)
            cached = cache.ocr_get(cache_key)
            if cached is not None:
                log("  OCR: кэш того же экрана")
                if calib_out is not None:
                    calib_out.clear()
                    calib_out.update({"geometry": geometry, "source": source,
                                      "confirmed": geometry is not None})
                return [dict(card) for card in cached]
        except Exception:
            cache_key = ""

    name_top = cfg_eff["card_top"] + cfg_eff["name_offset_y"]
    name_bot = name_top + cfg_eff["name_height"]
    # окно фильтра — с запасом под оба варианта геометрии (конфиг и калибровка)
    cfg_top = cfg["card_top"] + cfg["name_offset_y"]
    cfg_bot = cfg_top + cfg["name_height"]
    window_lo = min(name_top, cfg_top) - 60
    window_hi = max(name_bot, cfg_bot) + 60

    best = None      # (rank, cards, variant, proc, words)
    for idx, variant in enumerate(("soft", "contrast")):
        proc, scale = prepare_ocr(crop, variant)
        res = ocr.read_proc(proc)
        words = _word_boxes(res, {"left": x0, "top": y0}, scale)
        words = [w for w in words if window_lo <= w["cy"] <= window_hi]
        cards, note = _cards_from_words(words, cfg_eff,
                                  frames_found=frames_found)

        matched, quality = 0, 0.0
        if matcher and cards:
            for c in cards:
                if not c.get("name"):
                    continue
                s = matcher(c["name"]) or 0.0
                if s >= 0.80:
                    matched += 1
                    quality += s
        rank = (len(cards), matched, round(quality, 3), -idx)
        if best is None or rank > best[0]:
            best = (rank, cards, variant, proc, words, note)
        if debug:
            try:
                dbg = crop.copy()
                for w in words:
                    cv2.rectangle(dbg, (int(w["x0"] - x0), int(w["y0"] - y0)),
                                  (int(w["x1"] - x0), int(w["y1"] - y0)), (0, 0, 255), 1)
                imwrite_unicode(os.path.join(cfg["debug_dir"], f"band_{variant}_words.png"), dbg)
                imwrite_unicode(os.path.join(cfg["debug_dir"], f"band_{variant}_proc.png"), proc)
            except Exception:
                pass
        # если распозналась хотя бы половина карточек — второй проход не нужен
        if matcher and cards and matched >= max(1, (len(cards) + 1) // 2):
            break

    rank, cards, variant, proc, words, note = best
    if source == "авто" and geometry is not None \
            and len(geometry.get("lefts") or []) != len(cards):
        # авто-геометрия не подтвердилась распознаванием — по конфигу
        log(f"  Калибровка: авто не подтвердилось (боксов "
            f"{len(geometry.get('lefts') or [])}, карточек {len(cards)}) — "
            "работаю по конфигу")
        if calib_cache is not None:
            try:
                mod = scan_calibrate_module()
                if mod is not None:
                    mod.clear_cache(calib_cache)
            except Exception:
                pass
        geometry = None
        source = "конфиг"
        frames_found = 0
        cfg_eff = cfg
        cards, note = _cards_from_words(words, cfg, frames_found=frames_found)
    if note:
        log(note)
    if calib_out is not None:
        calib_out.clear()
        calib_out.update({"geometry": geometry, "source": source,
                          "confirmed": geometry is not None,
                          "frames_found": frames_found})
    if info_out is not None:
        info_out.clear()
        info_out.update({"frames_found": frames_found,
                         "layout": len(cards),
                         "texts": sum(1 for c in cards
                                      if not c.get("unrecognized")),
                         "source": source})
    if not cards:
        return []
    # точечный повтор только для нераспознанных карточек
    retry_unrecognized_cards(base_img, cards, cfg_eff, ocr)
    if matcher:
        matched_now = sum(1 for c in cards if c.get("name")
                          and (matcher(c["name"]) or 0.0) >= 0.80)
    else:
        matched_now = sum(1 for c in cards if c.get("name"))
    log(f"  наград: {len(cards)}, распознано {matched_now}")
    for index, card in enumerate(cards, 1):
        score = 0.0
        if matcher and card.get("name"):
            score = matcher(card["name"]) or 0.0
        log(f"  карточка {index}: {card.get('name')!r} "
            f"(score={score:.2f})")
    if cache is not None and cache_key:
        try:
            cache.ocr_set(cache_key, [dict(card) for card in cards])
        except Exception:
            pass
    if matcher:
        log(f"  OCR: {variant}, совпало {matched_now}/{len(cards)}")
    return cards


def read_owned_batch(full_img, sx, sy, cards, cfg, ocr, cache=None,
                     origin=(0, 0)):
    """Читает метки Crafted/N Owned всех карточек одним вызовом EasyOCR.

    ``origin`` — экранное смещение окна 1920×1080 (равномерный масштаб, 16:9 → 0).
    """
    import cv2
    import numpy as np
    if not cards:
        return []
    ox, oy = int(origin[0]), int(origin[1])
    strips, segments = [], [None] * len(cards)
    width = 0
    for card in cards:
        x0 = max(0, int(round(card["left"] * sx)) + ox - 2)
        x1 = min(full_img.shape[1],
                 int(round((card["left"] + cfg["card_width"]) * sx)) + ox)
        y0 = max(0, int(round(card["top"] * sy)) + oy)
        y1 = min(full_img.shape[0],
                 int(round((card["top"] + cfg.get("owned_strip_height", 30))
                           * sy)) + oy)
        if x1 - x0 < 20 or y1 - y0 < 10:
            strips.append(None)
            continue
        crop = full_img[y0:y1, x0:x1]
        if crop.ndim == 3 and crop.shape[2] == 4:
            gray = cv2.cvtColor(crop, cv2.COLOR_BGRA2GRAY)
        elif crop.ndim == 3:
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        else:
            gray = crop
        strip = cv2.resize(gray, None, fx=1.25, fy=1.25,
                           interpolation=cv2.INTER_CUBIC)
        strips.append(strip)
        width = max(width, strip.shape[1])
    gap = 12
    height = sum(s.shape[0] + gap for s in strips if s is not None)
    if width <= 0 or height <= 0:
        return [(None, None, None) for _ in cards]
    canvas = np.zeros((height, width), dtype=np.uint8)
    y = 0
    for idx, strip in enumerate(strips):
        if strip is None:
            segments[idx] = None
            continue
        canvas[y:y + strip.shape[0], :strip.shape[1]] = strip
        segments[idx] = (y, y + strip.shape[0])
        y += strip.shape[0] + gap
    key = ""
    if cache is not None:
        try:
            key = cache.image_key(canvas)
            cached = cache.ocr_get(key)
            if cached is not None:
                return [tuple(item) for item in cached]
        except Exception:
            key = ""
    try:
        res = ocr.reader.readtext(canvas, paragraph=False, mag_ratio=1.0,
                                  canvas_size=1280, decoder="greedy")
    except Exception as exc:
        log(f"  OCR меток: батч не удался ({type(exc).__name__}), поштучно")
        return [read_owned(full_img, sx, sy, card, cfg, ocr, origin=origin)
                for card in cards]
    parts = [[] for _ in cards]
    for item in res:
        try:
            if len(item) >= 3:
                bbox, text, conf = item[0], item[1], item[2]
            else:
                bbox, text, conf = None, item[1], item[2] if len(item) > 2 else 1
            if conf is not None and float(conf) <= 0.25:
                continue
            if bbox:
                ys = [float(point[1]) for point in bbox]
                cy = (min(ys) + max(ys)) / 2.0
            else:
                # упрощённый фейк: делим результаты поровну по карточкам
                cy = (len(parts) and
                      (parts.index([]) if [] in parts else 0) *
                      (height / max(1, len(cards)))) or 0
        except Exception:
            continue
        for idx, segment in enumerate(segments):
            if segment is not None and segment[0] <= cy <= segment[1]:
                parts[idx].append(str(text))
                break
    out = []
    for idx, values in enumerate(parts):
        text = " ".join(values).strip().upper()
        n, kind = parse_owned_label(text)
        out.append((n, kind, text))
    if cache is not None and key:
        try:
            cache.ocr_set(key, [tuple(item) for item in out])
        except Exception:
            pass
    return out


def read_owned(full_img, sx, sy, card, cfg, ocr, origin=(0, 0)):
    """Метка Crafted / N Owned в шапке карточки (читается с полного разрешения).

    Читаем всю верхнюю полосу карточки и без allowlist: на маленьком шрифте
    со свечением он только мешает (проверено на реальном кадре с 3 наградами).
    """
    import cv2
    ox, oy = int(origin[0]), int(origin[1])
    x0 = max(0, int(round(card["left"] * sx)) + ox - 2)
    x1 = min(full_img.shape[1],
             int(round((card["left"] + cfg["card_width"]) * sx)) + ox)
    y0 = max(0, int(round(card["top"] * sy)) + oy)
    y1 = min(full_img.shape[0],
             int(round((card["top"] + cfg.get("owned_strip_height", 30)) * sy))
             + oy)
    if x1 - x0 < 20 or y1 - y0 < 10:
        return None, None, None
    crop = full_img[y0:y1, x0:x1]
    if crop.ndim == 3 and crop.shape[2] == 4:
        gray = cv2.cvtColor(crop, cv2.COLOR_BGRA2GRAY)
    elif crop.ndim == 3:
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    else:
        gray = crop
    proc = cv2.resize(gray, None, fx=4.0, fy=4.0, interpolation=cv2.INTER_CUBIC)
    try:
        res = ocr.reader.readtext(proc, paragraph=False, mag_ratio=1.0,
                                  canvas_size=1600)
    except Exception:
        res = []
    text = " ".join(t for (_, t, c) in res if c > 0.25).strip().upper()
    n, kind = parse_owned_label(text)
    return n, kind, text


# ─────────────────────────────────────────────────────────────────────────
#  Оверлеи (Qt)
# ─────────────────────────────────────────────────────────────────────────
class ItemOverlay:
    pass  # заполняется ниже, после импорта Qt


class SummaryOverlay:
    pass


def build_overlays():
    """Фабрика классов — Qt импортируется лениво, чтобы --help работал без него."""
    from PyQt5.QtCore import Qt, QTimer, QObject, pyqtSignal, QPoint
    from PyQt5.QtGui import QFont
    from PyQt5.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                                 QMenu, QMessageBox, QPushButton, QVBoxLayout,
                                 QWidget)
    theme = ui_theme()

    class _ItemOverlay(QWidget):
        def __init__(self, cfg):
            super().__init__()
            self.cfg = cfg
            self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint |
                                Qt.Tool | Qt.WindowTransparentForInput |
                                Qt.WindowDoesNotAcceptFocus)
            self.setAttribute(Qt.WA_TranslucentBackground)
            self.setAttribute(Qt.WA_ShowWithoutActivating)
            outer = QVBoxLayout(self)
            outer.setContentsMargins(0, 0, 0, 0)
            self.frame = QFrame()
            self.frame.setObjectName("card")
            self._style_normal = """
                QFrame#card { background-color: rgba(8, 11, 18, 238);
                              border: 1px solid #24344a; border-radius: 6px; }
                QLabel { border: none; background: transparent; color: #e8eef7;
                         font-family: 'Segoe UI', Arial, sans-serif; }
            """
            self._style_best = """
                QFrame#card { background-color: rgba(12, 14, 8, 242);
                              border: 1px solid #e8c05a; border-radius: 6px; }
                QLabel { border: none; background: transparent; color: #f2f6fc;
                         font-family: 'Segoe UI', Arial, sans-serif; }
            """
            self.frame.setStyleSheet(self._style_normal)

            lay = QVBoxLayout(self.frame)
            lay.setContentsMargins(9, 2, 9, 3)
            lay.setSpacing(0)

            self.lbl_name = QLabel("")
            self.lbl_name.setStyleSheet("color:#cfd8e6; font-size:11px; font-weight:600;")
            self.lbl_name.setMaximumHeight(15)
            self.part_chip = item_icons_module().part_chip_label("", "")
            self._last_data = None
            self._icon_key = ""
            self._icon_provider = item_icons_provider(cfg)
            self.icon_label = QLabel()
            self.icon_label.setFixedSize(32, 32)
            self.icon_label.setStyleSheet("background: transparent;")
            self.icon_label.setToolTip("Иконка предмета")
            self.icon_badge = QLabel("Ч", self.icon_label)
            self.icon_badge.setFixedSize(34, 18)
            self.icon_badge.setAlignment(Qt.AlignCenter)
            self.icon_badge.setStyleSheet("background:transparent;")
            self.icon_badge.move(2, 14)
            self.icon_badge.setVisible(False)
            self._set_placeholder_icon()

            mid = QHBoxLayout()
            mid.setContentsMargins(0, 0, 0, 0)
            mid.setSpacing(6)
            self.lbl_price = QLabel("…")
            self.lbl_price.setStyleSheet(
                f"font-size:25px; font-weight:700; color:{theme.currency_platinum()};")
            self.lbl_price.setToolTip(theme.currency_tooltip("platinum"))
            self.lbl_ducats = QLabel("")
            self.lbl_ducats.setStyleSheet(
                f"color:{theme.currency_ducats()}; font-size:15px; "
                "font-weight:600;")
            self.lbl_ducats.setToolTip(theme.currency_tooltip("ducats"))
            self.lbl_ducats.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            mid.addWidget(self.lbl_price)
            mid.addStretch(1)
            mid.addWidget(self.lbl_ducats)

            self.lbl_set = QLabel("")
            set_font = QFont(self.font())
            set_font.setPixelSize(12)
            set_font.setWeight(QFont.DemiBold)
            self.lbl_set.setFont(set_font)
            self.lbl_set.setTextFormat(Qt.RichText)
            self.lbl_set.setToolTip("Полный комплект")
            self.lbl_set.setVisible(False)
            self.btn_set_market = QPushButton("↗")
            self.btn_set_market.setFixedSize(18, 18)
            market_font = QFont(self.font())
            market_font.setPixelSize(10)
            market_font.setBold(True)
            self.btn_set_market.setFont(market_font)
            self.btn_set_market.setStyleSheet(theme.button_qss(small=True))
            self.btn_set_market.setCursor(Qt.PointingHandCursor)
            self.btn_set_market.setToolTip("Открыть на маркете")
            self.btn_set_market.clicked.connect(self._open_set_market)
            self.btn_set_market.setVisible(False)
            set_row = QHBoxLayout()
            set_row.setContentsMargins(0, 1, 0, 0)
            set_row.setSpacing(4)
            set_row.addWidget(self.lbl_set)
            set_row.addWidget(self.btn_set_market)
            set_row.addStretch(1)

            bot = QHBoxLayout()
            bot.setContentsMargins(0, 0, 0, 0)
            bot.setSpacing(4)
            self.lbl_owned = QLabel("")
            self.lbl_owned.setStyleSheet("color:#7b8798; font-size:10px; font-weight:600;")
            self.lbl_sold = QLabel("")
            self.lbl_sold.setStyleSheet("color:#8b97a8; font-size:10px;")
            self.lbl_sold.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            bot.addWidget(self.lbl_owned)
            bot.addStretch(1)
            bot.addWidget(self.lbl_sold)

            top = QHBoxLayout()
            top.setContentsMargins(0, 0, 0, 0)
            top.setSpacing(6)
            top.addWidget(self.icon_label)
            top.addWidget(self.lbl_name, 1)
            lay.addLayout(top)
            chip_row = QHBoxLayout()
            chip_row.setContentsMargins(0, 0, 0, 0)
            chip_row.setSpacing(6)
            if self.part_chip is not None:
                chip_row.addWidget(self.part_chip)
            chip_row.addStretch(1)
            lay.addLayout(chip_row)
            lay.addLayout(mid)
            lay.addLayout(set_row)
            lay.addLayout(bot)

            outer.addWidget(self.frame)
            fs = float(cfg.get("font_scale", 1.0))
            self.setFixedSize(int(240 * fs), int(96 * fs))
            try:
                theme.on_currency_change(
                    lambda _p, _d: self._on_currency_changed())
            except Exception:
                pass

        def _on_currency_changed(self):
            try:
                self.lbl_price.setStyleSheet(
                    f"font-size:25px; font-weight:700; "
                    f"color:{theme.currency_platinum()};")
                self.lbl_ducats.setStyleSheet(
                    f"color:{theme.currency_ducats()}; font-size:15px; "
                    "font-weight:600;")
                self.lbl_price.setToolTip(theme.currency_tooltip("platinum"))
                self.lbl_ducats.setToolTip(theme.currency_tooltip("ducats"))
                if self._last_data:
                    self.set_data(self._last_data)
            except Exception:
                pass

        def _set_placeholder_icon(self):
            provider = self._icon_provider
            pix = provider.placeholder(32) if provider is not None else None
            if pix is not None:
                self.icon_label.setPixmap(pix)
            enabled = bool(self.cfg.get("show_item_icons", True))
            self.icon_label.setVisible(enabled)
            self.icon_badge.setVisible(False)

        def _request_icon(self, data):
            provider = self._icon_provider
            enabled = bool(self.cfg.get("show_item_icons", True))
            if provider is None or not enabled:
                self.icon_label.setVisible(False)
                self.icon_badge.setVisible(False)
                return
            name = (data.get("en_name") or data.get("market_name")
                    or data.get("name") or "")
            slug = str(data.get("slug") or "")
            game_ref = str(data.get("gameRef") or "")
            image = provider.resolve(name=name, slug=slug,
                                     en_name=data.get("en_name") or "",
                                     game_ref=game_ref)
            blueprint = False
            badge = ""
            try:
                badge = item_icons_module().part_badge(slug, name)
                blueprint = bool(badge)
            except Exception:
                badge = ""
                blueprint = False
            self.icon_label.setVisible(True)
            badge_pix = None
            try:
                bw, bh = item_icons_module().part_badge_size(slug, name)
                badge_pix = item_icons_module().part_badge_pixmap(
                    slug, name, bw, bh)
            except Exception:
                badge_pix = None
            if badge_pix is not None:
                self.icon_badge.setPixmap(badge_pix)
                self.icon_badge.setFixedSize(bw, bh)
            else:
                self.icon_badge.setText(badge or "Ч")
            self.icon_badge.setVisible(blueprint)
            self.icon_badge.raise_()
            key = slug or name or (image or "unknown")
            self._icon_key = key
            if not slug and not image:
                pix = provider.placeholder(32)
                if pix is not None:
                    self.icon_label.setPixmap(pix)
                self.icon_badge.setVisible(blueprint)
                return
            if (not (data.get("thumb") or data.get("icon"))
                    and not getattr(provider, "has_index", lambda: False)()):
                return
            self._set_placeholder_icon()
            self.icon_label.setVisible(True)
            self.icon_badge.setVisible(blueprint)
            self.icon_badge.raise_()

            def done(done_key, pixmap):
                if done_key != self._icon_key:
                    return
                try:
                    if pixmap is not None and not pixmap.isNull():
                        self.icon_label.setPixmap(
                            pixmap.scaled(32, 32, Qt.KeepAspectRatio,
                                          Qt.SmoothTransformation))
                except Exception:
                    pass

            try:
                provider.request_item(key, data, callback=done, size=32)
            except Exception:
                pass

        def apply_icon_setting(self):
            enabled = bool(self.cfg.get("show_item_icons", True))
            if not enabled:
                self.icon_label.setVisible(False)
                self.icon_badge.setVisible(False)
            elif self._last_data:
                self._request_icon(self._last_data)

        def _update_set_label(self, data):
            show = bool(self.cfg.get("overlay_show_set_price", True))
            price = (data or {}).get("set_price") if show else None
            if price is None:
                self.lbl_set.clear()
                self.lbl_set.setVisible(False)
                self.btn_set_market.setVisible(False)
                return
            name = ((data or {}).get("set_name")
                    or (data or {}).get("set_slug") or "комплект")
            suspicious = bool((data or {}).get("set_suspicious"))
            platinum = (theme.PALETTE["muted"] if suspicious
                        else theme.currency_platinum())
            mark = " (?)" if suspicious else ""
            set_icon = currency_icon_html("platinum", self.cfg, px=10)
            self.lbl_set.setText(
                '<span style="color:rgba(231,241,255,191);'
                ' font-weight:400;">компл.</span> '
                f'<span style="color:{platinum}; font-weight:700;">'
                f'{int(price)}{currency_sep(set_icon)}{set_icon}{mark}</span>')
            tip = format_set_price_tooltip(data)
            if suspicious and (data or {}).get("set_suspect_reason"):
                tip += "\n" + str((data or {}).get("set_suspect_reason"))
            self.lbl_set.setToolTip(tip + f"\n{name}")
            self.lbl_set.setStyleSheet(
                "background: transparent; font-size:12px;")
            self.lbl_set.setVisible(True)
            self.btn_set_market.setVisible(True)
            self.btn_set_market.setToolTip(
                f"Открыть на маркете: {name}")

        def _open_set_market(self):
            data = self._last_data or {}
            slug = (data.get("set_slug") or data.get("slug") or "")
            if slug:
                open_market_page(slug)

        def apply_set_setting(self):
            if self._last_data:
                self._update_set_label(self._last_data)
            else:
                self.lbl_set.clear()
                self.lbl_set.setVisible(False)

        def set_data(self, d):
            self._last_data = dict(d) if d else None
            if not d:
                self.hide()
                return
            self.frame.setStyleSheet(self._style_best if d.get("is_best")
                                     else self._style_normal)

            name = (d.get("market_name") or d.get("name") or "").strip()
            raw_name = name
            if d.get("unrecognized") or not name:
                name = "?"
            if len(name) > 34:
                name = name[:33] + "…"
            try:
                from PyQt5.QtGui import QFontMetrics
                _fm = QFontMetrics(self.lbl_name.font())
                self.lbl_name.setText(_fm.elidedText(
                    name, Qt.ElideRight, 220))
                self.lbl_name.setToolTip(name)
            except Exception:
                self.lbl_name.setText(name)
            try:
                chip_style = item_icons_module().part_style(
                    str(d.get("slug") or ""), raw_name)
                if getattr(self, "part_chip", None) is not None:
                    if chip_style.get("is_part"):
                        self.part_chip.setText(
                            item_icons_module().part_word(
                                str(d.get("slug") or ""), raw_name)
                            or chip_style["type"])
                        self.part_chip.setStyleSheet(
                            "background:#262F40; font-size:9px;"
                            f" font-weight:700; color:{chip_style['color']};"
                            " border-radius:8px; padding:2px 6px;")
                        self.part_chip.setVisible(True)
                    else:
                        self.part_chip.setVisible(False)
            except Exception:
                pass
            try:
                self._request_icon(d)
            except Exception:
                self._set_placeholder_icon()
            self._update_set_label(d)

            owned_txt = d.get("owned") or ""
            self.lbl_owned.setVisible(bool(owned_txt))
            self.lbl_owned.setText(owned_txt)
            color = "#5ce08a" if (d.get("owned_kind") == "CRAFTED" or
                                  (d.get("owned_n") or 0)) else "#7b8798"
            self.lbl_owned.setStyleSheet(
                f"color:{color}; font-size:10px; font-weight:600;")

            price = d.get("price")
            platinum = theme.currency_platinum()
            ducats = theme.currency_ducats()
            # 1.0.0: подсказки сбрасываем для каждого состояния
            self.lbl_price.setToolTip(theme.currency_tooltip("platinum"))
            self.lbl_ducats.setToolTip(theme.currency_tooltip("ducats"))
            if d.get("tradable") is False:
                # неторгуемый предмет: цены нет, дукаты/онлайн не выводим
                self.lbl_price.setText(
                    "<span style='color:#6c7789; font-size:15px;'>—</span>")
                self.lbl_price.setToolTip("не торгуется на warframe.market")
                self.lbl_ducats.setText("")
                self.lbl_ducats.setToolTip("")
                self.lbl_sold.setText("")
            elif d.get("unrecognized"):
                self.lbl_price.setText(
                    "<span style='color:#6c7789; font-size:15px;'>—</span>")
                self.lbl_ducats.setText("")
                self.lbl_sold.setText("")
            elif d.get("pending"):
                self.lbl_price.setText(
                    f"<span style='color:{theme.rgba(platinum, 0.70)};'>…</span>")
                self.lbl_ducats.setText("")
                self.lbl_sold.setText("<span style='color:#6f7b8c;'>запрашиваю</span>")
            elif isinstance(price, (int, float)) and price:
                tilde = "~" if d.get("sell_offline") else ""
                icon = currency_icon_html("platinum", self.cfg, px=10)
                sep = currency_sep(icon)
                self.lbl_price.setText(
                    f"<span style='color:{platinum};'>{tilde}{int(price)}</span>"
                    f"{sep}<span style='color:{theme.rgba(platinum, 0.72)}; "
                    f"font-size:13px;'>{icon}</span>")
                du = d.get("ducats")
                if du:
                    d_icon = currency_icon_html("ducats", self.cfg, px=10)
                    self.lbl_ducats.setText(
                        f"◆ {int(du)}{currency_sep(d_icon)}{d_icon}")
                else:
                    self.lbl_ducats.setText("")
                online = d.get("sell_online", 0)
                self.lbl_sold.setText(f"{online} онлайн" if online else "нет онлайн")
            elif d.get("matched"):
                self.lbl_price.setText("<span style='color:#6c7789; font-size:15px;'>нет ордеров</span>")
                if d.get("ducats"):
                    d_icon = currency_icon_html("ducats", self.cfg, px=10)
                    self.lbl_ducats.setText(
                        f"◆ {int(d['ducats'])}{currency_sep(d_icon)}{d_icon}")
                else:
                    self.lbl_ducats.setText("")
                self.lbl_sold.setText("")
            else:
                self.lbl_price.setText("<span style='color:#6c7789; font-size:15px;'>нет на рынке</span>")
                self.lbl_ducats.setText("")
                self.lbl_sold.setText("")
            self.show()

    class _SummaryOverlay(QWidget):
        def __init__(self, cfg):
            super().__init__()
            # t45/t46: cfg — по ссылке (настройки меняются на лету)
            self.cfg = cfg if isinstance(cfg, dict) else {}
            self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint |
                                Qt.Tool | Qt.WindowTransparentForInput |
                                Qt.WindowDoesNotAcceptFocus)
            self.setAttribute(Qt.WA_TranslucentBackground)
            self.setAttribute(Qt.WA_ShowWithoutActivating)
            outer = QVBoxLayout(self)
            outer.setContentsMargins(0, 0, 0, 0)
            f = QFrame()
            f.setObjectName("sum")
            f.setStyleSheet("""
                QFrame#sum { background-color: rgba(14, 22, 34, 244);
                             border: 1px solid #3f6f9e; border-radius: 9px; }
                QLabel { border: none; background: transparent;
                         font-family: 'Segoe UI', Arial, sans-serif; }
            """)
            lay = QVBoxLayout(f)
            lay.setContentsMargins(14, 8, 14, 8)
            lay.setSpacing(1)
            self.lbl_total = QLabel("Итого: …")
            self.lbl_total.setStyleSheet(
                f"font-size:21px; font-weight:700; "
                f"color:{theme.currency_platinum()};")
            self.lbl_total.setToolTip(theme.currency_tooltip("platinum"))
            self.lbl_best = QLabel(" ")
            self.lbl_best.setStyleSheet("font-size:11px; color:#9fb0c4;")
            lay.addWidget(self.lbl_total)
            lay.addWidget(self.lbl_best)
            outer.addWidget(f)
            fs = float(self.cfg.get("font_scale", 1.0))
            self.setFixedSize(int(380 * fs), int(66 * fs))
            self._last_summary = None
            try:
                theme.on_currency_change(
                    lambda _p, _d: self._on_currency_changed())
            except Exception:
                pass

        def apply_cfg(self, cfg):
            """1.0.0 (t46): актуальные настройки отображения без копий."""
            self.cfg = dict(cfg or {})
            if self._last_summary is not None:
                data, final = self._last_summary
                self.set_summary(data, final=final)
            return self

        def _on_currency_changed(self):
            try:
                self.lbl_total.setStyleSheet(
                    f"font-size:21px; font-weight:700; "
                    f"color:{theme.currency_platinum()};")
                if self._last_summary is not None:
                    data, final = self._last_summary
                    self.set_summary(data, final=final)
            except Exception:
                pass

        def set_summary(self, data, final=False):
            self._last_summary = ([dict(d) for d in (data or [])], bool(final))
            total, best = 0, None
            for d in data or []:
                if not d:
                    continue
                p = d.get("price")
                if isinstance(p, (int, float)) and p > 0:
                    total += p
                    if best is None or p > best[0]:
                        best = (p, d.get("market_name") or d.get("name") or "?")
            if final:
                if total > 0:
                    icon = currency_icon_html("platinum", self.cfg, px=10)
                    sep = currency_sep(icon)
                    self.lbl_total.setText(
                        f"<span style='color:{theme.currency_platinum()};'>"
                        f"Итого: {int(total)}{sep}{icon}</span>")
                    self.lbl_best.setText(
                        f"дороже всех: {best[1][:40]} ({int(best[0])}{sep}"
                        f"{icon})" if best else " ")
                    self.show()
                else:
                    self.lbl_total.setText("Итого: —")
                    self.lbl_best.setText("цены не найдены")
                    self.show()
            else:
                self.lbl_total.setText("Итого: …")
                self.lbl_best.setText("подгружаю цены…")
                self.show()

    class _SelectedOverlay(QWidget):
        """Редактор списка «Выбрано из реликвий»: добавить / изменить / удалить.

        Дополнительно: кнопка «продать» у каждой строки (warframe.market) и
        кнопка токена в шапке. Продажа — только через диалог с подтверждением.
        """

        add_requested = pyqtSignal()
        edit_requested = pyqtSignal(int)
        remove_requested = pyqtSignal(int)
        sell_requested = pyqtSignal(int)
        token_requested = pyqtSignal()
        market_requested = pyqtSignal()

        def __init__(self, cfg, embedded=False):
            super().__init__()
            self.cfg = cfg
            self.embedded = bool(embedded)
            self.items = []
            # панель интерактивная: кнопки должны получать клики
            if not self.embedded:
                self.setWindowFlags(Qt.WindowStaysOnTopHint |
                                    Qt.FramelessWindowHint | Qt.Tool)
                self.setAttribute(Qt.WA_TranslucentBackground)
            outer = QVBoxLayout(self)
            outer.setContentsMargins(0, 0, 0, 0)
            self.frame = QFrame()
            self.frame.setObjectName("sel")
            if self.embedded:
                self.frame.setStyleSheet(
                    "QFrame#sel { background: transparent; border: none; }"
                    "QLabel { background: transparent; }")
            else:
                self.frame.setStyleSheet(theme.window_qss("sel", 6))
            lay = QVBoxLayout(self.frame)
            lay.setContentsMargins(10, 6, 10, 7)
            lay.setSpacing(2)

            head = QHBoxLayout()
            head.setContentsMargins(0, 0, 0, 0)
            head.setSpacing(6)
            self.lbl_title = QLabel("Выбрано из реликвий")
            self.lbl_title.setStyleSheet(theme.section_qss())
            self._btn_style = theme.button_qss(small=True)
            self.btn_add = QPushButton("+ добавить")
            self.btn_add.setStyleSheet(self._btn_style)
            self.btn_add.setCursor(Qt.PointingHandCursor)
            self.btn_add.setFixedHeight(18)
            self.btn_add.clicked.connect(self.add_requested.emit)
            self.btn_token = QPushButton("🔑")
            self.btn_token.setStyleSheet(self._btn_style)
            self.btn_token.setCursor(Qt.PointingHandCursor)
            self.btn_token.setFixedSize(20, 18)
            self.btn_token.setToolTip("Токен warframe.market (для продажи)")
            self.btn_token.clicked.connect(self.token_requested.emit)
            self.btn_market = QPushButton("⌕")
            self.btn_market.setStyleSheet(self._btn_style)
            self.btn_market.setCursor(Qt.PointingHandCursor)
            self.btn_market.setFixedSize(20, 18)
            self.btn_market.setToolTip("Маркет: поиск и цены (Alt+M)")
            self.btn_market.clicked.connect(self.market_requested.emit)
            head.addWidget(self.lbl_title)
            head.addStretch(1)
            head.addWidget(self.btn_market)
            head.addWidget(self.btn_token)
            head.addWidget(self.btn_add)

            self.rows_box = QVBoxLayout()
            self.rows_box.setContentsMargins(0, 0, 0, 0)
            self.rows_box.setSpacing(1)

            self.lbl_total = QLabel("Итого выбрано: 0p")
            self.lbl_total.setStyleSheet(
                f"color:{theme.currency_platinum()}; font-size:12px; "
                "font-weight:700;")
            self.lbl_total.setToolTip(theme.currency_tooltip("platinum"))
            lay.addLayout(head)
            lay.addLayout(self.rows_box)
            if self.embedded:
                lay.addStretch(1)
            lay.addWidget(self.lbl_total)
            outer.addWidget(self.frame)
            self._fit(0)

        def _fit(self, n):
            if self.embedded:
                self.setMinimumHeight(120)
                return
            fs = float(self.cfg.get("font_scale", 1.0))
            h = 50 + 20 * max(1, n)
            self.setFixedSize(int(360 * fs), int(h * fs))

        def status_text(self):
            """Статус для шапки единого окна."""
            total = 0
            for item in self.items:
                price = item.get("price")
                if isinstance(price, (int, float)) and price:
                    total += price
            return f"выбрано: {len(self.items)} · {int(total)}p"

        # ── данные ──────────────────────────────────────────────────────
        def has_items(self):
            return bool(self.items)

        def add(self, d):
            self.items.append(dict(d))
            self._rebuild()

        def replace(self, idx, d):
            if 0 <= idx < len(self.items):
                self.items[idx] = dict(d)
                self._rebuild()

        def remove(self, idx):
            if 0 <= idx < len(self.items):
                self.items.pop(idx)
                self._rebuild()

        def _text_for(self, it):
            name = (it.get("market_name") or it.get("name") or "?")
            if len(name) > 28:
                name = name[:27] + "…"
            price = it.get("price")
            p = f"{int(price)}p" if isinstance(price, (int, float)) and price else "—"
            du = it.get("ducats")
            tail = f"  ◆{int(du)} дк" if du else ""
            return f"{name} — {p}{tail}"

        def _clear_rows(self):
            while self.rows_box.count():
                item = self.rows_box.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.setParent(None)
                    w.deleteLater()

        def _rebuild(self):
            self._clear_rows()
            total = 0
            rows = self.items[-8:]
            first = len(self.items) - len(rows)
            for i, it in enumerate(rows):
                idx = first + i
                price = it.get("price")
                if isinstance(price, (int, float)) and price:
                    total += price
                row = QWidget()
                rl = QHBoxLayout(row)
                rl.setContentsMargins(0, 0, 0, 0)
                rl.setSpacing(4)
                lbl = QLabel(f"{idx + 1}. {self._text_for(it)}")
                lbl.setStyleSheet("color:#dbe3ee; font-size:11px;")
                b_sell = QPushButton("₽")
                b_edit = QPushButton("✎")
                b_del = QPushButton("×")
                for b in (b_sell, b_edit, b_del):
                    b.setStyleSheet(self._btn_style)
                    b.setCursor(Qt.PointingHandCursor)
                    b.setFixedSize(18, 16)
                b_sell.setToolTip("Продать на warframe.market")
                b_edit.setToolTip("Изменить предмет")
                b_del.setToolTip("Удалить из списка")
                if it.get("tradable") is False:
                    # неторгуемый: продать нельзя
                    b_sell.setEnabled(False)
                    b_sell.setToolTip("предмет не продаётся")
                b_sell.clicked.connect(lambda _=False, i=idx: self.sell_requested.emit(i))
                b_edit.clicked.connect(lambda _=False, i=idx: self.edit_requested.emit(i))
                b_del.clicked.connect(lambda _=False, i=idx: self.remove_requested.emit(i))
                rl.addWidget(lbl, 1)
                rl.addWidget(b_sell)
                rl.addWidget(b_edit)
                rl.addWidget(b_del)
                self.rows_box.addWidget(row)
            self.lbl_total.setText(f"Итого выбрано: {int(total)}p")
            self._fit(len(rows))

    class _Manager(QObject):
        updated = pyqtSignal(list)
        done = pyqtSignal()

        def __init__(self, cfg, geom, market=None, api=None, embedded_ui=False):
            super().__init__()
            self.cfg = cfg
            self.geom = geom
            self.market = market          # MarketOrderClient (может быть None)
            self.api = api                # MarketAPI: сопоставление имён
            self.unified_window = None    # единое окно вкладок (ставит run_gui)
            self.cards = [_ItemOverlay(cfg) for _ in range(4)]
            self.summary = _SummaryOverlay(cfg)
            self.selected = _SelectedOverlay(cfg, embedded=embedded_ui)
            self.last_data = []
            self.last_candidates = []
            self.timer = QTimer(self)
            self.timer.setSingleShot(True)
            self.timer.timeout.connect(self.hide_all)
            self.sel_timer = QTimer(self)
            self.sel_timer.setSingleShot(True)
            self.sel_timer.timeout.connect(self.hide_selected)
            self.selected.add_requested.connect(self._on_add)
            self.selected.edit_requested.connect(self._on_edit)
            self.selected.remove_requested.connect(self._on_remove)
            self.selected.sell_requested.connect(self._on_sell)
            self.selected.token_requested.connect(self._on_token)
            self.selected.market_requested.connect(self._on_market)
            self.market_browser = None      # окно «Маркет» (ставит run_gui)
            self.updated.connect(self._apply)
            #: какие карточки последнего скана уже отмечены «взятыми»
            self._picked_indices = set()
            # результат последнего скана (для записи «выбрано» после Alt+1…4)
            self.last_results = []
            #: время последнего скана и результат последнего Alt+1..4
            self.last_scan_ts = 0.0
            self.last_pick = {}

        def _place_selected(self):
            g = self.geom
            w, h = self.selected.width(), self.selected.height()
            # 1.0.0: равномерный масштаб + смещение окна (16:9 → ox=oy=0)
            x = int(self.cfg.get("selected_left", 20) * g["sx"]) + int(g.get("ox", 0))
            y = int(self.cfg.get("selected_top", 700) * g["sy"]) + int(g.get("oy", 0))
            x = max(0, min(x, g["width"] - w))
            y = max(0, min(y, g["height"] - h))
            self.selected.move(x, y)

        def show_selected(self, force=False):
            if login_in_progress():
                # вход в браузере: окна приложения поверх браузера не всплывают
                return False
            if self.unified_window is not None:
                self.unified_window.show_tab(0)
                return True
            if not self.selected.has_items() and not force:
                return False
            self._place_selected()
            if self.cfg.get("focus_editor_on_f8", True):
                focus_editor(self.selected)
            else:
                self.selected.show()
                self.selected.raise_()
            self.sel_timer.start(int(self.cfg.get("selected_duration_ms", 30000)))
            return True

        def hide_selected(self):
            if self.unified_window is not None:
                if login_in_progress():
                    # во время входа окно и так спрятано; hide_window() вызвал
                    # бы on_close → restore_game_focus и отобрал фокус у браузера
                    return
                self.unified_window.hide_window()
                return
            self.selected.hide()
            self.sel_timer.stop()
            if not login_in_progress() and self.cfg.get("restore_game_focus", True):
                restore_game_focus()

        # ── редактор списка ────────────────────────────────────────────
        def _make_menu(self, anchor):
            """Меню наград последней реликвии (с ценами и дукатами)."""
            if not self.last_candidates:
                log("  Нет наград последней реликвии — нечего выбирать.")
                return None
            menu = QMenu()
            menu.setStyleSheet("""
                QMenu { background-color: #101826; color: #e8eef7;
                        border: 1px solid #3f5a78; font-size: 12px;
                        font-family: 'Segoe UI', Arial, sans-serif; }
                QMenu::item { padding: 4px 18px; }
                QMenu::item:selected { background-color: #2e5078; }
            """)
            actions = {}
            for i, c in enumerate(self.last_candidates):
                name = (c.get("market_name") or c.get("name") or "?")
                price = c.get("price")
                p = f"{int(price)}p" if isinstance(price, (int, float)) and price else "—"
                du = c.get("ducats")
                text = f"{i + 1}. {name} — {p}"
                if du:
                    text += f"   ◆ {du}"
                act = menu.addAction(text)
                actions[act] = i
            chosen = menu.exec_(anchor.mapToGlobal(QPoint(0, anchor.height())))
            if chosen in actions:
                return self.last_candidates[actions[chosen]]
            return None

        def _on_add(self):
            d = self._make_menu(self.selected.btn_add)
            if d is not None:
                self.selected.add(d)
                self.show_selected(force=True)
                self._save_selected()

        def _on_edit(self, idx):
            d = self._make_menu(self.selected.btn_add)
            if d is not None:
                self.selected.replace(idx, d)
                self.show_selected(force=True)
                self._save_selected()

        def _on_remove(self, idx):
            self.selected.remove(idx)
            self.show_selected(force=True)
            self._save_selected()

        def _save_selected(self):
            save_selected_list(self.cfg, self.selected.items)

        # ── взятая награда (Alt+1…Alt+4) ──────────────────────────────
        @staticmethod
        def _reward_name(card):
            card = card if isinstance(card, dict) else {}
            return str(card.get("market_name") or card.get("name")
                       or "предмет")

        @classmethod
        def _same_reward(cls, a, b):
            """Одна и та же награда (имя + цена), чтобы не дублировать строку."""
            a = a if isinstance(a, dict) else {}
            b = b if isinstance(b, dict) else {}
            if cls._reward_name(a).strip().casefold() != \
                    cls._reward_name(b).strip().casefold():
                return False
            pa, pb = a.get("price"), b.get("price")
            if not isinstance(pa, (int, float)) or not isinstance(pb, (int, float)):
                return True          # цена ещё неизвестна — считаем той же
            return int(pa) == int(pb)

        def _pick_message(self, text, **extra):
            result = dict(extra)
            result.setdefault("message", str(text))
            self.last_pick = result
            if self.unified_window is not None:
                try:
                    self.unified_window.lbl_status.setText(str(text))
                except Exception:
                    pass
            return result

        def pick_reward(self, index):
            """Alt+1…4: записать «взятую» награду из последнего скана Alt+Q.

            Возвращает ``{"ok", "added", "message", "item", "index", ...}``.
            Ничего не пишет, если скана не было, номер вне списка или награда
            уже добавлена. Только по действию пользователя — без автоугадывания.
            """
            try:
                index = int(index)
            except Exception:
                return self._pick_message("неверный номер награды",
                                          ok=False, index=None, added=False)
            if index < 0 or index >= 4:
                return self._pick_message("награда выбирается вашими "
                                          "клавишами отметки",
                                          ok=False, index=index, added=False)
            if login_in_progress():
                return self._pick_message(
                    "вход в браузере — хоткеи наград отключены",
                    ok=False, index=index, added=False, skipped="login")
            cards = [dict(card) for card in (self.last_candidates or [])
                     if isinstance(card, dict)]
            if not cards:
                return self._pick_message(
                    "нажмите Alt+Q, чтобы просканировать награды",
                    ok=False, index=index, added=False, reason="no_scan")
            if index >= len(cards):
                count = len(cards)
                return self._pick_message(
                    f"в этом скане только {count} наград",
                    ok=False, index=index, added=False,
                    reason="out_of_range", cards=count)
            card = cards[index]
            name = self._reward_name(card)
            price = card.get("price")
            tradable = card.get("tradable")
            price_text = (f"{int(price)}p"
                          if isinstance(price, (int, float)) and price else "—")
            already = index in self._picked_indices
            if already:
                self._picked_indices.add(index)
                return self._pick_message(f"уже добавлено: {name} — {price_text}",
                                          ok=True, index=index, added=False,
                                          item=card, name=name, price=price,
                                          tradable=tradable,
                                          reason="duplicate")
            # 1.0.0: разные позиции с одинаковым именем — разные награды
            # (две «Форма (Чертеж)» = количество 2), поэтому проверяем
            # только номер карточки, а не имя.
            self.selected.add(card)
            self._picked_indices.add(index)
            # окно/панель не открываем принудительно: Alt+1..4 нажимают в игре,
            # но уже видимая панель «Выбрано» обновилась в selected.add()
            self._save_selected()
            return self._pick_message(f"добавлено: {name} — {price_text}",
                                      ok=True, index=index, added=True,
                                      item=card, name=name, price=price,
                                      tradable=tradable)

        # ── продажа на warframe.market (только по кнопке) ──────────────
        def _market_slug(self, item):
            """Слаг для рынка: из записи или через сопоставление имени."""
            slug = (item or {}).get("slug")
            if slug:
                return slug
            if self.api is not None:
                name = (item or {}).get("market_name") or (item or {}).get("name")
                try:
                    match = self.api.match(name) if name else None
                except Exception:
                    match = None
                if match and match.get("slug"):
                    item["slug"] = match["slug"]
                    if not item.get("market_name"):
                        item["market_name"] = match.get("name")
                    return item["slug"]
            return None

        def _on_sell(self, idx):
            """Кнопка «₽» у строки: форма ордера; без токена — сначала инструкция."""
            items = self.selected.items
            if not (0 <= idx < len(items)):
                return
            item = dict(items[idx])
            name = item.get("market_name") or item.get("name") or "?"
            if self.market is None:
                themed_message(
                    self.selected,
                    "Модуль market_client.py не найден рядом с программой — "
                    "продажа недоступна.\nAlt+Q, F8 и журнал работают как раньше.",
                    title="Продажа на warframe.market", warning=True)
                return
            if not self._market_slug(item):
                themed_message(
                    self.selected,
                    f"«{name}» не сопоставлен с warframe.market — продать нельзя.",
                    title="Продажа на warframe.market")
                return
            if not self.market.has_token() and not self._ask_token():
                return
            self._open_sell_dialog(item)

        def _on_token(self):
            """Кнопка «🔑» в шапке: вставить или заменить токен в любой момент."""
            if self.market is None:
                themed_message(
                    self.selected,
                    "Модуль market_client.py не найден рядом с программой.",
                    title="Токен warframe.market", warning=True)
                return
            self._ask_token(announce=True)

        def _on_market(self):
            """Кнопка «⌕» в шапке редактора: открыть «Маркет» (Alt+M)."""
            if self.unified_window is not None:
                self.unified_window.show_tab(0)
                return
            if self.market_browser is None:
                log("  маркет: окно недоступно (нет market_browser.py).")
                return
            try:
                self.market_browser.show_browser()
            except Exception as e:
                log(f"  маркет: окно не открылось ({type(e).__name__}: {e})")

        def _ask_token(self, announce=False):
            """Окно с инструкцией и полем JWT. True, если токен появился."""
            try:
                _, token_cls = build_market_dialogs()
                dialog = token_cls(self.cfg, self.market, self.selected,
                                   save_fn=save_market_token)
                accepted = bool(dialog.exec_())
                has_token = self.market.has_token()
                if accepted and announce and has_token:
                    themed_message(
                        self.selected,
                        "Токен warframe.market: "
                        f"{market_token_masked(self.market.token())}.\n"
                        "Он хранится только в config.json на этом компьютере.",
                        title="Токен сохранён")
                return bool(accepted and has_token)
            except Exception as e:
                log(f"  токен: окно не открылось ({type(e).__name__}: {e})")
                return False

        def _open_sell_dialog(self, item):
            """Открывает форму sell-ордера (сеть — в фоновом потоке диалога)."""
            try:
                sell_cls, _ = build_market_dialogs()
                dialog = sell_cls(self.cfg, self.market, item, self.selected)
                dialog.exec_()
            except Exception as e:
                log(f"  продажа: форма не открылась ({type(e).__name__}: {e})")
                themed_message(
                    self.selected,
                    f"Не удалось открыть форму продажи ({type(e).__name__}).",
                    title="Продажа на warframe.market", warning=True)

        def is_visible(self):
            return any(w.isVisible() for w in self.cards)

        def has_data(self):
            return any(self.last_data)

        def show_last(self):
            if login_in_progress():
                return False
            if self.has_data():
                self._apply(self.last_data)
            self.show_selected()

        def place(self, widget, x_base, y_base, above_base=None):
            """x_base/y_base — позиция под карточкой, above_base — над карточкой."""
            g = self.geom
            w, h = widget.width(), widget.height()
            # 1.0.0: равномерный масштаб + смещение окна базы (16:9 → 0)
            ox, oy = int(g.get("ox", 0)), int(g.get("oy", 0))
            x = int(x_base * g["sx"]) + ox
            y = int(y_base * g["sy"]) + oy
            if y + h > g["height"] and above_base is not None:
                y = int(above_base * g["sy"]) + oy - h - int(6 * g["sy"])
            x = max(0, min(x, g["width"] - w))
            y = max(0, min(y, g["height"] - h))
            widget.move(x, y)

        def panel_y(self):
            """Где ставить плашки: под карточками или внизу экрана."""
            if self.cfg.get("overlay_layout", "bottom") == "bottom":
                return float(self.cfg.get("overlay_top", 830))
            return float(self.cfg["card_top"] + self.cfg["card_height"] +
                         self.cfg["overlay_offset_y"])

        def _apply(self, data):
            if login_in_progress():
                # Плашки и «Итого» не показываем, пока идёт вход: они topmost
                # и перекрыли бы браузер (и увели бы мышь у формы логина).
                self.hide_overlays()
                return
            for w in self.cards:
                w.hide()
            data = [d for d in (data or []) if d]
            if data:
                self.last_data = [dict(d) for d in data]
                self.last_candidates = [dict(d) for d in data]
                # новый скан — новые карточки: отметки «взято» сбрасываем
                self._picked_indices = set()
                self.last_scan_ts = time.time()
            else:
                # скан не нашёл карточек: «последнего скана» для Alt+1..4 нет
                self.last_candidates = []
                self._picked_indices = set()
            prices = [d.get("price") for d in data
                      if isinstance(d.get("price"), (int, float)) and d.get("price")]
            best_price = max(prices) if prices else None
            shown = 0
            for i, d in enumerate(data):
                if i >= len(self.cards):
                    continue
                d = dict(d)
                if d.get("card_left") is None:
                    d["card_left"] = self.cfg["cards_center_x"]
                d["is_best"] = bool(best_price and len(prices) > 1 and
                                    d.get("price") == best_price)
                self.cards[i].set_data(d)
                left = d["card_left"]
                self.place(self.cards[i], left, self.panel_y(),
                           above_base=(self.cfg["card_top"] - 6
                                       if self.cfg.get("overlay_layout") != "bottom"
                                       else None))
                shown += 1
            if shown:
                if self.cfg.get("show_summary", True):
                    self.summary.cfg = self.cfg
                    self.summary.set_summary(data, final=any(
                        isinstance(d.get("price"), (int, float))
                        for d in data if d))
                    cx = self.cfg["cards_center_x"]
                    self.summary.move(
                        max(0, int(cx * self.geom["sx"]
                                   + self.geom.get("ox", 0)
                                   - self.summary.width() / 2)),
                        int(self.cfg.get("summary_top", 84) * self.geom["sy"]
                            + self.geom.get("oy", 0)))
                    self.summary.raise_()
                else:
                    # «Итого» выключено в настройках (F8 → Настройки)
                    self.summary.hide()
            else:
                self.summary.hide()
            self.timer.start(int(self.cfg.get("overlay_duration_ms", 12000)))

        def hide_overlays(self):
            """Скрыть только плашки наград и «Итого» (без окон и без фокуса).

            Используется режимом входа: во время входа нельзя звать
            ``hide_selected``/``hide_window`` — они возвращают фокус игре
            (``restore_game_focus``) и отобрали бы его у браузера.
            """
            for w in self.cards:
                w.hide()
            self.summary.hide()
            self.timer.stop()

        def hide_all(self):
            self.hide_overlays()
            if login_in_progress():
                # окна приложения уже спрятаны режимом входа — не трогаем фокус
                return
            # B2: скрытие по таймеру/закрытие экрана наград закрывает и все
            # открытые диалоги приложения (подтверждение, формы, вход)
            close_app_dialogs()
            self.hide_selected()

    return _ItemOverlay, _SummaryOverlay, _Manager, Qt


# ─────────────────────────────────────────────────────────────────────────
#  Панель игровых событий (Alt+E): разломы, сортис, вторжения, циклы, Баро
#  Данные — из events_api.py (api.warframestat.us), только чтение.
# ─────────────────────────────────────────────────────────────────────────
def build_events_panel():
    """Фабрика панели событий в стиле AlecaFrame — Qt импортируется лениво.

    Оформление: карточки-строки с зеброй и подсветкой наведения, бейджи эры
    (Lith/Meso/Neo/Axi/Requiem), миссии, «СП» и «Шторма», полоса прогресса
    вторжений, таймеры-пилюли и тонкие разделители секций. Таймеры обновляются
    раз в секунду без пересоздания виджетов; при новых данных карточки
    перестраиваются целиком (один проход, без мигания).
    """
    from PyQt5.QtCore import Qt, QTimer
    from PyQt5.QtGui import QFont, QFontMetrics
    from PyQt5.QtWidgets import (QFrame, QHBoxLayout, QLabel, QProgressBar,
                                 QPushButton, QScrollArea, QSizePolicy,
                                 QVBoxLayout, QWidget)
    theme = ui_theme()

    #: Цвета бейджей эры разлома (как в AlecaFrame); текст — белый.
    tier_colors = {
        "Lith": "#b08d57", "Meso": "#8fb8d6", "Neo": "#d9a441",
        "Axi": "#a468c9", "Requiem": "#c0392b",
    }
    #: «СП» (стальной путь) и «Шторм» (шторм Бездны) — свои цвета.
    flag_colors = {"steel": "#d97a45", "storm": "#6f7fe8"}
    #: Цвет фракции: бейдж вторжения/сортиса и полоса прогресса.
    faction_colors = {
        "Grineer": "#b85450", "Corpus": "#4f87b8", "Infested": "#6f9b4a",
        "Orokin": "#b08d57", "Corrupted": "#8a6f3b", "Narmer": "#a05a8c",
        "The Murmur": "#8a6fb0", "Sentient": "#7a8fa8",
    }
    glyph_colors = {"☀": theme.PALETTE["warning"], "☾": theme.PALETTE["accent"],
                    "❄": theme.PALETTE["accent"], "◐": "#a468c9",
                    "◑": theme.PALETTE["warning"]}
    timer_colors = {"ok": theme.PALETTE["success"],
                    "warn": theme.PALETTE["warning"],
                    "hot": theme.PALETTE["danger"]}
    neutral_badge = theme.PALETTE["border"]
    # фон окна #0d131c (alpha 0.96), рамка #24344a, радиус 8
    panel_style = theme.window_qss("events", 8)

    class _EventsOverlay(QWidget):
        """Панель игровых событий (Alt+E) в стиле AlecaFrame."""

        def __init__(self, cfg, fmt=None, status_fn=None, on_refresh=None,
                     embedded=False):
            super().__init__()
            self.cfg = cfg
            self.embedded = bool(embedded)
            self._fmt = fmt or (lambda seconds: "")
            self._status_fn = status_fn
            self._on_refresh = on_refresh
            self._timers = []        # [(QLabel, row_dict)] — только таймеры
            self._status = ""
            self._sections_data = []
            self._tier_filter = ""
            self._saved_scroll = 0
            self._content_width = 492

            base_font = QFont("Segoe UI")
            base_font.setPixelSize(11)
            self._f_header = QFont(base_font)
            self._f_header.setPixelSize(13)
            self._f_header.setBold(True)
            self._f_section = QFont(base_font)
            self._f_section.setBold(True)
            self._f_text = QFont(base_font)
            self._f_detail = QFont(base_font)
            self._f_detail.setPixelSize(10)
            self._f_badge = QFont(base_font)
            self._f_badge.setPixelSize(9)
            self._f_badge.setBold(True)
            self._f_timer = QFont(base_font)
            self._f_glyph = QFont(base_font)
            self._f_glyph.setPixelSize(13)

            if not self.embedded:
                self.setWindowFlags(Qt.WindowStaysOnTopHint |
                                    Qt.FramelessWindowHint | Qt.Tool)
                self.setAttribute(Qt.WA_TranslucentBackground)
            outer = QVBoxLayout(self)
            outer.setContentsMargins(0, 0, 0, 0)
            self.frame = QFrame()
            self.frame.setObjectName("events")
            if self.embedded:
                self.frame.setStyleSheet(
                    "QFrame#events { background: transparent; border: none; }"
                    "QLabel { background: transparent; }")
            else:
                self.frame.setStyleSheet(panel_style)
            lay = QVBoxLayout(self.frame)
            lay.setContentsMargins(0 if self.embedded else 12,
                                   0 if self.embedded else 8,
                                   0 if self.embedded else 12,
                                   0 if self.embedded else 10)
            lay.setSpacing(6)

            head = QHBoxLayout()
            head.setContentsMargins(0, 0, 0, 0)
            head.setSpacing(8)
            self.lbl_title = QLabel("Игровые события")
            self.lbl_title.setObjectName("evTitle")
            self.lbl_title.setFont(self._f_header)
            self.lbl_title.setStyleSheet(
                f"color:{theme.PALETTE['text']}; font-size:13px; font-weight:700;")
            self.lbl_status = QLabel("…")
            self.lbl_status.setObjectName("evStatus")
            self.lbl_status.setFont(self._f_detail)
            self.lbl_status.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.lbl_status.setStyleSheet(theme.status_qss())
            self.btn_refresh = QPushButton("⟳")
            self.btn_refresh.setObjectName("evRefresh")
            self.btn_refresh.setFont(QFont("Segoe UI Symbol", 13))
            self.btn_refresh.setStyleSheet(theme.button_qss(small=True))
            self.btn_refresh.setCursor(Qt.PointingHandCursor)
            self.btn_refresh.setFixedSize(22, 20)
            self.btn_refresh.setToolTip("Обновить события сейчас")
            self.btn_refresh.clicked.connect(self._request_refresh)
            # t11: заметная кнопка «Повторить» при ошибке/устаревшем кэше
            self.btn_retry = QPushButton("Повторить")
            self.btn_retry.setObjectName("evRetry")
            self.btn_retry.setFont(self._f_detail)
            self.btn_retry.setStyleSheet(theme.button_qss(small=True))
            self.btn_retry.setCursor(Qt.PointingHandCursor)
            self.btn_retry.setToolTip("Повторить загрузку событий")
            self.btn_retry.clicked.connect(self._request_refresh)
            self.btn_retry.setVisible(False)
            head.addWidget(self.lbl_title)
            head.addStretch(1)
            head.addWidget(self.lbl_status)
            head.addWidget(self.btn_retry)
            head.addWidget(self.btn_refresh)

            self.scroll = QScrollArea()
            self.scroll.setWidgetResizable(True)
            self.scroll.setFrameShape(QFrame.NoFrame)
            self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.body = QWidget()
            self.body.setStyleSheet("background: transparent;")
            self.rows_box = QVBoxLayout(self.body)
            self.rows_box.setContentsMargins(0, 0, 2, 0)
            self.rows_box.setSpacing(2)
            self.scroll.setWidget(self.body)
            self.scroll.verticalScrollBar().valueChanged.connect(
                self._remember_scroll)

            if self.embedded:
                for widget in (self.lbl_title, self.lbl_status, self.btn_refresh):
                    widget.setVisible(False)
            else:
                lay.addLayout(head)
            lay.addWidget(self.scroll, 1)
            outer.addWidget(self.frame)
            if not self.embedded:
                self._fit_size()
            else:
                self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # ── размер и место: 520×680 в правом верхнем углу ──────────────
        def _fit_size(self, sx=1.0, sy=1.0):
            if self.embedded:
                return
            width = max(360, int(520 * float(sx)))
            height = max(300, int(680 * float(sy)))
            self.setFixedSize(width, height)
            self._content_width = width - 28   # поля 12+12 и скроллбар

        def resizeEvent(self, event):
            super().resizeEvent(event)
            if not self.embedded:
                return
            width = max(220, self.width() - 24)
            if abs(width - self._content_width) > 2:
                self._content_width = width
                if self._sections_data:
                    QTimer.singleShot(80, self._rebuild)

        def status_text(self):
            """Статус для шапки единого окна."""
            return self._status or "—"

        def focus_tier(self, tier):
            """Клик по «фиссуре» из центра уведомлений: фильтр по эре."""
            tier = str(tier or "").strip()
            self._tier_filter = tier
            if tier:
                self._status = f"фильтр: {tier}"
            self._rebuild()
            try:
                self.scroll.verticalScrollBar().setValue(0)
            except Exception:
                pass
            return tier

        def clear_tier_filter(self):
            self._tier_filter = ""
            self._rebuild()
            return True

        def refresh_active(self):
            self._request_refresh()

        def place(self, geom):
            """Правый верхний угол с учётом масштаба монитора."""
            if self.embedded:
                return
            try:
                sx = float(geom.get("sx") or 1.0)
                sy = float(geom.get("sy") or 1.0)
            except Exception:
                sx = sy = 1.0
            width = max(360, int(520 * sx))
            height = max(300, int(680 * sy))
            changed = (width != self.width() or height != self.height())
            self.setFixedSize(width, height)
            self._content_width = width - 28
            screen_w = int(geom.get("width") or width)
            screen_h = int(geom.get("height") or height)
            x = screen_w - width - int(24 * sx)
            y = int(24 * sy)
            x = max(0, min(x, max(0, screen_w - width)))
            y = max(0, min(y, max(0, screen_h - height)))
            self.move(x, y)
            if changed and self._sections_data:
                self._rebuild()

        # ── скролл между открытиями ────────────────────────────────────
        def _remember_scroll(self, value):
            self._saved_scroll = int(value)

        def _restore_scroll(self):
            bar = self.scroll.verticalScrollBar()
            try:
                bar.setValue(min(self._saved_scroll, bar.maximum()))
            except Exception:
                pass

        def showEvent(self, event):
            super().showEvent(event)
            QTimer.singleShot(0, self._restore_scroll)

        def hideEvent(self, event):
            try:
                self._saved_scroll = self.scroll.verticalScrollBar().value()
            except Exception:
                pass
            super().hideEvent(event)

        # ── статус и обновление ────────────────────────────────────────
        def set_status(self, text, stale=False):
            self._status = str(text or "")
            self.lbl_status.setStyleSheet(theme.status_qss(stale))
            self.lbl_status.setText(self._status)
            low = self._status.lower()
            show_retry = bool(stale) or any(
                token in low for token in ("нет сети", "нет данных", "сервер",
                                           "ошиб", "не удалось", "кэш"))
            self.btn_retry.setVisible(show_retry)

        def _request_refresh(self):
            """Кнопка «⟳»/«Повторить»: просит фон обновить снимок."""
            if self._on_refresh is None:
                self.set_status("обновление недоступно", True)
                return
            self.btn_retry.setVisible(False)
            self.set_status("обновляю…", False)
            try:
                self._on_refresh()
            except Exception:
                self.set_status("не удалось обновить", True)

        # ── содержимое ─────────────────────────────────────────────────
        def _clear_rows(self):
            while self.rows_box.count():
                item = self.rows_box.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.setParent(None)
                    widget.deleteLater()
            self._timers = []

        def set_content(self, sections, status=None, stale=False):
            """Новые данные: один проход перестройки карточек (без мигания)."""
            self._sections_data = [(str(name), list(rows or []))
                                   for name, rows in (sections or [])]
            if status is not None:
                self.set_status(status, stale)
            self._rebuild()
            self.refresh_timers()

        def _rebuild(self):
            saved = self._saved_scroll
            self.scroll.setUpdatesEnabled(False)
            self._clear_rows()
            index = 0
            shown = 0
            for name, rows in self._sections_data:
                if not rows:
                    continue
                self.rows_box.addWidget(self._make_section_header(name))
                for row in rows:
                    if self._tier_filter and isinstance(row, dict) \
                            and row.get("tier") \
                            and str(row.get("tier")).lower() != \
                            self._tier_filter.lower():
                        continue
                    self.rows_box.addWidget(self._make_row(row, zebra=index % 2 == 1))
                    index += 1
                    shown += 1
            if not shown:
                empty = QLabel("Сейчас нет активных событий"
                               if "нет данных" not in self._status
                               else "Нет данных: проверьте сеть или кэш")
                empty.setObjectName("evEmpty")
                empty.setFont(self._f_detail)
                empty.setStyleSheet(theme.muted_qss(10) + " padding-top:8px;")
                self.rows_box.addWidget(empty)
            self.rows_box.addStretch(1)
            self.scroll.setUpdatesEnabled(True)
            self._saved_scroll = saved
            QTimer.singleShot(0, self._restore_scroll)

        def _make_section_header(self, name):
            box = QWidget()
            lay = QVBoxLayout(box)
            lay.setContentsMargins(0, 8, 0, 2)
            lay.setSpacing(3)
            label = QLabel(str(name).upper())
            label.setObjectName("evSection")
            section_font = QFont(self._f_section)
            section_font.setLetterSpacing(QFont.AbsoluteSpacing, 1.0)
            label.setFont(section_font)
            label.setStyleSheet(theme.section_qss())
            line = QFrame()
            line.setObjectName("evSectionLine")
            line.setFixedHeight(1)
            line.setStyleSheet(theme.divider_qss())
            lay.addWidget(label)
            lay.addWidget(line)
            return box

        # ── бейджи ─────────────────────────────────────────────────────
        def _badges_for(self, row):
            """[(текст, фон, цвет текста)] по данным строки."""
            result = []
            for badge in row.get("badges") or []:
                if not isinstance(badge, dict):
                    continue
                text = str(badge.get("text") or "").strip()
                if not text:
                    continue
                kind = str(badge.get("kind") or "mission")
                if kind == "tier":
                    bg = tier_colors.get(str(row.get("tier") or ""), "#5a6b7d")
                    fg = "#ffffff"
                elif kind == "steel":
                    bg, fg = flag_colors["steel"], "#ffffff"
                elif kind == "storm":
                    bg, fg = flag_colors["storm"], "#ffffff"
                elif kind == "faction":
                    bg = faction_colors.get(str(row.get("faction") or ""),
                                            "#455a70")
                    fg = "#ffffff"
                else:
                    bg, fg = neutral_badge, theme.PALETTE["text"]
                result.append((text, bg, fg))
            return result

        @staticmethod
        def _elide(text, font, width):
            if not text or width <= 0:
                return ""
            return QFontMetrics(font).elidedText(str(text), Qt.ElideRight,
                                                 int(width))

        # ── таймеры ────────────────────────────────────────────────────
        def _timer_parts(self, row, now):
            """Текст и цвет пилюли: формат «14ч 22м», «59м 12с», «<1м»."""
            ends_at = row.get("ends_at")
            if not isinstance(ends_at, (int, float)):
                return "", "#93a1b5"
            left = float(ends_at) - float(now)
            if left <= 0:
                return "истекло", "#5f6b7d"
            text = self._fmt(left) or ""
            if (row.get("ends_prefix") or "до") == "через":
                text = f"через {text}"
            glyph = str(row.get("glyph") or "")
            state = str(row.get("state") or "")
            if glyph and state:
                text = f"{glyph} {state} {text}".strip()
                return text, glyph_colors.get(glyph, "#93a1b5")
            state_key = "ok" if left > 3600 else ("warn" if left >= 600 else "hot")
            return text, timer_colors[state_key]

        def _pill_style(self, color):
            return theme.pill_qss(color)

        # ── карточка строки ────────────────────────────────────────────
        def _make_row(self, row, zebra=False):
            row = dict(row or {})
            card = QFrame()
            card.setObjectName("evRow")
            card.setFixedHeight(24)
            card.setProperty("kind", str(row.get("kind") or ""))
            card.setProperty("zebra", bool(zebra))
            card.setStyleSheet(theme.row_qss(zebra=zebra, name="evRow"))
            lay = QHBoxLayout(card)
            lay.setContentsMargins(8, 0, 8, 0)
            lay.setSpacing(6)

            now = time.time()
            glyph = str(row.get("glyph") or "")
            badges = self._badges_for(row)
            progress = row.get("progress")
            has_progress = isinstance(progress, (int, float))
            timer_text, timer_color = self._timer_parts(row, now)

            badge_widths = [QFontMetrics(self._f_badge).horizontalAdvance(text) + 14
                            for text, _bg, _fg in badges]
            glyph_width = (QFontMetrics(self._f_glyph).horizontalAdvance(glyph) + 2
                           if glyph else 0)
            progress_width = 0
            if has_progress:
                percent_text = f"{int(round(float(progress)))}%"
                progress_width = 64 + 6 + QFontMetrics(
                    self._f_detail).horizontalAdvance(percent_text)
            timer_width = (QFontMetrics(self._f_timer).horizontalAdvance(timer_text)
                           + 16)
            widget_count = ((1 if glyph else 0) + len(badges) +
                            (1 if has_progress else 0) + 1)
            fixed = (16 + 6 * max(0, widget_count - 1) + glyph_width +
                     sum(badge_widths) + progress_width + timer_width)
            avail = max(60, self._content_width - fixed)

            subtext = str(row.get("subtext") or "")
            if row.get("kind") == "invasion" and row.get("rewards"):
                # (5) награды списком: «◈ 3 × Fieldron · ◈ 10 000 кр»
                subtext = " · ".join(f"◈ {item}" for item in row["rewards"][:2])
            title_budget = int(avail * 0.58) if subtext else avail
            detail_budget = max(0, avail - title_budget - 6)
            headline = self._elide(str(row.get("headline") or
                                       row.get("title") or ""),
                                   self._f_text, title_budget)

            if glyph:
                glyph_label = QLabel(glyph)
                glyph_label.setObjectName("evGlyph")
                glyph_label.setFont(self._f_glyph)
                glyph_label.setStyleSheet(
                    f"color:{glyph_colors.get(glyph, theme.PALETTE['muted'])};")
                lay.addWidget(glyph_label)

            for text, bg, fg in badges:
                badge_label = QLabel(text)
                badge_label.setObjectName("evBadge")
                badge_label.setFont(self._f_badge)
                badge_label.setFixedHeight(16)
                badge_label.setStyleSheet(theme.badge_qss(bg, fg))
                lay.addWidget(badge_label)

            if headline:
                title_label = QLabel(headline)
                title_label.setObjectName("evHeadline")
                title_label.setFont(self._f_text)
                title_label.setStyleSheet(f"color:{theme.PALETTE['text']};")
                lay.addWidget(title_label)

            if subtext and detail_budget >= 30:
                detail_label = QLabel(self._elide(subtext, self._f_detail,
                                                  detail_budget))
                detail_label.setObjectName("evSubtext")
                detail_label.setFont(self._f_detail)
                detail_label.setStyleSheet(theme.muted_qss(10))
                lay.addWidget(detail_label)

            if has_progress:
                bar = QProgressBar()
                bar.setObjectName("evProgress")
                bar.setRange(0, 100)
                bar.setValue(int(round(float(progress))))
                bar.setTextVisible(False)
                bar.setFixedSize(64, 6)
                color = faction_colors.get(str(row.get("faction") or ""),
                                           theme.PALETTE["accent"])
                bar.setStyleSheet(theme.progress_qss(color))
                percent_label = QLabel(f"{int(round(float(progress)))}%")
                percent_label.setObjectName("evPercent")
                percent_label.setFont(self._f_detail)
                percent_label.setStyleSheet(theme.muted_qss(10))
                lay.addWidget(bar)
                lay.addWidget(percent_label)

            lay.addStretch(1)

            if timer_text:
                timer = QLabel(timer_text)
                timer.setObjectName("evTimer")
                timer.setFont(self._f_timer)
                timer.setFixedHeight(18)
                timer.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                timer.setStyleSheet(self._pill_style(timer_color))
                lay.addWidget(timer)
                self._timers.append((timer, row))

            full = str(row.get("title") or headline or "")
            if row.get("detail"):
                full += "\n" + str(row.get("detail"))
            if full.strip():
                card.setToolTip(full)
            return card

        # ── тик раз в секунду ──────────────────────────────────────────
        def refresh_timers(self, now=None):
            """Обновляет только пилюли и подпись свежести (виджеты не трогаем)."""
            now = time.time() if now is None else now
            for label, row in self._timers:
                text, color = self._timer_parts(row, now)
                label.setText(text)
                label.setStyleSheet(self._pill_style(color))
            if self._status_fn is not None:
                try:
                    text = str(self._status_fn())
                except Exception:
                    text = ""
                if text:
                    self.set_status(
                        text, "устарел" in text or "нет данных" in text)

        # ── помощники для тестов/отладки ───────────────────────────────
        def visible_sections(self):
            """Названия непустых секций в порядке показа."""
            return [name for name, rows in self._sections_data if rows]

        def row_widgets(self):
            """Все карточки строк (objectName == evRow)."""
            found = []
            for i in range(self.rows_box.count()):
                widget = self.rows_box.itemAt(i).widget()
                if widget is not None and widget.objectName() == "evRow":
                    found.append(widget)
            return found

    return _EventsOverlay


class EventsPanelController:
    """Связка «клиент событий ↔ панель»: сеть в фоне, виджеты — в GUI-потоке.

    Все методы (``start``/``stop``/``refresh``/``tick``/``show``/``hide``/
    ``toggle``) вызываются из GUI-потока: Qt требует этого. Сеть живёт в
    отдельном потоке ``EventsPoller``; результат приходит в GUI через сигнал
    ``notify`` (в warframe.py — ``_UiBridge.events_updated``), поэтому список
    событий никогда не блокирует интерфейс.
    """

    def __init__(self, cfg, client, panel, geom, notify=None):
        self.cfg = cfg
        self.client = client
        self.panel = panel
        self.geom = geom
        self.notify = notify or (lambda: None)
        self.poller = None
        self._tick = None
        self._started = False
        self._network_note = False   # чтобы не повторять одно сообщение
        try:
            # кнопка «⟳» на панели → принудительное обновление в фоне
            self.panel._on_refresh = self.refresh_now
        except Exception:
            pass

    # ── запуск / остановка ─────────────────────────────────────────────
    def start(self):
        """Первый показ: таймер тика + (если включено) фоновый опрос."""
        if not self._started:
            from PyQt5.QtCore import QTimer
            self._started = True
            self._tick = QTimer()
            self._tick.setInterval(1000)
            self._tick.timeout.connect(self.tick)
            self._tick.start()
        if self.poller is not None:
            return
        if not self.cfg.get("events_enabled", True):
            if not self._network_note:
                self._network_note = True
                log("События: опрос выключен (events_enabled=false) — показываю кэш.")
            return
        mod = events_api_module()
        if mod is None:
            if not self._network_note:
                self._network_note = True
                log("События: модуль events_api.py не найден — панель только с кэшем.")
            return
        try:
            self.poller = mod.EventsPoller(
                self.client,
                poll_ms=self.cfg.get("events_poll_ms", 60000),
                on_update=self.notify)
            self.poller.start()
        except Exception as e:
            log(f"События: фоновый опрос не запущен ({type(e).__name__}: {e})")
            self.poller = None

    def stop(self):
        if self.poller is not None:
            try:
                self.poller.stop()
            except Exception:
                pass
            self.poller = None
        if self._tick is not None:
            try:
                self._tick.stop()
            except Exception:
                pass
            self._tick = None

    # ── отрисовка (GUI-поток) ──────────────────────────────────────────
    def refresh(self):
        """Перестраивает список из кэша клиента (сеть уже отработала в фоне)."""
        try:
            self.panel.set_content(self.client.sections(), self.client.status_text(),
                                   self.client.is_stale())
        except Exception as e:
            log(f"События: панель не обновлена ({type(e).__name__}: {e})")

    def refresh_now(self):
        """Кнопка «⟳»: принудительное обновление без блокировки GUI.

        Если фоновый опрос уже работает — будим его (запрос уйдёт из его
        потока); иначе запускаем разовый поток. Виджеты не трогаем: результат
        придёт в GUI через ``notify`` (сигнал ``events_updated``).
        """
        if self.poller is not None:
            try:
                self.poller.poll_now()
                return
            except Exception:
                pass
        if not self.cfg.get("events_enabled", True):
            if not self._network_note:
                self._network_note = True
                log("События: опрос выключен (events_enabled=false) — показываю кэш.")
            self.refresh()
            return
        threading.Thread(target=self._refresh_worker, daemon=True,
                         name="wf-events-refresh").start()

    def _refresh_worker(self):
        try:
            self.client.fetch(force=True)
        except Exception as e:
            log(f"События: принудительное обновление не удалось "
                f"({type(e).__name__}: {e})")
        try:
            self.notify()
        except Exception:
            pass

    def tick(self):
        """Раз в секунду: только таймеры и подпись свежести."""
        try:
            self.panel.refresh_timers()
        except Exception:
            pass

    def is_visible(self):
        try:
            return bool(self.panel.isVisible())
        except Exception:
            return False

    # ── показ/скрытие по Alt+E ─────────────────────────────────────────
    def show(self):
        self.start()
        if self.poller is not None:
            # не ждём целый период опроса: просим свежие данные сразу
            try:
                self.poller.poll_now()
            except Exception:
                pass
        self.refresh()
        try:
            self.panel.place(self.geom)
        except Exception:
            pass
        if self.cfg.get("focus_editor_on_f8", True):
            try:
                if not focus_editor(self.panel):
                    self.panel.show()
                    self.panel.raise_()
            except Exception:
                self.panel.show()
                self.panel.raise_()
        else:
            self.panel.show()
            self.panel.raise_()

    def hide(self):
        try:
            self.panel.hide()
        except Exception:
            pass
        if self.cfg.get("restore_game_focus", True):
            restore_game_focus()

    def toggle(self):
        if self.is_visible():
            self.hide()
        else:
            self.show()


def _hotkey_canon(value):
    try:
        mod = settings_ui_module()
        if mod is not None:
            return mod.canon_hotkey(value)
    except Exception:
        pass
    return str(value or "").strip().lower()


def _hotkey_variants(value):
    try:
        mod = settings_ui_module()
        if mod is not None:
            return mod.hotkey_variants(value)
    except Exception:
        pass
    key = _hotkey_canon(value)
    return [key] if key else []


def _keyboard_variants(value, keyboard_module=None):
    """Имена для ``keyboard``: только поддерживаемые (без кириллицы)."""
    try:
        mod = settings_ui_module()
        if mod is not None and hasattr(mod, "keyboard_hotkey_variants"):
            return mod.keyboard_hotkey_variants(value, keyboard_module)
    except Exception:
        pass
    key = _hotkey_canon(value)
    if not key:
        return []
    base = key.split("+")[-1]
    if base.isascii() and (base.isalnum() or base in ";.,/-='`"):
        return [key]
    return []


#: уже залогированные пропуски, чтобы не писать одно и то же на каждый старт
_HOTKEY_SKIP_LOGGED = set()


def _log_hotkey_skip(key, canonical=""):
    """ОДНА понятная строка на неподдерживаемый кириллический вариант."""
    text = str(key or "").strip()
    if not text or text in _HOTKEY_SKIP_LOGGED:
        return False
    _HOTKEY_SKIP_LOGGED.add(text)
    suffix = f" (физическая клавиша {canonical})" if canonical else ""
    log(f"Хоткей {text} не поддерживается — использую скан-код{suffix}")
    return True


def _scan_codes_for(keyboard_module, key_name):
    """Скан-коды физической клавиши (``keyboard.key_to_scan_codes`` + OEM-карта)."""
    fn = getattr(keyboard_module, "key_to_scan_codes", None)
    if callable(fn):
        try:
            codes = tuple(int(code) for code in fn(key_name))
            if codes:
                return codes
        except Exception:
            pass
    # keyboard не знает некоторые OEM-клавиши по имени ([ ] ' \ и т.п.),
    # но их скан-коды (set 1) стабильны на Windows — нужны для RU-раскладки
    return tuple(_OEM_SCAN_CODES.get(str(key_name or "").lower(), ()))


#: OEM-клавиши US-раскладки → скан-коды set 1 (для fallback по hook)
_OEM_SCAN_CODES = {
    "[": (26,), "]": (27,), ";": (39,), "'": (40,), ",": (51,),
    ".": (52,), "/": (53,), "\\": (43,), "-": (12,), "=": (13,),
    "`": (41,),
}


def _mod_pressed(keyboard_module, name):
    fn = getattr(keyboard_module, "is_pressed", None)
    if not callable(fn):
        return False
    try:
        return bool(fn(name))
    except Exception:
        return False


#: сколько помнить последнее срабатывание (антидребезг повторов клавиши), сек
_HOTKEY_REPEAT_SEC = 0.25


def install_scan_code_hotkeys(keyboard_module, bindings, logger=None,
                              clock=None, throttle=None):
    """Резервная регистрация хоткеев по скан-кодам (RU-раскладка).

    ``bindings`` — ``[(combo, action), ...]``; ``keyboard.hook`` получает
    события клавиатуры, мы сравниваем ``scan_code`` физической клавиши и
    зажатые модификаторы. Нужна, когда ``keyboard.add_hotkey`` не понимает
    имя клавиши (например, кириллицу или необычную пунктуацию).

    Возвращает число привязок; при отсутствии ``hook`` — 0.
    """
    import time as _time
    hook = getattr(keyboard_module, "hook", None)
    if not callable(hook):
        return 0
    clock = clock or _time.monotonic
    throttle = _HOTKEY_REPEAT_SEC if throttle is None else float(throttle)
    targets = {}
    for combo, action in bindings or []:
        if not callable(action):
            continue
        canon = _hotkey_canon(combo)
        if not canon:
            continue
        parts = canon.split("+")
        mods = frozenset(part for part in parts[:-1]
                         if part in ("ctrl", "alt", "shift", "win"))
        base = parts[-1]
        for code in _scan_codes_for(keyboard_module, base):
            targets.setdefault((mods, int(code)), action)
    if not targets:
        return 0
    state = {"last": {}}

    def on_event(event):
        try:
            if str(getattr(event, "event_type", "down")) not in ("down", ""):
                return
            code = int(getattr(event, "scan_code", -1))
            pressed = frozenset(
                name for name in ("ctrl", "alt", "shift", "win")
                if _mod_pressed(keyboard_module, name))
            action = targets.get((pressed, code))
            if action is None:
                return
            now = clock()
            key = (pressed, code)
            last = state["last"].get(key, -1e9)
            if now - last < throttle:
                return
            state["last"][key] = now
            action()
        except Exception as exc:
            log(f"Хоткей по скан-коду: действие не выполнено "
                f"({type(exc).__name__})")

    try:
        hook(on_event)
    except Exception as exc:
        log(f"Хоткей по скан-коду: hook не установлен "
            f"({type(exc).__name__})")
        return 0
    if callable(logger):
        try:
            logger(f"Хоткеи: {len(targets)} привязок через скан-коды")
        except Exception:
            pass
    return len(targets)


def register_hotkey(keyboard_module, key, action, suppress=True):
    """Зарегистрировать клавишу (RU-раскладка — по физической клавише).

    1.0.0: кириллические имена в ``keyboard.add_hotkey`` не передаём —
    библиотека не умеет с ними работать (``ValueError: Key 'й' is not
    mapped``). Канонизация уже переводит ``alt+й`` в ``alt+q`` (физическая
    клавиша), поэтому хоткей работает при любой раскладке; если backend не
    понимает и латинское имя — регистрируем через ``keyboard.hook`` и
    скан-коды. Любая ошибка — одной строкой в app.log, без repr.
    """
    canonical = _hotkey_canon(key)
    if not canonical or action is None:
        return False
    raw = str(key or "").strip()
    ru_variants = [variant for variant in _hotkey_variants(canonical)
                   if variant != canonical]
    logged = False
    if ru_variants:
        logged = _log_hotkey_skip(raw or canonical, canonical)
    ok = False
    for variant in _keyboard_variants(canonical, keyboard_module):
        try:
            try:
                keyboard_module.add_hotkey(variant, action, suppress=suppress)
            except TypeError:
                # старые/упрощённые заглушки keyboard без suppress
                keyboard_module.add_hotkey(variant, action)
            ok = True
        except Exception:
            # 1.0.0: без repr/многострочных трейсов — попробуем скан-код
            ok = False
    if ok:
        return True
    installed = install_scan_code_hotkeys(keyboard_module,
                                          [(canonical, action)])
    if installed:
        if not logged:
            _log_hotkey_skip(raw or canonical, canonical)
        return True
    if not logged:
        log(f"Хоткей {raw or canonical} не зарегистрирован — "
            "клавиатура недоступна (см. app.log)")
    return False


def add_hotkey_events(cfg, keyboard_module, action):
    """Регистрирует новый хоткей панели событий (``hotkey_events``, Alt+E).

    Регистрируются оба варианта раскладки; при ошибке одного второй остаётся.
    Возвращает ``True``, если зарегистрирован хотя бы один вариант.
    """
    return register_hotkey(keyboard_module,
                           cfg.get("hotkey_events", "alt+e"), action)


def add_hotkey_market(cfg, keyboard_module, action):
    """Регистрирует хоткей окна «Маркет» (``hotkey_market``, Alt+M)."""
    return register_hotkey(keyboard_module,
                           cfg.get("hotkey_market", "alt+m"), action)


def add_hotkey_window(cfg, keyboard_module, action):
    """Регистрирует главный хоткей единого окна (``hotkey_window``, F8)."""
    key = cfg.get("hotkey_window") or cfg.get("hotkey_editor", "f8")
    return register_hotkey(keyboard_module, key, action)


#: Alt+1…Alt+4 — «взята награда №N из последнего скана» (по умолчанию).
PICK_HOTKEYS_DEFAULT = ["alt+1", "alt+2", "alt+3", "alt+4"]

#: Ключи конфига с «занятыми» хоткеями: их Alt+1..4 не перетирают.
_OTHER_HOTKEY_KEYS = ("hotkey_scan", "hotkey_editor", "hotkey_events",
                      "hotkey_market", "hotkey_window", "hotkey_hide")


def pick_hotkeys(cfg):
    """Валидные пары ``(клавиша, индекс 0..3)`` для Alt+1…Alt+4.

    ``pick_hotkeys`` из config.json — список (или строка через запятую).
    Клавиши, занятые другими действиями (Alt+Q/F8/F9/Alt+E/Alt+M) и
    дубликаты, пропускаются — существующие бинды не перетираем.
    """
    cfg = cfg or {}
    if cfg.get("pick_enabled", True) is False:
        return []
    raw = cfg.get("pick_hotkeys", PICK_HOTKEYS_DEFAULT)
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(raw, (list, tuple)):
        raw = list(PICK_HOTKEYS_DEFAULT)
    busy = set()
    for key in _OTHER_HOTKEY_KEYS:
        value = _hotkey_canon(cfg.get(key) or "")
        if value:
            busy.add(value)
    out = []
    for index, key in enumerate(list(raw)[:4]):
        name = _hotkey_canon(key)
        if not name or name in busy:
            continue
        if any(name == existing for existing, _idx in out):
            continue
        out.append((name, index))
    return out


def add_hotkeys_pick(cfg, keyboard_module, emit):
    """Регистрирует Alt+1…4 без подавления клавиш (``suppress=False``).

    ``emit(index)`` вызывается из потока keyboard; ``run_gui`` отправляет его
    в GUI через сигнал. Возвращает список зарегистрированных пар.
    """
    pairs = pick_hotkeys(cfg)
    registered = []
    for key, index in pairs:
        if register_hotkey(
                keyboard_module, key,
                lambda i=index: emit(i), suppress=False):
            registered.append((key, index))
    return registered


# ─────────────────────────────────────────────────────────────────────────
#  Диалоги warframe.market: форма sell-ордера и окно токена
#  Qt импортируется лениво; сеть — только в фоновых потоках диалогов.
# ─────────────────────────────────────────────────────────────────────────
def build_market_dialogs():
    """Фабрика диалогов: ``(форма продажи, окно токена)``."""
    from PyQt5.QtCore import Qt, pyqtSignal
    from PyQt5.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog,
                                 QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                                 QMessageBox, QPushButton, QScrollArea,
                                 QSpinBox, QToolButton, QVBoxLayout)
    theme = ui_theme()
    dialog_style = theme.dialog_qss()

    def _verify_token_http(token, http_get=None):
        """GET /v2/me с Bearer для кнопки «Проверить токен» (без лога токена)."""
        token = str(token or "").strip()
        if not token:
            return {"ok": False, "state": "empty", "code": 0,
                    "message": "токен не введён"}
        headers = {"Authorization": f"Bearer {token}",
                   "Accept": "application/json",
                   "User-Agent": "VoidFrameRU/0.6.1"}
        url = "https://api.warframe.market/v2/me"
        if not callable(http_get):
            import urllib.error
            import urllib.request
            def http_get(address, request_headers, timeout=10):
                request = urllib.request.Request(address,
                                                 headers=request_headers)
                try:
                    with urllib.request.urlopen(request,
                                                timeout=timeout) as resp:
                        body = resp.read().decode("utf-8", "replace")
                        return resp.getcode(), body
                except urllib.error.HTTPError as exc:
                    return exc.code, ""
        try:
            try:
                raw = http_get(url, headers, 10)
            except TypeError:
                raw = http_get(url, headers)
        except Exception as exc:
            return {"ok": False, "state": "no_network", "code": 0,
                    "message": f"нет сети ({type(exc).__name__})"}
        code, body = (raw if isinstance(raw, tuple)
                      else (raw, ""))
        try:
            code = int(code)
        except Exception:
            code = 0
        if code == 200:
            nickname = ""
            try:
                data = json.loads(body) if body else {}
                payload = data.get("data") if isinstance(data, dict) else {}
                payload = payload if isinstance(payload, dict) else data
                nickname = str((payload or {}).get("ingameName")
                               or (payload or {}).get("slug")
                               or (payload or {}).get("name") or "")
            except Exception:
                nickname = ""
            return {"ok": True, "state": "ok", "code": 200,
                    "message": "токен принят", "nickname": nickname}
        if code == 401:
            return {"ok": False, "state": "unauthorized", "code": 401,
                    "message": "401 — гостевой или просрочен"}
        return {"ok": False, "state": "error", "code": code,
                "message": f"HTTP {code}"}

    class _TokenDialog(QDialog):
        """Ручной ввод JWT + общая инструкция для Chromium/Firefox."""

        INSTRUCTION = (
            "Как получить токен warframe.market\n\n"
            "Chromium (Chrome, Edge, Яндекс, Opera, Opera GX):\n"
            "1. Войдите на https://warframe.market/ в этом браузере.\n"
            "2. F12 или Ctrl+Shift+I → вкладка Application (в Opera GX: "
            "Storage → Cookies).\n"
            "3. Cookies → https://warframe.market → строка JWT → скопируйте "
            "Value.\n\n"
            "Firefox:\n"
            "1. Войдите на https://warframe.market/ в Firefox.\n"
            "2. F12 → Хранилище/Storage → Cookies → "
            "https://warframe.market → JWT → скопируйте значение.\n\n"
            "Важно: cookie JWT помечена HttpOnly, поэтому document.cookie "
            "в консоли её НЕ покажет — только Application → Cookies.\n\n"
            "«Гостевой» токен (до входа на сайт) выглядит так же, но "
            "/v2/me отвечает 401 — сначала войдите в аккаунт.\n\n"
            "Старый способ: DevTools (F12) → Network → Headers → "
            "Authorization: Bearer eyJ…; вставьте значение после Bearer.\n"
            "Быстрее: нажмите «🔑» в шапке F8 — вход через браузер, токен "
            "заберётся автоматически. Пароль оверлей не спрашивает.\n"
            "Токен хранится только в config.json на этом компьютере."
        )
        CHECK_COMMAND = (
            "Invoke-RestMethod -Uri "
            "https://api.warframe.market/v2/me -Headers "
            "@{Authorization='Bearer <JWT>'}"
        )
        check_done = pyqtSignal(dict)

        def __init__(self, cfg, client, parent=None, save_fn=None,
                     verify_fn=None, open_url=None, clipboard=None,
                     http_get=None, logger=None):
            super().__init__(parent)
            register_app_dialog(self)     # B2: скрытие приложения закроет окно
            self.cfg = cfg
            self.client = client
            self.save_fn = save_fn or (lambda cfg, token: False)
            self.verify_fn = verify_fn
            self.open_url = open_url
            self.clipboard = clipboard
            self.http_get = http_get
            self.logger = logger
            self._checked_token = ""
            self._checking = False
            self.setWindowTitle("Токен warframe.market")
            self.setModal(True)
            self.setMinimumWidth(600)
            self.setMaximumHeight(560)
            self.setStyleSheet(dialog_style)
            layout = QVBoxLayout(self)
            layout.setContentsMargins(16, 14, 16, 12)
            layout.setSpacing(8)
            title = QLabel("<b>Как получить токен warframe.market</b>")
            title.setStyleSheet(
                f"font-size:{theme.size('window')}px; font-weight:700;"
                f" color:{theme.PALETTE['text']};")
            self.btn_instruction = QToolButton()
            self.btn_instruction.setText("Как получить токен ▸")
            self.btn_instruction.setCheckable(True)
            self.btn_instruction.setChecked(False)
            self.btn_instruction.setToolButtonStyle(Qt.ToolButtonTextOnly)
            self.btn_instruction.setStyleSheet(theme.button_qss(small=True))
            self.btn_instruction.setCursor(Qt.PointingHandCursor)
            self.lbl_info = QLabel(self.INSTRUCTION)
            self.lbl_info.setWordWrap(True)
            self.lbl_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.lbl_info.setStyleSheet(theme.muted_qss(11))
            self.instruction_scroll = QScrollArea()
            self.instruction_scroll.setWidgetResizable(True)
            self.instruction_scroll.setMaximumHeight(250)
            self.instruction_scroll.setWidget(self.lbl_info)
            self.instruction_scroll.setVisible(False)

            def toggle_instruction(checked):
                self.instruction_scroll.setVisible(bool(checked))
                self.btn_instruction.setText(
                    "Как получить токен ▾" if checked
                    else "Как получить токен ▸")
            self.btn_instruction.toggled.connect(toggle_instruction)
            self.lbl_status = QLabel("сейчас: " + _token_status(client))
            self.lbl_status.setStyleSheet(theme.status_qss())
            self.lbl_status.setWordWrap(True)
            self.edit_token = QLineEdit()
            self.edit_token.setEchoMode(
                QLineEdit.Normal if cfg.get("market_token_visible")
                else QLineEdit.Password)
            self.edit_token.setPlaceholderText(
                "eyJhbGciOi… (без слова Bearer)")
            self.chk_show = QCheckBox("показать токен")
            self.chk_show.toggled.connect(
                lambda on: self.edit_token.setEchoMode(
                    QLineEdit.Normal if on else QLineEdit.Password))

            action_row = QHBoxLayout()
            self.btn_open_site = QPushButton("Открыть warframe.market")
            self.btn_open_site.clicked.connect(self._open_site)
            self.btn_check = QPushButton("Проверить токен")
            self.btn_check.clicked.connect(self._check)
            self.btn_copy = QPushButton("Скопировать инструкцию")
            self.btn_copy.clicked.connect(self._copy_instruction)
            self.btn_copy_command = QPushButton("Скопировать команду проверки")
            self.btn_copy_command.clicked.connect(self._copy_command)
            for button in (self.btn_open_site, self.btn_check, self.btn_copy,
                           self.btn_copy_command):
                button.setStyleSheet(theme.button_qss(small=True))
                button.setCursor(Qt.PointingHandCursor)
                action_row.addWidget(button)
            action_row.addStretch(1)

            buttons = QHBoxLayout()
            self.btn_save = QPushButton("Сохранить")
            self.btn_save.setDefault(True)
            self.btn_cancel = QPushButton("Отмена")
            self.btn_save.clicked.connect(self._save_clicked)
            self.btn_cancel.clicked.connect(self.reject)
            buttons.addStretch(1)
            buttons.addWidget(self.btn_cancel)
            buttons.addWidget(self.btn_save)
            layout.addWidget(title)
            layout.addWidget(self.btn_instruction)
            layout.addWidget(self.instruction_scroll)
            layout.addWidget(self.lbl_status)
            layout.addWidget(self.edit_token)
            layout.addWidget(self.chk_show)
            layout.addLayout(action_row)
            layout.addLayout(buttons)
            self.check_done.connect(self._on_check_done)
            self.resize(680, 540)

        def _log(self, text):
            if callable(self.logger):
                try:
                    self.logger(str(text))
                except Exception:
                    pass

        def _open_site(self):
            url = "https://warframe.market/"
            try:
                if callable(self.open_url):
                    self.open_url(url)
                else:
                    import webbrowser
                    webbrowser.open(url)
                return True
            except Exception as exc:
                self._log(f"токен: браузер не открылся "
                          f"({type(exc).__name__})")
                return False

        @staticmethod
        def _result_parts(result):
            if isinstance(result, dict):
                state = str(result.get("state") or "")
                code = result.get("code") or 0
                message = str(result.get("message") or "")
                nickname = str(result.get("nickname") or result.get("ingameName")
                               or result.get("name") or "")
                ok = bool(result.get("ok")) or state == "ok" or code == 200
                return ok, state, int(code or 0), message, nickname
            if isinstance(result, tuple):
                state = str(result[0] if result else "")
                message = str(result[1] if len(result) > 1 else "")
                return state == "ok", state, 0, message, ""
            text = str(result or "")
            return text in ("ok", "200"), text, 0, text, ""

        def _check(self):
            if self._checking:
                return False
            token = self.edit_token.text().strip()
            if len(token) < 20:
                self.lbl_status.setText("⚠ токен слишком короткий — "
                                        "скопируйте JWT целиком")
                return False
            self._checking = True
            self.btn_check.setEnabled(False)
            self.btn_check.setText("Проверяю…")
            self.lbl_status.setText("проверяю токен: GET /v2/me…")
            self._log("токен: проверка через /v2/me")

            def worker():
                try:
                    if callable(self.verify_fn):
                        result = self.verify_fn(token)
                    else:
                        result = _verify_token_http(token, self.http_get)
                except Exception as exc:
                    result = {"ok": False, "state": "error", "code": 0,
                              "message": f"нет сети ({type(exc).__name__})"}
                try:
                    self.check_done.emit(
                        result if isinstance(result, dict)
                        else {"ok": False, "state": "error",
                              "message": str(result)})
                except RuntimeError:
                    pass

            threading.Thread(target=worker, daemon=True,
                             name="wf-token-check").start()
            return True

        def _on_check_done(self, result):
            self._checking = False
            self.btn_check.setEnabled(True)
            self.btn_check.setText("Проверить токен")
            ok, state, code, message, nickname = self._result_parts(result)
            if ok:
                self._checked_token = self.edit_token.text().strip()
                who = nickname or "аккаунт"
                self.lbl_status.setText(f"✅ токен принят (ник: {who})")
            elif code == 401 or "401" in message or state in (
                    "unauthorized", "expired", "guest"):
                self.lbl_status.setText(
                    "⚠ 401 — гостевой или просрочен; сначала войдите на сайте")
            elif state in ("no_network", "network") or "нет сети" in message:
                self.lbl_status.setText("🌐 нет сети — проверьте интернет")
            else:
                self.lbl_status.setText(
                    f"⚠ токен не принят: {message or state or 'ошибка'}")

        def _copy_instruction(self):
            text = self.INSTRUCTION + "\n\nПроверка: " + self.CHECK_COMMAND
            return self._set_clipboard(text)

        def _copy_command(self):
            token = self.edit_token.text().strip() or "<JWT>"
            return self._set_clipboard(
                self.CHECK_COMMAND.replace("<JWT>", token))

        def _set_clipboard(self, text):
            try:
                if self.clipboard is not None:
                    self.clipboard.setText(text)
                else:
                    from PyQt5.QtWidgets import QApplication
                    QApplication.clipboard().setText(text)
                self.lbl_status.setText("скопировано в буфер обмена")
                return True
            except Exception as exc:
                self._log(f"токен: буфер недоступен ({type(exc).__name__})")
                return False

        def _save_clicked(self):
            token = self.edit_token.text().strip()
            if token and token != self._checked_token:
                answer = themed_confirm(
                    self, "Токен ещё не проверен через /v2/me. Сохранить?",
                    title="Токен не проверен",
                    confirm_text="Сохранить", cancel_text="Отмена")
                if not answer:
                    return False
            return self._save()

        def _save(self):
            """Проверяет и сохраняет токен. True при успехе (без печати токена)."""
            token = self.edit_token.text().strip()
            if len(token) < 20:
                themed_message(
                    self,
                    "Токен слишком короткий — скопируйте JWT целиком\n"
                    "(обычно он длиной 200+ символов).",
                    title="Токен warframe.market", warning=True)
                return False
            if not self.save_fn(self.cfg, token):
                themed_message(
                    self,
                    "Не удалось сохранить токен в config.json — "
                    "проверьте права на папку программы.",
                    title="Токен warframe.market", warning=True)
                return False
            try:
                self.client.set_token(token)
            except Exception:
                pass
            self.lbl_status.setText("сейчас: " + _token_status(self.client))
            self.accept()
            return True

    class _SellDialog(QDialog):
        """Форма sell-ордера: предмет, цена (подсказка), количество, ранг.

        Создание — только после подтверждения пользователем. Данные предмета
        и подсказка цены загружаются в фоновом потоке, форма не подвисает.
        """

        meta_ready = pyqtSignal(dict)
        meta_failed = pyqtSignal(str)
        hint_ready = pyqtSignal(dict)
        created = pyqtSignal(dict)
        create_failed = pyqtSignal(str)

        def __init__(self, cfg, client, item, parent=None, order_type="sell",
                     order=None, prefill_price=None, price_note=""):
            super().__init__(parent)
            register_app_dialog(self)     # B2: скрытие приложения закроет окно
            self.cfg = cfg
            self.client = client
            self.item = dict(item or {})
            self.order = dict(order or {}) if order else {}
            self.prefill_price = prefill_price
            self.price_note = str(price_note or "")
            self.order_type = "buy" if str(order_type).lower() == "buy" else "sell"
            if self.order:
                self.order_type = ("buy" if order_type_of(self.order) == "buy"
                                   else "sell")
            self.slug = str(self.item.get("slug") or "")
            self.meta = {}
            self.hint = {}
            self._price_user_edited = False
            self._price_from_order = False
            self.setWindowTitle("Обновить ордер" if self.order
                                else f"Создать {self.order_type}-ордер")
            self.setModal(True)
            self.setMinimumWidth(500)
            self.setStyleSheet(dialog_style)

            layout = QVBoxLayout(self)
            self.lbl_name = QLabel(self.item.get("market_name")
                                   or self.item.get("name") or self.slug or "?")
            self.lbl_name.setStyleSheet(
                f"font-size:{theme.size('title')}px; font-weight:700;"
                f" color:{theme.PALETTE['text']};")
            self.lbl_name.setWordWrap(True)
            self.lbl_hint = QLabel("загружаю цену и данные предмета…")
            self.lbl_hint.setStyleSheet(theme.muted_qss(11))
            self.lbl_hint.setWordWrap(True)
            self.lbl_price_note = QLabel(self.price_note)
            self.lbl_price_note.setStyleSheet(
                f"color:{theme.PALETTE['warning']}; font-size:11px;")
            self.lbl_price_note.setWordWrap(True)
            self.lbl_price_note.setVisible(bool(self.price_note))
            self.lbl_url = QLabel(
                f"<a href='https://warframe.market/items/{self.slug}' "
                f"style='color:{theme.PALETTE['accent']};'>"
                "страница предмета на warframe.market</a>")
            self.lbl_url.setOpenExternalLinks(True)

            # (6) переключатель sell/buy в стиле дизайн-системы
            type_row = QHBoxLayout()
            type_row.setSpacing(8)
            type_title = QLabel("Тип ордера")
            type_title.setStyleSheet(theme.muted_qss(11))
            self.btn_type_sell = QPushButton("Продать")
            self.btn_type_buy = QPushButton("Купить")
            self.type_group = QButtonGroup(self)
            self.type_group.setExclusive(True)
            for index, button in enumerate((self.btn_type_sell,
                                            self.btn_type_buy)):
                button.setCheckable(True)
                button.setStyleSheet(theme.button_qss(small=True))
                button.setCursor(Qt.PointingHandCursor)
                self.type_group.addButton(button, index)
            self.btn_type_sell.setChecked(self.order_type == "sell")
            self.btn_type_buy.setChecked(self.order_type == "buy")
            self.btn_type_sell.clicked.connect(lambda: self.set_order_type("sell"))
            self.btn_type_buy.clicked.connect(lambda: self.set_order_type("buy"))
            type_row.addWidget(type_title)
            type_row.addWidget(self.btn_type_sell)
            type_row.addWidget(self.btn_type_buy)
            type_row.addStretch(1)

            layout.addWidget(self.lbl_name)
            layout.addWidget(self.lbl_hint)
            layout.addWidget(self.lbl_price_note)
            layout.addWidget(self.lbl_url)
            layout.addLayout(type_row)

            form = QFormLayout()
            self.spin_price = QSpinBox()
            self.spin_price.setRange(0, 900000)
            self.spin_price.setSpecialValueText("не задана")
            # 1.0.0: «p» — текстом только если иконки валют выключены/нет ассетов
            self._price_currency_icon = currency_icon_label("platinum", cfg,
                                                            size=16)
            self.spin_price.setSuffix("" if self._price_currency_icon
                                      is not None else " p")
            default_price = (self.prefill_price
                             if isinstance(self.prefill_price, (int, float))
                             else (self.order.get("platinum")
                                   if self.order else self.item.get("price")))
            if isinstance(default_price, (int, float)) and 1 <= int(default_price) <= 900000:
                self.spin_price.setValue(int(default_price))
                # правка: цена взята из ордера и не должна перетираться подсказкой
                self._price_from_order = bool(self.order)
            self.spin_price.valueChanged.connect(self._on_price_edited)
            self.spin_quantity = QSpinBox()
            self.spin_quantity.setRange(1, 9999)
            try:
                default_qty = int(cfg.get("market_default_quantity", 1) or 1)
            except Exception:
                default_qty = 1
            if self.order:
                default_qty = int(self.order.get("quantity") or default_qty)
            else:
                try:
                    default_qty = int(self.item.get("quantity") or default_qty)
                except Exception:
                    pass
            self.spin_quantity.setValue(max(1, min(9999, default_qty)))
            self.spin_rank = QSpinBox()
            self.spin_rank.setRange(0, 0)
            self.lbl_rank = QLabel("Ранг мода")
            self.spin_rank.valueChanged.connect(self._on_rank_changed)
            self.spin_pertrade = QSpinBox()
            self.spin_pertrade.setRange(1, 6)
            self.lbl_pertrade = QLabel("Лот (perTrade)")
            self.combo_subtype = QComboBox()
            self.lbl_subtype = QLabel("Подтип")
            self.chk_visible = QCheckBox("виден на сайте (visible)")
            self.chk_visible.setChecked(bool(self.order.get("visible", True))
                                        if self.order else True)
            if self._price_currency_icon is not None:
                from PyQt5.QtWidgets import QHBoxLayout as _QHBoxLayout
                from PyQt5.QtWidgets import QWidget as _QWidget
                price_box = _QWidget()
                price_line = _QHBoxLayout(price_box)
                price_line.setContentsMargins(0, 0, 0, 0)
                price_line.setSpacing(4)
                price_line.addWidget(self.spin_price, 1)
                price_line.addWidget(self._price_currency_icon, 0)
                form.addRow("Цена", price_box)
            else:
                form.addRow("Цена", self.spin_price)
            form.addRow("Количество", self.spin_quantity)
            form.addRow(self.lbl_rank, self.spin_rank)
            form.addRow(self.lbl_pertrade, self.spin_pertrade)
            form.addRow(self.lbl_subtype, self.combo_subtype)
            form.addRow("", self.chk_visible)
            layout.addLayout(form)
            self.lbl_rank.setVisible(False)
            self.spin_rank.setVisible(False)
            self.lbl_pertrade.setVisible(False)
            self.spin_pertrade.setVisible(False)
            self.lbl_subtype.setVisible(False)
            self.combo_subtype.setVisible(False)

            self.lbl_status = QLabel("подготовка…")
            self.lbl_status.setStyleSheet(theme.status_qss())
            self.lbl_status.setWordWrap(True)
            layout.addWidget(self.lbl_status)

            buttons = QHBoxLayout()
            self.btn_site = QPushButton("Открыть на сайте")
            self.btn_retry = QPushButton("Обновить данные")
            self.btn_cancel = QPushButton("Отмена")
            self.btn_create = QPushButton("Создать ордер")
            self.btn_create.setDefault(True)
            self.btn_create.setEnabled(False)
            self.btn_create.clicked.connect(self._create)
            self.btn_site.clicked.connect(self._open_site)
            self.btn_retry.clicked.connect(self._start_load)
            self.btn_cancel.clicked.connect(self.reject)
            buttons.addWidget(self.btn_site)
            buttons.addWidget(self.btn_retry)
            buttons.addStretch(1)
            buttons.addWidget(self.btn_cancel)
            buttons.addWidget(self.btn_create)
            layout.addLayout(buttons)
            self._update_button_text()

            self.meta_ready.connect(self._apply_meta)
            self.meta_failed.connect(self._on_meta_failed)
            self.hint_ready.connect(self._on_hint_ready)
            self.created.connect(self._on_created)
            self.create_failed.connect(self._on_create_failed)
            self._start_load()

        # ── данные ─────────────────────────────────────────────────────
        def _start_load(self):
            if not self.slug:
                self._on_meta_failed("не задан слаг предмета — продажа невозможна")
                return
            self.btn_create.setEnabled(False)
            self.lbl_status.setText("загружаю цену и данные предмета…")
            threading.Thread(target=self._load_worker, daemon=True,
                             name="wf-market-meta").start()

        def _load_worker(self):
            try:
                meta = self.client.item(self.slug)
            except Exception as exc:
                self._emit(self.meta_failed, str(exc) or "не удалось получить предмет")
                return
            try:
                hint = self.client.price_hint(self.slug)
            except Exception as exc:
                hint = {"error": str(exc)}
            self._emit(self.meta_ready, {"meta": meta, "hint": hint})

        @staticmethod
        def _emit(signal, value):
            """Безопасная отправка сигнала из фонового потока."""
            try:
                signal.emit(value)
            except RuntimeError:
                pass          # окно уже закрыли

        def _on_price_edited(self, _value):
            self._price_user_edited = True
            self._update_price_state()

        def _update_price_state(self):
            """Цена обязательна: 0/пусто → кнопка disabled + подсветка."""
            valid = self.spin_price.value() >= 1
            if valid:
                self.spin_price.setStyleSheet("")
            else:
                self.spin_price.setStyleSheet(
                    f"border:1px solid {theme.PALETTE['danger']};")
            loaded = bool(self.meta.get("id"))
            self.btn_create.setEnabled(bool(loaded and valid))

        def _update_button_text(self):
            if self.order:
                self.btn_create.setText("Обновить ордер")
                self.setWindowTitle("Обновить ордер")
            else:
                self.btn_create.setText(f"Создать {self.order_type}-ордер")
                self.setWindowTitle(f"Создать {self.order_type}-ордер")

        def set_order_type(self, order_type):
            """Sell/buy: цена предзаполняется из низшей sell / высшей buy."""
            self.order_type = "buy" if str(order_type).lower() == "buy" else "sell"
            self.btn_type_sell.setChecked(self.order_type == "sell")
            self.btn_type_buy.setChecked(self.order_type == "buy")
            self._update_button_text()
            self._update_hint_text()
            if not self._price_user_edited and not self._price_from_order:
                value = (self.hint.get("buy") if self.order_type == "buy"
                         else self.hint.get("sell"))
                if value:
                    self._set_price(value)

        def _set_price(self, value):
            self.spin_price.blockSignals(True)
            self.spin_price.setValue(max(1, min(900000, int(value))))
            self.spin_price.blockSignals(False)

        def _apply_meta(self, payload):
            payload = dict(payload or {})
            self.meta = dict(payload.get("meta") or {})
            name = (self.meta.get("name") or self.item.get("market_name")
                    or self.item.get("name") or self.slug)
            self.lbl_name.setText(str(name))
            max_rank = int(self.meta.get("max_rank") or 0)
            if max_rank > 0:
                self.spin_rank.blockSignals(True)
                self.spin_rank.setRange(0, max_rank)
                rank = self.item.get("rank")
                if rank is None and self.order:
                    rank = self.order.get("rank")
                if isinstance(rank, int) and 0 <= rank <= max_rank:
                    self.spin_rank.setValue(rank)
                self.spin_rank.blockSignals(False)
                self.lbl_rank.setVisible(True)
                self.spin_rank.setVisible(True)
            if self.meta.get("bulk"):
                self.lbl_pertrade.setVisible(True)
                self.spin_pertrade.setVisible(True)
                if self.order and self.order.get("perTrade") is not None:
                    self.spin_pertrade.setValue(int(self.order["perTrade"]))
            subtypes = [str(x) for x in (self.meta.get("subtypes") or [])]
            if subtypes:
                self.combo_subtype.clear()
                self.combo_subtype.addItems(subtypes)
                if self.order and self.order.get("subtype"):
                    index = self.combo_subtype.findText(str(self.order["subtype"]))
                    if index >= 0:
                        self.combo_subtype.setCurrentIndex(index)
                self.lbl_subtype.setVisible(True)
                self.combo_subtype.setVisible(True)
            self._apply_hint(payload.get("hint") or {})
            self.lbl_status.setText("данные предмета получены")
            self._update_price_state()

        def _update_hint_text(self):
            parts = []
            sell, buy = self.hint.get("sell"), self.hint.get("buy")
            if sell:
                parts.append(f"низшая sell: {sell}p")
            if buy:
                parts.append(f"высшая buy: {buy}p")
            if self.hint.get("count"):
                parts.append(f"ордеров: {self.hint['count']}")
            if self.hint.get("sell_offline"):
                parts.append("онлайн-продавцов нет (цена по оффлайну)")
            if self.hint.get("error"):
                parts.append(f"подсказка недоступна: {self.hint['error']}")
            if self.hint.get("sell") is None and self.hint.get("buy") is None:
                parts.append("введите цену вручную")
            sell = self.hint.get("sell")
            if isinstance(sell, (int, float)) and sell <= 1:
                parts.append("рынок почти пуст — проверьте цену")
            self.lbl_hint.setText(" · ".join(parts)
                                  or "Подсказка цены недоступна — введите цену вручную.")

        def _apply_hint(self, hint):
            """Показывает подсказку цены; впервые — предзаполняет поле цены.

            При правке существующего ордера цена уже взята из ордера и
            подсказкой не перетирается (иначе PATCH уходил бы с чужой ценой).
            """
            self.hint = dict(hint or {})
            self._update_hint_text()
            if not self._price_user_edited and not self._price_from_order:
                value = (self.hint.get("buy") if self.order_type == "buy"
                         else self.hint.get("sell"))
                if value:
                    self._set_price(value)
            self._update_price_state()

        def _on_rank_changed(self, rank):
            """Сменили ранг мода — уточняем цену именно для этого ранга."""
            if not self.meta.get("id") or self.spin_rank.isHidden():
                return
            threading.Thread(target=self._hint_worker, args=(int(rank),),
                             daemon=True, name="wf-market-hint").start()

        def _hint_worker(self, rank):
            try:
                hint = self.client.price_hint(self.slug, rank=rank)
            except Exception:
                return
            self._emit(self.hint_ready, {"hint": hint, "rank": int(rank)})

        def _on_hint_ready(self, payload):
            payload = dict(payload or {})
            current = self.spin_rank.value() if not self.spin_rank.isHidden() else None
            if payload.get("rank") == current:
                self._apply_hint(payload.get("hint") or {})

        def _on_meta_failed(self, message):
            self.lbl_status.setText(f"не удалось подготовить ордер: {message}")
            self.lbl_status.setStyleSheet(theme.status_qss(stale=True))
            self.btn_create.setEnabled(False)

        # ── ордер ──────────────────────────────────────────────────────
        def _payload(self):
            """Поля для ``create_order``/``update_order``."""
            return {
                "item_id": self.meta.get("id"),
                "order_type": self.order_type,
                "platinum": self.spin_price.value(),
                "quantity": self.spin_quantity.value(),
                "visible": self.chk_visible.isChecked(),
                "rank": (self.spin_rank.value()
                         if not self.spin_rank.isHidden() else None),
                "per_trade": (self.spin_pertrade.value()
                              if not self.spin_pertrade.isHidden() else None),
                "subtype": (self.combo_subtype.currentText()
                            if not self.combo_subtype.isHidden() else None),
                "meta": self.meta,
            }

        def _create(self):
            if not self.meta or not self.meta.get("id"):
                self._on_meta_failed("данные предмета ещё не получены")
                return
            params = self._payload()
            if not self.order:
                try:
                    self.client.validate_order(
                        params["platinum"], params["quantity"],
                        rank=params["rank"], per_trade=params["per_trade"],
                        meta=params["meta"], subtype=params["subtype"])
                except Exception as exc:
                    themed_message(self, str(exc), title="Проверка ордера",
                                   warning=True)
                    return
            details = f"{params['platinum']}p × {params['quantity']}"
            if params["rank"] is not None:
                details += f", ранг {params['rank']}"
            if params["per_trade"] is not None:
                details += f", лот {params['per_trade']}"
            action = ("Обновить" if self.order
                      else f"Создать {params['order_type']}-ордер")
            answer = themed_confirm(
                self,
                f"{action} на warframe.market?\n\n"
                f"{self.lbl_name.text()}\n{details}",
                title="Подтверждение", confirm_text=action,
                cancel_text="Отмена")
            if not answer:
                return
            self.btn_create.setEnabled(False)
            self.lbl_status.setText("обновляю ордер…" if self.order
                                    else "создаю ордер…")
            threading.Thread(target=self._create_worker, daemon=True,
                             name="wf-market-create").start()

        def _create_worker(self):
            params = self._payload()
            if not params.get("item_id"):
                self._emit(self.create_failed, "не получен ID предмета")
                return
            try:
                if self.order:
                    order_id = self.order.get("id")
                    if not order_id:
                        self._emit(self.create_failed, "в ордере нет ID")
                        return
                    result = self.client.update_order(
                        order_id, platinum=params["platinum"],
                        quantity=params["quantity"], visible=params["visible"],
                        rank=params["rank"], per_trade=params["per_trade"],
                        subtype=params["subtype"])
                else:
                    result = self.client.create_order(
                        params["item_id"], params["platinum"], params["quantity"],
                        visible=params["visible"], rank=params["rank"],
                        per_trade=params["per_trade"], subtype=params["subtype"],
                        meta=params["meta"], order_type=params["order_type"])
            except Exception as exc:
                self._emit(self.create_failed, str(exc) or "не удалось создать ордер")
                return
            self._emit(self.created, result if isinstance(result, dict) else {})

        def _on_created(self, result):
            result = dict(result or {})
            if result.get("dry_run"):
                body = json.dumps(result.get("body") or {}, ensure_ascii=False, indent=2)
                self.lbl_status.setText("режим проверки: ордер не отправлен")
                self.btn_create.setEnabled(True)
                themed_message(
                    self,
                    "Ордер НЕ отправлен: включён market_dry_run.\n\n"
                    f"Тело запроса:\n{body}",
                    title="Режим проверки (market_dry_run)")
                return
            order = result.get("order") if isinstance(result.get("order"), dict) else result
            lines = ["Ордер обновлён на warframe.market." if self.order
                     else f"Ордер создан на warframe.market ({self.order_type})."]
            if order.get("id"):
                lines.append(f"ID: {order['id']}")
            if order.get("platinum"):
                lines.append(f"{order['platinum']}p × {order.get('quantity', '?')}")
            self.lbl_status.setText("ордер обновлён" if self.order else "ордер создан")
            themed_message(self, "\n".join(lines), title="Готово")
            self.accept()

        def _on_create_failed(self, message):
            self.lbl_status.setText(f"ордер не создан: {message}")
            self.lbl_status.setStyleSheet(theme.status_qss(stale=True))
            self.btn_create.setEnabled(True)
            themed_message(self, str(message), title="Ордер не создан",
                           warning=True)

        def _open_site(self):
            import webbrowser
            url = self.meta.get("url") or f"https://warframe.market/items/{self.slug}"
            try:
                webbrowser.open(url)
                self.lbl_status.setText("страница предмета открыта в браузере")
            except Exception as exc:
                self.lbl_status.setText(f"не удалось открыть браузер ({type(exc).__name__})")

    return _SellDialog, _TokenDialog


def _token_status(client):
    """Короткий статус токена для UI (никогда не сам токен)."""
    try:
        return client.token_status() if client is not None else "нет токена"
    except Exception:
        return "нет токена"


# ─────────────────────────────────────────────────────────────────────────
#  Фокус и мышь (чтобы редактор можно было навести в игре)
# ─────────────────────────────────────────────────────────────────────────
_game_hwnd_cache = {"hwnd": None}


def _user32():
    import ctypes
    return ctypes.windll.user32


def game_window_handle():
    """Находит окно Warframe (по имени процесса), кэширует."""
    import ctypes
    from ctypes import wintypes
    u = _user32()
    k = ctypes.windll.kernel32
    cached = _game_hwnd_cache.get("hwnd")
    if cached and u.IsWindow(cached):
        return cached
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not u.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = k.OpenProcess(0x1000, False, pid.value)
        if not h:
            return True
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        try:
            if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                if "warframe" in buf.value.lower():
                    found.append(hwnd)
        except Exception:
            pass
        k.CloseHandle(h)
        return True

    try:
        u.EnumWindows(cb, 0)
    except Exception:
        pass
    hwnd = found[0] if found else None
    _game_hwnd_cache["hwnd"] = hwnd
    return hwnd


def force_foreground(hwnd):
    """Выводит окно вперёд и делает активным (обход ограничения Windows)."""
    import ctypes
    u = _user32()
    try:
        u.ShowWindow(hwnd, 5)          # SW_SHOW
        u.BringWindowToTop(hwnd)
        u.AllowSetForegroundWindow(-1)
        if not u.SetForegroundWindow(hwnd):
            # трюк с «Alt»: снимает запрет на смену фокуса
            u.keybd_event(0x12, 0, 0, 0)
            u.SetForegroundWindow(hwnd)
            u.keybd_event(0x12, 0, 2, 0)
        return u.GetForegroundWindow() == hwnd
    except Exception:
        return False


def release_mouse_to_window(hwnd):
    """Отпускает захват мыши у игры и ставит курсор в центр окна."""
    import ctypes
    from ctypes import wintypes
    u = _user32()
    try:
        u.ClipCursor(None)             # снять клип, если игра его ставила
    except Exception:
        pass
    try:
        rect = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(rect))
        cx = (rect.left + rect.right) // 2
        cy = (rect.top + rect.bottom) // 2
        u.SetCursorPos(cx, cy)
    except Exception:
        pass


def focus_editor(widget):
    """Показать редактор поверх игры, отдать ему мышь и фокус."""
    hwnd = int(widget.winId())
    widget.show()
    widget.raise_()
    widget.activateWindow()
    ok = force_foreground(hwnd)
    release_mouse_to_window(hwnd)
    return ok


def restore_game_focus():
    """Вернуть фокус и захват мыши в игру."""
    hwnd = game_window_handle()
    if not hwnd:
        return False
    return force_foreground(hwnd)


# ─────────────────────────────────────────────────────────────────────────
#  Захват экрана
# ─────────────────────────────────────────────────────────────────────────
def _mss_factory():
    """mss.mss() устарел в новых версиях — берём mss.MSS, если он есть."""
    import mss
    return getattr(mss, "MSS", None) or mss.mss


def monitor_info(cfg):
    with _mss_factory()() as sct:
        mon = sct.monitors[int(cfg.get("monitor", 1))]
    return mon


def grab(cfg):
    import numpy as np
    with _mss_factory()() as sct:
        mon = sct.monitors[int(cfg.get("monitor", 1))]
        shot = np.array(sct.grab(mon))
    return shot, mon


def to_base(img, cfg):
    """Кадр → внутренние 1920×1080 без растяжения (1.0.0).

    Масштаб ``min(W/1920, H/1080)`` и центральная вырезка 1920×1080 —
    как ``ScreenScaling`` в WFInfo: 16:10/21:9/4:3 больше не «сплющивают»
    карточки. Для 16:9 результат прежний.
    """
    import cv2
    mod = reward_geometry_module()
    if mod is not None:
        try:
            base, _sc, _x0, _y0 = mod.to_base_uniform(
                img, int(cfg["base_width"]), int(cfg["base_height"]))
            if base is not None and base.size:
                return base
        except Exception as e:
            log(f"Кадр: равномерный масштаб не удался ({type(e).__name__}: {e})")
    bh, bw = int(cfg["base_height"]), int(cfg["base_width"])
    if img.shape[1] == bw and img.shape[0] == bh:
        return img
    interp = (cv2.INTER_AREA if (img.shape[1] > bw or img.shape[0] > bh)
              else cv2.INTER_CUBIC)
    return cv2.resize(img, (bw, bh), interpolation=interp)


# ─────────────────────────────────────────────────────────────────────────
#  Авто-режим: слежение за экраном наград
# ─────────────────────────────────────────────────────────────────────────
def templates_path(cfg):
    d = os.path.join(DATA_DIR, cfg.get("template_dir") or "templates")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _header_box(mon, cfg):
    """Полоса с заголовком экрана наград (доли экрана из конфига)."""
    x0, x1 = cfg.get("detect_region_x") or (0.10, 0.45)
    y0, y1 = cfg.get("detect_region_y") or (0.02, 0.10)
    return {"left": mon["left"] + int(mon["width"] * x0),
            "top": mon["top"] + int(mon["height"] * y0),
            "width": max(8, int(mon["width"] * (x1 - x0))),
            "height": max(8, int(mon["height"] * (y1 - y0)))}


def header_gray(img, mon, cfg):
    """Вырезает полосу заголовка из полноэкранного кадра."""
    import cv2
    x0, x1 = cfg.get("detect_region_x") or (0.10, 0.45)
    y0, y1 = cfg.get("detect_region_y") or (0.02, 0.10)
    crop = img[int(mon["height"] * y0):int(mon["height"] * y1),
               int(mon["width"] * x0):int(mon["width"] * x1)]
    if crop.size == 0:
        return None
    if crop.ndim == 3 and crop.shape[2] == 4:
        return cv2.cvtColor(crop, cv2.COLOR_BGRA2GRAY)
    if crop.ndim == 3:
        return cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return crop


def load_templates(cfg):
    out = []
    d = templates_path(cfg)
    try:
        for name in sorted(os.listdir(d)):
            if not name.lower().endswith(".png"):
                continue
            img = imread_unicode(os.path.join(d, name), 0)
            if img is not None and img.size:
                out.append(img)
    except Exception:
        pass
    return out


def save_template(cfg, gray):
    d = templates_path(cfg)
    names = [n for n in os.listdir(d) if n.lower().endswith(".png")]
    path = os.path.join(d, f"template_{len(names) + 1:02d}.png")
    imwrite_unicode(path, gray)
    return path


# ─────────────────────────────────────────────────────────────────────────
#  Журнал реликвий: что выпало и что выбрано
# ─────────────────────────────────────────────────────────────────────────
def relics_log_path(cfg):
    return os.path.join(DATA_DIR, cfg.get("relics_log") or "relics_log.txt")


def reset_relic_log(cfg):
    """Создаёт журнал заново при каждом запуске программы."""
    path = relics_log_path(cfg)
    if not cfg.get("relics_log_reset", True):
        return path
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("VoidFrameRU — журнал реликвий\n")
            f.write(f"Запуск: {datetime.datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n")
            f.write("Файл пересоздаётся при каждом запуске программы.\n")
            f.write("=" * 56 + "\n")
        log(f"Журнал реликвий: {path}")
    except Exception as e:
        log(f"  журнал не создан: {e}")
    return path


def append_relic_block(cfg, results, title="Награды"):
    """Дописывает блок с наградами одной реликвии (выбор уточняется позже).

    ``title`` — заголовок блока: «Награды» для OCR-скана и «Награда (лог)»
    для собственного дропа, найденного в EE.log.
    """
    path = relics_log_path(cfg)
    when = datetime.datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    lines = ["", f"[{when}]  {title}: {len(results)}"]
    total = 0
    for i, d in enumerate(results, 1):
        name = d.get("market_name") or d.get("name") or "?"
        price = d.get("price")
        if isinstance(price, (int, float)) and price:
            total += price
        extra = []
        if d.get("ducats"):
            extra.append(f"дук {d['ducats']}")
        if d.get("owned"):
            extra.append(str(d["owned"]))
        if d.get("sell_online"):
            extra.append(f"{d['sell_online']} онлайн")
        tail = ("  |  " + "  |  ".join(extra)) if extra else ""
        lines.append(f"  {i}. {name} — {fmt_plat(price)}{tail}")
    lines.append(f"  Итого: {int(total)}p")
    lines.append("  Выбран: ?")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        log(f"  журнал не записан: {e}")
    return when


def save_selected_list(cfg, items):
    """Пишет список «Выбрано из реликвий» (перезаписывает файл)."""
    path = os.path.join(DATA_DIR, cfg.get("selected_log") or "selected.txt")
    try:
        total = 0
        lines = ["Выбрано из реликвий",
                 f"Обновлено: {datetime.datetime.now().strftime('%d.%m.%Y %H:%M:%S')}",
                 "=" * 48, ""]
        for i, it in enumerate(items, 1):
            name = it.get("market_name") or it.get("name") or "?"
            price = it.get("price")
            if isinstance(price, (int, float)) and price:
                total += price
            du = it.get("ducats")
            tail = f"  ◆ {du} дк" if du else ""
            lines.append(f"{i}. {name} — {fmt_plat(price)}{tail}")
        lines.append("")
        lines.append(f"Итого: {int(total)}p")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        log(f"  список выбранного не записан: {e}")


def mark_relic_selection(cfg, index, results):
    """Заменяет последнюю строку «Выбран: ?» на выбранный предмет."""
    if index is None or not results or index >= len(results):
        return False
    d = results[index]
    name = d.get("market_name") or d.get("name") or "?"
    price = d.get("price")
    text = f"  Выбран: {index + 1}. {name}"
    if isinstance(price, (int, float)) and price:
        text += f" ({int(price)}p)"
    path = relics_log_path(cfg)
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].strip().startswith("Выбран:"):
                lines[i] = text
                break
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        log(f"  Выбран предмет: {name}")
        return True
    except Exception as e:
        log(f"  не удалось отметить выбор: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────
#  Игровой лог EE.log → собственный дроп (только чтение)
#  Подробности: wf_ru/LOG_SPEC.md. Лог — дополнительный источник: он даёт
#  точный путь своего дропа, но не награды остальных игроков и не выбор.
# ─────────────────────────────────────────────────────────────────────────
_LOG_MODULE = {"mod": None, "tried": False}


def log_parser_module():
    """Подключает модуль log_parser (лежит рядом с warframe.py), кэшируя результат.

    Импорт отдельным файлом, поэтому ищем и обычным ``import``, и по пути —
    приложение может запускаться через launcher/runpy, где папка скрипта
    не попадает в ``sys.path``. Если модуля нет — лог просто не читается.
    """
    if _LOG_MODULE["tried"]:
        return _LOG_MODULE["mod"]
    _LOG_MODULE["tried"] = True
    try:
        import log_parser as mod
        _LOG_MODULE["mod"] = mod
        return mod
    except Exception:
        pass
    try:
        import importlib.util
        here = os.path.dirname(os.path.abspath(__file__))
        # где может лежать log_parser.py: рядом с warframe.py, в папке программы
        # и в bundle/app (лаунчер запускает warframe.py через runpy)
        candidates = [here, APP_DIR, os.path.join(APP_DIR, "app")]
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            candidates.append(os.path.join(meipass, "app"))
        for directory in candidates:
            path = os.path.join(directory, "log_parser.py")
            if not os.path.isfile(path):
                continue
            spec = importlib.util.spec_from_file_location("log_parser", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            sys.modules.setdefault("log_parser", mod)
            _LOG_MODULE["mod"] = mod
            return mod
    except Exception as e:
        log(f"Модуль log_parser не подключён ({type(e).__name__}: {e})")
    return None


# ─────────────────────────────────────────────────────────────────────────
#  Игровые события (api.warframestat.us) — только чтение, см.
#  EVENTS_MARKET_SPEC.md. Модуль лежит рядом с warframe.py и подключается
#  лениво: без него оверлей работает как раньше (Alt+Q / F8 / журнал).
# ─────────────────────────────────────────────────────────────────────────
_EVENTS_MODULE = {"mod": None, "tried": False}


def events_api_module():
    """Подключает модуль events_api (лежит рядом с warframe.py).

    Тот же приём, что и у ``log_parser_module``: приложение может запускаться
    через launcher/runpy, где папка скрипта не попадает в ``sys.path``.
    Возвращает модуль или ``None`` — тогда панель событий просто недоступна.
    """
    if _EVENTS_MODULE["tried"]:
        return _EVENTS_MODULE["mod"]
    _EVENTS_MODULE["tried"] = True
    try:
        import events_api as mod
        _EVENTS_MODULE["mod"] = mod
        return mod
    except Exception:
        pass
    try:
        import importlib.util
        here = os.path.dirname(os.path.abspath(__file__))
        candidates = [here, APP_DIR, os.path.join(APP_DIR, "app")]
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            candidates.append(os.path.join(meipass, "app"))
        for directory in candidates:
            path = os.path.join(directory, "events_api.py")
            if not os.path.isfile(path):
                continue
            spec = importlib.util.spec_from_file_location("events_api", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            sys.modules.setdefault("events_api", mod)
            _EVENTS_MODULE["mod"] = mod
            return mod
    except Exception as e:
        log(f"Модуль events_api не подключён ({type(e).__name__}: {e})")
    return None


# ─────────────────────────────────────────────────────────────────────────
#  Продажа на warframe.market (см. EVENTS_MARKET_SPEC.md, часть II).
#  Модуль market_client.py лежит рядом и подключается лениво: без него
#  оверлей работает как раньше, кнопка «продать» вежливо сообщит об этом.
# ─────────────────────────────────────────────────────────────────────────
_MARKET_MODULE = {"mod": None, "tried": False}


def market_client_module():
    """Подключает модуль market_client (лежит рядом с warframe.py)."""
    if _MARKET_MODULE["tried"]:
        return _MARKET_MODULE["mod"]
    _MARKET_MODULE["tried"] = True
    try:
        import market_client as mod
        _MARKET_MODULE["mod"] = mod
        return mod
    except Exception:
        pass
    try:
        import importlib.util
        here = os.path.dirname(os.path.abspath(__file__))
        candidates = [here, APP_DIR, os.path.join(APP_DIR, "app")]
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            candidates.append(os.path.join(meipass, "app"))
        for directory in candidates:
            path = os.path.join(directory, "market_client.py")
            if not os.path.isfile(path):
                continue
            spec = importlib.util.spec_from_file_location("market_client", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            sys.modules.setdefault("market_client", mod)
            _MARKET_MODULE["mod"] = mod
            return mod
    except Exception as e:
        log(f"Модуль market_client не подключён ({type(e).__name__}: {e})")
    return None


# ─────────────────────────────────────────────────────────────────────────
#  Дизайн-система (ui_theme.py) и окно «Маркет» (market_browser.py).
#  Оба модуля лежат рядом и подключаются лениво; без них оверлей работает
#  как раньше (только со старыми окнами и встроенной палитрой).
# ─────────────────────────────────────────────────────────────────────────
_UI_THEME_MODULE = {"mod": None, "tried": False}
_MARKET_BROWSER_MODULE = {"mod": None, "tried": False}
_OVERLAY_WINDOW_MODULE = {"mod": None, "tried": False}
_MARKET_AUTH_MODULE = {"mod": None, "tried": False}
_MARKET_BROWSER_LOGIN_MODULE = {"mod": None, "tried": False}
_LOGIN_MODE_MODULE = {"mod": None, "tried": False}
_FARM_STATS_MODULE = {"mod": None, "tried": False}
_RELIC_POOL_MODULE = {"mod": None, "tried": False}
_FARM_STATS_UI_MODULE = {"mod": None, "tried": False}
_NOTIFICATIONS_MODULE = {"mod": None, "tried": False}
_LAUNCHER_MODULE = {"mod": None, "tried": False}
_SETTINGS_UI_MODULE = {"mod": None, "tried": False}
_NOTIFICATION_CENTER_MODULE = {"mod": None, "tried": False}
_CHAT_CLIENT_MODULE = {"mod": None, "tried": False}
_MARKET_STATUS_MODULE = {"mod": None, "tried": False}
_BROWSER_TOKEN_MODULE = {"mod": None, "tried": False}
_UPDATE_WATCHER_MODULE = {"mod": None, "tried": False}
_ITEM_ICONS_MODULE = {"mod": None, "tried": False}
_ICON_PACK_MODULE = {"mod": None, "tried": False}
_SCAN_CALIBRATE_MODULE = {"mod": None, "tried": False}
_REWARD_GEOMETRY_MODULE = {"mod": None, "tried": False}
_GAME_THEMES_MODULE = {"mod": None, "tried": False}

#: Страховка, если login_mode.py нет рядом (старая сборка): флаг всё равно
#: нужен guard-ам окон/хоткеев, чтобы не всплывать во время входа.
_FALLBACK_LOGIN_STATE = {"in_progress": False}


def _load_local_module(file_name, module_name, state):
    """Импортирует модуль рядом с warframe.py (launcher/runpy, exe/app)."""
    if state["tried"]:
        return state["mod"]
    state["tried"] = True
    try:
        mod = __import__(module_name)
        state["mod"] = mod
        return mod
    except Exception:
        pass
    try:
        import importlib.util
        here = os.path.dirname(os.path.abspath(__file__))
        candidates = [here, APP_DIR, os.path.join(APP_DIR, "app")]
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            candidates.append(os.path.join(meipass, "app"))
        for directory in candidates:
            path = os.path.join(directory, file_name)
            if not os.path.isfile(path):
                continue
            spec = importlib.util.spec_from_file_location(module_name, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            sys.modules.setdefault(module_name, mod)
            state["mod"] = mod
            return mod
    except Exception as e:
        log(f"Модуль {module_name} не подключён ({type(e).__name__}: {e})")
    return None


def ui_theme_module():
    """Палитра и QSS (ui_theme.py) — единый стиль всех окон."""
    return _load_local_module("ui_theme.py", "ui_theme", _UI_THEME_MODULE)


def market_browser_module():
    """Окно «Маркет» (market_browser.py) — Alt+M."""
    return _load_local_module("market_browser.py", "market_browser",
                              _MARKET_BROWSER_MODULE)


def overlay_window_module():
    """Единое окно с вкладками (overlay_window.py) — F8."""
    return _load_local_module("overlay_window.py", "overlay_window",
                              _OVERLAY_WINDOW_MODULE)


def market_auth_module():
    """Вход на warframe.market: токен-контроллер и запасной webview.

    Модуль опционален: если его нет рядом (старая сборка) или в системе нет
    PyQtWebEngine, оверлей работает как раньше — остаётся ручная вставка JWT.
    PyQtWebEngine внутри market_auth импортируется лениво и не здесь.
    """
    return _load_local_module("market_auth.py", "market_auth",
                              _MARKET_AUTH_MODULE)


def market_browser_login_module():
    """Вход через браузер пользователя по CDP (market_browser_login.py).

    Основной способ: встроенный Chromium 87 не проходит Cloudflare, поэтому
    запускаем Chrome/Edge/Yandex/Brave с отдельным профилем и забираем
    HttpOnly-cookie JWT через отладочный протокол. Модуль опционален: без него
    остаётся ручная вставка токена.
    """
    return _load_local_module("market_browser_login.py",
                              "market_browser_login",
                              _MARKET_BROWSER_LOGIN_MODULE)


def login_mode_module():
    """Режим входа (login_mode.py): общий флаг и скрытие окон приложения.

    Пока идёт вход на warframe.market в браузере, все окна оверлея спрятаны,
    а горячие клавиши/автоскан/плашки не показывают их повторно. Загрузка
    ленивая и с запасным вариантом: без модуля приложение работает как раньше.
    """
    return _load_local_module("login_mode.py", "login_mode", _LOGIN_MODE_MODULE)


def farm_stats_module():
    """Сборщик статистики фарма (farm_stats.py) — вкладка «Дроп».

    Модуль опционален: без него оверлей работает с базовыми вкладками.
    """
    return _load_local_module("farm_stats.py", "farm_stats", _FARM_STATS_MODULE)


def relic_pool_module():
    """Пулы реликвий (relic_pool.py) — вкладка «Дроп» → «Пулы реликвий».

    Модуль опционален: без него оверлей работает с базовыми вкладками.
    """
    return _load_local_module("relic_pool.py", "relic_pool", _RELIC_POOL_MODULE)


def farm_stats_ui_module():
    """Виджет вкладки «Дроп» (farm_stats_ui.py) — подвкладки статистика/пулы.

    Модуль опционален: без него вкладка «Дроп» не показывается.
    """
    return _load_local_module("farm_stats_ui.py", "farm_stats_ui",
                              _FARM_STATS_UI_MODULE)


def notifications_module():
    """Уведомления Windows (notifications.py): трей, тосты, события.

    Модуль опционален: без него приложение работает как раньше, без трея и
    уведомлений (QSystemTrayIcon не трогаем).
    """
    return _load_local_module("notifications.py", "notifications",
                              _NOTIFICATIONS_MODULE)


def launcher_module():
    """Лаунчер (launcher.py) — проверка/установка обновлений из окна F8.

    Модуль опционален: без него кнопка «Обновления» честно скажет, что
    обновления в этой сборке недоступны.
    """
    return _load_local_module("launcher.py", "launcher", _LAUNCHER_MODULE)


def settings_ui_module():
    """Окно «Настройки» (settings_ui.py): клавиши, уведомления, оверлей.

    Модуль опционален: без него кнопка-шестерёнка сообщит, что настройки в этой
    сборке недоступны, остальное работает как раньше.
    """
    return _load_local_module("settings_ui.py", "settings_ui",
                              _SETTINGS_UI_MODULE)


def notification_center_module():
    """Центр уведомлений (notification_center.py): история, тосты, звук."""
    return _load_local_module("notification_center.py", "notification_center",
                              _NOTIFICATION_CENTER_MODULE)


def chat_client_module():
    """Чат warframe.market (chat_client.py): HTTP/WS/вкладка."""
    return _load_local_module("chat_client.py", "chat_client",
                              _CHAT_CLIENT_MODULE)


def market_status_module():
    """Статус warframe.market (market_status.py): WS A/чип/авто."""
    return _load_local_module("market_status.py", "market_status",
                              _MARKET_STATUS_MODULE)


def browser_token_module():
    """JWT из профилей Chrome/Edge/Yandex/Opera/Firefox (browser_token.py)."""
    return _load_local_module("browser_token.py", "browser_token",
                              _BROWSER_TOKEN_MODULE)


def update_watcher_module():
    """Фоновая автопроверка обновлений (update_watcher.py)."""
    return _load_local_module("update_watcher.py", "update_watcher",
                              _UPDATE_WATCHER_MODULE)


def item_icons_module():
    """Иконки WFCD (item_icons.py); модуль опционален."""
    return _load_local_module("item_icons.py", "item_icons",
                              _ITEM_ICONS_MODULE)


_ICON_BUNDLE_STATE = {"ensured": False}


def item_icons_provider(cfg=None):
    """Провайдер иконок: DATA_DIR → bundle-архив → (только вручную) сеть."""
    mod = item_icons_module()
    if mod is None:
        return None
    cfg = cfg or {}
    if not _ICON_BUNDLE_STATE.get("ensured"):
        _ICON_BUNDLE_STATE["ensured"] = True
        try:
            mod.ensure_bundle(DATA_DIR, logger=log)
            info = mod.library_status(DATA_DIR)
            log(f"Иконки: библиотека {info.get('files', 0)} файлов "
                f"(комплект: {'да' if info.get('bundle') else 'нет'}), "
                "сеть за иконками выключена")
        except Exception as e:
            log(f"Иконки: bundle не распакован ({type(e).__name__}: {e})")
    try:
        return mod.get_provider(
            DATA_DIR, enabled=bool(cfg.get("show_item_icons", True)),
            auto_download=False)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────
#  Пакет иконок предметов (1.0.0, icon_pack.py): скачивание один раз,
#  работа офлайн, автообновление раз в icons_update_days дней.
# ─────────────────────────────────────────────────────────────────────────
_ICON_PACK_STATE = {"pack": None}


def icon_pack_module():
    """Загрузчик пакета иконок (icon_pack.py); модуль опционален."""
    return _load_local_module("icon_pack.py", "icon_pack", _ICON_PACK_MODULE)


def scan_calibrate_module():
    """Авто-калибровка скана (scan_calibrate.py); модуль опционален."""
    return _load_local_module("scan_calibrate.py", "scan_calibrate",
                              _SCAN_CALIBRATE_MODULE)


def reward_geometry_module():
    """Геометрия экрана наград из WFInfo (reward_geometry.py)."""
    return _load_local_module("reward_geometry.py", "reward_geometry",
                              _REWARD_GEOMETRY_MODULE)


def game_themes_module():
    """Темы интерфейса и проба (game_themes.py из данных WFInfo)."""
    return _load_local_module("game_themes.py", "game_themes",
                              _GAME_THEMES_MODULE)


def icon_pack_manager(cfg=None):
    """Единственный загрузчик пака для DATA_DIR (или None, если модуля нет)."""
    mod = icon_pack_module()
    if mod is None:
        return None
    cfg = cfg or {}
    pack = _ICON_PACK_STATE.get("pack")
    if pack is None:
        try:
            rate = float(cfg.get("icons_rate_limit", 3.0) or 3.0)
        except Exception:
            rate = 3.0
        try:
            pack = mod.IconPack(DATA_DIR, logger=log, app_version=APP_VERSION,
                                rate=rate)
        except Exception as e:
            log(f"Иконки: загрузчик пака не создан ({type(e).__name__}: {e})")
            return None
        _ICON_PACK_STATE["pack"] = pack
    return pack


def reset_icon_pack():
    """Сбросить синглтон загрузчика (тесты/смена DATA_DIR)."""
    _ICON_PACK_STATE["pack"] = None
    mod = icon_pack_module()
    if mod is not None:
        try:
            mod.reset_pack()
        except Exception:
            pass


def icons_status(cfg=None):
    """Сводка состояния пака иконок для настроек/прогресса."""
    pack = icon_pack_manager(cfg)
    if pack is None:
        return {"available": False, "have_pack": False, "files": 0,
                "bytes": 0, "running": False}
    try:
        info = dict(pack.status())
    except Exception as e:
        return {"available": False, "have_pack": False, "files": 0,
                "bytes": 0, "running": False,
                "error": f"{type(e).__name__}: {e}"}
    info["available"] = True
    return info


_ICON_REDRAW = {"fn": None}


def set_icon_redraw_callback(fn):
    """t54: зарегистрировать перерисовку видимых иконок после пака."""
    _ICON_REDRAW["fn"] = fn if callable(fn) else None
    return True


def _icons_after(result, cfg=None):
    """После скачивания/очистки: провайдер перечитывает манифест пака."""
    try:
        provider = item_icons_provider(cfg)
        if provider is not None:
            provider.reload_pack()
            provider.clear_cache()
    except Exception as e:
        log(f"Иконки: провайдер не перечитал пак ({type(e).__name__}: {e})")
    fn = _ICON_REDRAW.get("fn")
    if callable(fn):
        try:
            fn()
        except Exception as e:
            log(f"Иконки: перерисовка не удалась ({type(e).__name__}: {e})")
    return dict(result or {})


def icons_download(cfg=None, on_progress=None, on_done=None, force=False,
                   blocking=False):
    """Скачать пак иконок (по умолчанию — в фоне, окно не блокируется)."""
    pack = icon_pack_manager(cfg)
    if pack is None:
        return {"ok": False, "error": "модуль icon_pack.py не найден"}
    if pack.running:
        return {"ok": False, "busy": True, "error": "загрузка уже идёт"}

    def worker():
        result = pack.download(progress=on_progress, force=force)
        _icons_after(result, cfg)
        if callable(on_done):
            try:
                on_done(result)
            except Exception:
                pass
        return result

    if blocking:
        return worker()
    threading.Thread(target=worker, daemon=True,
                     name="wf-icon-pack").start()
    return {"ok": True, "started": True}


def icons_update(cfg=None, on_progress=None, on_done=None, blocking=False):
    """Сверить каталог и докачать только новые/изменённые иконки."""
    pack = icon_pack_manager(cfg)
    if pack is None:
        return {"ok": False, "error": "модуль icon_pack.py не найден"}
    if pack.running:
        return {"ok": False, "busy": True, "error": "загрузка уже идёт"}

    def worker():
        result = pack.update(progress=on_progress)
        _icons_after(result, cfg)
        if callable(on_done):
            try:
                on_done(result)
            except Exception:
                pass
        return result

    if blocking:
        return worker()
    threading.Thread(target=worker, daemon=True,
                     name="wf-icon-pack-update").start()
    return {"ok": True, "started": True}


def icons_clear(cfg=None):
    """Удалить пак иконок (кнопка «Очистить кэш иконок»)."""
    pack = icon_pack_manager(cfg)
    if pack is None:
        return {"ok": False, "error": "модуль icon_pack.py не найден"}
    if pack.running:
        return {"ok": False, "error": "идёт загрузка — дождитесь окончания"}
    result = pack.clear()
    _icons_after(result, cfg)
    log("Иконки: кэш очищен по кнопке в настройках")
    # t54: если пак включён — сразу скачать заново
    try:
        if bool((cfg or {}).get("icons_pack_enabled", True)):
            icons_download(cfg)
            result = dict(result or {})
            result["redownload_started"] = True
    except Exception as e:
        log(f"Иконки: повторная загрузка не запущена "
            f"({type(e).__name__}: {e})")
    return result


def icons_cancel(cfg=None):
    """Прервать фоновую загрузку пака (кнопка «Отмена» в шапке)."""
    pack = icon_pack_manager(cfg)
    if pack is None:
        return False
    if pack.running:
        pack.request_cancel()
        log("Иконки: запрошена отмена загрузки")
        return True
    return False


def icon_pack_progress_bridge(callback):
    """Мост «фоновый поток загрузчика → GUI-поток» для прогресса иконок.

    Возвращает QObject с ``emit_progress(payload)`` или ``None``, если Qt
    недоступен. Слот — метод самого QObject (создан в GUI-потоке), поэтому
    AutoConnection доставляет сигнал в GUI-поток: виджеты можно трогать
    только там, а загрузчик живёт в отдельном потоке.
    """
    try:
        from PyQt5.QtCore import QObject, pyqtSignal
    except Exception:
        return None

    class _IconPackProgress(QObject):
        progress = pyqtSignal(dict)

        def __init__(self):
            super().__init__()
            self._callback = callback
            self.progress.connect(self._deliver)

        def _deliver(self, payload):
            if callable(self._callback):
                try:
                    self._callback(dict(payload or {}))
                except Exception:
                    pass

        def emit_progress(self, payload):
            self.progress.emit(dict(payload or {}))
            return True

    try:
        return _IconPackProgress()
    except Exception:
        return None


def icons_autostart(cfg=None, on_progress=None, on_done=None):
    """Первый запуск: пака нет → качаем в фоне (не блокирует окно)."""
    cfg = cfg or {}
    if not cfg.get("icons_pack_enabled", True):
        return False
    return False
    pack = icon_pack_manager(cfg)
    if pack is None or pack.running:
        return False
    try:
        if not pack.needs_download():
            return False
    except Exception:
        return False
    log("Иконки: пака нет — начинаю скачивание в фоне (не более 3 запросов/с)")
    result = icons_download(cfg, on_progress=on_progress, on_done=on_done)
    return bool(result.get("started"))


def icons_update_watcher(cfg=None, on_progress=None, on_done=None,
                         stop_event=None, interval=3600, first_delay=20):
    """Фоновая сверка пака: раз в ``icons_update_days`` дней (0 — выкл)."""
    cfg = cfg or {}
    return False
    try:
        first_delay = float(first_delay)
    except Exception:
        first_delay = 20.0
    try:
        interval = max(60.0, float(interval))
    except Exception:
        interval = 3600.0

    def worker():
        delay = max(1.0, first_delay)
        while True:
            # ждём порцию, но просыпаемся, чтобы уважать stop_event
            waited = 0.0
            while waited < delay:
                if stop_event is not None and stop_event.is_set():
                    return
                time.sleep(min(5.0, delay - waited))
                waited += 5.0
            if stop_event is not None and stop_event.is_set():
                return
            try:
                days = int(cfg.get("icons_update_days", 7) or 0)
                pack = icon_pack_manager(cfg)
                if days > 0 and pack is not None and not pack.running \
                        and pack.needs_update(days):
                    log(f"Иконки: автообновление (раз в {days} дн.)")
                    result = pack.update(progress=on_progress)
                    _icons_after(result, cfg)
                    if callable(on_done):
                        try:
                            on_done(result)
                        except Exception:
                            pass
            except Exception as e:
                log(f"Иконки: автообновление не удалось "
                    f"({type(e).__name__}: {e})")
            delay = interval

    thread = threading.Thread(target=worker, daemon=True,
                              name="wf-icon-pack-watch")
    thread.start()
    return thread


def login_in_progress():
    """Идёт ли сейчас вход на warframe.market (браузер пользователя)?"""
    mod = login_mode_module()
    if mod is not None:
        try:
            return bool(mod.in_progress())
        except Exception:
            pass
    return bool(_FALLBACK_LOGIN_STATE["in_progress"])


def set_login_in_progress(value):
    """Запомнить «идёт вход»: guard-ы окон/хоткеев читают этот флаг."""
    value = bool(value)
    _FALLBACK_LOGIN_STATE["in_progress"] = value
    mod = login_mode_module()
    if mod is not None:
        try:
            mod.set_in_progress(value)
        except Exception:
            pass
    return value


def close_app_dialogs(keep=()):
    """Закрыть ВСЕ открытые окна-диалоги приложения (B2).

    Вызывается при любом скрытии оверлея (F9/``hide_all``, Esc/``hide_window``,
    скрытие по таймеру). Окна подтверждений, форма ордера, окно токена, окно
    входа закрываются централизованно через ``login_mode`` — новые диалоги
    достаточно зарегистрировать там же, и они попадут сюда автоматически.
    """
    mod = login_mode_module()
    if mod is None:
        return 0
    try:
        return int(mod.close_active_dialogs(keep=keep) or 0)
    except Exception:
        return 0


def register_app_dialog(dialog):
    """Зарегистрировать окно-диалог в общем списке (B2). Возвращает его же."""
    mod = login_mode_module()
    if mod is not None:
        try:
            mod.register_dialog(dialog)
        except Exception:
            pass
    return dialog


def log_login_windows(keep=()):
    """Пишет в лог список окон приложения перед входом (диагностика).

    Строка «Вход: окна приложения — …» нужна, чтобы по жалобе «окно
    перекрывает браузер» было видно, какие окна вообще есть и какие из них
    ``[topmost]``/``[видно]``. В лог попадают **все** окна приложения, включая
    диалог входа и пилюлю: ``keep`` (окна, которые не прячут) принимается для
    совместимости с ``_on_login_state``, но из отчёта ничего не исключает —
    иначе диагностика теряла бы ровно те окна, которые участвуют во входе.
    Возвращает фактическую строку (``""``, если модуль недоступен или
    диагностика не удалась).
    """
    mod = login_mode_module()
    if mod is None:
        log("Вход: login_mode.py не найден — диагностика окон недоступна")
        return ""
    try:
        report = mod.windows_report()
        text = "Вход: окна приложения — " + "; ".join(
            f"{item['class']}({item['title'] or '—'})"
            f"{' [topmost]' if item['topmost'] else ''}"
            f"{' [видно]' if item['visible'] else ''}"
            for item in report)
    except Exception as e:
        log(f"Вход: диагностика окон не удалась ({type(e).__name__}: {e})")
        return ""
    log(text)
    return text


class _FallbackTheme:
    """Минимальная палитра на случай, если ui_theme.py не нашли рядом."""

    PALETTE = {
        "window": "#0d131c", "panel": "#121a25", "card": "#17212e",
        "border": "#24344a", "text": "#e6edf6", "muted": "#93a1b5",
        "accent": "#4aa8ff", "success": "#43d17c", "warning": "#f5c451",
        "danger": "#ff6b6b", "platinum": "#E7F1FF", "ducats": "#C9A66B",
        "currency_platinum": "#E7F1FF", "currency_ducats": "#C9A66B",
        "currency_credits": "#9BC7A8",
        "hover": "#1e2a3a", "scroll": "#2a3a4e",
        "zebra": "rgba(255,255,255,0.03)", "grid": "#1f2b3a",
        "area": "rgba(74,168,255,0.18)",
    }
    FONT = "Segoe UI"
    FONT_SIZES = {"window": 14, "section": 11, "text": 12, "detail": 10,
                  "price": 13, "button": 11}

    @classmethod
    def size(cls, kind, default=12):
        return cls.FONT_SIZES.get(str(kind), default)

    @classmethod
    def rgba(cls, color, alpha):
        text = str(color or "").lstrip("#")
        try:
            r, g, b = (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
        except Exception:
            r, g, b = 13, 19, 28
        return f"rgba({r}, {g}, {b}, {float(alpha):.2f})"

    @classmethod
    def window_qss(cls, name="win", radius=8):
        p = cls.PALETTE
        return (f"QFrame#{name} {{ background-color: {cls.rgba(p['window'], 0.96)};"
                f" border: 1px solid {p['border']}; border-radius: {radius}px; }}"
                " QLabel { background: transparent; color: #e6edf6; }")

    @classmethod
    def panel_qss(cls, name="panel", radius=6):
        p = cls.PALETTE
        return (f"QFrame#{name} {{ background-color: {p['panel']};"
                f" border: 1px solid {p['border']}; border-radius: {radius}px; }}")

    @classmethod
    def section_qss(cls, accent=None):
        return f"color:{accent or cls.PALETTE['muted']}; font-size:11px; font-weight:700;"

    @classmethod
    def divider_qss(cls):
        return f"background-color:{cls.rgba(cls.PALETTE['muted'], 0.18)}; border:none;"

    @classmethod
    def row_qss(cls, zebra=False, selected=False, radius=4):
        p = cls.PALETTE
        background = p["hover"] if selected else (p["zebra"] if zebra else p["card"])
        return (f"QFrame#mRow {{ background-color:{background};"
                f" border-radius:{radius}px; border:1px solid transparent; }}"
                f"QFrame#mRow:hover {{ background-color:{p['hover']}; }}")

    @classmethod
    def badge_qss(cls, background, foreground="#ffffff", radius=3, small=True):
        return (f"background-color:{background}; color:{foreground};"
                f" border-radius:{radius}px; padding:0px 6px; font-size:9px;"
                " font-weight:600;")

    @classmethod
    def pill_qss(cls, color, radius=8):
        return (f"color:{color}; background-color:rgba(13,19,28,0.75);"
                f" border:1px solid #24344a; border-radius:{radius}px; padding:0px 7px;")

    @classmethod
    def button_qss(cls, primary=False, danger=False, small=False):
        p = cls.PALETTE
        padding = "2px 8px" if small else "5px 12px"
        return (f"QPushButton {{ color:{p['text']}; background-color:{p['card']};"
                f" border:1px solid {p['border']}; border-radius:4px;"
                f" padding:{padding}; font-size:11px; }}"
                f"QPushButton:hover {{ background-color:{p['hover']};"
                f" border-color:{p['accent']}; }}")

    @classmethod
    def dialog_qss(cls):
        return cls.window_qss("win", 8) + cls.button_qss()

    @classmethod
    def input_qss(cls):
        return ("QLineEdit, QSpinBox, QComboBox { background:#121a25;"
                " color:#e6edf6; border:1px solid #24344a; border-radius:6px;"
                " padding:6px 8px; }")

    @classmethod
    def scroll_qss(cls):
        return ("QScrollBar:vertical { background:transparent; width:8px; }"
                "QScrollBar::handle:vertical { background:#2a3a4e;"
                " border-radius:4px; min-height:24px; }"
                "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical"
                " { height:0px; }")

    @classmethod
    def progress_qss(cls, color):
        return ("QProgressBar { background:rgba(147,161,181,0.12); border:none;"
                f" border-radius:3px; }} QProgressBar::chunk {{ background:{color};"
                " border-radius:3px; }")

    @classmethod
    def status_qss(cls, stale=False):
        return f"color:{cls.PALETTE['warning'] if stale else cls.PALETTE['muted']};"

    @classmethod
    def muted_qss(cls, px=None):
        return f"color:{cls.PALETTE['muted']};"

    @classmethod
    def price_qss(cls, color=None):
        return f"color:{color or cls.PALETTE['platinum']}; font-size:13px; font-weight:700;"

    @classmethod
    def currency_qss(cls, kind="platinum", px=None):
        color = (cls.PALETTE.get("currency_ducats")
                 if str(kind) == "ducats" else
                 cls.PALETTE.get("currency_platinum"))
        size = f" font-size:{int(px)}px;" if px else ""
        return f"color:{color};{size} font-weight:700; background: transparent;"

    @staticmethod
    def currency_tooltip(kind="platinum"):
        return ("Дукаты (warframe.market)" if str(kind) == "ducats"
                else "Платина (warframe.market)")


def ui_theme():
    """Модуль дизайн-системы или запасная палитра (без падения)."""
    return ui_theme_module() or _FallbackTheme


def market_token_masked(token):
    """Безопасное представление токена для UI/консоли (сам токен не печатаем)."""
    token = str(token or "").strip()
    if not token:
        return "нет токена"
    tail = token[-4:] if len(token) >= 8 else ""
    return f"••••{tail}" if tail else "••••"


def order_type_of(order):
    """Тип ордера из ответа API: ``sell`` или ``buy``."""
    return str((order or {}).get("type") or "sell").lower()


def save_market_token(cfg, token, path=CONFIG_PATH):
    """Сохраняет JWT warframe.market в config.json (ключ ``market_token``).

    Токен никогда не печатается: в консоль уходит только факт сохранения.
    Существующий конфиг перечитывается, чтобы не потерять настройки
    пользователя; запись атомарная (tmp + os.replace). В памяти ``cfg``
    значение тоже обновляется.
    """
    token = str(token or "").strip()
    cfg["market_token"] = token
    raw = {}
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raw = {}
    except Exception as e:
        log(f"  токен: конфиг не прочитан ({type(e).__name__}), перепишу заново")
        raw = {}
    raw["market_token"] = token
    try:
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        log("  токен warframe.market сохранён в config.json")
        return True
    except Exception as e:
        log(f"  токен не сохранён ({type(e).__name__}: {e})")
        return False


def save_config_keys(cfg, values, path=CONFIG_PATH):
    """Аккуратно сохраняет несколько ключей в config.json (без потери старых).

    Используется единым окном для ``last_tab``, ``window_geometry`` и
    ``window_size``. Ничего секретного сюда не передаётся; токен пишется
    отдельно через :func:`save_market_token` и не логируется.
    """
    values = dict(values or {})
    raw = {}
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raw = {}
    except Exception as e:
        log(f"  конфиг не прочитан ({type(e).__name__}), перепишу заново")
        raw = {}
    raw.update(values)
    try:
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        cfg.update(values)
        return True
    except Exception as e:
        log(f"  конфиг не сохранён ({type(e).__name__}: {e})")
        return False


def log_settings(cfg):
    """Настройки лог-пайплайна с поддержкой старых имён ключей (``ee_log_*``)."""
    cfg = cfg or {}

    def pick(*names, default=None):
        for n in names:
            v = cfg.get(n)
            if v is not None:
                return v
        return default

    try:
        poll = int(pick("log_poll_ms", "ee_log_poll_ms", default=500) or 500)
    except Exception:
        poll = 500
    return {
        "enabled": bool(pick("log_enabled", "ee_log_enabled", default=True)),
        "path": str(pick("log_path", "ee_log_path", default="") or ""),
        "auto_detect": bool(pick("log_auto_detect", default=True)),
        "poll_ms": max(100, poll),
        "to_journal": bool(pick("log_drop_to_relics_log", default=True)),
        "auto_select": bool(pick("log_auto_select_own", default=False)),
    }


def log_drop_item(cfg, api, event):
    """Собственный дроп из EE.log → запись с ценой (без OCR и без плашек).

    ``event`` — событие ``log_parser`` (``{"kind": "drop", "path": ...}``)
    или просто строка пути. Возвращает запись предмета либо ``None``.
    """
    if isinstance(event, dict):
        path = event.get("path") or ""
        log_time = event.get("time")
        player_id = event.get("player_id")
    else:
        path, log_time, player_id = str(event or ""), None, None
    path = path.strip()
    if not path:
        return None
    logp = log_parser_module()
    if logp is None:
        log("  лог: модуль log_parser не найден — разбор EE.log недоступен")
        return None

    name = logp.display_name_from_path(path)
    slug = None
    try:
        slug = logp.slug_from_path(path, api)
    except Exception as e:
        log(f"  лог: путь не сопоставлен ({type(e).__name__}: {e})")
    if not slug:
        log(f"  лог: предмет не найден на рынке по пути {path}")
        log(f"        (путь всё равно записан в журнал: {name})")

    item = None
    if slug:
        try:
            item = api.lookup_slug(slug, name=name)
        except Exception as e:
            log(f"  лог: цена не получена для {slug} ({type(e).__name__}: {e})")
    if item is None:
        item = {"name": name, "market_name": None, "slug": slug, "price": None,
                "ducats": None, "sell_online": 0, "matched": bool(slug),
                "error": None if slug else "путь не сопоставлен"}
    item = dict(item)
    item["source"] = "log"
    item["game_path"] = path
    item["log_time"] = log_time
    item["player_id"] = player_id

    # ── консоль: что именно выпало и сколько стоит ─────────────────────
    log(f"  Лог EE.log: собственная награда — {item.get('market_name') or name}")
    log(f"    путь: {path}")
    if slug:
        log(f"    слаг: {slug}")
    price = item.get("price")
    extra = []
    if item.get("ducats"):
        extra.append(f"дук {item['ducats']}")
    if item.get("sell_online"):
        extra.append(f"{item['sell_online']} онлайн")
    tail = ("  |  " + "  |  ".join(extra)) if extra else ""
    log(f"    цена: {fmt_plat(price)}{tail}")
    return item


def append_log_drop(cfg, api, event):
    """Разбирает событие лога и дописывает собственный дроп в журнал реликвий."""
    item = log_drop_item(cfg, api, event)
    if item is None:
        return None
    if log_settings(cfg)["to_journal"]:
        append_relic_block(cfg, [item], title="Награда (лог)")
    return item


def start_log_tailer(cfg, on_event):
    """Запускает фоновое чтение EE.log. Возвращает поток или ``None``.

    Отсутствие лога/модуля не ошибка: приложение работает как раньше,
    пользователь видит одно предупреждение в консоли.
    """
    settings = log_settings(cfg)
    if not settings["enabled"]:
        log("EE.log: чтение лога выключено в конфиге (log_enabled=false).")
        return None
    logp = log_parser_module()
    if logp is None:
        log("EE.log: модуль log_parser.py не найден — лог не читаю, Alt+Q работает.")
        return None
    try:
        path = logp.resolve_log_path(cfg)
    except Exception as e:
        log(f"EE.log: путь не определён ({e}) — лог не читаю.")
        return None
    if not path:
        log("EE.log: путь не задан и автопоиск выключен (log_auto_detect=false).")
        return None
    if not os.path.isfile(path):
        log(f"EE.log не найден: {path}")
        log("  лог будет подхвачен, как только игра его создаст; "
            "Alt+Q и журнал работают как раньше.")
    else:
        log(f"EE.log: {path} (только чтение)")
    try:
        tailer = logp.EeLogTailer(path, on_event=on_event,
                                  poll_ms=settings["poll_ms"],
                                  start_at_end=True)
        tailer.start()
        return tailer
    except Exception as e:
        log(f"EE.log: хвост лога не запущен ({type(e).__name__}: {e})")
        return None


def detect_selected_slot(full_img, lefts, cfg):
    """Ищет карточку с золотой рамкой (наведение/выбор). Индекс или None."""
    import cv2
    import numpy as np
    if not lefts:
        return None
    img = (cv2.cvtColor(full_img, cv2.COLOR_BGRA2BGR)
           if full_img.ndim == 3 and full_img.shape[2] == 4 else full_img)
    sx = img.shape[1] / float(cfg["base_width"])
    sy = img.shape[0] / float(cfg["base_height"])
    h_img, w_img = img.shape[:2]
    best, best_score = None, 0.0
    for i, left in enumerate(lefts):
        x0, y0 = int(left * sx), int(cfg["card_top"] * sy)
        w, h = int(cfg["card_width"] * sx), int(cfg["card_height"] * sy)
        if x0 < 0 or y0 < 0 or x0 + w > w_img or y0 + h > h_img:
            continue
        strips = [img[y0 + 1:y0 + 4, x0 + 4:x0 + w - 4],
                  img[y0 + h - 4:y0 + h - 1, x0 + 4:x0 + w - 4],
                  img[y0 + 4:y0 + h - 4, x0 + 1:x0 + 4],
                  img[y0 + 4:y0 + h - 4, x0 + w - 4:x0 + w - 1]]
        px = np.concatenate([s.reshape(-1, 3) for s in strips if s.size])
        if not px.size:
            continue
        b, g, r = px[:, 0].astype(int), px[:, 1].astype(int), px[:, 2].astype(int)
        mask = (r > 140) & (g > 110) & (b < 120) & (r > b + 40)
        score = float(mask.mean())
        if score > best_score:
            best, best_score = i, score
    return best if best_score >= 0.02 else None


class ScreenWatcher(threading.Thread):
    """Следит за экраном наград и сообщает об открытии/закрытии.

    Сравнивает маленькую полосу с заголовком («VOID FISSURE / REWARDS»)
    с сохранёнными шаблонами. Если шаблона для текущего языка нет —
    он запоминается при первом успешном скане.
    """

    def __init__(self, cfg, mon, on_open, on_close):
        super().__init__(daemon=True, name="wf-watcher")
        self.cfg = cfg
        self.mon = mon
        self.on_open = on_open
        self.on_close = on_close
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._templates = load_templates(cfg)
        self.visible = False

    def stop(self):
        self._stop.set()

    def templates_count(self):
        with self._lock:
            return len(self._templates)

    def learn(self, full_img):
        """Запоминает заголовок экрана (другой язык интерфейса)."""
        import cv2
        try:
            gray = header_gray(full_img, self.mon, self.cfg)
            if gray is None:
                return
            with self._lock:
                for t in self._templates:
                    if t.shape != gray.shape:
                        t = cv2.resize(t, (gray.shape[1], gray.shape[0]))
                    if float(cv2.matchTemplate(gray, t, cv2.TM_CCOEFF_NORMED).max()) >= 0.9:
                        return
                if len(self._templates) >= 6:
                    return
                save_template(self.cfg, gray)
                self._templates.append(gray)
            log("  Запомнил шаблон экрана наград (авто-режим).")
        except Exception as e:
            log(f"  шаблон не сохранён: {e}")

    def _visible_now(self, sct):
        import cv2
        import numpy as np
        if not self._templates:
            return False
        shot = np.array(sct.grab(_header_box(self.mon, self.cfg)))
        if shot.ndim == 3 and shot.shape[2] == 4:
            gray = cv2.cvtColor(shot, cv2.COLOR_BGRA2GRAY)
        elif shot.ndim == 3:
            gray = cv2.cvtColor(shot, cv2.COLOR_BGR2GRAY)
        else:
            gray = shot
        thr = float(self.cfg.get("detect_threshold", 0.85))
        for t in self._templates:
            if t.shape != gray.shape:
                # другое разрешение экрана — масштабируем шаблон под полосу
                t = cv2.resize(t, (gray.shape[1], gray.shape[0]))
            if float(cv2.matchTemplate(gray, t, cv2.TM_CCOEFF_NORMED).max()) >= thr:
                return True
        return False

    def _track_selection(self, sct):
        """Больше не угадываем выбор: пользователь добавляет его сам."""
        return

    def run(self):
        interval = max(120, int(self.cfg.get("auto_poll_ms", 400))) / 1000.0
        need = max(1, int(self.cfg.get("auto_confirm", 2)))
        try:
            with _mss_factory()() as sct:
                hits = misses = 0
                while not self._stop.is_set():
                    try:
                        vis = self._visible_now(sct)
                    except Exception:
                        vis = False
                    if vis:
                        hits, misses = hits + 1, 0
                        self._track_selection(sct)
                    else:
                        misses, hits = misses + 1, 0
                    if not self.visible and hits >= need:
                        self.visible = True
                        try:
                            self.on_open()
                        except Exception:
                            pass
                    elif self.visible and misses >= need:
                        self.visible = False
                        try:
                            self.on_close()
                        except Exception:
                            pass
                    self._stop.wait(interval)
        except Exception as e:
            log(f"авто-режим остановлен: {e}")


# ─────────────────────────────────────────────────────────────────────────
#  Скан
# ─────────────────────────────────────────────────────────────────────────
class Scanner:
    def __init__(self, cfg, ocr, api, manager, geom):
        self.cfg = cfg
        self.ocr = ocr
        self.api = api
        self.mgr = manager
        self.geom = geom
        self._lock = threading.Lock()
        self.busy = False
        self.generation = 0
        self._pending_scan = False
        self.watcher = None      # авто-режим: учит шаблон экрана
        self.last_results = None
        # 1.0.0: кэш авто-калибровки на сессию (не пересчитываем каждый скан)
        self._calib_cache = {}
        self._calib_logged = False
        # 1.0.0: UI scale игры и тема интерфейса (данные WFInfo)
        self._ui_scale, self._ui_source = warframe_ui_scale(cfg)
        self._theme_checked = False
        self.detected_theme = {}
        self._init_wfinfo_geometry()
        try:
            import scan_cache
            self.cache = scan_cache.ScanCache(
                price_ttl=float(cfg.get("scan_price_ttl", 600) or 600))
        except Exception:
            self.cache = None

    def _init_wfinfo_geometry(self):
        """Сессия: подставить точные цифры WFInfo (×UI scale), если нет
        сохранённой калибровки t4. Старые конфиги при этом не сбрасываются."""
        cfg = self.cfg
        mod = scan_calibrate_module()
        saved = None
        if mod is not None:
            try:
                saved = mod.normalize_calibration(cfg.get("scan_calibration"))
            except Exception:
                saved = None
        if saved is not None:
            return False
        values = apply_wfinfo_geometry(cfg, self._ui_scale)
        if not values:
            return False
        geometry = wfinfo_geometry(self._ui_scale, cfg)
        if geometry:
            log(scan_calibration_line(geometry, "wfinfo", self.geom))
        return True

    def _apply_scan_calibration(self, info):
        """Авто-геометрия подтверждена: фиксируем в cfg на сессию + лог."""
        info = dict(info or {})
        if not info.get("confirmed"):
            return False
        geometry = dict(info.get("geometry") or {})
        if not geometry.get("lefts"):
            return False
        source = str(info.get("source") or "авто")
        if source != "авто":
            # встроенные цифры WFInfo уже применены на старте сессии
            return False
        mod = scan_calibrate_module()
        if mod is None:
            return False
        try:
            applied = mod.apply_geometry(self.cfg, geometry)
        except Exception as e:
            log(f"Калибровка: не применилась ({type(e).__name__}: {e})")
            return False
        if not applied:
            return False
        if not self._calib_logged:
            self._calib_logged = True
            log(scan_calibration_line(geometry, "авто", self.geom))
        return True

    def attach_ocr(self, engine):
        self.ocr = engine
        with self._lock:
            pending = self._pending_scan
        if pending:
            log("OCR: готов — выполняю отложенный Alt+Q")
            self.request_scan()

    def request_scan(self):
        if self.ocr is None:
            with self._lock:
                self._pending_scan = True
            log("Alt+Q: готовлю распознавание… (модель ещё загружается)")
            return
        with self._lock:
            if self.busy:
                if not self._pending_scan:
                    self._pending_scan = True
                    log("Alt+Q: скан уже идёт, повторю после него")
                # повторные нажатия не спамят лог
                return
            self.busy = True
            self._pending_scan = False
            self.generation += 1
            gen = self.generation
        threading.Thread(target=self._run, args=(gen,), daemon=True).start()

    def _drain_pending_scan(self):
        """После текущего скана — ровно один отложенный (если был)."""
        with self._lock:
            pending = self._pending_scan
            self._pending_scan = False
        if pending:
            log("  Скан: выполняю отложенный запрос (1)")
            self.request_scan()
            return True
        return False

    def _run(self, gen):
        t0 = time.time()
        perf = {"capture": 0.0, "ocr": 0.0, "match": 0.0,
                "prices": 0.0, "render": 0.0}
        log("\n" + "─" * 60)
        log(f"Скан {datetime.datetime.now().strftime('%H:%M:%S')}")
        try:
            # экран наград может быть ещё в анимации — пробуем несколько раз
            attempts = max(1, int(self.cfg.get("scan_retries", 2)) + 1)
            delay = max(0, int(self.cfg.get("scan_retry_delay_ms", 350))) / 1000.0
            self.api.load_items()

            def matcher(name):
                cached = self.cache.match_get(name) if self.cache else None
                if cached is not None:
                    return cached
                started = time.time()
                score = (self.api.match(name) or {}).get("score", 0.0)
                perf["match"] += time.time() - started
                if self.cache:
                    self.cache.match_set(name, score)
                return score

            cards = []
            owned = []
            calib_info = {}
            for attempt in range(attempts):
                started = time.time()
                full, mon = grab(self.cfg)
                base = to_base(full, self.cfg)
                perf["capture"] += time.time() - started
                sx = float(self.geom.get("sx") or 1.0)
                sy = float(self.geom.get("sy") or 1.0)
                # 1.0.0: тема интерфейса — один раз за сессию (данные WFInfo)
                if not self._theme_checked:
                    self._theme_checked = True
                    try:
                        self.detected_theme = detect_game_theme(
                            self.cfg, base, ui_scale=self._ui_scale)
                    except Exception as e:
                        log(f"Тема: не определена "
                            f"({type(e).__name__}: {e})")
                if self.cfg.get("show_debug"):
                    try:
                        imwrite_unicode(os.path.join(self.cfg["debug_dir"],
                                                     "last_screen.png"), base)
                    except Exception:
                        pass
                started = time.time()
                cards = detect_cards(base, self.cfg, self.ocr,
                                     debug=self.cfg.get("show_debug"),
                                     matcher=matcher, cache=self.cache,
                                     calib_cache=self._calib_cache,
                                     calib_out=calib_info)
                perf["ocr"] += time.time() - started
                if cards or attempt == attempts - 1:
                    break
                log(f"  Карточек не видно — переснимаю через {int(delay * 1000)} мс")
                time.sleep(delay)
            # авто-геометрия подтверждена — фиксируем на сессию (и логируем)
            self._apply_scan_calibration(calib_info)

            # ── фаза 1: имена сразу (метки Owned читаем позже) ─────────
            preliminary = []
            for idx, c in enumerate(cards):
                n, kind, _raw = (None, None, "")
                label = ""
                log(f"  · {c['name']!r}   {label or '—'}")
                preliminary.append({
                    "name": c["name"], "owned": label, "owned_n": 0,
                    "owned_kind": kind, "card_left": c["left"],
                    "price": None, "pending": True,
                    "unrecognized": bool(c.get("unrecognized")
                                         or not c.get("name")),
                    "matched": False, "sell_online": 0, "best_sell": None,
                    "best_buy": None, "ducats": None, "error": None,
                })
            if gen == self.generation:
                started = time.time()
                self._plates_ms = (time.time() - t0) * 1000
                self.mgr.updated.emit(preliminary)
                perf["render"] += time.time() - started
            if not preliminary:
                log("  Предметы не найдены — проверьте координаты (--calibrate)")
                self.mgr.updated.emit([])
                return

            # метки Crafted / N Owned — батчем после ранних плашек
            started = time.time()
            try:
                owned = read_owned_batch(
                    full, sx, sy, cards, self.cfg, self.ocr,
                    cache=self.cache,
                    origin=(self.geom.get("ox", 0), self.geom.get("oy", 0)))
            except Exception as exc:
                log(f"  OCR меток: батч не удался "
                    f"({type(exc).__name__}), поштучно")
                owned = [read_owned(full, sx, sy, card, self.cfg, self.ocr,
                                    origin=(self.geom.get("ox", 0),
                                            self.geom.get("oy", 0)))
                         for card in cards]
            perf["ocr"] += time.time() - started
            for idx, c in enumerate(preliminary):
                n, kind, _raw = (owned[idx] if idx < len(owned)
                                 else (None, None, ""))
                if kind == "CRAFTED":
                    label = "ИЗГОТОВЛЕНО"
                elif n:
                    label = f"{n} В НАЛИЧИИ"
                else:
                    label = ""
                c["owned"] = label
                c["owned_n"] = n or 0
                c["owned_kind"] = kind

            # ── фаза 2: цены параллельно, дубликаты объединяем ────────
            results = [None] * len(preliminary)
            unique = {}
            for idx, p in enumerate(preliminary):
                unique.setdefault(p["name"], []).append(idx)
            t_api = time.time()
            resolved = resolve_prices_parallel(
                self.api, list(unique.keys()), cache=self.cache,
                workers=int(self.cfg.get("workers", 4) or 4),
                timeout=2.5, logger=log)
            for name, indexes in unique.items():
                value = resolved.get(name) or {
                    "name": name, "price": None, "matched": False,
                    "error": "нет цены"}
                for idx in indexes:
                    results[idx] = dict(value)
            perf["prices"] = time.time() - t_api

            final = []
            for p, r in zip(preliminary, results):
                d = dict(p)
                d.update(r or {})
                d["pending"] = False
                d["owned"] = p["owned"]
                d["owned_n"] = p["owned_n"]
                d["card_left"] = p["card_left"]
                if gen == self.generation:
                    final.append(d)

            if gen != self.generation:
                return

            # 1.0.0: метрика OCR — видно эффект словаря на чужих ПК
            try:
                recognized = sum(1 for c in cards
                                 if not c.get("unrecognized"))
                corrected_n = sum(1 for c in cards if c.get("ocr_corrected"))
                matched_n = sum(1 for d in final if d.get("matched"))
                log(f"OCR: распознано {recognized}/4, "
                    f"скорректировано {corrected_n}, "
                    f"матч {matched_n}/4")
            except Exception:
                pass

            # цена комплекта для карточек-частей (в «Итого» не входит)
            for d in final:
                if not self.cfg.get("overlay_show_set_price", True):
                    continue
                slug = str(d.get("slug") or "")
                set_slug = set_slug_for_part(slug, d.get("name") or "")
                d["set_slug"] = set_slug
                d["set_price"] = None
                if not set_slug:
                    continue
                lookup_set = getattr(self.api, "lookup_slug", None)
                if not callable(lookup_set):
                    continue
                try:
                    info = lookup_set(set_slug)
                except Exception as exc:
                    log(f"  API комплект {set_slug}: "
                        f"{type(exc).__name__}")
                    continue
                if isinstance(info, dict) and info.get("price") is not None:
                    d["set_price"] = info.get("price")
                    d["set_name"] = (info.get("market_name")
                                     or info.get("name") or set_slug)
                    d["set_updated"] = time.time()
                    d["set_online"] = bool(info.get("sell_online"))
                    d["set_offline"] = bool(info.get("sell_offline"))
                    d["set_status"] = ("онлайн" if d["set_online"]
                                       else ("офлайн" if d["set_offline"]
                                             else "—"))
                    client = getattr(
                        getattr(getattr(self, "mgr", None),
                                "market_browser", None), "client", None)
                    if client is not None:
                        detail = lowest_sell_order(client, set_slug)
                        if detail.get("seller"):
                            d["set_seller"] = detail["seller"]
                            d["set_online"] = bool(detail.get("online"))
                            d["set_status"] = ("онлайн" if d["set_online"]
                                               else "офлайн")

            # подозрительно низкая цена комплекта: сравнить с частями сета
            parts_by_set = {}
            for d in final:
                set_slug = str(d.get("set_slug") or "")
                price = d.get("price")
                if set_slug and isinstance(price, (int, float)) and price:
                    parts_by_set.setdefault(set_slug, []).append(int(price))
            for d in final:
                set_slug = str(d.get("set_slug") or "")
                if not set_slug or d.get("set_price") is None:
                    continue
                check = assess_set_price(
                    d.get("price"), d.get("set_price"),
                    parts_by_set.get(set_slug) or [])
                d["set_suspicious"] = check["suspicious"]
                d["set_suspect_reason"] = check["reason"]
                d["set_parts_sum"] = check["parts_sum"]
                d["set_parts_count"] = check["parts_count"]

            started = time.time()
            self.mgr.updated.emit(final)
            perf["render"] += time.time() - started
            self._write_report(final)
            self._maybe_beep(final)
            if self.watcher is not None:
                self.watcher.learn(full)
            # выбор пользователь добавляет сам (кнопка «+ добавить» в панели),
            # здесь только запоминаем награды этой реликвии для меню
            self.last_results = final
            self.mgr.last_results = final
            append_relic_block(self.cfg, final)
            ocr_ms = max(0.0, perf["ocr"] - perf["match"]) * 1000
            log("scan: capture={:.0f} ocr={:.0f} match={:.0f} "
                "prices={:.0f} total={:.0f}".format(
                    perf["capture"] * 1000, ocr_ms,
                    perf["match"] * 1000, perf["prices"] * 1000,
                    (time.time() - t0) * 1000))
            log("Скан: capture={:.0f} ocr={:.0f} match={:.0f} "
                "prices={:.0f} render={:.0f} total={:.0f}".format(
                    perf["capture"] * 1000, ocr_ms,
                    perf["match"] * 1000, perf["prices"] * 1000,
                    perf["render"] * 1000, (time.time() - t0) * 1000))
            log(f"Alt+Q: плашки за {getattr(self, '_plates_ms', 0):.0f} мс, "
                f"цены за {perf['prices'] * 1000:.0f} мс")
            log(f"  Готово за {time.time() - t0:.1f} с")
        except Exception as e:
            log(f"ОШИБКА СКАНА: {type(e).__name__}: {e}")
            log_exception("Скан: подробности")
        finally:
            with self._lock:
                self.busy = False
            log("─" * 60 + "\n")
            self._drain_pending_scan()

    def _write_report(self, data):
        path = os.path.join(DATA_DIR, "last_rewards.txt")
        try:
            total = sum(d["price"] for d in data
                        if isinstance(d.get("price"), (int, float)) and d["price"])
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"=== {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n\n")
                for d in data:
                    f.write(f"{d.get('market_name') or d.get('name')} — "
                            f"{fmt_plat(d.get('price'))}  | S:{fmt_plat(d.get('best_sell'))} "
                            f"B:{fmt_plat(d.get('best_buy'))} | {d.get('owned', '')}\n")
                f.write(f"\nИТОГО: {int(total)} платины\n")
        except Exception as e:
            log(f"  last_rewards.txt не записан: {e}")

    def _maybe_beep(self, data):
        if not self.cfg.get("play_sound"):
            return
        try:
            threshold = float(self.cfg.get("alert_threshold", 100))
            if any(isinstance(d.get("price"), (int, float)) and d["price"] >= threshold
                   for d in data):
                import winsound
                winsound.Beep(1100, 140)
                winsound.Beep(1500, 140)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────
#  Режимы: GUI / калибровка / самопроверка
# ─────────────────────────────────────────────────────────────────────────
def make_geometry(cfg):
    """Геометрия монитора: равномерный масштаб ``min(W/1920, H/1080)`` (1.0.0).

    ``sx/sy`` — масштаб базы (было растяжение W/1920, H/1080), ``ox/oy`` —
    экранное смещение окна 1920×1080 внутри кадра (для 16:9 = 0). Плашки и
    «Итого» ставятся как ``x_base * sx + ox``.
    """
    mon = monitor_info(cfg)
    base_w = int(cfg["base_width"])
    base_h = int(cfg["base_height"])
    mod = reward_geometry_module()
    if mod is not None:
        try:
            sc, x0, y0 = mod.base_origin(mon["width"], mon["height"],
                                         base_w, base_h)
        except Exception as e:
            log(f"Геометрия экрана: масштаб не посчитан "
                f"({type(e).__name__}: {e})")
            sc = min(mon["width"] / float(base_w),
                     mon["height"] / float(base_h))
            x0 = y0 = 0
    else:
        sc = min(mon["width"] / float(base_w), mon["height"] / float(base_h))
        x0 = y0 = 0
    return {
        "sx": sc,
        "sy": sc,
        "ox": int(round(x0 * sc)),
        "oy": int(round(y0 * sc)),
        "sc": sc,
        "width": mon["width"],
        "height": mon["height"],
        "monitor": mon,
    }


_STARTUP = {"t0": _MODULE_TS, "catalog_ms": 0.0, "gui_ms": 0.0,
            "total_ms": 0.0, "import_ms": 0.0, "ocr_ms": 0.0,
            "ocr_ready": False, "ocr_logged": False}


def run_gui(cfg):
    _start = time.time()
    _STARTUP["t0"] = _MODULE_TS
    _STARTUP["import_ms"] = (_start - _MODULE_TS) * 1000
    # 0.6.4: цвета валют из config до создания плашек/панелей
    try:
        ui_theme().configure_currency_colors(cfg)
    except Exception:
        pass
    # Режим входа (login_mode.py) грузим первым: его общий флаг читают guard-ы
    # окон/хоткеев/плашек, а overlay_window.py и market_browser_login.py
    # импортируют его по имени (в сборке — через hiddenimports).
    login_mode_module()
    # 1.0.0 (t60): Reader создаётся лениво/в фоне — окно раньше OCR.
    ocr = None
    _, _, Manager, Qt = build_overlays()
    from PyQt5.QtCore import QObject, pyqtSignal
    from PyQt5.QtWidgets import QApplication
    import keyboard

    # Все действия с виджетами — ТОЛЬКО через сигналы: хоткеи и наблюдатель
    # работают в своих потоках, а Qt требует GUI-поток. Иначе окно зависает
    # и курсор превращается в «крутящийся».
    class _UiBridge(QObject):
        toggle_selected = pyqtSignal()
        toggle_events = pyqtSignal()
        market_toggle = pyqtSignal()
        window_toggle = pyqtSignal()
        scan = pyqtSignal()
        hide_all = pyqtSignal()
        screen_closed = pyqtSignal()
        events_updated = pyqtSignal()     # снимок событий обновлён (фоновый поток)
        log_selected = pyqtSignal(dict)   # собственный дроп из лога → «Выбрано»
        notification = pyqtSignal(str, str, str, bool)  # тост Windows (фон → GUI)
        notification_full = pyqtSignal(dict)  # центр уведомлений: запись целиком
        pick_reward = pyqtSignal(int)     # Alt+1…4: взята награда из скана
        stats_changed = pyqtSignal()      # 1.0.0: «Дроп» — данные изменились

    # ── вход через браузер: готовим Qt ДО QApplication (только webview) ──
    # Основной способ входа — браузер пользователя по CDP (Chromium 87 не
    # проходит Cloudflare). Встроенный QtWebEngine оставлен отключаемым
    # запасным вариантом (market_web_login=true) и требует
    # Qt.AA_ShareOpenGLContexts до создания QApplication; без него атрибут
    # не нужен, поэтому трогаем Qt только когда webview включён.
    if cfg.get("market_web_login", False):
        auth_mod_prep = market_auth_module()
        if auth_mod_prep is not None:
            try:
                auth_mod_prep.prepare_qt_for_webengine()
            except Exception:
                pass

    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    # 1.0.0: иконка приложения из assets/brand/icon.ico
    try:
        brand_icon = app_icon()
        if brand_icon is not None:
            app.setWindowIcon(brand_icon)
    except Exception:
        pass
    geom = make_geometry(cfg)
    # 1.0.0: миграция статуса — убираем устаревший авто-режим из старых config
    try:
        status_mod = market_status_module()
        if status_mod is not None:
            changed = status_mod.migrate_status_config(cfg) or {}
            if changed:
                save_config_keys(cfg, changed)
                log("Статус: авто-режим убран, переключено на «Онлайн» "
                    f"({', '.join(sorted(changed))})")
    except Exception as e:
        log(f"Статус: миграция не выполнена ({type(e).__name__}: {e})")
    # 1.0.0: UI scale игры (EE.cfg) и сохранённая калибровка скана — до первого скана
    try:
        log_ui_scale(cfg)
    except Exception as e:
        log(f"Масштаб интерфейса: не прочитан ({type(e).__name__}: {e})")
    try:
        init_scan_calibration(cfg, geom)
    except Exception as e:
        log(f"Калибровка: старт не применил сохранённую запись "
            f"({type(e).__name__}: {e})")
    # чат создаётся позже; _on_login_state должен видеть это имя
    chat_service = None
    chat_widget = None
    status_service = None
    api = MarketAPI(cfg)
    _t_catalog = time.time()
    api.load_items()
    _STARTUP["catalog_ms"] = (time.time() - _t_catalog) * 1000
    api.refresh_items_async()
    ui = _UiBridge()

    # Модули проверяем заранее: единое окно собирается только если есть все
    # три панели (редактор, маркет, события) и клиент рынка.
    browser_mod = market_browser_module()
    events_mod = events_api_module()
    unified_mod = overlay_window_module()

    # ── продажа на warframe.market: клиент и токен (без сети до клика) ──
    market = None
    market_mod = market_client_module()
    if market_mod is not None:
        try:
            market = market_mod.MarketOrderClient(cfg)
        except Exception as e:
            log(f"Продажа: клиент не создан ({type(e).__name__}: {e})")
            market = None

    # ── статистика фарма и пулы реликвий (вкладка «Дроп») ──────────────
    # Модули опциональны: без них единое окно остаётся с базовыми вкладками.
    farm_mod = farm_stats_module()
    pool_mod = relic_pool_module()
    ui_mod = farm_stats_ui_module()

    collector = None
    if farm_mod is not None and cfg.get("farm_stats_enabled", True):
        def _drop_slug(path):
            logp = log_parser_module()
            if logp is None:
                return None
            try:
                return logp.slug_from_path(path, api)
            except Exception:
                return None

        def _drop_price(slug):
            try:
                item = api.lookup_slug(slug)
            except Exception:
                return {}
            if not isinstance(item, dict):
                return {}
            return {"price": item.get("price"),
                    "name": item.get("market_name"),
                    "ducats": item.get("ducats"),
                    "tradable": item.get("tradable")}

        try:
            collector = farm_mod.FarmStatsCollector(
                cfg, price_fn=_drop_price, slug_fn=_drop_slug,
                logger=log, data_dir=DATA_DIR,
                on_change=(lambda: ui.stats_changed.emit()))
        except Exception as e:
            log(f"Дроп: сборщик не создан ({type(e).__name__}: {e})")
            collector = None

    pool = None
    if pool_mod is not None:
        name_index = {}
        try:
            for k, v in (getattr(api, "items", None) or {}).items():
                if isinstance(v, dict) and v.get("slug"):
                    name_index[str(k)] = v["slug"]
        except Exception:
            name_index = {}

        def _pool_price(slug):
            if market is None:
                return None
            try:
                return market.price_hint(slug).get("sell")
            except Exception:
                return None

        try:
            pool = pool_mod.RelicPool(cfg, price_fn=_pool_price, logger=log,
                                      data_dir=DATA_DIR, name_index=name_index)
        except Exception as e:
            log(f"Дроп: пулы не созданы ({type(e).__name__}: {e})")
            pool = None

    use_unified = bool(unified_mod is not None and browser_mod is not None
                       and events_mod is not None and market is not None)
    drops_panel = None          # создаётся лениво ниже, только если единое окно
    manager = Manager(cfg, geom, market=market, api=api,
                      embedded_ui=use_unified)
    scanner = Scanner(cfg, ocr, api, manager, geom)

    def _warm_ocr_background():
        started = time.time()
        try:
            engine = OcrEngine(cfg)
            engine._warm()
            scanner.attach_ocr(engine)
            _STARTUP["ocr_ms"] = (time.time() - started) * 1000
            _STARTUP["ocr_ready"] = True
            if not _STARTUP.get("ocr_logged"):
                _STARTUP["ocr_logged"] = True
                log(f"OCR: готов за {time.time() - started:.1f} с (фон)")
        except Exception as e:
            log(f"OCR: прогрев не удался ({type(e).__name__}: {e})")

    _STARTUP["ocr_started"] = time.time()
    threading.Thread(target=_warm_ocr_background, daemon=True,
                     name="wf-ocr-bg").start()
    reset_relic_log(cfg)

    # ── вход на warframe.market: браузер пользователя (CDP) ────────────
    # Ничего не делаем до клика по чипу «🔑»: браузер и сеть не трогаем.
    # Контроллер (проверка /v2/me, сохранение, выход) общий для обоих
    # способов; без модулей входа остаётся ручная вставка токена.
    auth_mod = market_auth_module()
    login_mod = market_browser_login_module()
    auth_ctl = None
    if auth_mod is not None and market is not None:
        try:
            auth_ctl = auth_mod.MarketAuthController(
                cfg, market,
                save_fn=save_market_token,
                data_dir=DATA_DIR,
                store_token=cfg.get("market_store_token", True),
                session_only=cfg.get("market_login_session_only", False),
                logger=log)
        except Exception as e:
            log(f"Вход: контроллер не создан ({type(e).__name__}: {e})")
            auth_ctl = None

    def _on_login_state(entering, keep=None, restore_dialog=False):
        """Вход в браузере пошёл/закончился: спрятать и вернуть окна.

        Вызывается окном входа (``market_browser_login.py``). ``keep`` —
        диалог входа и пилюля «Отменить»: их прятать нельзя, пилюля —
        единственная кнопка отмены, пока главное окно скрыто.
        ``restore_dialog`` — диалог снова показан (ошибка/таймаут/закрытие
        браузера): тогда фокус не переводим никуда, пользователь работает с
        диалогом.

        Код устойчив к старой сборке панелей (обновляется только warframe.py):
        если у единого окна нет enter/exit_login_mode, окно всё равно
        прячется/показывается, просто без снятия topmost у остальных окон.
        """
        entering = bool(entering)
        set_login_in_progress(entering)
        if entering:
            # во время входа чат не держит соединение и не показывает тосты
            if chat_service is not None:
                try:
                    chat_service.stop()
                except Exception:
                    pass
            if status_service is not None:
                try:
                    status_service.stop(restore=False)
                except Exception:
                    pass
        elif not entering and chat_service is not None:
            try:
                chat_service.start()
            except Exception:
                pass
        if not entering and status_service is not None:
            try:
                status_service.start()
            except Exception:
                pass
        if unified is None:
            if entering:
                manager.hide_overlays()
            return
        if entering:
            # плашки/«Итого» topmost — во время входа их не показываем вовсе
            manager.hide_overlays()
            # диагностика: какие окна приложения вообще есть (для лога/жалоб
            # «окно перекрывает браузер»)
            log_login_windows(keep=tuple(keep or ()))
            enter = getattr(unified, "enter_login_mode", None)
            if callable(enter):
                enter(keep=tuple(keep or ()))
            else:                       # старая сборка overlay_window.py
                try:
                    unified.hide()
                except Exception:
                    pass
            return
        exit_mode = getattr(unified, "exit_login_mode", None)
        if callable(exit_mode):
            exit_mode(restore=True)
        else:                           # старая сборка overlay_window.py
            try:
                unified.show_window()
            except Exception:
                pass
        # (в) фокус игры. Окно возвращается видимым, но БЕЗ активации
        # (focus=False: нет activateWindow/focus_editor/force_foreground),
        # поэтому мышь у игры не отбирается. Для штатного завершения входа
        # (успех/отмена) возвращаем фокус игре, как это делает закрытие окна
        # (``restore_game_focus`` в конфиге) — под браузером/окном её и не было.
        # При ошибке диалог показан заново: переводить фокус в игру нельзя.
        if not restore_dialog and cfg.get("restore_game_focus", True):
            try:
                restore_game_focus()
            except Exception:
                pass

    def make_auth_dialog(parent=None):
        """Окно входа: браузер пользователя (CDP) или ручная вставка.

        Если в config.json включён ``market_web_login``, показываем прежнее
        окно со встроенным QtWebEngine (запасной вариант — Cloudflare его
        обычно блокирует), иначе — выбор «Войти в браузере / Вставить
        токен вручную».
        """
        manual = lambda p: build_market_dialogs()[1](
            cfg, market, p, save_fn=save_market_token)
        if (cfg.get("market_web_login", False) and auth_mod is not None
                and auth_ctl is not None):
            AuthDialog = auth_mod.build_auth_dialog()
            return AuthDialog(auth_ctl, parent, manual_factory=manual)
        if login_mod is None or auth_ctl is None:
            raise RuntimeError("модули входа (market_browser_login.py / "
                               "market_auth.py) не найдены")
        LoginDialog = login_mod.build_login_dialog()
        return LoginDialog(auth_ctl, parent=parent, cfg=cfg,
                           data_dir=DATA_DIR, manual_factory=manual,
                           logger=log,
                           manual_open=lambda url: webbrowser.open(str(url)),
                           browser_token_finder=lambda:
                           browser_token_module().BrowserTokenFinder(
                               logger=log) if browser_token_module() else None,
                           save_values=lambda mapping: save_config_keys(cfg, mapping),
                           on_login_state=_on_login_state)

    # ── игровые события: панель вкладки «События», сеть — в фоне ────────
    events_ctl = None
    events_client = None
    events_panel = None
    if events_mod is not None:
        try:
            events_client = events_mod.WarframeStatusClient(cfg, data_dir=DATA_DIR)
            EventsOverlay = build_events_panel()
            events_panel = EventsOverlay(cfg, fmt=events_mod.format_timer,
                                         status_fn=events_client.status_text,
                                         embedded=use_unified)
            events_ctl = EventsPanelController(cfg, events_client, events_panel,
                                               geom, notify=ui.events_updated.emit)
            ui.events_updated.connect(events_ctl.refresh)
            # t11: предзагрузка событий сразу после старта (фон, без блокировки)
            events_ctl.start()
        except Exception as e:
            log(f"События: панель не создана ({type(e).__name__}: {e}) — "
                "работаю как раньше (Alt+Q / F8 / журнал).")
            events_ctl = None

    # ── маркет-браузер: вкладки «Маркет» и «Мои ордера» ────────────────
    browser = None
    if browser_mod is not None and market is not None:
        try:
            Browser = browser_mod.build_market_browser()
            browser = Browser(
                cfg, market,
                dialog_factory=lambda: (*build_market_dialogs(),
                                        save_market_token),
                embedded=use_unified)
            manager.market_browser = browser
        except Exception as e:
            log(f"Маркет: окно не создано ({type(e).__name__}: {e}) — "
                "поиск/цены доступны на сайте.")
            browser = None

    # ── обновления кода: проверка/установка из окна F8 и меню трея ──────
    def _updates_check():
        """Только проверка (без скачивания): что отдаёт лаунчер серверу."""
        mod = launcher_module()
        if mod is None:
            return {"checked": False, "available": False,
                    "current": APP_VERSION,
                    "error": "лаунчер (launcher.py) не найден"}
        try:
            return mod.check_for_update(cfg, data_dir_path=DATA_DIR,
                                        apply=False)
        except Exception as e:
            return {"checked": False, "available": False,
                    "current": APP_VERSION,
                    "error": f"{type(e).__name__}: {e}"}

    def _updates_apply(manifest):
        """Скачать и атомарно активировать обновление (вызывается из диалога)."""
        mod = launcher_module()
        if mod is None or not manifest:
            return {"ok": False, "error": "обновления недоступны"}
        try:
            return mod.apply_manifest(manifest, data_dir_path=DATA_DIR)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def _updates_restart():
        """Перезапустить приложение после установки обновления."""
        mod = launcher_module()
        if mod is not None:
            try:
                mod.restart_app()
            except Exception as e:
                log(f"Обновления: перезапуск не удался "
                    f"({type(e).__name__}: {e})")
        try:
            app.quit()
        except Exception:
            pass

    # ── центр уведомлений: история/цвета/тосты/свой звук (t15) ──────────
    center_mod = notification_center_module()
    notification_store = None
    notification_center = None
    toast_stack = None
    sound_player = None
    if center_mod is not None:
        try:
            notification_store = center_mod.HistoryStore(
                os.path.join(DATA_DIR, cfg.get("notifications_file")
                             or "notifications.json"),
                logger=log)
        except Exception as e:
            log(f"Центр уведомлений: история недоступна "
                f"({type(e).__name__}: {e})")
            notification_store = None
        try:
            sound_player = center_mod.SoundPlayer(DATA_DIR, logger=log)
        except Exception as e:
            log(f"Центр уведомлений: звук недоступен "
                f"({type(e).__name__}: {e})")
            sound_player = None
    else:
        log("Центр уведомлений: модуль notification_center.py не найден.")

    # ── чат warframe.market (t19): вкладка, HTTP/WS, фолбэк опроса ──────
    chat_mod = chat_client_module()
    if chat_mod is not None and cfg.get("chat_enabled", True):
        try:
            def _chat_token():
                if market is not None:
                    try:
                        token = market.token()
                        if token:
                            return token
                    except Exception:
                        pass
                return cfg.get("market_token") or ""

            ChatWidget = chat_mod.build_chat_widget()
            chat_widget = ChatWidget(cfg)
            ChatService = chat_mod.build_chat_service()
            chat_service = ChatService(
                cfg, token_fn=_chat_token,
                login_check=login_in_progress)
        except Exception as e:
            log(f"Чат: вкладка не создана ({type(e).__name__}: {e})")
            chat_service = None
            chat_widget = None
    elif chat_mod is None:
        log("Чат: модуль chat_client.py не найден — вкладка недоступна.")

    # ── статус warframe.market (t20): WS A, чип, авто-режим ────────────
    status_mod = market_status_module()

    def _status_game_focused():
        if sys.platform != "win32":
            return False
        hwnd = game_window_handle()
        if not hwnd:
            return False
        try:
            return int(ctypes.windll.user32.GetForegroundWindow()) == int(hwnd)
        except Exception:
            return False

    if status_mod is not None:
        try:
            def _status_token():
                if market is not None:
                    try:
                        return market.token() or ""
                    except Exception:
                        return ""
                return cfg.get("market_token") or ""

            def _status_log(message):
                """Диагностика WS статуса: консоль + status_diag.log (без токена)."""
                text = str(message or "")
                log(f"Статус: {text}")
                try:
                    when = datetime.datetime.now().strftime(
                        "%Y-%m-%d %H:%M:%S")
                    with open(os.path.join(DATA_DIR, "status_diag.log"),
                              "a", encoding="utf-8") as handle:
                        handle.write(f"{when} {text}\n")
                except Exception:
                    pass

            StatusService = status_mod.build_status_service()
            status_service = StatusService(
                cfg, token_fn=_status_token,
                focus_fn=_status_game_focused,
                login_check=login_in_progress,
                logger=_status_log)
        except Exception as e:
            log(f"Статус: сервис не создан ({type(e).__name__}: {e})")
            status_service = None
    else:
        log("Статус: модуль market_status.py не найден.")

    # ── единое окно с вкладками (F8) ───────────────────────────────────
    unified = None
    if use_unified and events_ctl is not None and events_panel is not None \
            and browser is not None:
        try:
            Unified = unified_mod.build_overlay_window()

            def _on_window_open():
                if events_ctl is not None:
                    events_ctl.start()
                if cfg.get("focus_editor_on_f8", True) and not login_in_progress():
                    focus_editor(unified)
            # панель «Дроп» собирается только когда единое окно реально строится
            _loot_orders_requested = [False]

            def _loot_sell_handler(record, action):
                """«Продать»/«Изменить цену» из строки лута."""
                record = dict(record or {})
                status = loot_sell_status(record, market, browser)
                if browser is None:
                    status.update(enabled=False,
                                  tooltip="маркет недоступен")
                if action == "status" and status.get("enabled") is not False:
                    try:
                        if (not getattr(browser, "_orders", None)
                                and not getattr(browser, "_busy_orders", False)
                                and not _loot_orders_requested[0]):
                            _loot_orders_requested[0] = True
                            browser._load_orders(force=True)
                    except Exception:
                        pass
                if action == "status":
                    return status
                if not status.get("enabled"):
                    return {"ok": False, "error": status.get("tooltip")}
                slug = str(record.get("slug") or "")
                if not slug:
                    return {"ok": False, "error": "в записи нет предмета"}
                name = (record.get("picked_name") or record.get("name")
                        or slug)
                try:
                    quantity = int(record.get("quantity") or
                                   record.get("picked_quantity") or 1)
                except Exception:
                    quantity = 1
                item = {"slug": slug, "name": name, "market_name": name,
                        "quantity": max(1, quantity)}
                hint = lowest_sell_order(market, slug)
                price = hint.get("price")
                if price is not None:
                    seller = hint.get("seller") or ""
                    where = f"у продавца {seller}" if seller else "на рынке"
                    if hint.get("online"):
                        where += ", онлайн"
                    note = f"по рынку: {int(price)}p ({where})"
                else:
                    note = (hint.get("error")
                            or "введите цену вручную")
                order = None
                try:
                    order = browser.find_my_sell_order(slug)
                except Exception:
                    order = None
                accepted = False
                try:
                    accepted = bool(browser.open_loot_order(
                        item, prefill_price=price, price_note=note,
                        order=order))
                except Exception as exc:
                    return {"ok": False,
                            "error": f"форма продажи: {exc}"}
                if not accepted:
                    return {"ok": False, "cancelled": True}
                message = f"Выставлено: {name}"
                if price is not None:
                    message += f" — {int(price)}p"
                log("  Продажа: " + message)
                try:
                    if notifier is not None:
                        notifier.notify("market", "Выставлено", message,
                                        force=True)
                except Exception:
                    pass
                try:
                    if drops_panel is not None:
                        drops_panel._listed_slugs.add(slug)
                        drops_panel.refresh()
                except Exception:
                    pass
                try:
                    browser._load_orders(force=True)
                except Exception:
                    pass
                return {"ok": True, "price": price, "mode":
                        "edit" if order is not None else "sell"}

            if ui_mod is not None and collector is not None and pool is not None:
                try:
                    build_drops_panel = ui_mod.build_drops_panel()
                    drops_panel = build_drops_panel(
                        cfg, collector, pool,
                        sell_handler=_loot_sell_handler)
                except Exception as e:
                    log(f"Дроп: панель не создана ({type(e).__name__}: {e})")
                    drops_panel = None
                if drops_panel is not None and browser is not None:
                    try:
                        browser.orders_ready.connect(
                            lambda _orders: drops_panel.refresh())
                    except Exception:
                        pass

            status_providers = [browser.status_text,
                                browser.status_text,
                                events_client.status_text]
            refresh_actions = [browser.refresh, browser.refresh,
                               events_ctl.refresh_now]
            drops_widget = None
            if drops_panel is not None:
                drops_widget = drops_panel
                status_providers.append(drops_panel.status_text)
                refresh_actions.append(drops_panel.refresh)

            # 1.0.0: карточка «Что нового» узнаёт о доступном обновлении
            update_state = {"info": {}}
            if center_mod is not None and notification_store is not None:
                try:
                    Center = center_mod.build_notification_center()
                    notification_center = Center(
                        cfg, notification_store,
                        on_open_updates=lambda: _open_settings(
                            auto_updates=True),
                        save_cfg=lambda mapping: save_config_keys(cfg,
                                                                  mapping),
                        update_info_fn=lambda: update_state.get("info") or {})
                except Exception as e:
                    log(f"Центр уведомлений: вкладка не создана "
                        f"({type(e).__name__}: {e})")
                    notification_center = None

            unified = Unified(
                cfg, manager.selected, browser, events_panel,
                status_providers=status_providers,
                refresh_actions=refresh_actions,
                drops_widget=drops_widget,
                notifications_widget=notification_center,
                chat_widget=chat_widget,
                token_factory=lambda: (*build_market_dialogs(),
                                       save_market_token),
                auth_factory=make_auth_dialog,
                save_values=lambda mapping: save_config_keys(cfg, mapping),
                market_client=market,
                on_open=_on_window_open,
                on_settings=lambda: _open_settings(),
                on_status_set=(lambda state, duration=None:
                               status_service.set_status(state, duration)
                               if status_service is not None else None),
                on_status_auto=(lambda enabled:
                                status_service.set_auto(enabled)
                                if status_service is not None else None),
                on_status_profile=(lambda:
                                   center_mod.open_url(
                                       "https://warframe.market/")
                                   if center_mod is not None else None),
                on_icons_cancel=lambda: icons_cancel(cfg),
                on_close=(restore_game_focus
                          if cfg.get("restore_game_focus", True) else None),
                hotkey_aliases=cfg.get("hotkey_aliases", True))
            manager.unified_window = unified
        except Exception as e:
            log(f"Единое окно не создано ({type(e).__name__}: {e}) — "
                "работаю со старыми окнами.")
            unified = None
            drops_panel = None
            manager.unified_window = None

    # 1.0.0: «Дроп» обновляет показатели по событиям (лог/отметки/сброс),
    # а не по таймеру каждую секунду
    if collector is not None and drops_panel is not None:
        try:
            ui.stats_changed.connect(drops_panel.schedule_refresh)
        except Exception as e:
            log(f"Дроп: обновление по событиям не подключено "
                f"({type(e).__name__}: {e})")

    # прозрачность окна (настройка «Оверлей»); применяется сразу
    if unified is not None:
        try:
            opacity = float(cfg.get("overlay_opacity", 1.0) or 1.0)
            unified.setWindowOpacity(max(0.4, min(1.0, opacity)))
        except Exception:
            pass

    # ── пакет иконок (1.0.0): прогресс в шапке/настройках ──────────────
    icon_pack = icon_pack_manager(cfg)
    _icons_signals = {"obj": None}
    _icons_dialog = {"cb": None}
    _icons_hide_gen = {"gen": 0}
    _icons_stop = threading.Event()

    def _icons_progress_gui(payload):
        """GUI-поток: прогресс «Иконки: N / M (P%)» в шапке и настройках."""
        payload = dict(payload or {})
        if unified is not None:
            try:
                if payload.get("running"):
                    unified.set_icons_progress(payload.get("done") or 0,
                                               payload.get("total") or 0,
                                               str(payload.get("text") or ""))
                elif payload.get("phase") == "done":
                    # финальная строка висит пару секунд и гаснет
                    unified.set_icons_progress(payload.get("done") or 0,
                                               payload.get("total") or 0,
                                               str(payload.get("text") or ""))
                    _icons_hide_gen["gen"] += 1
                    generation = _icons_hide_gen["gen"]

                    def _hide_later(gen=generation):
                        if _icons_hide_gen["gen"] != gen:
                            return
                        try:
                            unified.hide_icons_progress()
                        except Exception:
                            pass

                    try:
                        from PyQt5.QtCore import QTimer
                        QTimer.singleShot(5000, _hide_later)
                    except Exception:
                        pass
                else:
                    unified.hide_icons_progress()
            except Exception:
                pass
        callback = _icons_dialog["cb"]
        if callable(callback):
            try:
                callback(payload)
            except Exception:
                pass

    def _redraw_icon_surfaces():
        """t54: после пака/очистки — сбросить кэш и перерисовать UI."""
        try:
            if browser is not None:
                browser.apply_cfg(cfg)
        except Exception:
            pass
        try:
            if drops_panel is not None:
                drops_panel.apply_cfg(cfg)
        except Exception:
            pass
        log("Иконки обновлены → перерисовано: market/drops/orders")

    set_icon_redraw_callback(_redraw_icon_surfaces)

    try:
        _STARTUP["total_ms"] = (time.time() - _STARTUP["t0"]) * 1000
        _STARTUP["gui_ms"] = _STARTUP["total_ms"]
        ocr_ms = int(_STARTUP.get("ocr_ms") or 0)
        ready = "да" if _STARTUP.get("ocr_ready") else "нет"
        log(f"Старт: import={int(_STARTUP.get('import_ms') or 0)} "
            f"ocr={ocr_ms} catalog={int(_STARTUP['catalog_ms'])} "
            f"gui={int(_STARTUP['gui_ms'])} "
            f"total={int(_STARTUP['total_ms'])} "
            f"(ocr_ready={ready}, прогрев в фоне)")
    except Exception:
        pass

    if icon_pack is not None:
        try:
            _icons_signals["obj"] = icon_pack_progress_bridge(
                _icons_progress_gui)
        except Exception as e:
            log(f"Иконки: мост прогресса не создан ({type(e).__name__}: {e})")

    def _icons_emit(payload):
        """Из фонового потока — в GUI через сигнал (иначе напрямую)."""
        payload = dict(payload or {})
        bridge = _icons_signals["obj"]
        if bridge is not None:
            try:
                bridge.emit_progress(payload)
                return
            except Exception:
                pass
        _icons_progress_gui(payload)

    def _icons_done(result):
        """Итог фоновой операции: сводку пака/обновления пишет icon_pack.py."""
        result = dict(result or {})
        if result.get("ok"):
            return result
        if result.get("cancelled"):
            return result
        if result.get("error") or result.get("busy"):
            log(f"Иконки: {result.get('error') or 'операция не выполнена'}")
        return result

    # свои тосты (поверх игры, не забирают фокус)
    if center_mod is not None:
        try:
            ToastStack = center_mod.build_toast_stack()
            toast_stack = ToastStack(
                cfg, on_click=lambda record: _toast_activated(record),
                login_check=login_in_progress)
        except Exception as e:
            log(f"Центр уведомлений: тосты не созданы "
                f"({type(e).__name__}: {e})")
            toast_stack = None
    if unified is not None and notification_store is not None:
        try:
            unified.set_unread_badge(*notification_store.badge(cfg))
        except Exception:
            pass

    # ── уведомления Windows: трей, нативные тосты и события ────────────
    notify_mod = notifications_module()
    notifier = None
    notify_tray = None
    notify_poller = None

    def _game_focused():
        """В фокусе ли окно игры (фильтры notify_*_when_game_focused)."""
        if sys.platform != "win32":
            return False
        hwnd = game_window_handle()
        if not hwnd:
            return False
        try:
            return int(ctypes.windll.user32.GetForegroundWindow()) == int(hwnd)
        except Exception:
            return False

    def _tray_open():
        """Клик по трею / «Открыть»: показать окно F8."""
        if login_in_progress():
            # идёт вход в браузере: окна приложения не всплывают поверх него
            return
        try:
            if unified is not None:
                unified.show_tab(0)
            else:
                manager.show_selected(force=True)
        except Exception as e:
            log(f"Трей: не удалось открыть окно ({type(e).__name__}: {e})")

    def _tray_stop():
        for service, kwargs in ((chat_service, {}),
                                (status_service, {"restore": False}),
                                (events_ctl, {}),
                                (auto_update_watcher, {})):
            if service is None:
                continue
            try:
                stop = getattr(service, "stop", None)
                if callable(stop):
                    stop(**kwargs)
            except Exception:
                pass
        if notify_poller is not None:
            try:
                notify_poller.stop()
            except Exception:
                pass
        if notify_tray is not None:
            try:
                notify_tray.stop()
            except Exception:
                pass

    def _tray_exit():
        """«Выход» в меню трея: закрыть потоки/окна и завершить приложение."""
        log("Трей: выход из приложения.")
        _tray_stop()
        try:
            app.quit()
        except Exception:
            pass

    def _tray_updates():
        """«Проверить обновление»: настройки на вкладке «Обновления» + проверка."""
        _open_settings(auto_updates=True)

    def _show_notification(kind, title, text, sound):
        """GUI-поток: показать тост через трей (сигнал из фонового потока)."""
        if notify_tray is None:
            return False
        return notify_tray.show_message(title, text, kind=kind, sound=sound)

    def _dispatch_notification(record):
        """GUI: история + свой тост/системный тост + свой звук + бейдж."""
        if center_mod is None:
            return False
        badge_cb = None
        if unified is not None:
            badge_cb = lambda count, color: unified.set_unread_badge(count, color)
        result = center_mod.dispatch_notification(
            record, cfg=cfg, store=notification_store,
            center=notification_center, sound_player=sound_player,
            toast_stack=toast_stack, tray=notify_tray,
            login_check=login_in_progress, badge_cb=badge_cb)
        if not result.get("ok"):
            log(f"Центр уведомлений: запись не сохранена "
                f"({result.get('error') or 'ошибка'})")
        return bool(result.get("ok"))

    def _toast_activated(record):
        """Клик по тосту: лут → «Дроп», остальные → центр на записи."""
        record = dict(record or {})
        record_id = record.get("id")
        if record.get("action") == "drops" or record.get("kind") == "pick":
            if unified is not None:
                try:
                    titles = unified.tab_titles()
                    index = titles.index("Дроп") if "Дроп" in titles else 0
                    unified.show_tab(index)
                except Exception:
                    pass
            try:
                if drops_panel is not None:
                    drops_panel.refresh()
            except Exception:
                pass
            return record_id
        if unified is not None:
            try:
                unified.open_notifications(record_id)
            except Exception:
                pass
        return record_id

    def _notification_activated(record):
        """Клик по записи центра: действие по типу (market/chat/events/updates)."""
        record = dict(record or {})
        record_id = record.get("id")
        if notification_store is not None:
            notification_store.mark_read(record_id)
        if notification_center is not None:
            try:
                notification_center.refresh()
            except Exception:
                pass
        if unified is not None and notification_store is not None:
            try:
                unified.set_unread_badge(*notification_store.badge(cfg))
            except Exception:
                pass
        if center_mod is not None:
            result = center_mod.perform_action(
                record, unified=unified, browser=browser,
                events_panel=events_panel,
        open_updates=lambda: _open_settings(auto_updates=True),
                cfg=cfg)
        else:
            result = {"action": "center", "ok": False}
        return str(result.get("action") or "center")

    if center_mod is not None:
        ui.notification_full.connect(_dispatch_notification)
        if notification_center is not None:
            try:
                notification_center.record_activated.connect(
                    _notification_activated)
            except Exception:
                pass

    if notify_mod is not None:
        ui.notification.connect(_show_notification)
        try:
            notify_tray = notify_mod.build_tray()(
                cfg, on_open=_tray_open, on_updates=_tray_updates,
                on_settings=lambda: _open_settings(),
                on_about=lambda: show_about(None),
                on_exit=_tray_exit)
        except Exception as e:
            log(f"Уведомления: трей не создан ({type(e).__name__}: {e})")
            notify_tray = None
        try:
            notifier = notify_mod.NotificationManager(
                cfg,
                send=(lambda kind, title, text, sound:
                      ui.notification.emit(str(kind), str(title), str(text),
                                           bool(sound)) or True
                      if (notify_tray is not None and notify_tray.available())
                      else False),
                send_meta=((lambda record:
                            ui.notification_full.emit(dict(record)) or True)
                           if center_mod is not None else None),
                is_game_focused=_game_focused,
                logger=log)

            def _orders_provider():
                if market is None or not market.has_token():
                    return []
                orders = market.my_orders()
                try:
                    # кэш для центра уведомлений: клик «перебили цену» должен
                    # найти мой ордер, даже если вкладка «Мои ордера» не открыта
                    if browser is not None:
                        browser._orders_cache = [dict(x) for x in (orders or [])
                                                 if isinstance(x, dict)]
                except Exception:
                    pass
                return orders

            def _order_name(order):
                if browser is not None:
                    try:
                        return browser._order_item_name(order)
                    except Exception:
                        pass
                return order.get("itemId") or "предмет"

            def _events_provider():
                if events_client is None:
                    return {}
                try:
                    return events_client.snapshot() or {}
                except Exception:
                    return {}

            def _watch_provider():
                """Что сравнивать с рынком: список «Выбрано» и мои sell-ордера."""
                items = []
                try:
                    for item in list(manager.selected.items or []):
                        if not isinstance(item, dict):
                            continue
                        slug = item.get("slug")
                        price = item.get("price")
                        if slug and isinstance(price, (int, float)) and price > 0:
                            items.append({
                                "slug": str(slug),
                                "name": (item.get("market_name")
                                         or item.get("name") or str(slug)),
                                "price": float(price),
                            })
                except Exception:
                    pass
                try:
                    orders = list(getattr(browser, "_orders", None)
                                  or getattr(browser, "_orders_cache", None)
                                  or [])
                    catalog = dict(getattr(browser, "_items_by_id", None) or {})
                    for order in orders:
                        if order_type_of(order) != "sell":
                            continue
                        item = catalog.get(order.get("itemId")) or {}
                        slug = item.get("slug")
                        price = order.get("platinum")
                        if slug and isinstance(price, (int, float)) and price > 0:
                            items.append({
                                "slug": str(slug),
                                "name": item.get("name") or str(slug),
                                "price": float(price),
                                "order_id": order.get("id"),
                            })
                except Exception:
                    pass
                # один предмет — одна (наибольшая) моя цена
                best = {}
                for item in items:
                    old = best.get(item["slug"])
                    if old is None or item["price"] > old["price"]:
                        best[item["slug"]] = item
                order_ids = {item["slug"]: item.get("order_id")
                             for item in items if item.get("order_id")}
                for slug, item in best.items():
                    if not item.get("order_id") and order_ids.get(slug):
                        item["order_id"] = order_ids[slug]
                return list(best.values())

            def _lowest_price(slug):
                if market is None:
                    return None
                try:
                    top = market.top_orders(slug) or {}
                    orders = [order for order in (top.get("sell") or [])
                              if isinstance(order.get("platinum"),
                                            (int, float))]
                    if not orders:
                        return None
                    best = min(orders, key=lambda order:
                               float(order.get("platinum") or 0))
                    user = best.get("user")
                    user = user if isinstance(user, dict) else {}
                    return {"price": float(best.get("platinum")),
                            "seller_name": str(user.get("ingameName")
                                               or user.get("ingame_name")
                                               or "")}
                except Exception:
                    return None

            notify_poller = notify_mod.NotificationPoller(
                notifier,
                interval=cfg.get("notify_poll_sec", 60),
                orders_provider=_orders_provider,
                has_token=(lambda: bool(market and market.has_token())),
                name_of=_order_name,
                events_provider=_events_provider,
                watch_provider=_watch_provider,
                lowest_price=_lowest_price,
                logger=log)
            if notifier.notify_enabled() and (
                    center_mod is not None
                    or (notify_tray is not None and notify_tray.available())):
                notify_poller.start()
                log("Уведомления: включены (центр в оверлее, свои тосты, опрос "
                    f"каждые {int(notify_poller.interval)} с).")
            elif not notifier.notify_enabled():
                log("Уведомления: выключены (notify_enabled=false).")
            else:
                log("Уведомления: системный трей недоступен — тосты и опрос "
                    "не запускаю.")
        except Exception as e:
            log(f"Уведомления: не запущены ({type(e).__name__}: {e})")
            notifier = None
            notify_poller = None
    else:
        log("Уведомления: модуль notifications.py не найден — без трея и тостов.")

    # ── чат: сигналы сервиса → вкладка/бейдж; NEW_MESSAGE → центр ───────
    if chat_service is not None and chat_widget is not None:
        chat_service.chats_ready.connect(chat_widget.set_chats)
        chat_service.messages_ready.connect(chat_widget.set_messages)
        chat_service.message_received.connect(chat_widget.append_message)
        chat_service.message_sent.connect(chat_widget.append_message)
        chat_service.status_changed.connect(chat_widget.set_status)
        if unified is not None:
            chat_service.unread_changed.connect(unified.set_chat_badge)
        chat_widget.chat_selected.connect(chat_service.load_messages)
        chat_widget.send_requested.connect(chat_service.send)
        chat_widget.open_site_requested.connect(
            lambda: chat_mod.open_chat_site())

        def _chat_notify(record):
            if notifier is None:
                return False
            payload = (record.get("payload")
                       if isinstance(record.get("payload"), dict) else {})
            return notifier.notify(
                record.get("kind", "chat"), record.get("title", ""),
                record.get("text", ""),
                subject=payload.get("chat_id", ""),
                action="chat", payload=payload)

        chat_service.notify_fn = _chat_notify
        chat_service.start()

    # ── статус: сигналы → чип; старт после входа/с токеном ─────────────
    if status_service is not None:
        if unified is not None:
            status_service.status_changed.connect(unified.set_market_status)
            status_service.auto_changed.connect(unified.set_status_auto)
            status_service.error.connect(
                lambda text: unified.lbl_status.setText(str(text)))
            status_service.confirmed.connect(
                lambda label: unified.lbl_status.setText(f"статус: {label}"))
        else:
            status_service.error.connect(log)
        status_service.start()
        if unified is not None:
            unified.set_status_auto(status_service.auto)

    def toggle_selected():
        """F8: единое окно (первая вкладка «Маркет») или старый редактор."""
        if login_in_progress():
            # идёт вход в браузере: окно поверх браузера не показываем
            return
        if unified is not None:
            unified.show_tab(0)
            return
        if manager.selected.isVisible():
            manager.hide_selected()
        else:
            manager.show_selected(force=True)

    def toggle_window():
        """Главный хоткей единого окна: открыть/закрыть."""
        if login_in_progress():
            return
        if unified is not None:
            unified.toggle()
        else:
            toggle_selected()

    def toggle_events():
        """Alt+E: алиас вкладки «События» или старое окно событий."""
        if login_in_progress():
            return
        if unified is not None:
            if cfg.get("hotkey_aliases", True):
                unified.show_tab(2)
            return
        if events_ctl is None:
            log("События: панель недоступна (нет events_api.py или PyQt5).")
            return
        events_ctl.toggle()

    def toggle_market():
        """Alt+M: алиас вкладки «Маркет» или старое окно маркета."""
        if login_in_progress():
            return
        if unified is not None:
            if cfg.get("hotkey_aliases", True):
                unified.show_tab(0)
            return
        if browser is None:
            log("Маркет: окно недоступно (нет market_browser.py).")
            return
        browser.toggle()

    def _show_pick_toast(result, duplicate=False):
        """Эфемерный тост «Добавлено/Уже в дропах» — без центра/бейджа."""
        if center_mod is None or not callable(
                getattr(center_mod, "dispatch_toast_only", None)):
            return None
        record = pick_toast_record(result, duplicate=duplicate, cfg=cfg)
        try:
            return center_mod.dispatch_toast_only(
                record, cfg=cfg, toast_stack=toast_stack,
                login_check=login_in_progress)
        except Exception as exc:
            log(f"  Дроп: тост не показан ({type(exc).__name__}: {exc})")
            return None

    def pick_reward(index):
        """Alt+1…4: «взята награда №N» → «Выбрано» и «Дроп» (без угадывания)."""
        if login_in_progress():
            return False
        result = manager.pick_reward(index)
        message = str(result.get("message") or "")
        if not message:
            return False
        log(f"  Награда #{int(index) + 1}: {message}")
        if unified is not None:
            try:
                unified.lbl_status.setText(message)
            except Exception:
                pass
        if result.get("ok") and (result.get("added")
                                 or result.get("reason") == "duplicate"):
            _show_pick_toast(result,
                             duplicate=not bool(result.get("added")))
        if result.get("added") and collector is not None:
            item = result.get("item") or {}
            try:
                marked = collector.mark_picked(
                    result.get("name") or manager._reward_name(item),
                    price=item.get("price"), index=index,
                    tradable=item.get("tradable"))
                if not marked:
                    collector.record_pick(
                        result.get("name") or manager._reward_name(item),
                        price=item.get("price"), index=index,
                        slug=item.get("slug"),
                        tradable=item.get("tradable"))
                    marked = True
                if marked and drops_panel is not None:
                    drops_panel.refresh()
            except Exception as e:
                log(f"  Дроп: «взято» не записано ({type(e).__name__}: {e})")
            try:
                mark_relic_selection(
                cfg, index,
                (manager.last_results if manager is not None else [])
                or (scanner.last_results if scanner is not None else []))
            except Exception as e:
                log(f"  Журнал: «выбрано» не записано "
                    f"({type(e).__name__}: {e})")
        # тост/звук для «взято» не нужны: выбор уже подтверждён в «Дропе»
        return bool(result.get("ok"))

    def hide_all():
        """F9: скрыть всё (окно/плашки/редактор/события/маркет/диалоги)."""
        if login_in_progress():
            # во время входа всё уже спрятано; F9 не должен трогать пилюлю
            return
        # B2: сначала закрываем все открытые диалоги приложения, потом окна
        close_app_dialogs()
        if unified is not None:
            unified.hide_window()
        manager.hide_all()
        if events_ctl is not None:
            events_ctl.hide()
        if browser is not None:
            browser.hide()

    def request_scan():
        """Alt+Q/автоскан: во время входа окна приложения не показываем."""
        if login_in_progress():
            return
        scanner.request_scan()

    ui.toggle_selected.connect(toggle_selected)
    ui.toggle_events.connect(toggle_events)
    ui.market_toggle.connect(toggle_market)
    ui.window_toggle.connect(toggle_window)
    ui.scan.connect(request_scan)
    ui.hide_all.connect(hide_all)
    ui.screen_closed.connect(manager.hide_all)
    ui.pick_reward.connect(pick_reward)

    # старые хоткеи — ровно как раньше, но в обеих раскладках
    if unified is None:
        register_hotkey(keyboard, cfg.get("hotkey_editor", "f8"),
                        ui.toggle_selected.emit)
    register_hotkey(keyboard, cfg.get("hotkey_scan", "alt+q"),
                    ui.scan.emit)
    register_hotkey(keyboard, cfg.get("hotkey_hide", "f9"),
                    ui.hide_all.emit)
    if unified is not None:
        add_hotkey_window(cfg, keyboard, ui.window_toggle.emit)
    if events_ctl is not None:
        add_hotkey_events(cfg, keyboard, ui.toggle_events.emit)
    if browser is not None:
        add_hotkey_market(cfg, keyboard, ui.market_toggle.emit)
    # Alt+1…4: взятая награда из последнего скана (не подавляем клавиши игры)
    pick_registered = []
    if cfg.get("pick_enabled", True):
        pick_registered = add_hotkeys_pick(cfg, keyboard, ui.pick_reward.emit)

    def _pick_hint_text(values=None):
        try:
            source = values if values is not None else cfg
            return ui_mod.format_pick_hint(source.get("pick_hotkeys", []))
        except Exception:
            return "ваши клавиши отметки награды"

    def _log_pick_hint(values=None):
        try:
            hint = _pick_hint_text(values)
        except Exception:
            hint = "ваши клавиши отметки награды"
        log("Взятая награда из последнего скана: " + hint)

    _log_pick_hint()
    if not pick_registered:
        log("Награда: все клавиши заняты/выключены — "
            "см. pick_hotkeys в config.json.")

    # ── настройки (шестерёнка): клавиши и параметры без перезапуска ──────
    settings_mod = settings_ui_module()
    hotkey_binder = None
    if settings_mod is not None:
        hotkey_callbacks = {
            "scan": ui.scan.emit,
            "hide": ui.hide_all.emit,
            "window": (ui.window_toggle.emit if unified is not None
                       else ui.toggle_selected.emit),
            "events": (ui.toggle_events.emit if events_ctl is not None
                       else None),
            "market": (ui.market_toggle.emit if browser is not None else None),
        }
        for index in range(4):
            hotkey_callbacks[f"pick{index + 1}"] = (
                lambda i=index: ui.pick_reward.emit(i))
        try:
            hotkey_binder = settings_mod.HotkeyBinder(keyboard,
                                                      hotkey_callbacks)
            hotkey_binder.adopt(settings_mod.hotkeys_from_cfg(cfg))
        except Exception as e:
            log(f"Настройки: реестр горячих клавиш не создан "
                f"({type(e).__name__}: {e})")
            hotkey_binder = None

    def _settings_side_effects(settings_cfg, _values):
        """Применить настройки к уже работающему приложению (без перезапуска)."""
        # цвета валют: глобальная палитра + слушатели плашек/панелей
        try:
            ui_theme().set_currency_colors(
                settings_cfg.get("currency_platinum_color"),
                settings_cfg.get("currency_ducats_color"))
        except Exception as e:
            log(f"Настройки: цвета валют не применены "
                f"({type(e).__name__}: {e})")
        # иконки предметов: включение/выключение без перезапуска
        try:
            provider = item_icons_provider(settings_cfg)
            if provider is not None:
                provider.enabled = bool(settings_cfg.get("show_item_icons",
                                                         True))
            for card in list(getattr(manager, "cards", []) or []):
                try:
                    card.cfg["overlay_show_set_price"] = bool(
                        settings_cfg.get("overlay_show_set_price", True))
                    card.cfg["show_item_icons"] = bool(
                        settings_cfg.get("show_item_icons", True))
                except Exception:
                    pass
                apply_icon = getattr(card, "apply_icon_setting", None)
                if callable(apply_icon):
                    apply_icon()
                apply_set = getattr(card, "apply_set_setting", None)
                if callable(apply_set):
                    apply_set()
        except Exception as e:
            log(f"Настройки: иконки не переключены "
                f"({type(e).__name__}: {e})")
        # 1.0.0 (t46): иконки валют — «Итого», «Дроп», открытый «Маркет»
        try:
            apply_display_settings(
                settings_cfg,
                summary=getattr(manager, "summary", None),
                drops_panel=drops_panel,
                market_browser=browser)
        except Exception as e:
            log(f"Настройки: отображение цен не обновлено "
                f"({type(e).__name__}: {e})")
        # 1.0.0: ручной выбор темы интерфейса — перепроверим на следующем скане
        try:
            if scanner is not None:
                scanner.cfg["game_theme"] = str(
                    settings_cfg.get("game_theme") or "auto")
                scanner._theme_checked = False
        except Exception:
            pass
        # уведомления: фоновый опрос включается/выключается на лету
        if notify_poller is not None and notify_tray is not None:
            try:
                enabled = bool(settings_cfg.get("notify_enabled", True))
                if enabled and notify_tray.available():
                    if not notify_poller.is_alive():
                        notify_poller.start()
                elif notify_poller.is_alive():
                    notify_poller.stop()
            except Exception as e:
                log(f"Настройки: опрос уведомлений не переключён "
                    f"({type(e).__name__}: {e})")
        # автоматическая проверка обновлений: включить/выключить на лету
        try:
            if auto_update_watcher is not None:
                if auto_update_watcher.enabled():
                    if not auto_update_watcher.start():
                        # интервал мог измениться — перезапустить таймер
                        auto_update_watcher.stop()
                        auto_update_watcher.start()
                else:
                    auto_update_watcher.stop()
        except Exception as e:
            log(f"Настройки: автопроверка обновлений не переключена "
                f"({type(e).__name__}: {e})")
        # прозрачность окна/фона
        if unified is not None:
            try:
                opacity = float(settings_cfg.get("overlay_opacity", 1.0) or 1.0)
                unified.setWindowOpacity(max(0.4, min(1.0, opacity)))
            except Exception:
                pass
            # подписи/подсказки с новыми комбинациями
            try:
                if settings_mod is not None and hotkey_binder is not None:
                    unified.update_hotkey_hints(hotkey_binder.current())
            except Exception:
                pass
            try:
                unified.lbl_status.setText("настройки сохранены и применены")
            except Exception:
                pass
        # подсказка «Дропа» и консольная строка — по новым биндам
        try:
            if drops_panel is not None:
                drops_panel.cfg["pick_hotkeys"] = list(
                    settings_cfg.get("pick_hotkeys", []))
                drops_panel.update_pick_hint()
        except Exception as e:
            log(f"Дроп: подсказка биндов не обновлена "
                f"({type(e).__name__}: {e})")
        try:
            _log_pick_hint(settings_cfg)
        except Exception:
            pass
        # «Итого» выключено — убираем сразу (дальше решает Manager._apply)
        if not settings_cfg.get("show_summary", True):
            try:
                manager.summary.hide()
            except Exception:
                pass
        # центр уведомлений: цвета/режим/звук/тосты на лету
        if notification_center is not None:
            try:
                notification_center.set_cfg(settings_cfg)
            except Exception:
                pass
        if toast_stack is not None:
            try:
                toast_stack.set_cfg(settings_cfg)
            except Exception:
                pass
        if unified is not None and notification_store is not None:
            try:
                unified.set_unread_badge(*notification_store.badge(settings_cfg))
            except Exception:
                pass
        log("Настройки применены без перезапуска.")

    def _apply_settings(values):
        """Сохранить настройки (config.json + бэкап) и применить на лету."""
        if settings_mod is None:
            return {"ok": False, "error": "модуль settings_ui.py не найден"}
        result = settings_mod.apply_settings(
            cfg, values, hotkey_binder, CONFIG_PATH,
            extra_apply=_settings_side_effects)
        if result.get("ok"):
            log("Настройки сохранены в config.json"
                + (f" (бэкап: {os.path.basename(result.get('backup'))})"
                   if result.get("backup") else "") + ".")
        return result

    def _test_notification(kind="market"):
        """Кнопка «Проверить уведомление»: ТЕСТ мимо notify_enabled/кулдаунов."""
        if center_mod is None or notification_store is None:
            return {"ok": False, "error": "центр уведомлений недоступен"}
        badge_cb = None
        if unified is not None:
            badge_cb = lambda count, color: unified.set_unread_badge(count, color)
        return center_mod.dispatch_test_notification(
            kind, cfg=cfg, store=notification_store,
            center=notification_center, sound_player=sound_player,
            toast_stack=toast_stack, tray=notify_tray,
            login_check=login_in_progress, badge_cb=badge_cb)

    def _open_scan_calibration(parent=None):
        """Мастер «Калибровка скана»: снимок, авто-определение, сохранение."""
        mod = scan_calibrate_module()
        if mod is None:
            log("Калибровка: модуль scan_calibrate.py не найден")
            return {"ok": False, "error": "модуль калибровки не найден"}

        def _capture():
            full, _mon = grab(cfg)
            return to_base(full, cfg)

        def _save(payload):
            return save_scan_calibration(cfg, payload)

        def _check_trial(geometry):
            return _calibration_check_scan(cfg, geometry)

        try:
            Dialog = mod.build_calibration_dialog()
            dialog = Dialog(cfg, capture_fn=_capture, save_fn=_save,
                            check_fn=_check_trial,
                            ui_scale=float(geom.get("sx") or 1.0),
                            parent=(parent or unified))
        except Exception as e:
            log(f"Калибровка: мастер не открылся ({type(e).__name__}: {e})")
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        try:
            if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
                return {"ok": True, "dialog": dialog, "saved": False}
            dialog.exec_()
            result = dict(getattr(dialog, "last_result", {}) or {})
            if result.get("ok"):
                return {"ok": True, "saved": True,
                        "scan_calibration": dict(cfg.get("scan_calibration")
                                                 or {})}
            return {"ok": True, "saved": False}
        finally:
            try:
                dialog.deleteLater()
            except Exception:
                pass

    def _open_settings(auto_updates=False):
        """Настройки: клавиши/уведомления/обновления/прочее."""
        if settings_mod is None:
            log("Настройки: модуль settings_ui.py не найден.")
            return False
        if login_in_progress():
            # во время входа окна приложения не показываем
            return False
        try:
            Dialog = settings_mod.build_settings_dialog()

            def _icons_status_for_dialog():
                return icons_status(cfg)

            def _icons_download_for_dialog():
                return icons_download(cfg, on_progress=_icons_emit,
                                      on_done=_icons_done)

            def _icons_update_for_dialog():
                return icons_update(cfg, on_progress=_icons_emit,
                                    on_done=_icons_done)

            def _icons_clear_for_dialog():
                return icons_clear(cfg)

            def _icons_cancel_for_dialog():
                return {"ok": bool(icons_cancel(cfg))}

            dialog = Dialog(
                cfg, apply_fn=_apply_settings,
                on_test_notification=_test_notification,
                check_update_fn=_updates_check,
                apply_update_fn=_updates_apply,
                restart_fn=_updates_restart,
                auto_check=bool(auto_updates),
                data_dir=DATA_DIR, version=APP_VERSION,
                parent=unified if unified is not None else None,
                icons_status_fn=_icons_status_for_dialog,
                icons_download_fn=_icons_download_for_dialog,
                icons_update_fn=_icons_update_for_dialog,
                icons_clear_fn=_icons_clear_for_dialog,
                icons_cancel_fn=_icons_cancel_for_dialog,
                on_scan_calibrate=_open_scan_calibration,
                confirm_fn=(lambda text: themed_confirm(
                    dialog, text, title="Иконки предметов",
                    confirm_text="Удалить", danger=True)))
            if auto_updates:
                try:
                    dialog.tabs.setCurrentWidget(dialog.updates_page)
                except Exception:
                    pass
            # прогресс пака приходит из фонового потока в это окно
            _icons_dialog["cb"] = getattr(dialog, "set_icons_progress", None)
            try:
                dialog.refresh_icons_status()
            except Exception:
                pass
            try:
                dialog.exec_()
            finally:
                _icons_dialog["cb"] = None
            return True
        except Exception as e:
            _icons_dialog["cb"] = None
            log(f"Настройки: окно не открылось ({type(e).__name__}: {e})")
            return False

    watcher = None
    if cfg.get("auto_detect", True):
        delay = max(0, int(cfg.get("auto_scan_delay_ms", 450))) / 1000.0

        def _on_open():
            log("  Экран наград открыт — сканирую…")
            threading.Timer(delay, request_scan).start()

        def _on_close():
            log("  Экран наград закрыт — убираю плашки.")
            ui.screen_closed.emit()

        watcher = ScreenWatcher(cfg, geom["monitor"], _on_open, _on_close)
        scanner.watcher = watcher
        watcher.start()

    # ── EE.log: собственный дроп без OCR (лог игры только читаем) ──────
    def _apply_log_selected(item):
        """Добавляет дроп из лога в список «Выбрано» (если включено в конфиге)."""
        try:
            manager.selected.add(item)
            manager.show_selected(force=True)
            save_selected_list(cfg, manager.selected.items)
        except Exception as e:
            log(f"  лог: не удалось добавить в список выбранного ({e})")

    ui.log_selected.connect(_apply_log_selected)

    def _handle_log_drop(ev):
        try:
            item = append_log_drop(cfg, api, ev)
            if item is not None and log_settings(cfg)["auto_select"]:
                ui.log_selected.emit(item)
        except Exception as e:
            log(f"  лог: награда не обработана ({type(e).__name__}: {e})")

    def _on_log_event(ev):
        """События EE.log приходят из потока хвоста — UI только через сигналы."""
        kind = (ev or {}).get("kind")
        if kind == "open":
            log("  EE.log: экран реликвии открыт")
        elif kind == "rewards":
            log("  EE.log: экран наград заполнен")
        elif kind == "close":
            log("  EE.log: экран реликвии закрыт")
        elif kind == "drop":
            # за ценой идём в сеть — не тормозим поток чтения лога
            threading.Thread(target=_handle_log_drop, args=(ev,),
                             daemon=True).start()

    tailer = start_log_tailer(cfg, _on_log_event)

    # ── свой хвост статистики фарма (читает тот же EE.log только на чтение) ──
    if collector is not None:
        try:
            collector.start()
        except Exception as e:
            log(f"Дроп: хвост статистики не запущен ({type(e).__name__}: {e})")

    # ── автопроверка обновлений на ходу (t34) ─────────────────────────
    auto_update_watcher = None
    try:
        watcher_mod = update_watcher_module()
        if watcher_mod is not None:
            def _auto_update_available(info):
                update_state["info"] = dict(info or {})
                version = str((info or {}).get("version") or "").strip()
                if not version or notifier is None:
                    return False
                title = f"Доступно обновление {version}"
                text = ("Откройте настройки → «Обновления», чтобы проверить "
                        "и установить")
                try:
                    return notifier.notify("updates", title, text,
                                           force=True, action="updates")
                except Exception as exc:
                    log(f"Обновления: уведомление не показано "
                        f"({type(exc).__name__})")
                    return False

            auto_update_watcher = watcher_mod.UpdateWatcher(
                cfg, _updates_check,
                on_available=_auto_update_available,
                save_fn=lambda mapping: save_config_keys(cfg, mapping),
                logger=log)
            auto_update_watcher.start()
    except Exception as e:
        log(f"Обновления: автопроверка не запущена "
            f"({type(e).__name__}: {e})")
        auto_update_watcher = None

    log("=" * 60)
    log(f"VoidFrameRU {APP_VERSION} — готово.")
    log(f"{cfg.get('hotkey_scan', 'alt+q').upper()} — сканировать награды (плашки).")
    if unified is not None:
        hotkey = (cfg.get("hotkey_window") or cfg.get("hotkey_editor", "f8")).upper()
        tabs_desc = ("Маркет · Мои ордера · События · Дроп · Чат")
        log(f"{hotkey} — единое окно: {tabs_desc}.")
        if cfg.get("hotkey_aliases", True):
            log(f"{cfg.get('hotkey_events', 'alt+e').upper()} / "
                f"{cfg.get('hotkey_market', 'alt+m').upper()} — алиасы вкладок "
                "«События» / «Маркет».")
    else:
        log(f"{cfg.get('hotkey_editor', 'f8').upper()} — единое окно F8 "
            "(список выбранного остаётся в плашках/ваших клавишах "
            "отметки награды).")
        if events_ctl is not None:
            log(f"{cfg.get('hotkey_events', 'alt+e').upper()} — игровые события.")
        if browser is not None:
            log(f"{cfg.get('hotkey_market', 'alt+m').upper()} — маркет.")
    log(f"{cfg.get('hotkey_hide', 'f9').upper()} — скрыть всё.")
    if market is not None:
        if market.has_token():
            log("Продажа: токен warframe.market задан "
                f"({market_token_masked(market.token())}) — кнопка «₽» в редакторе (F8).")
        else:
            log("Продажа: токена warframe.market нет — кнопка «₽» в редакторе (F8) "
                "покажет инструкцию, как его вставить.")
    else:
        log("Продажа: модуль market_client.py не найден — продажа недоступна, "
            "остальное работает.")
    if watcher is not None:
        log(f"Авто-режим включён (шаблонов экрана: {watcher.templates_count()}).")
    else:
        log("Авто-режим выключен — сканирование только по Alt+Q.")
    if tailer is not None:
        log("EE.log: свой дроп из лога пишется в журнал (Alt+Q по-прежнему плашки).")
    else:
        log("EE.log: лог не читаю — работаю как раньше (Alt+Q / журнал).")
    log(f"Монитор: {geom['width']}x{geom['height']}  "
        f"(масштаб {geom['sx']:.2f}x{geom['sy']:.2f} от 1920x1080)")
    log("=" * 60)

    # ── иконки: первый запуск (пак в фоне) и автообновление раз в N дней ──
    if icon_pack is not None:
        try:
            started = icons_autostart(cfg, on_progress=_icons_emit,
                                      on_done=_icons_done)
            if not started and cfg.get("icons_pack_enabled", True):
                log("Иконки: пак на диске — работаем офлайн, без запросов.")
            icons_update_watcher(cfg, on_progress=_icons_emit,
                                 on_done=_icons_done, stop_event=_icons_stop)
        except Exception as e:
            log(f"Иконки: фоновая загрузка не запущена "
                f"({type(e).__name__}: {e})")
    else:
        log("Иконки: модуль icon_pack.py не найден — работают только "
            "иконки по требованию.")
    try:
        app.exec_()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            _icons_stop.set()
        except Exception:
            pass
        if auto_update_watcher is not None:
            try:
                auto_update_watcher.stop()
            except Exception:
                pass
        if unified is not None:
            unified.save_state()
        if events_ctl is not None:
            events_ctl.stop()
        if collector is not None:
            try:
                collector.stop()
            except Exception:
                pass
        if tailer is not None:
            tailer.stop()
        if watcher is not None:
            watcher.stop()
        if notify_poller is not None:
            try:
                notify_poller.stop()
            except Exception:
                pass
        if notify_tray is not None:
            try:
                notify_tray.stop()
            except Exception:
                pass
        if toast_stack is not None:
            try:
                toast_stack.hide_all()
            except Exception:
                pass
        if chat_service is not None:
            try:
                chat_service.stop()
            except Exception:
                pass
        if status_service is not None:
            try:
                status_service.stop(restore=True)
            except Exception:
                pass


def run_selftest(cfg, image_path):
    import cv2
    img = imread_unicode(image_path, cv2.IMREAD_UNCHANGED)
    if img is None:
        log(f"Не открыть файл: {image_path}")
        return 1
    log(f"Картинка: {img.shape}")
    ocr = OcrEngine(cfg)
    base = to_base(img, cfg) if img.shape[1] != cfg["base_width"] else img
    cards = detect_cards(base, cfg, ocr, debug=True)
    if not cards:
        log("Карточки не найдены")
        return 2
    for c in cards:
        log(f"  left={c['left']:.0f}  name={c['name']!r}")
    return 0


def run_test_api(cfg, name):
    api = MarketAPI(cfg)
    r = api.lookup(name)
    log(json.dumps(r, ensure_ascii=False, indent=2))
    return 0 if r.get("matched") else 2


def run_calibrate(cfg):
    import cv2
    full, mon = grab(cfg)
    base = to_base(full, cfg)
    ocr = OcrEngine(cfg)
    cards = detect_cards(base, cfg, ocr, debug=True)
    dbg = base.copy()
    for c in cards:
        import numpy as _np  # noqa: F401
        x0 = int(c["left"])
        y0 = int(c["top"])
        cv2.rectangle(dbg, (x0, y0),
                      (x0 + cfg["card_width"], y0 + cfg["card_height"]),
                      (0, 255, 0), 2)
        cv2.putText(dbg, c["name"][:28], (x0, y0 - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1, cv2.LINE_AA)
    out = os.path.join(cfg["debug_dir"], "calibration.png")
    imwrite_unicode(out, dbg)
    log(f"Найдено карточек: {len(cards)}")
    for c in cards:
        log(f"  left={c['left']:.0f}  {c['name']!r}")
    log(f"Разметка сохранена: {out}")
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="VoidFrameRU",
                                description="Warframe Reward Price Overlay (wfinfo-аналог)")
    p.add_argument("--calibrate", action="store_true",
                   help="сохранить скриншот с разметкой найденных карточек")
    p.add_argument("--selftest", metavar="PNG",
                   help="прогнать распознавание по готовой картинке")
    p.add_argument("--test-api", metavar="NAME",
                   help="проверить поиск предмета и цены без OCR")
    p.add_argument("--config", metavar="PATH", help="путь к config.json")
    p.add_argument("--gpu", action="store_true", help="использовать GPU для EasyOCR")
    p.add_argument("--no-update", action="store_true",
                   help="не проверять обновления (обрабатывает лаунчер)")
    p.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
    return p.parse_args(argv)


def apply_display_settings(settings_cfg, summary=None, drops_panel=None,
                           market_browser=None):
    """1.0.0 (t46): один способ применить настройки отображения цен."""
    for target in (summary, drops_panel, market_browser):
        apply = getattr(target, "apply_cfg", None)
        if callable(apply):
            try:
                apply(settings_cfg)
            except Exception as e:
                log(f"Настройки: apply_cfg не сработал "
                    f"({type(e).__name__}: {e})")
    return True


def resolve_prices_parallel(api, names, cache=None, workers=4,
                            timeout=1.5, logger=None):
    """Параллельно получить цены (до 4 воркеров, общий таймаут пачки).

    Кэш переиспользуется; недостающие/зависшие предметы получают
    ``price=None`` (плашки к этому моменту уже нарисованы с именами).
    Одна строка в лог: ``Цены: 4 предмета за 0.9 с (кэш 2)``.
    """
    from concurrent.futures import (ThreadPoolExecutor, as_completed,
                                    TimeoutError as FuturesTimeout)
    started = time.time()
    names = list(dict.fromkeys(str(n) for n in (names or []) if str(n)))
    out = {}
    pending = []
    cached = 0
    for name in names:
        value = cache.price_get(name) if cache is not None else None
        if value is not None:
            out[name] = dict(value)
            cached += 1
        else:
            pending.append(name)
    if pending:
        workers = max(1, min(4, int(workers or 4)))
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = {pool.submit(api.lookup, name): name
                       for name in pending}
            try:
                for future in as_completed(futures, timeout=float(timeout)):
                    name = futures[future]
                    try:
                        value = future.result()
                    except Exception as exc:
                        log(f"  API: {name}: {type(exc).__name__}")
                        value = {"name": name, "price": None,
                                 "matched": False, "error": "ошибка сети"}
                    out[name] = dict(value or {})
                    if cache is not None:
                        cache.price_set(name, value)
            except FuturesTimeout:
                log("  API: таймаут пачки цен")
        finally:
            pool.shutdown(wait=False)
        for name in pending:
            if name not in out:
                out[name] = {"name": name, "price": None,
                             "matched": False, "error": "таймаут цен"}
    elapsed = time.time() - started
    message = (f"Цены: {len(names)} предмета за {elapsed:.1f} с "
               f"(кэш {cached}, сеть "
               f"{max(0, len(names) - cached)})")
    try:
        (logger or log)(message)
    except Exception:
        pass
    return out


def main(argv=None):
    raw = list(sys.argv[1:] if argv is None else argv)
    try:
        stream = getattr(sys, "stdout", None)
        if stream is not None:
            stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
        except Exception:
            pass
        log(startup_line())
        if "--version" in raw:
            line = f"VoidFrameRU {APP_VERSION}"
            log(line)
            try:
                if getattr(sys, "stdout", None) is not None:
                    print(line, flush=True)
            except Exception:
                pass
            return 0
        args = parse_args(raw)
        enable_dpi_awareness()
        # 1.0.0: перенос данных старой версии (один раз, до чтения config)
        try:
            run_data_migration()
        except Exception as exc:
            log(f"Данные: миграция не выполнена ({type(exc).__name__})")
        cfg = load_config(args.config or CONFIG_PATH)
        ensure_dirs(cfg)
        if args.calibrate:
            return run_calibrate(cfg)
        if args.selftest:
            return run_selftest(cfg, args.selftest)
        if args.test_api:
            return run_test_api(cfg, args.test_api)
        run_gui(cfg)
        return 0
    except SystemExit:
        raise
    except Exception as exc:
        log_exception(f"Ошибка запуска: {type(exc).__name__}: {exc}")
        show_startup_error(exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
