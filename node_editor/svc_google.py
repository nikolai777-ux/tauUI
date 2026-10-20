"""Google AI Studio (the Gemini API) - how the app talks to it. Blocking calls, run in a background thread like
svc_replicate's, with the same functions (account / search / model / price / upload / create / get / outputs).

Google publishes no per-model input schema, so the inputs of each model FAMILY are written here from Google's API
docs (ai.google.dev/gemini-api/docs: text, image-generation, veo, speech-generation, music-generation) - protocol
knowledge, like the request format itself. Which family a model is comes from what the model list says it supports
(predictLongRunning = a video job) and its id (…-image, …-tts, lyria…).

Prices: Google's API has none. They are read from the service file (services/google.json "prices": {model id: price
table}), copied from Google's pricing page with the date - edit them there when Google changes them.
"""

import base64
import json
import mimetypes
import os
import tempfile
import time
import urllib.parse
import urllib.request

from .services import UA, HttpError, http

API = "https://generativelanguage.googleapis.com/v1beta"
UPLOAD = "https://generativelanguage.googleapis.com/upload/v1beta/files"
POLL_SECONDS = 5.0
INLINE_MAX = 15 * 1024 * 1024  # bigger files go through the Files API (kept 48 h by Google)
VOICES = ["Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede", "Callirrhoe", "Autonoe", "Enceladus",
          "Iapetus", "Umbriel", "Algieba", "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
          "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi", "Vindemiatrix", "Sadachbia",
          "Sadaltager", "Sulafat"]
SKIP = ("embedding", "aqa", "live", "robotics", "computer-use", "antigravity", "native-audio", "deep-research",
        "translate")


def _h(token, **extra):
    return {"x-goog-api-key": token, **extra}


def _models(token):
    d = http("GET", f"{API}/models?pageSize=1000", _h(token), timeout=30)
    return d.get("models", [])


def account(token):
    """Google returns no account name: the number of models the key reaches proves it works."""
    return f"{len(_models(token))} models"


def family(mid, methods=()):
    mid = mid.lower()
    if "predictLongRunning" in methods or mid.startswith("veo"):
        return "video"
    if "tts" in mid:
        return "speech"
    if mid.startswith("lyria"):
        return "music"
    if "image" in mid or "nano-banana" in mid:
        return "image"
    return "text"


def _row(m):
    mid = m["name"].split("/", 1)[1]
    fam = family(mid, m.get("supportedGenerationMethods", []))
    desc = (m.get("description") or m.get("displayName") or "").strip()
    return {"id": mid, "description": f"[{fam}] {desc}", "runs": 0, "url": "https://ai.google.dev/gemini-api/docs/models"}


def _usable(m):
    methods = m.get("supportedGenerationMethods", [])
    mid = m["name"].split("/", 1)[1].lower()
    return ("generateContent" in methods or "predictLongRunning" in methods) and not any(s in mid for s in SKIP)


def search(token, query):
    words = query.lower().split()
    rows = [_row(m) for m in _models(token) if _usable(m)]
    return [r for r in rows if all(w in (r["id"] + " " + r["description"]).lower() for w in words)]


def collections(token):
    return [(f, f.capitalize()) for f in ("text", "image", "video", "speech", "music")]


def collection(token, slug):
    return [r for r in (_row(m) for m in _models(token) if _usable(m)) if r["description"].startswith(f"[{slug}]")]


def _f(name, typ="string", media=None, multi=False, default=None, options=None, required=False, description="",
       minimum=None, maximum=None, title=None):
    return {"name": name, "title": title or name.replace("_", " ").capitalize(), "type": typ, "media": media,
            "multi": multi, "default": default, "minimum": minimum, "maximum": maximum, "options": options,
            "description": description, "required": required, "order": 0}


def fields_for(fam, mid=""):
    """The inputs of a model family (from Google's API docs)."""
    if fam == "video":
        return [_f("prompt", required=True),
                _f("first_frame", "uri", "image", description="The video starts on this picture (a node named "
                                                               "first_frame)"),
                _f("last_frame", "uri", "image", description="The video ends on this picture (with first_frame)"),
                _f("reference_images", "uri", "image", multi=True,
                   description="Up to 3 pictures of a person, character or product (8 s only)"),
                _f("aspect_ratio", options=["16:9", "9:16"], default="16:9"),
                _f("duration_seconds", options=["4", "6", "8"], default="8",
                   description="8 with 1080p / 4k or reference images"),
                _f("resolution", options=["720p", "1080p", "4k"], default="720p"),
                _f("negative_prompt", description="What the video should not show"),
                _f("person_generation", options=["allow_adult", "allow_all"], default=None,
                   description="allow_adult for pictures in; allow_all only text-to-video"),
                _f("seed", "integer")]
    if fam == "image":
        sizes = ["1K", "2K", "4K"] + (["512px"] if "flash" in mid else [])
        return [_f("prompt", required=True),
                _f("images", "uri", "image", multi=True, description="Pictures to edit or to take people / objects "
                                                                     "from (Pro: up to 5 people, 10 objects)"),
                _f("aspect_ratio", options=["1:1", "3:2", "2:3", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"],
                   default=None),
                _f("image_size", options=sizes, default=None)]
    if fam == "speech":
        return [_f("prompt", required=True, description="What to say (a style can be asked in words)"),
                _f("voice", options=VOICES, default="Kore")]
    if fam == "music":
        return [_f("prompt", required=True), _f("images", "uri", "image", multi=True)]
    return [_f("prompt", required=True),
            _f("media", "uri", "file", multi=True, description="Pictures, videos, sounds, PDFs to look at"),
            _f("system", "string", description="System instruction (who it is, how to answer)"),
            _f("thinking_level", options=["low", "medium", "high"], default=None),
            _f("max_output_tokens", "integer", default=None)]


def model(token, mid):
    d = http("GET", f"{API}/models/{mid}", _h(token), timeout=30)
    fam = family(mid, d.get("supportedGenerationMethods", []))
    return {"id": mid, "description": (d.get("description") or d.get("displayName") or "").strip(),
            "official": True, "version": d.get("version", ""), "fields": fields_for(fam, mid),
            "output": "text" if fam == "text" else "files", "url": "https://ai.google.dev/gemini-api/docs/models",
            "family": fam, "input_limit": d.get("inputTokenLimit"), "output_limit": d.get("outputTokenLimit")}


def price(mid, service=None):
    """The price table for a model from the service file's "prices" (Google's API has none)."""
    prices = ((service or {}).get("settings") or {}).get("prices") or {}
    table = prices.get(mid)
    if not table:
        return None
    return {"tiers": table.get("tiers", []), "source": prices.get("_source", "services/google.json"),
            "typical": table.get("typical")}


# ---- running -----------------------------------------------------------------------------------------------------
def upload(token, path):
    """A local file as a part Google takes: inline bytes when small, else uploaded with the Files API."""
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    size = os.path.getsize(path)
    if size <= INLINE_MAX:
        with open(path, "rb") as f:
            return {"inlineData": {"mimeType": mime, "data": base64.b64encode(f.read()).decode("ascii")}}
    req = urllib.request.Request(UPLOAD, method="POST", headers={
        "x-goog-api-key": token, "X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(size), "X-Goog-Upload-Header-Content-Type": mime,
        "Content-Type": "application/json", "User-Agent": UA},
        data=json.dumps({"file": {"display_name": os.path.basename(path)}}).encode())
    with urllib.request.urlopen(req, timeout=60) as r:
        url = r.headers.get("x-goog-upload-url")
    with open(path, "rb") as f:
        req = urllib.request.Request(url, method="POST", data=f.read(), headers={
            "X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize", "User-Agent": UA,
            "Content-Length": str(size)})
    with urllib.request.urlopen(req, timeout=1800) as r:
        info = json.loads(r.read().decode())["file"]
    while info.get("state") == "PROCESSING":  # videos are processed before they can be used
        time.sleep(3)
        info = http("GET", f"{API}/{info['name']}", _h(token), timeout=30)
    if info.get("state") == "FAILED":
        raise HttpError(0, f"Google couldn't process {os.path.basename(path)}")
    return {"fileData": {"mimeType": mime, "fileUri": info["uri"]}}


def _image_part(part):
    return {"inlineData": part["inlineData"]} if "inlineData" in part else part


def _save(data_b64, mime, folder):
    ext = mimetypes.guess_extension(mime.split(";")[0]) or ".bin"
    fd, path = tempfile.mkstemp(suffix={".jpe": ".jpg"}.get(ext, ext), dir=folder)
    with os.fdopen(fd, "wb") as f:
        f.write(base64.b64decode(data_b64))
    return path


def _pcm_to_wav(path, rate=24000):
    """Google's speech comes as raw 16-bit PCM (audio/L16): wrapped as a WAV file."""
    import wave
    with open(path, "rb") as f:
        pcm = f.read()
    out = os.path.splitext(path)[0] + ".wav"
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    os.remove(path)
    return out


def create(token, info, inputs):
    """Run the model: one request for text / image / speech / music (returned finished), a long-running job for
    video (returned processing - get() polls it)."""
    fam = info.get("family") or family(info["id"])
    mid = info["id"]
    if fam == "video":
        inst = {"prompt": inputs.get("prompt", "")}
        if inputs.get("first_frame"):
            inst["image"] = _image_part(inputs["first_frame"])
        if inputs.get("last_frame"):
            inst["lastFrame"] = _image_part(inputs["last_frame"])
        if inputs.get("reference_images"):
            inst["referenceImages"] = [{"image": _image_part(p), "referenceType": "asset"}
                                       for p in inputs["reference_images"]]
        params = {k2: inputs[k] for k, k2 in (("aspect_ratio", "aspectRatio"), ("duration_seconds", "durationSeconds"),
                                              ("resolution", "resolution"), ("negative_prompt", "negativePrompt"),
                                              ("person_generation", "personGeneration"), ("seed", "seed"))
                  if inputs.get(k) not in (None, "")}
        params["durationSeconds"] = int(float(params.get("durationSeconds", 8)))  # a number, not text
        if "seed" in params:
            params["seed"] = int(params["seed"])
        op = http("POST", f"{API}/models/{mid}:predictLongRunning", _h(token),
                  body={"instances": [inst], "parameters": params}, timeout=120)
        return {"id": op.get("name"), "status": "processing", "operation": op.get("name"),
                "duration": float(params["durationSeconds"])}
    parts = [{"text": inputs.get("prompt", "")}] if inputs.get("prompt") else []
    for key in ("media", "images"):
        parts += inputs.get(key) or []
    body = {"contents": [{"role": "user", "parts": parts}]}
    cfg = {}
    if fam == "image":
        cfg["responseModalities"] = ["IMAGE"]
        img = {k2: inputs[k] for k, k2 in (("aspect_ratio", "aspectRatio"), ("image_size", "imageSize"))
               if inputs.get(k)}
        if img:
            cfg["imageConfig"] = img
    elif fam == "speech":
        cfg["responseModalities"] = ["AUDIO"]
        cfg["speechConfig"] = {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": inputs.get("voice") or "Kore"}}}
    elif fam == "text":
        if inputs.get("system"):
            body["systemInstruction"] = {"parts": [{"text": inputs["system"]}]}
        if inputs.get("thinking_level"):
            cfg["thinkingConfig"] = {"thinkingLevel": inputs["thinking_level"]}
        for k, k2 in (("max_output_tokens", "maxOutputTokens"),):  # sampling params: deprecated by Google (2026-10)
            if inputs.get(k) not in (None, ""):
                cfg[k2] = inputs[k]
    if cfg:
        body["generationConfig"] = cfg
    d = http("POST", f"{API}/models/{mid}:generateContent", _h(token), body=body, timeout=900)
    return _finished(d, fam)


def _finished(d, fam):
    """A generateContent answer as a finished job: text, and files saved in a temp folder (file:// URLs)."""
    cand = (d.get("candidates") or [{}])[0]
    usage = d.get("usageMetadata") or {}
    metrics = {"input_tokens": usage.get("promptTokenCount", 0),
               "output_tokens": usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)}
    if not cand.get("content"):
        why = cand.get("finishReason") or (d.get("promptFeedback") or {}).get("blockReason") or "no answer"
        return {"status": "failed", "error": f"Google gave no result ({why})", "metrics": metrics}
    folder = tempfile.mkdtemp(prefix="gemini_")
    files, text = [], []
    for p in cand["content"].get("parts", []):
        if p.get("thought"):
            continue
        if "text" in p:
            text.append(p["text"])
        blob = p.get("inlineData")
        if blob:
            path = _save(blob["data"], blob.get("mimeType", ""), folder)
            if blob.get("mimeType", "").startswith("audio/L16") or blob.get("mimeType", "").startswith("audio/pcm"):
                rate = 24000
                for piece in blob["mimeType"].split(";"):
                    if piece.strip().startswith("rate="):
                        rate = int(piece.split("=")[1])
                path = _pcm_to_wav(path, rate)
            files.append(urllib.parse.urljoin("file:", urllib.request.pathname2url(path)))
    if fam == "image":
        metrics["image_output_count"] = len(files)
    return {"id": d.get("responseId", ""), "status": "succeeded", "output": {"files": files, "text": "".join(text)},
            "metrics": metrics}


def get(token, pred):
    if pred.get("status") != "processing":
        return pred
    op = http("GET", f"{API}/{pred['operation']}", _h(token), timeout=60)
    if not op.get("done"):
        return pred
    if op.get("error"):
        return {**pred, "status": "failed", "error": op["error"].get("message") or json.dumps(op["error"])}
    resp = (op.get("response") or {}).get("generateVideoResponse") or {}
    samples = resp.get("generatedSamples") or []
    if not samples:
        why = resp.get("raiMediaFilteredReasons") or "no video (filtered?)"
        return {**pred, "status": "failed", "error": f"Google made no video: {why}"}
    uris = [s["video"]["uri"] for s in samples if (s.get("video") or {}).get("uri")]
    return {**pred, "status": "succeeded", "output": {"files": uris, "text": ""},
            "metrics": {"video_output_duration_seconds": pred.get("duration", 8.0) * len(uris)}}


def cancel(token, pred):
    if pred.get("operation"):
        try:
            http("POST", f"{API}/{pred['operation']}:cancel", _h(token), timeout=30)
        except HttpError:
            pass


def outputs(pred):
    out = pred.get("output") or {}
    return list(out.get("files") or []), out.get("text") or ""


def fetch(token, url, path):
    """Save an output: a file Google made (needs the key) or one already saved here (file://)."""
    from .services import download
    if url.startswith("file:"):
        src = urllib.request.url2pathname(urllib.parse.urlsplit(url).path)
        ext = os.path.splitext(src)[1]
        dest = (path[:-4] if path.endswith(".bin") else os.path.splitext(path)[0]) + ext
        import shutil
        shutil.move(src, dest)  # the temp folder may be on another drive
        return dest
    return download(url, path, headers={"x-goog-api-key": token})


def file_name(url, i, count):
    if url.startswith("file:"):
        ext = os.path.splitext(urllib.parse.urlsplit(url).path)[1] or ".bin"
    else:
        ext = ".bin"  # Google's download links name no type: taken from the file itself
    return ext, ("" if count == 1 else f"_{i + 1}")
