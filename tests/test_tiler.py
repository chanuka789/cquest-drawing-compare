"""Phase 4, task 4.2: tile pyramid geometry, lossless round trips, and the cache."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import numpy as np

from engine.extract.tiler import (
    TILE_DIR_NAME,
    LevelInfo,
    PyramidManifest,
    TileCache,
    build_pyramid,
    build_thumbnail,
    load_manifest,
    load_tile,
    tile_path,
    tile_rel,
    tiles_root,
)

# ── helpers ────────────────────────────────────────────────────────────────


def _pattern_image(width: int, height: int, seed: int = 7) -> np.ndarray:
    """A deterministic grayscale image with unique corner markers."""
    rng = np.random.default_rng(seed)
    image = rng.integers(0, 256, (height, width), dtype=np.uint8)
    image[0:8, 0:8] = 1
    image[0:8, -8:] = 2
    image[-8:, 0:8] = 3
    image[-8:, -8:] = 4
    return image


def _pattern_colour(width: int, height: int, seed: int = 11) -> np.ndarray:
    """A deterministic BGR (OpenCV convention) image with corner markers."""
    rng = np.random.default_rng(seed)
    image = rng.integers(0, 256, (height, width, 3), dtype=np.uint8)
    image[0:8, 0:8, :] = (1, 2, 3)
    image[0:8, -8:, :] = (4, 5, 6)
    image[-8:, 0:8, :] = (7, 8, 9)
    image[-8:, -8:, :] = (10, 11, 12)
    return image


def _pattern_tile(seed: int, size: int = 512) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, (size, size), dtype=np.uint8)


def _tile_files(root: Path) -> int:
    return len(list(root.rglob("tile_*.png")))


# ── pyramid geometry ───────────────────────────────────────────────────────


def test_level_geometry_follows_the_map_server_scheme():
    image = _pattern_image(2000, 1500)
    pyramid = build_pyramid(image)

    expected_levels = math.ceil(math.log2(2000 / 512)) + 1
    assert expected_levels == 3
    assert [spec.level for spec in pyramid.levels] == [0, 1, 2]
    assert pyramid.top_level == 2

    # Level 0 is the whole sheet inside one tile; the top level is the source.
    level_0 = pyramid.crop_tile(0, 0, 0)
    assert level_0.shape[0] <= 512 and level_0.shape[1] <= 512
    assert level_0.shape == (375, 500)
    assert pyramid.levels[0].cols == 1 and pyramid.levels[0].rows == 1
    assert pyramid.crop_tile(2, 0, 0).shape == (512, 512)

    # Edge tiles are partial, so re-composition never needs padding.
    assert pyramid.crop_tile(2, 3, 2).shape == (1500 - 2 * 512, 2000 - 3 * 512)

    # Scale grows by powers of two up to the original.
    assert pyramid.levels[0].scale == 0.25
    assert pyramid.levels[1].scale == 0.5
    assert pyramid.levels[2].scale == 1.0

    # Tile counts: 4x3, 2x2, 1x1 -> 12 + 4 + 1.
    assert [(spec.cols, spec.rows) for spec in pyramid.levels] == [(1, 1), (2, 2), (4, 3)]
    assert pyramid.build_manifest().levels == [
        LevelInfo(0, 1, 1, 1),
        LevelInfo(1, 2, 2, 4),
        LevelInfo(2, 4, 3, 12),
    ]


def test_an_image_that_fits_gets_only_level_0():
    pyramid = build_pyramid(_pattern_image(400, 300))
    assert pyramid.top_level == 0
    assert len(pyramid.levels) == 1
    assert pyramid.levels[0].cols == 1 and pyramid.levels[0].rows == 1
    assert np.array_equal(pyramid.crop_tile(0, 0, 0), _pattern_image(400, 300))


def test_out_of_range_tiles_are_rejected():
    pyramid = build_pyramid(_pattern_image(2000, 1500))
    for bad_call in [
        lambda: pyramid.crop_tile(9, 0, 0),
        lambda: pyramid.crop_tile(0, 1, 0),
        lambda: pyramid.crop_tile(2, 4, 2),
        lambda: pyramid.crop_tile(2, -1, 0),
    ]:
        try:
            bad_call()
        except ValueError:
            continue
        raise AssertionError("expected ValueError")


# ── lossless recomposition and round trips ─────────────────────────────────


def test_recomposing_the_full_level_is_bitwise_identical_grayscale():
    image = _pattern_image(2000, 1500)
    pyramid = build_pyramid(image)

    spec = pyramid.levels[pyramid.top_level]
    canvas = np.zeros(image.shape, dtype=np.uint8)
    for y in range(spec.rows):
        for x in range(spec.cols):
            tile = pyramid.crop_tile(pyramid.top_level, x, y)
            canvas[y * 512 : y * 512 + tile.shape[0], x * 512 : x * 512 + tile.shape[1]] = tile

    assert np.array_equal(canvas, image)


def test_recomposing_the_full_level_is_bitwise_identical_colour():
    image = _pattern_colour(900, 700)
    pyramid = build_pyramid(image)

    spec = pyramid.levels[pyramid.top_level]
    canvas = np.zeros(image.shape, dtype=np.uint8)
    for y in range(spec.rows):
        for x in range(spec.cols):
            tile = pyramid.crop_tile(pyramid.top_level, x, y)
            canvas[y * 512 : y * 512 + tile.shape[0], x * 512 : x * 512 + tile.shape[1]] = tile

    assert np.array_equal(canvas, image)


def test_invert_white_flips_the_polarity():
    image = _pattern_image(300, 200)
    pyramid = build_pyramid(image, invert_white=True)
    assert np.array_equal(pyramid.crop_tile(0, 0, 0), 255 - image)
    # The caller's array is never mutated by inversion.
    assert np.array_equal(_pattern_image(300, 200), image)


def test_thumbnail_fits_one_tile_and_keeps_aspect():
    image = _pattern_image(2000, 1500)
    thumb = build_thumbnail(image)
    assert thumb.ndim == 2
    assert max(thumb.shape) <= 512
    assert thumb.shape == (375, 500)
    assert abs(thumb.shape[1] / thumb.shape[0] - 2000 / 1500) < 1e-9
    # The thumbnail is exactly level 0 of the pyramid.
    assert np.array_equal(thumb, build_pyramid(image).crop_tile(0, 0, 0))


def test_thumbnail_passes_small_images_through():
    image = _pattern_image(300, 200)
    thumb = build_thumbnail(image)
    assert thumb.shape == (200, 300)
    assert np.array_equal(thumb, image)


def test_save_load_round_trip_is_lossless(tmp_path: Path):
    root = tmp_path / "tiles"
    sheet_key = "abc123:3:200"
    image = _pattern_colour(900, 700, seed=5)
    pyramid = build_pyramid(image, file_hash="abc123", page_index=3, dpi=200)

    for spec in pyramid.levels:
        for y in range(spec.rows):
            for x in range(spec.cols):
                saved = pyramid.save_tile(root, sheet_key, spec.level, x, y)
                assert saved == tile_path(root, sheet_key, spec.level, x, y)
                assert saved.is_file()
                loaded = pyramid.load_tile(root, sheet_key, spec.level, x, y)
                assert loaded is not None
                assert np.array_equal(loaded, pyramid.crop_tile(spec.level, x, y))
                assert np.array_equal(loaded, load_tile(root, sheet_key, spec.level, x, y))

    # A tile that was never written reads back as None, not an error.
    assert pyramid.load_tile(root, sheet_key, 1, 7, 7) is None

    # The manifest round trips through JSON byte-for-byte in its fields.
    manifest_path = pyramid.save_manifest(root, sheet_key)
    assert manifest_path.is_file()
    manifest = pyramid.load_manifest(root, sheet_key)
    assert manifest is not None
    assert load_manifest(root, sheet_key) == manifest
    assert manifest.file_hash == "abc123"
    assert manifest.page_index == 3
    assert manifest.dpi == 200
    assert (manifest.width_px, manifest.height_px) == (900, 700)
    assert manifest.tile_size == 512
    assert [level.tile_count for level in manifest.levels] == [1, 4]

    via_json = PyramidManifest.from_dict(json.loads(json.dumps(manifest.as_dict())))
    assert via_json.as_dict() == manifest.as_dict()


# ── sharding ───────────────────────────────────────────────────────────────


def test_shard_layout_is_deterministic():
    root = Path("root")
    assert tile_rel("k", 0, 0, 0) == "k/0/00/00/tile_0_0.png"
    assert tile_path(root, "k", 3, 255, 20) == root / "k/3/00/00/tile_255_20.png"
    assert tile_path(root, "k", 3, 256, 20) == root / "k/3/01/00/tile_256_20.png"
    assert tile_path(root, "k", 3, 511, 20) == root / "k/3/01/00/tile_511_20.png"
    assert tile_path(root, "k", 3, 512, 257) == root / "k/3/02/01/tile_512_257.png"


def test_no_directory_exceeds_a_thousand_files_for_a_3000px_sheet(tmp_path: Path):
    image = _pattern_image(3000, 3000, seed=3)
    pyramid = build_pyramid(image)
    assert pyramid.top_level == 3
    assert [spec.cols * spec.rows for spec in pyramid.levels] == [1, 4, 9, 36]

    root = tmp_path / "pyramid"
    for spec in pyramid.levels:
        for y in range(spec.rows):
            for x in range(spec.cols):
                pyramid.save_tile(root, "big", spec.level, x, y)

    # Expected: 36 + 9 + 4 + 1 tiles and one manifest.
    assert _tile_files(root) == 50
    biggest = max(len(files) for _, _, files in os.walk(root))
    assert biggest <= 1000


# ── TileCache ──────────────────────────────────────────────────────────────


def test_cache_hits_do_not_rerender_and_stats_are_correct(tmp_path: Path):
    cache = TileCache(tmp_path / "cache", budget_bytes=256 * 1024 * 1024)
    calls: list[int] = []

    def render() -> np.ndarray:
        calls.append(1)
        return _pattern_tile(42)

    first = cache.get_or_render("sheet:0:200", 2, 0, 0, render)
    second = cache.get_or_render("sheet:0:200", 2, 0, 0, render)

    assert len(calls) == 1
    assert np.array_equal(first, second)

    stats = cache.stats()
    assert stats["hit_count"] == 1
    assert stats["miss_count"] == 1
    assert stats["hit_rate"] == 0.5
    assert stats["tile_count"] == 1
    path = tile_path(cache.root, "sheet:0:200", 2, 0, 0)
    assert path.is_file()
    assert stats["size_bytes"] == path.stat().st_size

    # A second request pair keeps the rate at 2 hits / 4 requests = 0.5.
    cache.get_or_render("sheet:0:200", 2, 0, 1, lambda: _pattern_tile(43))
    cache.get_or_render("sheet:0:200", 2, 0, 1, lambda: _pattern_tile(43))
    stats = cache.stats()
    assert stats["hit_count"] == 2
    assert stats["miss_count"] == 2
    assert stats["hit_rate"] == 0.5


def test_lru_state_and_counters_survive_a_new_instance(tmp_path: Path):
    root = tmp_path / "cache"
    cache = TileCache(root, budget_bytes=256 * 1024 * 1024)
    cache.get_or_render("s1", 1, 0, 0, lambda: _pattern_tile(1))

    reopened = TileCache(root, budget_bytes=256 * 1024 * 1024)
    calls: list[int] = []

    def render() -> np.ndarray:
        calls.append(1)
        return _pattern_tile(99)

    tile = reopened.get_or_render("s1", 1, 0, 0, render)
    assert calls == []
    assert np.array_equal(tile, _pattern_tile(1))
    stats = reopened.stats()
    assert stats["tile_count"] == 1
    assert stats["hit_count"] == 1
    # The original write's miss counter was persisted, and the reopen added no
    # second miss.
    assert stats["miss_count"] == 1


def test_cache_rebuilds_its_index_from_a_corrupt_lru_file(tmp_path: Path):
    root = tmp_path / "cache"
    TileCache(root, budget_bytes=1 << 30).get_or_render("s1", 1, 0, 0, lambda: _pattern_tile(5))
    (root / "lru.json").write_text("{not json", encoding="utf-8")

    reopened = TileCache(root, budget_bytes=1 << 30)
    calls: list[int] = []

    def render() -> np.ndarray:
        calls.append(1)
        return _pattern_tile(99)

    tile = reopened.get_or_render("s1", 1, 0, 0, render)
    assert calls == []
    assert np.array_equal(tile, _pattern_tile(5))
    assert reopened.stats()["tile_count"] == 1


def test_eviction_removes_the_oldest_and_stays_within_budget(tmp_path: Path):
    root = tmp_path / "cache"
    cache = TileCache(root, budget_bytes=1 << 40)
    keys = [(0, 0), (0, 1), (1, 0)]
    for seed, (x, y) in enumerate(keys):
        cache.get_or_render("sheet:0:200", 3, x, y, lambda s=seed: _pattern_tile(100 + s))

    assert cache.stats()["tile_count"] == 3
    newest_size = tile_path(root, "sheet:0:200", 3, 1, 0).stat().st_size

    cache.budget_bytes = newest_size
    removed = cache.evict_if_over_budget()

    assert removed == 2
    assert not tile_path(root, "sheet:0:200", 3, 0, 0).exists()
    assert not tile_path(root, "sheet:0:200", 3, 0, 1).exists()
    assert tile_path(root, "sheet:0:200", 3, 1, 0).exists()
    stats = cache.stats()
    assert stats["tile_count"] == 1
    assert stats["size_bytes"] <= cache.budget_bytes

    # The survivor still serves without re-rendering.
    calls: list[int] = []

    def render() -> np.ndarray:
        calls.append(1)
        return _pattern_tile(0)

    survivor = cache.get_or_render("sheet:0:200", 3, 1, 0, render)
    assert calls == []
    assert np.array_equal(survivor, _pattern_tile(102))
    assert cache.stats()["hit_count"] == 1


def test_eviction_triggers_after_misses_and_frees_disk(tmp_path: Path):
    root = tmp_path / "cache"
    cache = TileCache(root, budget_bytes=1 << 40)
    # Identical content per tile so every PNG has the same byte size and the
    # arithmetic below is exact.
    for x in range(4):
        cache.get_or_render("s", 1, x, 0, lambda: _pattern_tile(7))
    assert cache.stats()["tile_count"] == 4

    # Budget of two tile sizes: writing a fifth must evict the three oldest.
    one_tile = tile_path(root, "s", 1, 0, 0).stat().st_size
    cache.budget_bytes = 2 * one_tile
    cache.get_or_render("s", 1, 9, 0, lambda: _pattern_tile(7))

    stats = cache.stats()
    assert stats["tile_count"] == 2
    assert stats["size_bytes"] <= cache.budget_bytes
    assert not tile_path(root, "s", 1, 0, 0).exists()
    assert tile_path(root, "s", 1, 9, 0).exists()


def test_sheets_do_not_collide_under_one_root(tmp_path: Path):
    cache = TileCache(tmp_path / "cache", budget_bytes=1 << 40)
    key_a = "aaaa1111:0:200"
    key_b = "bbbb2222:1:200"

    cache.get_or_render(key_a, 2, 1, 1, lambda: _pattern_tile(201))
    cache.get_or_render(key_b, 2, 1, 1, lambda: _pattern_tile(202))

    assert cache.stats()["tile_count"] == 2
    path_a = tile_path(cache.root, key_a, 2, 1, 1)
    path_b = tile_path(cache.root, key_b, 2, 1, 1)
    assert path_a.is_file() and path_b.is_file()
    assert np.array_equal(load_tile(cache.root, key_a, 2, 1, 1), _pattern_tile(201))
    assert np.array_equal(load_tile(cache.root, key_b, 2, 1, 1), _pattern_tile(202))
    # The ':' in the conventional key never reaches the filesystem: the sheet
    # directory is the sanitised form, and no raw-key directory exists.
    assert (cache.root / "aaaa1111_0_200").is_dir()
    assert (cache.root / "bbbb2222_1_200").is_dir()
    assert not (cache.root / key_a).exists()
    assert not (cache.root / key_b).exists()


def test_cache_adopts_pyramid_written_tiles(tmp_path: Path):
    root = tmp_path / "cache"
    cache = TileCache(root, budget_bytes=1 << 40)
    image = _pattern_image(900, 700, seed=9)
    pyramid = build_pyramid(image, file_hash="f00d", page_index=0, dpi=200)
    pyramid.save_tile(root, "f00d:0:200", 1, 1, 1)
    assert cache.stats()["tile_count"] == 0  # not yet seen by the cache

    calls: list[int] = []

    def render() -> np.ndarray:
        calls.append(1)
        return _pattern_tile(0)

    tile = cache.get_or_render("f00d:0:200", 1, 1, 1, render)
    assert calls == []
    assert np.array_equal(tile, pyramid.crop_tile(1, 1, 1))
    assert cache.stats()["tile_count"] == 1


def test_tiles_root_maps_to_the_workspace_audit_dir(tmp_path: Path):
    from engine.core.workspace import create_workspace

    assert TILE_DIR_NAME == "tiles"
    workspace = create_workspace(tmp_path / "ws")
    assert tiles_root(workspace) == workspace.audit_dir / "tiles"
    assert tiles_root(workspace).parent == workspace.audit_dir

    cache = TileCache(tiles_root(workspace), budget_bytes=1 << 20)
    cache.get_or_render("s:0:200", 0, 0, 0, lambda: np.full((64, 64), 200, dtype=np.uint8))
    assert tile_path(workspace.audit_dir / "tiles", "s:0:200", 0, 0, 0).is_file()
