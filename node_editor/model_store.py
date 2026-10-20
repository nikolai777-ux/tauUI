"""The Hugging Face model store (the cache in <store>\\hub): what's there, what each repo says it is.

Everything is read from the repos' own files (model_index.json, config.json, adapter_config.json, README
front matter). huggingface_hub keeps identical files once, in <hub>\\blobs\\<xx>\\<hash>; a repo's
blobs\\<etag> is a link to it and <hash>.refs lists every repo using it. So a file is "shared" when another
repo is listed there, and deleting this repo doesn't free it. Deleting is left to huggingface_hub itself
(`hf cache rm`), which updates those lists and removes a shared file once nothing uses it."""

import json
import os
import re
from dataclasses import dataclass, field

from .model_info import model_card

REPO_DIR = re.compile(r"^(models|datasets|spaces)--(.+)$")


@dataclass
class StoreFile:
    path: str        # relative to the snapshot, with "/" separators
    size: int
    shared: bool     # another repo uses the same stored file


@dataclass
class Repo:
    repo_id: str
    kind: str        # models / datasets / spaces
    folder: str      # <store>\hub\models--org--name
    revision: str = ""
    snapshot: str = ""
    snapshots: int = 0
    files: list = field(default_factory=list)
    total: int = 0            # all blobs of the repo (every revision)
    exclusive: int = 0        # what deleting the repo frees
    stored: dict = field(default_factory=dict)     # {real file on disk: size}
    incomplete: int = 0       # unfinished downloads
    index: dict = field(default_factory=dict)      # model_index.json
    config: dict = field(default_factory=dict)     # config.json
    adapter: dict = field(default_factory=dict)    # adapter_config.json
    card: dict = field(default_factory=dict)       # README front matter

    @property
    def task(self):
        """The task the authors declared on the model card (Hugging Face's pipeline_tag), or None."""
        return self.card.get("pipeline_tag")

    @property
    def class_label(self):
        """(class, the file that names it) - what the model's code is, as its own files say; or None."""
        if self.index.get("_class_name"):
            return self.index["_class_name"], "model_index.json"
        if self.adapter:
            return f"{self.adapter.get('peft_type', '')} adapter".strip(), "adapter_config.json"
        arch = (self.config.get("architectures") or [None])[0] or self.config.get("_class_name") \
            or self.config.get("architecture") or self.config.get("model_type")
        return (str(arch), "config.json") if arch else None

    def components(self):
        """[(name, 'library.Class' or None, present)] from model_index.json."""
        top = {f.path.split("/")[0] for f in self.files if "/" in f.path}
        out = []
        for key, value in self.index.items():
            if key.startswith("_") or not isinstance(value, list) or len(value) != 2:
                continue
            lib, cls = value
            out.append((key, f"{lib}.{cls}" if lib and cls else None, key in top))
        return out

    def requirements(self):
        """[(label, value)] of what the repo's own files say it needs."""
        rows = []
        missing = [name for name, cls, present in self.components() if cls and not present]
        if missing:
            rows.append(("Missing parts", ", ".join(missing)))
        if self.adapter.get("base_model_name_or_path"):
            rows.append(("Base model", f"{self.adapter['base_model_name_or_path']} (adapter_config.json)"))
        if self.card.get("base_model"):
            rows.append(("Base model", f"{self.card['base_model']} (model card)"))
        return rows


def hub_dir(store):
    return os.path.join(store, "hub")


def scan(store):
    hub = hub_dir(store)
    repos = []
    if not os.path.isdir(hub):
        return repos
    for name in sorted(os.listdir(hub), key=str.lower):
        m = REPO_DIR.match(name)
        folder = os.path.join(hub, name)
        if m and os.path.isdir(folder):
            repos.append(_read_repo(m.group(1), m.group(2).replace("--", "/", 1), folder))
    return repos


def _read_repo(kind, repo_id, folder):
    repo = Repo(repo_id, kind, folder)
    snaps_dir = os.path.join(folder, "snapshots")
    snaps = [d for d in _listdir(snaps_dir) if os.path.isdir(os.path.join(snaps_dir, d))]
    repo.snapshots = len(snaps)
    main = _read_text(os.path.join(folder, "refs", "main"))
    if main in snaps:
        repo.revision = main
    elif snaps:
        repo.revision = max(snaps, key=lambda d: os.path.getmtime(os.path.join(snaps_dir, d)))
    if repo.revision:
        repo.snapshot = os.path.join(snaps_dir, repo.revision)
    blobs = os.path.join(folder, "blobs")
    shared = set()
    for n in _listdir(blobs):
        p = os.path.join(blobs, n)
        if n.endswith(".incomplete"):
            repo.incomplete += _size(p)
            continue
        real = os.path.realpath(p)  # the shared store's file, or the blob itself
        size = _size(real)
        repo.total += size
        repo.stored[real] = size
        if _used_elsewhere(real, folder):
            shared.add(real)
        else:
            repo.exclusive += size
    if repo.revision:
        for root, _, names in os.walk(repo.snapshot):
            for n in names:
                p = os.path.join(root, n)
                real = os.path.realpath(p)  # snapshot link -> repo blob -> shared store
                rel = os.path.relpath(p, repo.snapshot).replace("\\", "/")
                repo.files.append(StoreFile(rel, _size(real), real in shared))
        repo.files.sort(key=lambda f: (f.path.count("/") == 0, f.path.lower()))
        repo.index = _read_json(os.path.join(repo.snapshot, "model_index.json"))
        repo.config = _read_json(os.path.join(repo.snapshot, "config.json"))
        repo.adapter = _read_json(os.path.join(repo.snapshot, "adapter_config.json"))
        repo.card = model_card(repo.snapshot)
    return repo


def read_local(path):
    """A model of the user's own, outside the store: a folder (read like a repo snapshot) or a single file. Same
    facts, from the same files; `kind` = "local", `repo_id` = its name, `snapshot` = the path a Model node takes.
    Big sub-folders (checkpoints, corpora) are summed, not listed file by file."""
    path = os.path.normpath(path)
    name = os.path.basename(path)
    repo = Repo(name, "local", path)
    if not os.path.exists(path):
        repo.incomplete = 1  # shown as missing
        return repo
    repo.snapshot = path
    if os.path.isfile(path):
        size = _size(path)
        repo.files, repo.total = [StoreFile(name, size, False)], size
        repo.card = model_card(os.path.dirname(path)) if os.path.isfile(os.path.join(os.path.dirname(path),
                                                                                      "README.md")) else {}
        return repo
    for entry in sorted(_listdir(path), key=str.lower):
        full = os.path.join(path, entry)
        if entry.startswith("."):
            continue  # .cache (download leftovers), .git...
        if os.path.isfile(full):
            repo.files.append(StoreFile(entry, _size(full), False))
            continue
        listed = 0
        for root, dirs, names in os.walk(full):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for n in names:
                size = _size(os.path.join(root, n))
                repo.total += size
                if listed < 200:
                    rel = os.path.relpath(os.path.join(root, n), path).replace("\\", "/")
                    repo.files.append(StoreFile(rel, size, False))
                    listed += 1
    repo.total += sum(f.size for f in repo.files if "/" not in f.path)
    repo.exclusive = 0  # removing it from the Library frees nothing: the files stay where they are
    repo.index = _read_json(os.path.join(path, "model_index.json"))
    repo.config = _read_json(os.path.join(path, "config.json"))
    repo.adapter = _read_json(os.path.join(path, "adapter_config.json"))
    repo.card = model_card(path)
    return repo


def _used_elsewhere(real, folder):
    """True when the shared store's list for this file names another repo that still links to it."""
    hub, own = os.path.dirname(folder), os.path.basename(folder) + "/"
    for ref in _read_text(real + ".refs").splitlines():
        ref = ref.strip()
        if ref and not ref.startswith(own) and os.path.lexists(os.path.join(hub, *ref.split("/"))):
            return True
    return False


def physical_size(repos):
    """Bytes on disk for these repos, counting shared files once."""
    stored = {}
    for r in repos:
        stored.update(r.stored)
    return sum(stored.values())


def delete_command(repo):
    """The huggingface_hub command that removes the repo (and shared files nothing else uses)."""
    return f"hf cache rm {repo.kind.rstrip('s')}/{repo.repo_id} --yes"


def _size(path):
    try:
        return os.stat(path).st_size
    except OSError:
        return 0


def fmt_size(n):
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= div:
            return f"{n / div:.1f} {unit}" if unit != "KB" else f"{n / div:.0f} KB"
    return f"{n} B"


def _listdir(path):
    try:
        return os.listdir(path)
    except OSError:
        return []


def _read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}
