"""Parameter links (Houdini's channel references): one node's parameter follows another node's parameter.

Right-click a parameter > Copy Parameter; on another parameter > Paste Parameter makes the link. The linked
field is greyed and can't be edited; whenever the source changes, its value is copied over (so everything
downstream - the engine included - just sees an ordinary value). Break Connection keeps the last value.
Links are stored per node: node.links = {param name: (source node, source param name)}; they follow renames
(they hold the node itself) and are saved with the graph by name. Deleting a source keeps the values it gave.
"""

import copy

# the parameter last copied: (node, param name) - the app has one, like a clipboard
clipboard = None

NUMBERS = ("int", "float")
TEXTS = ("string", "text")
LINKABLE = NUMBERS + TEXTS + ("bool", "choice", "file", "folder", "image", "audio", "video")


def param_of(node, name):
    return next((p for p in node.parameters if p.name == name), None)


def compatible(src_param, dst_param):
    """Can dst take src's value? Same kind (int <-> float, one-line <-> multi-line text too; choices only with the
    same options)."""
    a, b = src_param.kind, dst_param.kind
    if a not in LINKABLE or b not in LINKABLE:
        return False
    if a in NUMBERS and b in NUMBERS or a in TEXTS and b in TEXTS:
        return True
    if a == "choice" and b == "choice":
        return [v for v, _ in src_param.choices()] == [v for v, _ in dst_param.choices()]
    return a == b


def convert(value, dst_param):
    if dst_param.kind == "int":
        return int(round(float(value)))
    if dst_param.kind == "float":
        return float(value)
    return copy.deepcopy(value)


def copy_param(node, name):
    global clipboard
    clipboard = (node, name)


def can_paste(node, name):
    """(ok, reason) for pasting the clipboard onto node.name."""
    if clipboard is None:
        return False, "nothing copied yet"
    src, sname = clipboard
    if src.scene() is None:
        return False, "the copied node was deleted"
    if src is node and sname == name:
        return False, "that's the same parameter"
    sp, dp = param_of(src, sname), param_of(node, name)
    if not sp or not dp or not compatible(sp, dp):
        return False, f"{sp.label if sp else sname} ({sp.kind if sp else '?'}) doesn't fit here"
    if _depends_on(src, sname, node, name):
        return False, "that would link it to itself"
    return True, ""


def _depends_on(node, name, target, tname):
    """Does node.name (directly or through a chain) follow target.tname?"""
    seen = set()
    while (node, name) not in seen:
        seen.add((node, name))
        if (node, name) == (target, tname):
            return True
        link = getattr(node, "links", {}).get(name)
        if not link:
            return False
        node, name = link
    return True


def paste_value(node, name):
    src, sname = clipboard
    node.set_param(name, convert(src.params[sname], param_of(node, name)))


def paste_link(node, name):
    src, sname = clipboard
    node.links[name] = (src, sname)
    node.set_param(name, convert(src.params[sname], param_of(node, name)))
    _changed(node)


def break_link(node, name):
    node.links.pop(name, None)
    node.notify()
    _changed(node)


def _changed(node):
    if hasattr(node, "links_changed"):
        node.links_changed()
    if node.scene() is not None:
        node.scene().update()  # the thin lines between linked nodes


def describe(node, name):
    """'photo_edit › Width' for a linked parameter, or ''."""
    link = node.links.get(name)
    if not link:
        return ""
    src, sname = link
    sp = param_of(src, sname)
    return f"{src.name} › {sp.label if sp else sname}"


def propagate(scene, source):
    """source's parameters changed: copy them to every parameter that follows them (which may chain on)."""
    for node in scene.nodes():
        for name, (src, sname) in list(getattr(node, "links", {}).items()):
            if src is source:
                value = convert(source.params[sname], param_of(node, name))
                if node.params.get(name) != value:
                    node.set_param(name, value)


def forget(scene, removed):
    """A node is being deleted: whatever followed it keeps its last value."""
    for node in scene.nodes():
        for name, (src, _s) in list(getattr(node, "links", {}).items()):
            if src is removed:
                del node.links[name]
                node.notify()
                _changed(node)


def pairs(scene):
    """Every (source node, node) pair joined by at least one parameter link, once each."""
    seen = set()
    for node in scene.nodes():
        for src, _s in getattr(node, "links", {}).values():
            key = frozenset((id(src), id(node)))
            if key not in seen and src.scene() is scene:
                seen.add(key)
                yield src, node


def to_names(node):
    return {name: [src.name, sname] for name, (src, sname) in node.links.items()}


def from_names(node, data, by_name):
    for name, (src_name, sname) in (data or {}).items():
        src = by_name.get(src_name)
        if src is not None and name in node.params and sname in src.params:
            node.links[name] = (src, sname)
