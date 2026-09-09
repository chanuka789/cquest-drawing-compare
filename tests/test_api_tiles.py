"""Phase 4, task 4.3: tile endpoints and the pre-render queue over HTTP.

Every test seeds a fresh :class:`ComparisonSession` with A3 drawing pages
(write_drawing_pdf renders fast enough at 200 DPI) and its own workspace, so
tiles land in ``<workspace>/_audit/tiles`` exactly like they do in the real
app, and no test depends on another's session state.
"""

from __future__ import annotations

import hashlib
import math
import time
from pathlib import Path

import cv2
import numpy as np

from engine.core.models import SheetRecord
from engine.core.session import ComparisonSession, get_session, reset_session
from engine.extract.tiler import tile_path, tiles_root
from tests.fixture_builder import A3_HEIGHT, A3_WIDTH, write_drawing_pdf


def _sheet_id(abs_path: str | Path, page_index: int = 0) -> str:
    key = f"{abs_path}|{page_index}".encode()
    return hashlib.sha1(key).hexdigest()[:16]


def _expected_px() -> tuple[int, int]:
    """The rendered A3 page size at the comparison DPI (TILE_DPI = 200)."""
    scale = 200.0 / 72.0
    return math.ceil(A3_WIDTH * scale), math.ceil(A3_HEIGHT * scale)


def _expected_top_level(width_px: int, height_px: int) -> int:
    return math.ceil(math.log2(max(width_px, height_px) / 512))


def _seed(tmp_path: Path, drawings: int = 1) -> ComparisonSession:
    """A fresh session: *drawings* A3 sheets on the old side and a workspace."""
    reset_session()
    session = get_session()
    for index in range(drawings):
        number = f"A-{101 + index}"
        write_drawing_pdf(
            tmp_path / "old" / f"{number}-RevC.pdf",
            drawing_no=number,
            revision="C",
            width=A3_WIDTH,
            height=A3_HEIGHT,
        )
    session.old.sheets = [
        SheetRecord(
            side="old",
            abs_path=str(path),
            filename=path.name,
            page_index=0,
            page_count=1,
            drawing_no=None,
            is_readable=True,
        )
        for path in sorted((tmp_path / "old").glob("*.pdf"))
    ]
    session.set_output_folder(str(tmp_path / "ws"))
    return session


def _decode_png(data: bytes) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image is not None, "response body is not a decodable PNG"
    return image


def test_manifest_reports_pyramid_geometry_and_rejects_unknown_sheets(client, tmp_path):
    session = _seed(tmp_path)
    path = session.old.sheets[0].abs_path
    sheet_id = _sheet_id(path)

    response = client.get(f"/api/tiles/{sheet_id}/manifest")
    assert response.status_code == 200
    manifest = response.json()

    width_px, height_px = _expected_px()
    assert manifest["sheet_id"] == sheet_id
    assert manifest["filename"] == Path(path).name
    assert manifest["dpi"] == 200
    assert manifest["tile_size"] == 512
    assert (manifest["width_px"], manifest["height_px"]) == (width_px, height_px)

    levels = manifest["levels"]
    assert len(levels) == _expected_top_level(width_px, height_px) + 1
    assert levels[0] == {"level": 0, "cols": 1, "rows": 1, "tile_count": 1}
    top = levels[-1]
    assert top["level"] == _expected_top_level(width_px, height_px)
    assert top["cols"] == math.ceil(width_px / 512)
    assert top["rows"] == math.ceil(height_px / 512)
    assert top["tile_count"] == top["cols"] * top["rows"]

    # A repeated request is served from the manifest already on disk.
    again = client.get(f"/api/tiles/{sheet_id}/manifest")
    assert again.status_code == 200
    assert again.json() == manifest

    # Unknown sheet ids are a structured 404, not an image or an HTML page.
    missing = client.get(f"/api/tiles/{'0' * 16}/manifest")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


def test_tiles_are_png_served_from_disk_and_bounds_checked(client, tmp_path):
    session = _seed(tmp_path)
    sheet_id = _sheet_id(session.old.sheets[0].abs_path)
    width_px, height_px = _expected_px()
    top = _expected_top_level(width_px, height_px)

    thumbnail = client.get(f"/api/tiles/{sheet_id}/thumbnail.png")
    assert thumbnail.status_code == 200
    assert thumbnail.headers["content-type"] == "image/png"
    thumb = _decode_png(thumbnail.content)
    assert max(thumb.shape[:2]) <= 512
    aspect = width_px / height_px
    assert abs(thumb.shape[1] / thumb.shape[0] - aspect) / aspect < 0.02

    # An interior tile of the full-resolution level is a full 512x512 PNG,
    # long-cached, and byte-identical on a second (pure file read) request.
    tile_url = f"/api/tiles/{sheet_id}/{top}/1/1.png"
    first = client.get(tile_url)
    assert first.status_code == 200
    assert first.headers["content-type"] == "image/png"
    assert "immutable" in first.headers["cache-control"]
    assert _decode_png(first.content).shape == (512, 512, 3)

    second = client.get(tile_url)
    assert second.status_code == 200
    assert second.content == first.content

    # The level-0 tile and thumbnail.png are the same file and the same bytes.
    level_zero = client.get(f"/api/tiles/{sheet_id}/0/0/0.png")
    assert level_zero.status_code == 200
    assert level_zero.content == thumbnail.content

    # Tiles outside the manifest grid are a structured 404.
    outside = client.get(f"/api/tiles/{sheet_id}/{top}/9/1.png")
    assert outside.status_code == 404
    assert outside.json()["error"]["code"] == "not_found"

    beyond = client.get(f"/api/tiles/{sheet_id}/99/0/0.png")
    assert beyond.status_code == 404
    assert beyond.json()["error"]["code"] == "not_found"


def test_render_queue_pre_renders_and_dedupes(client, tmp_path):
    session = _seed(tmp_path, drawings=2)
    sheet_ids = [_sheet_id(sheet.abs_path) for sheet in session.old.sheets]

    queued = client.post("/api/render/queue", json={"sheet_ids": sheet_ids})
    assert queued.status_code == 200
    assert queued.json() == {"queued": 2}

    # Duplicates within one request, and sheets already pending or in flight,
    # are never queued twice.
    again = client.post("/api/render/queue", json={"sheet_ids": [*sheet_ids, sheet_ids[0]]})
    assert again.status_code == 200
    assert again.json() == {"queued": 0}

    deadline = time.monotonic() + 30.0
    status = {}
    while time.monotonic() < deadline:
        status = client.get("/api/render/status").json()
        if status["done"] >= 2 and not status["running"]:
            break
        time.sleep(0.05)

    assert status["queued"] == 0
    assert status["done"] == 2
    assert status["failed"] == 0
    assert status["running"] is False
    assert status["last_message"]

    # The whole pyramid of both sheets is on disk, manifest included.
    root = tiles_root(session.workspace)
    width_px, height_px = _expected_px()
    top = _expected_top_level(width_px, height_px)
    for sheet_id in sheet_ids:
        assert (root / sheet_id / "manifest.json").is_file()
        assert tile_path(root, sheet_id, top, 1, 1).is_file()

    # A bad priority or an unknown sheet id is refused up front.
    bad = client.post("/api/render/queue", json={"sheet_ids": sheet_ids, "priority": "urgent"})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "validation_error"

    unknown = client.post("/api/render/queue", json={"sheet_ids": [sheet_ids[0], "0" * 16]})
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "validation_error"


def test_tiles_require_an_output_folder_first(client, tmp_path):
    reset_session()
    session = get_session()
    path = write_drawing_pdf(tmp_path / "old" / "A-101-RevC.pdf", width=A3_WIDTH, height=A3_HEIGHT)
    session.old.sheets = [
        SheetRecord(abs_path=str(path), filename=path.name, page_index=0, is_readable=True)
    ]

    response = client.get(f"/api/tiles/{_sheet_id(path)}/manifest")
    assert response.status_code == 422
    assert "output folder" in response.json()["error"]["message"]

    status = client.get("/api/render/status").json()
    assert status == {
        "queued": 0,
        "done": 0,
        "failed": 0,
        "running": False,
        "last_message": "",
    }
