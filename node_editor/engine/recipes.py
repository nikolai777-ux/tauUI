"""Engine side of the recipes: turn a job (from a Modality node) into an output file.

Runs inside the worker process. Keeps the last pipeline loaded so re-rendering with the
same models (e.g. a new seed or prompt) doesn't reload gigabytes of weights.
"""

import gc
import inspect
import os
import time

import torch
from PIL import Image
from PIL.PngImagePlugin import PngInfo

import loaders


class Cancelled(Exception):
    pass


_pipe = None
_pipe_key = None
_lora_key = ()


def _free():
    global _pipe, _pipe_key, _lora_key
    _pipe, _pipe_key, _lora_key = None, None, ()
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def _place(pipe, sizes_gb):
    """Everything on the GPU if it fits, otherwise move each part to the GPU only while it runs."""
    vram = torch.cuda.get_device_properties(0).total_memory / 1024**3
    shared = bool(os.environ.get("TAUUI_GPU_LOCK"))  # other apps use this GPU between our jobs: keep it free
    if sum(sizes_gb) + 6 < vram and not shared:
        pipe.to("cuda")
        return "all on GPU"
    pipe.enable_model_cpu_offload()
    pipe._offloaded = True
    return "parts take turns on the GPU"


def _memory_note():
    """After a render: a note when its peak went past the GPU's memory. Windows doesn't fail then - the rest runs
    from system RAM and the render crawls (measured: Qwen-Image-2.1 with four reference pictures, 1280x704
    1.45 s/step at a 25 GB peak; 2560x1440 33 s/step at 45 GB on the 32 GB card)."""
    if not torch.cuda.is_available():
        return ""
    vram = torch.cuda.get_device_properties(0).total_memory / 1024**3
    peak = torch.cuda.max_memory_allocated() / 1024**3
    if peak <= vram:
        return ""
    return (f"used {peak:.0f} GB at the peak on a {vram:.0f} GB GPU - the rest ran from system RAM, much slower "
            f"(a smaller size or fewer pictures in fits)")


def _picture_input(name, _param=None):
    """A pipeline input that takes pictures, by the library's own parameter name: image, last_image, mask_image,
    ref_image... - not prompt_embeds_mask (text embeddings) and not video inputs (routed as videos). Type hints
    can't decide: Wan's last_image is declared a tensor yet takes pictures; video inputs accept lists of images."""
    return name.endswith(("image", "images", "mask")) and "embeds" not in name and "video" not in name


def _part_providers(root, models):
    """The pipeline's model_index.json may leave parts empty ([null, null]) to be supplied separately (e.g.
    JoyAI-Video-Edit's text encoder / processor, which live in MiMo-VL's repo). Connected model FOLDERS (not single
    weight files, which stay LoRAs) then provide them. Returns (providers, the rest)."""
    import json
    try:
        with open(os.path.join(root, "model_index.json"), encoding="utf-8") as f:
            index = json.load(f)
    except (OSError, ValueError):
        return [], list(models)
    empty = [k for k, v in index.items() if not k.startswith("_") and isinstance(v, list) and v[:1] == [None]]
    if not empty:
        return [], list(models)
    providers = [m for m in models if os.path.isdir(m["path"]) and any(
        os.path.isfile(os.path.join(m["path"], f)) for f in ("config.json", "preprocessor_config.json"))]
    return providers, [m for m in models if m not in providers]


def _custom_code(root):
    """The module holding the pipeline's own code, when its model_index.json names a pipeline class diffusers
    doesn't have and parts from a module that sits as a .py file in the model's folder; else None."""
    import json
    import diffusers
    try:
        with open(os.path.join(root, "model_index.json"), encoding="utf-8") as f:
            index = json.load(f)
    except (OSError, ValueError):
        return None
    if hasattr(diffusers, index.get("_class_name", "")):
        return None
    for k, v in index.items():
        if not k.startswith("_") and isinstance(v, list) and v and isinstance(v[0], str) \
                and os.path.isfile(os.path.join(root, v[0] + ".py")):
            return v[0]
    return None


def _load_empty_parts(root, providers, status):
    """Each empty part, loaded from a provider folder with the class the pipeline's own __init__ declares for it
    (Optional[X] -> X): {part: object}. A part no provider can load stays empty (the pipeline says if it needs it)."""
    import inspect
    import json
    import typing
    import diffusers
    if not providers:
        return {}
    with open(os.path.join(root, "model_index.json"), encoding="utf-8") as f:
        index = json.load(f)
    cls = getattr(diffusers, index.get("_class_name", ""), None)
    if cls is None:
        return {}
    params = inspect.signature(cls.__init__).parameters
    empty = [k for k, v in index.items() if not k.startswith("_") and isinstance(v, list) and v[:1] == [None]]
    parts, report = {}, []
    for name in empty:
        ann = params[name].annotation if name in params else None
        classes = [c for c in (typing.get_args(ann) or (ann,)) if isinstance(c, type) and hasattr(c, "from_pretrained")]
        for m in providers:
            for c in classes:
                try:
                    kw = {"torch_dtype": torch.bfloat16} if issubclass(c, torch.nn.Module) else {}
                    parts[name] = c.from_pretrained(m["path"], **kw)
                except Exception:  # noqa: BLE001 - this folder doesn't hold that part; try the next
                    continue
                report.append(f"{name} ← {m.get('name') or os.path.basename(m['path'])}")
                break
            if name in parts:
                break
    if report:
        status("Parts from other models: " + ", ".join(report))
    return parts


_flex_compiled = False


def _compile_flex_attention():
    """FlexAttention is only usable compiled: eagerly it builds the whole score matrix (a 2 s Wan2.2-Animate clip
    asked for 536 GB). PyTorch's docs say to use torch.compile(flex_attention); diffusers looks the function up on
    the torch module at every call, so replacing it there once covers every pipeline that uses it. Needs Triton in
    the env (triton-windows); without it nothing changes and such a pipeline reports its own error."""
    global _flex_compiled
    if _flex_compiled:
        return
    _flex_compiled = True
    try:
        import triton  # noqa: F401 - the compiler FlexAttention's kernels are made with
        import torch.nn.attention.flex_attention as flex
    except ImportError:
        return
    flex.flex_attention = torch.compile(flex.flex_attention, dynamic=False)


def _size_gb(path):
    if os.path.isdir(path):
        return sum(os.path.getsize(os.path.join(path, f)) for f in os.listdir(path) if f.endswith(".safetensors")) / 1024**3
    return os.path.getsize(path) / 1024**3


def _build(job, status):
    fam, mode = job["family"], job["mode"]
    status("Loading diffusion model…")
    transformer = loaders.load_transformer(fam, job["diffusion"])
    status("Loading VAE…")
    vae = loaders.load_vae(job["vae_family"], job["vae"])
    status("Loading text encoder…")
    if fam == "z-image":
        from diffusers import ZImagePipeline
        te, tok = loaders.load_qwen3_text_encoder(job["text_encoder"])
        pipe = ZImagePipeline(scheduler=loaders.z_image_scheduler(), vae=vae, text_encoder=te,
                              tokenizer=tok, transformer=transformer)
    elif fam == "qwen-image":
        from diffusers import QwenImageEditPipeline, QwenImagePipeline
        te, tok, proc = loaders.load_qwen25vl_text_encoder(job["text_encoder"])
        if mode == "edit":
            pipe = QwenImageEditPipeline(scheduler=loaders.qwen_image_scheduler(), vae=vae, text_encoder=te,
                                         tokenizer=tok, processor=proc, transformer=transformer)
        else:
            pipe = QwenImagePipeline(scheduler=loaders.qwen_image_scheduler(), vae=vae, text_encoder=te,
                                     tokenizer=tok, transformer=transformer)
    else:
        raise ValueError(f"No recipe for '{fam}'")
    pipe.set_progress_bar_config(disable=True)
    placement = _place(pipe, [_size_gb(job[k]) for k in ("diffusion", "text_encoder", "vae")])
    status(f"Models ready ({placement})")
    return pipe


def _apply_loras(pipe, loras, status):
    global _lora_key
    key = tuple((l["path"], l["strength"]) for l in loras)
    if key == _lora_key:
        return
    if _lora_key:
        pipe.unload_lora_weights()
    names, weights = [], []
    for i, l in enumerate(loras):
        status(f"Loading LoRA {os.path.basename(l['path'])}…")
        name = f"lora{i}"
        pipe.load_lora_weights(l["path"], adapter_name=name)
        names.append(name)
        weights.append(l["strength"])
    if names:
        pipe.set_adapters(names, weights)
    _lora_key = key


def _single_file_job(job):
    """Turn 'these models are connected' into a single-file image job, or explain what is missing.

    This is the loader path for bare weight files (ComfyUI-style), where nothing in the files says what
    belongs together. It will be replaced by loading Hugging Face repos, which declare their own parts.
    """
    models = job["models"]
    by_role = {}
    for m in models:
        by_role.setdefault(m["role"], []).append(m)
    diffusion = by_role.get("diffusion", [])
    if not diffusion:
        roles = ", ".join(sorted({m["role"] for m in models}))
        raise ValueError(f"The engine can only run image diffusion models so far (connected: {roles})")
    missing = [what for role, what in (("text_encoder", "a text encoder"), ("vae", "a VAE")) if not by_role.get(role)]
    if missing:
        raise ValueError(f"{diffusion[0]['name']} is a bare weight file; merge in " + " and ".join(missing))
    d, te, vae = diffusion[0], by_role["text_encoder"][0], by_role["vae"][0]
    p = d["params"]
    return {
        "family": d["family"], "mode": "edit" if job["images"] else "t2i",
        "diffusion": d["path"], "text_encoder": te["path"], "vae": vae["path"], "vae_family": vae["family"],
        "loras": [{"path": m["path"], "strength": m["params"].get("strength", 1.0)} for m in by_role.get("lora", [])],
        "prompt": job["prompt"], "negative": "", "images": job["images"],
        "width": p["width"], "height": p["height"], "steps": p["steps"], "cfg": p["cfg"], "seed": p["seed"],
        "output_dir": job["output_dir"], "name": job["name"],
    }


def _pipeline_root(path):
    """The diffusers pipeline folder (the one with model_index.json) a model path belongs to, or None.
    Accepts the pipeline folder itself or one of its parts (e.g. <repo>/transformer)."""
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    for d in (folder, os.path.dirname(folder)):
        if os.path.isfile(os.path.join(d, "model_index.json")):
            return os.path.normpath(d)
    return None


def _repo_id(folder):
    """'org/name' for a folder in a Hugging Face cache (...\\models--org--name\\snapshots\\<rev>), else None."""
    parts = os.path.normpath(folder).split(os.sep)
    if len(parts) >= 3 and parts[-2] == "snapshots" and parts[-3].startswith("models--"):
        return parts[-3][len("models--"):].replace("--", "/", 1)
    return None


def _card_base_models(folder):
    """base_model entries from a model card's front matter (README.md), e.g. ['Qwen/Qwen-Image-Edit-2511']."""
    try:
        with open(os.path.join(folder, "README.md"), encoding="utf-8", errors="replace") as f:
            text = f.read(20000)
    except OSError:
        return []
    front = text.split("---", 2)[1] if text.startswith("---") else ""
    import re
    one = re.search(r"^base_model:[ \t]*(\S.*)$", front, re.M)
    many = re.search(r"^base_model:[ \t]*\n((?:[ \t]*-[ \t]*.*\n?)+)", front, re.M)
    if one:
        return [one.group(1).strip().strip("'\"")]
    if many:
        return [l.strip(" -'\"\t") for l in many.group(1).strip().splitlines()]
    return []


def _lora_notes(loras, root):
    """Warnings when a LoRA's model card says it was trained on a different base than this pipeline."""
    pipeline_id = _repo_id(root)
    notes = []
    for l in loras:
        folder = l["path"] if os.path.isdir(l["path"]) else os.path.dirname(l["path"])
        bases = _card_base_models(folder)
        if bases and pipeline_id and pipeline_id not in bases:
            notes.append(f"{l['name']} was trained on {', '.join(bases)} (this pipeline: {pipeline_id})")
    return notes


def _run_pipeline(root, job, emit, cancel_event, loras=()):
    """Load a diffusers pipeline the way its files declare it (model_index.json: parts and their classes;
    scheduler_config.json: the scheduler) and render. Only settings its __call__ accepts are passed.
    loras: other connected weight files, loaded into the pipeline with diffusers' own LoRA loader."""
    global _pipe, _pipe_key
    import runners
    status = lambda msg: emit(event="status", message=msg)  # noqa: E731
    t0 = time.time()
    providers, loras = _part_providers(root, loras)  # model folders filling parts the pipeline leaves empty
    key = ("pipeline", os.path.normcase(root), tuple(sorted(os.path.normcase(m["path"]) for m in providers)))
    if key != _pipe_key:
        _free()
        runners.free()  # make room on the GPU
        if _is_modular(root):
            status("Loading the modular pipeline its modular_model_index.json declares…")
            pipe = _load_modular(root)
            status(f"{type(pipe).__name__} ready (parts move to the GPU as they run)")
        else:
            from diffusers import DiffusionPipeline
            status("Loading the pipeline its model_index.json declares…")
            parts = _load_empty_parts(root, providers, status)
            custom = _custom_code(root)
            if custom:
                status(f"Using the model's own pipeline code ({custom}.py in its folder)")
                parts.update(custom_pipeline=custom, trust_remote_code=True)
            pipe = DiffusionPipeline.from_pretrained(root, torch_dtype=torch.bfloat16, **parts)
            pipe.set_progress_bar_config(disable=True)
            sizes = [sum(p.numel() * p.element_size() for p in c.parameters()) / 1024**3
                     for c in pipe.components.values() if isinstance(c, torch.nn.Module)]
            placement = _place(pipe, sizes)
            status(f"{type(pipe).__name__} ready ({placement})")
        _pipe, _pipe_key = pipe, key
    pipe = _pipe
    modular = _is_modular(root)
    _compile_flex_attention()
    if loras or _lora_key:
        try:
            _apply_loras(pipe, [{"path": l["path"], "strength": float(l["params"].get("strength", 1.0))}
                                for l in loras], status)
        except Exception as e:  # noqa: BLE001 - the loader says why it doesn't fit
            _free()  # a half-applied LoRA would leave the pipeline in an unknown state
            names = ", ".join(l["name"] for l in loras)
            raise ValueError(f"{names}: this pipeline's LoRA loader refused it ({type(e).__name__}: {e})") from e
    notes = _lora_notes(loras, root)

    # The settings shown on the pipeline's Model node (its diffusion part's, if one is marked so).
    own = [m for m in job["models"] if m not in loras]
    s = next((m["params"] for m in own if m["role"] in ("diffusion", "checkpoint")), own[0]["params"])
    steps = s["steps"]

    def on_step(p, i, t, kwargs):
        if cancel_event.is_set():
            raise Cancelled()
        if torch.cuda.is_available():
            torch.cuda.synchronize()
        emit(event="progress", step=i + 1, total=steps)
        return kwargs

    offered = {"prompt": job["prompt"], "width": s["width"], "height": s["height"], "num_inference_steps": steps,
               "guidance_scale": s["cfg"], "generator": torch.Generator("cpu").manual_seed(s["seed"]),
               "callback_on_step_end": on_step}
    # what the pipeline declares it takes (modular pipelines list their inputs; others: the __call__ signature)
    accepted = ({i.name: i for i in pipe.blocks.inputs if i.name} if modular
                else inspect.signature(pipe.__call__).parameters)
    # pictures (engine/pictures.py): all go to `image` - several as a list, which the pipeline itself interprets
    # (Qwen-Image-2.1 and FireRed / Edit-Plus: reference pictures); a node named after another picture input the
    # pipeline declares (last_image, mask_image, control_image...) goes to that one
    import pictures
    named = (job.get("named") or {}).get("image") or [[f"image{i + 1}", p] for i, p in enumerate(job["images"])]
    declared = {n: n for n, p in accepted.items() if _picture_input(n, p)}
    # the main picture input: `image`; a pipeline without one but with a single picture input of its own name
    # (e.g. ref_image) takes them there
    main = "image" if "image" in accepted else (next(iter(declared)) if len(declared) == 1 else None)
    routed, routing, unused = pictures.route(named, declared, main)
    for param, paths in routed.items():
        pics = [Image.open(p).convert("RGB") for p in paths]
        offered[param] = pics[0] if len(pics) == 1 else pics
    if routing:
        status(routing)
        if len(named) > 1 or set(routed) != {"image"}:
            notes.append(routing)  # stays in the Modality's status after the render
    if unused:
        notes.append(f"{type(pipe).__name__} takes no pictures - not used: {', '.join(unused)}")
    if job["videos"]:
        video_input = _video_input_name(accepted)
        if not video_input:
            raise ValueError(f"{type(pipe).__name__} doesn't declare a video input")
        status(f"Reading {os.path.basename(job['videos'][0])}…")
        frames, fps = _read_video(job["videos"][0])
        in_fps = fps
        offered[video_input] = frames
        offered[f"{video_input}_fps"] = fps  # e.g. driving_video_fps, when the pipeline declares it
    if "true_cfg_scale" in accepted:
        # Qwen-Image family: guidance_scale is only for guidance-distilled transformers; the real strength
        # is true_cfg_scale, which the pipeline applies only when a negative prompt is given (blank = " ").
        # Guidance <= 1 keeps the pipeline's own default (e.g. Qwen-Image-2.1's examples use no CFG).
        cfg = offered.pop("guidance_scale")
        if cfg > 1:
            offered["true_cfg_scale"] = cfg
            offered["negative_prompt"] = (s.get("negative") or "").strip() or " "
    elif (s.get("negative") or "").strip():
        offered["negative_prompt"] = s["negative"].strip()
    args = {k: v for k, v in offered.items() if k in accepted}
    ignored = sorted(set(offered) - set(args))
    # fps and the progress callback are only passed where declared; not worth reporting
    ignored = sorted(k for k in ignored if not k.endswith("_fps") and k != "callback_on_step_end")
    status("Rendering…" + (f" ({type(pipe).__name__} doesn't take: {', '.join(ignored)})" if ignored else ""))
    t_render = time.time()
    info = {"pipeline": root, "prompt": job["prompt"], "seed": s["seed"], "steps": steps,
            "guidance": s["cfg"], "width": s["width"], "height": s["height"],
            "loras": "; ".join(f"{l['path']} @ {l['params'].get('strength', 1.0)}" for l in loras)}
    if modular:
        wanted = [o.name for o in pipe.blocks.intermediate_outputs]
        output = "videos" if "videos" in wanted else "images"
        result = pipe(output=output, **args)
        frames = result if output == "videos" else None
        image = None if frames is not None else (result[0] if isinstance(result, list) else result)
    else:
        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
        try:
            result = pipe(**args)
        except (torch.OutOfMemoryError, RuntimeError) as e:
            # (on a MIG slice PyTorch's allocator fails an NVML assert instead of raising OutOfMemoryError)
            if not isinstance(e, torch.OutOfMemoryError) and "CUDACachingAllocator" not in str(e):
                raise
            # all parts fitted, the render's working memory didn't (e.g. several reference pictures on a 48 GB
            # card): parts take turns on the GPU from now on - this pipeline stays that way while it is loaded
            if getattr(pipe, "_offloaded", False):
                raise
            status("Out of GPU memory - again with the parts taking turns on the GPU…")
            pipe.to("cpu")
            gc.collect()
            torch.cuda.empty_cache()
            pipe.enable_model_cpu_offload()
            pipe._offloaded = True
            args["generator"] = torch.Generator("cpu").manual_seed(s["seed"])
            torch.cuda.reset_peak_memory_stats()
            result = pipe(**args)
            notes.append("Ran with the parts taking turns on the GPU (all on it ran out of memory)")
        memory = _memory_note()
        if memory:
            notes.append(memory)
        made = getattr(result.images[0], "size", None) if hasattr(result, "images") and result.images else None
        if made and tuple(made) != (s["width"], s["height"]):  # e.g. the pipeline rounds to its own multiple
            notes.append(f"asked {s['width']}x{s['height']}, {type(pipe).__name__} made {made[0]}x{made[1]}")
        frames = result.frames[0] if hasattr(result, "frames") else None
        image = None if frames is not None else result.images[0]
    declared_fps = getattr(accepted.get("fps"), "default", None) if "fps" in accepted else None
    # a video-to-video pipeline that declares no frame rate keeps the source's (its frames match the input's)
    out_fps = (args.get("fps") or (declared_fps if isinstance(declared_fps, (int, float)) else None)
               or (in_fps if job["videos"] else None) or 24)
    base = {"seconds": None, "render_seconds": round(time.time() - t_render, 1), "note": "; ".join(notes),
            "peak_gb": round(torch.cuda.max_memory_allocated() / 1024**3, 1) if torch.cuda.is_available() else None,
            "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else ""}
    if frames is not None:
        path = _write_video(frames, out_fps, job, s["seed"])
        return {**base, "kind": "video", "path": path, "seconds": round(time.time() - t0, 1)}
    path = _save_png(image, job, s["seed"], info)
    return {**base, "kind": "image", "path": path, "seconds": round(time.time() - t0, 1)}


# ---- modular pipelines and video -------------------------------------------------
def _is_modular(root):
    """A repo whose model_index.json names a pipeline this diffusers doesn't have as a class, but which ships a
    modular_model_index.json (diffusers' modular pipelines, e.g. Wan-Animate-2)."""
    import json
    import diffusers
    if not os.path.isfile(os.path.join(root, "modular_model_index.json")):
        return False
    try:
        with open(os.path.join(root, "model_index.json"), encoding="utf-8") as f:
            cls = json.load(f).get("_class_name", "")
    except (OSError, ValueError):
        cls = ""
    return not hasattr(diffusers, cls)


def _load_modular(root):
    """Modular pipeline from its folder. A component whose folder exists in this repo is loaded from here
    (its spec may point at a Hub branch - e.g. a merged PR - that an offline cache can't resolve)."""
    from diffusers import ComponentsManager, ModularPipeline
    manager = ComponentsManager()
    manager.enable_auto_cpu_offload(device="cuda")  # parts move to the GPU only while they run
    pipe = ModularPipeline.from_pretrained(root, components_manager=manager)
    for name, spec in pipe._component_specs.items():
        folder = getattr(spec, "subfolder", None) or name
        if getattr(spec, "pretrained_model_name_or_path", None) and os.path.isdir(os.path.join(root, folder)):
            spec.pretrained_model_name_or_path = root
            spec.revision = None
    pipe.load_components(torch_dtype=torch.bfloat16)
    return pipe


def _video_input_name(accepted):
    """The input a pipeline declares for a video: a name containing 'video' (e.g. video, driving_video)."""
    names = [n for n in accepted if "video" in n and not n.endswith(("_fps", "s")) and "latent" not in n]
    return names[0] if names else None


def _read_video(path):
    """(frames as RGB PIL images, fps) - OpenCV reads it (its FFmpeg build decodes common formats)."""
    import cv2
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise ValueError(f"Can't read the video {path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    frames = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
    cap.release()
    if not frames:
        raise ValueError(f"No frames in {path}")
    return frames, float(fps)


def _to_uint8_frames(frames):
    """Pipeline output (numpy [F,H,W,C] 0..1, a batch of those, torch tensors, or PIL images) -> uint8 arrays."""
    import numpy as np
    if isinstance(frames, (list, tuple)) and frames and isinstance(frames[0], Image.Image):
        return [np.asarray(f.convert("RGB")) for f in frames]
    if torch.is_tensor(frames):
        frames = frames.float().cpu().numpy()
    if isinstance(frames, (list, tuple)):
        frames = frames[0] if len(frames) and np.ndim(frames[0]) == 4 else np.stack(frames)
    frames = np.asarray(frames)
    if frames.ndim == 5:  # batch
        frames = frames[0]
    if frames.shape[-1] not in (1, 3, 4) and frames.shape[1] in (1, 3, 4):  # [F,C,H,W]
        frames = frames.transpose(0, 2, 3, 1)
    if frames.dtype != np.uint8:
        frames = (np.clip(frames, 0, 1) * 255).round().astype(np.uint8)
    return list(frames[..., :3])


def _write_video(frames, fps, job, seed):
    """H.264 MP4 via ffmpeg (widely playable); OpenCV's mp4v writer if ffmpeg isn't on PATH."""
    import shutil
    import subprocess
    frames = _to_uint8_frames(frames)
    os.makedirs(job["output_dir"], exist_ok=True)
    path = os.path.join(job["output_dir"], f"{job['name']}_{time.strftime('%Y%m%d_%H%M%S')}_s{seed}.mp4")
    h, w = frames[0].shape[:2]
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
               "-r", str(fps), "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "16",
               "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", path]
        proc = subprocess.run(cmd, input=b"".join(f.tobytes() for f in frames), capture_output=True)
        if proc.returncode == 0:
            return path
    import cv2
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), float(fps), (w, h))
    for f in frames:
        writer.write(cv2.cvtColor(f, cv2.COLOR_RGB2BGR))
    writer.release()
    return path


def _save_png(image, job, seed, info):
    os.makedirs(job["output_dir"], exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(job["output_dir"], f"{job['name']}_{stamp}_s{seed}.png")
    meta = PngInfo()
    for k, v in info.items():
        meta.add_text(k, str(v))
    image.save(path, pnginfo=meta)
    return path


def run(job, emit, cancel_event):
    """Render one job. Emits status/progress messages; returns the output file path."""
    global _pipe, _pipe_key
    status = lambda msg: emit(event="status", message=msg)  # noqa: E731
    t0 = time.time()
    if "chat" in job:  # a language model as an Executor's brain: one turn of the conversation, with tools
        import runners
        return runners.run_chat(job, emit, free_other=_free)
    if "models" in job:
        import runners
        if any(m["path"].lower().endswith(".gguf") for m in job["models"]):  # stable-diffusion.cpp's format
            import sdcpp
            return sdcpp.run(job, emit, cancel_event, free_other=lambda: (_free(), runners.free()))
        kind = runners.main_kind(job["models"])
        if kind is not None:  # a speech / text model the files declare how to run (+ PEFT adapters on top)
            return runners.run(job, emit, free_other=_free)
        roots = {_pipeline_root(m["path"]) for m in job["models"]} - {None}
        if len(roots) == 1:  # parts of one diffusers repo (or the repo itself), plus any LoRA files
            root = roots.pop()
            loras = [m for m in job["models"] if _pipeline_root(m["path"]) is None]
            return _run_pipeline(root, job, emit, cancel_event, loras)
        job = _single_file_job(job)
        runners.free()  # make room on the GPU for the image models

    key = (job["family"], job["mode"], job["diffusion"], job["text_encoder"], job["vae"])
    if key != _pipe_key:
        _free()
        _pipe = _build(job, status)
        _pipe_key = key
    pipe = _pipe
    _apply_loras(pipe, job.get("loras", []), status)

    steps = job["steps"]

    def on_step(p, i, t, kwargs):
        if cancel_event.is_set():
            raise Cancelled()
        # GPU work is queued asynchronously; wait for this step to really finish so progress is honest.
        if torch.cuda.is_available():
            torch.cuda.synchronize()
        emit(event="progress", step=i + 1, total=steps)
        return kwargs

    args = dict(prompt=job["prompt"], num_inference_steps=steps, callback_on_step_end=on_step,
                generator=torch.Generator("cpu").manual_seed(job["seed"]))
    negative = job.get("negative") or ""
    if job["family"] == "z-image":
        args.update(height=job["height"], width=job["width"], guidance_scale=job["cfg"])
        if negative:
            args["negative_prompt"] = negative
    else:  # qwen-image: "true" CFG runs only when there is a negative prompt
        args.update(true_cfg_scale=job["cfg"], negative_prompt=negative or " ")
        if job["mode"] == "edit":
            args["image"] = Image.open(job["images"][0]).convert("RGB")
        else:
            args.update(height=job["height"], width=job["width"])

    status("Rendering…")
    t_render = time.time()
    image = pipe(**args).images[0]

    path = _save_png(image, job, job["seed"], {k: job.get(k, "") for k in (
        "prompt", "negative", "seed", "steps", "cfg", "diffusion", "text_encoder", "vae")})
    return {"kind": "image", "path": path, "seconds": round(time.time() - t0, 1),
            "render_seconds": round(time.time() - t_render, 1)}
