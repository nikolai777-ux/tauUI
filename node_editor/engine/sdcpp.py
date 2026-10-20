"""Run models through stable-diffusion.cpp (sd-cli.exe): GGUF files, its native format.

sd.cpp recognises the architecture from the weights itself; we only say which connected file fills which of
its slots (--diffusion-model / text encoder / --vae / --audio-vae / --clip_vision), from what the app detected for
each file (the Model node's Role, which can be overridden). A text encoder goes to the flag sd-cli names after its
family (--t5xxl, --clip_l, --clip_g; any other - e.g. Qwen3-VL - is an --llm). Pictures: engine/pictures.py (by
default -r references; a node named after one of sd-cli's picture flags, e.g. end_img, goes to that flag). Runs inside the engine worker; sd-cli is a separate process, so
its GPU memory is released when it exits.
"""

import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

STEP = re.compile(r"\|\s*(\d+)/(\d+)\s*-\s*[\d.]+\s*s/it")


def _rocm_dll_dirs():
    """ROCm runtime folders shipped with this Python env's torch (sd.cpp's ROCm build needs them on PATH)."""
    site = os.path.join(sys.prefix, "Lib", "site-packages")
    return [d for d in glob.glob(os.path.join(site, "_rocm_sdk_*", "bin")) if os.path.isdir(d)]


def _text_flag(family):
    """sd-cli's text encoder flags are named after the encoder family they take."""
    family = (family or "").lower()
    if "t5" in family:
        return "--t5xxl"
    if family in ("clip-l", "clip-g"):
        return "--" + family.replace("-", "_")
    return "--llm"


def _slots(models):
    """{sd.cpp flag: path} from the connected models' detected roles; raises if it's ambiguous."""
    slots, unplaced = {}, []
    for m in models:
        role, media = m.get("role"), (m.get("media") or "")
        flag = {"diffusion": "--diffusion-model", "checkpoint": "--model", "image_encoder": "--clip_vision"}.get(role)
        if role == "text_encoder":
            flag = _text_flag(m.get("family"))
        if role == "vae":
            flag = "--audio-vae" if "audio" in media and "video" not in media else "--vae"
        if flag is None:
            unplaced.append(m)
            continue
        if flag in slots:
            raise ValueError(f"Two models for sd.cpp's {flag}: {slots[flag][0]} and {m['name']} - set Role on one")
        slots[flag] = (m["name"], m["path"])
    for m in unplaced:  # a model the app couldn't classify: the denoiser if that slot is still free
        if "--diffusion-model" not in slots and m["path"].lower().endswith(".gguf"):
            slots["--diffusion-model"] = (m["name"], m["path"])
        else:
            raise ValueError(f"{m['name']}: not sure which sd.cpp input this is - set its Role on the Model node")
    if "--diffusion-model" not in slots and "--model" not in slots:
        raise ValueError("No diffusion model among the connected models")
    return {flag: path for flag, (_, path) in slots.items()}


def _ffmpeg():
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg isn't on PATH (needed to prepare video/audio references and the MP4)")
    return exe


def _video_frames_dir(video, work, i):
    """sd.cpp takes a reference video as a folder of frames at 24 fps (+ its soundtrack as a WAV)."""
    folder = os.path.join(work, f"ref_video_{i}")
    os.makedirs(folder, exist_ok=True)
    subprocess.run([_ffmpeg(), "-v", "error", "-y", "-i", video, "-vf", "fps=24", os.path.join(folder, "%05d.png")],
                   check=True)
    wav = os.path.join(work, f"ref_video_{i}.wav")
    has_audio = subprocess.run([_ffmpeg(), "-v", "error", "-y", "-i", video, "-vn", "-ac", "2", "-ar", "32000", wav],
                               capture_output=True).returncode == 0 and os.path.getsize(wav) > 1000
    return folder, (wav if has_audio else None)


def _wav(audio, work, i):
    out = os.path.join(work, f"ref_audio_{i}.wav")
    subprocess.run([_ffmpeg(), "-v", "error", "-y", "-i", audio, "-ac", "2", "-ar", "32000", out], check=True)
    return out


_help = {}


def picture_flags(exe):
    """{name: flag} for the flags sd-cli's own --help describes as taking a picture (init_img -> --init-img,
    end_img -> --end-img, mask -> --mask ...). Flags that WRITE an image (-o, --preview-path) never take one, and
    'image' stays the default input (a node named image1 is just a picture)."""
    return _read_help(exe)[0]


def required_by(exe):
    """{flag: what} for sd-cli's own "..., required by <what>" help texts (e.g. --end-img: flf2v)."""
    return _read_help(exe)[1]


def _read_help(exe):
    if exe not in _help:
        text = subprocess.run([exe, "--help"], capture_output=True, text=True, errors="replace",
                              creationflags=subprocess.CREATE_NO_WINDOW).stdout
        flags, required = {}, {}
        for line in text.splitlines():
            m = re.match(r"\s*(?:-\w,\s*)?--([\w-]+)\s+<string>\s+(.*)", line)
            if not m:
                continue
            # "path to the end image", "path to control image" - not "path to write result image", not a model
            # whose help merely mentions an image model (--llm), not a folder of images
            if re.match(r"path to (?!write)(?:[\w-]+ )*?image\b", m.group(2), re.I):
                flags[m.group(1).replace("-", "_")] = "--" + m.group(1)
            need = re.search(r"required by ([\w.-]+)", m.group(2), re.I)
            if need:
                required["--" + m.group(1)] = need.group(1)
        flags.pop("image", None)
        _help[exe] = (flags, required)
    return _help[exe]


def _missing_required(exe, cmd, log):
    """sd-cli ran a model its log names (e.g. 'Wan2.1-FLF2V-14B') without a flag its help says that model needs:
    it doesn't stop by itself (tested: FLF2V with -r pictures gave noise), so say so, in its own words."""
    notes = []
    for flag, what in required_by(exe).items():
        if flag not in cmd and re.search(rf"(?<![a-z0-9]){re.escape(what)}(?![a-z0-9])", log, re.I):
            notes.append(f"sd-cli's help says {flag} is required by {what}, and it ran a {what} model without it"
                         f" - name the picture node {flag.lstrip('-').replace('-', '_')}")
    return notes


def run(job, emit, cancel_event, free_other):
    status = lambda msg: emit(event="status", message=msg)  # noqa: E731
    t0 = time.time()
    exe = job.get("sdcpp") or ""
    if not os.path.isfile(exe):
        raise ValueError(f"stable-diffusion.cpp (sd-cli.exe) not found: '{exe}' - set it in File > stable-diffusion.cpp")
    slots = _slots(job["models"])
    denoiser = next(m for m in job["models"] if m["path"] in (slots.get("--diffusion-model"), slots.get("--model")))
    p = denoiser["params"]
    video = "video" in (denoiser.get("media") or "")
    free_other()  # sd-cli needs the GPU memory the worker's own models hold

    os.makedirs(job["output_dir"], exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    base = os.path.join(job["output_dir"], f"{job['name']}_{stamp}_s{p.get('seed', 0)}")
    work = tempfile.mkdtemp(prefix="sdcpp_")
    cmd = [exe, "-M", "vid_gen" if video else "img_gen", "-v"]
    for flag, path in slots.items():
        cmd += [flag, path]
    cmd += ["-p", job["prompt"] or " ", "-W", str(p.get("width", 512)), "-H", str(p.get("height", 512)),
            "--steps", str(p.get("steps", 20)), "--cfg-scale", str(p.get("cfg", 1.0)), "-s", str(p.get("seed", 0)),
            # the recipe sd.cpp's own docs give for its large video models
            "--diffusion-fa", "--offload-to-cpu", "--rng", "cpu"]
    negative = (p.get("negative") or "").strip()
    if negative:
        cmd += ["-n", negative]
        if float(p.get("cfg", 1.0)) <= 1.0:
            status("Note: the negative prompt only takes effect with Guidance above 1")
    import pictures
    named = job.get("named", {}).get("image") or [[f"image{i + 1}", p] for i, p in enumerate(job["images"])]
    routed, routing, _unused = pictures.route(named, picture_flags(exe), "-r")
    for flag, paths in routed.items():
        for path in paths:
            cmd += [flag, path]
    if routing:
        status(routing)
    note = routing if len(named) > 1 or set(routed) != {"-r"} else ""  # stays in the Modality's status
    if video:
        cmd += ["--video-frames", str(p.get("frames") or 49)]
        for i, v in enumerate(job["videos"]):
            status(f"Preparing reference video {i + 1}…")
            folder, wav = _video_frames_dir(v, work, i)
            cmd += ["--ref-video", folder] + (["--ref-video-audio", wav] if wav else [])
        for i, a in enumerate(job["audios"]):
            cmd += ["--ref-audio", _wav(a, work, i)]
        out = base + ".avi"
    else:
        out = base + ".png"
    cmd += ["-o", base + (".mp4" if video else ".png")]

    env = dict(os.environ)
    if "rocm" in os.path.basename(os.path.dirname(exe)).lower():  # a ROCm build: give it this env's ROCm DLLs
        env["PATH"] = os.pathsep.join(_rocm_dll_dirs() + [env.get("PATH", "")])
    status("Starting stable-diffusion.cpp…")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    tail, buf, last_step = [], b"", None
    said = []  # sd.cpp's own [INFO] lines, paths left out - what it says it is running
    try:
        while True:
            if cancel_event.is_set():
                proc.kill()
                import recipes
                raise recipes.Cancelled()
            chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
            if not chunk:
                break
            sys.stderr.write(chunk.decode("utf-8", "replace"))  # full sd.cpp log goes to the engine log
            buf += chunk
            *lines, buf = re.split(rb"[\r\n]", buf)
            for raw in lines:
                line = raw.decode("utf-8", "replace")
                if line.strip():
                    tail = (tail + [line])[-20:]
                    if "[INFO" in line and len(said) < 400:
                        said.append(re.sub(r"'[^']*'", "", line))
                m = STEP.search(line)
                if m and (m.group(1), m.group(2)) != last_step:
                    last_step = (m.group(1), m.group(2))
                    emit(event="progress", step=int(m.group(1)), total=int(m.group(2)))
                elif "[INFO" in line and re.search(r"- loading \S.* from '", line):  # e.g. "loading llm from '...'"
                    status("Loading " + re.search(r"- loading (.+?) from '", line).group(1) + "…")
                elif "decoding audio" in line or "decode_first_stage" in line:
                    status("Decoding…")
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
    if proc.returncode != 0:
        errors = [l for l in tail if "error" in l.lower() or "fail" in l.lower()] or tail[-3:]
        raise RuntimeError("stable-diffusion.cpp failed: " + " | ".join(e.strip() for e in errors[-3:]))

    note = "; ".join(filter(None, [note] + _missing_required(exe, cmd, "\n".join(said))))
    if video:
        produced = next((f for f in (base + ".mp4.avi", base + ".avi", base + ".mp4") if os.path.isfile(f)), None)
        if not produced:
            raise RuntimeError("stable-diffusion.cpp finished but wrote no video")
        mp4 = base + ".mp4"
        if produced != mp4:  # its AVI (MJPEG + PCM) -> H.264 + AAC MP4, which players and Qt handle
            subprocess.run([_ffmpeg(), "-v", "error", "-y", "-i", produced, "-c:v", "libx264", "-pix_fmt", "yuv420p",
                            "-crf", "16", "-c:a", "aac", "-b:a", "192k", mp4], check=True)
            os.remove(produced)
        shutil.rmtree(work, ignore_errors=True)
        return {"kind": "video", "path": mp4, "seconds": round(time.time() - t0, 1), "note": note}
    shutil.rmtree(work, ignore_errors=True)
    return {"kind": "image", "path": base + ".png", "seconds": round(time.time() - t0, 1), "note": note}
