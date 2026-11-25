# -*- coding: utf-8 -*-
"""Геометрия скана наград: ники, бонусная строка, панели (t156)."""
from reward_geometry import card_lefts


def _nick_bottom(base_img, left, card_bottom, width=242):
    """t153: низ строки ников под карточкой (яркий текст в полосе +70)."""
    try:
        import cv2
        import numpy as np
        x0 = max(0, int(left))
        x1 = min(base_img.shape[1], int(left) + int(width))
        y0 = max(0, int(card_bottom) + 2)
        y1 = min(base_img.shape[0], int(card_bottom) + 82)
        crop = base_img[y0:y1, x0:x1]
        if crop.size == 0:
            return int(card_bottom) + 52
        b, g, r = cv2.split(crop)
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        mask = ((gray > 105) & (r > 90) & (r >= b - 10)).astype(np.uint8)
        counts = mask.sum(axis=1)
        rows = [i for i, count in enumerate(counts) if count >= 4]
        if not rows:
            return int(card_bottom) + 52
        return y0 + max(rows)
    except Exception:
        return int(card_bottom) + 52


def _bonus_top(base_img, card_bottom, cfg=None):
    """1.0.9: верх строки «Бесконечный Бонус …» (жёлтый текст под никами)."""
    try:
        import cv2
        import numpy as np
        h, w = base_img.shape[:2]
        y0 = max(0, int(card_bottom) + 120)
        y1 = min(h, int(card_bottom) + 380)
        if y1 - y0 < 10:
            return None
        crop = base_img[y0:y1, 0:w]
        b, g, r = cv2.split(crop)
        mask = ((r > 150) & (g > 120) & (b < 130)
                & (r.astype(np.int16) - b.astype(np.int16) > 60))
        counts = mask.sum(axis=1)
        rows = [i for i, count in enumerate(counts) if count >= 120]
        if not rows:
            return None
        return y0 + rows[0]
    except Exception:
        return None


def _panel_count(base_img, cfg, lefts_fn=None):
    """t149: тема-независимая занятость канонических слотов (Canny)."""
    try:
        import cv2
        gray = cv2.cvtColor(base_img, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 40, 100)
        top = int(cfg.get("card_top") or 224)
        width = int(cfg.get("card_width") or 242)
        height = int(cfg.get("card_height") or 235)
        for n in (4, 3, 2, 1):
            if lefts_fn is not None:
                lefts = [int(round(x)) for x in lefts_fn(n)]
            else:
                lefts = [int(round(x)) for x in
                         card_lefts(n, cfg.get("ui_scale") or 1.0)]
            scores = []
            for x in lefts:
                if x < 0 or x + width > gray.shape[1]:
                    scores.append(0.0)
                    continue
                roi_w = max(40, int(width * 0.75))
                roi = edges[top:top + height, x:x + roi_w]
                scores.append(float(roi.mean()) if roi.size else 0.0)
            if scores and min(scores) >= 20.0:
                return n, lefts
    except Exception:
        pass
    return 0, []
