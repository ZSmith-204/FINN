# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the FINN CLI (docs/cli-layer.md).

Build from the repository root:

    .venv/bin/pyinstaller finn.spec --clean --noconfirm

Output: ``dist/finn`` (Linux) or ``dist/finn.exe`` (Windows).  PyInstaller does
not cross-compile: each artifact must be built on its own operating system.
"""

import os

# `finn run` imports fastapi/uvicorn lazily (finn_cli.py) and the generated
# main.py imports fastapi, so their dependency graphs must be bundled so
# `run` works with no third-party installs.  PyInstaller ships hooks for
# these packages; listing them explicitly here is a safety net.
_hiddenimports = [
    "fastapi",
    "fastapi.staticfiles",
    "starlette",
    "starlette.staticfiles",
    "pydantic",
    "uvicorn",
    "anyio",
    "h11",
    "click",
]

a = Analysis(
    ["finn_cli.py"],
    pathex=[os.path.abspath(".")],
    binaries=[],
    # requirements.txt is read via sys._MEIPASS/_bundle_dir() when frozen.
    datas=[("requirements.txt", ".")],
    hiddenimports=_hiddenimports,
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
    name="finn",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
