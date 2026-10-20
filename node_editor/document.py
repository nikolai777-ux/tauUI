"""Saving and loading node graphs as JSON: one file holds every tab's graph."""

import json

from PySide6.QtCore import QPointF

from . import links
from .registry import node_class

FORMAT = "node-editor-graph"
VERSION = 3  # 2: "tabs" list (version 1 files hold a single graph); 3: a Modality's inputs are model, text, data


def graph_to_dict(scene, view=None):
    nodes = scene.nodes()
    data = {
        "format": FORMAT, "version": VERSION,
        "nodes": [{"type": n.type_name, "name": n.name, "pos": [n.pos().x(), n.pos().y()],
                   "params": {k: v for k, v in n.params.items() if v is not None},
                   **({"links": links.to_names(n)} if n.links else {})} for n in nodes],
        "edges": [],
    }
    for n in nodes:  # by input, in each input's own order (the order a model receives pictures in)
        for ii, sock in enumerate(n.input_sockets):
            for e in sock.edges:
                src = e.source
                data["edges"].append({"from": src.node.name, "output": src.node.output_sockets.index(src),
                                      "to": n.name, "input": ii})
    if view is not None:
        c = view.mapToScene(view.viewport().rect().center())
        data["view"] = {"center": [c.x(), c.y()], "zoom": view.transform().m11()}
    return data


MERGE_INPUTS = 5  # a Merge node's inputs


def merge_multi_wires(data):
    """Older graphs could plug several wires into one input; now an input takes one. Each such input gets a Merge
    node (more than 5 wires: Merges chained through the last input), the wires in the order the file lists them.
    Returns (data, notes) - the data is a changed copy when anything was merged."""
    specs = {n["name"]: n for n in data.get("nodes", [])}
    groups = {}
    for e in data.get("edges", []):
        groups.setdefault((e["to"], e["input"]), []).append(e)
    multi = {k: v for k, v in groups.items() if len(v) > 1 and k[0] in specs}
    if not multi:
        return data, []
    data = {**data, "nodes": list(data.get("nodes", [])), "edges": [e for e in data.get("edges", [])
                                                                     if (e["to"], e["input"]) not in multi]}
    taken = set(specs)
    notes = []
    for (to, ii), wires in multi.items():
        cls = node_class(specs[to]["type"])
        input_name = cls.inputs[ii][0] if cls is not None and ii < len(cls.inputs) else str(ii)
        x, y = specs[to].get("pos", (0, 0))

        def merge_for(sources, level):
            base = f"{to}_{input_name}_merge" + (str(level + 1) if level else "")
            name, i = base, 1
            while name in taken:
                name, i = f"{base}_{i}", i + 1
            taken.add(name)
            data["nodes"].append({"type": "merge", "name": name, "pos": [x - 130 * level, y - 110 - 40 * level],
                                  "params": {}})
            here = sources if len(sources) <= MERGE_INPUTS else sources[:MERGE_INPUTS - 1]
            for slot, e in enumerate(here):
                data["edges"].append({"from": e["from"], "output": e["output"], "to": name, "input": slot})
            if len(sources) > MERGE_INPUTS:  # the rest through another Merge in the last input, order kept
                inner = merge_for(sources[MERGE_INPUTS - 1:], level + 1)
                data["edges"].append({"from": inner, "output": 0, "to": name, "input": MERGE_INPUTS - 1})
            return name

        top = merge_for(wires, 0)
        data["edges"].append({"from": top, "output": 0, "to": to, "input": ii})
        notes.append(f"{to} › {input_name}: {', '.join(e['from'] for e in wires)} → {top}")
    return data, notes


def executors_to_modalities(data):
    """The Executor node became the Modality's Executor mode: an older file's Executor nodes load as Modalities
    with the Executor tick on - same name (its work folder and session), the same wires (inputs model, prompt ->
    text, image, video, audio, data keep their positions) and what it built / its conversation."""
    nodes = []
    for spec in data.get("nodes", []):
        if spec.get("type") == "executor":
            old = spec.get("params", {})
            keep = {k: old[k] for k in ("web", "overwrite", "built", "session_id", "log")
                    if k in old}
            spec = {**spec, "type": "modality", "params": {"executor": True, **keep}}
        nodes.append(spec)
    return {**data, "nodes": nodes}


OLD_MATERIAL_INPUTS = (2, 3, 4, 5)  # a Modality's image, video, audio, data inputs before version 3
DATA_INPUT = 2                      # ...now its one data input


def material_to_data(data):
    """Before version 3 a Modality had separate image, video, audio and data inputs; now one data input takes them
    all. One wire moves to it; several go through a Data node, in the order pictures, videos, sounds, data (so
    Picture 1, 2... stay as they were; more than 5: Data nodes chained through the last input).
    Returns (data, notes)."""
    if data.get("version", 1) >= 3:
        return data, []
    specs = {n["name"]: n for n in data.get("nodes", [])}
    targets = {n for n, s in specs.items() if s.get("type") == "modality"}
    wires = {}
    for e in data.get("edges", []):
        if e["to"] in targets and e["input"] in OLD_MATERIAL_INPUTS:
            wires.setdefault(e["to"], []).append(e)
    if not wires:
        return data, []
    data = {**data, "nodes": list(data.get("nodes", [])),
            "edges": [e for e in data.get("edges", []) if not (e["to"] in wires and e["input"] in OLD_MATERIAL_INPUTS)]}
    taken, notes = set(specs), []
    for to, ws in wires.items():
        ws = sorted(ws, key=lambda e: OLD_MATERIAL_INPUTS.index(e["input"]))  # stable: the file's order within one
        if len(ws) == 1:
            data["edges"].append({**ws[0], "input": DATA_INPUT})
            continue
        x, y = specs[to].get("pos", (0, 0))

        def data_for(sources, level):
            base = f"{to}_data" + (str(level + 1) if level else "")
            name, i = base, 1
            while name in taken:
                name, i = f"{base}_{i}", i + 1
            taken.add(name)
            data["nodes"].append({"type": "data", "name": name, "pos": [x - 130 * level, y - 110 - 40 * level],
                                  "params": {}})
            here = sources if len(sources) <= MERGE_INPUTS else sources[:MERGE_INPUTS - 1]
            for slot, e in enumerate(here):
                data["edges"].append({"from": e["from"], "output": e["output"], "to": name, "input": slot})
            if len(sources) > MERGE_INPUTS:
                inner = data_for(sources[MERGE_INPUTS - 1:], level + 1)
                data["edges"].append({"from": inner, "output": 0, "to": name, "input": MERGE_INPUTS - 1})
            return name

        top = data_for(ws, 0)
        data["edges"].append({"from": top, "output": 0, "to": to, "input": DATA_INPUT})
        notes.append(f"{to}: {', '.join(e['from'] for e in ws)} → {top}")
    return data, notes


def load_graph(data, scene, view=None):
    """Replace the scene contents with the graph in `data`. Returns a list of warnings."""
    if data.get("format") != FORMAT:
        raise ValueError("Not a node graph file")
    data, merged = merge_multi_wires(data)
    data = executors_to_modalities(data)
    data, collected = material_to_data(data)
    warnings = []
    if collected:
        warnings.append("A Modality's pictures, videos and sounds now go into its one data input; where several "
                        "came in, a Data node collects them (pictures first, then videos, sounds, data):\n  "
                        + "\n  ".join(collected))
    if merged:
        warnings.append("Several wires into one input became a Merge node (an input takes one wire now). The order "
                        "is the one this older file lists - check it where order matters (Picture 1, 2...), "
                        "then save:\n  " + "\n  ".join(merged))
    for n in scene.nodes():
        n.remove()
    by_name = {}
    for spec in data.get("nodes", []):
        cls = node_class(spec["type"])
        if cls is None:
            warnings.append(f"Unknown node type '{spec['type']}' ({spec['name']}) was skipped")
            continue
        node = cls(spec["name"])
        for key, value in spec.get("params", {}).items():
            if node.accepts_param(key):
                node.params[key] = value
        for key in node.params:  # let nodes react (e.g. a Model node reads its file)
            node.param_changed(key)
        node.setPos(QPointF(*spec.get("pos", (0, 0))))
        scene.addItem(node)
        by_name[node.name] = node
    for spec in data.get("nodes", []):  # parameter links, once every node exists
        if spec.get("links") and spec["name"] in by_name:
            links.from_names(by_name[spec["name"]], spec["links"], by_name)
    scene.graph_changed()  # output types settle first (a Modality's follows its saved result), then the wires

    def connect(e):
        src, dst = by_name.get(e["from"]), by_name.get(e["to"])
        try:
            return bool(src and dst and scene.connect(src.output_sockets[e["output"]], dst.input_sockets[e["input"]]))
        except IndexError:
            return False

    # A wire refused now may fit once the wires before it are in: a node's output type can depend on its own inputs
    # (Isolate gives video only once its shots are connected), so the refused ones are tried again while any fit.
    pending = [e for e in data.get("edges", []) if not connect(e)]
    while pending:
        scene.graph_changed()
        still = [e for e in pending if not connect(e)]
        if len(still) == len(pending):
            break
        pending = still
    warnings += [f"Connection {e['from']} → {e['to']} couldn't be restored" for e in pending]
    scene.graph_changed()
    if view is not None and "view" in data:
        v = data["view"]
        view.resetTransform()
        view.scale(v.get("zoom", 1.0), v.get("zoom", 1.0))
        view.centerOn(QPointF(*v.get("center", (0, 0))))
    return warnings


def tabs_to_dict(tabs, current=0):
    """`tabs`: list of (name, scene, view)."""
    graphs = []
    for name, scene, view in tabs:
        g = graph_to_dict(scene, view)
        del g["format"], g["version"]
        graphs.append({"name": name, **g})
    return {"format": FORMAT, "version": VERSION, "current": current, "tabs": graphs}


def read(path):
    """The tabs stored in a file: (list of (name, graph data), index of the tab that was showing)."""
    with open(path, encoding="utf-8-sig") as f:  # -sig: a file saved by an editor that adds a BOM opens too
        data = json.load(f)
    if data.get("format") != FORMAT:
        raise ValueError("Not a node graph file")
    if "tabs" not in data:  # version 1: a single graph
        return [(None, data)], 0
    tabs = [(t.get("name"), {"format": FORMAT, "version": data.get("version", 1), **t}) for t in data["tabs"]]
    return tabs, data.get("current", 0)


def save(path, tabs, current=0):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tabs_to_dict(tabs, current), f, indent=2, ensure_ascii=False)
