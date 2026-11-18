"""Chunked immutable text geometry and selection logic in canonical PDF space."""

from dataclasses import dataclass
from math import hypot, isfinite

from .geometry import PdfPoint, PdfQuad, PdfRect
from .identifiers import validate_target_id

# Product chunking targets actual compact-JSON size. The larger values are
# protocol/safety guards, not ordinary page-density limits.
TARGET_TEXT_CHUNK_WIRE_BYTES = 256 * 1024
MAX_TEXT_CHUNK_WIRE_BYTES = 512 * 1024
MAX_TEXT_INDEX_WIRE_BYTES = 1024 * 1024
MAX_TEXT_PAGE_CHUNKS = 4_096
MAX_PATHOLOGICAL_PAGE_CHARACTERS = 250_000
MAX_CHARACTER_TEXT = 16
HIT_TOLERANCE_PT = 3.0


@dataclass(frozen=True, slots=True)
class TextCharacter:
    index: int
    text: str
    quad: PdfQuad
    block_index: int
    line_index: int

    def __post_init__(self) -> None:
        if type(self.index) is not int or self.index < 0:
            raise ValueError("Invalid character index")
        if (not isinstance(self.text, str) or not self.text
                or len(self.text) > MAX_CHARACTER_TEXT or "\x00" in self.text):
            raise ValueError("Invalid character text")
        if not isinstance(self.quad, PdfQuad):
            raise ValueError("Invalid character quad")
        if (type(self.block_index) is not int or self.block_index < 0
                or type(self.line_index) is not int or self.line_index < 0):
            raise ValueError("Invalid text structure identity")


@dataclass(frozen=True, slots=True)
class TextLine:
    line_index: int
    block_index: int
    start: int
    end: int
    direction: tuple[float, float]

    def __post_init__(self) -> None:
        if (type(self.line_index) is not int or self.line_index < 0
                or type(self.block_index) is not int or self.block_index < 0
                or type(self.start) is not int or type(self.end) is not int
                or self.start < 0 or self.end <= self.start):
            raise ValueError("Invalid text line range")
        if (not isinstance(self.direction, tuple) or len(self.direction) != 2
                or any(type(value) not in (int, float) or not isfinite(value)
                       for value in self.direction)
                or hypot(*self.direction) < .5):
            raise ValueError("Invalid text line direction")
        object.__setattr__(self, "direction",
                           (float(self.direction[0]), float(self.direction[1])))


@dataclass(frozen=True, slots=True)
class TextChunkDescriptor:
    chunk_index: int
    chunk_id: str
    start_char_index: int
    end_char_index: int
    start_line_index: int
    end_line_index: int
    bounds: PdfRect
    wire_bytes: int

    def __post_init__(self) -> None:
        if (type(self.chunk_index) is not int or self.chunk_index < 0
                or type(self.start_char_index) is not int
                or type(self.end_char_index) is not int
                or self.start_char_index < 0
                or self.end_char_index < self.start_char_index
                or type(self.start_line_index) is not int
                or type(self.end_line_index) is not int
                or self.start_line_index < 0
                or self.end_line_index < self.start_line_index
                or type(self.wire_bytes) is not int
                or not 0 < self.wire_bytes <= MAX_TEXT_CHUNK_WIRE_BYTES
                or not isinstance(self.bounds, PdfRect)):
            raise ValueError("Invalid text chunk descriptor")
        object.__setattr__(self, "chunk_id", validate_target_id(self.chunk_id))


@dataclass(frozen=True, slots=True)
class TextPageIndex:
    document_id: str
    document_generation: int
    page_index: int
    page_text_id: str
    character_count: int
    line_count: int
    chunks: tuple[TextChunkDescriptor, ...]

    def __post_init__(self) -> None:
        _page_identity(self.document_id, self.document_generation, self.page_index)
        object.__setattr__(self, "page_text_id", validate_target_id(self.page_text_id))
        if (type(self.character_count) is not int or self.character_count < 0
                or self.character_count > MAX_PATHOLOGICAL_PAGE_CHARACTERS
                or type(self.line_count) is not int or self.line_count < 0
                or not isinstance(self.chunks, tuple)
                or len(self.chunks) > MAX_TEXT_PAGE_CHUNKS
                or any(not isinstance(chunk, TextChunkDescriptor)
                       or chunk.chunk_index != index
                       for index, chunk in enumerate(self.chunks))):
            raise ValueError("Invalid page text index")
        expected_char = 0
        for descriptor in self.chunks:
            if descriptor.start_char_index != expected_char:
                raise ValueError("Discontinuous page text index")
            expected_char = descriptor.end_char_index + 1
        if ((self.character_count == 0) != (not self.chunks)
                or expected_char != self.character_count
                or (self.chunks and self.chunks[-1].end_line_index + 1 != self.line_count)):
            raise ValueError("Incomplete page text index")


@dataclass(frozen=True, slots=True)
class TextChunk:
    document_id: str
    document_generation: int
    page_index: int
    page_text_id: str
    chunk_index: int
    chunk_id: str
    characters: tuple[TextCharacter, ...]
    lines: tuple[TextLine, ...]

    def __post_init__(self) -> None:
        _page_identity(self.document_id, self.document_generation, self.page_index)
        object.__setattr__(self, "page_text_id", validate_target_id(self.page_text_id))
        object.__setattr__(self, "chunk_id", validate_target_id(self.chunk_id))
        if (type(self.chunk_index) is not int or self.chunk_index < 0
                or not isinstance(self.characters, tuple) or not self.characters
                or not isinstance(self.lines, tuple) or not self.lines
                or any(not isinstance(char, TextCharacter) for char in self.characters)
                or any(not isinstance(line, TextLine) for line in self.lines)):
            raise ValueError("Invalid text chunk")
        expected = self.characters[0].index
        for char in self.characters:
            if char.index != expected:
                raise ValueError("Discontinuous text chunk characters")
            expected += 1
        expected = self.characters[0].index
        offset = self.characters[0].index
        for line in self.lines:
            if (line.start != expected or line.end > self.characters[-1].index + 1
                    or any(char.line_index != line.line_index
                           or char.block_index != line.block_index
                           for char in self.characters[line.start - offset:line.end - offset])):
                raise ValueError("Inconsistent text chunk line ranges")
            expected = line.end
        if expected != self.characters[-1].index + 1:
            raise ValueError("Text chunk lines do not cover characters")

    @property
    def start_char_index(self) -> int:
        return self.characters[0].index

    @property
    def end_char_index(self) -> int:
        return self.characters[-1].index


@dataclass(frozen=True, slots=True)
class ResolvedTextSelection:
    page_text_id: str
    anchor_index: int
    focus_index: int
    start_index: int
    end_index: int
    text: str
    quads: tuple[PdfQuad, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "page_text_id", validate_target_id(self.page_text_id))
        if any(type(value) is not int or value < 0 for value in (
                self.anchor_index, self.focus_index, self.start_index, self.end_index)):
            raise ValueError("Invalid selection indices")
        if self.start_index > self.end_index or not self.text or not self.quads:
            raise ValueError("Invalid resolved selection")

    @property
    def snapshot_id(self) -> str:
        """Compatibility name used by the source-independent M5 TextTarget."""
        return self.page_text_id


def hit_test(chunk: TextChunk, point: PdfPoint,
             tolerance: float = HIT_TOLERANCE_PT) -> int | None:
    """Return a nearby global character index from one bounded chunk."""
    if not isinstance(chunk, TextChunk) or not isinstance(point, PdfPoint):
        raise ValueError("Invalid hit-test input")
    if type(tolerance) not in (int, float) or not isfinite(tolerance) or tolerance < 0:
        raise ValueError("Invalid hit-test tolerance")
    best: tuple[float, int] | None = None
    for char in chunk.characters:
        distance = _quad_distance(point, char.quad)
        if distance <= tolerance and (best is None or (distance, char.index) < best):
            best = distance, char.index
    return best[1] if best else None


def resolve_chunk_indices(chunk: TextChunk, anchor_index: int,
                          focus_index: int) -> ResolvedTextSelection:
    if (type(anchor_index) is not int or type(focus_index) is not int
            or not chunk.start_char_index <= anchor_index <= chunk.end_char_index
            or not chunk.start_char_index <= focus_index <= chunk.end_char_index):
        raise ValueError("Selection endpoints are not in one chunk")
    lower, upper = sorted((anchor_index, focus_index))
    offset = chunk.start_char_index
    anchor = chunk.characters[anchor_index - offset]
    focus = chunk.characters[focus_index - offset]
    selected = tuple(char for char in chunk.characters[lower - offset:upper - offset + 1]
                     if anchor.block_index != focus.block_index
                     or char.block_index == anchor.block_index)
    return selection_from_characters(chunk.page_text_id, anchor_index, focus_index,
                                     selected, chunk.lines)


def selection_from_characters(page_text_id: str, anchor_index: int, focus_index: int,
                              characters: tuple[TextCharacter, ...],
                              lines: tuple[TextLine, ...]) -> ResolvedTextSelection:
    if not characters:
        raise ValueError("Empty selection")
    grouped: list[list[TextCharacter]] = []
    for char in characters:
        if not grouped or grouped[-1][-1].line_index != char.line_index:
            grouped.append([])
        grouped[-1].append(char)
    directions = {line.line_index: line.direction for line in lines}
    text = "\n".join("".join(char.text for char in group) for group in grouped)
    quads = tuple(_merge_line(group, directions[group[0].line_index])
                  for group in grouped)
    return ResolvedTextSelection(page_text_id, anchor_index, focus_index,
                                 characters[0].index, characters[-1].index, text, quads)


def descriptor_contains(descriptor: TextChunkDescriptor, point: PdfPoint,
                        tolerance: float = HIT_TOLERANCE_PT) -> bool:
    bounds = descriptor.bounds
    return (bounds.x0 - tolerance <= point.x <= bounds.x1 + tolerance
            and bounds.y0 - tolerance <= point.y <= bounds.y1 + tolerance)


def rect_intersects(first: PdfRect, second: PdfRect) -> bool:
    return not (first.x1 < second.x0 or second.x1 < first.x0
                or first.y1 < second.y0 or second.y1 < first.y0)


def _page_identity(document_id: str, generation: int, page_index: int) -> None:
    if not isinstance(document_id, str) or not 0 < len(document_id) <= 128:
        raise ValueError("Invalid page text document identity")
    if type(generation) is not int or generation < 1:
        raise ValueError("Invalid page text generation")
    if type(page_index) is not int or page_index < 0:
        raise ValueError("Invalid page text page")


def _merge_line(chars: list[TextCharacter], direction: tuple[float, float]) -> PdfQuad:
    length = hypot(*direction)
    ux, uy = direction[0] / length, direction[1] / length
    vx, vy = -uy, ux
    points = tuple(point for char in chars for point in char.quad.points)
    along = tuple(point.x * ux + point.y * uy for point in points)
    across = tuple(point.x * vx + point.y * vy for point in points)
    first, last = min(along), max(along)
    top, bottom = min(across), max(across)

    def point(along_value: float, across_value: float) -> PdfPoint:
        x = along_value * ux + across_value * vx
        y = along_value * uy + across_value * vy
        return PdfPoint(0.0 if -1e-9 < x < 0 else x,
                        0.0 if -1e-9 < y < 0 else y)

    return PdfQuad(point(first, top), point(last, top),
                   point(first, bottom), point(last, bottom))


def _quad_distance(point: PdfPoint, quad: PdfQuad) -> float:
    polygon = (quad.upper_left, quad.upper_right, quad.lower_right, quad.lower_left)
    sign = None
    inside = True
    for first, second in zip(polygon, polygon[1:] + polygon[:1]):
        cross = ((second.x - first.x) * (point.y - first.y)
                 - (second.y - first.y) * (point.x - first.x))
        if abs(cross) <= 1e-9:
            continue
        current = cross > 0
        if sign is None:
            sign = current
        elif current != sign:
            inside = False
            break
    if inside:
        return 0.0
    return min(_segment_distance(point, first, second)
               for first, second in zip(polygon, polygon[1:] + polygon[:1]))


def _segment_distance(point: PdfPoint, first: PdfPoint, second: PdfPoint) -> float:
    dx, dy = second.x - first.x, second.y - first.y
    length2 = dx * dx + dy * dy
    if length2 <= 1e-12:
        return hypot(point.x - first.x, point.y - first.y)
    t = max(0.0, min(1.0, ((point.x - first.x) * dx
                           + (point.y - first.y) * dy) / length2))
    return hypot(point.x - (first.x + t * dx), point.y - (first.y + t * dy))
