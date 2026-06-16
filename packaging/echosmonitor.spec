# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-dir spec for EchosMonitor (M7-B).

Builds a self-contained ``dist/echosmonitor/`` directory: a launcher plus the
Python runtime, PySide6, and every dependency. One-dir (not one-file) is the
deliberate choice — faster startup, no per-launch temp extraction, and it is
the input the OS installers in M7-C wrap (Inno/NSIS, AppImage, .dmg).

Run it through the repo's build scripts, not directly:

    ./scripts/build.sh           # Linux / macOS
    powershell scripts/build.ps1 # Windows

The load-bearing collection rules (each is a real failure mode without it):
  * copy_metadata('echosmonitor') — so importlib.metadata.version() resolves in
    the freeze (belt-and-suspenders with the _version.py fallback chain).
  * the packaged config/default.yaml + resources/EchosMonitor.png — the loader
    reads default.yaml via importlib.resources.as_file (config/loader.py); the
    icon via gui/resources.app_icon(). Both must be on disk in the bundle.
  * obspy data files + IO submodules — obspy resolves its MiniSEED/StationXML
    readers through entry points that PyInstaller's static analysis misses.
  * keyring backends — rule-15 credential storage loads the OS backend via an
    entry point (Secret Service / macOS Keychain / Windows credential locker).
  * IPython — hvsrpy's postprocessing does a top-level
    `from IPython.display import display`; `import hvsrpy` fails without it
    (the reason pyproject pins `ipython`).
"""

import os
import sys

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

# SPECPATH is injected by PyInstaller (the directory holding this spec). Anchor
# every spec-relative path on it so the build works from any working directory.
_HERE = SPECPATH
_REPO = os.path.abspath(os.path.join(_HERE, os.pardir))

# Per-platform icon (M7-C2). PyInstaller ignores PNG icons on Windows/macOS, so
# use the derived .ico/.icns (regenerate from the master PNG via
# packaging/make_icons.py). Linux keeps the PNG — the runtime QApplication icon
# is set from that same PNG via gui/resources.app_icon() regardless of platform.
if sys.platform == "win32":
    _ICON = os.path.join(_HERE, "icons", "EchosMonitor.ico")
elif sys.platform == "darwin":
    _ICON = os.path.join(_HERE, "icons", "EchosMonitor.icns")
else:
    _ICON = os.path.join(_REPO, "src", "echosmonitor", "resources", "EchosMonitor.png")

# --- metadata (importlib.metadata in the freeze) ----------------------------
datas = []
datas += copy_metadata("echosmonitor")

# obspy ships compiled C libraries (libmseed, evresp, ...) under obspy/lib/ and
# loads them at runtime via ctypes.CDLL (not import), so PyInstaller's static
# analysis misses them. Collect them into _internal/obspy/lib/ where
# obspy.core.util.libnames._load_cdll looks. The default search pattern is
# `lib*.so`, but obspy names them `mseed.cpython-*.so` (no `lib` prefix), so the
# patterns must be widened or nothing is collected.
binaries = []
binaries += collect_dynamic_libs(
    "obspy", search_patterns=["*.so", "*.dll", "*.dylib", "*.pyd"]
)

# --- our packaged resources (default.yaml + icon) ---------------------------
datas += collect_data_files("echosmonitor", includes=["**/*.yaml", "**/*.png"])

# --- obspy: data files + entry-point-resolved IO plugins --------------------
datas += collect_data_files("obspy")

# --- hidden imports PyInstaller's static analysis cannot see ----------------
hiddenimports = []
hiddenimports += collect_submodules("obspy.io")  # MiniSEED / StationXML readers
hiddenimports += collect_submodules("keyring.backends")  # rule-15 OS backends
hiddenimports += [
    "qasync",
    "pyqtgraph",
    "zeroconf",
    # hvsrpy -> IPython.display top-level import (see module docstring).
    "IPython",
    "IPython.display",
]

block_cipher = None

a = Analysis(
    [os.path.join(_HERE, "entry.py")],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    # obspy resolves its RELEASE-VERSION path via a relative frozen __file__;
    # the runtime hook makes that absolute so import obspy does not crash.
    runtime_hooks=[os.path.join(_HERE, "rthooks", "pyi_rth_obspy_version.py")],
    # Trim large unused stacks to keep the bundle smaller. PySide6 multimedia /
    # 3D / webengine are not used (the Map tab is pure pyqtgraph — decision log
    # M4-B); tkinter is never imported.
    excludes=[
        "tkinter",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtMultimedia",
        "PySide6.Qt3DCore",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="echosmonitor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # windowed GUI app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_ICON,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="echosmonitor",
)

# macOS: wrap the one-dir collection in a proper .app bundle so the M7-C2 .dmg
# ships a double-clickable application (Finder app, Dock icon, Gatekeeper
# quarantine target) rather than a bare folder. No-op on Linux/Windows.
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="EchosMonitor.app",
        icon=_ICON,
        bundle_identifier="org.echos.echosmonitor",
        info_plist={
            # The app is not a document editor and has no Retina-art reason to
            # advertise high-res capability beyond what Qt already handles.
            "NSHighResolutionCapable": True,
            "NSPrincipalClass": "NSApplication",
        },
    )

