# Phase 2 — Intake & Register
### Build plan for use with Claude Code

**Goal:** Point the app at two issue folders. It reads every drawing, works out what is new, missing, revised and unchanged, and gives you a register you can send to the design team.

**This is your first sellable output.** Phase 2 alone solves a real daily problem for document control and QS teams, with no comparison logic at all.

**Time:** About 2–3 weeks part-time. Eleven tasks.

---

## Part A — Before you start

### A1. New branch

```powershell
cd D:\GitHub\cquest-drawing-compare
git checkout main
git pull
git checkout -b phase-2-intake-register
```

### A2. Build a proper fixture set

Phase 2 lives or dies on real data. Before writing code, collect these:

```
D:\GitHub\_cqdc-fixtures\
├── 01_normal\              # 30-50 sheets, clean naming, both issues
│   ├── old\
│   └── new\
├── 02_partial_issue\       # old has 200, new has only 15 (very common)
│   ├── old\
│   └── new\
├── 03_renamed\             # same drawings, client changed naming standard
├── 04_multipage\           # one PDF containing 20 drawings
├── 05_messy\               # subfolders, duplicates, "Copy of", junk files
├── 06_scanned\             # at least 3 scanned/photocopied sheets
├── 07_locked\              # password-protected and corrupt PDFs
└── lists\
    ├── register_clean.xlsx
    ├── register_messy.xlsx # logo rows above the header, merged cells
    └── register.pdf
```

You do not need all of these on day one, but you need `01_normal` and `02_partial_issue` before Task 2.5.

**Anonymise them.** These stay outside the repo, but treat them as if they might leak.

### A3. Update `CLAUDE.md`

Add this section:

```markdown
## Current phase

Phase 2 — Intake & register. Scanning folders, identifying drawings,
reconciling the two sets, exporting the register.
No image rendering, no alignment, no comparison. Those are Phase 4+.

## Phase 2 rules

- Scanning must show results progressively. Never block the UI while
  reading a network folder.
- Every scan result is cached by (path, size, mtime). Re-scanning an
  unchanged folder must be near-instant.
- Every drawing number records HOW it was found (title block, filename,
  drawing list, or user). The user must be able to see and trust this.
- A missing drawing in a partial issue is NOT a deleted drawing.
  Never use the word "removed" until the issue type is known.
- The app never writes into the user's input folders. All output goes
  to the output folder the user chose.
```

---

## Part B — The real-world logic

This is the part that matters. Read it before Task 2.5.

### B1. Three folders, not two

| Picker | Purpose | Validation |
|---|---|---|
| **Previous issue** | Read-only source | Must exist, must be readable, must contain at least one PDF |
| **Current issue** | Read-only source | Same, plus must not be the same folder as Previous |
| **Output folder** | Where everything is written | Must be writable, **must not be inside either input folder**, warn if not empty |

**Why output cannot be inside an input folder:** the app writes an Excel register and later PDF overlays. If those land inside the "Current issue" folder, the next scan picks them up as drawings. You get a folder that pollutes itself.

**Write permission check upfront.** People point at read-only network shares constantly. Test by creating and deleting a temporary file when the folder is chosen, not when the export runs 20 minutes later.

**Default suggestion:** when both input folders are chosen, propose a name automatically:

```
D:\Projects\UVU\Comparisons\Compare_RevC_to_RevD_2026-09-01\
```

The user can accept it with one click or change it.

**Output workspace structure:**

```
Compare_RevC_to_RevD_2026-09-01/
├── 01_Register/
│   └── Drawing_Register.xlsx
├── 02_Overlays/            (empty until Phase 6)
├── 03_Reports/
├── 04_Renamed/             (empty until Phase 3)
└── _audit/
    ├── run_log.json        settings, versions, timestamps
    └── scan_cache.db
```

Create this structure the moment the output folder is confirmed. It shows the user what will happen.

---

### B2. Two-pass scanning — the key performance decision

A folder of 300 A0 drawings on a network share can take **four minutes** to read properly. Users will not wait four minutes staring at a spinner.

**Pass 1 — Fast (target: under 2 seconds)**
Uses only what the operating system already knows: filename, size, modified date, path. No PDF is opened.
→ The file list appears on screen immediately.

**Pass 2 — Deep (runs in the background, with progress)**
Opens each PDF: page count, page sizes, rotation, encryption, whether it has a text layer, layers present, content hash. Extracts the title block. Each result streams to the UI as it completes.

The user sees the list fill in with details, row by row. They can start reading and even cancel if it is the wrong folder.

**Cache key:** `absolute_path + size + modified_time`. If all three match a cached record, skip the deep pass entirely. The second scan of the same folder is instant. Store the cache in the output workspace so it travels with the comparison.

**Cancellation must work.** A cancel button that does nothing is worse than no cancel button.

---

### B3. Getting the drawing number — priority order

This is the single most important piece of logic in Phase 2. Everything downstream depends on it.

| Priority | Method | Confidence | `source_of_number` |
|---|---|---|---|
| 1 | Text in the title block zone matching the project pattern | High | `titleblock` |
| 2 | Text anywhere on the sheet matching the project pattern | Medium | `sheet_text` |
| 3 | Filename matching the project pattern | Medium | `filename` |
| 4 | Matched against an imported drawing list | Medium | `drawing_list` |
| 5 | User typed it | Certain | `user` |
| 6 | Vision model read it (Phase 7) | Medium | `ai` |

**Always store which method was used.** Show it in the UI as a small marker. A QS will trust a number that came from the title block and will check one that came from a filename.

**Title block zone detection, practically:**
Most sheets put the title block in the right-hand strip or the bottom strip. A workable approach without any AI:

1. Take the rightmost 25% of the page and the bottom 20%
2. Extract text with coordinates from those zones
3. Look for label text: `DRAWING NO`, `DWG NO`, `DRG NO`, `SHEET NO`, `DOCUMENT NO`, `REF`
4. The drawing number is usually the text immediately right of, or below, that label
5. If no label is found, take the text in that zone matching the project's drawing-number pattern

**⚠ The mismatch flag.** Extract the number from the title block **and** from the filename. When they differ, flag it. This is extremely common in real projects and document controllers care about it a great deal — it is often the first thing they check manually.

**Learn from corrections.** When the user fixes one drawing number, offer: *"Apply this pattern to the other 23 unidentified sheets?"* One correction should fix a whole set. This turns a frustrating screen into a fast one.

---

### B4. Revision parsing

Real projects use different schemes, and the scheme carries meaning:

| Scheme | Example | Meaning |
|---|---|---|
| Alpha | A, B, C, D | Generic. **Skips I and O** to avoid confusion with 1 and 0 |
| Preliminary | P01, P02, P03 | Not for construction |
| Construction | C01, C02 | Issued for construction |
| Tender | T01, T02 | Tender issue |
| Numeric | 00, 01, 02 | Generic |
| Mixed | P03 → C01 | Status changed. **A status change is significant even if the drawing did not.** |

Store the scheme in the sheet profile. To decide whether B is newer than A, the app must know the scheme.

**Real-world case worth handling:** `P03 → C01` looks like the revision went backwards. It did not — the drawing moved from preliminary to construction status. Detect scheme changes and label them as a **status change**, not a revision decrease.

---

### B5. Reconciliation — the actual status list

For every drawing number found in the old set, the new set, or the drawing list:

| Status | Meaning | Action |
|---|---|---|
| **Revised** | In both, revision increased | → Compare in Phase 4+ |
| **Unchanged** | In both, same revision, same file hash | → Skip comparison |
| **⚠ Same revision, different file** | Same rev but the file content differs | → **Flag loudly.** Someone reissued without bumping the revision. Compare it anyway. |
| **New** | In the new set only | → Fully new scope |
| **Not reissued** | In the old set only, and this is a partial issue | → Normal, no action |
| **Removed** | In the old set only, and this is a full issue | → Possible scope deletion, needs confirmation |
| **Superseded in folder** | Same number, two revisions in one folder | → Keep the highest, log the rest |
| **Duplicate file** | Identical hash in two locations | → Keep one, log the other |
| **Unidentified** | No drawing number could be found | → User must resolve |
| **Unreadable** | Corrupt, encrypted, or locked | → Quarantine list |
| **In list, not in folder** | The drawing list expects it | → Chase the design team |
| **In folder, not in list** | Undeclared drawing | → Query the design team |

### ⚠ B6. Partial issue detection — the insight that decides whether people trust the tool

In construction, a **partial issue is normal**. The consultant reissues only the 12 sheets that changed, not all 300.

If the app reports **"288 drawings removed"** on a partial issue, the user will close it and never open it again. The tool will look broken even though the code is correct.

**How to handle it:**

1. After scanning, compare set sizes. If the new set is smaller than about 60% of the old set, this is probably a partial issue.
2. **Ask, do not assume:**

   > "The current issue has 15 drawings and the previous issue has 203.
   > Is this a partial issue (only changed drawings reissued), or a full
   > issue (the complete set)?"

3. Change the language based on the answer:
   - Partial issue → "188 drawings not reissued" (neutral, no action)
   - Full issue → "188 drawings removed" (needs attention)

4. Record the answer in the audit log. It changes the meaning of the whole register.

Add a third option: **"Compare only what was reissued"** — the most common real intention.

---

### B7. Drawing list import — expect mess

Real drawing registers are Excel files a human formatted for printing, not for machines.

**What you will actually find:**
- The header row is at row 7, under a logo and project information block
- Merged cells across the title
- Column names that vary: `Drawing No`, `Dwg No.`, `Document Number`, `Sheet Ref`
- A **revision matrix** — one column per issue date, revision letters inside the grid, rather than a single "Rev" column
- Several worksheets, and only one is the register
- Blank separator rows between disciplines
- Section headers inside the data ("ARCHITECTURAL", "STRUCTURAL")

**Practical approach — do not try to be fully automatic:**

1. Read all worksheets. Score each one by how many cells look like drawing numbers. Pick the best.
2. Find the header row: scan the first 25 rows for a row containing two or more of: `drawing`, `dwg`, `no`, `number`, `title`, `rev`, `sheet`, `description`.
3. Guess the column mapping using a synonym table.
4. **Show the user a preview of the first 15 parsed rows with dropdowns to correct each column mapping.** Save the mapping to the sheet profile so it is remembered next time.
5. Skip blank rows and rows where the drawing number cell is empty.

Manual mapping with a good preview beats clever automatic parsing that gets it wrong silently.

---

### B8. Multi-page PDFs

One file can contain 20 drawings. The unit of work is a **sheet**, not a file.

Detect it: page count > 1. Then check whether each page has its own drawing number in its title block.

- Different numbers per page → treat each page as a separate drawing
- Same number on every page → it is a multi-sheet drawing ("Sheet 1 of 3")
- No numbers → ask the user how to handle the file

Never assume one file equals one drawing. This is a common source of wrong counts.

---

### B9. Performance targets

Write these down and test against them:

| Operation | Target | Set size |
|---|---|---|
| Fast pass, local drive | Under 2 s | 300 files |
| Fast pass, network drive | Under 8 s | 300 files |
| Deep pass, local drive | Under 90 s | 300 files |
| Deep pass, cached | Under 3 s | 300 files |
| Register table renders | Under 200 ms | 800 rows |
| Excel export | Under 10 s | 800 rows |

Run the deep pass across CPU cores with a process pool. Your machine will handle 6–8 workers comfortably.

---

## Part C — The eleven tasks

One task per Claude Code session. Test, commit, `/clear`, next.

---

### Task 2.1 — Folder scanner (fast pass)

**Prompt:**

> Build `engine/ingest/folder_scanner.py`.
>
> A `scan_folder(root: Path)` function that walks recursively and returns `ScannedFile` records with: absolute path, path relative to root, filename, extension, size, modified time, and depth.
>
> Requirements:
> - Route every path through `engine/utils/longpath.py` — real drawing folders exceed 260 characters
> - Ignore junk: `Thumbs.db`, `desktop.ini`, `.DS_Store`, files starting with `~$`, hidden files, and anything inside folders named `_archive`, `superseded`, `old` (case-insensitive) — but make this ignore list configurable and report what was skipped
> - Keep PDFs. Record other extensions separately as "other files found" so the user knows DWGs exist
> - Never follow symlinks or junctions — network shares have loops
> - Handle per-file permission errors without stopping the walk
> - Must complete in under 2 seconds for 300 files on a local drive
>
> Also build `engine/ingest/file_hasher.py` using `xxhash` for content hashing, reading in 1 MB chunks. Add a `quick_hash` that hashes only the first and last 64 KB plus the size, for fast duplicate screening.

**Test:** Point it at `05_messy`. Confirm junk is excluded, subfolders are found, and the timing target is met.

---

### Task 2.2 — PDF inspection (deep pass) with a job queue

**Prompt:**

> Build the deep inspection layer.
>
> 1. `engine/ingest/pdf_inspector.py` — using `pypdfium2` and `pikepdf`, extract per file: page count, per-page width/height in mm and detected sheet size (A0–A4), page rotation, whether encrypted or password-protected, whether a text layer exists, whether the PDF has optional content groups (layers) and their names, PDF producer metadata, and whether the page looks scanned (a single large image and no text).
> 2. `engine/ingest/quarantine.py` — catches per-file failures and records path, error type, and a plain-English reason. **Never let one bad file stop the batch.**
> 3. `engine/core/jobs.py` — a job queue backed by the `job` table using `ProcessPoolExecutor`. Must support: submit, progress reporting, cancel, and resume after a crash. Worker count defaults to `cpu_count() - 2`, minimum 2.
> 4. `engine/api/ws_progress.py` — WebSocket that streams progress events: `{stage, current, total, current_item, elapsed, eta}`.
> 5. `engine/storage/cache_store.py` — caches inspection results keyed by `path + size + mtime`. A cache hit must skip all work.
>
> Progress events must be sent at most 10 times per second, batched, so the UI is not flooded.

**Test:** Deep-scan 300 files, watch progress stream, press cancel mid-run and confirm it stops within a second. Re-run and confirm the cache makes it near-instant.

---

### Task 2.3 — Title block and drawing number extraction

**Prompt:**

> Build the title block layer.
>
> 1. `engine/titleblock/zone_detector.py` — identifies candidate title block zones: rightmost 25% and bottom 20% of the page. Returns zones in reading order.
> 2. `engine/extract/text_extractor.py` — extracts text from a PDF page with x, y, width, height, font size, and rotation for each text item, using `pypdfium2`.
> 3. `engine/titleblock/patterns.py` — a pattern library. Each project profile holds: drawing-number regex patterns, label keywords (`DRAWING NO`, `DWG NO`, `DRG NO`, `SHEET NO`, `DOCUMENT NO`, `REF`), revision patterns, and scale patterns. Ship a `default.json` with common patterns and a `keo.json` placeholder.
> 4. `engine/titleblock/field_extractor.py` — extracts drawing number, title, revision, scale, and sheet size using this priority order, recording `source_of_number` for each result: title block zone match → sheet text match → filename match → none.
>    - Find a label keyword, then take the nearest text to its right or below
>    - If no label, match the project's drawing-number regex within the zone
>    - Return a confidence score with every field
> 5. `engine/titleblock/scale_reader.py` — finds scale text such as `1:100`, `1/100`, `NTS`, `AS SHOWN`.
>
> Critical: always extract the number from BOTH the title block and the filename, and set a `number_mismatch` flag when they differ.
>
> No AI in this task. Regex and geometry only.

**Test:** Run on `01_normal`. Target: 90% or more identified from the title block. Check the mismatch flag catches a real case.

---

### Task 2.4 — Revision logic

**Prompt:**

> Build `engine/register/revision_logic.py`.
>
> - Detect the revision scheme from a set of revision strings: alpha (A, B, C — skipping I and O), preliminary (P01…), construction (C01…), tender (T01…), numeric (00, 01…), or unknown
> - `compare_revisions(old, new)` returning: `newer`, `older`, `same`, `status_change`, or `incomparable`
> - Treat `P03 → C01` as a **status change**, not a revision decrease. The same applies to any scheme change.
> - Handle messy input: `Rev A`, `REV.A`, `rev_a`, `A1`, `-A`, `(A)`
> - `is_significant(old, new)` — a status change is significant even when the drawing content is identical
>
> Write thorough tests for the edge cases, especially skipped I and O and scheme changes.

---

### Task 2.5 — Reconciliation engine ★ The core of Phase 2

**Prompt:**

> Build `engine/register/reconciler.py`. This produces the drawing register.
>
> Input: old sheets, new sheets, an optional imported drawing list, and an `issue_type` of `full`, `partial`, or `unknown`.
>
> Output: one `RegisterRow` per drawing number covering the union of all three sources, with these statuses:
> `revised`, `unchanged`, `same_rev_different_file`, `new`, `not_reissued`, `removed`, `superseded_in_folder`, `duplicate_file`, `unidentified`, `unreadable`, `in_list_not_in_folder`, `in_folder_not_in_list`.
>
> Matching order: exact drawing number → normalised drawing number (case, spaces, separators removed) → filename → unmatched.
>
> Critical rules:
> - `same_rev_different_file` means the revision is identical but the content hash differs. Flag this at high severity — it means someone reissued without bumping the revision.
> - When `issue_type` is `partial`, old-only drawings are `not_reissued`, never `removed`.
> - When `issue_type` is `unknown` and the new set is under 60% of the old set size, return `needs_issue_type_confirmation` instead of guessing, along with both counts.
> - Within one folder, if a drawing number appears at more than one revision, keep the highest and mark the rest `superseded_in_folder`.
>
> Also build `engine/register/summary.py` producing set-level counts and a plain-English summary sentence.
>
> Write tests using the `01_normal` and `02_partial_issue` fixtures.

**Test:** Run on `02_partial_issue`. It must ask about the issue type and must not report 188 drawings as removed.

---

### Task 2.6 — Drawing list import

**Prompt:**

> Build the drawing list importer.
>
> 1. `engine/register/list_importer.py` — accepts `.xlsx`, `.xls`, `.csv`, and `.pdf`
> 2. `engine/register/list_parser.py` — the messy-file logic:
>    - Read every worksheet, score each by how many cells match a drawing-number pattern, pick the highest
>    - Find the header row by scanning the first 25 rows for a row containing two or more of: `drawing`, `dwg`, `no`, `number`, `title`, `rev`, `sheet`, `description`, `status`
>    - Guess column mapping from a synonym table
>    - Detect a revision matrix layout (several date columns with revision letters inside) and offer to use the most recent column
>    - Skip blank rows, separator rows, and discipline section headers
>    - Return a `ParseResult` with the proposed mapping, a 15-row preview, and any warnings
> 3. `engine/api/routes_register.py` — an endpoint returning the parse preview, and another accepting a corrected column mapping
> 4. Save the confirmed mapping into the sheet profile for reuse
>
> For PDF lists, extract text and detect table structure from x-coordinate clustering. If confidence is low, say so clearly rather than returning wrong data.

**Test:** Run on `register_messy.xlsx`. The header row must be found correctly and the preview must be readable.

---

### Task 2.7 — Output workspace

**Prompt:**

> Build `engine/core/workspace.py`.
>
> - `validate_output_folder(path, old_folder, new_folder)` returning a structured result: exists, is writable (tested by creating and deleting a temp file), is not inside either input folder, is not the same as either input folder, is empty or not, and free disk space
> - `suggest_output_folder(old_folder, new_folder, old_rev, new_rev)` producing a name like `Compare_RevC_to_RevD_2026-09-01` in a sensible parent folder
> - `create_workspace(path)` building the folder structure: `01_Register`, `02_Overlays`, `03_Reports`, `04_Renamed`, `_audit`
> - `write_audit_log(workspace, run_info)` recording app version, settings used, folders, timestamps, issue type, and counts — as JSON. This must be defensible if the output supports a variation claim.
>
> Warn but do not block when the folder is not empty. Never overwrite an existing register silently — append a numeric suffix.

---

### Task 2.8 — UI: folder panels that expand into drawing lists

**Prompt:**

> Extend the Setup screen at `ui/src/screens/Setup/`.
>
> Add a third picker below the two issue panels: **Output folder**. It shows a suggested path once both inputs are chosen, with a "Change" control. Show validation state inline — a red message if the folder is not writable or sits inside an input folder.
>
> Change the two issue panels so that after a folder is chosen they expand into a scrollable drawing list:
>
> - Header line: `147 files · 143 drawings · 4 need attention`
> - Virtualised list (use `@tanstack/react-virtual`) with columns: drawing number, title, revision, pages, and a small status marker
> - While the deep pass runs, rows appear immediately from the fast pass with a subtle loading state, then fill in with details as results stream over the WebSocket
> - Rows that could not be identified show the filename in a muted style with a warning marker
> - A small marker showing where the number came from: title block, filename, or list. On hover, explain it in words.
> - Sort and a filter box at the top of each list
> - Panel is collapsible back to its compact state
>
> Keep the seam between the panels. The panels grow downward; the seam grows with them.
>
> Progress must be a real progress rail with the current filename and an estimated time remaining, not a spinner. Include a working cancel button.

**Test:** Point at a 300-file network folder. Rows must appear in under 8 seconds and fill in progressively.

---

### Task 2.9 — UI: the register screen ★ The payoff

**Prompt:**

> Build `ui/src/screens/Register/`. This is the screen users came for.
>
> A single merged table, one row per drawing, with the old and new sides separated by the seam:
>
> ```
> Drawing no. │ Title │ Old rev │ ║ │ New rev │ Status │ Note
> ```
>
> Requirements:
> - Virtualised, must handle 800 rows smoothly
> - Status column uses a coloured pill. Use the status palette from tokens, never brand red for data.
> - Summary bar at the top with counts per status, each clickable as a filter: `12 revised · 3 new · 188 not reissued · 2 need attention`
> - Filter chips, a search box, and column sorting
> - Rows needing attention (`same_rev_different_file`, `unidentified`, `unreadable`, `number_mismatch`) sort to the top by default and are visually distinct
> - Clicking a row opens a detail panel: both file paths, both revisions, extracted fields with their source, and any warnings
> - An inline edit control on the detail panel to correct a drawing number. After a correction, offer: "Apply this pattern to the other N unidentified sheets?"
> - An "Export register" button
>
> If the backend returned `needs_issue_type_confirmation`, show a clear dialog before the table appears, offering: full issue, partial issue, or compare only what was reissued. Show both counts in the question.
>
> Sentence case everywhere. No ALL-CAPS labels.

---

### Task 2.10 — Excel register export

**Prompt:**

> Build `engine/report/register_xlsx.py` using XlsxWriter.
>
> The workbook has four sheets:
>
> 1. **Summary** — project name, both folder paths, issue type, run date, app version, and the counts per status. Written so it can be pasted into an email.
> 2. **Register** — the full table with frozen header, autofilter, status column colour-coded to match the UI, columns sized sensibly, and a "source of number" column so the reader can judge each row's reliability.
> 3. **Needs attention** — only the flagged rows, with a plain-English explanation in each row: "Revision D appears in both issues but the files differ. The drawing may have been reissued without a revision change."
> 4. **Not readable** — quarantined files with paths and reasons.
>
> Add an `engine/api/routes_report.py` endpoint that generates the file into `01_Register/` in the workspace and returns the path. Never overwrite — append a numeric suffix.
>
> Header styling: C-Quest red `#CF0A2C` background with white text. This is a document, not the app chrome, so brand red is correct here.

---

### Task 2.11 — Tests and hardening

**Prompt:**

> Write the Phase 2 test suite against the fixture folders:
>
> - Scanner: junk exclusion, subfolder recursion, long paths, permission errors, timing target
> - Inspector: encrypted files, corrupt files, multi-page files, scanned detection
> - Title block: identification rate on `01_normal`, mismatch flag, source recording
> - Revision logic: all schemes, skipped I and O, status changes
> - Reconciler: every status, and specifically that `02_partial_issue` does not report removals
> - List parser: header row detection on `register_messy.xlsx`
> - Workspace: rejects an output folder inside an input folder, rejects a read-only folder
> - Golden test: a known fixture pair produces an exact expected register, so future changes cannot silently break it
>
> Then add error handling review: every user-facing error message must say what happened and what to do next, in plain language. No stack traces reach the UI.

---

## Part D — Definition of done

- [ ] Three folder pickers work, with validation on the output folder
- [ ] The file list appears in under 2 seconds locally, under 8 seconds on a network share
- [ ] Details fill in progressively while the deep pass runs
- [ ] Cancel stops the scan within one second
- [ ] Re-scanning an unchanged folder is near-instant
- [ ] 90% or more of `01_normal` drawings are identified from the title block
- [ ] `02_partial_issue` triggers the issue-type question and does not report false removals
- [ ] `same_rev_different_file` is detected and flagged prominently
- [ ] Locked and corrupt files land in quarantine without stopping the run
- [ ] A messy Excel drawing list imports with a correct preview
- [ ] The register screen handles 800 rows smoothly
- [ ] Correcting one drawing number can be applied to the whole set
- [ ] The Excel register exports into the workspace with all four sheets
- [ ] An audit log is written
- [ ] `pytest` passes, `ruff check .` is clean
- [ ] The packaged `.exe` still works

Then merge and tag `v0.2.0-register`.

---

## Part E — The real test

When the code is done, do this before writing another line:

**Run it on a live KEO issue and compare its answer against the real drawing list by hand.**

Count how many it got right, how many it got wrong, and how many it could not identify. Write the numbers down.

If the identification rate is above 90% and the reconciliation is correct, you have something a document controller would use tomorrow. If it is at 70%, the title block patterns need work before you go anywhere near Phase 3 — and no amount of clever comparison logic later will fix a register that gets the basics wrong.

---

## Final recommendation

**Task 2.5 is the one to get right.** The reconciliation logic is what makes the tool trustworthy. Specifically, the partial-issue rule in B6 is the difference between a tool people use and a tool people close.

**Task 2.3 is the one that will take longest.** Title block extraction on real drawings is fiddly. Budget more time than you think, and use the "apply this pattern to all" feature to cover the gap rather than chasing 100% automatic accuracy.

**Stop and sell after this phase.** Show the register export to a document controller or a QS colleague and watch what they do with it. Their reaction will tell you more about what to build in Phase 3 than any plan I can write.
