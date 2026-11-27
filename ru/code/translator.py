# -*- coding: utf-8 -*-
"""Переводчик чата (Alt+T): Yandex Cloud Translate v2 + кэш и лимит.

Только перевод сообщений чата по запросу — никакого авто-перевода и никаких
переводов интерфейса. HTTP — тот же ``requests``, что и в ``market_client``.
Ошибки не бросаются в UI: возвращается исходный текст и ``info["error"]``.
"""

import hashlib
import json
import os
import threading
import time

API_URL = "https://translate.api.cloud.yandex.net/translate/v2/translate"
FREE_URL = "https://api.mymemory.translated.net/get"
#: бесплатный лимит ≈5000 символов в день на IP — режем запрос до 450
FREE_LIMIT_CHARS = 450
PROVIDERS = ("auto", "yandex", "free")
CACHE_FILE = "translate_cache.json"
CACHE_LIMIT = 500
RATE_LIMIT = 10
RATE_WINDOW = 60.0
TIMEOUT = 6

#: ``callable(str)`` — лог приложения (в тестах подменяется).
log = None

_lock = threading.Lock()
_calls = []            # моменты запросов за последнюю минуту
_memory = {}           # кэш в памяти: ключ → перевод


def _log(message):
    fn = log
    if fn is None:
        try:
            import warframe
            fn = getattr(warframe, "log", None)
        except Exception:
            fn = None
    if fn is not None:
        try:
            fn(str(message))
        except Exception:
            pass


def data_dir(cfg=None):
    """Папка данных: ``cfg['data_dir']`` → ``WF_DATA_DIR`` → текущая."""
    return (str((cfg or {}).get("data_dir") or "")
            or os.environ.get("WF_DATA_DIR") or os.getcwd())


def is_russian(text):
    """Доля кириллицы среди букв > 0.5 — текст уже русский."""
    letters = [char for char in str(text or "") if char.isalpha()]
    if not letters:
        return False
    cyrillic = sum(1 for char in letters if "\u0400" <= char <= "\u04ff")
    return cyrillic / len(letters) > 0.5


def configured(cfg=None):
    """Есть ли ключ API (folderId не обязателен)."""
    return bool(str((cfg or {}).get("translate_api_key") or "").strip())


def provider_for(cfg=None):
    """Реальный провайдер: ``"yandex"`` или ``"free"`` (t196)."""
    cfg = cfg or {}
    choice = str(cfg.get("translate_provider") or "auto").strip().lower()
    if choice not in PROVIDERS:
        choice = "auto"
    if choice == "auto":
        return "yandex" if configured(cfg) else "free"
    return choice


def ready(cfg=None):
    """Можно ли переводить: выбранный провайдер доступен."""
    return provider_for(cfg) == "free" or configured(cfg)


def cache_key(text, target="ru", provider="yandex"):
    return hashlib.md5(f"{provider}:{target}:{text}".encode("utf-8")).hexdigest()


def _cache_path(cfg):
    return os.path.join(data_dir(cfg), CACHE_FILE)


def _load_cache(cfg):
    with _lock:
        if _memory:
            return _memory
    try:
        with open(_cache_path(cfg), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    with _lock:
        _memory.update({str(key): str(value) for key, value in data.items()})
        return _memory


def _save_cache(cfg, cache):
    path = _cache_path(cfg)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        temp = path + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(cache, handle, ensure_ascii=False)
        os.replace(temp, path)
    except OSError:
        pass


def cache_get(text, target="ru", cfg=None, provider="yandex"):
    key = cache_key(text, target, provider)
    cache = _load_cache(cfg)
    value = cache.get(key)
    if not value:
        return ""
    # LRU: отмечаем использование порядком ключей
    with _lock:
        cache.pop(key, None)
        cache[key] = value
    _save_cache(cfg, dict(cache))
    return value


def cache_put(text, target, translation, cfg=None, provider="yandex"):
    key = cache_key(text, target, provider)
    cache = _load_cache(cfg)
    with _lock:
        cache.pop(key, None)
        cache[key] = str(translation)
        while len(cache) > CACHE_LIMIT:
            cache.pop(next(iter(cache)))
        snapshot = dict(cache)
    _save_cache(cfg, snapshot)


def reset_state():
    """Сброс кэша в памяти и счётчика лимита (тесты/новый запуск)."""
    with _lock:
        _memory.clear()
        del _calls[:]


def _take_slot(now=None):
    """Лимит 10 запросов/мин: True — можно, False — отказ."""
    now = time.monotonic() if now is None else float(now)
    with _lock:
        while _calls and now - _calls[0] >= RATE_WINDOW:
            _calls.pop(0)
        if len(_calls) >= RATE_LIMIT:
            return False
        _calls.append(now)
        return True


def _request_yandex(text, target, key, folder):
    """Запрос перевода: ``(перевод, ошибка)``; 1 повтор при 5xx/таймауте."""
    import requests
    body = {"texts": [text], "targetLanguageCode": target}
    if folder:
        body["folderId"] = folder
    headers = {"Authorization": f"Api-Key {key}",
               "Content-Type": "application/json"}
    error = ""
    for _attempt in range(2):
        try:
            response = requests.post(API_URL, json=body, headers=headers,
                                     timeout=TIMEOUT)
        except Exception as exc:
            error = f"сеть: {type(exc).__name__}"
            continue
        if response.status_code == 200:
            try:
                translations = (response.json() or {}).get("translations") or []
                translated = str((translations[0] or {}).get("text") or "").strip()
            except (ValueError, AttributeError, IndexError, TypeError):
                translated = ""
            return (translated, "") if translated else ("", "пустой ответ")
        error = str(response.status_code)
        if int(response.status_code) < 500:
            break
    return "", error


def _request_free(text, target):
    """MyMemory (без ключа): ``(перевод, ошибка)``; 1 повтор при сбое сети."""
    import requests
    pair = "ru|en" if target == "en" else "en|ru"
    query = text[:FREE_LIMIT_CHARS]
    failed = "free_unavailable"
    for _attempt in range(2):
        try:
            response = requests.get(FREE_URL, timeout=TIMEOUT,
                                    params={"q": query, "langpair": pair})
        except Exception as exc:
            _log(f"Перевод(free): ошибка сети ({type(exc).__name__})")
            continue
        if response.status_code != 200:
            _log(f"Перевод(free): ошибка HTTP {response.status_code}")
            if int(response.status_code) < 500:
                return "", failed
            continue
        try:
            data = response.json() or {}
            translated = str((data.get("responseData") or {})
                             .get("translatedText") or "").strip()
        except (ValueError, AttributeError, TypeError):
            translated = ""
        upper = translated.upper()
        if (not translated or "MYMEMORY WARNING" in upper
                or "QUERY LENGTH LIMIT" in upper):
            _log("Перевод(free): ошибка — сервис вернул отказ")
            return "", failed
        if len(text) > FREE_LIMIT_CHARS:
            translated += "…"
        return translated, ""
    return "", failed


def translate(text, target="ru", cfg=None):
    """Перевод ``text`` → ``(перевод|исходный, info)``; исключений нет."""
    cfg = cfg or {}
    original = str(text or "").strip()
    target = "en" if str(target or "").lower().startswith("en") else "ru"
    provider = provider_for(cfg)
    started = time.monotonic()
    info = {"ok": False, "cached": False, "error": "", "skipped": "",
            "target": target, "provider": provider, "chars": len(original),
            "elapsed_ms": 0}
    if not original:
        info["error"] = "пустой текст"
        return original, info
    if is_russian(original):
        info.update(ok=True, skipped="ru")
        return original, info
    cached = cache_get(original, target, cfg, provider)
    if cached:
        info.update(ok=True, cached=True)
        _log(f"Перевод: {len(original)} симв. → {target} за 0 мс (кэш: да)")
        return cached, info
    key = str(cfg.get("translate_api_key") or "").strip()
    if provider == "yandex" and not key:
        info["error"] = "нужен ключ (Настройки → Прочее) или «Бесплатный»"
        _log("Перевод: ключ API не задан — перевод недоступен")
        return original, info
    if not _take_slot():
        info["error"] = "лимит 10 запросов/мин"
        _log("Перевод: лимит 10 запросов/мин — попробуйте через минуту")
        return original, info
    if provider == "free":
        translated, error = _request_free(original, target)
    else:
        folder = str(cfg.get("translate_folder_id") or "").strip()
        translated, error = _request_yandex(original, target, key, folder)
    info["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    if error:
        info["error"] = error
        _log(f"Перевод({provider}): ошибка {error} ({len(original)} симв.)")
        return original, info
    cache_put(original, target, translated, cfg, provider)
    info["ok"] = True
    _log(f"Перевод: {len(original)} симв. → {target} "
         f"за {info['elapsed_ms']} мс ({provider}, кэш: нет)")
    return translated, info
