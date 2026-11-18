"""Shared conservative layout guard for app-owned rich FreeText boxes."""

from .geometry import PageBounds, PdfRect
from .rich_text import RichText
from .styles import FreeTextStyle


TEXT_BOX_CONTENT_PADDING = 3.0


def rich_text_fits(rect: PdfRect, rich_text: RichText, style: FreeTextStyle) -> bool:
    """Return whether every rich-text run has a safe saved FreeText appearance.

    This is deliberately conservative: it is the one layout predicate used by
    both the GUI resize preview and the worker mutation guard.  Keeping it
    pure also avoids making a preview depend on worker timing.
    """
    usable_width = (rect.x1 - rect.x0) - 2 * (style.border_width + TEXT_BOX_CONTENT_PADDING)
    usable_height = (rect.y1 - rect.y0) - 2 * (style.border_width + TEXT_BOX_CONTENT_PADDING)
    if usable_width <= 0 or usable_height <= 0:
        return False
    line_width, line_height, total_height = 0.0, 0.0, 0.0
    for run in rich_text.runs:
        for char in run.text:
            char_width = run.font_size * _font_advance(run.font_family, run.bold, run.italic)
            line_height = max(line_height, run.font_size * 1.28)
            if char == "\n":
                total_height += line_height
                line_width, line_height = 0.0, 0.0
                continue
            if line_width and line_width + char_width > usable_width:
                total_height += line_height
                line_width, line_height = 0.0, run.font_size * 1.28
            if char_width > usable_width:
                return False
            line_width += char_width
    return total_height + line_height <= usable_height


def grow_rect_to_fit(rect: PdfRect, rich_text: RichText, style: FreeTextStyle,
                     bounds: PageBounds) -> PdfRect | None:
    """Expand ``rect`` just enough for ``rich_text``, without ever shrinking it.

    The GUI calls this while the editor is active; the worker still uses
    :func:`rich_text_fits` as its authoritative defensive guard.  Keeping the
    search here makes the two FreeText tools share the exact same conservative
    appearance model.  Height is preferred while retaining the current width;
    only when no height on that width can fit do we widen the box.
    """
    if rich_text_fits(rect, rich_text, style):
        return rect
    if rect.x0 < 0 or rect.y0 < 0 or rect.x1 > bounds.width or rect.y1 > bounds.height:
        return None

    original_width, original_height = rect.x1 - rect.x0, rect.y1 - rect.y0
    if original_width > bounds.width or original_height > bounds.height:
        return None

    def candidate(width: float, height: float) -> PdfRect:
        # Preserve the top-left origin where possible. At a page edge consume
        # the available opposite-side space, so a near-edge editor remains on
        # the page instead of failing merely because it cannot grow right/down.
        extra_width = width - original_width
        grow_right = min(extra_width, bounds.width - rect.x1)
        x1 = min(bounds.width, rect.x1 + grow_right)
        x0 = max(0.0, rect.x0 - (extra_width - grow_right))
        extra_height = height - original_height
        grow_down = min(extra_height, bounds.height - rect.y1)
        y1 = min(bounds.height, rect.y1 + grow_down)
        y0 = max(0.0, rect.y0 - (extra_height - grow_down))
        return PdfRect(x0, y0, x1, y1)

    def minimum_height(width: float) -> PdfRect | None:
        maximum = candidate(width, bounds.height)
        if not rich_text_fits(maximum, rich_text, style):
            return None
        low, high = original_height, bounds.height
        for _ in range(24):
            middle = (low + high) / 2
            if rich_text_fits(candidate(width, middle), rich_text, style):
                high = middle
            else:
                low = middle
        return candidate(width, high)

    same_width = minimum_height(original_width)
    if same_width is not None:
        return same_width
    if not rich_text_fits(candidate(bounds.width, bounds.height), rich_text, style):
        return None
    low, high = original_width, bounds.width
    for _ in range(24):
        middle = (low + high) / 2
        if not rich_text_fits(candidate(middle, bounds.height), rich_text, style):
            low = middle
        else:
            high = middle
    return minimum_height(high)


def _font_advance(family: str, bold: bool = False, italic: bool = False) -> float:
    # Includes a safety margin for glyph substitution in rich-text appearance.
    width = {"Helvetica": .72, "Times": .68, "Courier": .64}[family]
    return width * (1.08 if bold else 1.0) * (1.03 if italic else 1.0)
