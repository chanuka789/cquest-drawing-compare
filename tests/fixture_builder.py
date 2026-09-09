"""Build synthetic drawing PDFs for the test suite.

Real client drawings cannot live in the repository, so the suite generates its
own. The sheets here deliberately reproduce the awkward parts of the real
fixture set:

* a media box that is **not** at the origin, like the Lami Architects set
  whose box runs (-1192, -842) to (1192, 842)
* title block values that sit *below* their label (`Drawing No.`) as well as
  *to the right* of it (`Scale`)
* multi-page files, encrypted files, corrupt files and image-only sheets

Anything that needs to be true of a real drawing should be reproduced here
first, so the behaviour is pinned by a test rather than by one lucky file.
"""

from __future__ import annotations

import io
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import pikepdf

# A1 landscape in points, the size the real fixtures use.
A1_WIDTH = 2384.0
A1_HEIGHT = 1684.0
A3_WIDTH = 1191.0
A3_HEIGHT = 842.0


@dataclass(slots=True)
class SheetSpec:
    """One drawing sheet: what its title block says."""

    drawing_no: str = "A-101"
    title: str = "GROUND FLOOR PLAN"
    revision: str = "C"
    scale: str = "1 : 100"
    project_no: str = "LM2426"
    #: Extra text placed in the drawing area, away from the title block.
    body: list[str] = field(default_factory=lambda: ["GRID A", "GRID B", "3000", "3200"])
    #: Omit the title block entirely, to exercise the unidentified path.
    include_title_block: bool = True
    width: float = A1_WIDTH
    height: float = A1_HEIGHT


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _title_block_items(spec: SheetSpec) -> list[tuple[float, float, float, str]]:
    """Title block text as (x, y, font size, text), in page-local coordinates.

    Laid out like the real fixture: bottom-right corner, labels above their
    values, except `Scale` whose value sits to the right of the label.
    """
    right = spec.width - 60
    items: list[tuple[float, float, float, str]] = [
        # Company block, top right
        (right - 250, spec.height - 60, 11, "LAMI ARCHITECTS"),
        (right - 250, spec.height - 160, 8, "L E A D  A R C H I T E C T"),
        # Project and title, above the number block
        (right - 250, 300, 8, "P R O J E C T"),
        (right - 250, 280, 11, "Al Basateen Farm - Villa"),
        (right - 250, 200, 8, "Drawing Title"),
        (right - 250, 175, 12, spec.title),
        # Scale: value to the RIGHT of the label
        (right - 250, 120, 8, "Scale"),
        (right - 180, 120, 9, spec.scale),
        # Number block: values BELOW their labels
        (right - 250, 90, 8, "Project No."),
        (right - 250, 72, 10, spec.project_no),
        (right - 110, 90, 8, "Drawing No."),
        (right - 110, 72, 12, spec.drawing_no),
        (right - 20, 90, 8, "Rev."),
        (right - 20, 72, 12, spec.revision),
    ]
    return items


def _content_stream(spec: SheetSpec, x0: float, y0: float) -> bytes:
    parts: list[str] = []

    items: list[tuple[float, float, float, str]] = []
    if spec.include_title_block:
        items.extend(_title_block_items(spec))

    # Body text, spread across the left two thirds of the sheet.
    for index, text in enumerate(spec.body):
        items.append((spec.width * 0.15, spec.height * 0.7 - index * 40, 14, text))

    for local_x, local_y, size, text in items:
        parts.append(
            f"BT /F1 {size} Tf {x0 + local_x:.2f} {y0 + local_y:.2f} Td ({_escape(text)}) Tj ET"
        )

    # A border, so the sheet is not a blank page.
    parts.append(
        f"0.5 w {x0 + 20:.2f} {y0 + 20:.2f} {spec.width - 40:.2f} {spec.height - 40:.2f} re S"
    )
    return "\n".join(parts).encode("latin-1")


def build_pdf(
    path: str | Path,
    sheets: list[SheetSpec] | None = None,
    *,
    offset_origin: bool = True,
    password: str | None = None,
    producer: str = "C-Quest test fixture",
) -> Path:
    """Write a PDF containing one page per sheet spec.

    `offset_origin` centres the media box on (0, 0), reproducing the real
    fixture. Leave it on unless a test specifically needs a page at origin.
    """
    sheets = sheets or [SheetSpec()]
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    pdf = pikepdf.Pdf.new()
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font,
            Subtype=pikepdf.Name.Type1,
            BaseFont=pikepdf.Name.Helvetica,
            Encoding=pikepdf.Name.WinAnsiEncoding,
        )
    )

    for spec in sheets:
        x0 = -spec.width / 2 if offset_origin else 0.0
        y0 = -spec.height / 2 if offset_origin else 0.0
        stream = pdf.make_stream(_content_stream(spec, x0, y0))
        page = pikepdf.Dictionary(
            Type=pikepdf.Name.Page,
            MediaBox=[x0, y0, x0 + spec.width, y0 + spec.height],
            Resources=pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font)),
            Contents=stream,
        )
        pdf.pages.append(pikepdf.Page(pdf.make_indirect(page)))

    with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
        meta["pdf:Producer"] = producer

    if password is None:
        pdf.save(target)
    else:
        pdf.save(target, encryption=pikepdf.Encryption(owner=password, user=password, R=6))
    pdf.close()
    return target


def build_scanned_pdf(path: str | Path, pages: int = 1) -> Path:
    """A page holding one large image and no text, like a photocopied sheet."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    pdf = pikepdf.Pdf.new()
    width, height = A3_WIDTH, A3_HEIGHT
    pixels, rows = 200, 140
    raw = bytes([220]) * (pixels * rows)  # flat grey, compresses to nothing

    for _ in range(pages):
        image = pdf.make_stream(zlib.compress(raw))
        image.Type = pikepdf.Name.XObject
        image.Subtype = pikepdf.Name.Image
        image.Width = pixels
        image.Height = rows
        image.ColorSpace = pikepdf.Name.DeviceGray
        image.BitsPerComponent = 8
        image.Filter = pikepdf.Name.FlateDecode

        content = pdf.make_stream(
            f"q {width:.2f} 0 0 {height:.2f} 0 0 cm /Im0 Do Q".encode("latin-1")
        )
        page = pikepdf.Dictionary(
            Type=pikepdf.Name.Page,
            MediaBox=[0, 0, width, height],
            Resources=pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=image)),
            Contents=content,
        )
        pdf.pages.append(pikepdf.Page(pdf.make_indirect(page)))

    pdf.save(target)
    pdf.close()
    return target


def build_corrupt_pdf(path: str | Path) -> Path:
    """A file that starts like a PDF and then stops. Must land in quarantine."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    buffer = io.BytesIO()
    build_pdf_to_buffer(buffer)
    data = buffer.getvalue()
    target.write_bytes(data[: len(data) // 3])  # truncated: no xref, no trailer
    return target


def build_pdf_to_buffer(buffer: io.BytesIO, sheets: list[SheetSpec] | None = None) -> None:
    """Same as :func:`build_pdf` but into memory, used by the corrupt builder."""
    sheets = sheets or [SheetSpec()]
    pdf = pikepdf.Pdf.new()
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font,
            Subtype=pikepdf.Name.Type1,
            BaseFont=pikepdf.Name.Helvetica,
            Encoding=pikepdf.Name.WinAnsiEncoding,
        )
    )
    for spec in sheets:
        stream = pdf.make_stream(_content_stream(spec, 0.0, 0.0))
        page = pikepdf.Dictionary(
            Type=pikepdf.Name.Page,
            MediaBox=[0, 0, spec.width, spec.height],
            Resources=pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font)),
            Contents=stream,
        )
        pdf.pages.append(pikepdf.Page(pdf.make_indirect(page)))
    pdf.save(buffer)
    pdf.close()


# ── Whole fixture sets ─────────────────────────────────────────────────

#: A small architectural set, the shape of `01_normal`.
NORMAL_SET: list[tuple[str, str, str]] = [
    ("A-001", "SITE PLAN", "C"),
    ("A-101", "GROUND FLOOR PLAN", "C"),
    ("A-102", "FIRST FLOOR PLAN", "C"),
    ("A-103", "ROOF PLAN", "C"),
    ("A-104", "GROUND FLOOR REFLECTED CEILING PLAN", "C"),
    ("A-105", "FIRST FLOOR REFLECTED CEILING PLAN", "C"),
    ("A-201", "NORTH AND SOUTH ELEVATIONS", "C"),
    ("A-202", "EAST AND WEST ELEVATIONS", "C"),
    ("A-301", "SECTION A-A", "C"),
    ("A-302", "SECTION B-B", "C"),
]


def build_normal_pair(root: str | Path) -> tuple[Path, Path]:
    """Two full issues: three drawings revised, one new, everything else the same.

    Returns the (old, new) folders.
    """
    root = Path(root)
    old_dir = root / "old"
    new_dir = root / "new"

    for number, title, revision in NORMAL_SET:
        build_pdf(
            old_dir / f"{number}-{title.title().replace(' ', '-')}-Rev{revision}.pdf",
            [SheetSpec(drawing_no=number, title=title, revision=revision)],
        )

    for number, title, revision in NORMAL_SET:
        # A-101, A-102 and A-201 move to revision D; the rest stay at C.
        new_revision = "D" if number in {"A-101", "A-102", "A-201"} else revision
        body = ["GRID A", "GRID B", "3200", "3400"] if new_revision == "D" else None
        spec = SheetSpec(drawing_no=number, title=title, revision=new_revision)
        if body:
            spec.body = body
        build_pdf(
            new_dir / f"{number}-{title.title().replace(' ', '-')}-Rev{new_revision}.pdf",
            [spec],
        )

    build_pdf(
        new_dir / "A-303-Section-C-C-RevA.pdf",
        [SheetSpec(drawing_no="A-303", title="SECTION C-C", revision="A")],
    )

    return old_dir, new_dir


def build_partial_issue_pair(root: str | Path, old_count: int = 40) -> tuple[Path, Path]:
    """A partial issue: a large previous set, and only three sheets reissued.

    This is the case that decides whether people trust the tool. The new set
    must never be reported as hundreds of removals.
    """
    root = Path(root)
    old_dir = root / "old"
    new_dir = root / "new"

    for index in range(old_count):
        number = f"A-{100 + index}"
        build_pdf(
            old_dir / f"{number}-Rev-C.pdf",
            [SheetSpec(drawing_no=number, title=f"PLAN {index}", revision="C")],
        )

    for index in range(3):
        number = f"A-{100 + index}"
        build_pdf(
            new_dir / f"{number}-Rev-D.pdf",
            [SheetSpec(drawing_no=number, title=f"PLAN {index}", revision="D")],
        )

    return old_dir, new_dir


def build_messy_folder(root: str | Path) -> Path:
    """Junk files, an archive subfolder, a duplicate, and a nested set."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)

    build_pdf(root / "A-101-Rev-C.pdf", [SheetSpec(drawing_no="A-101", revision="C")])
    build_pdf(
        root / "Architectural" / "A-102-Rev-C.pdf",
        [SheetSpec(drawing_no="A-102", title="FIRST FLOOR PLAN", revision="C")],
    )
    build_pdf(
        root / "Architectural" / "Levels" / "A-103-Rev-C.pdf",
        [SheetSpec(drawing_no="A-103", title="ROOF PLAN", revision="C")],
    )

    # A true duplicate: identical bytes in two places.
    duplicate_source = root / "A-101-Rev-C.pdf"
    (root / "Copy of A-101-Rev-C.pdf").write_bytes(duplicate_source.read_bytes())

    # Superseded material that must be skipped whole.
    build_pdf(
        root / "superseded" / "A-101-Rev-B.pdf", [SheetSpec(drawing_no="A-101", revision="B")]
    )
    build_pdf(root / "_archive" / "A-999-Rev-A.pdf", [SheetSpec(drawing_no="A-999")])

    # Junk the scanner must exclude.
    (root / "Thumbs.db").write_bytes(b"not a drawing")
    (root / "desktop.ini").write_text("[.ShellClassInfo]", encoding="utf-8")
    (root / "~$register.xlsx").write_bytes(b"office lock file")

    # Other files worth reporting but not scanning as drawings.
    (root / "A-101.dwg").write_bytes(b"DWG placeholder")
    (root / "drawing-list.xlsx").write_bytes(b"XLSX placeholder")

    return root


# ── Drawing lists ──────────────────────────────────────────────────────


def build_clean_drawing_list(path: str | Path) -> Path:
    """A tidy register: header on row 1, one row per drawing."""
    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Register"
    sheet.append(["Drawing No", "Title", "Rev"])
    for number, title, revision in NORMAL_SET:
        sheet.append([number, title, revision])

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    book.save(target)
    return target


def build_messy_drawing_list(path: str | Path) -> Path:
    """The register as a human actually formats it.

    Reproduces what real files do: a logo block above the header, the header
    on row 7, a decoy worksheet, discipline headings inside the data, and
    blank separator rows.
    """
    import openpyxl

    book = openpyxl.Workbook()

    # A decoy sheet, of the kind every real workbook has.
    notes = book.active
    notes.title = "Notes"
    notes.append(["Project information"])
    notes.append(["Issued by", "Lami Architects"])
    notes.append(["Contact", "info@example.com"])

    sheet = book.create_sheet("Drawing Register")
    sheet.append(["ACME ARCHITECTS"])  # 1: logo row
    sheet.append([])  # 2
    sheet.append(["Project:", "Al Basateen Farm - Villa"])  # 3
    sheet.append(["Issue:", "IFC Rev D"])  # 4
    sheet.append(["Date:", "27/06/2025"])  # 5
    sheet.append([])  # 6
    sheet.append(["Dwg No.", "Sheet Name", "Rev.", "Status"])  # 7: the header
    sheet.append(["ARCHITECTURAL"])  # a discipline heading
    for number, title, revision in NORMAL_SET[:5]:
        sheet.append([number, title, revision, "For construction"])
    sheet.append([])  # separator
    sheet.append(["STRUCTURAL"])
    for number, title, revision in NORMAL_SET[5:]:
        sheet.append([number, title, revision, "For construction"])

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    book.save(target)
    return target


def build_revision_matrix_list(path: str | Path) -> Path:
    """A register with no Rev column: one column per issue date instead."""
    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Register"
    sheet.append(["Drawing No", "Title", "12/01/2025", "27/06/2025", "01/09/2026"])
    sheet.append(["A-101", "GROUND FLOOR PLAN", "A", "B", "C"])
    sheet.append(["A-102", "FIRST FLOOR PLAN", "A", "B", ""])
    sheet.append(["A-103", "ROOF PLAN", "A", "", ""])

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    book.save(target)
    return target


# ── Phase 3 fixtures: `03_renamed`, `08_naming_mess`, `09_collision` ────


#: The ten drawings of `03_renamed` shared by both issues, as
#: (old number, new number, title, body lines). The body of each drawing uses
#: words no other drawing shares, so content fingerprints of two different
#: drawings stay well below the 0.4 line while the old and new versions of one
#: drawing — identical body, identical title, identical sheet size — match at
#: 1.0. The body lines read as room names and notes pinned to each sheet.
RENAMED_SET: list[tuple[str, str, str, list[str]]] = [
    (
        "UVU-ARC-001",
        "UVU-KEO-XX-03-DR-A-0001",
        "GROUND FLOOR PLAN",
        ["TYPICAL ROOM LAYOUT", "WALL PARTITION HEIGHT 3001"],
    ),
    (
        "UVU-ARC-002",
        "UVU-KEO-XX-03-DR-A-0002",
        "ROOF PLAN",
        ["ROOF SLOPE FALL OUTLET", "PARAPET GUTTER 3002"],
    ),
    (
        "UVU-ARC-003",
        "UVU-KEO-XX-03-DR-A-0003",
        "EXTERNAL WALL SECTION",
        ["CAVITY INSULATION CLADDING", "TIE VAPOUR BARRIER 3003"],
    ),
    (
        "UVU-ARC-004",
        "UVU-KEO-XX-03-DR-A-0004",
        "STAIRCASE DETAILS",
        ["STAIR FLIGHT LANDING", "HANDRAIL RISER TREAD 3004"],
    ),
    (
        "UVU-ARC-005",
        "UVU-KEO-XX-03-DR-A-0005",
        "TOILET LAYOUT",
        ["TOILET SHOWER COMPARTMENT", "VANITY PIPEWORK VENT 3005"],
    ),
    (
        "UVU-ARC-006",
        "UVU-KEO-XX-03-DR-A-0006",
        "KITCHEN PLAN",
        ["KITCHEN BENCH HOOD", "EXTRACT DUCT SERVICE 3006"],
    ),
    (
        "UVU-ARC-007",
        "UVU-KEO-XX-03-DR-A-0007",
        "ENTRANCE LOBBY",
        ["ENTRANCE LOBBY RECEPTION", "DESK GLAZING SCREEN 3007"],
    ),
    (
        "UVU-ARC-008",
        "UVU-KEO-XX-03-DR-A-0008",
        "FOUNDATION PLAN",
        ["FOUNDATION STRIP CONCRETE", "REINFORCEMENT BLINDING BEAM 3008"],
    ),
    (
        "UVU-ARC-009",
        "UVU-KEO-XX-03-DR-A-0009",
        "FIRE ESCAPE ROUTES",
        ["CORRIDOR FIRE ESCAPE", "ROUTE EXIT LAMP 3009"],
    ),
    (
        "UVU-ARC-010",
        "UVU-KEO-XX-03-DR-A-0010",
        "PLANT DECK LAYOUT",
        ["PLANT DECK UNIT", "CONDENSER LOUVRE GRILLE 3010"],
    ),
]

#: A drawing present only in the old issue of `03_renamed`, so the matcher's
#: old-unmatched list has something real to hold.
RENAMED_OLD_ONLY: tuple[str, str, list[str]] = (
    "UVU-ARC-011",
    "OFFICE FURNITURE PLAN",
    ["FURNITURE SCHEDULE ITEM", "FINISH ANNOTATION 3011"],
)

#: A drawing present only in the new issue of `03_renamed`, so the matcher's
#: new-unmatched list has something real to hold.
RENAMED_NEW_ONLY: tuple[str, str, list[str]] = (
    "UVU-KEO-XX-03-DR-A-0011",
    "SIGNAGE SCHEDULE",
    ["WAYFINDING ARROW MARKING", "NOTICE ZONE 3012"],
)


def _renamed_old_filename(drawing_no: str, title: str) -> str:
    """The old-standard file name: the title travels with the number."""
    return f"{drawing_no}_{title.replace(' ', '_')}_RevC.pdf"


def _renamed_new_filename(drawing_no: str) -> str:
    """The new-standard file name: number only, no title."""
    return f"{drawing_no}_RevC.pdf"


def build_renamed_pair(root: str | Path) -> tuple[Path, Path]:
    """`03_renamed`: the same ten drawings under a new naming standard.

    The client changed the standard mid-project. Both sides carry the same ten
    drawings with identical title, body text and sheet size; only the drawing
    number (and so the file name) changes, `UVU-ARC-00N` on the old side and
    `UVU-KEO-XX-03-DR-A-000N` on the new. One extra drawing exists only on the
    old side and one only on the new side, so unmatched lists are exercised.
    Returns the (old, new) folders.
    """
    root = Path(root)
    old_dir = root / "old"
    new_dir = root / "new"

    for old_no, new_no, title, body in RENAMED_SET:
        revision = "C"
        build_pdf(
            old_dir / _renamed_old_filename(old_no, title),
            [SheetSpec(drawing_no=old_no, title=title, body=body, revision=revision)],
        )
        build_pdf(
            new_dir / _renamed_new_filename(new_no),
            [SheetSpec(drawing_no=new_no, title=title, body=body, revision=revision)],
        )

    old_no, old_title, old_body = RENAMED_OLD_ONLY
    build_pdf(
        old_dir / _renamed_old_filename(old_no, old_title),
        [SheetSpec(drawing_no=old_no, title=old_title, body=old_body, revision="C")],
    )

    new_no, new_title, new_body = RENAMED_NEW_ONLY
    build_pdf(
        new_dir / _renamed_new_filename(new_no),
        [SheetSpec(drawing_no=new_no, title=new_title, body=new_body, revision="C")],
    )

    return old_dir, new_dir


def build_naming_mess(root: str | Path) -> Path:
    """`08_naming_mess`: one folder full of naming sins on the same drawing.

    Every PDF is the same drawing — drawing number UVU-ARC-001 — saved under a
    name a real issue folder would contain: Windows copy junk, separators and
    case, a revision and status stamp, a date, a trailing space-dot, and en
    dashes that look exactly like hyphens. AGGRESSIVE cleaning must collapse
    all of them to ``uvuarc001``. Returns the ``new`` folder.
    """
    root = Path(root)
    new_dir = root / "new"
    spec = SheetSpec(drawing_no="UVU-ARC-001", title="GROUND FLOOR PLAN", revision="C")

    names = [
        "Copy of UVU-ARC-001.pdf",
        "UVU ARC 001 (1).pdf",
        "UVU_ARC_001_RevD_FOR APPROVAL.pdf",
        "01 - UVU-ARC-001 - 12.04.2026.pdf",
        "uvu-arc-001 FINAL final.pdf",
        "UVU\u2013ARC\u2013001.pdf",  # en dashes, not hyphens
        "UVU-ARC-001 .pdf",  # trailing space-dot
    ]
    for name in names:
        build_pdf(new_dir / name, [spec])

    return new_dir


def build_collision_folder(root: str | Path) -> Path:
    """`09_collision`: two different drawings whose cleaned names collide.

    Both received files read as drawing ``UVU-ARC-001`` once cleaned — the
    sequence prefix on the first and the date stamp on the second disappear at
    MEDIUM — so a naive rename that takes the number from the file name sends
    both to the same target. The title blocks tell the truth: they carry
    different drawing numbers (``UVU-ARC-001`` vs ``UVU-ARC-002``) and
    disjoint body text, so they are clearly two different drawings. Returns
    the ``new`` folder.
    """
    root = Path(root)
    new_dir = root / "new"

    build_pdf(
        new_dir / "01 - UVU-ARC-001.pdf",
        [
            SheetSpec(
                drawing_no="UVU-ARC-001",
                title="GROUND FLOOR PLAN",
                body=["ROOM 101 LAYOUT", "DOOR SCHEDULE D1 D2"],
            )
        ],
    )
    build_pdf(
        new_dir / "UVU-ARC-001 - 12.04.2026.pdf",
        [
            SheetSpec(
                drawing_no="UVU-ARC-002",
                title="ROOF PLAN",
                body=["ROOF DRAINAGE DETAIL", "FALL 1 IN 60 OUTLET"],
            )
        ],
    )

    return new_dir
