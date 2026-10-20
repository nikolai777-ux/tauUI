# Node reference

Every node type in the Tab menu, its inputs, outputs and settings. Settings are listed as they appear in the
Parameters pane; hover over one in the app for a short hint.

**Wires and types.** Outputs and inputs are coloured by type: *model*, *text*, *image*, *audio*, *video*, *geometry*,
*file*, *JSON*, *data* (material for a model) or *any*. An input accepts one wire; to bring several things in, use a
**Merge** (same kind) or **Data** (mixed material) node.

**Results stay in the graph.** A rendered result is shown in its node's pane and saved to that node's output
folder, but a result alone doesn't mark the graph as changed.

Contents: [Sources](#sources) · [Model](#model) · [Modality](#modality) · [Joining and picking](#joining-and-picking)
· [Editing](#editing) · [Film shots](#film-shots) · [3D](#3d) · [Other](#other)

---

## Sources

### Image · Audio · Video · File
Output: the file, typed as *image*, *audio*, *video* or *file*.

| Setting | What it does |
|---|---|
| File | The file. Drag one from Explorer onto the canvas to make the node. |

The pane shows the picture, a player with a frame-exact timeline (video: Left/Right = one frame, Space = play)
or a live spectrum display (sound), and what the file's own headers record (size, frame rate, codec, embedded
prompt and seed of generated pictures…). A **File** node takes any file a model may read (PDF, text…); a picture
through a File node is still a picture.

### Prompt
Output: *text*. Plain text – a prompt, a negative prompt, text to translate or speak.

| Setting | What it does |
|---|---|
| Prompt | The text the model receives |
| File | A text file: picking one loads it |
| Save to file | Writes the text to File |
| Spell check | Language for the red underlines (the spell checkers Windows has installed) |

### Geometry
Output: *geometry*. A 3D scene – OBJ, FBX, glTF/GLB, PLY, STL, DAE, 3DS, Alembic (needs Blender), BVH motion
capture (needs Blender).

| Setting | What it does |
|---|---|
| Scene | The 3D file: meshes, cameras, animation |
| Camera file | More cameras from another file (optional) |
| Camera (on the viewer's header) | Look through the default orbit camera or one of the file's cameras |

**Viewer:** left drag = orbit, middle drag = pan, right drag or wheel = move in, double-click = frame everything.
The icons on its header switch headlight, wireframe, textures, anti-aliasing, grid, ground and "at origin". The
cube in the corner turns with the view; click a face for a straight view. Animation plays on a frame timeline like
a video.

---

## Model
Output: *model*. Any AI model – a weights file or a model folder. **It detects what the model is from the model's
own files** (config.json, model_index.json, the weights' header, the model card) and shows only the settings that
fit. A model folder from Hugging Face dropped on the canvas becomes one node per part (transformer, text encoder,
VAE…); a model in your own folder is added with *Library › + Model*.

| Setting | Shown for | What it does |
|---|---|---|
| Runs on | all | **Local** (your GPU) or **Online** (a service) |
| Provider, Model | online | Which service, which of its models (*Library › + Model › Online…*) |
| File | local | The weights file or folder |
| Python env | local | The model environment it runs in (empty = the default one) |
| Role | local | What this part is: diffusion model, text encoder, VAE, LoRA, image encoder, checkpoint, speech recognition, translation, language model, text to speech… – *Auto-detect* reads it from the files |
| Width, Height | image / video models | Output size in pixels |
| Steps | image / video models | More steps: finer but slower |
| Guidance | image / video models | How strictly the prompt is followed |
| Negative prompt | image / video models | What the result should avoid |
| Frames | video models | Video length in frames |
| Seed | generating models | Same seed and settings = the same result |
| Strength | LoRAs | How strongly the LoRA applies |
| From / To language | translation | Language codes the model's tokenizer uses (empty when the model takes a text prefix instead) |
| Language | speech recognition | Spoken language (empty = detect) |
| Thinking | language models whose template supports it | Let the model reason before answering (shown in a Reasoning box) |
| Max new tokens, Temperature | language models | Length limit and variety of the answer |
| Voice sample | text to speech | The voice to clone / speak with |

The **Detected** rows below the settings list what the files say: type, precision, parameters, size, parts of a
pipeline, base model, task and licence from the model card. *Unknown* means the files don't say – the app doesn't
guess.

**LoRAs.** Wire a LoRA's Model node together with its base model into the same Merge. The model card's base model is
compared with yours and a mismatch is reported.

---

## Modality
Inputs: **model**, **text** (the prompt), **data** (pictures, videos, sounds, files). Output: the result; its
type follows what the model made (image, video, audio, text, JSON, geometry).

This is where models run. It sends the connected models, the prompt and the material to the engine and shows the
result.

| Setting | What it does |
|---|---|
| Render | Run it. Upstream Modalities without a result render first, so a chain runs in order |
| Stop | Stop the render (or the Executor's brain) |
| Free memory | Unload the models of its Python environment from memory |
| Overwrite results | Render again what already has results (otherwise existing upstream results are kept) |
| Output folder, File name | Where results are saved, under what name (default: the node's name + time + seed) |
| Budget *service* $ | (Executor) what the brain may spend on that service without asking you |
| Executor | Switch to Executor mode – see [EXECUTOR.md](EXECUTOR.md) |
| Web search, PowerShell | (Executor) what the brain may use |
| Prompt, Conversation, Log | (Executor) your messages, the brain's replies, everything it did |

**Several pictures.** Pictures arrive in wire / Merge / Data order and go to the model's main picture input (one
picture, or a list – e.g. reference pictures for Qwen-Image-2.1). A Data item **named** like another picture input the
model declares goes there instead (e.g. `end_img`, `mask_image`). The status line says which picture went where.

**Online models** (Replicate, Google) work the same way – see [ONLINE_SERVICES.md](ONLINE_SERVICES.md). The estimated
price is shown before you render.

---

## Joining and picking

### Merge
Inputs 0–4, output: a list. Up to five inputs of **one** type, kept in index order. The first wire decides the type.
Use it for a pipeline's parts (+ LoRAs) into a Modality's model input, or several prompts. More than five: a Merge
into input 4 of another Merge.

### Data
Inputs 0–4, output: *data*. The material for a model, in order: pictures, videos, sounds, JSON, other files (not
prompts – those go to the Modality's text input). Each input takes one item or a list; more than five: Data into
Data. The pane lists the items numbered.

### Delete
Input: a list, output: the same list without one item.

| Setting | What it does |
|---|---|
| Delete item | Which item to leave out, numbered as the list shows them (1 = first) |

### Isolate
Input: a list or shots/chunks, output: one item.

| Setting | What it does |
|---|---|
| Item | Which item of a list (shown in the pane, ready to edit) |
| Shot / Chunk | With a shots or chunks JSON: which shot to cut out of the video (or sound) they were found in |
| Output folder | Where cuts are saved (`<node>_shot001.mp4` …) |

A video is cut to the shot's exact frames, a sound to its seconds.

---

## Editing

### Edit Media
Input: an image, video or sound (also a shots/chunks JSON), output: the edited file. **The original is never
touched**: edits make a separate file ("instance") that updates live as you change settings. *Export…* saves a copy
where you want it.

**Pictures and video frames**

| Setting | What it does |
|---|---|
| Tools | Crop (drag a box, **Enter** applies, Esc cancels), rotate, flip, draw, reset |
| Pen colour, Pen width, Drawing | Draw on the picture; undo or clear the strokes |
| Width, Height, Keep proportions | Output size (0 = as it comes in; one side + lock = keep the aspect) |
| Exposure, Contrast, Saturation, Gamma, Sharpen | Picture adjustments (negative sharpen blurs) |

**Video**

| Setting | What it does |
|---|---|
| Trim | Yellow brackets on the timeline; drag them, double-click one to reset it |
| Speed / Frame rate | Linked: every frame is kept, changing one changes the other |
| Sound | Keep or mute |
| Render | Make the edited file now (it is also made when something downstream needs it) |

**Sound**

| Setting | What it does |
|---|---|
| Trim, fades | Brackets on the timeline; the small squares at their top edge drag out fade-in / fade-out |
| Noise reduction | Off / light / strong |
| Speed | Faster or slower, pitch kept |
| Volume (dB), Normalize | Louder or quieter; even out the loudness to a target (-14 / -16 / -23 LUFS) |
| Channels, Sample rate | Mono / stereo, samples per second |

**From shots / chunks:** *Shot* or *Chunk* picks the span, *Handles* adds film frames on each side, and the trim
brackets start on the span.

### Edit Geometry
Input/output: *geometry*. Animate or adjust a 3D scene in its viewer: click to select (Shift adds), drag the handle
to move, turn or scale, **auto-key** records keys as you move, **Set Key** keys everything selected. Keys on objects
the file already animates are added on top of that animation. The **Curves** editor below shows and edits the keys;
**Frame rate** is the speed it plays at. The trim brackets on the timeline limit the playing range. (Edits live in the
node; exporting them to a file is not built yet.)

---

## Film shots

A shot-detection model (for example OmniShotCut) in a Modality turns a video into a **shots JSON**: every shot with its
first/last frame and transitions. The result pane shows the film with each shot marked; click one to play it.

### Chunk
Input: shots JSON, output: chunks JSON (saved as `<node>_max<N>.json`).

| Setting | What it does |
|---|---|
| Max frames | Longest chunk: each shot is split into equal pieces no longer than this |
| Output folder | Where the file goes |

Feed shots or chunks into **Isolate** (cut one out) or **Edit Media** (edit one with handles).

---

## 3D

See **Geometry** and **Edit Geometry** above.

## Other

### Generic
A placeholder node with a number, a count, an on/off and a note – for sketching a graph before the real nodes exist.
