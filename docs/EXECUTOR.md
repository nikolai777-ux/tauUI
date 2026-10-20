# Executor mode – an AI model that builds graphs for you

Tick **Executor** on a Modality node and the language model connected to it becomes its **brain**. You describe what
you want in plain words; the brain proposes a plan, waits for your answer, then builds the nodes itself – in a tab of
its own – tests a sample, looks at the result, fixes what's wrong and tells you which nodes give the final results.

It works through the same graph you see: every node it makes is an ordinary node you can open, change and render.

## What can be the brain

| Brain | How to connect it | Notes |
|---|---|---|
| **Claude Code** (recommended) | A Model node: Runs on *Online*, Provider *Claude Code*, Model e.g. Claude Opus or Sonnet (*Library › + Model › Online…* adds it) | Uses **your own** Claude Code installation and sign-in – install it from https://claude.com/claude-code and run `claude` once to sign in. You are responsible for following Anthropic's terms. Can also search the web and run PowerShell commands if you allow it. |
| **Gemini** (Google AI Studio) | A Model node with a Google text model | Needs your `GEMINI_API_KEY`; billed per use. |
| **A local language model** | A Model node with a local language model (e.g. Qwen3.5-9B) | Free and private, but much slower and weaker at long builds. Its LoRA can be connected next to it for the brain to use. |

## A first build

1. A **Model** node for the brain → a **Modality** node's *model* input. Tick **Executor** on the Modality.
2. Optional: a **Prompt** node with your request → the *text* input; your pictures / videos / files → a **Data** node →
   the *data* input. Name the nodes after what they are ("hero_face", "costume_ref") – the brain reads the names.
3. Type into the Executor's **Prompt** box and press **Enter**, e.g.
   *"Make four portraits of the person in hero_face wearing the costume in costume_ref, front, side, back and
   chest-up, 1024×1536. Use a model that runs here. Test one first."*
4. The brain answers in the **Conversation** box with a plan: which models, which settings and where they come from,
   what it can't do. Reply in the same box – approve, change, or ask.
5. It builds and tests. The graph stays usable but the brain's nodes are its own; yours are never changed (it only
   wires *from* them). **Stop** stops it at any time. **Ctrl+Z** undoes a whole stretch of building.
6. When it's done it lists what it built, what it tested and how it looked, and which nodes are the final ones. Ask it
   to render the rest, change something, or try again – the conversation goes on.

The **Log** box shows everything it did, tool call by tool call.

## Settings on the Executor

| Setting | What it does |
|---|---|
| Web search | The brain may read web pages (Claude Code only) |
| PowerShell | The brain may run commands on this PC – e.g. Blender in the background, file conversions (Claude Code only) |
| Overwrite results | Render again what already has results |
| New conversation (↺ icon) | Start over with a fresh conversation; the nodes it built stay |
| Budget *service* $ | After Stop: what the brain may spend on each online service without asking you. 0 = it must ask before every paid render |
| Stop | Stops the brain and anything it is rendering |

## What the brain can do

It has its own set of tools in the app: look at pictures and video frames (zooming in), list and read files, read
each model's card and code to choose settings, list node types, create / wire / change / delete its own nodes (also in
batches), render a node and wait for it, render everything it built in order, estimate the price of an online model,
search and add online models, free GPU memory, and hand over to a fresh session when its memory gets long.

What guides it is the text in `prompts/executor.txt` – general rules: your words come first, ask when truly unclear,
say up front what can't be done, test before finishing, never touch your nodes, report honestly.

## Templates – a method for a kind of task

A **template** is a plain text file starting with `Method:` that describes how a kind of job is best done – for
example making a narrated picture story. Wire it in as a Prompt node (or name it in your request) and the brain follows
it as a guide. Examples are in `prompts/templates/`:

| Template | For |
|---|---|
| `pictures.txt` | Consistent story pictures: references and turnarounds for every character, true scale, a 3D layout first, checking every picture at full size |
| `still_story.txt` | Narrated picture stories: script, storyboard, narration timing, other languages fitted to the same timeline |
| `video_story_production.txt` | Short films: shot list, 3D layout, story reel, paid video waves, continuity, sound |
| `choosing_models.txt` | Picking a model: needs → shortlist from facts → bake-off → decision |

Write your own the same way; they are only text.

## Paid services

When the brain wants to use an online model it checks the price first (`estimate_cost`). It may only spend up to the
**Budget** you set per service; beyond that the app refuses and the brain has to ask you. Every paid job is logged in
`logs/online_spend.jsonl` with the node that ordered it.

## Good to know

- Long builds can take hours. The brain keeps its state in files in its work folder
  (`%LOCALAPPDATA%\NodeEditor\executor\<node name>`), so a crash or a new session can continue.
- If the app is closed while the brain works, open the graph again and type *please continue*.
- A local GPU runs one big model at a time; the brain plans renders in stages for that reason.
