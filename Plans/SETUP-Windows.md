# C-Quest Drawing Compare — Windows Setup Guide

**Do this once, before Phase 1.** Total time: about 60–90 minutes, mostly downloads.

Version information below was checked on 1 September 2026. Versions move, so if a number looks different on the website, take the website as correct.

---

## 0. Quick checklist

| # | Software | Version to install | Required? | Size |
|---|---|---|---|---|
| 1 | Python | **3.13.x** | Yes | ~30 MB |
| 2 | Node.js | **24.x LTS** | Yes | ~50 MB |
| 3 | Git for Windows | Latest | Yes | ~60 MB |
| 4 | VS Code | Latest | Yes | ~100 MB |
| 5 | WebView2 Runtime | Latest | Check first — usually already there | ~150 MB |
| 6 | Windows long path support | Registry change | Yes | — |
| 7 | Visual C++ Build Tools | 2022 | Only if a package fails to build | ~2 GB |
| 8 | Tesseract OCR | 5.x | Later (Phase 5) | ~60 MB |
| 9 | Ollama | Latest | Later (Phase 7), optional | ~600 MB |
| 10 | Inno Setup | 6.x | Later (Phase 8) | ~5 MB |

Install items 1–6 now. Leave 7–10 for later.

---

## 1. Python 3.13

### Which version, and why not the newest

The newest Python is 3.14. **Install 3.13 instead.**

The reason: this project uses OpenCV, and later PyTorch (for the local embedding model). Scientific and machine-learning packages are usually the last to publish ready-made Windows builds for a brand-new Python version. Python 3.13 has full support everywhere. Python 3.14 might work, but if it does not, you will lose a day fighting build errors instead of writing your app.

I am not fully sure whether PyTorch has published Windows builds for Python 3.14 yet — please check before you decide to use it. Python 3.13 is the safe choice today.

### Install

1. Go to https://www.python.org/downloads/windows/
2. Under **Python 3.13.x**, download **Windows installer (64-bit)**
3. Run the installer
4. ⚠ **On the first screen, tick "Add python.exe to PATH"** before clicking Install. This is the single most common mistake.
5. Choose **Customize installation** → tick everything → Next
6. Tick **"Install Python 3.13 for all users"** and **"Add Python to environment variables"**
7. Install

### Verify

Open **PowerShell** (press `Win`, type `powershell`, Enter):

```powershell
python --version
pip --version
```

You should see something like:
```
Python 3.13.15
pip 25.x from C:\Python313\Lib\site-packages\pip (python 3.13)
```

If `python` opens the Microsoft Store instead, the PATH was not set. Reinstall and tick the PATH box.

---

## 2. Node.js 24 LTS

React and Vite need Node. Install the **LTS** version, not the newest.

As of now, **Node.js 24 is the Active LTS line**. Node.js 26 exists but is still the "Current" line and only becomes LTS in October 2026. Use 24 for this project.

1. Go to https://nodejs.org/en/download
2. Choose the **LTS** tab, Windows, x64, `.msi` installer
3. Run it, accept the defaults
4. There is a checkbox for "Tools for Native Modules" — you can leave it **unticked** for now

### Verify

```powershell
node --version
npm --version
```

Expect `v24.x.x` and npm 11 or higher.

---

## 3. Git for Windows

1. Go to https://git-scm.com/download/win
2. Download and run the 64-bit installer
3. Accept the defaults, except:
   - Default editor → **Visual Studio Code**
   - Default branch name → **main**
   - Line endings → **Checkout as-is, commit Unix-style line endings**

### Verify and configure

```powershell
git --version
git config --global user.name "Chanuka Mudiyanselage"
git config --global user.email "your@email.com"
```

**Important for this project** — allow long file paths in Git:

```powershell
git config --global core.longpaths true
```

Construction drawing folders often have very deep paths. Without this, Git will silently fail on some files.

---

## 4. Visual Studio Code

1. Go to https://code.visualstudio.com
2. Download and install
3. Tick **"Add to PATH"** and **"Open with Code"** context menu options

### Extensions to install

Open VS Code → Extensions panel (`Ctrl+Shift+X`) → search and install:

| Extension | Publisher | Why |
|---|---|---|
| Python | Microsoft | Core support |
| Pylance | Microsoft | Fast type checking |
| Ruff | Astral Software | Linting and formatting |
| ES7+ React snippets | dsznajder | React shortcuts |
| Tailwind CSS IntelliSense | Tailwind Labs | Only if you use Tailwind |
| SQLite Viewer | Florian Klampfer | Inspect your project database |
| Even Better TOML | tamasfe | For `pyproject.toml` |
| GitLens | GitKraken | See file history |

---

## 5. WebView2 Runtime

This is what draws your UI. It is **already installed on Windows 11 and on most Windows 10 machines**, because Microsoft Edge uses it.

### Check first

```powershell
Get-ChildItem "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients" -ErrorAction SilentlyContinue |
  ForEach-Object { (Get-ItemProperty $_.PSPath).name } |
  Where-Object { $_ -like "*WebView2*" }
```

If it prints `Microsoft Edge WebView2 Runtime`, you are done.

If nothing prints, download the **Evergreen Standalone Installer** from:
https://developer.microsoft.com/microsoft-edge/webview2/

**Note for later:** when you ship the app to clients, your installer must check for WebView2 and install it if missing. Add this to the Phase 8 Inno Setup script.

---

## 6. Enable Windows long paths ⚠ Important for this project

Windows normally limits file paths to 260 characters. Real project folders break this all the time:

```
\\server\Projects\2026\UVU Jeddah Tower\03_Drawings\Architectural\IFC\Rev D\Level 03\UVU-KEO-ARC-L03-DR-A-001234-Rev-D-Reflected-Ceiling-Plan.pdf
```

Fix it once, at system level.

Open PowerShell **as Administrator** (right-click → Run as administrator):

```powershell
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" `
  -Name "LongPathsEnabled" -Value 1 -PropertyType DWORD -Force
```

**Restart your computer** for this to take effect.

Your code must still use the `\\?\` prefix in places (that is what `engine/utils/longpath.py` is for), but this registry setting is the necessary first step.

---

## 7. Create the project

Open PowerShell (normal, not admin):

```powershell
# Choose where your code lives — keep the path SHORT
mkdir C:\dev
cd C:\dev
mkdir cquest-drawing-compare
cd cquest-drawing-compare

git init
```

Keep the project path short (`C:\dev\...`). You will be handling long drawing paths inside the app, so do not waste characters on the project folder itself.

---

## 8. Python virtual environment

A virtual environment keeps this project's packages separate from every other Python project. Always use one.

```powershell
python -m venv .venv
```

Now activate it:

```powershell
.\.venv\Scripts\Activate.ps1
```

### If you get a red "running scripts is disabled" error

This is normal on a fresh Windows machine. Run this once:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

Type `Y` and press Enter. Then try activating again.

When it works, your prompt changes to:

```
(.venv) PS C:\dev\cquest-drawing-compare>
```

The `(.venv)` at the front tells you the environment is active. **You must activate it every time you open a new terminal.**

---

## 9. Install Python packages

Create a file called `requirements.txt` in the project folder:

```txt
# ── Web layer ──────────────────────────────────────────
fastapi>=0.115
uvicorn[standard]>=0.32
python-multipart>=0.0.20
pywebview>=5.3
websockets>=13.0

# ── Config & models ────────────────────────────────────
pydantic>=2.9
pydantic-settings>=2.6

# ── Database ───────────────────────────────────────────
SQLAlchemy>=2.0
alembic>=1.14

# ── PDF (permissive licences only) ─────────────────────
pypdfium2>=4.30
pikepdf>=9.4

# ── Image & geometry ───────────────────────────────────
opencv-python>=4.10
numpy>=2.1
Pillow>=11.0
scipy>=1.14
scikit-image>=0.24
shapely>=2.0

# ── CAD ────────────────────────────────────────────────
ezdxf>=1.3

# ── Text matching ──────────────────────────────────────
rapidfuzz>=3.10

# ── Excel / Word / data ────────────────────────────────
pandas>=2.2
openpyxl>=3.1
XlsxWriter>=3.2
python-docx>=1.1

# ── Utilities ──────────────────────────────────────────
xxhash>=3.5
loguru>=0.7
typer>=0.15
httpx>=0.28
python-dateutil>=2.9

# ── AI (cloud) ─────────────────────────────────────────
anthropic>=0.40

# ── Dev tools ──────────────────────────────────────────
pytest>=8.3
pytest-cov>=6.0
ruff>=0.8
pyinstaller>=6.11
```

Install everything:

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

This takes 5–15 minutes. OpenCV and SciPy are large.

### The heavy AI packages — install these later, not now

The local embedding model needs PyTorch, which is about **2.5 GB**. You do not need it until Phase 8. Keep it in a separate file, `requirements-ai.txt`:

```txt
sentence-transformers>=3.3
torch>=2.5
```

Install it only when you reach Phase 8:

```powershell
pip install -r requirements-ai.txt
```

### Note on `pypdfium2` vs `PyMuPDF`

You will see many tutorials using **PyMuPDF** (`fitz`). It is excellent, but it is licensed under **AGPL**, which creates a real problem if you sell a closed-source product. A commercial licence must be bought from Artifex.

`pypdfium2` uses a permissive licence (Apache/BSD-3) and does everything you need for this project. I have specified it for that reason.

**Please verify the current licence terms yourself before you commit to any PDF library.** Licence terms do change, and this decision affects whether you can legally sell the product.

---

## 10. Set up the React frontend

Still in the project folder, with the venv active:

```powershell
npm create vite@latest ui -- --template react-ts
cd ui
npm install
```

Then install the frontend packages this project needs:

```powershell
npm install zustand pixi.js @tanstack/react-virtual lucide-react clsx
npm install -D @types/node
```

Test that it runs:

```powershell
npm run dev
```

Open http://localhost:5173 in your browser. You should see the Vite starter page. Press `Ctrl+C` to stop.

```powershell
cd ..
```

---

## 11. Verify everything works

Create `check_setup.py` in the project root:

```python
"""Run this to confirm the development environment is ready."""

import importlib
import platform
import shutil
import sys

REQUIRED = [
    "fastapi",
    "uvicorn",
    "webview",
    "pydantic",
    "sqlalchemy",
    "pypdfium2",
    "pikepdf",
    "cv2",
    "numpy",
    "PIL",
    "scipy",
    "skimage",
    "shapely",
    "ezdxf",
    "rapidfuzz",
    "pandas",
    "openpyxl",
    "docx",
    "xxhash",
    "loguru",
    "typer",
    "httpx",
    "anthropic",
]

OPTIONAL = ["sentence_transformers", "torch", "pytesseract"]


def check(name):
    try:
        importlib.import_module(name)
        return True
    except ImportError:
        return False


print("=" * 56)
print("  C-Quest Drawing Compare — environment check")
print("=" * 56)
print(f"Python   : {sys.version.split()[0]}")
print(f"Windows  : {platform.platform()}")
print(f"Venv     : {'YES' if sys.prefix != sys.base_prefix else 'NO  <-- ACTIVATE IT'}")
print(f"Node     : {'found' if shutil.which('node') else 'MISSING'}")
print(f"Git      : {'found' if shutil.which('git') else 'MISSING'}")
print("-" * 56)

missing = [p for p in REQUIRED if not check(p)]
for p in REQUIRED:
    print(f"  {'ok ' if check(p) else 'MISSING'}  {p}")

print("-" * 56)
print("Optional (needed from Phase 5 / 8):")
for p in OPTIONAL:
    print(f"  {'ok ' if check(p) else '-  '}  {p}")

print("=" * 56)
if missing:
    print(f"RESULT: {len(missing)} package(s) missing: {', '.join(missing)}")
    print("Fix with: pip install -r requirements.txt")
else:
    print("RESULT: Ready. You can start Phase 1.")
print("=" * 56)
```

Run it:

```powershell
python check_setup.py
```

If it says **Ready**, you are done with setup.

### Quick smoke test — open a real window

Create `smoke_test.py`:

```python
import webview

webview.create_window(
    "C-Quest Drawing Compare",
    html="""
    <body style="margin:0;background:#14181C;color:#EDF1F5;
                 font-family:system-ui;display:grid;place-items:center;height:100vh">
      <div style="text-align:center">
        <div style="width:3px;height:80px;background:#CF0A2C;margin:0 auto 24px;
                    box-shadow:0 0 24px rgba(207,10,44,.5)"></div>
        <h1 style="font-weight:600;margin:0">Environment is working</h1>
        <p style="color:#9DA9B5;margin-top:8px">WebView2 is rendering correctly.</p>
      </div>
    </body>
    """,
    width=900,
    height=600,
)
webview.start()
```

```powershell
python smoke_test.py
```

A dark window with a red seam should appear. **This proves the whole UI approach works on your machine** — Python is running, WebView2 is rendering, and your brand colour looks right on the dark shell. If this works, nothing in Phase 1 will surprise you.

---

## 12. Add a `.gitignore`

```gitignore
# Python
.venv/
__pycache__/
*.pyc
*.egg-info/
build/
dist/

# Node
ui/node_modules/
ui/dist/

# Secrets — never commit
.env

# App data
*.db
*.sqlite3
cache/
logs/
outputs/

# Test fixtures — real drawings must not go to GitHub
tests/fixtures/**/*.pdf
tests/fixtures/**/*.dwg
tests/fixtures/**/*.dxf

# Windows
Thumbs.db
desktop.ini
```

⚠ The fixtures rule matters. Your test drawings will be real client drawings. Even anonymised, they should not be pushed to a public repository. Keep them in a local folder or a private repository only.

Then make your first commit:

```powershell
git add .
git commit -m "Set up development environment"
```

---

## 13. Optional installs — do these later

### Tesseract OCR (Phase 5, only for scanned drawings)

1. Download from https://github.com/UB-Mannheim/tesseract/wiki
2. Install to the default path `C:\Program Files\Tesseract-OCR`
3. Tick **Additional language data** if you expect Arabic title blocks
4. `pip install pytesseract`
5. Verify: `& "C:\Program Files\Tesseract-OCR\tesseract.exe" --version`

### Ollama (Phase 7, for the offline local model)

1. Download from https://ollama.com/download/windows
2. Install and run
3. Pull a small vision model, for example: `ollama pull llama3.2-vision`
4. Your RTX 4060 has enough VRAM for small and medium models. Very large models will be slow.

### Visual C++ Build Tools (only if a `pip install` fails with "Microsoft Visual C++ 14.0 required")

Most packages ship ready-made Windows builds, so you probably will not need this. If you do:

1. https://visualstudio.microsoft.com/visual-cpp-build-tools/
2. Install the **Desktop development with C++** workload only
3. About 2 GB

### Inno Setup (Phase 8, for the installer)

https://jrsoftware.org/isdl.php

---

## 14. Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| `python` opens Microsoft Store | PATH not set during install | Reinstall Python, tick "Add to PATH" |
| "running scripts is disabled" | PowerShell policy | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| `pip` installs to the wrong place | venv not active | Check for `(.venv)` in your prompt |
| `Microsoft Visual C++ 14.0 required` | Package has no prebuilt wheel | Install VC++ Build Tools, or use an older package version |
| pywebview window is blank | WebView2 missing | Install the Evergreen Runtime |
| `npm` not recognised | Node PATH not applied | Close and reopen PowerShell |
| Long path errors on real folders | Registry setting not applied | Enable long paths, then **restart** |
| OpenCV import fails | Conflicting opencv packages | `pip uninstall opencv-python opencv-python-headless` then reinstall one |

---

## 15. Your daily start-up routine

Every time you sit down to work:

```powershell
cd C:\dev\cquest-drawing-compare
.\.venv\Scripts\Activate.ps1
code .
```

Then in VS Code, use two terminals:
- Terminal 1: `python -m engine.main` (the backend)
- Terminal 2: `cd ui` then `npm run dev` (the frontend, with hot reload)

---

## Final recommendation

Do the setup in this exact order and **stop at the smoke test**. Do not start writing project code until the dark window with the red seam appears on your screen.

That single test proves four things at once: Python works, the venv works, pywebview works, and WebView2 renders correctly on your machine. Every hour you spend on Phase 1 after that is building on solid ground.

When the smoke test passes, tell me and I will write the Phase 1 skeleton — the FastAPI server, the pywebview shell, the project database, and the first working screen.
