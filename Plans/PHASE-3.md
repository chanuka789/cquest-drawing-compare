# Phase 3 — Matching & Rename
### Build plan for use with Claude Code

**Goal:** Pair every old drawing with its new version, even when the file names changed. Then let the user bulk rename a received drawing set to a proper naming standard, safely and reversibly.

**Time:** About 1.5–2 weeks part-time. Nine tasks.

---

## An honest note on scope before you start

Phase 3 contains **two separate things**, and it helps to be clear about which is which:

| Half | What it is | Who needs it |
|---|---|---|
| **A. Matching** | Pairing old and new drawings when the drawing number failed | The app itself. Phase 4 cannot run without it. |
| **B. Renaming** | Bulk renaming a received set to a naming standard | Document controllers and QS teams. A separate deliverable. |

**Half A is required.** Phase 4 comparison needs correct pairs.

**Half B is optional but commercially valuable.** Renaming 200 received files by hand to a company standard is a two-hour job that document controllers do every issue. Automating it is a genuine selling point, and it is much easier than drawing comparison.

If you are short on time, build Half A properly and ship Half B as a simple version. Do not skip Half A.

---

## Part A — Before you start

### A1. New branch

```powershell
cd D:\GitHub\cquest-drawing-compare
git checkout main
git pull
git checkout -b phase-3-matching-rename
```

### A2. Extend the fixture set

Add these to `D:\GitHub\_cqdc-fixtures\`:

```
03_renamed\
├── old\                  # original naming standard
└── new\                  # SAME drawings, completely different naming standard
                          # (client changed the standard mid-project)

08_naming_mess\
└── new\                  # one folder containing every naming sin:
                          #   "Copy of UVU-ARC-001.pdf"
                          #   "UVU ARC 001 (1).pdf"
                          #   "UVU_ARC_001_RevD_FOR APPROVAL.pdf"
                          #   "01 - UVU-ARC-001 - 12.04.2026.pdf"
                          #   "uvu-arc-001 FINAL final.pdf"
                          #   "UVU–ARC–001.pdf"   (en-dashes, not hyphens)

09_collision\
└── new\                  # two different drawings whose cleaned names collide
```

Fixture `03_renamed` is the one that proves the matcher works. Build it even if you have to rename copies by hand.

### A3. Update `CLAUDE.md`

```markdown
## Current phase

Phase 3 — Matching and rename. Pairing drawings across issues,
and safe bulk renaming. No rendering, no image comparison yet.

## Phase 3 rules

- NEVER modify, move, or delete a file in the user's input folders.
  Renames write copies into the output workspace by default.
- In-place rename requires a separate, explicit opt-in with a warning,
  and must still write a full undo log.
- Every rename operation is logged with old name, new name, file hash,
  and timestamp, so it can be reversed and verified.
- Always dry-run first. The user sees the full plan before anything
  touches the disk.
- Matching is a global assignment problem, not a per-file greedy search.
- Never auto-apply a match or rename below the confidence threshold.
  Low confidence goes to the user for a decision.
```

---

## Part B — The real-world logic

### B1. What actually happens to file names between issues

Collected from real projects. Your normaliser must survive all of these:

| Change | Example |
|---|---|
| Separator swap | `UVU-ARC-001` → `UVU_ARC_001` → `UVU ARC 001` |
| Case change | `uvu-arc-001` → `UVU-ARC-001` |
| Revision suffix added or removed | `UVU-ARC-001` → `UVU-ARC-001_RevD` |
| Revision format changed | `_RevD` → `-Rev D` → `(D)` → `_D` |
| Date stamp added | `UVU-ARC-001_2026-04-12` |
| Date format varies | `12.04.26`, `20260412`, `12-Apr-2026` |
| Sequence prefix added | `01_UVU-ARC-001`, `001 - UVU-ARC-001` |
| Status words added | `_FOR APPROVAL`, `_SUPERSEDED`, `_IFC`, `_PRELIMINARY` |
| Windows copy junk | `Copy of ...`, `... (1)`, `... - Copy` |
| Human junk | `_final`, `_final_final`, `_new`, `_USE THIS ONE` |
| Sheet size marker | `_A1`, `_A0` |
| Discipline prefix added or dropped | `ARC-UVU-001` vs `UVU-001` |
| **Unicode lookalikes** | en-dash `–` instead of hyphen `-`, non-breaking space, curly quotes |
| Trailing dot or space | `UVU-ARC-001 .pdf` (Windows silently strips these) |
| Complete standard change | `UVU-ARC-001` → `UVU-KEO-XX-03-DR-A-0001` |

**The Unicode one catches people out.** An en-dash looks identical to a hyphen on screen but is a different character. Normalise Unicode with NFKC and map lookalike characters before anything else, or you will chase a phantom bug for an afternoon.

---

### B2. The five matching tiers

Run in order. Stop at the first tier that gives a confident, unambiguous answer.

| Tier | Method | Confidence | When it saves you |
|---|---|---|---|
| 1 | Exact drawing number from the title block | 100% | Normal case. Already done in Phase 2. |
| 2 | Normalised drawing number | 98% | Formatting changed only |
| 3 | Normalised filename | 92% | Number not extractable, names are consistent |
| 4 | Token-set fuzzy match on the filename | 60–90% | Names drifted |
| 5 | **Content fingerprint** | 70–95% | ★ The naming standard changed completely |

### ★ B3. Content fingerprinting — the tier that saves the hard cases

When a client changes the naming standard mid-project, tiers 1 to 4 all fail. Nothing in the name of the old file resembles the new one.

But the **content** barely changed. Use that.

**Build a fingerprint from each sheet:**

1. Page size in mm, rounded to the nearest 5 mm
2. Page orientation
3. The set of text strings on the sheet, normalised, sorted, with title block noise removed
4. The count of vector paths, in a coarse bucket (0–100, 100–500, 500–2000, 2000+)
5. Grid bubble labels found on the sheet, sorted — for example `A,B,C,D,1,2,3,4`

Compare two fingerprints with Jaccard similarity on the text sets, weighted by the structural fields. Two versions of the same drawing typically share 85–98% of their text. Two different drawings usually share under 40%.

This is cheap to compute (you already extracted the text in Phase 2), needs no AI, and it is the only thing that will pair a fully renamed set.

**Set the threshold conservatively.** Above 85% similarity, propose the match. Between 70% and 85%, propose it and mark it for review. Below 70%, do not propose it.

---

### ★ B4. Matching is a global assignment problem, not a loop

This is the technical point most people get wrong.

**The naive approach:** for each old drawing, find the best-scoring new drawing. Take it. Move on.

**Why that fails:** suppose old drawing X scores 0.91 against new drawing P, and 0.89 against Q. A greedy loop takes P. But old drawing Y scores 0.95 against P and only 0.40 against Q. The correct global answer is X→Q and Y→P. Greedy gives you X→P and Y→nothing, and both are wrong.

**The correct approach:** build a score matrix of every old drawing against every new drawing, then solve it as an optimal assignment problem. `scipy.optimize.linear_sum_assignment` does this in one call and is already in your dependencies.

Scores below the threshold become "no match" rather than a forced pairing.

For very large sets (over about 1,500 × 1,500), block the matrix by discipline or drawing-number prefix first to keep it fast. In practice a single project set is well under that.

---

### B5. Ambiguity — what to do when the answer is not clean

| Case | Handling |
|---|---|
| Two new drawings score above threshold for one old drawing | Present both, let the user choose. Never guess. |
| One new drawing matches two old drawings | Usually a merged drawing. Flag it, allow a many-to-one pairing. |
| An old drawing splits into two new drawings | Flag it, allow one-to-many. Real and common — a plan split across two sheets. |
| Confidence between 70% and 90% | Propose, but require confirmation before use |
| Confidence below 70% | Leave unmatched. An unmatched drawing is honest; a wrong pair is dangerous. |

**Rule to hold to:** a wrong pair is much worse than no pair. A wrong pair produces a comparison report full of nonsense changes, and the user loses trust in the whole tool. An unmatched drawing just asks a question.

---

### B6. The naming template — use ISO 19650

Do not invent a naming format. The construction industry already has one, and international consultants in the Gulf use it.

**ISO 19650 / BS 1192 structure:**

```
Project - Originator - Volume/System - Level/Location - Type - Role - Number
UVU     - KEO        - XX            - 03            - DR   - A    - 0001
```

Plus an optional revision suffix: `UVU-KEO-XX-03-DR-A-0001_D`

**Build a template engine with tokens:**

| Token | Source |
|---|---|
| `{project}` | Profile setting |
| `{originator}` | Profile setting |
| `{volume}` | Parsed from the drawing number, or fixed |
| `{level}` | Parsed |
| `{type}` | Parsed (`DR` drawing, `SH` schedule, `SP` specification) |
| `{role}` | Parsed (`A` architectural, `S` structural, `M` mechanical) |
| `{number}` | Parsed |
| `{rev}` | From the title block |
| `{title}` | From the title block, cleaned and truncated |
| `{date}` | Issue date from the title block |
| `{status}` | IFC, IFA, IFT |

Example template:
```
{project}-{originator}-{volume}-{level}-{type}-{role}-{number}_{rev}
```

Ship three presets: **ISO 19650**, **Keep original + add revision**, and **Custom**. Show a live preview of three real filenames as the user edits the template. Nobody can read a template string and picture the result.

---

### ⚠ B7. Rename safety — this is where you can destroy someone's data

Renaming is the most dangerous thing this app will ever do. Treat it accordingly.

**Rule 1 — Copy, do not move.** By default, write renamed copies into `04_Renamed/` in the output workspace. The originals are untouched. If something is wrong, the user deletes a folder.

**Rule 2 — In-place rename is opt-in only.** Some document controllers genuinely want it. Allow it, but behind a checkbox with a clear warning, and only after a successful dry run.

**Rule 3 — Always dry run first.** Show the complete plan as a table. Nothing touches the disk until the user clicks apply.

**Windows-specific traps to handle:**

| Trap | What happens | Fix |
|---|---|---|
| Reserved names | `CON.pdf`, `PRN.pdf`, `AUX.pdf`, `NUL.pdf`, `COM1`–`COM9`, `LPT1`–`LPT9` | Detect and append a suffix |
| Invalid characters | `< > : " / \ | ? *` | Strip or replace |
| Trailing dot or space | Windows silently removes them, so your undo log no longer matches | Strip before writing |
| Case-only rename | `abc.pdf` → `ABC.pdf` fails, because Windows is case-insensitive | Rename via a temporary name in two steps |
| Path too long after rename | A longer name pushes past the limit | Check the final path length; warn before applying |
| File locked | Someone has it open in Bluebeam | Detect, skip, report clearly — do not crash |
| Name collision | Two different drawings clean to the same name | **Detect before applying.** Never overwrite. Append `_2`, `_3`. |
| Network share latency | A 300-file rename over `\\server\` takes minutes | Show progress; make it resumable |

**Rule 4 — The undo log is not optional.**

Record for every operation: source path, target path, file hash before, timestamp, and operation type. Store it as JSON in `_audit/rename_log.json` **and** in the SQLite database.

Undo must verify the hash before reversing. If the file changed since the rename, stop and tell the user rather than overwriting newer work.

---

### B8. Optional extra — file into the folder structure

Once files are renamed, sorting them into a standard folder structure is nearly free, and it is the natural next step for a document controller.

```
04_Renamed/
├── Architectural/
├── Structural/
├── MEP/
└── Landscape/
```

Derive the discipline from the role code in the drawing number. Make this a checkbox, off by default. Do not build a full folder-template engine in Phase 3 — one level of discipline folders is enough to be useful.

---

## Part C — The nine tasks

---

### Task 3.1 — The normaliser

**Prompt:**

> Build `engine/naming/normaliser.py`.
>
> A `normalise(name: str, level: NormLevel) -> str` function with three levels:
> - `LIGHT` — Unicode NFKC, lowercase, collapse whitespace, unify separators to a single hyphen
> - `MEDIUM` — light, plus strip revision suffixes, date stamps, sequence prefixes, and file extension
> - `AGGRESSIVE` — medium, plus strip status words, copy junk, sheet size markers, and all remaining separators, leaving only alphanumerics
>
> Handle every case in the table below. Return a `NormalisationResult` recording what was stripped, so the UI can explain a match to the user.
>
> [paste the B1 table]
>
> Critical details:
> - Apply Unicode NFKC normalisation FIRST, and map lookalike characters: en-dash and em-dash to hyphen, non-breaking space to space, curly quotes to straight quotes
> - Date stripping must handle `2026-04-12`, `12.04.26`, `20260412`, `12-Apr-2026`, `Apr 2026`
> - Revision stripping must handle `_RevD`, `-Rev D`, `(D)`, `_D`, `RevD`, `_P01`, `_C02`
> - Do not strip something that is part of the drawing number itself — for example, do not remove `01` from `UVU-ARC-001`. Strip from the ends inward, never from the middle.
>
> Make the strip word lists configurable in the sheet profile. Write extensive tests using the `08_naming_mess` fixture.

---

### Task 3.2 — Content fingerprinting

**Prompt:**

> Build `engine/naming/fingerprint.py`.
>
> A `build_fingerprint(sheet) -> SheetFingerprint` function producing:
> - `page_size_mm` — width and height, rounded to 5 mm
> - `orientation`
> - `text_tokens` — a normalised, deduplicated, sorted set of text strings from the sheet, **excluding the title block zone** so revision and date text does not affect it
> - `path_count_bucket` — 0–100, 100–500, 500–2000, 2000+
> - `grid_labels` — detected grid bubble labels, sorted
> - `text_hash` — a hash of the sorted token set for fast exact comparison
>
> And `similarity(a, b) -> float` returning 0–1, computed as Jaccard similarity on the text tokens (weight 0.7), plus structural agreement on page size, orientation, path bucket and grid labels (weight 0.3).
>
> Store fingerprints in the database so they are computed once. Reuse the text already extracted in Phase 2 — do not re-read the PDFs.
>
> Test with `03_renamed`: two versions of the same drawing must score above 0.85, and two different drawings must score below 0.4.

---

### Task 3.3 — The matcher ★ Core of Phase 3

**Prompt:**

> Build `engine/naming/matcher.py`.
>
> `match_sets(old_sheets, new_sheets, config) -> MatchResult`
>
> Five tiers, run in order, each removing matched pairs from the pool before the next tier:
> 1. Exact drawing number — confidence 1.0
> 2. Normalised drawing number (MEDIUM) — confidence 0.98
> 3. Normalised filename (MEDIUM, then AGGRESSIVE) — confidence 0.92
> 4. Token-set fuzzy filename match using `rapidfuzz.fuzz.token_set_ratio` — confidence is the score ÷ 100
> 5. Content fingerprint similarity — confidence is the similarity score
>
> **Tiers 4 and 5 must use global optimal assignment, not a greedy loop.** Build a score matrix of remaining old sheets against remaining new sheets and solve with `scipy.optimize.linear_sum_assignment`. Pairs scoring below the threshold become unmatched rather than forced.
>
> Return per pair: old sheet, new sheet, confidence, which tier produced it, and a plain-English reason ("filenames match after removing the date stamp and revision suffix").
>
> Handle and report: ambiguous matches (a second candidate within 0.05 of the best), one-to-many, many-to-one, and unmatched on both sides.
>
> Thresholds, configurable: auto-accept at 0.90 and above, review between 0.70 and 0.90, reject below 0.70.
>
> For sets larger than 1500 × 1500, block the matrix by drawing-number prefix before solving.
>
> Test on `03_renamed`: the target is over 95% correct pairing with zero wrong pairs. A wrong pair is a test failure; an unmatched drawing is not.

---

### Task 3.4 — The naming template engine

**Prompt:**

> Build `engine/naming/template.py`.
>
> A token-based template engine supporting: `{project}`, `{originator}`, `{volume}`, `{level}`, `{type}`, `{role}`, `{number}`, `{rev}`, `{title}`, `{date}`, `{status}`, and `{original}`.
>
> - `parse_drawing_number(number, pattern)` — splits an existing drawing number into ISO 19650 fields using a configurable pattern from the sheet profile
> - `render(template, sheet, profile) -> RenderResult` — produces the new filename plus a list of tokens that could not be resolved
> - Token modifiers: `{title:40}` truncates to 40 characters, `{rev:upper}` uppercases, `{number:pad4}` zero-pads
> - Sanitisation: strip invalid Windows characters, strip trailing dots and spaces, detect reserved device names
> - Missing token handling: never write the literal `{rev}` into a filename. Either omit the segment and its separator, or fall back to a configured default.
>
> Ship three presets in `profiles/`: `iso19650.json`, `keep_original_add_rev.json`, and `custom.json`.
>
> Add `preview(template, sheets, n=3)` returning three real example filenames, for a live preview in the UI.

---

### Task 3.5 — The rename planner

**Prompt:**

> Build `engine/naming/rename_planner.py`.
>
> `build_plan(sheets, template, profile, mode) -> RenamePlan` where mode is `copy` or `in_place`.
>
> For each file produce a `RenameAction` with: source path, target path, target folder, status, and any warnings.
>
> Statuses: `ok`, `unchanged` (target equals source), `collision`, `invalid_name`, `path_too_long`, `missing_tokens`, `locked`, `reserved_name`.
>
> Validation that must run before anything touches the disk:
> - **Collision detection** — two sources producing the same target. Resolve by appending `_2`, `_3`, and mark both rows as a collision so the user sees it.
> - **Target exists** — never overwrite. Append a suffix and flag it.
> - **Path length** — check the full final path against 259 characters and warn. If long paths are enabled, warn at 32,000 instead but still flag anything unusually long.
> - **Reserved Windows names** — `CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`, `LPT1`–`LPT9`, with or without an extension.
> - **Invalid characters** and trailing dots or spaces.
> - **File lock check** — attempt to open for exclusive read; report locked files rather than failing later.
> - **Free disk space** — for copy mode, the total size must fit, with a 10% margin.
>
> Optional discipline foldering: when enabled, derive a discipline folder from the role code and add it to the target path. One level only.
>
> Return a summary: how many will change, how many are unchanged, how many have problems.

---

### Task 3.6 — The rename executor and undo ⚠ Highest-risk task

**Prompt:**

> Build `engine/naming/rename_executor.py` and `engine/naming/undo_log.py`.
>
> `execute_plan(plan, mode, progress_callback) -> ExecutionResult`
>
> Safety requirements, all mandatory:
> - **Refuse to run** if the plan contains unresolved collisions or invalid names
> - Process one file at a time, writing an undo log entry **before** each operation, not after
> - For copy mode: copy with `shutil.copy2` to preserve timestamps, verify the hash of the copy matches the source, then move to the next
> - For in-place mode: require an `i_understand=True` argument. Handle case-only renames by going through a temporary name in two steps.
> - Continue past individual failures; collect them and report at the end
> - Report progress every file, with a working cancel
> - On cancel, stop cleanly. Already-completed operations stay done and stay in the undo log.
>
> The undo log records for every operation: sequence number, operation type, source path, target path, source hash, timestamp, and success flag. Write it to `_audit/rename_log.json` and to the `rename_log` table.
>
> `undo(log, verify=True)` reverses operations in reverse order. With `verify=True`, it checks the current file hash against the recorded hash before reversing. If a file changed after the rename, **stop and report** rather than overwriting newer work.
>
> Write tests that simulate: a locked file, a disk-full condition, a cancel halfway through, and an undo after external modification.

---

### Task 3.7 — UI: match review screen

**Prompt:**

> Build `ui/src/screens/Matching/`.
>
> A three-section screen:
>
> **1. Auto-matched** (collapsed by default) — a count and a "review" toggle. Confidence 0.90 and above.
>
> **2. Needs review** (expanded, the focus of the screen) — pairs between 0.70 and 0.90, and all ambiguous cases. Each row shows the old filename and drawing number on the left, the new on the right, the seam between them, a confidence bar, and the plain-English reason. Actions per row: accept, reject, or choose a different match from a dropdown of candidates.
>
> **3. Unmatched** — two side-by-side lists, old-only and new-only. The user can drag or select one from each side to pair them manually. Manual pairs get confidence 1.0 and source `user`.
>
> Details:
> - Keyboard-first: `J`/`K` to move between rows, `A` to accept, `R` to reject, `Enter` to open candidates. Reviewing 40 pairs must be fast.
> - Bulk actions: accept all above a confidence slider the user can drag, with a live count of what that would accept
> - The confidence bar uses the status palette, never brand red
> - A summary bar at the top: `183 auto-matched · 12 need review · 4 unmatched`
> - A "Continue" button that stays disabled while ambiguous pairs are unresolved

---

### Task 3.8 — UI: rename review screen

**Prompt:**

> Build `ui/src/screens/Rename/`.
>
> **Top section — the template builder:**
> - Preset dropdown: ISO 19650, keep original + revision, custom
> - The template string in an editable field with token chips the user can click to insert
> - **A live preview showing three real filenames**, updating as they type. This is essential — nobody can read a template and picture the result.
> - Mode selector: "Copy renamed files to the output folder" (default, selected) or "Rename files in place" (with a clear warning shown when chosen)
> - A checkbox for discipline subfolders
>
> **Middle section — the plan table:**
> - Virtualised, columns: current name, an arrow, new name, status pill
> - Changed portions of the name highlighted so the difference is visible at a glance
> - Rows with problems sort to the top and are visually distinct
> - Filter chips by status
> - Inline edit on the new name for one-off corrections
>
> **Bottom bar:**
> - Summary: `184 will be renamed · 12 unchanged · 3 need attention`
> - "Apply renames" — disabled while any collision or invalid name is unresolved
> - After applying: a result panel with counts, a link to the output folder, and an "Undo all renames" button that stays available for the session
>
> Applying shows a progress rail with the current filename and a cancel button.
>
> The apply confirmation dialog must state exactly what will happen in plain words: "184 files will be copied into `04_Renamed` with new names. Your original files will not be changed."

---

### Task 3.9 — Tests and safety hardening

**Prompt:**

> Write the Phase 3 test suite:
>
> - Normaliser: every case in the B1 table, especially Unicode lookalikes and trailing dots
> - Fingerprint: same drawing above 0.85, different drawings below 0.4, using `03_renamed`
> - Matcher: correct pairing rate on `03_renamed`. **Zero wrong pairs is a hard requirement** — assert that no pair is incorrect, and separately measure how many are unmatched.
> - Matcher: a constructed case where a greedy loop gives the wrong answer and optimal assignment gives the right one
> - Planner: collisions using `09_collision`, reserved names, invalid characters, path length, locked files
> - Executor: cancel halfway, verify the undo log is complete and correct
> - Undo: full reversal, and refusal to reverse when a file changed externally
> - Golden test: `08_naming_mess` produces an exact expected rename plan
>
> Then a safety review pass: confirm no code path can write to, move, or delete anything inside the two input folders in copy mode. Add an assertion in the executor that raises if a target path is inside an input folder while in copy mode.

---

## Part D — Definition of done

- [ ] `03_renamed` pairs at over 95% correct with **zero wrong pairs**
- [ ] Unicode lookalike characters are handled
- [ ] The matcher uses optimal assignment, proven by a test where greedy fails
- [ ] Ambiguous matches are presented, never guessed
- [ ] Manual pairing works from the unmatched lists
- [ ] The template preview updates live with three real filenames
- [ ] Collisions are detected before applying, never after
- [ ] Reserved names, invalid characters, and long paths are all caught
- [ ] Copy mode provably never touches the input folders
- [ ] In-place mode requires explicit opt-in and shows a warning
- [ ] Undo fully reverses a rename and refuses when a file changed externally
- [ ] Cancel works mid-rename and leaves a valid undo log
- [ ] Reviewing 40 pairs by keyboard takes under two minutes
- [ ] `pytest` passes, `ruff check .` is clean
- [ ] The packaged `.exe` still works

Merge and tag `v0.3.0-matching`.

---

## Part E — The real test

Two tests, both on real data:

**1. The matcher test.** Take a real set where the naming standard changed, run it, and check every pair by hand. Count correct, wrong, and unmatched. **Wrong pairs are the number that matters.** If there is even one wrong pair, raise the thresholds until there are none, and accept more unmatched drawings instead.

**2. The rename test.** Take a real received issue, rename it with the ISO 19650 preset, and time yourself doing the same job by hand. If the app saves 90 minutes, you have a feature people will pay for on its own.

---

## Final recommendation

**Task 3.3 and Task 3.6 are the two that matter.** The matcher decides whether Phase 4 gets correct input, and the executor is the only part of this application that can lose someone's files.

**On the matcher, tune for zero wrong pairs, not for high coverage.** It is tempting to push the threshold down to match more drawings. Resist it. An unmatched drawing costs the user 10 seconds of manual pairing. A wrong pair produces a comparison report full of invented changes and costs you the user's trust permanently.

**On the executor, build the undo log before the rename logic.** Write the log first, then the operation, and test the undo path before you ever run a real rename. It is the difference between a bug and a disaster.

**One thing worth considering:** Half B, the bulk rename, may be worth releasing on its own as a small free tool. It is easy to explain, it saves a document controller two hours per issue, and it puts your name in front of exactly the people who will later buy the comparison product. Something to think about once Phase 3 works.
