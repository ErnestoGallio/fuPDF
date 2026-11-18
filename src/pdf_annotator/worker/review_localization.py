"""Shared M20 localization and page fallback, used by the review worker."""

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import pymupdf

from ..domain.geometry import PdfPoint
from ..domain.identifiers import new_command_id
from ..domain.serialization import point_to_payload, quad_to_payload
from .review_localization_text import PageMatcher, extract_runs, page_bounds


STATUSES = ("text_exact", "text_normalized", "page_fallback", "ambiguous", "unresolved")


@dataclass
class Localization:
    correction_index: int
    operation: str
    page_index: int
    searched_text: str | None
    insert_anchor: dict[str, str] | None
    status: str = "unresolved"
    applicable: bool = False
    candidate_count: int = 0
    context_match_count: int | None = None
    matched_text: str | None = None
    source_range: dict[str, int] | None = None
    quads: list[list[list[float]]] = field(default_factory=list)
    insertion: dict[str, Any] | None = None
    comment_position: list[float] | None = None
    reason: str | None = None


def page_comment_positions(page: pymupdf.Page, count: int, *,
                           avoid_notes: bool = False) -> list[list[float]]:
    width, height = page.rect.width, page.rect.height
    # Score note-sized patches in display space so "top" is visually meaningful.
    size = min(20, width / 4, height / 8)
    rows = max(1, int(height / (4 * size)))
    columns = max(1, int(width / (1.5 * size)))
    occupied = [pymupdf.Rect(bbox) * page.rotation_matrix
                for kind, bbox in page.get_bboxlog() if kind != "ignore-text"]
    if avoid_notes:
        occupied.extend(annot.rect * page.rotation_matrix for annot in page.annots(types=(pymupdf.PDF_ANNOT_TEXT,)))
    patches = []
    for row in range(rows):
        y = row * size + size / 2
        for column in range(columns):
            side_offset = column // 2 * 1.5 * size
            x = width - 1.5 * size - side_offset if column % 2 == 0 else size / 2 + side_offset
            patch = pymupdf.Rect(x, y, x + size, y + size)
            collisions = sum(patch.intersects(rect) for rect in occupied)
            patches.append((collisions, column // 2, row, column, patch.tl))
    patches.sort(key=lambda entry: entry[:4])
    return [list(patches[index % len(patches)][4] * page.derotation_matrix) for index in range(count)]


def anchor_for(item: dict[str, Any]) -> tuple[str | None, str | None, str | None, str | None]:
    before, after = item["context_before"], item["context_after"]
    if item["operation"] == "insert":
        anchor = item["insert_anchor"]
        return anchor["text"], before, after, anchor["side"]
    if item["operation"] == "comment" and item["original_text"] is None:
        if before:
            return before, None, after, None
        return after, None, None, None
    return item["original_text"], before, after, None


def localize_item(index: int, item: dict[str, Any], matcher: PageMatcher | None,
                  page_text_id: str) -> Localization:
    query, before, after, side = anchor_for(item)
    result = Localization(index, item["operation"], item["page_index"], query, item["insert_anchor"])
    if matcher is None:
        result.reason = "page_out_of_range"
        return result
    if not query:
        result.reason = "no_text_reference"
        return result
    status, candidates = matcher.candidates(query)
    result.candidate_count = len(candidates)
    if not candidates:
        result.reason = "target_not_found_in_native_text" if matcher.runs else "no_native_text_on_page"
        return result
    context_required = len(candidates) > 1 or (
        item["operation"] == "comment" and item["original_text"] is None and after is not None)
    if context_required and (before or after):
        corroborated = [match for match in candidates if matcher.context_agrees(match, before, after, side)]
        result.context_match_count = len(corroborated)
        if len(corroborated) != 1:
            result.status = "ambiguous" if len(candidates) > 1 else "unresolved"
            result.reason = "context_not_unique" if corroborated else "context_does_not_confirm_target"
            return result
        candidates = corroborated
    if len(candidates) > 1:
        result.status, result.reason = "ambiguous", "multiple_text_matches"
        return result

    match = candidates[0]
    chars, quads = matcher.geometry(match, page_text_id,
                                    collapsed_whitespace=status == "text_normalized" and side is None)
    if not quads:
        result.reason = "target_geometry_unverified"
        return result
    run = matcher.runs[match.run_index]
    if side:
        edge = chars[0] if side == "before" else chars[-1]
        if edge.line_index in run.unsupported_insertion_lines:
            result.reason = "unsupported_insertion_direction"
            return result
        top, bottom = ((edge.quad.upper_left, edge.quad.lower_left) if side == "before"
                       else (edge.quad.upper_right, edge.quad.lower_right))
        result.insertion = {"side": side, "point": point_to_payload(bottom), "edge_top": point_to_payload(top)}
    if item["operation"] == "comment":
        point = quads[0].upper_right
        result.comment_position = point_to_payload(point)
    result.status, result.applicable = status, True
    result.matched_text = run.text[match.start:match.end]
    result.source_range = {"run_index": match.run_index, "start": match.start, "end_exclusive": match.end}
    result.quads = [quad_to_payload(quad) for quad in quads]
    return result


def localize_document(document: pymupdf.Document, corrections: list[dict[str, Any]], *,
                      base_only: bool = False) -> list[Localization]:
    by_page: dict[int, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for index, item in enumerate(corrections):
        by_page[item["page_index"]].append((index, item))
    results = []
    for page_index, items in by_page.items():
        page_text_id = new_command_id()  # Used only by the shared in-process selection geometry helper.
        if not 0 <= page_index < document.page_count:
            results.extend(localize_item(index, item, None, page_text_id) for index, item in items)
            continue
        page = document[page_index]
        bounds = page_bounds(page)
        matcher = PageMatcher(extract_runs(page, bounds, base_only=base_only), bounds)
        page_results = [localize_item(index, item, matcher, page_text_id) for index, item in items]
        fallback = [result for result in page_results if result.operation == "comment" and not result.applicable]
        positions = page_comment_positions(page, len(fallback)) if fallback else []
        for result, position in zip(fallback, positions, strict=True):
            if not bounds.contains(PdfPoint(*position)):
                raise ValueError("Posizione di commento fuori pagina")
            result.status, result.applicable, result.comment_position = "page_fallback", True, position
        results.extend(page_results)
    return sorted(results, key=lambda result: result.correction_index)
