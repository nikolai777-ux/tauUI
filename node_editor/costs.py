"""What an online run costs - from the service's own price table, never from a table of ours.

A price table (svc_replicate.price) has tiers; a tier applies when its conditions hold ("model variant = pro",
"with audio = yes", "target resolution = 720"). The conditions name things in words, not by input name, so a tier is
matched to the run's inputs by value: a condition holds when an input carries that value (a yes/no condition: a
yes/no input whose name shares a word with it). When no single tier can be told, the estimate is the range of the
tiers still possible - said so, not guessed.

Every paid run is written to logs/online_spend.jsonl (estimate before, cost after) so totals can be shown.
"""

import json
import os
import re
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEND_LOG = os.path.join(PROJECT_ROOT, "logs", "online_spend.jsonl")

# which input says how many units a metric will count (the run's own settings): seconds of output video, images...
UNIT_INPUTS = {
    "video_output_duration_seconds": ("duration", "seconds", "length", "video_length", "num_seconds", "duration_seconds"),
    "audio_output_duration_seconds": ("duration", "seconds", "length", "duration_seconds"),
    "image_output_count": ("num_outputs", "num_images", "number_of_images", "batch_size", "n"),
}
UNIT_WORDS = {"video_output_duration_seconds": "s", "audio_output_duration_seconds": "s", "image_output_count": "image",
              "predict_time": "s of GPU time"}
_STOP = {"with", "the", "a", "an", "of", "per", "model", "and", "or", "is", "in"}


def _words(text):
    return set(re.split(r"[^a-z0-9]+", str(text).lower())) - _STOP - {""}


def _holds(cond, values):
    """True / False / None (can't tell) for one tier condition against the inputs {name: value}."""
    want = cond["value"]
    if cond["subtype"] == "boolean" or isinstance(want, bool):
        names = [n for n, v in values.items() if isinstance(v, bool) and _words(n) & _words(cond["title"])]
        if not names:
            return None
        return any(values[n] == bool(want) for n in names)
    want = str(want).lower()
    # an input / metric NAMED like the condition ("model_variant" for "model variant") settles it - another input that
    # merely carries the same word ("moderation": "low") must not match a "low" tier
    named = [n for n, v in values.items() if _words(n) and _words(n) == _words(cond["title"]) and v is not None]
    if named:
        return any(str(values[n]).lower() == want for n in named)
    if any(str(v).lower() == want for v in values.values() if v is not None and not isinstance(v, (list, dict))):
        return True
    return None


def candidate_tiers(price, values):
    """The tiers that may apply: those whose every condition holds, else those not ruled out."""
    tiers = (price or {}).get("tiers") or []
    if not tiers:
        return [], False
    results = [(t, [_holds(c, values) for c in t["criteria"]]) for t in tiers]
    sure = [t for t, r in results if all(x is True for x in r)]
    if sure:
        # the most specific match (most conditions), e.g. "pro + with audio" over a tier without conditions
        best = max(len(t["criteria"]) for t in sure)
        sure = [t for t in sure if len(t["criteria"]) == best]
        # a tier whose value only extends a matched one ("std" / "std-with-video-input") may be the one that
        # applies: what the extension depends on isn't an input - so both stay possible (a range, not a guess)
        matched = {str(c["value"]).lower() for t in sure for c in t["criteria"]}
        more = [t for t, r in results if t not in sure and not any(x is False for x in r) and any(
            str(c["value"]).lower().startswith(tuple(m + sep for m in matched for sep in "-_ "))
            for c in t["criteria"])]
        return sure + more, not more
    return [t for t, r in results if not any(x is False for x in r)] or tiers, False


def units(metric, values, metrics=None):
    """How many of a metric's units: measured (after the run) or from the run's settings (before), else None."""
    if metrics and metric in metrics and isinstance(metrics[metric], (int, float)):
        return float(metrics[metric])
    if metric in UNIT_INPUTS:
        for name in UNIT_INPUTS[metric]:
            v = values.get(name)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
            if isinstance(v, str) and re.fullmatch(r"\d+(\.\d+)?", v.strip()):
                return float(v)
        return 1.0 if metric == "image_output_count" else None
    if metric in ("", "unspecified_billing_metric", "run_count", "prediction_count"):
        return 1.0
    return None


def _fmt(x):
    """Dollars with the cents a price really has: $0.168, $1.12, $0.003."""
    if 0 < x < 0.01:  # per-second / per-token rates: their own digits ($0.0014)
        return "$" + f"{x:.6f}".rstrip("0")
    text = f"{x:.3f}"
    return "$" + (text[:-1] if text.endswith("0") else text)


def estimate(price, values, metrics=None):
    """{"low", "high", "text", "tier", "sure"} - the cost of a run with these inputs (after the run: with its measured
    metrics), or None when the service shows no price."""
    if metrics:  # the service's own record of the run ("model_variant": "medium") settles which tier applied
        values = {**values, **{k: v for k, v in metrics.items() if isinstance(v, str)}}
    tiers, sure = candidate_tiers(price, values)
    if not tiers:
        return None
    lows, highs, parts = [], [], []
    for t in tiers:
        total, unknown = 0.0, False
        for p in t["prices"]:
            n = units(p["metric"], values, metrics)
            if n is None:
                unknown = True
                continue
            total += n * p["price"]
        if unknown and total == 0:
            lows.append(None)
            highs.append(None)
        else:
            lows.append(total)
            highs.append(total)
    known = [x for x in lows if x is not None]
    rates = "; ".join(_rate_range(tiers, i) for i in range(len(tiers[0]["prices"])))
    tier = tiers[0]["title"] if len(tiers) == 1 and tiers[0]["title"] else ""
    if not known:
        typical = (price or {}).get("typical")
        if typical:  # billed by time: the service's own typical run cost
            return {"low": typical, "high": typical, "tier": tier, "sure": False,
                    "text": f"≈ {_fmt(typical)} for a typical run (its page) · {rates}"}
        return {"low": None, "high": None, "tier": tier, "sure": sure,
                "text": f"{rates} (how many isn't known before the run)"}
    lo, hi = min(known), max(known)
    counts = [_count(units(p["metric"], values, metrics), p) for p in tiers[0]["prices"]]
    how = " + ".join(c for c in counts if c) or rates
    if not any(counts):
        how = rates
    elif len(tiers) == 1 or all(len(t["prices"]) == len(tiers[0]["prices"]) for t in tiers):
        how = " + ".join(f"{c} × {_rate_range(tiers, i, short=True)}" for i, c in enumerate(counts) if c)
    money = _fmt(lo) if abs(hi - lo) < 1e-12 else f"{_fmt(lo)}–{_fmt(hi)}"
    note = f" ({tier})" if tier else ("" if sure or len(tiers) == 1 else " (which price tier applies isn't certain)")
    return {"low": lo, "high": hi, "text": f"{'' if metrics else '≈ '}{money} · {how}{note}", "tier": tier, "sure": sure}


TOKEN = "_tokens"


def _rate(price, p, short=False):
    """One price as people read it: per 1M tokens, else per unit."""
    if p["metric"].endswith(TOKEN):
        return f"{_fmt(price * 1e6)}{'/1M' if short else ' per 1M ' + (p['unit'] or 'token') + 's'}"
    return _fmt(price) if short else f"{_fmt(price)} per {p['unit'] or p['metric'] or 'run'}"


def _rate_range(tiers, i, short=False):
    ps = [t["prices"][i] for t in tiers if i < len(t["prices"])]
    lo, hi = min(p["price"] for p in ps), max(p["price"] for p in ps)
    if lo == hi:
        return _rate(lo, ps[0], short)
    a, b = _rate(lo, ps[0], True), _rate(hi, ps[0], short)
    return f"{a}–{b}"


def _count(n, p):
    if n is None:
        return ""
    if p["metric"].endswith(TOKEN):
        return f"{n:,.0f} {(p['unit'] or 'token').replace(' token', '')} tokens"
    word = UNIT_WORDS.get(p["metric"], p["unit"] or "")
    return f"{n:g} {word}".strip() if p["metric"] else ""


def token_bound(details, values, media):
    """A run billed by tokens: the most it can cost, from the model's own limits - output: the run's
    max_output_tokens, else the model's output limit; input: a text-only request has no more tokens than characters,
    with media (not countable before) the model's input limit - beside what its runs usually cost here (the spend
    log). None when a limit isn't recorded."""
    tiers = (details.get("price") or {}).get("tiers") or []
    if len(tiers) != 1:
        return None
    rates = {p["metric"]: p["price"] for p in tiers[0]["prices"]}
    if set(rates) != {"input_tokens", "output_tokens"}:
        return None
    chars = sum(len(str(values.get(k) or "")) for k in ("prompt", "system"))
    n_in = details.get("input_limit") if media else chars
    out_set = values.get("max_output_tokens")
    n_out = int(out_set) if isinstance(out_set, (int, float)) and out_set > 0 else details.get("output_limit")
    if not n_in or not n_out:
        return None
    high = n_in * rates["input_tokens"] + n_out * rates["output_tokens"]
    typical, runs = measured(details.get("id") or details.get("model"))
    text = (f"at most {_fmt(high)} (≤ {n_in:,} input tokens"
            + (" - the model's limit: pictures / videos / sounds aren't counted before the run" if media else "")
            + f", ≤ {n_out:,} output tokens" + ("" if n_out == out_set else " - its limit; max_output_tokens lowers it")
            + ")" + (f" · usually ≈ {_fmt(typical)} here ({runs} run{'s' if runs > 1 else ''})" if runs else ""))
    est = {"low": typical if runs else 0.0, "high": high, "tier": "", "sure": False, "text": text}
    p95 = measured_p95(details.get("id") or details.get("model"))
    if media and p95 is not None:  # the input limit is far above any real request: count what such runs cost here
        est["likely"] = min(high, 2 * p95)
    return est


def measured_p95(model, at_least=20):
    """The 95th percentile cost of a model's succeeded paid runs in the spend log (None below `at_least` runs)."""
    costs = sorted(float(r.get("cost") or 0) for r in entries()
                   if r.get("model") == model and r.get("outcome") == "succeeded" and r.get("kind") != "brain")
    if len(costs) < at_least:
        return None
    return costs[min(len(costs) - 1, int(len(costs) * 0.95))]


def measured(model):
    """(median cost, number of runs) of a model's succeeded paid runs in the spend log."""
    costs = sorted(float(r.get("cost") or 0) for r in entries()
                   if r.get("model") == model and r.get("outcome") == "succeeded" and r.get("kind") != "brain")
    if not costs:
        return None, 0
    return costs[len(costs) // 2], len(costs)


def table_rows(price):
    """The price table as rows for the parameter pane."""
    rows = []
    for t in (price or {}).get("tiers") or []:
        cond = ", ".join(f"{c['title']} {c['description'] or c['value']}" for c in t["criteria"]) or "every run"
        for p in t["prices"]:
            rows.append((cond, _rate(p["price"], p)))
    return rows


def record(**facts):
    """Append one paid run to the spend log."""
    os.makedirs(os.path.dirname(SPEND_LOG), exist_ok=True)
    facts = {"when": time.strftime("%Y-%m-%d %H:%M:%S"), **facts}
    with open(SPEND_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(facts, ensure_ascii=False) + "\n")


_log = {"stamp": None, "rows": []}


def entries():
    """Every line of the spend log (read again only when the file changed - the pane asks while it draws)."""
    try:
        st = os.stat(SPEND_LOG)
    except OSError:
        return []
    stamp = (st.st_mtime_ns, st.st_size)
    if stamp != _log["stamp"]:
        rows = []
        with open(SPEND_LOG, encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        _log.update(stamp=stamp, rows=rows)
    return _log["rows"]


def spent(since=None, service=None):
    """Total recorded cost (dollars) - since a "YYYY-MM-DD" date, for one service."""
    return sum(float(r.get("cost") or 0) for r in entries()
               if not (since and r.get("when", "") < since) and not (service and r.get("service") != service))
