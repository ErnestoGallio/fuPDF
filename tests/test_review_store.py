from dataclasses import replace
import json

import pytest

from pdf_annotator.services import review_store
from pdf_annotator.services.review_batches import ReviewSnapshot, plan_batches
from pdf_annotator.services.review_store import ReviewStore


def test_result_is_the_commit_and_survives_aggregate_failure(tmp_path, monkeypatch):
    snapshot = ReviewSnapshot("snapshot", "document", 2, 5, 6, "hash", 123, "private-path")
    plan = plan_batches(snapshot)
    first, second = plan.batches
    store = ReviewStore(tmp_path / "job")
    store.create("job", plan, {"currency": "USD"})
    first = replace(first, sha256="pdf-hash")
    store.set_batch(first, "in_flight")
    attempt = {"job_id": "job", "snapshot_id": "snapshot", "batch_id": first.batch_id, "attempt_id": "attempt",
               "recorded_at_utc": "now", "status": "in_flight"}
    store.record_attempt("attempt", attempt)
    assert store.collect()["pages"][0]["status"] == "uncertain"
    attempt.update(status="validated", sent=True, usage={"input_tokens": 4}, estimated_cost_usd="0.1")
    store.record_attempt("attempt", attempt)
    assert store.collect()["pages"][0]["error"] == "result_not_committed"

    real_replace = review_store.os.replace

    def fail_one(source, target):
        if target.parent.name == "results":
            raise OSError("simulated disk failure")
        return real_replace(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(review_store.os, "replace", fail_one)
        with pytest.raises(OSError):
            store.commit(first, attempt, {"corrections": []})
    assert store.collect()["completed_pages"] == 0
    assert not list(store.directory.rglob(".review-*.tmp"))
    store.commit(first, attempt, {"corrections": []})
    store.set_batch(second, "failed", "response_incomplete")
    report = ReviewStore(store.directory).collect()
    assert report["completed_pages"] == 4 and len(report["pages"]) == 6
    assert report["attempts"] == 1 and report["usage"] == {"input_tokens": 4}
    assert report["estimated_cost_usd"] == "0.1" and report["unknown_cost_attempts"] == 0
    store.publish(report)
    assert json.loads((store.directory / "corrections.json").read_text()) == {"corrections": []}
    assert "private-path" not in (store.directory / "manifest.json").read_text()
