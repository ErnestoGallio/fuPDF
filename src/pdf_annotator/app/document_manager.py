"""GUI-owned session registry; file checks and all worker I/O stay off-thread."""

import logging
from dataclasses import replace
from queue import Empty
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Signal

from ..domain.annotation_commands import (AUTHORED_CREATIONS, AnnotationCommand, CommandContext,
                                          CommandSource, DeleteExternalAnnotation, Redo, Undo)
from ..domain.annotation_inventory import AnnotationDetail
from ..domain.annotation_inventory import (AnnotationCapabilities, AnnotationGeometry,
                                           AnnotationSummary)
from ..domain.annotation_models import AnnotationModel
from ..domain.editorial_metadata import EditorialMetadata
from ..domain.geometry import PdfRect
from ..domain.targets import (CalloutTarget, InkTarget, RectTarget,
                              TextInsertionTarget, TextRangeTarget)
from ..worker.annotation_inventory import summary_from_payload
from ..domain.errors import MutationErrorCode
from ..domain.results import CommandSubmission, HistoryDelta, MutationResult, RequestState
from ..services.annotation_service import AnnotationService
from ..services.text_cache import TextSnapshotCache
from ..util.errors import user_error
from ..util.paths import FileIdentity
from ..worker.client import RasterBudget, WorkerClient
from ..worker.document_service import DocumentSource, GeometryBatch, OpenDocumentResult
from ..worker.protocol import ErrorInfo, Operation, Response, VIEWER_SINGLE_RASTER_BYTES
from ..viewer.render_scheduler import ResidentBudget
from ..viewer.metrics import ViewerMetrics
from .document_session import DocumentSession, Lifecycle
from .file_resolver import FileResolver, MAX_PENDING_OPENS
from .save_coordinator import SaveCoordinator
from .review_application import ReviewApplicationBridge
from .windows_author import manual_annotation_author


class DocumentManager(QObject):
    session_changed = Signal(str)
    session_removed = Signal(str)
    active_changed = Signal(object)  # document id or None
    error_occurred = Signal(str, object)  # document id (empty before resolution), ErrorInfo
    shutdown_finished = Signal()
    session_tick = Signal(str)  # bounded READY polling, consumed only by attached viewers
    annotation_result = Signal(object)  # MutationResult
    inventory_changed = Signal(str)
    history_delta_changed = Signal(str, object)
    annotation_detail_changed = Signal(str, str)
    annotation_detail_failed = Signal(str, str)
    save_finished = Signal(str, bool)
    save_overwrite_required = Signal(str, str)
    document_reopened = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._manual_author = manual_annotation_author()
        self.sessions: dict[str, DocumentSession] = {}
        self.annotation_service = AnnotationService(self.sessions.get,
                                                    self._annotation_result)
        self.review_application = ReviewApplicationBridge(self)
        self.active_id: str | None = None
        self.budget = RasterBudget(max_bytes=2 * VIEWER_SINGLE_RASTER_BYTES,
                                   single_bytes=VIEWER_SINGLE_RASTER_BYTES)
        self.resident_budget = ResidentBudget()
        self.text_cache = TextSnapshotCache()
        self.metrics = ViewerMetrics()
        self.workers_started = 0
        self.shutting_down = False
        self.is_shutdown = False
        self._by_path: dict[str, str] = {}
        self._by_file: dict[tuple[int, int], str] = {}
        self._resolver = FileResolver()
        self.saves = SaveCoordinator(self)
        self.document_reopened.connect(self._reset_annotation_reads)
        self._pending: set[int] = set()
        self._request_serial = 0
        self._cursor = 0
        self._timer = QTimer(self)
        self._timer.setInterval(10)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

    @property
    def pending_open_count(self) -> int:
        return len(self._pending)

    def open_path(self, path: str) -> int | None:
        """Return an open-request token immediately; resolution is asynchronous."""
        if self.shutting_down:
            return None
        if len(self._pending) + len(self.saves.pending) >= MAX_PENDING_OPENS:
            self._report("", user_error("OPEN_BUSY"))
            return None
        self._request_serial += 1
        token = self._request_serial
        self._pending.add(token)
        self._resolver.submit(token, path)
        return token

    def activate(self, document_id: str) -> None:
        session = self.sessions.get(document_id)
        if session is None or session.lifecycle in (Lifecycle.CLOSING, Lifecycle.CLOSED):
            return
        if self.active_id != document_id:
            self.active_id = document_id
            self.active_changed.emit(document_id)

    def notify_state_changed(self, document_id: str) -> None:
        """Presentation hook for future state changes; no mutation commands in M1."""
        if document_id in self.sessions:
            self.session_changed.emit(document_id)

    def submit_annotation(self, command: AnnotationCommand) -> CommandSubmission:
        if (isinstance(command, AUTHORED_CREATIONS) and command.context.source == CommandSource.UI
                and (command.metadata is None or command.metadata.author is None)):
            metadata = replace(command.metadata or EditorialMetadata(), author=self._manual_author)
            command = replace(command, metadata=metadata)
        session = None
        if isinstance(command, DeleteExternalAnnotation):
            session = self.sessions.get(command.context.document_id)
            if session is not None:
                session.external_delete_ids[command.annotation_id.value] = command.logical_id
        submission = self.annotation_service.submit(command)
        # Rejections are synchronous. Do not let a retry inherit stale
        # bookkeeping for a command which never reached the worker.
        if (isinstance(command, DeleteExternalAnnotation) and session is not None
                and submission.state != RequestState.PENDING):
            session.external_delete_ids.pop(command.annotation_id.value, None)
        return submission

    def undo_active(self) -> CommandSubmission | None:
        session = self.sessions.get(self.active_id)
        if session is None or not session.can_undo:
            return None
        command = Undo(CommandContext.create(session.document_id, session.document_generation))
        return self.submit_annotation(command)

    def redo_active(self) -> CommandSubmission | None:
        session = self.sessions.get(self.active_id)
        if session is None or not session.can_redo:
            return None
        return self.submit_annotation(Redo(CommandContext.create(
            session.document_id, session.document_generation)))

    def request_close(self, document_id: str, *, discard_changes: bool = False) -> bool:
        session = self.sessions.get(document_id)
        if session is None or session.lifecycle == Lifecycle.CLOSING:
            return True
        if session.saving:
            self._report(document_id, ErrorInfo("SAVE_BUSY", "Attendi la fine del salvataggio prima di chiudere."))
            return False
        if discard_changes:
            self.annotation_service.cancel_document(document_id)
            session.document_revision = session.saved_revision
            session.annotations.clear()
            self.text_cache.remove_document(document_id)
        if not session.can_close:
            self._report(document_id, user_error("DIRTY_CLOSE_BLOCKED"))
            return False
        session.lifecycle = Lifecycle.CLOSING
        session.expected_request_id = None
        session.expected_operation = None
        self.session_changed.emit(document_id)  # detach UI before worker shutdown
        if session.worker is not None:
            session.worker.close_async()
        if self.active_id == document_id:
            self.active_id = next((key for key, other in self.sessions.items()
                                   if other.lifecycle not in (Lifecycle.CLOSING, Lifecycle.CLOSED)), None)
            self.active_changed.emit(self.active_id)
        return True

    def request_shutdown(self, *, discard_changes: bool = False) -> bool:
        if self.shutting_down:
            return True
        # Preflight all sessions before closing any: a future dirty document can
        # veto application close without already discarding the clean tabs.
        for session in self.sessions.values():
            if session.saving:
                self._report(session.document_id, ErrorInfo("SAVE_BUSY", "Attendi la fine del salvataggio prima di chiudere."))
                return False
            if not discard_changes and not session.can_close:
                self._report(session.document_id, user_error("DIRTY_CLOSE_BLOCKED"))
                return False
        self.shutting_down = True
        self._pending.clear()  # completed identity results can no longer start workers
        self._resolver.close_async()
        for document_id in tuple(self.sessions):
            session = self.sessions[document_id]
            self.request_close(document_id,
                               discard_changes=discard_changes and session.dirty)
        return True

    def _report(self, document_id: str, error: ErrorInfo) -> None:
        logging.getLogger("pdf_annotator").warning("document=%s error=%s", document_id, error.code)
        self.error_occurred.emit(document_id, error)

    def _resolved(self, identity: FileIdentity) -> None:
        # This reservation runs only on the GUI thread and precedes spawn. It
        # covers OPENING/FAILED/CLOSING sessions as well as READY sessions.
        existing = self._by_path.get(identity.path_key)
        if existing is None and identity.file_key is not None:
            existing = self._by_file.get(identity.file_key)
        if existing is not None:
            if self.sessions[existing].lifecycle == Lifecycle.CLOSING:
                self._report(existing, user_error("CLOSING"))
            else:
                self.activate(existing)
            return
        document_id = uuid4().hex
        session = DocumentSession(document_id, identity, None)
        self.sessions[document_id] = session
        self._by_path[identity.path_key] = document_id
        if identity.file_key is not None:
            self._by_file[identity.file_key] = document_id
        self.activate(document_id)
        try:
            session.worker = WorkerClient(document_id, DocumentSource(identity, self.budget.single_bytes), self.budget)
            self.workers_started += 1
            self._submit(session, Operation.OPEN_DOCUMENT)
        except Exception:
            self._fail(session, user_error("WORKER_FAILED"))

    def _submit(self, session: DocumentSession, operation: Operation, payload: dict | None = None) -> None:
        request_id = session.worker.submit(operation, payload)
        if request_id is None:
            raise ValueError("Lifecycle request refused")
        session.expected_request_id, session.expected_operation = request_id, operation

    def _consume(self, session: DocumentSession, response: Response) -> None:
        if (response.document_id != session.document_id
                or response.document_generation != session.document_generation
                or response.request_id != session.expected_request_id
                or response.operation != session.expected_operation):
            raise ValueError("Unexpected lifecycle response")
        if not response.success:
            self._fail(session, response.error)
            return
        session.expected_request_id = None
        session.expected_operation = None
        if response.operation == Operation.OPEN_DOCUMENT:
            metadata = OpenDocumentResult(**response.payload)
            session.page_count, session.format = metadata.page_count, metadata.format
        else:
            batch = GeometryBatch.from_payload(response.payload)
            if batch.start != len(session.pages) or batch.start + len(batch.pages) > session.page_count:
                raise ValueError("Unexpected page range")
            session.pages.extend(batch.pages)
        if len(session.pages) < session.page_count:
            self._submit(session, Operation.PAGE_GEOMETRY, {"start": len(session.pages)})
        else:
            session.lifecycle = Lifecycle.READY
            self.session_changed.emit(session.document_id)

    def start_annotation_inventory(self, document_id: str) -> None:
        """Begin lazy inventory only after a presentation consumer attaches."""
        session = self.sessions.get(document_id)
        if session is not None and session.lifecycle == Lifecycle.READY:
            self._request_inventory_summaries(session)

    def _reset_annotation_reads(self, document_id: str) -> None:
        session = self.sessions.get(document_id)
        if session is not None:
            session.inventory_detail_pending_id = None
            session.inventory_detail_subject = None
            session.inventory_detail_retries = 0
            session.annotation_detail_errors.clear()

    def _request_inventory_summaries(self, session: DocumentSession) -> None:
        if session.saving or session.worker is None or session.inventory_request_id is not None:
            return
        request_id = session.worker.submit(Operation.ANNOTATION_SUMMARIES,
                                           {"cursor": session.inventory_cursor})
        if request_id is not None:
            session.inventory_request_id = request_id
            session.inventory_operation = Operation.ANNOTATION_SUMMARIES

    def request_annotation_detail(self, document_id: str, logical_id: str) -> None:
        session = self.sessions.get(document_id)
        if (session is None or session.saving or session.lifecycle != Lifecycle.READY or session.worker is None
                or logical_id not in session.annotation_inventory):
            return
        if logical_id in session.annotation_details:
            return
        if session.inventory_request_id is not None or not session.inventory_complete:
            if session.inventory_detail_id != logical_id:
                session.inventory_detail_pending_id = logical_id
            return
        session.inventory_detail_pending_id = None
        session.inventory_detail_id = logical_id
        session.inventory_detail_offset = 0
        session.inventory_detail_buffer = ""
        session.inventory_detail_subject = None
        session.inventory_detail_retries = 0
        self._request_annotation_detail(session)

    def _start_pending_detail(self, session: DocumentSession) -> None:
        logical_id = session.inventory_detail_pending_id
        if logical_id is not None and session.inventory_request_id is None and session.inventory_complete:
            session.inventory_detail_pending_id = None
            self.request_annotation_detail(session.document_id, logical_id)

    def _request_annotation_detail(self, session: DocumentSession) -> None:
        if session.worker is None or session.inventory_request_id is not None or session.inventory_detail_id is None:
            return
        request_id = session.worker.submit(Operation.ANNOTATION_DETAIL, {
            "logical_id": session.inventory_detail_id, "offset": session.inventory_detail_offset})
        if request_id is not None:
            session.inventory_request_id = request_id
            session.inventory_operation = Operation.ANNOTATION_DETAIL
        else:
            self._annotation_detail_failed(session)

    def _poll_inventory(self, session: DocumentSession) -> None:
        # The fair polling loop also services lightweight session stand-ins used
        # before a document has opted into the M12 inventory lifecycle.  Inventory
        # state is deliberately optional there: normal render/lifecycle polling
        # must not require M12-only fields.
        request_id = getattr(session, "inventory_request_id", None)
        worker = getattr(session, "worker", None)
        if request_id is None or worker is None:
            return
        delivery = worker.poll(request_id)
        if delivery is None:
            return
        with delivery:
            response = delivery.response
            if (response is None or response.document_id != session.document_id
                    or response.document_generation != session.document_generation
                    or response.request_id != request_id or response.operation != session.inventory_operation):
                session.inventory_request_id = None
                session.inventory_operation = None
                if session.inventory_detail_id is not None:
                    if session.inventory_detail_retries < 1:
                        session.inventory_detail_retries += 1
                        session.inventory_detail_buffer = ""
                        session.inventory_detail_offset = 0
                        self._request_annotation_detail(session)
                    else:
                        self._annotation_detail_failed(session)
                        self._start_pending_detail(session)
                return
            operation = session.inventory_operation
            session.inventory_request_id = None
            session.inventory_operation = None
            if not response.success:
                if operation == Operation.ANNOTATION_DETAIL:
                    self._annotation_detail_failed(session)
                self._start_pending_detail(session)
                return
            changed_during_request = session.inventory_changed_during_request
            session.inventory_changed_during_request = set()
            if (operation == Operation.ANNOTATION_DETAIL
                    and session.inventory_detail_id in changed_during_request):
                # A native edit/history result overtook this detail response.
                # Restart just that detail, never the whole inventory.
                session.inventory_detail_buffer = ""
                session.inventory_detail_offset = 0
                if session.inventory_detail_id in session.annotation_inventory:
                    self._request_annotation_detail(session)
                else:
                    session.inventory_detail_id = None
                return
            if session.inventory_reload_pending:
                # The answer was requested against a pre-history-transition
                # native state. Drop it rather than briefly showing stale
                # sidebar entries, then fetch a fresh cursor-zero snapshot.
                session.inventory_reload_pending = False
                session.annotation_inventory.clear()
                session.annotation_details.clear()
                session.annotation_detail_errors.clear()
                session.inventory_detail_id = None
                session.inventory_detail_buffer = ""
                self._seed_app_inventory(session)
                session.inventory_cursor = 0
                session.inventory_complete = False
                self._request_inventory_summaries(session)
                return
            if (operation == Operation.ANNOTATION_DETAIL
                    and session.inventory_detail_pending_id is not None
                    and session.inventory_detail_pending_id != session.inventory_detail_id):
                # Selection changed while a chunk was in flight. Retire that
                # read and serve the latest requested target on the same channel.
                session.inventory_detail_id = None
                session.inventory_detail_buffer = ""
                self._start_pending_detail(session)
                return
            try:
                if operation == Operation.ANNOTATION_SUMMARIES:
                    records = response.payload["summaries"]
                    if not isinstance(records, list):
                        raise ValueError
                    for record in records:
                        if not isinstance(record, dict):
                            raise ValueError
                        summary = summary_from_payload(record)
                        if summary.logical_id in changed_during_request:
                            continue
                        summary = self._project_native_summary(session, summary)
                        if summary is not None:
                            session.annotation_inventory[summary.logical_id] = summary
                    session.inventory_cursor = int(response.payload["next_cursor"])
                    session.inventory_complete = bool(response.payload["complete"])
                    self.inventory_changed.emit(session.document_id)
                    if not session.inventory_complete:
                        self._request_inventory_summaries(session)
                    else:
                        self._start_pending_detail(session)
                elif operation == Operation.ANNOTATION_DETAIL:
                    logical_id = session.inventory_detail_id
                    if logical_id is None or response.payload.get("logical_id") != logical_id:
                        raise ValueError
                    contents = response.payload["contents"]
                    next_offset = response.payload["next_offset"]
                    complete = response.payload["complete"]
                    if (not isinstance(contents, str) or type(next_offset) is not int
                            or type(complete) is not bool
                            or next_offset != session.inventory_detail_offset + len(contents)
                            or (not complete and not contents)):
                        raise ValueError("Invalid annotation detail chunk")
                    if session.inventory_detail_offset == 0:
                        session.inventory_detail_subject = response.payload.get("subject")
                    session.inventory_detail_buffer += contents
                    session.inventory_detail_offset = next_offset
                    if complete:
                        from ..domain.serialization import rich_text_from_payload, style_from_payload
                        session.annotation_details[logical_id] = AnnotationDetail(
                            logical_id, session.inventory_detail_buffer,
                            response.payload.get("rich_contents") if isinstance(response.payload.get("rich_contents"), str) else None,
                            session.inventory_detail_subject if isinstance(session.inventory_detail_subject, str) else None,
                            rich_text_from_payload(response.payload["edit_rich_text"])
                            if response.payload.get("edit_rich_text") else None,
                            style_from_payload(response.payload["edit_style"])
                            if response.payload.get("edit_style") else None)
                        session.inventory_detail_id = None
                        session.inventory_detail_buffer = ""
                        session.annotation_detail_errors.discard(logical_id)
                        self.annotation_detail_changed.emit(session.document_id, logical_id)
                        self._start_pending_detail(session)
                    else:
                        self._request_annotation_detail(session)
            except (KeyError, TypeError, ValueError):
                self._annotation_detail_failed(session)
                self._start_pending_detail(session)

    def _annotation_detail_failed(self, session: DocumentSession) -> None:
        logical_id = session.inventory_detail_id
        session.inventory_detail_id = None
        session.inventory_detail_buffer = ""
        session.inventory_detail_offset = 0
        if logical_id is not None:
            session.annotation_detail_errors.add(logical_id)
            logging.getLogger(__name__).warning("Annotation detail failed for document %s", session.document_id)
            self.annotation_detail_failed.emit(session.document_id, logical_id)

    def _fail(self, session: DocumentSession, error: ErrorInfo) -> None:
        opening = session.lifecycle == Lifecycle.OPENING
        self.annotation_service.fail_document(session.document_id,
                                              MutationErrorCode.WORKER_FAILURE)
        session.error = error
        session.lifecycle = Lifecycle.FAILED
        self.saves.fail(session)
        session.expected_request_id = None
        self._report(session.document_id, error)
        if opening:
            self.request_close(session.document_id)
        else:
            # Keep an already-open failed tab identifiable and closable.
            self.session_changed.emit(session.document_id)

    def _retire(self, session: DocumentSession) -> None:
        document_id = session.document_id
        self.annotation_service.retire_document(document_id)
        self.text_cache.remove_document(document_id)
        self._by_path.pop(session.identity.path_key, None)
        if session.identity.file_key is not None:
            self._by_file.pop(session.identity.file_key, None)
        session.pages.clear()
        session.annotations.clear()
        session.annotation_inventory.clear()
        session.annotation_details.clear()
        session.external_delete_ids.clear()
        session.pending_mutations = 0
        session.worker = None
        session.lifecycle = Lifecycle.CLOSED
        self.sessions.pop(document_id)
        self.session_removed.emit(document_id)

    def _poll(self) -> None:
        with self.metrics.span("manager_poll_ms"):
            self._poll_once()

    def _poll_once(self) -> None:
        for _ in range(MAX_PENDING_OPENS):
            try:
                resolved = self._resolver.results.get_nowait()
            except Empty:
                break
            if self.saves.resolved(resolved):
                continue
            if resolved.request_id not in self._pending:
                continue
            self._pending.remove(resolved.request_id)
            if resolved.error is not None:
                self._report("", resolved.error)
            else:
                self._resolved(resolved.identity)
        # Bound adoption work per Qt tick even when many documents are opening.
        # The active viewer gets a tick every cycle, even with more than eight
        # open tabs. Remaining lifecycle/drain work keeps bounded round-robin fairness.
        ids = tuple(key for key in self.sessions if key != self.active_id)
        selected = [self.active_id] if self.active_id in self.sessions else []
        slots = 8 - len(selected)
        selected.extend(ids[(self._cursor + offset) % len(ids)]
                        for offset in range(min(slots, len(ids))))
        for document_id in selected:
            session = self.sessions[document_id]
            worker = session.worker
            if session.lifecycle == Lifecycle.CLOSING:
                if worker is None or worker.stopped.is_set():
                    self._retire(session)
                continue
            if worker is not None and worker.failure and session.lifecycle != Lifecycle.FAILED:
                self._fail(session, user_error("WORKER_FAILED"))
                continue
            if worker is not None and session.lifecycle == Lifecycle.OPENING:
                delivery = worker.poll(session.expected_request_id)
                if delivery is not None:
                    with delivery:
                        try:
                            self._consume(session, delivery.response)
                        except (ValueError, TypeError, KeyError):
                            self._fail(session, user_error("PROTOCOL_ERROR"))
            elif worker is not None and session.lifecycle == Lifecycle.READY:
                if self.annotation_service.has_pending(session.document_id):
                    self.annotation_service.poll_document(session.document_id)
                if getattr(session, "saving", False):
                    self.saves.poll(session)
                    continue
                self._poll_inventory(session)
                self.session_tick.emit(session.document_id)
        self._cursor = (self._cursor + slots) % max(1, len(ids))
        reviews_drained = self.review_application.drained()
        if self.shutting_down and not self.sessions and self._resolver.stopped.is_set() and reviews_drained:
            self.is_shutdown = True
            self._timer.stop()
            self.shutdown_finished.emit()

    def _annotation_result(self, result: MutationResult) -> None:
        session = self.sessions.get(result.document_id)
        if result.success and session is not None:
            if result.history_delta is not None:
                self._apply_history_delta(session, result.history_delta)
                self.history_delta_changed.emit(result.document_id, result.history_delta)
            elif result.annotation is not None:
                session.annotations[result.annotation.annotation_id.value] = result.annotation
                summary = self._app_annotation_summary(result.annotation)
                session.annotation_inventory[summary.logical_id] = summary
                session.annotation_details[summary.logical_id] = AnnotationDetail(
                    summary.logical_id, result.annotation.text or "")
                self.inventory_changed.emit(result.document_id)
            elif result.annotation_id is not None:
                logical_id = session.external_delete_ids.pop(result.annotation_id.value,
                                                              result.annotation_id.value)
                session.annotations.pop(result.annotation_id.value, None)
                session.annotation_inventory.pop(logical_id, None)
                session.annotation_details.pop(logical_id, None)
                if session.selected_annotation_id == logical_id:
                    session.selected_annotation_id = None
                self.inventory_changed.emit(result.document_id)
            if result.refresh_inventory:
                # Explicit safety fallback for adapters unable to describe a
                # trustworthy transition delta. Product M14 entries avoid it.
                session.annotation_inventory.clear()
                session.annotation_details.clear()
                session.annotation_detail_errors.clear()
                self._seed_app_inventory(session)
                session.inventory_cursor = 0
                session.inventory_complete = False
                if session.inventory_request_id is None:
                    self._request_inventory_summaries(session)
                else:
                    session.inventory_reload_pending = True
                self.inventory_changed.emit(result.document_id)
        elif session is not None and result.annotation_id is not None:
            session.external_delete_ids.pop(result.annotation_id.value, None)
        self.annotation_result.emit(result)
        if result.document_id in self.sessions:
            self.session_changed.emit(result.document_id)


    def _apply_history_delta(self, session: DocumentSession, delta: HistoryDelta) -> None:
        """Apply one worker-authoritative step without disturbing other entries."""
        if session.inventory_request_id is not None:
            session.inventory_changed_during_request.update(delta.changed_ids)
        removed = set(delta.removed_ids) | set(delta.inventory_removed_ids)
        for logical_id in removed:
            session.annotations.pop(logical_id, None)
            session.annotation_inventory.pop(logical_id, None)
            session.annotation_details.pop(logical_id, None)
        for model in delta.added + delta.updated:
            logical_id = model.annotation_id.value
            session.annotations[logical_id] = model
            summary = self._app_annotation_summary(model)
            session.annotation_inventory[logical_id] = summary
            session.annotation_details[logical_id] = AnnotationDetail(logical_id, model.text or "")
        for summary in delta.inventory_added + delta.inventory_updated:
            summary = self._project_native_summary(session, summary)
            if summary is None:
                continue
            previous = session.annotation_inventory.get(summary.logical_id)
            if previous is not None:
                # Permitted native edits/history do not mutate reply links.
                # Keep their presentation metadata even if the worker's lazy
                # inventory was invalidated by an unrelated app mutation.
                summary = replace(summary, reply=summary.reply or previous.reply,
                                  reply_ids=summary.reply_ids or previous.reply_ids)
            session.annotation_inventory[summary.logical_id] = summary
            # A changed native object can no longer rely on stale detail text.
            if summary.logical_id in {item.logical_id for item in delta.inventory_updated}:
                session.annotation_details.pop(summary.logical_id, None)
        if session.selected_annotation_id in removed:
            session.selected_annotation_id = None

    @staticmethod
    def _app_annotation_summary(model: AnnotationModel) -> AnnotationSummary:
        target = model.target
        if isinstance(target, TextRangeTarget):
            points = tuple(point for quad in target.quads for point in quad.points)
            geometry = AnnotationGeometry(PdfRect(min(point.x for point in points),
                                                   min(point.y for point in points),
                                                   max(point.x for point in points),
                                                   max(point.y for point in points)),
                                          target.quads)
        elif isinstance(target, TextInsertionTarget):
            geometry = AnnotationGeometry(PdfRect(min(target.boundary_top.x, target.anchor.x),
                                                   min(target.boundary_top.y, target.anchor.y),
                                                   max(target.boundary_top.x, target.anchor.x) + .1,
                                                   max(target.boundary_top.y, target.anchor.y) + .1))
        elif isinstance(target, InkTarget):
            points = tuple(point for path in target.paths for point in path)
            geometry = AnnotationGeometry(PdfRect(min(point.x for point in points),
                                                   min(point.y for point in points),
                                                   max(point.x for point in points),
                                                   max(point.y for point in points)), paths=target.paths)
        elif isinstance(target, CalloutTarget):
            geometry = AnnotationGeometry(target.text_rect, callout_line=(target.anchor, target.knee))
        elif isinstance(target, RectTarget):
            geometry = AnnotationGeometry(target.rect)
        else:  # Annotation command validation makes this unreachable.
            raise ValueError("Unsupported app annotation target")
        return AnnotationSummary(model.annotation_id.value, model.page_index, model.kind.value,
                                 model.kind.value, geometry, (model.text or "")[:240],
                                 model.metadata.author if model.metadata else None, None,
                                 None, {}, AnnotationCapabilities(selectable=True, deletable=True,
                                 movable=model.kind.value in ("ink", "text_box", "callout"),
                                 resizable=model.kind.value in ("text_box", "callout"),
                                 editable_text=model.kind.value in ("text_box", "callout"),
                                 editable_style=model.kind.value in ("text_box", "callout")), (), True,
                                 "app")

    @staticmethod
    def _project_native_summary(session: DocumentSession, summary: AnnotationSummary) -> AnnotationSummary | None:
        """One identity bridge for inventory reads and all mutation/history deltas.

        Native /IRT and /NM remain untouched. Only the presentation parent is
        aliased to the already-present app model, whose target stays authoritative.
        """
        def model_for(logical_id):
            try:
                prefix, page, xref = logical_id.split(":")
                if prefix != "native":
                    return None
                return next((model for model in session.annotations.values()
                    if model.page_index == int(page) and model.backing is not None
                    and model.backing.xref == int(xref)), None)
            except (ValueError, AttributeError):
                return None

        model = model_for(summary.logical_id)
        if model is not None:
            if summary.kind != "sticky_note" and (model.metadata is None or model.metadata.reason is None):
                return None  # Existing app-owned profiles retain their projection.
            summary = replace(summary, logical_id=model.annotation_id.value, provenance="app",
                              geometry=DocumentManager._app_annotation_summary(model).geometry)
        if summary.reply is not None and summary.reply.parent_logical_id is not None:
            parent = model_for(summary.reply.parent_logical_id)
            if parent is not None and parent.metadata is not None and parent.metadata.reason is not None:
                summary = replace(summary, reply=replace(summary.reply,
                    parent_logical_id=parent.annotation_id.value))
        return summary

    def _seed_app_inventory(self, session: DocumentSession) -> None:
        for model in session.annotations.values():
            summary = self._app_annotation_summary(model)
            session.annotation_inventory[summary.logical_id] = summary
            session.annotation_details[summary.logical_id] = AnnotationDetail(
                summary.logical_id, model.text or "")

    @staticmethod
    def _is_app_backing_summary(session: DocumentSession,
                                summary: AnnotationSummary) -> bool:
        """Never let lossy native inventory replace an app-owned rich model."""
        try:
            _prefix, page, xref = summary.logical_id.split(":")
            page_index, backing_xref = int(page), int(xref)
        except (AttributeError, ValueError):
            return False
        return any(model.page_index == page_index and model.backing is not None
                   and model.backing.xref == backing_xref
                   for model in session.annotations.values())
