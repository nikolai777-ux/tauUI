"""Node body shapes (Houdini-style). Each shape is a function (QRectF) -> QPainterPath,
plus optional decorative inner lines. Pick one per node type with `shape_name`."""

import json
import math
import os
from functools import lru_cache

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt
from PySide6.QtGui import QPainterPath, QPainterPathStroker, QPolygonF


def _poly(points):
    path = QPainterPath()
    path.addPolygon(QPolygonF([QPointF(x, y) for x, y in points]))
    path.closeSubpath()
    return path


def rect(r):
    p = QPainterPath()
    p.addRoundedRect(r, 6, 6)
    return p


def squared(r):
    p = QPainterPath()
    p.addRoundedRect(r, 1.5, 1.5)
    return p


def cigar(r):
    p = QPainterPath()
    p.addRoundedRect(r, r.height() / 2, r.height() / 2)
    return p


def oval(r):
    p = QPainterPath()
    p.addEllipse(r)
    return p


def bulge(r):
    """Convex top and bottom, rounded sides (Houdini's merge look)."""
    b = r.height() * 0.28
    x0, x1, y0, y1, c = r.left(), r.right(), r.top() + b / 2, r.bottom() - b / 2, 8
    p = QPainterPath(QPointF(x0 + c, y0))
    p.quadTo(QPointF(r.center().x(), y0 - b), QPointF(x1 - c, y0))
    p.quadTo(QPointF(x1, y0), QPointF(x1, y0 + c))
    p.lineTo(x1, y1 - c)
    p.quadTo(QPointF(x1, y1), QPointF(x1 - c, y1))
    p.quadTo(QPointF(r.center().x(), y1 + b), QPointF(x0 + c, y1))
    p.quadTo(QPointF(x0, y1), QPointF(x0, y1 - c))
    p.lineTo(x0, y0 + c)
    p.quadTo(QPointF(x0, y0), QPointF(x0 + c, y0))
    return p


def bulge_down(r):
    b = r.height() * 0.3
    x0, x1, y0, y1, c = r.left(), r.right(), r.top(), r.bottom() - b / 2, 5
    p = QPainterPath(QPointF(x0 + c, y0))
    p.lineTo(x1 - c, y0)
    p.quadTo(QPointF(x1, y0), QPointF(x1, y0 + c))
    p.lineTo(x1, y1 - c)
    p.quadTo(QPointF(x1, y1), QPointF(x1 - c, y1))
    p.quadTo(QPointF(r.center().x(), y1 + b), QPointF(x0 + c, y1))
    p.quadTo(QPointF(x0, y1), QPointF(x0, y1 - c))
    p.lineTo(x0, y0 + c)
    p.quadTo(QPointF(x0, y0), QPointF(x0 + c, y0))
    return p


def _tabbed(r, left):
    """A folder: body with a raised tab on one side of the top edge."""
    t = r.height() * 0.3           # tab height
    body = QRectF(r.left(), r.top() + t, r.width(), r.height() - t)
    tw = r.width() * 0.45
    tx = r.left() + 3 if left else r.right() - 3 - tw
    p = QPainterPath()
    p.addRoundedRect(body, 4, 4)
    tab = QPainterPath()
    tab.addRoundedRect(QRectF(tx, r.top(), tw, t + 6), 3, 3)
    return p.united(tab)


def tabbed_left(r):
    return _tabbed(r, True)


def tabbed_right(r):
    return _tabbed(r, False)


def chevron_down(r):
    d = r.height() * 0.3
    x0, x1, y0, y1 = r.left(), r.right(), r.top(), r.bottom()
    return _poly([(x0, y0), (x1, y0), (x1, y1 - d), (r.center().x(), y1), (x0, y1 - d)])


def chevron_up(r):
    d = r.height() * 0.3
    x0, x1, y0, y1 = r.left(), r.right(), r.top(), r.bottom()
    return _poly([(x0, y0 + d), (r.center().x(), y0), (x1, y0 + d), (x1, y1), (x0, y1)])


def trapezoid_down(r):
    i = r.width() * 0.12
    return _poly([(r.left(), r.top()), (r.right(), r.top()), (r.right() - i, r.bottom()), (r.left() + i, r.bottom())])


def trapezoid_up(r):
    i = r.width() * 0.12
    return _poly([(r.left() + i, r.top()), (r.right() - i, r.top()), (r.right(), r.bottom()), (r.left(), r.bottom())])


def clipped_left(r):
    c = r.height() * 0.35
    return _poly([(r.left() + c, r.top()), (r.right(), r.top()), (r.right(), r.bottom()),
                  (r.left() + c, r.bottom()), (r.left(), r.bottom() - c), (r.left(), r.top() + c)])


def clipped_right(r):
    c = r.height() * 0.35
    return _poly([(r.left(), r.top()), (r.right() - c, r.top()), (r.right(), r.top() + c),
                  (r.right(), r.bottom() - c), (r.right() - c, r.bottom()), (r.left(), r.bottom())])


def tilted(r):
    s = r.height() * 0.35
    return _poly([(r.left() + s, r.top()), (r.right(), r.top()), (r.right() - s, r.bottom()), (r.left(), r.bottom())])


def hexagon(r):
    s = r.height() * 0.45
    cy = r.center().y()
    return _poly([(r.left() + s, r.top()), (r.right() - s, r.top()), (r.right(), cy),
                  (r.right() - s, r.bottom()), (r.left() + s, r.bottom()), (r.left(), cy)])


def octagon(r):
    c = r.height() * 0.3
    return _poly([(r.left() + c, r.top()), (r.right() - c, r.top()), (r.right(), r.top() + c),
                  (r.right(), r.bottom() - c), (r.right() - c, r.bottom()), (r.left() + c, r.bottom()),
                  (r.left(), r.bottom() - c), (r.left(), r.top() + c)])


def wave(r):
    """Flag with a wavy bottom edge."""
    a = r.height() * 0.12
    y1 = r.bottom() - a
    p = QPainterPath(QPointF(r.left(), r.top() + 3))
    p.quadTo(QPointF(r.left(), r.top()), QPointF(r.left() + 3, r.top()))
    p.lineTo(r.right() - 3, r.top())
    p.quadTo(QPointF(r.right(), r.top()), QPointF(r.right(), r.top() + 3))
    p.lineTo(r.right(), y1)
    w = r.width()
    p.cubicTo(QPointF(r.right() - w * 0.25, y1 - 2 * a), QPointF(r.left() + w * 0.75, y1 + 3 * a),
              QPointF(r.center().x(), y1))
    p.cubicTo(QPointF(r.left() + w * 0.25, y1 - 3 * a), QPointF(r.left() + w * 0.25, y1 + 2 * a),
              QPointF(r.left(), y1))
    p.closeSubpath()
    return p


def ticket(r):
    """Rounded box with half-round notches on both sides."""
    p = QPainterPath()
    p.addRoundedRect(r, 5, 5)
    n = r.height() * 0.22
    notches = QPainterPath()
    notches.addEllipse(QPointF(r.left(), r.center().y()), n, n)
    notches.addEllipse(QPointF(r.right(), r.center().y()), n, n)
    return p.subtracted(notches)


def cloud(r):
    p = QPainterPath()
    p.addRoundedRect(r.adjusted(4, 4, -4, -4), 8, 8)
    k = 6
    for i in range(k):
        x = r.left() + 8 + (r.width() - 16) * i / (k - 1)
        for y in (r.top() + 6, r.bottom() - 6):
            bump = QPainterPath()
            bump.addEllipse(QPointF(x, y), 7, 6)
            p = p.united(bump)
    return p


def pointy(r):
    """Arrow pointing right."""
    s = r.height() * 0.5
    return _poly([(r.left(), r.top()), (r.right() - s, r.top()), (r.right(), r.center().y()),
                  (r.right() - s, r.bottom()), (r.left(), r.bottom())])


def star_burst(r):
    """Rounded body with a jagged 'burst' outline."""
    cx, cy = r.center().x(), r.center().y()
    pts = []
    n = 18
    for i in range(n * 2):
        a = math.pi * i / n
        k = 1.0 if i % 2 == 0 else 0.86
        pts.append((cx + math.cos(a) * r.width() / 2 * k, cy + math.sin(a) * r.height() / 2 * k))
    return _poly(pts)


# ---- picture shapes: simple silhouettes that say what a node is ----------------
def _stroke(path, width):
    s = QPainterPathStroker()
    s.setWidth(width)
    s.setCapStyle(Qt.RoundCap)
    return s.createStroke(path)


def camera(r):
    """Video: camera body + lens."""
    x0, y0, w, h = r.left(), r.top(), r.width(), r.height()
    p = QPainterPath()
    p.addRoundedRect(QRectF(x0, y0, w * 0.68, h), h * 0.2, h * 0.2)
    lens = _poly([(x0 + w * 0.75, y0 + h * 0.34), (x0 + w, y0 + h * 0.06),
                  (x0 + w, y0 + h * 0.94), (x0 + w * 0.75, y0 + h * 0.66)])
    return p.united(lens)


def mountains(r):
    """Image: two peaks and a sun."""
    x0, y0, w, h, y1 = r.left(), r.top(), r.width(), r.height(), r.bottom()
    p = _poly([(x0, y1), (x0 + w * 0.3, y0 + h * 0.3), (x0 + w * 0.46, y0 + h * 0.58),
               (x0 + w * 0.7, y0), (r.right(), y1)])
    sun = QPainterPath()
    sun.addEllipse(QPointF(x0 + w * 0.45, y0 + h * 0.14), h * 0.13, h * 0.13)
    return p.united(sun)


def speaker(r):
    """Audio: speaker with sound waves."""
    x0, y0, w, h, cy = r.left(), r.top(), r.width(), r.height(), r.center().y()
    p = QPainterPath()
    p.addRoundedRect(QRectF(x0, cy - h * 0.2, w * 0.2, h * 0.4), 2, 2)
    p = p.united(_poly([(x0 + w * 0.17, cy - h * 0.2), (x0 + w * 0.42, y0), (x0 + w * 0.42, r.bottom()),
                        (x0 + w * 0.17, cy + h * 0.2)]))
    cx = x0 + w * 0.42
    for k in (0.28, 0.52, 0.76):
        rad = h * k
        arc = QPainterPath()
        box = QRectF(cx - rad, cy - rad, 2 * rad, 2 * rad)
        arc.arcMoveTo(box, -42)
        arc.arcTo(box, -42, 84)
        p = p.united(_stroke(arc, h * 0.09))
    return p


def robot(r):
    """Model: a robot head with antenna, ears and eyes."""
    x0, y0, w, h, cx = r.left(), r.top(), r.width(), r.height(), r.center().x()
    p = QPainterPath()
    p.addRoundedRect(QRectF(x0 + w * 0.13, y0 + h * 0.24, w * 0.74, h * 0.76), h * 0.14, h * 0.14)
    for ex in (x0, r.right() - w * 0.14):
        ear = QPainterPath()
        ear.addRoundedRect(QRectF(ex, y0 + h * 0.46, w * 0.14, h * 0.3), 2, 2)
        p = p.united(ear)
    antenna = QPainterPath()
    antenna.addRect(QRectF(cx - 1.5, y0 + h * 0.08, 3, h * 0.18))
    antenna.addEllipse(QPointF(cx, y0 + h * 0.08), h * 0.08, h * 0.08)
    p = p.united(antenna)
    eyes = QPainterPath()
    for sx in (-1, 1):
        eyes.addEllipse(QPointF(cx + sx * w * 0.17, y0 + h * 0.52), h * 0.09, h * 0.09)
    return p.subtracted(eyes)


def _pt(r, fx, fy):
    return QPointF(r.left() + r.width() * fx, r.top() + r.height() * fy)


BRAIN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "brain.json")


@lru_cache(maxsize=1)
def _brain_data():
    """Silhouette + grooves traced from a vector illustration (tools/extract_brain.py), or None."""
    try:
        with open(BRAIN_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _from_polygons(polys, r):
    p = QPainterPath()
    p.setFillRule(Qt.OddEvenFill)  # holes stay holes
    for poly in polys:
        p.addPolygon(QPolygonF([QPointF(r.left() + x * r.width(), r.top() + y * r.height()) for x, y in poly]))
        p.closeSubpath()
    return p


def brain(r):
    data = _brain_data()
    if data:
        return _from_polygons(data["silhouette"], r)
    return _brain_drawn(r)


def _brain_drawn(r):
    """Fallback brain (if the traced one is missing): frontal lobe left, cerebellum and stem bottom right."""
    P = lambda fx, fy: _pt(r, fx, fy)  # noqa: E731
    p = QPainterPath(P(0.10, 0.62))
    p.cubicTo(P(0.02, 0.56), P(0.00, 0.40), P(0.03, 0.30))          # forehead
    p.cubicTo(P(0.07, 0.12), P(0.18, 0.04), P(0.32, 0.04))          # top front
    p.cubicTo(P(0.46, 0.00), P(0.62, 0.00), P(0.74, 0.04))          # crown
    p.cubicTo(P(0.90, 0.09), P(1.00, 0.24), P(0.99, 0.42))          # back of the head
    p.cubicTo(P(0.98, 0.56), P(0.95, 0.66), P(0.90, 0.72))
    p.cubicTo(P(0.88, 0.84), P(0.78, 0.88), P(0.70, 0.84))          # cerebellum
    p.cubicTo(P(0.66, 0.82), P(0.63, 0.78), P(0.62, 0.74))
    p.cubicTo(P(0.52, 0.80), P(0.40, 0.82), P(0.30, 0.78))          # temporal lobe underside
    p.cubicTo(P(0.20, 0.76), P(0.13, 0.70), P(0.10, 0.62))
    p.closeSubpath()
    # a few soft bumps along the top so the silhouette isn't too smooth
    for fx, fy, rx, ry in ((0.17, 0.2, 0.1, 0.14), (0.38, 0.1, 0.12, 0.1), (0.6, 0.1, 0.12, 0.1),
                           (0.82, 0.22, 0.1, 0.14)):
        bump = QPainterPath()
        bump.addEllipse(P(fx, fy), r.width() * rx, r.height() * ry)
        p = p.united(bump)
    stem = _poly([(r.left() + r.width() * fx, r.top() + r.height() * fy)
                  for fx, fy in ((0.54, 0.74), (0.64, 0.76), (0.61, 1.0), (0.555, 1.0))])
    return p.united(stem)


def _brain_grooves(r):
    P = lambda fx, fy: _pt(r, fx, fy)  # noqa: E731
    grooves = []

    def curve(*pts):
        path = QPainterPath(P(*pts[0]))
        if len(pts) == 3:
            path.quadTo(P(*pts[1]), P(*pts[2]))
        else:
            path.cubicTo(P(*pts[1]), P(*pts[2]), P(*pts[3]))
        grooves.append(path)

    curve((0.12, 0.42), (0.20, 0.26), (0.32, 0.32))                   # frontal
    curve((0.27, 0.14), (0.35, 0.26), (0.27, 0.46))
    curve((0.45, 0.10), (0.54, 0.22), (0.42, 0.34), (0.52, 0.46))    # central sulcus
    curve((0.62, 0.12), (0.64, 0.28), (0.78, 0.28))                   # parietal
    curve((0.84, 0.36), (0.92, 0.50), (0.80, 0.60))                   # occipital
    curve((0.22, 0.62), (0.34, 0.50), (0.50, 0.66), (0.66, 0.56))    # lateral sulcus
    curve((0.34, 0.72), (0.44, 0.66), (0.56, 0.70))                   # temporal
    curve((0.68, 0.79), (0.76, 0.74), (0.86, 0.78))                   # cerebellum
    return grooves


def terminal(r):
    """Prompt: a terminal window (the > and _ are drawn as light grooves)."""
    p = QPainterPath()
    p.addRoundedRect(r, r.height() * 0.18, r.height() * 0.18)
    return p


def _terminal_grooves(r):
    P = lambda fx, fy: _pt(r, fx, fy)  # noqa: E731
    chevron = QPainterPath(P(0.2, 0.26))
    chevron.lineTo(P(0.38, 0.48))
    chevron.lineTo(P(0.2, 0.7))
    underscore = QPainterPath(P(0.46, 0.74))
    underscore.lineTo(P(0.78, 0.74))
    return [chevron, underscore]


EDIT_BOX = 100 / 42     # the box: the Prompt node's proportions
PENCIL_ASPECT = 100 / 54  # the node: the box plus headroom for the pencil sticking out of it


def _edit_box(r):
    h = r.width() / EDIT_BOX
    return QRectF(r.left(), r.bottom() - h, r.width(), h)


def _pencil(r):
    """(tip, cone base, end, half width, axis, normal) of the Edit icon's pencil: its own proportions (never
    stretched with the box), at 45 degrees, tip inside the box, sticking out of the gap at the top-right."""
    box = _edit_box(r)
    h = box.height()
    length, half = h * 1.38, h * 0.16
    axis, normal = QPointF(0.7071, -0.7071), QPointF(0.7071, 0.7071)
    tip = QPointF(box.left() + box.width() * 0.42, box.top() + h * 0.7)
    base = tip + axis * (length * 0.22)
    end = tip + axis * length
    return tip, base, end, half, axis, normal


def pencil_icon(r):
    """Edit: a solid box (the Prompt's size) with a pencil lying across it and sticking out at the top-right;
    a thin gap is cut around the pencil so it reads as its own shape."""
    box = _edit_box(r)
    h = box.height()
    rad = h * 0.18
    body = QPainterPath()
    body.addRoundedRect(box, rad, rad)
    tip, base, end, half, axis, normal = _pencil(r)
    pencil = QPainterPath(tip)
    for pt in (base + normal * half, end + normal * half, end - normal * half, base - normal * half):
        pencil.lineTo(pt)
    pencil.closeSubpath()
    rounded = QPainterPathStroker()  # soften the pencil's corners a little
    rounded.setWidth(h * 0.05)
    rounded.setJoinStyle(Qt.RoundJoin)
    pencil = pencil.united(rounded.createStroke(pencil))
    gap = QPainterPathStroker()
    gap.setWidth(h * 0.16)
    gap.setJoinStyle(Qt.RoundJoin)
    cut = pencil.united(gap.createStroke(pencil))
    return body.subtracted(cut).united(pencil).simplified()


def _pencil_grooves(r):
    """The lines across the pencil: where the point starts and the band near its end."""
    tip, base, end, half, axis, normal = _pencil(r)
    lines = []
    for at in (base, tip + (end - tip) * 0.8):
        line = QPainterPath(at + normal * (half * 0.75))
        line.lineTo(at - normal * (half * 0.75))
        lines.append(line)
    return lines


def _brain_groove_shapes(r):
    data = _brain_data()
    return [_from_polygons(data["grooves"], r)] if data else _brain_grooves(r)


def target(r):
    """Isolate: a rounded frame with a crosshair (the circle and its four ticks are grooves)."""
    p = QPainterPath()
    p.addRoundedRect(r, r.height() * 0.22, r.height() * 0.22)
    return p


def _target_grooves(r):
    c, h = r.center(), r.height()
    ring = QPainterPath()
    ring.addEllipse(c, h * 0.27, h * 0.27)
    out = [ring]
    for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):  # ticks crossing the ring, as on a sight
        tick = QPainterPath(QPointF(c.x() + dx * h * 0.13, c.y() + dy * h * 0.13))
        tick.lineTo(QPointF(c.x() + dx * h * 0.42, c.y() + dy * h * 0.42))
        out.append(tick)
    return out


def backspace(r):
    """Delete: a backspace key - pointed on the left - with a hollow cross (grooves)."""
    h = r.height()
    p = QPainterPath(QPointF(r.left(), r.center().y()))
    p.lineTo(QPointF(r.left() + h * 0.5, r.top()))
    p.lineTo(r.topRight())
    p.lineTo(r.bottomRight())
    p.lineTo(QPointF(r.left() + h * 0.5, r.bottom()))
    p.closeSubpath()
    return p


def _backspace_grooves(r):
    h = r.height()
    cx, cy, d = (r.left() + h * 0.5 + r.right()) / 2, r.center().y(), h * 0.22
    a = QPainterPath(QPointF(cx - d, cy - d))
    a.lineTo(QPointF(cx + d, cy + d))
    b = QPainterPath(QPointF(cx + d, cy - d))
    b.lineTo(QPointF(cx - d, cy + d))
    return [a, b]


def document(r):
    """File: a sheet with its top-right corner folded."""
    fold = r.height() * 0.32
    p = QPainterPath(r.topLeft())
    p.lineTo(QPointF(r.right() - fold, r.top()))
    p.lineTo(QPointF(r.right(), r.top() + fold))
    p.lineTo(r.bottomRight())
    p.lineTo(r.bottomLeft())
    p.closeSubpath()
    return p


def _document_grooves(r):
    fold = r.height() * 0.32
    corner = QPainterPath(QPointF(r.right() - fold, r.top()))
    corner.lineTo(QPointF(r.right() - fold, r.top() + fold))
    corner.lineTo(QPointF(r.right(), r.top() + fold))
    lines = []
    for fy, end in ((0.42, 0.62), (0.6, 0.8), (0.78, 0.7)):  # lines of text
        line = QPainterPath(_pt(r, 0.14, fy))
        line.lineTo(_pt(r, end, fy))
        lines.append(line)
    return [corner] + lines


def cube(r):
    """Geometry: a cube seen from a corner (its silhouette; the three front edges are grooves)."""
    P = lambda fx, fy: _pt(r, fx, fy)  # noqa: E731
    p = QPainterPath(P(0.5, 0.0))
    for fx, fy in ((0.95, 0.24), (0.95, 0.76), (0.5, 1.0), (0.05, 0.76), (0.05, 0.24)):
        p.lineTo(P(fx, fy))
    p.closeSubpath()
    return p


def _cube_grooves(r):
    P = lambda fx, fy: _pt(r, fx, fy)  # noqa: E731
    out = []
    for fx, fy in ((0.12, 0.28), (0.88, 0.28), (0.5, 0.92)):  # from the near corner, stopping short of the outline
        edge = QPainterPath(P(0.5, 0.5))
        edge.lineTo(P(fx, fy))
        out.append(edge)
    return out


# Light "cut-out" lines, like the white lines in an icon: (function, stroke width as a fraction of
# height, or None when the function returns ready-made filled shapes).
GROOVES = {"brain": (_brain_groove_shapes, None), "terminal": (_terminal_grooves, 0.1), "pencil": (_pencil_grooves, 0.07),
           "target": (_target_grooves, 0.085), "backspace": (_backspace_grooves, 0.1),
           "document": (_document_grooves, 0.07), "cube": (_cube_grooves, 0.06)}


SHAPES = {
    "rect": rect, "squared": squared, "cigar": cigar, "oval": oval,
    "bulge": bulge, "bulge_down": bulge_down, "tabbed_left": tabbed_left, "tabbed_right": tabbed_right,
    "chevron_down": chevron_down, "chevron_up": chevron_up, "trapezoid_down": trapezoid_down,
    "trapezoid_up": trapezoid_up, "clipped_left": clipped_left, "clipped_right": clipped_right,
    "tilted": tilted, "hexagon": hexagon, "octagon": octagon, "pointy": pointy,
    "wave": wave, "ticket": ticket, "cloud": cloud, "burst": star_burst,
    "camera": camera, "mountains": mountains, "speaker": speaker, "robot": robot, "brain": brain,
    "terminal": terminal, "pencil": pencil_icon, "target": target, "backspace": backspace, "document": document,
    "cube": cube,
}


# Natural width / height of each shape. Nodes all share one width; their height follows this.
ASPECT = {
    "rect": 3.1, "squared": 3.1, "cigar": 3.1, "oval": 3.1, "bulge": 3.3, "bulge_down": 3.1,
    "tabbed_left": 2.6, "tabbed_right": 2.6, "chevron_down": 3.1, "chevron_up": 3.1, "trapezoid_down": 3.1,
    "trapezoid_up": 3.1, "clipped_left": 3.1, "clipped_right": 3.1, "tilted": 3.1, "hexagon": 3.1,
    "octagon": 3.1, "pointy": 3.1, "wave": 3.1, "ticket": 3.1, "cloud": 3.1, "burst": 3.1,
    "camera": 2.37, "mountains": 2.1, "speaker": 1.6, "robot": 1.4, "terminal": 1.33, "brain": 1.21, "pencil": PENCIL_ASPECT,
    "target": 3.1, "backspace": 3.1,  # the sizes Isolate / Delete had (ticket / trapezoid)
    "document": 2.1, "cube": 1.4,     # File like the Image node; Geometry a little taller
}
if _brain_data():
    ASPECT["brain"] = _brain_data()["aspect"]


def aspect(name):
    return ASPECT.get(name, 3.1)


def grooves(name, r):
    """(paths, pen width) for light cut-out lines; pen width None means: fill the paths. ([], 0) if none."""
    if name not in GROOVES:
        return [], 0
    width = GROOVES[name][1]
    paths = _grooves_cached(name, r.x(), r.y(), r.width(), r.height())
    return [QPainterPath(p) for p in paths], (None if width is None else max(1.5, r.height() * width))


@lru_cache(maxsize=256)
def _grooves_cached(name, x, y, w, h):
    return tuple(GROOVES[name][0](QRectF(x, y, w, h)))


def lowest_x(name):
    """x (0..1 of the width) of a shape's lowest point - e.g. where the brain stem ends."""
    path = body(name, QRectF(0, 0, 1000, 1000))
    best = max((pt for poly in path.toSubpathPolygons() for pt in poly), key=lambda pt: pt.y())
    return best.x() / 1000


def body(name, r):
    return QPainterPath(_body_cached(name, r.x(), r.y(), r.width(), r.height()))


@lru_cache(maxsize=256)
def _body_cached(name, x, y, w, h):  # shapes are rebuilt only when a node's size changes
    return SHAPES.get(name, rect)(QRectF(x, y, w, h))


def details(name, r):
    """Faint inner lines that give some shapes their character (drawn clipped to the body)."""
    if name in ("bulge", "bulge_down", "cigar"):
        return [QLineF(x, r.top() - 10, x, r.bottom() + 10) for x in
                (r.left() + r.width() * f for f in (0.2, 0.35, 0.65, 0.8))]
    if name in ("tabbed_left", "tabbed_right"):
        t = r.height() * 0.3
        return [QLineF(r.left(), r.top() + t + 2, r.right(), r.top() + t + 2),
                QLineF(r.left() + r.width() * 0.22, r.top() + t, r.left() + r.width() * 0.17, r.bottom()),
                QLineF(r.left() + r.width() * 0.78, r.top() + t, r.left() + r.width() * 0.83, r.bottom())]
    if name == "robot":  # mouth
        y = r.top() + r.height() * 0.8
        return [QLineF(r.center().x() - r.width() * 0.18, y, r.center().x() + r.width() * 0.18, y)]
    if name == "squared":
        return [QLineF(r.left() + 7, r.top(), r.left() + 7, r.bottom()), QLineF(r.right() - 7, r.top(), r.right() - 7, r.bottom())]
    return []


def edge_y(path, x, top=True):
    """Where a vertical line at x meets the top (or bottom) outline of the path."""
    b = path.boundingRect()
    steps = int(b.height() * 2) + 2
    for i in range(steps):
        y = b.top() + i * 0.5 if top else b.bottom() - i * 0.5
        if path.contains(QPointF(x, y)):
            return y
    return b.top() if top else b.bottom()
