# tauUI

A node-graph desktop app for working with AI models – image, video, sound, 3D and language models – locally on
your own GPU or through online services you have an account with.

Every shape on the canvas is a node (a picture, a prompt, a model, a sound) and wires carry data between them.
A Modality node runs the models wired into it. In Executor mode, a language model you connect can plan and build a
whole graph from a plain-language request, and test its results.

**Status:** a personal project, shared as is. It was developed and tested on one Windows 11 PC (AMD GPU, ROCm).
Expect rough edges.

## What you need

- Windows 10/11 and Python 3.12 or newer (developed on 3.14).
- The app itself only needs PySide6 (`pip install -r requirements.txt`).
- **Models run in their own Python environment**, separate from the app (PyTorch, diffusers, transformers…). Set it
  in *File > Preferences > Default Python env…*, or per Model node.
- Optional programs the app calls if you install them: **ffmpeg** (video and sound editing – needed for most media
  work), **Blender** (Alembic / BVH import), **stable-diffusion.cpp** (GGUF models).

## Run from source

```
pip install -r requirements.txt
python main.py
```
(or `run.bat`, which expects a virtual environment in `.venv`).

## Models

**No AI models are included.** You download them yourself (for example from Hugging Face) and the app's Library lists
them. Every model has its own licence – some forbid commercial use. The Model node shows a model's licence when its
files declare one; following it is your responsibility.

## Online services

Online services (Replicate, Google AI Studio) use **your own account and API key**, kept in a Windows user
environment variable (`REPLICATE_API_TOKEN`, `GEMINI_API_KEY`) – never in graph files. Paid jobs show their estimated
cost first; you pay the service directly.

Executor mode can use **Claude Code** as its brain. That uses your own Claude Code installation and sign-in; you are
responsible for following Anthropic's terms for how you use it.

## Credits

Aslan Zamaev – design, direction, testing. Built with Claude (Anthropic) as coding partner.
Thanks to Tamerlan Zamaev and Zulia Shavaeva for their advice and ideas. See `CREDITS.txt`.

## Licence

Copyright (C) 2026 Aslan Zamaev.
tauUI is free software under the **GNU General Public License v3** (see `LICENSE`). It comes with **no warranty**.
It uses Qt for Python (PySide6) – see `THIRD_PARTY_NOTICES.txt`.

## Documentation

See [docs/README.md](docs/README.md): getting started, every node, online services, the Executor, the code.

## Building a Windows .exe

See `BUILD.txt`.
