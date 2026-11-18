"""Bounded GUI-only freehand gesture state in canonical PDF coordinates."""

from dataclasses import dataclass
from math import hypot

from ..domain.geometry import PdfPoint
from ..domain.targets import MAX_INK_POINTS, InkTarget

MIN_SAMPLE_DISTANCE_PX = 0.75
MIN_GESTURE_DISTANCE_PX = 2.0


@dataclass(frozen=True, slots=True)
class PenGestureMetrics:
    raw_events: int
    final_points: int


class PenGestureController:
    """Collect one single-page path without any worker or IPC dependency."""

    def __init__(self) -> None:
        self.page_index: int | None = None
        self._points: list[PdfPoint] = []
        self._display_points: list[tuple[float, float]] = []
        self._sample_distance_px = MIN_SAMPLE_DISTANCE_PX
        self._raw_events = 0
        self._travel_px = 0.0

    @property
    def active(self) -> bool:
        return self.page_index is not None

    @property
    def points(self) -> tuple[PdfPoint, ...]:
        return tuple(self._points)

    @property
    def metrics(self) -> PenGestureMetrics:
        return PenGestureMetrics(self._raw_events, len(self._points))

    def begin(self, page_index: int, canonical: PdfPoint,
              display: tuple[float, float]) -> None:
        self.cancel()
        self.page_index = page_index
        self._points = [canonical]
        self._display_points = [display]
        self._raw_events = 1

    def update(self, canonical: PdfPoint, display: tuple[float, float], *,
               final: bool = False) -> bool:
        if not self.active:
            return False
        self._raw_events += 1
        previous = self._display_points[-1]
        distance = hypot(display[0] - previous[0], display[1] - previous[1])
        if not final and distance < self._sample_distance_px:
            return False
        if distance <= 1e-9:
            return False
        self._travel_px += distance
        self._points.append(canonical)
        self._display_points.append(display)
        if len(self._points) > MAX_INK_POINTS:
            self._decimate()
        return True

    def finalize(self) -> tuple[InkTarget | None, PenGestureMetrics]:
        metrics = self.metrics
        page_index = self.page_index
        points = tuple(self._points)
        meaningful = (page_index is not None and len(points) >= 2
                      and self._travel_px >= MIN_GESTURE_DISTANCE_PX)
        self.cancel()
        if not meaningful:
            return None, metrics
        return InkTarget(page_index, (points,)), metrics

    def cancel(self) -> None:
        self.page_index = None
        self._points.clear()
        self._display_points.clear()
        self._sample_distance_px = MIN_SAMPLE_DISTANCE_PX
        self._raw_events = 0
        self._travel_px = 0.0

    def _decimate(self) -> None:
        # Keep endpoints and every second interior sample. Repeated passes remain
        # deterministic, bounded and preserve the whole gesture instead of truncating it.
        keep = [0, *range(2, len(self._points) - 1, 2), len(self._points) - 1]
        self._points = [self._points[index] for index in keep]
        self._display_points = [self._display_points[index] for index in keep]
        self._sample_distance_px *= 2
