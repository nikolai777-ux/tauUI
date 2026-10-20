"""tauUI's logo: the user's picture (assets/tauUI.png) when it is there, else a small Greek tau and "UI" on a dark
rounded square, drawn with the system's fonts."""

import os

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPixmap

APP_NAME = "tauUI"
BACKGROUND = QColor(28, 36, 48)
TAU = QColor(120, 180, 255)
UI = QColor(232, 232, 232)


def _fit(font, text, width, height, start):
    """Set the font's pixel size to `start`, or smaller until the text fits width x height; returns the size."""
    from PySide6.QtGui import QFontMetricsF
    px = max(5, round(start))
    while px > 5:
        font.setPixelSize(px)
        r = QFontMetricsF(font).tightBoundingRect(text)
        if r.width() <= width and r.height() <= height:
            break
        px -= 1
    font.setPixelSize(px)
    return px


PICTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "tauUI.png")


def pixmap(size):
    if os.path.exists(PICTURE):
        pm = QPixmap(PICTURE)
        if not pm.isNull():
            return pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return drawn(size)


def drawn(size):
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)
    r = QRectF(0, 0, size, size)
    path = QPainterPath()
    path.addRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), size * 0.2, size * 0.2)
    p.fillPath(path, BACKGROUND)
    # a small τ, then "UI" (0.8 of the largest that fits); both stand on one line (their bottoms flush), the pair
    # centred in the square
    from PySide6.QtGui import QFontMetricsF
    ui = QFont("Segoe UI", 1, QFont.DemiBold)
    ui_px = _fit(ui, "UI", size * 0.56, size * 0.72, size * 0.72 * 0.8)
    tau = QFont("Cambria", 1)
    tau.setItalic(True)
    _fit(tau, "τ", size * 0.2, size * 0.5, ui_px * 0.7)
    ui_box, tau_box = QFontMetricsF(ui).tightBoundingRect("UI"), QFontMetricsF(tau).tightBoundingRect("τ")
    gap = size * 0.03
    left = (size - (tau_box.width() + gap + ui_box.width())) / 2
    bottom = (size + ui_box.height()) / 2  # the line both stand on
    p.setFont(tau)
    p.setPen(TAU)
    p.drawText(QPointF(left - tau_box.left(), bottom - tau_box.bottom()), "τ")
    p.setFont(ui)
    p.setPen(UI)
    p.drawText(QPointF(left + tau_box.width() + gap - ui_box.left(), bottom - ui_box.bottom()), "UI")
    p.end()
    return pm


def icon():
    ic = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        ic.addPixmap(pixmap(size))
    return ic
