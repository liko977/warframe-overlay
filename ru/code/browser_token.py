# -*- coding: utf-8 -*-
"""Токен warframe.market из профилей браузеров пользователя (t30, 0.6.1).

Поддерживаются:

* Chromium-семейство (Chrome, Edge, Yandex, Chromium, Brave, Vivaldi):
  ``User Data/**/Network/Cookies`` (или старый ``Cookies``). Значения ``v10``/
  ``v11`` — AES-256-GCM, ключ из ``Local State`` после DPAPI; ``v20``
  (App-Bound Encryption) честно не поддерживается.
* Firefox-семейство (Firefox, Waterfox, LibreWolf): ``Profiles/*/cookies.sqlite``,
  таблица ``moz_cookies``, значение в открытом виде.

Приватность: читается только cookie ``JWT`` с домена warframe.market; значение
никогда не логируется (только длина/имя/домен).
"""

import base64
import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import urllib.error
import urllib.request

SITE = "https://warframe.market/"
API_ME = "https://api.warframe.market/v2/me"
COOKIE_NAME = "JWT"
COOKIE_DOMAIN = "warframe.market"

#: (имя в UI, относительные кандидаты от LOCALAPPDATA/APPDATA/PROGRAMDATA)
CHROMIUM_BROWSERS = (
    ("Chrome", ("Google/Chrome/User Data",
                "Google/Chrome Beta/User Data",
                "Google/Chrome Dev/User Data")),
    ("Edge", ("Microsoft/Edge/User Data",)),
    ("Yandex", ("Yandex/YandexBrowser/User Data",)),
    ("Opera", ("Opera Software/Opera Stable",
               "Opera Software/Opera Next")),
    ("Opera GX", ("Opera Software/Opera GX Stable",
                  "Opera Software/Opera GX")),
    ("Chromium", ("Chromium/User Data",)),
    ("Brave", ("BraveSoftware/Brave-Browser/User Data",)),
    ("Vivaldi", ("Vivaldi/User Data",)),
    ("Comodo Dragon", ("Comodo/Dragon/User Data",
                       "COMODO/Dragon/User Data")),
    ("Atom", ("Mail.Ru/Atom/User Data", "Atom/User Data")),
)

FIREFOX_BROWSERS = (
    ("Firefox", "Mozilla/Firefox"),
    ("Waterfox", "Waterfox"),
    ("LibreWolf", "LibreWolf"),
    ("SeaMonkey", "Mozilla/SeaMonkey"),
)


class BrowserTokenError(RuntimeError):
    """Базовая ошибка чтения токена."""


class CookieDbLocked(BrowserTokenError):
    """База cookie занята браузером."""


class UnsupportedV20(BrowserTokenError):
    """Профиль использует App-Bound Encryption (v20)."""


class DecryptError(BrowserTokenError):
    """Не удалось расшифровать значение cookie."""


def mask_token(token):
    token = str(token or "")
    if not token:
        return "—"
    if len(token) <= 10:
        return "•" * len(token)
    return "••••" + token[-4:]


def looks_like_jwt(value):
    text = str(value or "").strip()
    return len(text) > 30 and text.count(".") == 2


def is_warframe_domain(host):
    host = str(host or "").strip().lower().lstrip(".")
    return host == COOKIE_DOMAIN or host.endswith("." + COOKIE_DOMAIN)


def _redact(message):
    """Страховка логов: вырезает JWT-подобные строки."""
    text = str(message or "")
    parts = []
    for chunk in text.split():
        if looks_like_jwt(chunk):
            parts.append("••••")
        else:
            parts.append(chunk)
    return " ".join(parts)


# ─────────────────────────────────────────────────────────────────────────
#  DPAPI / AES-256-GCM (v10/v11)
# ─────────────────────────────────────────────────────────────────────────
class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_decrypt_windows(data):
    """DPAPI CryptUnprotectData (Windows). Только для Local State."""

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    raw = bytes(data or b"")
    if not raw:
        raise DecryptError("пустой DPAPI-блок")
    buffer = ctypes.create_string_buffer(raw, len(raw))
    blob_in = _Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
    except Exception as exc:                  # не Windows / нет DLL
        raise DecryptError(f"DPAPI недоступен ({type(exc).__name__})")
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0,
        ctypes.byref(blob_out))
    if not ok:
        raise DecryptError("DPAPI не расшифровал ключ")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        try:
            kernel32.LocalFree(blob_out.pbData)
        except Exception:
            pass


def _aes_gcm_decrypt(key, nonce, ciphertext, tag):
    """AES-256-GCM: cryptography, если есть; иначе понятная ошибка."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except Exception as exc:
        raise DecryptError(
            "нет cryptography для AES-GCM — установите пакет cryptography "
            "или войдите вручную") from exc
    try:
        return AESGCM(bytes(key)).decrypt(bytes(nonce),
                                          bytes(ciphertext) + bytes(tag), None)
    except Exception as exc:
        raise DecryptError(f"cookie не расшифрована ({type(exc).__name__})")


def local_state_key(user_data_dir, dpapi_fn=None):
    """Ключ Chromium из ``Local State`` (после DPAPI)."""
    path = os.path.join(str(user_data_dir or ""), "Local State")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        encoded = str((data.get("os_crypt") or {}).get("encrypted_key") or "")
        raw = base64.b64decode(encoded)
    except Exception as exc:
        raise DecryptError("Local State не прочитан "
                           f"({type(exc).__name__})")
    if raw.startswith(b"DPAPI"):
        raw = raw[5:]
    fn = dpapi_fn or _dpapi_decrypt_windows
    try:
        key = fn(raw)
    except Exception as exc:
        if isinstance(exc, BrowserTokenError):
            raise
        raise DecryptError(f"DPAPI: {type(exc).__name__}")
    if len(key) != 32:
        raise DecryptError(f"ключ AES неверной длины ({len(key)})")
    return key


def decrypt_chromium_value(raw, key, decrypt_fn=None):
    """``v10``/``v11`` AES-256-GCM → строка; ``v20`` → UnsupportedV20."""
    raw = bytes(raw or b"")
    if len(raw) < 3:
        return ""
    prefix = raw[:3]
    if prefix in (b"v10", b"v11"):
        if len(raw) < 3 + 12 + 16:
            return ""
        if decrypt_fn is not None:
            try:
                plain = decrypt_fn(raw, key)
            except TypeError:
                plain = decrypt_fn(raw)
        else:
            if not key:
                raise DecryptError("нет ключа Local State")
            plain = _aes_gcm_decrypt(key, raw[3:15], raw[15:-16], raw[-16:])
        return bytes(plain).decode("utf-8", "replace").strip()
    if prefix == b"v20":
        raise UnsupportedV20(
            "этот профиль использует App-Bound Encryption (v20) — "
            "вход из него не поддерживается, войдите вручную")
    return ""


# ─────────────────────────────────────────────────────────────────────────
#  Чтение SQLite (копия → immutable → понятная ошибка)
# ─────────────────────────────────────────────────────────────────────────
def _connect(connect_fn, target, uri=False):
    fn = connect_fn or sqlite3.connect
    try:
        if uri:
            return fn(target, uri=True)
        return fn(target)
    except TypeError:
        return fn(target)


def _rows(result):
    """cursor.fetchall() или уже готовый список (для инъекций/тестов)."""
    fetch = getattr(result, "fetchall", None)
    return list(fetch()) if callable(fetch) else list(result or [])


def _query(db_path, sql, params=(), copy_fn=None, connect_fn=None):
    """Читает БД: копия; при PermissionError — immutable URI; иначе locked."""
    db_path = str(db_path or "")
    temp_dir = None
    try:
        temp_dir = tempfile.mkdtemp(prefix="wf_browser_cookie_")
        temp_db = os.path.join(temp_dir, "Cookies")
        try:
            if callable(copy_fn):
                copy_fn(db_path, temp_db)
            else:
                shutil.copy2(db_path, temp_db)
            conn = _connect(connect_fn, temp_db)
            try:
                return _rows(conn.execute(sql, params))
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except PermissionError:
            pass
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
        # копия не удалась/заблокирована — immutable-режим по исходной БД
        uri = f"file:{db_path}?immutable=1&mode=ro"
        try:
            conn = _connect(connect_fn, uri, uri=True)
            try:
                return _rows(conn.execute(sql, params))
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except PermissionError as exc:
            raise CookieDbLocked(
                "база cookie занята браузером — закройте браузер и нажмите "
                "«Попробовать снова»") from exc
        except sqlite3.OperationalError as exc:
            raise CookieDbLocked(
                "база cookie занята или недоступна — закройте браузер и "
                "нажмите «Попробовать снова»") from exc
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────
#  Поиск профилей
# ─────────────────────────────────────────────────────────────────────────
def _norm_roots(roots):
    result = []
    for item in roots or []:
        if isinstance(item, dict):
            family = str(item.get("family") or "chromium")
            name = str(item.get("name") or "")
            path = str(item.get("path") or "")
        else:
            family, name, path = "chromium", "", str(item)
        if path:
            result.append({"family": family, "name": name,
                           "path": os.path.abspath(os.path.expandvars(path))})
    return result


def _unique_paths(paths):
    result = []
    seen = set()
    for path in paths or []:
        if not path:
            continue
        key = os.path.normcase(os.path.abspath(os.path.expandvars(path)))
        if key in seen:
            continue
        seen.add(key)
        result.append(os.path.abspath(os.path.expandvars(path)))
    return result


def opera_legacy_key():
    """Исторический Chromium/Opera static key (на случай отсутствия Local State)."""
    return hashlib.pbkdf2_hmac("sha1", b"peanuts", b"saltysalt", 1, 16)


def default_chromium_roots(environ=None):
    """Кандидаты путей Chromium-семейства: LocalAppData/AppData/ProgramData."""
    env = dict(os.environ if environ is None else environ)
    bases = [env.get("LOCALAPPDATA"), env.get("APPDATA"),
             env.get("PROGRAMDATA"), env.get("PROGRAMFILES"),
             env.get("PROGRAMFILES(X86)")]
    bases = [base for base in bases if base]
    result = []
    for name, suffixes in CHROMIUM_BROWSERS:
        paths = []
        # Opera/Opera GX исторически живут в Roaming (APPDATA), остальные —
        # в LOCALAPPDATA; проверяем все базы, но Opera — в первую очередь
        # в APPDATA, чтобы название в UI/логе было правильным.
        ordered = bases
        if name in ("Opera", "Opera GX"):
            ordered = ([env.get("APPDATA")] +
                       [base for base in bases if base != env.get("APPDATA")])
        for base in ordered:
            if not base:
                continue
            for suffix in suffixes:
                paths.append(os.path.join(base, *suffix.split("/")))
        for path in _unique_paths(paths):
            result.append({"family": "chromium", "name": name, "path": path})
    return result


def default_firefox_roots(environ=None):
    env = dict(os.environ if environ is None else environ)
    bases = [env.get("APPDATA"), env.get("LOCALAPPDATA")]
    result = []
    for name, suffix in FIREFOX_BROWSERS:
        for base in bases:
            if not base:
                continue
            result.append({"family": "firefox", "name": name,
                           "path": os.path.join(base, *suffix.split("/"))})
    return result


def chromium_profiles(user_data, name="", max_depth=2):
    """Все профили в ``User Data`` (Default, Profile 1, …)."""
    user_data = os.path.abspath(str(user_data or ""))
    result = []
    if not os.path.isdir(user_data):
        return result
    candidates = [user_data]
    try:
        for entry in sorted(os.scandir(user_data), key=lambda e: e.name):
            if entry.is_dir() and entry.name != "System Profile":
                candidates.append(entry.path)
    except Exception:
        pass
    for base in candidates:
        for rel in (os.path.join("Network", "Cookies"), "Cookies"):
            db = os.path.join(base, rel)
            if os.path.isfile(db):
                result.append({
                    "family": "chromium", "browser": name,
                    "profile": base,
                    "user_data": user_data,
                    "db_path": db,
                    "local_state": os.path.join(user_data, "Local State"),
                })
                break
    return result


def firefox_profiles(profile_root, name=""):
    """Все ``Profiles/*/cookies.sqlite`` в каталоге Firefox."""
    root = os.path.abspath(str(profile_root or ""))
    result = []
    if not os.path.isdir(root):
        return result
    roots = [root]
    profiles_dir = os.path.join(root, "Profiles")
    if os.path.isdir(profiles_dir):
        roots.append(profiles_dir)
    for base in roots:
        try:
            entries = sorted(os.scandir(base), key=lambda e: e.name)
        except Exception:
            continue
        for entry in entries:
            db = os.path.join(entry.path, "cookies.sqlite")
            if entry.is_dir() and os.path.isfile(db):
                result.append({"family": "firefox", "browser": name,
                               "profile": entry.path, "db_path": db})
    return result


def find_cookie_stores(environ=None, roots=None, firefox_roots=None,
                       extra_paths=None, scan_generic=True):
    """Упорядоченный список баз cookie (Chromium, затем Firefox)."""
    stores = []
    chrom_roots = (_norm_roots(roots) if roots is not None else
                   (default_chromium_roots(environ)
                    if firefox_roots is None else []))
    if scan_generic and roots is None and firefox_roots is None:
        chrom_roots.extend(scan_chromium_roots(environ))
    for root in chrom_roots:
        if root["family"] != "chromium":
            continue
        known = str(root.get("name") or "") in KNOWN_BROWSER_ORDER
        for store in chromium_profiles(root["path"], root["name"]):
            store["known"] = known
            stores.append(store)
    fire_roots = (_norm_roots(firefox_roots) if firefox_roots is not None else
                  (default_firefox_roots(environ) if roots is None else []))
    for root in fire_roots:
        if root["family"] != "firefox":
            continue
        known = str(root.get("name") or "") in KNOWN_BROWSER_ORDER
        for store in firefox_profiles(root["path"], root["name"]):
            store["known"] = known
            stores.append(store)
    for path in extra_paths or []:
        stores.extend(manual_profile_stores(path))
    unique = []
    seen = set()
    for store in stores:
        key = (store.get("family"), os.path.normcase(store.get("db_path", "")))
        if key in seen:
            continue
        seen.add(key)
        unique.append(store)
    return unique


def _find_local_state(path):
    base = os.path.abspath(str(path or ""))
    for _ in range(4):
        candidate = os.path.join(base, "Local State")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(base)
        if parent == base:
            break
        base = parent
    return ""


def manual_profile_stores(path):
    """Разбор вручную выбранной папки (профиль/User Data/Network)."""
    path = os.path.abspath(str(path or ""))
    result = []
    if not os.path.isdir(path):
        return result
    firefox_db = os.path.join(path, "cookies.sqlite")
    if os.path.isfile(firefox_db):
        result.append({"family": "firefox", "browser": "Указанный профиль",
                       "profile": path, "db_path": firefox_db})
        return result
    found = chromium_profiles(path, "Указанный профиль")
    if not found:
        found.extend(firefox_profiles(path, "Указанный профиль"))
    for store in found:
        local_state = _find_local_state(store.get("profile") or path)
        if local_state:
            store["local_state"] = local_state
            store["user_data"] = os.path.dirname(local_state)
        elif not store.get("local_state"):
            store["local_state"] = _find_local_state(path)
    result.extend(found)
    if result:
        return result
    # выбрали папку Network — поднимаемся к профилю
    if os.path.basename(path).lower() == "network":
        profile = os.path.dirname(path)
        db = os.path.join(path, "Cookies")
        if os.path.isfile(db):
            result.append({
                "family": "chromium", "browser": "Указанный профиль",
                "profile": profile, "user_data": profile,
                "db_path": db,
                "local_state": _find_local_state(profile),
            })
        if result:
            return result
    # выбрали папку браузера — ищем Local State + cookies в профилях
    for store in chromium_profiles(path, "Указанный профиль"):
        result.append(store)
    return result


def _local_state_has_os_crypt(path):
    """В каталоге есть Local State с секцией os_crypt (generic-кандидат)."""
    try:
        with open(os.path.join(str(path or ""), "Local State"),
                  "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return isinstance(data, dict) and isinstance(
            data.get("os_crypt"), dict)
    except Exception:
        return False


def _is_suspect_name(path):
    text = os.path.normcase(str(path or "")).replace("/", "\\")
    return any(part in text for part in _SUSPECT_PARTS)


def is_suspect_store(store):
    """Electron/лаунчер (скрывается, если JWT не найдена)."""
    if (store or {}).get("known"):
        return False
    text = " ".join(str((store or {}).get(key) or "")
                    for key in ("browser", "profile", "db_path"))
    return _is_suspect_name(text)


def browser_sort_key(store):
    name = str((store or {}).get("browser") or "")
    try:
        index = KNOWN_BROWSER_ORDER.index(name)
    except ValueError:
        index = len(KNOWN_BROWSER_ORDER)
    return (0 if index < len(KNOWN_BROWSER_ORDER) else 1, index, name.lower())
_SCAN_SKIP = {
    "windows", "winsxs", "system32", "syswow64", "temp", "tmp", "cache",
    "node_modules", ".git", "packages", "microsoft teams", "nvidia",
    "amd", "intel", "installer", "downloads", "onedrive",
    "copilot", "ebwebview", "qtwebengine", "htmlcache", "steam", "razer",
    "python", "dsh", "dsh-desktop",
}
_SCAN_SKIP_PARTS = ("webview", "cache", "copilot", "steam", "razer",
                    "dsh", "qtwebengine", "nvidia")
#: Порядок известных браузеров в списке UI.
KNOWN_BROWSER_ORDER = (
    "Chrome", "Edge", "Yandex", "Opera", "Opera GX", "Firefox",
    "Waterfox", "LibreWolf", "SeaMonkey", "Chromium", "Brave", "Vivaldi",
    "Comodo Dragon", "Atom",
)
#: Electron/лаунчеры: в generic-списке скрываем, если JWT реально нет.
_SUSPECT_PARTS = (
    "discord", "slack", "teams", "telegram", "vscode", "cursor",
    "obsidian", "r2modman", "launcher", "music", "dsh-desktop", "steam",
    "epic", "battle.net", "electron", "copilot", "webview", "yandexmusic",
)


def _human_browser_name(path, fallback="Браузер"):
    name = os.path.basename(os.path.normpath(str(path or "")))
    lower = name.lower()
    known = {
        "opera gx stable": "Opera GX", "opera gx": "Opera GX",
        "opera stable": "Opera", "opera next": "Opera",
        "google chrome": "Chrome", "chrome": "Chrome",
        "microsoft edge": "Edge", "edge": "Edge",
        "yandexbrowser": "Yandex", "yandex": "Yandex",
        "brave-browser": "Brave", "brave": "Brave",
        "comodo dragon": "Comodo Dragon", "dragon": "Comodo Dragon",
    }
    if lower in known:
        return known[lower]
    if lower in ("user data", "default", "profile 1", "profile 2"):
        name = os.path.basename(os.path.dirname(os.path.normpath(path)))
        if name.lower() in known:
            return known[name.lower()]
    name = name.replace("_", " ").replace("-", " ").strip()
    return name or fallback


def scan_chromium_roots(environ=None, max_depth=3, max_dirs=400):
    """Обход LOCALAPPDATA/APPDATA: `<X>/User Data` и Opera-подобные пути.

    Ограничен глубиной/количеством каталогов; содержимое БД здесь не читается.
    """
    env = dict(os.environ if environ is None else environ)
    bases = [env.get("LOCALAPPDATA"), env.get("APPDATA")]
    bases = [base for base in bases if base and os.path.isdir(base)]
    found = []
    visited = 0

    def add(path):
        if not _local_state_has_os_crypt(path):
            return False
        for store in chromium_profiles(path, _human_browser_name(path)):
            found.append({"family": "chromium",
                          "name": store["browser"], "path": path})
            return True
        return False

    def walk(base, depth):
        nonlocal visited
        if depth > max_depth or visited >= max_dirs:
            return
        try:
            entries = sorted(os.scandir(base), key=lambda e: e.name)
        except Exception:
            return
        for entry in entries:
            if visited >= max_dirs:
                return
            if not entry.is_dir(follow_symlinks=False):
                continue
            lower = entry.name.lower()
            if lower in _SCAN_SKIP or lower.startswith("."):
                continue
            if any(part in lower for part in _SCAN_SKIP_PARTS):
                continue
            if not add(entry.path):
                walk(entry.path, depth + 1)
            visited += 1

    for base in bases:
        walk(base, 1)
    # дедуплицируем корни
    result = []
    seen = set()
    for item in found:
        key = os.path.normcase(os.path.abspath(item["path"]))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result



# ─────────────────────────────────────────────────────────────────────────
#  Извлечение JWT
# ─────────────────────────────────────────────────────────────────────────
def chromium_token(store, dpapi_fn=None, decrypt_fn=None,
                   copy_fn=None, connect_fn=None, logger=None):
    """JWT из Chromium-профиля; либо пустая строка/ошибка."""
    db_path = str((store or {}).get("db_path") or "")
    user_data = str((store or {}).get("user_data")
                    or os.path.dirname(db_path))
    rows = _query(
        db_path,
        "SELECT host_key, name, value, encrypted_value FROM cookies "
        "WHERE name=?", (COOKIE_NAME,), copy_fn=copy_fn,
        connect_fn=connect_fn)
    last_error = None
    for host, name, value, encrypted in rows:
        if str(name) != COOKIE_NAME or not is_warframe_domain(host):
            continue
        plain = str(value or "").strip()
        if not plain and encrypted:
            raw = bytes(encrypted)
            is_opera = "opera" in str((store or {}).get("browser") or "").lower()
            if raw[:3] == b"v20":
                raise UnsupportedV20(
                    "этот профиль использует App-Bound Encryption (v20) — "
                    "вход из него не поддерживается, войдите вручную")
            try:
                key = None
                key_error = None
                if decrypt_fn is None:
                    try:
                        key = local_state_key(user_data, dpapi_fn=dpapi_fn)
                    except BrowserTokenError as exc:
                        key_error = exc
                        if not is_opera:
                            raise
                        key = opera_legacy_key()
                        if callable(logger):
                            logger(f"cookie {store.get('browser')}: Local State "
                                   "не прочитан, пробую legacy-ключ")
                try:
                    plain = decrypt_chromium_value(encrypted, key,
                                                   decrypt_fn=decrypt_fn)
                except UnsupportedV20:
                    raise
                except BrowserTokenError:
                    if not is_opera or key_error is not None:
                        raise
                    # Opera: ключ из Local State мог быть другим — пробуем
                    # исторический static key.
                    legacy = opera_legacy_key()
                    if legacy == key:
                        raise
                    if callable(logger):
                        logger(f"cookie {store.get('browser')}: "
                               "пробую legacy-ключ Opera")
                    plain = decrypt_chromium_value(encrypted, legacy,
                                                   decrypt_fn=decrypt_fn)
            except UnsupportedV20:
                raise
            except BrowserTokenError as exc:
                last_error = exc
                if callable(logger):
                    logger(f"cookie {store.get('browser')}: "
                           f"{type(exc).__name__}: {exc}")
                continue
            except Exception as exc:
                last_error = DecryptError(
                    f"cookie не расшифрована ({type(exc).__name__})")
                if callable(logger):
                    logger("cookie расшифровка: "
                           f"{type(exc).__name__}")
                continue
        if looks_like_jwt(plain):
            if callable(logger):
                logger(f"cookie домен={host} имя={COOKIE_NAME} "
                       f"длина={len(plain)}")
            return plain
    if last_error is not None:
        raise last_error
    return ""


def firefox_token(store, copy_fn=None, connect_fn=None, logger=None):
    """JWT из Firefox cookies.sqlite (plaintext moz_cookies)."""
    db_path = str((store or {}).get("db_path") or "")
    row = _query(
        db_path,
        "SELECT host, name, value FROM moz_cookies WHERE name=?",
        (COOKIE_NAME,), copy_fn=copy_fn, connect_fn=connect_fn)
    empty_value = False
    for host, name, value in row:
        if str(name) != COOKIE_NAME or not is_warframe_domain(host):
            continue
        text = str(value or "").strip()
        if not text:
            empty_value = True
            continue
        if looks_like_jwt(text):
            if callable(logger):
                logger(f"cookie домен={host} имя={COOKIE_NAME} "
                       f"длина={len(text)}")
            return text
    if empty_value:
        raise DecryptError(
            "значение cookie пустое — возможно, включён мастер-пароль "
            "Firefox; войдите вручную")
    return ""


def verify_token(token, http_get=None):
    """GET /v2/me с Bearer: 200/401/network/error."""
    token = str(token or "").strip()
    if not token:
        return {"ok": False, "status": "empty", "code": 0,
                "message": "токен не найден"}
    if not callable(http_get):
        def http_get(url, headers, timeout=10):
            request = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=timeout) as resp:
                    return resp.getcode()
            except urllib.error.HTTPError as exc:
                return exc.code
    try:
        headers = {"Authorization": f"Bearer {token}",
                   "Accept": "application/json",
                   "User-Agent": "VoidFrameRU/0.6.1"}
        try:
            code = http_get(API_ME, headers, 10)
        except TypeError:
            code = http_get(API_ME, headers)
    except Exception as exc:
        return {"ok": False, "status": "no_network", "code": 0,
                "message": f"нет сети ({type(exc).__name__})"}
    if isinstance(code, (tuple, list)) and code:
        code = code[0]
    elif hasattr(code, "status_code"):
        code = getattr(code, "status_code")
    try:
        code = int(code)
    except Exception:
        code = 0
    if code == 200:
        return {"ok": True, "status": "ok", "code": 200,
                "message": "токен принят"}
    if code == 401:
        return {"ok": False, "status": "unauthorized", "code": 401,
                "message": "токен отклонён (401) — войдите заново"}
    return {"ok": False, "status": "error", "code": int(code or 0),
            "message": f"проверка токена: HTTP {code}"}


class BrowserTokenFinder:
    """Поиск JWT по профилям браузеров с понятным итогом."""

    def __init__(self, environ=None, roots=None, firefox_roots=None,
                 dpapi_fn=None, decrypt_fn=None, copy_fn=None,
                 connect_fn=None, http_get=None, logger=None,
                 extra_paths=None, scan_generic=True):
        self.environ = environ
        self.roots = roots
        self.firefox_roots = firefox_roots
        self.dpapi_fn = dpapi_fn
        self.decrypt_fn = decrypt_fn
        self.copy_fn = copy_fn
        self.connect_fn = connect_fn
        self.http_get = http_get
        self.logger = logger
        self.extra_paths = list(extra_paths or [])
        self.scan_generic = bool(scan_generic)
        self.checked = []
        self._stores_cache = None
        self._profiles_cache = None

    def _log(self, message):
        if callable(self.logger):
            try:
                self.logger(_redact(message))
            except Exception:
                pass

    def refresh(self):
        """Сбросить кэш (кнопка «Найти заново»)."""
        self._stores_cache = None
        self._profiles_cache = None
        return True

    def stores(self):
        if self._stores_cache is None:
            self._stores_cache = find_cookie_stores(
                self.environ, self.roots, self.firefox_roots,
                extra_paths=self.extra_paths,
                scan_generic=self.scan_generic)
        return list(self._stores_cache)

    def profile_has_jwt(self, store):
        """Быстрая проверка наличия строки JWT (без расшифровки значения)."""
        db_path = str((store or {}).get("db_path") or "")
        try:
            if (store or {}).get("family") == "firefox":
                rows = _query(db_path,
                              "SELECT host, name FROM moz_cookies "
                              "WHERE name=? LIMIT 1",
                              (COOKIE_NAME,), copy_fn=self.copy_fn,
                              connect_fn=self.connect_fn)
                return any(is_warframe_domain(row[0]) for row in rows)
            rows = _query(db_path,
                          "SELECT host_key, name FROM cookies "
                          "WHERE name=? LIMIT 1",
                          (COOKIE_NAME,), copy_fn=self.copy_fn,
                          connect_fn=self.connect_fn)
            return any(is_warframe_domain(row[0]) for row in rows)
        except Exception:
            return False

    def scan_profiles(self, probe=True):
        """Список профилей для UI: браузер/профиль/есть ли JWT.

        Настоящие браузеры идут первыми; Electron/лаунчеры с пустыми cookie
        скрываются, но профиль с реально найденной JWT показывается.
        """
        if self._profiles_cache is not None:
            return list(self._profiles_cache)
        result = []
        for store in sorted(self.stores(), key=browser_sort_key):
            has_jwt = bool(probe and self.profile_has_jwt(store))
            if probe and is_suspect_store(store) and not has_jwt:
                continue
            profile_path = str(store.get("profile") or "")
            user_data = str(store.get("user_data") or "")
            try:
                same = (profile_path and user_data and
                        os.path.normcase(os.path.abspath(profile_path)) ==
                        os.path.normcase(os.path.abspath(user_data)))
            except Exception:
                same = False
            profile_name = ("Default" if same else
                            (os.path.basename(profile_path) or "Default"))
            item = {
                "browser": str(store.get("browser") or "Браузер"),
                "profile": profile_name,
                "family": store.get("family") or "chromium",
                "db_path": store.get("db_path") or "",
                "has_jwt": has_jwt,
            }
            result.append(item)
        self._profiles_cache = result
        return list(result)

    def find_token(self, only=None):
        self.checked = []
        stores = sorted(self.stores(), key=browser_sort_key)
        if only:
            key = os.path.normcase(os.path.abspath(str(only)))
            prefix = key + os.sep
            stores = [s for s in stores
                      if os.path.normcase(os.path.abspath(
                          str(s.get("db_path") or ""))) == key
                      or os.path.normcase(os.path.abspath(
                          str(s.get("profile") or ""))) == key
                      or os.path.normcase(os.path.abspath(
                          str(s.get("db_path") or ""))).startswith(prefix)
                      or os.path.normcase(os.path.abspath(
                          str(s.get("profile") or ""))).startswith(prefix)]
        if not stores:
            return {"ok": False, "status": "no_profiles",
                    "token": "", "checked": [],
                    "checked_count": 0,
                    "message": "проверено 0 профилей — профили браузеров "
                               "не найдены; войдите вручную или укажите "
                               "папку профиля"}
        errors = []
        for store in stores:
            browser = store.get("browser") or "браузер"
            family = store.get("family") or "chromium"
            # дешёвый фильтр Electron/лаунчеров: JWT там почти всегда нет
            if is_suspect_store(store) and not self.profile_has_jwt(store):
                continue
            if browser not in self.checked:
                profile_name = os.path.basename(
                    str(store.get("profile") or "")) or "Default"
                label = f"{browser} ({profile_name})"
                if label not in self.checked:
                    self.checked.append(label)
            self._log(f"проверяю {browser}: {store.get('db_path')}")
            try:
                token = (firefox_token(store, copy_fn=self.copy_fn,
                                       connect_fn=self.connect_fn,
                                       logger=self.logger)
                         if family == "firefox" else
                         chromium_token(store, dpapi_fn=self.dpapi_fn,
                                        decrypt_fn=self.decrypt_fn,
                                        copy_fn=self.copy_fn,
                                        connect_fn=self.connect_fn,
                                        logger=self.logger))
            except UnsupportedV20 as exc:
                errors.append(f"{browser}: {exc}")
                continue
            except CookieDbLocked as exc:
                errors.append(f"{browser}: {exc}")
                continue
            except BrowserTokenError as exc:
                errors.append(f"{browser}: {exc}")
                continue
            except Exception as exc:
                errors.append(f"{browser}: ошибка чтения "
                              f"{type(exc).__name__}")
                continue
            if token:
                result = verify_token(token, self.http_get)
                if result.get("ok"):
                    return {"ok": True, "status": "ok", "token": token,
                            "browser": browser,
                            "profile": store.get("profile", ""),
                            "checked": self.checked,
                            "message": f"токен принят из {browser}"}
                if result.get("status") == "unauthorized":
                    errors.append(f"{browser}: токен отклонён (401)")
                else:
                    errors.append(f"{browser}: {result.get('message')}")
            else:
                errors.append(f"{browser}: cookie JWT не найдена")
        checked = list(dict.fromkeys(self.checked))
        count = len(checked)
        message = (f"проверено {count} профилей"
                   + (f" ({', '.join(checked)})" if checked else "")
                   + ", JWT не найден — войдите на warframe.market в браузере "
                     "и нажмите «Найти заново»; либо укажите папку профиля "
                     "вручную")
        if errors:
            message += "; " + "; ".join(errors[-3:])
        return {"ok": False, "status": "not_found", "token": "",
                "checked": checked, "checked_count": count,
                "message": message}


def find_token(environ=None, roots=None, firefox_roots=None, logger=None,
               decrypt_fn=None, http_get=None, extra_paths=None,
               scan_generic=True):
    """Удобная обёртка: найти и проверить JWT из профилей браузеров."""
    return BrowserTokenFinder(
        environ=environ, roots=roots, firefox_roots=firefox_roots,
        logger=logger, decrypt_fn=decrypt_fn, http_get=http_get,
        extra_paths=extra_paths, scan_generic=scan_generic).find_token()


__all__ = [
    "API_ME", "COOKIE_DOMAIN", "COOKIE_NAME", "BrowserTokenError",
    "CookieDbLocked", "UnsupportedV20", "DecryptError", "BrowserTokenFinder",
    "decrypt_chromium_value", "find_cookie_stores", "chromium_profiles",
    "firefox_profiles", "chromium_token", "firefox_token", "verify_token",
    "local_state_key", "looks_like_jwt", "is_warframe_domain", "mask_token",
    "manual_profile_stores", "scan_chromium_roots", "find_token",
]
