"""Read-only M16 snapshots for deterministic multi-document stress tests.

This harness deliberately reads the application's existing logical accounting.
It does not enable metrics, request work, or retain GUI/PDF objects.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping


def snapshot(manager: Any, views: Mapping[str, Any] | None = None) -> dict[str, object]:
    """Return a JSON-serializable view of current multi-document resources.

    ``views`` is normally ``MainWindow._widgets`` in UI stress tests. Keeping
    it explicit lets manager-only lifecycle tests use the same snapshot without
    coupling product code to a test registry.
    """
    attached_views = views or {}
    resident_by_document = _resident_by_document(manager.resident_budget)
    text_by_document = _text_by_document(manager.text_cache)
    documents = []
    reserved_total = 0

    for document_id, session in sorted(manager.sessions.items()):
        worker = session.worker
        worker_state = _worker_state(worker) if worker is not None else None
        if worker_state is not None:
            reserved_total += worker_state["reserved_rasters"]
        documents.append({
            "document_id": document_id,
            "active": document_id == manager.active_id,
            "lifecycle": session.lifecycle.value,
            "generation": session.document_generation,
            "page_count": session.page_count,
            "revision": {
                "document": session.document_revision,
                "saved": session.saved_revision,
                "pending_mutations": session.pending_mutations,
                "dirty": session.dirty,
                "history_position": session.history_position,
                "history_length": session.history_length,
                "history_state_id": session.history_state_id,
                "saved_history_state_id": session.saved_history_state_id,
            },
            "search": {
                "active": bool(session.ui.search.query),
                "generation": session.ui.search.generation,
                "complete": session.ui.search.complete,
                "result_count": len(session.ui.search.results),
                "next_page": session.ui.search.next_page,
            },
            "annotations": {
                "app_model_count": len(session.annotations),
                "inventory_count": len(session.annotation_inventory),
                "detail_count": len(session.annotation_details),
            },
            "worker": worker_state,
            "raster_cache": resident_by_document.get(document_id, _empty_cache_state()),
            "text_cache": text_by_document.get(document_id, {"entries": 0, "bytes": 0}),
            "viewer": _viewer_state(attached_views.get(document_id)),
        })

    resident = manager.resident_budget
    text_cache = manager.text_cache
    return {
        "schema_version": 1,
        "global": {
            "session_count": len(manager.sessions),
            "active_document_id": manager.active_id,
            "pending_open_count": manager.pending_open_count,
            "workers_started": manager.workers_started,
            "raster_cache": {
                "max_bytes": resident.max_bytes,
                "bytes": resident.bytes,
                "peak_bytes": resident.peak_bytes,
                "entries": resident.count,
                "active_entries": len(resident.keys),
                "retired_entries": resident.count - len(resident.keys),
                "hits": resident.hits,
                "misses": resident.misses,
                "evictions": resident.evictions,
            },
            "in_flight_raster": {
                "max_bytes": manager.budget.max_bytes,
                "max_count": manager.budget.max_count,
                "single_bytes": manager.budget.single_bytes,
                "bytes": manager.budget.bytes,
                "count": manager.budget.count,
                "peak_bytes": manager.budget.peak_bytes,
                "document_reserved_count": reserved_total,
            },
            "text_cache": {
                "max_bytes": text_cache.max_bytes,
                "bytes": text_cache.bytes,
                "peak_bytes": text_cache.peak_bytes,
                "entries": text_cache.entry_count,
                "hits": text_cache.hits,
                "misses": text_cache.misses,
                "evictions": text_cache.evictions,
            },
        },
        "documents": documents,
    }


def _worker_state(worker: Any) -> dict[str, object]:
    # WorkerClient owns this lock; the copied values are already existing
    # transport accounting, and this short read never communicates with it.
    with worker._lock:
        reserved_rasters = sum(1 for reserved in worker._records.values() if reserved)
        pending_requests = len(worker._records)
        deferred_responses = len(worker._deferred)
        submitted = _operation_counts(worker.submitted_counts)
        completed = _operation_counts(worker.completed_counts)
    return {
        "pid": worker.pid,
        "stopped": worker.stopped.is_set(),
        "close_requested": worker._closing.is_set(),
        "failure_code": worker.failure.code if worker.failure is not None else None,
        "active_request_id": worker.active_request_id,
        "pending_requests": pending_requests,
        "deferred_responses": deferred_responses,
        "reserved_rasters": reserved_rasters,
        "stale_discarded": worker.stale_discarded,
        "cancelled_queued": worker.cancelled_queued,
        "submitted": submitted,
        "completed": completed,
    }


def _operation_counts(counts: Any) -> dict[str, int]:
    return {operation.value: count for operation, count in sorted(
        counts.items(), key=lambda item: item[0].value)}


def _resident_by_document(cache: Any) -> dict[str, dict[str, int]]:
    values = (*cache._entries.values(), *cache._retired)
    result: defaultdict[str, dict[str, int]] = defaultdict(_empty_cache_state)
    for entry in values:
        state = result[entry.key.document_id]
        state["entries"] += 1
        state["bytes"] += entry.cost
        state["retired_entries"] += int(entry.retired)
        state["regional_entries"] += int(entry.key.region is not None)
        state["annotation_appearance_entries"] += int(entry.key.annotation_id is not None)
    return dict(result)


def _empty_cache_state() -> dict[str, int]:
    return {"entries": 0, "bytes": 0, "retired_entries": 0,
            "regional_entries": 0, "annotation_appearance_entries": 0}


def _text_by_document(cache: Any) -> dict[str, dict[str, int]]:
    result: defaultdict[str, dict[str, int]] = defaultdict(lambda: {"entries": 0, "bytes": 0})
    for key, (_value, cost) in cache._entries.items():
        state = result[key[0]]
        state["entries"] += 1
        state["bytes"] += cost
    return dict(result)


def _viewer_state(view: Any | None) -> dict[str, object] | None:
    if view is None:
        return None
    scheduler = view.scheduler
    return {
        "active": scheduler.active,
        "pending": scheduler.pending is not None,
        "visible_page_count": len(scheduler.visible),
        "nearby_page_count": len(scheduler.nearby),
        "visible_target_count": len(scheduler._visible_keys),
        "prefetch_target_count": len(scheduler._prefetch_keys),
        "requested": scheduler.requested_count,
        "completed": scheduler.completed_count,
        "adopted": scheduler.adopted_count,
        "discarded": scheduler.discarded_count,
        "backpressure": scheduler.backpressure_count,
        "regional_requested": scheduler.regional_requested_count,
        "annotation_appearance_pending": view._appearance_pending is not None,
        "search_pending": view.search_layer.pending is not None,
        "search_requested": view.search_layer.requested,
        "search_adopted": view.search_layer.adopted,
        "search_stale_discarded": view.search_layer.stale_discarded,
    }
