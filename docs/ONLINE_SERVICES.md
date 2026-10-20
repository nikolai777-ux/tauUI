# Online services

Besides models on your own GPU, tauUI can run models on online services **with your own account**. They show up in
the Library's **Online** list next to the local models and are used through the same Model and Modality nodes.

| Service | What it offers | Your key |
|---|---|---|
| **Replicate** | Thousands of models: image (FLUX, Seedream, GPT Image, Nano Banana…), video (Kling, Veo…), 3D (TRELLIS, Hunyuan3D), sound | `REPLICATE_API_TOKEN` |
| **Google AI Studio** | Gemini text and vision models, Gemini speech (TTS), Imagen / Nano Banana, Veo, Lyria music | `GEMINI_API_KEY` |
| **Claude Code** | Claude as a brain for Executor mode, or answering a prompt in a normal Modality | your own Claude Code sign-in |

You pay the service directly; tauUI never sees your payment details.

## 1. Set your key

Create a key on the service's website (Replicate: *Account › API tokens*; Google: *aistudio.google.com › Get API
key*), then store it as a **Windows user variable** – it is never written into graphs or app files:

```
setx REPLICATE_API_TOKEN r8_your_token_here
setx GEMINI_API_KEY your_key_here
```

Restart the app afterwards. The Library shows the service with your account name or the number of its models; a
problem (missing or wrong key) is shown on the same line.

The services the app knows are small files in `services\` (`replicate.json`, `google.json`) – see
`services\README.txt`. You can add one with *Library › + Model › Service…*.

## 2. Add online models

Right-click a service in the Library › **Browse…**: search, look at a model's inputs and price, and add it. Each added
model becomes a small file in `online_models\`. Or *Library › + Model › Online model…* for Claude Code models.

## 3. Use one

1. Drag the online model from the Library onto the canvas – it becomes a Model node that **runs online**.
2. Its settings come from the service's own description of the model (sizes, durations, quality options…), with the
   service's defaults; only what you change is sent.
3. Wire it into a Modality like a local model, with a Prompt and (if the model takes them) pictures, video or sound
   through a Data node. A Data item **named** after one of the model's inputs goes to that input
   (e.g. `start_image`, `end_image`); otherwise each kind goes to the model's main input for it.
4. The Modality shows the **estimated price** before you render. Above the service's `confirm_over` amount ($1 by
   default) it asks before sending.
5. Render: files are uploaded, the job runs on the service, the result is downloaded at once (Replicate deletes its
   copies after an hour) and saved to the Modality's output folder.

If the app or PC stops while a job runs, the job id is kept in the node; it is picked up again when you open the graph.

## Prices and spending

- Replicate's prices are read from each model's page; Google's from the price table in `services\google.json`
  (dated – check Google's pricing page when it matters).
- When a price can't be known exactly (it depends on the result), the estimate shows a range, and the upper end counts.
- **Every paid run is logged** in `logs\online_spend.jsonl`: model, estimate, real cost, which node ordered it.
- In Executor mode, each service has a **Budget $** the brain may not exceed without asking you.
- At most a few jobs run at once per service (`max_running`, default 3); the rest wait unsent – stopping them costs
  nothing.

## Claude Code

Claude Code is not a key-based service: tauUI starts the `claude` program installed on your PC, signed in to your own
Claude account. Install it from https://claude.com/claude-code and run `claude` once to sign in. Using it through
tauUI follows Anthropic's terms for your account – you are responsible for that use.

In a normal Modality (Executor off), a Claude model answers one prompt with the connected pictures. In Executor mode
it is the brain – see [EXECUTOR.md](EXECUTOR.md).
