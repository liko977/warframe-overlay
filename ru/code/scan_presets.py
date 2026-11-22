# -*- coding: utf-8 -*-
"""1.0.3: готовые пресеты раскладки скана (без автокалибровки).

Единственный источник геометрии скана — ``cfg["scan_preset"]``.
Сохранённый ``scan_calibration`` игнорируется (совместимость старых конфигов).
"""
import math

BASE_W = 1920.0
BASE_H = 1080.0
CANONICAL_LEFT = 476.0
CENTER_PITCH = 242.0

PRESETS = (
    ("auto", "Авто по разрешению (WFInfo)"),
    ("1920x1080_center", "1920×1080 — центр"),
    ("2560x1440_center", "2560×1440 — центр"),
    ("3840x2160_center", "3840×2160 — центр"),
    ("2560x1080_center", "2560×1080 — центр (21:9)"),
    ("1920x1200_center", "1920×1200 — центр (16:10)"),
    ("1920x1080_bottom", "1920×1080 — НИЗ экрана"),
    ("2560x1440_bottom", "2560×1440 — НИЗ экрана"),
    ("custom", "Пользовательская"),
)

BOTTOM = {"lefts": [320.0, 540.0, 760.0, 980.0], "card_width": 215.0,
          "card_pitch": 220.0, "card_top": 745.0, "card_height": 90.0,
          "name_offset_y": 8.0, "name_height": 40.0}


def preset_label(preset_id):
    for pid, label in PRESETS:
        if pid == preset_id:
            return label
    return "Авто по разрешению (WFInfo)"


def canonical_lefts(count, start=None):
    count = max(1, min(4, int(count or 4)))
    first = 476.0 if start is None else float(start)
    first = first + (4 - count) * (CENTER_PITCH / 2.0)
    return [round(first + CENTER_PITCH * i, 1) for i in range(count)]


def _scaled_center(scale, count=4):
    return {
        "lefts": [round(x * scale, 1) for x in canonical_lefts(count)],
        "card_width": round(242.0 * scale, 1),
        "card_pitch": round(242.0 * scale, 1),
        "card_top": round(224.0 * scale, 1),
        "card_height": round(235.0 * scale, 1),
        "name_offset_y": round(187.0 * scale, 1),
        "name_height": round(48.0 * scale, 1),
    }


def scan_geometry(cfg=None, width=1920, height=1080, count=4):
    """Геометрия по ``cfg['scan_preset']``. Возвращает dict."""
    cfg = cfg or {}
    pid = str(cfg.get("scan_preset") or "auto")
    if pid not in dict(PRESETS):
        pid = "auto"
    width = float(width or 1920)
    height = float(height or 1080)
    if pid == "custom":
        custom = dict(cfg.get("scan_custom") or {})
        geometry = _scaled_center(1.0, count)
        for key in ("lefts", "card_width", "card_pitch", "card_top",
                    "card_height", "name_offset_y", "name_height"):
            if custom.get(key) is not None:
                geometry[key] = custom[key]
        geometry["lefts"] = [float(x) for x in geometry["lefts"]][:count]
        return geometry
    if pid == "auto":
        scale = min(width / BASE_W, height / BASE_H)
        geometry = _scaled_center(scale, count)
    elif pid.endswith("_bottom"):
        scale = 1.0 if pid.startswith("1920x1080") else (4.0 / 3.0)
        geometry = {
            "lefts": [round(x * scale, 1) for x in BOTTOM["lefts"]][
                :max(1, min(4, count))],
            "card_width": round(BOTTOM["card_width"] * scale, 1),
            "card_pitch": round(BOTTOM["card_pitch"] * scale, 1),
            "card_top": round(BOTTOM["card_top"] * scale, 1),
            "card_height": round(BOTTOM["card_height"] * scale, 1),
            "name_offset_y": round(BOTTOM["name_offset_y"] * scale, 1),
            "name_height": round(BOTTOM["name_height"] * scale, 1),
        }
    else:
        preset_w = float(pid.split("x")[0])
        scale = preset_w / BASE_W
        geometry = _scaled_center(scale, count)
    geometry["preset"] = pid
    geometry["preset_label"] = preset_label(pid)
    geometry["screen"] = (int(width), int(height))
    return geometry


def preset_log_line(geometry):
    lefts = ",".join(str(int(round(float(x))))
                     for x in (geometry.get("lefts") or []))
    return (f"Раскладка: {graphics(geometry)} "
            f"(lefts=[{lefts}] pitch={int(round(float(geometry.get('card_pitch') or 0)))} "
            f"top={int(round(float(geometry.get('card_top') or 0)))})")


def graphics(geometry):
    width, height = geometry.get("screen") or (1920, 1080)
    return f"{int(width)}×{int(height)} — {geometry.get('preset_label')}"


def application_line(cfg, geometry):
    """Строка лога применения пресета (t84)."""
    lefts = ",".join(str(int(round(float(x))))
                     for x in (geometry.get("lefts") or []))
    center = cfg.get("cards_center_x")
    pitch = float(geometry.get("card_pitch") or 242)
    width = float(geometry.get("card_width") or 242)
    n = len(geometry.get("lefts") or [])
    total = n * width + (n - 1) * (pitch - width) if n else 0
    first = float((geometry.get("lefts") or [0])[0])
    grid = [int(round(first + i * pitch)) for i in range(n)]
    return (f"Раскладка применена: preset={cfg.get('scan_preset', 'auto')} "
            f"lefts=[{lefts}] center_x={float(center or 0):.1f} "
            f"(card_lefts → [{','.join(str(x) for x in grid)}])")


def ensure_preset(cfg):
    """Нормализовать cfg['scan_preset']; старую калибровку игнорируем."""
    cfg = cfg if isinstance(cfg, dict) else {}
    pid = str(cfg.get("scan_preset") or "auto")
    if pid not in dict(PRESETS):
        pid = "auto"
    cfg["scan_preset"] = pid
    return pid


def apply_geometry(cfg, geometry):
    """Записать геометрию в cfg (для скана/плашек) без калибровки."""
    cfg = cfg if isinstance(cfg, dict) else {}
    lefts = [float(x) for x in (geometry.get("lefts") or [])]
    if not lefts:
        return cfg
    cfg["card_width"] = float(geometry.get("card_width") or 242)
    cfg["card_pitch"] = float(geometry.get("card_pitch") or 242)
    cfg["card_height"] = float(geometry.get("card_height") or 235)
    # t84: центр ряда = lefts[0] + полная ширина ряда / 2
    n = len(lefts)
    width = float(geometry.get("card_width") or 242)
    pitch = float(geometry.get("card_pitch") or 242)
    total = n * width + (n - 1) * (pitch - width)
    cfg["cards_center_x"] = lefts[0] + total / 2.0
    cfg["card_top"] = float(geometry.get("card_top") or 224)
    cfg["name_offset_y"] = float(geometry.get("name_offset_y") or 187)
    cfg["name_height"] = float(geometry.get("name_height") or 48)
    band_left = max(0, min(lefts) - 60)
    band_right = min(1920, max(lefts) + cfg["card_width"] + 60)
    band_top = max(0, cfg["card_top"] + cfg["name_offset_y"] - 40)
    band_h = max(20, cfg["name_height"] + 80)
    cfg["scan_band"] = {"left": int(round(band_left)),
                        "top": int(round(band_top)),
                        "width": int(round(band_right - band_left)),
                        "height": int(round(band_h))}
    return cfg


__all__ = ["PRESETS", "BOTTOM", "preset_label", "scan_geometry",
           "preset_log_line", "application_line", "ensure_preset",
           "apply_geometry",
           "canonical_lefts"]
