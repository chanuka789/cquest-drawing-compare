# C-Quest Drawing Compare

Windows desktop application for construction drawing set reconciliation and
revision comparison, for quantity surveyors and document controllers.

**Local-first.** Drawings are processed on the machine they sit on. AI is
optional, off by default, and every feature works without it.

Current state: **Phase 2 — Intake & register.** Point it at two issue folders
and it reads every drawing, works out what is new, missing, revised and
unchanged, and exports a register you can send to the design team.

No image rendering, alignment or overlay comparison yet — those are Phase 4
and later.

---

## What is here

| Path | What it is |
|---|---|
| `engine/` | Python backend: FastAPI API, SQLite storage, the pywebview shell |
| `engine/ingest/` | Folder scanning, PDF inspection, hashing, quarantine |
| `engine/titleblock/` | Reading the drawing number, title, revision and scale |
| `engine/register/` | Revision logic, reconciliation, drawing list import |
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


---

## What Phase 2 does

1. **Scan** both issue folders. The fast pass lists the files in under two
   seconds without opening a single PDF; the deep pass then opens each one in
   a process pool, streaming progress, and the rows fill in as it goes.
   Re-scanning an unchanged folder is instant, because results are cached by
   path, size and modified time.
2. **Identify** each sheet from its own title block — drawing number, title,
   revision, scale — and record **how** the number was found, so a reader can
   judge which rows to trust. A disagreement between the title block and the
   file name is flagged.
3. **Reconcile** the two sets into a register: revised, unchanged, new, not
   reissued, removed, same revision but a different file, unidentified,
   unreadable, and the drawing-list cross-checks.
4. **Export** an Excel workbook with a summary you can paste into an email, the
   full register, a "needs attention" sheet, and the quarantine list — plus a
   JSON audit log of exactly what was run.

### Two rules that shape the answers

**A partial issue is normal.** Consultants reissue the twelve sheets that
changed, not all three hundred. When the two sets are very different sizes and
nobody has said which kind of issue this is, the application asks rather than
guessing — because reporting "288 drawings removed" on a partial issue makes a
correct tool look broken.

**Same revision, different file.** When the revision matches but the content
hash does not, somebody reissued a drawing without bumping the revision. It is
easy to miss by hand, and it is flagged prominently.
