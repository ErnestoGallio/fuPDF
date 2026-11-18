"""Typed geometric and selection-derived targets in canonical PDF space."""

from dataclasses import dataclass
from enum import Enum
from math import isfinite

from .geometry import PdfPoint, PdfQuad, PdfRect
from .identifiers import new_command_id, validate_target_id

MAX_TEXT_CHARS = 16_384
MAX_TEXT_RANGE_CHARS = 250_000
MAX_INK_PATHS = 64
MAX_INK_POINTS = 1_024
MAX_TEXT_QUADS = 8_192


def page_index(value: int) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("Invalid page index")
    return value


def plain_text(value: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value) or len(value) > MAX_TEXT_CHARS:
        raise ValueError("Invalid plain text")
    if "\x00" in value:
        raise ValueError("Invalid plain text")
    return value


@dataclass(frozen=True, slots=True)
class RectTarget:
    page_index: int
    rect: PdfRect

    def __post_init__(self) -> None:
        page_index(self.page_index)
        if not isinstance(self.rect, PdfRect):
            raise ValueError("Invalid rectangle target")


@dataclass(frozen=True, slots=True)
class InkTarget:
    page_index: int
    paths: tuple[tuple[PdfPoint, ...], ...]

    def __post_init__(self) -> None:
        page_index(self.page_index)
        if (not isinstance(self.paths, tuple) or not self.paths
                or len(self.paths) > MAX_INK_PATHS
                or any(not isinstance(path, tuple) or len(path) < 2
                       or any(not isinstance(point, PdfPoint) for point in path)
                       for path in self.paths)
                or sum(len(path) for path in self.paths) > MAX_INK_POINTS):
            raise ValueError("Invalid ink target")


@dataclass(frozen=True, slots=True)
class CalloutTarget:
    page_index: int
    text_rect: PdfRect
    anchor: PdfPoint
    knee: PdfPoint | None = None

    def __post_init__(self) -> None:
        page_index(self.page_index)
        if (not isinstance(self.text_rect, PdfRect) or not isinstance(self.anchor, PdfPoint)
                or (self.knee is not None and not isinstance(self.knee, PdfPoint))):
            raise ValueError("Invalid callout target")


@dataclass(frozen=True, slots=True)
class TextRangeTarget:
    target_id: str
    page_index: int
    quads: tuple[PdfQuad, ...]
    original_text: str
    snapshot_id: str | None = None
    start_char_index: int | None = None
    end_char_index: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_id", validate_target_id(self.target_id))
        page_index(self.page_index)
        if (not isinstance(self.quads, tuple) or not self.quads
                or len(self.quads) > MAX_TEXT_QUADS
                or any(not isinstance(quad, PdfQuad) for quad in self.quads)):
            raise ValueError("Invalid text range quads")
        if (not isinstance(self.original_text, str) or not self.original_text
                or len(self.original_text) > MAX_TEXT_RANGE_CHARS
                or "\x00" in self.original_text):
            raise ValueError("Invalid original range text")
        identities = (self.snapshot_id, self.start_char_index, self.end_char_index)
        if any(value is not None for value in identities):
            if (self.snapshot_id is None or self.start_char_index is None
                    or self.end_char_index is None):
                raise ValueError("Incomplete confirmed text range")
            object.__setattr__(self, "snapshot_id", validate_target_id(self.snapshot_id))
            if (type(self.start_char_index) is not int or type(self.end_char_index) is not int
                    or self.start_char_index < 0
                    or self.end_char_index < self.start_char_index):
                raise ValueError("Invalid confirmed text range")

    @classmethod
    def create(cls, page: int, quads: tuple[PdfQuad, ...], original_text: str, *,
               snapshot_id: str | None = None, start_char_index: int | None = None,
               end_char_index: int | None = None) -> "TextRangeTarget":
        return cls(new_command_id(), page, quads, original_text, snapshot_id,
                   start_char_index, end_char_index)


class BoundaryAffinity(str, Enum):
    BEFORE = "before"
    AFTER = "after"


@dataclass(frozen=True, slots=True)
class TextInsertionTarget:
    target_id: str
    page_index: int
    anchor: PdfPoint
    affinity: BoundaryAffinity
    context_before: str = ""
    context_after: str = ""
    snapshot_id: str | None = None
    char_index: int | None = None
    boundary_top: PdfPoint | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_id", validate_target_id(self.target_id))
        page_index(self.page_index)
        if not isinstance(self.anchor, PdfPoint) or not isinstance(self.affinity, BoundaryAffinity):
            raise ValueError("Invalid insertion target")
        plain_text(self.context_before, allow_empty=True)
        plain_text(self.context_after, allow_empty=True)
        if not self.context_before and not self.context_after:
            raise ValueError("Insertion target requires local text context")
        confirmed = (self.snapshot_id, self.char_index, self.boundary_top)
        if any(value is not None for value in confirmed):
            if (self.snapshot_id is None or type(self.char_index) is not int
                    or self.char_index < 0 or not isinstance(self.boundary_top, PdfPoint)
                    or self.boundary_top == self.anchor):
                raise ValueError("Incomplete confirmed insertion boundary")
            object.__setattr__(self, "snapshot_id", validate_target_id(self.snapshot_id))

    @classmethod
    def create(cls, page: int, anchor: PdfPoint, affinity: BoundaryAffinity,
               *, context_before: str = "", context_after: str = "") -> "TextInsertionTarget":
        return cls(new_command_id(), page, anchor, affinity, context_before, context_after)


AnnotationTarget = RectTarget | InkTarget | CalloutTarget | TextRangeTarget | TextInsertionTarget


def target_page(target: AnnotationTarget) -> int:
    if not isinstance(target, (RectTarget, InkTarget, CalloutTarget,
                               TextRangeTarget, TextInsertionTarget)):
        raise ValueError("Invalid annotation target")
    return target.page_index


def target_points(target: AnnotationTarget) -> tuple[PdfPoint, ...]:
    if isinstance(target, RectTarget):
        return target.rect.points
    if isinstance(target, InkTarget):
        return tuple(point for path in target.paths for point in path)
    if isinstance(target, CalloutTarget):
        return (*target.text_rect.points, target.anchor, *((target.knee,) if target.knee else ()))
    if isinstance(target, TextRangeTarget):
        return tuple(point for quad in target.quads for point in quad.points)
    if isinstance(target, TextInsertionTarget):
        return (target.anchor,) + ((target.boundary_top,) if target.boundary_top else ())
    raise ValueError("Invalid annotation target")


def clamp_ink_translation(target: InkTarget, width: float, height: float,
                          delta_x: float, delta_y: float) -> tuple[float, float]:
    """Keep a whole Ink path on its original page without deforming it."""
    if (not isinstance(target, InkTarget)
            or any(type(value) not in (int, float) or not isfinite(value)
                   for value in (width, height, delta_x, delta_y))
            or width <= 0 or height <= 0):
        raise ValueError("Invalid Ink translation")
    points = target_points(target)
    min_x, max_x = min(point.x for point in points), max(point.x for point in points)
    min_y, max_y = min(point.y for point in points), max(point.y for point in points)
    return (min(float(width) - max_x, max(-min_x, float(delta_x))),
            min(float(height) - max_y, max(-min_y, float(delta_y))))


def translate_ink_target(target: InkTarget, delta_x: float, delta_y: float) -> InkTarget:
    if (not isinstance(target, InkTarget)
            or any(type(value) not in (int, float) or not isfinite(value)
                   for value in (delta_x, delta_y))):
        raise ValueError("Invalid Ink translation")
    return InkTarget(target.page_index, tuple(
        tuple(PdfPoint(point.x + float(delta_x), point.y + float(delta_y)) for point in path)
        for path in target.paths))
