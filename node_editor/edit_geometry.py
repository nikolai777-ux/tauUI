"""Edit Geometry: the 3D scene coming in (a Geometry node), edited - objects moved / turned / scaled with a handle
and keyed, the camera looked through moved and keyed, the scene's time trimmed, sped up or slowed down, at a frame
rate of its own. Like Edit Media for pictures / video / sound.

The pane: the viewer (geometry_view.py, editable: click to select, the handle, auto-key / Set Key on its header), the
timeline under it (trim brackets), and the curve editor (curve_editor.py) for what is selected - or, with nothing
selected, the camera looked through (the viewer's own Default camera is not part of the scene and can't be keyed).
What is kept: param "edits" (curves.py) - keys and set values per object, in the scene's own frames; t_in / t_out
(the kept frames), speed, fps. Every change is one undo step. The output is the same scene file; the edits live in
this node and show in its viewer - writing them into a file is not built yet (the info rows say so)."""

import math
import os

from .node import GEOMETRY, Node, Param, set_hints
from .registry import register_node


def _has_scene(n):
    return bool(n.scene_path())


@register_node
class EditGeometryNode(Node):
    type_name = "edit_geometry"
    label = "Edit Geometry"
    shape_name = "pencil"
    inputs = [("geometry", GEOMETRY)]
    outputs = [("geometry", GEOMETRY)]
    parameters = [
        Param("view", "View", "geometry_view", None, visible=_has_scene),
        Param("curves", "Curves", "curve_editor", None, visible=_has_scene),
        # the rate it plays at - every frame kept, so it IS the speed (no Speed of its own); shows the scene's own
        # rate until changed (follow_fps), like Edit Media's
        Param("fps", "Frame rate", "float", 0.0, step=1, minimum=1, maximum=240, visible=_has_scene),
        # the kept frames of the scene's timeline (0 = its first / last) - the brackets on the timeline
        Param("t_in", "Start", "data", 0),
        Param("t_out", "End", "data", 0),
        Param("edits", "Edits", "data", {}),
        Param("autokey", "Auto key", "data", False),
        # the viewer's own settings (header toggles, the camera looked through) - as on the Geometry node
        Param("headlight", "Headlight", "data", False),
        Param("textures", "Textures", "data", True),
        Param("wire", "Wireframe", "data", False),
        Param("msaa", "Multisample", "data", True),
        Param("grid", "Grid", "data", True),
        Param("at_origin", "At origin", "data", False),
        Param("ground", "Ground", "data", False),
        Param("camera", "Camera", "data", "Default"),
    ]

    def __init__(self, name):
        super().__init__(name)
        self.geometry_info = {}  # what the viewer loaded (geometry_view.py)
        self._selection = []     # selected object paths (the viewer's)
        self._active_cam_path = ""
        self._samples = {}       # the file's own curves of what the curve editor shows
        self._animated = {}      # path → channels the file animates

    # ---- what comes in ----------------------------------------------------------------------------------------
    def scene_path(self):
        values = [v for v in self.input_values("geometry") if isinstance(v, str) and v]
        return values[0] if values else ""

    def value(self):
        return self.scene_path() or None

    def upstream_changed(self):
        self.notify()

    # ---- time -------------------------------------------------------------------------------------------------
    def scene_fps(self):
        return float(self.geometry_info.get("file_fps") or 0) or 24.0

    def scene_frames(self):
        ms = self.geometry_info.get("duration_ms") or 0
        return round(ms / 1000 * self.scene_fps()) + 1 if ms else 0  # the last key sits on a frame (ms are rounded)

    def kept(self):
        """(first, last) scene frames kept."""
        n = self.scene_frames()
        a = int(self.params.get("t_in") or 1)
        b = int(self.params.get("t_out") or n)
        return max(1, min(a, n)), max(1, min(b, n))

    def out_fps(self):
        return float(self.params.get("fps") or 0) or self.scene_fps()

    def follow_fps(self):
        """Frame rate shows the scene's own until changed (a value still equal to the one followed before - or 0 -
        follows); the scene's rate is known once the viewer has loaded it."""
        if not self.geometry_info.get("duration_ms"):
            return
        now = self.scene_fps()
        cur = float(self.params.get("fps") or 0)
        if (not cur or cur == getattr(self, "_fps_followed", None)) and cur != now and "fps" not in self.links:
            self.params["fps"] = now
            self.notify()
        self._fps_followed = now

    def result_frames(self):
        """Frames out: every kept frame, played at the node's frame rate."""
        a, b = self.kept()
        return b - a + 1, self.out_fps()

    # ---- the pane ---------------------------------------------------------------------------------------------
    def subtitle(self):
        path = self.scene_path()
        if not path:
            return "no scene"
        keyed = sum(1 for e in (self.params.get("edits") or {}).values() if e.get("keys") or e.get("offsets"))
        return os.path.basename(path) + (f" · {keyed} keyed" if keyed else "")

    def problem(self):
        state = self.geometry_info.get("state", "") or self.geometry_info.get("error", "")
        return state if state.startswith(("error", "the 3D viewer")) else ""

    def info_rows(self):
        path = self.scene_path()
        if not path:
            return [("Scene", "connect a Geometry node")]
        rows = [("Scene", path)]
        info = self.geometry_info
        if info.get("converting"):
            rows.append(("Status", "converting…"))
        if info.get("duration_ms"):
            a, b = self.kept()
            n, fps = self.result_frames()
            scene_rate = f"{self.scene_fps():g} fps" + ("" if info.get("file_fps") else " (assumed)")
            rows.append(("Scene time", f"frames {a}–{b} of {self.scene_frames()} at {scene_rate}"))
            rows.append(("Result", f"{n} frames at {fps:g} fps = {n / fps:.2f} s"
                         + (f" (×{fps / self.scene_fps():.3g} speed)" if abs(fps - self.scene_fps()) > 1e-6 else "")))
        edits = self.params.get("edits") or {}
        if edits:
            keyed = [p for p, e in edits.items() if e.get("keys") or e.get("offsets")]
            moved = [p for p, e in edits.items() if e.get("set") and p not in keyed]
            parts = ([f"{len(keyed)} keyed"] if keyed else []) + ([f"{len(moved)} moved"] if moved else [])
            rows.append(("Edits", ", ".join(parts) + " - shown here; writing them into a file is not built yet"))
        if self._selection:
            rows.append(("Selected", ", ".join(p.split("/")[-1] for p in self._selection)))
        return rows


set_hints(EditGeometryNode, {
    "view": "Click to select; drag the handle to move, turn, scale",
    "curves": "Keys of what is selected (or the camera)",
    "fps": "Frames per second it plays at (= its speed)",
})
