# Architecture – how the code is organised

tauUI is about 23,000 lines of Python in two processes:

```
 ┌────────────────────────── the app (PySide6 / Qt) ──────────────────────────┐
 │  main.py ── MainWindow: Library | graph tabs + Console | Parameters        │
 │     node graph (view.py, node.py, nodes.py, modality.py …)                 │
 │     engine/client.py ── one worker per Python environment ─┐               │
 │     services.py / svc_*.py ── online services (HTTP)       │               │
 │     executor.py ── the Executor's brain (claude / Gemini / local)          │
 └────────────────────────────────────────────────────────────│───────────────┘
                                   JSON lines over stdin/stdout│
 ┌──────────────────── worker (the user's model environment) ──▼──────────────┐
 │  engine/worker.py → recipes.py (diffusers) · runners.py (transformers…)    │
 │                     sdcpp.py (stable-diffusion.cpp) · shots.py             │
 └────────────────────────────────────────────────────────────────────────────┘
```

The app itself imports only Qt (and comtypes for the spell checker). Everything that needs PyTorch runs in a
**worker process** started in the model's Python environment, so the app never depends on the model libraries.

## Design rules that run through the code

- **No per-model tables or special cases.** What the app says about a model comes from the model's own files
  (config.json, model_index.json, the safetensors / GGUF header, the model card) or the libraries' own registries.
  If the files don't say, the app says "not recorded" or "Unknown" – it doesn't guess.
- **One wire per input.** Several things in = a Merge (same kind) or a Data node (material).
- **Settings come from the model's code.** Only settings a pipeline's `__call__` accepts are passed; an online model's
  settings come from the service's schema of it.
- **Keys stay in Windows user variables**, never in graphs or settings files.
- **Results are files** in an output folder (Edit Media's live results are temporary "instances").

## The app

| File | What it holds |
|---|---|
| `main.py` | Entry point; MainWindow (menus, panes, shortcuts, save/open, autosave, restart); `--mcp-server` mode for the built .exe |
| `node_editor/view.py` | The graph scene (connect rules, unique names, change signals) and view (Tab menu, pan / zoom, rename) |
| `node_editor/node.py` | `Node`, `Socket`, `Param` base classes; data types; drawing |
| `node_editor/registry.py` | `@register_node` – how a class gets into the Tab menu |
| `node_editor/nodes.py` | Image, Audio, Video, File, Geometry, Prompt, Merge, Data, Delete, Model, Generic |
| `node_editor/modality.py` | The Modality node: collects model + prompt + material into a job, renders, shows the result |
| `node_editor/edit_media*.py` | Edit Media node, its picture view (crop / draw) and timeline (trim / fades) |
| `node_editor/isolate.py`, `chunks.py`, `spans.py`, `shots_view.py` | Picking items, cutting shots, chunks, the shots viewer |
| `node_editor/edit_geometry.py`, `curves.py`, `curve_editor.py` | Edit Geometry node, keyframes, the curve editor |
| `node_editor/geometry_view.py` + `assets/geometry_view.qml` | The 3D viewer (Qt Quick 3D) – one viewer for the whole app, re-bound to the selected node |
| `node_editor/scene_cache.py`, `normals.py` | 3D files → Qt's format (balsam; Alembic / BVH through Blender); smooth normals for OBJ |
| `node_editor/param_panel.py`, `ruler.py`, `playbar.py`, `audiobar.py` | The Parameters pane and its widgets |
| `node_editor/library.py`, `model_store.py`, `model_info.py` | The Library pane, the Hugging Face store scan, model identification from file headers |
| `node_editor/document.py`, `graph_tabs.py`, `clipboard.py`, `undo.py`, `links.py` | `.tau` files (JSON, all tabs in one file, old-version upgrades), tabs, copy/paste, undo/redo, parameter links |
| `node_editor/settings.py`, `preferences.py`, `machine.py` | Per-user settings (registry `HKCU\Software\tauUI`), Preferences dialogs, what this PC has |
| `node_editor/console.py` | The PowerShell console pane |
| `node_editor/spellcheck.py` | Windows' own spell checker through COM |
| `node_editor/node_shapes.py`, `icons.py`, `logo.py`, `pane_splitter.py`, `tab_menu.py`, `edge.py` | Drawing |

## The engine (worker side) – `node_editor/engine/`

| File | What it holds |
|---|---|
| `client.py` | App side: one QProcess worker per environment, jobs out, status / progress / results back as Qt signals; cancel by flag file; stderr → `logs/engine.log` |
| `worker.py` | The worker process: reads JSON jobs, hands them to `recipes.run`, sends events back |
| `recipes.py` | Routes a job: a diffusers repo (or its parts) → `DiffusionPipeline.from_pretrained` the way `model_index.json` declares it (also a model's own pipeline code), pictures / video routed to the pipeline's declared inputs, LoRAs, GPU placement and an out-of-memory retry |
| `runners.py` | Speech, translation, language models, upscalers – chosen from what the files and libraries declare (transformers registries, PEFT adapters, spandrel…) |
| `sdcpp.py` | GGUF models through `sd-cli.exe` (stable-diffusion.cpp) |
| `pictures.py` | One rule for which picture goes to which model input |
| `shots.py` | Shot detection → the shots JSON |
| `loaders.py` | Single `.safetensors` files into diffusers / transformers models |
| `remote.py` | The same worker on another machine over SSH (a rented GPU) |

A **job** is a dictionary: the connected models (path, role, settings), the prompt, the material (images, videos,
sounds, named items), the output folder. A worker keeps one big model loaded and frees it when the next job needs
another.

## Online services

| File | What it holds |
|---|---|
| `services.py` | The services in `services/*.json`: key lookup, status, background HTTP, downloads |
| `svc_replicate.py`, `svc_google.py` | How each service is called |
| `service_run.py` | A Modality with a service model: upload, submit, poll, download, resume after a crash |
| `online.py`, `online_run.py` | Online model files (`online_models/*.json`), Claude Code models, one-shot Claude answers |
| `costs.py` | Prices from the services' own price data; estimates; the spend log `logs/online_spend.jsonl` |

## The Executor

| File | What it holds |
|---|---|
| `executor.py` | Executor mode of the Modality: the brain session (a `claude -p` process in stream-JSON mode, a Gemini loop, or a local model loop), the conversation, phases, handover, budgets |
| `executor_tools.py` | Every tool the brain can call (look_at, list_models, create_node, connect, render_node, finish …) – run on the Qt thread |
| `mcp_server.py` | A tiny MCP server Claude Code starts; it relays tool calls to the app over a local TCP connection with a token |
| `gemini_brain.py` | Gemini as the brain |
| `prompts/executor.txt` | The brain's general instructions; `prompts/templates/` = method texts |

## Adding a node type

1. In `nodes.py` (or a new module imported by it), subclass `Node`, set `type_name`, `label`, `inputs`, `outputs` (lists of
   `(name, data type)`) and `parameters` (a list of `Param(name, label, kind, default, …, hint=…)`).
2. Decorate it with `@register_node` – it appears in the Tab menu.
3. Implement `value()` (what goes downstream) and, if it shows rows in the pane, `info_rows()`.
4. Save / load is automatic: parameters are written by name into the `.tau` file.

## Building

Source run: `python main.py`. Windows build: see `BUILD.txt` (PyInstaller, one folder, Qt as separate DLLs, the
engine's `.py` files beside the exe because workers run them in the user's environments).
