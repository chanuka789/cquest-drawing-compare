# Phase 5 — Compare & Mask
### Detailed build plan for use with Claude Code

**Goal:** Given two perfectly aligned sheets, find every change that matters and report none that do not.

**The measure of success is not how many changes you find. It is how few wrong ones you report.** If the first sheet a user opens shows 300 changes when two doors moved, they will close the tool and never open it again. Everything in this phase is built around that.

**Time:** 3–4 weeks part-time. Sixteen tasks.

---

## Part A — How to organise the work

```
                    5.0 Change-injection harness  ← BUILD FIRST
                              │
    ┌─────────────────────────┼─────────────────────────┐
    ▼                         ▼                         ▼
WORKSTREAM A            WORKSTREAM B            WORKSTREAM C
Masking                 Text stream             Geometry streams
 5.1 Template detect     5.5 Classification       5.8 Path extraction
 5.2 Title block zones   5.6 Text matching        5.9 Vector diff
 5.3 Watermarks/stamps   5.7 Numeric analysis     5.10 Hatch regions
 5.4 Mask engine + UI                             5.11 Raster diff
                                                  5.12 Noise filters
    └─────────────────────────┼─────────────────────────┘
                              ▼
                    5.13 Tolerance engine
                    5.14 Comparison orchestrator
                    5.15 Change persistence + API
                    5.16 Validation and tests
```

**Solo build order:** 5.0 → 5.13 → 5.2 → 5.4 → 5.5 → 5.6 → 5.7 → 5.11 → 5.12 → 5.8 → 5.9 → 5.10 → 5.1 → 5.3 → 5.14 → 5.15 → 5.16

The text stream comes before the geometry streams deliberately. It is the highest-value output, the easiest to get right, and it gives you something demonstrable within the first week.

### A1. Branch and fixtures

```powershell
git checkout main && git pull
git checkout -b phase-5-compare
```

New fixtures:

```
11_comparison\
├── rev_letter_only\    # ★ IDENTICAL sheets, only the revision letter differs
│                       #   MUST report zero changes. This is the gate.
├── line_weight\        # same drawing, replotted with different pen weights
├── colour_vs_mono\     # same drawing, colour plot vs monochrome plot
├── font_substituted\   # same text, different font (font not installed at plot)
├── layer_toggled\      # a layer switched off between issues
├── real_small\         # 2-3 genuine changes only
├── real_heavy\         # 20+ genuine changes
├── dimension_only\     # a dimension text changed, the line did not move
├── hatch_change\       # blockwork hatch changed to concrete hatch
├── renumbered\         # door marks renumbered, nothing else changed
└── watermarked\        # one issue stamped PRELIMINARY, the other not
```

**`rev_letter_only` is the most important fixture in the project.** Build it first: copy a sheet, edit only the revision letter in the title block, re-export. Every noise filter you write is tested against it.

### A2. Update `CLAUDE.md`

```markdown
## Current phase

Phase 5 — Compare and mask. Finding real changes between aligned sheets.
Clustering into readable regions, severity scoring, and reporting are
Phase 6. Phase 5 produces raw, filtered change records.

## Phase 5 rules

- A false positive costs more than a missed change. When a difference
  could be cosmetic, mark it cosmetic and hide it by default.
- Text changes are the highest-value output. They get their own stream
  and their own report section. Never let them get buried under geometry.
- All tolerances are specified by the user in real-world millimetres at
  drawing scale, converted per sheet. Never expose pixels to the user.
- Never report a hatch region as thousands of individual line changes.
  One region, one change.
- If every stroke on the sheet shows a thin halo of difference, that is
  alignment residual, not a design change. Detect it and say so.
- The gate for this phase: rev_letter_only must report ZERO changes.
```

---

## Part B — The technical design

---

### ★ B1. The core insight: three streams, not one

Most comparison tools do a pixel diff and stop. That is why they produce unusable output on construction drawings.

| Stream | Compares | Value | Reliability |
|---|---|---|---|
| **Text** | Strings with coordinates | ★★★★★ Highest | Very high |
| **Vector** | Path geometry | ★★★★ High | High on CAD PDFs, unavailable on scans |
| **Raster** | Pixels | ★★ Fallback | Noisy, but always available |

**Why text ranks first.** Consider what actually costs money on a construction project:

- A dimension changing from 3000 to 3200 — a wall moved, quantities change
- A note changing from "1 hour fire rated" to "2 hour fire rated" — a completely different partition specification
- A wall tag changing from W1 to W3 — different build-up, different rate
- A level changing from +3.600 to +3.750 — slab thickness or finish change
- A hatch changing from blockwork to concrete — different trade entirely

Four of those five are **text changes**. In a raster diff they are a handful of dark pixels lost among thousands of pixels from line weight noise. In a text diff they are the first thing on the report.

**Run all three streams, keep their results separate, and present text first.**

---

### B2. Masking — what must never be compared

| Element | Why exclude | How to find it |
|---|---|---|
| Title block fields | Revision, date, signature change every issue | Zone detection plus profile |
| Revision history table | Read it, do not diff it — it often describes the change in words | Table structure below the title block |
| Company logo | Sometimes re-exported at a different resolution | Inside the title block zone |
| Watermarks | `PRELIMINARY`, `SUPERSEDED`, `NOT FOR CONSTRUCTION`, `DRAFT` | Large rotated low-density text |
| Print stamps, QR codes | Added by document control after issue | Corner regions, high-frequency square patterns |
| Digital signature blocks | Added post-issue | Annotation layer |
| Post-issue annotations | Bluebeam comments are not design changes | Already separated in Phase 4 |
| Sheet frame lines | Identical every time, pure noise | Long straight lines near the page edge |

**What to keep, despite the temptation:**
- **North arrow** — a rotation here is a real and significant change
- **Scale bar** — a change means the drawing scale changed
- **Key plan** — the highlighted zone changing can be meaningful
- **General notes** — a change here is often the single most expensive change on the sheet

### ★ B3. Sheet templates — detect once, apply to hundreds

Every project uses two or three sheet templates. The title block sits in the same place on all 300 sheets.

So do not detect the title block 300 times. Instead:

1. Fingerprint each sheet's frame geometry: page size, the positions of long straight lines near the edges, and the enclosed rectangular regions
2. Cluster sheets by that fingerprint — typically two or three clusters per project
3. Detect zones once per cluster, on a representative sheet
4. **Show the user the detected zones and let them adjust with a rectangle tool**
5. Save to the profile, apply to every sheet in the cluster

This turns an unreliable 300-times-repeated detection into one reliable detection the user confirms in ten seconds. Automatic detection will never be perfect; a confirmed template is.

**Detecting the title block within a template:**
1. Find long straight lines near the right and bottom edges (Hough lines, minimum length 30% of page dimension)
2. Build the rectangular regions they enclose
3. The title block is the region containing label text such as `DRAWING NO`, `DWG NO`, `SCALE`, `DRAWN`, `CHECKED`
4. Sub-zones inside it: the revision field is near `REV`, the date field near `DATE`, and so on

**Detecting watermarks:**
- Text rotated to something other than 0° or 90°, commonly 45°
- Font size several times the median on the sheet
- Bounding box spanning more than 40% of the page
- Low ink density (usually grey or outlined)
- Or the string matches a known word list

Any two of those conditions is enough.

---

### ★ B4. The text stream in detail

**Step 1 — Transform old text into new coordinates.** Apply the Phase 4 alignment matrix to every old text item's position. Now both sets live in the same space.

**Step 2 — Classify every text item.** No AI needed, just patterns:

| Category | Pattern | Example |
|---|---|---|
| `dimension` | Pure number, or number plus mm/m, near a dimension line | `3000`, `2400 mm` |
| `level` | `+n.nnn`, `-n.nnn`, `±0.000`, `FFL`, `SSL` | `+3.600`, `FFL +0.150` |
| `tag` | Short alphanumeric, 1–6 characters, often enclosed | `D-12`, `W3`, `C1` |
| `room` | Text inside a closed region, often with an area figure | `BEDROOM 1` |
| `scale` | `1:n`, `NTS`, `AS SHOWN` | `1:100` |
| `grid` | Single letter or number inside a circle | `A`, `12` |
| `note` | A sentence, more than four words | `Provide 100mm blockwork...` |
| `spec` | Contains specification keywords | `FIRE RATED`, `WATERPROOF`, `HR`, `MIN` |
| `titleblock` | Falls inside a masked zone | (excluded) |

Classification drives severity later, and it drives how the change is described in words.

**Step 3 — Match items between the two sets.**

1. Exact match: identical normalised string within the position tolerance → **unchanged**
2. Position match: identical string, position differs by more than tolerance → **moved**
3. For everything still unmatched: within a search radius (default 15 mm on paper), build a score matrix of old-unmatched against new-unmatched, scoring on string similarity and proximity, then solve with optimal assignment. Matched pairs are **modified**.
4. Whatever remains: old-only is **removed**, new-only is **added**

Use optimal assignment here for the same reason as Phase 3 — a greedy nearest-match loop mispairs text in dense areas like dimension strings, where five numbers sit within centimetres of each other.

**Step 4 — Analyse numeric changes.** When both sides parse as numbers, compute the absolute delta, the percentage delta, and the real-world delta at drawing scale.

Report it the way a QS reads it:

> Dimension changed: 3000 → 3200 (+200 mm, +6.7%)

**Step 5 — Two traps that must both be handled.**

- A **dimension text changes while the line does not move.** Either the drawing was previously wrong, or it is marked not-to-scale. This is a real finding.
- A **line moves while the dimension text stays the same.** Also a real finding, and often a drafting error worth flagging.

Detect both by cross-referencing the text stream against the geometry stream in the same region. When they disagree, say so explicitly — this is the kind of finding that makes a QS trust the tool.

---

### ★ B5. The vector stream

When the PDF carries real vectors, compare geometry rather than pixels. Far cleaner.

**Normalisation before comparison — this is where it succeeds or fails:**

1. **Flatten curves** to polylines at a fixed tolerance (0.1 mm on paper). Two Béziers with different control points can render identically; flattened, they become identical point sequences.
2. **Merge collinear connected segments.** One line drawn as three segments must equal the same line drawn as one.
3. **Canonicalise direction.** A path from A to B must hash the same as the same path from B to A. Pick a deterministic start point (lowest x, then lowest y) and direction.
4. **Quantise coordinates** to the tolerance grid before hashing.
5. **Separate geometry from style.** Line weight, dash pattern and colour live in the graphics state, not the path. Hash the geometry alone. Compare style separately and report style-only differences as cosmetic.

Then it is a set operation: hash every normalised path on both sides. Hashes only in old are removed, only in new are added, present in both are unchanged.

For near-matches — the same shape shifted slightly — do a spatial search among the unmatched sets and report as moved rather than as one removal plus one addition.

### ★ B6. Hatches — the single biggest source of vector noise

A hatched wall is not one object. It is often two thousand short parallel line segments.

Compare them individually and one hatch pattern change produces two thousand change records. The report is destroyed.

**Treat hatch as a region:**

1. **Detect** dense clusters of short parallel segments — more than 20 segments, similar angles within 2°, regular spacing
2. **Characterise** the region: bounding polygon, dominant angle, mean spacing, segment count, whether double-hatched (cross-hatch)
3. **Compare region signatures** between sheets
4. **Report one change per region:**
   - Region present in both, same signature → unchanged
   - Region present in both, different angle or spacing → **hatch pattern changed** (material change — high value)
   - Region only in new → hatched area added
   - Region only in old → hatched area removed
   - Region boundary changed → hatched area resized, with the area delta in m²

A hatch change from blockwork to concrete is a genuine trade change and one of the most valuable findings the tool can produce. It deserves one clear record, not two thousand.

---

### ★ B7. The raster stream — tolerant difference, not XOR

A plain pixel XOR on line drawings is useless. Every line has slightly different anti-aliasing, and a one-pixel line weight change lights up the entire sheet.

**Use a distance-transform tolerance instead:**

1. Binarise both aligned images with adaptive thresholding (handles uneven scan lighting)
2. Compute the distance transform of the new image's ink
3. For every ink pixel in the old image, look up its distance to the nearest new ink. If that distance is under the tolerance, it is **matched**.
4. Repeat in the other direction: new ink against the old image's distance transform
5. Unmatched pixels on either side are the actual difference

This handles line weight change, sub-pixel jitter, anti-aliasing and minor alignment residual in one step — because it asks "is there ink nearby?" rather than "is there ink exactly here?".

Then: connected components on the unmatched pixels, drop components smaller than the minimum size, and cluster what remains into regions.

---

### ★ B8. Noise filters — the adoption decision

Each of these is a specific, common false positive with a specific fix.

| Noise | How to detect it | Fix |
|---|---|---|
| **Line weight change** | The estimated global stroke width differs between sheets | Measure stroke width via distance transform on the skeleton; normalise both before comparing |
| **Line type change** | Path geometry hashes match, dash arrays differ | Compare geometry only; report as cosmetic |
| **Colour vs mono plot** | Colour histograms differ, binarised images agree | Always binarise before geometry comparison |
| **Font substitution** | Text strings match exactly, glyph pixels differ | Where the text stream reports "unchanged", suppress raster differences inside that text's bounding box |
| **Redrawn in place** | Vector object IDs differ, normalised geometry hashes match | Geometry hashing handles this by construction |
| **Layer toggled** | All changed content shares one PDF optional content group | Report as a single "layer visibility changed" record |
| **Background or xref updated** | One large coherent region changed | Report as one region, not thousands of fragments |
| **Renumbered tags** | Many tag-category text changes, positions unchanged, and the old and new values follow a consistent mapping | Detect the pattern and report as one "tags renumbered" record listing the mapping |
| **Anti-alias and JPEG speckle** | Isolated components of 1–3 pixels | Minimum component size, plus morphological opening |
| **Scan speckle** | Random isolated dots across the whole sheet | Median filter before binarising |
| **★ Alignment residual** | The change map is dominated by thin halos that trace existing strokes | **Detect and warn**, do not report as change |

### ★ B9. The alignment-residual detector

This one is worth building carefully, because without it a slightly imperfect alignment silently produces hundreds of fake changes.

**How to detect it:** skeletonise the change mask. If the resulting components are predominantly thin (under 3 px wide), long, and their centrelines lie within 2 px of strokes present in *both* images, the change map is tracing existing geometry rather than showing new geometry.

**What to do:** do not report these as changes. Instead surface a warning on the sheet:

> "This sheet aligned to 2.8 mm accuracy, which is not tight enough for reliable comparison at 1:50. Try manual alignment for a better result."

This turns a silent failure into an honest, actionable message.

---

### B10. Tolerance — expressed the way a QS thinks

The conversion chain:

```
site mm  →  ÷ scale denominator  →  paper mm  →  × px_per_mm  →  pixels
```

At 1:100 and 200 DPI: 50 mm on site = 0.5 mm on paper = 3.9 px.

**Two independent tolerances:**

| Tolerance | Default | Meaning |
|---|---|---|
| Position | 25 mm on site | How far something may move before it counts |
| Minimum region size | 50 mm on site | How small a change region may be before it is ignored |

**The clamp that stops absurd results.** A 1:5 detail sheet with a 25 mm site tolerance gives 5 mm on paper — enormous, and it would hide nearly everything. A 1:500 site plan gives 0.05 mm — smaller than a pixel.

So: convert per sheet using that sheet's own scale, then clamp the paper-millimetre result between 0.2 mm and 3.0 mm. Show the user the effective tolerance for each sheet so nothing is hidden.

For sheets with no scale (NTS, details), fall back to a paper-millimetre default of 0.5 mm and label them clearly.

---

### B11. Performance budgets

| Operation | Target |
|---|---|
| Text stream, one pair | < 500 ms |
| Vector stream, one pair | < 3 s |
| Hatch region detection | < 2 s |
| Raster stream, A1 at 200 DPI | < 4 s |
| Noise filter pass | < 1 s |
| Full comparison, one pair | < 8 s |
| Batch of 100 pairs | < 12 min, 6 workers |

Run the raster stream on downsampled images for region detection, then refine only the detected regions at full resolution. Most of a sheet is unchanged; do not pay full price for it.

---

## Part C — The sixteen tasks

---

### ★ Task 5.0 — The change-injection harness (build first)

The Phase 4 equivalent of ground truth. Without it you cannot measure precision and recall, and you will tune filters by feel.

**Prompt:**

> Build `tests/harness/inject.py`, a synthetic change generator.
>
> `inject_changes(source_pdf, page, change_spec) -> InjectedPair`
>
> Takes a real drawing and produces a modified version with a known list of changes. Supported injections:
> - `move_object(bbox, dx_mm, dy_mm)` — cut a region and paste it offset
> - `delete_object(bbox)` — erase a region to white
> - `add_object(bbox, shape)` — draw a rectangle, circle or line
> - `change_text(old_string, new_string)` — find and replace text on the sheet
> - `change_dimension(old_value, new_value)` — a numeric text change
> - `change_hatch(bbox, new_angle, new_spacing)` — replace a hatch pattern
> - `renumber_tags(mapping)` — bulk tag renumbering
>
> And cosmetic injections that must **not** be reported:
> - `change_line_weight(factor)`
> - `change_line_type(pattern)`
> - `substitute_font(new_font)`
> - `plot_mono()` — colour to monochrome
> - `add_watermark(text)`
> - `change_revision_letter(new_rev)`
> - `toggle_layer(layer_name)`
>
> Return both sheets plus a ground-truth change list with exact bounding boxes and types.
>
> Then build `tests/harness/precision_recall.py`:
> - Runs the comparison engine on injected pairs
> - Matches reported changes against ground truth by bounding box overlap (IoU above 0.3)
> - Reports **precision** (of what was reported, how much was real), **recall** (of what was real, how much was found), and **the false positive count**
> - A separate cosmetic-only run: apply only cosmetic injections and assert the reported change count is **zero**
> - Outputs a CSV plus a summary table, with a golden file for regression
>
> The headline metric for Phase 5 is **false positives on cosmetic-only pairs. It must be zero.**

---

### Task 5.1 — Sheet template detection and clustering

**Prompt:**

> Build `engine/masking/template_detect.py`.
>
> `fingerprint_frame(sheet) -> FrameFingerprint`
> - Page size and orientation
> - Long straight lines near the page edges, found with `cv2.HoughLinesP`, minimum length 30% of the page dimension, positions normalised to fractions of page size
> - The rectangular regions those lines enclose
> - A hash of that structure, quantised so minor variation does not split clusters
>
> `cluster_sheets(sheets) -> list[SheetTemplate]`
> - Groups sheets by frame fingerprint similarity
> - Picks a representative sheet per cluster — the one closest to the cluster centre
> - Typically produces two or three clusters per project
> - Returns each cluster's member sheets and its representative
>
> `apply_template(template, sheet) -> Zones` — maps zones from the representative onto any sheet in the cluster, adjusting for small page size differences.
>
> This is the key efficiency: detect zones once per template, not once per sheet. Detection on 300 sheets is unreliable; detection on three representatives that a user confirms is reliable.

---

### Task 5.2 — Title block and frame zone detection

**Prompt:**

> Build `engine/masking/titleblock_zones.py`.
>
> `detect_zones(sheet) -> DetectedZones`
>
> 1. Find long straight lines near the right and bottom edges
> 2. Build the enclosed rectangular regions from their intersections
> 3. Identify the title block: the region containing two or more label keywords from `DRAWING NO`, `DWG NO`, `DRG NO`, `SHEET NO`, `SCALE`, `DRAWN`, `CHECKED`, `APPROVED`, `DATE`, `PROJECT`, `CLIENT`
> 4. Identify sub-zones inside it by proximity to their labels: revision field, date field, drawn-by, checked-by, sheet number, scale
> 5. Identify the revision history table: a gridded region, usually directly above or beside the title block, with a header row containing `REV`, `DATE`, `DESCRIPTION` or `AMENDMENT`
> 6. Identify the sheet frame: the outer border rectangle
>
> Return every zone with a confidence score and the evidence used, so the UI can explain what it found.
>
> **Extract the revision history table content separately** — parse its rows into revision, date and description. Do not diff it, but keep the text: it often states in words what changed, and the Phase 6 cloud cross-check will use it.
>
> Never mask the north arrow, the scale bar, the key plan or the general notes. Detect and explicitly protect those regions.

---

### Task 5.3 — Watermark and stamp detection

**Prompt:**

> Build `engine/masking/watermark_detect.py`.
>
> `detect_watermarks(sheet) -> list[Watermark]`
>
> Score each text item against these signals, requiring two or more:
> - Rotation is neither 0° nor 90° (45° is typical)
> - Font size more than 3× the sheet median
> - Bounding box spans more than 40% of a page dimension
> - Fill is grey or outlined rather than solid black
> - The string matches a known list: `PRELIMINARY`, `DRAFT`, `SUPERSEDED`, `NOT FOR CONSTRUCTION`, `FOR APPROVAL`, `FOR INFORMATION`, `VOID`, `COPY`, `CONFIDENTIAL` — configurable
>
> `detect_stamps(sheet) -> list[Stamp]`
> - QR codes and barcodes: high-frequency square patterns, use `cv2.QRCodeDetector` where available
> - Digital signature blocks: rectangular regions in a page corner containing keywords such as `Digitally signed`, `Signature`, `Verified`
> - Received or approval stamps: bordered rectangular regions with date text, usually in a corner
>
> `detect_annotations(sheet)` — reuse the annotation layer already separated during Phase 4 rendering. Post-issue Bluebeam comments are not design changes.
>
> Return each detection with a confidence score. Anything below 0.7 is proposed to the user rather than applied silently.

---

### Task 5.4 — Mask engine and the mask editor

**Prompt:**

> Build `engine/masking/mask_engine.py` and `ui/src/screens/Compare/MaskEditor.tsx`.
>
> **Backend:**
> - `MaskSet` — a named collection of rectangular and polygonal zones, each with a type (`titleblock`, `watermark`, `stamp`, `annotation`, `frame`, `user`) and an enabled flag
> - `build_mask(sheet, template, detections, user_zones) -> MaskSet`
> - `apply_mask(image_or_items, mask_set)` — works on raster images, text item lists, and vector path lists alike
> - `save_to_profile` / `load_from_profile` — masks persist per sheet template and are reused across the project
> - `invert_mask` — a debug view showing only what is being excluded
>
> **Frontend mask editor:**
> - Displays the representative sheet with detected zones drawn as translucent overlays, colour-coded by type
> - Each zone can be toggled, resized by dragging handles, moved, or deleted
> - A rectangle tool to draw new exclusion zones, and a polygon tool for irregular shapes
> - A "protected regions" list showing what the app deliberately kept: north arrow, scale bar, key plan, general notes. The user can protect more.
> - A live preview toggle that greys out everything masked, so the user sees exactly what will be compared
> - "Apply to all 143 sheets using this template" as the primary action
>
> The default flow: auto-detect, show the user, let them adjust in ten seconds, apply to the whole cluster. Never require per-sheet work.

---

### Task 5.5 — Text item classification

**Prompt:**

> Build `engine/compare/text_classify.py`.
>
> `classify(text_item, context) -> TextCategory` returning one of: `dimension`, `level`, `tag`, `room`, `scale`, `grid`, `note`, `spec`, `titleblock`, `unknown`.
>
> Rules, all configurable in the profile:
> - `dimension` — matches a pure number or number-with-unit pattern, and lies within 8 mm on paper of a dimension line (a short line with terminators). Handle `3000`, `3,000`, `2400mm`, `2.4m`, `3000/2` and dimension strings.
> - `level` — matches `[+-±]?\d+\.\d{2,3}` or is preceded by `FFL`, `SSL`, `TOS`, `SOF`, `IL`, `CL`
> - `tag` — 1 to 6 alphanumeric characters, and either enclosed in a detected circle, hexagon or rectangle, or matching a configured tag pattern such as `D-\d+` or `W\d+`
> - `room` — text inside a closed polygon region, often adjacent to an area value
> - `scale` — matches `1\s*:\s*\d+`, or equals `NTS` or `AS SHOWN`
> - `grid` — a single letter or number inside a detected grid bubble
> - `spec` — contains keywords from a configurable list: `FIRE`, `RATED`, `HR`, `HOUR`, `WATERPROOF`, `ACOUSTIC`, `MIN`, `MAX`, `THK`, `GRADE`, `CLASS`
> - `note` — more than four words
> - `titleblock` — falls inside a masked title block zone
>
> `parse_numeric(text) -> ParsedNumber | None` — extracts a value and a unit, handling thousands separators, decimals, fractions and ranges.
>
> Return a confidence with each classification. Write tests covering realistic drawing text from all disciplines.

---

### Task 5.6 — Text matching and diff ★ Highest-value task

**Prompt:**

> Build `engine/compare/text_diff.py`.
>
> `diff_text(old_items, new_items, transform, tolerance, mask) -> TextDiffResult`
>
> 1. Apply the alignment transform to every old item's position, so both sets are in the same coordinate space
> 2. Drop items inside masked zones
> 3. Normalise strings: NFKC, collapse whitespace, and optionally case-fold (configurable — case changes in specification text can matter)
>
> Matching, in order:
> - **Exact**: identical string within the position tolerance → `unchanged`
> - **Moved**: identical string, position differs by more than the tolerance → `moved`, with the distance in millimetres on site
> - **Modified**: for all remaining unmatched items, build a score matrix within a search radius (default 15 mm on paper) scoring on string similarity (`rapidfuzz`) and inverse distance, then solve with `scipy.optimize.linear_sum_assignment`. Pairs above the score threshold become `modified`.
> - **Added / Removed**: whatever remains
>
> **Use optimal assignment, not a greedy loop.** In dimension strings, five numbers can sit within centimetres of each other, and greedy matching mispairs them, turning one real change into two false ones.
>
> Each result carries: category, old text, new text, both positions, the change type, distance moved, string similarity, and a plain-English description.
>
> `detect_renumbering(changes) -> RenumberPattern | None` — when many `tag`-category modifications share a consistent old-to-new mapping and their positions are unchanged, report one "tags renumbered" record with the mapping instead of dozens of separate changes.

---

### Task 5.7 — Numeric and dimension analysis

**Prompt:**

> Build `engine/compare/dimension_diff.py`.
>
> `analyse_numeric_change(change, sheet_scale) -> NumericAnalysis`
> - Parse both sides as numbers where possible
> - Absolute delta, percentage delta, and the real-world delta at drawing scale
> - Direction: increased or decreased
> - A formatted description: `3000 → 3200 (+200 mm, +6.7%)`
>
> `cross_check_geometry(text_change, geometry_changes, radius) -> CrossCheckResult`
>
> Two cases that must both be caught:
> - **Dimension text changed but no geometry moved nearby** → flag as `dimension_text_only`. Either the drawing was previously wrong, or the item is not to scale. A real finding.
> - **Geometry moved but the nearby dimension text is unchanged** → flag as `geometry_moved_dimension_stale`. Often a drafting error, and worth raising.
>
> `analyse_level_change(change)` — same treatment for level values, which are usually in metres while dimensions are in millimetres. Handle the unit difference explicitly.
>
> `estimate_quantity_impact(change, sheet_scale)` — for a dimension change on a linear element, report the length delta. **Only where the geometry is available and unambiguous. Never guess a quantity.** Return `None` rather than an estimate when confidence is low.

---

### Task 5.8 — Path extraction and normalisation

**Prompt:**

> Build `engine/extract/vector_extractor.py` and `engine/compare/path_normalise.py`.
>
> **Extraction** using `pypdfium2`: every path object with its point sequence, fill and stroke state, line width, dash array, colour, and any owning optional content group.
>
> **Normalisation** — the step that decides whether vector comparison works:
> 1. **Flatten** Bézier curves to polylines at 0.1 mm on paper. Two curves with different control points that render identically must normalise to the same points.
> 2. **Merge** connected collinear segments within an angular tolerance of 0.5°. One line drawn as three segments must equal the same line drawn as one.
> 3. **Canonicalise direction**: choose a deterministic start point (lowest x, then lowest y) and traverse in a deterministic direction, so a path and its reverse hash identically.
> 4. **Quantise** coordinates to the tolerance grid
> 5. **Separate style from geometry**: hash the point sequence alone. Keep line width, dash array and colour in a separate style hash.
>
> `normalise_path(path, tolerance) -> NormalisedPath` with `geometry_hash`, `style_hash`, `bbox`, `length`, and `point_count`.
>
> Also handle: degenerate paths (zero length), paths entirely outside the page, and clipping paths, which should be excluded as they are not drawn content.

---

### Task 5.9 — Vector geometry diff

**Prompt:**

> Build `engine/compare/vector_diff.py`.
>
> `diff_vectors(old_paths, new_paths, transform, tolerance, mask) -> VectorDiffResult`
>
> 1. Apply the alignment transform to old path coordinates
> 2. Drop paths inside masked zones
> 3. Normalise both sets
> 4. Set comparison on geometry hashes: in both → unchanged, old only → candidate removal, new only → candidate addition
> 5. For candidates, spatial near-match: build an R-tree over the unmatched new paths and search near each unmatched old path. When a path of similar length and shape is found within the tolerance, classify as `moved` rather than as one removal plus one addition.
> 6. Where geometry hashes match but style hashes differ, emit a `style_only` change flagged as cosmetic
>
> Report per change: type, bounding box, path count involved, total length in millimetres on site, and geometry type where identifiable (line, rectangle, polyline, circle, arc).
>
> Use `shapely` for the geometric operations and an R-tree spatial index — a linear scan across 50,000 paths is far too slow.
>
> Handle graceful degradation: if a PDF has fewer than 50 path objects it is probably a scanned image, so skip the vector stream and record why.

---

### Task 5.10 — Hatch region detection and comparison ★

**Prompt:**

> Build `engine/compare/hatch.py`. This prevents the single largest source of vector noise.
>
> `detect_hatch_regions(paths) -> list[HatchRegion]`
> 1. Filter to short segments — under 15 mm on paper
> 2. Group by angle into 2° buckets
> 3. Within each bucket, cluster spatially (DBSCAN on segment midpoints)
> 4. A cluster qualifies as hatch when it contains more than 20 segments with regular spacing (low variance in perpendicular distance between neighbours)
> 5. Detect cross-hatch by finding two overlapping clusters at different angles in the same area
> 6. Compute the region's bounding polygon as the concave hull of its segments
>
> Each `HatchRegion` carries: polygon, dominant angle(s), mean spacing, segment count, area in m² at drawing scale, and a `signature` combining angle, spacing and cross-hatch flag.
>
> `diff_hatch(old_regions, new_regions, transform) -> list[HatchChange]`
> - Match regions by polygon overlap (IoU above 0.5)
> - Same signature, same polygon → unchanged
> - **Different signature, same polygon → `hatch_pattern_changed`.** A material change. High value — for example blockwork to concrete.
> - Same signature, different polygon → `hatch_area_changed`, with the area delta in m²
> - New only → `hatch_added`, with area
> - Old only → `hatch_removed`, with area
>
> **One change record per region. Never per segment.** A single hatch region can contain two thousand segments; reporting them individually destroys the report.
>
> Exclude hatch segments from the general vector diff once they belong to a detected region, so they are not counted twice.

---

### Task 5.11 — Tolerant raster diff

**Prompt:**

> Build `engine/compare/raster_diff.py`.
>
> `diff_raster(old_img, new_img, transform, tolerance_px, mask) -> RasterDiffResult`
>
> 1. Warp the old image by the alignment transform into the new image's space, using `cv2.INTER_NEAREST` for binary content to avoid introducing grey edges
> 2. Binarise both with adaptive Gaussian thresholding — handles uneven lighting on scans
> 3. Apply the mask
> 4. **Distance-transform tolerant matching**, not XOR:
>    - Compute `cv2.distanceTransform` on the inverse of the new binary image
>    - Old ink pixels whose distance to the nearest new ink is under `tolerance_px` are matched
>    - Repeat in the opposite direction
>    - Unmatched pixels on either side form the difference mask
>    This absorbs line weight change, sub-pixel jitter, anti-aliasing and small alignment residual in one operation.
> 5. Morphological opening with a 2×2 kernel to remove speckle
> 6. Connected components on the difference mask
> 7. Drop components below the minimum area threshold
> 8. Return regions with bounding box, pixel area, area in mm² on site, and whether the ink was added or removed
>
> Also `estimate_stroke_width(binary_img)` — via distance transform on the skeleton — used by the line weight noise filter to normalise before comparing.
>
> Performance: run steps 1 to 7 on a 4× downsampled image to find candidate regions, then re-run at full resolution only inside those regions. Most of a sheet is unchanged and should not be paid for.

---

### Task 5.12 — The noise filter suite ★ Determines adoption

**Prompt:**

> Build `engine/compare/noise_filter.py`. Each filter takes the raw change list plus both sheets and returns changes with `is_cosmetic` set, or removed entirely.
>
> - `filter_line_weight` — estimate global stroke width on both sheets. If they differ by more than 20%, normalise (dilate the thinner) and re-run the raster diff. Mark residual differences that vanish after normalisation as cosmetic.
> - `filter_line_type` — geometry hash matches, dash array differs → cosmetic
> - `filter_colour_plot` — colour histograms differ substantially but binarised images agree → cosmetic, and record a note that plot settings changed
> - `filter_font_substitution` — where the text stream reports a string as unchanged, suppress raster changes inside that string's bounding box
> - `filter_layer_toggle` — if all changes in a region share one optional content group present in one sheet and absent in the other, collapse to a single `layer_visibility_changed` record naming the layer
> - `filter_speckle` — remove components under the minimum area, and remove isolated components with no neighbour within 5 mm
> - `filter_background_update` — one large coherent region (over 15% of sheet area) with high internal change density → collapse to a single `background_updated` record
>
> **`detect_alignment_residual(change_mask, old_binary, new_binary) -> ResidualReport`**
> - Skeletonise the change mask
> - Measure the width distribution of its components
> - If more than 70% of change pixels lie in components under 3 px wide, and their centrelines fall within 2 px of strokes present in **both** images, the change map is tracing existing geometry
> - Return `is_residual=True` with the estimated residual magnitude in millimetres
> - When residual is detected, **do not report the changes**. Emit a sheet-level warning with a suggestion to realign manually.
>
> Every filter records what it removed and why, so a debug view can show the user exactly what was suppressed. Nothing is ever silently discarded.

---

### Task 5.13 — The tolerance engine

**Prompt:**

> Build `engine/compare/tolerance.py`.
>
> `ToleranceSpec` — position tolerance and minimum region size, both specified in real-world millimetres at drawing scale, defaults 25 mm and 50 mm.
>
> `resolve_for_sheet(spec, sheet) -> ResolvedTolerance`
> - Read the drawing scale from the title block, from the Phase 2 extraction
> - Convert: site mm ÷ scale denominator = paper mm; paper mm × px_per_mm = pixels
> - **Clamp the paper-millimetre result between 0.2 mm and 3.0 mm.** Without this, a 1:5 detail gets a 5 mm paper tolerance that hides everything, and a 1:500 site plan gets 0.05 mm, smaller than a pixel.
> - Where no scale is available (NTS, unparsed), fall back to a paper default of 0.5 mm and set `scale_known=False`
> - Return the resolved values in all three units plus whether the clamp was applied
>
> `format_for_user(value_px, sheet)` — always presents both: "0.4 mm on paper (40 mm on site at 1:100)". **Never show a user a pixel count.**
>
> Add a per-sheet tolerance override, stored with the comparison, for the case where one drawing needs different treatment.

---

### Task 5.14 — The comparison orchestrator

**Prompt:**

> Build `engine/compare/orchestrator.py`.
>
> `compare_pair(pair, alignment, config) -> ComparisonResult`
>
> Pipeline:
> 1. Refuse to proceed unless the alignment verdict is `good` or better, or the user explicitly overrode it. Record the reason for skipping.
> 2. Resolve tolerances for this sheet
> 3. Build the mask set from the sheet template plus any user zones
> 4. Run the **text stream** — always
> 5. Run the **vector stream** — if both PDFs have usable vectors
> 6. Run **hatch detection and diff** — as part of the vector stream
> 7. Run the **raster stream** — always, as the safety net
> 8. Run the **alignment residual detector**. If residual is detected, abandon the geometry results, keep the text results, and attach a sheet-level warning.
> 9. Run the **noise filter suite** across all streams
> 10. **Deduplicate across streams** — the same physical change will appear in more than one stream. Merge by bounding box overlap, keeping the richest description, and record which streams found it. A change found by two streams gets higher confidence.
> 11. Cross-check dimensions against geometry
> 12. Primitive clustering: merge change regions whose bounding boxes are within 5 mm on paper. Semantic clustering and severity are Phase 6.
>
> Return: the change list, per-stream statistics, what was filtered and why, the effective tolerances, any warnings, and timings per stage.
>
> `compare_batch(pairs, config, progress_callback)` — process pool, resumable, cancellable, writing to the `change` and `change_text` tables.
>
> Total time budget per pair, configurable, default 60 seconds. On timeout, return partial results with a clear note about what did not complete.

---

### Task 5.15 — Persistence and API

**Prompt:**

> Build the storage and API layer for changes.
>
> Extend the schema:
> - `change` — pair_id, bbox in page coordinates, stream(s) that found it, type, is_cosmetic, confidence, description, area_mm2, geometry_type
> - `change_text` — change_id, category, old_text, new_text, old_position, new_position, distance_moved_mm, numeric_delta, percent_delta, cross_check_flag
> - `change_hatch` — change_id, old_signature, new_signature, area_delta_m2
> - `comparison_run` — pair_id, config snapshot, tolerances used, mask set id, per-stream statistics, warnings, timings, engine version
> - `filtered_change` — everything a noise filter removed, with the reason. Never discard silently; the debug view needs this.
>
> `engine/api/routes_changes.py`:
> - `POST /api/compare/queue` — queue pairs for comparison
> - `GET /api/compare/status`
> - `GET /api/changes/{pair_id}` — with filters for stream, type, cosmetic, and category
> - `GET /api/changes/{pair_id}/filtered` — what was suppressed and why
> - `PATCH /api/changes/{change_id}` — set the user status: open, confirmed, dismissed
> - `GET /api/compare/{pair_id}/stats`
>
> Every comparison run stores a full configuration snapshot. If this output ever supports a variation claim, it must be reproducible.

---

### Task 5.16 — Validation and tests

**Prompt:**

> Complete the Phase 5 test suite.
>
> **The gate test.** `rev_letter_only` must produce **zero** changes. This is a hard requirement and its own test case. If it fails, nothing else in the phase matters.
>
> **Cosmetic-only suite.** `line_weight`, `colour_vs_mono`, `font_substituted`, `layer_toggled`, `watermarked` — each must report zero non-cosmetic changes.
>
> **Precision and recall** using the Task 5.0 harness, across the injection matrix. Targets: precision above 0.90, recall above 0.85, and **zero false positives on cosmetic-only pairs**.
>
> **Specific behaviour tests:**
> - `dimension_only` — reports a dimension text change and flags `dimension_text_only`
> - `hatch_change` — reports exactly ONE change for the hatch region, not thousands
> - `renumbered` — reports one renumbering record with the mapping, not dozens of tag changes
> - `real_small` — finds all 2–3 genuine changes with no extras
> - `real_heavy` — finds all genuine changes and the report stays readable
> - A deliberately mis-aligned pair triggers the residual detector and reports a warning, not changes
>
> **Unit tests** for each classifier rule, each noise filter in isolation, path normalisation invariants (a reversed path hashes identically, a split line hashes as one line), tolerance conversion and clamping.
>
> **Performance tests** against the budgets.
>
> **Regression golden files** for the precision and recall summary. Any change that lowers precision or raises the false positive count fails the suite.

---

## Part D — Definition of done

**The gate**
- [ ] **`rev_letter_only` reports zero changes**
- [ ] All cosmetic-only fixtures report zero non-cosmetic changes
- [ ] False positive count on cosmetic-only injections is zero

**Accuracy**
- [ ] Precision above 0.90, recall above 0.85 on the injection matrix
- [ ] `real_small` finds all genuine changes with no extras
- [ ] Text changes are found reliably, including dimension, tag, level and note categories

**Behaviour**
- [ ] A hatch region change produces one record, not thousands
- [ ] Tag renumbering produces one record with the mapping
- [ ] A toggled layer produces one record naming the layer
- [ ] Dimension-versus-geometry cross-checks work in both directions
- [ ] The alignment residual detector catches a deliberately mis-aligned pair and warns instead of reporting changes

**Masking**
- [ ] Sheet templates cluster correctly on a real project set
- [ ] Title block, revision table, watermarks and stamps are detected
- [ ] North arrow, scale bar, key plan and general notes are protected
- [ ] The mask editor applies to a whole template cluster in one action

**Reporting**
- [ ] Tolerances are shown in paper and site millimetres, never pixels
- [ ] Everything suppressed is retrievable in a debug view with its reason
- [ ] Every run stores a reproducible configuration snapshot

**Engineering**
- [ ] `pytest` passes, `ruff check .` is clean
- [ ] Performance budgets met
- [ ] Golden regression files committed
- [ ] The packaged `.exe` still works

Merge and tag `v0.5.0-compare`.

---

## Part E — Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| **Too many false positives** | High | The whole noise filter suite. The gate test is non-negotiable. |
| Hatch noise floods the report | High | Region-based hatch comparison, Task 5.10 |
| Small alignment residual creates fake changes | Medium | The residual detector, and warn rather than report |
| Text extraction misses rotated or stylised text | Medium | Fall back to the raster stream in those regions; report the gap honestly |
| Vector extraction unavailable on scanned sheets | Certain | Detect early, skip the stream, record why |
| Deduplication across streams merges genuinely separate changes | Medium | Use a tight overlap threshold; when in doubt, keep both |
| Comparison is too slow at batch scale | Medium | Downsampled candidate detection, then refine only the regions |

---

## Final recommendation

**Build Task 5.0 and the `rev_letter_only` fixture before writing any comparison code.** Two identical sheets differing only in the revision letter is the simplest possible test and the hardest one to pass. Every filter you write gets measured against it, and the day it returns zero is the day the phase is genuinely working.

**Build the text stream before the geometry streams.** It is the most valuable output, the most reliable, and the easiest to get right. Within a week you will have something worth showing a colleague: "the dimension on grid B/4 changed from 3000 to 3200, and the partition note changed from 1 hour to 2 hour fire rated." That is a report a QS will read. A pixel diff is not.

**Task 5.12 is the one that decides whether anyone uses this.** The comparison algorithms are the interesting engineering, but the noise filters are the product. Spend your care there.

**One honest expectation:** you will not eliminate every false positive, and you should not aim to. Aim instead for a report where the real changes are at the top, the cosmetic ones are collapsed and hidden by default, and the user can always ask "what did you hide, and why?" A tool that shows twelve changes and explains that it suppressed four hundred cosmetic differences is trustworthy. A tool that shows four hundred and twelve is not.
