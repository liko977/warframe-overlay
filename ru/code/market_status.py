# -*- coding: utf-8 -*-
"""Статус warframe.market (online/ingame/invisible) из оверлея.

WS A: ``wss://ws.warframe.market/socket`` с subprotocol ``wfm``,
JSON ``{route,payload}``. Авторизация — ``@wfm|cmd/auth/signIn`` с пустым
токеном (сессия из cookie ``JWT``), текущий статус приходит в
``@wfm|event/status/set``. Смена — ``@wfm|cmd/status/set``; ``offline``
отдельной командой не ставится (это закрытие сокета).

Модуль не импортирует PyQt5 на уровне модуля: статус/валидация/сокет
тестируются на заглушках, Qt (QThread/QWebSocket, сервис и чип) — лениво.
"""

import json
import threading
import time

try:
    import ui_theme as theme
except Exception:                       # pragma: no cover
    theme = None


WS_URL = "wss://ws.warframe.market/socket"
SUBPROTOCOL = "wfm"
ORIGIN = "https://warframe.market"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
AUTH_ROUTE = "@wfm|cmd/auth/signIn"
STATUS_SET_ROUTE = "@wfm|cmd/status/set"
STATUS_EVENT_ROUTE = "@wfm|event/status/set"
STATUSES = ("online", "ingame", "invisible")
STATUS_LABELS = {
    "online": "Онлайн",
    "ingame": "В игре",
    "invisible": "Невидимка",
    "offline": "Оффлайн",
    "": "—",
}
STATUS_COLORS = {
    "online": "success",
    "ingame": "accent",
    "invisible": "muted",
    "offline": "muted",
    "": "muted",
}
#: (секунды, подпись) — как пресеты сайта; None = бессрочно.
DURATION_PRESETS = ((None, "Бессрочно"), (1800, "30 мин"),
                    (3600, "1 ч"), (7200, "2 ч"), (14400, "4 ч"))
MIN_DURATION = 60
RECONNECT_MIN = 5
RECONNECT_MAX = 60

ERROR_TEXT = {
    "app.errors.invalidStatus": "недопустимый статус",
    "app.field.tooSmall": "слишком короткая длительность (минимум 60 с)",
    "app.errors.unauthorized": "нужен вход на маркет",
}


# ─────────────────────────────────────────────────────────────────────────
#  Валидация/парсинг (без Qt)
# ─────────────────────────────────────────────────────────────────────────
def normalize_status(value):
    status = str(value or "").strip().lower()
    return status if status in STATUSES else ""


def status_label(status):
    return STATUS_LABELS.get(str(status or "").lower(), "—")


def status_color(status):
    key = STATUS_COLORS.get(str(status or "").lower(), "muted")
    return theme.PALETTE.get(key, "#93a1b5") if theme is not None else "#93a1b5"


def validate_status_request(status, duration=None):
    """Проверить команду до отправки → ``(ok, error, norm_status, duration)``."""
    status = normalize_status(status)
    if not status:
        return False, "недопустимый статус", "", None
    if status == "invisible":
        if duration not in (None, ""):
            return False, "для «Невидимки» длительность только бессрочно", \
                status, None
        return True, "", status, None
    if duration in (None, ""):
        return True, "", status, None
    try:
        duration = int(duration)
    except Exception:
        return False, "длительность должна быть числом секунд", status, None
    if duration < MIN_DURATION:
        return False, f"слишком короткая длительность (минимум {MIN_DURATION} с)", \
            status, None
    return True, "", status, duration


def parse_status_payload(payload):
    payload = payload if isinstance(payload, dict) else {}
    status = normalize_status(payload.get("status"))
    return {"status": status,
            "status_set_at": payload.get("statusSetAt"),
            "status_until": payload.get("statusUntil"),
            "duration": None}


def status_error_text(payload):
    """Ошибка ``cmd/status/set:error`` → понятная русская строка."""
    payload = payload if isinstance(payload, dict) else {}
    reasons = []
    for value in payload.values():
        if isinstance(value, (list, tuple)):
            values = value
        else:
            values = [value]
        for item in values:
            text = str(item or "")
            reasons.append(ERROR_TEXT.get(text, text))
    return "; ".join(reason for reason in reasons if reason) or "ошибка статуса"


def status_menu_spec():
    """Пункты меню чипа (для UI/тестов): (подпись, status/auto)."""
    return [("Онлайн", "online"), ("В игре", "ingame"),
            ("Невидимка", "invisible"), ("Авто", "auto")]


def duration_menu_spec():
    return list(DURATION_PRESETS)


def ws_request_headers(token=""):
    """Заголовки WS-апгрейда (Cloudflare требует Origin/UA; cookie JWT)."""
    headers = {"Origin": ORIGIN, "User-Agent": USER_AGENT,
               "Sec-WebSocket-Protocol": SUBPROTOCOL}
    token = str(token or "")
    if token:
        headers["Cookie"] = f"JWT={token}"
    return headers


# ─────────────────────────────────────────────────────────────────────────
#  WebSocket
# ─────────────────────────────────────────────────────────────────────────
class StatusSocket:
    """WS A: авторизация, текущий статус, set_status, реконнект."""

    def __init__(self, token="", transport_factory=None,
                 on_status=None, on_error=None, on_connected=None,
                 on_disconnected=None, on_log=None,
                 reconnect_min=RECONNECT_MIN, reconnect_max=RECONNECT_MAX):
        self.token = str(token or "")
        self.transport_factory = transport_factory
        self.on_status = on_status
        self.on_error = on_error
        self.on_connected = on_connected
        self.on_disconnected = on_disconnected
        self.on_log = on_log
        self.reconnect_min = max(1, int(reconnect_min or RECONNECT_MIN))
        self.reconnect_max = max(self.reconnect_min,
                                 int(reconnect_max or RECONNECT_MAX))
        self.backoff = self.reconnect_min
        self.connected = False
        self.authenticated = False
        self.stopped = True
        self.current_status = ""
        self.current_until = None
        self.last_error = ""
        self.pending = []
        self._previous_status = ""
        self._pending_status = ""
        self._auth_retried = False
        self.last_status_source = ""
        self._transport = None
        self._reconnect_timer = None
        self._keepalive_timer = None
        self.keepalive_sec = 45
        self.status = ""

    def _log(self, message):
        if callable(self.on_log):
            try:
                self.on_log(str(message))
            except Exception:
                pass

    # ── подключение ──────────────────────────────────────────────────
    def start(self):
        if not self.token:
            self._set_error("нужен вход на маркет")
            return False
        self.stopped = False
        return self._connect()

    def _connect(self):
        if self.stopped:
            return False
        try:
            if callable(self.transport_factory):
                self._transport = self.transport_factory(self)
            else:
                self._transport = _QtWfmTransport(self)
            self._transport.open()
            return True
        except Exception as exc:
            self._set_error(f"WebSocket: {type(exc).__name__}")
            self._schedule_reconnect()
            return False

    def stop(self):
        self.stopped = True
        self.connected = False
        self.authenticated = False
        self._cancel_timer()
        self._cancel_keepalive()
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                pass
            self._transport = None
        return True

    def _cancel_timer(self):
        if self._reconnect_timer is not None:
            try:
                self._reconnect_timer.cancel()
            except Exception:
                pass
            self._reconnect_timer = None

    def _schedule_reconnect(self):
        if self.stopped:
            return
        delay = self.backoff
        self.backoff = min(self.reconnect_max,
                           max(self.reconnect_min, self.backoff * 2))
        self._cancel_timer()
        timer = threading.Timer(delay, self._reconnect)
        timer.daemon = True
        self._reconnect_timer = timer
        timer.start()

    def _reconnect(self):
        if not self.stopped:
            self._connect()

    # ── события ──────────────────────────────────────────────────────
    def _on_open(self):
        self.connected = True
        self.backoff = self.reconnect_min
        self._auth_retried = False
        self._cancel_timer()
        self._log("WS открыт, отправляю auth/signIn")
        self._send_frame({"route": AUTH_ROUTE, "payload": {"token": ""}},
                         require_auth=False)
        if callable(self.on_connected):
            try:
                self.on_connected(True)
            except Exception:
                pass

    def _on_disconnected(self, reason=""):
        self.connected = False
        self.authenticated = False
        self.status = str(reason or "")
        self._cancel_keepalive()
        self._log(f"WS закрыт ({self.status or 'без причины'})")
        if callable(self.on_disconnected):
            try:
                self.on_disconnected(self.status)
            except Exception:
                pass
        self._schedule_reconnect()

    def _cancel_keepalive(self):
        if self._keepalive_timer is not None:
            try:
                self._keepalive_timer.cancel()
            except Exception:
                pass
            self._keepalive_timer = None

    def _start_keepalive(self):
        """Удерживать WS-сессию: ping раз в 45 с (иначе сайт откатит offline)."""
        self._cancel_keepalive()

        def tick():
            if self.stopped or not self.connected:
                return
            self._keepalive_ping()
            self._start_keepalive()

        timer = threading.Timer(self.keepalive_sec, tick)
        timer.daemon = True
        self._keepalive_timer = timer
        timer.start()

    def _keepalive_ping(self):
        try:
            pinger = getattr(self._transport, "ping", None)
            if callable(pinger):
                pinger()
                return True
        except Exception:
            pass
        return False

    def _on_text(self, text):
        try:
            frame = json.loads(text) if isinstance(text, str) else dict(text)
        except Exception:
            self._set_error("битый JSON от WebSocket")
            return None
        if not isinstance(frame, dict):
            return None
        route = str(frame.get("route") or "")
        payload = frame.get("payload")
        if route.endswith("auth/signIn:ok"):
            self.authenticated = True
            self.last_error = ""
            self._log("auth/signIn:ok — статус-сессия авторизована")
            self._start_keepalive()
            pending = list(self.pending)
            self.pending = []
            for item in pending:
                self._send_frame(item)
        elif route.endswith("auth/signIn:error"):
            reason = str(payload or "auth/signIn:error")
            self._log(f"auth/signIn:error ({reason})")
            if self.token and not self._auth_retried:
                # запасной вариант: JWT прямо в payload (работает и без cookie)
                self._auth_retried = True
                self._log("повтор auth с токеном в payload")
                self._send_frame({"route": AUTH_ROUTE,
                                  "payload": {"token": self.token}},
                                 require_auth=False)
            else:
                self._set_error("нужен вход на маркет")
        elif route.endswith("event/status/set"):
            parsed = parse_status_payload(payload)
            if parsed["status"]:
                self.current_status = parsed["status"]
                self.current_until = parsed.get("status_until")
                self._log("event/status/set: "
                          f"{self.current_status}"
                          + (f" до {self.current_until}" if self.current_until
                             else ""))
                self._emit_status("event")
        elif route.endswith("status/set:ok"):
            parsed = parse_status_payload(payload)
            if parsed["status"]:
                self.current_status = parsed["status"]
                self.current_until = parsed.get("status_until")
            elif self._pending_status:
                self.current_status = self._pending_status
            self._pending_status = ""
            self._log("cmd/status/set:ok: "
                      f"{self.current_status or 'подтверждено'}")
            self._emit_status("ok")
        elif route.endswith("status/set:error"):
            text = status_error_text(payload)
            self._log(f"cmd/status/set:error: {text}")
            self.current_status = self._previous_status
            self._pending_status = ""
            # t27: НЕ эмитим статус после ошибки — иначе UI затрёт текст
            # ошибки строкой «статус: <прежний>»
            self._set_error(text)
        return frame

    def _emit_status(self, source="event"):
        self.last_status_source = str(source or "event")
        if callable(self.on_status):
            try:
                self.on_status(self.current_status, self.current_until)
            except Exception:
                pass

    def _set_error(self, text):
        self.last_error = str(text or "ошибка статуса")
        self.status = self.last_error
        if callable(self.on_error):
            try:
                self.on_error(self.last_error)
            except Exception:
                pass

    # ── команды ──────────────────────────────────────────────────────
    def _send_frame(self, frame, require_auth=True):
        text = json.dumps(frame, ensure_ascii=False)
        if self.connected and self._transport is not None \
                and (self.authenticated or not require_auth):
            try:
                self._transport.send(text)
                return True
            except Exception as exc:
                self._set_error(f"WebSocket: {type(exc).__name__}")
                self._on_disconnected(str(exc))
        self.pending.append(frame)
        return False

    def set_status(self, status, duration=None):
        """Поставить статус. ``offline`` = закрыть сокет (без команды)."""
        status_raw = str(status or "").strip().lower()
        if status_raw in ("offline", "оффлайн"):
            self.stop()
            self.current_status = "offline"
            self._emit_status()
            return {"ok": True, "offline": True, "status": "offline"}
        ok, error, status, duration = validate_status_request(status, duration)
        if not ok:
            self._set_error(error)
            return {"ok": False, "error": error, "status": status}
        frame = {"route": STATUS_SET_ROUTE,
                 "payload": {"status": status, "duration": duration}}
        self._previous_status = self.current_status
        self._pending_status = status
        self._log(f"cmd/status/set: {status} · duration={duration}")
        sent = self._send_frame(frame)
        return {"ok": True, "status": status, "duration": duration,
                "queued": not sent, "authenticated": self.authenticated}

    def set_auto_status(self, game_focused):
        """Один шаг авто-режима: ingame при фокусе игры, иначе online."""
        desired = "ingame" if game_focused else "online"
        if desired == self.current_status:
            return False
        self.set_status(desired, None)
        return True


class _QtWfmTransport:
    """QWebSocket (subprotocol wfm) в QThread с Cookie JWT."""

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
            try:
                self._ws.setSubprotocols([SUBPROTOCOL])
            except Exception:
                # в PyQt5 5.15 метода нет — сообщаем subprotocol заголовком
                pass
            self._ws.textMessageReceived.connect(self.owner._on_text)
            self._ws.connected.connect(self.owner._on_open)
            self._ws.disconnected.connect(
                lambda: self.owner._on_disconnected("disconnected"))
            self._ws.error.connect(lambda _err: self.owner._set_error(
                getattr(self._ws, "errorString", lambda: "")()))
            request = QNetworkRequest(QUrl(WS_URL))
            # Cloudflare/warframe.market рвёт TLS без Origin/UA (проверено t24)
            for key, value in ws_request_headers(
                    getattr(self.owner, "token", "")).items():
                try:
                    request.setRawHeader(key.encode("ascii"),
                                         str(value).encode("ascii"))
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
            raise RuntimeError("сокет не открыт")
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
#  Сервис для GUI: авто-режим и восстановление онлайн
# ─────────────────────────────────────────────────────────────────────────
def build_status_service():
    from PyQt5.QtCore import QObject, QTimer, pyqtSignal

    class StatusService(QObject):
        status_changed = pyqtSignal(str, object)
        error = pyqtSignal(str)
        connected = pyqtSignal(bool)
        auto_changed = pyqtSignal(bool)
        confirmed = pyqtSignal(str)

        def __init__(self, cfg, token_fn=None, focus_fn=None,
                     login_check=None, transport_factory=None, logger=None,
                     parent=None):
            super().__init__(parent)
            self.cfg = dict(cfg or {})
            self.token_fn = token_fn
            self.focus_fn = focus_fn
            self.login_check = login_check or (lambda: False)
            self.transport_factory = transport_factory
            self.logger = logger
            self.socket = None
            self.auto = False
            self.last_status = ""
            self.last_auto = ""
            self.started = False
            self._timer = QTimer(self)
            self._timer.setInterval(int(self.cfg.get("status_poll_ms", 5000)
                                         or 5000))
            self._timer.timeout.connect(self.auto_tick)
            self._confirm_timer = QTimer(self)
            self._confirm_timer.setSingleShot(True)
            self._confirm_timer.setInterval(6000)
            self._confirm_timer.timeout.connect(self._confirm_timeout)
            self._awaiting_status = ""
            self._suppress_confirm = False

        def token(self):
            if callable(self.token_fn):
                try:
                    return str(self.token_fn() or "")
                except Exception:
                    return ""
            return str(self.cfg.get("market_token") or "")

        def start(self):
            if self.started and self.socket is not None:
                return True
            if self.login_check():
                return False
            token = self.token()
            if not token:
                self.error.emit("нужен вход на маркет — статус недоступен")
                return False
            self.socket = StatusSocket(
                token, transport_factory=self.transport_factory,
                on_status=self._on_socket_status,
                on_error=self._on_socket_error,
                on_connected=lambda _ok: self.connected.emit(True),
                on_disconnected=lambda _reason: self.connected.emit(False),
                on_log=self.logger)
            self.started = True
            self.socket.start()
            self.auto = bool(self.cfg.get("status_auto", False))
            if self.auto:
                self._timer.start()
                self.auto_tick()
            return True

        def stop(self, restore=True):
            self._timer.stop()
            self._confirm_timer.stop()
            self._awaiting_status = ""
            self._suppress_confirm = False
            self.started = False
            if self.socket is not None:
                if restore and bool(self.cfg.get("status_restore_online",
                                                 True)) and self.token():
                    self.socket.set_status("online", None)
                self.socket.stop()
                self.socket = None
            return True

        def set_status(self, status, duration=None):
            """Явный клик по меню: выключает авто и отправляет команду."""
            if str(status or "").lower() in STATUSES + ("offline",):
                self.set_auto(False)
            self._suppress_confirm = False
            if self.socket is None:
                if not self.start() or self.socket is None:
                    return {"ok": False, "error": "нужен вход на маркет"}
            result = self.socket.set_status(status, duration)
            if not result.get("ok"):
                self._suppress_confirm = True
                self.error.emit(result.get("error") or "ошибка статуса")
            elif result.get("status") not in ("offline", ""):
                self._awaiting_status = str(result.get("status") or "")
                self._confirm_timer.start()
            return result

        def set_auto(self, enabled):
            self.auto = bool(enabled)
            if self.auto:
                self._timer.start()
                self.auto_tick()
            else:
                self._timer.stop()
                self.last_auto = ""
            self.auto_changed.emit(self.auto)
            return self.auto

        def auto_tick(self):
            if not self.auto or self.socket is None \
                    or not self.socket.authenticated:
                return False
            focused = False
            if callable(self.focus_fn):
                try:
                    focused = bool(self.focus_fn())
                except Exception:
                    focused = False
            desired = "ingame" if focused else "online"
            if desired == self.last_auto:
                return False
            self.last_auto = desired
            self.socket.set_status(desired, None)
            return True

        def _confirm_timeout(self):
            if not self._awaiting_status:
                return
            text = ("нет подтверждения статуса — проверьте вход/сеть")
            self._awaiting_status = ""
            self._suppress_confirm = True
            self.error.emit(text)

        def _on_socket_error(self, text):
            # t27: после ошибки не даём «статус: …» затереть текст ошибки
            self._suppress_confirm = True
            self.error.emit(str(text or "ошибка статуса"))

        def _on_socket_status(self, status, until=None):
            self.last_status = str(status or "")
            source = str(getattr(self.socket, "last_status_source", "event"))
            if source == "ok":
                # сервер подтвердил новую команду — ошибка больше не актуальна
                self._suppress_confirm = False
            if self.last_status:
                self._confirm_timer.stop()
                self._awaiting_status = ""
            if self.last_status in ("online", "ingame"):
                self.last_auto = self.last_status if self.auto else ""
            self.status_changed.emit(self.last_status, until)
            if self.last_status and not self._suppress_confirm:
                self.confirmed.emit(status_label(self.last_status))

    return StatusService


# ─────────────────────────────────────────────────────────────────────────
#  UI: чип статуса в шапке
# ─────────────────────────────────────────────────────────────────────────
def make_status_chip_style(status, auto=False):
    color = status_color(status)
    text = status_label(status)
    if auto:
        text = f"{text} · авто"
    return f"● {text}", color
