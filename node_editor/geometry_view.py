"""The 3D viewer in the parameter pane (Geometry node, Isolate showing a 3D item): assets/geometry_view.qml in a
QQuickWidget, and under it - when the scene is animated - the same frame timeline and transport as a video
(playbar.Playbar, driven by AnimClock instead of a media player). It follows the node's Scene, Camera file, Camera and
Frame rate and the header's toggles, and hands back what it loaded (bounding box, cameras, animation length, or an
error) as node.geometry_info - shown in the node's info rows and used for the Camera choices.
On an Edit Geometry node (a node with "edits") it is editable: the QML says what is selected and hands over each
finished move, which becomes keys or set values here (curves.commit, one undo step); auto-key and Set Key sit on the
header; the timeline gets trim brackets (t_in / t_out) and red marks at the selection's keys; play runs the kept span
at the node's speed."""

import math
import os

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from shiboken6 import isValid
from PySide6.QtWidgets import QVBoxLayout, QWidget

# the header's toggles (Maya-like icons, hints only): (param, icon, hint); on = checked. Headlight off = the key +
# fill + rim rig (no icon of its own)
TOGGLES = (("headlight", "headlight", "Headlight: one light from the camera (off: key, fill, rim + shadow)"),
           ("wire", "wireframe", "Wireframe on shaded"),
           ("textures", "textures", "Textures: the file's materials (off: plain grey)"),
           ("msaa", "multisample", "Multisample antialiasing"),
           ("grid", "grid", "Grid on the ground plane"),
           ("ground", "ground", "Ground under the model (the shadow falls on it)"),
           ("at_origin", "origin", "Stand the model at the origin (off: where the file puts it)"))
DEFAULTS = {"textures": True, "msaa": True, "grid": True}
DEFAULT_CAMERA = "Default"  # the viewer's own orbit camera
QML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "geometry_view.qml")


class AnimClock(QObject):
    """A scene's animation time, with the part of QMediaPlayer's interface the video Playbar uses (position in ms,
    duration, play / pause, the frame rate as metadata) - so a 3D animation gets the same timeline as a video. Plays in
    real time at its frame rate, looping."""
    positionChanged = Signal(int)
    durationChanged = Signal(int)
    metaDataChanged = Signal()
    playbackStateChanged = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        from PySide6.QtCore import QElapsedTimer
        self._pos, self._duration, self.fps, self._playing = 0, 0, 24.0, False
        self.span, self.speed = None, 1.0  # play only this (ms, ms) span, this much faster (Edit Geometry)
        self.known_fps = 0.0  # what the file / the user says (0: not recorded - the Playbar says "24 assumed")
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._clock = QElapsedTimer()

    # the media player's interface
    def position(self):
        return self._pos

    def duration(self):
        return self._duration

    def setPosition(self, ms):
        ms = max(0, min(int(ms), self._duration))
        if ms != self._pos:
            self._pos = ms
            self.positionChanged.emit(ms)

    def metaData(self):
        clock = self

        class _Meta:
            def value(self, _key):
                return clock.known_fps
        return _Meta()

    def playbackState(self):
        from PySide6.QtMultimedia import QMediaPlayer
        return QMediaPlayer.PlayingState if self._playing else QMediaPlayer.PausedState

    def play(self):
        if self._duration <= 0 or self._playing:
            return
        self._playing, self._start_pos = True, self._pos
        self._clock.start()
        self._timer.start(max(5, round(1000 / self.fps / 2)))
        self.playbackStateChanged.emit(self.playbackState())

    def pause(self):
        if self._playing:
            self._playing = False
            self._timer.stop()
            self.playbackStateChanged.emit(self.playbackState())

    # ours
    def set_timing(self, duration_ms, fps):
        """duration = the scene's last key; fps 0 = not recorded (24 used)."""
        self.known_fps = fps if fps and fps > 0 else 0.0
        fps = self.known_fps or 24.0
        # the last key is the LAST FRAME's time (on a frame; the converter rounds it to whole ms): one frame more, so
        # the Playbar counts it - it floors duration × fps, so the length is rounded UP to whole ms
        frames = round(duration_ms / 1000 * fps) + 1 if duration_ms > 0 else 0
        duration_ms = math.ceil(frames * 1000 / fps) if frames else 0
        changed = (duration_ms, fps) != (self._duration, self.fps)
        self._duration, self.fps = duration_ms, fps
        if changed:
            self.durationChanged.emit(self._duration)
            self.metaDataChanged.emit()
            self.setPosition(self._pos)

    def _tick(self):
        if self._duration <= 0:
            return self.pause()
        a, b = self.span or (0, self._duration)
        start = self._start_pos if a <= self._start_pos <= b else a
        ms = a + (start - a + self._clock.elapsed() * self.speed) % max(1, b - a + 1)
        frame_ms = 1000 / self.fps  # shown frame by frame, like a video
        self.setPosition(math.floor(ms / frame_ms) * frame_ms + 1)


class GeometryView(QWidget):
    def __init__(self, node, param, parent=None):
        super().__init__(parent)
        from PySide6.QtQuickWidgets import QQuickWidget
        from .playbar import Playbar
        self.node, self.param = node, param
        self.view = QQuickWidget(self)
        self.view.setResizeMode(QQuickWidget.SizeRootObjectToView)
        self.view.setSource(QUrl.fromLocalFile(QML))
        self.clock = AnimClock(self)
        self.playbar = Playbar(self.clock)
        self.playbar.setVisible(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.playbar)
        self._set_value = None  # the pane's setter (undo), handed over with the header widgets
        bar = self.playbar.timeline
        bar.trim_dragged.connect(self._trim_drag)
        bar.trim_released.connect(self._trim_done)
        self._key_buttons()
        errors = [e.toString() for e in self.view.errors()]
        if errors:  # QML / Qt Quick 3D not usable here: say so on the node instead of an empty box
            node.geometry_info = {"error": "the 3D viewer can't start: " + "; ".join(errors)[:300]}
        self.clock.positionChanged.connect(self._time)
        self._poll = QTimer(self, interval=300)
        self._poll.timeout.connect(self._read_back)
        self._bind(node, param)

    def _bind(self, node, param):
        """Show this node (and viewer parameter) from now on: its scene, settings, camera, frame, selection."""
        self.node, self.param = node, param
        self._shown = None
        self._reported = None
        self._notes = {"scene": "", "cameras": ""}
        self._seen = {}
        self._edits_shown = None
        self._duration_seen = 0
        self.editable = "edits" in node.params  # an Edit Geometry node
        root = self.view.rootObject()
        self._commit_seen = int(root.property("commitSerial") or 0) if root is not None else 0
        if self.editable:
            node._viewer = self  # the curve editor follows this viewer's time
        bar = self.playbar.timeline
        bar.trim, bar.trim_editable, bar.marks = None, self.editable, []
        self.playbar.display_fps = None
        for b in self._keying:
            b.setVisible(self.editable)
        if root is not None:
            root.setProperty("editable", self.editable)
            root.setProperty("selection", list(getattr(node, "_selection", []) or []) if self.editable else [])
            root.setProperty("edits", {})
        self._poll.start()
        self.refresh(node)

    def set_node(self, node, param=None):
        """The one viewer of the app shows another node (param_panel) - or, with None, nothing (its scene unloaded)."""
        self.sleep()
        if node is None:
            root = self.view.rootObject()
            if root is not None:
                root.setProperty("selection", [])
                root.setProperty("source", QUrl())
                root.setProperty("cameraSource", QUrl())
            self._shown = None
            return
        self._bind(node, param or self.param)

    def _key_buttons(self):
        """Auto-key (red when on, like Maya) and Set Key - on the playbar's row, after the fps (Edit Geometry)."""
        from PySide6.QtCore import QSize
        from PySide6.QtWidgets import QToolButton
        from . import icons
        auto = QToolButton(objectName="AutoKey", toolTip="Auto key: every move sets a key (off: moves only)",
                           checkable=True)
        auto.setIcon(icons.icon("autokey", 14))
        auto.setIconSize(QSize(14, 14))
        auto.toggled.connect(lambda on: self.editable and self._set_value
                             and on != bool(self.node.params.get("autokey")) and self._set_value("autokey", on))
        key = QToolButton(objectName="ViewToggle", toolTip="Set key on what is selected (or the camera looked "
                                                           "through) at this frame")
        key.setIcon(icons.icon("key", 14))
        key.setIconSize(QSize(14, 14))
        key.clicked.connect(self.set_key)
        self.playbar.add_tail(auto)
        self.playbar.add_tail(key)
        self._auto_button, self._keying = auto, (auto, key)

    default_height = 380  # the pane's box: drag the grip under it; double-click = this again

    def _settings(self, node):
        value = node.params.get(self.param.name)
        if isinstance(value, str) and value:
            path = value
        elif hasattr(node, "scene_path"):  # Edit Geometry: the scene wired into it
            path = node.scene_path()
        else:
            path = node.params.get("file") or ""
        p = node.params
        return ((path, p.get("camera_file") or "", p.get("camera") or DEFAULT_CAMERA, float(p.get("fps") or 0))
                + tuple(bool(p.get(name, DEFAULTS.get(name, False))) for name, _i, _h in TOGGLES))

    def refresh(self, node):
        """The node changed: show its current file with its current settings (only what differs is set)."""
        root = self.view.rootObject()
        self.live_refresh(node)
        settings = self._settings(node)
        if root is None or settings == self._shown:
            return
        path, camera_file, camera, fps = settings[:4]
        flags = dict(zip((name for name, _i, _h in TOGGLES), settings[4:]))
        root.setProperty("atOrigin", flags["at_origin"])
        for name in ("textures", "wire", "msaa", "grid"):
            root.setProperty(name, flags[name])
        if self._shown is None or path != self._shown[0]:
            self._load(path)
        if self._shown is None or camera_file != self._shown[1]:
            self._load_cameras(camera_file)
        root.setProperty("cameraName", "" if camera == DEFAULT_CAMERA else camera)
        root.setProperty("headlight", flags["headlight"])
        root.setProperty("ground", flags["ground"])
        self._timing(root)
        self._shown = settings
        self._sync_menus()

    def live_refresh(self, node):
        """Every node change (the pane calls refresh): the edits and the time settings, which aren't in _settings."""
        if not self.editable:
            return
        root = self.view.rootObject()
        edits = node.params.get("edits") or {}
        if root is not None and edits is not self._edits_shown:
            self._edits_shown = edits
            root.setProperty("edits", edits)
        self._timing(root)
        self._marks()

    def header_widgets(self, set_value):
        """The viewer's toggles on the box's header line beside "View" - icons with hints, like Maya's viewport bar -
        then the camera looked through (only on a node that keeps these settings)."""
        from PySide6.QtCore import QSize
        from PySide6.QtWidgets import QComboBox, QHBoxLayout, QToolButton, QWidget
        from . import icons
        if "headlight" not in self.node.params:
            return []
        self._buttons = {}
        strip = QWidget()  # small icons close together, like Maya's bar
        row = QHBoxLayout(strip)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        for name, icon, hint in TOGGLES:
            b = QToolButton(objectName="ViewToggle", toolTip=hint, checkable=True)
            b.setIcon(icons.icon(icon, 14))
            b.setIconSize(QSize(14, 14))
            row.addWidget(b)
            b.toggled.connect(lambda on, n=name: on != bool(self.node.params.get(n, DEFAULTS.get(n, False)))
                              and set_value(n, on))
            self._buttons[name] = b
        self._set_value = set_value
        self._camera_box = None
        if "camera" in self.node.params:
            box = QComboBox(objectName="ViewCamera", toolTip="Camera looked through (Default: orbit with the mouse)")
            box.setSizeAdjustPolicy(QComboBox.AdjustToContents)
            box.setFixedHeight(b.sizeHint().height())  # as tall as the toggles
            box.currentIndexChanged.connect(lambda i: box.itemData(i) is not None
                                            and box.itemData(i) != (self.node.params.get("camera") or DEFAULT_CAMERA)
                                            and set_value("camera", box.itemData(i)))
            row.addSpacing(6)
            row.addWidget(box)
            self._camera_box = box
        self._sync_menus()
        return [strip]

    def _sync_menus(self):
        for name, b in getattr(self, "_buttons", {}).items():
            if not isValid(b):
                continue
            b.blockSignals(True)
            b.setChecked(bool(self.node.params.get(name, DEFAULTS.get(name, False))))
            b.blockSignals(False)
        auto = getattr(self, "_auto_button", None)
        if auto is not None:
            auto.blockSignals(True)
            auto.setChecked(bool(self.node.params.get("autokey")))
            auto.blockSignals(False)
        box = getattr(self, "_camera_box", None)
        if box is not None and isValid(box):  # Default + the cameras the files brought (a saved one not loaded yet stays, marked)
            current = self.node.params.get("camera") or DEFAULT_CAMERA
            names = (getattr(self.node, "geometry_info", None) or {}).get("cameras") or []
            items = [(DEFAULT_CAMERA, DEFAULT_CAMERA)] + [(n, n) for n in names]
            if current not in [v for v, _l in items]:
                items.append((current, f"{current} (not loaded)"))
            box.blockSignals(True)
            if [(box.itemData(i), box.itemText(i)) for i in range(box.count())] != items:
                box.clear()
                for value, label in items:
                    box.addItem(label, value)
            box.setCurrentIndex(max(0, box.findData(current)))
            box.blockSignals(False)

    # ---- files → converted scenes (scene_cache.py) ---------------------------------------------------------------
    def _load(self, path):
        from . import scene_cache
        root = self.view.rootObject()
        self._reported = None
        seen = getattr(self.node, "_camera", None)  # this node's last view of this file: back to it, not framed
        root.setProperty("restore", list(seen[1]) if seen and seen[0] == path else [])
        root.setProperty("source", QUrl())
        root.setProperty("recordedFps", 0.0)
        root.setProperty("sceneFbx", path.lower().endswith(".fbx"))
        self.clock.pause()
        self.clock.set_timing(0, 0)
        self._duration_seen = 0
        if not path or not os.path.isfile(path):
            self._notes["scene"] = f"error: no such file: {path}" if path else ""
            return
        self._notes["scene"] = "converting…"
        scene_cache.start(path, lambda qml, note, p=path: self._converted("source", 0, p, qml, note))

    def _load_cameras(self, path):
        from . import scene_cache
        root = self.view.rootObject()
        root.setProperty("cameraSource", QUrl())
        root.setProperty("camFileFbx", path.lower().endswith(".fbx"))
        if not path or not os.path.isfile(path):
            self._notes["cameras"] = ""
            return
        self._notes["cameras"] = "converting…"
        scene_cache.start(path, lambda qml, note, p=path: self._converted("cameraSource", 1, p, qml, note))

    def _converted(self, prop, index, path, qml, note):
        root = self.view.rootObject() if self._poll.isActive() else None
        if root is None or (self._shown and self._shown[index] != path):
            return  # released, or another file meanwhile
        self._notes["scene" if index == 0 else "cameras"] = note if qml else ("error: " + note)
        if qml:
            if index == 0:  # a rate the converter read from the file (BVH), for a scene that keeps none
                from . import scene_cache
                root.setProperty("recordedFps", scene_cache.recorded_fps(path))
            root.setProperty(prop, QUrl.fromLocalFile(qml))
            if index == 0:  # the time this node was at before (the same file)
                at = getattr(self.node, "_anim_ms", None)
                self._resume_ms = at[1] if at and at[0] == path else 0

    # ---- editing (Edit Geometry) ------------------------------------------------------------------------------
    def set_key(self):
        root = self.view.rootObject()
        if root is not None:
            root.setProperty("keySerial", int(root.property("keySerial") or 0) + 1)

    def _commit(self, data):
        """A finished move (or Set Key) from the viewer → the node's edits, one undo step."""
        from . import curves
        import json
        try:
            data = json.loads(data)
        except ValueError:
            return
        animated = getattr(self.node, "_animated", {}) or {}
        edits = self.node.params.get("edits") or {}
        new = edits
        wanted = data.get("offsets") or {}
        for path, values in (data.get("moves") or {}).items():
            if data.get("keyAll"):
                new = curves.key_all(new, path, values, wanted.get(path) or {}, data.get("frame", 1),
                                     animated.get(path, []))
            else:
                new = curves.commit(new, path, values, wanted.get(path) or {}, data.get("frame", 1),
                                    bool(self.node.params.get("autokey")), animated.get(path, []))
        if new != edits and self._set_value:
            self._set_value("edits", new)

    def _marks(self):
        """Red marks on the timeline: the frames the selection (or the camera looked through) has keys on."""
        edits = self.node.params.get("edits") or {}
        paths = self.node._selection or ([self.node._active_cam_path] if self.node._active_cam_path else [])
        frames = sorted({round(k[0]) for p in paths for kind in ("keys", "offsets")
                         for ks in ((edits.get(p) or {}).get(kind) or {}).values() for k in ks})
        if frames != self.playbar.timeline.marks:
            self.playbar.timeline.marks = frames
            self.playbar.timeline.update()

    def _trim_drag(self, which, frame):
        if not self.editable:
            return
        bar = self.playbar.timeline
        a, b = bar.trim
        bar.trim = (frame, b) if which == "in" else (a, frame)
        bar.update()

    def _trim_done(self):
        if not self.editable or not self.playbar.timeline.trim:
            return
        a, b = self.playbar.timeline.trim
        n = self.node.scene_frames()
        if self._set_value:
            self._set_value("t_in", 0 if a <= 1 else a)
            self._set_value("t_out", 0 if b >= n else b)

    def _read_edit_state(self, root):
        """Selection, the camera looked through, the file's animated channels and curves, and finished moves."""
        import json
        changed = False
        for name, attr, parse in (("selection", "_selection", lambda v: [str(x) for x in (_plain(v) or [])]),
                                  ("activeCamPath", "_active_cam_path", lambda v: str(v or "")),
                                  ("samplesJson", "_samples", lambda v: json.loads(v or "{}")),
                                  ("animatedJson", "_animated", lambda v: json.loads(v or "{}"))):
            raw = root.property(name)
            raw = _plain(raw)
            key = json.dumps(raw, sort_keys=True, default=str)
            if self._seen.get(name) != key:
                self._seen[name] = key
                setattr(self.node, attr, parse(raw))
                changed = True
        serial = int(root.property("commitSerial") or 0)
        if serial != self._commit_seen:
            self._commit_seen = serial
            self._commit(root.property("commitJson") or "")
        if changed:
            self._marks()
            self.node.notify()

    # ---- kept for the session (param_panel: its page is only shown / hidden) ---------------------------------------
    def sleep(self):
        """Another node is shown: still loaded; it stops asking the QML and its animation stops."""
        self._poll.stop()
        self.clock.pause()

    def wake(self):
        """Its node is shown again: as it was (scene, camera, frame); settings changed meanwhile are taken now."""
        if not self._poll.isActive():
            self._poll.start()
        self.refresh(self.node)

    def unload(self):
        """Its node was deleted: the scene goes (memory), the viewer stays - undo may bring the node back, and it
        reloads then."""
        root = self.view.rootObject()
        if root is not None and self._shown is not None:
            root.setProperty("selection", [])
            root.setProperty("source", QUrl())
            root.setProperty("cameraSource", QUrl())
        self._shown = None

    def release(self):
        """The pane is about to drop this viewer: stop it and unload its scenes; the pane then deletes it as usual.
        (Measured 2026-10-02 with the real pane switching nodes 40-150 times: this survives. Flushing the clean-up
        inside the pane's rebuild crashed in Qt Quick's item tree, and taking the viewer out of the pane to delete it
        later crashed at once. The old crash - QSSGRenderRoot::markDirty, a 3D view going away while its objects were
        still being cleaned up - came from views created / destroyed all the time: the viewer's views now live as
        long as it does, and `alive` = false stops its timers here.)"""
        self._poll.stop()
        self.clock.pause()
        root = self.view.rootObject()
        if root is not None:
            root.setProperty("alive", False)  # its timers stop; the wireframe scene unloads
            root.setProperty("selection", [])
            root.setProperty("source", QUrl())
            root.setProperty("cameraSource", QUrl())

    # ---- time -----------------------------------------------------------------------------------------------------
    def _timing(self, root):
        """The clock's length and rate (the file's own rate, else not recorded); on an Edit Geometry node also the
        kept span (trim brackets) and the speed it plays at, and the scene's frame rate the keys count in."""
        if root is None:
            return
        file_fps = float(root.property("fileFps") or 0)
        self.clock.set_timing(getattr(self, "_duration_seen", 0), file_fps)
        if not self.editable:
            return
        root.setProperty("keyFps", file_fps or 24.0)
        n = self.node.scene_frames()
        if n:
            a, b = self.node.kept()
            fps = file_fps or 24.0
            self.clock.span = ((a - 1) * 1000 / fps, (b - 1) * 1000 / fps + 1)
            # every frame kept: played at the node's frame rate - that is its speed; the playbar says that rate
            out = self.node.out_fps()
            self.clock.speed = max(0.01, out / fps)
            self.playbar.display_fps = out
            self.playbar._sync(self.clock.position())
            bar = self.playbar.timeline
            if bar.trim != (a, b) and not bar._grab:
                bar.trim = (a, b)
                bar.update()

    def _time(self, ms):
        root = self.view.rootObject()
        if root is not None:
            root.setProperty("animMs", float(ms))
        if self.editable:
            self.node._frame = self.playbar.current()
        if self._shown:
            self.node._anim_ms = (self._shown[0], ms)  # kept for the next viewer of this node

    # ---- what the viewer loaded -------------------------------------------------------------------------------------
    def _read_back(self):
        root = self.view.rootObject()
        if root is None:
            return
        state = root.property("loadState") or ""
        scene_note = self._notes["scene"]
        if scene_note.startswith("error"):
            state = scene_note
        elif not state and scene_note == "converting…":
            state = ""
        size = tuple(round(float(root.property(k) or 0), 4) for k in ("sizeX", "sizeY", "sizeZ"))
        centre = tuple(round(float(root.property(k) or 0), 4) for k in ("centreX", "centreY", "centreZ"))
        cameras = _plain(root.property("cameraNames")) or []
        duration = float(root.property("durationMs") or 0)
        info = {"state": state, "size": size if state == "ok" else None, "centre": centre if state == "ok" else None,
                "note": "" if scene_note in ("converting…",) or scene_note.startswith("error") else scene_note,
                "converting": scene_note == "converting…", "cameras": [str(c) for c in cameras],
                "camera_note": self._notes["cameras"], "duration_ms": duration if state == "ok" else 0,
                "file_fps": float(root.property("fileFps") or 0)}
        if self.editable:
            self._read_edit_state(root)
        if state == "ok" and duration != getattr(self, "_duration_seen", 0):
            self._duration_seen = duration
            self.node.geometry_info = {**(self.node.geometry_info or {}), "duration_ms": duration,
                                       "file_fps": float(root.property("fileFps") or 0)}
            if self.editable:
                self.node.follow_fps()
            self._timing(root)
            self.playbar.setVisible(duration > 0)
            resume = getattr(self, "_resume_ms", 0)
            if resume:
                self.clock.setPosition(resume)
                self._resume_ms = 0
        camera = _plain(root.property("cameraState"))
        if state == "ok" and self._shown and camera is not None:  # kept for the next viewer of this node
            try:
                self.node._camera = (self._shown[0], [float(v) for v in camera])
            except (TypeError, ValueError):
                pass
        if info != self._reported:
            self._reported = info
            self.node.geometry_info = info
            self._sync_menus()  # the camera list may have changed
            self.node.notify()


def _plain(value):
    """A QML array arrives as a QJSValue: a Python list."""
    return value.toVariant() if hasattr(value, "toVariant") else value
