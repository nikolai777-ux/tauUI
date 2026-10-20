"""The Edit Geometry node's curve editor (pane box under the viewer; its height: the grip under it).

Shows the channels (curves.py) of what is selected in the viewer - or, with nothing selected, of the camera looked
through (the viewer's own Default camera is not in the scene: nothing to show). Each object's channels are listed on
the left - click one to show only it (Shift / Ctrl: add or take one away; click the object's name: all of its; click
the shown one again: everything); colours: X red, Y green, Z blue;
the graph shows the keys made here (editable) and, dimmed, the file's own animation of the same channels. On a
channel the file animates, the keys are an additive layer (curves.py "offsets"): drawn on the result (the file's
motion + the offset curve), dragged up / down = a bigger / smaller offset.
Mouse: wheel = zoom (Shift: time only, Ctrl: values only), middle drag = pan, F or double-click on empty graph = fit
everything; click a key = select it (Shift adds), drag on empty graph = a box selecting the keys in it; drag selected
keys = move them (whole frames); Del = delete them. The strip at the top shows frames; click / drag it = go to that
frame (the viewer follows). Every change is one undo step (the node's "edits")."""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from . import curves

AXIS = {"x": QColor(225, 85, 85), "y": QColor(95, 200, 95), "z": QColor(95, 140, 240)}
STYLE = {"t": Qt.SolidLine, "r": Qt.SolidLine, "s": Qt.SolidLine}  # one look for all (the user's choice)
LIST_W = 128   # the channel list's width
RULER_H = 18   # the frame strip at the top
KEY_R = 3.5    # a key's half size
PICK = 6       # pixels around a key that grab it


class CurveEditor(QWidget):
    default_height = 220

    def __init__(self, node, set_value, parent=None):
        super().__init__(parent)
        self.node, self.set_value = node, set_value
        self.setFocusPolicy(Qt.ClickFocus)
        self.setMouseTracking(True)
        self.setMinimumHeight(80)
        self.isolated = set()        # (path, channel) shown alone (empty: all)
        self.selected = set()        # (path, channel, key index) selected keys
        self.view = None             # (f0, f1, v0, v1) shown
        self._shown_for = None       # the paths the view was fitted for
        self._drag = None            # what the mouse is doing
        self._clock = None

    # ---- what to show -----------------------------------------------------------------------------------------
    def paths(self):
        n = self.node
        sel = list(getattr(n, "_selection", []) or [])
        cam = getattr(n, "_active_cam_path", "") or ""
        return sel or ([cam] if cam else [])

    def edits(self):
        return self.node.params.get("edits") or {}

    def channels(self):
        """[(path, channel, keys (ours) or None, samples (the file's) or None)] - channels with anything to draw.
        self.modes[(path, ch)]: "abs" (keys are values) or "add" (keys are offsets on the file's motion)."""
        edits, samples, out = self.edits(), getattr(self.node, "_samples", {}) or {}, []
        self.modes = {}
        for path in self.paths():
            e = edits.get(path) or {}
            keys, offsets = e.get("keys") or {}, e.get("offsets") or {}
            file = samples.get(path) or {}
            for ch in curves.CHANNELS:
                ours = keys.get(ch) or offsets.get(ch)
                if ours or file.get(ch):
                    self.modes[(path, ch)] = "add" if (offsets.get(ch) and not keys.get(ch)) else "abs"
                    out.append((path, ch, ours, file.get(ch)))
        return out

    def on(self, path, ch):
        """Drawn now (nothing isolated, or one of the isolated)."""
        return not self.isolated or (path, ch) in self.isolated

    def shown(self, path, ch, file, f, v):
        """Where a key / curve value is drawn: an offset sits on the file's motion."""
        return v + curves.file_value(file, f) if self.modes.get((path, ch)) == "add" else v

    def refresh(self, node):
        """The node changed (the pane calls this): follow its viewer's time; refit when what is shown changed."""
        viewer = getattr(node, "_viewer", None)
        clock = getattr(viewer, "clock", None)
        if clock is not None and clock is not self._clock:
            self._clock = clock
            clock.positionChanged.connect(lambda _ms: self.update())
        paths = tuple(self.paths())
        if paths != self._shown_for:
            self._shown_for = paths
            self.selected.clear()
            self.isolated = set()
            self.fit()
        self.update()

    # ---- the view ---------------------------------------------------------------------------------------------
    def graph(self):
        return QRectF(LIST_W, RULER_H, max(10, self.width() - LIST_W - 1), max(10, self.height() - RULER_H - 1))

    def frames(self):
        return getattr(self.node, "scene_frames", lambda: 0)() or 1

    def fit(self):
        vals, fr = [], []
        for path, ch, keys, file in self.channels():
            if not self.on(path, ch):
                continue
            for k in file or []:
                fr.append(k[0])
                vals.append(k[1])
            for k in keys or []:
                fr.append(k[0])
                vals.append(self.shown(path, ch, file, k[0], k[1]))
        f0, f1 = (min(fr + [1]), max(fr + [self.frames()])) if fr else (1, self.frames())
        v0, v1 = (min(vals), max(vals)) if vals else (-1, 1)
        if v1 - v0 < 1e-6:
            v0, v1 = v0 - 1, v1 + 1
        pad_v, pad_f = (v1 - v0) * 0.1, max(1, (f1 - f0) * 0.03)
        self.view = (f0 - pad_f, f1 + pad_f, v0 - pad_v, v1 + pad_v)
        self.update()

    def to_px(self, f, v):
        g, (f0, f1, v0, v1) = self.graph(), self.view
        return QPointF(g.left() + (f - f0) / (f1 - f0) * g.width(), g.bottom() - (v - v0) / (v1 - v0) * g.height())

    def to_fv(self, p):
        g, (f0, f1, v0, v1) = self.graph(), self.view
        return (f0 + (p.x() - g.left()) / g.width() * (f1 - f0), v0 + (g.bottom() - p.y()) / g.height() * (v1 - v0))

    def current_frame(self):
        viewer = getattr(self.node, "_viewer", None)
        return viewer.playbar.current() if viewer is not None else 1

    # ---- painting ---------------------------------------------------------------------------------------------
    def paintEvent(self, _):
        if self.view is None:
            self.fit()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(30, 30, 30))
        g = self.graph()
        chans = self.channels()
        self._paint_list(p, chans)
        p.save()
        p.setClipRect(g.adjusted(0, -RULER_H, 0, 0))
        self._paint_grid(p, g)
        moved = self._moved_keys()
        for path, ch, keys, file in chans:
            if not self.on(path, ch):
                continue
            color = AXIS[ch[1]]
            style = STYLE[ch[0]]
            p.setBrush(Qt.NoBrush)  # (the key squares' fill must not fill the curves)
            if file:  # the file's own curve (read-only), dimmed
                dim = QColor(color)
                dim.setAlpha(110)
                p.setPen(QPen(dim, 1.2, style))
                path_ = QPainterPath(self.to_px(*file[0]))
                for f, v in file[1:]:
                    path_.lineTo(self.to_px(f, v))
                p.drawPath(path_)
            if keys:
                ks = moved.get((path, ch), keys)
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(color, 1.6, style))
                f0, f1 = self.view[0], self.view[1]
                steps = 240
                curve = QPainterPath()
                for i in range(steps + 1):
                    f = f0 + (f1 - f0) * i / steps
                    pt = self.to_px(f, self.shown(path, ch, file, f, curves.evaluate(ks, f)))
                    curve.moveTo(pt) if i == 0 else curve.lineTo(pt)
                p.drawPath(curve)
                for i, (f, v) in enumerate(ks):
                    pt = self.to_px(f, self.shown(path, ch, file, f, v))
                    sel = (path, ch, i) in self.selected
                    p.setPen(QPen(QColor(20, 20, 20), 1))
                    p.setBrush(QColor(255, 230, 90) if sel else color)
                    p.drawRect(QRectF(pt.x() - KEY_R, pt.y() - KEY_R, 2 * KEY_R, 2 * KEY_R))
        # the current frame
        x = self.to_px(self.current_frame(), 0).x()
        p.setPen(QPen(QColor(80, 150, 240), 1.5))
        p.drawLine(QPointF(x, 0), QPointF(x, g.bottom()))
        if self._drag and self._drag[0] == "box":
            p.setPen(QPen(QColor(220, 220, 220), 1, Qt.DashLine))
            p.setBrush(QColor(255, 255, 255, 25))
            p.drawRect(QRectF(self._drag[1], self._drag[2]).normalized())
        p.restore()
        if not chans:
            p.setPen(QColor(130, 130, 130))
            p.drawText(g.adjusted(12, 0, -12, 0), Qt.AlignCenter | Qt.TextWordWrap,
                       "Select an object in the viewer - or look through a file's camera - to see its keys")

    def _paint_list(self, p, chans):
        p.fillRect(QRectF(0, 0, LIST_W, self.height()), QColor(36, 36, 36))
        self._rows = []
        y, last = 4, None
        fm = p.fontMetrics()
        for path, ch, keys, _file in chans:
            if path != last:
                p.setPen(QColor(200, 200, 200))
                p.drawText(QRectF(6, y, LIST_W - 8, fm.height()), Qt.AlignLeft | Qt.AlignVCenter,
                           fm.elidedText(path.split("/")[-1], Qt.ElideRight, LIST_W - 12))
                self._rows.append((QRectF(0, y, LIST_W, fm.height()), path, None))
                y += fm.height() + 2
                last = path
            row = QRectF(0, y, LIST_W, fm.height())
            off = not self.on(path, ch)
            if (path, ch) in self.isolated:
                p.fillRect(row, QColor(61, 90, 120))
            color = QColor(AXIS[ch[1]])
            if off:
                color.setAlpha(60)
            p.setPen(QPen(color, 2, STYLE[ch[0]]))
            p.drawLine(QPointF(14, y + fm.height() / 2), QPointF(30, y + fm.height() / 2))
            p.setPen(QColor(110, 110, 110) if off else QColor(185, 185, 185))
            tag = "" if not keys else (" +" if self.modes.get((path, ch)) == "add" else "")
            p.drawText(QRectF(36, y, LIST_W - 40, fm.height()), Qt.AlignLeft | Qt.AlignVCenter,
                       curves.LABELS[ch] + tag + ("" if keys else "  (file)"))
            self._rows.append((row, path, ch))
            y += fm.height() + 1

    def _paint_grid(self, p, g):
        f0, f1, v0, v1 = self.view
        p.fillRect(QRectF(g.left(), 0, g.width(), RULER_H), QColor(40, 40, 40))
        font = p.font()
        font.setPointSizeF(max(6.5, font.pointSizeF() * 0.75))
        p.setFont(font)

        def step_for(span, px, min_px):
            for e in range(-4, 8):
                for m in (1, 2, 5):
                    st = m * 10 ** e
                    if st / span * px >= min_px:
                        return st
            return span
        fs = max(1, step_for(f1 - f0, g.width(), 40))
        f = (int(f0 // fs) + 1) * fs
        while f <= f1:
            x = self.to_px(f, 0).x()
            p.setPen(QPen(QColor(48, 48, 48), 1))
            p.drawLine(QPointF(x, g.top()), QPointF(x, g.bottom()))
            p.setPen(QColor(140, 140, 140))
            p.drawText(QRectF(x - 20, 0, 40, RULER_H), Qt.AlignCenter, f"{f:g}")
            f += fs
        vs = step_for(v1 - v0, g.height(), 26)
        v = (v0 // vs + 1) * vs
        while v <= v1:
            y = self.to_px(0, v).y()
            p.setPen(QPen(QColor(60, 60, 60) if abs(v) < vs * 1e-6 else QColor(45, 45, 45), 1))
            p.drawLine(QPointF(g.left(), y), QPointF(g.right(), y))
            p.setPen(QColor(120, 120, 120))
            p.drawText(QRectF(g.left() + 3, y - 12, 60, 12), Qt.AlignLeft | Qt.AlignBottom, f"{v:.4g}")
            v += vs

    # ---- keys under the mouse, moved keys ---------------------------------------------------------------------
    def key_at(self, pos):
        best = None
        for path, ch, keys, file in self.channels():
            if not keys or not self.on(path, ch):
                continue
            for i, (f, v) in enumerate(keys):
                pt = self.to_px(f, self.shown(path, ch, file, f, v))
                d = abs(pt.x() - pos.x()) + abs(pt.y() - pos.y())
                if d <= PICK * 1.5 and (best is None or d < best[0]):
                    best = (d, (path, ch, i))
        return best[1] if best else None

    def _moved_keys(self):
        """{(path, ch): keys} with the selected keys shifted by the drag in progress (frames whole)."""
        if not (self._drag and self._drag[0] == "move"):
            return {}
        df, dv = self._drag[3], self._drag[4]
        out = {}
        for path, ch, keys, _file in self.channels():
            if not keys:
                continue
            idx = [i for (p, c, i) in self.selected if p == path and c == ch]
            if not idx:
                continue
            moved = [[k[0] + df, k[1] + dv] if i in idx else list(k) for i, k in enumerate(keys)]
            # a moved key landing on another frame's key takes its place
            by_frame = {}
            for i, k in enumerate(moved):
                if i in idx or k[0] not in by_frame:
                    by_frame[k[0]] = k
            out[(path, ch)] = sorted(by_frame.values(), key=lambda k: k[0])
        return out

    def _store(self, new_keys):
        """{(path, ch): keys} → the node's edits (one undo step)."""
        edits = curves.copy_edits(self.edits())
        for (path, ch), keys in new_keys.items():
            entry = edits.setdefault(path, {"keys": {}, "offsets": {}, "set": {}})
            kind = "offsets" if self.modes.get((path, ch)) == "add" else "keys"
            if keys:
                entry[kind][ch] = keys
            else:
                entry[kind].pop(ch, None)
            if not entry["keys"] and not entry["offsets"] and not entry["set"]:
                edits.pop(path)
        self.set_value("edits", edits)

    # ---- mouse / keys -------------------------------------------------------------------------------------------
    def mousePressEvent(self, e):
        pos = e.position()
        if pos.x() < LIST_W:  # the channel list: show only what is clicked (see the top)
            for row, path, ch in getattr(self, "_rows", []):
                if row.contains(pos):
                    self._isolate(path, ch, bool(e.modifiers() & (Qt.ShiftModifier | Qt.ControlModifier)))
            return
        if pos.y() < RULER_H and e.button() == Qt.LeftButton:  # the frame strip
            self._drag = ("frame",)
            self._go(pos)
            return
        if e.button() == Qt.MiddleButton:
            self._drag = ("pan", pos, self.view)
            return
        if e.button() != Qt.LeftButton:
            return
        hit = self.key_at(pos)
        shift = bool(e.modifiers() & Qt.ShiftModifier)
        if hit:
            if shift:
                self.selected ^= {hit}
            elif hit not in self.selected:
                self.selected = {hit}
            self._drag = ("move", pos, self.to_fv(pos), 0, 0.0)
        else:
            if not shift:
                self.selected = set()
            self._drag = ("box", pos, pos)
        self.update()

    def _isolate(self, path, ch, add):
        """A click in the list: that channel alone (or, on the object's name, all of its); the same again = all;
        with Shift / Ctrl: add it / take it away."""
        picked = {(path, c) for p, c, _k, _f in self.channels() if p == path} if ch is None else {(path, ch)}
        if add:
            self.isolated = (self.isolated - picked) if picked <= self.isolated else (self.isolated | picked)
        else:
            self.isolated = set() if self.isolated == picked else picked
        self.selected = {k for k in self.selected if self.on(k[0], k[1])}
        self.fit()

    def mouseMoveEvent(self, e):
        pos = e.position()
        d = self._drag
        if not d:
            return
        if d[0] == "frame":
            self._go(pos)
        elif d[0] == "pan":
            (f0, f1, v0, v1), g = d[2], self.graph()
            df = (pos.x() - d[1].x()) / g.width() * (f1 - f0)
            dv = (pos.y() - d[1].y()) / g.height() * (v1 - v0)
            self.view = (f0 - df, f1 - df, v0 + dv, v1 + dv)
        elif d[0] == "move":
            f, v = self.to_fv(pos)
            self._drag = ("move", d[1], d[2], round(f - d[2][0]), v - d[2][1])
        elif d[0] == "box":
            self._drag = ("box", d[1], pos)
        self.update()

    def mouseReleaseEvent(self, e):
        d, self._drag = self._drag, None
        if not d:
            return
        if d[0] == "move" and (d[3] or abs(d[4]) > 1e-9):
            moved = {}
            for (path, ch), keys in self._moved_keys_from(d).items():
                moved[(path, ch)] = keys
            self.selected = set()
            self._store(moved)
        elif d[0] == "box":
            box = QRectF(d[1], d[2]).normalized()
            for path, ch, keys, file in self.channels():
                if keys and self.on(path, ch):
                    for i, (f, v) in enumerate(keys):
                        if box.contains(self.to_px(f, self.shown(path, ch, file, f, v))):
                            self.selected.add((path, ch, i))
        self.update()

    def _moved_keys_from(self, drag):
        keep, self._drag = self._drag, drag
        try:
            return self._moved_keys()
        finally:
            self._drag = keep

    def mouseDoubleClickEvent(self, e):
        if e.position().x() >= LIST_W and not self.key_at(e.position()):
            self.fit()

    def wheelEvent(self, e):
        if self.view is None or e.position().x() < LIST_W:
            return
        f, v = self.to_fv(e.position())
        k = 0.85 if e.angleDelta().y() > 0 else 1 / 0.85
        f0, f1, v0, v1 = self.view
        zf = not (e.modifiers() & Qt.ControlModifier)
        zv = not (e.modifiers() & Qt.ShiftModifier)
        if zf:
            f0, f1 = f + (f0 - f) * k, f + (f1 - f) * k
        if zv:
            v0, v1 = v + (v0 - v) * k, v + (v1 - v) * k
        self.view = (f0, f1, v0, v1)
        self.update()
        e.accept()

    def _go(self, pos):
        viewer = getattr(self.node, "_viewer", None)
        if viewer is not None:
            viewer.clock.pause()
            viewer.playbar.go(round(self.to_fv(pos)[0]))

    def event(self, e):
        # Del / F belong to the curve editor while it has the focus (the window's Del deletes nodes otherwise)
        if e.type() == e.Type.ShortcutOverride and e.key() in (Qt.Key_Delete, Qt.Key_Backspace, Qt.Key_F):
            e.accept()
            return True
        return super().event(e)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Delete, Qt.Key_Backspace) and self.selected:
            out = {}
            for path, ch, keys, _file in self.channels():
                idx = {i for (p, c, i) in self.selected if p == path and c == ch}
                if idx and keys:
                    out[(path, ch)] = [k for i, k in enumerate(keys) if i not in idx]
            self.selected = set()
            self._store(out)
        elif e.key() == Qt.Key_F:
            self.fit()
        else:
            super().keyPressEvent(e)
