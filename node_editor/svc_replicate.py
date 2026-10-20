"""Replicate (replicate.com) - how the app talks to it. Everything here is a plain blocking call: the app runs it in a
background thread (services.in_background / service_run.py).

What a model takes comes from its own OpenAPI schema (the version's openapi_schema), what it costs from the price
table its model page carries (the `billingConfig` the page is built from - Replicate's API has no price field).
Inputs that are files are uploaded with the Files API first; results are downloaded at once (Replicate deletes an
API prediction's files after an hour).
"""

import json
import os
import re
import urllib.parse

from .services import HttpError, http, multipart

API = "https://api.replicate.com/v1"
SITE = "https://replicate.com"
POLL_SECONDS = 2.0


def _h(token, **extra):
    return {"Authorization": f"Bearer {token}", **extra}


def account(token):
    """The account's name (also proves the key works)."""
    d = http("GET", f"{API}/account", _h(token), timeout=20)
    return d.get("username") or d.get("name") or "?"


def _model_row(m):
    owner, name = m.get("owner", ""), m.get("name", "")
    return {"id": f"{owner}/{name}", "description": (m.get("description") or "").strip(),
            "runs": m.get("run_count") or 0, "url": m.get("url") or f"{SITE}/{owner}/{name}"}


def search(token, query):
    d = http("GET", f"{API}/search?query={urllib.parse.quote(query)}&limit=50", _h(token), timeout=30)
    return [_model_row(r.get("model") or r) for r in d.get("models", [])]


def collections(token):
    d = http("GET", f"{API}/collections", _h(token), timeout=30)
    return [(c["slug"], c.get("name") or c["slug"]) for c in d.get("results", [])]


def collection(token, slug):
    d = http("GET", f"{API}/collections/{slug}", _h(token), timeout=30)
    return [_model_row(m) for m in d.get("models", [])]


# ---- one model: its inputs (schema) and its price ----------------------------------------------------------------
MEDIA_WORDS = (("image", ("image", "img", "photo", "picture", "frame", "mask", "face", "portrait", "sketch")),
               ("video", ("video", "clip", "driving", "footage")),
               ("audio", ("audio", "sound", "speech", "voice", "music", "song", "wav")),
               ("geometry", ("mesh", "glb", "obj", "3d")))


def _media(name, prop):
    """What kind of file a uri input takes: from its name, else its description (the schema's own words)."""
    words = set(re.split(r"[^a-z0-9]+", name.lower()))
    for kind, keys in MEDIA_WORDS:
        if words & set(keys):
            return kind
    desc = (prop.get("description") or "").lower()
    for kind, keys in MEDIA_WORDS:
        if any(re.search(rf"\b{k}s?\b", desc) for k in keys[:3]):
            return kind
    return "file"


def fields(schema):
    """The model's inputs, in its own order: [{name, title, type, media, multi, default, minimum, maximum, options,
    description, required}] - type: string / integer / number / boolean / array / uri."""
    comps = (schema or {}).get("components", {}).get("schemas", {})
    inp = comps.get("Input", {})
    required = set(inp.get("required") or [])
    out = []
    for name, prop in inp.get("properties", {}).items():
        ref = None
        for part in prop.get("allOf") or []:
            ref = part.get("$ref", ref)
        enum = comps.get(ref.split("/")[-1], {}) if ref else {}
        typ = prop.get("type") or enum.get("type") or "string"
        f = {"name": name, "title": prop.get("title") or name.replace("_", " ").capitalize(),
             "type": typ, "media": None, "multi": False, "default": prop.get("default"),
             "minimum": prop.get("minimum"), "maximum": prop.get("maximum"),
             "options": enum.get("enum") or prop.get("enum"), "description": (prop.get("description") or "").strip(),
             "required": name in required, "order": prop.get("x-order", 999)}
        items = prop.get("items") or {}
        if prop.get("format") == "uri":
            f.update(type="uri", media=_media(name, prop))
        elif typ == "array" and items.get("format") == "uri":
            f.update(type="uri", media=_media(name, prop), multi=True)
        out.append(f)
    return sorted(out, key=lambda f: f["order"])


def output_kind(schema):
    """What the model returns, from its schema: "files" (uri / array of uri) or "text" (string / token stream)."""
    o = (schema or {}).get("components", {}).get("schemas", {}).get("Output", {})
    if o.get("format") == "uri" or (o.get("items") or {}).get("format") == "uri":
        return "files"
    if o.get("type") == "string" or (o.get("type") == "array" and (o.get("items") or {}).get("type") == "string"):
        return "text"
    return "files"


def model(token, mid):
    """{id, description, official, version, fields, output, url} for "owner/name" (its latest version)."""
    d = http("GET", f"{API}/models/{mid}", _h(token), timeout=30)
    v = d.get("latest_version") or {}
    schema = v.get("openapi_schema") or {}
    return {"id": mid, "description": (d.get("description") or "").strip(), "official": bool(d.get("is_official")),
            "version": v.get("id") or "", "fields": fields(schema), "output": output_kind(schema),
            "url": d.get("url") or f"{SITE}/{mid}", "license": d.get("license_url") or "",
            "runs": d.get("run_count") or 0}


def price(mid, service=None):
    """The model page's own price table: {"tiers": [{"title", "criteria": [{"title", "value", "subtype"}],
    "prices": [{"price": float, "metric", "unit", "title"}]}]}, or None when the page has none."""
    html = http("GET", f"{SITE}/{mid}", {"User-Agent": "Mozilla/5.0"}, timeout=30, raw=True).decode("utf-8", "replace")
    i = html.find('"billingConfig"')
    cfg = {}
    if i >= 0:
        try:
            cfg, _ = json.JSONDecoder().raw_decode(html[html.index("{", i):])
        except ValueError:
            cfg = {}
    tiers = []
    for t in cfg.get("current_tiers") or []:
        prices = []
        for p in t.get("prices") or []:
            m = re.search(r"[\d.]+", str(p.get("price", "")).replace(",", ""))
            if m:
                per = str(p.get("title") or "").lower()  # "per thousand output images" / "per million ..."
                scale = 1000.0 if "thousand" in per else 1e6 if "million" in per else 1.0
                prices.append({"price": float(m.group(0)) / scale, "metric": p.get("metric") or "",
                               "unit": p.get("metric_display") or p.get("title") or "", "title": p.get("title") or ""})
        crit = [{"title": c.get("title") or "", "value": c.get("value"), "subtype": c.get("subtype") or "",
                 "description": c.get("description") or ""} for c in t.get("criteria") or []]
        if prices:
            tiers.append({"title": t.get("title") or "", "criteria": crit, "prices": prices})
    if tiers:
        return {"tiers": tiers, "source": f"{SITE}/{mid}"}
    # no price per output: billed by GPU time - the page names the hardware, its price per second and a typical run
    rate = re.search(r'"price":\s*"\$([\d.]+) per second",\s*"p50price":\s*"\$([\d.]+)"', html)
    hw = re.search(r'"hardware":\s*"([^"]+)"', html)
    if not rate:
        return None
    return {"tiers": [{"title": "", "criteria": [], "prices": [{
        "price": float(rate.group(1)), "metric": "predict_time", "title": "per second of GPU time",
        "unit": "second of GPU time" + (f" ({hw.group(1)})" if hw else "")}]}],
        "typical": float(rate.group(2)), "source": f"{SITE}/{mid}"}


# ---- running -----------------------------------------------------------------------------------------------------
DATA_URL_MAX = 5 * 1024 * 1024


def upload(token, path):
    """A local file → something the model can read: small files as a data: URL inside the request (partner models
    such as Kling can't open Replicate's own file links), bigger ones through Replicate's Files API."""
    if os.path.getsize(path) <= DATA_URL_MAX:
        import base64
        import mimetypes
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            return f"data:{mime};base64," + base64.b64encode(f.read()).decode("ascii")
    body, ctype = multipart({}, {"content": path})
    d = http("POST", f"{API}/files", _h(token, **{"Content-Type": ctype}), body=body, timeout=600)
    url = (d.get("urls") or {}).get("get")
    if not url:
        raise HttpError(0, "upload gave no URL")
    return url


def create(token, info, inputs):
    """Start a prediction; returns it (id, status, urls...)."""
    if info.get("official") or not info.get("version"):
        return http("POST", f"{API}/models/{info['id']}/predictions", _h(token), body={"input": inputs}, timeout=120)
    return http("POST", f"{API}/predictions", _h(token), body={"version": info["version"], "input": inputs},
                timeout=120)


def get(token, prediction):
    url = (prediction.get("urls") or {}).get("get") or f"{API}/predictions/{prediction['id']}"
    return http("GET", url, _h(token), timeout=60)


def cancel(token, prediction):
    url = (prediction.get("urls") or {}).get("cancel") or f"{API}/predictions/{prediction['id']}/cancel"
    try:
        http("POST", url, _h(token), timeout=30)
    except HttpError:
        pass


def outputs(prediction):
    """(urls, text) from a finished prediction's output: files to download, or the text it wrote."""
    out = prediction.get("output")
    if out is None:
        return [], ""
    if isinstance(out, str):
        return ([out], "") if out.startswith("http") else ([], out)
    if isinstance(out, list):
        if out and all(isinstance(x, str) and x.startswith("http") for x in out):
            return out, ""
        if all(isinstance(x, str) for x in out):
            return [], "".join(out)  # a language model's token stream
    if isinstance(out, dict):
        urls = [v for v in out.values() if isinstance(v, str) and v.startswith("http")]
        urls += [x for v in out.values() if isinstance(v, list) for x in v if isinstance(x, str) and x.startswith("http")]
        if urls:
            return urls, ""
    return [], json.dumps(out, indent=2, ensure_ascii=False)


def file_name(url, i, count):
    """A downloaded output's extension, from its URL."""
    ext = os.path.splitext(urllib.parse.urlsplit(url).path)[1].lower() or ".bin"
    return ext, ("" if count == 1 else f"_{i + 1}")
