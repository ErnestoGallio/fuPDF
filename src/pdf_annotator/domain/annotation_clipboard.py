"""Versioned, source-independent clipboard payloads for app FreeText comments."""

from dataclasses import dataclass
import json
from math import isfinite
from typing import Any

from .annotation_models import AnnotationModel, AnnotationOwnership
from .geometry import PageBounds, PdfPoint, PdfRect
from .kinds import AnnotationKind
from .rich_text import RichText
from .serialization import (canonical_json, rich_text_from_payload, rich_text_to_payload,
                            style_from_payload, style_to_payload, target_from_payload,
                            target_to_payload)
from .styles import FreeTextStyle
from .targets import CalloutTarget, RectTarget


ANNOTATION_CLIPBOARD_MIME = "application/x-personal-pdf-annotator-annotation"
ANNOTATION_CLIPBOARD_VERSION = 1
_MAX_CLIPBOARD_BYTES = 1_000_000


@dataclass(frozen=True, slots=True)
class AnnotationClipboardPayload:
    """Only the state needed to recreate one app-owned FreeText annotation."""

    kind: AnnotationKind
    target: RectTarget | CalloutTarget
    text: str
    rich_text: RichText
    style: FreeTextStyle
    source_bounds: PageBounds

    def __post_init__(self) -> None:
        if self.kind == AnnotationKind.TEXT_BOX and not isinstance(self.target, RectTarget):
            raise ValueError("Text box clipboard target is invalid")
        if self.kind == AnnotationKind.CALLOUT and not isinstance(self.target, CalloutTarget):
            raise ValueError("Callout clipboard target is invalid")
        if self.kind not in (AnnotationKind.TEXT_BOX, AnnotationKind.CALLOUT):
            raise ValueError("Unsupported clipboard annotation")
        if not isinstance(self.rich_text, RichText) or self.rich_text.plain != self.text:
            raise ValueError("Clipboard rich text is invalid")
        if not isinstance(self.style, FreeTextStyle) or not isinstance(self.source_bounds, PageBounds):
            raise ValueError("Clipboard annotation data is invalid")

    @classmethod
    def from_model(cls, model: AnnotationModel, source_bounds: PageBounds) -> "AnnotationClipboardPayload":
        if (model.ownership != AnnotationOwnership.CREATED_BY_APP
                or model.kind not in (AnnotationKind.TEXT_BOX, AnnotationKind.CALLOUT)
                or not isinstance(model.target, (RectTarget, CalloutTarget))
                or not isinstance(model.style, FreeTextStyle) or not model.text):
            raise ValueError("Annotation cannot be copied")
        rich = model.rich_text or RichText.plain_text(
            model.text, font_family=model.style.font_family,
            font_size=model.style.font_size, color=model.style.text)
        return cls(model.kind, model.target, model.text, rich, model.style, source_bounds)

    def to_bytes(self) -> bytes:
        return canonical_json({
            "version": ANNOTATION_CLIPBOARD_VERSION,
            "kind": self.kind.value,
            "target": target_to_payload(self.target),
            "text": self.text,
            "rich_text": rich_text_to_payload(self.rich_text),
            "style": style_to_payload(self.style),
            "source_bounds": [self.source_bounds.width, self.source_bounds.height],
        }).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> "AnnotationClipboardPayload":
        if not isinstance(raw, bytes) or not raw or len(raw) > _MAX_CLIPBOARD_BYTES:
            raise ValueError("Invalid clipboard payload")
        try:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict) or set(data) != {
                    "version", "kind", "target", "text", "rich_text", "style", "source_bounds"}:
                raise ValueError
            if data["version"] != ANNOTATION_CLIPBOARD_VERSION:
                raise ValueError
            bounds = data["source_bounds"]
            if not isinstance(bounds, list) or len(bounds) != 2:
                raise ValueError
            target = target_from_payload(data["target"])
            style = style_from_payload(data["style"])
            if not isinstance(target, (RectTarget, CalloutTarget)) or not isinstance(style, FreeTextStyle):
                raise ValueError
            return cls(AnnotationKind(data["kind"]), target, data["text"],
                       rich_text_from_payload(data["rich_text"]), style, PageBounds(*bounds))
        except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid clipboard payload") from exc

    def target_for_destination(self, page_index: int, bounds: PageBounds,
                               offset_x: float, offset_y: float) -> RectTarget | CalloutTarget:
        """Scale canonical geometry between page sizes, then offset and clamp it."""
        if (type(page_index) is not int or page_index < 0
                or any(type(value) not in (int, float) or not isfinite(value)
                       for value in (offset_x, offset_y))):
            raise ValueError("Invalid clipboard destination")
        scale_x, scale_y = bounds.width / self.source_bounds.width, bounds.height / self.source_bounds.height
        rect = _fit_rect(_scale_rect(_rect(self.target), scale_x, scale_y), bounds,
                         float(offset_x), float(offset_y))
        if isinstance(self.target, RectTarget):
            return RectTarget(page_index, rect)
        anchor = _clamp_point(_scale_point(self.target.anchor, scale_x, scale_y), bounds)
        knee = (_clamp_point(_scale_point(self.target.knee, scale_x, scale_y), bounds)
                if self.target.knee is not None else None)
        # A single translation preserves the box/leader relationship whenever it
        # fits; individual point clamps are only the deterministic last resort.
        dx, dy = rect.x0 - _scale_rect(self.target.text_rect, scale_x, scale_y).x0, rect.y0 - _scale_rect(self.target.text_rect, scale_x, scale_y).y0
        anchor = _clamp_point(PdfPoint(anchor.x + dx, anchor.y + dy), bounds)
        knee = (_clamp_point(PdfPoint(knee.x + dx, knee.y + dy), bounds) if knee else None)
        return CalloutTarget(page_index, rect, anchor, knee)


def _rect(target: RectTarget | CalloutTarget) -> PdfRect:
    return target.rect if isinstance(target, RectTarget) else target.text_rect


def _scale_point(point: PdfPoint, sx: float, sy: float) -> PdfPoint:
    return PdfPoint(point.x * sx, point.y * sy)


def _scale_rect(rect: PdfRect, sx: float, sy: float) -> PdfRect:
    return PdfRect(rect.x0 * sx, rect.y0 * sy, rect.x1 * sx, rect.y1 * sy)


def _clamp_point(point: PdfPoint, bounds: PageBounds) -> PdfPoint:
    return PdfPoint(min(bounds.width, max(0.0, point.x)), min(bounds.height, max(0.0, point.y)))


def _fit_rect(rect: PdfRect, bounds: PageBounds, offset_x: float, offset_y: float) -> PdfRect:
    width, height = rect.x1 - rect.x0, rect.y1 - rect.y0
    scale = min(1.0, bounds.width / width, bounds.height / height)
    width, height = width * scale, height * scale
    x0, y0 = rect.x0 * scale + offset_x, rect.y0 * scale + offset_y
    x0 = min(bounds.width - width, max(0.0, x0))
    y0 = min(bounds.height - height, max(0.0, y0))
    return PdfRect(x0, y0, x0 + width, y0 + height)
