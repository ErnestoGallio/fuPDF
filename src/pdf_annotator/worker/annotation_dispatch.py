"""Authoritative typed mutation dispatch inside one document-owning worker."""

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from contextlib import nullcontext
import logging
from time import perf_counter
from typing import Any

from ..domain.annotation_commands import (AnnotationCommand, Redo, Undo, command_annotation_id)
from ..domain.annotation_models import AnnotationModel
from ..domain.errors import DomainValidationError, MutationError, MutationErrorCode
from ..domain.geometry import PageBounds
from ..domain.results import HistoryDelta, MutationOutcome, MutationResult
from ..domain.serialization import command_from_payload, context_from_payload, result_to_payload, canonical_json
from ..domain.validation import validate_command_target

SEEN_COMMAND_LIMIT = 256
HISTORY_BYTE_BUDGET = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class AppliedMutation:
    """Minimal adapter acknowledgement; inverse data belongs to later milestones."""

    mutated: bool = True
    annotation: AnnotationModel | None = None
    history_delta: HistoryDelta | None = None


MutationAdapter = Callable[[Any, AnnotationCommand], AppliedMutation]


class AnnotationCommandDispatcher:
    def __init__(self, document_id: str, generation: int,
                 bounds_for_page: Callable[[int], PageBounds],
                 adapter: MutationAdapter | None = None) -> None:
        self.document_id = document_id
        self.generation = generation
        self.document_revision = 0
        self._bounds_for_page = bounds_for_page
        self._adapter = adapter
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._history: list[tuple[Any, int]] = []
        self._history_states: list[int] = [0]
        self._history_cursor = 0
        self._next_history_state = 0
        self._history_bytes = 0

    @property
    def seen_count(self) -> int:
        return len(self._seen)

    def rebind_generation(self, generation: int) -> None:
        """Native locators were verified by Save; refresh DTO generation only."""
        self.generation = generation
        refreshed = []
        for entry, size in self._history:
            if entry is not None and hasattr(entry, "before"):
                entry = replace(entry,
                    before=replace(entry.before, document_generation=generation) if entry.before else None,
                    after=replace(entry.after, document_generation=generation) if entry.after else None)
            refreshed.append((entry, size))
        self._history = refreshed

    def _remember(self, command_id: str) -> None:
        self._seen[command_id] = None
        self._seen.move_to_end(command_id)
        while len(self._seen) > SEEN_COMMAND_LIMIT:
            self._seen.popitem(last=False)

    def _failure(self, command_id: str, code: MutationErrorCode,
                 *, outcome: MutationOutcome = MutationOutcome.FAILED) -> MutationResult:
        return MutationResult(command_id, self.document_id, self.generation, outcome,
                              self.document_revision, self.document_revision,
                              error=MutationError.create(code),
                              history_position=self._history_cursor,
                              history_length=len(self._history),
                              history_state_id=self._history_states[self._history_cursor])

    def _success(self, command: AnnotationCommand, annotation: AnnotationModel | None,
                 *, annotation_id=None, refresh_inventory: bool = False,
                 history_delta: HistoryDelta | None = None) -> MutationResult:
        before = self.document_revision
        self.document_revision += 1
        return MutationResult(command.context.command_id, self.document_id, self.generation,
                              MutationOutcome.SUCCESS, before, self.document_revision,
                              annotation_id=(annotation.annotation_id if annotation is not None
                                             else annotation_id if annotation_id is not None
                                             else command_annotation_id(command)),
                              annotation=annotation,
                              history_position=self._history_cursor,
                              history_length=len(self._history),
                              history_state_id=self._history_states[self._history_cursor],
                              refresh_inventory=refresh_inventory,
                              history_delta=history_delta)

    @staticmethod
    def _entry_size(entry: Any) -> int:
        size = getattr(entry, "byte_size", None)
        return size if type(size) is int and size > 0 else 256

    def _push_history(self, entry: Any) -> None:
        if self._history_cursor < len(self._history):
            discarded = self._history[self._history_cursor:]
            self._history_bytes -= sum(size for _entry, size in discarded)
            del self._history[self._history_cursor:]
            del self._history_states[self._history_cursor + 1:]
        size = self._entry_size(entry)
        self._history.append((entry, size))
        self._history_bytes += size
        self._next_history_state += 1
        self._history_states.append(self._next_history_state)
        self._history_cursor += 1
        # Keep a valid current state even for one unusually large annotation:
        # eviction never rejects a confirmed PDF mutation merely because its
        # native appearance payload is large.
        while self._history_bytes > HISTORY_BYTE_BUDGET and len(self._history) > 1:
            _entry, discarded_size = self._history.pop(0)
            self._history_bytes -= discarded_size
            self._history_states.pop(0)
            self._history_cursor -= 1

    def dispatch(self, doc: Any, payload: dict[str, Any], *,
                 finalize: Callable[[MutationResult], MutationResult] | None = None,
                 authorize_review: Callable[[AnnotationCommand], None] | None = None) -> MutationResult:
        """Validate twice-trusted wire data, reject duplicates, then invoke one adapter."""
        try:
            context = context_from_payload(payload)
        except DomainValidationError:
            raise  # no valid command id exists for a typed MutationResult
        try:
            command = command_from_payload(payload)
        except DomainValidationError as exc:
            code = exc.code
            return self._failure(context.command_id, code)
        if (command.context.document_id != self.document_id
                or command.context.document_generation != self.generation):
            return self._failure(command.context.command_id, MutationErrorCode.STALE_GENERATION,
                                 outcome=MutationOutcome.STALE)
        try:
            validate_command_target(command, self._bounds_for_page)
            if command.context.review_snapshot_id is not None:
                if authorize_review is None:
                    raise DomainValidationError(MutationErrorCode.STALE_TEXT_TARGET)
                authorize_review(command)
        except DomainValidationError as exc:
            return self._failure(command.context.command_id, exc.code)
        if command.context.command_id in self._seen:
            return self._failure(command.context.command_id, MutationErrorCode.DUPLICATE_COMMAND)
        # Remember before executing: a retry after an adapter exception cannot
        # accidentally apply a partially completed PDF mutation twice.
        self._remember(command.context.command_id)
        started = perf_counter()
        logger = logging.getLogger("pdf_annotator")
        logger.info("annotation worker_start command=%s document=%s kind=%s source=%s",
                    command.context.command_id, self.document_id, command.kind.value,
                    command.context.source.value)
        before_history = None
        checkpoint = (self.document_revision, self._history.copy(), self._history_states.copy(),
                      self._history_cursor, self._next_history_state, self._history_bytes)

        def execute() -> MutationResult:
            nonlocal before_history
            if isinstance(command, (Undo, Redo)):
                if self._history_cursor == (0 if isinstance(command, Undo) else len(self._history)):
                    raise DomainValidationError(MutationErrorCode.INVALID_COMMAND)
                entry = self._history[self._history_cursor - 1 if isinstance(command, Undo)
                                      else self._history_cursor][0]
                method = "undo_history" if isinstance(command, Undo) else "redo_history"
                action = getattr(self._adapter, method, None)
                if action is None:
                    raise DomainValidationError(MutationErrorCode.UNSUPPORTED_COMMAND)
                annotation = action(doc, entry)
                self._history_cursor += -1 if isinstance(command, Undo) else 1
                state_model = getattr(entry, "before", None) if isinstance(command, Undo) else getattr(entry, "after", None)
                fallback = getattr(entry, "after", None) if isinstance(command, Undo) else getattr(entry, "before", None)
                delta_factory = getattr(self._adapter, "history_delta", None)
                delta = delta_factory(entry, undo=isinstance(command, Undo)) if delta_factory is not None else None
                return self._success(command, annotation,
                    annotation_id=state_model.annotation_id if state_model is not None else
                                  fallback.annotation_id if fallback is not None else None,
                    refresh_inventory=delta is None, history_delta=delta)
            capture = getattr(self._adapter, "history_before", None)
            before_history = capture(doc, command) if capture is not None else None
            applied = self._adapter(doc, command)
            if not isinstance(applied, AppliedMutation) or not applied.mutated:
                raise RuntimeError("Mutation adapter did not confirm a mutation")
            build = getattr(self._adapter, "history_after", None)
            if before_history is not None and build is not None:
                try:
                    entry = build(doc, before_history, applied)
                except TypeError:
                    entry = build(before_history, applied)  # historical focused test seam
                self._push_history(entry)
            else:
                self._push_history(None)
            return self._success(command, applied.annotation, history_delta=applied.history_delta)

        if self._adapter is None:
            result = self._failure(command.context.command_id,
                                   MutationErrorCode.UNSUPPORTED_COMMAND)
        else:
            try:
                entry = None
                if isinstance(command, Undo) and self._history_cursor > 0:
                    entry = self._history[self._history_cursor - 1][0]
                elif isinstance(command, Redo) and self._history_cursor < len(self._history):
                    entry = self._history[self._history_cursor][0]
                transaction = getattr(self._adapter, "atomic_mutation", None)
                guard = transaction(doc, command, entry) if transaction is not None else nullcontext()
                with guard:
                    result = execute()
                    if finalize is not None:
                        result = finalize(result)
                    self._validate_result(result)
            except Exception as exc:
                (self.document_revision, self._history, self._history_states, self._history_cursor,
                 self._next_history_state, self._history_bytes) = checkpoint
                if getattr(doc, "is_closed", False):
                    raise  # rollback failure: the worker must retire this document
                rollback = getattr(self._adapter, "rollback_history_before", None)
                if before_history is not None and rollback is not None:
                    rollback(doc, before_history)
                result = self._failure(command.context.command_id,
                    exc.code if isinstance(exc, DomainValidationError) else MutationErrorCode.INTERNAL_PDF_MUTATION)
        logger.info("annotation worker_done command=%s document=%s kind=%s outcome=%s "
                    "revision_before=%s revision_after=%s duration_ms=%.3f",
                    command.context.command_id, self.document_id, command.kind.value,
                    result.outcome.value, result.revision_before, result.document_revision,
                    (perf_counter() - started) * 1000)
        return result

    @staticmethod
    def _validate_result(result: MutationResult) -> None:
        from .protocol import MAX_CONTROL_BYTES
        # Reserve room for the transport envelope. The production finalizer
        # additionally prepares the exact frame before committing PDF/history.
        if len(canonical_json(result_to_payload(result)).encode("utf-8")) > MAX_CONTROL_BYTES - 1024:
            raise ValueError("Mutation result exceeds IPC limit")
