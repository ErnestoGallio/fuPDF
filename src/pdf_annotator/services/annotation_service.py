"""Source-independent annotation command orchestration.

The service owns application-side validation and request lifecycle only. PDF
encoding and authoritative revision changes remain inside the document worker.
"""

from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
import logging
from time import perf_counter
from typing import Any

from ..domain.annotation_commands import (COMMAND_TYPES, AnnotationCommand,
                                           DeleteAnnotation, MoveInk,
                                           DeleteExternalAnnotation,
                                           MoveFreeText, ResizeFreeText, TransferFreeText,
                                           UpdateFreeText, MoveCallout, ResizeCallout,
                                           MoveCalloutAnchor, TransferCallout, UpdateCallout,
                                           command_annotation_id)
from ..domain.annotation_models import AnnotationOwnership
from ..domain.external_edits import EditExternalAnnotation, ExternalEditKind, CAPABILITY_FOR_EDIT
from ..domain.errors import (DomainValidationError, MutationError,
                             MutationErrorCode)
from ..domain.geometry import PageBounds
from ..domain.kinds import AnnotationKind
from ..domain.results import (CommandSubmission, MutationOutcome,
                              MutationResult, RequestState)
from ..domain.serialization import command_to_payload, result_from_payload
from ..domain.targets import CalloutTarget, InkTarget, RectTarget
from ..domain.validation import validate_command_target
from ..worker.protocol import MAX_PENDING_REQUESTS, Operation, Response

COMPLETED_COMMAND_LIMIT = 256
RESULT_LIMIT = 256


@dataclass(frozen=True, slots=True)
class _PendingCommand:
    command: AnnotationCommand
    request_id: int
    started_at: float


class AnnotationService:
    """Validate, submit and adopt typed mutations without depending on Qt."""

    def __init__(self, session_lookup: Callable[[str], Any | None],
                 result_sink: Callable[[MutationResult], None] | None = None) -> None:
        self._session_lookup = session_lookup
        self._result_sink = result_sink
        self._pending: dict[str, OrderedDict[str, _PendingCommand]] = {}
        self._completed: dict[str, OrderedDict[str, None]] = {}
        self._results: deque[MutationResult] = deque(maxlen=RESULT_LIMIT)

    def has_pending(self, document_id: str) -> bool:
        return bool(self._pending.get(document_id))

    def pending_count(self, document_id: str) -> int:
        return len(self._pending.get(document_id, ()))

    def submit(self, command: AnnotationCommand) -> CommandSubmission:
        if not isinstance(command, COMMAND_TYPES):
            raise TypeError("Expected an annotation command")
        context = command.context
        session = self._session_lookup(context.document_id)
        if session is None:
            return self._reject(command, MutationErrorCode.DOCUMENT_NOT_READY)
        pending = self._pending.setdefault(context.document_id, OrderedDict())
        completed = self._completed.setdefault(context.document_id, OrderedDict())
        if context.command_id in pending or context.command_id in completed:
            return self._reject(command, MutationErrorCode.DUPLICATE_COMMAND)
        if context.document_generation != session.document_generation:
            return self._reject(command, MutationErrorCode.STALE_GENERATION,
                                MutationOutcome.STALE)
        if (getattr(session.lifecycle, "value", None) != "ready" or session.worker is None
                or getattr(session, "saving", False)):
            return self._reject(command, MutationErrorCode.DOCUMENT_NOT_READY)
        try:
            if isinstance(command, EditExternalAnnotation):
                summary = session.annotation_inventory.get(command.logical_id)
                owned_highlight = (command.edit == ExternalEditKind.COLOR and any(
                    model.kind == AnnotationKind.HIGHLIGHT and model.backing is not None
                    and f"native:{model.page_index}:{model.backing.xref}" == command.logical_id
                    for model in session.annotations.values()))
                if not owned_highlight and (summary is None or summary.page_index != command.page_index
                        or (command.edit == ExternalEditKind.COLOR and summary.kind != "highlight")
                        or not getattr(summary.capabilities, CAPABILITY_FOR_EDIT[command.edit])
                        or (command.style is not None and not summary.capabilities.editable_style)):
                    raise DomainValidationError(MutationErrorCode.READ_ONLY_ANNOTATION)
            elif isinstance(command, DeleteExternalAnnotation):
                summary = session.annotation_inventory.get(command.logical_id)
                if (summary is None or summary.page_index != command.page_index
                        or not summary.capabilities.deletable):
                    raise DomainValidationError(MutationErrorCode.READ_ONLY_ANNOTATION)
            elif isinstance(command, (DeleteAnnotation, MoveInk, UpdateFreeText,
                                    MoveFreeText, ResizeFreeText, TransferFreeText,
                                    UpdateCallout, MoveCallout, ResizeCallout,
                                    MoveCalloutAnchor, TransferCallout)):
                model = session.annotations.get(command.annotation_id.value)
                page_index = (command.target.page_index if isinstance(command, (ResizeFreeText, ResizeCallout,
                                                                                MoveCalloutAnchor, UpdateCallout))
                              else command.source_page_index if isinstance(command, (TransferFreeText,
                                                                                      TransferCallout))
                              else command.page_index)
                if model is None or model.page_index != page_index:
                    raise DomainValidationError(MutationErrorCode.ANNOTATION_NOT_FOUND)
                if model.ownership != AnnotationOwnership.CREATED_BY_APP:
                    raise DomainValidationError(MutationErrorCode.READ_ONLY_ANNOTATION)
                if (isinstance(command, MoveInk)
                        and (model.kind != AnnotationKind.INK
                             or not isinstance(model.target, InkTarget))):
                    raise DomainValidationError(MutationErrorCode.READ_ONLY_ANNOTATION)
                if (isinstance(command, (UpdateFreeText, MoveFreeText, ResizeFreeText, TransferFreeText))
                        and (model.kind != AnnotationKind.TEXT_BOX
                             or not isinstance(model.target, RectTarget))):
                    raise DomainValidationError(MutationErrorCode.READ_ONLY_ANNOTATION)
                if (isinstance(command, (UpdateCallout, MoveCallout, ResizeCallout,
                                         MoveCalloutAnchor, TransferCallout))
                        and (model.kind != AnnotationKind.CALLOUT
                             or not isinstance(model.target, CalloutTarget))):
                    raise DomainValidationError(MutationErrorCode.READ_ONLY_ANNOTATION)
            validate_command_target(command, lambda page: self._page_bounds(session, page))
            payload = command_to_payload(command)
        except DomainValidationError as exc:
            return self._reject(command, exc.code)
        except (TypeError, ValueError):
            return self._reject(command, MutationErrorCode.INVALID_COMMAND)
        if len(pending) >= MAX_PENDING_REQUESTS:
            return self._reject(command, MutationErrorCode.BACKPRESSURE)
        try:
            request_id = session.worker.submit(Operation.ANNOTATION_COMMAND, payload)
        except (TypeError, ValueError):
            return self._reject(command, MutationErrorCode.INVALID_COMMAND)
        if request_id is None:
            return self._reject(command, MutationErrorCode.BACKPRESSURE)
        pending[context.command_id] = _PendingCommand(command, request_id, perf_counter())
        session.pending_mutations += 1
        logging.getLogger("pdf_annotator").info(
            "annotation queued command=%s document=%s kind=%s source=%s",
            context.command_id, context.document_id, command.kind.value,
            context.source.value)
        return CommandSubmission(context.command_id, RequestState.PENDING, request_id)

    def poll_document(self, document_id: str) -> MutationResult | None:
        pending = self._pending.get(document_id)
        if not pending:
            return None
        command_id, record = next(iter(pending.items()))
        session = self._session_lookup(document_id)
        if session is None:
            return self._finish_missing(document_id, command_id, record,
                                        MutationErrorCode.CANCELLED,
                                        MutationOutcome.CANCELLED)
        command = record.command
        if command.context.document_generation != session.document_generation:
            return self._finish(session, command_id, record,
                                self._failure(command, session.document_revision,
                                              MutationErrorCode.STALE_GENERATION,
                                              MutationOutcome.STALE))
        worker = session.worker
        if worker is None or worker.failure is not None:
            return self._finish(session, command_id, record,
                                self._failure(command, session.document_revision,
                                              MutationErrorCode.WORKER_FAILURE))
        delivery = worker.poll(record.request_id)
        if delivery is None:
            if worker.is_pending(record.request_id):
                return None
            return self._finish(session, command_id, record,
                                self._failure(command, session.document_revision,
                                              MutationErrorCode.PROTOCOL_ERROR))
        with delivery:
            response = delivery.response
            try:
                result = self._adopt_response(session, record, response)
            except (DomainValidationError, TypeError, ValueError, KeyError):
                result = self._failure(command, session.document_revision,
                                       MutationErrorCode.PROTOCOL_ERROR)
        return self._finish(session, command_id, record, result)

    def fail_document(self, document_id: str,
                      code: MutationErrorCode = MutationErrorCode.WORKER_FAILURE) -> None:
        self._resolve_all(document_id, code, MutationOutcome.FAILED)

    def cancel_document(self, document_id: str) -> None:
        self._resolve_all(document_id, MutationErrorCode.CANCELLED,
                          MutationOutcome.CANCELLED)

    def retire_document(self, document_id: str) -> None:
        """Cancel outstanding work and release all per-session bookkeeping."""
        self.cancel_document(document_id)
        self._pending.pop(document_id, None)
        self._completed.pop(document_id, None)

    def take_results(self) -> tuple[MutationResult, ...]:
        results = tuple(self._results)
        self._results.clear()
        return results

    @staticmethod
    def _page_bounds(session: Any, page_index: int) -> PageBounds:
        if type(page_index) is not int or not 0 <= page_index < len(session.pages):
            raise IndexError(page_index)
        page = session.pages[page_index]
        return PageBounds(page.width_pt, page.height_pt)

    def _adopt_response(self, session: Any, record: _PendingCommand,
                        response: Response | None) -> MutationResult:
        command = record.command
        context = command.context
        if (not isinstance(response, Response)
                or response.document_id != context.document_id
                or response.document_generation != context.document_generation
                or response.request_id != record.request_id
                or response.operation != Operation.ANNOTATION_COMMAND):
            raise ValueError("Mismatched mutation response")
        if not response.success:
            try:
                code = MutationErrorCode(response.error.code) if response.error else MutationErrorCode.PROTOCOL_ERROR
            except ValueError:
                code = MutationErrorCode.PROTOCOL_ERROR
            return self._failure(command, session.document_revision, code)
        result = result_from_payload(response.payload)
        if (result.command_id != context.command_id
                or result.document_id != context.document_id
                or result.document_generation != context.document_generation):
            raise ValueError("Mismatched mutation result")
        if result.success:
            if result.revision_before != session.document_revision:
                raise ValueError("Non-contiguous document revision")
            session.document_revision = result.document_revision
            session.history_position = result.history_position
            session.history_length = result.history_length
            session.history_state_id = result.history_state_id
        elif (result.revision_before != session.document_revision
              or result.document_revision != session.document_revision):
            raise ValueError("Failure changed document revision")
        return result

    def _reject(self, command: AnnotationCommand, code: MutationErrorCode,
                outcome: MutationOutcome = MutationOutcome.FAILED) -> CommandSubmission:
        session = self._session_lookup(command.context.document_id)
        revision = session.document_revision if session is not None else 0
        result = self._failure(command, revision, code, outcome)
        self._record(result)
        return CommandSubmission(command.context.command_id,
                                 self._state(result.outcome), result=result)

    @staticmethod
    def _failure(command: AnnotationCommand, revision: int, code: MutationErrorCode,
                 outcome: MutationOutcome = MutationOutcome.FAILED) -> MutationResult:
        return MutationResult(command.context.command_id, command.context.document_id,
                              command.context.document_generation, outcome,
                              revision, revision,
                              annotation_id=command_annotation_id(command),
                              error=MutationError.create(code))

    def _finish(self, session: Any, command_id: str, record: _PendingCommand,
                result: MutationResult) -> MutationResult:
        self._pending[record.command.context.document_id].pop(command_id, None)
        session.pending_mutations = max(0, session.pending_mutations - 1)
        logging.getLogger("pdf_annotator").info(
            "annotation adopted command=%s document=%s kind=%s source=%s outcome=%s "
            "revision_before=%s revision_after=%s duration_ms=%.3f",
            command_id, result.document_id, record.command.kind.value,
            record.command.context.source.value, result.outcome.value,
            result.revision_before, result.document_revision,
            (perf_counter() - record.started_at) * 1000)
        self._record(result)
        return result

    def _finish_missing(self, document_id: str, command_id: str,
                        record: _PendingCommand, code: MutationErrorCode,
                        outcome: MutationOutcome) -> MutationResult:
        self._pending[document_id].pop(command_id, None)
        result = self._failure(record.command, 0, code, outcome)
        self._record(result)
        return result

    def _resolve_all(self, document_id: str, code: MutationErrorCode,
                     outcome: MutationOutcome) -> None:
        pending = self._pending.get(document_id)
        if not pending:
            return
        session = self._session_lookup(document_id)
        revision = session.document_revision if session is not None else 0
        records = tuple(pending.values())
        pending.clear()
        if session is not None:
            session.pending_mutations = max(0, session.pending_mutations - len(records))
        for record in records:
            self._record(self._failure(record.command, revision, code, outcome))

    def _record(self, result: MutationResult) -> None:
        completed = self._completed.setdefault(result.document_id, OrderedDict())
        completed[result.command_id] = None
        completed.move_to_end(result.command_id)
        while len(completed) > COMPLETED_COMMAND_LIMIT:
            completed.popitem(last=False)
        self._results.append(result)
        if self._result_sink is not None:
            self._result_sink(result)

    @staticmethod
    def _state(outcome: MutationOutcome) -> RequestState:
        return RequestState(outcome.value)
