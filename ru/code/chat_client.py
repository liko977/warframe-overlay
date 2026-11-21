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
import os
import threading
import time
import uuid

try:
    import ui_theme as theme
except Exception:                       # pragma: no cover
    theme = None


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
        import webbrowser
        return bool(webbrowser.open(CHAT_SITE_URL))
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
    ``QWebSocket`` в отдельном ``QThread`` (как в market_browser_login.py).
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
    from PyQt5.QtCore import QObject, QTimer, pyqtSignal

    class ChatService(QObject):
        chats_ready = pyqtSignal(list)
        messages_ready = pyqtSignal(str, list)
        unread_changed = pyqtSignal(int)
        status_changed = pyqtSignal(str)
        message_received = pyqtSignal(dict)
        message_sent = pyqtSignal(dict)

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
                self._fallback.start()
                self._set_status(READ_ONLY_TEXT)
            return True

        def _on_socket_connected(self):
            try:
                self._fallback.stop()
            except Exception:
                pass
            self._set_status("чат: на связи")

        def stop(self):
            self.started = False
            if self.socket is not None:
                self.socket.stop()
                self.socket = None
            try:
                self._fallback.stop()
            except Exception:
                pass
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
            self._fallback.start()
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
                                 QScrollArea, QTextBrowser, QVBoxLayout,
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

    class ChatWidget(QWidget):
        chat_selected = pyqtSignal(str)
        send_requested = pyqtSignal(str, str)
        open_site_requested = pyqtSignal()

        def __init__(self, cfg=None, parent=None):
            super().__init__(parent)
            self.cfg = dict(cfg or {})
            self.chats = []
            self.current_chat = ""
            self.messages = {}
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
            right.setSpacing(6)
            self.history = QTextBrowser()
            self.history.setFont(font(11))
            self.history.setStyleSheet(
                f"QTextBrowser {{ background:{color('panel')};"
                f" color:{color('text')}; border:1px solid {color('border')};"
                " border-radius:6px; padding:6px; }")
            right.addWidget(self.history, 1)
            send_row = QHBoxLayout()
            self.edit = QLineEdit()
            self.edit.setFont(font(11))
            self.edit.setPlaceholderText("Сообщение…")
            self.edit.returnPressed.connect(self.send_current)
            self.btn_send = QPushButton("Отправить")
            self.btn_send.setStyleSheet(button_qss(primary=True))
            self.btn_send.setCursor(Qt.PointingHandCursor)
            self.btn_send.clicked.connect(self.send_current)
            send_row.addWidget(self.edit, 1)
            send_row.addWidget(self.btn_send)
            right.addLayout(send_row)
            body.addLayout(right, 1)
            outer.addLayout(body, 1)
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

        def set_messages(self, chat_id, messages):
            self.messages[str(chat_id)] = [dict(m) for m in (messages or [])]
            if str(chat_id) != str(self.current_chat):
                return
            parts = []
            for message in messages or []:
                own = str(message.get("message_from") or "") in ("me", "self")
                sender = "Вы" if own else str(message.get("message_from") or "собеседник")
                text = (message.get("message") or message.get("raw_message")
                        or "").replace("<", "&lt;").replace(">", "&gt;")
                align = "right" if own else "left"
                bg = color("accent") if own else color("card")
                fg = "#0d131c" if own else color("text")
                parts.append(
                    f"<div align='{align}'><span style='background:{bg};"
                    f"color:{fg};padding:4px 8px;border-radius:6px;'>"
                    f"<b>{sender}</b>: {text}</span></div>")
            self.history.setHtml("<br>".join(parts) if parts else
                                 f"<div style='color:{color('muted')}'>"
                                 "История пуста</div>")

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
