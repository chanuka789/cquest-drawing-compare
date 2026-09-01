"""Stage 1, pass 2: open each PDF and find out what it really is.

This is the expensive pass, so it runs in the background with progress and is
cached by (path, size, modified time).

Deliberate choices worth knowing:

* `pypdfium2` reads pages, geometry and text; `pikepdf` reads encryption,
  optional content groups and metadata. Neither is AGPL, so both can ship in
  a commercial product. PyMuPDF is not used anywhere.
* Object enumeration is skipped when a page already has plenty of text. On a
  real A1 sheet that is nine thousand path objects we do not need to walk.
* Nothing here raises for a bad file. A failure is returned as part of the
  result so one corrupt drawing cannot stop a 300-file run.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw
from loguru import logger

from engine.utils.longpath import long_path

POINTS_PER_MM = 72.0 / 25.4

#: ISO A series, portrait, in millimetres.
SHEET_SIZES: dict[str, tuple[float, float]] = {
    "A0": (841.0, 1189.0),
    "A1": (594.0, 841.0),
    "A2": (420.0, 594.0),
    "A3": (297.0, 420.0),
    "A4": (210.0, 297.0),
}

#: How far off a nominal size a sheet may be and still be called that size.
#: Real title blocks add a binding margin, so the tolerance is generous.
SHEET_SIZE_TOLERANCE_MM = 25.0

#: Below this many characters, a page is treated as having no usable text.
MIN_TEXT_CHARS = 20


def detect_sheet_size(width_mm: float, height_mm: float) -> str:
    """Name the nearest ISO A size, or 'Custom'. Orientation is ignored."""
    short, long_side = sorted((width_mm, height_mm))

    best_name = "Custom"
    best_error = SHEET_SIZE_TOLERANCE_MM
    for name, (nominal_short, nominal_long) in SHEET_SIZES.items():
        error = max(abs(short - nominal_short), abs(long_side - nominal_long))
        if error < best_error:
            best_name, best_error = name, error
    return best_name


@dataclass(slots=True)
class PageInfo:
    """One page of a PDF. In this product a page is a drawing sheet."""

    index: int
    width_mm: float
    height_mm: float
    sheet_size: str
    rotation: int
    text_chars: int
    image_count: int
    is_landscape: bool

    @property
    def has_text(self) -> bool:
        return self.text_chars >= MIN_TEXT_CHARS

    @property
    def looks_scanned(self) -> bool:
        """A big image and no text: a photocopy, not a vector drawing."""
        return self.image_count > 0 and not self.has_text


@dataclass(slots=True)
class PdfInfo:
    """Everything the deep pass learned about one file."""

    path: str
    size: int = 0
    page_count: int = 0
    pages: list[PageInfo] = field(default_factory=list)
    is_encrypted: bool = False
    needs_password: bool = False
    layer_names: list[str] = field(default_factory=list)
    producer: str | None = None
    is_readable: bool = True
    error_note: str | None = None

    @property
    def has_layers(self) -> bool:
        return bool(self.layer_names)

    @property
    def has_text_layer(self) -> bool:
        return any(page.has_text for page in self.pages)

    @property
    def looks_scanned(self) -> bool:
        """True when every page looks like a photocopy."""
        return bool(self.pages) and all(page.looks_scanned for page in self.pages)

    @property
    def is_multipage(self) -> bool:
        return self.page_count > 1

    def as_dict(self) -> dict[str, object]:
        """Serialisable form, used for the cache and for process-pool results."""
        return {
            "path": self.path,
            "size": self.size,
            "page_count": self.page_count,
            "pages": [asdict(page) for page in self.pages],
            "is_encrypted": self.is_encrypted,
            "needs_password": self.needs_password,
            "layer_names": list(self.layer_names),
            "producer": self.producer,
            "is_readable": self.is_readable,
            "error_note": self.error_note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> PdfInfo:
        pages = [PageInfo(**page) for page in data.get("pages", [])]  # type: ignore[arg-type]
        return cls(
            path=str(data["path"]),
            size=int(data.get("size", 0)),  # type: ignore[arg-type]
            page_count=int(data.get("page_count", 0)),  # type: ignore[arg-type]
            pages=pages,
            is_encrypted=bool(data.get("is_encrypted", False)),
            needs_password=bool(data.get("needs_password", False)),
            layer_names=list(data.get("layer_names", [])),  # type: ignore[arg-type]
            producer=data.get("producer"),  # type: ignore[arg-type]
            is_readable=bool(data.get("is_readable", True)),
            error_note=data.get("error_note"),  # type: ignore[arg-type]
        )


def _read_container_facts(path: str, info: PdfInfo) -> None:
    """Encryption, optional content groups and producer, read with pikepdf."""
    try:
        with pikepdf.open(long_path(path)) as pdf:
            info.is_encrypted = pdf.is_encrypted
            producer = pdf.docinfo.get("/Producer") if pdf.docinfo is not None else None
            info.producer = str(producer) if producer else None

            root = pdf.Root
            if "/OCProperties" in root:
                for group in root.OCProperties.get("/OCGs", []):
                    name = group.get("/Name")
                    if name:
                        info.layer_names.append(str(name))
    except pikepdf.PasswordError:
        info.is_encrypted = True
        info.needs_password = True
        info.is_readable = False
        info.error_note = (
            "This file is password-protected. Ask the sender for an unprotected "
            "copy, or remove the password and scan again."
        )
    except pikepdf.PdfError as exc:
        info.is_readable = False
        info.error_note = (
            "This file is damaged and could not be opened. Try re-downloading it "
            f"from the source. ({exc})"
        )


def _read_pages(path: str, info: PdfInfo) -> None:
    """Page geometry, rotation and content, read with pypdfium2."""
    document = None
    try:
        document = pdfium.PdfDocument(long_path(path))
        info.page_count = len(document)

        for index in range(len(document)):
            page = document[index]
            width_pt, height_pt = page.get_size()
            width_mm = width_pt / POINTS_PER_MM
            height_mm = height_pt / POINTS_PER_MM

            text_chars = 0
            try:
                textpage = page.get_textpage()
                text_chars = len(textpage.get_text_range().strip())
            except pdfium.PdfiumError:
                text_chars = 0

            # Only walk the page objects when there is no text to go on.
            # A real A1 sheet holds thousands of paths and counting them all
            # for every file would dominate the deep pass.
            image_count = 0
            if text_chars < MIN_TEXT_CHARS:
                try:
                    image_count = sum(
                        1 for obj in page.get_objects() if obj.type == pdfium_raw.FPDF_PAGEOBJ_IMAGE
                    )
                except pdfium.PdfiumError:
                    image_count = 0

            info.pages.append(
                PageInfo(
                    index=index,
                    width_mm=round(width_mm, 1),
                    height_mm=round(height_mm, 1),
                    sheet_size=detect_sheet_size(width_mm, height_mm),
                    rotation=page.get_rotation(),
                    text_chars=text_chars,
                    image_count=image_count,
                    is_landscape=width_mm >= height_mm,
                )
            )
    except pdfium.PdfiumError as exc:
        info.is_readable = False
        if info.error_note is None:
            info.error_note = (
                f"This file could not be read as a PDF. It may be damaged or incomplete. ({exc})"
            )
    finally:
        if document is not None:
            document.close()


def inspect_pdf(path: str | Path, size: int | None = None) -> PdfInfo:
    """Open one PDF and report what it contains.

    Never raises for a bad file: the failure is recorded on the result so the
    caller can quarantine it and carry on.
    """
    target = str(path)
    info = PdfInfo(path=target)

    try:
        info.size = size if size is not None else Path(long_path(target)).stat().st_size
    except OSError as exc:
        info.is_readable = False
        info.error_note = (
            "This file could not be opened. It may have been moved or the "
            f"network drive disconnected. ({exc.strerror})"
        )
        return info

    _read_container_facts(target, info)
    if info.needs_password:
        return info  # nothing more can be read without the password

    _read_pages(target, info)

    if info.is_readable and info.page_count == 0:
        info.is_readable = False
        info.error_note = "This PDF contains no pages."

    if not info.is_readable:
        logger.warning("Unreadable file | {} | {}", target, info.error_note)

    return info
