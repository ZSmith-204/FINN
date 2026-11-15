# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for FINN Studio (the PyQt6 desktop GUI).

Build from the repository root:

    .venv/bin/pyinstaller finn-studio.spec --clean --noconfirm

Output: ``dist/finn-studio`` (Linux) or ``dist/finn-studio.exe`` (Windows).
The frozen app is a windowed one-file executable; it does not bundle the FINN
CLI. At runtime it shells out to the ``finn`` executable found on PATH (see
``default_cli_command()`` in ``finn_studio.py``), so ``finn`` must be installed
separately and discoverable. PyInstaller does not cross-compile: build each
artifact on its own operating system.

PyQt6 is handled by PyInstaller's bundled hooks; no extra hidden imports or
data files are required.
"""

import os

a = Analysis(
    ["finn_studio.py"],
    pathex=[os.path.abspath(".")],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="finn-studio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
