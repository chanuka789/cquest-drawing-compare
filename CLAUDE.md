# C-Quest Drawing Compare

Windows desktop application for construction drawing set reconciliation
and revision comparison. Used by quantity surveyors and document controllers.

## Architecture

- **Engine:** Python (FastAPI), runs on 127.0.0.1 on a random free port
- **Shell:** pywebview (WebView2) — provides the native window
- **UI:** React + TypeScript + Vite, in `ui/`
- **Database:** SQLite via SQLAlchemy, one file per project
- **Packaging:** PyInstaller → Windows .exe (one-folder build)

Installed toolchain on this machine (checked 1 Sep 2026): Python 3.14.4,
Node 24.14.1, React 19, Vite 8, TypeScript 6. The original plan targeted
Python 3.13 / React 18; the newer versions are installed and working.
Ruff `target-version` stays at `py313` so the code remains 3.13-compatible.

## Hard rules — do not break these

1. **Local-first.** All drawing processing happens on this machine.
   No network calls except optional, explicit AI calls the user turned on.
2. **No PyMuPDF / fitz.** It is AGPL and this is a commercial product.
   Use `pypdfium2` and `pikepdf` only.
3. **Never modify the user's original drawing files.** Read-only always.
   Renames operate on copies and must be reversible.
4. **Never guess a quantity or a cost.** If confidence is low, return
   a candidate for the user to confirm, not an answer.
5. **All UI text is sentence case.** No ALL-CAPS labels.
6. **Money and tolerances:** tolerances are stored in millimetres at
   drawing scale, never in pixels.
7. **AI is optional.** Every feature must work with AI switched off.

## Code conventions

- Python: type hints everywhere. Pydantic models for anything crossing
  the API boundary. `ruff` for lint and format. Line length 100.
- Routers in `engine/api/` stay thin — they validate input, call a
  service in `engine/core/` or a module, and return. No logic in routers.
- Long-running work never blocks the API. Use the job queue.
- Logging with `loguru`. Never use `print()`.
- Errors: raise typed exceptions from `engine/utils/errors.py`.
  The API layer converts them to structured JSON.
- TypeScript: strict mode. No `any`. State in Zustand stores.
- CSS: use the tokens in `ui/src/styles/tokens.css`. No hard-coded colours.

## Design system

- Brand red `#CF0A2C` appears ONLY in the application chrome —
  buttons, active states, focus rings, the seam.
- Inside the drawing canvas, use the diff palette only:
  old `#0E63C4`, new `#E8442A`, unchanged 55% grey.
  Brand red must never appear inside the canvas.
- Font: Funnel Sans, one family. Use `font-variant-numeric: tabular-nums`
  for data columns instead of adding a monospace font.
- Dark chrome (`--room-*`), bright sheet (`--sheet`). The drawing is the
  brightest thing on screen.

## Paths

- App data: `%LOCALAPPDATA%\CQuest\DrawingCompare\`
  (database, logs, cache — never inside the repo)
- Test fixtures live OUTSIDE the repo at `D:\GitHub\_cqdc-fixtures\`
- Windows long paths: always route file access through
  `engine/utils/longpath.py`. Real drawing folders exceed 260 characters.

## Commands

- Run backend (dev): `python -m engine.main`
- Run frontend (dev): `cd ui && npm run dev`
- Tests: `pytest`
- Lint: `ruff check . && ruff format .`
- Build exe: `pyinstaller packaging/build.spec` (or `.\packaging\build.ps1`)

## Current phase

Phase 1 — Skeleton. Building the shell, database, and Setup screen only.
No PDF processing, no comparison logic yet. Do not add features from
later phases even if they seem easy.
