"""Small diagnostic export controller; all file/worker waits run off Qt."""

from queue import Empty, Queue
from threading import Event, Thread

from PySide6.QtCore import QObject, QTimer, Signal

from ..services.structured_text_export import export_document
from ..services.review_worker import ReviewWorkerError
from ..worker.protocol import ErrorInfo
from .document_session import Lifecycle


class TextExportController(QObject):
    changed = Signal()
    finished = Signal(str, str)
    failed = Signal(str, object)

    def __init__(self, manager, parent=None) -> None:
        super().__init__(parent)
        self.manager = manager
        self._thread: Thread | None = None
        self._stop = Event()
        self._results = Queue(maxsize=1)
        self._document_id: str | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(25)
        self._timer.timeout.connect(self._poll)
        manager.session_removed.connect(self._removed)

    @property
    def busy(self) -> bool:
        return self._document_id is not None

    def start(self, document_id: str, path: str) -> bool:
        session = self.manager.sessions.get(document_id)
        if (self.busy or self.manager.shutting_down or session is None or session.lifecycle != Lifecycle.READY
                or session.saving or session.pending_mutations or session.worker is None):
            return False
        client, revision, page_count = session.worker, session.document_revision, session.page_count
        protected = tuple(item.canonical_path for item in self.manager.sessions.values())
        self._document_id = document_id
        self._stop.clear()

        def work():
            try:
                result = export_document(client, revision, page_count, path, stopping=self._stop.is_set,
                                         protected_paths=protected)
                self._results.put((True, result))
            except (OSError, ValueError, RuntimeError, ReviewWorkerError) as exc:
                # Do not surface engine details, paths or PDF text in error logs.
                self._results.put((False, type(exc).__name__))

        self._thread = Thread(target=work, name="structured-text-export", daemon=True)
        self._thread.start()
        self._timer.start()
        self.changed.emit()
        return True

    def _removed(self, document_id: str) -> None:
        if document_id == self._document_id:
            self._stop.set()

    def _poll(self) -> None:
        if self.manager.shutting_down:
            self._stop.set()
        try:
            success, result = self._results.get_nowait()
        except Empty:
            return
        document_id = self._document_id
        self._document_id = None
        self._thread = None
        self._timer.stop()
        if success:
            self.finished.emit(document_id, result)
        elif not self._stop.is_set():
            self.failed.emit(document_id, ErrorInfo("TEXT_EXPORT_FAILED",
                "Esportazione non riuscita. Mantieni il PDF aperto e invariato, poi riprova con un file JSON scrivibile."))
        self.changed.emit()
