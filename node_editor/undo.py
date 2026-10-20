"""Undo / redo: one QUndoStack per graph tab (scene.undo_stack); the window's QUndoGroup follows the current tab.

Only user actions are recorded, where they happen: the parameter pane, the crop box / pen, dragging nodes,
wiring, renaming, adding and deleting. What follows from them - Edit Media's size-follow, linked parameters copying
their source, a Model node re-reading its file, render results - is not recorded; after an undo it recomputes
the way it always does.
A parameter edit is recorded as the node's values (and links) before / after, so an action that changes several
values at once (a 90° turn swapping Width / Height, Reset) is one step. Results (the *_view parameters) are
never part of it. Typing into one field is one step per visit to the field (edits merge until it loses focus).
"""

import copy
from contextlib import contextmanager

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QUndoCommand

# parameter kinds that are results or buttons, not settings
NOT_RECORDED = ("image_view", "geometry_view", "curve_editor", "text_view", "audio_view", "video_view", "data_view", "log_view", "media_picture", "media_timeline",
                "button", "icon", "chat_view")

_recording = 0   # inside record(): nested edits belong to the outer one
_applying = 0    # inside an undo / redo: whatever it sets off isn't a new user action


@contextmanager
def _apply():
    global _applying
    _applying += 1
    try:
        yield
    finally:
        _applying -= 1


# ---- parameters -------------------------------------------------------------------------------------------------
def snapshot(node):
    kinds = {p.name: p.kind for p in node.parameters}
    params = {k: copy.deepcopy(v) for k, v in node.params.items() if kinds.get(k) not in NOT_RECORDED}
    return params, dict(node.links)


def restore(node, snap):
    from . import links
    params, lnks = snap
    changed = [k for k, v in params.items() if node.params.get(k) != v]
    node.prepareGeometryChange()
    for k in changed:
        node.params[k] = copy.deepcopy(params[k])
    relinked = node.links != lnks
    node.links = dict(lnks)
    node.params_restored(changed)
    if relinked:
        links._changed(node)
    node.notify()


@contextmanager
def record(node, text, merge=None):
    """Wrap a user's edit of one node: its values before / after become one undo step (none if nothing changed).
    merge: edits with the same key right after each other become one step (typing into a field)."""
    global _recording
    scene = node.scene()
    if scene is None or _recording or _applying:
        yield
        return
    before = snapshot(node)
    _recording += 1
    try:
        yield
    finally:
        _recording -= 1
        if node.scene() is scene:
            after = snapshot(node)
            if after != before:
                scene.undo_stack.push(ParamEdit(node, before, after, text, merge))


class Pending:
    """record() for an edit spread over several events (e.g. dragging a trim bracket): values taken now, the step
    pushed by finish() - one step however many values the drag went through."""

    def __init__(self, node, text):
        self.node, self.text, self.scene = node, text, node.scene()
        self.before = snapshot(node)

    def finish(self):
        if self.scene is not None and self.node.scene() is self.scene and not _applying:
            after = snapshot(self.node)
            if after != self.before:
                self.scene.undo_stack.push(ParamEdit(self.node, self.before, after, self.text))


class _FocusCounter(QObject):
    """Counts how often a field got the focus: typing merges into one step only within one visit."""

    def eventFilter(self, obj, event):
        if event.type() == QEvent.FocusIn:
            obj.setProperty("undo_visit", (obj.property("undo_visit") or 0) + 1)
        return False


_focus_counter = _FocusCounter()


def typing_key(widget, node, name):
    if not widget.property("undo_tracked"):
        widget.setProperty("undo_tracked", True)
        widget.installEventFilter(_focus_counter)
    return id(node), name, id(widget), widget.property("undo_visit")


class ParamEdit(QUndoCommand):
    def __init__(self, node, before, after, text, merge=None):
        super().__init__(text)
        self.node, self.before, self.after, self.merge = node, before, after, merge
        self._pushed = True  # push() calls redo(); the edit is already done

    def id(self):
        return 1 if self.merge is not None else -1

    def mergeWith(self, other):
        if not isinstance(other, ParamEdit) or other.merge != self.merge or other.node is not self.node:
            return False
        self.after = other.after
        self.setObsolete(self.after == self.before)  # typed and deleted again: nothing left to undo
        return True

    def redo(self):
        if self._pushed:
            self._pushed = False
            return
        with _apply():
            restore(self.node, self.after)

    def undo(self):
        with _apply():
            restore(self.node, self.before)


# ---- wires ------------------------------------------------------------------------------------------------------
def edge_state(sockets):
    """{socket: [(output socket, input socket)...] in connection order} for the sockets and the far ends of
    their wires (the order decides e.g. which prompt comes first)."""
    sockets = set(sockets)
    sockets |= {s for sock in list(sockets) for e in sock.edges for s in (e.source, e.target)}
    return {s: [(e.source, e.target) for e in s.edges] for s in sockets}


def apply_edges(scene, state):
    from .edge import Edge
    want = [p for pairs in state.values() for p in pairs]
    for sock in state:
        for e in list(sock.edges):
            if (e.source, e.target) not in want:
                e.remove()
    have = {(e.source, e.target) for sock in state for e in sock.edges}
    for pair in dict.fromkeys(want):
        if pair not in have:
            scene.addItem(Edge(*pair))
            have.add(pair)
    for sock, pairs in state.items():
        sock.edges.sort(key=lambda e, pairs=pairs: pairs.index((e.source, e.target))
                        if (e.source, e.target) in pairs else len(pairs))
        sock.update()
    scene.graph_changed()


class EdgesEdit(QUndoCommand):
    """Wires connected (and the ones that replaced) - recorded as the sockets' wires before / after."""

    def __init__(self, scene, before, after, text):
        super().__init__(text)
        self.scene, self.before, self.after = scene, before, after
        self._pushed = True

    def redo(self):
        if self._pushed:
            self._pushed = False
            return
        with _apply():
            apply_edges(self.scene, self.after)

    def undo(self):
        with _apply():
            apply_edges(self.scene, self.before)


# ---- nodes ------------------------------------------------------------------------------------------------------
class RemoveItems(QUndoCommand):
    """Delete nodes and / or wires. The node objects are kept; undo puts them back with their wires and the
    parameter links that pointed to them (a running job isn't resumed)."""

    def __init__(self, scene, nodes, edges, text):
        super().__init__(text)
        self.scene, self.nodes = scene, list(nodes)
        gone = {(e.source, e.target) for e in edges} | {(e.source, e.target) for n in self.nodes for e in n.edges()}
        self.edges_before = edge_state({s for n in self.nodes for s in n.sockets}
                                       | {s for e in edges for s in (e.source, e.target)})
        self.edges_after = {s: [p for p in pairs if p not in gone] for s, pairs in self.edges_before.items()}
        self.links = [(n, name, link) for n in scene.nodes() for name, link in n.links.items()
                      if link[0] in self.nodes]

    def redo(self):
        with _apply():
            self._take_out()

    def undo(self):
        with _apply():
            self._put_back()

    def _take_out(self):
        apply_edges(self.scene, self.edges_after)
        for n in self.nodes:
            n.remove()
        self.scene.modified.emit()

    def _put_back(self):
        from . import links
        for n in self.nodes:
            self.scene.addItem(n)
            n.readded()
        apply_edges(self.scene, self.edges_before)
        for n, name, link in self.links:
            n.links[name] = link
            links._changed(n)
            n.notify()
        if self.nodes:
            self.scene.clearSelection()
            for n in self.nodes:
                n.setSelected(True)
        self.scene.graph_changed()


class AddNodes(RemoveItems):
    """Nodes just made (Tab menu, a drop, a paste): undo takes them out again."""

    def __init__(self, scene, nodes, text):
        super().__init__(scene, nodes, [], text)
        self._pushed = True

    def redo(self):
        if self._pushed:
            self._pushed = False
            return
        with _apply():
            self._put_back()

    def undo(self):
        with _apply():
            self._take_out()


class MoveNodes(QUndoCommand):
    def __init__(self, before, after, text):
        super().__init__(text)
        self.before, self.after = before, after
        self._pushed = True

    def _set(self, positions):
        with _apply():
            for node, pos in positions.items():
                node.setPos(pos)

    def redo(self):
        if self._pushed:
            self._pushed = False
            return
        self._set(self.after)

    def undo(self):
        self._set(self.before)


class Rename(QUndoCommand):
    def __init__(self, node, old, new):
        super().__init__(f"Rename {old} to {new}")
        self.node, self.old, self.new = node, old, new
        self._pushed = True

    def _set(self, name):
        node = self.node
        node.prepareGeometryChange()
        node.name = name
        node.update()
        scene = node.scene()
        if scene is not None:
            scene.node_renamed.emit(node)
            scene.modified.emit()
            scene.update()  # link lines / anything showing names

    def redo(self):
        if self._pushed:
            self._pushed = False
            return
        self._set(self.new)

    def undo(self):
        self._set(self.old)


def applying():
    """True while an undo / redo runs (its side effects aren't new user actions)."""
    return bool(_applying)
