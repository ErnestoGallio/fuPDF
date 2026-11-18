"""Local review ledger. The per-batch result file is the coverage commit."""

from dataclasses import asdict
from decimal import Decimal
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from .review_batches import ReviewBatch, ReviewPlan


def review_root() -> Path:
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return local / "fuPDF" / "review-jobs"


def write_json(path: Path, value: Any) -> None:
    temp = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent,
                                prefix=".review-", suffix=".tmp", delete=False) as stream:
            temp = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def batch_record(batch: ReviewBatch, model: str | None = None) -> dict:
    record = asdict(batch)
    del record["path"]
    if model is not None:
        record["model"] = model
    return record


def _batch_name(batch_id: str) -> str:
    # Internal batch IDs are scoped to this job's snapshot. Persist only the
    # interval in the filesystem name and keep the complete ID in the JSON.
    _, interval = batch_id.rsplit(":", 1)
    first, last = interval.split("-")
    return f"{int(first)}-{int(last)}.json"


class ReviewStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._model: str | None = None

    def create(self, job_id: str, plan: ReviewPlan, configuration: dict) -> None:
        self._model = configuration.get("model")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        for name in ("batches", "results", "attempts"):
            (self.directory / name).mkdir(mode=0o700)
        snapshot = asdict(plan.snapshot)
        del snapshot["path"]
        write_json(self.directory / "manifest.json", {
            "version": 1, "job_id": job_id, "snapshot": snapshot,
            "first_page": plan.first_page, "last_page": plan.last_page,
            "roots": [batch_record(batch, self._model) for batch in plan.batches],
            "configuration": configuration,
        })

    def set_batch(self, batch: ReviewBatch, status: str, error: str | None = None,
                  children: tuple[ReviewBatch, ...] = ()) -> None:
        write_json(self.directory / "batches" / _batch_name(batch.batch_id), {
            "batch": batch_record(batch, self._model), "status": status, "error": error,
            "children": [batch_record(child, self._model) for child in children],
        })

    def record_attempt(self, attempt_id: str, attempt: dict) -> None:
        write_json(self.directory / "attempts" / f"{attempt_id}.json", attempt)

    def commit(self, batch: ReviewBatch, attempt: dict, corrections: dict) -> None:
        path = self.directory / "results" / _batch_name(batch.batch_id)
        if path.exists():
            raise FileExistsError("Result already committed")
        write_json(path, {
            "job_id": attempt["job_id"], "snapshot_id": attempt["snapshot_id"],
            "attempt_id": attempt["attempt_id"], "batch": batch_record(batch, self._model),
            **corrections,
        })

    def collect(self) -> dict:
        """Rebuild the aggregate from atomically published records, including after interruption."""
        manifest = read_json(self.directory / "manifest.json")
        model = manifest["configuration"].get("model")
        attempts = [read_json(path) for path in (self.directory / "attempts").glob("*.json")]
        attempts.sort(key=lambda attempt: (attempt["recorded_at_utc"], attempt["attempt_id"]))
        latest = {attempt["batch_id"]: attempt for attempt in attempts}
        leaves = []
        corrections: list[dict] = []
        provenance = []
        pages = []
        queue = list(reversed(manifest["roots"]))
        while queue:
            batch = queue.pop()
            batch_id = batch["batch_id"]
            name = _batch_name(batch_id)
            path = self.directory / "batches" / name
            state = read_json(path) if path.exists() else {}
            if state.get("status") == "split":
                queue.extend(reversed(state["children"]))
                continue
            path = self.directory / "results" / name
            status, error = state.get("status", "pending"), state.get("error")
            if path.exists():
                result = read_json(path)
                status, error = "completed", None
                batch = result["batch"]
                for index, correction in enumerate(result["corrections"]):
                    provenance.append({
                        "index": len(corrections), "batch_index": index,
                        "job_id": result["job_id"], "snapshot_id": result["snapshot_id"],
                        "batch_id": batch_id, "attempt_id": result["attempt_id"],
                    })
                    corrections.append(correction)
            elif status in ("pending", "in_flight", "retry_wait"):
                attempt = latest.get(batch_id, {})
                if attempt.get("status") == "in_flight":
                    status, error = "uncertain", "remote_outcome_unknown"
                elif attempt.get("status") == "validated":
                    status, error = "failed", "result_not_committed"
                elif attempt.get("status") in ("failed", "uncertain", "cancelled", "retry_wait"):
                    status, error = attempt["status"], attempt.get("error")
                elif status == "in_flight":
                    status, error = "pending", "attempt_not_recorded"
            leaves.append({**batch, "model": batch.get("model", model), "status": status, "error": error})
            pages.extend({"page_index": page, "batch_id": batch_id, "status": status, "error": error}
                         for page in batch["target_pages"])
        costs = [Decimal(item["estimated_cost_usd"]) for item in attempts
                 if item.get("estimated_cost_usd") is not None]
        expected = [item for item in attempts if item.get("sent", True)]
        usage = {}
        for item in expected:
            for key, value in item.get("usage", {}).items():
                if value is not None:
                    usage[key] = usage.get(key, 0) + value
        return {
            "job_id": manifest["job_id"], "snapshot_id": manifest["snapshot"]["snapshot_id"],
            "model": model,
            "batches": leaves, "pages": pages,
            "completed_pages": sum(page["status"] == "completed" for page in pages),
            "corrections": corrections, "provenance": provenance,
            "attempts": len(expected), "usage": usage,
            "estimated_cost_usd": str(sum(costs, Decimal(0))) if costs else None,
            "unknown_cost_attempts": len(expected) - len(costs),
        }

    def publish(self, report: dict) -> None:
        write_json(self.directory / "corrections.json", {"corrections": report["corrections"]})
        write_json(self.directory / "report.json", {key: value for key, value in report.items()
                                                    if key != "corrections"})
