"""Deferred review application and its durable per-proposal ledger."""

import asyncio
from collections import Counter
from dataclasses import asdict, dataclass
from hashlib import sha256
from itertools import groupby
from time import monotonic
from typing import Awaitable, Callable
from uuid import UUID

from ..domain.annotation_commands import AnnotationCommand
from ..domain.errors import MutationErrorCode
from ..domain.identifiers import new_command_id
from ..domain.results import MutationResult
from ..domain.serialization import canonical_json, command_from_payload
from ..worker.client import WorkerClient
from .review_batches import ReviewSnapshot
from .review_store import ReviewStore, read_json, write_json
from .review_worker import ReviewWorker, ReviewWorkerError


@dataclass(frozen=True)
class MutationDelivery:
    status: str  # accepted, rejected, deferred, cancelled, session_invalid
    result: MutationResult | None = None


SubmitMutation = Callable[[WorkerClient, AnnotationCommand, Callable[[], bool]], Awaitable[MutationDelivery]]
REVISIT = {"ready", "not_attempted", "submitting", "uncertain"}
FATAL_WORKER_ERRORS = {"worker_unavailable", "review_response_identity", "review_response_lost",
                       "REVIEW_SOURCE_CHANGED", "REVIEW_SNAPSHOT_UNKNOWN", "REVIEW_SNAPSHOT_CHANGED",
                       "REVIEW_INVALID_PROOF", "WORKER_FAILED"}


def analysis_can_apply(analysis: dict) -> bool:
    return (analysis["status"] in ("completed", "partial")
            and analysis.get("error") is None
            and all(batch["status"] not in ("pending", "in_flight", "retry_wait")
                    for batch in analysis["batches"]))


def proposal_identity(job_id: str, batch_id: str, batch_index: int) -> str:
    # /NM uses the production annotation UUID format. The inputs are the
    # immutable result's identity, never the aggregate's renumberable index.
    identity = canonical_json({"job_id": job_id, "batch_id": batch_id, "index": batch_index})
    return UUID(bytes=sha256(identity.encode()).digest()[:16], version=4).hex


@dataclass
class Proposal:
    correction: dict
    record: dict
    proof: dict | None = None

    @property
    def annotation_id(self) -> str:
        return self.record["annotation_id"]

    def wire(self) -> dict:
        return {**{key: self.record[key] for key in ("job_id", "snapshot_id", "annotation_id")},
                "proposal": self.correction}


def _span(item: Proposal) -> tuple[int, int, int] | None:
    if item.correction["operation"] == "comment" or item.proof is None:
        return None
    localization = item.proof["location"]["localization"]
    source = localization["source_range"]
    if not source:
        return None
    start, end = source["start"], source["end_exclusive"]
    if item.correction["operation"] == "insert":
        start = end = start if item.correction["insert_anchor"]["side"] == "before" else end
    return source["run_index"], start, end


def _overlap(a: tuple[int, int, int], b: tuple[int, int, int]) -> bool:
    run, start, end = a
    other_run, other_start, other_end = b
    if run != other_run:
        return False
    if start == end and other_start == other_end:
        return start == other_start
    if start == end:
        return other_start < start < other_end
    if other_start == other_end:
        return start < other_start < end
    return max(start, other_start) < min(end, other_end)


class DeferredReviewApplication:
    def __init__(self, submit: SubmitMutation) -> None:
        self._submit = submit

    async def run(self, worker: ReviewWorker, snapshot: ReviewSnapshot | None, store: ReviewStore,
                  analysis: dict, cancelled: Callable[[], bool]) -> dict:
        directory = store.directory / "application"
        directory.mkdir(mode=0o700, exist_ok=True)
        manifest = read_json(store.directory / "manifest.json")
        analysis_snapshot = manifest["snapshot"]
        if (analysis["job_id"] != manifest["job_id"] or analysis["snapshot_id"] != analysis_snapshot["snapshot_id"]
                or worker.client.document_id != analysis_snapshot["document_id"]
                or (snapshot is not None and (snapshot.snapshot_id != analysis["snapshot_id"]
                    or snapshot.document_id != analysis_snapshot["document_id"]
                    or snapshot.sha256 != analysis_snapshot["sha256"]
                    or snapshot.document_revision != analysis_snapshot["document_revision"]
                    or snapshot.document_generation != analysis_snapshot["document_generation"]))):
            return self._report(store, analysis, [], "failed", "session_invalid")

        def record(item: Proposal, status: str, **extra) -> None:
            updated = {**item.record, **extra, "status": status}
            write_json(directory / f"{item.annotation_id}.json", updated)
            item.record = updated

        proposals = self._proposals(analysis, directory)
        skipped = set()
        for item in proposals:
            if item.record["status"] == "applied":
                skipped.add(item.annotation_id)
        blocked = not analysis_can_apply(analysis)
        interrupted = False
        error = None

        def stop() -> bool:
            return cancelled() or worker.unavailable

        if not blocked and not stop():
            try:
                for _page, items in groupby(proposals, key=lambda item: item.record["page_index"]):
                    if stop():
                        break
                    pending = []
                    for item in items:
                        status = item.record["status"]
                        if status not in REVISIT:
                            continue
                        if stop():
                            break
                        if status in ("submitting", "uncertain"):
                            if not await self._reconcile(worker, item, stop, record):
                                raise ReviewWorkerError("application_uncertain")
                            continue
                        if snapshot is None:
                            record(item, "session_invalid", reason="snapshot_unavailable")
                            continue
                        try:
                            item.proof = await worker.localize(item.wire(), stop)
                            location = item.proof["location"]["localization"]
                        except ReviewWorkerError as exc:
                            if str(exc) in FATAL_WORKER_ERRORS or str(exc) == "cancelled":
                                raise
                            record(item, "not_localizable", reason=str(exc))
                            continue
                        if location["applicable"]:
                            record(item, "ready", localization=location, reason=None)
                            pending.append(item)
                        else:
                            status = "ambiguous" if location["status"] == "ambiguous" else "not_localizable"
                            record(item, status, localization=location, reason=location["reason"])
                    rejected = self._conflicts(pending)
                    for item in pending:
                        if stop():
                            break
                        conflict = rejected.get(item.annotation_id)
                        if conflict:
                            record(item, "discarded", reason=conflict)
                        else:
                            fatal = await self._apply(worker, item, stop, record)
                            if fatal is not None:
                                raise ReviewWorkerError(fatal)
                    for item in pending:
                        item.proof = None
            except ReviewWorkerError as exc:
                error = str(exc)
            except OSError:
                # An intent already on disk remains uncertain after a failed
                # acknowledgement write. Never submit a second proposal here.
                return self._report(store, analysis, proposals, "failed", "persistence_failed", skipped)
        interrupted = cancelled() or error == "cancelled"
        if worker.unavailable and not interrupted:
            error = error or "session_invalid"
        remainder = ("not_attempted" if blocked or interrupted else
                     "session_invalid" if error and error != "application_uncertain" else "not_attempted")
        for item in proposals:
            if item.record["status"] == "ready":
                record(item, remainder, reason=error or ("analysis_not_eligible" if blocked else "interrupted"))
        status = ("deferred" if blocked else "interrupted" if interrupted else
                  "failed" if error else "completed")
        return self._report(store, analysis, proposals, status, error, skipped)

    @staticmethod
    def _proposals(analysis: dict, directory) -> list[Proposal]:
        proposals = []
        identities = set()
        covered = {batch["batch_id"]: set(batch["target_pages"]) for batch in analysis["batches"]
                   if batch["status"] == "completed"}
        for index, (correction, origin) in enumerate(zip(analysis["corrections"], analysis["provenance"], strict=True)):
            if (origin["job_id"] != analysis["job_id"] or origin["snapshot_id"] != analysis["snapshot_id"]
                    or origin["index"] != index or type(origin["batch_index"]) is not int
                    or origin["batch_index"] < 0
                    or correction["page_index"] not in covered.get(origin["batch_id"], ())):
                raise ValueError("Invalid review provenance")
            annotation_id = proposal_identity(origin["job_id"], origin["batch_id"], origin["batch_index"])
            if annotation_id in identities:
                raise ValueError("Duplicate review provenance")
            identities.add(annotation_id)
            identity = {key: origin[key] for key in ("job_id", "snapshot_id", "batch_id", "batch_index", "attempt_id")}
            identity.update(annotation_id=annotation_id, operation=correction["operation"],
                            page_index=correction["page_index"],
                            proposal_sha256=sha256(canonical_json(correction).encode()).hexdigest())
            path = directory / f"{annotation_id}.json"
            state = read_json(path) if path.exists() else None
            if state is not None and any(state[key] != value for key, value in identity.items()):
                raise ValueError("Review proposal identity changed")
            proposals.append(Proposal(correction, state or {**identity, "status": "ready"}))
        return sorted(proposals, key=lambda item: item.record["page_index"])

    @staticmethod
    def _conflicts(items: list[Proposal]) -> dict[str, str]:
        rejected = {}
        spans = [(span, item) for item in items if (span := _span(item)) is not None]
        for index, (span, first) in enumerate(spans):
            for other_span, second in spans[index + 1:]:
                if not _overlap(span, other_span):
                    continue
                first_action = first.correction["operation"], first.correction["replacement_text"]
                second_action = second.correction["operation"], second.correction["replacement_text"]
                if span == other_span and first_action == second_action:
                    rejected.setdefault(second.annotation_id, "duplicate_proposal")
                else:
                    rejected[first.annotation_id] = rejected[second.annotation_id] = "overlapping_proposals"
        return rejected

    @staticmethod
    async def _reconcile(worker: ReviewWorker, item: Proposal, stop, record) -> bool:
        data = {key: item.record[key] for key in ("annotation_id", "page_index", "operation")}
        native = await worker.lookup(data, stop)
        if native.get("pdf_nm") == item.annotation_id:
            record(item, "applied", native=native, reconciled=True, reason=None)
            return True
        record(item, "uncertain", reason="native_outcome_unconfirmed")
        return False

    async def _apply(self, worker: ReviewWorker, item: Proposal, stop, record) -> str | None:
        deadline = monotonic() + 600
        while not stop() and monotonic() < deadline:
            command_id = new_command_id()
            try:
                confirmed = await worker.confirm({**item.wire(), **item.proof, "command_id": command_id}, stop)
            except ReviewWorkerError as exc:
                if str(exc) in FATAL_WORKER_ERRORS or str(exc) == "cancelled":
                    raise
                result = "target_changed" if str(exc) == MutationErrorCode.STALE_TEXT_TARGET.value else "command_error"
                record(item, result, reason=str(exc))
                return None
            if confirmed["status"] != "confirmed":
                record(item, confirmed["status"], reason=confirmed["reason"])
                return None
            command = command_from_payload(confirmed["command"])
            if (command.context.command_id != command_id or command.annotation_id.value != item.annotation_id
                    or command.context.document_id != worker.client.document_id
                    or command.context.review_snapshot_id != item.record["snapshot_id"]):
                raise ReviewWorkerError("review_response_identity")
            if stop():
                break
            record(item, "submitting", command_id=command_id,
                   document_generation=command.context.document_generation, live_status=confirmed["live_status"])
            delivered = await self._submit(worker.client, command, stop)
            if delivered.status == "deferred":
                record(item, "ready", reason=None)
                await asyncio.sleep(0.02)
                continue
            if delivered.status == "cancelled":
                record(item, "not_attempted", reason="interrupted")
                return "cancelled"
            if delivered.status == "session_invalid":
                record(item, "session_invalid", reason="session_invalid")
                return "session_invalid"
            result = delivered.result
            if (result is not None and result.success and result.annotation_id == command.annotation_id
                    and result.command_id == command_id and result.document_id == command.context.document_id
                    and result.document_generation == command.context.document_generation):
                backing = result.annotation.backing if result.annotation else None
                if backing is not None and backing.pdf_nm == item.annotation_id:
                    record(item, "applied", native=asdict(backing), revision=result.document_revision, reason=None)
                    return None
            code = result.error.code if result is not None and result.error is not None else None
            if code == MutationErrorCode.REVIEW_DOCUMENT_CHANGED:
                record(item, "session_invalid", reason=code.value)
                return code.value
            if code is None or code in (MutationErrorCode.PROTOCOL_ERROR, MutationErrorCode.WORKER_FAILURE,
                                        MutationErrorCode.CANCELLED, MutationErrorCode.DUPLICATE_COMMAND):
                record(item, "uncertain", reason=code.value if code else "unreliable_acknowledgement")
                return "application_uncertain"
            status = ("discarded" if code == MutationErrorCode.REVIEW_CONFLICT else
                      "target_changed" if code == MutationErrorCode.STALE_TEXT_TARGET else "command_error")
            record(item, status, reason=code.value)
            return None
        record(item, "not_attempted", reason="interrupted" if stop() else "worker_admission_timeout")
        return None

    @staticmethod
    def _report(store: ReviewStore, analysis: dict, proposals: list[Proposal], status: str,
                error: str | None, skipped=()) -> dict:
        outcomes = [{**item.record, "status": ("already_applied" if item.annotation_id in skipped else
                     "uncertain" if item.record["status"] == "submitting" else item.record["status"])}
                    for item in proposals]
        counts = Counter(item["status"] for item in outcomes)
        summary = {
            "status": status, "error": error,
            "model": analysis.get("model"),
            "pages_analyzed": analysis["completed_pages"],
            "pages_not_analyzed": len(analysis["pages"]) - analysis["completed_pages"],
            "proposals": len(analysis["corrections"]),
            "localized": sum(bool((item.get("localization") or {}).get("applicable")) for item in outcomes),
            "applied": counts["applied"], "already_applied": counts["already_applied"],
            "applied_total": counts["applied"] + counts["already_applied"],
            "discarded": sum(counts[key] for key in ("discarded", "ambiguous", "not_localizable", "target_changed")),
            "errors": sum(counts[key] for key in ("command_error", "session_invalid")),
            "uncertain": counts["uncertain"], "not_attempted": counts["not_attempted"] + counts["ready"],
            "estimated_cost_usd": analysis["estimated_cost_usd"],
            "unknown_cost_attempts": analysis["unknown_cost_attempts"],
        }
        try:
            write_json(store.directory / "application_report.json", {
                "version": 1, "job_id": analysis["job_id"], "snapshot_id": analysis["snapshot_id"],
                "cycle": analysis["cycle"], **summary, "outcomes": outcomes,
            })
        except OSError:
            summary.update(status="failed", error="persistence_failed")
        return summary
