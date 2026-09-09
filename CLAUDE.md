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

Phase 4 — Render and align. Rasterising sheets into tiles, computing
the transform that maps the old sheet onto the new one, and the
lightbox viewer. No change detection yet — that is Phase 5.

Phase 3 (matching & rename) is built and its rules still apply to that
code: renames never touch the input folders and always write a full,
verified undo log; matching is a global assignment problem; nothing
below the confidence threshold is auto-applied. Phase 2 (intake &
register) likewise: scan progressively, cache by (path, size, mtime),
every drawing number records HOW it was found, a missing drawing in a
partial issue is not a deletion, and the app never writes into the
input folders.

## Phase 4 rules

- Refusing to align is a correct outcome. A confident wrong transform
  is the worst possible failure. When in doubt, return `failed`.
- Default transform model is SIMILARITY (translate, rotate, uniform
  scale). Escalate to affine only for detected scanned sheets.
  Never use homography unless the user explicitly enables it.
- All tolerances and errors are expressed in millimetres on paper AND
  in real-world millimetres at drawing scale. Never report pixels to
  the user.
- Render tiles losslessly. JPEG artifacts create false differences.
- Comparison DPI default is 200. Never hard-code it.
- Every alignment result records its method, its quality metrics, and
  its verdict. The user must be able to see why the app trusted it.

## Known real-world facts (learned the hard way, keep in mind)

- **A PDF page box is NOT always at the origin.** The Lami Architects
  fixture has a mediabox of (-1192, -842, 1192, 842). Always compute
  zones from `page.get_mediabox()`, never from `(0, 0, width, height)`.
- **Title block values sit BELOW their label as often as to the right.**
  `Drawing No.` -> value below; `Scale` -> value to the right. Weight the
  off-axis offset when matching, or the neighbouring cell wins on a tie
  and every sheet reads as the project number.
- **Label order is priority order.** "drawing title" must beat
  "description", or a materials legend headed DESCRIPTION is mistaken
  for the title block.
- **pdfium is NOT thread-safe.** Under concurrent use it reports good
  PDFs as `Data format error`, so valid drawings get quarantined as
  damaged. Every document goes through
  `engine/utils/pdf_runtime.open_document`. Parallelism comes from the
  process pool, where each worker has its own pdfium.
- **PyInstaller + multiprocessing needs `freeze_support()` first thing
  in `main()`**, or every pool worker opens its own application window.
- **Uvicorn waits for open connections before running lifespan
  shutdown.** Long-lived readers must watch
  `engine.core.events.shutdown_requested`, which `EngineServer.stop()`
  raises *before* setting `should_exit`.
- **Register literal API routes before parameterised ones.**
  `/api/scan/{side}` declared first swallows `/api/scan/cancel`.
