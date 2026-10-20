"""Run speech, translation and language models, choosing HOW from what the files and libraries declare.

No per-model lists: a model is matched by
  - transformers' own registries (config.json "architectures" -> CTC / speech seq2seq / text seq2seq classes)
  - an adapter's adapter_config.json (base model + task type)
  - a library's own file format: VoxCPM config ("architecture": "voxcpm..."), Pocket TTS yaml
    (flow_lm + mimi sections), GigaAM checkpoints named like the library's own models.
Runs inside the engine worker (imports torch & co. from the model's environment).
"""

import glob
import json
import os
import subprocess
import sys
import time

import torch

_loaded = {"key": None, "obj": None}  # keep the last model in memory so re-running is quick


def _cached(key, build, free_other):
    if _loaded["key"] != key:
        free_other()
        _loaded["key"], _loaded["obj"] = None, None
        _loaded["obj"], _loaded["key"] = build(), key
    return _loaded["obj"]


def free():
    _loaded["key"], _loaded["obj"] = None, None
    import gc
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _folder(path):
    return path if os.path.isdir(path) else os.path.dirname(path)


# ---- what kind of model is this? (only declared facts) ----------------------------
def _spandrel_model(path):
    """The image model spandrel recognises in a weight file (upscalers, restorers - identified by spandrel's own
    architecture registry from the tensors), or None."""
    try:
        import spandrel
    except ImportError:
        return None
    try:
        return spandrel.ModelLoader().load_from_file(path)
    except Exception:  # noqa: BLE001 - not an architecture spandrel knows
        return None


def classify(path):
    """(kind, detail) with kind in: asr, seq2seq, causal_lm, voxcpm, pocket_tts, gigaam, image_model, shots -
    or (None, reason)."""
    folder = _folder(path)
    import shots
    found = shots.detector(folder)  # the model card is tagged shot-boundary-detection
    if found is not None:
        return ("shots", found[0]) if found[0] else found
    if os.path.isfile(path) and path.lower().endswith((".safetensors", ".pth", ".pt")):
        model = _spandrel_model(path)
        if model is not None:
            return "image_model", model
    if path.lower().endswith(".ckpt"):
        try:
            import gigaam
            if os.path.splitext(os.path.basename(path))[0] in getattr(gigaam, "_MODEL_HASHES", {}):
                return "gigaam", None
        except ImportError:
            pass
        return None, "a .ckpt checkpoint that no installed library recognises by name"
    adapter = _read_json(os.path.join(folder, "adapter_config.json"))
    if adapter and not os.path.isfile(os.path.join(folder, "config.json")):
        task = adapter.get("task_type", "")
        if task == "SEQ_2_SEQ_LM":
            return "seq2seq", adapter
        if task == "CAUSAL_LM":
            return "causal_lm", adapter
        return None, f"an adapter with task type '{task}'"
    config = _read_json(os.path.join(folder, "config.json"))
    if str(config.get("architecture", "")).lower().startswith("voxcpm"):
        return "voxcpm", None
    pocket_configs = []
    for yml in sorted(glob.glob(os.path.join(folder, "*.yaml"))):
        with open(yml, encoding="utf-8", errors="replace") as f:
            text = f.read()
        if "flow_lm:" in text and "mimi:" in text and "weights_path:" in text:
            weights = next((l.split(":", 1)[1].strip() for l in text.splitlines() if l.startswith("weights_path:")), "")
            pocket_configs.append((os.path.isfile(weights), yml))
    if pocket_configs:
        usable = [y for ok, y in pocket_configs if ok]  # a config whose weights_path really exists
        if not usable:
            return None, "a Pocket TTS config whose weights_path points to a missing file"
        return "pocket_tts", usable[0]
    arch = (config.get("architectures") or [None])[0]
    if arch:
        from transformers.models.auto import modeling_auto as ma
        def known(*maps):
            return any(arch in set(getattr(ma, m, {}).values()) for m in maps)
        if known("MODEL_FOR_CTC_MAPPING_NAMES", "MODEL_FOR_SPEECH_SEQ_2_SEQ_MAPPING_NAMES"):
            return "asr", None
        if known("MODEL_FOR_SEQ_TO_SEQ_CAUSAL_LM_MAPPING_NAMES"):
            return "seq2seq", None
        if known("MODEL_FOR_CAUSAL_LM_MAPPING_NAMES", "MODEL_FOR_IMAGE_TEXT_TO_TEXT_MAPPING_NAMES"):
            return "causal_lm", None
        return None, f"architecture {arch}"
    return None, "no config.json, adapter_config.json or known library files"


def peft_adapter(path):
    """A PEFT adapter folder's adapter_config.json (a folder with adapter_config.json and no config.json), or {}."""
    folder = _folder(path)
    if os.path.isfile(os.path.join(folder, "config.json")):
        return {}
    return _read_json(os.path.join(folder, "adapter_config.json"))


def split_models(models):
    """(main model, [PEFT adapters]) when the connected models are one model plus adapters on top of it
    (files decide: an adapter has adapter_config.json, the base has config.json). A lone adapter is its own
    main model (seq2seq adapters load their base from the id they name). Otherwise (None, [])."""
    adapters = [m for m in models if peft_adapter(m["path"])]
    mains = [m for m in models if m not in adapters]
    if len(mains) == 1:
        return mains[0], adapters
    if not mains and len(adapters) == 1:
        return adapters[0], []
    return None, []


def main_kind(models):
    """classify() of the model a job runs when it's a speech / text model (+ adapters), else None."""
    main, _adapters = split_models(models)
    return classify(main["path"])[0] if main else None


# ---- runners -----------------------------------------------------------------
def run_asr(model, audio, status, free_other):
    from transformers import pipeline
    path = model["path"]

    def build():
        status("Loading speech model…")
        return pipeline("automatic-speech-recognition", model=path, device="cuda", dtype=torch.float16)

    asr = _cached(("asr", path), build, free_other)
    import soundfile as sf
    wav, sr = sf.read(audio, dtype="float32")
    if wav.ndim > 1:
        wav = wav.mean(1)
    kwargs = {}
    language = (model["params"].get("language") or "").strip()
    if language and asr.model.can_generate():  # e.g. Whisper; CTC models have no language switch
        kwargs["generate_kwargs"] = {"language": language}
    status("Transcribing…")
    return asr({"raw": wav, "sampling_rate": sr}, **kwargs)["text"].strip()


def run_gigaam(model, audio, status, free_other):
    import gigaam
    path = model["path"]
    name = os.path.splitext(os.path.basename(path))[0]

    def build():
        status("Loading GigaAM…")
        return gigaam.load_model(name, download_root=os.path.dirname(path))

    m = _cached(("gigaam", path), build, free_other)
    status("Transcribing…")
    result = m.transcribe(audio)
    return str(getattr(result, "text", result)).strip()


def run_seq2seq(model, adapter, text, status, free_other):
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
    path, params = model["path"], model["params"]

    def build():
        status("Loading translation model…")
        if adapter:  # PEFT adapter: base model named in its adapter_config.json
            from peft import PeftModel
            # (a repo id comes from the app's store - the worker's HF_HOME)
            base = AutoModelForSeq2SeqLM.from_pretrained(adapter["base_model_name_or_path"], dtype=torch.bfloat16)
            net = PeftModel.from_pretrained(base, path)
        else:
            net = AutoModelForSeq2SeqLM.from_pretrained(path, dtype=torch.bfloat16)
        return AutoTokenizer.from_pretrained(path), net.to("cuda").eval()

    tok, net = _cached(("seq2seq", path), build, free_other)
    source = (params.get("source_lang") or "").strip()
    target = (params.get("target_lang") or "").strip()
    if source and hasattr(tok, "src_lang"):
        tok.src_lang = source  # e.g. NLLB 'rus_Cyrl'
    ins = tok(text, return_tensors="pt").to("cuda")
    gen = {"max_new_tokens": int(params.get("max_tokens") or 512)}
    if target:
        # only a special language token the tokenizer declares (e.g. NLLB 'eng_Latn') - never an ordinary word piece
        if target not in getattr(tok, "added_tokens_encoder", {}):
            raise ValueError(f"'{target}' isn't a language token of this model's tokenizer; "
                             "leave 'To language' empty and say the target in the text if the model works that way")
        gen["forced_bos_token_id"] = tok.convert_tokens_to_ids(target)
    status("Translating…")
    with torch.inference_mode():
        out = net.generate(**ins, **gen)
    return tok.decode(out[0], skip_special_tokens=True).strip()


def _card_task(folder):
    """pipeline_tag from the model card's front matter (README.md), or ''."""
    try:
        with open(os.path.join(folder, "README.md"), encoding="utf-8", errors="replace") as f:
            text = f.read(20000)
    except OSError:
        return ""
    front = text.split("---", 2)[1] if text.startswith("---") else ""
    line = next((l for l in front.splitlines() if l.startswith("pipeline_tag:")), "")
    return line.split(":", 1)[1].strip().strip("'\"") if line else ""


def generation_task(path):
    """Which transformers pipeline runs this language model: the task its model card declares when
    transformers has a pipeline for it; otherwise the task transformers' own registries map its class to."""
    from transformers.pipelines import SUPPORTED_TASKS
    folder = _folder(path)
    declared = _card_task(folder)
    if declared in ("image-text-to-text", "text-generation") and declared in SUPPORTED_TASKS:
        return declared
    from transformers.models.auto import modeling_auto as ma
    arch = (_read_json(os.path.join(folder, "config.json")).get("architectures") or [None])[0]
    vision = arch in set(getattr(ma, "MODEL_FOR_IMAGE_TEXT_TO_TEXT_MAPPING_NAMES", {}).values())
    return "image-text-to-text" if vision else "text-generation"


def has_image_processor(path):
    """Whether the folder has the files transformers builds an image processor from."""
    folder = _folder(path)
    return any(os.path.isfile(os.path.join(folder, f)) for f in ("preprocessor_config.json", "processor_config.json"))


def _adapter_name(path):
    import hashlib
    return "lora_" + hashlib.md5(os.path.normcase(os.path.abspath(path)).encode()).hexdigest()[:10]


def _apply_adapters(net, adapters, status):
    """Make exactly the connected PEFT adapters active on the loaded model, each at its node's Strength
    (a multiplier on the adapter's own lora_alpha / r scale). Loaded once; disconnected ones are removed."""
    wanted = {_adapter_name(a["path"]): a for a in adapters}
    loaded = getattr(net, "_node_editor_adapters", set())
    gone = sorted(loaded - set(wanted))
    if gone:
        net.delete_adapter(gone)
    for name, a in wanted.items():
        if name not in loaded:
            status(f"Loading adapter {a['name']}…")
            net.load_adapter(a["path"], adapter_name=name)
    net._node_editor_adapters = set(wanted)
    if not wanted:
        return
    net.set_adapter(list(wanted))
    from peft.tuners.lora import LoraLayer
    for module in net.modules():
        if isinstance(module, LoraLayer):
            for name, a in wanted.items():
                module.set_scale(name, float(a["params"].get("strength", 1.0)))


def _adapter_notes(model, adapters):
    """Notes when an adapter's adapter_config.json names a base other than the connected model's repo."""
    parts = os.path.normpath(_folder(model["path"])).split(os.sep)
    repo = (parts[-3][len("models--"):].replace("--", "/", 1)
            if len(parts) >= 3 and parts[-2] == "snapshots" and parts[-3].startswith("models--") else None)
    notes = []
    for a in adapters:
        base = peft_adapter(a["path"]).get("base_model_name_or_path")
        if base and repo and base != repo:
            notes.append(f"{a['name']} was trained on {base} (connected base: {repo})")
        elif base and not repo:
            notes.append(f"{a['name']} names its base {base}; {model['name']} has no repo id to compare")
    return notes


def _user_message(job, vision):
    """One user message: each connected input as a block named after its node (<prompt1>...</prompt1>, an image
    between <photo1> and </photo1>). A single text goes as it is."""
    named = job.get("named") or {}
    texts = named.get("text") or ([["text", job["prompt"]]] if job["prompt"] else [])
    images = named.get("image") or [[f"image{i + 1}", p] for i, p in enumerate(job["images"])]
    if named.get("video") or job["videos"] or named.get("audio") or job["audios"]:
        raise ValueError("Language models here take text and images only (video / audio aren't supported yet)")
    if images and not vision:
        raise ValueError("This language model doesn't take images")
    if not (texts or images):
        raise ValueError("Nothing to answer: connect a prompt (text) to the Modality")
    if len(texts) == 1 and not images:
        return texts[0][1] if not vision else [{"type": "text", "text": texts[0][1]}]
    parts = []
    from PIL import Image  # opened here: some torchvision builds (ROCm) can't decode PNG files themselves
    for name, p in images:
        parts += [{"type": "text", "text": f"<{name}>"}, {"type": "image", "image": Image.open(p).convert("RGB")},
                  {"type": "text", "text": f"</{name}>"}]
    for name, text in texts:
        parts.append({"type": "text", "text": f"<{name}>\n{text}\n</{name}>"})
    return parts if vision else "\n\n".join(p["text"] for p in parts)


def load_language_model(model, adapters, status, free_other, notes=None):
    """The transformers pipeline for a language model (kept loaded between renders) with exactly the given
    PEFT adapters active. Returns (pipeline, task, tokenizer)."""
    from transformers import pipeline
    path = model["path"]
    task = generation_task(path)
    if task == "image-text-to-text" and not has_image_processor(path):
        # a vision model saved without its image processor (e.g. a merge that kept only model + tokenizer)
        task = "text-generation"
        if notes is not None:
            notes.append("no preprocessor_config.json in the model folder, so it ran text-only (no image input)")

    def build():
        status(f"Loading language model ({task})…")
        return pipeline(task, model=path, device_map="cuda", dtype=torch.bfloat16)

    pipe = _cached(("generate", path, task), build, free_other)
    if adapters is not None:  # None: leave the loaded adapters as they are (run_chat switches them off itself)
        _apply_adapters(pipe.model, adapters, status)
    return pipe, task, getattr(pipe, "tokenizer", None) or pipe.processor.tokenizer


def stop_tokens(net, tokenizer):
    """The model's declared end token(s) AND the tokenizer's eos_token (the chat template's end of turn):
    a model whose config ends on <|endoftext|> while its template ends turns with <|im_end|> would otherwise
    run on past the answer (seen with a LoRA trained to end on the template's token)."""
    stops = net.generation_config.eos_token_id
    stops = set(stops if isinstance(stops, list) else [stops] if stops is not None else [])
    if tokenizer.eos_token_id is not None:
        stops.add(tokenizer.eos_token_id)
    return sorted(stops)


def run_generate(model, job, status, free_other, adapters=(), notes=None):
    """A language model (+ PEFT adapters) answering the connected prompts (about the connected pictures).
    Returns (answer, reasoning)."""
    params = model["params"]
    pipe, task, tokenizer = load_language_model(model, adapters, status, free_other, notes)
    vision = task == "image-text-to-text"
    messages = [{"role": "user", "content": _user_message(job, vision)}]

    torch.manual_seed(int(params.get("seed") or 0))
    temperature = float(params.get("temperature") or 0)
    gen = {"max_new_tokens": int(params.get("max_tokens") or 512), "do_sample": temperature > 0}
    if temperature > 0:
        gen["temperature"] = temperature
    stops = stop_tokens(pipe.model, tokenizer)
    if stops:
        gen["eos_token_id"] = stops
    template = str(getattr(getattr(pipe, "processor", None), "chat_template", None)
                   or getattr(tokenizer, "chat_template", "") or "")
    template_vars = {}
    if "enable_thinking" in template:  # the chat template's own switch
        template_vars["enable_thinking"] = bool(params.get("thinking"))
    status("Generating…")
    if vision:  # loose keyword arguments go to the chat template; generation settings explicitly
        out = pipe(text=messages, return_full_text=False, generate_kwargs=gen, **template_vars)
    else:
        out = pipe(messages, return_full_text=False, tokenizer_encode_kwargs=template_vars, **gen)
    text = str(out[0]["generated_text"]).strip()
    if "</think>" in template and "</think>" in text:  # the template's own marker ends the reasoning
        reasoning, answer = text.split("</think>", 1)
        return answer.strip(), reasoning.replace("<think>", "").strip()
    if template_vars.get("enable_thinking") and "</think>" in template:
        # No end-of-reasoning marker: either it was still thinking when 'Max new tokens' ran out, or the model
        # doesn't reason in its template's format (e.g. a fine-tune). We can't tell which, so show the text.
        return text.replace("<think>", "").strip(), (
            "(Thinking was on, but the output has no </think>, so reasoning and answer can't be told apart - "
            "the Result above is everything the model wrote. If it stopped at the 'Max new tokens' limit it may "
            "be unfinished thinking; if the model wasn't trained to think, turn Thinking off.)")
    return text, ""


def run_image_model(model, descriptor, image_path, out_path, status, free_other):
    """An image-to-image model spandrel loaded (e.g. an upscaler): run the image through it."""
    import numpy as np
    from PIL import Image
    path = model["path"]

    def build():
        status(f"Loading {descriptor.architecture.name} ({descriptor.purpose}, {descriptor.scale}×)…")
        descriptor.to("cuda").eval()
        if descriptor.supports_half:
            descriptor.half()
        return descriptor

    net = _cached(("image_model", path), build, free_other)
    img = np.asarray(Image.open(image_path).convert("RGB"), dtype=np.float32) / 255.0
    x = torch.from_numpy(img).permute(2, 0, 1).unsqueeze(0).to("cuda")
    x = x.half() if net.supports_half else x
    status(f"Running {net.architecture.name} on {img.shape[1]}×{img.shape[0]}…")
    with torch.inference_mode():
        y = net(x)
    out = (y.squeeze(0).float().clamp(0, 1).permute(1, 2, 0).cpu().numpy() * 255.0).round().astype(np.uint8)
    Image.fromarray(out).save(out_path)
    return out_path


def run_voxcpm(model, text, out_path, status, free_other):
    import numpy as np
    import soundfile as sf
    from voxcpm import VoxCPM
    path, params = model["path"], model["params"]

    def build():
        status("Loading VoxCPM…")
        return VoxCPM(voxcpm_model_path=path, enable_denoiser=False, device="cuda")

    tts = _cached(("voxcpm", path), build, free_other)
    torch.manual_seed(int(params.get("seed") or 0))
    voice = (params.get("voice") or "").strip() or None
    status("Speaking…")
    wav = tts.generate(text=text, reference_wav_path=voice, cfg_value=2.0, inference_timesteps=10,
                       normalize=False, denoise=False)
    wav = np.asarray(wav, dtype=np.float32).squeeze()
    sr = getattr(getattr(tts, "tts_model", tts), "sample_rate", None)
    if not sr:
        raise RuntimeError("VoxCPM didn't report its sample rate")
    sf.write(out_path, wav, sr)
    return out_path


def run_pocket_tts(model, config_yaml, text, out_path, status):
    params = model["params"]
    voice = (params.get("voice") or "").strip()
    if not voice:
        raise ValueError("Pocket TTS needs a voice: set 'Voice sample' on the model node to a voice .safetensors")
    cli = os.path.join(os.path.dirname(sys.executable), "pocket-tts.exe")
    status("Speaking (Pocket TTS)…")
    p = subprocess.run([cli, "generate", "--config", config_yaml, "--voice", voice, "--text", text,
                        "--output-path", out_path, "--device", "cuda"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout).strip().splitlines()[-1])
    return out_path


# ---- a language model as an Executor's brain ------------------------------------
_PROBE = ("PROBEFN", "PROBEA", "PROBEV1", "PROBEB", "PROBEV2")


def _common_prefix(a, b):
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return n


def call_format(tokenizer, template_vars):
    """How this model writes a tool call, read from its own chat template: a probe call (a made-up name with two
    made-up arguments) is rendered as a past assistant turn and compared with the same turn without it. Two
    layouts come out of templates: a JSON object between markers ({"style": "json", open, close, name key,
    arguments key}) or tags around the name and each argument ({"style": "tags", open, mid, p1, p2, p3, close}).
    None if the template writes no tool calls."""
    fn, a, v1, b, v2 = _PROBE
    user = {"role": "user", "content": "hi"}
    call = {"role": "assistant", "content": "",
            "tool_calls": [{"type": "function", "function": {"name": fn, "arguments": {a: v1, b: v2}}}]}
    try:
        with_call = tokenizer.apply_chat_template([user, call], tokenize=False, **template_vars)
        without = tokenizer.apply_chat_template([user, {"role": "assistant", "content": ""}], tokenize=False,
                                                **template_vars)
    except Exception:  # noqa: BLE001 - a template that can't render tool calls
        return None
    if fn not in with_call:
        return None
    start = _common_prefix(with_call, without)
    while start > 0 and not with_call[start - 1].isspace() and with_call[start - 1] != ">":
        start -= 1  # the call's own marker may begin like what follows without it ("<tool_call>" / "<|im_end|>")
    end = _common_prefix(with_call[::-1], without[::-1])
    text = with_call[start:len(with_call) - end]
    k = text.find(fn)
    brace = text.rfind("{", 0, k)
    if brace >= 0 and f'"{fn}"' in text:
        obj, length = json.JSONDecoder().raw_decode(text[brace:])
        name_key = next(key for key, val in obj.items() if val == fn)
        args_key = next(key for key, val in obj.items() if a in json.dumps(val))
        return {"style": "json", "open": text[:brace], "close": text[brace + length:], "name": name_key,
                "args": args_key}
    ia, i1, ib, i2 = (text.find(s) for s in (a, v1, b, v2))
    if min(ia, i1, ib, i2) < 0:
        return None
    x, y, z = text[k + len(fn):ia], text[i1 + len(v1):ib], text[i2 + len(v2):]
    p1 = y[len(y) - _common_prefix(x[::-1], y[::-1]):]  # what starts every argument
    p3 = y[:len(y) - len(p1)]                            # what ends every argument
    if not z.startswith(p3):
        return None
    return {"style": "tags", "open": text[:k], "mid": x[:len(x) - len(p1)], "p1": p1, "p2": text[ia + len(a):i1],
            "p3": p3, "close": z[len(p3):]}


def _literal(s):
    """A template's literal text as a pattern that tolerates different whitespace between its pieces."""
    import re
    pieces = [re.escape(p) for p in s.split()]
    return r"\s*" + r"\s*".join(pieces) + r"\s*" if pieces else r"\s*"


def parse_calls(text, fmt):
    """(text before the first call, [{"name", "arguments"}]) - the calls written the way call_format found.
    Tag-style values stay text (the app converts them by the tool's declared types)."""
    import re
    calls = []
    opener = re.compile(_literal(fmt["open"]))
    first = opener.search(text)
    if not first:
        return text.strip(), []
    content = text[:first.start()].strip()
    if fmt["style"] == "json":
        pos = 0
        while (m := opener.search(text, pos)) is not None:
            brace = text.find("{", m.end())
            if brace < 0:
                break
            try:
                obj, length = json.JSONDecoder().raw_decode(text[brace:])
            except ValueError:
                break
            args = obj.get(fmt["args"], {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            calls.append({"name": str(obj.get(fmt["name"], "")), "arguments": args if isinstance(args, dict) else {}})
            pos = brace + length
        return content, calls
    call = re.compile(_literal(fmt["open"]) + r"(?P<name>[^\s<>\"]+)" + _literal(fmt["mid"]) + r"(?P<body>.*?)"
                      + _literal(fmt["close"]), re.S)
    arg = re.compile(_literal(fmt["p1"]) + r"(?P<key>[^\s<>=\"]+?)" + _literal(fmt["p2"]) + r"(?P<val>.*?)"
                     + _literal(fmt["p3"]) + r"(?=" + _literal(fmt["p1"]) + r"|$)", re.S)
    for m in call.finditer(text):
        body = m.group("body")
        calls.append({"name": m.group("name"), "arguments": {a.group("key"): a.group("val").strip()
                                                              for a in arg.finditer(body)}})
    return content, calls


def run_chat(job, emit, free_other):
    """One turn of a language model as an Executor's brain: the conversation so far + the tools (job["chat"]) in,
    its words and tool calls out. The model's PEFT adapters loaded for renders are switched off for the brain."""
    status = lambda msg: emit(event="status", message=msg)  # noqa: E731
    t0 = time.time()
    model, _adapters = split_models(job["models"])
    if model is None:
        raise ValueError("the brain is one language model")
    params = model["params"]
    pipe, _task, tokenizer = load_language_model(model, None, status, free_other)
    template = str(getattr(tokenizer, "chat_template", "") or "")
    template_vars = {"enable_thinking": bool(params.get("thinking"))} if "enable_thinking" in template else {}
    key = ("call_format", model["path"], tuple(template_vars.items()))
    if key not in _formats:
        _formats[key] = call_format(tokenizer, template_vars)
    fmt = _formats[key]
    if fmt is None:
        raise ValueError(f"{model['name']}'s chat template writes no tool calls, so it can't work as a brain")
    chat = job["chat"]
    prompt = tokenizer.apply_chat_template(chat["messages"], tools=chat["tools"], add_generation_prompt=True,
                                           tokenize=False, **template_vars)
    net = pipe.model
    ids = tokenizer(prompt, return_tensors="pt", add_special_tokens=False).to(net.device)
    limit = int(params.get("max_tokens") or 512)
    temperature = float(params.get("temperature") or 0)
    gen = {"max_new_tokens": limit, "do_sample": temperature > 0, "eos_token_id": stop_tokens(net, tokenizer)}
    if temperature > 0:
        gen["temperature"] = temperature
    torch.manual_seed(int(params.get("seed") or 0))
    adapters = bool(getattr(net, "_node_editor_adapters", None))
    status(f"Thinking… ({ids['input_ids'].shape[1]} tokens in)")
    if adapters:
        net.disable_adapters()
    try:
        with torch.inference_mode():
            out = net.generate(**ids, **gen)
    finally:
        if adapters:
            net.enable_adapters()
    new = out[0][ids["input_ids"].shape[1]:]
    text = tokenizer.decode(new, skip_special_tokens=False)
    for stop in gen["eos_token_id"]:  # the end-of-turn token and anything after it
        text = text.split(tokenizer.decode([stop]), 1)[0]
    literals = "".join(str(v) for v in fmt.values())
    for special in tokenizer.all_special_tokens:  # padding etc. - but not a marker the call format uses
        if special not in literals:
            text = text.replace(special, "")
    reasoning = ""
    if "</think>" in template and "</think>" in text:
        reasoning, text = text.split("</think>", 1)
        reasoning = reasoning.replace("<think>", "").strip()
    content, calls = parse_calls(text, fmt)
    return {"kind": "chat", "path": "", "content": content, "tool_calls": calls, "reasoning": reasoning,
            "raw": text, "prompt_tokens": int(ids["input_ids"].shape[1]), "new_tokens": int(len(new)),
            "cut": len(new) >= limit, "seconds": round(time.time() - t0, 1)}


_formats = {}


# ---- entry point ---------------------------------------------------------------
def run(job, emit, free_other):
    """Run a job whose main model is a speech or text model. Returns the engine 'done' payload."""
    status = lambda msg: emit(event="status", message=msg)  # noqa: E731
    t0 = time.time()
    model, adapters = split_models(job["models"])
    if model is None:
        raise ValueError("Speech, translation and language models run on their own (plus adapters on top) - "
                         "connect one model and, optionally, its adapters")
    kind, detail = classify(model["path"])
    if kind is None:
        raise ValueError(f"{model['name']}: the engine doesn't know how to run {detail}")
    if kind == "causal_lm" and detail:  # detail = adapter_config.json: a language-model adapter on its own
        raise ValueError(f"{model['name']} is an adapter - connect its base model too (through a Merge node); "
                         f"its adapter_config.json names {detail.get('base_model_name_or_path', 'no base')}")
    if adapters and kind != "causal_lm":
        raise ValueError("Adapters on top of a model are supported for language models only (so far)")

    os.makedirs(job["output_dir"], exist_ok=True)
    base = os.path.join(job["output_dir"], f"{job['name']}_{time.strftime('%Y%m%d_%H%M%S')}")

    if kind == "shots":
        if not job["videos"]:
            raise ValueError("Connect a video to find its shots")
        import shots
        out = base + ".json"  # its "format" line says shots
        data = shots.run(model, job["videos"][0], out, status, lambda key, build: _cached(key, build, free_other))
        names = [n for n, _ in (job.get("named") or {}).get("video") or []]
        note = f"one video at a time: used {names[0]}, not used: {', '.join(names[1:])}" if len(names) > 1 else ""
        return {"kind": "data", "path": out, "seconds": round(time.time() - t0, 1), "note": note,
                "text": f"{len(data['shots'])} shots, {len(data['transitions'])} transitions"}
    if kind == "image_model":
        if not job["images"]:
            raise ValueError("Connect an image for this model to process")
        out = base + ".png"
        run_image_model(model, detail, job["images"][0], out, status, free_other)
        names = [n for n, _ in (job.get("named") or {}).get("image") or []]
        note = (f"it takes one picture: used {names[0]}, not used: {', '.join(names[1:])}"
                if len(names) > 1 else "")
        return {"kind": "image", "path": out, "seconds": round(time.time() - t0, 1), "note": note}
    if kind in ("asr", "gigaam"):
        if not job["audios"]:
            raise ValueError("Connect audio to transcribe")
        runner = run_asr if kind == "asr" else run_gigaam
        text = runner(model, job["audios"][0], status, free_other)
    elif kind == "causal_lm":
        notes = _adapter_notes(model, adapters)
        text, reasoning = run_generate(model, job, status, free_other, adapters, notes)
        with open(base + ".txt", "w", encoding="utf-8") as f:
            f.write(text if not reasoning else f"{text}\n\n--- reasoning ---\n{reasoning}")
        return {"kind": "text", "text": text, "reasoning": reasoning, "path": base + ".txt",
                "seconds": round(time.time() - t0, 1), "note": "; ".join(notes)}
    elif kind == "seq2seq":
        if not job["prompt"]:
            raise ValueError("Connect text to translate")
        text = run_seq2seq(model, detail, job["prompt"], status, free_other)
    else:  # text to speech
        if not job["prompt"]:
            raise ValueError("Connect text to speak")
        out = base + ".wav"
        if kind == "voxcpm":
            run_voxcpm(model, job["prompt"], out, status, free_other)
        else:
            run_pocket_tts(model, detail, job["prompt"], out, status)
        return {"kind": "audio", "path": out, "seconds": round(time.time() - t0, 1)}

    with open(base + ".txt", "w", encoding="utf-8") as f:
        f.write(text)
    return {"kind": "text", "text": text, "path": base + ".txt", "seconds": round(time.time() - t0, 1)}
