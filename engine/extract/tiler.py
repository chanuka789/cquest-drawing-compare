"""Lossless tile pyramids for rendered sheets, and the LRU-bounded tile cache.

Phase 4 renders each sheet once and serves it many times: the lightbox pans
and zooms, the alignment methods read the same pixels from different levels,
and Phase 5 diffs will read them again. Task 4.2 turns one full-resolution
image into a pyramid of lossless tiles and stores them on disk with a size
budget, so nothing downstream ever re-renders the PDF unless a tile is really
missing.

Why a pyramid, and how the levels are numbered
----------------------------------------------
A map-server scheme: level 0 is the whole sheet squeezed into one tile and
every higher level doubles the resolution, so the top level is the original
image:

    Level 0   1 x 1 tile          (thumbnail, fits inside one tile)
    Level 1   2 x 2 tiles
    ...
    Level N   full resolution     (never resampled -- tiles are exact crops)

A sheet of ``max(w, h)`` pixels gets ``ceil(log2(max / tile_size)) + 1``
levels; level *L* is the source downscaled by ``1 / 2 ** (N - L)`` (always
``INTER_AREA``, and the top level is the source itself). A sheet that already
fits inside one tile has a single level 0.

Lossless, always
----------------
Tiles are PNG via ``cv2.imencode``/``cv2.imdecode`` and nothing else. JPEG
ringing around line work shows up as invented differences in Phase 5. Colour
input is expected in **BGR** channel order (the OpenCV convention and what
the pdfium renderer produces); 4-channel BGRA input is flattened onto white.
Grayscale (2D) input stays grayscale. Only uint8 images are accepted.

The cache key
-------------
The full key of one tile is ``file_hash + page_index + dpi + level + x + y``.
Callers compose the sheet part as ``sheet_key = f"{file_hash}:{page_index}:{dpi}"``
and the remaining components are encoded in the directory and file name, so
the full key is recoverable from the on-disk layout alone:

    <root>/<sheet_key>/<level>/<xx>/<yy>/tile_<x>_<y>.png

``xx``/``yy`` are two-hex-digit shards of ``x >> 8`` and ``y >> 8`` so no leaf
directory grows without bound. For the sizes this app renders (A0 at 300 dpi
is a 28 x 20 grid at the top level) each leaf stays around 560 files or less,
comfortably under the ~1000-file rule of thumb. ``sheet_key`` is sanitised
before it touches the filesystem, because the conventional ``"hash:0:200"``
form contains colons, which Windows refuses in file names.

Manifest and cache live in the workspace
----------------------------------------
Per sheet, ``manifest.json`` records levels, tile counts, dimensions, DPI and
the source file identity, so the viewer knows what exists without probing.
Tiles are stored under ``<workspace>/_audit/tiles`` (see :func:`tiles_root`)
so the cache travels with the comparison. :class:`TileCache` owns that root:
it keeps an ``lru.json`` touch-order index guarded by a ``threading.Lock`` and
evicts the least recently used tiles against a byte budget (default 5 GB).
"""

from __future__ import annotations

import json
import math
import operator
import os
import re
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import cv2
import numpy as np
from loguru import logger
from numpy.typing import NDArray

from engine.utils.longpath import long_path

if TYPE_CHECKING:
    from engine.core.workspace import Workspace

#: Default square tile edge in pixels. Level 0 fits the whole sheet in one tile.
tile_size = 512

#: The directory a workspace keeps its tile cache in.
TILE_DIR_NAME = "tiles"

MANIFEST_FILE_NAME = "manifest.json"
LRU_FILE_NAME = "lru.json"
LRU_FORMAT_VERSION = 1

#: Default cache budget: 5 GB of tiles before the oldest are evicted.
DEFAULT_CACHE_BUDGET_BYTES = 5 * 1024**3

ImageArray = NDArray[np.uint8]

_PNG_EXTENSION = ".png"
_TILE_PREFIX = "tile_"

#: Characters Windows refuses in a file or directory name.
_WINDOWS_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def tiles_root(workspace: Workspace) -> Path:
    """The tile cache directory for a workspace: ``<workspace>/_audit/tiles``.

    ``workspace`` is any object exposing an ``audit_dir`` path (the
    ``engine.core.workspace.Workspace`` class does); it is only touched
    through that attribute so this module never imports the workspace module
    at import time.
    """
    return Path(workspace.audit_dir) / TILE_DIR_NAME


# ── Geometry and manifest types ────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class LevelSpec:
    """One pyramid level: its tile grid and its scale relative to the source."""

    level: int
    cols: int
    rows: int
    #: Level image width divided by source width; 1.0 at the top (full) level.
    scale: float


@dataclass(frozen=True, slots=True)
class LevelInfo:
    """A level as recorded in a manifest."""

    level: int
    cols: int
    rows: int
    tile_count: int

    def as_dict(self) -> dict[str, int]:
        return {
            "level": self.level,
            "cols": self.cols,
            "rows": self.rows,
            "tile_count": self.tile_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LevelInfo:
        return cls(
            level=int(data["level"]),
            cols=int(data["cols"]),
            rows=int(data["rows"]),
            tile_count=int(data["tile_count"]),
        )


@dataclass(frozen=True, slots=True)
class PyramidManifest:
    """Everything the viewer needs to know about a sheet without probing tiles."""

    #: Hash of the source PDF file the tiles were rendered from.
    file_hash: str = ""
    #: Page inside that PDF.
    page_index: int = 0
    #: Render DPI. ``None`` when the pyramid was built without render metadata.
    dpi: int | None = None
    width_px: int = 0
    height_px: int = 0
    levels: list[LevelInfo] = field(default_factory=list)
    tile_size: int = tile_size
    created_at_iso: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "file_hash": self.file_hash,
            "page_index": self.page_index,
            "dpi": self.dpi,
            "width_px": self.width_px,
            "height_px": self.height_px,
            "levels": [level.as_dict() for level in self.levels],
            "tile_size": self.tile_size,
            "created_at_iso": self.created_at_iso,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PyramidManifest:
        return cls(
            file_hash=str(data["file_hash"]),
            page_index=int(data["page_index"]),
            dpi=None if data["dpi"] is None else int(data["dpi"]),
            width_px=int(data["width_px"]),
            height_px=int(data["height_px"]),
            levels=[LevelInfo.from_dict(entry) for entry in data["levels"]],
            tile_size=int(data["tile_size"]),
            created_at_iso=str(data["created_at_iso"]),
        )


# ── Path layout ─────────────────────────────────────────────────────────────


def _sanitise_sheet_key(sheet_key: str) -> str:
    """Make a sheet key safe as one directory name.

    The conventional key ``"<file_hash>:<page_index>:<dpi>"`` contains
    colons, which are illegal in Windows file names, so they are replaced
    deterministically. Distinct keys that differ only in illegal characters
    could collide; the convention never produces such a pair.
    """
    cleaned = _WINDOWS_ILLEGAL.sub("_", str(sheet_key)).strip(" .")
    if not cleaned or cleaned in {".", ".."}:
        return "sheet"
    return cleaned


def sheet_dir(dir_root: str | Path, sheet_key: str) -> Path:
    """The per-sheet directory under *dir_root*."""
    return Path(dir_root) / _sanitise_sheet_key(sheet_key)


def tile_rel(sheet_key: str, level: int, x: int, y: int) -> str:
    """The tile's path relative to a cache root, e.g. ``a1b2/3/01/00/tile_256_5.png``.

    Shards are two-hex-digit subdirectories of ``x >> 8`` and ``y >> 8``, so
    a directory fills only as x or y crosses a 256-tile boundary.
    """
    level_i = operator.index(level)
    x_i = operator.index(x)
    y_i = operator.index(y)
    if min(level_i, x_i, y_i) < 0:
        raise ValueError(f"level, x and y must be >= 0, got ({level_i}, {x_i}, {y_i})")
    return (
        f"{_sanitise_sheet_key(sheet_key)}/{level_i}/"
        f"{x_i >> 8:02x}/{y_i >> 8:02x}/{_TILE_PREFIX}{x_i}_{y_i}{_PNG_EXTENSION}"
    )


def tile_path(dir_root: str | Path, sheet_key: str, level: int, x: int, y: int) -> Path:
    """Absolute layout of one tile: ``dir_root/<sheet_key>/<level>/<xx>/<yy>/tile_x_y.png``."""
    return Path(dir_root) / tile_rel(sheet_key, level, x, y)


def manifest_path(dir_root: str | Path, sheet_key: str) -> Path:
    """Where a sheet's manifest.json lives."""
    return sheet_dir(dir_root, sheet_key) / MANIFEST_FILE_NAME


# ── Image helpers ───────────────────────────────────────────────────────────


def _normalise(image: NDArray[np.uint8]) -> ImageArray:
    """Validate *image* and bring it to the module convention (2D gray or 3D BGR).

    4-channel BGRA input is flattened onto a white background, matching how a
    PDF renderer paints transparent regions over paper.
    """
    if not isinstance(image, np.ndarray):
        raise TypeError(f"expected a numpy array, got {type(image).__name__}")
    if image.dtype != np.uint8:
        raise TypeError(f"tiles must be uint8 (8-bit) images, got dtype {image.dtype}")
    if image.ndim == 2:
        return image
    if image.ndim == 3:
        channels = image.shape[2]
        if channels == 3:
            return image
        if channels == 4:
            alpha = image[..., 3:4].astype(np.float32) / 255.0
            colour = image[..., :3].astype(np.float32)
            flattened = colour * alpha + 255.0 * (1.0 - alpha)
            return flattened.astype(np.uint8)
    raise ValueError(f"expected a 2D grayscale or 3/4-channel image, got shape {image.shape}")


def _encode_png(image: ImageArray) -> bytes:
    ok, buffer = cv2.imencode(_PNG_EXTENSION, image)
    if not ok:
        raise RuntimeError("cv2.imencode failed to write the PNG")
    return buffer.tobytes()


def _decode_png(data: bytes) -> ImageArray | None:
    """Decode PNG *data* back to the exact pixel values that were encoded."""
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None or image.dtype != np.uint8:
        return None
    return image


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """Write *data* to *target* atomically (temp file + ``os.replace``).

    Readers never observe a half-written PNG or JSON file.
    """
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


def _read_bytes(target: Path) -> bytes | None:
    try:
        return Path(long_path(target)).read_bytes()
    except OSError:
        return None


def _top_level(width: int, height: int, size: int) -> int:
    """Highest level index: the level whose image is the original."""
    largest = max(width, height)
    if largest <= size:
        return 0
    return math.ceil(math.log2(largest / size))


def _level_size(full: int, halves: int) -> int:
    """Full size downscaled by 2**halves, rounded to the nearest integer."""
    if halves <= 0:
        return full
    return (full + (1 << (halves - 1))) // (1 << halves)


def load_tile(
    dir_root: str | Path, sheet_key: str, level: int, x: int, y: int
) -> ImageArray | None:
    """Read one tile PNG from disk, or None when it has not been written yet.

    A tile that exists but cannot be decoded (truncated or corrupt) also
    yields None, so callers can treat it as a miss and re-render.
    """
    data = _read_bytes(tile_path(dir_root, sheet_key, level, x, y))
    if data is None:
        return None
    return _decode_png(data)


def load_manifest(dir_root: str | Path, sheet_key: str) -> PyramidManifest | None:
    """Read a sheet's manifest.json, or None when absent or unreadable."""
    target = manifest_path(dir_root, sheet_key)
    data = _read_bytes(target)
    if data is None:
        return None
    try:
        return PyramidManifest.from_dict(json.loads(data))
    except (ValueError, KeyError, TypeError) as error:
        logger.warning("Ignoring unreadable tile manifest {}: {}", target, error)
        return None


# ── The pyramid ─────────────────────────────────────────────────────────────


class Pyramid:
    """An in-memory sheet image sliced into a map-server-style tile pyramid.

    The input image is kept **by reference** as the top level (no copy): do
    not mutate it while the pyramid is in use. Lower levels are resized from
    the source on first use with :func:`cv2.resize` and ``INTER_AREA``, and
    cached for the lifetime of the pyramid.
    """

    def __init__(
        self,
        image: ImageArray,
        tile_size: int = tile_size,
        *,
        invert_white: bool = False,
        file_hash: str = "",
        page_index: int = 0,
        dpi: int | None = None,
    ) -> None:
        if tile_size < 1:
            raise ValueError(f"tile_size must be >= 1, got {tile_size}")
        normalised = _normalise(image)
        if normalised.size == 0:
            raise ValueError("cannot build a pyramid over an empty image")
        #: "invert white" flips the polarity so paper is black and marks are
        #: bright; alignment helpers that count bright "ink" pixels use this.
        if invert_white:
            normalised = cv2.bitwise_not(normalised)

        self.image = normalised
        self.tile_size = tile_size
        self.file_hash = file_hash
        self.page_index = page_index
        self.dpi = dpi

        height, width = normalised.shape[:2]
        top = _top_level(width, height, tile_size)
        specs: list[LevelSpec] = []
        sizes: list[tuple[int, int]] = []
        for level in range(top + 1):
            level_w = _level_size(width, top - level)
            level_h = _level_size(height, top - level)
            specs.append(
                LevelSpec(
                    level=level,
                    cols=(level_w + tile_size - 1) // tile_size,
                    rows=(level_h + tile_size - 1) // tile_size,
                    scale=level_w / width,
                )
            )
            sizes.append((level_w, level_h))

        self._specs = tuple(specs)
        self._sizes = tuple(sizes)
        self._level_images: dict[int, ImageArray] = {}
        self._image_lock = threading.Lock()

    # -- geometry --------------------------------------------------------

    @property
    def levels(self) -> tuple[LevelSpec, ...]:
        """One spec per level, level 0 (thumbnail) first, top level last."""
        return self._specs

    @property
    def top_level(self) -> int:
        return self._specs[-1].level

    @property
    def width_px(self) -> int:
        return self._sizes[-1][0]

    @property
    def height_px(self) -> int:
        return self._sizes[-1][1]

    def _spec_for(self, level: int) -> LevelSpec:
        for spec in self._specs:
            if spec.level == level:
                return spec
        raise ValueError(f"pyramid has levels 0..{self.top_level}, not level {level}")

    # -- tiles -----------------------------------------------------------

    def _level_image(self, level: int) -> ImageArray:
        """The level image, resized from the source once and cached."""
        self._spec_for(level)
        if level == self.top_level:
            return self.image
        with self._image_lock:
            cached = self._level_images.get(level)
            if cached is not None:
                return cached
            level_w, level_h = self._sizes[level]
            resized = cv2.resize(self.image, (level_w, level_h), interpolation=cv2.INTER_AREA)
            self._level_images[level] = resized
            return resized

    def crop_tile(self, level: int, x: int, y: int) -> ImageArray:
        """The (x, y) tile of *level* as an ndarray; edge tiles are partial."""
        spec = self._spec_for(level)
        x_i = operator.index(x)
        y_i = operator.index(y)
        if x_i < 0 or x_i >= spec.cols or y_i < 0 or y_i >= spec.rows:
            raise ValueError(
                f"tile ({x_i}, {y_i}) is outside level {level} (a {spec.cols}x{spec.rows} grid)"
            )
        image = self._level_image(level)
        level_w, level_h = self._sizes[level]
        x0 = x_i * self.tile_size
        y0 = y_i * self.tile_size
        return image[y0 : min(y0 + self.tile_size, level_h), x0 : min(x0 + self.tile_size, level_w)]

    def save_tile(self, dir_root: str | Path, sheet_key: str, level: int, x: int, y: int) -> Path:
        """Write one tile as a lossless PNG and return the path written."""
        tile = self.crop_tile(level, x, y)
        target = tile_path(dir_root, sheet_key, level, x, y)
        _atomic_write_bytes(target, _encode_png(tile))
        return target

    def load_tile(
        self, dir_root: str | Path, sheet_key: str, level: int, x: int, y: int
    ) -> ImageArray | None:
        """Read any on-disk tile (same signature and layout as :func:`load_tile`)."""
        return load_tile(dir_root, sheet_key, level, x, y)

    # -- manifest --------------------------------------------------------

    def build_manifest(self) -> PyramidManifest:
        """The manifest describing this pyramid, stamped with the current time."""
        return PyramidManifest(
            file_hash=self.file_hash,
            page_index=self.page_index,
            dpi=self.dpi,
            width_px=self.width_px,
            height_px=self.height_px,
            levels=[
                LevelInfo(spec.level, spec.cols, spec.rows, spec.cols * spec.rows)
                for spec in self._specs
            ],
            tile_size=self.tile_size,
            created_at_iso=datetime.now(UTC).isoformat(),
        )

    def save_manifest(self, dir_root: str | Path, sheet_key: str) -> Path:
        """Write manifest.json next to the sheet's tiles; returns the path."""
        target = manifest_path(dir_root, sheet_key)
        payload = json.dumps(self.build_manifest().as_dict(), indent=2).encode("utf-8")
        _atomic_write_bytes(target, payload)
        return target

    def load_manifest(self, dir_root: str | Path, sheet_key: str) -> PyramidManifest | None:
        """Read a sheet manifest (same layout as :func:`load_manifest`)."""
        return load_manifest(dir_root, sheet_key)


def build_pyramid(
    image: ImageArray,
    tile_size: int = tile_size,
    *,
    invert_white: bool = False,
    file_hash: str = "",
    page_index: int = 0,
    dpi: int | None = None,
) -> Pyramid:
    """Build the full tile pyramid of *image*.

    Levels run from level 0 (the whole sheet downscaled to fit one tile) up
    to level N (the original, never resampled), each a power-of-two step up
    in resolution. Every lower level is ``cv2.resize`` of the *source* with
    ``INTER_AREA`` (never an upscale, never a cascade of downscales). An
    image that already fits inside one tile gets level 0 only.

    ``file_hash``, ``page_index`` and ``dpi`` are render metadata recorded in
    the manifest for the cache key ``file_hash + page_index + dpi + level +
    x + y``; pass them when the renderer knows them (it normally does).
    """
    return Pyramid(
        image,
        tile_size=tile_size,
        invert_white=invert_white,
        file_hash=file_hash,
        page_index=page_index,
        dpi=dpi,
    )


def build_thumbnail(image: ImageArray, tile_size: int = tile_size) -> ImageArray:
    """Level-0-only downscale: the whole sheet fitted inside one tile.

    Fast enough for the register list: a single ``INTER_AREA`` resize, or the
    input itself (not a copy) when it already fits.
    """
    normalised = _normalise(image)
    if normalised.size == 0:
        raise ValueError("cannot thumbnail an empty image")
    height, width = normalised.shape[:2]
    top = _top_level(width, height, tile_size)
    thumb_w = _level_size(width, top)
    thumb_h = _level_size(height, top)
    if thumb_w == width and thumb_h == height:
        return normalised
    return cv2.resize(normalised, (thumb_w, thumb_h), interpolation=cv2.INTER_AREA)


# ── The on-disk tile cache ──────────────────────────────────────────────────


class TileCache:
    """An LRU-bounded on-disk cache of rendered PNG tiles.

    One instance owns one root directory (normally ``tiles_root(workspace)``)
    and stores tiles in exactly the layout :class:`Pyramid` uses, so tiles a
    pyramid pre-rendered with ``save_tile`` are adopted on the first hit and
    evicted like any others.

    Bookkeeping lives in ``root/lru.json``: every tile's touch time and byte
    size plus running hit/miss counters, all guarded by one
    ``threading.Lock``. Every hit and miss is persisted, so the LRU order and
    the counters survive a restart. If the file is missing or corrupt the
    cache rebuilds its index from the tiles on disk (ordered by file mtime).

    The key of one entry is the tile's path relative to the root, i.e. the
    full ``file_hash + page_index + dpi + level + x + y`` key encoded as
    directories and file name. The byte budget is enforced by
    :meth:`evict_if_over_budget` (called after every miss and available
    explicitly); ``budget_bytes`` is a plain attribute so an operator can
    change the budget of a live cache.
    """

    def __init__(
        self, root_dir: str | Path, budget_bytes: int = DEFAULT_CACHE_BUDGET_BYTES
    ) -> None:
        self.root = Path(root_dir)
        #: Current byte budget; may be adjusted after construction.
        self.budget_bytes = budget_bytes
        self._lock = threading.Lock()
        self._lru_path = self.root / LRU_FILE_NAME
        self._entries: dict[str, tuple[int, int]] = {}
        self._hits = 0
        self._misses = 0
        Path(long_path(self.root)).mkdir(parents=True, exist_ok=True)
        self._load_state()

    # -- state -----------------------------------------------------------

    def _load_state(self) -> None:
        raw = _read_bytes(self._lru_path)
        if raw is None:
            self._adopt_existing_tiles()
            return
        try:
            payload = json.loads(raw)
            self._entries = {
                rel: (int(meta["t"]), int(meta["s"])) for rel, meta in payload["entries"].items()
            }
            self._hits = int(payload["hits"])
            self._misses = int(payload["misses"])
        except (ValueError, KeyError, TypeError) as error:
            logger.warning(
                "Unreadable tile cache index {}; rebuilding from disk: {}", self._lru_path, error
            )
            self._entries = {}
            self._hits = 0
            self._misses = 0
            self._adopt_existing_tiles()

    def _adopt_existing_tiles(self) -> None:
        """Index tile PNGs already on disk (mtime order) when lru.json is absent."""
        base = Path(long_path(self.root, force=True))
        for child in base.rglob(f"{_TILE_PREFIX}*{_PNG_EXTENSION}"):
            try:
                stat = child.stat()
            except OSError:
                continue
            if not stat.st_size:
                continue
            rel = child.relative_to(base).as_posix()
            self._entries.setdefault(rel, (stat.st_mtime_ns, stat.st_size))

    def _persist(self) -> None:
        """Write lru.json. Callers hold ``self._lock``."""
        payload = {
            "version": LRU_FORMAT_VERSION,
            "hits": self._hits,
            "misses": self._misses,
            "entries": {
                rel: {"t": touched, "s": size} for rel, (touched, size) in self._entries.items()
            },
        }
        _atomic_write_bytes(self._lru_path, json.dumps(payload).encode("utf-8"))

    # -- the read/write path ----------------------------------------------

    def get_or_render(
        self,
        sheet_key: str,
        level: int,
        x: int,
        y: int,
        render_fn: Callable[[], ImageArray],
    ) -> ImageArray:
        """Serve tile (*level*, *x*, *y*) of *sheet_key* from cache or render it.

        On a file hit the PNG is decoded and returned without calling
        *render_fn*, and the entry is touched. On a miss *render_fn* is
        called (it must return the tile image as uint8 grayscale or BGR), the
        PNG is written atomically, counters are updated and persisted, and
        the cache is brought back under budget. Errors raised by *render_fn*
        propagate and change nothing.
        """
        rel = tile_rel(sheet_key, level, x, y)
        target = self.root / rel
        data = _read_bytes(target)
        if data is not None:
            tile = _decode_png(data)
            if tile is not None:
                with self._lock:
                    self._hits += 1
                    known = self._entries.get(rel)
                    size = known[1] if known is not None else len(data)
                    self._entries[rel] = (time.time_ns(), size)
                    self._persist()
                return tile
            logger.warning("Corrupt cached tile {}; re-rendering", rel)

        tile = _normalise(render_fn())
        png = _encode_png(tile)
        _atomic_write_bytes(target, png)
        with self._lock:
            self._misses += 1
            self._entries[rel] = (time.time_ns(), len(png))
            self._persist()
        self.evict_if_over_budget()
        return tile

    # -- eviction and stats ------------------------------------------------

    def evict_if_over_budget(self) -> int:
        """Delete the least recently used tiles until size is within budget.

        Returns how many tiles were deleted. Entries are removed oldest
        first; orphaned empty directories are pruned up to the sheet folder.
        """
        removed = 0
        with self._lock:
            total = sum(size for _, size in self._entries.values())
            if total <= self.budget_bytes:
                return 0
            oldest_first = sorted(self._entries.items(), key=lambda item: item[1][0])
            for rel, (_, size) in oldest_first:
                if total <= self.budget_bytes:
                    break
                self._unlink_tile(rel)
                self._entries.pop(rel, None)
                total -= size
                removed += 1
            if removed:
                self._persist()
        if removed:
            logger.debug("Tile cache evicted {} tiles to stay within budget", removed)
        return removed

    def _unlink_tile(self, rel: str) -> None:
        file = Path(long_path(self.root / rel))
        try:
            file.unlink()
        except OSError:
            return
        # Prune now-empty shard/level directories up to (but not including)
        # the sheet directory, where manifest.json lives.
        current = file.parent
        sheet = Path(long_path(sheet_dir(self.root, rel.split("/", 1)[0])))
        while current != sheet and current != current.parent:
            try:
                current.rmdir()
            except OSError:
                return
            current = current.parent

    def stats(self) -> dict[str, Any]:
        """Current cache state: size, tile count and hit/miss counters."""
        with self._lock:
            total = sum(size for _, size in self._entries.values())
            total_ops = self._hits + self._misses
            hit_rate = self._hits / total_ops if total_ops else 0.0
            return {
                "size_bytes": total,
                "tile_count": len(self._entries),
                "hit_count": self._hits,
                "miss_count": self._misses,
                "hit_rate": hit_rate,
                "budget_bytes": self.budget_bytes,
            }
