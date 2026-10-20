# Getting started

This guide takes you from a fresh Windows PC to your first generated picture.

tauUI has two halves:

- **the app** – the node editor you see. It only needs Qt and starts in seconds;
- **the models** – they run in a separate *model environment*: a Python virtual environment with PyTorch and the
  model libraries, which you set up once for your GPU. The app starts a background "worker" in that environment
  and sends it jobs.

Keeping them apart means the app never breaks when you update PyTorch, and different models can use different
environments.

---

## 1. Install the app

**Option A – the Windows build.** Unzip `tauUI-windows-x64.zip` anywhere you can write to (not `Program Files`),
for example `C:\tauUI\`, and start `tauUI\tauUI.exe`. The other files in that folder are Python and Qt – leave them
where they are.

**Option B – from source.** Install Python 3.12 or newer, then in the source folder:

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py
```

(`run.bat` does the last line for you.)

The app keeps its settings per Windows user (registry `HKCU\Software\tauUI`). Graphs are saved as `.tau` files.

## 2. Install the helper programs (optional, recommended)

| Program | Needed for | Get it |
|---|---|---|
| **ffmpeg** | Editing video and sound, playing results, cutting shots – most media work | https://ffmpeg.org (put `ffmpeg.exe` and `ffprobe.exe` on your PATH) |
| **Blender** | Opening Alembic (`.abc`) and BVH files in the 3D viewer | https://www.blender.org (found automatically, or *File > Preferences > Blender…*) |
| **stable-diffusion.cpp** | Running GGUF model files | https://github.com/leejet/stable-diffusion.cpp (then *File > stable-diffusion.cpp (sd-cli.exe)…*) |
| **Claude Code** | Using Claude as the Executor's brain | https://claude.com/claude-code |

## 3. Set up a model environment

Make a virtual environment **outside** the app folder, with PyTorch for your GPU. Choose the PyTorch command
for your hardware at https://pytorch.org/get-started/locally/ (NVIDIA CUDA, AMD ROCm, or CPU only).

```
python -m venv C:\venvs\models
C:\venvs\models\Scripts\pip install torch torchvision torchaudio  --index-url <the URL pytorch.org gives you>
C:\venvs\models\Scripts\pip install diffusers transformers accelerate safetensors peft pillow numpy soundfile opencv-python-headless spandrel huggingface_hub
```

What each part is for:

| Package | Used for |
|---|---|
| torch | everything that runs on the GPU |
| diffusers | image and video models (Z-Image, Qwen-Image, FLUX.2, MOSS-Image, Wan…) |
| transformers | language, speech, translation and vision models (Qwen, Whisper, NLLB, MADLAD…) |
| accelerate, safetensors, peft | loading big models, LoRAs |
| pillow, numpy, soundfile, opencv-python-headless | pictures, sound and video frames |
| spandrel | upscalers and restorers (Real-ESRGAN…) |
| huggingface_hub | the `hf` command for downloading models |

Some models need their own library as well – their model card says so (for example `voxcpm`, `gigaam`,
`omnishotcut`). Install those into the same environment when you use them.

Then tell the app about it: **File > Preferences > Default Python env…** → pick `C:\venvs\models`.
A single Model node can use a different environment in its own **Python env** field.

## 4. Get a model

Models are not included. The easiest source is Hugging Face. Download into the shared Hugging Face folder (the app
reads it; the Console at the bottom of the app already has the `hf` command):

```
hf download Tongyi-MAI/Z-Image-Turbo
```

That folder is `%HF_HOME%` if you have set that variable, otherwise `C:\Users\<you>\.cache\huggingface`.
To use another folder, set the Windows user variable `HF_HOME` before starting the app. Models you keep somewhere else: **Library › + Model › Local model
folder…** (or *…file…*) – they are listed by path and never moved.

Click **⟳** in the Library to rescan. Every model shows its size; clicking one shows what its files declare –
type, parts, licence.

> **Check each model's licence.** Some (for example Qwen-Image-2.1 or FLUX.2 klein 9B) forbid commercial use.

## 5. Your first picture

1. **Add a Model node.** Drag *Tongyi-MAI/Z-Image-Turbo* from the Library onto the canvas. A diffusers model
   becomes one node per part (transformer, text_encoder, vae); select the **transformer** node to see the settings.
2. **Set it up:** Width 1024, Height 1024, **Steps 9**, **Guidance 0** (the values its model card uses).
3. **Add a Prompt node:** move the mouse over the canvas, press **Tab**, type *prompt*, press Enter. Write
   *a red fox in a snowy forest, storybook illustration*.
4. **Add a Modality node** (Tab › *modality*). It is where models run.
5. **Wire them:** drag from each model part's output dot to the Modality's **model** input (several parts → put a
   **Merge** node in between, Tab › *merge*), and from the Prompt's output to the Modality's **text** input.
6. Select the Modality and press **Render**. The first render loads the model (about a minute); the picture
   appears in the Parameters pane and is saved to the Modality's **Output folder**.

Change the Seed for another picture; same seed + same settings = the same picture.

## 6. Working with the editor

| Action | How |
|---|---|
| Add a node | **Tab** over the canvas, type, Enter |
| Move around | middle mouse drag = pan, wheel or right drag = zoom |
| Rename | double-click a node's name |
| Connect | drag from an output dot to an input dot (each input takes one wire – use **Merge** for several) |
| Disconnect | drag a wire's input end off into empty space |
| Delete | select, **Del** |
| Undo / redo | **Ctrl+Z** / **Ctrl+Y** |
| Copy / paste / duplicate | **Ctrl+C** / **Ctrl+V** / **Ctrl+D** (works between tabs and windows) |
| Tabs | **Ctrl+T** new, **Ctrl+W** close, double-click to rename; all tabs are saved in one `.tau` file |
| Number sliders ("rulers") | drag the scale; **Shift** = fine, **Ctrl** = ×10, wheel = one step, double-click = default |
| Link two parameters | right-click a parameter › *Copy Parameter*, on the other › *Paste Parameter – linked* |

The graph is saved automatically every 5 minutes to an `autosave` folder (*File > Preferences > Autosave…*).

## 7. Where to go next

- [NODES.md](NODES.md) – every node and setting
- [ONLINE_SERVICES.md](ONLINE_SERVICES.md) – Replicate and Google models, prices, budgets
- [EXECUTOR.md](EXECUTOR.md) – letting an AI model build and run graphs for you
- [ARCHITECTURE.md](ARCHITECTURE.md) – how the code is organised

## Troubleshooting

| Problem | What to check |
|---|---|
| Model node says *no Python environment* | *File > Preferences > Default Python env…* |
| Render fails at once | the Console at the bottom and `logs/engine.log` show the worker's error – usually a missing package in the model environment |
| Out of GPU memory | smaller size, fewer reference pictures, or **Free memory** on the Modality; the engine retries once with parts taking turns on the GPU |
| No video / sound editing | `ffmpeg` and `ffprobe` must be on PATH |
| 3D viewer is black | *File > Preferences > 3D…* → try another graphics API, then restart |
