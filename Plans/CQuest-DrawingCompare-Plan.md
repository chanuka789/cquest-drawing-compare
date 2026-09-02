# C-Quest Drawing Compare — Project Structure & Build Plan

**Product:** Windows desktop application for construction drawing set reconciliation and revision comparison
**Owner:** Chanuka Mudiyanselage
**Processing model:** Local-first. AI is optional and used only where local code cannot do the job.

---

## 1. Direct answer — the recommended technology stack

| Layer | Choice | Why |
|---|---|---|
| Language (engine) | Python 3.12 | All the PDF, geometry and image libraries you need are here |
| Desktop shell | **pywebview** (uses Edge WebView2, already on Windows 10/11) | Lets you build the UI in HTML/CSS/React, which is where your design skill already is. No Rust needed. |
| Local server | FastAPI + Uvicorn on `127.0.0.1` random port | Clean separation between UI and engine; also makes a future web version easy |
| Frontend | React 18 + Vite + TypeScript | Fast, and you can build the exact C-Quest look |
| Canvas rendering | PixiJS (WebGL) or plain Canvas 2D with tiles | Drawings are huge; DOM rendering will not survive |
| Database | SQLite (via SQLAlchemy) | Single file per project, no server, easy backup |
| Job engine | `concurrent.futures.ProcessPoolExecutor` + SQLite job table | Uses all CPU cores, survives restart |
| Packaging | PyInstaller → Inno Setup installer | Standard Windows `.exe` + installer with licence key |

**Why not PySide6/Qt?** It is a fine choice technically, but getting a truly distinctive modern UI out of Qt takes much longer than out of CSS. You already design in the browser every day. Use that advantage.

**Why not Electron or Tauri?** Electron adds ~150 MB and a second runtime. Tauri is excellent but adds Rust to your learning load. pywebview gives you 90% of Tauri's benefit with pure Python. You can migrate to Tauri later without changing the frontend or the engine — only the shell.

---

## 2. Complete project structure

```
cquest-drawing-compare/
│
├── README.md
├── pyproject.toml                    # Python deps, build config
├── .env.example                      # AI keys (never committed)
├── LICENSE.txt
│
├── engine/                           # ═══ PYTHON BACKEND ═══
│   ├── __init__.py
│   ├── main.py                       # Entry point: starts FastAPI, opens pywebview window
│   ├── settings.py                   # App config, paths, defaults
│   │
│   ├── api/                          # ── FastAPI routes (thin, no logic here) ──
│   │   ├── __init__.py
│   │   ├── routes_project.py         # create/open/close project
│   │   ├── routes_ingest.py          # scan folders, build inventory
│   │   ├── routes_register.py        # drawing register, reconciliation
│   │   ├── routes_naming.py          # match names, propose renames, undo
│   │   ├── routes_compare.py         # queue comparisons, get results
│   │   ├── routes_changes.py         # change list, triage, status updates
│   │   ├── routes_boq.py             # BOQ import, mapping, costing
│   │   ├── routes_report.py          # export overlay PDF / Excel / HTML
│   │   ├── routes_ai.py              # AI actions (all opt-in)
│   │   ├── routes_settings.py        # profiles, tolerances, AI mode
│   │   ├── routes_tiles.py           # serve rendered image tiles to canvas
│   │   └── ws_progress.py            # WebSocket: live job progress
│   │
│   ├── core/                         # ── Domain models & orchestration ──
│   │   ├── models.py                 # Pydantic: Drawing, Sheet, Change, Region…
│   │   ├── enums.py                  # ChangeType, Severity, MatchConfidence…
│   │   ├── project.py                # Project lifecycle
│   │   ├── pipeline.py               # Orchestrates the full compare run
│   │   ├── jobs.py                   # Job queue, resume, cancel, retry
│   │   └── events.py                 # Progress event bus → WebSocket
│   │
│   ├── ingest/                       # ── STAGE 1: read the folders ──
│   │   ├── folder_scanner.py         # Recursive scan, long-path safe, junk filter
│   │   ├── file_hasher.py            # MD5/xxhash for true duplicate detection
│   │   ├── pdf_inspector.py          # Page count, size, rotation, encryption, layers
│   │   ├── multipage_splitter.py     # One PDF = many drawings
│   │   └── quarantine.py             # Corrupt / locked / unreadable file log
│   │
│   ├── titleblock/                   # ── Read the sheet's own identity ──
│   │   ├── zone_detector.py          # Find title block strip (right / bottom)
│   │   ├── field_extractor.py        # Drawing no., title, rev, scale, date, sheet size
│   │   ├── patterns.py               # Regex library per client naming standard
│   │   ├── scale_reader.py           # "1:100" text + scale bar detection
│   │   └── ai_fallback.py            # Vision model ONLY when regex fails
│   │
│   ├── register/                     # ── STAGE 2: set reconciliation ──
│   │   ├── list_importer.py          # Drawing list from PDF / Excel / Word
│   │   ├── list_parser.py            # Find the real header row, normalise columns
│   │   ├── reconciler.py             # New / Missing / Revised / Unchanged / Duplicate
│   │   ├── revision_logic.py         # Rev sequence: A→B, P01→P02, C01, T03…
│   │   └── register_export.py        # Excel register with status colours
│   │
│   ├── naming/                       # ── STAGE 3: name matching & rename ──
│   │   ├── normaliser.py             # Strip dates, revs, spaces, prefixes, junk words
│   │   ├── matcher.py                # 3-tier: title block → normalised → fuzzy
│   │   ├── rename_planner.py         # Build proposed rename table + confidence
│   │   ├── rename_executor.py        # Copy-based rename, never touches originals
│   │   └── undo_log.py               # Full reversible log (old, new, time, hash)
│   │
│   ├── extract/                      # ── Get content out of each sheet ──
│   │   ├── text_extractor.py         # Text + X/Y coords + font + size
│   │   ├── vector_extractor.py       # Paths, lines, curves, fills
│   │   ├── layer_extractor.py        # PDF optional content groups (OCG)
│   │   ├── raster_renderer.py        # Render to image at target DPI, tiled
│   │   ├── dxf_reader.py             # ezdxf path when DXF/DWG is available
│   │   └── ocr.py                    # Tesseract, only for scanned sheets
│   │
│   ├── align/                        # ── STAGE 4: the hardest part ──
│   │   ├── grid_bubble_detector.py   # ★ Find A/B/C — 1/2/3 grid circles
│   │   ├── feature_aligner.py        # ORB/SIFT + RANSAC fallback
│   │   ├── titleblock_aligner.py     # Corner-based fallback
│   │   ├── transform.py              # Affine: translate, rotate, scale, mirror
│   │   ├── scale_normaliser.py       # A1↔A3, 1:100↔1:50
│   │   ├── deskew.py                 # For scanned sheets
│   │   └── quality_check.py          # Reject bad alignment before comparing
│   │
│   ├── masking/                      # ── STAGE 5: what to ignore ──
│   │   ├── mask_engine.py            # Apply exclusion zones
│   │   ├── titleblock_mask.py        # Auto-mask the title strip
│   │   ├── watermark_remover.py      # PRELIMINARY / SUPERSEDED stamps
│   │   ├── stamp_detector.py         # Signature blocks, QR, print stamps
│   │   └── profile_store.py          # Saved per-client mask profiles
│   │
│   ├── compare/                      # ── STAGE 6: find the changes ──
│   │   ├── raster_diff.py            # Aligned pixel diff + contours
│   │   ├── vector_diff.py            # Geometry set difference
│   │   ├── text_diff.py              # ★ Dimensions, tags, notes — the money
│   │   ├── dimension_diff.py         # Number changes: 3000 → 3200
│   │   ├── hatch_diff.py             # Material change detection
│   │   ├── tolerance.py              # Tolerance set in REAL mm, not pixels
│   │   └── noise_filter.py           # Line weight, font, colour, anti-alias
│   │
│   ├── classify/                     # ── STAGE 7: make it readable ──
│   │   ├── clusterer.py              # 400 blobs → 12 meaningful regions
│   │   ├── change_typer.py           # Added / Removed / Moved / Modified / Cosmetic
│   │   ├── severity_scorer.py        # Rank by cost impact, not by pixel area
│   │   ├── cloud_detector.py         # Find designer's revision clouds
│   │   ├── cloud_crosscheck.py       # ★ Changed but NOT clouded
│   │   └── ai_classifier.py          # Vision model for ambiguous regions only
│   │
│   ├── boq/                          # ── STAGE 8: link to money ──
│   │   ├── boq_importer.py           # Excel BOQ in
│   │   ├── mapping_table.py          # Drawing tag → BOQ item (project mapping)
│   │   ├── semantic_matcher.py       # Local embeddings (all-MiniLM-L6-v2)
│   │   ├── quantity_estimator.py     # From DXF/vector only, never from pixels
│   │   └── cost_calculator.py        # Qty × rate → variation value
│   │
│   ├── report/                       # ── STAGE 9: output ──
│   │   ├── overlay_pdf.py            # Old/new colour overlay, Bluebeam-style
│   │   ├── sidebyside_pdf.py         # Two-up with change callouts
│   │   ├── register_xlsx.py          # Set reconciliation workbook
│   │   ├── change_register_xlsx.py   # Change list workbook
│   │   ├── variation_docx.py         # Draft variation narrative (AI writing)
│   │   ├── audit_trail.py            # Settings, versions, timestamps — defensible
│   │   └── templates/                # Jinja2 + docx/xlsx templates
│   │
│   ├── ai/                           # ── The optional "brain" ──
│   │   ├── provider.py               # Abstract interface
│   │   ├── anthropic_provider.py     # Claude API (cloud)
│   │   ├── local_provider.py         # Ollama / llama.cpp (offline)
│   │   ├── null_provider.py          # Offline mode — returns "not available"
│   │   ├── embeddings.py             # sentence-transformers, always local
│   │   ├── prompts/                  # Versioned prompt files
│   │   │   ├── classify_region.md
│   │   │   ├── read_titleblock.md
│   │   │   ├── describe_change.md
│   │   │   └── variation_narrative.md
│   │   ├── redaction.py              # ★ Strip client/project data before sending
│   │   ├── crop_policy.py            # ★ Only small crops leave the machine
│   │   ├── cost_meter.py             # Token + money counter shown in UI
│   │   └── cache.py                  # Hash-based, never pay twice
│   │
│   ├── storage/
│   │   ├── db.py                     # SQLAlchemy engine, migrations
│   │   ├── schema.py                 # Tables
│   │   ├── cache_store.py            # Rendered tiles, extraction results
│   │   └── paths.py                  # %LOCALAPPDATA%\CQuest\DrawingCompare
│   │
│   └── utils/
│       ├── longpath.py               # Windows \\?\ prefix handling
│       ├── units.py                  # mm ↔ px ↔ points at scale
│       ├── logging_setup.py
│       └── errors.py
│
├── ui/                               # ═══ REACT FRONTEND ═══
│   ├── index.html
│   ├── vite.config.ts
│   ├── package.json
│   ├── src/
│   │   ├── main.tsx
│   │   ├── App.tsx
│   │   ├── styles/
│   │   │   ├── tokens.css            # C-Quest design tokens
│   │   │   ├── base.css
│   │   │   └── fonts/                # Funnel Sans woff2 (bundled, offline)
│   │   ├── screens/
│   │   │   ├── Setup/                # ★ The two folder pickers
│   │   │   ├── Register/             # Reconciliation table
│   │   │   ├── Rename/               # Rename review + undo
│   │   │   ├── Queue/                # Batch progress
│   │   │   ├── Compare/              # ★ The lightbox viewer
│   │   │   ├── Changes/              # Change triage list
│   │   │   ├── BOQ/                  # Mapping & costing
│   │   │   ├── Reports/              # Export
│   │   │   └── Settings/             # AI mode, tolerances, profiles
│   │   ├── components/
│   │   │   ├── FolderDrop/           # Drag-drop folder target
│   │   │   ├── Lightbox/             # WebGL canvas + tile loader
│   │   │   ├── OverlayControls/      # Opacity slider, blink, swipe
│   │   │   ├── ChangeCard/
│   │   │   ├── StatusPill/
│   │   │   ├── DataTable/            # Virtualised, 5000+ rows
│   │   │   ├── ProgressRail/
│   │   │   └── AIBadge/              # Shows when AI was used, and cost
│   │   ├── hooks/
│   │   ├── store/                    # Zustand state
│   │   ├── api/                      # Typed fetch client + WebSocket
│   │   └── lib/
│   └── public/
│
├── profiles/                         # Shipped client sheet profiles
│   ├── default.json
│   ├── keo.json
│   └── _schema.json
│
├── tests/
│   ├── fixtures/                     # ★ Real anonymised drawing pairs
│   │   ├── clean_pair/
│   │   ├── scale_changed/
│   │   ├── shifted_plot/
│   │   ├── renamed_files/
│   │   ├── scanned/
│   │   └── multipage/
│   ├── test_ingest.py
│   ├── test_register.py
│   ├── test_naming.py
│   ├── test_align.py                 # ★ Most important test suite
│   ├── test_compare.py
│   └── test_golden.py                # Known-answer regression tests
│
├── packaging/
│   ├── build.spec                    # PyInstaller
│   ├── installer.iss                 # Inno Setup
│   ├── licence/                      # Licence key check
│   └── assets/                       # Icon, splash, EULA
│
└── docs/
    ├── ARCHITECTURE.md
    ├── DESIGN_SYSTEM.md
    ├── AI_POLICY.md                  # ★ Give this to clients
    ├── USER_GUIDE.md
    └── ROADMAP.md
```

---

## 3. Database schema (SQLite, one file per project)

```sql
project        (id, name, created, old_folder, new_folder, profile_id, settings_json)
file           (id, project_id, side, abs_path, rel_path, filename, size, hash,
                page_count, is_readable, error_note)
sheet          (id, file_id, page_index, dwg_number, title, revision, scale,
                sheet_size, rotation, issue_date, source_of_number)
pair           (id, project_id, old_sheet_id, new_sheet_id, match_method,
                confidence, status)          -- status: matched/new/missing/ambiguous
alignment      (id, pair_id, matrix_json, method, rms_error, quality, accepted)
change         (id, pair_id, x, y, w, h, type, severity, is_cosmetic,
                is_clouded, description, user_status)   -- user_status: open/confirmed/dismissed
change_text    (id, change_id, old_text, new_text, kind)  -- dimension/tag/note
boq_link       (id, change_id, boq_item_code, qty, rate, amount, confidence, confirmed_by_user)
rename_log     (id, project_id, old_name, new_name, applied_at, reversed_at)
ai_call        (id, project_id, purpose, provider, model, input_tokens,
                output_tokens, cost, cached, created_at)
job            (id, project_id, kind, payload_json, state, attempts, error, updated_at)
```

Two design rules worth keeping:

- Every AI call is logged in `ai_call`. You can show the client exactly what left the machine.
- `pair.status` is set before any comparison runs. Set reconciliation must work even if comparison fails.

---

## 4. AI integration — how the "brain" works

### The rule

> Local code does the work. AI is called only when local code cannot answer, and only on small pieces of data.

### Three modes, chosen by the user in Settings

| Mode | What happens | For whom |
|---|---|---|
| **Offline** (default) | No network at all. Embeddings run locally. AI features are visibly disabled. | Clients with data restrictions |
| **Local model** | Ollama running a small vision/text model on your RTX 4060 | You, and technical users |
| **Cloud (Claude API)** | Best quality. Every call logged and cost-metered. | Users who accept it |

### Where AI is genuinely worth calling

| Purpose | Input sent | Local alternative tried first |
|---|---|---|
| Read a messy or handwritten title block | 1 small crop (~400×200 px) | Regex on extracted text |
| Classify an ambiguous change region | 2 small crops (old + new) | Rule-based classifier |
| Match a drawing note to a BOQ description | Text only | Local embeddings + fuzzy |
| Write the change description in words | Structured JSON only, no image | Template sentence |
| Draft the variation narrative | Structured JSON only | Template |
| Read a drawing list from a badly formatted PDF | Extracted text only | Table parser |

### Where AI must NOT be used

- Detecting changes (unreliable, slow, expensive, and non-repeatable)
- Measuring quantities (a wrong number here is a professional liability)
- Anything that must give the same answer every time for an audit trail

### Safety controls to build in from day one

1. **Crop policy** — never send a full sheet. Maximum crop size enforced in code.
2. **Redaction** — strip project name, client name, addresses from any text sent.
3. **Consent screen** — first time cloud AI is used, show exactly what will be sent.
4. **Cache** — hash the input; identical crops never cost twice.
5. **Cost meter** — visible running total in the UI, plus a hard monthly cap.
6. **Graceful failure** — if AI fails or is off, the app still completes the run.

---

## 5. UI/UX design direction

### The concept: **the light table**

Before CAD, drawing offices compared revisions by putting two tracing sheets on a **light table** — a dark desk with a lit glass panel. That is exactly what this app does, so that is the design.

The application chrome is a dark, quiet drawing room. The sheet is the only bright thing on screen. Nothing competes with the drawing. This is not decoration — it is also correct ergonomics, because drawing work is long and a bright grey UI around a white sheet causes eye strain.

### Design tokens

```css
:root {
  /* Room — the chrome */
  --room-900: #14181C;   /* app shell */
  --room-700: #1B2127;   /* panels, rails */
  --room-500: #262E36;   /* raised surfaces */
  --room-300: #3A444F;   /* borders, dividers */

  /* Light — the sheet */
  --sheet:    #F7F6F3;   /* warm paper, not pure white */
  --sheet-dim:#E8E6E1;

  /* Text */
  --text-hi:  #EDF1F5;
  --text-mid: #9DA9B5;
  --text-low: #66727E;

  /* Brand — chrome only */
  --brand:      #CF0A2C;
  --brand-soft: #FF3355;
  --brand-glow: rgba(207,10,44,0.28);

  /* Status */
  --ok:    #2BB673;
  --warn:  #E39A00;
  --info:  #2E9BD6;
}
```

### ⚠ One conflict you must solve deliberately

Your brand colour is red. In drawing comparison, **red already means "removed"**. If both use red, users will misread the output.

**The rule to follow:**

- `#CF0A2C` appears **only in the chrome** — logo, primary buttons, active tab, focus ring.
- The **diff palette appears only inside the lightbox** and never in the chrome.
- Inside the lightbox use the industry convention users already know from Bluebeam: **old = blue `#0E63C4`, new = red-orange `#E8442A`, unchanged = 55% grey**.

Because the chrome is dark and the sheet is bright paper, the two zones read as separate worlds and the two reds never sit next to each other. Add a colour-blind-safe preset (blue / orange) in Settings.

### Typography

**Funnel Sans throughout** — one family, bundled as woff2 so it works offline.

| Role | Size / weight | Notes |
|---|---|---|
| Screen title | 28px / 600 | Sentence case, never all-caps |
| Section heading | 18px / 600 | |
| Body & table | 14px / 400 | |
| Data columns | 14px / 400 | `font-variant-numeric: tabular-nums` |
| Micro label | 12px / 500 | |

Use tabular figures instead of adding a monospace font. Drawing numbers and revision codes then align perfectly in columns, without a second typeface.

### Screen 1 — Setup (the two folder pickers)

This is the screen you asked about, so it deserves the boldness.

```
┌──────────────────────────────────────────────────────────────────────┐
│  ◤ C-Quest Drawing Compare                          ⚙  Offline mode  │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│      Compare two drawing issues                                      │
│      Point to the folder for each issue. Subfolders are included.    │
│                                                                      │
│   ┌───────────────────────────┐ ║ ┌───────────────────────────┐     │
│   │                           │ ║ │                           │     │
│   │      PREVIOUS ISSUE       │ ║ │      CURRENT ISSUE        │     │
│   │                           │ ║ │                           │     │
│   │   Drop a folder here      │ ║ │   Drop a folder here      │     │
│   │        or browse          │ ║ │        or browse          │     │
│   │                           │ ║ │                           │     │
│   │   ─────────────────────   │ ║ │   ─────────────────────   │     │
│   │   IFC Rev C                │ ║ │  IFC Rev D                │     │
│   │   \\srv\Proj\03_IFC_RevC  │ ║ │  \\srv\Proj\03_IFC_RevD   │     │
│   │   147 PDF · 2.1 GB        │ ║ │  151 PDF · 2.3 GB         │     │
│   └───────────────────────────┘ ║ └───────────────────────────┘     │
│                                 ║                                    │
│   ┌──────────────────────────────────────────────────────────────┐  │
│   │  Drawing list (optional)     issued-drawing-list-revD.xlsx  ×│  │
│   └──────────────────────────────────────────────────────────────┘  │
│                                                                      │
│   Sheet profile: KEO A1 ▾        Tolerance: 25 mm ▾                  │
│                                                                      │
│                                   ┌────────────────────────────┐    │
│                                   │  Build the register  →     │    │
│                                   └────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────┘
```

**The one memorable element:** the vertical **seam** (`║`) between the two panels. It is a thin brand-red line with a soft glow. It is the comparison axis, and it carries through the whole app — it becomes the swipe divider in the lightbox viewer, and the split marker in the change list. One idea, used consistently.

**The one orchestrated motion:** when the second folder is dropped, the seam pulses once from top to bottom and the "Build the register" button becomes active. That is the only non-user-triggered animation in the app. Everything else moves only in response to a click.

**Copy rules:**
- Label the panels **"Previous issue" / "Current issue"**, not "Folder A / Folder B". Users think in issues.
- Empty state is an instruction, not a mood: "Drop a folder here or browse".
- Errors say what to do: "12 files could not be opened. They are password-protected. View list."

### Screen 5 — The lightbox viewer

```
┌────────┬──────────────────────────────────────────────┬──────────────┐
│ SHEETS │                                              │   CHANGES    │
│        │        ┌──────────────────────────┐          │              │
│ ● A-101│        │                          │          │  ▸ 12 open   │
│ ● A-102│        │      the sheet, lit      │          │              │
│ ○ A-103│        │      on dark room        │          │  1 Door moved│
│ ● A-104│        │                          │          │    1200 mm   │
│ ⚠ A-105│        │            ║ ← swipe     │          │    ★★★       │
│        │        └──────────────────────────┘          │              │
│        │  [Overlay] [Swipe] [Blink] [Old] [New]       │  2 Dim change│
│        │  Opacity ●────────                            │    3000→3200 │
│        │                                              │    ★★★★      │
└────────┴──────────────────────────────────────────────┴──────────────┘
```

Four viewing modes, because different users trust different ones: **Overlay, Swipe, Blink (alternate old/new), Single**. Keyboard: `←/→` change sheet, `Space` blink, `Tab` next change, `Enter` confirm, `D` dismiss.

### Performance rules for the UI

1. Render tiles from the backend at the needed zoom level only. Never load a full A0 at 300 DPI into the browser.
2. Use WebGL (PixiJS) for the canvas. Canvas 2D will stutter above ~4000 px.
3. Virtualise every table. A register can hold 800 rows and a change list 5,000.
4. Keep all heavy work in the Python process pool. The UI must never freeze.
5. Show a real progress rail with sheet names, not a spinner. Trust comes from visible progress.

---

## 6. Build plan — 8 phases

Each phase ends with something you can actually use on a live project.

### Phase 1 — Skeleton (1 week)
FastAPI + pywebview + React talking to each other. One window opens, one API call returns. SQLite created. Logging works. PyInstaller produces a working `.exe`.
**Done when:** you can double-click an `.exe` and see your own UI.

### Phase 2 — Intake & register (2 weeks) ★ First sellable output
Folder scan, PDF inspection, duplicate hashing, quarantine list, title block extraction, drawing list import, reconciliation, Excel register export.
**Done when:** you point it at two real KEO issue folders and it tells you what is new, missing and revised, and the answer is correct.

### Phase 3 — Naming & rename (1 week)
Normaliser, 3-tier matcher, rename proposal table, safe copy-based execution, undo log.
**Done when:** a folder with inconsistent names is matched correctly and you can reverse every rename.

### Phase 4 — Render & align (3 weeks) ★ Hardest phase
Tiled rendering, tile server, lightbox canvas, grid bubble detection, feature matching fallback, scale normalisation, alignment quality check and rejection.
**Done when:** a sheet issued at A1 and reissued at A3 with the plan moved still aligns within tolerance, and a bad alignment is refused rather than reported wrongly.

### Phase 5 — Compare & mask (2 weeks)
Title block masking, watermark removal, text diff, dimension diff, raster diff, tolerance in mm, noise filtering.
**Done when:** a sheet with only a revision letter change reports **zero** changes.

### Phase 6 — Classify & report (2 weeks)
Clustering, change typing, severity scoring, cloud detection and cross-check, overlay PDF, change register Excel, audit trail.
**Done when:** a real revised sheet produces a change list a colleague can read and agree with.

### Phase 7 — AI layer (1 week)
Provider abstraction, three modes, redaction, crop policy, consent screen, cost meter, cache, and the four prompts.
**Done when:** switching to Offline mode disables AI everywhere and nothing breaks.

### Phase 8 — BOQ & commercial (2 weeks)
BOQ import, mapping table, local embedding matcher, cost calculation, variation narrative export, licence key, installer, user guide.

**Total: about 14 weeks part-time.** Phases 2 and 3 alone (3 weeks) already solve a real daily problem and can be shown to a client.

---

## 7. Risk register

| Risk | Impact | What to do |
|---|---|---|
| Alignment fails on real drawings | Whole product fails | Build the fixture test set in Phase 1, before writing the aligner |
| Too many false positives | Nobody uses it twice | Default to hiding cosmetic changes; measure false-positive rate on real sheets |
| Bluebeam already does overlay | Weak positioning | Compete on **set reconciliation + register + BOQ link**, not on overlay |
| PyMuPDF is AGPL | Legal problem when selling | Use `pypdfium2` (BSD/Apache). **Verify licence terms yourself before committing.** |
| Client bans cloud AI | Lost sale | Offline mode is the default and is a headline feature |
| Scanned drawings | Poor results | Detect scanned sheets early and warn honestly, do not fail silently |
| Scope creep | Never ships | Ship Phase 2 as version 1.0 |

---

## 8. Final recommendation

1. **Start with the fixtures, not the code.** Collect 20 real anonymised drawing pairs from projects you have worked on: a clean pair, a rescaled pair, a shifted pair, a renamed set, a scanned sheet, a multi-page PDF. Everything you build gets tested against these.

2. **Ship the register first.** Phases 2 and 3 are three weeks and produce a genuinely useful tool. Use it yourself on a live KEO issue. If it saves you two hours, it will save a document controller two days.

3. **Treat AI as a feature you can switch off.** Build every path to work without it. Then AI becomes a selling point rather than a dependency, and "your drawings never leave your computer" stays true.

4. **Spend the design effort on the Setup screen and the lightbox.** Those are the two screens users see every single time. The rest can be clean and quiet.

One thing I am not fully sure about and you should verify yourself: the exact licence position of any PDF library you choose for commercial resale, and whether your employment contract with KEO affects ownership of tools you build in your own time. Both are worth checking properly before you invest 14 weeks.
