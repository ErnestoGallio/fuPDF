"""Bounded, non-PyMuPDF filesystem helper; no resolve/stat in the Qt event loop."""

from dataclasses import dataclass
from queue import Empty, Queue
from threading import Event, Thread

from ..util.errors import open_error, user_error
from ..util.paths import FileIdentity, SaveTarget, identify_file, identify_save_target
from ..worker.protocol import ErrorInfo

MAX_PENDING_OPENS = 16


@dataclass(frozen=True)
class ResolvedFile:
    request_id: int
    identity: FileIdentity | None = None
    error: ErrorInfo | None = None
    target: SaveTarget | None = None


class FileResolver:
    def __init__(self) -> None:
        self.requests: Queue[tuple[int, str, bool]] = Queue(MAX_PENDING_OPENS)
        self.results: Queue[ResolvedFile] = Queue(MAX_PENDING_OPENS)
        self.stopped = Event()
        self._closing = Event()
        self._thread = Thread(target=self._run, name="file-identity", daemon=True)
        self._thread.start()

    def submit(self, request_id: int, path: str, *, destination: bool = False) -> None:
        # Manager retains at most MAX_PENDING_OPENS credits including results.
        self.requests.put_nowait((request_id, path, destination))

    def close_async(self) -> None:
        self._closing.set()

    def _run(self) -> None:
        try:
            while not self._closing.is_set():
                try:
                    request_id, path, destination = self.requests.get(timeout=0.02)
                except Empty:
                    continue
                try:
                    result = (ResolvedFile(request_id, target=identify_save_target(path)) if destination
                              else ResolvedFile(request_id, identify_file(path)))
                except ValueError:
                    result = ResolvedFile(request_id, error=user_error("NOT_PDF"))
                except Exception as exc:
                    result = ResolvedFile(request_id, error=open_error(exc))
                if not self._closing.is_set():
                    self.results.put_nowait(result)
        finally:
            self.stopped.set()
