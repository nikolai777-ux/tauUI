"""3D files → scenes the viewer loads (geometry_view.py / assets/geometry_view.qml).

Every file goes through Qt's own asset converter, balsam (beside PySide6), into a cached folder: a QML scene + its
meshes / textures / animation data - with cameras, skins and animations (as Timelines the viewer drives frame by frame:
--manualAnimations). Before that, two files need a step of their own:
- an OBJ without normals gets smooth ones (normals.py);
- Alembic (.abc), which Qt's importer can't read, goes through Blender (assets/abc_to_glb.py → GLB). Object and
  camera animation survive; geometry that deforms through Alembic's own vertex caches comes in still (GLB has no place
  for them) - said in the note.
- BVH (motion capture: a skeleton and its motion, nothing to see) goes through Blender too (assets/bvh_to_glb.py):
  every bone gets a bone shape riding on it; the file's frame rate is kept beside the scene (FPS) - glTF keeps
  seconds - and handed to the viewer (recorded_fps).
Cached in %TEMP%\\NodeEditor\\scenes\\<name>_<hash of path, size, date>, so a file converts once."""

import glob
import hashlib
import os
import tempfile

ROOT = os.path.join(tempfile.gettempdir(), "NodeEditor", "scenes")
DONE = "converted.txt"  # written last: a folder without it is an interrupted conversion
FPS = "fps.txt"  # the frame rate a conversion read from the file, when the converted scene keeps none (BVH)
_jobs = []  # running conversions (kept alive)


def balsam_exe():
    import PySide6
    return os.path.join(os.path.dirname(PySide6.__file__), "balsam.exe")


def folder_for(path):
    st = os.stat(path)
    key = hashlib.sha1(f"{os.path.abspath(path).lower()}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:12]
    return os.path.join(ROOT, f"{os.path.splitext(os.path.basename(path))[0]}_{key}")


def cached(path):
    """(qml path, note) of a finished conversion, else None."""
    folder = folder_for(path)
    done = os.path.join(folder, DONE)
    qml = glob.glob(os.path.join(folder, "scene", "*.qml"))
    if os.path.isfile(done) and qml:
        with open(done, encoding="utf-8") as f:
            return qml[0], f.read()
    return None


def recorded_fps(path):
    """The frame rate the conversion read from the file (BVH), else 0."""
    try:
        with open(os.path.join(folder_for(path), FPS), encoding="utf-8") as f:
            return float(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0.0


def start(path, done):
    """Convert `path` in the background; done(qml path or None, note) on the Qt thread."""
    from PySide6.QtCore import QTimer
    from . import normals
    hit = cached(path)
    if hit:
        QTimer.singleShot(0, lambda: done(*hit))
        return
    folder = folder_for(path)
    os.makedirs(folder, exist_ok=True)
    notes = []

    def to_balsam(source):
        out = os.path.join(folder, "scene")
        _run(balsam_exe(), ["--manualAnimations", "-o", out, source], lambda ok, log: balsam_done(ok, log, out))

    def balsam_done(ok, log, out):
        qml = glob.glob(os.path.join(out, "*.qml"))
        if not qml:
            last = [line for line in log.splitlines() if line.strip()][-3:]
            done(None, "couldn't convert it: " + (" / ".join(last) or "the converter made nothing"))
            return
        note = "; ".join(notes)
        with open(os.path.join(folder, DONE), "w", encoding="utf-8") as f:
            f.write(note)
        done(qml[0], note)

    ext = os.path.splitext(path)[1].lower()
    if ext in (".abc", ".bvh"):
        from .settings import blender_exe
        kind = {".abc": "Alembic", ".bvh": "BVH"}[ext]
        exe = blender_exe()
        if not exe:
            done(None, f"{kind} needs Blender to convert it (none found - File › Preferences › Blender…)")
            return
        glb = os.path.join(folder, f"from_{ext[1:]}.glb")
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", f"{ext[1:]}_to_glb.py")
        args = [path, glb] + ([os.path.join(folder, FPS)] if ext == ".bvh" else [])

        def blender_done(ok, log):
            if not os.path.isfile(glb):
                last = [line for line in log.splitlines() if line.strip()][-3:]
                done(None, f"Blender couldn't convert the {kind} file: " + (" / ".join(last) or "no output"))
                return
            if ext == ".abc":
                notes.append("Alembic, converted by Blender: object and camera animation kept; geometry deformed "
                             "by the file's own vertex caches is shown still")
            else:
                fps = recorded_fps(path)
                bones = next((line.split(" bones ")[1].split()[0] for line in log.splitlines()
                              if line.startswith("BVH TO GLB DONE") and " bones " in line), "?")
                notes.append(f"BVH (motion capture), converted by Blender: {bones} bones drawn as bone shapes"
                             + (f"; {fps:g} fps" if fps else ""))
            to_balsam(glb)
        _run(exe, ["--background", "--factory-startup", "--python", script, "--"] + args, blender_done)
    elif path.lower().endswith(".obj") and normals.needs_normals(path):
        def normals_done(out, note):
            notes.append("normals " + note)
            to_balsam(out or path)
        normals.start(path, normals_done)
    else:
        to_balsam(path)


def _run(exe, args, finished):
    """A converter process; finished(ok, its output) on the Qt thread."""
    from PySide6.QtCore import QProcess
    proc = QProcess()
    proc.setProcessChannelMode(QProcess.MergedChannels)
    entry = [proc]
    _jobs.append(entry)

    def end(*_):
        log = bytes(proc.readAll()).decode("utf-8", errors="replace")
        if entry in _jobs:
            _jobs.remove(entry)
        finished(proc.exitStatus() == QProcess.NormalExit and proc.exitCode() == 0, log)
    proc.finished.connect(end)
    proc.errorOccurred.connect(lambda err: err == QProcess.FailedToStart and end())
    proc.start(exe, args)
