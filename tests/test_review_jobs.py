from collections import Counter
from contextlib import nullcontext
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from threading import Event, Lock
from uuid import uuid4

import pytest

from pdf_annotator.services import review_jobs
from pdf_annotator.services.review_batches import ReviewSnapshot, context_variants, make_batch
from pdf_annotator.services.review_contract.metrics import TERRA_MODEL
from pdf_annotator.services.review_jobs import ReviewScheduler, ReviewSettings
from pdf_annotator.services.review_limits import RateLimits
from pdf_annotator.services.review_transport import OpenAIReviewTransport, TransportResult, classify_response
from pdf_annotator.worker.protocol import Operation
from .conftest import wait_until
from .test_save_m15 import _opened, manager
from .test_review_snapshot import source


def success(corrections=()):
    return TransportResult(200, {"status": "completed", "output": [{
        "type": "message", "status": "completed", "content": [{
            "type": "output_text", "text": json.dumps({"corrections": list(corrections)})}],
    }], "usage": {"input_tokens": 100, "output_tokens": 10,
                  "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}}})


def comment(page):
    return {"operation": "comment", "page_index": page, "original_text": None, "replacement_text": None,
            "insert_anchor": None, "context_before": None, "context_after": None,
            "reason": "Verifica unità", "category": "verification_needed"}


class MockTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, body):
        request = json.loads(body)
        pages = request["text"]["format"]["schema"]["properties"]["corrections"]["items"]["properties"]["page_index"]
        self.calls.append((pages["minimum"], pages["maximum"], request))
        return self.response(pages["minimum"], pages["maximum"])


class FakeWorker:
    def __init__(self, root, pages, *, too_large=(), max_length=4):
        self.root = root
        root.mkdir()
        self.closing = self.unavailable = False
        self.page_count = pages
        self.oversized = set(too_large)
        self.max_length = max_length
        self.extracts = {}
        self.released = False
        self.document_id = uuid4().hex

    @property
    def client(self):
        return self

    def review_reader(self):
        return nullcontext()

    async def capture(self, stopping):
        return ReviewSnapshot(uuid4().hex, self.document_id, 1, 3, self.page_count,
                              "snapshot-hash", 999, "snapshot-private-path")

    async def extract(self, snapshot, batch, max_context_pages, stopping):
        first, last = batch.target_pages[0], batch.target_pages[-1]
        if len(batch.target_pages) > self.max_length or set(batch.target_pages) & self.oversized:
            return replace(make_batch(snapshot, first, last, ()), status="too_large", size_bytes=50_000_000)
        context = next(value for value in context_variants(snapshot, first, last)
                       if len(value) <= max_context_pages)
        batch = make_batch(snapshot, first, last, context)
        data = bytes(page.page_index for page in batch.page_map)
        extract_id = uuid4().hex
        path = self.root / extract_id
        path.write_bytes(data)
        self.extracts[extract_id] = path
        return replace(batch, status="ready", extract_id=extract_id, path=str(path),
                       sha256=sha256(data).hexdigest(), size_bytes=len(data))

    async def release_extract(self, snapshot, extract_id):
        self.extracts.pop(extract_id).unlink()

    async def release(self, snapshot):
        assert not self.extracts
        self.released = True


class FakeClock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    async def sleep(self, duration):
        import asyncio
        self.now += duration
        await asyncio.sleep(0.001)


@pytest.fixture
def fake_scheduler(tmp_path, monkeypatch):
    monkeypatch.setattr(review_jobs, "ReviewWorker", lambda client: client)
    clock = FakeClock()
    scheduler = ReviewScheduler(tmp_path / "jobs", clock=clock, sleep=clock.sleep, jitter=lambda: 0)
    yield scheduler, clock
    scheduler.shutdown().result(15)


def test_retries_validation_selective_recovery_and_m20_aggregate(fake_scheduler, tmp_path):
    scheduler, clock = fake_scheduler
    worker = FakeWorker(tmp_path / "extracts", 12)
    counts = Counter()
    times = []

    def responses(first, last):
        counts[first] += 1
        if first == 0 and counts[first] <= 2:
            times.append(clock())
            return classify_response(429 if counts[first] == 1 else 503, {}, {"retry-after": "2"})
        if first == 4 and counts[first] == 1:
            return success([comment(3)])  # sent as context
        if first == 8 and counts[first] == 1:
            return TransportResult(error="remote_outcome_unknown", uncertain=True)
        return success([comment(first)] if first != 4 else [])

    transport = MockTransport(responses)
    job = scheduler.start(worker, transport, project="opaque-project", context={"opera": "  ", "destinatari": "classe"})
    partial = job.future.result(15)
    assert partial["status"] == "partial" and partial["completed_pages"] == 4
    assert [batch["status"] for batch in partial["batches"]] == ["completed", "failed", "uncertain"]
    assert counts == {0: 3, 4: 1, 8: 1}
    assert times[1] - times[0] >= 2
    assert not worker.extracts and not worker.released
    assert partial["unknown_cost_attempts"] == 3
    assert partial["attempts"] == 5
    assert partial["usage"]["output_tokens"] == 20
    assert partial["estimated_cost_usd"] == "0.00030"
    request = next(request for first, _, request in transport.calls if first == 4)
    assert request["store"] is False
    context, mapping, pdf = request["input"][0]["content"]
    assert json.loads(context["text"].split("\n", 1)[1]) == {"destinatari": "classe"}
    assert "1 → 3 (context)" in mapping["text"] and "2 → 4 (target)" in mapping["text"]
    assert pdf["type"] == "input_file"
    retry = scheduler.retry(job, (partial["batches"][1]["batch_id"],)).result(15)
    assert retry["status"] == "partial" and retry["completed_pages"] == 8
    assert counts == {0: 3, 4: 2, 8: 1}
    final = scheduler.retry(job, (partial["batches"][2]["batch_id"],)).result(15)
    assert final["status"] == "completed" and final["completed_pages"] == 12
    assert [item["page_index"] for item in final["corrections"]] == [0, 8]
    assert final["snapshot_id"] == partial["snapshot_id"]
    assert len(final["provenance"]) == 2 and worker.released
    assert final["attempts"] == 7
    assert final["model"] == TERRA_MODEL
    assert all(body["model"] == TERRA_MODEL and body["reasoning"] == {"effort": "medium"}
               for _, _, body in transport.calls)
    data = json.loads((job.store.directory / "corrections.json").read_text(encoding="utf-8"))
    assert data == {"corrections": final["corrections"]}
    manifest = (job.store.directory / "manifest.json").read_text(encoding="utf-8")
    assert "opaque-project" not in manifest and "snapshot-private-path" not in manifest
    assert "raw_response" not in str(job.store.collect())


def test_two_global_slots_and_cancellation_drain(tmp_path, monkeypatch):
    monkeypatch.setattr(review_jobs, "ReviewWorker", lambda client: client)
    scheduler = ReviewScheduler(tmp_path / "jobs")
    gate = Event()
    remaining = Event()
    lock = Lock()
    producing = 0
    peak = 0
    counts = Counter()

    def post(label, wait):
        def call(first, last):
            nonlocal producing, peak
            with lock:
                counts[label] += 1
                producing += 1
                peak = max(peak, producing)
                if producing == 2:
                    remaining.set()
            assert wait.wait(12)
            with lock:
                producing -= 1
            return success()
        return call

    paths = [FakeWorker(tmp_path / label, 8) for label in ("one", "two", "three")]
    settings = ReviewSettings(project_limits=RateLimits(30, 1_000_000))
    try:
        jobs = [scheduler.start(worker, MockTransport(post(label, gate)), project="shared", settings=settings)
                for label, worker in enumerate(paths)]
        assert remaining.wait(8)
        for job in jobs[:2]:
            job.cancel()
            assert not job.future.cancel()
        assert sum(counts.values()) == 2
        assert peak == 2
        gate.set()
        results = [job.future.result(15) for job in jobs]
        assert results[2]["status"] == "completed"
        assert results[0]["status"] == results[1]["status"] == "interrupted"
        assert sum(item["completed_pages"] for item in results[:2]) <= 8
        assert peak == 2
    finally:
        gate.set()
        scheduler.shutdown().result(15)


def test_split_by_tpm_and_file_size_and_report_all_pages(fake_scheduler, tmp_path):
    scheduler, _ = fake_scheduler
    worker = FakeWorker(tmp_path / "extracts", 8, too_large=(1,), max_length=2)
    transport = MockTransport(lambda _first, _last: success())
    settings = ReviewSettings(tokens_per_page=10_000, project_limits=RateLimits(60, 49_000))
    job = scheduler.start(worker, transport, project="project", settings=settings)
    report = job.future.result(15)
    assert report["status"] == "partial" and report["completed_pages"] == 7
    assert {item["page_index"]: item["status"] for item in report["pages"]} == {
        page: "too_large" if page == 1 else "completed" for page in range(8)
    }
    assert all(last - first < 2 for first, last, _ in transport.calls)
    assert all(not first <= 1 <= last for first, last, _ in transport.calls)
    assert worker.released


def test_unadmissible_single_page_has_terminal_outcome(fake_scheduler, tmp_path):
    scheduler, _ = fake_scheduler
    worker = FakeWorker(tmp_path / "extracts", 2)
    transport = MockTransport(lambda *_: success())
    settings = ReviewSettings(project_limits=RateLimits(rpm=10, tpm=1))
    report = scheduler.start(worker, transport, project="p", settings=settings).future.result(15)
    assert report["status"] == "failed"
    assert [page["error"] for page in report["pages"]] == ["batch_exceeds_rate_budget"] * 2
    assert not transport.calls and report["attempts"] == 0


def test_systemic_error_and_new_credential_only_retries_selected(fake_scheduler, tmp_path):
    scheduler, _ = fake_scheduler
    worker = FakeWorker(tmp_path / "extracts", 9)
    transport = MockTransport(lambda *_: classify_response(401, {}, {}))
    job = scheduler.start(worker, transport, project="first")
    report = job.future.result(15)
    assert [item["error"] for item in report["batches"]] == ["authentication_failed"] * 3
    assert len(transport.calls) == 1
    next_transport = MockTransport(lambda *_: success())
    middle = next(item["batch_id"] for item in report["batches"] if item["target_pages"][0] == 4)
    retry = scheduler.retry(job, (middle,), transport=next_transport, project="second").result(15)
    assert retry["completed_pages"] == 4 and len(next_transport.calls) == 1


def test_503_retries_stop_at_three_and_incomplete_usage_is_preserved(fake_scheduler, tmp_path):
    scheduler, _ = fake_scheduler
    worker = FakeWorker(tmp_path / "extracts", 8)
    incomplete = success()
    incomplete.body["status"] = "incomplete"
    transport = MockTransport(lambda first, _: classify_response(503, {}, {}) if first == 0 else incomplete)
    report = scheduler.start(worker, transport, project="p").future.result(15)
    assert Counter(first for first, _, _ in transport.calls) == {0: 3, 4: 1}
    assert [batch["error"] for batch in report["batches"]] == ["service_unavailable", "response_incomplete"]
    assert report["completed_pages"] == 0 and report["usage"]["input_tokens"] == 100


def test_proposals_with_active_credential_are_discarded_before_persistence(
        fake_scheduler, tmp_path, monkeypatch, caplog, capsys):
    scheduler, _ = fake_scheduler
    worker = FakeWorker(tmp_path / "extracts", 1)
    key = 'synthetic"key\\for-offline-tests'
    transport = OpenAIReviewTransport(key)
    accepted = comment(0)
    rejected = {**accepted, "reason": f"Sensitive reason: {key}"}
    rejected_anchor = {**accepted, "operation": "insert", "category": "grammar",
                       "insert_anchor": {"text": key, "side": "before"}, "replacement_text": "Testo"}
    mock = MockTransport(lambda *_: success([rejected, accepted, rejected_anchor]))
    monkeypatch.setattr(transport, "post", mock.post)

    job = scheduler.start(worker, transport, project="opaque-project")
    report = job.future.result(15)
    assert len(mock.calls) == 1 and report["status"] == "completed"
    assert report["corrections"] == [accepted]
    attempt, = (job.store.directory / "attempts").glob("*.json")
    diagnostic = json.loads(attempt.read_text(encoding="utf-8"))
    assert diagnostic["diagnostic"] == "proposal_contains_active_credential"
    assert diagnostic["discarded_proposals"] == 2
    captured = capsys.readouterr()
    diagnostics = [captured.out, captured.err, caplog.text, str(report)]
    diagnostics.extend(path.read_text(encoding="utf-8") for path in job.store.directory.rglob("*") if path.is_file())
    assert all(key not in value and json.dumps(key)[1:-1] not in value and "Sensitive reason" not in value
               for value in diagnostics)


def test_persistence_failure_stops_admission_preserving_committed_batch(fake_scheduler, tmp_path, monkeypatch):
    scheduler, _ = fake_scheduler
    worker = FakeWorker(tmp_path / "extracts", 12)
    original = review_jobs.ReviewStore.commit
    attempted = []

    def commit(store, batch, attempt, corrections):
        attempted.append(batch.batch_id)
        if len(attempted) == 2:
            raise OSError("simulated disk error")
        return original(store, batch, attempt, corrections)

    monkeypatch.setattr(review_jobs.ReviewStore, "commit", commit)
    transport = MockTransport(lambda *_: success())
    job = scheduler.start(worker, transport, project="p")
    result = job.future.result(15)
    assert result["error"] == "persistence_failed" and result["completed_pages"] == 4
    by_id = {batch["batch_id"]: batch for batch in result["batches"]}
    assert by_id[attempted[0]]["status"] == "completed"
    assert by_id[attempted[1]]["error"] == "persistence_failed"
    assert len(transport.calls) == 2
    assert job.store.collect()["completed_pages"] == 4


def test_actual_worker_save_barrier_preserves_late_extract_reply(tmp_path, manager, pdf_fixture_copy, monkeypatch):
    session = _opened(manager, pdf_fixture_copy(tmp_path / "source.pdf"))
    scheduler = ReviewScheduler(tmp_path / "jobs", jitter=lambda: 0)
    submitted = Event()
    can_poll = Event()
    worker = session.worker
    original_submit, original_poll = worker.submit, worker.poll
    first_extract = None

    def submit(operation, payload=None):
        nonlocal first_extract
        request = original_submit(operation, payload)
        if operation == Operation.REVIEW_EXTRACT and request and first_extract is None:
            first_extract = request
            submitted.set()
        return request

    def poll(request_id=None, *, include_review=True):
        if request_id is not None and request_id == first_extract and not can_poll.is_set():
            return None
        return original_poll(request_id, include_review=include_review)

    monkeypatch.setattr(worker, "submit", submit)
    monkeypatch.setattr(worker, "poll", poll)
    transport = MockTransport(lambda *_: success())
    results = []
    manager.save_finished.connect(lambda doc, ok: results.append((doc, ok)))
    try:
        start_generation = session.document_generation
        job = scheduler.start(worker, transport, project="p", first=0, last=0)
        wait_until(submitted.is_set)
        assert manager.saves.request(session.document_id, str(tmp_path / "saved.pdf"))
        wait_until(lambda: worker.completed_counts[Operation.REVIEW_EXTRACT] == 1)
        # Ordinary Save draining cannot take the review-owned response.
        assert worker.is_pending(first_extract)
        assert not results and session.saving
        can_poll.set()
        wait_until(lambda: bool(results) and job.future.done(), timeout=30)
        assert results == [(session.document_id, True)]
        assert session.document_generation == start_generation + 1
        assert job.future.result()["status"] == "completed"
        assert job.future.result()["completed_pages"] == 1 and len(transport.calls) == 1
        assert job.snapshot is None
        manifest = json.loads((job.store.directory / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["snapshot"]["document_generation"] == start_generation
        assert worker.completed_counts[Operation.REVIEW_RELEASE_EXTRACT] == 1
    finally:
        can_poll.set()
        shutdown = scheduler.shutdown()
        wait_until(shutdown.done, timeout=30)
        shutdown.result()


def test_actual_worker_regenerates_same_extract_for_retry(tmp_path, manager, source):
    session = _opened(manager, source)
    clock = FakeClock()
    scheduler = ReviewScheduler(tmp_path / "jobs", clock=clock, sleep=clock.sleep, jitter=lambda: 0)
    replies = iter([classify_response(503, {}, {}), success()])
    transport = MockTransport(lambda *_: next(replies))
    try:
        before = Path(session.canonical_path).read_bytes()
        job = scheduler.start(session.worker, transport, project="p", first=1, last=2)
        wait_until(job.future.done, timeout=30)
        report = job.future.result()
        assert report["status"] == "completed"
        assert transport.calls[0] == transport.calls[1]
        assert session.document_revision == 0 and not session.dirty
        assert Path(session.canonical_path).read_bytes() == before
    finally:
        shutdown = scheduler.shutdown()
        wait_until(shutdown.done, timeout=30)
        shutdown.result()
