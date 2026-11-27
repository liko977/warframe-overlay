# -*- coding: utf-8 -*-
"""Чат warframe.market: HTTP-история, WebSocket-обмен и вкладка оверлея.

Протокол — ``CHAT_WS_SPIKE.md``:

* HTTP ``https://api.warframe.market/v1/im/chats[/<id>]`` с заголовком
  ``Cookie: JWT=<token>`` (Bearer для IM не работает);
* WS ``wss://warframe.market/socket?platform=pc``: JSON ``{type,payload}``,
  ``SEND_MESSAGE``/``MESSAGE_SENT``, ``NEW_MESSAGE``, ``MESSAGE_WAS_READ``,
  ``@WS/ERROR``;
* без WS — опрос ``/im/chats`` раз в 30 с (режим «чат: только чтение»).

Модуль не импортирует PyQt5 на уровне модуля: парсинг/HTTP/очередь сокета
тестируются на заглушках; Qt (QThread/QWebSocket, сигналы сервиса и виджет)
подключается лениво.
"""

import datetime
import json
import threading
import time
import uuid

try:
    import ui_theme as theme
except Exception:                       # pragma: no cover
    theme = None

try:                                    # t184: переводчик чата (Alt+T)
    import translator
except Exception:                       # pragma: no cover
    translator = None


def _safe_log(context, exc=None):
    """Одна строка в app.log, без repr/traceback (1.0.0)."""
    try:
        import ui_safe
        return ui_safe.log_internal(context, exc)
    except Exception:
        return str(context or "ошибка")


HTTP_BASE = "https://api.warframe.market/v1"
CHATS_PATH = "/im/chats"
WS_URL = "wss://warframe.market/socket?platform=pc"
CHAT_SITE_URL = "https://warframe.market/im/chats"
TOKEN_MISSING_TEXT = "войдите на маркет, чтобы открыть чат"
READ_ONLY_TEXT = "чат: только чтение"
TTL_SECONDS = 25
FALLBACK_POLL_SEC = 30
KEEPALIVE_SEC = 45
RECONNECT_MIN = 5
RECONNECT_MAX = 60


# ─────────────────────────────────────────────────────────────────────────
#  Ошибки/парсинг ответов
# ─────────────────────────────────────────────────────────────────────────
class ChatError(RuntimeError):
    pass


class ChatAuthError(ChatError):
    pass


class ChatNetworkError(ChatError):
    pass


def parse_timestamp(value):
    """ISO-8601/epoch → epoch-секунды (без падения на мусоре)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except Exception:
        pass
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def format_chat_time(value, now=None):
    ts = parse_timestamp(value)
    if ts is None:
        return ""
    now = time.time() if now is None else float(now)
    dt = datetime.datetime.fromtimestamp(ts)
    if now - ts < 86400:
        return dt.strftime("%H:%M")
    return dt.strftime("%d.%m")


def parse_message(item):
    """Одно сообщение API → компактный dict."""
    item = item if isinstance(item, dict) else {}
    text = item.get("message") or item.get("raw_message") or ""
    return {
        "id": str(item.get("id") or ""),
        "chat_id": str(item.get("chat_id") or ""),
        "message_from": str(item.get("message_from") or ""),
        "message": str(text),
        "raw_message": str(item.get("raw_message") or text),
        "send_date": str(item.get("send_date") or ""),
        "ts": parse_timestamp(item.get("send_date")),
    }


def parse_messages(payload):
    """Ответ ``/im/chats/<id>`` → список сообщений (сначала старые)."""
    payload = payload if isinstance(payload, dict) else {}
    raw = payload.get("payload", payload)
    if isinstance(raw, dict):
        raw = raw.get("messages")
    if not isinstance(raw, list):
        return []
    return [parse_message(item) for item in raw if isinstance(item, dict)]


def parse_chat(item, user_id=""):
    """Чат API → нормализованная запись для списка/UI."""
    item = item if isinstance(item, dict) else {}
    messages = [parse_message(m) for m in (item.get("messages") or [])
                if isinstance(m, dict)]
    last = messages[-1] if messages else None
    chat_with = [p for p in (item.get("chat_with") or [])
                 if isinstance(p, dict)]
    partner = next((p for p in chat_with
                    if str(p.get("id") or "") != str(user_id or "")),
                   chat_with[0] if chat_with else {})
    title = (item.get("chat_name") or partner.get("ingame_name")
             or partner.get("ingameName") or "Диалог")
    status = str(partner.get("status") or partner.get("platform")
                 or "offline")
    return {
        "id": str(item.get("id") or ""),
        "chat_name": str(title),
        "chat_with": chat_with,
        "status": status,
        "last_update": str(item.get("last_update") or ""),
        "unread_count": int(item.get("unread_count") or 0),
        "messages": messages,
        "last_message": last,
    }


def parse_chats(payload, user_id=""):
    """Ответ ``/im/chats`` → ``{"chats", "unread"}``."""
    payload = payload if isinstance(payload, dict) else {}
    raw = payload.get("payload", payload)
    if isinstance(raw, dict):
        raw = raw.get("chats")
    if not isinstance(raw, list):
        return []
    chats = [parse_chat(item, user_id=user_id) for item in raw
             if isinstance(item, dict)]
    chats.sort(key=lambda chat: (chat.get("last_update") or ""), reverse=True)
    return chats


def unread_total(chats):
    total = 0
    for chat in chats or []:
        try:
            total += max(0, int(chat.get("unread_count") or 0))
        except Exception:
            continue
    return total


def open_chat_site(opener=None):
    """Открыть «Открыть на сайте» (opener — мок для тестов)."""
    if callable(opener):
        try:
            opener(CHAT_SITE_URL)
            return True
        except Exception:
            return False
    try:
        import ui_url
        return bool(ui_url.open_url(CHAT_SITE_URL))
    except Exception:
        return False


def notification_record(message, chat=None):
    """NEW_MESSAGE → запись центра уведомлений (kind=chat, синий акцент)."""
    message = dict(message or {})
    chat = dict(chat or {})
    author = chat.get("chat_name") or message.get("message_from") or "игрок"
    chat_id = str(message.get("chat_id") or chat.get("id") or "")
    text = str(message.get("raw_message") or message.get("message") or "")
    return {
        "kind": "chat",
        "title": f"Сообщение от {author}",
        "text": text[:240],
        "ts": message.get("ts") or time.time(),
        "sound": True,
        "action": "chat",
        "payload": {"author": str(author), "chat_id": chat_id,
                    "message_id": str(message.get("id") or "")},
    }


# ─────────────────────────────────────────────────────────────────────────
#  HTTP: список/история/токен
# ─────────────────────────────────────────────────────────────────────────
class ChatHttp:
    """HTTP IM-API (Cookie JWT, TTL, без запросов без токена)."""

    def __init__(self, cfg=None, token_fn=None, http_get=None, clock=None,
                 ttl=TTL_SECONDS):
        self.cfg = dict(cfg or {})
        self.token_fn = token_fn
        self._http = http_get
        self.clock = clock or time.time
        self.ttl = max(5.0, float(ttl or TTL_SECONDS))
        self._chats = []
        self._chats_at = 0.0
        self._messages = {}
        self._messages_at = {}
        self.last_error = ""
        self.last_status = None
        self.user_id = str(self.cfg.get("chat_user_id") or "")

    # ── токен ─────────────────────────────────────────────────────────
    def token(self):
        if callable(self.token_fn):
            try:
                value = self.token_fn()
            except Exception:
                value = ""
        else:
            value = self.cfg.get("market_token") or ""
        value = str(value or "").strip()
        if value.lower().startswith("jwt "):
            value = value[4:].strip()
        if value.lower().startswith("bearer "):
            value = value[7:].strip()
        return value

    def has_token(self):
        return bool(self.token())

    # ── сеть ──────────────────────────────────────────────────────────
    def _default_http(self, path, headers):
        import requests
        url = HTTP_BASE + path
        response = requests.get(url, headers=headers, timeout=6)
        try:
            payload = response.json()
        except Exception:
            payload = {}
        return int(response.status_code), payload

    def _request(self, path):
        token = self.token()
        if not token:
            raise ChatAuthError(TOKEN_MISSING_TEXT)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Platform": "pc",
            "Language": "ru",
            "Cookie": f"JWT={token}",
        }
        getter = self._http or self._default_http
        try:
            result = getter(path, headers)
        except Exception as exc:
            raise ChatNetworkError(f"нет сети ({type(exc).__name__})") from exc
        if isinstance(result, tuple) and len(result) >= 2:
            status, payload = result[0], result[1]
        else:
            status, payload = 200, result
        self.last_status = int(status or 0)
        if self.last_status == 401:
            self.last_error = "нужен вход на маркет"
            raise ChatAuthError(self.last_error)
        if self.last_status >= 400:
            self.last_error = f"сервер вернул {self.last_status}"
            raise ChatNetworkError(self.last_error)
        if not isinstance(payload, dict):
            self.last_error = "сервер вернул некорректный ответ"
            raise ChatNetworkError(self.last_error)
        self.last_error = ""
        return payload

    # ── публичные методы ──────────────────────────────────────────────
    def fetch_chats(self, force=False):
        """``{"ok","chats","unread","error","cached"}``; TTL 20–30 с."""
        if not self.has_token():
            return {"ok": False, "chats": [], "unread": 0,
                    "error": TOKEN_MISSING_TEXT, "cached": False}
        now = self.clock()
        if not force and self._chats and now - self._chats_at < self.ttl:
            return {"ok": True, "chats": list(self._chats),
                    "unread": unread_total(self._chats), "error": "",
                    "cached": True}
        try:
            payload = self._request(CHATS_PATH)
            chats = parse_chats(payload, user_id=self.user_id)
        except ChatError as exc:
            return {"ok": False, "chats": list(self._chats),
                    "unread": unread_total(self._chats), "error": str(exc),
                    "cached": bool(self._chats)}
        self._chats = chats
        self._chats_at = now
        return {"ok": True, "chats": list(chats), "unread": unread_total(chats),
                "error": "", "cached": False}

    def fetch_messages(self, chat_id, force=False):
        chat_id = str(chat_id or "")
        if not chat_id:
            return {"ok": False, "messages": [], "error": "не выбран диалог"}
        if not self.has_token():
            return {"ok": False, "messages": [],
                    "error": TOKEN_MISSING_TEXT}
        now = self.clock()
        cached = self._messages.get(chat_id)
        if not force and cached is not None \
                and now - self._messages_at.get(chat_id, 0) < self.ttl:
            return {"ok": True, "messages": list(cached), "error": "",
                    "cached": True}
        try:
            payload = self._request(f"{CHATS_PATH}/{chat_id}")
            messages = parse_messages(payload)
        except ChatError as exc:
            return {"ok": False, "messages": list(cached or []),
                    "error": str(exc), "cached": bool(cached)}
        self._messages[chat_id] = messages
        self._messages_at[chat_id] = now
        return {"ok": True, "messages": list(messages), "error": "",
                "cached": False}

    def invalidate(self):
        self._chats = []
        self._chats_at = 0.0
        self._messages.clear()
        self._messages_at.clear()
        return True


# ─────────────────────────────────────────────────────────────────────────
#  WebSocket: очередь, отправка, реконнект
# ─────────────────────────────────────────────────────────────────────────
class ChatSocket:
    """WS-клиент чата: JSON-действия, очередь, реконнект/keepalive.

    ``transport_factory(owner)`` возвращает объект с ``open()``, ``send(text)``,
    ``close()`` и (необязательно) ``ping()``. Без фабрики используется
    ``QWebSocket`` в отдельном ``QThread``.
    """

    def __init__(self, cfg=None, token="", on_message=None, on_sent=None,
                 on_error=None, on_connected=None, on_disconnected=None,
                 transport_factory=None, keepalive=KEEPALIVE_SEC,
                 reconnect_min=RECONNECT_MIN, reconnect_max=RECONNECT_MAX):
        self.cfg = dict(cfg or {})
        self.token = str(token or "")
        self.on_message = on_message
        self.on_sent = on_sent
        self.on_error = on_error
        self.on_connected = on_connected
        self.on_disconnected = on_disconnected
        self.transport_factory = transport_factory
        self.keepalive_sec = max(15, int(keepalive or KEEPALIVE_SEC))
        self.reconnect_min = max(1, int(reconnect_min or RECONNECT_MIN))
        self.reconnect_max = max(self.reconnect_min,
                                 int(reconnect_max or RECONNECT_MAX))
        self.backoff = self.reconnect_min
        self.connected = False
        self.stopped = True
        self.outbox = []
        self._transport = None
        self._keepalive_timer = None
        self._reconnect_timer = None
        self._qt_thread = None
        self.status = ""

    # ── транспорт ─────────────────────────────────────────────────────
    def start(self):
        if self.stopped is False:
            return True
        self.stopped = False
        return self._connect()

    def _connect(self):
        if self.stopped:
            return False
        try:
            if callable(self.transport_factory):
                self._transport = self.transport_factory(self)
            else:
                self._transport = _QtWebSocketTransport(self)
            open_fn = getattr(self._transport, "open", None)
            if callable(open_fn):
                open_fn()
            return True
        except Exception as exc:
            self._set_error(f"WebSocket: {type(exc).__name__}")
            self._schedule_reconnect()
            return False

    def stop(self):
        self.stopped = True
        self.connected = False
        self._cancel_timer("_keepalive_timer")
        self._cancel_timer("_reconnect_timer")
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                pass
            self._transport = None
        return True

    def _cancel_timer(self, name):
        timer = getattr(self, name, None)
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
            setattr(self, name, None)

    def _schedule_timer(self, name, seconds, callback):
        self._cancel_timer(name)
        timer = threading.Timer(max(0.01, float(seconds)), callback)
        timer.daemon = True
        setattr(self, name, timer)
        timer.start()
        return timer

    # ── события сокета ────────────────────────────────────────────────
    def _on_open(self):
        self.connected = True
        self.status = "connected"
        self.backoff = self.reconnect_min
        self._cancel_timer("_reconnect_timer")
        self._flush()
        self._start_keepalive()
        if callable(self.on_connected):
            try:
                self.on_connected()
            except Exception:
                pass

    def _on_disconnected(self, reason=""):
        was = self.connected
        self.connected = False
        self.status = f"closed: {reason}" if reason else "closed"
        self._cancel_timer("_keepalive_timer")
        if callable(self.on_disconnected):
            try:
                self.on_disconnected(self.status)
            except Exception:
                pass
        if was and not self.stopped:
            self._schedule_reconnect()

    def _schedule_reconnect(self):
        if self.stopped:
            return
        delay = self.backoff
        self.backoff = min(self.reconnect_max, max(self.reconnect_min,
                                                   self.backoff * 2))
        self._schedule_timer("_reconnect_timer", delay, self._reconnect)

    def _reconnect(self):
        if self.stopped:
            return
        self._connect()

    def _start_keepalive(self):
        self._cancel_timer("_keepalive_timer")

        def tick():
            if self.stopped or not self.connected:
                return
            try:
                pinger = getattr(self._transport, "ping", None)
                if callable(pinger):
                    pinger()
            except Exception:
                pass
            self._start_keepalive()

        self._schedule_timer("_keepalive_timer", self.keepalive_sec, tick)

    # ── кадры ─────────────────────────────────────────────────────────
    def _on_text(self, text):
        try:
            frame = json.loads(text) if isinstance(text, str) else dict(text)
        except Exception:
            self._set_error("битый JSON от WebSocket")
            return None
        if not isinstance(frame, dict):
            return None
        kind = str(frame.get("type") or "")
        payload = frame.get("payload")
        if kind.endswith("NEW_MESSAGE"):
            message = parse_message(payload)
            if callable(self.on_message):
                try:
                    self.on_message(message)
                except Exception:
                    pass
        elif kind.endswith("MESSAGE_SENT"):
            payload = payload if isinstance(payload, dict) else {}
            message = parse_message(payload.get("message") or payload)
            message["temp_id"] = str(payload.get("temp_id") or "")
            if callable(self.on_sent):
                try:
                    self.on_sent(message)
                except Exception:
                    pass
        elif kind == "@WS/ERROR" or kind.endswith("ERROR"):
            self._set_error(str(payload or "ошибка WebSocket"))
        return frame

    def _set_error(self, text):
        self.status = str(text or "ошибка чата")
        if callable(self.on_error):
            try:
                self.on_error(self.status)
            except Exception:
                pass

    # ── отправка ──────────────────────────────────────────────────────
    def _send_frame(self, frame):
        text = json.dumps(frame, ensure_ascii=False)
        transport = self._transport
        if self.connected and transport is not None:
            try:
                transport.send(text)
                return True
            except Exception as exc:
                self._set_error(f"WebSocket: {type(exc).__name__}")
                self._on_disconnected(str(exc))
        self.outbox.append(frame)
        return False

    def _flush(self):
        pending = list(self.outbox)
        self.outbox = []
        for frame in pending:
            if not self._send_frame(frame):
                self.outbox.append(frame)

    def send_message(self, chat_id, text=""):
        text = str(text or "").strip()
        if not text:
            return None
        temp_id = f"tmp-{uuid.uuid4().hex[:12]}"
        frame = {"type": "@WS/chats/SEND_MESSAGE",
                 "payload": {"chat_id": str(chat_id or ""),
                             "message": text, "temp_id": temp_id}}
        self._send_frame(frame)
        return temp_id

    def mark_read(self, message_id):
        message_id = str(message_id or "")
        if not message_id:
            return False
        frame = {"type": "@WS/chats/MESSAGE_WAS_READ",
                 "payload": {"message_id": message_id}}
        return self._send_frame(frame)


class _QtWebSocketTransport:
    """QWebSocket в QThread с processEvents (по образцу login-модуля)."""

    def __init__(self, owner):
        self.owner = owner
        self._ws = None
        self._qthread = None
        self._inbox = None
        self._stop = False

    def open(self):
        import queue
        from PyQt5.QtCore import QThread
        self._inbox = queue.Queue()
        owner = self
        owner._stop = False

        class _Thread(QThread):
            def run(self_inner):           # noqa: N805
                owner._run()

        self._qthread = _Thread()
        self._qthread.start()

    def _run(self):
        import queue
        from PyQt5.QtCore import QCoreApplication, QEventLoop, QUrl
        from PyQt5.QtNetwork import QNetworkRequest
        from PyQt5.QtWebSockets import QWebSocket
        try:
            self._ws = QWebSocket()
            self._ws.textMessageReceived.connect(self.owner._on_text)
            self._ws.connected.connect(self.owner._on_open)
            self._ws.disconnected.connect(
                lambda: self.owner._on_disconnected("disconnected"))
            self._ws.error.connect(lambda _err: self.owner._set_error(
                getattr(self._ws, "errorString", lambda: "")()))
            request = QNetworkRequest(QUrl(WS_URL))
            # авторизация чата — только cookie JWT в handshake
            try:
                token = str(getattr(self.owner, "token", "") or "")
                if token:
                    request.setRawHeader(b"Cookie",
                                         f"JWT={token}".encode("ascii"))
            except Exception:
                pass
            self._ws.open(request)
            while not self._stop:
                QCoreApplication.processEvents(QEventLoop.AllEvents, 20)
                try:
                    item = self._inbox.get_nowait()
                except queue.Empty:
                    continue
                if item is None:
                    break
                try:
                    self._ws.sendTextMessage(str(item))
                except Exception:
                    break
            try:
                self._ws.close()
            except Exception:
                pass
        except Exception as exc:
            self.owner._set_error(f"WebSocket: {type(exc).__name__}")

    def send(self, text):
        if self._inbox is None:
            raise ChatError("сокет не открыт")
        self._inbox.put(str(text))

    def ping(self):
        try:
            if self._ws is not None:
                self._ws.ping()
        except Exception:
            pass

    def close(self):
        self._stop = True
        try:
            if self._inbox is not None:
                self._inbox.put(None)
        except Exception:
            pass
        if self._qthread is not None:
            try:
                self._qthread.wait(3000)
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────
#  Сервис для GUI: HTTP в фоне, сигналы
# ─────────────────────────────────────────────────────────────────────────
def build_chat_service():
    """Фабрика QObject-сервиса: сигналы списка/истории/новых сообщений."""
    from PyQt5.QtCore import QObject, QTimer, Qt, pyqtSignal

    class ChatService(QObject):
        chats_ready = pyqtSignal(list)
        messages_ready = pyqtSignal(str, list)
        unread_changed = pyqtSignal(int)
        status_changed = pyqtSignal(str)
        message_received = pyqtSignal(dict)
        message_sent = pyqtSignal(dict)
        # 1.0.0: таймер резервного опроса запускается ТОЛЬКО в GUI-потоке.
        # WS-поток эмитит сигнал → Qt доставляет queued-вызовом, без
        # RuntimeError «cannot be started from another thread».
        fallback_start = pyqtSignal()
        fallback_stop = pyqtSignal()

        def __init__(self, cfg, token_fn=None, notify_fn=None,
                     login_check=None, http=None, socket_factory=None,
                     parent=None):
            super().__init__(parent)
            self.cfg = dict(cfg or {})
            self.http = http or ChatHttp(self.cfg, token_fn=token_fn)
            self.token_fn = token_fn
            self.notify_fn = notify_fn
            self.login_check = login_check or (lambda: False)
            self.socket_factory = socket_factory
            self.socket = None
            self.chats = []
            self.messages = {}
            self.selected = ""
            self.unread = 0
            self.status = ""
            self.started = False
            self._threads = []
            self._fallback = QTimer(self)
            self._fallback.setInterval(FALLBACK_POLL_SEC * 1000)
            self._fallback.timeout.connect(self.poll_once)
            # 1.0.0: старт/стоп таймера — только через queued-сигналы, чтобы
            # WS-поток не запускал QTimer «из другого потока».
            self.fallback_start.connect(self._start_fallback,
                                        Qt.QueuedConnection)
            self.fallback_stop.connect(self._stop_fallback,
                                       Qt.QueuedConnection)

        def _start_fallback(self):
            try:
                if not self._fallback.isActive():
                    self._fallback.start()
                return True
            except Exception as exc:
                _safe_log("чат: резервный опрос не запущен", exc)
                return False

        def _stop_fallback(self):
            try:
                self._fallback.stop()
                return True
            except Exception as exc:
                _safe_log("чат: резервный опрос не остановлен", exc)
                return False

        # ── состояние ─────────────────────────────────────────────────
        def has_token(self):
            return self.http.has_token()

        def _set_status(self, text):
            self.status = str(text or "")
            self.status_changed.emit(self.status)

        def _set_unread(self, unread):
            self.unread = max(0, int(unread or 0))
            self.unread_changed.emit(self.unread)

        def start(self):
            if self.started and self.socket is not None:
                return True
            if self.login_check():
                self._set_status("вход в браузере — чат не подключён")
                return False
            if not self.has_token():
                self._set_status(TOKEN_MISSING_TEXT)
                return False
            token = self.http.token()
            self.socket = ChatSocket(
                self.cfg, token,
                on_message=self._on_socket_message,
                on_sent=self._on_socket_sent,
                on_error=lambda text: self._set_status(text),
                on_connected=self._on_socket_connected,
                on_disconnected=self._on_socket_disconnected,
                transport_factory=self.socket_factory)
            self.started = True
            self.socket.start()
            self.refresh_chats(force=True)
            if not self.socket.connected:
                self._start_fallback()
                self._set_status(READ_ONLY_TEXT)
            return True

        def _on_socket_connected(self):
            # вызывается из WS-потока: таймер трогаем только через сигнал
            self.fallback_stop.emit()
            self._set_status("чат: на связи")

        def stop(self):
            self.started = False
            if self.socket is not None:
                self.socket.stop()
                self.socket = None
            self.fallback_stop.emit()
            return True

        # ── HTTP в фоне ───────────────────────────────────────────────
        def _spawn(self, target):
            thread = threading.Thread(target=target, daemon=True,
                                      name="wf-chat-http")
            self._threads.append(thread)
            thread.start()
            return thread

        def refresh_chats(self, force=False):
            self._spawn(lambda: self._fetch_chats_worker(force))

        def _fetch_chats_worker(self, force):
            result = self.http.fetch_chats(force=force)
            if result.get("ok"):
                self.chats = result.get("chats") or []
                self._set_unread(result.get("unread") or 0)
                self.chats_ready.emit(list(self.chats))
                if not self.status:
                    self._set_status("чат: на связи")
            else:
                self._set_status(result.get("error") or "ошибка чата")

        def load_messages(self, chat_id, force=False):
            self.selected = str(chat_id or "")
            if not self.selected:
                return
            self._spawn(lambda: self._fetch_messages_worker(self.selected,
                                                            force))

        def _fetch_messages_worker(self, chat_id, force):
            result = self.http.fetch_messages(chat_id, force=force)
            if result.get("ok"):
                self.messages[chat_id] = result.get("messages") or []
                self.messages_ready.emit(chat_id,
                                         list(self.messages[chat_id]))
                messages = self.messages[chat_id]
                if messages and self.socket is not None:
                    try:
                        self.socket.mark_read(messages[-1].get("id"))
                    except Exception:
                        pass
                changed = False
                for chat in self.chats:
                    if str(chat.get("id")) == str(chat_id) \
                            and int(chat.get("unread_count") or 0):
                        chat["unread_count"] = 0
                        changed = True
                if changed:
                    self._set_unread(unread_total(self.chats))
            else:
                self._set_status(result.get("error") or "ошибка чата")

        def poll_once(self):
            if not self.has_token():
                self._set_status(TOKEN_MISSING_TEXT)
                return False
            self.refresh_chats(force=True)
            return True

        # ── сокет ─────────────────────────────────────────────────────
        def _on_socket_message(self, message):
            message = dict(message or {})
            chat_id = str(message.get("chat_id") or "")
            self.messages.setdefault(chat_id, []).append(message)
            if chat_id != self.selected:
                self._set_unread(self.unread + 1)
            elif message.get("id") and self.socket is not None:
                # открытый диалог: подтверждаем прочтение
                try:
                    self.socket.mark_read(message.get("id"))
                except Exception:
                    pass
            self.message_received.emit(message)
            if callable(self.notify_fn):
                try:
                    self.notify_fn(notification_record(message,
                                                       self.chat_by_id(chat_id)))
                except Exception:
                    pass

        def _on_socket_sent(self, message):
            chat_id = str(message.get("chat_id") or "")
            temp_id = str(message.get("temp_id") or "")
            items = self.messages.setdefault(chat_id, [])
            for index, item in enumerate(items):
                if temp_id and item.get("temp_id") == temp_id:
                    items[index] = message
                    break
            else:
                items.append(message)
            self.message_sent.emit(message)

        def _on_socket_disconnected(self, status=""):
            if not self.socket or self.socket.stopped:
                return
            # WS-поток → queued-сигнал → QTimer.start() в GUI-потоке
            self.fallback_start.emit()
            self._set_status(READ_ONLY_TEXT)

        def chat_by_id(self, chat_id):
            for chat in self.chats:
                if str(chat.get("id")) == str(chat_id):
                    return chat
            return {}

        def send(self, chat_id, text):
            if not self.has_token():
                self._set_status(TOKEN_MISSING_TEXT)
                return None
            if self.socket is None:
                self.start()
            if self.socket is None:
                return None
            temp_id = self.socket.send_message(chat_id, text)
            if temp_id:
                optimistic = {"id": "", "chat_id": str(chat_id),
                              "message_from": "me", "message": str(text),
                              "raw_message": str(text), "send_date": "",
                              "ts": time.time(), "temp_id": temp_id}
                self.messages.setdefault(str(chat_id), []).append(optimistic)
                self.message_sent.emit(optimistic)
            return temp_id

        def mark_read(self, message_id):
            if self.socket is None:
                return False
            return self.socket.mark_read(message_id)

    return ChatService


# ─────────────────────────────────────────────────────────────────────────
#  UI: вкладка «Чат»
# ─────────────────────────────────────────────────────────────────────────
def build_chat_widget():
    """Фабрика виджета чата: список диалогов + история + отправка."""
    from PyQt5.QtCore import Qt, pyqtSignal
    from PyQt5.QtGui import QFont
    from PyQt5.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit,
                                 QListWidget, QListWidgetItem, QPushButton,
                                 QScrollArea, QSizePolicy, QVBoxLayout,
                                 QWidget)

    def font(size, bold=False):
        result = QFont(theme.FONT if theme is not None else "Segoe UI")
        result.setPixelSize(int(size))
        result.setBold(bool(bold))
        return result

    def button_qss(**kwargs):
        return theme.button_qss(**kwargs) if theme is not None else ""

    def color(name, fallback="#e6edf6"):
        if theme is not None:
            return theme.PALETTE.get(name, fallback)
        return fallback

    class _ElidedLabel(QLabel):
        """QLabel с многоточием по фактической ширине (автор сообщения)."""

        def __init__(self, text="", parent=None):
            super().__init__(parent)
            self._full = str(text or "")
            self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
            self.setMinimumWidth(0)
            self._apply()

        def set_full_text(self, text):
            self._full = str(text or "")
            self._apply()

        def _apply(self):
            self.setText(self.fontMetrics().elidedText(
                self._full, Qt.ElideRight, max(10, self.width())))

        def resizeEvent(self, event):       # noqa: N802 (Qt API)
            super().resizeEvent(event)
            self._apply()

    class _NickLabel(_ElidedLabel):
        """t188: ник в колонке АВТОР — клик открывает историю по нику."""

        def __init__(self, text="", parent=None, on_click=None):
            super().__init__(text, parent)
            self._on_click = on_click
            self.setCursor(Qt.PointingHandCursor)

        def mouseReleaseEvent(self, event):    # noqa: N802 (Qt API)
            if callable(self._on_click):
                self._on_click()
            super().mouseReleaseEvent(event)

    class ChatWidget(QFrame):
        chat_selected = pyqtSignal(str)
        send_requested = pyqtSignal(str, str)
        open_site_requested = pyqtSignal()
        translation_ready = pyqtSignal(str, int, str)   # t184: Alt+T

        #: сетка строки сообщения и её шапки: (ключ, ширина); 0 — stretch
        CHAT_GRID = (("time", 44), ("author", 90), ("tag", 44), ("text", 0))

        def __init__(self, cfg=None, parent=None):
            super().__init__(parent)
            self.cfg = dict(cfg or {})
            self.chats = []
            self.current_chat = ""
            self.messages = {}
            self._translations = {}     # t184: перевод только в отображении
            self.history_nick = ""      # t188: режим «ИСТОРИЯ · <ник>»
            self._chat_history = None
            self._recorded = set()      # антидубли записей истории
            self.confirm = None         # callable(str) → bool (тесты/UI)
            self.toast = None           # callable(str) — тост приложения
            self.setObjectName("chatPanel")
            self.setStyleSheet(
                (theme.panel_qss("chatPanel", radius=0) if theme is not None
                 else f"QFrame#chatPanel {{ background:{color('panel')}; }}"))
            outer = QVBoxLayout(self)
            outer.setContentsMargins(8, 8, 8, 8)
            outer.setSpacing(6)
            head = QHBoxLayout()
            self.lbl_status = QLabel(TOKEN_MISSING_TEXT)
            self.lbl_status.setFont(font(10))
            self.lbl_status.setStyleSheet(
                f"color:{color('muted')}; background:transparent;")
            self.btn_site = QPushButton("Открыть на сайте")
            self.btn_site.setStyleSheet(button_qss(small=True))
            self.btn_site.setCursor(Qt.PointingHandCursor)
            self.btn_site.clicked.connect(self.open_site_requested.emit)
            head.addWidget(self.lbl_status, 1)
            head.addWidget(self.btn_site)
            outer.addLayout(head)
            body = QHBoxLayout()
            body.setSpacing(8)
            self.list = QListWidget()
            self.list.setFixedWidth(280)
            self.list.setFont(font(11))
            self.list.setStyleSheet(
                f"QListWidget {{ background:{color('panel')};"
                f" color:{color('text')}; border:1px solid {color('border')};"
                " border-radius:6px; }")
            self.list.itemSelectionChanged.connect(self._on_selection)
            body.addWidget(self.list)
            right = QVBoxLayout()
            right.setSpacing(4)
            self.history_header = self._chat_header()
            right.addWidget(self.history_header)
            # t188: в режиме истории — «назад» и «очистить»
            tools = QHBoxLayout()
            tools.setSpacing(4)
            self.btn_hist_close = QPushButton("✕")
            self.btn_hist_close.setToolTip("Назад к диалогу")
            self.btn_hist_close.clicked.connect(self.close_history)
            self.btn_hist_clear = QPushButton("Очистить")
            self.btn_hist_clear.setToolTip("Удалить историю этого ника")
            self.btn_hist_clear.clicked.connect(self.clear_history)
            for button in (self.btn_hist_close, self.btn_hist_clear):
                button.setStyleSheet(button_qss(small=True)
                                     + " border-radius:2px;")
                button.setFixedHeight(24)
                button.setCursor(Qt.PointingHandCursor)
                button.setVisible(False)
                tools.addWidget(button)
            tools.addStretch(1)
            right.addLayout(tools)
            self.history = QScrollArea()
            self.history.setWidgetResizable(True)
            self.history.setFrameShape(QFrame.NoFrame)
            self.history.setStyleSheet(
                "QScrollArea { background: transparent; border: none; }"
                "QScrollArea > QWidget > QWidget { background: transparent; }"
                + (theme.scroll_qss() if theme is not None else ""))
            self.history_host = QWidget()
            self.history_lay = QVBoxLayout(self.history_host)
            self.history_lay.setContentsMargins(0, 0, 0, 0)
            self.history_lay.setSpacing(0)
            self.history_lay.setAlignment(Qt.AlignTop)
            self.history.setWidget(self.history_host)
            right.addWidget(self.history, 1)
            send_row = QHBoxLayout()
            send_row.setSpacing(4)
            self.edit = QLineEdit()
            self.edit.setFont(font(11))
            self.edit.setPlaceholderText("Сообщение…")
            self.edit.setFixedHeight(28)
            self.edit.setStyleSheet(
                "QLineEdit { background:%s; color:%s; border:1px solid %s;"
                " border-radius:0px; padding:0px 8px; font-size:11px; }"
                % (color("panel"), color("text"), color("border")))
            self.edit.returnPressed.connect(self.send_current)
            self.btn_send = QPushButton("Отправить")
            send_font = QFont("Consolas")
            send_font.setPixelSize(9)
            send_font.setCapitalization(QFont.AllUppercase)
            self.btn_send.setFont(send_font)
            self.btn_send.setStyleSheet(button_qss(primary=True)
                                        + " border-radius:2px;")
            self.btn_send.setCursor(Qt.PointingHandCursor)
            self.btn_send.setFixedHeight(24)
            self.btn_send.clicked.connect(self.send_current)
            send_row.addWidget(self.edit, 1)
            send_row.addWidget(self.btn_send)
            right.addLayout(send_row)
            body.addLayout(right, 1)
            outer.addLayout(body, 1)
            self.translation_ready.connect(self._apply_translation)
            self.set_status(TOKEN_MISSING_TEXT)

        def set_status(self, text):
            self.lbl_status.setText(str(text or ""))

        def set_chats(self, chats):
            self.chats = [dict(chat) for chat in (chats or [])]
            self.list.blockSignals(True)
            self.list.clear()
            for chat in self.chats:
                last = chat.get("last_message") or {}
                text = chat.get("chat_name") or "Диалог"
                preview = (last.get("message") or "")[:48]
                when = format_chat_time(chat.get("last_update")
                                        or last.get("send_date"))
                unread = int(chat.get("unread_count") or 0)
                suffix = f"  [{unread}]" if unread else ""
                item = QListWidgetItem(
                    f"{text}{suffix}\n{preview}" + (f" · {when}" if when else ""))
                item.setData(Qt.UserRole, chat.get("id"))
                self.list.addItem(item)
            self.list.blockSignals(False)

        # ── t185: строки сообщений в сетке E (время | автор | метка | текст) ─
        def _chat_line(self, host):
            line = QHBoxLayout(host)
            line.setContentsMargins(8, 0, 8, 0)
            line.setSpacing(6)
            return line

        def _chat_cell(self, key, align=Qt.AlignLeft):
            label = QLabel("")
            label.setAlignment(align | Qt.AlignVCenter)
            width = dict(self.CHAT_GRID)[key]
            if width:
                label.setFixedWidth(width)
            return label

        def _chat_header(self):
            """Шапка списка: та же сетка, что у строк сообщений."""
            head = QWidget()
            head.setFixedHeight(24)
            line = self._chat_line(head)
            for key, title in (("time", "ВРЕМЯ"), ("author", "АВТОР"),
                               ("tag", ""), ("text", "СООБЩЕНИЕ")):
                label = self._chat_cell(key)
                label.setText(title)
                if theme is not None:
                    label.setStyleSheet(theme.caps_header_qss())
                else:
                    label.setFont(font(9))
                label.setFixedHeight(24)
                line.addWidget(label, 1 if key == "text" else 0)
            return head

        # ── t188: история переписки по нику ─────────────────────────────
        def _history_store(self):
            if getattr(self, "_chat_history", None) is None:
                import chat_history as history_mod
                limit = int(self.cfg.get("chat_history_limit", 100) or 100)
                self._chat_history = history_mod.ChatHistory(self.cfg,
                                                             limit=limit)
            return self._chat_history

        @staticmethod
        def _message_utc(message):
            """Время сообщения → float (число, ISO-строка или «сейчас»)."""
            raw = (message or {}).get("send_date") or (message or {}).get("ts")
            if raw is None:
                return time.time()
            try:
                return float(raw)
            except (TypeError, ValueError):
                pass
            try:
                return datetime.datetime.fromisoformat(
                    str(raw).replace("Z", "+00:00")).timestamp()
            except ValueError:
                return time.time()

        def record_message(self, message):
            """Записать сообщение чата в историю по нику (если включено)."""
            if not bool(self.cfg.get("chat_history_enabled", True)):
                return False
            text = self._message_text(message)
            if not text:
                return False
            own = str((message or {}).get("message_from") or "") in ("me",
                                                                    "self")
            nick = "Вы" if own else str((message or {}).get("message_from")
                                        or "собеседник")
            label, _body = self._trade_tag(text)
            utc = self._message_utc(message)
            key = (str((message or {}).get("message_from") or ""), utc, text)
            if key in self._recorded:
                return False
            try:
                added = bool(self._history_store().add(
                    nick, text, label=label, outgoing=own, utc=utc))
            except Exception as exc:
                _safe_log("chat history", exc)
                return False
            if added:
                self._recorded.add(key)
                if len(self._recorded) > 2000:
                    self._recorded.clear()
            return added

        def record_messages(self, messages):
            """Записать пачку сообщений (без дублей по нику/времени/тексту)."""
            for message in messages or []:
                self.record_message(message)

        def open_history(self, nick):
            """Клик по нику: показать историю только этого ника."""
            nick = str(nick or "").strip()
            if not nick:
                return False
            self.history_nick = nick
            self.hist_buttons_visible(True)
            self.set_status(f"ИСТОРИЯ · {nick}")
            self.render_history()
            return True

        def close_history(self):
            self.history_nick = ""
            self.hist_buttons_visible(False)
            chat_id = str(self.current_chat or "")
            self.set_status(chat_id or TOKEN_MISSING_TEXT)
            self.set_messages(chat_id, self.messages.get(chat_id) or [])
            return True

        def hist_buttons_visible(self, visible):
            for name in ("btn_hist_close", "btn_hist_clear"):
                button = getattr(self, name, None)
                if button is not None:
                    button.setVisible(bool(visible))

        def render_history(self, nick=None):
            nick = str(nick or getattr(self, "history_nick", "") or "")
            if not nick:
                return False
            records = [{"message_from": nick, "message": item.get("text"),
                        "send_date": item.get("utc"),
                        "trade_tag": item.get("label")}
                       for item in self._history_store().records(nick)]
            self._render_rows(records, trans_key=f"hist:{nick}",
                              empty="История пуста")
            if not records:
                self.set_status(f"ИСТОРИЯ · {nick} — пусто")
            return True

        def clear_history(self, nick=None):
            """ОЧИСТИТЬ: удалить историю ника (с подтверждением)."""
            target = str(nick or getattr(self, "history_nick", "") or "")
            question = (f"Удалить историю переписки с {target}?" if target
                        else "Удалить всю историю переписки?")
            confirm = getattr(self, "confirm", None)
            if callable(confirm) and not confirm(question):
                return False
            self._history_store().clear(target or None)
            if getattr(self, "history_nick", ""):
                self.render_history()
            self._toast("история очищена")
            return True

        def _on_nick_click(self, nick):
            return self.open_history(nick)

        @staticmethod
        def _trade_tag(text):
            """Первое слово WTS/WTB/WTT → (метка, текст без неё)."""
            raw = str(text or "")
            parts = raw.split(None, 1)
            if parts and parts[0].upper() in ("WTS", "WTB", "WTT"):
                return parts[0].upper(), (parts[1] if len(parts) > 1 else "")
            return "", raw

        @staticmethod
        def _sender_of(message):
            own = str((message or {}).get("message_from") or "") in ("me", "self")
            return "Вы" if own else str((message or {}).get("message_from")
                                        or "собеседник")

        def _trans_key(self):
            """Ключ переводов: история по нику или текущий диалог (t188)."""
            nick = getattr(self, "history_nick", "")
            return f"hist:{nick}" if nick else str(self.current_chat or "")

        def _message_row(self, message, index, zebra, trans_key=None):
            """Строка сообщения 28 px (с переводом — 42 px), без радиусов."""
            row = QFrame()
            row.setObjectName("chatRow")
            row.setProperty("message_index", int(index))
            key = trans_key if trans_key is not None else str(self.current_chat)
            translated = (self._translations.get(key) or {}).get(index)
            row.setFixedHeight(28 + (14 if translated else 0))
            background = (theme.PALETTE["zebra"] if (zebra and theme is not None)
                          else "transparent")
            row.setStyleSheet(
                f"QFrame#chatRow {{ background-color: {background};"
                " border: none; border-radius: 0px; }")
            line = self._chat_line(row)
            when = self._chat_cell("time", Qt.AlignLeft)
            when.setText(format_chat_time(message.get("send_date")
                                          or message.get("ts")))
            when.setFont(font(10))
            if theme is not None:
                when.setStyleSheet(theme.muted_qss(10)
                                   + " font-family:'Consolas', monospace;")
            author = _NickLabel(self._sender_of(message),
                                on_click=lambda nick=self._sender_of(message):
                                self.open_history(nick))
            author.setFixedWidth(dict(self.CHAT_GRID)["author"])
            author.setFont(font(10, True))
            author.setStyleSheet(f"color:{color('text')}; background:transparent;")
            author.setToolTip(self._sender_of(message))
            tag_text, body = self._trade_tag(self._message_text(message))
            tag_host = QWidget()
            tag_host.setFixedWidth(dict(self.CHAT_GRID)["tag"])
            tag_lay = QHBoxLayout(tag_host)
            tag_lay.setContentsMargins(0, 0, 0, 0)
            tag_lay.setSpacing(0)
            if tag_text:
                chip = QLabel(f"[{tag_text}]")
                if theme is not None:
                    chip.setStyleSheet(theme.chip_qss(tag_text))
                chip.setFont(font(9))
                chip.setFixedHeight(18)
                chip.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
                chip.setToolTip(tag_text)
                tag_lay.addWidget(chip, 0)
            tag_lay.addStretch(1)
            text_col = QWidget()
            text_lay = QVBoxLayout(text_col)
            text_lay.setContentsMargins(0, 0, 0, 0)
            text_lay.setSpacing(0)
            body_label = _ElidedLabel(body or self._message_text(message))
            body_label.setFont(font(11))
            body_label.setStyleSheet(
                f"color:{color('text')}; background:transparent;")
            body_label.setToolTip(self._message_text(message))
            text_lay.addWidget(body_label)
            if translated:
                tr_label = _ElidedLabel(str(translated))
                tr_label.setFont(font(10))
                tr_label.setStyleSheet(
                    f"color:{color('muted')}; background:transparent;"
                    " padding-left:14px;")
                tr_label.setToolTip("перевод (Alt+T)")
                text_lay.addWidget(tr_label)
            line.addWidget(when, 0)
            line.addWidget(author, 0)
            line.addWidget(tag_host, 0)
            line.addWidget(text_col, 1)
            return row

        def set_messages(self, chat_id, messages):
            self.messages[str(chat_id)] = [dict(m) for m in (messages or [])]
            if str(chat_id) != str(self.current_chat):
                return
            self.record_messages(messages)
            if getattr(self, "history_nick", ""):
                self.render_history()
                return
            self._render_rows(messages, str(chat_id), "История пуста")

        def _render_rows(self, messages, trans_key, empty):
            """Перерисовать строки истории (диалог или история по нику)."""
            while self.history_lay.count():
                item = self.history_lay.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.setParent(None)
                    widget.deleteLater()
            self.history_rows = []
            if not messages:
                hint = QLabel(empty)
                hint.setFont(font(11))
                hint.setStyleSheet(
                    f"color:{color('muted')}; background:transparent;")
                self.history_lay.addWidget(hint)
                self.history_lay.addStretch(1)
                return
            for index, message in enumerate(messages):
                row = self._message_row(message, index, zebra=index % 2 == 1,
                                        trans_key=trans_key)
                self.history_rows.append(row)
                self.history_lay.addWidget(row)
            self.history_lay.addStretch(1)

        def history_texts(self):
            """Полные тексты строк истории (без обрезки многоточием)."""
            out = []
            for row in getattr(self, "history_rows", []) or []:
                for label in row.findChildren(QLabel):
                    value = getattr(label, "_full", "") or label.text()
                    if value:
                        out.append(str(value))
            return out

        # ── t184: перевод сообщения по Alt+T ───────────────────────────
        @staticmethod
        def _message_text(message):
            message = message or {}
            return str(message.get("message") or message.get("raw_message")
                       or "").strip()

        def message_at(self, pos=None):
            """Индекс сообщения под курсором (по строке-виджету) или None."""
            point = pos
            if point is None:
                from PyQt5.QtGui import QCursor
                point = self.history.viewport().mapFromGlobal(QCursor.pos())
            widget = self.history.viewport().childAt(point)
            while widget is not None and widget is not self.history.viewport():
                index = widget.property("message_index")
                if index is not None:
                    return int(index)
                widget = widget.parentWidget()
            return None

        def _toast(self, text):
            self.set_status(str(text))
            if callable(self.toast):
                try:
                    self.toast(str(text))
                except Exception:
                    pass

        def translate_under_cursor(self, pos=None):
            """Alt+T: перевод сообщения под курсором (в истории — по нику)."""
            nick = getattr(self, "history_nick", "")
            if nick:
                records = self._history_store().records(nick)
                index = self.message_at(pos)
                if index is None or index >= len(records):
                    self._toast("перевод: наведите курсор на сообщение")
                    return None
                rows = self._translations.get(f"hist:{nick}") or {}
                if index in rows:
                    rows.pop(index, None)
                    self.render_history(nick)
                    return ""
                self._translate_async(f"hist:{nick}", index,
                                      str(records[index].get("text") or ""))
                return None
            chat_id = str(self.current_chat or "")
            messages = self.messages.get(chat_id) or []
            if not chat_id or not messages:
                self._toast("перевод: откройте диалог с сообщениями")
                return None
            index = self.message_at(pos)
            if index is None or index >= len(messages):
                self._toast("перевод: наведите курсор на сообщение")
                return None
            shown = self._translations.setdefault(chat_id, {})
            if index in shown:
                shown.pop(index, None)
                self.set_messages(chat_id, messages)
                return ""
            if translator is None:
                self._toast("перевод недоступен в этой сборке")
                return None
            if translator.ready(self.cfg) is False:
                self._toast("перевод: нужен ключ — вставьте его в Настройках "
                            "или выберите «Бесплатный»")
                return None
            text = self._message_text(messages[index])
            self._translate_async(chat_id, index, text)
            return None

        def _translate_async(self, chat_id, index, text):
            def worker():
                try:
                    translated, info = translator.translate(text, cfg=self.cfg)
                except Exception as exc:      # перевод не должен ронять UI
                    translated, info = text, {"error": str(exc)}
                if (info or {}).get("error"):
                    error = info["error"]
                    if error == "free_unavailable":
                        self._toast("бесплатный перевод недоступен, попробуйте "
                                    "позже или выберите Яндекс")
                    else:
                        self._toast(f"перевод: ошибка {error}")
                    return
                try:
                    self.translation_ready.emit(chat_id, int(index), translated)
                except RuntimeError:
                    pass

            threading.Thread(target=worker, daemon=True,
                             name="wf-chat-translate").start()

        def _apply_translation(self, chat_id, index, text):
            chat_id = str(chat_id)
            self._translations.setdefault(chat_id, {})[int(index)] = str(text)
            if chat_id.startswith("hist:"):
                self.render_history(chat_id[5:])
                self._schedule_translation_hide(chat_id, int(index))
            else:
                self.set_messages(chat_id, self.messages.get(chat_id) or [])

        def _schedule_translation_hide(self, chat_id, index):
            """t188: перевод в истории исчезает через ``chat_translate_ttl``."""
            try:
                ttl = float(self.cfg.get("chat_translate_ttl", 6.0) or 0)
            except (TypeError, ValueError):
                ttl = 6.0
            if ttl <= 0:
                return False
            from PyQt5.QtCore import QTimer

            def hide():
                translations = self._translations.get(chat_id) or {}
                if index in translations:
                    translations.pop(index, None)
                    if chat_id.startswith("hist:"):
                        self.render_history(chat_id[5:])

            QTimer.singleShot(int(ttl * 1000), hide)
            return True

        def append_message(self, message):
            message = dict(message or {})
            chat_id = str(message.get("chat_id") or self.current_chat)
            items = self.messages.setdefault(chat_id, [])
            temp_id = str(message.get("temp_id") or "")
            replaced = False
            if temp_id:
                for index, item in enumerate(items):
                    if str(item.get("temp_id") or "") == temp_id:
                        items[index] = message
                        replaced = True
                        break
            if not replaced:
                items.append(message)
                self.record_message(message)      # t188: история по нику
            if chat_id == self.current_chat:
                self.set_messages(chat_id, items)

        def _on_selection(self):
            item = self.list.currentItem()
            if item is None:
                return
            chat_id = str(item.data(Qt.UserRole) or "")
            self.current_chat = chat_id
            self.set_messages(chat_id, self.messages.get(chat_id, []))
            self.chat_selected.emit(chat_id)

        def select_chat(self, chat_id):
            for index in range(self.list.count()):
                item = self.list.item(index)
                if str(item.data(Qt.UserRole)) == str(chat_id):
                    self.list.setCurrentRow(index)
                    return True
            return False

        def send_current(self):
            text = self.edit.text().strip()
            if not text or not self.current_chat:
                return False
            self.send_requested.emit(self.current_chat, text)
            self.edit.clear()
            return True

        def badge_text(self):
            total = sum(int(chat.get("unread_count") or 0)
                        for chat in self.chats)
            return str(total)

    return ChatWidget
