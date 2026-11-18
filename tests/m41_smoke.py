"""Native Windows M4.1 regional-render smoke and bounded diagnostics.

Fixture creation and direct PyMuPDF comparisons run in helper processes. The
GUI process talks to PyMuPDF only through each document's existing worker.
"""

import json
import multiprocessing as mp
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
from time import perf_counter
from collections import Counter

from .m3_benchmark import working_set

ZOOMS = (1.0, 1.5, 3.0, 4.0, 6.0, 8.0)


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))] if ordered else 0.0


def native_smoke(directory: Path) -> dict:
    from PySide6 import __version__ as qt_version
    from PySide6.QtCore import QPoint, QPointF, QTimer, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication

    from pdf_annotator.app.document_session import Lifecycle
    from pdf_annotator.app.main_window import MainWindow
    from pdf_annotator.viewer.metrics import ViewerMetrics
    from pdf_annotator.viewer.render_scheduler import requested_raster_scale
    from pdf_annotator.worker.client import WorkerClient
    from pdf_annotator.worker.protocol import Operation, VIEWER_SINGLE_RASTER_BYTES

    app = QApplication([])
    assert sys.platform == "win32" and app.platformName() == "windows"
    assert "pymupdf" not in sys.modules
    window = MainWindow()
    window.resize(1280, 900)
    window.manager.metrics.enabled = True
    window.show()
    window.raise_()
    window.activateWindow()
    diagnostics = ViewerMetrics()
    diagnostics.enabled = True
    frames: list[tuple[str, object]] = []
    samples: list[dict] = []
    memory: list[dict] = []
    views = []
    workers = []
    render_requests: list[tuple] = []
    original_submit = WorkerClient.submit

    def observe_submit(client, operation, payload=None):
        request_id = original_submit(client, operation, payload)
        if operation == Operation.RENDER and request_id is not None:
            data = payload or {}
            render_requests.append((client.document_id, data.get("page_index"),
                                    data.get("render_scale"), tuple(data.get("tile") or ())))
        return request_id

    WorkerClient.submit = observe_submit
    state = {"failed": None, "phase": "open", "last": perf_counter(),
             "ticks": 0, "active_ticks": 0}

    def ready(name: str):
        return next((view for view in window._widgets.values()
                     if Path(view.session.canonical_path).stem == name
                     and view.session.lifecycle == Lifecycle.READY), None)

    def attach(view) -> None:
        view.scheduler.metrics.enabled = True
        views.append(view)
        workers.append(view.session.worker)

    def top_left(view, page_index: int) -> None:
        page = view.page_layout.pages[page_index]
        view.horizontalScrollBar().setValue(round(page.x))
        view.verticalScrollBar().setValue(round(page.y))

    def wheel(view, delta: int, *, control: bool = False) -> None:
        event = QWheelEvent(
            QPointF(420, 320), QPointF(420, 320), QPoint(), QPoint(0, delta),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.ControlModifier if control else Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False,
        )
        QApplication.sendEvent(view.viewport(), event)

    def metric_values(view, name: str, since: int) -> list[float]:
        values = view.scheduler.metrics.samples.get(name, ())
        return list(values)[since:]

    def sample_memory(label: str) -> None:
        cache = window.manager.resident_budget
        memory.append({
            "phase": label,
            "resident": cache.bytes,
            "resident_count": cache.count,
            "transit": window.manager.budget.bytes,
            "gui_working_set": working_set(os.getpid()),
            "worker_working_sets": [working_set(worker.pid) for worker in workers
                                     if not worker.stopped.is_set()],
            "frame_bytes": sum(frame.sizeInBytes() for _, frame in frames),
        })

    def capture(name: str) -> None:
        assert window.windowHandle().isExposed()
        frames.append((name, window.grab().toImage()))

    def delay(seconds: float):
        deadline = perf_counter() + seconds
        return lambda: perf_counter() >= deadline

    def exercise_zoom(view, zoom: float):
        state["phase"] = f"zoom-{zoom:g}"
        request_start = view.scheduler.requested_count
        region_start = view.scheduler.regional_requested_count
        ready_start = len(view.scheduler.metrics.samples.get("viewport_to_ready_ms", ()))
        request_metric_start = len(view.scheduler.metrics.samples.get("request_to_visible_ms", ()))
        hit_start = window.manager.resident_budget.hits
        miss_start = window.manager.resident_budget.misses
        eviction_start = window.manager.resident_budget.evictions
        view.set_zoom(zoom)
        for page_index in range(5):
            top_left(view, page_index)
            yield lambda: view.scheduler.settled
        visible_keys = tuple(key for key in view.scheduler._visible_keys)
        expected_scale = requested_raster_scale(zoom, view.scheduler.dpr)
        regional = any(key.region is not None for key in visible_keys)
        if regional:
            assert all(key.region is not None and key.scale == expected_scale
                       for key in visible_keys)
        else:
            assert all(key.region is None for key in visible_keys)
        entries = [entry for key in visible_keys
                   if (entry := window.manager.resident_budget.get(key, record=False)) is not None]
        assert entries and all(entry.cost <= VIEWER_SINGLE_RASTER_BYTES for entry in entries)
        viewport_values = metric_values(view, "viewport_to_ready_ms", ready_start)
        request_values = metric_values(view, "request_to_visible_ms", request_metric_start)
        samples.append({
            "zoom": zoom,
            "strategy": "regional" if regional else "full-page",
            "requested_scale": expected_scale,
            "renders": view.scheduler.requested_count - request_start,
            "regional_renders": view.scheduler.regional_requested_count - region_start,
            "visible_regions": len(visible_keys),
            "request_to_raster_p50_ms": statistics.median(request_values) if request_values else 0.0,
            "request_to_raster_p95_ms": percentile(request_values, .95),
            "viewport_to_raster_p50_ms": statistics.median(viewport_values) if viewport_values else 0.0,
            "viewport_to_raster_p95_ms": percentile(viewport_values, .95),
            "cache_hits": window.manager.resident_budget.hits - hit_start,
            "cache_misses": window.manager.resident_budget.misses - miss_start,
            "evictions": window.manager.resident_budget.evictions - eviction_start,
            "resident": window.manager.resident_budget.bytes,
            "gui_working_set": working_set(os.getpid()),
            "worker_working_set": working_set(view.session.worker.pid),
        })
        if zoom >= 3:
            capture(f"viewer-regional-{round(zoom * 100):03d}")

    def workflow():
        window.open_paths([str(directory / "quality.pdf")])
        yield lambda: ready("quality") is not None
        quality = ready("quality")
        attach(quality)
        yield lambda: quality.scheduler.settled
        for zoom in ZOOMS:
            yield from exercise_zoom(quality, zoom)

        # Slow reading scroll and return to a still-resident region.
        state["phase"] = "slow-scroll"
        quality.set_zoom(8)
        top_left(quality, 0)
        yield lambda: quality.scheduler.settled
        initial_keys = set(quality.scheduler.visible_region_keys(0))
        for position in range(0, 1801, 180):
            quality.verticalScrollBar().setValue(position)
            yield lambda: quality.scheduler.settled
        before_return = quality.scheduler.requested_count
        top_left(quality, 0)
        yield lambda: quality.scheduler.settled
        assert set(quality.scheduler.visible_region_keys(0)) == initial_keys
        assert quality.scheduler.requested_count == before_return

        # A rapid traversal must converge on the latest viewport, not its path.
        state["phase"] = "rapid-scroll"
        before_rapid = quality.scheduler.requested_count
        for position in range(0, min(quality.verticalScrollBar().maximum(), 10000), 400):
            quality.verticalScrollBar().setValue(position)
        final_target_count = len(quality.scheduler.target_keys)
        yield lambda: quality.scheduler.settled
        assert quality.scheduler.requested_count - before_rapid <= final_target_count + 2

        # Exercise the production Ctrl+wheel path and every M4 layout.
        state["phase"] = "wheel-layout"
        quality.set_zoom(4)
        yield lambda: quality.scheduler.settled
        wheel(quality, 120, control=True)
        yield lambda: quality.scheduler.settled
        wheel(quality, -120, control=True)
        yield lambda: quality.scheduler.settled
        for mode in ("double", "double_cover", "single"):
            quality.set_layout(mode)
            yield lambda: quality.scheduler.settled

        # Long-document jumps twice: resident bytes must remain bounded and
        # the second traversal must not increase the controlled peak.
        window.open_paths([str(directory / "long.pdf")])
        yield lambda: ready("long") is not None
        long_view = ready("long")
        attach(long_view)
        long_view.set_layout("double")
        long_view.set_zoom(4)
        yield lambda: long_view.scheduler.settled
        assert not quality.scheduler.rasters
        traversal_peaks = []
        for traversal in range(2):
            state["phase"] = f"long-traversal-{traversal + 1}"
            peak = 0
            for page_index in range(0, 300, 25):
                top_left(long_view, page_index)
                yield lambda: long_view.scheduler.settled
                peak = max(peak, window.manager.resident_budget.bytes)
            traversal_peaks.append(peak)
            sample_memory(state["phase"])
        assert max(traversal_peaks) <= window.manager.resident_budget.max_bytes
        assert traversal_peaks[1] <= window.manager.resident_budget.peak_bytes

        # Two workers remain independent; inactive rasters and prefetch are gone.
        state["phase"] = "tab-switch"
        long_requests = long_view.scheduler.requested_count
        window.manager.activate(quality.session.document_id)
        yield lambda: quality.scheduler.settled
        assert not any(key.document_id == long_view.session.document_id
                       for key in window.manager.resident_budget.keys)
        yield delay(.2)
        assert long_view.scheduler.requested_count == long_requests
        window.manager.activate(long_view.session.document_id)
        yield lambda: long_view.scheduler.settled
        assert window.manager.workers_started == 2
        sample_memory("tab-return")

        state["phase"] = "cleanup"
        quality_worker, long_worker = quality.session.worker, long_view.session.worker
        window.manager.request_close(quality.session.document_id)
        yield lambda: quality_worker.stopped.is_set()
        window.manager.request_close(long_view.session.document_id)
        yield lambda: long_worker.stopped.is_set()
        assert window.manager.resident_budget.bytes == window.manager.budget.bytes == 0

    iterator, condition = workflow(), [lambda: True]
    timer = QTimer()
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    started = perf_counter()

    def tick() -> None:
        now = perf_counter()
        state["ticks"] += 1
        state["active_ticks"] += int(window.isActiveWindow())
        diagnostics.add("event_loop_ms", (now - state["last"]) * 1000)
        state["last"] = now
        try:
            if now - started > 180:
                raise TimeoutError(state["phase"])
            if condition[0]():
                condition[0] = next(iterator)
        except StopIteration:
            timer.stop()
            window.close()
        except Exception as exc:
            state["failed"] = repr(exc)
            timer.stop()
            window.close()

    timer.timeout.connect(tick)
    timer.start(10)
    app.exec()
    WorkerClient.submit = original_submit
    assert state["failed"] is None, state
    assert window.manager.is_shutdown and not mp.active_children()
    for name, frame in frames:
        assert frame.save(str(directory / f"{name}.png"))
    cache = window.manager.resident_budget
    request_counts = Counter(render_requests)
    return {
        "platform": app.platformName(), "qt": qt_version,
        "dpr": views[0].scheduler.dpr, "samples": samples,
        "event_loop": diagnostics.report().get("event_loop_ms", {}),
        "resident_limit": cache.max_bytes, "resident_peak": cache.peak_bytes,
        "transit_limit": window.manager.budget.max_bytes,
        "transit_peak": window.manager.budget.peak_bytes,
        "single_raster": window.manager.budget.single_bytes,
        "cache_hits": cache.hits, "cache_misses": cache.misses,
        "evictions": cache.evictions,
        "views": [{"name": Path(view.session.canonical_path).stem,
                   "renders": view.scheduler.requested_count,
                   "regional_renders": view.scheduler.regional_requested_count,
                   "rerenders": sum(count - 1 for key, count in request_counts.items()
                                    if key[0] == view.session.document_id),
                   "discarded": view.scheduler.discarded_count + worker.stale_discarded,
                   "metrics": view.scheduler.metrics.report()}
                  for view, worker in zip(views, workers)],
        "memory": memory, "children_after_close": len(mp.active_children()),
        "ticks": state["ticks"], "active_ticks": state["active_ticks"],
    }


def main() -> None:
    root = Path(".m0-artifacts")
    root.mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="m41-native-", dir=root)).resolve()
    subprocess.run([sys.executable, "-m", "tests.render_quality_benchmark",
                    "--fixtures", str(directory)], check=True, timeout=60)
    subprocess.run([sys.executable, "-m", "tests.m3_benchmark",
                    "--fixtures", str(directory)], check=True, timeout=120)
    subprocess.run([sys.executable, "-m", "tests.render_quality_benchmark",
                    "--render", str(directory)], check=True, timeout=120)
    report = native_smoke(directory)
    (directory / "m41-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("M41_NATIVE_PASS " + str(directory))
    print(json.dumps({key: value for key, value in report.items()
                      if key not in ("memory", "views")}, indent=2))


if __name__ == "__main__":
    mp.freeze_support()
    main()
