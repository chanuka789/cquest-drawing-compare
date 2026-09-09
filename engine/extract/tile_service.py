"""Serving layer for sheet tile pyramids: on-demand rendering, disk caching,
the in-memory level cache, and the pre-render queue (Phase 4, task 4.3).

The routes in ``engine/api/routes_tiles.py`` stay thin; everything that
renders, crops, encodes or queues lives here, built on the raster renderer
(``engine/extract/raster_renderer.py``) and the tiler
(``engine/extract/tiler.py``). One :class:`TileService` is created per
:class:`ComparisonSession` and attached to it, so the queue state and the
in-memory level cache survive individual HTTP requests.

How a tile gets served
----------------------
The pyramid for one sheet lives on disk under ``<workspace>/_audit/tiles`` in
exactly the layout the tiler defines (``<sheet_id>/<level>/<shard>/<shard>/
tile_<x>_<y>.png`` plus ``manifest.json``), so a :class:`TileCache`
constructed over that root later adopts every tile written here. A request
for a tile that exists is a pure file read. A request for a tile that does
not exist renders the PDF once at the service DPI (colour, converted to the
tiler's BGR convention), crops *only* the requested tile from the level
image, stores it, and serves it. Nothing ever re-renders the PDF for a tile
that is already on disk.

Why an in-memory level cache at all? A full render costs 1-2 seconds, far
more than the crop-and-encode of one tile, so serving a freshly opened sheet
tile by tile without re-rendering per request needs the rendered page kept
alive for a short while. The cache holds the full-level images (as tiler
:class:`~engine.extract.tiler.Pyramid` objects, which also memoise each
downscaled level) of at most ``LEVEL_IMAGE_CACHE`` sheets, most recently used
first, keyed by sheet id. Memory is guarded, not just bounded: a colour A0
render at 200 DPI is roughly 186 MB (9362 x 6622 x 3 uint8), so images larger
than ``MAX_LEVEL_IMAGE_BYTES`` (200 MB) are rendered per request and never
kept, and everything below that is capped at four sheets.

The queue
---------
The plan (task 4.3) says the queue runs in the Phase 2 process pool. That is
deliberately not done yet: pdfium renders serialise on the process-wide lock
anyway (see ``engine/utils/pdf_runtime.py``), so parallel workers inside one
process add nothing until the alignment batches of task 4.10 exist and can
run in separate processes. Until then one daemon worker per service drains a
FIFO queue, one sheet (whole pyramid, all levels and tiles) at a time, and a
worker is only alive while there is work, so replacing the session never
leaks an idle thread. The four priority levels (``immediate``, ``high``,
``normal``, ``low``) are validated and accepted but every job currently runs
FIFO; the distinction becomes real when the pool lands. Queue progress is
polled through ``GET /api/render/status`` (a small dict, like the matching
state) rather than streamed over the WebSocket, which keeps this phase small.

Cache identity
--------------
A sheet id is the first 16 hex characters of ``sha1(abs_path|page_index)``
(computed by the routes module, which owns the session index). Tiles are
therefore immutable while the PDF at that path is unchanged; detecting that a
file was replaced and re-rendering its pyramid is deliberately out of scope
for task 4.3.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict, deque
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

import cv2
import numpy as np
from loguru import logger

from engine.extract.raster_renderer import RenderOptions, render_page
from engine.extract.tiler import build_pyramid, load_manifest, tile_path, tiles_root
from engine.utils.errors import NotFoundError, ValidationError
from engine.utils.longpath import long_path

if TYPE_CHECKING:
    from engine.core.session import ComparisonSession
    from engine.extract.tiler import Pyramid, PyramidManifest

#: How many full-level images the in-memory cache keeps per service.
LEVEL_IMAGE_CACHE = 4

#: Above this size a level image is rendered per request and never kept.
#: A colour A0 render at 200 DPI is ~186 MB; this guard sits just above it.
MAX_LEVEL_IMAGE_BYTES = 200 * 1024 * 1024

_PNG_EXTENSION = ".png"


def _read_bytes(target: Path) -> bytes | None:
    """The file's bytes, or None when it does not exist (or cannot be read)."""
    try:
        return Path(long_path(target)).read_bytes()
    except OSError:
        return None


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """Write *data* atomically (temp file + ``os.replace``), like the tiler."""
    real = Path(long_path(target))
    real.parent.mkdir(parents=True, exist_ok=True)
    temp = real.with_name(f".{real.name}.{os.getpid()}.tmp")
    try:
        temp.write_bytes(data)
        os.replace(temp, real)
    except BaseException:
        with suppress(OSError):
            temp.unlink()
        raise


def _encode_png(image: np.ndarray) -> bytes:
    ok, buffer = cv2.imencode(_PNG_EXTENSION, image)
    if not ok:
        raise RuntimeError("cv2.imencode failed to write the PNG")
    return buffer.tobytes()


class TileService:
    """Tile pyramids and the render queue for one comparison session.

    Constructed once per session by the routes (which pass the session's
    sheet-index builder); every method is safe to call from the API thread
    pool and from the queue worker.
    """

    def __init__(
        self,
        session: ComparisonSession,
        *,
        dpi: int,
        sheet_index: Callable[[ComparisonSession], dict[str, tuple[str, int]] | None],
    ) -> None:
        self._session = session
        self._dpi = dpi
        self._sheet_index = sheet_index
        self._lock = threading.Lock()
        #: Most-recently-used full-level images, keyed by sheet id.
        self._levels: OrderedDict[str, Pyramid] = OrderedDict()
        self._pending: deque[str] = deque()
        self._inflight: set[str] = set()
        self._worker: threading.Thread | None = None
        self._done = 0
        self._failed = 0
        self._last_message = ""

    # -- identity and paths ------------------------------------------------

    @property
    def _tiles_root(self) -> Path:
        """The on-disk tile cache root for the session's workspace."""
        workspace = self._session.workspace
        if workspace is None:
            raise ValidationError("Choose an output folder before viewing sheets.")
        return tiles_root(workspace)

    def _lookup(self, sheet_id: str) -> tuple[str, int] | None:
        index = self._sheet_index(self._session)
        return (index or {}).get(sheet_id)

    def _require_sheet(self, sheet_id: str) -> tuple[str, int]:
        entry = self._lookup(sheet_id)
        if entry is None:
            raise NotFoundError(
                "That sheet is not part of this comparison.", detail={"sheet_id": sheet_id}
            )
        return entry

    def _name_of(self, sheet_id: str) -> str:
        entry = self._lookup(sheet_id)
        return Path(entry[0]).name if entry is not None else sheet_id

    # -- rendering ---------------------------------------------------------

    def _render_pyramid(self, sheet_id: str) -> Pyramid:
        """Render one sheet's page at the service DPI and return its pyramid.

        The render is colour (the viewer shows the sheet as issued); the
        renderer returns RGB, so colour is converted to the tiler's BGR
        convention here. Grayscale passes straight through. Pyramids within
        the byte guard are kept in the in-memory LRU.
        """
        abs_path, page_index = self._require_sheet(sheet_id)
        result = render_page(
            abs_path,
            page_index,
            RenderOptions(dpi=self._dpi, colour=True, grayscale_for_compare=False),
        )
        if result.colour is not None:
            image = cv2.cvtColor(result.colour, cv2.COLOR_RGB2BGR)
        elif result.grayscale is not None:
            image = result.grayscale
        else:
            raise RuntimeError(f"render_page returned no image for sheet {sheet_id}")
        pyramid = build_pyramid(image, page_index=page_index, dpi=self._dpi)
        if image.nbytes <= MAX_LEVEL_IMAGE_BYTES:
            with self._lock:
                self._levels[sheet_id] = pyramid
                self._levels.move_to_end(sheet_id)
                while len(self._levels) > LEVEL_IMAGE_CACHE:
                    dropped = self._levels.popitem(last=False)
                    logger.debug("Evicted {} from the in-memory tile cache", dropped[0])
        else:
            logger.warning(
                "Level image of {} is {:.0f} MB; rendering per request without "
                "caching (over the {:.0f} MB guard)",
                sheet_id,
                image.nbytes / 1024 / 1024,
                MAX_LEVEL_IMAGE_BYTES / 1024 / 1024,
            )
        return pyramid

    def _level_pyramid(self, sheet_id: str) -> Pyramid:
        """The cached pyramid for *sheet_id*, rendering it when not cached."""
        with self._lock:
            pyramid = self._levels.get(sheet_id)
            if pyramid is not None:
                self._levels.move_to_end(sheet_id)
                return pyramid
        return self._render_pyramid(sheet_id)

    def _manifest(self, sheet_id: str) -> PyramidManifest:
        """The sheet's manifest, rendering and writing it when absent.

        ``manifest.json`` only records geometry; serving tiles never needs
        it, and pre-rendering only writes the manifest at the end of a sheet.
        """
        root = self._tiles_root
        manifest = load_manifest(root, sheet_id)
        if manifest is not None:
            return manifest
        pyramid = self._render_pyramid(sheet_id)
        pyramid.save_manifest(root, sheet_id)
        return pyramid.build_manifest()

    # -- the public serving API --------------------------------------------

    def ensure_manifest(self, sheet_id: str) -> dict[str, Any]:
        """The manifest of *sheet_id* as the JSON the viewer expects.

        Generates (renders) the pyramid on demand when no manifest exists
        yet; generation itself writes no tiles - only ``manifest.json``.
        """
        entry = self._require_sheet(sheet_id)
        manifest = self._manifest(sheet_id)
        return {
            "sheet_id": sheet_id,
            "filename": Path(entry[0]).name,
            "dpi": manifest.dpi,
            "width_px": manifest.width_px,
            "height_px": manifest.height_px,
            "levels": [level.as_dict() for level in manifest.levels],
            "tile_size": manifest.tile_size,
        }

    def get_tile(self, sheet_id: str, level: int, x: int, y: int) -> bytes:
        """PNG bytes for one tile, computing and storing it when missing.

        Tile files are immutable once written: a request for a stored tile is
        a pure file read. A missing tile is cropped from the level image
        (rendered once per sheet, cached in the in-memory LRU) and stored, so
        the next request for it is also a pure file read.
        """
        root = self._tiles_root
        manifest = self._manifest(sheet_id)
        level_info = next((info for info in manifest.levels if info.level == level), None)
        if level_info is None or x < 0 or y < 0 or x >= level_info.cols or y >= level_info.rows:
            raise NotFoundError(
                "That tile is outside this sheet's rendered area.",
                detail={
                    "sheet_id": sheet_id,
                    "level": level,
                    "x": x,
                    "y": y,
                    "cols": level_info.cols if level_info is not None else None,
                    "rows": level_info.rows if level_info is not None else None,
                },
            )
        target = tile_path(root, sheet_id, level, x, y)
        data = _read_bytes(target)
        if data is not None:
            return data
        pyramid = self._level_pyramid(sheet_id)
        png = _encode_png(pyramid.crop_tile(level, x, y))
        _atomic_write_bytes(target, png)
        return png

    def get_thumbnail(self, sheet_id: str) -> bytes:
        """The level-0 (whole-sheet) tile PNG - the thumbnail."""
        return self.get_tile(sheet_id, 0, 0, 0)

    # -- the pre-render queue ----------------------------------------------

    def enqueue(self, sheet_ids: Sequence[str], priority: str = "normal") -> int:
        """Queue whole-pyramid pre-renders; returns how many sheets are new.

        A sheet already pending, already in flight, or listed twice in one
        request is never queued twice (dedupe). ``priority`` is accepted for
        API compatibility but every job runs FIFO in the single worker for
        now - see the module docstring.
        """
        with self._lock:
            known: set[str] = set(self._pending) | set(self._inflight)
            fresh: list[str] = []
            for sheet_id in sheet_ids:
                if sheet_id not in known:
                    known.add(sheet_id)
                    fresh.append(sheet_id)
            self._pending.extend(fresh)
            worker = self._worker
            if worker is None or not worker.is_alive():
                self._worker = threading.Thread(
                    target=self._drain, name="cqdc-tile-render", daemon=True
                )
                self._worker.start()
        if fresh:
            logger.info("Queued {} sheet(s) for tile pre-render", len(fresh))
        return len(fresh)

    def status(self) -> dict[str, Any]:
        """Queue state: pending, finished, failed, running, last message."""
        with self._lock:
            return {
                "queued": len(self._pending),
                "done": self._done,
                "failed": self._failed,
                "running": bool(self._inflight),
                "last_message": self._last_message,
            }

    def _render_sheet(self, sheet_id: str) -> int:
        """Render every level and tile of one sheet to disk. Tiles written."""
        pyramid = self._render_pyramid(sheet_id)
        root = self._tiles_root
        written = 0
        for spec in pyramid.levels:
            for y in range(spec.rows):
                for x in range(spec.cols):
                    pyramid.save_tile(root, sheet_id, spec.level, x, y)
                    written += 1
        pyramid.save_manifest(root, sheet_id)
        return written

    def _drain(self) -> None:
        """Worker loop: pop the FIFO queue, render one sheet per iteration.

        The worker exits when the queue is empty and clears itself under the
        lock, so a later :meth:`enqueue` starts a fresh one and no idle
        thread outlives a session reset.
        """
        while True:
            with self._lock:
                if not self._pending:
                    self._worker = None
                    return
                sheet_id = self._pending.popleft()
                self._inflight.add(sheet_id)
            try:
                written = self._render_sheet(sheet_id)
            except Exception as exc:
                logger.warning("Tile pre-render failed for sheet {}: {}", sheet_id, exc)
                with self._lock:
                    self._failed += 1
                    self._last_message = f"Failed to pre-render {self._name_of(sheet_id)}"
            else:
                with self._lock:
                    self._done += 1
                    self._last_message = f"Pre-rendered {self._name_of(sheet_id)} ({written} tiles)"
            finally:
                with self._lock:
                    self._inflight.discard(sheet_id)
