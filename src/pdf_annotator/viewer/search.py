"""Nonblocking, page-sliced Ctrl+F search state for one document view."""

from dataclasses import dataclass
from typing import Callable

from ..app.document_session import DocumentSession, Lifecycle
from ..domain.search import SearchMatch
from ..domain.serialization import quad_from_payload
from ..worker.protocol import ErrorInfo, Operation


@dataclass(frozen=True, slots=True)
class SearchTicket:
    request_id: int
    document_generation: int
    search_generation: int


class DocumentSearchController:
    """Schedules exactly one low-priority search slice at a time."""

    def __init__(self, session: DocumentSession, changed: Callable[[], None],
                 failed: Callable[[ErrorInfo], None]) -> None:
        self.session = session
        self._changed, self._failed = changed, failed
        self.active = False
        self.pending: SearchTicket | None = None
        self.requested = self.adopted = self.stale_discarded = 0

    @property
    def state(self):
        return self.session.ui.search

    def set_active(self, active: bool) -> None:
        self.active = active and self.session.lifecycle == Lifecycle.READY

    def set_query(self, query: str) -> None:
        if not isinstance(query, str):
            raise ValueError("Search query must be text")
        state = self.state
        state.generation += 1
        state.query = query
        state.results.clear()
        state.current_result = None
        state.next_page = state.normalized_offset = 0
        state.complete = not bool(query)
        self._changed()

    def close(self) -> None:
        self.set_query("")

    def navigate(self, forward: bool) -> SearchMatch | None:
        state = self.state
        if not state.results:
            return None
        current = state.current_result
        if current is None:
            state.current_result = 0
        elif forward and current + 1 < len(state.results):
            state.current_result = current + 1
        elif not forward and current > 0:
            state.current_result = current - 1
        elif state.complete:
            state.current_result = 0 if forward else len(state.results) - 1
        else:
            return None
        self._changed()
        return state.results[state.current_result]

    def tick(self, *, render_pending: bool, visible_raster_ready: bool) -> None:
        self._poll()
        state = self.state
        if (not self.active or self.pending is not None or state.complete or not state.query
                or self.session.worker is None or render_pending or not visible_raster_ready):
            return
        request_id = self.session.worker.submit(Operation.SEARCH_SLICE, {
            "query": state.query, "page_index": state.next_page,
            "normalized_offset": state.normalized_offset,
            "search_generation": state.generation,
        })
        if request_id is not None:
            self.pending = SearchTicket(request_id, self.session.document_generation,
                                        state.generation)
            self.requested += 1

    def _poll(self) -> None:
        ticket, worker = self.pending, self.session.worker
        if ticket is None or worker is None:
            return
        delivery = worker.poll(ticket.request_id)
        if delivery is None:
            if not worker.is_pending(ticket.request_id):
                self.pending = None
            return
        self.pending = None
        with delivery:
            response = delivery.response
            state = self.state
            if (response is None or response.document_id != self.session.document_id
                    or response.document_generation != self.session.document_generation
                    or ticket.document_generation != self.session.document_generation
                    or ticket.search_generation != state.generation):
                self.stale_discarded += 1
                return
            if not response.success:
                if response.error is not None:
                    self._failed(response.error)
                return
            try:
                records = response.payload["matches"]
                matches = tuple(self._match(record) for record in records)
                if any(match.page_index < state.next_page for match in matches):
                    raise ValueError
                state.results.extend(matches)
                if state.current_result is None and state.results:
                    state.current_result = 0
                state.next_page = int(response.payload["next_page"])
                state.normalized_offset = int(response.payload["normalized_offset"])
                state.complete = bool(response.payload["complete"])
                if not 0 <= state.next_page <= self.session.page_count:
                    raise ValueError
                self.adopted += 1
            except (KeyError, TypeError, ValueError):
                self._failed(ErrorInfo("PROTOCOL_ERROR", "Risposta di ricerca non valida."))
                return
        self._changed()

    @staticmethod
    def _match(record: object) -> SearchMatch:
        if not isinstance(record, dict):
            raise ValueError
        expected = {"page_index", "start_char_index", "end_char_index", "quads", "preview"}
        if set(record) != expected or not isinstance(record["quads"], list):
            raise ValueError
        return SearchMatch(record["page_index"], record["start_char_index"],
                           record["end_char_index"],
                           tuple(quad_from_payload(value) for value in record["quads"]),
                           record["preview"])
