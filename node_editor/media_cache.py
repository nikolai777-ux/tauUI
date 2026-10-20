"""Instances: the results of Edit Media nodes, kept by the app (not in the user's folders) while they are needed.

Each Edit Media node owns one instance - its current result. It is replaced when the node's source or settings
change (the old file is deleted), deleted with the node, and the whole folder is cleared when the app closes
(and on start, in case the app was killed). Files written a moment ago are normally still in Windows' memory
cache, so a model reading an instance right after it was made rarely touches the disk. Export copies an
instance somewhere the user chooses - the only way a result leaves this folder.
Source nodes need no instance: a model reads the original file itself.
"""

import os
import shutil
import tempfile

CACHE_DIR = os.path.join(tempfile.gettempdir(), "NodeEditor", "instances")


def path_for(owner, key, ext):
    """Where the instance of `owner` (a node) for settings `key` goes."""
    import hashlib
    os.makedirs(CACHE_DIR, exist_ok=True)
    digest = hashlib.sha1(repr(key).encode("utf-8")).hexdigest()[:12]
    return os.path.join(CACHE_DIR, f"{id(owner):x}_{digest}{ext}")


def release(path):
    """Delete an instance file (only ever inside the cache folder)."""
    if path and os.path.dirname(os.path.abspath(path)) == os.path.abspath(CACHE_DIR):
        try:
            os.remove(path)
        except OSError:
            pass


def clear():
    shutil.rmtree(CACHE_DIR, ignore_errors=True)
