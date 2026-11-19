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
    python warframe.py               # обычный режим (F8 — скан, F9 — скрыть)
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
import re
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─────────────────────────────────────────────────────────────────────────
#  Базовые константы
# ─────────────────────────────────────────────────────────────────────────
BASE_W, BASE_H = 1920.0, 1080.0
APP_VERSION = "0.3.1"
API_ROOT = "https://api.warframe.market/v2"
ITEMS_ENDPOINT = API_ROOT + "/items"


# ─────────────────────────────────────────────────────────────────────────
#  Пути (работают и из исходника, и из собранного .exe)
# ─────────────────────────────────────────────────────────────────────────
def _app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _pick_data_dir(app_dir):
    """Папка для config.json / кэшей: рядом с программой, иначе LOCALAPPDATA/temp."""
    env = os.environ.get("WF_DATA_DIR")
    if env:
        try:
            os.makedirs(env, exist_ok=True)
            return env
        except Exception:
            pass
    candidates = [app_dir,
                  os.path.join(os.environ.get("LOCALAPPDATA") or
                               os.path.expanduser("~"), "WarframePriceOverlay"),
                  os.path.join(os.environ.get("TEMP") or os.path.expanduser("~"),
                               "WarframePriceOverlay")]
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
ITEMS_CACHE = os.path.join(DATA_DIR, "items_cache.json")


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
    "hotkey_scan": "f8",
    "hotkey_hide": "f9",
    "overlay_duration_ms": 12000,
    "alert_threshold": 100,          # платина: выше — звуковой сигнал
    "play_sound": True,
    "show_debug": True,              # сохранять картинки в ocr_debug/
    "debug_dir": "ocr_debug",

    # ── сеть ────────────────────────────────────────────────────────────
    "request_timeout": 8,
    "workers": 8,
    "price_ttl": 180,                # сек, кэш цен
    "items_cache_ttl": 86400,        # сек, кэш списка предметов

    # ── обновления ──────────────────────────────────────────────────────
    "update_url": "",                # URL манифеста update.json (см. README)
    "update_check": True,            # проверять обновления при запуске

    # ── экран / геометрия (координаты для 1920x1080) ────────────────────
    "monitor": 1,                    # 1 = основной монитор (mss)
    "base_width": 1920,
    "base_height": 1080,
    "card_width": 240,               # ширина карточки
    "card_pitch": 250,               # шаг между карточками
    "cards_center_x": 965,           # центр ряда карточек
    "card_top": 220,                 # верх карточки
    "card_height": 250,
    "name_offset_y": 140,            # текст названия = card_top + это
    "name_height": 100,
    "owned_strip_height": 40,        # шапка карточки: Crafted / N Owned
    "scan_band": {"top": 340, "left": 300, "width": 1320, "height": 145},
    "overlay_offset_y": 14,          # отступ плашки от низа карточки
    "force_cards": 0,                # 0 = авто, 1..4 = принудительно
    "scan_retries": 2,               # сколько раз переснять, если ничего не нашли
    "scan_retry_delay_ms": 350,
    "font_scale": 1.0,
}


# ─────────────────────────────────────────────────────────────────────────
#  Мелкие утилиты
# ─────────────────────────────────────────────────────────────────────────
def log(msg=""):
    try:
        print(msg, flush=True)
    except Exception:
        pass


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
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                cfg = deep_merge(cfg, json.load(f))
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
    if not isinstance(p, (int, float)) or p <= 0:
        return "#8b95a5"
    if p < 15:
        return "#b9c3d1"
    if p < 40:
        return "#dbe3ee"
    if p < 80:
        return "#ffd75e"
    if p < 150:
        return "#ffa23a"
    return "#ff6a3a"


# ─────────────────────────────────────────────────────────────────────────
#  Warframe Market API v2
# ─────────────────────────────────────────────────────────────────────────
def normalize_name(text):
    """Приводит распознанное название к сравнимому виду."""
    if not text:
        return ""
    t = text.lower()
    t = t.replace("—", " ").replace("–", " ")
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    # мусор, который OCR цепляет с экрана наград
    t = re.sub(r"\b(owned|crafted|built|ownedcrafted|owneo|onned|onnd|ondde)\b", " ", t)
    t = re.sub(r"\b(plat|platinum|ducats?)\b", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


class MarketAPI:
    """Тонкая обёртка над API warframe.market (v2) с кэшами."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            "Accept": "application/json",
            "Platform": "pc",
            "Language": "en",
            "Crossplay": "true",
        }
        self.timeout = cfg.get("request_timeout", 8)
        self._lock = threading.Lock()
        self._price_cache = {}      # slug -> (ts, data)
        self._detail_cache = {}     # slug -> (ts, data)
        self.items = {}             # norm_name -> {"slug", "name"}
        self._loaded = False

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
    def load_items(self):
        if self._loaded:
            return True
        # диск
        try:
            if os.path.exists(ITEMS_CACHE):
                with open(ITEMS_CACHE, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if time.time() - d.get("ts", 0) < self.cfg.get("items_cache_ttl", 86400):
                    self.items = d.get("items") or {}
                    if self.items:
                        self._loaded = True
                        log(f"База предметов: {len(self.items)} (кэш)")
                        return True
        except Exception:
            pass
        # сеть
        try:
            data = self._get(ITEMS_ENDPOINT)
            rows = (data or {}).get("data") or []
            items = {}
            for it in rows:
                slug = it.get("slug")
                name = ((it.get("i18n") or {}).get("en") or {}).get("name") or ""
                if not slug or not name:
                    continue
                items[normalize_name(name)] = {"slug": slug, "name": name}
            if items:
                with self._lock:
                    self.items = items
                self._loaded = True
                try:
                    with open(ITEMS_CACHE, "w", encoding="utf-8") as f:
                        json.dump({"ts": time.time(), "items": items}, f, ensure_ascii=False)
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
                    log(f"База предметов: {len(self.items)} (просроченный кэш)")
                    return True
        except Exception:
            pass
        return False

    # ── поиск предмета ─────────────────────────────────────────────────
    def match(self, ocr_name):
        ocr_name = (ocr_name or "").strip()
        key = normalize_name(ocr_name)
        if not key or not self.items:
            return None
        hit = self.items.get(key)
        if hit:
            return dict(hit, score=1.0, query=key)
        # вариант без «blueprint»/«set» на конце
        variants = [key]
        for suffix in (" blueprint", " set", " prime blueprint", " blueprint set"):
            if key.endswith(suffix):
                variants.append(key[: -len(suffix)].strip())
        for v in variants:
            hit = self.items.get(v)
            if hit:
                return dict(hit, score=0.95, query=key)
        try:
            names = list(self.items.keys())
            cands = difflib.get_close_matches(key, names, n=3, cutoff=0.66)
        except Exception:
            cands = []
        best, best_score = None, 0.0
        for c in cands:
            score = difflib.SequenceMatcher(None, key, c).ratio()
            # лёгкий бонус, если совпали ключевые слова (prime + тип детали)
            key_words = set(key.split())
            c_words = set(c.split())
            if key_words and c_words:
                score += 0.10 * len(key_words & c_words) / max(1, len(key_words))
            if score > best_score:
                best, best_score = c, score
        # защита от «похожего, но другого» предмета: все значимые слова
        # запроса должны присутствовать в найденном названии
        if best and best_score >= 0.70:
            missing = [w for w in key.split()
                       if len(w) > 2 and w not in set(best.split())]
            if missing and best_score < 0.90:
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

    def lookup(self, ocr_name):
        """Полная информация по распознанному названию."""
        out = {
            "name": ocr_name, "slug": None, "market_name": None,
            "price": None, "best_sell": None, "best_buy": None,
            "sell_online": 0, "buy_online": 0, "ducats": None,
            "tradable": None, "matched": False, "error": None,
        }
        if not self.load_items():
            out["error"] = "нет списка предметов"
            return out
        m = self.match(ocr_name)
        if not m:
            out["error"] = "не найден на рынке"
            return out
        out.update({"slug": m["slug"], "market_name": m["name"], "matched": True})

        detail = self._detail(m["slug"])
        if detail:
            out["ducats"] = detail.get("ducats")
            out["tradable"] = detail.get("tradable")

        info = self._orders(m["slug"])
        out["best_sell"] = info.get("best_sell")
        out["best_buy"] = info.get("best_buy")
        out["sell_online"] = info.get("sell_online", 0)
        out["buy_online"] = info.get("buy_online", 0)
        out["sell_offline"] = info.get("sell_offline", False)
        out["price"] = out["best_sell"] if out["best_sell"] is not None else out["best_buy"]
        return out


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
    contrast — CLAHE + 3x (страховка для тёмных/шумных кадров).
    """
    import cv2
    import numpy as np
    global _SHARPEN
    if _SHARPEN is None:
        _SHARPEN = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]], dtype=np.float32)
    gray = _to_gray(img)
    if variant == "contrast":
        gray = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(gray)
        scale = 3.0
    else:
        scale = 2.0
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
                torch.set_num_threads(max(1, min(os.cpu_count() or 4, 8)))
            except Exception:
                pass
        self.reader = easyocr.Reader(["en"], gpu=use_gpu,
                                     model_storage_directory=models_dir())
        log(f"EasyOCR готов за {time.time()-t:.1f} с")
        try:
            import numpy as np
            warm = np.zeros((40, 160, 3), dtype=np.uint8)
            self.reader.readtext(warm, paragraph=False, canvas_size=400, mag_ratio=1.0)
        except Exception:
            pass

    def read_proc(self, proc):
        try:
            return self.reader.readtext(proc, paragraph=False, mag_ratio=1.0,
                                        canvas_size=1600, width_ths=0.7)
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
    t = (text or "").upper()
    letters = re.sub(r"[^A-Z]", "", t)
    digits = re.findall(r"\d+", t)
    kind = None
    if len(letters) >= 3:
        ratios = {k: difflib.SequenceMatcher(None, letters, k).ratio()
                  for k in ("CRAFTED", "OWNED")}
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
            score -= 5.0          # пустая карточка — сильный минус
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
    text = re.sub(r"[^A-Za-z0-9'\- ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _cards_from_words(words, cfg):
    """Превращает слова в список карточек (1..4) с названиями."""
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
    elif not words:
        return [], "  На экране не найден текст наград"
    else:
        best = None
        for n in (1, 2, 3, 4):
            s, lefts, groups = score_layout(n, words, cfg)
            if best is None or s > best[0]:
                best = (s, n, lefts, groups)
        s, n_best, lefts, groups = best
        if s < 0.4:
            return [], f"  Не удалось разобрать раскладку (score={s:.2f})"
        note = (f"  Карточек: {n_best}  (score={s:.2f}, "
                f"lefts={[int(l) for l in lefts]})")

    cards = []
    for i in range(n_best):
        name = join_words(groups.get(i, []))
        if not name:
            continue
        cards.append({"left": float(lefts[i]),
                      "top": float(cfg["card_top"]),
                      "name": name})
    if not cards:
        return [], "  Текст найден, но не привязался к карточкам"
    return cards, note


def detect_cards(base_img, cfg, ocr, debug=False, matcher=None):
    """
    Возвращает список карточек [{'left', 'top', 'name'}] для 1–4 наград.

    Делает один проход OCR, а если названия плохо узнаются — второй,
    с другим контрастом; берёт тот вариант, где больше совпадений с
    базой предметов (matcher — функция name -> score 0..1).
    """
    import cv2

    band = dict(cfg["scan_band"])
    x0 = max(0, int(band["left"]))
    y0 = max(0, int(band["top"]))
    x1 = min(base_img.shape[1], int(band["left"] + band["width"]))
    y1 = min(base_img.shape[0], int(band["top"] + band["height"]))
    crop = base_img[y0:y1, x0:x1]
    if crop.size == 0:
        return []

    name_top = cfg["card_top"] + cfg["name_offset_y"]
    name_bot = name_top + cfg["name_height"]

    best = None      # (rank, cards, variant, proc, words)
    for idx, variant in enumerate(("soft", "contrast")):
        proc, scale = prepare_ocr(crop, variant)
        res = ocr.read_proc(proc)
        words = _word_boxes(res, {"left": x0, "top": y0}, scale)
        words = [w for w in words if name_top - 30 <= w["cy"] <= name_bot + 20]
        cards, note = _cards_from_words(words, cfg)

        matched, quality = 0, 0.0
        if matcher and cards:
            for c in cards:
                s = matcher(c["name"]) or 0.0
                if s >= 0.80:
                    matched += 1
                    quality += s
        rank = (matched, round(quality, 3), len(cards), -idx)
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
    if note:
        log(note)
    if not cards:
        return []
    if matcher:
        log(f"  OCR: {variant}, совпало {rank[0]}/{len(cards)}")
    return cards


def read_owned(full_img, sx, sy, card, cfg, ocr):
    """Метка Crafted / N Owned в шапке карточки (читается с полного разрешения).

    Читаем всю верхнюю полосу карточки и без allowlist: на маленьком шрифте
    со свечением он только мешает (проверено на реальном кадре с 3 наградами).
    """
    import cv2
    x0 = max(0, int(round(card["left"] * sx)) - 2)
    x1 = min(full_img.shape[1],
             int(round((card["left"] + cfg["card_width"]) * sx)))
    y0 = max(0, int(round(card["top"] * sy)))
    y1 = min(full_img.shape[0],
             int(round((card["top"] + cfg.get("owned_strip_height", 40)) * sy)))
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
    from PyQt5.QtCore import Qt, QTimer, QObject, pyqtSignal
    from PyQt5.QtGui import QFont
    from PyQt5.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                                 QVBoxLayout, QWidget)

    class _ItemOverlay(QWidget):
        def __init__(self, cfg):
            super().__init__()
            self.cfg = cfg
            self.card = None
            self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint |
                                Qt.Tool | Qt.WindowTransparentForInput |
                                Qt.WindowDoesNotAcceptFocus)
            self.setAttribute(Qt.WA_TranslucentBackground)
            self.setAttribute(Qt.WA_ShowWithoutActivating)
            outer = QVBoxLayout(self)
            outer.setContentsMargins(0, 0, 0, 0)
            self.frame = QFrame()
            self.frame.setObjectName("card")
            self.frame.setStyleSheet("""
                QFrame#card { background-color: rgba(9, 13, 21, 236);
                              border: 1px solid #2b3d52; border-radius: 7px; }
                QLabel { border: none; background: transparent; color: #e8eef7;
                         font-family: 'Segoe UI', Arial, sans-serif; }
            """)
            lay = QVBoxLayout(self.frame)
            lay.setContentsMargins(10, 7, 10, 7)
            lay.setSpacing(1)

            top = QHBoxLayout()
            top.setContentsMargins(0, 0, 0, 0)
            top.setSpacing(4)
            self.lbl_owned = QLabel("0 OWNED")
            self.lbl_owned.setStyleSheet("color:#7b8798; font-size:11px; font-weight:600;")
            self.lbl_ducats = QLabel("")
            self.lbl_ducats.setStyleSheet("color:#8fa1b8; font-size:11px;")
            self.lbl_ducats.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            top.addWidget(self.lbl_owned)
            top.addStretch(1)
            top.addWidget(self.lbl_ducats)

            self.lbl_price = QLabel("…")
            self.lbl_price.setStyleSheet("font-size:23px; font-weight:700;")
            self.lbl_market = QLabel("S: —   B: —")
            self.lbl_market.setStyleSheet("color:#b7c2d0; font-size:11px;")
            self.lbl_name = QLabel("")
            self.lbl_name.setStyleSheet("color:#6f7b8c; font-size:10px;")
            self.lbl_name.setMaximumHeight(13)

            lay.addLayout(top)
            lay.addWidget(self.lbl_price)
            lay.addWidget(self.lbl_market)
            lay.addWidget(self.lbl_name)

            outer.addWidget(self.frame)
            fs = float(cfg.get("font_scale", 1.0))
            self.setFixedSize(int(240 * fs), int(104 * fs))

        def set_data(self, d):
            if not d:
                self.hide()
                return
            n_owned = d.get("owned_n") or 0
            owned_txt = d.get("owned") or ""
            # если метку не разобрали — не врём про «0 OWNED», просто не показываем
            self.lbl_owned.setVisible(bool(owned_txt))
            self.lbl_owned.setText(owned_txt)
            color = "#5ce08a" if (d.get("owned_kind") == "CRAFTED" or n_owned) else "#7b8798"
            self.lbl_owned.setStyleSheet(
                f"color:{color}; font-size:11px; font-weight:600;")

            du = d.get("ducats")
            if d.get("pending"):
                self.lbl_ducats.setText("")
            elif d.get("matched") and du:
                self.lbl_ducats.setText(f"{du} duc")
            elif d.get("matched") and d.get("tradable") is False:
                self.lbl_ducats.setText("не торгуется")
            elif d.get("error"):
                self.lbl_ducats.setText(d["error"])
            else:
                self.lbl_ducats.setText("")

            price = d.get("price")
            if d.get("pending"):
                self.lbl_price.setText("<span style='color:#8fa1b8;'>P: …</span>")
            elif price:
                tilde = "~" if d.get("sell_offline") else ""
                self.lbl_price.setText(f"<span style='color:#4fb3ff;'>P:</span> "
                                       f"<span style='color:{price_color(price)};'>"
                                       f"{tilde}{int(price)}</span>")
            elif d.get("matched"):
                self.lbl_price.setText("<span style='color:#6c7789;'>нет ордеров</span>")
            else:
                self.lbl_price.setText("<span style='color:#6c7789;'>нет на рынке</span>")

            if d.get("pending"):
                self.lbl_market.setText(
                    "<span style='color:#6f7b8c;'>запрашиваю цены…</span>")
            elif d.get("matched"):
                bs = fmt_plat(d.get("best_sell")).replace("p", "")
                bb = fmt_plat(d.get("best_buy")).replace("p", "")
                if d.get("sell_offline"):
                    bs += "~"
                online = d.get("sell_online", 0)
                online_txt = f"{online} online" if online else "нет онлайн"
                self.lbl_market.setText(
                    f"<span style='color:#ffd75e;'>S:</span> {bs} "
                    f"<span style='color:#8fa1b8;'>·</span> "
                    f"<span style='color:#5cdcff;'>B:</span> {bb} "
                    f"<span style='color:#6f7b8c;'>·</span> "
                    f"<span style='color:#94a3b5;'>{online_txt}</span>")
            else:
                self.lbl_market.setText("<span style='color:#5d6777;'>цена не найдена</span>")

            name = (d.get("market_name") or d.get("name") or "").strip()
            if len(name) > 30:
                name = name[:29] + "…"
            self.lbl_name.setText(name)
            self.show()

    class _SummaryOverlay(QWidget):
        def __init__(self, cfg):
            super().__init__()
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
            self.lbl_total.setStyleSheet("font-size:21px; font-weight:700; color:#ffd75e;")
            self.lbl_best = QLabel(" ")
            self.lbl_best.setStyleSheet("font-size:11px; color:#9fb0c4;")
            lay.addWidget(self.lbl_total)
            lay.addWidget(self.lbl_best)
            outer.addWidget(f)
            fs = float(cfg.get("font_scale", 1.0))
            self.setFixedSize(int(380 * fs), int(66 * fs))

        def set_summary(self, data, final=False):
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
                    self.lbl_total.setText(
                        f"<span style='color:#ffd75e;'>Итого: {int(total)}p</span>")
                    self.lbl_best.setText(f"дороже всех: {best[1][:40]} ({int(best[0])}p)"
                                          if best else " ")
                    self.show()
                else:
                    self.lbl_total.setText("Итого: —")
                    self.lbl_best.setText("цены не найдены")
                    self.show()
            else:
                self.lbl_total.setText("Итого: …")
                self.lbl_best.setText("подгружаю цены…")
                self.show()

    class _Manager(QObject):
        updated = pyqtSignal(list)
        done = pyqtSignal()

        def __init__(self, cfg, geom):
            super().__init__()
            self.cfg = cfg
            self.geom = geom
            self.cards = [_ItemOverlay(cfg) for _ in range(4)]
            self.summary = _SummaryOverlay(cfg)
            self.timer = QTimer(self)
            self.timer.setSingleShot(True)
            self.timer.timeout.connect(self.hide_all)
            self.updated.connect(self._apply)

        def place(self, widget, x_base, y_base, above_base=None):
            """x_base/y_base — позиция под карточкой, above_base — над карточкой."""
            g = self.geom
            w, h = widget.width(), widget.height()
            x = int(x_base * g["sx"])
            y = int(y_base * g["sy"])
            if y + h > g["height"] and above_base is not None:
                y = int(above_base * g["sy"]) - h - int(6 * g["sy"])
            x = max(0, min(x, g["width"] - w))
            y = max(0, min(y, g["height"] - h))
            widget.move(x, y)

        def _apply(self, data):
            for w in self.cards:
                w.hide()
            shown = 0
            for i, d in enumerate(data or []):
                if i >= len(self.cards) or not d:
                    continue
                d = dict(d)
                if d.get("card_left") is None:
                    d["card_left"] = self.cfg["cards_center_x"]
                self.cards[i].set_data(d)
                left = d["card_left"]
                below = self.cfg["card_top"] + self.cfg["card_height"] + \
                    self.cfg["overlay_offset_y"]
                self.place(self.cards[i], left, below,
                           above_base=self.cfg["card_top"] - 6)
                shown += 1
            if shown:
                self.summary.set_summary(data, final=any(isinstance(d.get("price"), (int, float))
                                                         for d in data if d))
                cx = self.cfg["cards_center_x"]
                self.summary.move(max(0, int(cx * self.geom["sx"] - self.summary.width() / 2)),
                                  int(84 * self.geom["sy"]))
                self.summary.raise_()
            else:
                self.summary.hide()
            self.timer.start(int(self.cfg.get("overlay_duration_ms", 12000)))

        def hide_all(self):
            for w in self.cards:
                w.hide()
            self.summary.hide()

    return _ItemOverlay, _SummaryOverlay, _Manager, Qt


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
    import cv2
    bh, bw = int(cfg["base_height"]), int(cfg["base_width"])
    if img.shape[1] == bw and img.shape[0] == bh:
        return img
    interp = cv2.INTER_AREA if (img.shape[1] > bw or img.shape[0] > bh) else cv2.INTER_CUBIC
    return cv2.resize(img, (bw, bh), interpolation=interp)


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

    def request_scan(self):
        with self._lock:
            if self.busy:
                log("Скан уже выполняется…")
                return
            self.busy = True
            self.generation += 1
            gen = self.generation
        threading.Thread(target=self._run, args=(gen,), daemon=True).start()

    def _run(self, gen):
        t0 = time.time()
        log("\n" + "─" * 60)
        log(f"Скан {datetime.datetime.now().strftime('%H:%M:%S')}")
        try:
            # экран наград может быть ещё в анимации — пробуем несколько раз
            attempts = max(1, int(self.cfg.get("scan_retries", 2)) + 1)
            delay = max(0, int(self.cfg.get("scan_retry_delay_ms", 350))) / 1000.0
            self.api.load_items()
            matcher = lambda n: (self.api.match(n) or {}).get("score", 0.0)  # noqa: E731
            cards = []
            for attempt in range(attempts):
                full, mon = grab(self.cfg)
                base = to_base(full, self.cfg)
                sx = mon["width"] / float(self.cfg["base_width"])
                sy = mon["height"] / float(self.cfg["base_height"])
                if self.cfg.get("show_debug"):
                    try:
                        imwrite_unicode(os.path.join(self.cfg["debug_dir"],
                                                     "last_screen.png"), base)
                    except Exception:
                        pass
                cards = detect_cards(base, self.cfg, self.ocr,
                                     debug=self.cfg.get("show_debug"),
                                     matcher=matcher)
                if cards or attempt == attempts - 1:
                    break
                log(f"  Карточек не видно — переснимаю через {int(delay * 1000)} мс")
                time.sleep(delay)

            # ── фаза 1: имена сразу ────────────────────────────────────
            preliminary = []
            for c in cards:
                n, kind, _raw = read_owned(full, sx, sy, c, self.cfg, self.ocr)
                if kind == "CRAFTED":
                    label = "CRAFTED"
                elif n:
                    label = f"{n} OWNED"
                else:
                    label = ""
                log(f"  · {c['name']!r}   {label or '—'}")
                preliminary.append({
                    "name": c["name"], "owned": label, "owned_n": n or 0,
                    "owned_kind": kind, "card_left": c["left"],
                    "price": None, "pending": True,
                    "matched": False, "sell_online": 0, "best_sell": None,
                    "best_buy": None, "ducats": None, "error": None,
                })
            if gen == self.generation:
                self.mgr.updated.emit(preliminary)
            if not preliminary:
                log("  Предметы не найдены — проверьте координаты (--calibrate)")
                self.mgr.updated.emit([])
                return

            # ── фаза 2: цены параллельно ──────────────────────────────
            results = [None] * len(preliminary)
            t_api = time.time()
            workers = max(1, min(int(self.cfg.get("workers", 8)), 8))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(self.api.lookup, p["name"]): i
                        for i, p in enumerate(preliminary)}
                for fut in as_completed(futs):
                    i = futs[fut]
                    try:
                        results[i] = fut.result()
                    except Exception as e:
                        log(f"  API: {preliminary[i]['name']}: {e}")
                        results[i] = {"name": preliminary[i]["name"],
                                      "price": None, "matched": False,
                                      "error": "ошибка сети"}
            log(f"  API: {time.time() - t_api:.1f} с")

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

            self.mgr.updated.emit(final)
            self._write_report(final)
            self._maybe_beep(final)
            log(f"  Готово за {time.time() - t0:.1f} с")
        except Exception as e:
            log(f"ОШИБКА СКАНА: {type(e).__name__}: {e}")
            traceback.print_exc()
        finally:
            with self._lock:
                self.busy = False
            log("─" * 60 + "\n")

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
    mon = monitor_info(cfg)
    return {
        "sx": mon["width"] / float(cfg["base_width"]),
        "sy": mon["height"] / float(cfg["base_height"]),
        "width": mon["width"],
        "height": mon["height"],
        "monitor": mon,
    }


def run_gui(cfg):
    # ВАЖНО: EasyOCR/torch грузим ДО PyQt5. Если Qt инициализируется первым,
    # на Windows torch может не суметь загрузить c10.dll (WinError 1114).
    ocr = OcrEngine(cfg)
    _, _, Manager, Qt = build_overlays()
    from PyQt5.QtWidgets import QApplication
    import keyboard

    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    geom = make_geometry(cfg)
    manager = Manager(cfg, geom)
    api = MarketAPI(cfg)
    api.load_items()
    scanner = Scanner(cfg, ocr, api, manager, geom)

    keyboard.add_hotkey(cfg.get("hotkey_scan", "f8"), scanner.request_scan)
    keyboard.add_hotkey(cfg.get("hotkey_hide", "f9"), manager.hide_all)

    log("=" * 60)
    log(f"Warframe Price Overlay {APP_VERSION} — готово.")
    log(f"{cfg.get('hotkey_scan', 'f8').upper()} — скан, "
        f"{cfg.get('hotkey_hide', 'f9').upper()} — скрыть плашки.")
    log(f"Монитор: {geom['width']}x{geom['height']}  "
        f"(масштаб {geom['sx']:.2f}x{geom['sy']:.2f} от 1920x1080)")
    log("=" * 60)
    try:
        app.exec_()
    except KeyboardInterrupt:
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
    p = argparse.ArgumentParser(prog="WarframePriceOverlay",
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


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = parse_args(argv)
    enable_dpi_awareness()
    cfg = load_config(args.config or CONFIG_PATH)
    ensure_dirs(cfg)
    if args.calibrate:
        return run_calibrate(cfg)
    if args.selftest:
        return run_selftest(cfg, args.selftest)
    if args.test_api:
        return run_test_api(cfg, args.test_api)
    try:
        run_gui(cfg)
        return 0
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    code = main()
    if code:
        try:
            input("\n[ПРОЦЕСС ЗАВЕРШЕН] Enter, чтобы закрыть окно...")
        except Exception:
            pass
