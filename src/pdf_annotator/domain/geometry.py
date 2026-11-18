"""Canonical unrotated PDF geometry value objects."""

from dataclasses import dataclass
from math import isfinite


def _coordinate(value: float, name: str) -> float:
    # PDF annotation geometry is not constrained to the CropBox.  Imported
    # /Rect and /CL coordinates can legitimately be negative or extend past a
    # page edge; creation commands apply their own page-bound policy later.
    if type(value) not in (int, float) or not isfinite(value):
        raise ValueError(f"Invalid {name}")
    return float(value)


@dataclass(frozen=True, slots=True)
class PdfPoint:
    x: float
    y: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "x", _coordinate(self.x, "PDF point"))
        object.__setattr__(self, "y", _coordinate(self.y, "PDF point"))


@dataclass(frozen=True, slots=True)
class PdfRect:
    x0: float
    y0: float
    x1: float
    y1: float

    def __post_init__(self) -> None:
        for name in ("x0", "y0", "x1", "y1"):
            object.__setattr__(self, name, _coordinate(getattr(self, name), "PDF rectangle"))
        if self.x1 <= self.x0 or self.y1 <= self.y0:
            raise ValueError("Degenerate PDF rectangle")

    @property
    def points(self) -> tuple[PdfPoint, ...]:
        return (PdfPoint(self.x0, self.y0), PdfPoint(self.x1, self.y0),
                PdfPoint(self.x1, self.y1), PdfPoint(self.x0, self.y1))


@dataclass(frozen=True, slots=True)
class PdfQuad:
    upper_left: PdfPoint
    upper_right: PdfPoint
    lower_left: PdfPoint
    lower_right: PdfPoint

    def __post_init__(self) -> None:
        if not all(isinstance(point, PdfPoint) for point in self.points):
            raise ValueError("Invalid PDF quad")
        perimeter = (self.upper_left, self.upper_right, self.lower_right, self.lower_left)
        twice_area = abs(sum(a.x * b.y - b.x * a.y
                             for a, b in zip(perimeter, perimeter[1:] + perimeter[:1])))
        if twice_area <= 1e-9:
            raise ValueError("Degenerate PDF quad")

    @property
    def points(self) -> tuple[PdfPoint, ...]:
        return self.upper_left, self.upper_right, self.lower_left, self.lower_right


@dataclass(frozen=True, slots=True)
class PageBounds:
    width: float
    height: float

    def __post_init__(self) -> None:
        for name in ("width", "height"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not isfinite(value) or value <= 0:
                raise ValueError("Invalid page bounds")
            object.__setattr__(self, name, float(value))

    def contains(self, point: PdfPoint) -> bool:
        return 0 <= point.x <= self.width and 0 <= point.y <= self.height
