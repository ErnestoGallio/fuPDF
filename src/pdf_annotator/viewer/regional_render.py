"""Pure deterministic regional-render geometry in rotated page-display points."""

from dataclasses import dataclass
from math import ceil, floor, isfinite, sqrt

Rect = tuple[float, float, float, float]
Size = tuple[float, float]

DEFAULT_TILE_PX = 768
MIN_TILE_PX = 512


@dataclass(frozen=True, order=True)
class TileRegion:
    column: int
    row: int
    tile_px: int = DEFAULT_TILE_PX

    def __post_init__(self) -> None:
        if any(type(value) is not int for value in (self.column, self.row, self.tile_px)):
            raise ValueError("Invalid tile identity")
        if self.column < 0 or self.row < 0 or self.tile_px < MIN_TILE_PX:
            raise ValueError("Invalid tile identity")


def raster_bytes(size: Size, scale: float) -> int:
    """Conservative Qt RGB888 storage including 4-byte row alignment."""
    width, height = size
    if not all(isfinite(value) and value > 0 for value in (width, height, scale)):
        raise ValueError("Invalid raster estimate")
    pixel_width = ceil(width * scale)
    pixel_height = ceil(height * scale)
    return ((pixel_width * 3 + 3) // 4 * 4) * pixel_height


def regional_raster_bytes(rect: Rect, scale: float) -> int:
    """Conservative storage for MuPDF's transformed integer clip bounds."""
    if not all(isfinite(value) for value in (*rect, scale)) or scale <= 0:
        raise ValueError("Invalid regional estimate")
    x0, y0, x1, y1 = rect
    if x1 <= x0 or y1 <= y0:
        raise ValueError("Invalid regional estimate")
    # PyMuPDF stores Rect coordinates as float32. A start may round down while
    # the matching end rounds up, so reserve two transformed pixels per axis.
    pixel_width = ceil(x1 * scale) - floor(x0 * scale) + 2
    pixel_height = ceil(y1 * scale) - floor(y0 * scale) + 2
    return ((pixel_width * 3 + 3) // 4 * 4) * pixel_height


def full_page_fits(size: Size, scale: float, limit: int) -> bool:
    return type(limit) is int and limit > 0 and raster_bytes(size, scale) <= limit


def tile_size_for_limit(limit: int, preferred: int = DEFAULT_TILE_PX) -> int | None:
    """Retain the measured default; shrink only for controlled smaller budgets."""
    if type(limit) is not int or limit <= 0 or type(preferred) is not int or preferred < MIN_TILE_PX:
        raise ValueError("Invalid tile limit")
    maximum = floor(sqrt(limit / 3)) - 2
    return min(preferred, maximum) if maximum >= MIN_TILE_PX else None


def tile_rect(page_size: Size, scale: float, region: TileRegion) -> Rect:
    width, height = page_size
    if not all(isfinite(value) and value > 0 for value in (width, height, scale)):
        raise ValueError("Invalid tile geometry")
    step = region.tile_px / scale
    x0, y0 = region.column * step, region.row * step
    if x0 >= width or y0 >= height:
        raise ValueError("Tile outside page")
    return x0, y0, min(width, x0 + step), min(height, y0 + step)


def regions_for_rect(page_size: Size, scale: float, clip: Rect,
                     tile_px: int = DEFAULT_TILE_PX) -> tuple[TileRegion, ...]:
    """Return stable row-major tiles intersecting a page-local display-space clip."""
    width, height = page_size
    x0, y0, x1, y1 = clip
    if (not all(isfinite(value) for value in (*page_size, scale, *clip))
            or width <= 0 or height <= 0 or scale <= 0 or tile_px < MIN_TILE_PX):
        raise ValueError("Invalid regional clip")
    x0, y0 = max(0.0, x0), max(0.0, y0)
    x1, y1 = min(width, x1), min(height, y1)
    if x1 <= x0 or y1 <= y0:
        return ()
    step = tile_px / scale
    first_column, first_row = floor(x0 / step), floor(y0 / step)
    last_column = min(ceil(width / step) - 1, ceil(x1 / step) - 1)
    last_row = min(ceil(height / step) - 1, ceil(y1 / step) - 1)
    return tuple(TileRegion(column, row, tile_px)
                 for row in range(first_row, last_row + 1)
                 for column in range(first_column, last_column + 1))


def adjacent_regions(page_size: Size, scale: float, visible: tuple[TileRegion, ...],
                     direction: int) -> tuple[TileRegion, ...]:
    """One deterministic row ahead (or behind) for bounded vertical overscan."""
    if not visible or direction not in (-1, 1):
        return ()
    tile_px = visible[0].tile_px
    columns = range(min(region.column for region in visible),
                    max(region.column for region in visible) + 1)
    row = ((max(region.row for region in visible) + 1) if direction > 0 else
           (min(region.row for region in visible) - 1))
    if row < 0:
        return ()
    candidates = tuple(TileRegion(column, row, tile_px) for column in columns)
    valid = []
    for region in candidates:
        try:
            tile_rect(page_size, scale, region)
        except ValueError:
            continue
        valid.append(region)
    return tuple(valid)
