"""Qt-owned review sessions, credentials and nonblocking shutdown."""

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Signal

from ..services.review_jobs import ReviewJob, ReviewProgress, ReviewScheduler, ReviewSettings
from ..services.review_contract.metrics import DEFAULT_MODEL
from ..services.review_transport import OpenAIReviewTransport, ReviewTransport
from ..services.review_worker import ReviewWorkerError
from ..util.windows_credentials import CredentialError, WindowsCredentialStore, resolve_key, validate_key
from ..worker.client import WorkerClient
from .document_session import Lifecycle

if TYPE_CHECKING:
    from .document_manager import DocumentManager


@dataclass
class ReviewEntry:
    job: ReviewJob = field(repr=False)
    title: str
    requested_pages: int
    future: Future | None = field(default=None, repr=False)
    progress: ReviewProgress | None = None
    phase: str = "analysis"
    report: dict | None = field(default=None, repr=False)
    stopping: bool = False


@dataclass
class Launch:
    document_id: str
    client: WorkerClient
    settings: dict = field(default_factory=dict, repr=False)
    retry_ids: tuple[str, ...] = ()
    transport: ReviewTransport | None = field(default=None, repr=False)
    cleanup: Future | None = field(default=None, repr=False)


class ReviewController(QObject):
    changed = Signal()
    shutdown_finished = Signal()

    def __init__(self, manager: "DocumentManager", parent: QObject, *, credential_store=None,
                 scheduler_factory=ReviewScheduler, transport_factory=OpenAIReviewTransport) -> None:
        super().__init__(parent)
        self.manager = manager
        self.credential_store = credential_store if credential_store is not None else WindowsCredentialStore()
        self._scheduler_factory = scheduler_factory
        self._transport_factory = transport_factory
        self._scheduler: ReviewScheduler | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="review-credentials")
        self._pending: Future | None = None
        self._pending_kind = ""
        self._launch: Launch | None = None
        self._project = uuid4().hex
        self._closing = False
        self._shutdown: Future | None = None
        self.is_shutdown = False
        self.entries: dict[str, ReviewEntry] = {}
        self.latest_document: str | None = None
        self.credential_state = "unchecked"
        self.credential_error: str | None = None
        self.errors: dict[str, str] = {}
        self.timer = QTimer(self)
        self.timer.setInterval(120)
        self.timer.timeout.connect(self._poll)
        manager.session_changed.connect(self._session_changed)
        manager.session_removed.connect(self._session_removed)

    @property
    def credential_busy(self) -> bool:
        return self._pending is not None

    @property
    def closing(self) -> bool:
        return self._closing

    def _task(self, kind: str, fn, *args) -> None:
        self._pending_kind = kind
        self._pending = self._executor.submit(fn, *args)
        self.credential_error = None
        self._changed()

    def refresh_credentials(self) -> None:
        if not self._closing and not self.credential_busy and self._launch is None:
            self._task("read", lambda: resolve_key(self.credential_store)[1])

    def save_key(self, value: str) -> None:
        if not self._closing and not self.credential_busy and self._launch is None:
            self._task("write", self._change_key, value, self._active_jobs())

    def delete_key(self) -> None:
        if not self._closing and not self.credential_busy and self._launch is None:
            self._task("delete", self._change_key, None, self._active_jobs())

    def _active_jobs(self) -> tuple[ReviewJob, ...]:
        return tuple(entry.job for entry in self.entries.values() if not entry.job.future.done())

    def _change_key(self, value: str | None, active: tuple[ReviewJob, ...]) -> str:
        if value is None:
            self.credential_store.delete()
        else:
            self.credential_store.write(validate_key(value))
        for job in active:
            job.cancel()
        return "vault" if value is not None else resolve_key(self.credential_store)[1]

    def _transport(self) -> tuple[ReviewTransport | None, str]:
        key, source = resolve_key(self.credential_store)
        return (self._transport_factory(key) if key is not None else None), source

    def busy(self, document_id: str) -> bool:
        entry = self.entries.get(document_id)
        return self.preparing(document_id) or bool(entry is not None and not entry.job.future.done())

    def preparing(self, document_id: str) -> bool:
        return self._launch is not None and self._launch.document_id == document_id

    def can_retry(self, document_id: str) -> bool:
        entry = self.entries.get(document_id)
        return bool(entry and entry.job.can_retry and self._ready_worker(document_id) is not None)

    def can_resume_application(self, document_id: str) -> bool:
        entry = self.entries.get(document_id)
        return bool(not self._closing and self._scheduler is not None and entry
                    and entry.job.can_recover_publication and not self.preparing(document_id)
                    and self._ready_worker(document_id) is not None)

    def requested_pages(self, document_id: str) -> int | None:
        launch = self._launch
        if launch is not None and launch.document_id == document_id and launch.settings:
            return launch.settings["last"] - launch.settings["first"] + 1
        entry = self.entries.get(document_id)
        return entry.requested_pages if entry is not None else None

    def _ready_worker(self, document_id: str) -> WorkerClient | None:
        session = self.manager.sessions.get(document_id)
        if session is None or session.lifecycle != Lifecycle.READY or session.saving:
            return None
        return session.worker

    def start(self, document_id: str, *, profile: str, first: int, last: int,
              context: dict[str, str], model: str = DEFAULT_MODEL) -> bool:
        session = self.manager.sessions.get(document_id)
        if session is None or not 0 <= first <= last < session.page_count:
            return False
        try:
            settings = ReviewSettings(profile=profile, model=model)
        except ValueError:
            self.errors[document_id] = "Modello o profilo di revisione non valido."
            self.changed.emit()
            return False
        return self._queue_launch(document_id, settings={
            "settings": settings, "first": first, "last": last, "context": dict(context),
        })

    def retry(self, document_id: str, ids: tuple[str, ...]) -> bool:
        if not ids or not self.can_retry(document_id):
            return False
        return self._queue_launch(document_id, retry_ids=ids)

    def resume_application(self, document_id: str) -> None:
        if self.can_resume_application(document_id):
            self.errors.pop(document_id, None)
            try:
                self._scheduler.resume_application(self.entries[document_id].job)
            except ValueError:
                self.errors[document_id] = "Impossibile riprendere l’applicazione in questa scheda."
            self._changed()

    def _queue_launch(self, document_id: str, **options) -> bool:
        worker = self._ready_worker(document_id)
        if (self._closing or self._launch is not None or self.credential_busy
                or worker is None or self.busy(document_id)):
            return False
        self.errors.pop(document_id, None)
        self._launch = Launch(document_id, worker, **options)
        self._task("launch", self._transport)
        return True

    def stop(self, document_id: str) -> None:
        if self._launch is not None and self._launch.document_id == document_id:
            self._launch = None
        entry = self.entries.get(document_id)
        if entry is not None and not entry.job.future.done():
            entry.stopping = True
            entry.job.cancel()
        self._changed()

    def monitor_document(self) -> str | None:
        if self.busy(self.manager.active_id):
            return self.manager.active_id
        running = next((key for key in reversed(self.entries) if self.busy(key)), None)
        return running or self.latest_document

    def _session_changed(self, document_id: str) -> None:
        session = self.manager.sessions.get(document_id)
        if session is not None and session.lifecycle != Lifecycle.READY:
            if self.busy(document_id):
                self.stop(document_id)
        if self.entries or self._launch is not None:
            self.changed.emit()

    def _session_removed(self, document_id: str) -> None:
        self.errors.pop(document_id, None)
        if self.busy(document_id):
            self.stop(document_id)
        self._trim_entries()
        self.changed.emit()

    def _trim_entries(self) -> None:
        for document_id, entry in tuple(self.entries.items()):
            if (document_id not in self.manager.sessions and document_id != self.latest_document
                    and entry.future is entry.job.future):
                del self.entries[document_id]

    def _changed(self) -> None:
        if not self.timer.isActive() and not self.is_shutdown:
            self.timer.start()
        self.changed.emit()

    def _poll_credentials(self) -> bool:
        future = self._pending
        if future is None or not future.done():
            return False
        self._pending = None
        kind, self._pending_kind = self._pending_kind, ""
        try:
            value = future.result()
        except Exception as exc:
            self.credential_error = str(exc) if isinstance(exc, CredentialError) else "vault_unavailable"
            if kind in ("read", "launch"):
                self.credential_state = "error"
            if kind == "launch":
                self._launch = None
            return True
        if kind == "launch":
            transport, self.credential_state = value
            if transport is None:
                self._launch = None
            elif self._launch is not None and not self._closing:
                self._launch.transport = transport
        else:
            self.credential_state = value
            if kind in ("write", "delete"):
                self._project = uuid4().hex
                for document_id in tuple(self.entries):
                    if self.busy(document_id):
                        self.stop(document_id)
        return True

    def _poll_launch(self) -> bool:
        launch = self._launch
        if launch is None or launch.transport is None:
            return False
        document_id = launch.document_id
        session = self.manager.sessions.get(document_id)
        if (not self._closing and session is not None and session.lifecycle == Lifecycle.READY
                and session.worker is launch.client and session.saving):
            return False
        if self._closing or self._ready_worker(document_id) is not launch.client:
            self._launch = None
            self.errors[document_id] = "La scheda non è disponibile per l’avvio."
            return True
        entry = self.entries.get(document_id)
        try:
            if entry is not None and not launch.retry_ids:
                if launch.cleanup is None:
                    launch.cleanup = self.manager.review_application.close(document_id)
                if launch.cleanup is not None and not launch.cleanup.done():
                    return False
                if launch.cleanup is not None:
                    launch.cleanup.result()
            if self._scheduler is None:
                self._scheduler = self._scheduler_factory()
            if launch.retry_ids:
                self._scheduler.retry(entry.job, launch.retry_ids, transport=launch.transport, project=self._project)
                entry.future, entry.report, entry.stopping = None, None, False
            else:
                job = self.manager.review_application.start(self._scheduler, document_id, launch.transport,
                                                             project=self._project, **launch.settings)
                title = Path(self.manager.sessions[document_id].canonical_path).name
                pages = launch.settings["last"] - launch.settings["first"] + 1
                self.entries[document_id] = ReviewEntry(job, title, pages)
            self.latest_document = document_id
        except (ValueError, RuntimeError, OSError, ReviewWorkerError):
            self.errors[document_id] = "Impossibile avviare la revisione con la configurazione corrente."
        finally:
            # A cleanup still in flight owns its snapshot and keeps this launch
            # pending for the next Qt tick.
            if launch.cleanup is None or launch.cleanup.done():
                self._launch = None
        return True

    def _poll(self) -> None:
        changed = self._poll_credentials()
        changed = self._poll_launch() or changed
        for entry in self.entries.values():
            job = entry.job
            if entry.progress is not job.progress or entry.phase != job.phase:
                entry.progress, entry.phase = job.progress, job.phase
                changed = True
            if job.future.done() and entry.future is not job.future:
                entry.future = job.future
                try:
                    entry.report = job.future.result()
                except Exception:
                    entry.report = {"status": "failed", "error": "review_result_unavailable"}
                entry.stopping = False
                changed = True
        self._trim_entries()
        if self._closing and self._pending is None and (self._shutdown is None or self._shutdown.done()):
            self.is_shutdown = True
            self.timer.stop()
            self.shutdown_finished.emit()
            return
        elif self._pending is None and self._launch is None and all(
                entry.future is entry.job.future for entry in self.entries.values()) and not self._closing:
            self.timer.stop()
        if changed:
            self.changed.emit()

    def shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._launch = None
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._shutdown = self._scheduler.shutdown() if self._scheduler is not None else None
        if self._pending is None and self._shutdown is None:
            self.is_shutdown = True
            self.timer.stop()
        else:
            self.timer.start()
