# -*- coding: utf-8 -*-
"""1.0.3: один экземпляр приложения (QLocalServer/QLocalSocket)."""
import getpass
import os
import re

from PyQt5.QtCore import QObject, QTimer, pyqtSignal
from PyQt5.QtCore import QSharedMemory
from PyQt5.QtNetwork import QLocalServer, QLocalSocket


def user_name():
    try:
        name = getpass.getuser()
    except Exception:
        name = os.environ.get("USERNAME") or "user"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name or "user")


def socket_name(prefix="VoidFrameRU"):
    return f"{prefix}-{user_name()}"


class SingleInstance(QObject):
    show_requested = pyqtSignal()

    def __init__(self, name=None, logger=None, timeout_ms=500,
                 parent=None):
        super().__init__(parent)
        self.name = name or socket_name()
        self.logger = logger
        self.timeout_ms = int(timeout_ms or 500)
        self.state = "unknown"       # primary | second | normal | error
        self.pid = 0
        self.stale = False
        self.error = ""
        self._server = None
        self._socket = None
        self._memory = None
        self._timer = None

    def _log(self, message):
        if callable(self.logger):
            try:
                self.logger(message)
            except Exception:
                pass

    def acquire(self):
        sock = QLocalSocket(self)
        sock.connectToServer(self.name)
        if sock.waitForConnected(self.timeout_ms):
            self._socket = sock
            # t81: подключение уже доказывает, что экземпляр запущен;
            # show — best-effort, pid/ответ не обязательны.
            try:
                sock.write(b"show")
                sock.flush()
                sock.waitForBytesWritten(200)
            except Exception as e:
                self._log(f"single_instance: show не отправлен "
                          f"({type(e).__name__}: {e})")
            try:
                if sock.waitForReadyRead(300):
                    raw = bytes(sock.readAll()).decode("ascii", "ignore")
                    digits = "".join(ch for ch in raw if ch.isdigit())
                    self.pid = int(digits) if digits else 0
            except Exception:
                self.pid = 0
            self.state = "second"
            try:
                sock.disconnectFromServer()
            except Exception:
                pass
            return self.state
        # сокет не отвечает: снимаем устаревший и становимся сервером
        self.stale = True
        try:
            QLocalServer.removeServer(self.name)
        except Exception:
            pass
        server = QLocalServer(self)
        server.newConnection.connect(self._on_new_connection)
        if server.listen(self.name):
            self._server = server
            self.state = "primary"
        else:
            self.error = server.errorString()
            # QLocalServer может быть запрещён политикой/песочницей —
            # используем QSharedMemory (команда show через флаг).
            memory = QSharedMemory(self.name)
            if memory.create(64):
                self._memory = memory
                memory.lock()
                payload = f"primary:{os.getpid()}".encode("ascii")
                memory.data()[:64] = payload + b"\x00" * (64 - len(payload))
                memory.unlock()
                self._timer = QTimer(self)
                self._timer.setInterval(150)
                self._timer.timeout.connect(self._poll_memory)
                self._timer.start()
                self.state = "primary"
            else:
                attached = QSharedMemory(self.name)
                if attached.attach():
                    self._memory = attached
                    self.pid = self._memory_pid()
                    payload = f"show:{os.getpid()}".encode("ascii")
                    attached.lock()
                    data = attached.data()
                    data[:min(64, len(payload))] = payload[:64]
                    attached.unlock()
                    self.state = "second"
                else:
                    self.state = "normal"
        return self.state

    def _memory_pid(self):
        try:
            self._memory.lock()
            raw = bytes(self._memory.data()[:64])
            self._memory.unlock()
            text = raw.decode("ascii", "ignore").strip("\x00 ")
            return int(text.split(":")[-1]) if text.split(":")[-1].isdigit() else 0
        except Exception:
            return 0

    def _poll_memory(self):
        if self._memory is None:
            return
        try:
            self._memory.lock()
            data = self._memory.data()
            raw = bytes(data[:64]).rstrip(b"\x00")
            if raw.startswith(b"show"):
                data[:64] = b"\x00" * 64
                emit = True
            else:
                emit = False
            self._memory.unlock()
            if emit:
                self.show_requested.emit()
        except Exception:
            pass

    def _on_new_connection(self):
        while self._server is not None and self._server.hasPendingConnections():
            conn = self._server.nextPendingConnection()
            try:
                conn.readAll()
            except Exception:
                pass
            # t81: любое входящее подключение поднимает окно
            self.show_requested.emit()
            try:
                conn.write(str(os.getpid()).encode("ascii"))
                conn.flush()
                conn.disconnectFromServer()
            except Exception as e:
                self._log(f"single_instance: pipe write failed "
                          f"({type(e).__name__}: {e})")

    def close(self):
        try:
            if self._socket is not None:
                self._socket.disconnectFromServer()
                self._socket.deleteLater()
                self._socket = None
        except Exception:
            pass
        try:
            if self._timer is not None:
                self._timer.stop()
                self._timer.deleteLater()
                self._timer = None
        except Exception:
            pass
        try:
            if self._memory is not None:
                self._memory.detach()
                self._memory = None
        except Exception:
            pass
        try:
            if self._server is not None:
                self._server.close()
                self._server.deleteLater()
                self._server = None
        except Exception:
            pass


_INSTANCE = None


def acquire(logger=None, timeout_ms=500):
    """Вернуть SingleInstance (primary/second/...)."""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = SingleInstance(logger=logger, timeout_ms=timeout_ms)
        _INSTANCE.acquire()
    return _INSTANCE


def show_requested_signal():
    return _INSTANCE.show_requested if _INSTANCE is not None else None


def close():
    global _INSTANCE
    if _INSTANCE is not None:
        _INSTANCE.close()
        _INSTANCE = None


__all__ = ["SingleInstance", "socket_name", "acquire", "close",
           "show_requested_signal"]
