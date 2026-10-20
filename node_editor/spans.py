"""Shots and chunks as sources: what Isolate and Edit Media share when a .shots / .chunks file is plugged in.

- which video the spans are about: the one the shots' Modality is fed NOW (e.g. an Edit Media result), through a
  Chunk node too, else the file the .shots / .chunks names;
- which span a node has picked (a shot of a .shots, a chunk of a .chunks);
- the ffmpeg command that cuts a span's exact frames (a video) or its seconds (a sound).
"""

import os

from .node import VIDEO


def load(path, _cache={}):
    """The .shots / .chunks data in `path` (read again when the file changes), or None."""
    from .node import JSON, KIND_EXT
    from .shots_view import load as read, spans_of
    if not path or not path.lower().endswith(KIND_EXT[JSON]) or not os.path.isfile(path):
        return None  # only JSON files are read (a video isn't opened as text)
    stamp = os.path.getmtime(path)
    if _cache.get(path, (None,))[0] != stamp:
        data = read(path)
        _cache[path] = (stamp, data if spans_of(data) is not None else None)
    return _cache[path][1]


def unit(data):
    """"chunk" for a .chunks file, "shot" for a .shots file."""
    return "chunk" if "chunks" in (data or {}) else "shot"


def spans(data):
    from .shots_view import spans_of
    return (spans_of(data) or []) if data else []


def pick(data, number):
    """The span numbered `number` (1-based, clamped), or None."""
    items = spans(data)
    return items[max(1, min(len(items), int(number or 1))) - 1] if items else None


def analysed_video(node, input_name):
    """The video the spans arriving at node's input were found in, as it is fed now; else the file they name."""
    for maker in node.sources(input_name):
        if hasattr(maker, "analysed_video"):  # a Chunk node: asks its own shots' maker
            video = maker.analysed_video()
            if video:
                return video
            continue
        videos = [v for v in (maker.videos() if hasattr(maker, "videos") else []) if isinstance(v, str) and v]
        if videos and os.path.isfile(videos[0]):
            return videos[0]
    values = [v for v in node.input_values(input_name) if isinstance(v, str) and v]
    return ((load(values[0]) if values else None) or {}).get("video", "")


def cut_command(src, kind, first, last, fps, out):
    """ffmpeg arguments cutting frames first..last (1-based, inclusive) out of a video - exactly: seek to a moment
    before the first one, then take the count - or the same time span out of a sound (16-bit WAV)."""
    if kind == VIDEO:
        frames = last - first + 1
        return ["-v", "error", "-y", "-ss", f"{max(0.0, (first - 1 - 0.25) / fps):.6f}", "-i", src,
                "-map", "0:v:0", "-frames:v", str(frames),
                "-vf", "crop=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-map", "0:a:0?", "-c:a", "aac", "-b:a", "192k", "-t", f"{frames / fps:.6f}",
                "-movflags", "+faststart", out]
    start, end = (first - 1) / fps, last / fps
    return ["-v", "error", "-y", "-ss", f"{start:.6f}", "-t", f"{end - start:.6f}", "-i", src,
            "-map", "0:a:0", "-c:a", "pcm_s16le", out]
