"""Small tool icons drawn in code: light strokes that read on the dark theme, sharp at any UI scale."""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

COLOR = QColor(220, 220, 220)


def icon(kind, size=18):
    s = size * 2  # drawn at 2x, scaled down sharp
    pm = QPixmap(s, s)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(COLOR, s * 0.085, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    P = lambda fx, fy: QPointF(s * fx, s * fy)  # noqa: E731

    if kind == "crop":  # two interlocking corner brackets
        a = QPainterPath(P(0.28, 0.08)); a.lineTo(P(0.28, 0.72)); a.lineTo(P(0.92, 0.72))
        b = QPainterPath(P(0.08, 0.28)); b.lineTo(P(0.72, 0.28)); b.lineTo(P(0.72, 0.92))
        p.drawPath(a); p.drawPath(b)
    elif kind in ("rotate_left", "rotate_right"):  # an open circle with an arrow head
        rect = QRectF(s * 0.18, s * 0.2, s * 0.64, s * 0.64)
        path = QPainterPath()
        if kind == "rotate_right":
            path.arcMoveTo(rect, 150); path.arcTo(rect, 150, -270)
            head = [P(0.6, 0.08), P(0.47, 0.2), P(0.62, 0.3)]
        else:
            path.arcMoveTo(rect, 30); path.arcTo(rect, 30, 270)
            head = [P(0.4, 0.08), P(0.53, 0.2), P(0.38, 0.3)]
        p.drawPath(path)
        h = QPainterPath(head[0]); h.lineTo(head[1]); h.lineTo(head[2])
        p.drawPath(h)
    elif kind in ("flip_h", "flip_v"):  # two triangles mirrored across a dashed line
        if kind == "flip_h":
            left = [P(0.42, 0.22), P(0.42, 0.78), P(0.1, 0.78)]
            right = [P(0.58, 0.22), P(0.58, 0.78), P(0.9, 0.78)]
            axis = (P(0.5, 0.08), P(0.5, 0.92))
        else:
            left = [P(0.22, 0.42), P(0.78, 0.42), P(0.78, 0.1)]
            right = [P(0.22, 0.58), P(0.78, 0.58), P(0.78, 0.9)]
            axis = (P(0.08, 0.5), P(0.92, 0.5))
        p.setBrush(COLOR)
        tri = QPainterPath(left[0]); tri.lineTo(left[1]); tri.lineTo(left[2]); tri.closeSubpath()
        p.drawPath(tri)
        p.setBrush(Qt.NoBrush)
        tri = QPainterPath(right[0]); tri.lineTo(right[1]); tri.lineTo(right[2]); tri.closeSubpath()
        p.drawPath(tri)
        dash = QPen(COLOR, s * 0.05, Qt.DashLine)
        p.setPen(dash)
        p.drawLine(*axis)
    elif kind in ("lock", "unlock"):  # a padlock, shackle closed or open
        p.setBrush(Qt.NoBrush)
        shackle = QPainterPath()
        shackle.moveTo(P(0.32, 0.46))
        shackle.lineTo(P(0.32, 0.3))
        shackle.arcTo(QRectF(s * 0.32, s * 0.1, s * 0.36, s * 0.36), 180, -180)
        shackle.lineTo(P(0.68, 0.36 if kind == "unlock" else 0.46))
        if kind == "unlock":
            shackle.translate(s * 0.12, -s * 0.06)
        p.drawPath(shackle)
        p.setBrush(COLOR)
        p.drawRoundedRect(QRectF(s * 0.22, s * 0.46, s * 0.56, s * 0.42), s * 0.06, s * 0.06)
    elif kind == "pen":  # a pencil, point down-left
        body = QPainterPath(P(0.16, 0.84))
        for pt in (P(0.22, 0.62), P(0.7, 0.14), P(0.86, 0.3), P(0.38, 0.78)):
            body.lineTo(pt)
        body.closeSubpath()
        p.drawPath(body)
        p.drawLine(P(0.6, 0.24), P(0.76, 0.4))
    elif kind == "undo":  # a hooked arrow back
        path = QPainterPath(P(0.3, 0.42))
        path.cubicTo(P(0.62, 0.2), P(0.9, 0.42), P(0.8, 0.66))
        path.cubicTo(P(0.72, 0.82), P(0.5, 0.84), P(0.36, 0.8))
        p.drawPath(path)
        h = QPainterPath(P(0.36, 0.2)); h.lineTo(P(0.24, 0.44)); h.lineTo(P(0.5, 0.52))
        p.drawPath(h)
    elif kind == "clear":  # a waste bin
        p.drawLine(P(0.16, 0.24), P(0.84, 0.24))
        p.drawLine(P(0.4, 0.24), P(0.42, 0.12)); p.drawLine(P(0.42, 0.12), P(0.58, 0.12))
        p.drawLine(P(0.58, 0.12), P(0.6, 0.24))
        bin_ = QPainterPath(P(0.24, 0.3)); bin_.lineTo(P(0.3, 0.88)); bin_.lineTo(P(0.7, 0.88)); bin_.lineTo(P(0.76, 0.3))
        p.drawPath(bin_)
        p.drawLine(P(0.44, 0.42), P(0.45, 0.76)); p.drawLine(P(0.56, 0.42), P(0.55, 0.76))
    elif kind == "reset":  # a counter-clockwise arrow around a dot
        rect = QRectF(s * 0.2, s * 0.2, s * 0.6, s * 0.6)
        path = QPainterPath(); path.arcMoveTo(rect, 60); path.arcTo(rect, 60, 280)
        p.drawPath(path)
        h = QPainterPath(P(0.46, 0.06)); h.lineTo(P(0.66, 0.2)); h.lineTo(P(0.46, 0.34))
        p.drawPath(h)
    # ---- the 3D viewer's toggles (after Maya's viewport bar) -------------------------------------------------------
    elif kind == "headlight":  # a light bulb
        bulb = QPainterPath()
        bulb.moveTo(P(0.38, 0.66))
        bulb.cubicTo(P(0.38, 0.54), P(0.22, 0.48), P(0.22, 0.34))
        bulb.cubicTo(P(0.22, 0.17), P(0.35, 0.08), P(0.5, 0.08))
        bulb.cubicTo(P(0.65, 0.08), P(0.78, 0.17), P(0.78, 0.34))
        bulb.cubicTo(P(0.78, 0.48), P(0.62, 0.54), P(0.62, 0.66))
        bulb.closeSubpath()
        p.drawPath(bulb)
        p.drawLine(P(0.4, 0.77), P(0.6, 0.77))
        p.drawLine(P(0.43, 0.88), P(0.57, 0.88))
    elif kind == "wireframe":  # a shaded cube with its edges drawn over it
        top = [P(0.5, 0.1), P(0.88, 0.28), P(0.5, 0.46), P(0.12, 0.28)]
        left = [P(0.12, 0.28), P(0.5, 0.46), P(0.5, 0.92), P(0.12, 0.72)]
        right = [P(0.5, 0.46), P(0.88, 0.28), P(0.88, 0.72), P(0.5, 0.92)]
        p.setPen(Qt.NoPen)
        for face, shade in ((top, 0.75), (left, 0.45), (right, 0.3)):
            path = QPainterPath(face[0])
            for pt in face[1:]:
                path.lineTo(pt)
            path.closeSubpath()
            p.fillPath(path, QColor(COLOR.red(), COLOR.green(), COLOR.blue(), int(255 * shade)))
        p.setPen(QPen(QColor(30, 30, 30), s * 0.05))
        for a, b in ((P(0.31, 0.19), P(0.69, 0.37)), (P(0.69, 0.19), P(0.31, 0.37)), (P(0.31, 0.37), P(0.31, 0.82)),
                     (P(0.69, 0.37), P(0.69, 0.82)), (P(0.12, 0.5), P(0.5, 0.69)), (P(0.5, 0.69), P(0.88, 0.5))):
            p.drawLine(a, b)
        p.setPen(pen)
        outline = QPainterPath(top[0])
        for pt in (top[1], right[2], right[3], left[3], top[3]):
            outline.lineTo(pt)
        outline.closeSubpath()
        p.drawPath(outline)
        p.drawLine(P(0.12, 0.28), P(0.5, 0.46)); p.drawLine(P(0.5, 0.46), P(0.88, 0.28))
        p.drawLine(P(0.5, 0.46), P(0.5, 0.92))
    elif kind == "textures":  # a sphere with a checker on it
        r = QRectF(s * 0.1, s * 0.1, s * 0.8, s * 0.8)
        ball = QPainterPath()
        ball.addEllipse(r)
        p.save()
        p.setClipPath(ball)
        n = 4
        for i in range(n):
            for j in range(n):
                if (i + j) % 2 == 0:
                    p.fillRect(QRectF(r.x() + i * r.width() / n, r.y() + j * r.height() / n, r.width() / n,
                                      r.height() / n), COLOR)
        p.restore()
        p.drawEllipse(r)
    elif kind == "multisample":  # a circle: stair-stepped edge on the left half, smooth on the right
        r = QRectF(s * 0.14, s * 0.14, s * 0.72, s * 0.72)
        arc = QPainterPath()
        arc.arcMoveTo(r, -90)
        arc.arcTo(r, -90, 180)
        p.drawPath(arc)
        steps = QPainterPath(P(0.5, 0.14))
        for x, y in ((0.32, 0.14), (0.32, 0.22), (0.22, 0.22), (0.22, 0.34), (0.14, 0.34), (0.14, 0.66),
                     (0.22, 0.66), (0.22, 0.78), (0.32, 0.78), (0.32, 0.86), (0.5, 0.86)):
            steps.lineTo(P(x, y))
        p.drawPath(steps)
    elif kind == "ground":  # a ball standing on a plane, its shadow beside it
        p.setBrush(QColor(COLOR.red(), COLOR.green(), COLOR.blue(), 90))
        p.setPen(Qt.NoPen)
        p.drawEllipse(QRectF(s * 0.34, s * 0.66, s * 0.56, s * 0.18))
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        plane = QPainterPath(P(0.2, 0.62))
        for pt in (P(0.96, 0.62), P(0.8, 0.92), P(0.04, 0.92)):
            plane.lineTo(pt)
        plane.closeSubpath()
        p.drawPath(plane)
        p.setBrush(QColor(37, 37, 37))
        p.drawEllipse(QRectF(s * 0.24, s * 0.1, s * 0.46, s * 0.46))
    elif kind == "grid":  # a floor grid seen in perspective: 3 x 3 cells
        back_l, back_r, front_r, front_l = P(0.26, 0.26), P(0.74, 0.26), P(0.96, 0.86), P(0.04, 0.86)
        plane = QPainterPath(back_l)
        for pt in (back_r, front_r, front_l):
            plane.lineTo(pt)
        plane.closeSubpath()
        p.drawPath(plane)
        thin = QPen(pen)
        thin.setWidthF(pen.widthF() * 0.7)
        p.setPen(thin)
        mix = lambda a, b, t: QPointF(a.x() + (b.x() - a.x()) * t, a.y() + (b.y() - a.y()) * t)
        for t in (1 / 3, 2 / 3):
            p.drawLine(mix(back_l, back_r, t), mix(front_l, front_r, t))  # running away from the viewer
            d = t * t * 0.4 + t * 0.6  # rows bunch up towards the back
            p.drawLine(mix(back_l, front_l, d), mix(back_r, front_r, d))
        p.setPen(pen)
    elif kind == "origin":  # the axes from the origin: up, right, toward the viewer
        o = P(0.36, 0.64)
        for end, head in ((P(0.36, 0.08), ((0.28, 0.18), (0.44, 0.18))), (P(0.92, 0.64), ((0.82, 0.56), (0.82, 0.72))),
                          (P(0.1, 0.9), ((0.12, 0.78), (0.22, 0.88)))):
            p.drawLine(o, end)
            h = QPainterPath(P(*head[0])); h.lineTo(end); h.lineTo(P(*head[1]))
            p.drawPath(h)
        p.setBrush(COLOR)
        p.drawEllipse(o, s * 0.06, s * 0.06)
    elif kind == "key":  # a key: ring bow, shaft, two teeth
        p.drawEllipse(QRectF(s * 0.1, s * 0.3, s * 0.36, s * 0.36))
        p.drawLine(P(0.46, 0.48), P(0.92, 0.48))
        p.drawLine(P(0.8, 0.48), P(0.8, 0.66))
        p.drawLine(P(0.68, 0.48), P(0.68, 0.62))
    elif kind == "autokey":  # a record dot in a ring (red when the button is on - see the ViewToggle style)
        p.drawEllipse(QRectF(s * 0.12, s * 0.12, s * 0.76, s * 0.76))
        p.setBrush(QColor(225, 70, 70))
        p.setPen(Qt.NoPen)
        p.drawEllipse(QRectF(s * 0.3, s * 0.3, s * 0.4, s * 0.4))
    p.end()
    return QIcon(pm)
