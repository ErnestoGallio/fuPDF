"""Qt-thread handoff to the ordinary document annotation service."""

import asyncio
from concurrent.futures import Future
import logging
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QObject, Qt, Signal, Slot

from ..domain.annotation_commands import AnnotationCommand
from ..domain.errors import MutationErrorCode
from ..domain.results import MutationResult, RequestState
from ..services.review_application import DeferredReviewApplication, MutationDelivery
from ..services.review_jobs import ReviewJob, ReviewScheduler
from ..services.review_transport import ReviewTransport
from ..worker.client import WorkerClient
from ..worker.protocol import ErrorInfo
from .document_session import Lifecycle

if TYPE_CHECKING:
    from .document_manager import DocumentManager


class ReviewApplicationBridge(QObject):
    _submission = Signal(object, object, object, object)

    def __init__(self, manager: "DocumentManager") -> None:
        super().__init__(manager)
        self._manager = manager
        self._jobs: dict[str, tuple[ReviewScheduler, ReviewJob]] = {}
        self._closing: list[Future] = []
        self._pending: dict[str, tuple[AnnotationCommand, Future]] = {}
        self._application = DeferredReviewApplication(self.submit)
        self._submission.connect(self._submit_on_qt, Qt.ConnectionType.QueuedConnection)
        manager.annotation_result.connect(self._acknowledge)
        manager.session_changed.connect(self._session_changed)

    def start(self, scheduler: ReviewScheduler, document_id: str, transport: ReviewTransport,
              **options) -> ReviewJob:
        session = self._manager.sessions.get(document_id)
        if (session is None or session.lifecycle != Lifecycle.READY or session.worker is None
                or self._manager.shutting_down or document_id in self._jobs):
            raise ValueError("Review requires an available document session")
        job = scheduler.start(session.worker, transport, application=self._application, **options)
        self._jobs[document_id] = scheduler, job
        return job

    def close(self, document_id: str) -> Future | None:
        entry = self._jobs.pop(document_id, None)
        if entry is None:
            return None
        scheduler, job = entry
        future = scheduler.close(job)
        self._closing.append(future)
        return future

    def drained(self) -> bool:
        waiting = []
        for future in self._closing:
            if not future.done():
                waiting.append(future)
            elif (error := future.exception()) is not None:
                logging.getLogger("pdf_annotator").warning("review cleanup error=%s", type(error).__name__)
        self._closing[:] = waiting
        return not self._jobs and not self._closing and not self._pending

    async def submit(self, client: WorkerClient, command: AnnotationCommand,
                     stopping: Callable[[], bool]) -> MutationDelivery:
        future = Future()
        self._submission.emit(client, command, stopping, future)
        return await asyncio.wrap_future(future)

    @Slot(object, object, object, object)
    def _submit_on_qt(self, client: WorkerClient, command: AnnotationCommand,
                      stopping: Callable[[], bool], future: Future) -> None:
        context = command.context
        session = self._manager.sessions.get(context.document_id)
        if session is None or session.lifecycle != Lifecycle.READY or session.worker is not client:
            future.set_result(MutationDelivery("session_invalid"))
        elif stopping() or self._manager.shutting_down:
            future.set_result(MutationDelivery("cancelled"))
        elif session.saving or session.document_generation != context.document_generation:
            future.set_result(MutationDelivery("deferred"))
        else:
            submission = self._manager.submit_annotation(command)
            if submission.state == RequestState.PENDING:
                self._pending[context.command_id] = command, future
            elif submission.result.error.code == MutationErrorCode.BACKPRESSURE:
                future.set_result(MutationDelivery("deferred"))
            else:
                future.set_result(MutationDelivery("rejected", submission.result))

    @Slot(object)
    def _acknowledge(self, result: MutationResult) -> None:
        pending = self._pending.pop(result.command_id, None)
        if pending is None:
            return
        command, future = pending
        context = command.context
        mismatch = (result.document_id != context.document_id
                    or result.document_generation != context.document_generation)
        if mismatch:
            future.set_result(MutationDelivery("accepted"))
        else:
            future.set_result(MutationDelivery("accepted", result))
        if mismatch or (result.error is not None and result.error.code == MutationErrorCode.PROTOCOL_ERROR):
            session = self._manager.sessions.get(context.document_id)
            if session is not None and session.lifecycle == Lifecycle.READY:
                # A lost success can leave the worker one revision ahead of
                # AnnotationService. Retire the session's mutation path.
                self._manager._fail(session, ErrorInfo("PROTOCOL_ERROR", "Risposta di annotazione incerta."))

    @Slot(str)
    def _session_changed(self, document_id: str) -> None:
        session = self._manager.sessions.get(document_id)
        if session is None or session.lifecycle != Lifecycle.READY:
            self.close(document_id)
