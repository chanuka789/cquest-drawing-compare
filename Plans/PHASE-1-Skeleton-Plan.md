# Phase 1 — Skeleton
### Build plan for use with Claude Code

**Project:** `D:\GitHub\cquest-drawing-compare`
**Goal of Phase 1:** A real Windows application window that opens, talks to its own backend, has a database, and shows the Setup screen. No drawing logic yet.
**Time:** About one week part-time. Eight sessions.

---

## Part A — Before you start Phase 1

Five things to do first. About 45 minutes.

### A1. Install Claude Code

Open PowerShell and run:

```powershell
irm https://claude.ai/install.ps1 | iex
```

This is the native installer, which Anthropic now recommends over npm. It auto-updates in the background.

Close PowerShell, open a new one, then verify:

```powershell
claude --version
claude doctor
```

`claude --version` should print something like `2.1.211 (Claude Code)`.

You already have Git for Windows installed, which is good — it lets Claude Code use Bash for shell commands instead of PowerShell.

**Note:** Claude Code needs a Pro, Max, Team, or Console account. The free plan does not include it. On first run, `claude` will open your browser to log in.

### A2. Create a working branch

Never build a phase on `main`. If it goes wrong, you throw away the branch, not your work.

```powershell
cd D:\GitHub\cquest-drawing-compare
git checkout -b phase-1-skeleton
```

### A3. Create your `.env` file

```powershell
New-Item -Path .env -ItemType File
```

Put this inside it (open in VS Code):

```
CQDC_DEV=1
ANTHROPIC_API_KEY=
CQDC_LOG_LEVEL=DEBUG
```

Leave the API key empty for now. You do not need it until Phase 7.

Confirm `.env` is in your `.gitignore`. It already is, but check.

### A4. Create a fixtures folder OUTSIDE the repository

```powershell
mkdir D:\GitHub\_cqdc-fixtures
mkdir D:\GitHub\_cqdc-fixtures\old
mkdir D:\GitHub\_cqdc-fixtures\new
```

Put a few real PDF drawings in `old` and `new` — even just three or four for now. You need something to point the folder pickers at in Task 1.7.

Keeping them outside the repo means there is no way to accidentally commit client drawings.

### A5. Create `CLAUDE.md` — the most important step

This file is Claude Code's memory of your project. It loads into every session. Without it, Claude Code guesses your conventions and you spend your time correcting it.

Create `CLAUDE.md` in the project root with this content:

```markdown
# C-Quest Drawing Compare

Windows desktop application for construction drawing set reconciliation
and revision comparison. Used by quantity surveyors and document controllers.

## Architecture

- **Engine:** Python 3.13, FastAPI, runs on 127.0.0.1 on a random free port
- **Shell:** pywebview (WebView2) — provides the native window
- **UI:** React 18 + TypeScript + Vite, in `ui/`
- **Database:** SQLite via SQLAlchemy, one file per project
- **Packaging:** PyInstaller → single Windows .exe

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
- Build exe: `pyinstaller packaging/build.spec`

## Current phase

Phase 1 — Skeleton. Building the shell, database, and Setup screen only.
No PDF processing, no comparison logic yet. Do not add features from
later phases even if they seem easy.
```

Commit it:

```powershell
git add CLAUDE.md .env.example
git commit -m "Add CLAUDE.md project instructions"
```

---

## Part B — Key decisions Claude Code needs to be told

These are the four things Claude Code will get wrong if you do not specify them. Read this section before Task 1.5.

### B1. Threading — this is the classic pywebview mistake

On Windows, `webview.start()` **must run on the main thread**. Uvicorn must therefore run on a background thread.

```
main thread    →  webview.start()   (blocks until window closes)
background     →  uvicorn.run()     (daemon thread)
```

If you get this backwards, the window either never appears or freezes.

### B2. Port — never hard-code 8000

Ask the operating system for a free port, then tell the window which one to use:

```python
import socket

sock = socket.socket()
sock.bind(("127.0.0.1", 0))
port = sock.getsockname()[1]
sock.close()
```

Hard-coding a port means the app fails if the user already has something on 8000. That is a bad first impression.

### B3. Two channels, two jobs

| Channel | Used for | Why |
|---|---|---|
| **HTTP / WebSocket** (FastAPI) | All data: projects, register, changes, progress | Testable, typed, works in a browser during development |
| **`js_api` bridge** (pywebview) | Native OS dialogs ONLY — folder picker, file picker, save dialog | The browser cannot return a real folder path. Only the native shell can. |

This split matters. Keep everything on HTTP except the dialogs. It means you can develop and test the whole UI in Chrome, without launching the desktop window every time.

The folder picker specifically:

```python
webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
```

### B4. Dev mode vs production mode

| | Dev (`CQDC_DEV=1`) | Production |
|---|---|---|
| UI comes from | Vite server at `localhost:5173` | Built files in `ui/dist`, served by FastAPI |
| Hot reload | Yes | No |
| CORS | Needed (different ports) | Not needed (same origin) |

Build both paths in Phase 1. If you leave production mode until Phase 8, packaging will be painful.

### B5. Clean shutdown

When the user closes the window, the Python process must fully exit. Register an `on_closed` handler that stops the Uvicorn server. Without this, closing the window leaves a Python process running in the background forever.

---

## Part C — The eight tasks

Work through these in order. **One task per Claude Code session.** After each task: test it, commit it, then run `/clear` before starting the next.

---

### Task 1.1 — Folder skeleton

**Goal:** Every folder and `__init__.py` from the architecture exists, plus `pyproject.toml` and `ruff` config.

**Prompt for Claude Code:**

> Create the folder skeleton for this project exactly as described in CLAUDE.md and the architecture below. Create empty `__init__.py` files in every Python package. Do not write any logic yet — only the structure, `pyproject.toml` with ruff configured (line length 100, target py313), and a `.gitignore` check.
>
> [paste the `engine/` tree from your plan document here]

**Test:** `ruff check .` runs without error.

**Commit:** `Add project folder skeleton`

---

### Task 1.2 — Settings, paths, and logging

**Goal:** The app knows where it lives and writes proper logs.

**Prompt:**

> Build three modules:
>
> 1. `engine/settings.py` — Pydantic Settings class reading from `.env`. Fields: `dev_mode` (bool, from `CQDC_DEV`), `log_level`, `anthropic_api_key` (optional), `app_name`, `version`.
> 2. `engine/storage/paths.py` — resolves and creates app data directories under `%LOCALAPPDATA%\CQuest\DrawingCompare\` with subfolders `db`, `logs`, `cache`, `profiles`. Must work when running from a PyInstaller bundle, so do not rely on `__file__` for the app data path.
> 3. `engine/utils/logging_setup.py` — loguru configured with a rotating file sink in the logs folder (10 MB, keep 5 files) plus a console sink. Log level from settings.
>
> Also create `engine/utils/longpath.py` with a helper that adds the `\\?\` prefix to absolute Windows paths longer than 240 characters, and a `safe_open` wrapper. Include a docstring explaining why this exists.

**Test:** A small script that imports paths and prints them; confirm the folders appear in `%LOCALAPPDATA%`.

**Commit:** `Add settings, app paths, and logging`

---

### Task 1.3 — Database

**Goal:** A SQLite project file is created with the full schema.

**Prompt:**

> Build the database layer using SQLAlchemy 2.0 declarative style with type annotations:
>
> - `engine/storage/schema.py` — the tables below
> - `engine/storage/db.py` — engine creation, session factory, `create_project_db(path)` and `open_project_db(path)` functions
> - Enable `PRAGMA foreign_keys=ON` and `PRAGMA journal_mode=WAL` on connect
> - Store a `schema_version` value in a `meta` table so future migrations are possible
>
> [paste the SQL schema from the plan document]
>
> For Phase 1, only `project`, `file`, `sheet`, and `meta` need to be usable. Create the other tables but leave them unused.

**Test:** `pytest` with a test that creates a temporary project database and inserts one project row.

**Commit:** `Add SQLite schema and database layer`

---

### Task 1.4 — FastAPI application

**Goal:** A working API with health, version, and proper error handling.

**Prompt:**

> Build the FastAPI application:
>
> - `engine/api/app.py` — creates the app, sets up CORS (allow `http://localhost:5173` only when `dev_mode` is true), registers exception handlers, mounts routers
> - `engine/utils/errors.py` — base `AppError` plus `NotFoundError`, `ValidationError`, `UnreadableFileError`, `AlignmentFailedError`. Each has a `code` and a user-facing `message`
> - Exception handler that turns any `AppError` into `{"error": {"code": ..., "message": ..., "detail": ...}}` with the right HTTP status
> - `engine/api/routes_system.py` with `GET /api/health` returning status, version, dev_mode, and app data path
> - In production mode, mount `ui/dist` as static files at `/` with an SPA fallback so refreshing does not 404
>
> Do not add any other routes yet.

**Test:** `uvicorn engine.api.app:app --reload`, then open `http://127.0.0.1:8000/api/health` in a browser.

**Commit:** `Add FastAPI app with health endpoint and error handling`

---

### Task 1.5 — The pywebview shell ★ Hardest task

**Goal:** `python -m engine.main` opens a real window with your UI in it.

**Prompt:**

> Build `engine/main.py`, the application entry point. Requirements:
>
> 1. Find a free port by binding to `127.0.0.1:0` and reading the assigned port. Never hard-code a port.
> 2. Start Uvicorn on that port in a **daemon background thread**. `webview.start()` must run on the **main thread** — this is required on Windows.
> 3. Wait for the server to respond to `/api/health` before opening the window, with a timeout of 15 seconds and a clear error if it fails.
> 4. Window URL: if `dev_mode` is true, load `http://localhost:5173`. Otherwise load `http://127.0.0.1:{port}`.
> 5. Window: title "C-Quest Drawing Compare", 1440×900, minimum 1100×700, background `#14181C`.
> 6. Expose a `js_api` class with these methods only:
>    - `pick_folder(title)` → returns a folder path string or None
>    - `pick_file(title, file_types)` → returns a file path or None
>    - `get_api_port()` → returns the port so the frontend knows where to call
>    All other communication goes over HTTP, not through `js_api`.
> 7. Register an `on_closed` handler that shuts down Uvicorn cleanly so the Python process fully exits.
> 8. Log startup, chosen port, dev/production mode, and shutdown.
>
> Write it so it fails loudly with a readable message if WebView2 is missing.

**Test:**
```powershell
# Terminal 1
cd ui; npm run dev
# Terminal 2
python -m engine.main
```
The window opens showing the Vite starter page. Close it — check Task Manager to confirm no `python.exe` is left running.

**Commit:** `Add pywebview shell with dev and production modes`

---

### Task 1.6 — Frontend foundation

**Goal:** Design tokens, a typed API client, and proof the UI can reach the backend.

**Prompt:**

> Set up the frontend foundation in `ui/src`:
>
> 1. `styles/tokens.css` — CSS custom properties for the design system in CLAUDE.md. Include the room scale, sheet colours, text scale, brand, status colours, a spacing scale, radii, and two shadows. Add a comment block at the top stating the rule that brand red is chrome-only.
> 2. `styles/base.css` — reset, Funnel Sans as the body font with a system fallback stack, `tabular-nums` utility class, visible keyboard focus ring using brand red, and `prefers-reduced-motion` handling.
> 3. `api/client.ts` — typed fetch wrapper. Reads the API port from `window.pywebview.api.get_api_port()` when running inside the desktop shell, and falls back to `http://127.0.0.1:8000` in the browser. Handles the structured error shape from the backend and throws a typed `ApiError`.
> 4. `api/types.ts` — TypeScript types matching the Pydantic models.
> 5. `lib/native.ts` — small wrapper around `window.pywebview.api` that detects whether the desktop bridge exists, so the app degrades gracefully in a plain browser.
> 6. `store/appStore.ts` — Zustand store with connection status and app info.
> 7. Replace `App.tsx` with a temporary screen that calls `/api/health` and shows the result.
>
> Do not use Tailwind. Write plain CSS with the tokens.

**Test:** In the browser at `localhost:5173`, you see the health response. Then run `python -m engine.main` and see the same thing inside the window.

**Commit:** `Add design tokens, API client, and native bridge`

---

### Task 1.7 — The Setup screen ★ The one that matters

**Goal:** The real first screen, with the two folder pickers and the seam.

**Prompt:**

> Build the Setup screen at `ui/src/screens/Setup/`. This is the first screen users see, so it carries the visual identity.
>
> Layout: two large panels side by side, separated by a thin vertical seam. Left panel is "Previous issue", right panel is "Current issue". Below them, an optional drawing list file selector. Below that, a sheet profile dropdown and a tolerance dropdown. Bottom right, the primary action button "Build the register", disabled until both folders are chosen.
>
> Behaviour:
> - Clicking a panel calls `native.pickFolder()` and shows the chosen path
> - Once chosen, the panel shows: folder name, full path (truncated in the middle, with the full path as a tooltip), and a placeholder for file count
> - A small "Change" control to pick a different folder
> - The seam is a 2px vertical line in brand red with a soft glow
> - When the second folder is chosen, the seam animates once — a single pulse from top to bottom, about 600ms. This is the only non-user-triggered animation in the app. Respect `prefers-reduced-motion`.
> - Full keyboard access: both panels reachable by Tab, Enter opens the dialog, visible focus ring
>
> Copy rules: sentence case only, no ALL-CAPS labels. Empty state reads "Drop a folder here or browse". Errors say what to do, not just what failed.
>
> Do not implement the folder scan yet. The button should log its intent and do nothing else. That is Phase 2.

**Test:** Point both pickers at `D:\GitHub\_cqdc-fixtures\old` and `\new`. Paths appear. Seam pulses once. Button becomes active.

**Commit:** `Add Setup screen with folder pickers`

---

### Task 1.8 — Tests and the packaged build

**Goal:** Prove it builds into an `.exe` that a client could run.

**Prompt:**

> Two things:
>
> 1. Set up pytest properly — `tests/conftest.py` with fixtures for a temporary app data directory and a temporary project database. Write tests for: settings loading, path creation, long path helper, database creation, and the health endpoint using FastAPI's TestClient.
>
> 2. Create `packaging/build.spec` for PyInstaller. It must bundle the built `ui/dist` folder as data, include the app icon, produce a one-folder build (not one-file — it starts faster and is easier to debug), and set `console=False`. Add a `packaging/build.ps1` script that runs `npm run build` in `ui/`, then PyInstaller, then reports the output path.

**Test:**
```powershell
.\packaging\build.ps1
```
Then find the `.exe` in `dist/`, and **run it with `CQDC_DEV` unset**. The window must open and load the built UI, not the Vite server.

**Commit:** `Add tests and PyInstaller build`

---

## Part D — Definition of done

Phase 1 is complete when all of these are true:

- [ ] `python -m engine.main` opens a window and loads the Setup screen
- [ ] Both folder pickers open a real Windows folder dialog and return real paths
- [ ] The seam pulses once when the second folder is chosen
- [ ] `/api/health` responds and the UI displays it
- [ ] A SQLite database file is created under `%LOCALAPPDATA%\CQuest\DrawingCompare\db\`
- [ ] Log files appear under `...\logs\` and rotate
- [ ] Closing the window leaves no `python.exe` running
- [ ] `pytest` passes
- [ ] `ruff check .` is clean
- [ ] `.\packaging\build.ps1` produces an `.exe` that runs without Vite and without a terminal window
- [ ] Nothing outside Phase 1 scope was added

Then:

```powershell
git checkout main
git merge phase-1-skeleton
git tag v0.1.0-skeleton
```

Push in GitHub Desktop.

---

## Part E — How to work with Claude Code on this project

**One task, one session.** Run `/clear` between tasks. A long session fills with old context and the quality drops.

**Read the diff before you accept it.** You are the engineer, Claude Code is the typist. If you accept code you do not understand, you cannot debug it in Phase 4 when alignment fails.

**Commit after every task.** Small commits mean you can always go back one step instead of losing a day.

**When it goes wrong, say what is wrong, not "fix it".** "The window opens but stays white, and the console shows a CORS error" gets a fix. "It does not work" gets a guess.

**Point it at CLAUDE.md when it drifts.** If it adds a feature from Phase 4, or hard-codes a colour, say: "Check CLAUDE.md — brand red is chrome-only" and it will correct itself.

**Update CLAUDE.md as you learn.** When you make a decision — a library choice, a naming rule, a gotcha you hit — add it. The file gets more valuable every week.

---

## Final recommendation

Do not skip Task 1.5. It is the hardest task in Phase 1 and everything else sits on top of it. The threading rule, the dynamic port, and the clean shutdown are the three things that quietly break desktop Python apps, and they are much easier to get right now, with 200 lines of code, than in Phase 6 with 6,000.

Task 1.7 is where you will want to spend extra time on polish. That screen is the first thing any client sees. Everything after it can be quiet and functional.

When Phase 1 is done and the `.exe` runs, come back and I will plan Phase 2 — the folder scan and drawing register, which is your first genuinely useful and sellable output.
