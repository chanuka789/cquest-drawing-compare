# PyInstaller specification for C-Quest Drawing Compare.
#
# One-folder build, not one-file. A one-file build unpacks itself to a
# temporary directory on every launch, which is slower to start and much
# harder to debug when a client reports a problem. Inno Setup packages the
# folder into a single installer in Phase 8, so the user never sees it.
#
# Run it with:  pyinstaller packaging/build.spec   (or packaging\build.ps1)

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

SPEC_DIR = Path(SPECPATH).resolve()
PROJECT_ROOT = SPEC_DIR.parent

APP_NAME = "C-Quest Drawing Compare"
ICON = SPEC_DIR / "assets" / "icon.ico"
UI_DIST = PROJECT_ROOT / "ui" / "dist"

if not UI_DIST.is_dir():
    raise SystemExit(
        f"The built UI is missing at {UI_DIST}.\n"
        "Run 'npm run build' inside ui/ first, or use packaging\\build.ps1 "
        "which does both steps in order."
    )

# The built frontend must land at ui/dist inside the bundle, because
# engine.storage.paths.bundle_root() looks for it there in a frozen build.
datas = [(str(UI_DIST), "ui/dist")]

profiles = PROJECT_ROOT / "profiles"
if any(profiles.glob("*.json")):
    datas.append((str(profiles), "profiles"))

# Phase 3: shipped naming template presets, next to the sheet profiles.
naming_templates = PROJECT_ROOT / "naming_templates"
if any(naming_templates.glob("*.json")):
    datas.append((str(naming_templates), "naming_templates"))

# Uvicorn and pywebview load these by name at runtime, so static analysis
# cannot see them.
hiddenimports = [
    *collect_submodules("uvicorn"),
    *collect_submodules("webview.platforms"),
    "engine.api.routes_system",
    "engine.api.routes_naming",
    "engine.api.routes_changes",
]

a = Analysis(
    [str(PROJECT_ROOT / "engine" / "main.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Kept out deliberately: heavy libraries Phase 1 does not use. Remove an
    # entry from this list in the phase that actually needs it.
    excludes=["tkinter", "matplotlib", "torch", "sentence_transformers", "pytest"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # no console window; loguru writes the log files
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
