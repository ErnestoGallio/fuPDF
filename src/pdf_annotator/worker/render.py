"""Shared bounded opaque renderer. Only called in the owning PDF process."""

import math
from typing import Any

from .protocol import MAX_SINGLE_RASTER_BYTES, MIN_RASTER_BYTES, frame_limit, positive_number


def bounded_scale(width: float, height: float, requested: float,
                  limit: int = MAX_SINGLE_RASTER_BYTES) -> float:
    if not all(math.isfinite(v) and v > 0 for v in (width, height, requested)):
        raise ValueError("Invalid render dimensions/scale")
    frame_limit(limit)
    # Reserve rounding space before MuPDF allocates. The pixmap uses integer bounds.
    scale = min(requested, math.sqrt(limit / 3 / width / height))
    # Include Qt's possible 4-byte RGB row padding in the same allocation bound.
    while ((math.ceil(width * scale) + 1) * 3 + 3) // 4 * 4 * (math.ceil(height * scale) + 1) > limit:
        scale *= 0.98
    return scale


def render_page(doc: Any, payload: dict[str, Any], *,
                raster_limit: int = MAX_SINGLE_RASTER_BYTES) -> tuple[dict[str, Any], bytes]:
    import pymupdf

    index = payload.get("page_index", 0)
    if type(index) is not int or not 0 <= index < len(doc):
        raise ValueError("Invalid page index")
    zoom = positive_number(payload, "geometric_zoom", 1.0)
    dpr = positive_number(payload, "dpr", 1.0)
    limit = payload.get("max_bytes", MAX_SINGLE_RASTER_BYTES)
    if type(limit) is not int or not MIN_RASTER_BYTES <= limit <= raster_limit:
        raise ValueError("Invalid raster limit")
    page = doc[index]
    requested = positive_number(payload, "render_scale", zoom * dpr)
    clip_data = payload.get("clip")
    clip = None
    tile = None
    if clip_data is not None:
        if (not isinstance(clip_data, list) or len(clip_data) != 4
                or not all(type(value) in (int, float) and math.isfinite(value)
                           for value in clip_data)):
            raise ValueError("Invalid regional clip")
        tile = payload.get("tile")
        if (not isinstance(tile, list) or len(tile) != 3
                or any(type(value) is not int or value < 0 for value in tile)):
            raise ValueError("Invalid tile identity")
        clip = pymupdf.Rect(clip_data)
        intersection = clip & page.rect
        if (intersection.is_empty
                or any(abs(actual - expected) > 1e-4
                       for actual, expected in zip(tuple(intersection), tuple(clip)))):
            raise ValueError("Regional clip outside page")
        clip = intersection
        pixel_width = math.ceil(clip.x1 * requested) - math.floor(clip.x0 * requested)
        pixel_height = math.ceil(clip.y1 * requested) - math.floor(clip.y0 * requested)
        if pixel_width * pixel_height * 3 > limit:
            raise ValueError("Regional raster exceeds limit")
        scale = requested
    else:
        scale = bounded_scale(page.rect.width, page.rect.height, requested, limit)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale),
                          colorspace=pymupdf.csRGB, alpha=False, annots=False, clip=clip)
    size = pix.stride * pix.height
    if size > limit or pix.n != 3 or pix.alpha:
        raise RuntimeError("Renderer violated the pre-allocation estimate")
    coverage = ([pix.x / scale, pix.y / scale,
                 (pix.x + pix.width) / scale, (pix.y + pix.height) / scale]
                if clip is not None else None)
    return ({"page_index": index, "width": pix.width, "height": pix.height,
             "stride": pix.stride, "format": "RGB888", "alpha": False,
             "byte_count": size, "geometric_zoom": zoom, "dpr": dpr,
             "requested_scale": requested, "raster_scale": scale,
             "degraded": scale < zoom * dpr, "clip": clip_data,
             "tile": tile, "coverage": coverage}, pix.samples)
