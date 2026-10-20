"""Identify AI model files by reading only their header (no weights are loaded).

A .safetensors file starts with an 8-byte little-endian length N, followed by N bytes
of JSON: {tensor_name: {"dtype", "shape", "data_offsets"}, "__metadata__": {...}}.
The tensor names reveal what the model is (VAE, text encoder, diffusion model, LoRA...).
"""

import json
import os
import re
import struct
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from math import prod

# ---- roles ---------------------------------------------------------------
DIFFUSION = "diffusion"
TEXT_ENCODER = "text_encoder"
VAE = "vae"
LORA = "lora"
IMAGE_ENCODER = "image_encoder"
CHECKPOINT = "checkpoint"
OTHER = "other"
UNKNOWN = "unknown"
# Complete models: they run on their own with just their input.
SPEECH_RECOGNITION = "asr"
TRANSLATION = "translation"
LANGUAGE_MODEL = "llm"
TEXT_TO_SPEECH = "tts"

ROLE_LABELS = {
    DIFFUSION: "Diffusion model",
    TEXT_ENCODER: "Text encoder",
    VAE: "VAE",
    LORA: "LoRA",
    IMAGE_ENCODER: "Image encoder",
    CHECKPOINT: "Checkpoint (bundle)",
    SPEECH_RECOGNITION: "Speech recognition",
    TRANSLATION: "Translation",
    LANGUAGE_MODEL: "Language model",
    TEXT_TO_SPEECH: "Text to speech",
    OTHER: "Other model",
    UNKNOWN: "Unknown",
}

DTYPE_BYTES = {"F64": 8, "I64": 8, "F32": 4, "I32": 4, "F16": 2, "BF16": 2, "I16": 2,
               "F8_E4M3": 1, "F8_E5M2": 1, "I8": 1, "U8": 1, "BOOL": 1}
DTYPE_NAMES = {"F32": "fp32", "F16": "fp16", "BF16": "bf16", "F8_E4M3": "fp8 e4m3",
               "F8_E5M2": "fp8 e5m2", "I8": "int8", "U8": "uint8", "F64": "fp64"}

# Tensors that hold quantization bookkeeping rather than weights.
AUX_SUFFIXES = (".comfy_quant", ".weight_scale", ".weight_scale_2", ".input_scale",
                ".scale_weight", ".scale_input")
LORA_MARKERS = (".lora_A.", ".lora_B.", ".lora_up.", ".lora_down.", ".lora.",
                "lora_unet_", "lora_te", ".hada_", ".lokr_")
LORA_PREFIXES = ("base_model.model.", "model.diffusion_model.", "diffusion_model.",
                 "transformer.", "unet.")


@dataclass
class ModelInfo:
    role: str = UNKNOWN
    family: str = ""          # machine tag, e.g. "qwen-image" (used later to match parts)
    family_label: str = ""    # human-readable architecture
    media: str = ""           # image / video / audio / text
    quantized: str = ""       # "" or description of a non-standard quantization
    details: list = field(default_factory=list)  # [(label, value)] for the parameter panel
    error: str = ""


# ---- header reading --------------------------------------------------------
GGML_TYPES = {0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 6: "Q5_0", 7: "Q5_1", 8: "Q8_0", 9: "Q8_1", 10: "Q2_K",
              11: "Q3_K", 12: "Q4_K", 13: "Q5_K", 14: "Q6_K", 15: "Q8_K", 16: "IQ2_XXS", 17: "IQ2_XS",
              18: "IQ3_XXS", 19: "IQ1_S", 20: "IQ4_NL", 21: "IQ3_S", 22: "IQ2_S", 23: "IQ4_XS", 24: "I8",
              25: "I16", 26: "I32", 27: "I64", 28: "F64", 29: "IQ1_M", 30: "BF16"}  # the GGUF format's own type ids


def read_gguf_header(path):
    """({tensor name: {"dtype", "shape"}}, {metadata key: value}) from a GGUF file's header (no tensor data)."""
    scalar = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}

    def rd(f, fmt):
        return struct.unpack("<" + fmt, f.read(struct.calcsize(fmt)))[0]

    def rstr(f):
        return f.read(rd(f, "Q")).decode("utf-8", "replace")

    def rval(f, t):
        if t == 8:
            return rstr(f)
        if t == 9:
            et, n = rd(f, "I"), rd(f, "Q")
            vals = [rval(f, et) for _ in range(n)]
            return vals if n <= 16 else f"[{n} values]"
        return rd(f, scalar[t])

    with open(path, "rb") as f:
        if f.read(4) != b"GGUF":
            raise ValueError("not a GGUF file")
        rd(f, "I")  # version
        n_tensors, n_kv = rd(f, "Q"), rd(f, "Q")
        metadata = {}
        for _ in range(n_kv):
            key, t = rstr(f), rd(f, "I")
            metadata[key] = rval(f, t)
        header = {}
        for _ in range(n_tensors):
            name = rstr(f)
            dims = [rd(f, "Q") for _ in range(rd(f, "I"))]
            ttype = rd(f, "I")
            rd(f, "Q")  # data offset
            header[name] = {"dtype": GGML_TYPES.get(ttype, f"type{ttype}"), "shape": dims[::-1]}  # torch order
    return header, {k: v for k, v in metadata.items() if not k.startswith("tokenizer.ggml.")}


def read_header(path):
    if path.lower().endswith(".gguf"):
        return read_gguf_header(path)
    with open(path, "rb") as f:
        raw = f.read(8)
        if len(raw) < 8:
            raise ValueError("file too small")
        (n,) = struct.unpack("<Q", raw)
        if n <= 1 or n > 200 * 1024 * 1024:
            raise ValueError("not a safetensors file")
        header = json.loads(f.read(n))
    metadata = header.pop("__metadata__", None) or {}
    return header, metadata


FOLDER_MARKERS = ("config.json", "adapter_config.json")
WEIGHT_EXT = (".safetensors", ".sft", ".gguf")  # formats whose header we can read
OTHER_WEIGHT_EXT = (".ckpt", ".pt", ".pth", ".bin", ".nemo", ".onnx")


def _is_model_folder(folder):
    return os.path.isdir(folder) and any(os.path.isfile(os.path.join(folder, m)) for m in FOLDER_MARKERS)


def model_root(path):
    """What a model 'is' on disk: a Hugging Face style folder (config.json / adapter_config.json,
    or weights split into shards) or a single weight file. Any file inside such a folder
    resolves to the folder, so a model is always ONE node."""
    if os.path.isdir(path):
        return path
    folder = os.path.dirname(path)
    if _is_model_folder(folder) or path.endswith(".index.json") or re.search(r"-\d{5}-of-\d{5}\.safetensors$", path):
        return folder
    return path


def model_files(path):
    root = model_root(path)
    if os.path.isdir(root):
        return sorted(os.path.join(root, f) for f in os.listdir(root) if f.endswith(WEIGHT_EXT))
    return [root]


def chat_template(path):
    """The model's chat template text (chat_template.jinja / .json, or tokenizer_config.json), or ''."""
    root = model_root(path)
    if not os.path.isdir(root):
        return ""
    try:
        with open(os.path.join(root, "chat_template.jinja"), encoding="utf-8") as f:
            return f.read()
    except OSError:
        pass
    for name in ("chat_template.json", "tokenizer_config.json"):
        template = _read_json(os.path.join(root, name)).get("chat_template")
        if isinstance(template, str):
            return template
    return ""


def display_name(folder):
    """A readable name for a model folder: the repo name for a Hugging Face cache snapshot
    (...\\models--org--name\\snapshots\\<revision>), otherwise the folder's own name."""
    parts = os.path.normpath(folder).split(os.sep)
    if len(parts) >= 3 and parts[-2] == "snapshots" and parts[-3].startswith("models--"):
        return parts[-3].split("--", 2)[-1]
    return os.path.basename(folder)


def has_weights(folder):
    return os.path.isdir(folder) and any(f.endswith(WEIGHT_EXT + OTHER_WEIGHT_EXT) for f in os.listdir(folder))


def declared_part(path):
    """(part name, 'library.Class') when the model is a part of a diffusers pipeline whose model_index.json
    lists it (e.g. ('vae', 'diffusers.AutoencoderKL')); otherwise None."""
    root = model_root(path)
    if not os.path.isdir(root):
        return None
    entry = _read_json(os.path.join(os.path.dirname(root), "model_index.json")).get(os.path.basename(root))
    if isinstance(entry, list) and len(entry) == 2 and all(entry):
        return os.path.basename(root), f"{entry[0]}.{entry[1]}"
    return None


def pipeline_parts(folder):
    """[(part, 'library.Class', part folder)] for a diffusers pipeline folder (model_index.json), keeping
    only the parts that have weight files (the scheduler and tokenizer are settings, not models)."""
    index = _read_json(os.path.join(folder, "model_index.json"))
    parts = []
    for name, entry in index.items():
        sub = os.path.join(folder, name)
        if not name.startswith("_") and isinstance(entry, list) and len(entry) == 2 and all(entry) \
                and has_weights(sub):
            parts.append((name, f"{entry[0]}.{entry[1]}", sub))
    return parts


def inspect(path) -> ModelInfo:
    """Inspect a model file or folder. Results are cached until the files change."""
    if not os.path.exists(path):
        return ModelInfo(error="File not found")
    root = model_root(path)
    files = model_files(path)
    markers = [os.path.join(root, m) for m in FOLDER_MARKERS] if os.path.isdir(root) else []
    stamp = tuple((f, os.stat(f).st_mtime, os.stat(f).st_size) for f in files + [m for m in markers if os.path.isfile(m)])
    return _inspect_cached(root, stamp)


@lru_cache(maxsize=128)
def _inspect_cached(root, stamp):
    weights = [(f, m, s) for f, m, s in stamp if f.endswith(WEIGHT_EXT)]
    header, metadata = {}, {}
    error = ""
    try:
        for f, _, _ in weights:
            h, m = read_header(f)
            header.update(h)
            metadata.update(m)
    except Exception as e:  # noqa: BLE001 - any parse failure means "not a model we can read"
        error = f"Can't read as .safetensors ({e})"

    info = analyze(header, metadata) if header else ModelInfo()
    if header and any(f.lower().endswith(".gguf") for f, _, _ in weights):
        _set_detail(info, "Format", "GGUF (quantized; runs with stable-diffusion.cpp / llama.cpp)")
    if os.path.isdir(root):
        _analyze_folder(info, root)  # config.json is the authority for complete models
    part = declared_part(root) if header else None
    if info.role == UNKNOWN and part and part[0] in ("transformer", "unet"):
        # diffusers names a pipeline's denoising network 'transformer' or 'unet': its model_index.json says what
        # this part is even when the weight layout is new to us
        info.role = DIFFUSION
        _set_detail(info, "Type", f"{ROLE_LABELS[DIFFUSION]} (the pipeline's {part[0]}, model_index.json)")
    if info.role == UNKNOWN:
        _guess_from_name(info, root)
    index = _read_json(os.path.join(root, "model_index.json")) if os.path.isdir(root) else {}
    if not header and index.get("_class_name"):
        # a whole diffusers pipeline: its model_index.json declares it complete (the parts are in its sub-folders);
        # the engine loads it as one (DiffusionPipeline.from_pretrained) - one Model node for all of it
        if info.role == UNKNOWN:
            info.role = CHECKPOINT
        _set_detail(info, "Pipeline", f"{index['_class_name']} (model_index.json) - all its parts, as one model")
    elif not header:
        others = [f for f in (os.listdir(root) if os.path.isdir(root) else [root]) if f.endswith(OTHER_WEIGHT_EXT)]
        if others and not error:  # a PyTorch checkpoint etc.: it may well run, the app just can't look inside
            _set_detail(info, "Format", "{} - contents not inspected here (only safetensors / GGUF are read)".format(
                ", ".join(sorted({os.path.splitext(f)[1] for f in others}))))
        else:
            info.error = error or "No model weights here"
    tags = model_card(root if os.path.isdir(root) else os.path.dirname(root)).get("tags")
    if tags:
        info.details.append(("Card tags", tags))
    if len(weights) > 1:
        info.details.append(("Files", f"{len(weights)} weight files"))
    if weights:
        info.details.append(("File size", f"{sum(s for _, _, s in weights) / 1024**3:.2f} GB"))
    return info


# ---- where a model comes from (only what the files themselves say) ---------
HF = "https://huggingface.co/"


def _hf_id(value):
    """'org/name' Hugging Face ids only (not local paths)."""
    value = (value or "").strip()
    return value if re.fullmatch(r"[\w.\-]+/[\w.\-]+", value) else None


def model_card(folder):
    """Front-matter fields of a Hugging Face model card (README.md) in the folder or its parent."""
    for d in (folder, os.path.dirname(folder)):
        path = os.path.join(d, "README.md")
        if os.path.isfile(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
            front = m.group(1) if m else ""
            fields = {}
            for key in ("pipeline_tag", "library_name", "base_model", "license", "tags"):
                one = re.search(rf"^{key}:[ \t]*(\S.*)$", front, re.M)  # same line only (else it's a list)
                many = re.search(rf"^{key}:\s*\n((?:\s*-\s*.*\n?)+)", front, re.M)
                if one:
                    fields[key] = one.group(1).strip().strip("'\"")
                elif many:
                    fields[key] = ", ".join(x.strip(" -'\"") for x in many.group(1).strip().splitlines())
            return fields
    return {}


def sources(path):
    """[(label, value)] of evidence about origin; a value may be ('link', text, url).
    Nothing here is guessed: it is read from the files, or it is a search link for you to check."""
    root = model_root(path)
    rows, ids = [], []
    if os.path.isdir(root):
        for name, key in (("config.json", "_name_or_path"), ("adapter_config.json", "base_model_name_or_path")):
            value = _read_json(os.path.join(root, name)).get(key)
            if _hf_id(value):
                ids.append((("Base model" if name.startswith("adapter") else "Hugging Face"), value))
            elif value and name.startswith("adapter"):
                rows.append(("Base model", value))  # a local folder
        card = model_card(root)
        for base in (card.get("base_model") or "").split(","):
            if _hf_id(base) and base.strip() not in [i for _, i in ids]:
                ids.append(("Base model", base.strip()))
        if card.get("pipeline_tag"):
            rows.append(("Declared task", f"{card['pipeline_tag']} (model card)"))
        if card.get("library_name"):
            rows.append(("Library", card["library_name"]))
        if card.get("license"):
            rows.append(("License", card["license"]))
        if not ids and os.path.isdir(os.path.join(root, ".cache", "huggingface")):
            rows.append(("Downloaded", "from Hugging Face (the repo name wasn't recorded)"))
    else:
        try:
            _, meta = read_header(root)
        except Exception:  # noqa: BLE001
            meta = {}
        for key, label in (("modelspec.title", "Title"), ("modelspec.author", "Author")):
            if meta.get(key):
                rows.append((label, meta[key]))
        if str(meta.get("modelspec.implementation", "")).startswith("http"):
            url = meta["modelspec.implementation"]
            rows.append(("Source", ("link", url.replace("https://", ""), url)))
        if _hf_id(str(meta.get("base_model", "")).split(" ")[0]):
            ids.append(("Base model", str(meta["base_model"]).split(" ")[0]))
        card = model_card(os.path.dirname(root))  # a file downloaded with its repo's model card
        for base in (card.get("base_model") or "").split(","):
            if _hf_id(base) and base.strip() not in [i for _, i in ids]:
                ids.append(("Base model", base.strip()))
    for label, repo in ids:
        rows.insert(0, (label, ("link", repo, HF + repo)))
    if not rows:
        rows.append(("Source", "not recorded in the files"))
    return rows


TOKENIZER_FILES = ("tokenizer.json", "tokenizer_config.json", "vocab.json", "spiece.model", "tokenizer.model")
PROCESSOR_FILES = ("preprocessor_config.json", "processor_config.json", "feature_extractor_config.json")


def contents(path):
    """[(label, value)] facts about what is on disk - no interpretation of what the model is.

    - a diffusers pipeline folder: its model_index.json lists every part; each part is checked for presence
    - a model folder: config + weights (+ tokenizer / processor files)
    - an adapter: it names the base model it needs
    - a single weight file: it records nothing about what else it needs
    """
    root = model_root(path)
    if not os.path.isdir(root):
        return [("Contents", "a single weight file - it doesn't record what it needs to run")]
    for folder in (root, os.path.dirname(root)):
        index = _read_json(os.path.join(folder, "model_index.json"))
        if index:
            parts = [k for k, v in index.items() if not k.startswith("_") and isinstance(v, list) and v[0]]
            missing = [k for k in parts if not os.path.isdir(os.path.join(folder, k))]
            rows = [("Pipeline", f"{index.get('_class_name', '?')} (model_index.json)"),
                    ("Parts", ", ".join(parts))]
            if folder != root:
                rows.insert(0, ("Contents", f"one part ('{os.path.basename(root)}') of the pipeline in "
                                            f"{os.path.basename(folder)}"))
            rows.append(("Complete", "yes - all listed parts are here" if not missing
                         else "no - missing: " + ", ".join(missing)))
            return rows
    files = set(os.listdir(root))
    if "adapter_config.json" in files and "config.json" not in files:
        base = _read_json(os.path.join(root, "adapter_config.json")).get("base_model_name_or_path", "?")
        return [("Contents", f"an adapter - it needs its base model: {base}")]
    have = ["config"] if "config.json" in files else []
    have += ["weights"] if any(f.endswith(WEIGHT_EXT) for f in files) else []
    have += ["tokenizer"] if files & set(TOKENIZER_FILES) else []
    have += ["processor"] if files & set(PROCESSOR_FILES) else []
    return [("Contents", " + ".join(have) or "no model files")]


# ---- complete models (Hugging Face style folders) ---------------------------
TRANSLATION_HINTS = ("madlad", "nllb", "m2m", "marian", "opus-mt", "-mt", "_mt", "translat")


def _analyze_folder(info, folder):
    name = os.path.basename(folder.rstrip("\\/")).lower()
    adapter = os.path.join(folder, "adapter_config.json")
    if os.path.isfile(adapter) and not os.path.isfile(os.path.join(folder, "config.json")):
        cfg = _read_json(adapter)
        base = cfg.get("base_model_name_or_path", "?")
        info.role, info.family, info.family_label = LORA, base.lower(), f"for {base}"
        info.media = {"CAUSAL_LM": "text", "SEQ_2_SEQ_LM": "text"}.get(cfg.get("task_type"), info.media)
        _set_detail(info, "Type", ROLE_LABELS[LORA] + f" ({cfg.get('peft_type', 'adapter')})")
        if cfg.get("r"):
            _set_detail(info, "Rank", str(cfg["r"]))
        return
    cfg = _read_json(os.path.join(folder, "config.json"))
    arch = " ".join(cfg.get("architectures") or [cfg.get("architecture") or ""])
    mtype = str(cfg.get("model_type") or cfg.get("architecture") or "")
    a = (arch + " " + mtype).lower()
    if name.startswith("text_encoder") or os.path.isfile(os.path.join(os.path.dirname(folder), "model_index.json")):
        return  # a part of a diffusers pipeline: keep the weight-based detection (text encoder, VAE...)
    if any(k in a for k in ("whisper", "wav2vec2", "hubert", "forctc", "speechseq2seq", "conformer")):
        role, media = SPEECH_RECOGNITION, "audio → text"
    elif any(k in a for k in ("voxcpm", "tts", "vits", "bark", "speecht5", "parler")) or "audio_vae_config" in cfg:
        role, media = TEXT_TO_SPEECH, "text → audio"
    elif any(k in a for k in ("marian", "m2m_100", "nllb")) or ("t5" in a and any(h in name for h in TRANSLATION_HINTS)):
        role, media = TRANSLATION, "text → text"
    elif any(k in a for k in ("forcausallm", "forconditionalgeneration", "t5", "bart")):
        vision = "vision_config" in cfg or "visual" in cfg
        if vision and not any(os.path.isfile(os.path.join(folder, f))
                              for f in ("preprocessor_config.json", "processor_config.json")):
            vision = False  # no image processor saved with it: the engine runs it text-only
            _set_detail(info, "Images", "not available - no preprocessor_config.json in the folder")
        role, media = LANGUAGE_MODEL, "text + image → text" if vision else "text → text"
    else:
        return
    info.role, info.media = role, media
    info.family = mtype.lower()
    info.family_label = (cfg.get("architectures") or [mtype or "?"])[0]
    size = cfg.get("text_config", {}).get("hidden_size") or cfg.get("hidden_size") or cfg.get("d_model")
    _set_detail(info, "Type", ROLE_LABELS[role])
    _set_detail(info, "Model", info.family_label)
    _set_detail(info, "Media", media)
    if size:
        _set_detail(info, "Hidden size", str(size))


def _guess_from_name(info, root):
    """Last resort for models without a readable config: clues in the folder/file name."""
    p = root.lower().replace("\\", "/")
    for key, role, media in (("tts", TEXT_TO_SPEECH, "text → audio"), ("asr", SPEECH_RECOGNITION, "audio → text"),
                             ("whisper", SPEECH_RECOGNITION, "audio → text")):
        if key in p:
            info.role, info.media = role, media
            info.family_label = "guessed from the name"
            _set_detail(info, "Type", ROLE_LABELS[role] + " (guessed from the name)")
            return


def _set_detail(info, label, value):
    for i, (k, _) in enumerate(info.details):
        if k == label:
            info.details[i] = (label, value)
            return
    info.details.insert(len([d for d in info.details if d[0] in ("Type", "Model", "Title", "Media")]), (label, value))


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# ---- analysis --------------------------------------------------------------
def analyze(header, metadata) -> ModelInfo:
    keys = list(header)
    tops = Counter(k.split(".")[0] for k in keys)
    info = ModelInfo()

    if any(m in k for k in keys for m in LORA_MARKERS):
        _analyze_lora(info, header, keys)
    elif any(k.startswith("model.diffusion_model.") for k in keys):
        _analyze_checkpoint(info, header, keys)
    elif "encoder" in tops and "decoder" in tops and "layers" not in tops:
        _analyze_vae(info, header, keys, metadata)
    elif _analyze_text_encoder(info, header, keys, tops, metadata):
        pass
    elif "vision_model" in tops and "text_model" not in tops:
        info.role, info.media = IMAGE_ENCODER, "image"
        hidden = _shape(header, "vision_model.post_layernorm.weight")
        info.family, info.family_label = "clip-vision", "CLIP Vision" + (" (ViT-H)" if hidden and hidden[0] == 1280 else "")
    elif "detector" in tops and "tracker" in tops:
        info.role, info.family, info.family_label, info.media = OTHER, "sam3", "SAM 3 (segmentation + tracking)", "video"
    else:
        fam = _diffusion_family(keys, tops)
        if fam:
            info.role = DIFFUSION
            info.family, info.family_label, info.media = fam
        else:
            info.role = UNKNOWN

    _add_precision(info, header, keys, metadata)
    title = metadata.get("modelspec.title")
    rows = [("Type", ROLE_LABELS[info.role])]
    if info.family_label:
        rows.append(("Model", info.family_label))
    if title:
        rows.append(("Title", title))
    if info.media:
        rows.append(("Media", info.media))
    info.details = rows + info.details
    return info


def _shape(header, key):
    t = header.get(key)
    return t["shape"] if t else None


def _diffusion_family(keys, tops):
    """Return (tag, label, media) for diffusion-model weights, or None.
    Works on full models and on LoRA keys (which only cover the blocks)."""
    has = lambda s: any(s in k for k in keys)  # noqa: E731
    if "double_blocks" in tops or "single_blocks" in tops or has("double_blocks.") :
        return "flux", "Flux", "image"
    if "txt_norm" in tops or has("txt_mlp") or has("img_mod."):
        return "qwen-image", "Qwen-Image (MMDiT)", "image"
    if "noise_refiner" in tops or "context_refiner" in tops or has("noise_refiner.") or has("context_refiner."):
        return "z-image", "Z-Image (Lumina-2 style DiT)", "image"
    if "token_refiner" in tops or "video_patch_proj" in tops or has("token_refiner."):
        return "minimax-h3", "MiniMax H3 (audio + video)", "video"
    if "vid_in" in tops and "vid_out" in tops:
        return "seedvr2", "SeedVR2 (video restoration)", "video"
    if ("patch_embedding" in tops and "text_embedding" in tops) or (has("cross_attn.") and has("ffn.")):
        label = "Wan (image-to-video)" if "img_emb" in tops or has("k_img") else "Wan (video)"
        return "wan", label, "video"
    if "joint_blocks" in tops or has("joint_blocks."):
        return "sd3", "Stable Diffusion 3", "image"
    if "input_blocks" in tops or has("input_blocks.") or has("lora_unet_input_blocks"):
        return ("sdxl", "SDXL UNet", "image") if "label_emb" in tops else ("sd1", "Stable Diffusion 1.x UNet", "image")
    if "adaln_single" in tops:
        return "ltx", "LTX-Video", "video"
    return None


def _analyze_lora(info, header, keys):
    info.role = LORA
    base = []
    for k in keys:
        for p in LORA_PREFIXES:
            if k.startswith(p):
                k = k[len(p):]
                break
        base.append(re.split(r"\.lora_|\.alpha$|\.diff", k)[0])
    fam = _diffusion_family(base, Counter(b.split(".")[0] for b in base))
    if fam:
        info.family, label, info.media = fam
        info.family_label = f"for {label}"
    rank = next((header[k]["shape"][0] for k in keys
                 if (".lora_down." in k or ".lora_A." in k) and len(header[k]["shape"]) >= 2), None)
    if rank:
        info.details.append(("Rank", str(rank)))


def _analyze_checkpoint(info, header, keys):
    stripped = [k[len("model.diffusion_model."):] for k in keys if k.startswith("model.diffusion_model.")]
    fam = _diffusion_family(stripped, Counter(k.split(".")[0] for k in stripped))
    parts = ["diffusion"]
    if any(k.startswith("first_stage_model.") for k in keys):
        parts.append("VAE")
    if any(k.startswith(("conditioner.", "cond_stage_model.", "text_encoders.")) for k in keys):
        parts.append("text encoder")
    info.role = CHECKPOINT if len(parts) > 1 else DIFFUSION
    if fam:
        info.family, info.family_label, info.media = fam
    info.details.append(("Contains", ", ".join(parts)))


def _analyze_vae(info, header, keys, metadata):
    info.role, info.media = VAE, "image"
    meta_keys = " ".join(metadata)
    if "audio_vae" in meta_keys:
        info.media = "audio"
    elif "video_vae" in meta_keys:
        info.media = "video"
    if "minimax_h3" in meta_keys:
        info.family, info.family_label = "minimax-h3", f"MiniMax H3 {info.media} VAE"
        return
    if "conv1.weight" in header and any(k.startswith("decoder.upsamples") for k in keys):
        z = header["conv1.weight"]["shape"][0] // 2
        info.media = "image + video"
        if z == 48:
            info.family, info.family_label = "wan2.2-vae", "Wan 2.2 VAE"
        else:
            info.family, info.family_label = "wan2.1-vae", "Wan 2.1 / Qwen-Image VAE"
        info.details.append(("Latent channels", str(z)))
        return
    conv_out = _shape(header, "encoder.conv_out.weight")
    z = conv_out[0] // 2 if conv_out else None
    if z:
        info.details.append(("Latent channels", str(z)))
    if conv_out and len(conv_out) == 5:  # 3D convolutions -> video VAE
        info.media = "image + video"
        info.family, info.family_label = "video-vae", f"Video VAE ({z} ch)"
        return
    arch = metadata.get("modelspec.architecture", "")
    if "flux" in arch.lower() or z == 16:
        info.family, info.family_label = "flux-vae", "Flux-style VAE (16 ch; Flux, Z-Image, SD3)"
    elif z == 4:
        info.family, info.family_label = "sd-vae", "SD / SDXL VAE (4 ch)"
    else:
        info.family_label = "VAE"


def _analyze_text_encoder(info, header, keys, tops, metadata=None):
    has = lambda s: any(s in k for k in keys)  # noqa: E731
    arch = str((metadata or {}).get("general.architecture", ""))
    if arch.endswith("encoder"):  # a GGUF file names its architecture; llama.cpp's text encoders end so (t5encoder)
        base = str(metadata.get("general.basename") or arch).lower()
        size = str(metadata.get("general.size_label") or "").lower()
        info.role, info.media = TEXT_ENCODER, "text"
        info.family = f"{base}-{size}" if size else base
        info.family_label = f"{metadata.get('general.name') or base} ({arch}, as the file's metadata says)"
        return True
    if "model" in tops and has("model.layers.") and has("model.embed_tokens"):
        qwen3 = has(".q_norm.")
        vision = "visual" in tops
        name = "Qwen3" if qwen3 else "Qwen2.5"
        if vision:
            name += "-VL"
        layers = len({k.split(".")[2] for k in keys if k.startswith("model.layers.")})
        hidden = _shape(header, "model.norm.weight")
        info.role, info.family = TEXT_ENCODER, name.lower()
        info.family_label = f"{name} language model" + (" (reads images too)" if vision else "")
        info.media = "text + image" if vision else "text"
        info.details.append(("Layers", f"{layers}" + (f", hidden size {hidden[0]}" if hidden else "")))
        return True
    if "encoder" in tops and "shared" in tops:
        vocab = _shape(header, "shared.weight")
        umt5 = vocab and vocab[0] > 200000
        info.role, info.media = TEXT_ENCODER, "text"
        info.family, info.family_label = ("umt5-xxl", "UMT5-XXL (multilingual T5)") if umt5 else ("t5", "T5")
        return True
    if "text_model" in tops:
        hidden = _shape(header, "text_model.final_layer_norm.weight")
        info.role, info.media = TEXT_ENCODER, "text"
        big = hidden and hidden[0] >= 1280
        info.family, info.family_label = ("clip-g", "CLIP-G") if big else ("clip-l", "CLIP-L")
        return True
    return False


def _weight_precision(header, keys):
    """Describe the precision of the real weight matrices, e.g. 'fp8 e4m3 + bf16'."""
    fp4 = {k[: -len(".weight_scale_2")] for k in keys if k.endswith(".weight_scale_2")}
    counts = Counter()
    for k, t in header.items():
        if k.endswith(AUX_SUFFIXES) or not k.endswith(".weight") or len(t["shape"]) < 2:
            continue
        name = "fp4 (nvfp4)" if k[: -len(".weight")] in fp4 else DTYPE_NAMES.get(t["dtype"], t["dtype"].lower())
        counts[name] += 1
    total = sum(counts.values()) or 1
    kinds = [n for n, c in counts.most_common() if c / total >= 0.1]
    return " + ".join(kinds[:3]) or "?"


def _add_precision(info, header, keys, metadata):
    comfy = "_quantization_metadata" in metadata or any(k.endswith(".comfy_quant") for k in keys)
    scaled = "scaled_fp8" in header
    if comfy or scaled:
        prec = _weight_precision(header, keys)
        info.quantized = "ComfyUI " + ("scaled fp8" if scaled else "mixed quantization")
        info.details.append(("Precision", f"{prec} — {info.quantized}"))
        info.details.append(("Format", "ComfyUI-specific (needs a custom loader)"))
    else:
        info.details.append(("Precision", _weight_precision(header, keys)))
        info.details.append(("Format", "standard safetensors"))
        params = sum(prod(t["shape"]) for t in header.values())
        info.details.append(("Parameters", _fmt_count(params)))
    info.details.append(("Tensors", str(len(keys))))


def _fmt_count(n):
    if n >= 1e9:
        return f"{n / 1e9:.2f} B"
    if n >= 1e6:
        return f"{n / 1e6:.0f} M"
    return str(n)
