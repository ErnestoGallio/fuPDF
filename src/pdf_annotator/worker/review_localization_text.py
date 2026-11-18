"""Native PDF text runs and reversible matching for the M20.2 experiment."""

from dataclasses import dataclass
from math import hypot
import re
from typing import Iterator
import unicodedata

import pymupdf

from pdf_annotator.domain.geometry import PageBounds, PdfPoint, PdfQuad
from pdf_annotator.domain.text import TextCharacter, TextLine, selection_from_characters


EXTRACTION_FLAGS = pymupdf.TEXTFLAGS_RAWDICT & ~(
    pymupdf.TEXT_PRESERVE_IMAGES | pymupdf.TEXT_CID_FOR_UNKNOWN_UNICODE
)
APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'"})
LIGATURES = dict(zip("ﬀﬁﬂﬃﬄﬅﬆ", ("ff", "fi", "fl", "ffi", "ffl", "st", "st")))
LINE_HYPHEN = re.compile(r"(?<=[^\W\d_])[-\u00ad][^\S\n]*\n[^\S\n]*(?=[^\W\d_])")
SEPARATOR = "\ufffd"


@dataclass(frozen=True)
class TextRun:
    text: str
    glyphs: tuple[TextCharacter | None, ...]
    lines: tuple[TextLine, ...]
    unsupported_insertion_lines: frozenset[int]
    code_font: bool


@dataclass(frozen=True)
class Match:
    run_index: int
    start: int
    end: int  # exclusive index in the original extracted run


@dataclass(frozen=True)
class TextView:
    text: str
    source_spans: tuple[tuple[int, int], ...]


def page_bounds(page: pymupdf.Page) -> PageBounds:
    width, height = page.rect.width, page.rect.height
    return PageBounds(height, width) if page.rotation in (90, 270) else PageBounds(width, height)


def character_quad(direction: tuple[float, float], span: dict, record: dict,
                   bounds: PageBounds) -> PdfQuad | None:
    try:
        raw_quad = pymupdf.recover_char_quad(direction, span, record)
        quad = PdfQuad(*(PdfPoint(point.x, point.y) for point in raw_quad))
    except (ValueError, RuntimeError, ZeroDivisionError):
        return None
    return quad if all(bounds.contains(point) for point in quad.points) else None


def adjacent_lines(previous: dict, current: dict) -> bool:
    ux, uy = previous["dir"]
    vx, vy = current["dir"]
    if ux * vx + uy * vy < .999:
        return False

    def projections(line: dict) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = line["bbox"]
        corners = ((x0, y0), (x0, y1), (x1, y0), (x1, y1))
        along = [x * ux + y * uy for x, y in corners]
        across = [x * -uy + y * ux for x, y in corners]
        return min(along), max(along), min(across), max(across)

    a0, a1, b0, b1 = projections(previous)
    c0, c1, d0, d1 = projections(current)
    previous_origin = previous["spans"][0]["chars"][0]["origin"]
    current_origin = current["spans"][0]["chars"][0]["origin"]
    step = -(current_origin[0] - previous_origin[0]) * uy + (current_origin[1] - previous_origin[1]) * ux
    height = max(b1 - b0, d1 - d0)
    # Restrict line joins within a block to overlapping reading lanes and plausible leading.
    return max(a0, c0) < min(a1, c1) and .5 * height <= step <= 1.8 * height


def line_groups(lines: list[dict]) -> Iterator[list[dict]]:
    group: list[dict] = []
    for line in lines:
        if group and not adjacent_lines(group[-1], line):
            yield group
            group = []
        group.append(line)
    if group:
        yield group


def is_mark(char: str) -> bool:
    return unicodedata.category(char).startswith("M")


def build_run(raw_lines: list[dict], block_index: int, bounds: PageBounds) -> TextRun:
    text: list[str] = []
    glyphs: list[TextCharacter | None] = []
    lines: list[TextLine] = []
    unsupported = set()
    code_font = False
    for line_index, raw_line in enumerate(raw_lines):
        if line_index:
            text.append("\n")
            glyphs.append(None)
        start = len(glyphs)
        direction = tuple(raw_line["dir"])
        if raw_line["wmode"]:
            unsupported.add(line_index)
        for span in raw_line["spans"]:
            code_font |= bool(span["flags"] & pymupdf.TEXT_FONT_MONOSPACED)
            for record in span["chars"]:
                char = record["c"]
                if not char:
                    continue
                if any(unicodedata.bidirectional(c) in {"R", "AL", "AN"} for c in char):
                    unsupported.add(line_index)
                quad = character_quad(direction, span, record, bounds)
                glyph = None
                if char != SEPARATOR and (char.isprintable() or char.isspace() or char == "\u00ad"):
                    if quad is not None:
                        glyph = TextCharacter(len(glyphs), char, quad, block_index, line_index)
                    elif len(char) == 1 and is_mark(char) and len(glyphs) > start:
                        # Some fonts encode the combining component with zero advance.
                        glyph = glyphs[-1]
                text.append(char if glyph is not None or char.isspace() else SEPARATOR)
                glyphs.extend([glyph] * len(text[-1]))
        if len(glyphs) > start:
            lines.append(TextLine(line_index, block_index, start, len(glyphs), direction))
    return TextRun("".join(text), tuple(glyphs), tuple(lines), frozenset(unsupported), code_font)


def base_textpage(page: pymupdf.Page, flags: int) -> pymupdf.TextPage:
    # The pinned binding's Page.get_textpage includes annotations. Run only
    # page contents through the same text device in canonical CropBox space,
    # without temporarily changing /Rotate in the live document.
    mupdf = pymupdf.mupdf
    matrix = page.derotation_matrix
    text = mupdf.FzStextPage(mupdf.FzRect(*(page.rect * matrix)))
    device = mupdf.fz_new_stext_device(text, mupdf.FzStextOptions(flags))
    try:
        mupdf.fz_run_page_contents(page.this, device, mupdf.FzMatrix(*matrix), mupdf.FzCookie())
    finally:
        mupdf.fz_close_device(device)
    return pymupdf.TextPage(text)


def extract_runs(page: pymupdf.Page, bounds: PageBounds, *,
                 base_only: bool = False) -> tuple[TextRun, ...]:
    if base_only:
        text = base_textpage(page, EXTRACTION_FLAGS)
        from .text_service import MAX_PATHOLOGICAL_PAGE_CHARACTERS
        if len(text.extractText()) > MAX_PATHOLOGICAL_PAGE_CHARACTERS:
            from ..domain.errors import DomainValidationError, MutationErrorCode
            raise DomainValidationError(MutationErrorCode.TEXT_PAGE_TOO_COMPLEX, page_index=page.number)
        raw = text.extractRAWDICT(sort=False)
    else:
        raw = page.get_text("rawdict", flags=EXTRACTION_FLAGS, sort=False)
    runs = []
    for block_index, block in enumerate(raw["blocks"]):
        if block["type"] != 0:
            continue
        lines = []
        for line in block["lines"]:
            spans = [span for span in line["spans"] if span["chars"]]
            if spans and hypot(*line["dir"]) > .5:
                lines.append({**line, "spans": spans})
        runs.extend(build_run(group, block_index, bounds) for group in line_groups(lines))
    return tuple(runs)


def normalized(text: str, *, dehyphenate: bool = False) -> TextView:
    """Map each output character to the full original whitespace or Unicode cluster."""
    result: list[str] = []
    spans: list[tuple[int, int]] = []
    removed = {match.start(): match.end() for match in LINE_HYPHEN.finditer(text)} if dehyphenate else {}
    start = 0
    while start < len(text):
        if start in removed:
            start = removed[start]
            continue
        end = start + 1
        if text[start].isspace():
            while end < len(text) and text[end].isspace():
                end += 1
            value = " "
        else:
            while end < len(text) and is_mark(text[end]):
                end += 1
            composed = unicodedata.normalize("NFC", text[start:end]).translate(APOSTROPHES)
            value = "".join(LIGATURES.get(c, c) for c in composed)
        result.append(value)
        spans.extend([(start, end)] * len(value))
        start = end
    return TextView("".join(result), tuple(spans))


def word_character(char: str) -> bool:
    return char.isalnum() or char == "_" or is_mark(char)


def occurrences(text: str, query: str) -> Iterator[tuple[int, int]]:
    if not query.strip() or SEPARATOR in query:
        return
    start = text.find(query)
    while start >= 0:
        end = start + len(query)
        embedded_start = start > 0 and word_character(query[0]) and word_character(text[start - 1])
        embedded_end = end < len(text) and word_character(query[-1]) and word_character(text[end])
        if not embedded_start and not embedded_end:
            yield start, end
        start = text.find(query, start + 1)


class PageMatcher:
    def __init__(self, runs: tuple[TextRun, ...], bounds: PageBounds) -> None:
        self.runs = runs
        self.bounds = bounds
        self.views = tuple(normalized(run.text) for run in runs)
        self.dehyphenated = {
            index: normalized(run.text, dehyphenate=True) for index, run in enumerate(runs)
            if not run.code_font and LINE_HYPHEN.search(run.text)
        }

    def candidates(self, query: str) -> tuple[str, list[Match]]:
        exact = [Match(index, start, end) for index, run in enumerate(self.runs)
                 for start, end in occurrences(run.text, query)]
        if exact:
            return "text_exact", exact
        needle = normalized(query).text
        matches: set[Match] = set()
        for index, run in enumerate(self.runs):
            views = [self.views[index]]
            if index in self.dehyphenated:
                views.append(self.dehyphenated[index])
            for view in views:
                for start, end in occurrences(view.text, needle):
                    spans = view.source_spans
                    if (start and spans[start - 1] == spans[start]) or (
                            end < len(spans) and spans[end] == spans[end - 1]):
                        continue
                    matches.add(Match(index, spans[start][0], spans[end - 1][1]))
        return "text_normalized", sorted(matches, key=lambda m: (m.run_index, m.start, m.end))

    def context_agrees(self, match: Match, before: str | None, after: str | None,
                       side: str | None = None) -> bool:
        run = self.runs[match.run_index].text
        start = match.end if side == "after" else match.start
        end = match.start if side == "before" else match.end
        left = normalized(run[:start].rsplit(SEPARATOR, 1)[-1]).text.rstrip()
        right = normalized(run[end:].split(SEPARATOR, 1)[0]).text.lstrip()
        prefix = normalized(before or "").text.strip()
        suffix = normalized(after or "").text.strip()
        if prefix:
            cut = len(left) - len(prefix)
            if not left.endswith(prefix) or (cut > 0 and word_character(left[cut - 1])
                                              and word_character(prefix[0])):
                return False
        if suffix:
            cut = len(suffix)
            if not right.startswith(suffix) or (cut < len(right) and word_character(right[cut])
                                                 and word_character(suffix[-1])):
                return False
        return True

    def geometry(self, match: Match, page_text_id: str, *, collapsed_whitespace: bool
                 ) -> tuple[tuple[TextCharacter, ...], tuple[PdfQuad, ...]]:
        run = self.runs[match.run_index]
        span = run.glyphs[match.start:match.end]
        if not span or span[0] is None or span[-1] is None:
            return (), ()
        if (match.start and run.glyphs[match.start - 1] == span[0]) or (
                match.end < len(run.glyphs) and run.glyphs[match.end] == span[-1]):
            return (), ()
        selected_text = run.text[match.start:match.end]
        # Interline whitespace collapsed by matching is not visible range content.
        layout_spaces = {i for gap in re.finditer(r"[^\S\n]*\n\s*", selected_text)
                         for i in range(gap.start(), gap.end())} if collapsed_whitespace else set()
        characters = tuple(dict.fromkeys(glyph for i, glyph in enumerate(span)
                                         if glyph is not None and i not in layout_spaces))
        if not characters:
            return (), ()
        selection = selection_from_characters(page_text_id, characters[0].index, characters[-1].index,
                                              characters, run.lines)
        if any(not self.bounds.contains(point) for quad in selection.quads for point in quad.points):
            return (), ()
        return characters, selection.quads
