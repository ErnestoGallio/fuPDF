"""Canonical geometry is crop-local, unrotated PyMuPDF points, never pixels.

CropBox and MediaBox are metadata, not an additional translation to apply to
coordinates already returned by the page API. M0 fixtures prove this distinction.
"""

from dataclasses import dataclass
from math import isfinite

Point = tuple[float, float]
Rect = tuple[float, float, float, float]


@dataclass(frozen=True)
class PageGeometry:
    page_index: int
    width_pt: float
    height_pt: float
    rotation: int
    cropbox: Rect
    mediabox: Rect

    def __post_init__(self) -> None:
        if self.rotation not in (0, 90, 180, 270):
            raise ValueError("Unsupported display rotation")
        if not all(isfinite(v) and v > 0 for v in (self.width_pt, self.height_pt)):
            raise ValueError("Invalid canonical page dimensions")

    @property
    def displayed_size(self) -> Point:
        if self.rotation in (90, 270):
            return self.height_pt, self.width_pt
        return self.width_pt, self.height_pt


@dataclass(frozen=True)
class CoordinateMapper:
    geometry: PageGeometry
    zoom: float = 1.0
    scene_origin: Point = (0.0, 0.0)
    viewport_scene_origin: Point = (0.0, 0.0)

    def __post_init__(self) -> None:
        if not isfinite(self.zoom) or self.zoom <= 0:
            raise ValueError("Zoom must be positive and finite")

    def canonical_to_display(self, point: Point) -> Point:
        x, y = point
        w, h = self.geometry.width_pt, self.geometry.height_pt
        return {0: (x, y), 90: (h - y, x), 180: (w - x, h - y),
                270: (y, w - x)}[self.geometry.rotation]

    def display_to_canonical(self, point: Point) -> Point:
        x, y = point
        w, h = self.geometry.width_pt, self.geometry.height_pt
        return {0: (x, y), 90: (y, h - x), 180: (w - x, h - y),
                270: (w - y, x)}[self.geometry.rotation]

    def canonical_to_viewport(self, point: Point) -> Point:
        x, y = self.canonical_to_display(point)
        return (x * self.zoom + self.scene_origin[0] - self.viewport_scene_origin[0],
                y * self.zoom + self.scene_origin[1] - self.viewport_scene_origin[1])

    def viewport_to_canonical(self, point: Point) -> Point:
        return self.display_to_canonical((
            (point[0] - self.scene_origin[0] + self.viewport_scene_origin[0]) / self.zoom,
            (point[1] - self.scene_origin[1] + self.viewport_scene_origin[1]) / self.zoom,
        ))

    def canonical_to_raster(self, point: Point, raster_scale: float) -> Point:
        """raster_scale already includes the requested DPR and any memory cap."""
        if not isfinite(raster_scale) or raster_scale <= 0:
            raise ValueError("Invalid raster scale")
        x, y = self.canonical_to_display(point)
        return x * raster_scale, y * raster_scale
