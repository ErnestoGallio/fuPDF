"""The GUI only submits, polls and requests shutdown.

One helper thread owns spawn/send/recv/join for each dedicated process. It never
imports PyMuPDF. Credits include queued, executing and unadopted raster replies.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from collections import Counter
from collections.abc import Iterator
import multiprocessing as mp
from queue import Empty, Queue
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any, Callable, TYPE_CHECKING

from .document_service import DocumentSource
from .protocol import (ErrorInfo, MAX_IN_FLIGHT_RASTERS,
                       MAX_IN_FLIGHT_RASTER_BYTES, MAX_PENDING_REQUESTS,
                       MAX_SINGLE_RASTER_BYTES, REVIEW_OPERATIONS, Operation, Request, Response,
                       decode, encode, frame_limit)

if TYPE_CHECKING:
    from ..spikes.fixtures import FixtureSpec


class RasterBudget:
    """Shared application-level reservation pool, not a raster cache."""

    def __init__(self, max_bytes: int = MAX_IN_FLIGHT_RASTER_BYTES,
                 max_count: int = MAX_IN_FLIGHT_RASTERS,
                 single_bytes: int = MAX_SINGLE_RASTER_BYTES) -> None:
        frame_limit(single_bytes)
        if max_bytes < single_bytes or max_count < 1:
            raise ValueError("Budget must admit at least one raster")
        self.max_bytes, self.max_count = max_bytes, max_count
        self.single_bytes = single_bytes
        self.bytes = self.count = self.peak_bytes = 0
        self._lock = Lock()

    def reserve(self) -> bool:
        with self._lock:
            if self.count >= self.max_count or self.bytes + self.single_bytes > self.max_bytes:
                return False
            self.bytes += self.single_bytes
            self.count += 1
            self.peak_bytes = max(self.peak_bytes, self.bytes)
            return True

    def release(self) -> None:
        with self._lock:
            self.bytes -= self.single_bytes
            self.count -= 1
            assert self.bytes >= 0 and self.count >= 0


@dataclass
class Delivery:
    """Consume then release; callers must not retain a borrowed pixel buffer."""

    response: Response | None
    _release: Callable[[], None]

    def release(self) -> None:
        if self.response is not None:
            self.response = None
            self._release()

    def __enter__(self) -> "Delivery":
        return self

    def __exit__(self, *args: object) -> None:
        self.release()


class WorkerClient:
    """Shared transport; M0 fixtures and M1 user documents have separate owners."""

    def __init__(self, document_id: str, fixture: FixtureSpec | DocumentSource, budget: RasterBudget,
                 *, shutdown_timeout: float = 2.0, request_timeout: float = 30.0) -> None:
        self.document_id, self.fixture, self.budget = document_id, fixture, budget
        self.generation = 1
        self.pid: int | None = None
        self.failure: ErrorInfo | None = None
        self.forced_termination = False
        self.stale_discarded = 0
        self.active_request_id: int | None = None
        self.submitted_counts: Counter[Operation] = Counter()
        self.completed_counts: Counter[Operation] = Counter()
        self.cancelled_queued = 0
        self.stopped = Event()
        self._closing = Event()
        self._shutdown_timeout, self._request_timeout = shutdown_timeout, request_timeout
        self._close_at = 0.0
        self._requests: Queue[Request] = Queue(maxsize=MAX_PENDING_REQUESTS)
        self._responses: Queue[Response] = Queue(maxsize=MAX_PENDING_REQUESTS)
        self._deferred: dict[int, Response] = {}
        self._records: dict[int, bool] = {}
        self._next_id = 0
        self._latest_render = 0
        self._latest_appearance = 0
        self._review_request_id: int | None = None
        self._review_paused = False
        self._review_file_lock = Lock()
        self._lock = Lock()
        self._thread = Thread(target=self._run, name=f"ipc-{document_id}", daemon=True)
        self._thread.start()

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._records)

    def is_pending(self, request_id: int) -> bool:
        with self._lock:
            return request_id in self._records

    @property
    def closing(self) -> bool:
        return self._closing.is_set()

    def pause_review(self, paused: bool) -> None:
        with self._lock:
            self._review_paused = paused

    @contextmanager
    def review_reader(self) -> Iterator[None]:
        """Keep the private directory alive until a background reader closes."""
        with self._review_file_lock:
            if self.closing or self.failure or self.stopped.is_set():
                raise OSError("Review worker is closing")
            yield

    def cancel_renders(self) -> None:
        """Skip queued/ignore completed renders; never interrupt a MuPDF call."""
        with self._lock:
            self._latest_render = -1

    def cancel_annotation_appearances(self) -> None:
        """Discard queued/returned read-only appearance rasters."""
        with self._lock:
            self._latest_appearance = -1

    def submit(self, operation: Operation, payload: dict | None = None) -> int | None:
        """Return None for backpressure; never wait for a worker or pipe."""
        with self._lock:
            if self._closing.is_set() or self.stopped.is_set() or self.failure:
                return None
            if len(self._records) >= MAX_PENDING_REQUESTS:
                return None
            if operation in REVIEW_OPERATIONS:
                if (self._review_paused or not isinstance(self.fixture, DocumentSource)
                        or self._review_request_id is not None
                        or (operation in (Operation.REVIEW_EXTRACT, Operation.REVIEW_LOCALIZE,
                                          Operation.REVIEW_CONFIRM) and self._records)):
                    return None
                if operation == Operation.REVIEW_SNAPSHOT and payload:
                    raise ValueError("Snapshot workspace is assigned by the transport")
            request_id = self._next_id + 1
            request = Request(self.document_id, self.generation, request_id, operation, payload or {})
            request = decode(encode(request))  # snapshot mutable input at submission
            reserved = operation in (Operation.RENDER, Operation.ANNOTATION_APPEARANCE)
            if reserved and not self.budget.reserve():
                return None
            self._next_id = request_id
            self._records[request_id] = reserved
            if operation == Operation.RENDER:
                self._latest_render = request_id
            elif operation == Operation.ANNOTATION_APPEARANCE:
                self._latest_appearance = request_id
            elif operation in REVIEW_OPERATIONS:
                self._review_request_id = request_id
            self._requests.put_nowait(request)
            self.submitted_counts[operation] += 1
            return request_id

    def _release(self, request_id: int) -> None:
        with self._lock:
            reserved = self._records.pop(request_id, False)
            if self._review_request_id == request_id:
                self._review_request_id = None
        if reserved:
            self.budget.release()

    def poll(self, request_id: int | None = None, *, include_review: bool = True) -> Delivery | None:
        """Nonblocking targeted poll; unrelated replies remain bounded and routed."""
        if request_id is not None and (type(request_id) is not int or request_id < 1):
            raise ValueError("Invalid request identity")
        with self._lock:
            if request_id is None and not include_review:
                # Review replies can own files. Only the targeted consumer
                # may adopt them, including while Save drains ordinary reads.
                request_id_to_pop = next((key for key in reversed(self._deferred)
                                          if self._deferred[key].operation not in REVIEW_OPERATIONS), None)
                response = self._deferred.pop(request_id_to_pop, None)
            elif request_id is None and self._deferred:
                _, response = self._deferred.popitem()
            else:
                response = self._deferred.pop(request_id, None)
        if response is not None:
            if response.document_generation != self.generation:
                self.stale_discarded += 1
                self._release(response.request_id)
                return None
            new_generation = response.payload.get("new_generation")
            if new_generation is not None:
                self.generation = new_generation
            return Delivery(response, lambda rid=response.request_id: self._release(rid))
        for _ in range(MAX_PENDING_REQUESTS):
            try:
                response = self._responses.get_nowait()
            except Empty:
                return None
            if (self._closing.is_set() or response.document_id != self.document_id
                    or response.document_generation != self.generation
                    or (response.operation == Operation.RENDER
                        and response.request_id != self._latest_render)
                    or (response.operation == Operation.ANNOTATION_APPEARANCE
                        and response.request_id != self._latest_appearance)):
                self.stale_discarded += 1
                self._release(response.request_id)
                continue
            if response.request_id != request_id and (
                    request_id is not None or (not include_review and response.operation in REVIEW_OPERATIONS)):
                with self._lock:
                    self._deferred[response.request_id] = response
                continue
            new_generation = response.payload.get("new_generation")
            if new_generation is not None:
                self.generation = new_generation
            return Delivery(response, lambda rid=response.request_id: self._release(rid))
        return None

    def close_async(self) -> None:
        if not self._closing.is_set():
            self._close_at = monotonic()
            self._closing.set()

    def _exchange(self, conn, process, request: Request) -> Response:
        conn.send_bytes(encode(request))
        started = monotonic()
        while not conn.poll(0.02):
            if not process.is_alive():
                raise ChildProcessError("Worker exited")
            if self._closing.is_set() and monotonic() - self._close_at > self._shutdown_timeout:
                raise TimeoutError("Shutdown deadline")
            # The validator has its own bounded lifetime. Never kill its owner
            # at the ordinary render timeout while an atomic save is running.
            long_pdf_io = request.operation in (Operation.SAVE, Operation.REVIEW_SNAPSHOT, Operation.REVIEW_EXTRACT,
                                                Operation.REVIEW_LOCALIZE, Operation.REVIEW_CONFIRM)
            timeout = max(self._request_timeout, 600) if long_pdf_io else self._request_timeout
            if monotonic() - started > timeout:
                raise TimeoutError("Request deadline")
        response = decode(conn.recv_bytes(frame_limit(self.budget.single_bytes)),
                          self.budget.single_bytes)
        if not isinstance(response, Response):
            raise ValueError("Expected response")
        if (response.document_id, response.request_id, response.operation) != (
                request.document_id, request.request_id, request.operation):
            raise ValueError("Mismatched worker response")
        return response

    def _run(self) -> None:
        from .document_service import document_worker_main

        ctx = mp.get_context("spawn")
        parent = child = process = None
        started = False
        active_request_id: int | None = None
        review_workspace = None
        try:
            parent, child = ctx.Pipe(duplex=True)
            if isinstance(self.fixture, DocumentSource):
                target = document_worker_main
            else:
                from .process import worker_main
                target = worker_main
            process = ctx.Process(target=target, args=(child, self.document_id, self.fixture),
                                  name=f"pdf-{self.document_id}")
            process.start()
            started = True
            self.pid = process.pid
            child.close()
            while not self._closing.is_set():
                try:
                    request = self._requests.get(timeout=0.02)
                except Empty:
                    if not process.is_alive():
                        raise ChildProcessError("Worker exited")
                    continue
                if (request.document_generation != self.generation
                            or (request.operation == Operation.RENDER
                            and request.request_id != self._latest_render)
                            or (request.operation == Operation.ANNOTATION_APPEARANCE
                            and request.request_id != self._latest_appearance)):
                    self.stale_discarded += 1
                    self.cancelled_queued += 1
                    self._release(request.request_id)
                    continue
                active_request_id = request.request_id
                self.active_request_id = request.request_id
                if request.operation == Operation.REVIEW_SNAPSHOT:
                    if review_workspace is None:
                        try:
                            review_workspace = TemporaryDirectory(prefix="fupdf-review-", ignore_cleanup_errors=True)
                        except OSError:
                            self._responses.put_nowait(Response(self.document_id, request.document_generation,
                                request.request_id, request.operation, False, error=ErrorInfo(
                                    "REVIEW_SNAPSHOT_FAILED", "Directory temporanea della revisione non disponibile.")))
                            active_request_id = self.active_request_id = None
                            continue
                    request = replace(request, payload={"workspace": review_workspace.name})
                response = self._exchange(parent, process, request)
                self.completed_counts[request.operation] += 1
                if self._closing.is_set():
                    self._release(request.request_id)
                else:
                    self._responses.put_nowait(response)
                active_request_id = None
                self.active_request_id = None
                # Do not retain an extra raster reference while waiting for more work.
                del response
            if process.is_alive():
                close = Request(self.document_id, self.generation, self._next_id + 1, Operation.CLOSE)
                self._exchange(parent, process, close)
                process.join(timeout=self._shutdown_timeout)
        except Exception as exc:
            # Error codes only: no document text/path or arbitrary engine diagnostics.
            self.failure = ErrorInfo(type(exc).__name__, "PDF worker transport stopped")
        finally:
            if started and self.failure and process.is_alive() and not self._closing.is_set():
                process.join(timeout=0.2)
            if started and process.is_alive():
                self.forced_termination = True
                process.terminate()
                process.join(timeout=self._shutdown_timeout)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=self._shutdown_timeout)
            if parent is not None:
                parent.close()
            if child is not None:
                child.close()
            if started and not process.is_alive():
                process.close()
            # Retire queued replies/requests. Delivered leases remain reserved until
            # their consumer releases them, even if their originating worker closes.
            while True:
                try:
                    response = self._responses.get_nowait()
                    self._release(response.request_id)
                except Empty:
                    break
            with self._lock:
                deferred_ids = tuple(self._deferred)
                self._deferred.clear()
            for request_id in deferred_ids:
                self._release(request_id)
            while True:
                try:
                    self._release(self._requests.get_nowait().request_id)
                except Empty:
                    break
            if active_request_id is not None:
                self._release(active_request_id)
            if review_workspace is not None:
                with self._review_file_lock:
                    review_workspace.cleanup()
            self.stopped.set()
