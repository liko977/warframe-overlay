# -*- coding: utf-8 -*-
"""Программные иконки оверлея (без файлов и внешних зависимостей).

Шестерёнка настроек рисуется QPainter: 12 зубьев, кольцо и внутреннее
отверстие. Цвет — из палитры ui_theme; Qt импортируется лениво.
"""

try:
    import ui_theme as theme
except Exception:                    # pragma: no cover
    theme = None


def _palette_color(name, fallback):
    if theme is not None:
        try:
            return str(theme.PALETTE.get(name) or fallback)
        except Exception:
            pass
    return fallback


def gear_pixmap(size=22, color=None):
    """QPixmap с шестерёнкой (12 зубьев + отверстие), прозрачный фон."""
    from PyQt5.QtCore import QRectF, Qt
    from PyQt5.QtGui import QColor, QPainter, QPixmap

    size = max(8, int(size or 22))
    color = QColor(color or _palette_color("accent", "#4aa8ff"))
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        center = size / 2.0
        outer = size * 0.38
        tooth_w = max(1.4, size * 0.10)
        tooth_h = max(2.0, size * 0.16)
        painter.save()
        painter.translate(center, center)
        for index in range(12):
            painter.save()
            painter.rotate(index * 30.0)
            painter.drawRoundedRect(
                QRectF(-tooth_w / 2.0, -outer - tooth_h * 0.75,
                       tooth_w, tooth_h), 0.8, 0.8)
            painter.restore()
        painter.restore()
        painter.drawEllipse(QRectF(center - outer, center - outer,
                                   outer * 2, outer * 2))
        # внутреннее отверстие — вырезаем до прозрачности
        hole = outer * 0.42
        painter.setCompositionMode(QPainter.CompositionMode_Clear)
        painter.drawEllipse(QRectF(center - hole, center - hole,
                                   hole * 2, hole * 2))
    finally:
        painter.end()
    return pix


def gear_icon(size=22, color=None):
    """QIcon шестерёнки (16×16 / 22×22 — стандартные размеры кнопки)."""
    from PyQt5.QtGui import QIcon
    icon = QIcon()
    for px in sorted({int(size or 22), 16, 22}):
        icon.addPixmap(gear_pixmap(px, color))
    return icon


__all__ = ["gear_pixmap", "gear_icon"]
