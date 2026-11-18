"""Worker-owned progressive text extraction, chunk caching and range resolution."""

from collections import Counter, OrderedDict
from hashlib import sha256
import json
import logging
from math import hypot, isfinite
from typing import Any, Iterator
import unicodedata
from uuid import UUID

from ..domain.errors import DomainValidationError, MutationErrorCode
from ..domain.geometry import PdfPoint, PdfQuad, PdfRect
from ..domain.identifiers import new_command_id
from ..domain.serialization import (point_from_payload, text_chunk_to_payload,
                                    text_page_index_to_payload)
from ..domain.targets import BoundaryAffinity, TextInsertionTarget, TextRangeTarget
from ..domain.text import (MAX_CHARACTER_TEXT, MAX_PATHOLOGICAL_PAGE_CHARACTERS,
                           MAX_TEXT_CHUNK_WIRE_BYTES, MAX_TEXT_INDEX_WIRE_BYTES,
                           MAX_TEXT_PAGE_CHUNKS, TARGET_TEXT_CHUNK_WIRE_BYTES,
                           TextCharacter, TextChunk, TextChunkDescriptor, TextLine,
                           TextPageIndex, descriptor_contains, hit_test,
                           selection_from_characters)
from ..services.text_cache import (TextSnapshotCache, WORKER_TEXT_CACHE_BYTES,
                                   text_object_cost)

CONFIRMED_RANGE_LIMIT = 64
_GEOMETRY_FIELDS = ("ul", "ur", "ll", "lr")


class WorkerTextService:
    def __init__(self, document_id: str, generation: int, document: Any,
                 *, cache_bytes: int = WORKER_TEXT_CACHE_BYTES,
                 chunk_target_bytes: int = TARGET_TEXT_CHUNK_WIRE_BYTES) -> None:
        if (type(chunk_target_bytes) is not int or chunk_target_bytes < 16 * 1024
                or chunk_target_bytes > MAX_TEXT_CHUNK_WIRE_BYTES):
            raise ValueError("Invalid text chunk target")
        self.document_id = document_id
        self.generation = generation
        self.document = document
        self.chunk_target_bytes = chunk_target_bytes
        self.cache = TextSnapshotCache(cache_bytes)
        self.extractions = self.chunk_reloads = self.cache_hits = 0
        self._confirmed: OrderedDict[str, TextRangeTarget | TextInsertionTarget] = OrderedDict()
        self._logged_losses: OrderedDict[int, None] = OrderedDict()

    def page_index(self, page_index: int) -> TextPageIndex:
        self._validate_page(page_index, "page_index")
        cached = self.cache.index(self.document_id, self.generation, page_index, record=False)
        if cached is not None:
            self.cache_hits += 1
            return cached
        try:
            index, _ = self._extract(page_index, wanted_chunk=None, build_index=True)
        except Exception:
            # A failed progressive build must not leave inaccessible partial
            # chunks occupying the bounded cache.
            self.cache.remove_page(self.document_id, self.generation, page_index)
            raise
        if not self.cache.put(index):
            raise self._complex(page_index, "index_exceeds_cache_budget",
                                f"bytes={text_object_cost(index)} budget={self.cache.max_bytes}")
        index_wire = self._wire_bytes(text_page_index_to_payload(index))
        if index_wire > MAX_TEXT_INDEX_WIRE_BYTES:
            raise self._complex(page_index, "index_exceeds_wire_budget",
                                f"bytes={index_wire} budget={MAX_TEXT_INDEX_WIRE_BYTES}")
        self.extractions += 1
        return index

    def chunk(self, page_index: int, page_text_id: str, chunk_index: int) -> TextChunk:
        index = self.page_index(page_index)
        if page_text_id != index.page_text_id:
            raise DomainValidationError(
                MutationErrorCode.STALE_TEXT_TARGET,
                validator="WorkerTextService.chunk", field="page_text_id",
                reason="generation_identity_mismatch", value_category="identity",
                page_index=page_index)
        if type(chunk_index) is not int or not 0 <= chunk_index < len(index.chunks):
            raise DomainValidationError(
                MutationErrorCode.INVALID_COMMAND,
                validator="WorkerTextService.chunk", field="chunk_index",
                reason="outside_page_index", value_category=type(chunk_index).__name__,
                page_index=page_index)
        cached = self.cache.chunk(self.document_id, self.generation, page_index,
                                  page_text_id, chunk_index, record=False)
        if cached is not None:
            self.cache_hits += 1
            return cached
        _, found = self._extract(page_index, wanted_chunk=chunk_index, build_index=False)
        if found is None or found.chunk_id != index.chunks[chunk_index].chunk_id:
            raise DomainValidationError(
                MutationErrorCode.INVALID_GEOMETRY,
                validator="WorkerTextService.chunk", field="chunk",
                reason="nondeterministic_reextraction", value_category="page_text",
                page_index=page_index)
        self.cache.put(found)
        self.chunk_reloads += 1
        return found

    def resolve(self, payload: dict[str, Any]) -> tuple[str, TextRangeTarget | None]:
        if not isinstance(payload, dict) or set(payload) != {
                "page_index", "page_text_id", "anchor", "focus"}:
            raise DomainValidationError(
                MutationErrorCode.INVALID_COMMAND,
                validator="WorkerTextService.resolve", field="payload",
                reason="unexpected_fields", value_category=type(payload).__name__)
        page_index = payload.get("page_index")
        index = self.page_index(page_index)
        if payload.get("page_text_id") != index.page_text_id:
            return "stale", None
        try:
            anchor = point_from_payload(payload["anchor"])
            focus = point_from_payload(payload["focus"])
        except DomainValidationError as exc:
            raise DomainValidationError(
                MutationErrorCode.INVALID_GEOMETRY,
                validator="WorkerTextService.resolve", field="anchor_or_focus",
                reason="invalid_point", value_category="geometry",
                page_index=page_index) from exc
        anchor_index = self._hit(index, anchor)
        focus_index = self._hit(index, focus)
        if anchor_index is None or focus_index is None:
            return "no_selection", None
        return "confirmed", self._confirm_range(index, anchor_index, focus_index)

    def confirm_range(self, page_index: int, page_text_id: str, first: int, last: int) -> TextRangeTarget:
        """Confirm already identified glyphs against this worker's live snapshot."""
        index = self.page_index(page_index)
        if index.page_text_id != page_text_id:
            raise DomainValidationError(MutationErrorCode.STALE_TEXT_TARGET)
        if type(first) is not int or type(last) is not int or not 0 <= first <= last < index.character_count:
            raise DomainValidationError(MutationErrorCode.INVALID_GEOMETRY)
        return self._confirm_range(index, first, last)

    def _confirm_range(self, index: TextPageIndex, anchor_index: int, focus_index: int) -> TextRangeTarget:
        target = self._resolve_indices(index, anchor_index, focus_index)
        self._confirmed[target.target_id] = target
        self._confirmed.move_to_end(target.target_id)
        while len(self._confirmed) > CONFIRMED_RANGE_LIMIT:
            self._confirmed.popitem(last=False)
        return target

    def resolve_insertion(self, payload: dict[str, Any]) -> tuple[str, TextInsertionTarget | None]:
        """Confirm an explicit M6 character side, including either side of line ends.

        The global character index plus affinity removes ambiguity at repeated
        words and discontinuous runs. Geometry comes from that exact character;
        an empty page or a stale index cannot fabricate a boundary.
        """
        if not isinstance(payload, dict) or set(payload) != {
                "page_index", "page_text_id", "char_index", "affinity"}:
            raise DomainValidationError(MutationErrorCode.INVALID_COMMAND)
        index = self.page_index(payload["page_index"])
        if payload["page_text_id"] != index.page_text_id:
            return "stale", None
        position = payload["char_index"]
        if index.character_count == 0:
            return "no_selection", None
        if type(position) is not int or not 0 <= position < index.character_count:
            raise DomainValidationError(MutationErrorCode.INVALID_COMMAND)
        try:
            affinity = BoundaryAffinity(payload["affinity"])
        except (ValueError, TypeError) as exc:
            raise DomainValidationError(MutationErrorCode.INVALID_COMMAND) from exc
        char = self._character(index, position)
        before = affinity == BoundaryAffinity.BEFORE
        target = TextInsertionTarget(
            new_command_id(), index.page_index,
            char.quad.lower_left if before else char.quad.lower_right, affinity,
            "" if before else char.text, char.text if before else "",
            index.page_text_id, position,
            char.quad.upper_left if before else char.quad.upper_right)
        self._confirmed[target.target_id] = target
        while len(self._confirmed) > CONFIRMED_RANGE_LIMIT:
            self._confirmed.popitem(last=False)
        return "confirmed", target

    def require_confirmed(self, target: TextRangeTarget | TextInsertionTarget) -> None:
        confirmed = self._confirmed.get(target.target_id)
        if confirmed is None or confirmed != target:
            raise DomainValidationError(MutationErrorCode.STALE_TEXT_TARGET)

    def trim(self) -> None:
        self.cache.clear()

    def search_slice(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Search one bounded result slice of one page inside the PDF owner.

        The normalized stream is deliberately local to this page.  It maps each
        NFKC/casefold code point back to the M6 character that supplied its
        canonical quad, so no document text or PyMuPDF object crosses IPC.
        """
        if not isinstance(payload, dict) or set(payload) != {
                "query", "page_index", "normalized_offset", "search_generation"}:
            raise DomainValidationError(MutationErrorCode.INVALID_COMMAND)
        query, page_index, offset = (payload["query"], payload["page_index"],
                                     payload["normalized_offset"])
        if (not isinstance(query, str) or not query or type(page_index) is not int
                or type(offset) is not int or offset < 0
                or type(payload["search_generation"]) is not int
                or payload["search_generation"] < 1):
            raise DomainValidationError(MutationErrorCode.INVALID_COMMAND)
        normalized_query = unicodedata.normalize("NFKC", query).casefold()
        if not normalized_query:
            return {"matches": [], "next_page": page_index + 1,
                    "normalized_offset": 0, "complete": page_index + 1 >= self.document.page_count}
        index = self.page_index(page_index)
        if index.character_count == 0:
            return {"matches": [], "next_page": page_index + 1,
                    "normalized_offset": 0, "complete": page_index + 1 >= self.document.page_count}
        characters = tuple(character for chunk in self._chunks_for_range(
            index, 0, index.character_count - 1) for character in chunk.characters)
        normalized_parts: list[str] = []
        mapping: list[int] = []
        by_index = {character.index: character for character in characters}
        for character in characters:
            part = unicodedata.normalize("NFKC", character.text).casefold()
            normalized_parts.append(part)
            mapping.extend((character.index,) * len(part))
        haystack = "".join(normalized_parts)
        matches: list[dict[str, Any]] = []
        cursor = min(offset, len(haystack))
        # This bounds one IPC reply, rather than the number of matches a valid
        # document can have.  The returned normalized cursor resumes the page.
        while len(matches) < 64:
            found = haystack.find(normalized_query, cursor)
            if found < 0:
                break
            end = found + len(normalized_query)
            start_index, end_index = mapping[found], mapping[end - 1]
            selected = tuple(by_index[item] for item in range(start_index, end_index + 1)
                             if item in by_index)
            if selected:
                matches.append({
                    "page_index": page_index,
                    "start_char_index": start_index,
                    "end_char_index": end_index,
                    "quads": [[[point.x, point.y] for point in character.quad.points]
                              for character in selected],
                    "preview": "".join(character.text for character in selected)[:160],
                })
            cursor = end
        more = haystack.find(normalized_query, cursor) >= 0
        return {"matches": matches,
                "next_page": page_index if more else page_index + 1,
                "normalized_offset": cursor if more else 0,
                "complete": not more and page_index + 1 >= self.document.page_count}

    def metrics(self) -> dict[str, int]:
        return {"cache_bytes": self.cache.bytes, "cache_peak_bytes": self.cache.peak_bytes,
                "cache_entries": self.cache.entry_count, "cache_evictions": self.cache.evictions,
                "extractions": self.extractions, "chunk_reloads": self.chunk_reloads,
                "cache_hits": self.cache_hits}

    def _hit(self, index: TextPageIndex, point: PdfPoint) -> int | None:
        hits: list[int] = []
        for descriptor in index.chunks:
            if not descriptor_contains(descriptor, point):
                continue
            found = hit_test(self.chunk(index.page_index, index.page_text_id,
                                        descriptor.chunk_index), point)
            if found is not None:
                hits.append(found)
        return min(hits) if hits else None

    def _resolve_indices(self, index: TextPageIndex, anchor_index: int,
                         focus_index: int) -> TextRangeTarget:
        lower, upper = sorted((anchor_index, focus_index))
        anchor_char = self._character(index, anchor_index)
        focus_char = self._character(index, focus_index)
        same_block = anchor_char.block_index == focus_char.block_index
        text_lines: list[str] = []
        quads: list[PdfQuad] = []
        first_selected = last_selected = None
        current_line: list[TextCharacter] = []
        current_definition: TextLine | None = None

        def finish_line() -> None:
            nonlocal current_line, current_definition
            if not current_line or current_definition is None:
                return
            resolved = selection_from_characters(
                index.page_text_id, anchor_index, focus_index,
                tuple(current_line), (current_definition,))
            text_lines.append(resolved.text)
            quads.extend(resolved.quads)
            current_line, current_definition = [], None

        for chunk in self._chunks_for_range(index, lower, upper):
            definitions = {line.line_index: line for line in chunk.lines}
            for char in chunk.characters:
                if char.index < lower or char.index > upper:
                    continue
                if same_block and char.block_index != anchor_char.block_index:
                    continue
                if current_line and current_line[-1].line_index != char.line_index:
                    finish_line()
                if not current_line:
                    definition = definitions[char.line_index]
                    current_definition = TextLine(definition.line_index,
                                                  definition.block_index,
                                                  char.index, char.index + 1,
                                                  definition.direction)
                else:
                    definition = current_definition
                    current_definition = TextLine(definition.line_index,
                                                  definition.block_index,
                                                  definition.start, char.index + 1,
                                                  definition.direction)
                current_line.append(char)
                first_selected = char.index if first_selected is None else first_selected
                last_selected = char.index
        finish_line()
        if first_selected is None or last_selected is None or not text_lines or not quads:
            raise DomainValidationError(
                MutationErrorCode.INVALID_GEOMETRY,
                validator="WorkerTextService._resolve_indices", field="selection",
                reason="empty_resolved_range", value_category="text_range",
                page_index=index.page_index)
        try:
            return TextRangeTarget(new_command_id(), index.page_index, tuple(quads),
                                   "\n".join(text_lines), index.page_text_id,
                                   first_selected, last_selected)
        except ValueError as exc:
            raise DomainValidationError(
                MutationErrorCode.INVALID_GEOMETRY,
                validator="TextRangeTarget", field="selection",
                reason="resolved_target_exceeds_safety_limit",
                value_category="text_range", page_index=index.page_index) from exc

    def _chunks_for_range(self, index: TextPageIndex, lower: int,
                          upper: int) -> Iterator[TextChunk]:
        descriptors = tuple(item for item in index.chunks
                            if item.end_char_index >= lower
                            and item.start_char_index <= upper)
        cached = [self.cache.chunk(
            self.document_id, self.generation, index.page_index,
            index.page_text_id, item.chunk_index, record=False)
            for item in descriptors]
        if all(chunk is not None for chunk in cached):
            yield from cached
            return
        # One cold RAWDICT extraction services the whole logical range. It does
        # not issue N independent reloads when an eviction removed many chunks.
        wanted = {item.chunk_index for item in descriptors}
        for chunk in self._iter_chunks(index.page_index, index.page_text_id):
            if chunk.chunk_index in wanted:
                self.cache.put(chunk)
                yield chunk
            if chunk.start_char_index > upper:
                break
        self.chunk_reloads += 1

    def _character(self, index: TextPageIndex, character_index: int) -> TextCharacter:
        descriptor = next((item for item in index.chunks
                           if item.start_char_index <= character_index <= item.end_char_index), None)
        if descriptor is None:
            raise ValueError("Character is outside page index")
        chunk = self.chunk(index.page_index, index.page_text_id, descriptor.chunk_index)
        return chunk.characters[character_index - descriptor.start_char_index]

    def _extract(self, page_index: int, *, wanted_chunk: int | None,
                 build_index: bool) -> tuple[TextPageIndex, TextChunk | None]:
        page_text_id = self._page_text_id(page_index)
        descriptors: list[TextChunkDescriptor] = []
        found: TextChunk | None = None
        character_count = line_count = 0
        for chunk in self._iter_chunks(page_index, page_text_id):
            character_count = chunk.end_char_index + 1
            line_count = max(line_count, chunk.lines[-1].line_index + 1)
            wire_bytes = self._wire_bytes(text_chunk_to_payload(chunk))
            if wire_bytes > MAX_TEXT_CHUNK_WIRE_BYTES:
                raise self._complex(page_index, "chunk_exceeds_wire_budget",
                                    f"bytes={wire_bytes} budget={MAX_TEXT_CHUNK_WIRE_BYTES}")
            descriptor = TextChunkDescriptor(
                chunk.chunk_index, chunk.chunk_id, chunk.start_char_index,
                chunk.end_char_index, chunk.lines[0].line_index,
                chunk.lines[-1].line_index, self._bounds(chunk), wire_bytes)
            descriptors.append(descriptor)
            if build_index:
                self.cache.put(chunk)
            if chunk.chunk_index == wanted_chunk:
                found = chunk
                if not build_index:
                    cached = self.cache.index(self.document_id, self.generation,
                                              page_index, record=False)
                    if cached is None:
                        raise ValueError("Missing page index during chunk reload")
                    return cached, found
            if len(descriptors) > MAX_TEXT_PAGE_CHUNKS:
                raise self._complex(page_index, "chunk_count_safety_limit",
                                    f"count>{MAX_TEXT_PAGE_CHUNKS}")
        index = TextPageIndex(self.document_id, self.generation, page_index, page_text_id,
                              character_count, line_count, tuple(descriptors))
        return index, found

    def _iter_chunks(self, page_index: int, page_text_id: str) -> Iterator[TextChunk]:
        chunk_index = 0
        pending: list[tuple[tuple[TextCharacter, ...], TextLine]] = []
        for characters, line in self._extract_lines(page_index):
            for piece in self._line_pieces(page_index, page_text_id, chunk_index,
                                           characters, line):
                trial = self._make_chunk(page_index, page_text_id, chunk_index,
                                         (*pending, piece))
                if pending and self._wire_bytes(text_chunk_to_payload(trial)) > self.chunk_target_bytes:
                    yield self._make_chunk(page_index, page_text_id, chunk_index,
                                           tuple(pending))
                    chunk_index += 1
                    pending = []
                    piece = self._renumber_piece(piece)
                pending.append(piece)
        if pending:
            yield self._make_chunk(page_index, page_text_id, chunk_index, tuple(pending))

    def _line_pieces(self, page_index: int, page_text_id: str, chunk_index: int,
                     characters: tuple[TextCharacter, ...],
                     line: TextLine) -> Iterator[tuple[tuple[TextCharacter, ...], TextLine]]:
        whole = (characters, line)
        whole_trial = self._make_chunk(page_index, page_text_id, chunk_index, (whole,))
        if self._wire_bytes(text_chunk_to_payload(whole_trial)) <= self.chunk_target_bytes:
            yield whole
            return
        offset = 0
        while offset < len(characters):
            low, high, best = 1, len(characters) - offset, 1
            while low <= high:
                size = (low + high) // 2
                subset = characters[offset:offset + size]
                definition = TextLine(line.line_index, line.block_index,
                                      subset[0].index, subset[-1].index + 1,
                                      line.direction)
                trial = self._make_chunk(page_index, page_text_id, chunk_index,
                                         ((subset, definition),))
                if self._wire_bytes(text_chunk_to_payload(trial)) <= self.chunk_target_bytes:
                    best, low = size, size + 1
                else:
                    high = size - 1
            subset = characters[offset:offset + best]
            yield subset, TextLine(line.line_index, line.block_index,
                                   subset[0].index, subset[-1].index + 1,
                                   line.direction)
            offset += best

    @staticmethod
    def _renumber_piece(piece: tuple[tuple[TextCharacter, ...], TextLine]
                        ) -> tuple[tuple[TextCharacter, ...], TextLine]:
        return piece

    def _make_chunk(self, page_index: int, page_text_id: str, chunk_index: int,
                    pieces: tuple[tuple[tuple[TextCharacter, ...], TextLine], ...]) -> TextChunk:
        characters = tuple(char for chars, _ in pieces for char in chars)
        lines = tuple(line for _, line in pieces)
        chunk_id = self._stable_id(
            f"{page_text_id}:{chunk_index}:{characters[0].index}:{characters[-1].index}")
        return TextChunk(self.document_id, self.generation, page_index, page_text_id,
                         chunk_index, chunk_id, characters, lines)

    def _extract_lines(self, page_index: int
                       ) -> Iterator[tuple[tuple[TextCharacter, ...], TextLine]]:
        import pymupdf

        page = self.document.load_page(page_index)
        try:
            flags = pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES
            text_page = page.get_textpage(flags=flags)
            preflight = text_page.extractText(sort=False)
            if (isinstance(preflight, str)
                    and len(preflight) > MAX_PATHOLOGICAL_PAGE_CHARACTERS * 11 // 10):
                raise self._complex(
                    page_index, "pathological_text_preflight_safety_limit",
                    f"text_bytes={len(preflight.encode('utf-8'))}")
            raw = text_page.extractRAWDICT(sort=False)
        except DomainValidationError:
            raise
        except Exception as exc:
            raise DomainValidationError(
                MutationErrorCode.INVALID_GEOMETRY,
                validator="TextPage.extractRAWDICT", field="page_text",
                reason="extraction_failed", value_category=type(exc).__name__,
                page_index=page_index) from exc
        if not isinstance(raw, dict):
            raise DomainValidationError(
                MutationErrorCode.INVALID_GEOMETRY,
                validator="TextPage.extractRAWDICT", field="rawdict",
                reason="malformed_output", value_category=type(raw).__name__,
                page_index=page_index)
        width, height = float(page.rect.width), float(page.rect.height)
        if page.rotation in (90, 270):
            width, height = height, width
        next_character = next_line = 0
        skipped: Counter[str] = Counter()
        for block_index, block in enumerate(raw.get("blocks", ())):
            if block.get("type") != 0:
                continue
            for raw_line in block.get("lines", ()):
                try:
                    direction = self._direction(raw_line.get("dir", (1.0, 0.0)))
                except ValueError:
                    skipped["invalid_line_direction"] += sum(
                        len(span.get("chars", ())) for span in raw_line.get("spans", ()))
                    continue
                characters: list[TextCharacter] = []
                for span in raw_line.get("spans", ()):
                    for record in span.get("chars", ()):
                        text = record.get("c")
                        if not isinstance(text, str) or not text:
                            skipped["missing_text"] += 1
                            continue
                        if "\x00" in text:
                            skipped["control_character"] += 1
                            continue
                        if len(text) > MAX_CHARACTER_TEXT:
                            skipped["oversized_character_text"] += 1
                            continue
                        try:
                            quad = pymupdf.recover_char_quad(direction, span, record)
                            domain_quad, reason = self._quad(quad, width, height)
                        except Exception:
                            domain_quad, reason = None, "quad_recovery_failed"
                        if domain_quad is None:
                            if (reason == "degenerate_geometry" and self._zero_width(record)
                                    and not text.isspace() and characters
                                    and len(characters[-1].text) + len(text) <= MAX_CHARACTER_TEXT):
                                previous = characters[-1]
                                characters[-1] = TextCharacter(
                                    previous.index, previous.text + text, previous.quad,
                                    previous.block_index, previous.line_index)
                                skipped["coalesced_zero_width_component"] += 1
                                continue
                            skipped[reason] += 1
                            continue
                        if next_character >= MAX_PATHOLOGICAL_PAGE_CHARACTERS:
                            raise self._complex(
                                page_index, "pathological_character_safety_limit",
                                f"count>{MAX_PATHOLOGICAL_PAGE_CHARACTERS}")
                        characters.append(TextCharacter(next_character, text, domain_quad,
                                                        block_index, next_line))
                        next_character += 1
                if characters:
                    yield (tuple(characters), TextLine(
                        next_line, block_index, characters[0].index,
                        characters[-1].index + 1, direction))
                    next_line += 1
        self._log_normalization(page_index, skipped)

    def _log_normalization(self, page_index: int, skipped: Counter[str]) -> None:
        coalesced = skipped.pop("coalesced_zero_width_component", 0)
        logger = logging.getLogger("pdf_annotator")
        if coalesced:
            logger.debug(
                "document=%s operation=text_extract page=%s event=normalized_raw_chars "
                "reason=coalesced_zero_width_component count=%s",
                self.document_id, page_index, coalesced)
        lost = sum(skipped.values())
        if not lost or page_index in self._logged_losses:
            return
        reasons = ",".join(f"{key}:{skipped[key]}" for key in sorted(skipped))
        logger.warning(
            "document=%s operation=text_extract page=%s error=filtered_significant_chars "
            "validator=WorkerTextService._extract_lines field=character reason=%s "
            "value_category=count skipped=%s",
            self.document_id, page_index, reasons, lost)
        self._logged_losses[page_index] = None
        while len(self._logged_losses) > 64:
            self._logged_losses.popitem(last=False)

    def _page_text_id(self, page_index: int) -> str:
        return self._stable_id(f"{self.document_id}:{self.generation}:{page_index}")

    @staticmethod
    def _stable_id(value: str) -> str:
        return UUID(bytes=sha256(value.encode("utf-8")).digest()[:16], version=4).hex

    def _validate_page(self, page_index: int, field: str) -> None:
        if type(page_index) is not int or not 0 <= page_index < self.document.page_count:
            raise DomainValidationError(
                MutationErrorCode.PAGE_OUT_OF_RANGE,
                validator="WorkerTextService", field=field,
                reason="outside_document", value_category=type(page_index).__name__,
                page_index=page_index if type(page_index) is int else None)

    @staticmethod
    def _wire_bytes(payload: dict[str, Any]) -> int:
        return len(json.dumps(payload, ensure_ascii=True, allow_nan=False,
                              separators=(",", ":")).encode("utf-8"))

    @staticmethod
    def _bounds(chunk: TextChunk) -> PdfRect:
        points = tuple(point for char in chunk.characters for point in char.quad.points)
        return PdfRect(min(point.x for point in points), min(point.y for point in points),
                       max(point.x for point in points), max(point.y for point in points))

    @staticmethod
    def _direction(value: Any) -> tuple[float, float]:
        if (not isinstance(value, (tuple, list)) or len(value) != 2
                or any(type(item) not in (int, float) or not isfinite(item)
                       for item in value)
                or hypot(float(value[0]), float(value[1])) < .5):
            raise ValueError("Invalid line direction")
        return float(value[0]), float(value[1])

    @staticmethod
    def _quad(quad: Any, width: float,
              height: float) -> tuple[PdfQuad | None, str]:
        try:
            raw_points = tuple((getattr(quad, name).x, getattr(quad, name).y)
                               for name in _GEOMETRY_FIELDS)
        except (AttributeError, TypeError):
            return None, "malformed_geometry"
        values = tuple(value for point in raw_points for value in point)
        if any(type(value) not in (int, float) or not isfinite(value) for value in values):
            return None, "nonfinite_geometry"
        if any(x < 0 or y < 0 or x > width or y > height for x, y in raw_points):
            return None, "outside_cropbox"
        try:
            return PdfQuad(*(PdfPoint(x, y) for x, y in raw_points)), ""
        except ValueError:
            return None, "degenerate_geometry"

    @staticmethod
    def _zero_width(record: dict[str, Any]) -> bool:
        bbox = record.get("bbox")
        return (isinstance(bbox, (tuple, list)) and len(bbox) == 4
                and all(type(value) in (int, float) and isfinite(value) for value in bbox)
                and (abs(bbox[2] - bbox[0]) <= 1e-9
                     or abs(bbox[3] - bbox[1]) <= 1e-9))

    @staticmethod
    def _complex(page_index: int, reason: str, category: str) -> DomainValidationError:
        return DomainValidationError(
            MutationErrorCode.TEXT_PAGE_TOO_COMPLEX,
            validator="WorkerTextService", field="page_text", reason=reason,
            value_category=category, page_index=page_index)
