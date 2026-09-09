# Phase 4 — Render & Align
### Detailed build plan for use with Claude Code

**Goal:** Turn two matched PDF sheets into two images that sit perfectly on top of each other, and refuse honestly when that is not possible.

**Why this phase decides the product:** every change you report in Phase 5 and 6 is measured against this alignment. If the transform is 5 pixels out, every line on the sheet looks moved. If the transform is wrong but confident, the report is full of invented changes and the user never trusts the tool again.

**Time:** 3–4 weeks part-time. Fifteen tasks across three workstreams.

---

## Part A — How to organise the work

If this were a twenty-person team, the work would split into three parallel workstreams with a clear contract between them. You are one person with Claude Code, but the same structure still helps: it tells you what can be built and tested independently, and what must wait.

```
WORKSTREAM A — Rendering            WORKSTREAM B — Alignment
  4.1 Render core                     4.4 Anchor extraction
  4.2 Tile pyramid + cache            4.5 Grid bubble detector
  4.3 Tile server + queue             4.6 Transform fitting
        │                             4.7 Image-based fallbacks
        │                             4.8 ECC refinement
        │                             4.9 Quality gate
        │                             4.10 Orchestrator
        └──────────┬──────────────────────────┘
                   ▼
        WORKSTREAM C — Viewer
          4.11 Lightbox canvas
          4.12 View modes
          4.13 Manual alignment
          4.14 Alignment review screen

        4.0 Ground-truth harness  ← BUILD THIS FIRST
        4.15 Test suite and benchmarks
```

**Build order for a solo developer:** 4.0 → 4.1 → 4.2 → 4.3 → 4.11 → 4.4 → 4.6 → 4.9 → 4.10 → 4.5 → 4.7 → 4.8 → 4.12 → 4.13 → 4.14 → 4.15

Getting the viewer working early (4.11) is deliberate. You cannot debug alignment without seeing it.

### A1. Branch and fixtures

```powershell
git checkout main
git pull
git checkout -b phase-4-render-align
```

Add these fixtures:

```
10_alignment\
├── clean_pair\         # same scale, same position, small real change
├── shifted\            # content plotted 40 mm to the right
├── rescaled\           # A1 1:100 reissued as A3 1:50
├── rotated\            # plan re-oriented 90°
├── page_rotated\       # PDF /Rotate flag differs
├── scanned\            # printed and rescanned, with skew
├── no_grid\            # a detail sheet with no grid bubbles
├── sparse_text\        # a sheet with almost no text
└── impossible\         # genuinely different drawings — MUST be refused
```

The `impossible` fixture is as important as the others. It proves the quality gate works.

### A2. Update `CLAUDE.md`

```markdown
## Current phase

Phase 4 — Render and align. Rasterising sheets into tiles, computing
the transform that maps the old sheet onto the new one, and the
lightbox viewer. No change detection yet — that is Phase 5.

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
```

---

## Part B — The technical design

Read this fully before Task 4.4. These are the decisions that a strong team would settle before writing code.

---

### B1. Why 200 DPI

Pick the resolution from the smallest thing that must be detected, not by habit.

The smallest meaningful mark on a construction drawing is text. Drawing standards put minimum lettering at about 2.5 mm cap height, occasionally 1.8 mm.

| DPI | 1 mm on paper | 2.5 mm text height | A1 sheet | A0 sheet |
|---|---|---|---|---|
| 150 | 5.9 px | 15 px | 17 MP | 35 MP |
| **200** | **7.87 px** | **20 px** | **31 MP** | **62 MP** |
| 300 | 11.8 px | 30 px | 70 MP | 139 MP |

200 DPI gives 20 pixels of text height, which is comfortably enough for shape comparison, at half the memory cost of 300 DPI. Make it configurable from 150 to 300, default 200.

**The accuracy target that follows from this.** At 200 DPI, one pixel is 0.127 mm on paper. At 1:100, that is 12.7 mm on site. A QS wants to ignore movements below about 25 mm on site, which is 2 pixels.

> **Therefore: alignment must achieve an RMS residual under 2 pixels.**
> That is the number to test against. Everything else is detail.

---

### B2. Rendering — the decisions that matter

**Lossless tiles, always.** PNG or lossless WebP. JPEG compression on a line drawing produces ringing around every line, and those artifacts appear as differences in Phase 5. This is not a small effect — it can double the false-positive count.

**Grayscale for comparison, colour for display.** Render once in colour for the viewer. Convert to grayscale for the alignment and diff pipeline. Do not throw away the colour render; users want to see the sheet as it was issued.

**Render annotations separately.** `pypdfium2` can render with or without annotations. Do both:
- Content render (`draw_annots=False`) → used for alignment and geometry comparison
- Annotation render → kept as a separate layer

This matters because a Bluebeam comment or a stamp added after issue is not a design change. It also means the revision clouds, which are often annotations, can be isolated for the Phase 6 cross-check.

**Apply page rotation first.** PDFs carry a `/Rotate` value of 0, 90, 180 or 270. Two revisions can disagree on it. Normalise before anything else, or your alignment will chase a 90° rotation it should never have seen.

**Tile pyramid, like a map server:**

```
Level 0   whole sheet in one 512 px tile     (thumbnail)
Level 1   2 × 2 tiles
Level 2   4 × 4 tiles
...
Level N   full resolution at the chosen DPI
```

512 × 512 tiles. For an A0 at 200 DPI that is about 250 tiles at the top level and roughly 330 across the whole pyramid. As grayscale PNG on mostly-white line art, expect 3–8 MB per sheet.

**Render lazily.** Do not pre-render 600 sheets. Generate level 0 thumbnails for everything on scan (fast), then build the full pyramid only for sheets the user opens or that enter the comparison queue.

**Cache key:** `file_hash + page_index + dpi + level + tile_x + tile_y`. Store in the output workspace so the cache travels with the comparison. Add a size budget with least-recently-used eviction, default 5 GB.

---

### B3. Choosing the transform model — a safety decision, not a technical one

| Model | Degrees of freedom | Handles | Use when |
|---|---|---|---|
| Translation | 2 | Shift | Never alone — too restrictive |
| **Similarity** | **4** | Shift, rotation, uniform scale | **Default. Correct for all CAD-exported PDFs.** |
| Affine | 6 | Adds shear, non-uniform scale | Scanned sheets only |
| Homography | 8 | Adds perspective | Photographed drawings only, explicit opt-in |

**Why restricting the degrees of freedom is a safety feature.** A homography fitted to noisy correspondences can produce a transform that "aligns" the sheets by warping the drawing into a trapezoid. The residual error looks small, the maths reports success, and every downstream result is nonsense. A similarity transform cannot do that. It can only fail visibly.

**Rule:** start with similarity. Escalate to affine only when the sheet is detected as scanned *and* similarity failed. Never allow homography by default.

**Reflection check.** A similarity fit should not produce a mirror image. Check the determinant of the transform matrix — negative means reflection. If it appears, reject the transform unless mirroring was explicitly enabled.

---

### ★ B4. Alignment strategy cascade

Six methods, tried in order. Each returns a transform plus a confidence. The orchestrator stops at the first result that passes the quality gate.

#### Method 1 — Unique text anchors ★ Best for CAD PDFs

You already extracted every text string with coordinates in Phase 2. Use it.

1. Take all text from **the model area only**, excluding the title block zone
2. Normalise each string
3. Keep only strings that appear **exactly once on each sheet** — these are unambiguous
4. Each such string gives a correspondence: its centre point on the old sheet maps to its centre on the new
5. With three or more correspondences, fit a similarity transform with RANSAC

**Why exclude the title block.** The project name, client name and address sit in the same place on every sheet of every revision. They will match perfectly. If you include them, you align the *sheet frame*, not the *drawing content*. When the content has moved and the frame has not, that is exactly the wrong answer — and it looks convincing.

This method is fast (no image processing at all), works across large scale changes, and is accurate to sub-pixel. It should succeed on the large majority of real CAD-exported drawings.

#### Method 2 — Grid bubbles ★ Construction-specific

When text extraction is thin or absent, fall back to the structure that every architectural and structural drawing has.

1. Hough circle detection, with the radius range derived from the expected bubble size — typically 8–12 mm on paper, so 63–95 px at 200 DPI
2. Filter candidates: grid bubbles cluster along sheet edges and line up in rows and columns. Use collinearity to reject false circles.
3. Read the label inside each bubble — from extracted text at that location, or by OCR on the crop if the sheet is scanned
4. Match bubbles by label between the two sheets
5. Fit the transform

Grid bubbles are ideal anchors: few in number, well separated across the sheet, and stable between revisions.

#### Method 3 — Phase correlation with log-polar ★ Best for scanned sheets

Underused, and much better on line drawings than feature matching.

1. Convert both images to grayscale, apply a Hanning window
2. FFT both, take magnitude spectra
3. Transform the magnitudes to log-polar coordinates — rotation becomes a shift along one axis, scale becomes a shift along the other
4. Phase-correlate the log-polar images to recover rotation and scale
5. Apply that, then phase-correlate the corrected images to recover translation

This is global. It does not depend on finding distinctive features, which is exactly why it beats ORB and SIFT on drawings full of repetitive hatch and grid patterns.

#### Method 4 — Feature matching (SIFT + RANSAC)

The classic approach, but be honest about it: **line drawings are a bad case for feature detectors.** Few distinctive corners, heavy repetition, large uniform white areas.

Use SIFT rather than ORB — its patent expired in 2020, it is free, and it handles scale change far better. Set a high contrast threshold to suppress noise matches. Use RANSAC with many iterations and a tight inlier threshold.

Treat this as a fallback, not a primary method.

#### Method 5 — Sheet border corners

Detect the outer drawing frame rectangle and align its corners.

**This aligns the sheet, not the content.** Use it as a coarse starting point for the other methods, or as an absolute last resort, and always mark it low confidence.

#### Method 6 — Metadata prior (a check, not a method)

From the title blocks you know both drawing scales (`1:100`, `1:50`), and from the PDF you know both page sizes. The expected content scale factor is:

```
expected_scale = (old_scale_denominator / new_scale_denominator) × (new_page_size / old_page_size)
```

A sheet at 1:100 on A1 reissued at 1:50 on A3 gives 2 × (1/√2) = **1.414**.

Do not align with this. Use it to **sanity-check** whatever the alignment produced. If the computed scale is 3.2 and the metadata expects 1.41, something is wrong — reject and try the next method.

---

### B5. Refinement — coarse to fine

Every coarse transform gets polished with **ECC** (Enhanced Correlation Coefficient, `cv2.findTransformECC`).

- Feed it the coarse transform as its starting point
- Restrict the motion model to `MOTION_EUCLIDEAN` or `MOTION_AFFINE` to match the chosen model
- Run it on an image pyramid, coarse level first, feeding each result into the next level
- Cap the iterations and always have a timeout — ECC can fail to converge and spin

This typically takes a 3-pixel residual down to under 1 pixel. It is the difference between "roughly aligned" and "usable for diffing".

---

### ★ B6. The quality gate — the most important code in Phase 4

Any alignment implementation can produce a transform. The thing that separates a professional tool from a demo is **knowing when the transform is wrong**.

Compute all seven of these:

| # | Metric | Threshold | What it catches |
|---|---|---|---|
| 1 | RMS residual of correspondences | < 2 px (0.25 mm on paper) | General inaccuracy |
| 2 | RANSAC inlier ratio | > 0.6 | Mostly-wrong correspondences |
| 3 | Anchor count | ≥ 3 minimum, ≥ 8 for "good" | Under-determined fits |
| 4 | **Anchor spread** — convex hull area of the anchors as a fraction of sheet area | > 0.25 | ★ Anchors clustered in one corner |
| 5 | Transform sanity — scale within 0.2×–5×, rotation within 2° of 0/90/180/270 for CAD sheets, shear near zero, determinant positive | Pass/fail | Wildly wrong fits |
| 6 | **Post-warp ink overlap** — fraction of dark pixels that coincide after warping | > 0.6 | ★ Fits that are numerically fine but visually wrong |
| 7 | **Hold-out cross-validation** — fit on 80% of anchors, measure error on the remaining 20% | < 3 px | ★ Overfitting to noise |

**Metric 4 deserves emphasis.** If every anchor sits in the bottom-right corner, the transform will be excellent there and badly wrong at the opposite corner. The residual will look perfect. Only the spread metric catches this.

**Metric 6 deserves emphasis.** It is the only check that asks the question a human would ask: *does it actually look aligned?* Warp the old image, overlay it, and count how many dark pixels land on dark pixels. Cheap to compute and it catches failures nothing else catches.

**Verdict:**

| Verdict | Condition | What happens |
|---|---|---|
| `excellent` | All metrics comfortably pass, ≥ 8 anchors, RMS < 1 px | Proceed automatically |
| `good` | All metrics pass | Proceed automatically |
| `poor` | Passes, but marginally on two or more metrics | Proceed only with user confirmation |
| `failed` | Any metric fails | Do not compare. Offer manual alignment. |

**Never silently downgrade a failure into a result.** A sheet reported as "could not align" costs the user 30 seconds of manual work. A sheet reported as aligned when it is not costs you the user.

---

### B7. Manual alignment — the essential escape hatch

Automatic alignment will fail on some sheets. That is acceptable. Having no way to recover is not.

The user clicks two to four matching points on the old sheet and the new sheet — a grid intersection, a corner, a column centre. Two points give a similarity transform; three or more allow a least-squares fit with a residual you can report back.

Bluebeam users already know this workflow, so it needs no explanation. It turns a dead end into a twenty-second task, and it is far less code than another automatic method.

---

### B8. Performance budgets

| Operation | Target | Notes |
|---|---|---|
| Render one A1 page at 200 DPI | < 1.5 s | Single core |
| Build full tile pyramid, A1 | < 3 s | |
| Tile served from cache | < 20 ms | |
| Method 1 alignment (text anchors) | < 300 ms | No image processing |
| Method 3 alignment (phase correlation) | < 2 s | FFT on downsampled images |
| Method 4 alignment (SIFT) | < 5 s | |
| ECC refinement | < 2 s | Pyramid, capped iterations |
| Full alignment of one pair, typical | < 4 s | |
| Batch of 100 pairs | < 6 min | 6 worker processes |
| Viewer pan and zoom | 60 fps | |
| Viewer tile load on zoom change | < 200 ms | |

Run alignment on downsampled images first — 25% scale is usually enough for the coarse stage — then refine at full resolution. This alone gives roughly a 10× speed-up.

---

## Part C — The fifteen tasks

---

### ★ Task 4.0 — The ground-truth harness (build this first)

This is what a strong team does before writing any alignment code, and it is the single highest-value task in Phase 4.

**The idea:** take one real drawing. Apply a *known* transform to it — shift 40 mm, rotate 1.5°, scale 1.414. Now you have a test pair where you know the exact correct answer. You can measure your alignment error precisely instead of squinting at overlays.

**Prompt:**

> Build `tests/harness/synthetic.py`, a ground-truth test pair generator.
>
> `generate_pair(source_pdf, page, transform_spec, degradation_spec) -> SyntheticPair`
>
> Transform spec: translation in mm, rotation in degrees, uniform scale, and optional page size change (A1 → A3).
>
> Degradation spec, to simulate real-world imperfection:
> - `line_weight_change` — dilate or erode strokes by n pixels
> - `noise` — Gaussian noise at a given sigma
> - `jpeg_artifacts` — round-trip through JPEG at a given quality
> - `skew` — small shear, to simulate a scan
> - `blur` — Gaussian blur radius
> - `content_change` — erase a rectangular region and optionally paste a shape, to simulate a real design change
>
> Return the two rendered images plus the exact ground-truth 3×3 transform matrix.
>
> Also build `tests/harness/benchmark.py`:
> - Generates a matrix of test cases across all transform and degradation combinations
> - Runs the alignment engine on each
> - Reports per case: computed transform, ground-truth transform, RMS error in px and in mm, method used, verdict, and time taken
> - Outputs a CSV and a summary table
> - Flags any case where the verdict was `good` or better but the actual error exceeded 2 px — **these are the dangerous failures and the count must be zero**
>
> Include a `pytest` marker so the full benchmark runs only on demand, not on every test run.

**Why this matters more than it looks:** without ground truth you are tuning thresholds by eye. With it, you can change a parameter and immediately see whether error went up or down across 200 cases. This turns Phase 4 from guesswork into engineering.

---

### Task 4.1 — Render core

**Prompt:**

> Build `engine/extract/raster_renderer.py`.
>
> `render_page(pdf_path, page_index, dpi, options) -> RenderResult`
>
> Using `pypdfium2`:
> - Scale factor is `dpi / 72`
> - Apply the PDF `/Rotate` flag so output is always upright, and record what rotation was applied
> - Render twice when `separate_annotations=True`: once with `draw_annots=False` (content) and once with annotations, then derive the annotation layer by difference
> - Return colour and grayscale variants
> - Record the exact pixels-per-mm so every later measurement can convert to millimetres
>
> Also:
> - `detect_scanned(page)` — a single large image object covering most of the page and little or no text
> - `estimate_ink_coverage(image)` — fraction of non-white pixels, used later by the quality gate
> - `binarize(image, method)` — Otsu and adaptive Gaussian options, for the alignment pipeline
>
> Memory safety: an A0 at 300 DPI is 139 megapixels. Render in horizontal bands when the estimated size exceeds a configurable budget (default 200 MB), and never hold more than two full pages in memory at once.
>
> Every returned object carries `px_per_mm` and `dpi`. No code downstream may assume a DPI.

---

### Task 4.2 — Tile pyramid and cache

**Prompt:**

> Build `engine/extract/tiler.py` and extend `engine/storage/cache_store.py`.
>
> - `build_pyramid(image, tile_size=512) -> Pyramid` producing levels from full resolution down to a single tile, each level half the previous
> - Tiles saved as **lossless** PNG, grayscale where the source is monochrome. Never JPEG.
> - Cache key: `file_hash + page_index + dpi + level + x + y`
> - Store under `_audit/tiles/` in the output workspace, in a sharded directory structure so no folder exceeds about 1,000 files
> - `get_tile(...)` returns from cache or generates on demand
> - LRU eviction against a configurable size budget, default 5 GB, with a `cache_stats()` function reporting size, tile count and hit rate
> - `build_thumbnail(...)` producing just level 0, fast, for the register list
>
> Add a `PyramidManifest` per sheet recording levels, tile counts, dimensions and DPI, so the viewer knows what exists without probing.

---

### Task 4.3 — Tile server and render queue

**Prompt:**

> Build `engine/api/routes_tiles.py` and the render queue.
>
> Endpoints:
> - `GET /api/tiles/{sheet_id}/{level}/{x}/{y}.png` — returns a tile, generating on demand if absent. Set long-lived cache headers, since tiles are immutable for a given file hash.
> - `GET /api/tiles/{sheet_id}/manifest` — the pyramid manifest
> - `POST /api/render/queue` — queue sheets for pyramid generation
> - `GET /api/render/status` — queue state
>
> Render queue behaviour:
> - Priority levels: `immediate` for the sheet the user is viewing, `high` for its neighbours in the list, `normal` for the batch comparison queue, `low` for background pre-rendering
> - Runs in the Phase 2 process pool
> - Deduplicates: never render the same sheet twice concurrently
> - Progress over the existing WebSocket
>
> Tiles must stream, not buffer. Use `StreamingResponse`.

---

### Task 4.4 — Anchor extraction ★ Method 1

**Prompt:**

> Build `engine/align/anchors.py`.
>
> `extract_text_anchors(sheet, exclude_zones) -> list[Anchor]`
>
> - Uses the text already extracted in Phase 2 — do not re-read the PDF
> - **Excludes the title block zones**, which is essential: title block text is identical between revisions and in the same position, so including it aligns the sheet frame rather than the drawing content
> - Also excludes text shorter than 2 characters, and pure punctuation
> - Normalises each string: NFKC, uppercase, collapse whitespace
> - Each anchor has: normalised text, centre point in page coordinates, bounding box, font size
>
> `find_correspondences(old_anchors, new_anchors) -> list[Correspondence]`
> - Keep only strings that appear **exactly once in each set**. Discard any string that repeats — ambiguity is worse than fewer anchors.
> - Each correspondence carries both points and a weight (longer, larger text is more reliable)
> - Return diagnostics: total anchors on each side, unique matches found, and how many were discarded as ambiguous
>
> `assess_anchor_quality(correspondences, page_size)` returning count, convex hull area as a fraction of page area, and a min/max spacing summary.

---

### Task 4.5 — Grid bubble detector ★ Method 2

**Prompt:**

> Build `engine/align/grid_bubbles.py`.
>
> `detect_bubbles(image, px_per_mm) -> list[GridBubble]`
>
> 1. `cv2.HoughCircles` with the radius range derived from the expected bubble size — configurable, default 8–12 mm on paper, converted to pixels via `px_per_mm`
> 2. Filter candidates:
>    - Reject circles whose interior is mostly ink (filled shapes are not bubbles)
>    - Reject circles far from the sheet edges, configurable, default keep those within 15% of an edge — but allow interior bubbles as a lower-confidence group
>    - **Collinearity filter:** real grid bubbles line up in rows and columns. Cluster centres by x and by y, and keep circles belonging to a line of three or more.
> 3. Read the label: first look for extracted text whose centre falls inside the circle. Only if no text exists (a scanned sheet) fall back to OCR on the crop, restricted to an alphanumeric character set.
> 4. Return centre, radius, label, and confidence for each
>
> `match_bubbles(old, new) -> list[Correspondence]` — match by label, discarding any label appearing more than once per sheet.
>
> Grid bubbles are ideal anchors because they are few, well separated across the sheet, and stable between revisions. When six or more match, prefer this method over generic text anchors.

---

### Task 4.6 — Transform fitting ★ Core

**Prompt:**

> Build `engine/align/transform.py`.
>
> Transform model classes with a common interface: `Similarity` (4 DOF), `Affine` (6 DOF), `Homography` (8 DOF, opt-in only). Each supports `apply(points)`, `inverse()`, `to_matrix()`, `from_matrix()`, and `decompose()` returning translation, rotation, scale and shear.
>
> `fit_transform(correspondences, model, method) -> FitResult`
> - RANSAC by default: configurable iterations (default 2000), inlier threshold in pixels (default 3.0), and minimum inlier ratio
> - Least-squares refit using only the inliers once RANSAC settles
> - Weighted fitting, using the correspondence weights
> - Returns the transform, the inlier mask, the inlier ratio, RMS residual, and the per-correspondence residuals
>
> `validate_transform(transform, expected_scale, is_scanned) -> ValidationResult`
> - Scale must fall within 0.2× to 5×
> - **Determinant must be positive** — a negative determinant means a reflection, which should be rejected unless mirroring is explicitly enabled
> - For non-scanned sheets, rotation must be within 2° of 0, 90, 180 or 270
> - Shear must be near zero for non-scanned sheets
> - When an expected scale is supplied from metadata, the computed scale must be within 15% of it
>
> `cross_validate(correspondences, model, holdout=0.2)` — fit on 80%, measure RMS on the held-out 20%, repeat five times, return mean and standard deviation. This catches overfitting that the plain residual hides.
>
> Convert every reported error into millimetres on paper and, when the drawing scale is known, into real-world millimetres.

---

### Task 4.7 — Image-based fallbacks ★ Methods 3 and 4

**Prompt:**

> Build `engine/align/image_align.py`.
>
> **`align_phase_correlation(old_img, new_img) -> AlignResult`** — the log-polar method:
> 1. Convert to grayscale, apply a Hanning window to reduce edge effects
> 2. FFT both, take the magnitude spectra
> 3. Convert both magnitude spectra to log-polar coordinates
> 4. Phase-correlate the log-polar images: the x shift gives rotation, the y shift gives log scale
> 5. Apply the recovered rotation and scale to the old image
> 6. Phase-correlate the corrected images to recover translation
> 7. Return the transform and the peak correlation response as confidence
>
> This is global and needs no feature detection, which makes it the strongest method for scanned line drawings, where repetitive hatch and grid patterns defeat feature detectors.
>
> **`align_features(old_img, new_img, detector) -> AlignResult`**:
> - SIFT by default (patent expired 2020, free, better under scale change than ORB); ORB available as a faster option
> - High contrast threshold to suppress noise keypoints
> - FLANN matching with Lowe's ratio test at 0.75
> - RANSAC to fit the model
> - Return the transform, inlier count and inlier ratio
>
> Be honest in the docstring: line drawings are a difficult case for feature detectors. Expect this method to underperform phase correlation on drawings, and treat it as a fallback.
>
> **`align_sheet_border(old_img, new_img)`** — detect the outer frame rectangle by finding the largest quadrilateral contour, align its corners. Always return low confidence with a note that this aligns the sheet, not the content.
>
> All three run on downsampled images (default 25%) for speed, then scale the transform back up.

---

### Task 4.8 — ECC refinement

**Prompt:**

> Build `engine/align/refine.py`.
>
> `refine_ecc(old_img, new_img, initial_transform, model) -> RefineResult`
>
> - `cv2.findTransformECC` with `MOTION_EUCLIDEAN` for similarity and `MOTION_AFFINE` for affine
> - Multi-resolution: build a 3-level pyramid, run ECC at the coarsest level first, scale the result and feed it into the next level
> - Seed with the coarse transform from the previous stage
> - Termination: 200 iterations or an epsilon of 1e-6, whichever comes first
> - **Hard timeout, default 5 seconds** — ECC can fail to converge and spin indefinitely
> - Apply a Gaussian blur (sigma around 1.5) before ECC. Counter-intuitive, but it smooths the correlation surface and greatly improves convergence on line art.
> - If ECC fails or diverges, return the input transform unchanged and record that refinement did not apply — never return a worse transform than you were given
> - Return the refined transform, the final correlation coefficient, iterations used, and the improvement in RMS
>
> Log before-and-after RMS for every refinement so the benchmark can measure whether this stage is earning its cost.

---

### Task 4.9 — The quality gate ★ Most important code in Phase 4

**Prompt:**

> Build `engine/align/quality.py`.
>
> `assess(transform, correspondences, old_img, new_img, context) -> QualityAssessment`
>
> Compute all seven metrics:
> 1. `rms_residual_px` and its conversions to mm on paper and mm on site
> 2. `inlier_ratio`
> 3. `anchor_count`
> 4. `anchor_spread` — convex hull area of the anchor points divided by the page area
> 5. `transform_sanity` — scale range, rotation near an axis, near-zero shear, positive determinant
> 6. `ink_overlap` — warp the old binarised image by the transform, then compute the fraction of dark pixels that coincide with dark pixels in the new image. Use the Jaccard index over dark pixels. This is the only metric that asks the question a human would ask: does it look aligned?
> 7. `holdout_rms` — from the cross-validation in Task 4.6
>
> Default thresholds, all configurable:
> - RMS under 2.0 px, inlier ratio over 0.6, at least 3 anchors, spread over 0.25, ink overlap over 0.6, hold-out RMS under 3.0 px
>
> Verdict: `excellent`, `good`, `poor`, or `failed`, per the rules in the design section.
>
> Return a **plain-English explanation** for every verdict, suitable for showing to a user: "Aligned using 14 grid bubbles. Average error 0.6 mm on paper, which is 60 mm on site at 1:100." Or: "Could not align. Only 2 matching reference points were found, and they are both in the bottom-right corner of the sheet."
>
> Include `explain_failure()` producing a specific suggestion: try manual alignment, check the sheets are the same drawing, or the sheet may be scanned.
>
> Write tests using the `impossible` fixture: two genuinely different drawings **must** return `failed`. This is a hard requirement.

---

### Task 4.10 — The alignment orchestrator

**Prompt:**

> Build `engine/align/orchestrator.py`.
>
> `align_pair(old_sheet, new_sheet, config) -> AlignmentResult`
>
> The strategy cascade:
> 1. Compute the expected scale from metadata — drawing scales and page sizes. Use it as a validation prior throughout.
> 2. Detect whether either sheet is scanned. If so, allow affine and lower the rotation-sanity constraint.
> 3. Try grid bubbles. If six or more match, fit, refine, assess. Return on `good` or better.
> 4. Try text anchors. If three or more correspond, fit, refine, assess. Return on `good` or better.
> 5. Try phase correlation. Fit, refine, assess. Return on `good` or better.
> 6. Try SIFT features. Fit, refine, assess. Return on `good` or better.
> 7. Try sheet border corners as a coarse transform, then ECC refinement, then assess. Return only on `good`.
> 8. If everything fails, return `failed` with the best attempt attached, all diagnostics, and a suggestion to align manually.
>
> Every attempt is recorded, even the failures, so the review screen can explain what was tried.
>
> `align_batch(pairs, config, progress_callback)` — runs across the process pool, resumable, cancellable, writing results to the `alignment` table.
>
> `apply_manual(old_points, new_points, model)` — fits from user-clicked correspondences and runs the same quality assessment, so a manual alignment is measured on the same scale as an automatic one.
>
> Total time budget for one pair, configurable, default 30 seconds. On timeout, return the best attempt so far with a `poor` verdict rather than nothing.

---

### Task 4.11 — Lightbox canvas core

**Prompt:**

> Build `ui/src/components/Lightbox/`.
>
> A PixiJS WebGL canvas rendering tiled sheet images.
>
> - `TileLayer` — loads tiles for the current viewport and zoom level from the tile endpoint. Chooses the level whose resolution is closest to the current screen scale. Prefetches one ring of adjacent tiles. Evicts tiles outside a memory budget, default 300 tiles.
> - Two layers: old and new. The old layer has the alignment transform applied as a PixiJS matrix, so no image resampling happens in Python.
> - Pan by drag, zoom by wheel with the pointer as the anchor, pinch on touch devices
> - Keyboard: arrows to pan, `+`/`-` to zoom, `0` to fit, `1` for 100%
> - A minimap in the corner showing the viewport rectangle
> - A scale indicator showing the current zoom and the equivalent real-world size
> - Loading state per tile — a subtle placeholder, never a full-screen spinner
> - Graceful degradation: if WebGL is unavailable, fall back to Canvas 2D with a warning
>
> Background is `--room-900`. The sheet sits on `--sheet`. The drawing must be the brightest thing on screen.
>
> Target 60 fps while panning at any zoom level. Test with an A0 sheet at 200 DPI.

---

### Task 4.12 — View modes and controls

**Prompt:**

> Extend the Lightbox with four viewing modes and their controls.
>
> - **Overlay** — both layers drawn together. Old in `#0E63C4`, new in `#E8442A`, coinciding ink in 55% grey. Opacity slider for the old layer. Implement as a PixiJS shader or a blend mode, not as CSS.
> - **Swipe** — a draggable vertical divider. Old on the left, new on the right. **The divider is the seam**, in brand red, carrying the visual idea from the Setup screen through to here.
> - **Blink** — alternates between old and new at an adjustable interval, default 600 ms. Space bar toggles playback. This is the mode experienced reviewers trust most, because the eye detects movement far better than it detects colour difference.
> - **Single** — one sheet at a time, `O` and `N` to switch.
>
> Control bar: mode buttons, opacity slider, blink speed, a colour-blind-safe palette toggle (blue and orange), a "show annotations layer" toggle, and a reset view button.
>
> Keyboard throughout: `1`–`4` for modes, `Space` for blink, `[` and `]` for opacity, `O`/`N` for single-sheet switching.
>
> Remember the user's last mode and restore it. Reviewers develop a preference and switching back every time is irritating.
>
> Brand red appears only on the seam divider and in the chrome. It must never appear as drawing content.

---

### Task 4.13 — Manual alignment UI

**Prompt:**

> Build `ui/src/screens/Compare/ManualAlign.tsx`.
>
> Side-by-side view, old sheet left, new sheet right, each independently pannable and zoomable.
>
> Workflow:
> 1. The user clicks a point on the old sheet. A numbered marker appears.
> 2. They click the matching point on the new sheet. The pair links, shown in a matching colour.
> 3. Repeat. Two pairs are the minimum; three or more give a residual estimate.
> 4. After the second pair, show a live preview of the resulting alignment and the estimated error.
> 5. "Apply alignment" runs the same quality assessment as the automatic path.
>
> Details that make it usable:
> - Magnifier loupe near the cursor, so the user can place points precisely without zooming in and losing context
> - Snap assistance: after a click, search a small radius for a line intersection or a corner and offer to snap to it
> - Per-point residual shown once three or more pairs exist, so a badly placed point can be identified and removed
> - Undo the last point, clear all
> - A hint line suggesting good anchor choices: grid intersections, column centres, building corners
>
> Show the resulting error in millimetres on paper and on site, never in pixels.
>
> This screen turns a failed alignment into a twenty-second task. Make it fast and forgiving.

---

### Task 4.14 — Alignment review screen

**Prompt:**

> Build `ui/src/screens/Alignment/`.
>
> A batch view of alignment results across the whole comparison set.
>
> **Summary bar:** `183 aligned · 8 need review · 3 could not align`, each clickable as a filter.
>
> **The list**, one row per pair: drawing number, both revisions, method used, quality verdict pill, RMS error in millimetres, and a thumbnail overlay preview.
>
> **Sorting:** failures first, then `poor`, then by descending error. The user should never have to hunt for problems.
>
> **Detail panel** on row click:
> - The alignment overlay at a readable size
> - Every quality metric with its threshold, shown as a small bar so it is obvious which one failed
> - The plain-English explanation
> - Which methods were tried and why each was rejected
> - Buttons: accept, re-run with different settings, align manually, exclude this pair from comparison
>
> **Batch actions:** accept all `good` and above, re-run all failures with a chosen method, export the alignment report.
>
> **The queue view** while a batch runs: progress rail, current drawing name, throughput in sheets per minute, estimated time remaining, and a working cancel that leaves completed results intact.
>
> Sentence case, no ALL-CAPS. Errors in millimetres, never pixels.

---

### Task 4.15 — Test suite and benchmark

**Prompt:**

> Complete the Phase 4 test suite.
>
> **Unit tests:** renderer DPI accuracy and rotation handling, tiler pyramid correctness and cache hits, anchor extraction with title block exclusion, bubble detection on the `no_grid` fixture returning empty rather than false positives, transform fitting and decomposition, reflection rejection, quality metrics each in isolation.
>
> **Ground-truth benchmark** using the Task 4.0 harness. Run the full matrix: translation from 0 to 200 mm, rotation from 0 to 180°, scale from 0.5× to 2.0×, crossed with each degradation type at three severities. Report:
> - Success rate per method
> - Mean and 95th percentile error per method
> - Time per method
> - **Dangerous failure count — cases where the verdict was `good` or better but the true error exceeded 2 px. This number must be zero.**
>
> **Integration tests on real fixtures:** each folder in `10_alignment` with an expected verdict. `clean_pair`, `shifted`, `rescaled` and `rotated` must reach `good` or better. `impossible` **must** return `failed`.
>
> **Performance tests** against the budgets in the design document, marked as slow tests.
>
> **Regression guard:** store the benchmark summary as a golden file. Any future change that raises the mean error or produces a dangerous failure fails the test suite.

---

## Part D — Definition of done

**Correctness**
- [ ] `clean_pair`, `shifted`, `rescaled`, `rotated` all align to `good` or better
- [ ] A1 at 1:100 reissued as A3 at 1:50 aligns within 2 px RMS
- [ ] `impossible` returns `failed` — never a confident wrong answer
- [ ] `no_grid` and `sparse_text` still align through the fallback methods
- [ ] Scanned sheets align through phase correlation
- [ ] Reflections are rejected unless explicitly enabled
- [ ] **Dangerous failure count in the benchmark is zero**

**Rendering**
- [ ] Tiles are lossless — no JPEG anywhere in the pipeline
- [ ] Page rotation flags are normalised
- [ ] Annotations are separable from content
- [ ] A0 at 300 DPI renders without exhausting memory
- [ ] Cache eviction keeps within the configured budget

**Viewer**
- [ ] 60 fps panning on an A0 at 200 DPI
- [ ] All four modes work, and the mode preference persists
- [ ] The colour-blind palette is available
- [ ] Manual alignment produces a usable transform in under 30 seconds of user time

**Reporting**
- [ ] Every alignment records method, metrics, and verdict
- [ ] Errors are reported in millimetres on paper and on site, never pixels
- [ ] Failures come with a specific, actionable explanation

**Engineering**
- [ ] `pytest` passes, `ruff check .` is clean
- [ ] Benchmark golden file is committed
- [ ] Performance budgets met
- [ ] The packaged `.exe` still works

Merge and tag `v0.4.0-alignment`.

---

## Part E — Risks specific to this phase

| Risk | Likelihood | What to do |
|---|---|---|
| **Confident wrong alignment** | Medium | The quality gate, and specifically the ink-overlap and anchor-spread metrics. The benchmark's dangerous-failure count is the measure. |
| Feature matching underperforms on line art | High | Expected. This is why phase correlation ranks above SIFT in the cascade. |
| ECC fails to converge | Medium | Hard timeout, pre-blur, and never return a worse transform than the input |
| Tile cache fills the disk | Medium | LRU eviction with a budget, and visible cache statistics |
| Viewer stutters on A0 | Medium | Correct level-of-detail selection and a tile memory budget. Test on the largest sheet early, not last. |
| Scale metadata is wrong or absent | High | Use it only as a validation prior, never as the alignment itself |
| Alignment is too slow at batch scale | Medium | Coarse stage on 25% downsampled images, then refine |

---

## Final recommendation

**Build Task 4.0 first, before any alignment code.** Without ground truth you will tune thresholds by looking at overlays and guessing. With it, you change one parameter and immediately see the effect across 200 cases. This is the difference between engineering and hoping, and it is what separates a tool that works on your test drawings from one that works on everyone's.

**Task 4.9, the quality gate, is where to spend your care.** Every other part of Phase 4 can be improved later. A confident wrong alignment cannot be fixed later, because by the time the user notices, they have already stopped trusting the reports.

**Get the viewer working early, at Task 4.11.** Alignment is nearly impossible to debug without seeing it. Building the canvas before the fallback methods will save you days.

**One honest expectation to set:** you will not reach 100% automatic alignment, and you should not try. A realistic target is 90–95% automatic on CAD-exported PDFs and 60–75% on scanned sheets, with the rest going to manual alignment. That is a good result. Chasing the last five percent with more automatic methods will cost more time than the manual alignment screen costs your users, and it introduces exactly the confident-wrong-answer risk you are trying to avoid.
