"""Copy / paste of nodes, through the system clipboard (so it works across tabs and between app windows).

The copied nodes travel as JSON (like a saved graph): their settings - not results -, positions, the wires
between them, and their parameter links. Pasting makes new nodes with unique names, keeps their layout, puts
them at the mouse and selects them. Wires to nodes that weren't copied are dropped (like Houdini). A link whose
source was copied points to the copy; a link to a node outside the copy is kept when pasting into the same tab
and dropped in another one (links can't cross graphs) - the value stays either way.
Results aren't copied: a pasted Modality is empty, a pasted Edit renders its own instance.
The parameter clipboard (Copy Parameter, links.py) is separate.
"""

import json
import os

from PySide6.QtCore import QMimeData, QPointF
from PySide6.QtWidgets import QApplication

from . import links, undo
from .registry import node_class

MIME = "application/x-node-editor-nodes"
FORMAT = "node-editor-nodes"


def _graph_id(scene):
    """Which graph the nodes came from: links to nodes outside the copy only make sense back in it."""
    return f"{os.getpid()}:{id(scene)}"


def to_data(scene, nodes):
    names = {n.name for n in nodes}
    data = {"format": FORMAT, "graph": _graph_id(scene), "nodes": [], "edges": []}
    for n in nodes:
        kinds = {p.name: p.kind for p in n.parameters}
        data["nodes"].append({
            "type": n.type_name, "name": n.name, "pos": [n.pos().x(), n.pos().y()],
            "params": {k: v for k, v in n.params.items() if v is not None and kinds.get(k) not in undo.NOT_RECORDED
                       and k not in n.not_copied},
            **({"links": links.to_names(n)} if n.links else {})})
        for oi, sock in enumerate(n.output_sockets):
            for e in sock.edges:
                if e.target.node.name in names:
                    data["edges"].append({"from": n.name, "output": oi, "to": e.target.node.name,
                                          "input": e.target.node.input_sockets.index(e.target)})
    return data


def copy(scene, nodes):
    if not nodes:
        return False
    mime = QMimeData()
    mime.setData(MIME, json.dumps(to_data(scene, nodes), ensure_ascii=False).encode("utf-8"))
    QApplication.clipboard().setMimeData(mime)
    return True


def clipboard_data():
    mime = QApplication.clipboard().mimeData()
    if mime is None or not mime.hasFormat(MIME):
        return None
    try:
        data = json.loads(bytes(mime.data(MIME)).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if data.get("format") == FORMAT else None


def paste(scene, data, at=None, offset=None, verb="Paste"):
    """Make the nodes in `data` in `scene`, centred on `at` (scene point) or moved by `offset`; one undo step.
    Returns the new nodes."""
    specs = [s for s in data.get("nodes", []) if node_class(s.get("type"))]
    if not specs:
        return []
    xs = [s.get("pos", (0, 0))[0] for s in specs]
    ys = [s.get("pos", (0, 0))[1] for s in specs]
    if at is not None:
        shift = at - QPointF((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
    else:
        shift = offset or QPointF()
    taken = {n.name for n in scene.nodes()}
    made = {}  # copied name -> new node
    for spec in specs:
        name = scene.unique_name(spec["name"], taken)
        taken.add(name)
        node = node_class(spec["type"])(name)
        for key, value in spec.get("params", {}).items():
            if node.accepts_param(key):
                node.params[key] = value
        for key in node.params:  # let nodes react (e.g. a Model node reads its file)
            node.param_changed(key)
        node.setPos(QPointF(*spec.get("pos", (0, 0))) + shift)
        scene.addItem(node)
        made[spec["name"]] = node
    # links: to a copied node -> its copy; to a node outside -> only back in the same graph
    outside = {n.name: n for n in scene.nodes() if n not in made.values()} \
        if data.get("graph") == _graph_id(scene) else {}
    for spec in specs:
        node = made[spec["name"]]
        for pname, (src_name, sname) in (spec.get("links") or {}).items():
            src = made.get(src_name) or outside.get(src_name)
            if src is not None and pname in node.params and sname in src.params:
                node.links[pname] = (src, sname)
    scene.graph_changed()  # output types settle first, then the wires
    for e in data.get("edges", []):
        src, dst = made.get(e["from"]), made.get(e["to"])
        try:
            if src and dst:
                scene.connect(src.output_sockets[e["output"]], dst.input_sockets[e["input"]])
        except IndexError:
            pass
    nodes = list(made.values())
    for node in nodes:  # linked values follow their sources (an outside source may have changed since)
        for pname, (src, sname) in node.links.items():
            value = links.convert(src.params[sname], links.param_of(node, pname))
            if node.params.get(pname) != value:
                node.set_param(pname, value)
        if node.links:
            links._changed(node)
    scene.clearSelection()
    for node in nodes:
        node.setSelected(True)
    scene.graph_changed()
    what = ", ".join(n.name for n in nodes[:3]) + ("…" if len(nodes) > 3 else "")
    scene.undo_stack.push(undo.AddNodes(scene, nodes, f"{verb} {what}"))
    return nodes
