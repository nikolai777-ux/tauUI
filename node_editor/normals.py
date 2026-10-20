"""Smooth normals for an OBJ that has none (Qt's importer then shades every face flat - the blocky look).

Each face corner gets the average of the face normals around its vertex (area-weighted), counting only faces within
CREASE degrees of its own face - so hard edges stay hard. The result is a copy in the temp folder (the user's file is
never touched): the same lines, the normals added (all `vn` first, so absolute indices work next to the file's own,
possibly negative, indices), and a `.mtl` it names copied beside it with texture paths made absolute. Cached by the
file's path, size and date; computed in a thread, handed back on the Qt thread.
"""

import hashlib
import math
import os
import re
import tempfile
import threading

CREASE = 60.0  # degrees
CACHE = os.path.join(tempfile.gettempdir(), "NodeEditor", "geometry")
_jobs = []  # running jobs (kept alive with their timers)


def needs_normals(path):
    """An OBJ without a single `vn` line."""
    if not path.lower().endswith(".obj"):
        return False
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return False
    return b"\nvn " not in data and not data.startswith(b"vn ")


def cached_path(path):
    st = os.stat(path)
    key = hashlib.sha1(f"{os.path.abspath(path)}|{st.st_size}|{st.st_mtime}|{CREASE}".encode()).hexdigest()[:16]
    return os.path.join(CACHE, f"{os.path.splitext(os.path.basename(path))[0]}_{key}.obj")


def smooth(path, out):
    """Write `path` with smooth normals to `out`. Returns a note for the user."""
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    verts, faces = [], []  # faces: (line number, [vertex index (0-based)], [the corner tokens])
    for i, line in enumerate(lines):
        if line.startswith("v "):
            p = line.split()
            verts.append((float(p[1]), float(p[2]), float(p[3])))
        elif line.startswith("f "):
            toks = line.split()[1:]
            idx = []
            for t in toks:
                v = int(t.split("/")[0])
                idx.append(v - 1 if v > 0 else len(verts) + v)  # negative = relative to the vertices so far
            faces.append((i, idx, toks))
    # face normals (Newell: right for any polygon; its length = twice the area, so the sum is area-weighted)
    fn, unit = [], []
    for _i, idx, _t in faces:
        nx = ny = nz = 0.0
        for a, b in zip(idx, idx[1:] + idx[:1]):
            (x1, y1, z1), (x2, y2, z2) = verts[a], verts[b]
            nx += (y1 - y2) * (z1 + z2)
            ny += (z1 - z2) * (x1 + x2)
            nz += (x1 - x2) * (y1 + y2)
        fn.append((nx, ny, nz))
        length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
        unit.append((nx / length, ny / length, nz / length))
    around = [[] for _ in verts]
    for f, (_i, idx, _t) in enumerate(faces):
        for v in set(idx):
            around[v].append(f)
    limit = math.cos(math.radians(CREASE))
    normals, index = [], {}

    def normal_of(f, v):
        u = unit[f]
        sx = sy = sz = 0.0
        for g in around[v]:
            w = unit[g]
            if g == f or u[0] * w[0] + u[1] * w[1] + u[2] * w[2] >= limit:
                n = fn[g]
                sx, sy, sz = sx + n[0], sy + n[1], sz + n[2]
        length = math.sqrt(sx * sx + sy * sy + sz * sz) or 1.0
        key = (round(sx / length, 4), round(sy / length, 4), round(sz / length, 4))
        if key not in index:
            index[key] = len(normals) + 1
            normals.append(key)
        return index[key]

    rewritten = {}
    for f, (i, idx, toks) in enumerate(faces):
        corners = []
        for v, t in zip(idx, toks):
            parts = t.split("/")
            vt = parts[1] if len(parts) > 1 else ""
            corners.append(f"{parts[0]}/{vt}/{normal_of(f, v)}")
        rewritten[i] = "f " + " ".join(corners)
    folder = os.path.dirname(os.path.abspath(path))
    body = []
    for i, line in enumerate(lines):
        if i in rewritten:
            body.append(rewritten[i])
        elif line.startswith("mtllib "):
            body.append("mtllib " + " ".join(_copy_mtl(os.path.join(folder, name), os.path.dirname(out))
                                             for name in line.split()[1:]))
        else:
            body.append(line)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(f"# {os.path.basename(path)} with smooth normals added by tauUI (crease {CREASE:g} degrees)\n")
        f.write("".join(f"vn {x} {y} {z}\n" for x, y, z in normals))
        f.write("\n".join(body) + "\n")
    os.replace(tmp, out)
    return f"computed - the file has none (smooth, hard edges over {CREASE:g}°)"


MAP_KEYS = re.compile(r"^\s*(map_\w+|bump|disp|decal|refl|norm)\b", re.I)


def _copy_mtl(src, folder):
    """A material library beside the copy, its texture paths made absolute (they were relative to the original)."""
    name = os.path.basename(src)
    if not os.path.isfile(src):
        return name
    base = os.path.dirname(src)
    out = []
    with open(src, encoding="utf-8", errors="replace") as f:
        for line in f.read().splitlines():
            if MAP_KEYS.match(line):
                parts = line.split()
                tex = parts[-1]
                if not os.path.isabs(tex):
                    parts[-1] = os.path.normpath(os.path.join(base, tex)).replace("\\", "/")
                line = " ".join(parts)
            out.append(line)
    with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return name


def start(path, done):
    """Smooth normals for `path` in a thread; done(out path or None, note) is called on the Qt thread."""
    from PySide6.QtCore import QTimer
    out = cached_path(path)
    if os.path.isfile(out):
        QTimer.singleShot(0, lambda: done(out, f"computed - the file has none (smooth, hard edges over {CREASE:g}°)"))
        return None
    job = {"out": None, "note": "", "finished": False}

    def work():
        try:
            job["note"] = smooth(path, out)
            job["out"] = out
        except Exception as e:  # noqa: BLE001 - show the file as it is, and say why
            job["note"] = f"couldn't compute them ({type(e).__name__}: {e}) - shown as the file is"
        job["finished"] = True

    timer = QTimer(interval=100)

    def check():
        if job["finished"]:
            timer.stop()
            _jobs.remove(entry)
            done(job["out"], job["note"])
    timer.timeout.connect(check)
    entry = (job, timer)
    _jobs.append(entry)
    threading.Thread(target=work, daemon=True).start()
    timer.start()
    return job
