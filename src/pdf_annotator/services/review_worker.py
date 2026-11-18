"""Async adapter for the existing document worker's review operations."""

import asyncio
from dataclasses import dataclass
from time import monotonic
from typing import Callable

from .review_batches import ReviewBatch, ReviewPage, ReviewSnapshot, make_batch
from ..worker.client import WorkerClient
from ..worker.protocol import Operation, Response


class ReviewWorkerError(Exception):
    pass


@dataclass
class ReviewWorker:
    client: WorkerClient

    def __post_init__(self) -> None:
        self._lock = asyncio.Lock()

    @property
    def unavailable(self) -> bool:
        return bool(self.client.failure or self.client.closing or self.client.stopped.is_set())

    async def _request(self, operation: Operation, payload: dict,
                       stopping: Callable[[], bool] = lambda: False) -> Response:
        async with self._lock:
            deadline = monotonic() + 600
            while True:
                if self.unavailable:
                    raise ReviewWorkerError("worker_unavailable")
                if stopping():
                    raise ReviewWorkerError("cancelled")
                request_id = self.client.submit(operation, payload)
                if request_id is not None:
                    break
                if monotonic() >= deadline:
                    raise ReviewWorkerError("worker_admission_timeout")
                await asyncio.sleep(0.02)
            # An admitted operation can publish files even if the job stops.
            # Claim the response and let the owner release them.
            while not self.unavailable:
                delivery = self.client.poll(request_id)
                if delivery is not None:
                    with delivery:
                        response = delivery.response
                        if (response.document_id != self.client.document_id
                                or response.document_generation != self.client.generation
                                or response.request_id != request_id or response.operation != operation):
                            raise ReviewWorkerError("review_response_identity")
                        if not response.success:
                            raise ReviewWorkerError(response.error.code if response.error else "worker_error")
                        return response
                if not self.client.is_pending(request_id):
                    raise ReviewWorkerError("review_response_lost")
                await asyncio.sleep(0.02)
            raise ReviewWorkerError("worker_unavailable")

    async def capture(self, stopping: Callable[[], bool]) -> ReviewSnapshot:
        response = await self._request(Operation.REVIEW_SNAPSHOT, {}, stopping)
        try:
            snapshot = ReviewSnapshot(**response.payload["snapshot"])
            if (snapshot.document_id != response.document_id
                    or snapshot.document_generation != response.document_generation):
                raise ValueError("Snapshot binding mismatch")
        except (TypeError, KeyError, ValueError):
            raise ReviewWorkerError("invalid_snapshot") from None
        return snapshot

    async def extract(self, snapshot: ReviewSnapshot, batch: ReviewBatch, max_context_pages: int,
                      stopping: Callable[[], bool]) -> ReviewBatch:
        first, last = batch.target_pages[0], batch.target_pages[-1]
        response = await self._request(Operation.REVIEW_EXTRACT, {
            "snapshot_id": snapshot.snapshot_id, "first_page": first, "last_page": last,
            "split": False, "max_context_pages": max_context_pages,
        }, stopping)
        extract_id = None
        try:
            plan = response.payload["plan"]
            item, = plan["batches"]
            extract_id = item.get("extract_id")
            prepared = ReviewBatch(**{
                **item, "target_pages": tuple(item["target_pages"]),
                "context_pages": tuple(item["context_pages"]),
                "page_map": tuple(ReviewPage(**page) for page in item["page_map"]),
            })
            expected = make_batch(snapshot, first, last, prepared.context_pages)
            if (ReviewSnapshot(**plan["snapshot"]) != snapshot or plan["first_page"] != first
                    or plan["last_page"] != last or prepared.batch_id != batch.batch_id
                    or prepared.target_pages != batch.target_pages or prepared.page_map != expected.page_map
                    or not set(prepared.context_pages) <= set(batch.context_pages)
                    or len(prepared.context_pages) > max_context_pages
                    or prepared.status not in ("ready", "too_large")):
                raise ValueError("Invalid batch binding")
        except (TypeError, KeyError, ValueError, AttributeError):
            if isinstance(extract_id, str):
                await self.release_extract(snapshot, extract_id)
            raise ReviewWorkerError("invalid_extract") from None
        return prepared

    async def release_extract(self, snapshot: ReviewSnapshot, extract_id: str) -> None:
        data = {"snapshot_id": snapshot.snapshot_id, "extract_id": extract_id}
        try:
            await self._request(Operation.REVIEW_RELEASE_EXTRACT, data)
        except ReviewWorkerError as exc:
            if str(exc) != "REVIEW_CLEANUP_FAILED":
                raise
            await self._request(Operation.REVIEW_RELEASE_EXTRACT, data)

    async def release(self, snapshot: ReviewSnapshot) -> None:
        await self._request(Operation.REVIEW_RELEASE, {"snapshot_id": snapshot.snapshot_id})

    async def localize(self, data: dict, stopping: Callable[[], bool]) -> dict:
        return await self._application_request(Operation.REVIEW_LOCALIZE, data, stopping)

    async def confirm(self, data: dict, stopping: Callable[[], bool]) -> dict:
        return await self._application_request(Operation.REVIEW_CONFIRM, data, stopping)

    async def lookup(self, data: dict, stopping: Callable[[], bool]) -> dict:
        return (await self._request(Operation.REVIEW_LOOKUP, data, stopping)).payload

    async def _application_request(self, operation: Operation, data: dict,
                                   stopping: Callable[[], bool]) -> dict:
        try:
            return (await self._request(operation, data, stopping)).payload
        except ValueError:
            raise ReviewWorkerError("REVIEW_ITEM_TOO_LARGE") from None
