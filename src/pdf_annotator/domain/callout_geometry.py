"""Deterministic Callout leader geometry in canonical page coordinates."""

from __future__ import annotations

from .geometry import PdfPoint, PdfRect

CALLOUT_LEADER_OFFSET = 12.0


def callout_attachment_side(rect: PdfRect, attachment: PdfPoint) -> str:
    """Return the closest box edge for an observed Callout attachment."""
    distances = ((abs(attachment.x - rect.x0), "left"),
                 (abs(attachment.x - rect.x1), "right"),
                 (abs(attachment.y - rect.y0), "top"),
                 (abs(attachment.y - rect.y1), "bottom"))
    return min(distances, key=lambda item: item[0])[1]


def _attachment_side(rect: PdfRect, anchor: PdfPoint) -> str:
    """Choose the edge intersected by the center-to-anchor direction."""
    half_width = (rect.x1 - rect.x0) / 2
    half_height = (rect.y1 - rect.y0) / 2
    horizontal = abs(anchor.x - (rect.x0 + rect.x1) / 2) / half_width
    vertical = abs(anchor.y - (rect.y0 + rect.y1) / 2) / half_height
    if horizontal >= vertical:
        return "left" if anchor.x <= (rect.x0 + rect.x1) / 2 else "right"
    return "top" if anchor.y <= (rect.y0 + rect.y1) / 2 else "bottom"


def callout_line(rect: PdfRect, anchor: PdfPoint, *, side: str | None = None) -> tuple[PdfPoint, PdfPoint, PdfPoint]:
    """Return anchor, knee and box attachment for an Acrobat-like leader."""
    if side is None:
        side = _attachment_side(rect, anchor)
    if side not in ("left", "right", "top", "bottom"):
        raise ValueError("Invalid Callout attachment side")
    if side in ("left", "right"):
        attachment = PdfPoint(rect.x0 if side == "left" else rect.x1,
                              min(rect.y1, max(rect.y0, anchor.y)))
        available = max(0.0, rect.x0 - anchor.x if side == "left"
                        else anchor.x - rect.x1)
        offset = min(CALLOUT_LEADER_OFFSET, available)
        knee = PdfPoint(attachment.x + (-offset if side == "left" else offset), attachment.y)
    else:
        attachment = PdfPoint(min(rect.x1, max(rect.x0, anchor.x)),
                              rect.y0 if side == "top" else rect.y1)
        available = max(0.0, rect.y0 - anchor.y if side == "top"
                        else anchor.y - rect.y1)
        offset = min(CALLOUT_LEADER_OFFSET, available)
        knee = PdfPoint(attachment.x, attachment.y + (-offset if side == "top" else offset))
    return anchor, knee, attachment


def move_callout_box(rect: PdfRect, line: tuple[PdfPoint, ...], dx: float, dy: float) -> tuple[PdfRect, tuple[PdfPoint, PdfPoint, PdfPoint]]:
    """Translate only a Callout box; its arrow endpoint remains fixed."""
    if len(line) != 3:
        raise ValueError("Callout requires exactly three leader points")
    moved = PdfRect(rect.x0 + dx, rect.y0 + dy, rect.x1 + dx, rect.y1 + dy)
    return moved, callout_line(moved, line[0])
