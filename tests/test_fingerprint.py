"""Fingerprint tests: same drawing above 0.85, different drawings below 0.4.

Uses real synthetic PDFs (tests.fixture_builder) so the whole pipeline —
text extraction, title-block exclusion, path counting — is exercised, not just
the pure similarity math.
"""

from __future__ import annotations

from engine.naming import fingerprint as fp
from engine.naming.fingerprint import (
    SheetFingerprint,
    build_document_fingerprints,
    build_fingerprint,
    fingerprints_from_payload,
    page_fingerprints_payload,
)
from tests.fixture_builder import A1_HEIGHT, A1_WIDTH, SheetSpec, build_pdf


def _fingerprint_of(path, page: int = 0) -> SheetFingerprint:
    return build_document_fingerprints(str(path))[page]


def test_same_sheet_scores_1(tmp_path):
    path = build_pdf(
        tmp_path / "A-101.pdf", [SheetSpec(drawing_no="A-101", title="GROUND FLOOR PLAN")]
    )
    one = _fingerprint_of(path)
    assert fp.similarity(one, one) == 1.0


def test_same_drawing_different_naming_standard_scores_above_85(tmp_path):
    """03_renamed scenario: identical content, different number on each side."""
    spec = SheetSpec(
        drawing_no="UVU-ARC-001",
        title="GROUND FLOOR PLAN",
        body=[
            "TYPICAL ROOM LAYOUT",
            "WALL TYPE W1 200MM BLOCKWORK",
            "CEILING LEVEL 3000 FINISHED",
            "DOOR SCHEDULE D1 D2 D3",
        ],
    )
    old_pdf = build_pdf(tmp_path / "old" / "UVU-ARC-001_RevD.pdf", [spec], offset_origin=True)
    new_spec = SheetSpec(
        drawing_no="UVU-KEO-XX-03-DR-A-0001",
        title="GROUND FLOOR PLAN",
        body=spec.body,
    )
    new_pdf = build_pdf(
        tmp_path / "new" / "UVU-KEO-XX-03-DR-A-0001_D.pdf", [new_spec], offset_origin=True
    )
    score = fp.similarity(_fingerprint_of(old_pdf), _fingerprint_of(new_pdf))
    assert score >= 0.85, score


def test_two_different_drawings_score_below_40(tmp_path):
    bodies = {
        "A-101": ["TYPICAL ROOM LAYOUT", "WALL TYPE W1 BLOCKWORK", "CEILING 3000"],
        "A-102": ["ROOF DRAINAGE DETAIL", "FALL 1 IN 60", "OUTLET 2000"],
    }
    paths: list[str] = []
    for number, body in bodies.items():
        paths.append(
            str(
                build_pdf(
                    tmp_path / f"{number}.pdf",
                    [SheetSpec(drawing_no=number, title=f"{number} TITLE", body=body)],
                )
            )
        )
    score = fp.similarity(_fingerprint_of(paths[0]), _fingerprint_of(paths[1]))
    assert score < 0.4, score


def test_revision_change_keeps_similarity_high(tmp_path):
    base = [
        "TYPICAL ROOM LAYOUT PLAN",
        "WALL TYPE W1 200MM BLOCKWORK",
        "CEILING LEVEL 3000 FINISHED FLOOR",
        "DOOR SCHEDULE D1 D2 D3",
    ]
    revised = list(base)
    revised[2] = "CEILING LEVEL 3100 FINISHED FLOOR"
    old = build_pdf(
        tmp_path / "RevC.pdf",
        [SheetSpec(drawing_no="A-101", revision="C", body=base)],
    )
    new = build_pdf(
        tmp_path / "RevD.pdf",
        [SheetSpec(drawing_no="A-101", revision="D", body=revised)],
    )
    assert fp.similarity(_fingerprint_of(old), _fingerprint_of(new)) >= 0.85


def test_title_block_noise_is_excluded(tmp_path):
    """Only the drawing number/rev differ -> tokens must be identical."""
    one = build_pdf(
        tmp_path / "one.pdf",
        [SheetSpec(drawing_no="A-101", revision="C", body=["ROOM PLAN GRID A B"])],
    )
    two = build_pdf(
        tmp_path / "two.pdf",
        [SheetSpec(drawing_no="A-999", revision="Z", body=["ROOM PLAN GRID A B"])],
    )
    left, right = _fingerprint_of(one), _fingerprint_of(two)
    assert left.text_tokens == right.text_tokens
    assert fp.similarity(left, right) == 1.0


def test_scanned_sheets_are_not_similar_to_each_other(tmp_path):
    from tests.fixture_builder import build_scanned_pdf

    build_scanned_pdf(tmp_path / "scan-a.pdf")
    build_scanned_pdf(tmp_path / "scan-b.pdf")
    a = _fingerprint_of(tmp_path / "scan-a.pdf")
    b = _fingerprint_of(tmp_path / "scan-b.pdf")
    assert not a.has_text_signal()
    assert fp.similarity(a, b) == 0.0


def test_grid_label_margins_are_collected():
    page = _make_page_with_margin_labels()
    fingerprint = build_fingerprint(page, path_count=3)
    assert set(fingerprint.grid_labels) >= {"A", "1"}


def test_structural_difference_pulls_score_down(tmp_path):
    """Same text but very different size/orientation cannot look identical."""
    body = ["SAME TEXT ON BOTH SHEETS"]
    a = build_pdf(
        tmp_path / "a1.pdf",
        [SheetSpec(width=A1_WIDTH, height=A1_HEIGHT, body=body)],
    )
    b = build_pdf(
        tmp_path / "a3.pdf",
        [SheetSpec(width=1191.0, height=842.0, body=body)],
    )
    assert fp.similarity(_fingerprint_of(a), _fingerprint_of(b)) < 1.0


def test_payload_round_trips():
    fingerprint = SheetFingerprint(
        page_size_mm=(840.0, 595.0),
        orientation="landscape",
        text_tokens=frozenset({"room", "plan", "wall"}),
        path_bucket=1,
        grid_labels=("A", "1"),
        text_hash="abc",
    )
    payload = page_fingerprints_payload({0: fingerprint, 2: fingerprint})
    restored = fingerprints_from_payload(payload)
    assert 0 in restored and 2 in restored
    assert restored[0] == fingerprint


def test_path_buckets():
    assert fp.path_bucket(0) == 0
    assert fp.path_bucket(99) == 0
    assert fp.path_bucket(100) == 1
    assert fp.path_bucket(500) == 2
    assert fp.path_bucket(2000) == 3
    assert fp.path_bucket(50000) == 3


def _make_page_with_margin_labels():
    """Build a PageText whose items sit in the margins, like grid bubbles."""
    from engine.extract.text_extractor import PageBox, PageText, TextItem

    box = PageBox(x0=0.0, y0=0.0, x1=1000.0, y1=700.0)
    items = [
        TextItem("A", 10.0, 10.0, 20.0, 20.0, 12.0, 0.01, 0.01),
        TextItem("1", 970.0, 660.0, 20.0, 20.0, 12.0, 0.97, 0.97),
        TextItem("NOT A LABEL", 200.0, 200.0, 200.0, 20.0, 12.0, 0.2, 0.2),
    ]
    return PageText(page_index=0, box=box, items=items)
