# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: builds dist/cratekeeper.app for the current architecture.

Run via scripts/build_macos.sh (which also makes the .icns, signs, smoke-tests
and wraps the bundle in a DMG), or directly:

    pyinstaller --noconfirm --clean packaging/cratekeeper.spec
"""

import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
sys.path.insert(0, ROOT)
from cratekeeper import __version__  # noqa: E402

ICON = os.path.join(ROOT, "build", "icon.icns")
if not os.path.exists(ICON):
    ICON = None

datas = [(os.path.join(ROOT, "cratekeeper", "web"), os.path.join("cratekeeper", "web"))]
binaries = []
hiddenimports = [
    "cratekeeper.app",
    "cratekeeper.cli",
    "cratekeeper.actions",
    "sqlcipher3",
    "sqlcipher3.dbapi2",
    "_watchdog_fsevents",
    "watchdog.observers.fsevents",
    "watchdog.observers.kqueue",
    "watchdog.observers.polling",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

# pyrekordbox pulls in sqlalchemy + numpy and resolves table classes lazily;
# collect everything so the frozen app matches a normal install.
for pkg in ("pyrekordbox", "mutagen", "webview"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
hiddenimports += collect_submodules("sqlalchemy.dialects.sqlite")
hiddenimports += collect_submodules("anyio")

a = Analysis(
    [os.path.join(SPECPATH, "entry.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter", "_tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "gi",
        "matplotlib", "pandas", "scipy", "IPython", "jupyter", "pytest",
        "qtpy", "clr", "cefpython3",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="cratekeeper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # windowed: launching from Finder must not open Terminal
    argv_emulation=False,
    target_arch=None,  # native arch of the build machine (CI builds both)
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="cratekeeper")

app = BUNDLE(
    coll,
    name="cratekeeper.app",
    icon=ICON,
    bundle_identifier="com.cratekeeper.app",
    version=__version__,
    info_plist={
        "CFBundleName": "cratekeeper",
        "CFBundleDisplayName": "cratekeeper",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "LSMinimumSystemVersion": "11.0",
        "LSApplicationCategoryType": "public.app-category.music",
        "NSHighResolutionCapable": True,
        "NSSupportsAutomaticGraphicsSwitching": True,
        "NSHumanReadableCopyright": "MIT License",
        # Scanning a music folder under ~/Music, ~/Documents, external drives
        # etc. triggers TCC prompts; these strings are what macOS shows.
        "NSDocumentsFolderUsageDescription": "cratekeeper indexes the music folder you choose.",
        "NSDownloadsFolderUsageDescription": "cratekeeper indexes the music folder you choose.",
        "NSDesktopFolderUsageDescription": "cratekeeper indexes the music folder you choose.",
        "NSRemovableVolumesUsageDescription": "cratekeeper indexes music on external drives you choose.",
        "NSNetworkVolumesUsageDescription": "cratekeeper indexes music on network drives you choose.",
        "NSAppleEventsUsageDescription": "cratekeeper hands tracks to Mixed In Key for analysis.",
    },
)
