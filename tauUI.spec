# PyInstaller spec for the Windows build:  pyinstaller tauUI.spec
# One folder (dist\tauUI\) with tauUI.exe and everything beside it - no _internal sub-folder, so the app finds its
# folders (prompts, services, online_models, node_editor\engine for the model workers) next to the exe as it does
# when run from source. Qt stays as separate, replaceable DLLs (LGPL).
import os

here = os.path.abspath(".")
datas = [
    ("node_editor", "node_editor"),      # the engine's worker scripts run in the user's model environments as .py
    ("prompts", "prompts"),
    ("services", "services"),
    ("online_models", "online_models"),
    ("licenses", "licenses"),
    ("docs", "docs"),
    ("LICENSE", "."),
    ("THIRD_PARTY_NOTICES.txt", "."),
    ("README.md", "."),
    ("CREDITS.txt", "."),
    ("BUILD.txt", "."),
    ("tauUI.png", "."),
    ("tauUI_dark.png", "."),
]

import PySide6
# Qt's 3D asset converter (scene_cache.py runs it from beside PySide6)
binaries = [(os.path.join(os.path.dirname(PySide6.__file__), "balsam.exe"), "PySide6")]

a = Analysis(
    ["main.py"],
    pathex=[here],
    binaries=binaries,
    datas=datas,
    hiddenimports=["PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQml", "PySide6.QtPdf",
                   "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtNetwork", "comtypes.client"],
    excludes=["tkinter", "torch", "diffusers", "transformers", "numpy"],  # models run in their own environments
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="tauUI", icon=None, console=False,
          contents_directory=".")
coll = COLLECT(exe, a.binaries, a.datas, name="tauUI")
