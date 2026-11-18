"""Measure the production worker with deterministic PDFs. No network or GUI."""

import argparse
from ctypes import Structure, WinDLL, byref, c_size_t, sizeof, wintypes
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter, sleep

from pdf_annotator.util.paths import identify_file
from pdf_annotator.worker.client import RasterBudget, WorkerClient
from pdf_annotator.worker.document_service import DocumentSource
from pdf_annotator.worker.protocol import Operation


class ProcessMemoryCounters(Structure):
    _fields_ = [("cb", wintypes.DWORD), ("page_faults", wintypes.DWORD)] + [
        (field, c_size_t) for field in ("peak", "working_set", "peak_paged_pool", "paged_pool",
                                     "peak_nonpaged_pool", "nonpaged_pool", "pagefile", "peak_pagefile")]


class WorkerMemory:
    def __init__(self, pid: int) -> None:
        kernel = WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.close_handle = kernel.CloseHandle
        self.handle = kernel.OpenProcess(0x0410, False, pid)
        if not self.handle:
            raise OSError("Cannot sample PDF worker memory")
        self.query = WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
        self.query.argtypes = wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD
        self.query.restype = wintypes.BOOL

    def read(self) -> tuple[int, int]:
        counters = ProcessMemoryCounters()
        counters.cb = sizeof(counters)
        if not self.query(self.handle, byref(counters), counters.cb):
            raise OSError("Cannot read PDF worker memory")
        return counters.working_set, counters.peak

    def close(self) -> None:
        self.close_handle(self.handle)


def wait(client, request_id, memory=None) -> tuple[dict, float, int | None]:
    deadline = perf_counter() + 120
    peak = None
    while perf_counter() < deadline:
        if memory is not None:
            sample, _lifetime = memory.read()
            peak = sample if peak is None else max(sample, peak)
        delivery = client.poll(request_id)
        if delivery is not None:
            with delivery:
                if not delivery.response.success:
                    raise RuntimeError(delivery.response.error)
                return delivery.response.payload, perf_counter(), peak
        if client.failure is not None:
            raise RuntimeError(client.failure)
        sleep(.002)
    raise TimeoutError("Benchmark exceeded 120 s")


def submit(client, operation, payload=None) -> tuple[int, float]:
    started = perf_counter()
    request = client.submit(operation, payload)
    if request is None:
        raise RuntimeError("Benchmark request backpressured")
    return request, started


def standalone(client, operation, payload) -> float:
    request, started = submit(client, operation, payload)
    _, finished, _ = wait(client, request)
    return (finished - started) * 1000


def operation_then_render(client, memory, operation, payload=None) -> tuple[dict, dict]:
    before, _ = memory.read()
    request, started = submit(client, operation, payload)
    render, render_started = submit(client, Operation.RENDER, {"page_index": 0, "render_scale": .7})
    text, text_started = submit(client, Operation.TEXT_PAGE_INDEX, {"page_index": 0})
    result, finished, sampled = wait(client, request, memory)
    _, render_finished, _ = wait(client, render)
    _, text_finished, _ = wait(client, text)
    return result, {
        **result["timing_ms"], "round_trip_ms": (finished - started) * 1000,
        "queued_render_ms": (render_finished - render_started) * 1000,
        "queued_text_after_render_ms": (text_finished - text_started) * 1000,
        "working_set_before_mib": before / 2**20,
        "sampled_peak_working_set_mib": sampled / 2**20,
    }


def p95(samples: list[float]) -> float:
    return sorted(samples)[math.ceil(len(samples) * .95) - 1]


def benchmark(path: Path, label: str, repeats: int) -> dict:
    client = WorkerClient(f"m21-benchmark-{label}", DocumentSource(identify_file(str(path))), RasterBudget())
    memory = None
    try:
        request, _ = submit(client, Operation.OPEN_DOCUMENT)
        info, _, _ = wait(client, request)
        memory = WorkerMemory(client.pid)
        render_args = {"page_index": 0, "render_scale": .7}
        text_args = {"page_index": 0}
        standalone(client, Operation.RENDER, render_args)
        standalone(client, Operation.TEXT_PAGE_INDEX, text_args)
        baseline, text_baseline, snapshots, extracts = [], [], [], []
        for _ in range(repeats):
            baseline.append(standalone(client, Operation.RENDER, render_args))
            text_baseline.append(standalone(client, Operation.TEXT_PAGE_INDEX, text_args))
            captured, ms = operation_then_render(client, memory, Operation.REVIEW_SNAPSHOT)
            snapshot = captured["snapshot"]
            snapshots.append({"size_mib": snapshot["size_bytes"] / 2**20, **ms})
            last = min(info["page_count"] - 1, 3)
            result, ms = operation_then_render(client, memory, Operation.REVIEW_EXTRACT, {
                "snapshot_id": snapshot["snapshot_id"], "first_page": 0, "last_page": last,
            })
            extracts.append({"batches": result["plan"]["batch_count"],
                             "unprocessable_pages": result["plan"]["unprocessable_pages"], **ms})
            release, _ = submit(client, Operation.REVIEW_RELEASE, {"snapshot_id": snapshot["snapshot_id"]})
            wait(client, release)
        _, lifetime_peak = memory.read()
        return {"fixture": label, "pages": info["page_count"], "source_mib": path.stat().st_size / 2**20,
                "baseline_render_p95_ms": p95(baseline),
                "baseline_text_p95_ms": p95(text_baseline),
                "render_after_snapshot_p95_ms": p95([r["queued_render_ms"] for r in snapshots]),
                "render_after_extract_p95_ms": p95([r["queued_render_ms"] for r in extracts]),
                "text_after_snapshot_and_render_p95_ms": p95([r["queued_text_after_render_ms"] for r in snapshots]),
                "text_after_extract_and_render_p95_ms": p95([r["queued_text_after_render_ms"] for r in extracts]),
                "worker_lifetime_peak_working_set_mib": lifetime_peak / 2**20,
                "snapshots": snapshots, "extracts": extracts}
    finally:
        if memory is not None:
            memory.close()
        client.close_async()
        if not client.stopped.wait(10):
            raise RuntimeError("Benchmark worker did not close")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--sample", type=Path, help="optional local PDF")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    from tests.m21_review_fixtures import make_pdf

    with TemporaryDirectory(prefix="fupdf-m21-bench-") as tmp:
        light, heavy = Path(tmp) / "light.pdf", Path(tmp) / "images.pdf"
        make_pdf(light, 48)
        make_pdf(heavy, 96, image_side=800)
        for path, label in ((light, "text"), (heavy, "images"), (args.sample, "sample")):
            if path is not None:
                result = benchmark(path, label, args.repeats)
                print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
