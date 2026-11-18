"""DTOs for temporary, worker-produced document search results."""

from dataclasses import dataclass

from .geometry import PdfQuad, PdfRect


@dataclass(frozen=True, slots=True)
class SearchMatch:
    """One literal match, expressed in M6's canonical page coordinates."""

    page_index: int
    start_char_index: int
    end_char_index: int
    quads: tuple[PdfQuad, ...]
    preview: str

    def __post_init__(self) -> None:
        if (type(self.page_index) is not int or self.page_index < 0
                or type(self.start_char_index) is not int or self.start_char_index < 0
                or type(self.end_char_index) is not int
                or self.end_char_index < self.start_char_index or not self.quads
                or not isinstance(self.preview, str)):
            raise ValueError("Invalid search match")

    @property
    def bbox(self) -> PdfRect:
        points = tuple(point for quad in self.quads for point in quad.points)
        return PdfRect(min(point.x for point in points), min(point.y for point in points),
                       max(point.x for point in points), max(point.y for point in points))
