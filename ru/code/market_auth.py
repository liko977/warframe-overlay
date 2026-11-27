# -*- coding: utf-8 -*-
"""Вход на warframe.market прямо из приложения (встроенное окно браузера).

См. AUTH_SPEC.md. Идея простая: пользователь один раз входит в аккаунт
**на самом сайте** во встроенном браузере (PyQtWebEngine), сайт ставит
HttpOnly-cookie ``JWT``, приложение забирает её из нативного cookie-store
движка (из JavaScript HttpOnly не читается, а из cookie-store — читается),
проверяет через ``GET /v2/me`` и сохраняет в ``config.json`` (``market_token``).

Правила безопасности
--------------------
1. **Пароль нигде не спрашивается, не сохраняется и не логируется.** Его
   вводит пользователь в форму сайта внутри встроенного браузера; наш код
   пароля не видит.
2. **Токен никогда не печатается и не логируется** — только маска
   ``••••1234``. Все сообщения прогоняются через :func:`scrub`, который
   вырезает похожие на JWT строки (страховка от случайной утечки).
3. Сеть (проверка ``/v2/me``) выполняется **вне GUI-потока**.
4. t194: браузерного входа больше нет — токен вводится вручную в диалоге
   ``_TokenDialog`` (warframe.py), а модуль только проверяет и сохраняет его
   (``accept_token`` / ``verify_token`` / ``logout``).
5. Модуль самостоятельный: не импортирует ``warframe.py`` и не требует Qt на
   уровне модуля — поэтому его легко тестировать (``tests_auth.py``).

Пример без UI (проверка и сохранение токена)::

    import market_auth
    ctl = market_auth.MarketAuthController(
        cfg, market_client, save_fn=save_market_token, data_dir=DATA_DIR)
    result = ctl.accept_token("eyJhbGciOi…")
    result["state"]      # "ok" | "guest" | "expired" | "no_network" | …
    result["message"]    # русский текст без токена
"""

import base64
import json
import os
import re
import threading
import time

try:                        # клиент рынка лежит рядом; нужен только ради
    import market_client as mc   # масок/нормализации и классов ошибок
except Exception:                # pragma: no cover - на всякий случай
    mc = None

SITE = "https://warframe.market"
LOGIN_URL = SITE + "/login"
PROFILE_FOLDER = "wf_market_webview"    # папка persistent-профиля движка
PROFILE_NAME = "wf-market"              # имя профиля PyQtWebEngine
MIN_TOKEN_LEN = 20                      # короче — это не JWT

#: Современный User-Agent: QtWebEngine иначе подставляет «QtWebEngine/5.15»,
#: по чему Cloudflare может опознать старый движок (AUTH_SPEC §4.2).
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ── состояния входа (используются в UI и тестах) ────────────────────────
STATE_OK = "ok"                 # 200 от /v2/me — вошли
STATE_EMPTY = "empty"           # токена нет
STATE_GUEST = "guest"           # 401, но токен гостевой (auth_type=cookie)
STATE_EXPIRED = "expired"       # 401 и срок действия истёк
STATE_UNAUTHORIZED = "unauthorized"   # 401 по другой причине
STATE_NO_NETWORK = "no_network"       # нет связи (не повод стирать токен)
STATE_ERROR = "error"                 # прочая ошибка проверки
STATE_NO_ENGINE = "no_engine"         # PyQtWebEngine недоступен

STATE_TEXT = {
    STATE_OK: "вход выполнен",
    STATE_EMPTY: "гость (нет токена)",
    STATE_GUEST: "гость (нужен вход)",
    STATE_EXPIRED: "токен истёк",
    STATE_UNAUTHORIZED: "токен не принят",
    STATE_NO_NETWORK: "нет связи",
    STATE_ERROR: "ошибка проверки",
    STATE_NO_ENGINE: "браузер недоступен",
}

#: Похоже на JWT: три base64url-сегмента через точку. Нужен только для
#: маскирования в сообщениях (страховка логов), не для проверки подписи.
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{2,}")

# ─────────────────────────────────────────────────────────────────────────
#  Проверка доступности движка (ленивая, без падений)
# ─────────────────────────────────────────────────────────────────────────
_ENGINE = {"checked": False, "ok": False, "reason": ""}
_QT_ATTR_READY = False


def _engine_reason(exc):
    """Короткая русская причина, почему встроенный браузер недоступен."""
    text = str(exc or "")
    name = type(exc).__name__
    if "before a QCoreApplication" in text or "AA_ShareOpenGLContexts" in text:
        return ("движок браузера подключён слишком поздно: нужен "
                "Qt.AA_ShareOpenGLContexts до создания QApplication")
    if isinstance(exc, ModuleNotFoundError) or "No module named" in text:
        return ("модуль PyQtWebEngine не установлен "
                "(pip install PyQtWebEngine==5.15.7)")
    if "DLL" in text or "dll" in text or name in ("ImportError", "OSError"):
        return "не загружаются библиотеки QtWebEngine (нужна пересборка приложения)"
    return f"движок браузера не запустился ({name})"


def prepare_qt_for_webengine():
    """Разрешает ленивый импорт QtWebEngine после создания QApplication.

    QtWebEngine требует ``Qt.AA_ShareOpenGLContexts`` **до** ``QGuiApplication``,
    иначе ``import PyQt5.QtWebEngineWidgets`` падает с ImportError:
    «must be imported or Qt.AA_ShareOpenGLContexts must be set before a
    QCoreApplication instance is created». Атрибут безопасен и без движка
    (PyQtWebEngine может быть не установлен), поэтому вызывать можно всегда.

    Вызывается из ``warframe.run_gui`` перед ``QApplication([])``; двигатель
    при этом остаётся ленивым — сам PyQtWebEngine импортируется только при
    открытии окна входа.
    """
    global _QT_ATTR_READY
    if _QT_ATTR_READY:
        return True
    try:
        from PyQt5.QtCore import QCoreApplication, Qt
        from PyQt5.QtWidgets import QApplication
        attr = getattr(Qt, "AA_ShareOpenGLContexts", None)
        if attr is None:
            return False
        if QApplication.instance() is None:
            QCoreApplication.setAttribute(attr, True)
        # если QApplication уже создан, менять атрибут поздно: engine_status()
        # увидит это через testAttribute()/проверку выше и честно скажет,
        # что движок недоступен (ручная вставка токена останется)
        _QT_ATTR_READY = True
        return True
    except Exception:
        return False


def _late_import_safe():
    """Можно ли импортировать QtWebEngine сейчас, не уронив процесс.

    Импорт после ``QApplication`` без ``AA_ShareOpenGLContexts`` не просто
    падает с ImportError: QtWebEngine успевает частично инициализироваться и
    может уронить приложение при завершении. Поэтому такой импорт не делаем.
    """
    try:
        from PyQt5.QtCore import QCoreApplication, Qt
        from PyQt5.QtWidgets import QApplication
        if QApplication.instance() is None:
            return True
        attr = getattr(Qt, "AA_ShareOpenGLContexts", None)
        if attr is None:
            return False
        return bool(QCoreApplication.testAttribute(attr))
    except Exception:
        return False


def engine_status(refresh=False):
    """``(доступен, причина)`` — можно ли открыть встроенный браузер.

    Импорт PyQtWebEngine выполняется только здесь и только по требованию:
    если движка нет, приложение обязано работать как раньше (AUTH_SPEC §8.3).
    """
    if refresh:
        _ENGINE["checked"] = False
        _ENGINE["ok"] = False
        _ENGINE["reason"] = ""
    if not _ENGINE["checked"]:
        _ENGINE["checked"] = True
        prepare_qt_for_webengine()   # атрибут нужен, если QApplication ещё не было
        if not _late_import_safe():
            _ENGINE["ok"] = False
            _ENGINE["reason"] = ("движок браузера не подготовлен: нужен "
                                 "Qt.AA_ShareOpenGLContexts до QApplication")
            return False, _ENGINE["reason"]
        try:
            from PyQt5.QtWebEngineWidgets import QWebEngineView  # noqa: F401
            _ENGINE["ok"] = True
            _ENGINE["reason"] = ""
        except Exception as exc:      # нет модуля / битые DLL / нет Qt
            _ENGINE["ok"] = False
            _ENGINE["reason"] = _engine_reason(exc)
    return bool(_ENGINE["ok"]), str(_ENGINE["reason"])


def engine_unavailable_text(reason=""):
    """Понятное объяснение для окна входа (когда движка нет)."""
    reason = reason or engine_status()[1] or "движок браузера не установлен"
    return (
        "Встроенный браузер в этой сборке недоступен:\n"
        f"   {reason}.\n\n"
        "Войти всё равно можно вручную — вставкой токена (запасной путь):\n"
        "1. Откройте https://warframe.market в своём браузере и войдите.\n"
        "2. DevTools (F12) → вкладка Network → обновите страницу.\n"
        "3. Любой запрос к api.warframe.market → Request Headers →\n"
        "   Authorization: Bearer eyJ… — скопируйте значение после «Bearer ».\n"
        "4. Вставьте его в поле ниже и нажмите «Сохранить токен».\n\n"
        "Чтобы включить вход через встроенное окно, установите\n"
        "PyQtWebEngine==5.15.11 (или скачайте новую сборку приложения).")


def set_engine_available(ok, reason=""):
    """Принудительно задаёт доступность движка (диагностика и тесты)."""
    _ENGINE["checked"] = True
    _ENGINE["ok"] = bool(ok)
    _ENGINE["reason"] = str(reason or "")


# ─────────────────────────────────────────────────────────────────────────
#  Токен: нормализация, маска, разбор JWT
# ─────────────────────────────────────────────────────────────────────────
def normalize_token(value):
    """Чистый JWT без ``Bearer``/``JWT``/кавычек/пробелов (как в клиенте)."""
    if mc is not None:
        try:
            return mc.normalize_token(value)
        except Exception:
            pass
    text = str(value or "").strip().strip('"').strip("'").strip()
    for prefix in ("Bearer ", "bearer ", "JWT ", "jwt "):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    return text


def mask_token(token):
    """Безопасное представление токена для UI/логов (никогда не целиком)."""
    if mc is not None:
        try:
            return mc.mask_token(token)
        except Exception:
            pass
    token = normalize_token(token)
    if not token:
        return "нет токена"
    tail = token[-4:] if len(token) >= 8 else ""
    return f"••••{tail}" if tail else "••••"


def scrub(text):
    """Вырезает из строки всё похожее на JWT (страховка от утечки в лог)."""
    return _JWT_RE.sub("••••(токен скрыт)", str(text or ""))


def looks_like_jwt(value):
    """Грубая проверка формы JWT (без проверки подписи — это делает сервер)."""
    token = normalize_token(value)
    return token.count(".") == 2 and len(token) >= MIN_TOKEN_LEN


def jwt_payload(token):
    """Payload JWT (dict) или ``{}``, если разобрать нельзя.

    Подпись НЕ проверяется: payload нужен только для подсказок (``exp``,
    ``auth_type``); источник истины — ответ ``GET /v2/me`` (AUTH_SPEC §6.2).
    """
    token = normalize_token(token)
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    segment = parts[1]
    segment += "=" * (-len(segment) % 4)
    try:
        raw = base64.urlsafe_b64decode(segment.encode("ascii", "replace"))
        payload = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def token_expiry(token):
    """Unix-время окончания действия (или ``None``)."""
    try:
        return float(jwt_payload(token).get("exp"))
    except Exception:
        return None


def token_expired(token, now=None):
    """Истёк ли токен по ``exp`` (без сети; ``None`` в exp → False)."""
    expiry = token_expiry(token)
    if expiry is None:
        return False
    return expiry <= (time.time() if now is None else float(now))


def is_guest_token(token):
    """Гостевой токен сайта: ``payload.auth_type == "cookie"`` (AUTH_SPEC §6.2)."""
    return str(jwt_payload(token).get("auth_type") or "").strip().lower() == "cookie"


def classify_rejected(token):
    """401 без сети: гость / истёк / просто не принят."""
    if is_guest_token(token):
        return STATE_GUEST
    if token_expired(token):
        return STATE_EXPIRED
    return STATE_UNAUTHORIZED


# ─────────────────────────────────────────────────────────────────────────
#  Перехват HttpOnly-cookie JWT
# ─────────────────────────────────────────────────────────────────────────
def _as_bytes(value):
    """QByteArray/bytes/str → bytes (QNetworkCookie отдаёт QByteArray)."""
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    try:
        return bytes(value)          # QByteArray и bytearray
    except Exception:
        return str(value).encode("utf-8", "replace")


def _as_text(value):
    """QByteArray/bytes/str → str (для JWT — ASCII, но декодируем мягко)."""
    return _as_bytes(value).decode("utf-8", "replace").strip()


def cookie_token(name, domain, value):
    """Имя/домен/значение cookie → JWT или ``""``.

    Фильтр из AUTH_SPEC §4.3: нас интересует только cookie ``JWT`` домена
    ``warframe.market`` (сайт ставит её с ``Domain=.warframe.market``,
    ``Secure`` и ``HttpOnly``). Значение читается из нативного cookie-store,
    поэтому HttpOnly не мешает.
    """
    if _as_text(name).upper() != "JWT":
        return ""
    host = _as_text(domain).lower().lstrip(".").strip()
    if host != "warframe.market" and not host.endswith(".warframe.market"):
        return ""
    token = _as_text(value)
    return token if looks_like_jwt(token) else ""


def _cookie_parts(cookie):
    """``QNetworkCookie`` (или словарь/кортеж) → ``(name, domain, value)``."""
    if cookie is None:
        return b"", b"", b""
    if isinstance(cookie, dict):
        return (cookie.get("name", ""), cookie.get("domain", ""),
                cookie.get("value", ""))
    if isinstance(cookie, (list, tuple)) and len(cookie) >= 3:
        return cookie[0], cookie[1], cookie[2]
    result = []
    for attr in ("name", "domain", "value"):
        try:
            member = getattr(cookie, attr, None)
            result.append(member() if callable(member) else member)
        except Exception:
            result.append(b"")
    return tuple(result)


#: Приёмник лога (warframe.log или тестовый список) и последние 200 сообщений.
_LOGGER = None
_LOG_LOCK = threading.Lock()
_LOG_BUFFER = []


def set_logger(fn):
    """Подменяет приёмник лога (``warframe.log`` или тестовый список)."""
    global _LOGGER
    _LOGGER = fn if callable(fn) else None


def log_messages():
    """Накопленные сообщения (для тестов/диагностики; токенов там нет)."""
    with _LOG_LOCK:
        return list(_LOG_BUFFER)


def clear_log():
    with _LOG_LOCK:
        del _LOG_BUFFER[:]


def _log(message):
    text = scrub(message)
    with _LOG_LOCK:
        _LOG_BUFFER.append(text)
        del _LOG_BUFFER[:-200]           # держим последние 200 строк
    target = _LOGGER
    if target is not None:
        try:
            target(text)
            return
        except Exception:
            pass
    try:
        print(text, flush=True)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────
#  Контроллер: проверка / сохранение / выход (без Qt)
# ─────────────────────────────────────────────────────────────────────────
def _error_state(exc):
    """Класс ошибки клиента → состояние авторизации."""
    if mc is not None:
        if isinstance(exc, mc.MarketAuthError):
            return STATE_UNAUTHORIZED
        if isinstance(exc, mc.MarketNetworkError):
            return STATE_NO_NETWORK
    name = type(exc).__name__
    if name == "MarketAuthError":
        return STATE_UNAUTHORIZED
    if name == "MarketNetworkError":
        return STATE_NO_NETWORK
    return STATE_ERROR


def _engine_error_reason_text(exc):
    return f"проверка токена не удалась ({type(exc).__name__})"


class MarketAuthController:
    """Логика входа warframe.market, независимая от GUI.

    :param cfg: настройки оверлея; читает ``market_store_token`` и
        ``market_login_session_only``, пишет ``market_token`` (через ``save_fn``).
    :param client: ``market_client.MarketOrderClient`` (или совместимый объект
        с ``set_token``/``has_token``/``token``/``_request``).
    :param save_fn: ``save_fn(cfg, token) -> bool`` — сохранение в config.json
        (в приложении это ``warframe.save_market_token``).
    :param data_dir: папка данных (рядом с config.json) для профиля браузера.
    :param verify: необязательная подмена проверки токена ``verify(token) ->
        (state, message)`` (используется в тестах).
    """

    def __init__(self, cfg, client=None, save_fn=None, data_dir="",
                 verify=None, profile_factory=None, logger=None,
                 store_token=None, session_only=None):
        self.cfg = cfg if isinstance(cfg, dict) else {}
        self.client = client
        self.save_fn = save_fn
        self.data_dir = str(data_dir or self.cfg.get("data_dir") or "")
        self.verify = verify if callable(verify) else None
        self.profile_factory = profile_factory
        self._logger = logger
        if logger is not None:
            set_logger(logger)
        if store_token is None:
            store_token = bool(self.cfg.get("market_store_token", True))
        self._store_token = bool(store_token)
        if session_only is None:
            session_only = bool(self.cfg.get("market_login_session_only", False))
        self._session_only = bool(session_only)
        self.last_state = STATE_EMPTY
        self.last_message = ""
        self._lock = threading.RLock()
        self._profile = None

    # ── свойства и мелкие помощники ────────────────────────────────────
    def store_token(self):
        """Сохранять ли JWT в config.json (``market_store_token``)."""
        return bool(self._store_token)

    def session_only(self):
        """Не сохранять cookie профиля (``market_login_session_only``)."""
        return bool(self._session_only)

    def profile_dir(self):
        """Папка persistent-профиля встроенного браузера."""
        base = self.data_dir or "."
        return os.path.join(base, PROFILE_FOLDER)

    def current_token(self):
        """Токен из памяти клиента (никогда не печатать без маски)."""
        client = self.client
        if client is None:
            return ""
        getter = getattr(client, "token", None)
        if callable(getter):
            try:
                return normalize_token(getter())
            except Exception:
                return ""
        return ""

    def _set_client_token(self, token):
        """Устанавливает токен в памяти клиента (пустая строка — очистить)."""
        client = self.client
        setter = getattr(client, "set_token", None) if client is not None else None
        if callable(setter):
            try:
                setter(normalize_token(token))
                return True
            except Exception:
                return False
        return False

    def has_token(self):
        """Есть ли токен (для чипа в шапке окна)."""
        client = self.client
        checker = getattr(client, "has_token", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                return False
        return bool(self.current_token())

    def _result(self, state, message, token=""):
        """Единый формат ответа; в тексте только маска токена."""
        text = scrub(message)
        return {
            "state": state,
            "ok": state == STATE_OK,
            "message": text,
            "status": STATE_TEXT.get(state, str(state)),
            "masked": mask_token(token) if token else "",
        }

    # ── профиль и cookie (Qt только по требованию) ─────────────────────
    def profile(self):
        """Persistent-профиль ``wf-market`` (кэшируется на процесс).

        Профиль всегда «висит» на ``QApplication`` и живёт до конца процесса:
        если сделать его дочерним для окна, закрытие окна уничтожило бы
        хранилище cookie, а кэш остался бы с битым объектом. Для выхода из
        аккаунта профиль нужен и без открытого окна.

        Возвращает ``None``, если PyQtWebEngine недоступен или Qt ещё не
        создан: приложение при этом продолжает работать (ручная вставка).
        """
        if self._profile is not None:
            return self._profile
        path = os.path.abspath(self.profile_dir())
        available, reason = engine_status()
        if not available:
            _log(f"вход: встроенный браузер недоступен — {reason}")
            return None
        try:
            # Chromium-песочница QtWebEngine требует записи в системные папки
            # (LocalLow\...\QtWebEngine) и в ограниченных окружениях (запуск
            # из защищённой папки, портативная сборка) падает с FATAL
            # platform_channel.cc. Мы открываем только warframe.market,
            # поэтому отключаем её ДО первого запуска движка.
            os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
            prepare_qt_for_webengine()
            from PyQt5.QtWidgets import QApplication
            app = QApplication.instance()
            if app is None:
                _log("вход: встроенный браузер недоступен (GUI ещё не создан)")
                return None
            from PyQt5.QtWebEngineWidgets import QWebEngineProfile
            cached = _PROFILES.get(path)
            if cached is not None:
                self._profile = cached
                return cached
            # Кэш и cookie держим только в папке данных приложения: иначе
            # Chromium уходит в %LocalAppData%Low и может не иметь прав записи.
            storage = os.path.join(path, "storage")
            cache = os.path.join(path, "cache")
            os.makedirs(storage, exist_ok=True)
            os.makedirs(cache, exist_ok=True)
            profile = QWebEngineProfile(PROFILE_NAME, app)
            profile.setPersistentStoragePath(storage)
            profile.setCachePath(cache)
            profile.setHttpCacheType(QWebEngineProfile.DiskHttpCache)
            if self.session_only():
                # вход живёт до закрытия приложения (AUTH_SPEC §7.2, уровень 2)
                profile.setPersistentCookiesPolicy(
                    QWebEngineProfile.NoPersistentCookies)
            else:
                profile.setPersistentCookiesPolicy(
                    QWebEngineProfile.ForcePersistentCookies)
            profile.setHttpUserAgent(USER_AGENT)
            _PROFILES[path] = profile
            self._profile = profile
            _log(f"вход: профиль браузера готов ({path})")
            return profile
        except Exception as exc:
            _log(f"вход: профиль браузера не создан ({type(exc).__name__})")
            return None

    def cookie_store(self, profile=None):
        """``QWebEngineCookieStore`` профиля (или ``None``)."""
        profile = profile if profile is not None else self.profile()
        if profile is None:
            return None
        try:
            return profile.cookieStore()
        except Exception as exc:
            _log(f"вход: cookie-store недоступен ({type(exc).__name__})")
            return None

    def delete_cookies(self, profile=None):
        """Удаляет cookie профиля (logout). ``False`` — движок недоступен."""
        store = self.cookie_store(profile)
        if store is None:
            _log("выход: cookie движка не удалены (браузер недоступен)")
            return False
        try:
            store.deleteAllCookies()
            _log("выход: cookie профиля warframe.market удалены")
            return True
        except Exception as exc:
            _log(f"выход: cookie не удалены ({type(exc).__name__})")
            return False

    # ── проверка токена (сеть; вызывать вне GUI-потока) ────────────────
    def _check_me(self):
        """``GET /v2/me`` текущим токеном клиента → состояние."""
        client = self.client
        request = getattr(client, "_request", None)
        if callable(request):
            request("GET", "/me", auth=True)      # 401 → MarketAuthError
            return STATE_OK
        checker = getattr(client, "token_ok", None)
        if callable(checker):
            return STATE_OK if checker() else STATE_UNAUTHORIZED
        raise RuntimeError("клиент warframe.market не умеет проверять токен")

    def verify_token(self, token, classify=True):
        """Проверяет JWT через ``GET /v2/me`` (Bearer) и возвращает состояние.

        Ничего не сохраняет на диск — этим занимается :meth:`accept_token`.
        Токен в сообщениях только маской.
        """
        token = normalize_token(token)
        if not token:
            return self._result(STATE_EMPTY, STATE_TEXT[STATE_EMPTY])
        if not looks_like_jwt(token):
            return self._result(
                STATE_UNAUTHORIZED,
                "это не похоже на JWT: скопируйте токен целиком "
                "(он длинный и состоит из трёх частей через точку)", token)
        client = self.client
        if self.verify is not None:
            # подмена проверки (тесты): возвращает кортеж/dict/строку-состояние
            try:
                raw = self.verify(token)
            except Exception as exc:
                return self._result(_error_state(exc),
                                    _engine_error_reason_text(exc), token)
            state, message = _coerce_verify_result(raw)
        else:
            if client is None:
                return self._result(STATE_ERROR,
                                    "клиент warframe.market недоступен", token)
            setter = getattr(client, "set_token", None)
            if callable(setter):
                try:
                    setter(token)
                except Exception as exc:
                    return self._result(STATE_ERROR,
                                        _engine_error_reason_text(exc), token)
            try:
                state = self._check_me()
                message = ""
            except Exception as exc:
                state = _error_state(exc)
                message = ""
        if state == STATE_UNAUTHORIZED and classify:
            state = classify_rejected(token)
        if not message:
            message = self._describe(state, token)
        return self._result(state, message, token)

    def _describe(self, state, token):
        """Русский текст состояния (токен — только маской)."""
        masked = mask_token(token)
        if state == STATE_OK:
            return f"вход выполнен: {masked}"
        if state == STATE_GUEST:
            return ("на сайте вы не вошли (гость) — войдите в аккаунт "
                    "в открытом окне браузера")
        if state == STATE_EXPIRED:
            return "срок действия токена истёк — войдите заново"
        if state == STATE_UNAUTHORIZED:
            return "токен не принят (401) — войдите в аккаунт заново"
        if state == STATE_NO_NETWORK:
            return ("нет связи с warframe.market — проверьте интернет "
                    "и нажмите «Проверить вход» ещё раз")
        if state == STATE_EMPTY:
            return ("токена нет — войдите через окно браузера "
                    "или вставьте JWT вручную")
        return "не удалось проверить токен — попробуйте ещё раз"

    # ── приём и сохранение токена ──────────────────────────────────────
    def accept_token(self, token, persist=True):
        """Проверяет токен и (если он валиден) сохраняет его.

        Сохранение — только при ``market_store_token=true``; иначе JWT живёт
        в памяти клиента до выхода из приложения (AUTH_SPEC §7.2).
        """
        with self._lock:
            previous = self.current_token()
            result = self.verify_token(token)
            state = result["state"]
            self.last_state = state
            self.last_message = result["message"]
            if state != STATE_OK:
                # не оставляем невалидный токен в памяти клиента: иначе чип
                # в шапке стал бы зелёным без реального входа
                self._set_client_token(previous)
                _log(f"вход не выполнен: {result['message']}")
                return result
            token = normalize_token(token)
            saved = False
            if not self.store_token():
                # токен не пишем на диск: затираем и старый, если он был
                self.cfg["market_token"] = ""
                if callable(self.save_fn):
                    try:
                        self.save_fn(self.cfg, "")
                    except Exception:
                        pass
                result["message"] = (f"вход выполнен: {result['masked']} — "
                                     "токен только в памяти (не сохраняется)")
                _log("вход выполнен: токен только в памяти "
                     "(market_store_token=false)")
            elif persist:
                if callable(self.save_fn):
                    try:
                        saved = bool(self.save_fn(self.cfg, token))
                    except Exception as exc:
                        _log(f"вход: токен не сохранён ({type(exc).__name__})")
                        saved = False
                if saved:
                    self.cfg["market_token"] = token
                    result["message"] = (f"вход выполнен: {result['masked']} — "
                                         "токен сохранён в config.json")
                    _log(f"вход выполнен: {result['masked']}, "
                         "токен сохранён в config.json")
                else:
                    result["message"] = (f"вход выполнен: {result['masked']} — "
                                         "но сохранить токен не удалось")
                    _log("вход выполнен, но сохранить токен не удалось")
            else:
                # persist=False: токен только в памяти этого сеанса,
                # config.json не трогаем вообще
                result["message"] = (f"вход выполнен: {result['masked']} — "
                                     "токен не сохранялся (persist=False)")
                _log("вход выполнен: токен не сохранялся (persist=False)")
            result["saved"] = bool(saved)
            return result

    def check_current(self):
        """Проверяет токен, который уже есть у клиента (при открытии окна)."""
        token = self.current_token()
        if not token:
            self.last_state = STATE_EMPTY
            self.last_message = self._describe(STATE_EMPTY, "")
            return self._result(STATE_EMPTY, self.last_message)
        result = self.verify_token(token)
        self.last_state = result["state"]
        self.last_message = result["message"]
        return result

    # ── выход ──────────────────────────────────────────────────────────
    def logout(self, profile=None):
        """Выход: чистит cookie профиля, токен в памяти и в config.json.

        :param profile: необязательный профиль движка (в тестах — подделка
            с ``cookieStore().deleteAllCookies()``).
        """
        with self._lock:
            deleted = self.delete_cookies(profile)
            client = self.client
            if client is not None:
                setter = getattr(client, "set_token", None)
                if callable(setter):
                    try:
                        setter("")
                    except Exception:
                        pass
            self.cfg["market_token"] = ""
            cleared = False
            if callable(self.save_fn):
                try:
                    cleared = bool(self.save_fn(self.cfg, ""))
                except Exception:
                    cleared = False
            self.last_state = STATE_EMPTY
            if deleted and cleared:
                message = "вы вышли: cookie браузера и токен удалены"
            elif deleted:
                message = "вы вышли: cookie браузера удалены, config.json не обновлён"
            elif cleared:
                message = ("вы вышли: токен удалён, но cookie браузера остались "
                           "(браузер недоступен)")
            else:
                message = "вы вышли из аккаунта в приложении"
            self.last_message = message
            _log("выход: " + message)
            return {"state": STATE_EMPTY, "ok": True, "message": message,
                    "cookies_deleted": bool(deleted),
                    "config_cleared": bool(cleared)}

    # ── статус для UI ──────────────────────────────────────────────────
    def status_text(self):
        """Короткий статус (гость / вошли / истёк) — без токена целиком."""
        token = self.current_token()
        if not token:
            if self.last_state == STATE_EXPIRED:
                return "истёк: войдите заново"
            if self.last_state in (STATE_GUEST, STATE_UNAUTHORIZED):
                return "гость: нужен вход"
            return "гость (нет токена)"
        masked = mask_token(token)
        if self.last_state == STATE_OK:
            return f"вошли: {masked}"
        if self.last_state == STATE_EXPIRED:
            return "истёк: войдите заново"
        if self.last_state in (STATE_GUEST, STATE_UNAUTHORIZED):
            return "гость: нужен вход"
        if self.last_state == STATE_NO_NETWORK:
            return f"токен {masked}: нет связи"
        return f"токен {masked} (не проверен)"


#: Кэш persistent-профилей: один QWebEngineProfile на папку данных.
#: Qt не любит несколько профилей с одним именем/путём, поэтому кэш обязателен
#: (и заодно позволяет выйти из аккаунта без открытого окна браузера).
_PROFILES = {}


def _coerce_verify_result(raw):
    """Приводит ответ ``verify`` к ``(state, message)``."""
    if isinstance(raw, dict):
        return (str(raw.get("state") or STATE_ERROR),
                str(raw.get("message") or ""))
    if isinstance(raw, (tuple, list)):
        state = str(raw[0] if raw else STATE_ERROR)
        message = str(raw[1]) if len(raw) > 1 else ""
        return state, message
    if isinstance(raw, bool):
        return (STATE_OK if raw else STATE_UNAUTHORIZED), ""
    return str(raw or STATE_ERROR), ""


# ─────────────────────────────────────────────────────────────────────────
#  Окно входа (PyQt5/PyQtWebEngine импортируются лениво)
# ─────────────────────────────────────────────────────────────────────────
