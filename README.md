# C-Quest Drawing Compare

Windows desktop application for construction drawing set reconciliation and
revision comparison, for quantity surveyors and document controllers.

**Local-first.** Drawings are processed on the machine they sit on. AI is
optional, off by default, and every feature works without it.

Current state: **Phase 1 — Skeleton.** The shell, the database and the Setup
screen. No PDF processing or comparison logic yet.

---

## What is here

| Path | What it is |
|---|---|
| `engine/` | Python backend: FastAPI API, SQLite storage, the pywebview shell |
| `ui/` | React + TypeScript frontend built with Vite |
| `packaging/` | PyInstaller spec, build script, application icon |
| `profiles/` | Shipped client sheet profiles (Phase 2) |
| `tests/` | pytest suite |
| `Plans/` | Product and phase plans |

Application data — database, logs, cache — lives outside the repository at
`%LOCALAPPDATA%\CQuest\DrawingCompare\`.

---

## Daily start-up

```powershell
cd D:\GitHub\cquest-drawing-compare
.\.venv\Scripts\Activate.ps1
code .
```

Then two terminals:

```powershell
python -m engine.main
```

```powershell
cd ui
npm run dev
```

`python -m engine.main` opens the desktop window. With `CQDC_DEV=1` it loads
the UI from the Vite dev server, so the frontend hot-reloads inside the real
window.

### Developing in a browser instead

Everything except the native folder and file dialogs works over HTTP, so the
whole UI can be built in Chrome:

```powershell
python -m uvicorn engine.api.app:app --reload --port 8000
```

The API client falls back to `http://127.0.0.1:8000` when the desktop bridge
is absent. Folder picking needs the desktop window.

---

## Commands

| Task | Command |
|---|---|
| Run the app | `python -m engine.main` |
| Run the API only | `python -m uvicorn engine.api.app:app --reload --port 8000` |
| Run the frontend | `cd ui` then `npm run dev` |
| Tests | `pytest` |
| Lint and format | `ruff check . && ruff format .` |
| Type-check the UI | `cd ui` then `npx tsc -b` |
| Build the `.exe` | `.\packaging\build.ps1` |

The build script builds the frontend first, then runs PyInstaller, and prints
where the application landed. It produces a one-folder build in `dist\`.

---

## Configuration

Copy `.env.example` to `.env`. Nothing in it is required.

| Variable | Meaning |
|---|---|
| `CQDC_DEV` | `1` loads the UI from Vite; unset or `0` loads the built UI |
| `CQDC_LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `CQDC_APP_DATA` | Overrides the app data folder (the test suite uses this) |
| `ANTHROPIC_API_KEY` | Optional, not used before Phase 7 |

---

## Rules that shape the code

These are enforced in review, and explained in `CLAUDE.md`.

1. Local-first. No network calls except AI the user explicitly turned on.
2. No PyMuPDF / `fitz` — it is AGPL. `pypdfium2` and `pikepdf` only.
3. The user's original drawings are never modified. Renames work on copies
   and are reversible.
4. Never guess a quantity or a cost. Low confidence returns a candidate for
   the user to confirm.
5. Tolerances are stored in millimetres at drawing scale, never in pixels.
6. Brand red `#CF0A2C` appears only in the application chrome. Inside the
   drawing canvas, red already means "removed".
7. Every feature must work with AI switched off.

---

## Licence

See `LICENSE.txt`. Commercial product — check the licence terms of every
dependency before shipping.
