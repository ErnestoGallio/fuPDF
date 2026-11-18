"""Background analysis cycles over immutable snapshots, with no PDF mutations."""

import asyncio
from collections import deque
from concurrent.futures import Future
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
from hashlib import sha256
import json
from pathlib import Path
from queue import Empty, Queue
from random import random
from threading import Event, Lock, Thread
from time import monotonic
from uuid import uuid4

from .review_application import DeferredReviewApplication, analysis_can_apply
from .review_batches import (
    REVIEW_PDF_LIMIT, ReviewBatch, ReviewSnapshot, context_variants, make_batch,
    plan_batches, validate_correction_pages,
)
from .review_contract.corrections import parse_corrections
from .review_contract.metrics import DEFAULT_MODEL, SOL_MODEL, STANDARD_RATES, Rates, estimated_cost, get_usage
from .review_contract.prompt_profiles import DEFAULT_PROMPT_PROFILE, PROMPT_PROFILES
from .review_contract.responses import (
    PageInterval, RunFailure, compose_mapped_prompt, normalize_context, request_body, response_text,
)
from .review_limits import RateController, RateLimits
from .review_store import ReviewStore, batch_record, read_json, review_root
from .review_transport import MAX_RATE_WAIT, OpenAIReviewTransport, ReviewTransport, TransportResult
from .review_worker import ReviewWorker, ReviewWorkerError
from ..worker.client import WorkerClient


MAX_REQUEST_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class ReviewSettings:
    model: str = DEFAULT_MODEL
    reasoning: str = "medium"
    profile: str = DEFAULT_PROMPT_PROFILE
    max_output_tokens: int = 8192
    tokens_per_page: int = 4000
    rates: Rates | None = None
    model_family: str | None = None
    model_context_tokens: int | None = None
    project_limits: RateLimits = RateLimits()
    model_limits: RateLimits = RateLimits()

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or self.model not in STANDARD_RATES:
            raise ValueError("Unknown review model")
        if self.profile not in PROMPT_PROFILES:
            raise ValueError("Unknown review profile")
        efforts = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
        if self.reasoning not in (efforts[2:] if self.model == SOL_MODEL else efforts):
            raise ValueError("Invalid reasoning effort")
        if (type(self.max_output_tokens) is not int or self.max_output_tokens < 16
                or self.max_output_tokens > 128_000
                or type(self.tokens_per_page) is not int or self.tokens_per_page < 1):
            raise ValueError("Invalid token budget")
        if self.rates is not None and any(not rate.is_finite() or rate < 0
                                         for rate in asdict(self.rates).values()):
            raise ValueError("Invalid token pricing")
        if self.model_context_tokens is not None and (
                type(self.model_context_tokens) is not int or self.model_context_tokens < 1):
            raise ValueError("Invalid model context window")

    @property
    def family(self) -> str:
        return self.model_family or self.model

    @property
    def pricing(self) -> Rates:
        return self.rates or STANDARD_RATES[self.model]

    @property
    def context_window(self) -> int:
        return self.model_context_tokens or 1_050_000


@dataclass(eq=False)
class ReviewJob:
    job_id: str
    store: ReviewStore
    settings: ReviewSettings
    _worker: ReviewWorker = field(repr=False)
    _project: str = field(repr=False)
    _transport: ReviewTransport = field(repr=False)
    _context: dict[str, str] = field(repr=False)
    _application: DeferredReviewApplication | None = field(default=None, repr=False)
    phase: str = field(default="analysis", init=False)
    progress: "ReviewProgress | None" = field(default=None, init=False)
    future: Future = field(init=False, repr=False)
    snapshot: ReviewSnapshot | None = field(default=None, init=False, repr=False)
    _instructions: str = field(default="", init=False, repr=False)
    _cancel: Event = field(default_factory=Event, init=False, repr=False)
    _closing: Future | None = field(default=None, init=False, repr=False)
    _cycle: int = field(default=0, init=False)
    _unpublished_status: str | None = field(default=None, init=False)
    _base_tokens: int = field(default=0, init=False)
    _extract_hashes: dict[tuple, str] = field(default_factory=dict, init=False, repr=False)

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def can_retry(self) -> bool:
        return (self.future.done() and self.snapshot is not None and self._closing is None
                and not self._worker.unavailable)

    @property
    def can_recover_publication(self) -> bool:
        return self._unpublished_status is not None and self.can_retry


@dataclass(frozen=True)
class ReviewProgress:
    pages_total: int
    batches_total: int
    pages_analyzed: int = 0
    batches_completed: int = 0
    batches_failed: int = 0
    corrections: int = 0
    cost_usd: Decimal | None = None
    unknown_cost_attempts: int = 0
    rate_waiting_batches: int = 0

    @classmethod
    def from_report(cls, report: dict, retrying: frozenset[str] = frozenset()) -> "ReviewProgress":
        cost = report["estimated_cost_usd"]
        return cls(len(report["pages"]), len(report["batches"]), report["completed_pages"],
                   sum(batch["status"] == "completed" for batch in report["batches"]),
                   sum(batch["status"] != "completed" and batch["batch_id"] not in retrying
                       for batch in report["batches"]),
                   len(report["corrections"]), Decimal(cost) if cost is not None else None,
                   report["unknown_cost_attempts"])


@dataclass
class Cycle:
    error: str | None = None
    suspended: str | None = None
    rate_waiting: set[str] = field(default_factory=set)

    def set_rate_wait(self, job: ReviewJob, batch_id: str, waiting: bool) -> None:
        if waiting:
            self.rate_waiting.add(batch_id)
        else:
            self.rate_waiting.discard(batch_id)
        if (progress := job.progress) is not None and progress.rate_waiting_batches != len(self.rate_waiting):
            job.progress = replace(progress, rate_waiting_batches=len(self.rate_waiting))


@dataclass(frozen=True)
class BatchOutcome:
    batch: ReviewBatch
    status: str
    error: str | None = None
    children: tuple[ReviewBatch, ...] = ()
    correction_count: int = 0


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_extract(client: WorkerClient, batch: ReviewBatch) -> bytes:
    if not batch.path or not batch.sha256 or not batch.extract_id:
        raise ReviewWorkerError("invalid_extract")
    try:
        with client.review_reader(), Path(batch.path).open("rb") as stream:
            pdf = stream.read(REVIEW_PDF_LIMIT + 1)
    except OSError:
        raise ReviewWorkerError("extract_read_failed") from None
    if not 0 < len(pdf) <= REVIEW_PDF_LIMIT or len(pdf) != batch.size_bytes or sha256(pdf).hexdigest() != batch.sha256:
        raise ReviewWorkerError("extract_changed")
    return pdf


class ReviewScheduler:
    """One instance per application. Public methods enqueue work and return immediately."""

    def __init__(self, root: Path | None = None, *, clock=monotonic,
                 sleep=asyncio.sleep, jitter=random) -> None:
        self.root = review_root() if root is None else root
        self._clock, self._sleep, self._jitter = clock, sleep, jitter
        self._rates = RateController()
        self._staging = asyncio.Semaphore(2)
        self._jobs: dict[str, ReviewJob] = {}
        self._lock = Lock()
        self._shutdown: Future | None = None
        self._submissions: Queue = Queue()
        self._thread = Thread(target=self._run_loop, name="review-scheduler", daemon=True)
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.run(self._serve())

    async def _serve(self) -> None:
        active: set[asyncio.Task] = set()
        while self._shutdown is None or not self._shutdown.done() or active:
            try:
                coroutine, public = self._submissions.get_nowait()
            except Empty:
                await asyncio.sleep(0.01)
                continue
            task = asyncio.create_task(self._complete(coroutine, public))
            active.add(task)
            task.add_done_callback(active.discard)

    @staticmethod
    async def _complete(coroutine, public: Future) -> None:
        try:
            public.set_result(await coroutine)
        except Exception as exc:
            public.set_exception(exc)

    def _submit(self, coroutine) -> Future:
        # Cancellation is a job operation. Cancelling the public Future must
        # never free a slot while its blocking HTTPS call still owns it.
        public = Future()
        public.set_running_or_notify_cancel()

        self._submissions.put_nowait((coroutine, public))
        return public

    def start(self, client: WorkerClient, transport: ReviewTransport, *, project: str,
              settings: ReviewSettings = ReviewSettings(), context: dict[str, str] | None = None,
              first: int = 0, last: int | None = None,
              application: DeferredReviewApplication | None = None) -> ReviewJob:
        if not project:
            raise ValueError("Expected an opaque project or credential ID")
        effective = normalize_context({} if context is None else context)
        self._check_metadata(transport, settings, effective)
        with self._lock:
            if self._shutdown is not None or any(job._worker.client is client for job in self._jobs.values()):
                raise ValueError("Review scheduler or document already in use")
            job_id = uuid4().hex
            job = ReviewJob(job_id, ReviewStore(self.root / job_id), settings, ReviewWorker(client),
                            project, transport, effective, application)
            self._jobs[job_id] = job
            job.future = self._submit(self._start(job, first, last))
            return job

    def retry(self, job: ReviewJob, batch_ids: tuple[str, ...], *,
              transport: ReviewTransport | None = None, project: str | None = None) -> Future:
        """Explicitly re-run selected incomplete leaves against the retained snapshot."""
        ids = frozenset(batch_ids)
        if not ids or (transport is None) != (project is None) or project == "":
            raise ValueError("Select failed batch IDs and supply both fields for a new credential")
        if transport is not None:
            self._check_metadata(transport, job.settings, job._context)
        with self._lock:
            if (self._shutdown is not None or self._jobs.get(job.job_id) is not job
                    or job._closing is not None or not job.future.done() or job.snapshot is None):
                raise ValueError("Review snapshot is not available for retry")
            job._cancel.clear()
            job.phase = "analysis"
            job.future = self._submit(self._retry(job, ids, transport, project))
            return job.future

    def resume_application(self, job: ReviewJob) -> Future:
        """Re-read committed proposals and reconcile any earlier mutation intent."""
        with self._lock:
            if (self._shutdown is not None or self._jobs.get(job.job_id) is not job
                    or job._closing is not None or job._application is None or not job.future.done()):
                raise ValueError("No settled review application to resume")
            job._cancel.clear()
            job.future = self._submit(self._resume_application(job))
            return job.future

    async def _resume_application(self, job: ReviewJob) -> dict:
        if job._unpublished_status is not None:
            try:
                snapshot = job.snapshot
                if snapshot is None or job._worker.unavailable:
                    raise ValueError("Review snapshot is unavailable")
                analysis = job.store.collect()
                status = job._unpublished_status
                analysis.update(status=status, error=None, cycle=job._cycle)
                if (analysis["job_id"] != job.job_id or analysis["snapshot_id"] != snapshot.snapshot_id
                        or (status == "completed") != (analysis["completed_pages"] == len(analysis["pages"]))
                        or not analysis_can_apply(analysis)):
                    raise ValueError("Review analysis identity or coverage changed")
            except (OSError, ValueError, KeyError, TypeError):
                return {"status": "failed", "error": "review_recovery_unavailable", "cycle": job._cycle,
                        "application": {"status": "deferred"}}
            self._publish_analysis(job, analysis)
            return await self._apply(job, analysis)
        published = read_json(job.store.directory / "report.json")
        analysis = {**published, **job.store.collect()}
        if (published["cycle"] != job._cycle or published["job_id"] != job.job_id
                or published["snapshot_id"] != analysis["snapshot_id"] or not analysis_can_apply(analysis)):
            raise ValueError("Review analysis has not reached an eligible outcome")
        return await self._apply(job, analysis)

    def close(self, job: ReviewJob) -> Future:
        """Stop admission, drain active work and release the snapshot asynchronously."""
        with self._lock:
            if job._closing is not None and (not job._closing.done() or self._jobs.get(job.job_id) is not job):
                return job._closing
            if self._jobs.get(job.job_id) is not job or self._shutdown is not None:
                raise ValueError("Unknown review job or scheduler shutting down")
            job.cancel()
            job._closing = self._submit(self._close(job))
            return job._closing

    def shutdown(self) -> Future:
        with self._lock:
            if self._shutdown is None:
                closing = []
                for job in self._jobs.values():
                    job.cancel()
                    if job._closing is None or job._closing.done():
                        job._closing = self._submit(self._close(job))
                    closing.append(job._closing)
                self._shutdown = self._submit(self._finish_shutdown(closing))
            return self._shutdown

    async def _finish_shutdown(self, closing: list[Future]) -> None:
        results = await asyncio.gather(*(asyncio.wrap_future(item) for item in closing), return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                raise result

    @staticmethod
    def _check_metadata(transport: ReviewTransport, settings: ReviewSettings, context: dict[str, str]) -> None:
        if isinstance(transport, OpenAIReviewTransport):
            transport.check_local_metadata(settings.model, settings.profile, settings.family, *context.values())

    async def _start(self, job: ReviewJob, first: int, last: int | None) -> dict:
        settings = job.settings
        try:
            job._instructions = PROMPT_PROFILES[settings.profile].path.read_text(encoding="utf-8")
            if isinstance(job._transport, OpenAIReviewTransport):
                job._transport.check_local_metadata(job._instructions)
            job._base_tokens = (len(job._instructions.encode("utf-8"))
                                + len(json.dumps(job._context, ensure_ascii=False).encode("utf-8"))
                                + 1024 + settings.max_output_tokens)
            job.snapshot = await job._worker.capture(job._cancel.is_set)
            plan = plan_batches(job.snapshot, first, last)
            job.store.create(job.job_id, plan, {
                "model": settings.model, "reasoning": settings.reasoning, "profile": settings.profile,
                "model_family": settings.family, "max_output_tokens": settings.max_output_tokens,
                "model_context_tokens": settings.context_window,
                "request_timeout_seconds": getattr(job._transport, "timeout", None),
                "tokens_per_page": settings.tokens_per_page, "project_limits": asdict(settings.project_limits),
                "model_limits": asdict(settings.model_limits),
                "prompt_sha256": sha256(job._instructions.encode("utf-8")).hexdigest(),
                "editorial_context": job._context, "currency": "USD",
                "rates_per_million": {key: str(value) for key, value in asdict(settings.pricing).items()},
            })
            job.progress = ReviewProgress(plan.last_page - plan.first_page + 1, len(plan.batches))
        except (OSError, UnicodeError, ValueError, ReviewWorkerError) as exc:
            cleanup_error = await self._try_release(job)
            code = str(exc) if isinstance(exc, ReviewWorkerError) else "review_setup_failed"
            job.phase = "finished"
            return {"job_id": job.job_id, "status": "interrupted" if code == "cancelled" else "failed",
                    "error": code, "cleanup_error": cleanup_error}
        return await self._run_cycle(job, plan.batches)

    async def _retry(self, job: ReviewJob, ids: frozenset[str], transport: ReviewTransport | None,
                     project: str | None) -> dict:
        snapshot = job.snapshot
        if snapshot is None or job._worker.unavailable:
            raise ValueError("Review snapshot is unavailable")
        persisted = job.store.collect()
        eligible = {item["batch_id"]: item for item in persisted["batches"]
                    if item["status"] not in ("completed", "too_large")}
        if not ids <= eligible.keys():
            raise ValueError("Retry applies only to selected incomplete batches")
        if transport is not None and project is not None:
            job._transport, job._project = transport, project
            for budget in self._rates.budgets(project, job.settings.family):
                budget.blocked = None
        batches = []
        for item in eligible.values():
            if item["batch_id"] in ids:
                first, last = item["target_pages"][0], item["target_pages"][-1]
                batches.append(make_batch(snapshot, first, last, next(context_variants(snapshot, first, last))))
        job.progress = ReviewProgress.from_report(persisted, ids)
        return await self._run_cycle(job, tuple(batches))

    async def _run_cycle(self, job: ReviewJob, batches: tuple[ReviewBatch, ...]) -> dict:
        job._cycle += 1
        job._unpublished_status = None
        settings = job.settings
        self._rates.configure(job._project, settings.family, settings.project_limits, settings.model_limits)
        queue = deque(batches)
        active: set[asyncio.Task] = set()
        cycle = Cycle()
        while queue or active:
            while queue and len(active) < 2:
                active.add(asyncio.create_task(self._run_batch(job, queue.popleft(), cycle)))
            done, active = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                outcome = task.result()
                cycle.set_rate_wait(job, outcome.batch.batch_id, False)
                try:
                    if outcome.status != "completed":
                        job.store.set_batch(outcome.batch, outcome.status, outcome.error, outcome.children)
                    queue.extendleft(reversed(outcome.children))
                    self._update_progress(job, outcome)
                except OSError:
                    cycle.error = "persistence_failed"
        report = job.store.collect()
        job.progress = ReviewProgress.from_report(report)
        if job._cancel.is_set() or job._worker.client.closing:
            status = "interrupted"
        elif cycle.suspended:
            status = "suspended"
        elif report["completed_pages"] == len(report["pages"]):
            status = "completed"
        else:
            status = "partial" if report["completed_pages"] else "failed"
        report.update(status=status, error=cycle.error or cycle.suspended, cycle=job._cycle)
        if job._application is None and self._analysis_exhausted(report):
            report["cleanup_error"] = await self._try_release(job)
        self._publish_analysis(job, report)
        if job._application is not None:
            return await self._apply(job, report)
        job.phase = "finished"
        return report

    @staticmethod
    def _publish_analysis(job: ReviewJob, report: dict) -> None:
        job._unpublished_status = None
        try:
            job.store.publish(report)
        except OSError:
            if job._application is not None and analysis_can_apply(report):
                job._unpublished_status = report["status"]
            report["error"] = "persistence_failed"

    @staticmethod
    def _update_progress(job: ReviewJob, outcome: BatchOutcome) -> None:
        progress = job.progress
        if progress is None:
            return
        if outcome.status == "split":
            job.progress = replace(progress, batches_total=progress.batches_total + len(outcome.children) - 1)
        elif outcome.status == "completed":
            job.progress = replace(progress, pages_analyzed=progress.pages_analyzed + len(outcome.batch.target_pages),
                                   batches_completed=progress.batches_completed + 1,
                                   corrections=progress.corrections + outcome.correction_count)
        else:
            job.progress = replace(progress, batches_failed=progress.batches_failed + 1)

    @staticmethod
    def _analysis_exhausted(report: dict) -> bool:
        return all(item["status"] in ("completed", "too_large") for item in report["batches"])

    async def _apply(self, job: ReviewJob, report: dict) -> dict:
        if analysis_can_apply(report) and not job._cancel.is_set() and not job._worker.unavailable:
            job.phase = "application"
        try:
            report["application"] = await job._application.run(
                job._worker, job.snapshot, job.store, report, job._cancel.is_set)
            if (self._analysis_exhausted(report) and not report["application"]["uncertain"]
                    and not report["application"]["not_attempted"]
                    and report["application"]["status"] == "completed"):
                report["cleanup_error"] = await self._try_release(job)
        except (OSError, ValueError, KeyError) as exc:
            report["application"] = {"status": "failed", "error": (
                "persistence_failed" if isinstance(exc, OSError) else "invalid_application_ledger")}
        finally:
            job.phase = "finished"
        return report

    def _estimate(self, job: ReviewJob, pages: int) -> int:
        return self._rates.estimate(job._project, job.settings.family, pages,
                                    job._base_tokens, job.settings.tokens_per_page)

    def _stop_reason(self, job: ReviewJob, cycle: Cycle) -> str | None:
        if job._cancel.is_set() or job._worker.client.closing:
            return "cancelled"
        if job._worker.unavailable:
            return "worker_unavailable"
        return cycle.error

    def _split(self, snapshot: ReviewSnapshot, batch: ReviewBatch, reason: str) -> BatchOutcome:
        first, last = batch.target_pages[0], batch.target_pages[-1]
        if first == last:
            return BatchOutcome(batch, "too_large" if reason == "pdf_too_large" else "failed", reason)
        middle = first + (last - first + 1) // 2
        children = tuple(make_batch(snapshot, start, stop, next(context_variants(snapshot, start, stop)))
                         for start, stop in ((first, middle - 1), (middle, last)))
        return BatchOutcome(batch, "split", reason, children)

    async def _run_batch(self, job: ReviewJob, batch: ReviewBatch, cycle: Cycle) -> BatchOutcome:
        try:
            return await self._attempt_batch(job, batch, cycle)
        except OSError:
            cycle.error = "persistence_failed"
            return BatchOutcome(batch, "failed", cycle.error)
        except ReviewWorkerError as exc:
            error = str(exc)
            if error == "REVIEW_CLEANUP_FAILED":
                cycle.error = error
            stopped = self._stop_reason(job, cycle)
            return BatchOutcome(batch, "cancelled" if stopped == "cancelled" else "failed", stopped or error)

    async def _attempt_batch(self, job: ReviewJob, batch: ReviewBatch, cycle: Cycle) -> BatchOutcome:
        snapshot = job.snapshot
        assert snapshot is not None
        estimate_pages = len(batch.target_pages)
        first_attempt = self._clock()
        retry_at, count = first_attempt, 0
        project, family = job._project, job.settings.family
        while True:
            reason = self._stop_reason(job, cycle)
            if reason:
                return BatchOutcome(batch, "cancelled" if reason == "cancelled" else "failed", reason)
            estimate = self._estimate(job, estimate_pages)
            now = self._clock()
            # The FIFO staging semaphore gives ready jobs a turn once a
            # global slot opens. Only rate-blocked work waits outside it.
            delay, reason = self._rates.availability(project, family, estimate, now, check_global_slots=False)
            context_window = job.settings.context_window
            if not reason and context_window is not None and estimate > context_window:
                reason = "model_context_exceeded"
            if reason in ("batch_exceeds_rate_budget", "model_context_exceeded"):
                if estimate_pages > len(batch.target_pages):
                    estimate_pages = len(batch.target_pages)
                    continue
                return self._split(snapshot, batch, reason)
            if reason:
                if reason in ("authentication_failed", "quota_exhausted", "permission_denied", "model_unavailable"):
                    cycle.suspended = reason
                return BatchOutcome(batch, "failed", reason)
            delay = max(delay, retry_at - now)
            cycle.set_rate_wait(job, batch.batch_id, delay > 0 and (
                retry_at > now or self._rates.budget_delay(project, family, estimate, now) > 0))
            if delay > 0:
                if now + delay - first_attempt > MAX_RATE_WAIT:
                    return BatchOutcome(batch, "failed", "rate_wait_exceeded")
                await self._sleep(min(delay, 0.25))
                continue
            async with self._staging:
                if (self._stop_reason(job, cycle) or self._rates.availability(
                        project, family, estimate, self._clock()) != (0.0, None)):
                    continue
                limit = min((value for value in (self._rates.token_limit(project, family), context_window)
                             if value is not None), default=None)
                max_context = len(batch.context_pages)
                while max_context and limit is not None and self._estimate(
                        job, len(batch.target_pages) + max_context) > limit:
                    max_context -= 1
                prepared = await job._worker.extract(snapshot, batch, max_context,
                                                     lambda: self._stop_reason(job, cycle) is not None)
                if prepared.status == "too_large":
                    return self._split(snapshot, prepared, "pdf_too_large")
                try:
                    pdf = await asyncio.to_thread(_read_extract, job._worker.client, prepared)
                finally:
                    await job._worker.release_extract(snapshot, prepared.extract_id or "")
                if self._stop_reason(job, cycle):
                    del pdf
                    continue
                key = (prepared.batch_id, prepared.page_map)
                prior_hash = job._extract_hashes.setdefault(key, prepared.sha256)
                if prepared.sha256 != prior_hash:
                    return BatchOutcome(prepared, "failed", "regenerated_extract_changed")
                estimate_pages = len(prepared.page_map)
                estimate = self._estimate(job, estimate_pages)
                delay, reason = self._rates.availability(project, family, estimate, self._clock())
                if reason or delay:
                    del pdf
                    continue
                outcome, new_retry = await self._send(job, prepared, pdf, estimate, count + 1, cycle)
                del pdf
                if outcome is not None:
                    if outcome.error == "model_context_exceeded":
                        return self._split(snapshot, prepared, outcome.error)
                    return outcome
                if new_retry is not None:
                    count += 1
                    retry_at = new_retry
            await self._sleep(0)

    async def _send(self, job: ReviewJob, batch: ReviewBatch, pdf: bytes, estimate: int,
                    count: int, cycle: Cycle) -> tuple[BatchOutcome | None, float | None]:
        settings = job.settings
        pages = PageInterval(batch.target_pages[0], batch.target_pages[-1])
        page_map = ", ".join(f"{page.extract_index + 1} → {page.page_index} ({page.role})"
                             for page in batch.page_map)
        mapping = (f"Posizione nel PDF allegato (da 1) → page_index fisico originale (da 0): {page_map}. "
                   "Ignora la numerazione stampata. Proponi correzioni soltanto per le pagine target. "
                   "Le pagine context servono solo come riferimento.")
        prompt = compose_mapped_prompt(job._instructions, mapping, job._context)
        prompt_json = json.dumps(prompt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        prompt_bytes = prompt_json.encode("utf-8")
        if len(prompt_bytes) + 4 * ((len(pdf) + 2) // 3) > MAX_REQUEST_BYTES:
            return BatchOutcome(batch, "failed", "request_size_limit"), None
        body = await asyncio.to_thread(request_body, pdf, pages, settings.model, settings.reasoning,
                                       settings.max_output_tokens, prompt)
        if len(body) > MAX_REQUEST_BYTES:
            return BatchOutcome(batch, "failed", "request_size_limit"), None
        now = self._clock()
        if self._stop_reason(job, cycle) or self._rates.availability(
                job._project, settings.family, estimate, now) != (0.0, None):
            return None, None
        reservation = self._rates.reserve(job._project, settings.family, estimate, now)
        attempt_id = uuid4().hex
        attempt = {
            "attempt_id": attempt_id, "job_id": job.job_id, "snapshot_id": job.snapshot.snapshot_id,
            "model": settings.model, "batch_id": batch.batch_id, "batch": batch_record(batch, settings.model),
            "cycle": job._cycle,
            "number": count, "recorded_at_utc": _timestamp(), "status": "in_flight",
            "reserved_tokens": estimate, "effective_prompt_sha256": sha256(prompt_bytes).hexdigest(),
            "request_timeout_seconds": getattr(job._transport, "timeout", None),
        }
        result = TransportResult(error="not_sent", sent=False)
        usage = get_usage({})
        try:
            job.store.set_batch(batch, "in_flight")
            job.store.record_attempt(attempt_id, attempt)
            result = await asyncio.to_thread(self._post, job, cycle, body)
            usage = get_usage(result.body)
        finally:
            now = self._clock()
            retry_at = self._rates.finish(reservation, result, usage, len(batch.page_map), now,
                                          (2 ** (count - 1)) * (1 + self._jitter()))
        try:
            cost = estimated_cost(usage, settings.pricing, settings.model) if result.sent else None
        except DecimalException:
            cost = None
        error = result.error
        corrections = None
        discarded_sensitive = 0
        if error is None:
            try:
                corrections = parse_corrections(response_text(result.body), pages.first, pages.last)
                validate_correction_pages(batch, corrections["corrections"])
                if isinstance(job._transport, OpenAIReviewTransport):
                    items = corrections["corrections"]
                    safe = [item for item in items if not job._transport.contains_credential(
                        *item.values(), (item["insert_anchor"] or {}).get("text"))]
                    discarded_sensitive = len(items) - len(safe)
                    corrections["corrections"] = safe
            except (RunFailure, ValueError) as exc:
                error = str(exc)
                corrections = None
        if result.systemic and result.error == "request_rejected":
            cycle.error = result.error
        if result.systemic:
            cycle.suspended = result.error
        will_retry = (result.retryable and count < 3 and retry_at - now <= MAX_RATE_WAIT
                      and not self._stop_reason(job, cycle))
        status = ("validated" if corrections is not None else "uncertain" if result.uncertain
                  else "retry_wait" if will_retry else "cancelled" if error == "cancelled" else "failed")
        attempt.update(status=status, error=error, sent=result.sent, http_status=result.status,
                       request_id=result.headers.get("x-request-id"), rate_headers={
                           key: val for key, val in result.headers.items()
                           if key.startswith("x-ratelimit-") or key == "retry-after"
                       }, usage=usage, estimated_cost_usd=str(cost) if cost is not None else None,
                       retry_delay_seconds=max(0, retry_at - now) if will_retry else None,
                       elapsed_seconds=round(now - reservation.started, 3), finished_at_utc=_timestamp())
        if discarded_sensitive:
            attempt.update(diagnostic="proposal_contains_active_credential",
                           discarded_proposals=discarded_sensitive)
        job.store.record_attempt(attempt_id, attempt)
        if result.sent and (progress := job.progress) is not None:
            job.progress = replace(progress, cost_usd=(
                progress.cost_usd if cost is None else (progress.cost_usd or Decimal(0)) + cost),
                unknown_cost_attempts=progress.unknown_cost_attempts + int(cost is None))
        if corrections is not None:
            job.store.commit(batch, attempt, corrections)
            return BatchOutcome(batch, "completed", correction_count=len(corrections["corrections"])), now
        if will_retry:
            job.store.set_batch(batch, "retry_wait", error)
            return None, retry_at
        return BatchOutcome(batch, status, error), now

    def _post(self, job: ReviewJob, cycle: Cycle, body: bytes) -> TransportResult:
        reason = self._stop_reason(job, cycle)
        if reason:
            return TransportResult(error=reason, sent=False)
        try:
            return job._transport.post(body)
        except Exception:
            # An injected transport can fail after it has sent bytes. Keep the
            # reservation through return and publish a sanitized uncertain result.
            return TransportResult(error="remote_outcome_unknown", uncertain=True)

    async def _try_release(self, job: ReviewJob) -> str | None:
        if job.snapshot is not None and not job._worker.unavailable:
            try:
                await job._worker.release(job.snapshot)
            except ReviewWorkerError as exc:
                if str(exc) not in ("REVIEW_SNAPSHOT_UNKNOWN", "worker_unavailable"):
                    return str(exc)
        job.snapshot = None
        return None

    async def _close(self, job: ReviewJob) -> None:
        await asyncio.gather(asyncio.wrap_future(job.future), return_exceptions=True)
        cleanup_error = await self._try_release(job)
        if cleanup_error:
            raise ReviewWorkerError(cleanup_error)
        with self._lock:
            del self._jobs[job.job_id]
