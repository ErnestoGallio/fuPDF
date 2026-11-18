"""Rasterize a native external FreeText appearance in its owning worker."""

import math
from typing import Any

from .protocol import MAX_SINGLE_RASTER_BYTES, MIN_RASTER_BYTES, frame_limit, positive_number


def _native_id(value: object) -> tuple[int, int]:
    try:
        prefix, page, xref = str(value).split(":")
        if prefix != "native":
            raise ValueError
        return int(page), int(xref)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid annotation identity") from exc


def _appearance_scale(width: float, height: float, requested: float, limit: int) -> float:
    if not all(math.isfinite(value) and value > 0 for value in (width, height, requested)):
        raise ValueError("Invalid appearance bounds")
    scale = min(requested, math.sqrt(limit / 4 / width / height))
    while math.ceil(width * scale) * math.ceil(height * scale) * 4 > limit:
        scale *= .98
    return scale


def render_annotation_appearance(document: Any, payload: dict[str, Any], *,
                                 raster_limit: int = MAX_SINGLE_RASTER_BYTES) -> tuple[dict[str, object], bytes]:
    """Return the authored /AP/N, never the page raster with ``annots=True``."""
    import pymupdf

    page_index, xref = _native_id(payload.get("logical_id"))
    if payload.get("page_index") != page_index or not 0 <= page_index < document.page_count:
        raise ValueError("Invalid annotation page")
    if document.xref_get_key(xref, "Subtype") != ("name", "/FreeText"):
        raise ValueError("Annotation is not FreeText")
    ap_kind, _ap_value = document.xref_get_key(xref, "AP")
    if ap_kind != "dict":
        raise ValueError("Annotation has no normal appearance")
    limit = payload.get("max_bytes", MAX_SINGLE_RASTER_BYTES)
    if type(limit) is not int or not MIN_RASTER_BYTES <= limit <= raster_limit:
        raise ValueError("Invalid appearance raster limit")
    requested = positive_number(payload, "render_scale", 1.0)
    page = document.load_page(page_index)
    annotation = page.load_annot(xref)
    if annotation is None:
        raise ValueError("Annotation not found")
    rect = annotation.rect
    scale = _appearance_scale(rect.width, rect.height, requested, limit)
    pixmap = annotation.get_pixmap(matrix=pymupdf.Matrix(scale, scale),
                                   colorspace=pymupdf.csRGB, alpha=True)
    size = pixmap.stride * pixmap.height
    if size > limit or pixmap.n != 4 or not pixmap.alpha:
        raise RuntimeError("Appearance renderer violated bounds")
    return ({"logical_id": payload["logical_id"], "page_index": page_index,
             "annotation_xref": xref, "width": pixmap.width, "height": pixmap.height,
             "stride": pixmap.stride, "format": "RGBA8888", "alpha": True,
             "byte_count": size, "requested_scale": requested, "raster_scale": scale,
             "coverage": [pixmap.x / scale, pixmap.y / scale,
                          (pixmap.x + pixmap.width) / scale,
                          (pixmap.y + pixmap.height) / scale]}, pixmap.samples)
