"""Keyframed channels of the Edit Geometry node: the curves between keys and the node's edits.

A channel is one number of an object's transform - tx ty tz (position), rx ry rz (rotation, degrees), sx sy sz
(scale) - in the object's own (parent's) space, as the file has it. Keys are [frame, value] pairs (frames of the
scene's own timeline, 1-based); between keys the curve is a smooth monotone cubic (Fritsch-Carlson: it never
overshoots the keys - like Maya's "clamped" / auto tangents), flat before the first key and after the last.
assets/geometry_view.qml evaluates the same curve in JavaScript (evalCurve) - keep the two alike.

The node's edits (param "edits"): {object path: {"keys": {channel: [[frame, value]]}, "offsets": {channel: [[frame,
offset]]}, "set": {channel: value}}}
- keys: a channel the file does NOT animate, driven by them (absolute values);
- offsets: a channel the file animates - an ADDITIVE layer (like a Maya animation layer): the file's own motion plus
  this curve of offsets, so the original animation stays (one key shifts all of it; several blend between them);
- set: a value moved by hand on a channel nothing animates (auto-key off).
An object path names the object from the scene's top ("scene/ROOT/Rig/Bender"; "cameras/..." for a camera file)."""

CHANNELS = ("tx", "ty", "tz", "rx", "ry", "rz", "sx", "sy", "sz")
LABELS = {"tx": "Translate X", "ty": "Translate Y", "tz": "Translate Z", "rx": "Rotate X", "ry": "Rotate Y",
          "rz": "Rotate Z", "sx": "Scale X", "sy": "Scale Y", "sz": "Scale Z"}


def _slopes(keys):
    """Fritsch-Carlson tangents (per frame) at each key."""
    n = len(keys)
    d = [(keys[i + 1][1] - keys[i][1]) / max(1e-9, keys[i + 1][0] - keys[i][0]) for i in range(n - 1)]
    m = [0.0] * n
    m[0], m[-1] = d[0], d[-1]
    for i in range(1, n - 1):
        m[i] = 0.0 if d[i - 1] * d[i] <= 0 else (d[i - 1] + d[i]) / 2
    for i in range(n - 1):
        if d[i] == 0:
            m[i] = m[i + 1] = 0.0
            continue
        a, b = m[i] / d[i], m[i + 1] / d[i]
        s = a * a + b * b
        if s > 9:
            t = 3 / s ** 0.5
            m[i], m[i + 1] = t * a * d[i], t * b * d[i]
    return m


def evaluate(keys, frame):
    """The channel's value at a (fractional) frame."""
    if not keys:
        return None
    if len(keys) == 1 or frame <= keys[0][0]:
        return keys[0][1]
    if frame >= keys[-1][0]:
        return keys[-1][1]
    i = next(i for i in range(len(keys) - 1) if keys[i][0] <= frame <= keys[i + 1][0])
    (f0, v0), (f1, v1) = keys[i], keys[i + 1]
    m = _slopes(keys)
    h = f1 - f0
    t = (frame - f0) / h
    t2, t3 = t * t, t * t * t
    return ((2 * t3 - 3 * t2 + 1) * v0 + (t3 - 2 * t2 + t) * h * m[i] + (-2 * t3 + 3 * t2) * v1
            + (t3 - t2) * h * m[i + 1])


def with_key(keys, frame, value):
    """The keys with one at `frame` (replacing a key already there), in frame order."""
    out = [k for k in (keys or []) if abs(k[0] - frame) > 1e-6] + [[frame, value]]
    return sorted(out, key=lambda k: k[0])


def copy_edits(edits):
    """A deep copy of the node's edits (they are changed as a new value, never in place - undo keeps the old)."""
    return {path: {"keys": {c: [list(k) for k in ks] for c, ks in (e.get("keys") or {}).items()},
                   "offsets": {c: [list(k) for k in ks] for c, ks in (e.get("offsets") or {}).items()},
                   "set": dict(e.get("set") or {})} for path, e in (edits or {}).items()}


def _entry(out, path):
    entry = out.setdefault(path, {"keys": {}, "offsets": {}, "set": {}})
    entry.setdefault("offsets", {})
    return entry


def _tidy(out, path):
    e = out.get(path)
    if e is not None and not e["keys"] and not e["offsets"] and not e["set"]:
        out.pop(path)
    return out


def commit(edits, path, values, wanted, frame, auto_key, animated):
    """The edits after the user moved an object (or a camera) to `values` {channel: value} at `frame`. `wanted` =
    {channel: the offset that gives it} for the channels the file animates (`animated`) - the viewer works it out
    (turning: the rotation from the file's to the new one, as Euler angles - not a difference of angles).
    Auto-key on → a key on each moved channel: on an animated one an OFFSET key (the motion stays, shifted / turned),
    else an absolute key. Off → an animated or keyed channel changes only until the frame changes, as in Maya -
    nothing kept; any other channel keeps the value ("set")."""
    out = copy_edits(edits)
    entry = _entry(out, path)
    for ch, v in values.items():
        additive = ch in animated and ch in wanted and not entry["keys"].get(ch)
        if auto_key:
            if additive:
                entry["offsets"][ch] = with_key(entry["offsets"].get(ch), frame, wanted[ch])
            else:
                entry["keys"][ch] = with_key(entry["keys"].get(ch), frame, v)
            entry["set"].pop(ch, None)
        elif entry["keys"].get(ch) or entry["offsets"].get(ch) or ch in animated:
            continue
        else:
            entry["set"][ch] = v
    return _tidy(out, path)


def key_all(edits, path, values, wanted, frame, animated):
    """Set Key: a key at `frame` on every given channel (Maya's S) - an offset key (as it is now) on the ones the
    file animates."""
    out = copy_edits(edits)
    entry = _entry(out, path)
    for ch, v in values.items():
        if ch in animated and ch in wanted and not entry["keys"].get(ch):
            entry["offsets"][ch] = with_key(entry["offsets"].get(ch), frame, wanted[ch])
        else:
            entry["keys"][ch] = with_key(entry["keys"].get(ch), frame, v)
        entry["set"].pop(ch, None)
    return _tidy(out, path)


def file_value(samples, frame):
    """The file's own curve (sampled [[frame, value]]) at a frame, between samples straight."""
    if not samples:
        return 0.0
    if frame <= samples[0][0]:
        return samples[0][1]
    for (f0, v0), (f1, v1) in zip(samples, samples[1:]):
        if f0 <= frame <= f1:
            return v0 + (v1 - v0) * (frame - f0) / max(1e-9, f1 - f0)
    return samples[-1][1]
