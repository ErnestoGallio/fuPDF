"""Nonblocking progressive text scheduling and per-view selection state."""

from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

from ..app.document_session import DocumentSession, Lifecycle
from ..domain.geometry import PdfPoint, PdfRect
from ..domain.serialization import (point_to_payload, target_from_payload,
                                    text_chunk_from_payload,
                                    text_page_index_from_payload)
from ..domain.targets import BoundaryAffinity, TextInsertionTarget, TextRangeTarget
from ..domain.text import (ResolvedTextSelection, TextCharacter, TextChunk, TextPageIndex,
                           descriptor_contains, hit_test, rect_intersects,
                           resolve_chunk_indices)
from ..services.text_cache import TextSnapshotCache
from ..worker.protocol import ErrorInfo, Operation

PREFETCH_CHUNKS_PER_VISIBLE_PAGE = 2
NEGATIVE_PAGE_LIMIT = 64
_PERMANENT_TEXT_ERRORS = {"invalid_geometry", "text_page_too_complex"}


@dataclass(frozen=True, slots=True)
class TextRequestTicket:
    operation: Operation
    request_id: int
    page_index: int | None
    generation: int
    gesture_generation: int
    selection_final: bool = False
    selection_points: tuple[PdfPoint, PdfPoint] | None = None


class TextSelectionController:
    def __init__(self, session: DocumentSession, cache: TextSnapshotCache,
                 changed: Callable[[], None], failed: Callable[[ErrorInfo], None]) -> None:
        self.session = session
        self.cache = cache
        self._changed = changed
        self._failed = failed
        self.active = False
        self.visible: tuple[int, ...] = ()
        self.visible_clips: dict[int, PdfRect] = {}
        self.pending: TextRequestTicket | None = None
        self.provisional: ResolvedTextSelection | TextRangeTarget | None = None
        self.confirmed: TextRangeTarget | None = None
        self.insertion: TextInsertionTarget | None = None
        self.insertion_preview: tuple[PdfPoint, PdfPoint] | None = None
        self.drag_page: int | None = None
        self._anchor_point: PdfPoint | None = None
        self._focus_point: PdfPoint | None = None
        self._queued_selection: tuple[dict, bool] | None = None
        self._last_resolved: tuple[PdfPoint, PdfPoint, TextRangeTarget] | None = None
        self._dragging = False
        self._gesture_generation = 0
        self._generation = session.document_generation
        self._trim_requested = False
        self._negative_pages: OrderedDict[tuple[int, int], ErrorInfo] = OrderedDict()
        self.requested = self.adopted = self.stale_discarded = 0
        self.worker_cache_bytes = self.worker_cache_peak_bytes = 0
        self.worker_extractions = self.worker_chunk_reloads = self.worker_cache_hits = 0

    @property
    def has_selection(self) -> bool:
        return self.provisional is not None or self.confirmed is not None

    @property
    def has_target(self) -> bool:
        """Whether this view currently owns either kind of native-text target."""
        return self.has_selection or self.insertion is not None or self.insertion_preview is not None

    @property
    def visual_selection(self) -> ResolvedTextSelection | TextRangeTarget | None:
        if self.confirmed is not None:
            return self.confirmed
        return self.provisional if self._dragging else None

    @property
    def highlight_ready(self) -> bool:
        return self.confirmed is not None and self._queued_selection is None

    @property
    def needs_interaction(self) -> bool:
        if not self.active:
            return False
        if self.pending is not None or self._queued_selection is not None:
            return True
        for page in self.visible:
            if (self.session.document_generation, page) in self._negative_pages:
                continue
            index = self.page_text(page, record=False)
            if index is None or any(self.chunk(page, item.chunk_index, record=False) is None
                                    for item in self._desired_descriptors(index)):
                return True
        return False

    def page_text(self, page_index: int, *, record: bool = True) -> TextPageIndex | None:
        return self.cache.index(self.session.document_id,
                                self.session.document_generation, page_index, record=record)

    def chunk(self, page_index: int, chunk_index: int,
              *, record: bool = True) -> TextChunk | None:
        index = self.page_text(page_index, record=False)
        if index is None:
            return None
        return self.cache.chunk(self.session.document_id,
                                self.session.document_generation, page_index,
                                index.page_text_id, chunk_index, record=record)

    def loaded_chunks(self, page_index: int) -> tuple[TextChunk, ...]:
        index = self.page_text(page_index, record=False)
        if index is None:
            return ()
        return tuple(chunk for descriptor in index.chunks
                     if (chunk := self.chunk(page_index, descriptor.chunk_index,
                                             record=False)) is not None)

    def set_active(self, active: bool) -> None:
        active = active and self.session.lifecycle == Lifecycle.READY
        if active == self.active:
            return
        self.active = active
        if not active:
            # A caret is a transient pointer interaction, unlike a confirmed M6
            # range selection which belongs to the document's independent view
            # state. Do not let an old insertion target reappear after a tab
            # switch or resolve a response while the view is inactive.
            self.clear_insertion()
            self.cache.remove_document(self.session.document_id)
            self._trim_requested = True
        self._changed()

    def set_visible(self, pages: tuple[int, ...],
                    clips: dict[int, PdfRect] | None = None) -> None:
        self.visible = pages
        self.visible_clips = clips or {}

    def begin_drag(self, page_index: int, point: PdfPoint) -> bool:
        self.clear()
        index = self.page_text(page_index)
        if index is None or not any(descriptor_contains(item, point) for item in index.chunks):
            return False
        self.drag_page = page_index
        self._dragging = True
        self._anchor_point = self._focus_point = point
        local = self._local_hit(page_index, point)
        if local is not None:
            chunk, character_index = local
            self.provisional = resolve_chunk_indices(chunk, character_index, character_index)
        self._queue_resolution(final=False)
        self._changed()
        return True

    def begin_insertion(self, page_index: int, point: PdfPoint) -> bool:
        """Confirm the closest side of an already indexed M6 character.

        The local choice is only a preview.  The worker resolves the same global
        character index and affinity into the bounded M10 confirmation cache.
        """
        self.clear()
        index = self.page_text(page_index)
        if index is None:
            return False
        local = self._local_hit(page_index, point)
        if local is None:
            return False
        chunk, character_index = local
        character = next((item for item in chunk.characters
                          if item.index == character_index), None)
        if character is None:
            return False
        candidates = [
            (_segment_distance_sq(point, character.quad.upper_left, character.quad.lower_left),
             character.index, BoundaryAffinity.BEFORE,
             character.quad.upper_left, character.quad.lower_left),
            (_segment_distance_sq(point, character.quad.upper_right, character.quad.lower_right),
             character.index, BoundaryAffinity.AFTER,
             character.quad.upper_right, character.quad.lower_right),
        ]
        if not candidates:
            return False
        _distance, char_index, affinity, top, bottom = min(
            candidates, key=lambda item: (item[0], item[1], item[2].value))
        self.insertion_preview = top, bottom
        self._queued_selection = ({
            "page_index": page_index,
            "page_text_id": index.page_text_id,
            "char_index": char_index,
            "affinity": affinity.value,
        }, True)
        self.drag_page = page_index
        self._changed()
        return True

    def can_begin_insertion(self, page_index: int, point: PdfPoint) -> bool:
        """Return whether the already-loaded canonical character model has a boundary.

        This deliberately has no side effects: contextual menus must not clear a
        valid selection merely to decide whether Insert Text should be offered.
        The eventual command still goes through :meth:`begin_insertion` and the
        worker-confirmed M10 target path.
        """
        return self.page_text(page_index, record=False) is not None and self._local_hit(
            page_index, point) is not None

    def select_word(self, page_index: int, point: PdfPoint) -> bool:
        """Request a canonical, worker-confirmed range for the clicked word.

        A word is a run of alphanumeric characters. Apostrophes and hyphens are
        retained only when they sit between two alphanumeric characters, so
        ``l'arte`` and ``pre-stampa`` stay useful units while surrounding
        punctuation is excluded. This is based solely on M6 character geometry,
        never screen-space glyph guessing.
        """
        local = self._local_hit(page_index, point)
        if local is None or self.page_text(page_index, record=False) is None:
            return False
        chunk, character_index = local
        characters = tuple(item for item in chunk.characters
                           if item.line_index == next(
                               candidate.line_index for candidate in chunk.characters
                               if candidate.index == character_index))
        clicked = next((item for item in characters if item.index == character_index), None)
        if clicked is None or not _word_alphanumeric(clicked.text):
            return False
        position = next(index for index, item in enumerate(characters)
                        if item.index == character_index)
        start = end = position
        while start and _word_member(characters, start - 1):
            start -= 1
        while end + 1 < len(characters) and _word_member(characters, end + 1):
            end += 1
        # Trim defensively in case an unusual multi-codepoint PDF character
        # appears at a word boundary.
        while start < end and not _word_alphanumeric(characters[start].text):
            start += 1
        while end > start and not _word_alphanumeric(characters[end].text):
            end -= 1
        first, last = characters[start], characters[end]
        self.clear()
        self.drag_page = page_index
        self._anchor_point = _quad_center(first.quad)
        self._focus_point = _quad_center(last.quad)
        self.provisional = resolve_chunk_indices(chunk, first.index, last.index)
        self._dragging = False
        self._queue_resolution(final=True)
        self._changed()
        return True

    def update_drag(self, point: PdfPoint) -> None:
        if not self._dragging or self.drag_page is None or self._anchor_point is None:
            return
        index = self.page_text(self.drag_page, record=False)
        if index is None or not any(descriptor_contains(item, point)
                                    for item in index.chunks):
            # Match the established M6 endpoint behavior: whitespace or the
            # clamped edge of the starting page keeps the last valid focus.
            return
        self._focus_point = point
        anchor = self._local_hit(self.drag_page, self._anchor_point)
        focus = self._local_hit(self.drag_page, point)
        if anchor is not None and focus is not None and anchor[0].chunk_id == focus[0].chunk_id:
            self.provisional = resolve_chunk_indices(anchor[0], anchor[1], focus[1])
        self.confirmed = None
        self._queue_resolution(final=False)
        self._changed()

    def end_drag(self) -> None:
        if (self.drag_page is None or self._anchor_point is None
                or self._focus_point is None):
            self._dragging = False
            self._changed()
            return
        self._dragging = False
        if (self._last_resolved is not None
                and self._last_resolved[:2] == (self._anchor_point, self._focus_point)):
            self.confirmed = self._last_resolved[2]
            self.provisional = None
            self._queued_selection = None
        else:
            self._queue_resolution(final=True)
        self._changed()

    def clear(self) -> None:
        self._gesture_generation += 1
        self.provisional = None
        self.confirmed = None
        self.insertion = None
        self.insertion_preview = None
        self.drag_page = None
        self._anchor_point = self._focus_point = None
        self._queued_selection = None
        self._last_resolved = None
        self._dragging = False
        self._changed()

    def clear_insertion(self) -> None:
        """Discard only the UI-only insertion target and its stale reply."""
        if self.insertion is None and self.insertion_preview is None:
            return
        self._gesture_generation += 1
        self.insertion = None
        self.insertion_preview = None
        if self._queued_selection is not None and "char_index" in self._queued_selection[0]:
            self._queued_selection = None
        if self.drag_page is not None and not self._dragging and self.confirmed is None:
            self.drag_page = None
        self._changed()

    def selected_text(self) -> str | None:
        if self.confirmed is not None:
            return self.confirmed.original_text
        if self.provisional is not None and self._dragging:
            return (self.provisional.original_text
                    if isinstance(self.provisional, TextRangeTarget)
                    else self.provisional.text)
        return None

    def tick(self, *, visible_raster_ready: bool, render_pending: bool) -> None:
        if self.session.document_generation != self._generation:
            self.cache.remove_document(self.session.document_id)
            self._generation = self.session.document_generation
            self._negative_pages.clear()
            self.clear()
        self._poll()
        if self.pending is not None or self.session.worker is None:
            return
        if not self.active:
            if self._trim_requested:
                self._submit(Operation.TEXT_TRIM, {}, None)
                self._trim_requested = False
            return
        if render_pending or not visible_raster_ready:
            return
        if self._queued_selection is not None:
            payload, final = self._queued_selection
            operation = (Operation.TEXT_INSERTION if "char_index" in payload
                         else Operation.TEXT_SELECTION)
            if self._submit(operation, payload, payload["page_index"],
                            selection_final=final,
                            selection_points=(self._anchor_point, self._focus_point)):
                self._queued_selection = None
            return
        for page_index in self.visible:
            if (self.session.document_generation, page_index) in self._negative_pages:
                continue
            index = self.page_text(page_index, record=False)
            if index is None:
                self._submit(Operation.TEXT_PAGE_INDEX, {"page_index": page_index}, page_index)
                return
            for descriptor in self._desired_descriptors(index):
                if self.chunk(page_index, descriptor.chunk_index, record=False) is None:
                    self._submit(Operation.TEXT_CHUNK, {
                        "page_index": page_index, "page_text_id": index.page_text_id,
                        "chunk_index": descriptor.chunk_index}, page_index)
                    return

    def _queue_resolution(self, *, final: bool) -> None:
        if (self.drag_page is None or self._anchor_point is None
                or self._focus_point is None):
            return
        index = self.page_text(self.drag_page, record=False)
        if index is None:
            return
        self._queued_selection = ({
            "page_index": self.drag_page,
            "page_text_id": index.page_text_id,
            "anchor": point_to_payload(self._anchor_point),
            "focus": point_to_payload(self._focus_point),
        }, final)

    def _submit(self, operation: Operation, payload: dict, page_index: int | None,
                *, selection_final: bool = False,
                selection_points: tuple[PdfPoint | None, PdfPoint | None] | None = None) -> bool:
        worker = self.session.worker
        request_id = worker.submit(operation, payload) if worker is not None else None
        if request_id is None:
            return False
        points = None
        if selection_points is not None and all(isinstance(point, PdfPoint)
                                                for point in selection_points):
            points = selection_points[0], selection_points[1]
        self.pending = TextRequestTicket(
            operation, request_id, page_index, self.session.document_generation,
            self._gesture_generation, selection_final, points)
        self.requested += 1
        return True

    def _poll(self) -> None:
        ticket = self.pending
        worker = self.session.worker
        if ticket is None or worker is None:
            return
        delivery = worker.poll(ticket.request_id)
        if delivery is None:
            if not worker.is_pending(ticket.request_id):
                self.pending = None
                if ticket.gesture_generation == self._gesture_generation:
                    self.clear()
                    self._failed(ErrorInfo("PROTOCOL_ERROR", "Risposta testuale non valida."))
            return
        self.pending = None
        with delivery:
            response = delivery.response
            if (response is None or response.document_id != self.session.document_id
                    or response.document_generation != self.session.document_generation
                    or ticket.generation != self.session.document_generation):
                self.stale_discarded += 1
                return
            if ticket.gesture_generation != self._gesture_generation:
                self.stale_discarded += 1
                return
            if (ticket.operation in (Operation.TEXT_PAGE_INDEX, Operation.TEXT_CHUNK)
                    and (ticket.page_index not in self.visible or not self.active)):
                self.stale_discarded += 1
                return
            if not response.success:
                if (ticket.page_index is not None and ticket.operation in (
                        Operation.TEXT_PAGE_INDEX, Operation.TEXT_CHUNK)
                        and response.error is not None
                        and response.error.code in _PERMANENT_TEXT_ERRORS):
                    key = self.session.document_generation, ticket.page_index
                    if key not in self._negative_pages:
                        self._negative_pages[key] = response.error
                        while len(self._negative_pages) > NEGATIVE_PAGE_LIMIT:
                            self._negative_pages.popitem(last=False)
                        self._failed(response.error)
                elif response.error is not None:
                    self._failed(response.error)
                if ticket.operation == Operation.TEXT_SELECTION:
                    self.clear()
                return
            try:
                if ticket.operation == Operation.TEXT_PAGE_INDEX:
                    self.cache.put(text_page_index_from_payload(response.payload["page_text"]))
                    self._adopt_metrics(response.payload["metrics"])
                    self.adopted += 1
                elif ticket.operation == Operation.TEXT_CHUNK:
                    self.cache.put(text_chunk_from_payload(response.payload["chunk"]))
                    self._adopt_metrics(response.payload["metrics"])
                    self.adopted += 1
                elif ticket.operation == Operation.TEXT_SELECTION:
                    status = response.payload["status"]
                    if status in ("no_selection", "stale"):
                        if status == "stale":
                            self.stale_discarded += 1
                        if ticket.selection_final:
                            self.clear()
                        else:
                            self.provisional = None
                        return
                    if status != "confirmed":
                        raise ValueError("Unknown text selection status")
                    target = target_from_payload(response.payload["target"])
                    if (not isinstance(target, TextRangeTarget)
                            or target.page_index != self.drag_page
                            or ticket.selection_points is None):
                        self.stale_discarded += 1
                        return
                    current_points = self._anchor_point, self._focus_point
                    if (not self._dragging
                            and ticket.selection_points != current_points):
                        # A preview that completed after release cannot become
                        # the final selection for newer focus coordinates.
                        self.stale_discarded += 1
                        return
                    self._last_resolved = (*ticket.selection_points, target)
                    if (ticket.selection_final or not self._dragging
                            or ticket.selection_points == current_points):
                        if ticket.selection_final or not self._dragging:
                            self.confirmed = target
                            self.provisional = None
                        else:
                            self.provisional = target
                    self.adopted += 1
                elif ticket.operation == Operation.TEXT_INSERTION:
                    if response.payload["status"] != "confirmed":
                        self.clear()
                        return
                    target = target_from_payload(response.payload["target"])
                    if not isinstance(target, TextInsertionTarget):
                        raise ValueError("Invalid insertion target")
                    self.insertion = target
                    self.insertion_preview = target.boundary_top, target.anchor
                    self.adopted += 1
                elif ticket.operation == Operation.TEXT_TRIM:
                    self.worker_cache_bytes = response.payload["cache_bytes"]
            except (KeyError, TypeError, ValueError):
                self.clear()
                self._failed(ErrorInfo("PROTOCOL_ERROR", "Risposta testuale non valida."))
                return
        self._changed()

    def _adopt_metrics(self, metrics: dict) -> None:
        self.worker_cache_bytes = metrics["cache_bytes"]
        self.worker_cache_peak_bytes = metrics["cache_peak_bytes"]
        self.worker_extractions = metrics["extractions"]
        self.worker_chunk_reloads = metrics["chunk_reloads"]
        self.worker_cache_hits = metrics["cache_hits"]

    def _local_hit(self, page_index: int, point: PdfPoint
                   ) -> tuple[TextChunk, int] | None:
        hits: list[tuple[int, TextChunk]] = []
        for chunk in self.loaded_chunks(page_index):
            found = hit_test(chunk, point)
            if found is not None:
                hits.append((found, chunk))
        if not hits:
            return None
        found, chunk = min(hits, key=lambda item: item[0])
        return chunk, found

    def _desired_descriptors(self, index: TextPageIndex):
        clip = self.visible_clips.get(index.page_index)
        descriptors = list(index.chunks)
        if clip is not None:
            descriptors = [item for item in descriptors if rect_intersects(item.bounds, clip)]
            center_x, center_y = (clip.x0 + clip.x1) / 2, (clip.y0 + clip.y1) / 2
            descriptors.sort(key=lambda item: (
                (max(item.bounds.x0, min(center_x, item.bounds.x1)) - center_x) ** 2
                + (max(item.bounds.y0, min(center_y, item.bounds.y1)) - center_y) ** 2,
                item.chunk_index))
        return tuple(descriptors[:PREFETCH_CHUNKS_PER_VISIBLE_PAGE])


def _segment_distance_sq(point: PdfPoint, start: PdfPoint, end: PdfPoint) -> float:
    dx, dy = end.x - start.x, end.y - start.y
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return (point.x - start.x) ** 2 + (point.y - start.y) ** 2
    ratio = max(0.0, min(1.0, ((point.x - start.x) * dx + (point.y - start.y) * dy)
                           / length_sq))
    x, y = start.x + ratio * dx, start.y + ratio * dy
    return (point.x - x) ** 2 + (point.y - y) ** 2


def _quad_center(quad) -> PdfPoint:
    points = quad.points
    return PdfPoint(sum(point.x for point in points) / 4, sum(point.y for point in points) / 4)


def _word_alphanumeric(text: str) -> bool:
    return bool(text) and text.isalnum()


def _word_member(characters: tuple[TextCharacter, ...], index: int) -> bool:
    """Whether a character belongs to a deterministic linguistic word unit."""
    text = characters[index].text
    if _word_alphanumeric(text):
        return True
    connectors = {"'", "’", "-", "‐", "‑"}
    return (text in connectors and 0 < index < len(characters) - 1
            and _word_alphanumeric(characters[index - 1].text)
            and _word_alphanumeric(characters[index + 1].text))
