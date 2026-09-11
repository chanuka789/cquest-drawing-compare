"""Task 5.4 (API) — sheet templates and the mask editor.

The flow this supports is the point of the whole masking design: detect the
zones **once per template**, show them to the user, let them adjust in ten
seconds, and apply the confirmed result to every sheet in the cluster. A
project of three hundred sheets normally has two or three templates, so the
user's whole involvement is two or three ten-second reviews.

Nothing here detects per sheet at request time beyond the representatives:
three hundred detections are three hundred chances to be wrong, and a
confirmed template is worth more than any amount of cleverness.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from loguru import logger
from pydantic import BaseModel, Field

from engine.core.session import ComparisonSession, get_session
from engine.masking.mask_engine import MaskSet, load_from_profile, save_to_profile
from engine.masking.template_detect import (
    SheetTemplate,
    apply_template,
    cluster_sheets,
    detect_template_zones,
)
from engine.masking.types import FracRect, ProtectedRegion, ProtectedType, SheetView, Zone, ZoneType
from engine.utils.errors import NotFoundError, ValidationError

router = APIRouter(prefix="/api", tags=["masking"])

#: Templates are detected at this resolution. Frame geometry does not need
#: the comparison DPI, and a low one keeps the review screen instant.
TEMPLATE_DPI = 100


class RectPayload(BaseModel):
    x0: float = Field(ge=-0.5, le=1.5)
    y0: float = Field(ge=-0.5, le=1.5)
    x1: float = Field(ge=-0.5, le=1.5)
    y1: float = Field(ge=-0.5, le=1.5)

    def to_rect(self) -> FracRect:
        return FracRect(self.x0, self.y0, self.x1, self.y1).clipped()


class ZonePayload(BaseModel):
    type: str = "user"
    rect: RectPayload
    polygon: list[tuple[float, float]] = Field(default_factory=list)
    label: str = ""
    enabled: bool = True
    ink_only: bool = False
    ink_threshold: int = 130
    match_text: str = ""

    def to_zone(self) -> Zone:
        try:
            zone_type = ZoneType(self.type)
        except ValueError as exc:
            allowed = ", ".join(str(value) for value in ZoneType)
            raise ValidationError(
                f"'{self.type}' is not a zone type. Use one of: {allowed}."
            ) from exc
        return Zone(
            type=zone_type,
            rect=self.rect.to_rect(),
            polygon=[(float(x), float(y)) for x, y in self.polygon],
            label=self.label,
            confidence=1.0,
            evidence=["Confirmed by the user in the mask editor."],
            enabled=self.enabled,
            user_edited=True,
            ink_only=self.ink_only,
            ink_threshold=self.ink_threshold,
            match_text=self.match_text,
        )


class ProtectedPayload(BaseModel):
    type: str = "user_protected"
    rect: RectPayload
    label: str = ""

    def to_region(self) -> ProtectedRegion:
        try:
            kind = ProtectedType(self.type)
        except ValueError:
            kind = ProtectedType.USER
        return ProtectedRegion(
            type=kind,
            rect=self.rect.to_rect(),
            label=self.label,
            confidence=1.0,
            evidence=["Marked by the user as always compared."],
        )


class SaveMaskRequest(BaseModel):
    """What the mask editor sends when the user presses apply."""

    zones: list[ZonePayload] = Field(default_factory=list)
    protected: list[ProtectedPayload] = Field(default_factory=list)
    #: Save it under this profile name so other projects can reuse it.
    profile_id: str = ""


def _sheet_views(session: ComparisonSession) -> list[tuple[str, SheetView]]:
    """Render and read every new-side sheet, cheaply, for fingerprinting."""
    from engine.api.routes_tiles import sheet_id as tile_sheet_id
    from engine.extract.raster_renderer import RenderOptions, render_page
    from engine.extract.text_extractor import extract_page_text
    from engine.utils.pdf_runtime import open_document

    views: list[tuple[str, SheetView]] = []
    for sheet in session.new.sheets:
        if not sheet.is_readable:
            continue
        try:
            render = render_page(
                sheet.abs_path, sheet.page_index, RenderOptions(dpi=TEMPLATE_DPI, colour=False)
            )
            with open_document(sheet.abs_path) as document:
                page_text = extract_page_text(document[sheet.page_index], sheet.page_index)
        except Exception as exc:  # a damaged sheet must not stop the review
            logger.warning("Could not read {} for templating: {}", sheet.abs_path, exc)
            continue
        views.append(
            (
                tile_sheet_id(sheet.abs_path, sheet.page_index),
                SheetView(
                    page_text=page_text,
                    gray=render.grayscale,
                    dpi=TEMPLATE_DPI,
                    scale_text=sheet.scale,
                    source_path=sheet.abs_path,
                    page_index=sheet.page_index,
                ),
            )
        )
    return views


def _templates_of(session: ComparisonSession) -> list[dict[str, Any]]:
    identified = _sheet_views(session)
    if not identified:
        raise ValidationError("There are no readable sheets to build a template from.")

    views = [view for _id, view in identified]
    templates = cluster_sheets(views)
    payload: list[dict[str, Any]] = []

    for template in templates:
        detect_template_zones(template, views[template.representative])
        session.mask_templates[template.template_id] = template
        entry = template.as_dict()
        entry["representative_sheet_id"] = identified[template.representative][0]
        entry["member_sheet_ids"] = [identified[index][0] for index in template.members]
        payload.append(entry)
    return payload


@router.get("/mask/templates", summary="Sheet templates and their detected zones")
def mask_templates() -> dict[str, Any]:
    """Cluster the new issue's sheets and detect zones once per cluster."""
    session = get_session()
    templates = _templates_of(session)
    return {
        "templates": templates,
        "sheet_count": sum(int(entry["sheet_count"]) for entry in templates),
    }


@router.get("/mask/{template_id}", summary="One template's mask")
def mask_for_template(template_id: str) -> dict[str, Any]:
    session = get_session()
    template = session.mask_templates.get(template_id)
    if template is None:
        raise NotFoundError("That sheet template is no longer in this comparison.")
    return template.as_dict()


@router.put("/mask/{template_id}", summary="Apply an edited mask to the whole cluster")
def save_mask(template_id: str, body: SaveMaskRequest) -> dict[str, Any]:
    """The primary action of the editor: confirm once, apply to every sheet."""
    session = get_session()
    template: SheetTemplate | None = session.mask_templates.get(template_id)
    if template is None:
        raise NotFoundError("That sheet template is no longer in this comparison.")

    template.zones = [payload.to_zone() for payload in body.zones]
    template.protected = [payload.to_region() for payload in body.protected]
    template.confirmed = True

    if body.profile_id:
        mask = MaskSet(name=body.profile_id, template_id=template_id)
        mask.zones = list(template.zones)
        mask.protected = list(template.protected)
        save_to_profile(mask, profile_id=body.profile_id)

    logger.info(
        "Mask confirmed | template={} | zones={} | sheets={}",
        template_id,
        len(template.zones),
        template.sheet_count,
    )
    return {
        "template_id": template_id,
        "applied_to": template.sheet_count,
        "zones": len(template.zones),
        "protected": len(template.protected),
        "confirmed": True,
    }


@router.post("/mask/{template_id}/load/{profile_id}", summary="Reuse a saved mask")
def load_mask(template_id: str, profile_id: str) -> dict[str, Any]:
    """Apply a mask saved from another project to this template."""
    session = get_session()
    template = session.mask_templates.get(template_id)
    if template is None:
        raise NotFoundError("That sheet template is no longer in this comparison.")

    mask = load_from_profile(profile_id)
    template.zones = list(mask.zones)
    template.protected = list(mask.protected)
    template.confirmed = True
    return template.as_dict()


@router.get("/mask/{template_id}/preview/{sheet_id}", summary="A template applied to one sheet")
def preview_template(template_id: str, sheet_id: str) -> dict[str, Any]:
    """Where the template's zones land on another sheet in the cluster."""
    from engine.api.routes_tiles import _sheet_index

    session = get_session()
    template = session.mask_templates.get(template_id)
    if template is None:
        raise NotFoundError("That sheet template is no longer in this comparison.")

    index = _sheet_index(session) or {}
    target = index.get(sheet_id)
    if target is None:
        raise NotFoundError("That sheet is no longer in this comparison.")

    from engine.extract.raster_renderer import RenderOptions, render_page

    render = render_page(target[0], target[1], RenderOptions(dpi=TEMPLATE_DPI, colour=False))
    view = SheetView(gray=render.grayscale, dpi=TEMPLATE_DPI)
    zones, protected = apply_template(template, view)
    return {
        "template_id": template_id,
        "sheet_id": sheet_id,
        "zones": [zone.as_dict() for zone in zones],
        "protected": [region.as_dict() for region in protected],
    }
