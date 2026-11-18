"""Standalone M6 chunk-size and dense-page benchmark; no product entry point."""

import json
from pathlib import Path
import statistics
import tempfile
from time import perf_counter

from .m6_fixtures import create_text_limit_pdf


CHARACTER_CASES = (8_000, 10_000, 16_000, 30_000)
TARGETS = (128 * 1024, 256 * 1024, 512 * 1024)


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def measure(path: Path, target: int, repetitions: int) -> dict:
    import pymupdf

    from pdf_annotator.domain.serialization import point_to_payload
    from pdf_annotator.services.text_cache import text_object_cost
    from pdf_annotator.worker.text_service import WorkerTextService

    extraction_ms: list[float] = []
    result = {}
    for repetition in range(repetitions):
        with pymupdf.open(path) as document:
            service = WorkerTextService(
                "123456781234467892341234567890ab", 1, document,
                chunk_target_bytes=target)
            started = perf_counter()
            index = service.page_index(0)
            extraction_ms.append((perf_counter() - started) * 1000)
            if repetition:
                continue
            chunks = [service.chunk(0, index.page_text_id, item.chunk_index)
                      for item in index.chunks]
            first, second = chunks[0], chunks[-1]
            anchor_char = first.characters[-10]
            focus_char = second.characters[min(10, len(second.characters) - 1)]
            center = lambda char: type(char.quad.upper_left)(
                sum(point.x for point in char.quad.points) / 4,
                sum(point.y for point in char.quad.points) / 4)
            started = perf_counter()
            status, selection = service.resolve({
                "page_index": 0, "page_text_id": index.page_text_id,
                "anchor": point_to_payload(center(anchor_char)),
                "focus": point_to_payload(center(focus_char))})
            selection_ms = (perf_counter() - started) * 1000
            assert status == "confirmed" and selection is not None
            result = {
                "chunks": len(index.chunks),
                "wire_total": sum(item.wire_bytes for item in index.chunks),
                "wire_max_frame": max(item.wire_bytes for item in index.chunks),
                "index_memory": text_object_cost(index),
                "max_chunk_memory": max(text_object_cost(chunk) for chunk in chunks),
                "worker_cache_peak": service.cache.peak_bytes,
                "worker_cache_bytes": service.cache.bytes,
                "gui_two_chunk_memory": (text_object_cost(index)
                                         + sum(text_object_cost(chunk)
                                               for chunk in chunks[:2])),
                "selection_ms": selection_ms,
                "selection_chars": len(selection.original_text),
            }
    result.update({
        "extract_p50_ms": statistics.median(extraction_ms),
        "extract_p95_ms": percentile(extraction_ms, .95),
        "extract_samples": extraction_ms,
    })
    return result


def main() -> None:
    root = Path(".m0-artifacts")
    root.mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="m6-dense-", dir=root)).resolve()
    report = {"directory": str(directory), "targets": {}}
    paths = {}
    for characters in CHARACTER_CASES:
        path = directory / f"dense-{characters}.pdf"
        create_text_limit_pdf(path, lines=characters // 100, characters_per_line=100)
        paths[characters] = path
    for target in TARGETS:
        report["targets"][str(target)] = {
            str(characters): measure(paths[characters], target,
                                     repetitions=5 if target == 256 * 1024 else 1)
            for characters in CHARACTER_CASES}
    destination = directory / "dense-report.json"
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("M6_DENSE_BENCHMARK_PASS " + str(directory))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
