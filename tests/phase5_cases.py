"""The Phase 5 comparison matrix: one source sheet and every case built on it.

Defined in one place because two things need it and they must agree: the
fixture builder that writes the folders under ``_cqdc-fixtures``, and the test
suite that measures precision and recall. A case that exists only in the test
run is a case nobody can open and look at.

The cases follow the plan's fixture list. Half of them are **cosmetic-only**
and must report zero changes — that is the gate for the whole phase, and
``rev_letter_only`` is the most important fixture in the project.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tests.fixture_builder import A1_HEIGHT, A1_WIDTH, write_drawing_pdf
from tests.harness.inject import (
    AddLayer,
    AddObject,
    AddWatermark,
    ChangeDimension,
    ChangeHatch,
    ChangeLineType,
    ChangeLineWeight,
    ChangeRevisionLetter,
    ChangeSpec,
    ChangeText,
    Colourise,
    DeleteObject,
    MoveObject,
    PlotMono,
    RenumberTags,
    SubstituteFont,
    ToggleLayer,
)

#: The page box of the fixture sheets is centred on the origin, like every
#: real drawing whose media box is not at (0, 0) — the Lami Architects sheet
#: runs from (-1192, -842). Case regions are therefore in page coordinates.
PAGE_X0 = -A1_WIDTH / 2
PAGE_Y0 = -A1_HEIGHT / 2

#: The labels the source sheet carries. Chosen to exercise every text
#: category: dimensions, a level, tags, a note, specification wording.
SOURCE_LABELS: tuple[str, ...] = (
    "RM-01",
    "RM-02",
    "3000",
    "2400",
    "D-12",
    "D-13",
    "D-14",
    "D-15",
    "W3",
    "FFL +3.600",
    "Provide 100mm blockwork throughout to all party walls",
    "1 HOUR FIRE RATED",
)

#: An empty part of the drawing, safe to add an object into.
EMPTY_REGION = (300.0, 300.0, 520.0, 450.0)
#: A region holding a handful of drawn objects and nothing else.
OBJECT_REGION = (-500.0, -300.0, -200.0, 0.0)
#: A wall-shaped strip, hatched finely enough to read as a texture. The
#: thickness is deliberate: 8.5 pt is 3 mm on paper, which at 1:100 is a
#: 300 mm wall. An earlier version used 50 pt, a wall 1.7 m thick, whose
#: hatch segments were longer than any real hatch is — and the detector
#: correctly refused to call them hatch.
WALL_REGION = (-900.0, -650.0, -300.0, -641.5)
#: Where the setting-out layer's content sits.
LAYER_REGION = (300.0, 300.0, 700.0, 700.0)

#: Hatch spacing in points: 2 mm on paper, which is what a wall hatch is.
WALL_HATCH_SPACING = 2.0 * 72.0 / 25.4


def write_source(path: str | Path) -> Path:
    """The sheet every case is built from: A-101, Rev C, 1:100."""
    return write_drawing_pdf(
        path,
        drawing_no="A-101",
        revision="C",
        scale="1 : 100",
        labels=list(SOURCE_LABELS),
    )


@dataclass(frozen=True, slots=True)
class Case:
    """One fixture case and what it is supposed to prove."""

    spec: ChangeSpec
    purpose: str

    @property
    def name(self) -> str:
        return self.spec.name


def _spec(name: str, **kwargs: object) -> ChangeSpec:
    return ChangeSpec(name=name, **kwargs)  # type: ignore[arg-type]


def cosmetic_cases() -> list[Case]:
    """Cases that must report **zero** changes. The phase gate lives here."""
    return [
        Case(
            _spec("rev_letter_only", new=[ChangeRevisionLetter("C", "D")]),
            "Identical sheets, only the revision letter differs. Zero changes.",
        ),
        Case(
            _spec("line_weight", new=[ChangeLineWeight(2.0)]),
            "The same drawing replotted with heavier pens.",
        ),
        Case(
            _spec("line_type", new=[ChangeLineType()]),
            "The same geometry, dashed instead of solid.",
        ),
        Case(
            _spec("colour_vs_mono", base=[Colourise()], new=[PlotMono()]),
            "A colour plot against a monochrome one of the same drawing.",
        ),
        Case(
            _spec("font_substituted", new=[SubstituteFont("Times-Roman")]),
            "The plotting machine did not have the drawing office's font.",
        ),
        Case(
            _spec(
                "layer_toggled",
                base=[AddLayer("SETTING OUT", LAYER_REGION)],
                new=[ToggleLayer("SETTING OUT")],
            ),
            "A layer switched off. One record naming the layer, not hundreds.",
        ),
        Case(
            _spec("watermarked", new=[AddWatermark("PRELIMINARY")]),
            "One issue stamped PRELIMINARY, the other not.",
        ),
        Case(
            _spec(
                "watermark_and_rev",
                new=[AddWatermark("NOT FOR CONSTRUCTION"), ChangeRevisionLetter("C", "E")],
            ),
            "Both of the usual cosmetic differences at once.",
        ),
    ]


def genuine_cases() -> list[Case]:
    """Cases with real changes, which must be found."""
    return [
        Case(
            _spec("dimension_only", new=[ChangeDimension("3000", "3200")]),
            "A dimension text changed while the line did not move.",
        ),
        Case(
            _spec("level_change", new=[ChangeText("FFL +3.600", "FFL +3.750")]),
            "A level raised by 150 mm.",
        ),
        Case(
            _spec("spec_change", new=[ChangeText("1 HOUR FIRE RATED", "2 HOUR FIRE RATED")]),
            "A partition specification changed: a different wall entirely.",
        ),
        Case(
            _spec(
                "renumbered",
                new=[
                    RenumberTags({"D-12": "D-22", "D-13": "D-23", "D-14": "D-24", "D-15": "D-25"})
                ],
            ),
            "Door marks renumbered, nothing else changed. One record.",
        ),
        Case(
            _spec("object_added", new=[AddObject(EMPTY_REGION, "rect")]),
            "A new object drawn in an empty part of the sheet.",
        ),
        Case(
            _spec("object_removed", new=[DeleteObject(OBJECT_REGION)]),
            "Objects erased from one area.",
        ),
        Case(
            _spec("object_moved", new=[MoveObject(OBJECT_REGION, 30.0, 0.0)]),
            "Objects shifted 30 mm on paper.",
        ),
        Case(
            _spec(
                "hatch_change",
                base=[ChangeHatch(WALL_REGION, 45.0, WALL_HATCH_SPACING)],
                new=[ChangeHatch(WALL_REGION, 90.0, WALL_HATCH_SPACING)],
            ),
            "A wall's hatch changed from blockwork to concrete. ONE record.",
        ),
        Case(
            _spec(
                "real_small",
                new=[
                    ChangeDimension("2400", "2600"),
                    AddObject(EMPTY_REGION, "circle"),
                    ChangeText("RM-02", "RM-07"),
                ],
            ),
            "Three genuine changes and nothing else.",
        ),
        Case(
            _spec(
                "real_heavy",
                new=[
                    ChangeDimension("3000", "3350"),
                    ChangeDimension("2400", "2100"),
                    ChangeText("FFL +3.600", "FFL +3.900"),
                    ChangeText("1 HOUR FIRE RATED", "2 HOUR FIRE RATED"),
                    ChangeText("RM-01", "RM-11"),
                    AddObject(EMPTY_REGION, "rect"),
                    AddObject((560.0, 300.0, 700.0, 430.0), "circle"),
                    DeleteObject(OBJECT_REGION),
                    MoveObject((-900.0, 200.0, -600.0, 500.0), 25.0, 10.0),
                ],
            ),
            "A heavily revised sheet that must still read as a list, not a mess.",
        ),
        Case(
            _spec(
                "change_under_watermark",
                new=[AddWatermark("PRELIMINARY"), ChangeDimension("3000", "3400")],
            ),
            "A real change on a stamped sheet: the stamp goes, the change stays.",
        ),
        Case(
            _spec(
                "change_with_replot",
                new=[ChangeLineWeight(1.8), ChangeDimension("2400", "2750")],
            ),
            "A real change on a sheet that was also replotted with heavier pens.",
        ),
    ]


def all_cases() -> list[Case]:
    """Every case, cosmetic first — that is the order they matter in."""
    return [*cosmetic_cases(), *genuine_cases()]


def case_by_name(name: str) -> Case:
    for case in all_cases():
        if case.name == name:
            return case
    raise KeyError(f"No Phase 5 case called {name!r}")
