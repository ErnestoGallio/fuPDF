"""Continuous page/spread geometry; no Qt objects or PDF engine calls."""

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import Sequence

from .coordinate_mapper import PageGeometry

BASE_SCALE = 1.0  # logical Qt pixels per PDF point, independent of DPR
PAGE_MARGIN = 20.0
PAGE_GAP = 16.0
PAGE_COLUMN_GAP = 0.0
PAGE_ROW_GAP = PAGE_GAP / 2


class LayoutMode(str, Enum):
    SINGLE = "single"
    DOUBLE = "double"
    DOUBLE_COVER = "double_cover"


@dataclass(frozen=True)
class PageRect:
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class LayoutRow:
    page_indices: tuple[int, ...]
    y: float
    width: float
    height: float


class PageLayout:
    def __init__(self, pages: Sequence[PageGeometry], scale: float = BASE_SCALE,
                 mode: LayoutMode | str = LayoutMode.SINGLE, *, column_gap: float = PAGE_COLUMN_GAP) -> None:
        if (not pages or not isfinite(scale) or scale <= 0
                or not isfinite(column_gap) or column_gap < 0):
            raise ValueError("Invalid page layout")
        self.scale = scale
        self.mode = LayoutMode(mode)
        self.column_gap = column_gap  # logical pixels, never multiplied by zoom or DPR
        sizes = [(p.displayed_size[0] * scale, p.displayed_size[1] * scale) for p in pages]
        first = 1 if self.mode == LayoutMode.DOUBLE_COVER else 0
        step = 1 if self.mode == LayoutMode.SINGLE else 2
        groups = ([(0,)] if first else []) + [tuple(range(i, min(i + step, len(pages))))
                                             for i in range(first, len(pages), step)]
        row_sizes = [(sum(sizes[i][0] for i in group) + column_gap * (len(group) - 1),
                      max(sizes[i][1] for i in group)) for group in groups]
        self.width = max(w for w, h in row_sizes) + 2 * PAGE_MARGIN
        y = PAGE_MARGIN
        rectangles = []
        rows = []
        page_rows = []
        for row_index, (group, (width, height)) in enumerate(zip(groups, row_sizes)):
            # An incomplete final spread starts at the left edge; the cover and
            # single-page rows are centered. No phantom page enters the model.
            orphan = (len(group) == 1 and self.mode != LayoutMode.SINGLE
                      and not (self.mode == LayoutMode.DOUBLE_COVER and row_index == 0))
            x = PAGE_MARGIN if orphan else (self.width - width) / 2
            for index in group:
                page_width, page_height = sizes[index]
                rectangles.append(PageRect(x, y, page_width, page_height))
                page_rows.append(row_index)
                x += page_width + column_gap
            rows.append(LayoutRow(group, y, width, height))
            y += height + PAGE_ROW_GAP
        self.height = y - PAGE_ROW_GAP + PAGE_MARGIN
        if max(self.width, self.height) > 2**31 - 1:
            raise ValueError("Document exceeds Qt scroll coordinate range")
        self.pages = tuple(rectangles)
        self.rows = tuple(rows)
        self.page_rows = tuple(page_rows)
        self._tops = tuple(row.y for row in self.rows)
        self._bottoms = tuple(row.y + row.height for row in self.rows)

    def visible(self, x: float, y: float, width: float, height: float) -> tuple[int, ...]:
        """Strict page rectangle intersection, O(log rows + intersecting rows)."""
        if width <= 0 or height <= 0:
            return ()
        start = bisect_right(self._bottoms, y)
        end = bisect_left(self._tops, y + height)
        return tuple(i for row in self.rows[start:end] for i in row.page_indices
                     if (self.pages[i].x < x + width and self.pages[i].x + self.pages[i].width > x
                         and self.pages[i].y < y + height and self.pages[i].y + self.pages[i].height > y))

    def nearby(self, visible: tuple[int, ...], y: float, height: float,
               count: int = 1, *, direction: int = 1) -> tuple[int, ...]:
        if type(count) is not int or not 0 <= count <= 4:
            raise ValueError("Nearby window must be between zero and four rows per side")
        if not count:
            return ()
        # Empty viewports (e.g. a gap) still have well-defined adjacent rows.
        first = self.page_rows[visible[0]] if visible else bisect_right(self._bottoms, y)
        end = self.page_rows[visible[-1]] + 1 if visible else bisect_left(self._tops, y + height)
        following = list(range(end, min(len(self.rows), end + count)))
        preceding = list(range(first - 1, max(-1, first - count - 1), -1))
        row_order = list(range(first, end)) + (following + preceding if direction >= 0
                                               else preceding + following)
        return tuple(i for row in row_order for i in self.rows[row].page_indices if i not in visible)

    def page_at(self, scene_y: float, scene_x: float | None = None) -> int:
        row = self.rows[max(0, min(len(self.rows) - 1, bisect_right(self._tops, scene_y) - 1))]
        if scene_x is None:
            return row.page_indices[0]
        return min(row.page_indices,
                   key=lambda i: max(self.pages[i].x - scene_x, 0,
                                     scene_x - self.pages[i].x - self.pages[i].width))


# Existing single-layout callers share the same geometry engine and default.
SingleLayout = PageLayout
