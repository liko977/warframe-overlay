# -*- coding: utf-8 -*-
"""Вход на warframe.market через браузер пользователя (любой: Chrome, Edge,
Yandex, Opera, Opera GX, Firefox…).

Почему не встроенный Chromium: QtWebEngine 5.15 — это Chromium 87, и
Cloudflare не пропускает его на /login («Performing security verification»,
уход на /error/404, JWT не появляется). Поэтому основной способ входа —
запустить **браузер пользователя** с отдельным профилем в папке данных
приложения и забрать HttpOnly-cookie ``JWT``, проверить ``GET /v2/me`` и
сохранить токен. Список браузеров собирается из реестра Windows и типичных
путей, семейство определяется по имени/пути, есть ручной выбор любого .exe.

Два транспорта по семейству:

* **Chromium** (Chrome, Edge, Yandex, Opera, Opera GX, Brave, Vivaldi…):
  ``--remote-debugging-port`` + ``/json/list`` + WebSocket CDP
  (``Network.enable``/``Network.getAllCookies``);
* **Firefox** (Firefox, Waterfox, LibreWolf…): CDP у Gecko нет — запускаем
  ``-no-remote -new-instance -profile <наш профиль>`` и читаем копию
  ``cookies.sqlite`` (таблица ``moz_cookies``), пока не появится JWT.

Обе схемы проверены капитаном вживую (Chrome/Edge/Yandex — CDP; Firefox —
профиль). Браузер закрывается после входа (Chromium — ``Browser.close``,
Firefox — ``taskkill`` по PID нашего процесса).

Границы безопасности:
* **Пароль нигде не спрашивается и не хранится** — его вводит пользователь
  в настоящем браузере на сайте warframe.market; наш код его не видит.
* **Токен только маской** в логах/сообщениях (``••••1234``), полностью
  уходит лишь на ``api.warframe.market`` в заголовке ``Authorization``.
* **Личный профиль пользователя не трогаем**: только свои папки
  ``<данные>/wf_browser_profile`` (Chromium) и ``<данные>/wf_firefox_profile``
  (Firefox), удаляются кнопкой «Выйти».

Всё сетевое/браузерное — в фоновом потоке: GUI не блокируется. Модуль не
требует PyQtWebEngine; Qt нужен только для самого окна входа (ленивый импорт
в :func:`build_login_dialog`) и для WebSocket-транспорта.
"""

import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

try:                              # клиент рынка/токен-контроллер лежат рядом
    import market_auth as ma
except Exception:                 # pragma: no cover - модули всегда вместе
    ma = None

SITE = "https://warframe.market/"
PROFILE_FOLDER = "wf_browser_profile"        # профиль Chromium-браузера
FIREFOX_PROFILE_FOLDER = "wf_firefox_profile"  # профиль Firefox (cookies.sqlite)
PROBE_FOLDER = "wf_browser_probe"            # временная папка для кнопки «Проверить»
DEFAULT_TIMEOUT = 600                   # 10 минут на вход (сек)
DEFAULT_POLL = 1.0                      # период опроса cookie, сек
AUTH_DIAG_FILE = "auth_diag.log"        # t29: диагностика входа в DATA_DIR
DISCOVERY_TIMEOUT = 20.0                # ожидание отладочного порта, сек
PORT_ATTEMPTS = 3                       # сколько портов пробовать подряд
MIN_TOKEN_LEN = 20

#: Семейства браузеров: Chromium умеет CDP, Firefox — только свой профиль.
FAMILY_CHROMIUM = "chromium"
FAMILY_FIREFOX = "firefox"
FAMILY_UNKNOWN = "unknown"
FAMILY_TEXT = {
    FAMILY_CHROMIUM: "Chromium (CDP)",
    FAMILY_FIREFOX: "Firefox (отдельный профиль)",
    FAMILY_UNKNOWN: "неизвестный браузер",
}
_FAMILY_ORDER = {FAMILY_CHROMIUM: 0, FAMILY_FIREFOX: 1, FAMILY_UNKNOWN: 2}

#: Ключевые слова для определения семейства по имени exe и пути.
CHROMIUM_HINTS = (
    "chrome", "chromium", "msedge", "edge", "yandex", "opera", "brave",
    "vivaldi", "dragon", "comodo", "atom", "thorium", "slimjet", "epic",
    "kinza", "sidekick", "avast", "avg browser", "360", "maxthon", "arc",
)
FIREFOX_HINTS = (
    "firefox", "waterfox", "librewolf", "palemoon", "basilisk", "seamonkey",
    "icecat", "iceweasel", "floorp", "midori",
)

#: Типичные установочные пути (относительно корней из BROWSER_ROOT_VARS).
KNOWN_BROWSERS = (
    (("Google", "Chrome", "Application", "chrome.exe"),
     "Google Chrome", FAMILY_CHROMIUM),
    (("Microsoft", "Edge", "Application", "msedge.exe"),
     "Microsoft Edge", FAMILY_CHROMIUM),
    (("Yandex", "YandexBrowser", "Application", "browser.exe"),
     "Yandex Browser", FAMILY_CHROMIUM),
    (("BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
     "Brave", FAMILY_CHROMIUM),
    (("Vivaldi", "Application", "vivaldi.exe"), "Vivaldi", FAMILY_CHROMIUM),
    (("Chromium", "Application", "chrome.exe"), "Chromium", FAMILY_CHROMIUM),
    (("Comodo", "Dragon", "dragon.exe"), "Comodo Dragon", FAMILY_CHROMIUM),
    (("Opera", "opera.exe"), "Opera", FAMILY_CHROMIUM),
    (("Opera", "launcher.exe"), "Opera", FAMILY_CHROMIUM),
    (("Opera GX", "opera.exe"), "Opera GX", FAMILY_CHROMIUM),
    (("Opera GX", "launcher.exe"), "Opera GX", FAMILY_CHROMIUM),
    (("Programs", "Opera", "opera.exe"), "Opera", FAMILY_CHROMIUM),
    (("Programs", "Opera", "launcher.exe"), "Opera", FAMILY_CHROMIUM),
    (("Programs", "Opera GX", "opera.exe"), "Opera GX", FAMILY_CHROMIUM),
    (("Programs", "Opera GX", "launcher.exe"), "Opera GX", FAMILY_CHROMIUM),
    (("Mozilla Firefox", "firefox.exe"), "Mozilla Firefox", FAMILY_FIREFOX),
    (("Waterfox", "waterfox.exe"), "Waterfox", FAMILY_FIREFOX),
    (("Waterfox", "Waterfox", "waterfox.exe"), "Waterfox", FAMILY_FIREFOX),
    (("LibreWolf", "librewolf.exe"), "LibreWolf", FAMILY_FIREFOX),
    (("LibreWolf", "LibreWolf", "librewolf.exe"), "LibreWolf", FAMILY_FIREFOX),
)
BROWSER_ROOT_VARS = ("PROGRAMFILES", "PROGRAMW6432", "PROGRAMFILES(X86)",
                     "LOCALAPPDATA")

#: Ветки реестра, где Windows хранит установленные браузеры.
REGISTRY_ROOTS = (
    ("HKLM", r"SOFTWARE\Clients\StartMenuInternet"),
    ("HKLM", r"SOFTWARE\WOW6432Node\Clients\StartMenuInternet"),
    ("HKCU", r"SOFTWARE\Clients\StartMenuInternet"),
    ("HKCU", r"SOFTWARE\WOW6432Node\Clients\StartMenuInternet"),
)
SKIP_REGISTRY_NAMES = {"IEXPLORE", "INTERNET EXPLORER"}

#: Флаги Firefox: отдельный профиль в папке данных, без чужого инстанса.
FIREFOX_FLAGS = ("-no-remote", "-new-instance", "-profile")

#: Тихий user.js для профиля Firefox (без приветствий и проверок по умолчанию).
FIREFOX_USER_JS = """\
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("toolkit.telemetry.enabled", false);
user_pref("app.update.enabled", false);
user_pref("network.cookie.cookieBehavior", 0);
"""

# ── состояния входа (для UI и тестов) ───────────────────────────────────
STATE_OK = "ok"
STATE_NO_BROWSER = "no_browser"
STATE_BROWSER_CLOSED = "browser_closed"
STATE_TIMEOUT = "timeout"
STATE_NO_TARGET = "no_target"
STATE_CANCELLED = "cancelled"
STATE_ERROR = "error"
STATE_EMPTY = "empty"

STATE_TEXT = {
    STATE_OK: "токен получен",
    STATE_NO_BROWSER: "браузер не найден",
    STATE_BROWSER_CLOSED: "браузер закрыт",
    STATE_TIMEOUT: "время ожидания истекло",
    STATE_NO_TARGET: "нет соединения с браузером",
    STATE_CANCELLED: "вход отменён",
    STATE_ERROR: "ошибка входа",
    STATE_EMPTY: "ожидаю вход",
}


class BrowserLoginError(RuntimeError):
    """Ошибка входа через браузер (сообщение уже на русском, без токена)."""


class BrowserNotFound(BrowserLoginError):
    """Не нашли ни одного поддерживаемого браузера."""


class BrowserClosed(BrowserLoginError):
    """Пользователь закрыл браузер до того, как вход завершился."""


class DiscoveryFailed(BrowserLoginError):
    """Отладочный порт не поднялся за отведённое время."""


class CdpError(BrowserLoginError):
    """Ошибка протокола CDP / соединения с браузером."""


class CdpTimeout(CdpError):
    """Браузер не ответил по CDP за отведённое время."""


# ─────────────────────────────────────────────────────────────────────────
#  Мелкие утилиты: маскирование, браузер, порт, cookie
# ─────────────────────────────────────────────────────────────────────────
def mask_token(token):
    """Безопасное представление токена (никогда не целиком)."""
    if ma is not None:
        try:
            return ma.mask_token(token)
        except Exception:
            pass
    token = str(token or "").strip()
    if not token:
        return "нет токена"
    tail = token[-4:] if len(token) >= 8 else ""
    return f"••••{tail}" if tail else "••••"


def scrub(text):
    """Вырезает из строки всё похожее на JWT (страховка логов)."""
    if ma is not None:
        try:
            return ma.scrub(text)
        except Exception:
            pass
    return str(text or "")


def looks_like_jwt(value):
    token = str(value or "").strip()
    return token.count(".") == 2 and len(token) >= MIN_TOKEN_LEN


def browser_family(path, name=""):
    """Семейство браузера по имени exe/пути: ``chromium``, ``firefox``, ``unknown``."""
    text = (str(path or "") + " " + str(name or "")).lower().replace("\\", "/")
    for hint in FIREFOX_HINTS:
        if hint in text:
            return FAMILY_FIREFOX
    for hint in CHROMIUM_HINTS:
        if hint in text:
            return FAMILY_CHROMIUM
    return FAMILY_UNKNOWN


def browser_title(path, fallback=""):
    """Человекочитаемое название браузера (Chrome, Yandex, Opera GX, Firefox…)."""
    text = (str(path or "") + " " + str(fallback or "")).lower()
    rules = (
        ("opera gx", "Opera GX"), ("opera", "Opera"),
        ("yandex", "Yandex Browser"),
        ("msedge", "Microsoft Edge"), ("edge", "Microsoft Edge"),
        ("brave", "Brave"), ("vivaldi", "Vivaldi"),
        ("dragon", "Comodo Dragon"), ("comodo", "Comodo Dragon"),
        ("atom", "Atom"), ("thorium", "Thorium"),
        ("librewolf", "LibreWolf"), ("waterfox", "Waterfox"),
        ("palemoon", "Pale Moon"), ("basilisk", "Basilisk"),
        ("firefox", "Mozilla Firefox"),
        ("chrome", "Google Chrome"), ("chromium", "Chromium"),
    )
    for key, title in rules:
        if key in text:
            return title
    fallback = str(fallback or "").strip()
    if fallback:
        return fallback
    return os.path.basename(str(path or "")) or "браузер"


def _exe_from_command(command):
    """Путь к exe из значения реестра ``shell\\open\\command``."""
    text = os.path.expandvars(str(command or "").strip())
    if not text:
        return ""
    if text.startswith('"'):
        end = text.find('"', 1)
        if end > 1:
            return text[1:end].strip()
    for token in text.split():
        token = token.strip('"')
        if token.lower().endswith(".exe"):
            return token
    return ""


def _registry_entries_default():
    """Браузеры из реестра Windows (HKLM/HKCU, включая WOW6432Node)."""
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except Exception:
        return []
    hives = {"HKLM": winreg.HKEY_LOCAL_MACHINE,
             "HKCU": winreg.HKEY_CURRENT_USER}
    found = []
    for hive_name, subkey in REGISTRY_ROOTS:
        hive = hives.get(hive_name)
        if hive is None:
            continue
        try:
            with winreg.OpenKey(hive, subkey) as root:
                count = winreg.QueryInfoKey(root)[0]
                names = [winreg.EnumKey(root, index) for index in range(count)]
        except OSError:
            continue
        for raw_name in names:
            if str(raw_name).upper() in SKIP_REGISTRY_NAMES:
                continue
            command_key = subkey + "\\" + raw_name + r"\shell\open\command"
            try:
                with winreg.OpenKey(hive, command_key) as key:
                    command, _kind = winreg.QueryValueEx(key, "")
            except OSError:
                continue
            exe = _exe_from_command(command)
            if exe:
                found.append({"name": str(raw_name), "command": command,
                              "path": exe, "source": "registry"})
    return found


def _normalize_registry(registry):
    """Приводит подделки/записи реестра к списку ``{"name", "path"}``."""
    entries = []
    for item in registry or []:
        if isinstance(item, dict):
            name = str(item.get("name") or "")
            path = str(item.get("path") or "")
            command = str(item.get("command") or "")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            name, command = str(item[0]), str(item[1])
            path = ""
        else:
            continue
        if not path:
            path = _exe_from_command(command)
        if not path:
            continue
        entries.append({"name": name, "path": path,
                        "source": "registry"})
    return entries


def _dedupe_browsers(entries):
    """Убирает дубликаты по реальному пути (регистр/разделители не важны)."""
    seen = set()
    result = []
    for entry in entries:
        path = str(entry.get("path") or "").strip()
        if not path:
            continue
        full = os.path.abspath(path)
        key = os.path.normcase(full)
        if key in seen:
            continue
        seen.add(key)
        item = dict(entry)
        item["path"] = full
        item.setdefault("name", browser_title(full))
        item.setdefault("family", browser_family(full, item.get("name")))
        result.append(item)
    return result


def discover_browsers(explicit="", environ=None, is_file=None, roots=None,
                      registry=None, read_registry=None):
    """Все найденные браузеры (реестр + типичные пути + ручной выбор).

    Возвращает список словарей ``{"path", "name", "family", "source"}``,
    дедуплицированный по реальному пути. Порядок: сохранённый в конфиге
    (``market_browser_path``), затем Chromium-семейство, затем Firefox,
    затем остальные.

    :param explicit: путь из ``config.json`` (может быть любым .exe).
    :param environ: подмена переменных окружения (тесты).
    :param is_file: подмена ``os.path.isfile`` (тесты).
    :param roots: явные корни установки (тесты).
    :param registry: готовые записи реестра — список ``{"name", "command"}``
        или ``(name, command)`` (тесты).
    :param read_registry: подмена чтения реестра (тесты).
    """
    is_file = is_file or os.path.isfile
    env = os.environ if environ is None else environ
    explicit = str(explicit or "").strip().strip('"')
    entries = []

    if explicit and is_file(explicit):
        entries.append({"path": explicit,
                        "name": browser_title(explicit),
                        "family": browser_family(explicit),
                        "source": "config"})
    for key in ("WFRU_BROWSER", "WF_MARKET_BROWSER"):
        value = str(env.get(key) or "").strip().strip('"')
        if value and is_file(value):
            entries.append({"path": value,
                            "name": browser_title(value),
                            "family": browser_family(value),
                            "source": "env"})
            break

    if registry is None:
        reader = read_registry or _registry_entries_default
        try:
            registry = reader()
        except Exception:
            registry = []
    for item in _normalize_registry(registry):
        path = item["path"]
        if not is_file(path):
            continue
        entries.append({"path": path,
                        "name": browser_title(path, item.get("name")),
                        "family": browser_family(path, item.get("name")),
                        "source": "registry"})

    if roots is not None:
        bases = [str(root) for root in roots if root]
    else:
        bases = []
        for var in BROWSER_ROOT_VARS:
            value = env.get(var) or env.get(var.upper()) or env.get(var.lower())
            if value and value not in bases:
                bases.append(str(value))
    for base in bases:
        for parts, title, family in KNOWN_BROWSERS:
            path = os.path.join(base, *parts)
            if not is_file(path):
                continue
            entries.append({"path": path, "name": title, "family": family,
                            "source": "path"})

    deduped = _dedupe_browsers(entries)
    # config/env — наверх, дальше Chromium, Firefox, прочие (порядок стабилен)
    def sort_key(indexed):
        index, item = indexed
        source = item.get("source")
        source_rank = 0 if source in ("config", "env") else 1
        family_rank = _FAMILY_ORDER.get(item.get("family"), 2)
        return (source_rank, family_rank, index)

    ordered = [item for _index, item in
               sorted(enumerate(deduped), key=sort_key)]
    return ordered


def find_browsers(explicit="", **kwargs):
    """Список найденных браузеров (см. :func:`discover_browsers`)."""
    return discover_browsers(explicit, **kwargs)


def find_browser(explicit="", environ=None, is_file=None, roots=None,
                 registry=None, read_registry=None):
    """Путь к выбранному/первому найденному браузеру или ``""``.

    Сохранённый в конфиге путь приоритетен; иначе — первый Chromium,
    затем Firefox, затем прочие (порядок :func:`discover_browsers`).
    """
    browsers = discover_browsers(explicit, environ=environ, is_file=is_file,
                                 roots=roots, registry=registry,
                                 read_registry=read_registry)
    return browsers[0]["path"] if browsers else ""


def free_port(host="127.0.0.1"):
    """Свободный TCP-порт (bind на 0)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def jwt_from_cookies(cookies):
    """Список cookie из CDP → значение ``JWT`` для warframe.market (или ``""``)."""
    for cookie in cookies or []:
        if not isinstance(cookie, dict):
            continue
        if str(cookie.get("name") or "") != "JWT":
            continue
        domain = str(cookie.get("domain") or "").strip().lower().lstrip(".")
        if domain != "warframe.market" and not domain.endswith(".warframe.market"):
            continue
        value = str(cookie.get("value") or "").strip()
        if looks_like_jwt(value):
            return value
    return ""


def _pick_page_target(targets):
    """target ``type=page`` с ``webSocketDebuggerUrl`` (или None)."""
    items = [item for item in (targets or []) if isinstance(item, dict)]
    for item in items:
        if (str(item.get("type") or "") == "page"
                and item.get("webSocketDebuggerUrl")):
            return item
    for item in items:
        if item.get("webSocketDebuggerUrl"):
            return item
    return None


# ─────────────────────────────────────────────────────────────────────────
#  Firefox: cookie JWT из отдельного профиля (cookies.sqlite)
# ─────────────────────────────────────────────────────────────────────────
class FirefoxProfileCookies:
    """Чтение JWT из ``cookies.sqlite`` отдельного профиля Firefox.

    Firefox (Gecko) не поддерживает CDP, зато хранит cookie в открытом виде
    в ``cookies.sqlite`` (таблица ``moz_cookies``). Браузер держит файл
    открытым, поэтому читаем **копию** базы во временной папке (вместе с
    ``-wal``/``-shm``), а не сам профиль. Личный профиль пользователя не
    используется — только наша папка в данных приложения.

    :param profile_dir: папка нашего профиля Firefox.
    :param domain: домен, для которого ищем cookie (``warframe.market``).
    :param copy_func: подмена копирования файла (тесты).
    :param sqlite_connect: подмена ``sqlite3.connect`` (тесты).
    :param keep_temp: не удалять временную копию (диагностика).
    """

    def __init__(self, profile_dir, domain="warframe.market",
                 copy_func=None, sqlite_connect=None, keep_temp=False):
        self.profile_dir = str(profile_dir or "")
        self.domain = str(domain or "warframe.market").strip().lower().lstrip(".")
        self.copy_func = copy_func or shutil.copy2
        self.sqlite_connect = sqlite_connect
        self.keep_temp = bool(keep_temp)

    def db_path(self):
        return os.path.join(self.profile_dir, "cookies.sqlite")

    def _copy_db(self):
        """Копия cookies.sqlite (+wal/shm) во временную папку.

        Возвращает ``(temp_dir, db_path)`` или ``(None, None)``, если файла
        ещё нет/скопировать не удалось (браузер мог держать его открытым).
        """
        source = self.db_path()
        if not os.path.isfile(source):
            return None, None
        temp_dir = tempfile.mkdtemp(prefix="wf_ff_cookies_")
        target = os.path.join(temp_dir, "cookies.sqlite")
        try:
            self.copy_func(source, target)
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            return None, None
        for suffix in ("-wal", "-shm"):
            side = source + suffix
            if not os.path.isfile(side):
                continue
            try:
                self.copy_func(side, target + suffix)
            except Exception:
                pass
        return temp_dir, target

    def cookies(self):
        """``[(name, value, host), …]`` из копии БД (или ``[]``)."""
        import sqlite3
        temp_dir, path = self._copy_db()
        if path is None:
            return []
        connect = self.sqlite_connect or sqlite3.connect
        rows = []
        try:
            connection = connect(path)
            try:
                cursor = connection.execute(
                    "SELECT name, value, host FROM moz_cookies")
                rows = [(str(row[0]), str(row[1]), str(row[2]))
                        for row in cursor.fetchall()]
            finally:
                try:
                    connection.close()
                except Exception:
                    pass
        except Exception:
            rows = []
        finally:
            if not self.keep_temp:
                shutil.rmtree(temp_dir, ignore_errors=True)
        return rows

    def token(self):
        """JWT для домена ``warframe.market`` из профиля (или ``""``)."""
        for name, value, host in self.cookies():
            if name != "JWT":
                continue
            host = str(host or "").strip().lower().lstrip(".")
            if host != self.domain and not host.endswith("." + self.domain):
                continue
            if looks_like_jwt(value):
                return value
        return ""


def _taskkill(pid):
    """Аккуратно завершает процесс браузера вместе с дочерними (Windows)."""
    if not pid:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10)
            return
        except Exception:
            pass
    try:
        import signal
        os.kill(int(pid), signal.SIGTERM)
    except Exception:
        pass


def _terminate_process(proc, family="", kill_process=None):
    """Закрывает запущенный нами браузер (Firefox — taskkill по PID)."""
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return
    except Exception:
        return
    if family == FAMILY_FIREFOX:
        pid = getattr(proc, "pid", None)
        if pid:
            try:
                (kill_process or _taskkill)(int(pid))
                try:
                    proc.wait(timeout=3)
                except Exception:
                    pass
                return
            except Exception:
                pass
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def raise_process_window(pid, attempts=15, interval=0.4, sleep=None):
    """Поднимает первое видимое окно процесса ``pid`` на передний план.

    Chrome с отдельным профилем показывает окно с задержкой, поэтому
    повторяем несколько раз (по умолчанию ~6 с). ``SetForegroundWindow``
    напрямую из фонового процесса Windows блокирует (нет права переднего
    плана), поэтому используется тот же приём, что и в
    ``warframe.force_foreground``: ``AllowSetForegroundWindow(-1)`` + трюк с
    нажатием Alt.

    Все Win32-вызовы обёрнуты в ``try/except``: любая ошибка — просто
    ``False``. Пользователь переключится на браузер сам — это не критично,
    окна приложения к этому моменту уже спрятаны.

    :return: ``True``, если окно процесса удалось вывести на передний план.
    """
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0 or os.name != "nt":
        return False
    waiter = sleep or time.sleep
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
    except Exception:
        return False

    enum_proc_type = None
    try:
        enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                            wintypes.LPARAM)
    except Exception:
        return False

    def windows_of_process():
        found = []

        def callback(hwnd, _lparam):
            try:
                owner = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                if int(owner.value) != pid:
                    return True
                if not user32.IsWindowVisible(hwnd):
                    return True
                if int(user32.GetWindowTextLengthW(hwnd)) <= 0:
                    return True
                found.append(int(hwnd))
            except Exception:
                pass
            return True

        try:
            user32.EnumWindows(enum_proc_type(callback), 0)
        except Exception:
            return []
        return found

    def foreground(hwnd):
        try:
            return int(user32.GetForegroundWindow()) == int(hwnd)
        except Exception:
            return False

    attempts = max(1, int(attempts))
    for attempt in range(attempts):
        for hwnd in windows_of_process():
            try:
                user32.ShowWindow(hwnd, 9)          # SW_RESTORE
                user32.BringWindowToTop(hwnd)
            except Exception:
                pass
            try:
                user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
            except Exception:
                pass
            try:
                if user32.SetForegroundWindow(hwnd) and foreground(hwnd):
                    return True
            except Exception:
                pass
            try:
                # трюк с «Alt»: снимает запрет Windows на смену фокуса
                user32.keybd_event(0x12, 0, 0, 0)
                user32.SetForegroundWindow(hwnd)
                user32.keybd_event(0x12, 0, 2, 0)
                if foreground(hwnd):
                    return True
            except Exception:
                pass
        if attempt + 1 < attempts:
            try:
                waiter(interval)
            except Exception:
                pass
    return False


def _default_http_json(url, timeout=2.0):
    request = urllib.request.Request(
        url, headers={"User-Agent": "VoidFrameRU/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read().decode("utf-8", "replace")
    return json.loads(data)


# ─────────────────────────────────────────────────────────────────────────
#  WebSocket-транспорт поверх QtWebSockets (ленивый импорт PyQt5)
# ─────────────────────────────────────────────────────────────────────────
class _WsSocketThread:
    """Поток с собственным ``QWebSocket`` и обработкой событий Qt.

    ``QWebSocket`` нельзя надёжно использовать в обычном Python-потоке
    (сокет не получает событий без Qt-потока), поэтому сокет живёт в
    ``QThread``: там же крутится ``QCoreApplication.processEvents``, а обмен
    с вызывающим потоком идёт через потокобезопасные очереди.
    """

    def __init__(self, url, timeout):
        self._url = str(url)
        self._timeout = float(timeout)
        self._inbox = queue.Queue()        # команды: str (отправить), None (закрыть)
        self._outbox = queue.Queue()       # текстовые сообщения из браузера
        self._ready = threading.Event()
        self._error = ""
        self._stop_requested = False
        self._ws = None
        self._qthread = None

    # эти методы вызываются в потоке QThread ─────────────────────────
    def _on_text(self, text):
        self._outbox.put(str(text))

    def _on_error(self, _error):
        try:
            self._error = str(self._ws.errorString() or "ошибка WebSocket")
        except Exception:
            self._error = "ошибка WebSocket"
        self._outbox.put("")

    def _connected(self):
        try:
            from PyQt5.QtNetwork import QAbstractSocket
            return self._ws is not None and \
                self._ws.state() == QAbstractSocket.ConnectedState
        except Exception:
            return False

    def run(self):
        try:
            from PyQt5.QtCore import QCoreApplication, QEventLoop, QUrl
            from PyQt5.QtWebSockets import QWebSocket
            self._ws = QWebSocket()
            self._ws.textMessageReceived.connect(self._on_text)
            self._ws.error.connect(self._on_error)
            self._ws.open(QUrl(self._url))
            deadline = time.monotonic() + self._timeout
            while (not self._connected() and not self._error
                   and time.monotonic() < deadline):
                QCoreApplication.processEvents(QEventLoop.AllEvents, 50)
            if self._error or not self._connected():
                if not self._error:
                    self._error = "браузер не открыл WebSocket вовремя"
                self._ready.set()
                return
            self._ready.set()
            while not self._stop_requested:
                QCoreApplication.processEvents(QEventLoop.AllEvents, 20)
                try:
                    item = self._inbox.get_nowait()
                except queue.Empty:
                    continue
                if item is None:
                    break
                try:
                    self._ws.sendTextMessage(str(item))
                except Exception as exc:
                    self._error = f"WebSocket: {type(exc).__name__}"
                    break
            try:
                self._ws.close()
            except Exception:
                pass
            QCoreApplication.processEvents(QEventLoop.AllEvents, 50)
        except Exception as exc:          # pragma: no cover - диагностика
            self._error = f"WebSocket: {type(exc).__name__}: {exc}"
            self._ready.set()

    # эти методы вызываются из вызывающего потока ──────────────────
    def start_socket(self, wait_seconds):
        from PyQt5.QtCore import QThread
        owner = self

        class _WsQThread(QThread):
            def run(self_inner):          # noqa: N805 - имя ради ясности
                owner.run()

        self._qthread = _WsQThread()
        self._qthread.start()
        return self._ready.wait(max(1.0, float(wait_seconds)))

    def send(self, text):
        if self._error:
            raise CdpError(self._error)
        self._inbox.put(str(text))

    def recv(self, timeout):
        if self._error:
            raise CdpError(self._error)
        try:
            return self._outbox.get(timeout=max(0.01, float(timeout)))
        except queue.Empty:
            if self._error:
                raise CdpError(self._error)
            raise CdpTimeout("браузер не ответил по CDP")

    def stop_socket(self):
        self._stop_requested = True
        try:
            self._inbox.put(None)
        except Exception:
            pass
        if self._qthread is not None:
            try:
                self._qthread.wait(3000)
            except Exception:
                pass


class QtWebSocketTransport:
    """Синхронный WS-транспорт для CDP поверх ``QWebSocket``.

    Сокет работает в отдельном ``QThread`` (см. :class:`_WsSocketThread`),
    поэтому транспорт можно безопасно вызывать из фонового потока входа.
    Заголовок ``Origin`` QWebSocket не отправляет; доступ открывает флаг
    браузера ``--remote-allow-origins=*``.
    """

    def __init__(self, url, timeout=10.0):
        from PyQt5.QtCore import QCoreApplication
        if QCoreApplication.instance() is None:
            raise CdpError("нет QApplication: QtWebSockets недоступны")
        self._thread = _WsSocketThread(url, timeout)
        if not self._thread.start_socket(float(timeout) + 2.0):
            self._thread.stop_socket()
            raise CdpTimeout("браузер не открыл WebSocket вовремя")
        if self._thread._error:
            error = self._thread._error
            self._thread.stop_socket()
            raise CdpError(f"WebSocket: {error}")

    def send_text(self, text):
        self._thread.send(text)

    def recv_text(self, timeout):
        text = self._thread.recv(timeout)
        if text == "" and self._thread._error:
            raise CdpError(f"WebSocket: {self._thread._error}")
        return text

    def close(self):
        self._thread.stop_socket()


def _default_transport_factory(url, timeout):
    return QtWebSocketTransport(url, timeout)


class CdpClient:
    """Мини-клиент CDP: сопоставление ответов по ``id`` и таймауты."""

    def __init__(self, ws_url, transport_factory=None, timeout=10.0):
        self.ws_url = str(ws_url or "")
        self.timeout = float(timeout)
        self._factory = transport_factory or _default_transport_factory
        self._transport = None
        self._next_id = 0
        self._lock = threading.RLock()

    def connect(self):
        self._transport = self._factory(self.ws_url, self.timeout)
        return self

    def call(self, method, params=None, timeout=None):
        """Отправляет команду и ждёт ответ с тем же ``id``.

        События CDP (у них нет ``id``) пропускаются.
        """
        if self._transport is None:
            raise CdpError("нет соединения с браузером (CDP)")
        with self._lock:
            self._next_id += 1
            message_id = self._next_id
            payload = {"id": message_id, "method": str(method)}
            if params:
                payload["params"] = dict(params)
            self._transport.send_text(json.dumps(payload))
            deadline = time.monotonic() + (self.timeout
                                           if timeout is None else float(timeout))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CdpTimeout(f"браузер не ответил на {method}")
                try:
                    text = self._transport.recv_text(remaining)
                except CdpTimeout:
                    raise
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except Exception:
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("id") != message_id:
                    continue          # события Network.* и прочие — мимо
                error = message.get("error")
                if isinstance(error, dict):
                    raise CdpError(str(error.get("message")
                                       or "ошибка протокола CDP"))
                return message

    def close(self):
        transport, self._transport = self._transport, None
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass


def _default_popen(cmd):
    kwargs = {}
    if sys.platform == "win32":
        # не показывать консоль у GUI-браузера; окно браузера — наше
        kwargs["creationflags"] = 0x08000000   # CREATE_NO_WINDOW
    return subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, **kwargs)


# ─────────────────────────────────────────────────────────────────────────
#  Сеанс входа через браузер (без Qt — тестируется с подделками)
# ─────────────────────────────────────────────────────────────────────────
class BrowserLoginSession:
    """Один сеанс входа: запуск браузера → опрос cookie → токен → закрытие.

    :param auth: ``market_auth.MarketAuthController`` (проверка ``/v2/me``
        и сохранение токена).
    :param cfg: настройки оверлея (``market_browser_path``,
        ``market_login_timeout_sec``).
    :param data_dir: папка данных приложения (там наш профиль браузера).
    :param browser_path: путь к браузеру (иначе ищем автоматически).
    :param timeout: сколько ждать входа, сек.
    :param poll_interval: период опроса ``Network.getAllCookies``, сек.
    :param http_json: подмена HTTP-функции ``/json/list`` (тесты).
    :param transport_factory: подмена WS-транспорта CDP (тесты).
    :param port_provider: подмена выбора порта (тесты).
    :param popen: подмена ``subprocess.Popen`` (тесты).
    :param sleep: подмена ``time.sleep``.
    :param logger: приёмник лога (по умолчанию ``print``).
    :param foreground_raiser: подмена подъёма окна браузера на передний план
        (``raise_process_window``); в тестах — без Win32.
    """

    def __init__(self, auth, cfg=None, data_dir="", browser_path=None,
                 timeout=None, poll_interval=None, discovery_timeout=None,
                 port_attempts=None, http_json=None, transport_factory=None,
                 port_provider=None, popen=None, sleep=None, logger=None,
                 clock=None, firefox_reader_factory=None, kill_process=None,
                 foreground_raiser=None, stage_callback=None):
        self.auth = auth
        self.cfg = cfg if isinstance(cfg, dict) else {}
        self.data_dir = str(data_dir or self.cfg.get("data_dir") or "")
        self.diag_path = (os.path.join(os.path.abspath(self.data_dir),
                                       AUTH_DIAG_FILE)
                          if self.data_dir else "")
        self.stage_callback = stage_callback
        self.last_port = 0
        self.browser_path = browser_path
        if timeout is None:
            try:
                timeout = float(self.cfg.get("market_login_timeout_sec")
                                or DEFAULT_TIMEOUT)
            except Exception:
                timeout = DEFAULT_TIMEOUT
        self.timeout = max(5.0, float(timeout))
        self.poll_interval = (DEFAULT_POLL if poll_interval is None
                              else max(0.0, float(poll_interval)))
        self.discovery_timeout = (DISCOVERY_TIMEOUT
                                  if discovery_timeout is None
                                  else max(0.1, float(discovery_timeout)))
        self.port_attempts = (PORT_ATTEMPTS if port_attempts is None
                              else max(1, int(port_attempts)))
        self.http_json = http_json or _default_http_json
        self.transport_factory = transport_factory
        self.port_provider = port_provider or free_port
        self.popen = popen or _default_popen
        self.sleep = sleep or time.sleep
        self.logger = logger
        self.clock = clock or time.monotonic
        self.firefox_reader_factory = firefox_reader_factory
        self.kill_process = kill_process
        self.foreground_raiser = foreground_raiser
        self._stop = threading.Event()
        self._proc = None
        self._cdp = None
        self._family = ""
        self.last_state = STATE_EMPTY
        self.last_message = ""

    # ── пути и логи ───────────────────────────────────────────────────
    def profile_dir(self):
        """Отдельный профиль Chromium-браузера (только внутри папки данных)."""
        base = os.path.abspath(self.data_dir or ".")
        return os.path.join(base, PROFILE_FOLDER)

    def firefox_profile_dir(self):
        """Отдельный профиль Firefox (внутри папки данных)."""
        base = os.path.abspath(self.data_dir or ".")
        return os.path.join(base, FIREFOX_PROFILE_FOLDER)

    def _log(self, message):
        text = scrub(message)
        self._diag(text)
        if callable(self.logger):
            try:
                self.logger(text)
                return
            except Exception:
                pass
        try:
            print(text, flush=True)
        except Exception:
            pass

    def _diag(self, message):
        """Пишет диагностику входа в ``DATA_DIR/auth_diag.log`` (без токена)."""
        if not self.diag_path:
            return
        try:
            os.makedirs(os.path.dirname(self.diag_path), exist_ok=True)
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(self.diag_path, "a", encoding="utf-8") as handle:
                handle.write(f"{stamp} {scrub(message)}\n")
        except Exception:
            pass

    def _stage(self, text):
        """Промежуточный статус для окна входа / пилюли."""
        if callable(self.stage_callback):
            try:
                self.stage_callback(str(text))
            except Exception:
                pass

    def _result(self, state, message, **extra):
        self.last_state = state
        self.last_message = scrub(message)
        payload = {"state": state, "ok": state == STATE_OK,
                   "message": self.last_message,
                   "status": STATE_TEXT.get(state, str(state))}
        payload.update(extra)
        self._diag("результат входа: state="
                   f"{state!r} message={self.last_message[:300]!r}")
        return payload

    # ── отмена и очистка ──────────────────────────────────────────────
    def cancel(self):
        """Просит завершить вход и закрывает запущенный браузер."""
        self._stop.set()
        proc, self._proc = self._proc, None
        _terminate_process(proc, self._family, self.kill_process)

    def _remove_profile_dir(self, path):
        """Удаляет папку профиля, только если она внутри папки данных."""
        path = os.path.abspath(path)
        base = os.path.abspath(self.data_dir or ".")
        try:
            if os.path.commonpath([path, base]) != base:
                self._log("вход: профиль вне папки данных — не удаляю")
                return False
        except Exception:
            return False
        if not os.path.isdir(path):
            return True
        try:
            shutil.rmtree(path)
            return True
        except Exception as exc:
            self._log(f"выход: профиль не удалён ({type(exc).__name__})")
            return False

    def clear_profile(self):
        """Удаляет наши профили браузеров (кнопка «Выйти»): Chromium и Firefox."""
        removed_chromium = self._remove_profile_dir(self.profile_dir())
        removed_firefox = self._remove_profile_dir(self.firefox_profile_dir())
        removed = bool(removed_chromium and removed_firefox)
        if removed:
            self._log("выход: профили браузеров удалены")
        return removed

    # ── запуск браузера и отладочный порт ─────────────────────────────
    def _find_browser(self):
        """Путь к браузеру; битый явный путь — :class:`BrowserNotFound`.

        Явно заданный путь (аргумент ``browser_path`` или
        ``config.json → market_browser_path``) — это осознанный выбор
        пользователя: если файла нет, молча подменять его другим браузером
        нельзя, иначе UI показывает один браузер, а запускается другой.
        Автоподбор (первый доступный Chromium → Firefox → прочие) работает
        только когда путь пуст («Авто»).
        """
        explicit = self.browser_path
        if explicit is None:
            explicit = self.cfg.get("market_browser_path") or ""
        explicit = str(explicit or "").strip().strip('"')
        if explicit:
            if os.path.isfile(explicit):
                return explicit
            raise BrowserNotFound(
                f"файл браузера не найден: {explicit} — выберите другой .exe "
                "(config.json → market_browser_path) или «Авто»")
        return find_browser("")

    def _launch(self, browser, port):
        profile = self.profile_dir()
        os.makedirs(profile, exist_ok=True)
        self.last_port = int(port)
        cmd = [
            str(browser),
            f"--remote-debugging-port={int(port)}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-allow-origins=*",
            SITE,
        ]
        self._log(f"вход: запускаю браузер ({os.path.basename(str(browser))}), "
                  f"путь {browser}, порт {int(port)}, профиль {profile}")
        self._stage("браузер запущен — войдите на warframe.market ИМЕННО "
                    "В ЭТОМ ОКНЕ (профиль приложения)")
        return self.popen(cmd)

    def _prepare_firefox_profile(self):
        """Создаёт наш профиль Firefox и тихий user.js (без приветствий)."""
        profile = self.firefox_profile_dir()
        os.makedirs(profile, exist_ok=True)
        prefs = os.path.join(profile, "user.js")
        try:
            with open(prefs, "w", encoding="utf-8") as handle:
                handle.write(FIREFOX_USER_JS)
        except Exception:
            pass
        return profile

    def _launch_firefox(self, browser, profile):
        cmd = [str(browser)] + list(FIREFOX_FLAGS) + [str(profile), SITE]
        self._log("вход: запускаю Firefox с отдельным профилем "
                  f"({profile}) — CDP у Gecko нет, читаем cookies.sqlite")
        self._stage("браузер запущен — войдите на warframe.market ИМЕННО "
                    "В ЭТОМ ОКНЕ (профиль приложения)")
        return self.popen(cmd)

    def _raise_browser_window(self, proc):
        """Поднимает окно запущенного браузера на передний план (в фоне).

        Пока идёт ``_wait_target``/опрос cookie, окно Chrome с новым профилем
        может появиться с задержкой; подъём повторяется в отдельном потоке и
        никогда не блокирует вход. Ошибки не пробрасываются: окна приложения
        уже спрятаны, пользователь при необходимости переключится сам.
        """
        pid = getattr(proc, "pid", None)
        if not pid:
            return False
        raiser = self.foreground_raiser or raise_process_window
        if not callable(raiser):
            return False

        def worker():
            try:
                raiser(int(pid))
            except Exception:
                pass

        try:
            threading.Thread(target=worker, daemon=True,
                             name="wf-browser-raise").start()
        except Exception:
            return False
        return True

    def _wait_target(self, port, proc):
        """Ждёт ``/json/list`` и возвращает target страницы."""
        deadline = self.clock() + self.discovery_timeout
        last_error = None
        while self.clock() < deadline:
            if self._stop.is_set():
                raise BrowserLoginError("вход отменён")
            if proc.poll() is not None:
                raise BrowserClosed(
                    "браузер закрылся до подключения — повторите вход")
            try:
                targets = self.http_json(
                    f"http://127.0.0.1:{int(port)}/json/list", 2.0)
                self._diag("/json/list: целей "
                           f"{len(targets) if isinstance(targets, list) else '?'}")
                for item in (targets or [])[:10]:
                    if isinstance(item, dict):
                        self._diag(
                            "  target: type="
                            f"{item.get('type')!r} title="
                            f"{str(item.get('title') or '')[:80]!r} url="
                            f"{str(item.get('url') or '')[:120]!r}")
            except Exception as exc:
                last_error = exc
                self._diag(f"/json/list недоступен: {type(exc).__name__}: {exc}")
                self.sleep(0.4)
                continue
            target = _pick_page_target(targets)
            if target is not None:
                return target
            self.sleep(0.4)
        raise DiscoveryFailed(
            f"отладочный порт {int(port)} не поднялся"
            + (f" ({type(last_error).__name__})" if last_error else ""))

    def _connect(self, target):
        cdp = CdpClient(str(target.get("webSocketDebuggerUrl") or ""),
                        transport_factory=self.transport_factory,
                        timeout=10.0).connect()
        try:
            cdp.call("Network.enable", timeout=5.0)
        except CdpError as exc:
            self._log(f"вход: Network.enable не принят ({exc}) — продолжаю")
        return cdp

    def _get_cookies(self, cdp):
        response = cdp.call("Network.getAllCookies", timeout=5.0)
        result = response.get("result")
        if not isinstance(result, dict):
            return []
        cookies = result.get("cookies")
        return cookies if isinstance(cookies, list) else []

    # ── основной сценарий ─────────────────────────────────────────────
    def run(self, should_stop=None):
        """Синхронный сценарий входа (вызывать в фоновом потоке).

        Семейство браузера определяет транспорт: Chromium — CDP, Firefox —
        отдельный профиль и ``cookies.sqlite``. Возвращает
        ``{"state": …, "ok": bool, "message": …, "status": …}``; токен в
        сообщениях только маской.
        """
        self._stop.clear()
        self.last_state = STATE_EMPTY
        if should_stop is not None and callable(should_stop):
            # внешний признак отмены (например, окно закрыли)
            original_stop = self._stop

            def watcher():
                while not original_stop.is_set():
                    try:
                        if should_stop():
                            original_stop.set()
                            return
                    except Exception:
                        return
                    self.sleep(0.2)

            threading.Thread(target=watcher, daemon=True,
                             name="wf-browser-login-watch").start()
        try:
            browser = self._find_browser()
        except BrowserNotFound as exc:
            # явный путь битый: не подменяем молча другим браузером
            message = str(exc)
            self._log(message)
            return self._result(STATE_NO_BROWSER,
                                message + ". Можно вставить токен вручную.")
        self._log(f"вход: DATA_DIR={self.data_dir or '—'}, "
                  f"auth_diag={self.diag_path or '—'}, "
                  f"таймаут={int(self.timeout)}с, попыток порта={self.port_attempts}")
        if not browser:
            message = ("браузер не найден: установите Chrome/Edge/Yandex/Opera/"
                       "Opera GX/Firefox или укажите путь в config.json → "
                       "market_browser_path")
            self._log(message)
            self._stage("браузер не найден — используйте ручной путь")
            return self._result(STATE_NO_BROWSER, message
                                + ". Можно вставить токен вручную.")
        family = browser_family(browser, browser_title(browser))
        name = browser_title(browser)
        self._log(f"вход: найден браузер «{name}», путь {browser}, семейство: "
                  f"{FAMILY_TEXT.get(family, family)}")
        if family == FAMILY_FIREFOX:
            return self._run_firefox(browser, name)
        if family != FAMILY_CHROMIUM:
            message = (
                f"не удалось определить тип браузера "
                f"«{os.path.basename(browser)}»: поддерживаются Chromium "
                "(Chrome, Edge, Yandex, Opera, Opera GX, Brave, Vivaldi, "
                "Chromium) и Firefox (Firefox, Waterfox, LibreWolf). Выберите "
                "другой .exe или вставьте токен вручную.")
            self._log(message)
            return self._result(STATE_ERROR, message)
        return self._run_chromium(browser)

    def _run_chromium(self, browser):
        """CDP-вход для Chromium-семейства (Chrome, Edge, Yandex, Opera…)."""
        profile = self.profile_dir()
        try:
            os.makedirs(profile, exist_ok=True)
        except Exception as exc:
            return self._result(
                STATE_ERROR,
                f"не удалось создать папку профиля браузера "
                f"({type(exc).__name__})")
        self._family = FAMILY_CHROMIUM
        last_error = ""
        for attempt in range(self.port_attempts):
            if self._stop.is_set():
                return self._result(STATE_CANCELLED, "вход отменён")
            attempt_started = self.clock()
            # повтор («Я вошёл — проверить снова») переиспользует порт сессии
            port = int(self.last_port or 0) if attempt == 0 else 0
            if port <= 0:
                try:
                    port = int(self.port_provider() or 0)
                except Exception:
                    port = 0
            if port <= 0:
                port = free_port()
            try:
                proc = self._launch(browser, port)
            except Exception as exc:
                return self._result(
                    STATE_ERROR,
                    "не удалось запустить браузер "
                    f"({type(exc).__name__}): проверьте market_browser_path")
            self._proc = proc
            # браузер должен оказаться на переднем плане, а не под оверлеем
            self._raise_browser_window(proc)
            try:
                target = self._wait_target(port, proc)
                return self._login_loop(target, proc)
            except BrowserClosed:
                # браузер мог сразу выйти из-за занятого порта — пробуем
                # следующий порт, если попытки ещё остались
                quick = (self.clock() - attempt_started) < 5.0
                if quick and attempt + 1 < self.port_attempts:
                    last_error = "браузер сразу закрылся (возможно, порт занят)"
                    self._log(f"вход: {last_error} — пробую следующий порт")
                    continue
                return self._result(
                    STATE_BROWSER_CLOSED,
                    "браузер закрыт до завершения входа — нажмите «Войти "
                    "в браузере» ещё раз")
            except DiscoveryFailed as exc:
                last_error = str(exc)
                self._log(f"вход: {last_error} — пробую следующий порт")
                continue
            except BrowserLoginError as exc:
                if self._stop.is_set():
                    return self._result(STATE_CANCELLED, "вход отменён")
                return self._result(STATE_ERROR, str(exc))
            finally:
                self._finish(proc)
        self._stage("не удалось подключиться к браузеру — используйте "
                    "ручной путь")
        return self._result(
            STATE_NO_TARGET,
            "не удалось подключиться к браузеру"
            + (f": {last_error}" if last_error else "")
            + " — закройте лишние окна браузера и повторите вход или "
              "используйте «Войти через свой браузер (вручную)»")

    def _run_firefox(self, browser, name=""):
        """Вход через Firefox: отдельный профиль + cookies.sqlite (CDP нет)."""
        try:
            profile = self._prepare_firefox_profile()
        except Exception as exc:
            return self._result(
                STATE_ERROR,
                f"не удалось создать профиль Firefox ({type(exc).__name__})")
        self._family = FAMILY_FIREFOX
        try:
            proc = self._launch_firefox(browser, profile)
        except Exception as exc:
            return self._result(
                STATE_ERROR,
                "не удалось запустить браузер "
                f"({type(exc).__name__}): проверьте market_browser_path")
        self._proc = proc
        # и окно Firefox — на передний план (не под окнами оверлея)
        self._raise_browser_window(proc)
        try:
            factory = self.firefox_reader_factory
            reader = (factory(profile) if callable(factory)
                      else FirefoxProfileCookies(profile))
            return self._firefox_loop(proc, reader, name)
        except BrowserLoginError as exc:
            if self._stop.is_set():
                return self._result(STATE_CANCELLED, "вход отменён")
            return self._result(STATE_ERROR, str(exc))
        finally:
            self._finish(proc)

    def _token_result(self, token, checked):
        """Проверяет новый JWT; ``None`` — ещё не вошли, ждём дальше."""
        if not token or token in checked:
            return None
        checked.add(token)
        self._log(f"вход: поймана cookie JWT ({mask_token(token)})")
        self._stage("cookie найдена, проверяю")
        result = self._accept(token)
        if result.get("ok"):
            self._stage("токен принят")
            return result
        message = str(result.get("message") or "")
        state = str(result.get("state") or "")
        if (state in ("unauthorized", "expired", "guest")
                or "401" in message or "не принят" in message):
            self._stage("токен отклонён (401)")
        else:
            self._stage(f"токен не принят: {message}")
        self._log(f"вход: токен пока не принят ({result.get('message')}) — "
                  "жду вход на сайте")
        return None

    @staticmethod
    def _cookie_summary(cookies):
        """Имена/домены cookie для диагностики (значения не логируются)."""
        pairs = []
        for cookie in cookies or []:
            if not isinstance(cookie, dict):
                continue
            name = str(cookie.get("name") or "")[:40]
            domain = str(cookie.get("domain") or "")[:80]
            pairs.append(f"{domain}:{name}")
        return pairs[:80]

    def _ok_message(self, token, result, note=""):
        message = (f"токен получен: {mask_token(token)} — "
                   + str(result.get("message") or "вход выполнен"))
        if note:
            message += f" ({note})"
        return message

    def _firefox_loop(self, proc, reader, name=""):
        """Опрос cookies.sqlite нашего профиля Firefox до появления JWT."""
        deadline = self.clock() + self.timeout
        checked = set()
        while True:
            if self._stop.is_set():
                return self._result(STATE_CANCELLED, "вход отменён")
            if proc.poll() is not None:
                return self._result(
                    STATE_BROWSER_CLOSED,
                    "Firefox закрыт до завершения входа — повторите (если "
                    "профиль занят другим окном Firefox, закройте его)")
            if self.clock() >= deadline:
                minutes = max(1, int(round(self.timeout / 60.0)))
                self._stage("cookie не появилась — вход не выполнен")
                return self._result(
                    STATE_TIMEOUT,
                    f"cookie не появилась: не дождались входа за {minutes} мин "
                    "(Firefox при первом запуске готовит профиль дольше) или "
                    "вставьте токен вручную")
            try:
                token = reader.token()
                cookies = []
                cookie_reader = getattr(reader, "cookies", None)
                if callable(cookie_reader):
                    cookies = cookie_reader() or []
                self._log("вход: cookies.sqlite — cookie "
                          f"{len(cookies)}; JWT "
                          f"{'найдена' if token else 'не найдена'}")
                if cookies:
                    self._log("вход: cookie (домен:имя): "
                              + ", ".join(self._cookie_summary(
                                  [{"domain": host, "name": cookie_name}
                                   for cookie_name, _value, host in cookies])))
            except Exception as exc:
                self._log(f"вход: cookies.sqlite не прочитан "
                          f"({type(exc).__name__}: {exc})")
                token = ""
            result = self._token_result(token, checked)
            if result is not None:
                message = self._ok_message(token, result, "Firefox")
                self._log(message)
                return self._result(STATE_OK, message,
                                    masked=mask_token(token),
                                    saved=bool(result.get("saved")),
                                    family=FAMILY_FIREFOX)
            remaining = deadline - self.clock()
            if remaining <= 0:
                continue
            self.sleep(min(self.poll_interval, max(0.05, remaining)))

    def _login_loop(self, target, proc):
        cdp = self._connect(target)
        self._cdp = cdp
        deadline = self.clock() + self.timeout
        checked = set()
        while True:
            if self._stop.is_set():
                return self._result(STATE_CANCELLED, "вход отменён")
            if proc.poll() is not None:
                return self._result(
                    STATE_BROWSER_CLOSED,
                    "браузер закрыт до завершения входа — повторите попытку")
            if self.clock() >= deadline:
                minutes = max(1, int(round(self.timeout / 60.0)))
                self._stage("cookie не появилась — вход не выполнен")
                return self._result(
                    STATE_TIMEOUT,
                    f"cookie не появилась: не дождались входа за {minutes} мин — "
                    "повторите вход (войдите именно в окне с профилем "
                    "приложения) или вставьте токен вручную")
            try:
                tokens_cookies = self._get_cookies(cdp)
            except CdpError as exc:
                # браузер мог закрыться между poll() и запросом
                if proc.poll() is not None:
                    return self._result(
                        STATE_BROWSER_CLOSED,
                        "браузер закрыт до завершения входа")
                self._log(f"вход: cookie не получены ({exc})")
                tokens_cookies = []
            cookies = tokens_cookies
            token = jwt_from_cookies(cookies)
            self._log("вход: cookie получены "
                      f"{len(cookies)}; JWT "
                      f"{'найдена' if token else 'не найдена'}")
            if cookies:
                self._log("вход: cookie (домен:имя): "
                          + ", ".join(self._cookie_summary(cookies)))
            if not token:
                self._stage("жду cookie JWT")
            result = self._token_result(token, checked)
            if result is not None:
                message = self._ok_message(token, result)
                self._log(message)
                return self._result(STATE_OK, message,
                                    masked=mask_token(token),
                                    saved=bool(result.get("saved")),
                                    family=FAMILY_CHROMIUM)
            remaining = deadline - self.clock()
            if remaining <= 0:
                continue
            self.sleep(min(self.poll_interval, max(0.05, remaining)))

    def _accept(self, token):
        if self.auth is None:
            return {"state": STATE_ERROR, "ok": False,
                    "message": "контроллер авторизации недоступен"}
        try:
            result = self.auth.accept_token(token)
            state = str(result.get("state") or "")
            http_code = (200 if result.get("ok") else
                         401 if state in ("unauthorized", "expired", "guest")
                         else "error")
            self._diag("проверка GET /v2/me (Authorization: Bearer): "
                       f"HTTP {http_code} state={state!r} "
                       f"ok={bool(result.get('ok'))} "
                       f"message={str(result.get('message') or '')[:200]!r}")
            return result
        except Exception as exc:
            self._diag(f"проверка GET /v2/me: ошибка "
                       f"{type(exc).__name__}: {exc}")
            return {"state": STATE_ERROR, "ok": False,
                    "message": f"не удалось проверить токен "
                               f"({type(exc).__name__})"}

    def _finish(self, proc):
        """Закрывает CDP (Chromium) и браузер (Firefox — taskkill по PID)."""
        cdp, self._cdp = self._cdp, None
        if cdp is not None:
            try:
                cdp.call("Browser.close", timeout=2.0)
            except Exception:
                pass
            try:
                cdp.close()
            except Exception:
                pass
        _terminate_process(proc, self._family, self.kill_process)
        self._proc = None
        self._family = ""


# ─────────────────────────────────────────────────────────────────────────
#  Проверка выбранного браузера (кнопка «Проверить» в окне входа)
# ─────────────────────────────────────────────────────────────────────────
def _profile_created(profile_dir):
    """Firefox создал файлы в нашем профиле (значит, запустился)."""
    try:
        for name in ("cookies.sqlite", "prefs.js", "places.sqlite"):
            if os.path.exists(os.path.join(profile_dir, name)):
                return True
        return bool(os.path.isdir(profile_dir) and os.listdir(profile_dir))
    except Exception:
        return False


def probe_browser(browser_path, data_dir="", timeout=None, popen=None,
                  http_json=None, sleep=None, kill_process=None,
                  family=None, name="", clock=None):
    """Проверяет, что выбранный браузер запускается (без входа в аккаунт).

    Для Chromium ждём отладочный порт и ``/json/list``; для Firefox — что
    процесс жив и создал файлы нашего отдельного профиля. Браузер после
    проверки закрывается.

    :return: ``{"state": …, "ok": bool, "message": …, "family", "name"}``.
    """
    path = str(browser_path or "").strip().strip('"')
    title = name or browser_title(path)
    fam = family or browser_family(path, title)
    if not path or not os.path.isfile(path):
        return {"state": STATE_ERROR, "ok": False, "family": fam, "name": title,
                "message": (f"файл браузера не найден: {path or 'путь не задан'}"
                            " — выберите другой .exe")}
    if fam not in (FAMILY_CHROMIUM, FAMILY_FIREFOX):
        return {"state": STATE_ERROR, "ok": False, "family": FAMILY_UNKNOWN,
                "name": title,
                "message": ("не удалось определить тип браузера: поддерживаются "
                            "Chromium (Chrome, Edge, Yandex, Opera, Opera GX, "
                            "Brave, Vivaldi) и Firefox (Firefox, Waterfox, "
                            "LibreWolf)")}
    timeout = 12.0 if timeout is None else max(1.0, float(timeout))
    popen = popen or _default_popen
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    http_json = http_json or _default_http_json
    base = os.path.abspath(data_dir or tempfile.gettempdir())
    probe_dir = os.path.join(base, PROBE_FOLDER)
    try:
        os.makedirs(probe_dir, exist_ok=True)
    except Exception as exc:
        return {"state": STATE_ERROR, "ok": False, "family": fam, "name": title,
                "message": f"нет доступа к папке проверки ({type(exc).__name__})"}
    proc = None
    try:
        deadline = clock() + timeout
        if fam == FAMILY_CHROMIUM:
            port = free_port()
            proc = popen([path, f"--remote-debugging-port={int(port)}",
                          f"--user-data-dir={probe_dir}", "--no-first-run",
                          "--no-default-browser-check",
                          "--remote-allow-origins=*", SITE])
            while clock() < deadline:
                if proc.poll() is not None:
                    return {"state": STATE_ERROR, "ok": False,
                            "family": fam, "name": title,
                            "message": "браузер закрылся сразу после запуска "
                                       "— проверьте market_browser_path"}
                try:
                    targets = http_json(
                        f"http://127.0.0.1:{int(port)}/json/list", 1.5)
                except Exception:
                    targets = None
                if _pick_page_target(targets) is not None:
                    return {"state": STATE_OK, "ok": True, "family": fam,
                            "name": title,
                            "message": f"{title}: запускается, CDP отвечает"}
                sleep(0.3)
            return {"state": STATE_ERROR, "ok": False, "family": fam,
                    "name": title,
                    "message": f"{title}: отладочный порт не ответил за "
                               f"{int(timeout)} с"}
        # Firefox: отдельный профиль, CDP нет — проверяем сам запуск
        proc = popen([path] + list(FIREFOX_FLAGS) + [probe_dir, SITE])
        while clock() < deadline:
            if proc.poll() is not None:
                return {"state": STATE_ERROR, "ok": False, "family": fam,
                        "name": title,
                        "message": "Firefox закрылся сразу после запуска — "
                                   "проверьте market_browser_path"}
            if _profile_created(probe_dir):
                return {"state": STATE_OK, "ok": True, "family": fam,
                        "name": title,
                        "message": f"{title}: запускается (отдельный профиль, "
                                   "cookie читаются из cookies.sqlite)"}
            sleep(0.4)
        return {"state": STATE_ERROR, "ok": False, "family": fam, "name": title,
                "message": f"{title}: профиль не создался за {int(timeout)} с"}
    except Exception as exc:
        return {"state": STATE_ERROR, "ok": False, "family": fam, "name": title,
                "message": f"{title}: проверка не удалась "
                           f"({type(exc).__name__})"}
    finally:
        _terminate_process(proc, fam, kill_process)
        shutil.rmtree(probe_dir, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────
#  Окно входа: выбор «Войти в браузере» / «Вставить токен вручную»
# ─────────────────────────────────────────────────────────────────────────
def callback_accepts(func, names):
    """Какие keyword-аргументы принимает колбэк (совместимость со старым видом).

    Окно входа зовёт ``on_login_state(entering, keep=…, restore_dialog=…)``;
    если передан старый однопараметровый колбэк, лишние аргументы не
    передаются. Проверка по сигнатуре (а не по ``TypeError``) — чтобы ошибка
    *внутри* колбэка не приводила к повторному вызову.
    """
    names = tuple(names or ())
    try:
        import inspect
        params = inspect.signature(func).parameters
    except Exception:
        return set()
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return set(names)
    accepted = set()
    for name in names:
        param = params.get(name)
        if param is not None and param.kind in (
                inspect.Parameter.KEYWORD_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD):
            accepted.add(name)
    return accepted


def build_login_dialog():
    """Фабрика класса окна выбора способа входа (Qt импортируется лениво)."""
    from PyQt5.QtCore import Qt, QTimer, pyqtSignal
    from PyQt5.QtGui import QFont
    from PyQt5.QtWidgets import (QComboBox, QDialog, QFileDialog, QHBoxLayout,
                                 QLabel, QPushButton, QVBoxLayout, QWidget)

    try:
        import ui_theme as theme
    except Exception:                 # pragma: no cover - тема всегда рядом
        theme = None

    try:                              # B2: общий список окон приложения
        import login_mode
    except Exception:                 # pragma: no cover - модуль всегда рядом
        login_mode = None

    def qss():
        return theme.dialog_qss() if theme is not None else ""

    def color(name, default="#e6edf6"):
        if theme is None:
            return default
        return theme.PALETTE.get(name, default)

    def font(size, bold=False):
        f = QFont(theme.FONT if theme is not None else "Segoe UI")
        f.setPixelSize(int(size))
        f.setBold(bool(bold))
        return f

    class _LoginPill(QWidget):
        """Маленькая topmost-пилюля «⏳ Вход… [Отменить]» (260×56).

        Это единственное видимое окно приложения во время входа. Пилюля
        **topmost** (``WindowStaysOnTopHint``) намеренно: иначе в
        полноэкранном браузере (Chrome F11) она оказалась бы под ним и
        «Отменить» было бы не нажать. Она компактная и стоит в правом верхнем
        углу текущего монитора (отступ 16 px), поэтому центр страницы логина
        не закрывает. ``Qt.Window`` (а не ``Qt.Tool``) — чтобы у неё была
        кнопка на панели задач (запасной путь к «Отменить»).
        ``WA_ShowWithoutActivating`` — показ пилюли не забирает клавиатурный
        фокус у браузера; ``activateWindow()`` нигде не вызывается.
        """

        WIDTH = 260
        HEIGHT = 56
        MARGIN = 16

        def __init__(self, dialog):
            super().__init__(None, Qt.Window | Qt.FramelessWindowHint |
                             Qt.WindowStaysOnTopHint)
            self.dialog = dialog
            if login_mode is not None:
                try:
                    login_mode.register_dialog(self)   # B2: закроется при скрытии
                except Exception:
                    pass
            self.setWindowTitle("Вход на warframe.market")
            self.setAttribute(Qt.WA_ShowWithoutActivating, True)
            self.setAttribute(Qt.WA_StyledBackground, True)
            self.setFixedSize(self.WIDTH, self.HEIGHT)
            self.setToolTip(
                "Вход на warframe.market: войдите в открытом браузере. "
                "«Отменить» прервёт вход и вернёт окно оверлея.")
            self.setStyleSheet(
                "QWidget { background-color:%s; border:1px solid %s;"
                " border-radius:8px; }"
                % (color("panel", "#121a25"), color("border", "#24344a")))
            row = QHBoxLayout(self)
            row.setContentsMargins(12, 8, 10, 8)
            row.setSpacing(8)
            self.lbl_state = QLabel("⏳ Вход…")
            self.lbl_state.setFont(font(11, True))
            self.lbl_state.setStyleSheet(f"color:{color('text')};")
            self.lbl_state.setToolTip(
                "Вход на warframe.market — войдите в открытом браузере")
            self.btn_cancel = QPushButton("Отменить")
            self.btn_cancel.setFont(font(11, True))
            self.btn_cancel.setStyleSheet(
                theme.button_qss(small=True) if theme is not None else "")
            self.btn_cancel.setToolTip(
                "Прервать вход, закрыть браузер и вернуть окно оверлея")
            self.btn_cancel.setCursor(Qt.PointingHandCursor)
            self.btn_cancel.clicked.connect(self.cancel)
            row.addWidget(self.lbl_state)
            row.addStretch(1)
            row.addWidget(self.btn_cancel)
            self._place()

        def _place(self):
            """Правый верхний угол текущего монитора, отступ 16 px.

            Текущий монитор — тот, где курсор (пользователь только что нажал
            «Войти в браузере»), иначе основной. Пилюля маленькая, поэтому
            форму логина по центру не закрывает.
            """
            try:
                from PyQt5.QtGui import QCursor
                from PyQt5.QtWidgets import QApplication
                screen = None
                try:
                    screen = QApplication.screenAt(QCursor.pos())
                except Exception:
                    screen = None
                if screen is None:
                    screen = QApplication.primaryScreen()
                area = screen.availableGeometry() if screen is not None else None
                if area is None:
                    return
                x = area.right() - self.MARGIN - self.width() + 1
                y = area.top() + self.MARGIN
                self.move(max(area.left(), int(x)), max(area.top(), int(y)))
            except Exception:
                pass

        def cancel(self):
            """Кнопка «Отменить»: прервать вход (главное окно скрыто)."""
            try:
                self.dialog.cancel_login()
            except Exception:
                pass
            return True

    class MarketBrowserLoginDialog(QDialog):
        """Вход на warframe.market: браузер пользователя (CDP) или вручную.

        Пароль вводится только на сайте в настоящем браузере; приложение его
        не видит и не хранит. Токен показывается лишь маской.
        """

        auth_changed = pyqtSignal()          # токен изменился (без значения)
        progress = pyqtSignal(str, str)      # (state, message) из фонового потока
        probe_done = pyqtSignal(bool, str)   # результат кнопки «Проверить»
        stage = pyqtSignal(str)              # промежуточный статус входа
        token_from_browser_done = pyqtSignal(dict)
        browser_profiles_ready = pyqtSignal(list)

        def __init__(self, controller, parent=None, cfg=None, data_dir="",
                     manual_factory=None, session_factory=None,
                     browser_finder=None, logger=None, browser_lister=None,
                     save_values=None, probe_factory=None, choose_file=None,
                     pill_factory=None, on_login_state=None, manual_open=None,
                     browser_token_finder=None, choose_folder=None):
            super().__init__(parent)
            if login_mode is not None:
                try:
                    login_mode.register_dialog(self)   # B2: закроется при скрытии
                except Exception:
                    pass
            self.controller = controller
            self.cfg = cfg if isinstance(cfg, dict) else {}
            self.data_dir = str(data_dir or "")
            self.manual_factory = manual_factory
            self.session_factory = session_factory
            self.browser_finder = browser_finder   # старый способ (один путь)
            self.browser_lister = browser_lister
            self.save_values = save_values or (lambda mapping: True)
            self.probe_factory = probe_factory
            self.choose_file = choose_file
            self.logger = logger
            #: (dialog) -> QWidget — своя пилюля «Отменить» (иначе _LoginPill)
            self.pill_factory = pill_factory
            #: (entering: bool, keep=(), restore_dialog=False) -> None —
            #: спрятать/вернуть окна оверлея (keep — что нельзя прятать)
            self.on_login_state = on_login_state
            #: (url) -> bool — открыть warframe.market в обычном браузере
            self.manual_open = manual_open
            #: () -> BrowserTokenFinder — чтение JWT из профилей браузеров
            self.browser_token_finder = browser_token_finder
            self.choose_folder = choose_folder
            self._manual_profile_path = ""
            self._token_finder = None
            self._token_thread = None
            self._profiles_thread = None
            self._login_state_kwargs = callback_accepts(
                on_login_state, ("keep", "restore_dialog"))
            self._session = None
            self._thread = None
            self._probe_thread = None
            self._browsers = []
            self._closed = False
            self._busy = False
            self._login_mode = False
            self._pill = None
            self._last_stage = ""
            #: была ли модальность до входа (BUG-1: снимаем её на время входа)
            self._modal_before_hide = False

            self.progress.connect(self._on_progress)
            self.probe_done.connect(self._on_probe_done)
            self.stage.connect(self._on_stage)
            self.token_from_browser_done.connect(self._on_token_from_browser)
            self.browser_profiles_ready.connect(self._on_browser_profiles)
            self.setWindowTitle("Вход на warframe.market")
            self.setModal(True)
            self.setMinimumWidth(780)
            self.setStyleSheet(qss())

            layout = QVBoxLayout(self)
            layout.setContentsMargins(16, 14, 16, 14)
            layout.setSpacing(8)

            title = QLabel("<b>Вход на warframe.market</b>")
            title.setFont(font(14, True))
            title.setStyleSheet(f"color:{color('text')};")
            self.lbl_info = QLabel(
                "Самый быстрый путь — «Взять токен из моего браузера»: "
                "приложение прочитает cookie JWT из уже авторизованного "
                "профиля Chrome, Edge, Yandex, Opera GX, Firefox… "
                "Ничего вводить не нужно.\n\n"
                "Либо «Войти в браузере»: откроется отдельное окно браузера "
                "(профиль приложения). Войдите на warframe.market ИМЕННО В ЭТОМ "
                "ОКНЕ — cookie обычного браузера в этот режим не попадает.\n\n"
                "1. Выберите браузер ниже (Chrome, Edge, Yandex, Opera, Opera GX, "
                "Brave, Vivaldi, Firefox…)\n"
                "   и нажмите «Войти в браузере» — откроется его отдельное окно "
                "с профилем приложения.\n"
                "2. Войдите на warframe.market — пароль вводится только на "
                "сайте.\n"
                "3. Приложение само заберёт cookie JWT, проверит его через "
                "GET /v2/me и сохранит токен.\n\n"
                "Chromium-браузеры (Chrome, Edge, Yandex, Opera…) отдают токен "
                "по CDP.\n"
                "Firefox-браузеры (Firefox, Waterfox, LibreWolf) работают через "
                "отдельный профиль: приложение\n"
                "читает cookies.sqlite из своей папки — личный профиль не "
                "используется.\n"
                "Ручной путь доступен сразу: «Войти через свой браузер "
                "(вручную)» → DevTools → Application → Cookies → JWT.\n"
                "Пароль оверлей не спрашивает и не хранит; токен — только "
                "маской.")
            self.lbl_info.setWordWrap(True)
            self.lbl_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.lbl_info.setStyleSheet(
                theme.muted_qss(11) if theme is not None else "")

            # ── выбор браузера + проверка запуска ─────────────────────
            browser_row = QHBoxLayout()
            browser_row.setSpacing(8)
            lbl_browser = QLabel("Браузер:")
            lbl_browser.setFont(font(11))
            self.combo_browser = QComboBox()
            self.combo_browser.setFont(font(11))
            self.combo_browser.setStyleSheet(
                theme.input_qss() if theme is not None else "")
            self.combo_browser.setMinimumWidth(460)
            self.combo_browser.currentIndexChanged.connect(
                self._on_browser_selected)
            self.btn_check_browser = QPushButton("Проверить")
            self.btn_check_browser.setFont(font(11))
            self.btn_check_browser.setToolTip(
                "Запустить выбранный браузер с отдельным профилем и проверить, "
                "что он отвечает (потом закроется)")
            self.btn_check_browser.clicked.connect(self._check_browser)
            browser_row.addWidget(lbl_browser)
            browser_row.addWidget(self.combo_browser, 1)
            browser_row.addWidget(self.btn_check_browser)

            self.lbl_token = QLabel("")
            self.lbl_token.setWordWrap(True)
            self.lbl_status = QLabel("")
            self.lbl_status.setWordWrap(True)

            self.btn_from_browser = QPushButton(
                "Взять токен из моего браузера")
            self.btn_from_browser.setFont(font(12, True))
            self.btn_from_browser.setMinimumHeight(30)
            self.btn_from_browser.setStyleSheet(
                theme.button_qss(primary=True) if theme is not None else "")
            self.btn_from_browser.setToolTip(
                "Прочитать cookie JWT из уже авторизованного профиля Chrome, "
                "Edge, Yandex, Opera GX, Firefox… (личный профиль, без "
                "повторного входа)")
            self.btn_from_browser.clicked.connect(self.take_token_from_browser)
            self.btn_token_retry = QPushButton("Попробовать снова")
            self.btn_token_retry.setVisible(False)
            self.btn_token_retry.clicked.connect(self.take_token_from_browser)
            token_row = QHBoxLayout()
            token_row.addWidget(self.btn_from_browser)
            token_row.addWidget(self.btn_token_retry)
            token_row.addStretch(1)
            self.cmb_browser_token = QComboBox()
            self.cmb_browser_token.setFont(font(11))
            self.cmb_browser_token.addItem("Авто (первый, где есть JWT)", "")
            self.cmb_browser_token.setMinimumWidth(420)
            self.btn_profile_refresh = QPushButton("Найти заново")
            self.btn_profile_refresh.clicked.connect(
                self.refresh_browser_profiles)
            self.btn_pick_profile = QPushButton("Указать папку профиля…")
            self.btn_pick_profile.setToolTip(
                "Выбрать папку профиля браузера вручную (Cookies, "
                "Network/Cookies или cookies.sqlite)")
            self.btn_pick_profile.clicked.connect(self.pick_profile_folder)
            self.btn_howto = QPushButton("Как войти?")
            self.btn_howto.setToolTip(
                "Инструкция: где взять JWT в Chrome/Edge/Yandex/Opera GX "
                "и Firefox")
            self.btn_howto.clicked.connect(self.open_manual)
            token_choose_row = QHBoxLayout()
            token_choose_row.addWidget(QLabel("Взять токен из браузера:"))
            token_choose_row.addWidget(self.cmb_browser_token, 1)
            token_choose_row.addWidget(self.btn_profile_refresh)
            token_choose_row.addWidget(self.btn_pick_profile)
            token_choose_row.addWidget(self.btn_howto)
            buttons = QHBoxLayout()
            self.btn_browser = QPushButton("Войти в браузере")
            self.btn_browser.setFont(font(11, True))
            self.btn_browser.clicked.connect(self.start_browser_login)
            self.btn_retry = QPushButton("Я вошёл — проверить снова")
            self.btn_retry.setToolTip(
                "Повторно забрать cookie JWT из уже открытого окна браузера "
                "и проверить токен без перезапуска приложения")
            self.btn_retry.clicked.connect(self.retry_login)
            self.btn_manual = QPushButton(
                "Войти через свой браузер (вручную)")
            self.btn_manual.setToolTip(
                "Открыть warframe.market в вашем обычном браузере; JWT — "
                "DevTools → Application → Cookies → JWT, затем вставить в поле")
            self.btn_manual.clicked.connect(self.open_manual)
            self.btn_logout = QPushButton("Выйти")
            self.btn_logout.setToolTip(
                "Удалить токен из config.json и профиль браузера приложения")
            self.btn_logout.clicked.connect(self.logout)
            self.btn_close = QPushButton("Закрыть")
            self.btn_close.clicked.connect(self._finish)
            buttons.addWidget(self.btn_browser)
            buttons.addWidget(self.btn_retry)
            buttons.addWidget(self.btn_manual)
            buttons.addWidget(self.btn_logout)
            buttons.addStretch(1)
            buttons.addWidget(self.btn_close)

            layout.addWidget(title)
            layout.addWidget(self.lbl_info)
            layout.addLayout(browser_row)
            layout.addWidget(self.lbl_token)
            layout.addWidget(self.lbl_status)
            layout.addLayout(token_row)
            layout.addLayout(token_choose_row)
            layout.addStretch(1)
            layout.addLayout(buttons)

            self._refresh_token_label()
            self._refresh_browser_list()
            QTimer.singleShot(0, self.refresh_browser_profiles)

        # ── список браузеров и выбор ──────────────────────────────────
        def _discover(self):
            """Все найденные браузеры (реестр + пути + выбор из конфига)."""
            explicit = str(self.cfg.get("market_browser_path") or "")
            items = []
            try:
                if callable(self.browser_lister):
                    items = self.browser_lister(explicit)
                elif callable(self.browser_finder):
                    found = self.browser_finder(explicit)
                    items = [found] if isinstance(found, str) else found
                else:
                    items = discover_browsers(explicit)
            except Exception:
                items = []
            return self._normalize_browsers(items)

        def _normalize_browsers(self, items):
            """Приводит элементы к ``{"path", "name", "family"}`` и дедуплицирует."""
            result = []
            seen = set()
            for item in items or []:
                if isinstance(item, dict):
                    path = str(item.get("path") or "").strip()
                    name = str(item.get("name") or "")
                    family = str(item.get("family") or "")
                else:
                    path = str(item or "").strip()
                    name = ""
                    family = ""
                if not path:
                    continue
                key = os.path.normcase(os.path.abspath(path))
                if key in seen:
                    continue
                seen.add(key)
                name = name or browser_title(path)
                family = family or browser_family(path, name)
                result.append({"path": path, "name": name, "family": family})
            return result

        def _selected_path(self):
            """Путь из комбобокса (``""`` = авто; ручной выбор — тоже путь)."""
            if not hasattr(self, "combo_browser"):
                return str(self.cfg.get("market_browser_path") or "")
            data = self.combo_browser.currentData()
            if data is None or data == "__custom__":
                return str(self.cfg.get("market_browser_path") or "")
            return str(data or "")

        def selected_browser(self):
            """Итоговый путь браузера для входа (учитывая «Авто»)."""
            path = self._selected_path()
            if path:
                return path
            if self._browsers:
                return str(self._browsers[0]["path"] or "")
            return ""

        def _refresh_browser_list(self, announce=True):
            """Заполняет список браузеров и восстанавливает сохранённый выбор."""
            if not hasattr(self, "combo_browser"):
                return
            self._browsers = self._discover()
            explicit = str(self.cfg.get("market_browser_path") or "").strip()
            self.combo_browser.blockSignals(True)
            self.combo_browser.clear()
            self.combo_browser.addItem("Авто (первый найденный Chromium)", "")
            for item in self._browsers:
                label = f"{item['name']} — {os.path.dirname(item['path'])}"
                if item["family"] == FAMILY_FIREFOX:
                    label += "  ·  Firefox-профиль"
                self.combo_browser.addItem(label, item["path"])
            self.combo_browser.addItem("Другой… (выбрать .exe)", "__custom__")
            index = 0
            if explicit:
                found = self.combo_browser.findData(explicit)
                if found >= 0:
                    index = found
                else:
                    suffix = ("" if os.path.isfile(explicit)
                              else " (файл не найден)")
                    self.combo_browser.insertItem(
                        1, f"Выбранный вручную: {explicit}{suffix}", explicit)
                    index = 1
            self.combo_browser.setCurrentIndex(index)
            self.combo_browser.blockSignals(False)
            self._update_browser_state(announce)

        def _update_browser_state(self, announce=True):
            """Включает/выключает вход и пишет статус по выбранному браузеру."""
            path = self.selected_browser()
            explicit = str(self.cfg.get("market_browser_path") or "").strip()
            if explicit:
                path = explicit
                # Явно выбранный пользователем путь: если файла нет, нельзя
                # молча запускать другой браузер — показываем ошибку.
                if not os.path.isfile(explicit):
                    self.btn_browser.setEnabled(False)
                    self.btn_browser.setToolTip(
                        "Файл выбранного браузера не найден — выберите другой "
                        ".exe или «Авто»")
                    if announce:
                        self._set_status(
                            f"файл браузера не найден: {explicit} — выберите "
                            "другой .exe или «Авто» "
                            "(config.json → market_browser_path)", warn=True)
                    return
            if path:
                name = browser_title(path)
                family = browser_family(path, name)
                if family == FAMILY_UNKNOWN:
                    self.btn_browser.setEnabled(False)
                    self.btn_browser.setToolTip(
                        "Не удалось определить тип браузера — выберите другой "
                        ".exe или вставьте токен вручную")
                    if announce:
                        self._set_status(
                            f"«{os.path.basename(path)}»: не удалось определить "
                            "тип браузера — выберите Chrome/Edge/Yandex/Opera/"
                            "Opera GX/Firefox или вставьте токен вручную",
                            warn=True)
                else:
                    self.btn_browser.setEnabled(not self._busy)
                    self.btn_browser.setToolTip(
                        f"Откроется: {name} ({FAMILY_TEXT.get(family, family)})")
                    if announce:
                        prefix = "готово: " if explicit else "готово: авто — "
                        self._set_status(
                            f"{prefix}{name} "
                            f"({FAMILY_TEXT.get(family, family)}) — нажмите "
                            "«Войти в браузере»")
            elif self._browsers:
                first = self._browsers[0]
                self.btn_browser.setEnabled(not self._busy)
                self.btn_browser.setToolTip(
                    f"Авто: будет запущен {first['name']}")
                if announce:
                    self._set_status(
                        f"готово: авто — {first['name']} "
                        f"({FAMILY_TEXT.get(first['family'], first['family'])})")
            else:
                self.btn_browser.setEnabled(False)
                self.btn_browser.setToolTip(
                    "Браузер не найден — укажите путь в config.json → "
                    "market_browser_path")
                if announce:
                    self._set_status(
                        "браузер не найден (Chrome, Edge, Yandex, Opera, "
                        "Opera GX, Brave, Vivaldi, Firefox): установите его, "
                        "выберите «Другой…», укажите путь в config.json → "
                        "market_browser_path или вставьте токен вручную.",
                        warn=True)

        def _on_browser_selected(self, index):
            data = self.combo_browser.itemData(index)
            if data == "__custom__":
                self._choose_custom_browser()
                return
            self._save_browser_choice(str(data or ""))
            self._update_browser_state(announce=True)

        def _save_browser_choice(self, path):
            self.cfg["market_browser_path"] = str(path or "")
            try:
                self.save_values({"market_browser_path": str(path or "")})
            except Exception:
                pass

        def _default_choose_file(self, parent, start):
            path, _selected = QFileDialog.getOpenFileName(
                parent, "Выберите браузер", start,
                "Исполняемые файлы (*.exe);;Все файлы (*)")
            return path or ""

        def _choose_custom_browser(self):
            """Ручной выбор любого .exe (Chrome-подобный или Firefox-подобный)."""
            start = os.path.dirname(self._selected_path() or "")
            if not start:
                start = os.path.expanduser("~")
            try:
                chooser = self.choose_file or self._default_choose_file
                path = str(chooser(self, start) or "").strip()
            except Exception as exc:
                self._set_status(
                    f"не удалось выбрать файл ({type(exc).__name__})",
                    warn=True)
                return ""
            if not path:
                # отмена — вернуть прежний выбор
                self._refresh_browser_list(announce=False)
                return ""
            self._save_browser_choice(path)
            self._refresh_browser_list(announce=False)
            family = browser_family(path, browser_title(path))
            name = browser_title(path)
            if family == FAMILY_UNKNOWN:
                self._set_status(
                    f"«{os.path.basename(path)}»: не удалось определить тип — "
                    "выберите Chrome/Edge/Yandex/Opera/Opera GX/Firefox или "
                    "вставьте токен вручную", warn=True)
            else:
                self._set_status(
                    f"выбран браузер: {name} "
                    f"({FAMILY_TEXT.get(family, family)})")
            return path

        # ── проверка запуска браузера (кнопка «Проверить») ────────────
        def _check_browser(self):
            if self._probe_thread is not None:
                return False
            path = self.selected_browser()
            if not path:
                self._set_status("сначала выберите браузер", warn=True)
                return False
            self._set_status(f"проверяю браузер «{browser_title(path)}»…")
            self._set_busy(True)

            def worker():
                try:
                    factory = self.probe_factory
                    if callable(factory):
                        result = factory(path)
                    else:
                        result = probe_browser(path, self.data_dir)
                except Exception as exc:
                    result = {"ok": False,
                              "message": f"проверка не удалась "
                                         f"({type(exc).__name__})"}
                try:
                    self.probe_done.emit(bool(result.get("ok")),
                                         str(result.get("message") or ""))
                except RuntimeError:
                    pass

            self._probe_thread = threading.Thread(
                target=worker, daemon=True, name="wf-browser-probe")
            self._probe_thread.start()
            return True

        def _on_probe_done(self, ok, message):
            self._probe_thread = None
            if self._closed:
                return
            self._set_busy(False)
            self._set_status(message or ("браузер запускается" if ok else
                                         "браузер не запускается"),
                             ok=ok, warn=not ok)

        def _refresh_token_label(self):
            text = "сейчас: "
            try:
                text += self.controller.status_text()
            except Exception:
                text += "нет данных"
            self.lbl_token.setText(text)
            self.lbl_token.setStyleSheet(
                theme.status_qss() if theme is not None else "")

        def _set_status(self, text, ok=False, warn=False):
            self.lbl_status.setText(scrub(text))
            if theme is None:
                return
            if ok:
                self.lbl_status.setStyleSheet(
                    f"color:{color('success')}; font-size:11px;")
            elif warn:
                self.lbl_status.setStyleSheet(
                    f"color:{color('warning')}; font-size:11px;")
            else:
                self.lbl_status.setStyleSheet(theme.status_qss())

        def _set_busy(self, busy):
            self._busy = bool(busy)
            for button in (self.btn_browser, self.btn_retry, self.btn_manual,
                           self.btn_logout, self.btn_check_browser,
                           self.btn_from_browser, self.btn_token_retry,
                           self.btn_profile_refresh, self.btn_pick_profile,
                           self.btn_howto):
                button.setEnabled(not self._busy)
            if hasattr(self, "cmb_browser_token"):
                self.cmb_browser_token.setEnabled(not self._busy)
            if hasattr(self, "combo_browser"):
                self.combo_browser.setEnabled(not self._busy)
            if not self._busy:
                self._update_browser_state(announce=False)

        def _on_stage(self, text):
            """Промежуточный статус из фонового потока (пилюля/строка)."""
            text = scrub(text)
            try:
                self._last_stage = text
            except Exception:
                pass
            if self._pill is not None:
                try:
                    label = getattr(self._pill, "lbl_state", None)
                    if label is not None:
                        label.setText(text[:80])
                        label.setToolTip(text)
                except Exception:
                    pass
            if not self._closed:
                self._set_status(text)

        def _stage_from_thread(self, text):
            try:
                self.stage.emit(str(text))
            except RuntimeError:
                pass

        # ── режим входа: окна приложения не перекрывают браузер ──────
        def _notify_login_state(self, entering, restore_dialog=False):
            """Сообщает оверлею «вход начался/закончился» (если колбэк есть)."""
            if not callable(self.on_login_state):
                return
            kwargs = {}
            if "keep" in self._login_state_kwargs:
                kwargs["keep"] = (tuple(w for w in (self, self._pill)
                                        if w is not None)
                                  if entering else ())
            if "restore_dialog" in self._login_state_kwargs:
                kwargs["restore_dialog"] = bool(restore_dialog) and not entering
            try:
                self.on_login_state(bool(entering), **kwargs)
            except Exception:
                pass

        def _enter_login_mode(self):
            """Спрятать диалог, показать пилюлю «Отменить», спрятать оверлей.

            Порядок важен: сначала прячем модальное окно (оно перекрывало бы
            браузер) и снимаем модальность, чтобы пилюля получала клики, затем
            показываем пилюлю и только потом просим оверлей спрятать главное
            окно и все прочие окна приложения. Вызывается **до** запуска
            браузера, чтобы ни одно окно не успело оказаться поверх него.
            """
            if self._login_mode:
                return False
            self._login_mode = True
            try:
                self._pill = (self.pill_factory(self)
                              if callable(self.pill_factory) else _LoginPill(self))
            except Exception:
                self._pill = None
            # Пока окно входа спрятано, приложение не должно быть модально
            # заблокировано: иначе пилюля «Отменить» не получит клики, а после
            # входа главное окно могло остаться выключенным (BUG-1).
            # ВАЖЕН ПОРЯДОК: сначала hide() (Qt снимает окно с modal-стека),
            # и только потом снимаем флаги модальности. Если снять модальность
            # у видимого окна ДО hide(), Qt оставляет «мёртвую» запись в
            # modal-стеке — окно приложения остаётся некликабельным.
            self._modal_before_hide = bool(self.isModal())
            try:
                self.hide()
            except Exception:
                pass
            try:
                self.setModal(False)
                self.setWindowModality(Qt.NonModal)
            except Exception:
                pass
            if self._pill is not None:
                try:
                    self._pill.show()
                except Exception:
                    self._pill = None
            self._notify_login_state(True)
            return True

        def _exit_login_mode(self, restore_dialog=True):
            """Вернуть окна после входа (успех/ошибка/отмена/таймаут).

            :param restore_dialog: показать диалог снова (ошибки/таймаут/закрытие
                браузера). Для успеха и отмены диалог закрывается штатно.
            """
            if not self._login_mode:
                return False
            self._login_mode = False
            pill, self._pill = self._pill, None
            if pill is not None:
                try:
                    pill.hide()
                    pill.close()
                    pill.deleteLater()
                except Exception:
                    pass
            self._notify_login_state(False, restore_dialog=restore_dialog)
            if restore_dialog and not self._closed:
                try:
                    # диалог снова модален: пользователь работает с ним
                    self.setModal(True)
                    self.setWindowModality(Qt.ApplicationModal)
                except Exception:
                    pass
                try:
                    self.show()
                    self.raise_()
                except Exception:
                    pass
            else:
                # успех/отмена: окно входа закрывается или уже скрыто —
                # модальность не должна «залипнуть» и блокировать оверлей.
                # Порядок как в _enter_login_mode: сначала hide(), потом флаги.
                try:
                    self.hide()
                except Exception:
                    pass
                try:
                    self.setModal(False)
                    self.setWindowModality(Qt.NonModal)
                except Exception:
                    pass
            return True

        def cancel_login(self):
            """«Отменить» в пилюле: прервать вход и вернуть окна.

            Работает и когда главное окно спрятано: пилюля — отдельное окно
            приложения. Сеанс отменяется, браузер закрывается, оверлей
            возвращается.
            """
            self._set_status("вход отменён", warn=True)
            try:
                self._refresh_token_label()
            except Exception:
                pass
            return self._finish()

        # ── вход через браузер (сеть/браузер — в фоне) ────────────────
        def _make_session(self):
            path = self.selected_browser()
            if callable(self.session_factory):
                try:
                    session = self.session_factory(path)
                except TypeError:
                    session = self.session_factory()
                if session is not None:
                    try:
                        session.stage_callback = self._stage_from_thread
                    except Exception:
                        pass
                return session
            return BrowserLoginSession(
                self.controller, cfg=self.cfg, data_dir=self.data_dir,
                browser_path=path, logger=self.logger,
                stage_callback=self._stage_from_thread)

        def start_browser_login(self):
            if self._thread is not None:
                return False
            path = self.selected_browser()
            explicit = str(self.cfg.get("market_browser_path") or "").strip()
            if explicit and not os.path.isfile(explicit):
                # битый путь из config.json: не подменяем молча другим браузером
                self._set_status(
                    f"файл браузера не найден: {explicit} — выберите другой "
                    ".exe или «Авто» (config.json → market_browser_path)",
                    warn=True)
                self._update_browser_state(announce=False)
                return False
            if path:
                family = browser_family(path, browser_title(path))
                if family == FAMILY_UNKNOWN:
                    self._set_status(
                        f"«{os.path.basename(path)}»: не удалось определить тип "
                        "браузера — выберите Chrome/Edge/Yandex/Opera/Opera GX/"
                        "Firefox или вставьте токен вручную", warn=True)
                    return False
            elif not self._browsers:
                self._set_status(
                    "браузер не найден: установите Chrome/Edge/Yandex/Opera/"
                    "Opera GX/Firefox, выберите «Другой…» или вставьте токен "
                    "вручную", warn=True)
                return False
            try:
                session = self._make_session()
            except Exception as exc:
                self._set_status(
                    f"не удалось подготовить вход ({type(exc).__name__})",
                    warn=True)
                return False
            return self._start_session(session)

        def retry_login(self):
            """«Я вошёл — проверить снова»: повторный захват без рестарта."""
            if self._thread is not None:
                return False
            session = self._session
            if session is None:
                return self.start_browser_login()
            return self._start_session(session)

        def _start_session(self, session):
            self._session = session
            self._set_busy(True)
            path = self.selected_browser()
            name = browser_title(path) if path else "браузер"
            self._set_status(f"ожидаю вход… открываю {name}; войдите на "
                             "warframe.market")
            # Прячем окна ДО запуска браузера: ни одно окно приложения не
            # должно перекрывать его и забирать мышь.
            self._enter_login_mode()
            self._thread = threading.Thread(
                target=self._worker, args=(session,), daemon=True,
                name="wf-browser-login")
            try:
                self._thread.start()
            except Exception as exc:
                self._thread = None
                self._session = None
                self._exit_login_mode(restore_dialog=True)
                self._set_status(
                    f"не удалось запустить вход ({type(exc).__name__})",
                    warn=True)
                return False
            return True

        def _worker(self, session):
            try:
                result = session.run()
            except Exception as exc:
                result = {"state": STATE_ERROR,
                          "message": f"вход не удался ({type(exc).__name__})"}
            try:
                self.progress.emit(str(result.get("state") or STATE_ERROR),
                                   str(result.get("message") or ""))
            except RuntimeError:      # окно уже закрыто
                pass

        def _on_progress(self, state, message):
            # Вход закончился (успех/ошибка/отмена/таймаут/браузер закрыт) —
            # сначала возвращаем окна, потом показываем статус.
            self._exit_login_mode(
                restore_dialog=(state not in (STATE_OK, STATE_CANCELLED)))
            self._thread = None
            self._busy = False
            if self._closed:
                return
            for button in (self.btn_browser, self.btn_retry, self.btn_manual,
                           self.btn_logout, self.btn_from_browser,
                           self.btn_token_retry, self.btn_profile_refresh,
                           self.btn_pick_profile, self.btn_howto):
                button.setEnabled(True)
            if hasattr(self, "cmb_browser_token"):
                self.cmb_browser_token.setEnabled(True)
            if state == STATE_OK:
                self._set_status(message, ok=True)
                self._refresh_token_label()
                try:
                    self.auth_changed.emit()
                except RuntimeError:
                    pass
                QTimer.singleShot(1200, self._finish)
                return
            if state == STATE_CANCELLED:
                self._set_status("вход отменён", warn=True)
                self._update_browser_state(announce=False)
                return
            self._set_status("вход не выполнен: "
                             + (message or STATE_TEXT.get(state, "не удалось")),
                             warn=True)
            self._refresh_token_label()
            self._update_browser_state(announce=False)

        # ── токен из личного профиля браузера (t30) ───────────────────
        def _make_token_finder(self):
            if self._token_finder is not None:
                return self._token_finder
            factory = self.browser_token_finder
            if factory is None:
                import browser_token
                self._token_finder = browser_token.BrowserTokenFinder(
                    logger=self.logger)
                return self._token_finder
            if callable(factory) and not hasattr(factory, "find_token"):
                self._token_finder = factory()
            else:
                self._token_finder = factory
            return self._token_finder

        def refresh_browser_profiles(self):
            """«Найти заново»: перечитать список профилей (кэш сбрасывается)."""
            try:
                finder = self._make_token_finder()
            except Exception:
                finder = None
            if finder is None:
                return False
            if hasattr(finder, "refresh"):
                try:
                    finder.refresh()
                except Exception:
                    pass
            self.btn_profile_refresh.setEnabled(False)
            def worker():
                try:
                    items = (finder.scan_profiles()
                             if callable(getattr(finder, "scan_profiles", None))
                             else [])
                except Exception:
                    items = []
                try:
                    self.browser_profiles_ready.emit(list(items or []))
                except RuntimeError:
                    pass

            if self._profiles_thread is not None and \
                    getattr(self._profiles_thread, "is_alive", lambda: False)():
                return False
            self._profiles_thread = threading.Thread(
                target=worker, daemon=True, name="wf-browser-profiles")
            self._profiles_thread.start()
            return True

        def _on_browser_profiles(self, items):
            self.btn_profile_refresh.setEnabled(True)
            if self._closed or not hasattr(self, "cmb_browser_token"):
                return
            selected = self.cmb_browser_token.currentData()
            self.cmb_browser_token.blockSignals(True)
            self.cmb_browser_token.clear()
            self.cmb_browser_token.addItem("Авто (первый, где есть JWT)", "")
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                browser = str(item.get("browser") or "Браузер")
                profile = str(item.get("profile") or "Default")
                has = "JWT есть" if item.get("has_jwt") else "JWT нет"
                label = f"{browser} — {profile} — {has}"
                self.cmb_browser_token.addItem(label,
                                               item.get("db_path") or profile)
            if selected:
                index = self.cmb_browser_token.findData(selected)
                if index >= 0:
                    self.cmb_browser_token.setCurrentIndex(index)
            self.cmb_browser_token.blockSignals(False)

        def pick_profile_folder(self):
            """«Указать папку…»: ручной выбор профиля браузера."""
            folder = ""
            try:
                if callable(self.choose_folder):
                    folder = str(self.choose_folder() or "")
                else:
                    from PyQt5.QtWidgets import QFileDialog
                    folder = QFileDialog.getExistingDirectory(
                        self, "Папка профиля браузера")
            except Exception:
                folder = ""
            folder = folder.strip()
            if not folder:
                return False
            self._manual_profile_path = folder
            try:
                finder = self._make_token_finder()
                if finder is not None:
                    paths = getattr(finder, "extra_paths", None)
                    if isinstance(paths, list):
                        paths.append(folder)
                    if callable(getattr(finder, "refresh", None)):
                        finder.refresh()
            except Exception:
                pass
            self._set_status(f"проверяю выбранную папку: {folder}")
            self.refresh_browser_profiles()
            return self.take_token_from_browser(only=folder)

        def take_token_from_browser(self, only=None):
            """«Взять токен из моего браузера»: чтение cookie JWT из профиля."""
            if self._token_thread is not None:
                return False
            try:
                finder = self._make_token_finder()
            except Exception as exc:
                self._set_status(
                    f"чтение cookie браузера недоступно "
                    f"({type(exc).__name__}) — войдите вручную", warn=True)
                self.btn_token_retry.setVisible(True)
                return False
            if finder is None or not callable(getattr(finder, "find_token", None)):
                self._set_status("чтение cookie браузера недоступно — "
                                 "войдите вручную", warn=True)
                self.btn_token_retry.setVisible(True)
                return False
            if only is None:
                try:
                    only = self.cmb_browser_token.currentData() or None
                except Exception:
                    only = None
            self._set_busy(True)
            self.btn_token_retry.setVisible(False)
            self._set_status("ищу cookie JWT в профилях браузеров…")
            try:
                self._token_thread = threading.Thread(
                    target=self._token_worker, args=(finder, only), daemon=True,
                    name="wf-browser-token")
                self._token_thread.start()
            except Exception as exc:
                self._token_thread = None
                self._set_busy(False)
                self._set_status(f"не удалось запустить чтение "
                                 f"({type(exc).__name__})", warn=True)
                self.btn_token_retry.setVisible(True)
                return False
            return True

        def _token_worker(self, finder, only=None):
            try:
                try:
                    result = (finder.find_token(only=only) if only
                              else finder.find_token())
                except TypeError:
                    result = finder.find_token()
            except Exception as exc:
                result = {"ok": False, "status": "error", "token": "",
                          "checked": [],
                          "message": f"ошибка чтения "
                                     f"({type(exc).__name__})"}
            try:
                self.token_from_browser_done.emit(dict(result or {}))
            except RuntimeError:
                pass

        def _on_token_from_browser(self, result):
            self._token_thread = None
            self._set_busy(False)
            if self._closed:
                return
            result = dict(result or {})
            token = str(result.get("token") or "")
            browser = str(result.get("browser") or "")
            if result.get("ok") and token:
                try:
                    accepted = self.controller.accept_token(token)
                except Exception as exc:
                    accepted = {"state": STATE_ERROR, "ok": False,
                                "message": f"проверка не удалась "
                                           f"({type(exc).__name__})"}
                if accepted.get("ok"):
                    self._set_status(
                        f"токен принят из {browser or 'браузера'}: "
                        f"{mask_token(token)}", ok=True)
                    self.btn_token_retry.setVisible(False)
                    self._refresh_token_label()
                    try:
                        self.auth_changed.emit()
                    except RuntimeError:
                        pass
                    return
                reason = str(accepted.get("message") or "токен отклонён")
                self._set_status("вход не выполнен: " + reason, warn=True)
                self.btn_token_retry.setVisible(True)
                return
            checked = ", ".join(str(x) for x in (result.get("checked") or []))
            message = str(result.get("message") or "cookie JWT не найдена")
            if checked:
                message += f" (проверено: {checked})"
            message += (" — ручной путь: «Войти через свой браузер "
                        "(вручную)» или «Указать папку профиля…»")
            self._set_status("вход не выполнен: " + message, warn=True)
            self.btn_token_retry.setVisible(True)

        # ── ручная вставка и выход ────────────────────────────────────
        def open_manual(self):
            # Ручной путь: открываем warframe.market в ОБЫЧНОМ браузере
            # (если колбэк дан приложением), затем — поле для JWT.
            if callable(self.manual_open):
                try:
                    self.manual_open(SITE)
                except Exception:
                    pass
            if not callable(self.manual_factory):
                self._set_status("ручная вставка недоступна", warn=True)
                return False
            try:
                dialog = self.manual_factory(self)
            except Exception as exc:
                self._set_status(
                    f"окно ручной вставки не открылось "
                    f"({type(exc).__name__})", warn=True)
                return False
            if dialog is None:
                return False
            try:
                dialog.exec_()
            except Exception:
                pass
            finally:
                try:
                    dialog.deleteLater()
                except Exception:
                    pass
            self._refresh_token_label()
            if self._has_token():
                self._set_status("токен из ручной вставки сохранён", ok=True)
                try:
                    self.auth_changed.emit()
                except RuntimeError:
                    pass
            return True

        def _has_token(self):
            checker = getattr(self.controller, "has_token", None)
            if not callable(checker):
                return False
            try:
                return bool(checker())
            except Exception:
                return False

        def _profile_cleaner(self):
            session = self._session
            if session is not None:
                return session
            try:
                return BrowserLoginSession(self.controller, cfg=self.cfg,
                                           data_dir=self.data_dir)
            except Exception:
                return None

        def logout(self):
            try:
                result = self.controller.logout()
            except Exception as exc:
                self._set_status(
                    f"выход не удался ({type(exc).__name__})", warn=True)
                return False
            message = str(result.get("message") or "вы вышли")
            cleaner = self._profile_cleaner()
            removed = False
            if cleaner is not None:
                try:
                    removed = bool(cleaner.clear_profile())
                except Exception:
                    removed = False
            if removed:
                message += "; профили браузеров удалены"
            self._set_status(message)
            self._refresh_token_label()
            try:
                self.auth_changed.emit()
            except RuntimeError:
                pass
            return True

        # ── закрытие ─────────────────────────────────────────────────
        def _finish(self):
            if self._closed:
                return
            self._closed = True
            # страховка: если окно закрыли путём, минующим _on_progress,
            # оверлей всё равно должен вернуться (пилюля — исчезнуть)
            self._exit_login_mode(restore_dialog=False)
            session, self._session = self._session, None
            if session is not None:
                try:
                    session.cancel()
                except Exception:
                    pass
            try:
                self.accept()
            except Exception:
                try:
                    self.close()
                except Exception:
                    pass

        def closeEvent(self, event):
            self._closed = True
            self._exit_login_mode(restore_dialog=False)
            session, self._session = self._session, None
            if session is not None:
                try:
                    session.cancel()
                except Exception:
                    pass
            super().closeEvent(event)

        def reject(self):
            self._closed = True
            self._exit_login_mode(restore_dialog=False)
            session, self._session = self._session, None
            if session is not None:
                try:
                    session.cancel()
                except Exception:
                    pass
            super().reject()

    return MarketBrowserLoginDialog
