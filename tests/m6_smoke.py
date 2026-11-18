"""Native Windows M6 workflow, bounded text diagnostics and captured frames."""

import json
import multiprocessing as mp
import os
from pathlib import Path
import statistics
import sys
import tempfile
from time import monotonic, perf_counter

from .m3_benchmark import working_set
from .m6_fixtures import create_in_subprocess


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))] if ordered else 0.0


def native_smoke(directory: Path) -> dict:
    from PySide6.QtCore import QPoint, QPointF, QTimer, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from pdf_annotator.app.document_session import Lifecycle
    from pdf_annotator.app.main_window import MainWindow
    from pdf_annotator.services.text_cache import text_object_cost
    from pdf_annotator.viewer.layout_engine import LayoutMode
    from pdf_annotator.worker.protocol import Operation

    assert sys.platform == "win32"
    app = QApplication([])
    assert app.platformName() == "windows" and "pymupdf" not in sys.modules
    window = MainWindow()
    window.resize(1280, 900)
    window.manager.metrics.enabled = True
    window.show()
    window.raise_()
    window.activateWindow()
    heartbeat: list[float] = []
    last_beat = perf_counter()
    timer = QTimer()

    def beat() -> None:
        nonlocal last_beat
        now = perf_counter()
        heartbeat.append((now - last_beat) * 1000)
        last_beat = now

    timer.timeout.connect(beat)

    def wait(predicate, timeout=30.0) -> None:
        deadline = monotonic() + timeout
        while not predicate() and monotonic() < deadline:
            app.processEvents()
            QTest.qWait(2)
        assert predicate(), "M6 native smoke timed out"

    def open_pdf(path: Path):
        started = perf_counter()
        window.open_paths([str(path)])
        wait(lambda: any(session.canonical_path == str(path.resolve())
                         and session.lifecycle == Lifecycle.READY
                         for session in window.manager.sessions.values()))
        session = next(session for session in window.manager.sessions.values()
                       if session.canonical_path == str(path.resolve()))
        view = window._widgets[session.document_id]
        view.scheduler.metrics.enabled = True
        wait(lambda: view.scheduler.settled)
        first_raster = (perf_counter() - started) * 1000
        wait(lambda: bool(view.text_layer.loaded_chunks(view.scheduler.visible[0])))
        return session, view, first_raster

    def char_point(view, page_index: int, char_index: int) -> QPoint:
        chunks = view.text_layer.loaded_chunks(page_index)
        characters = tuple(char for chunk in chunks for char in chunk.characters)
        quad = characters[char_index].quad
        x = sum(point.x for point in quad.points) / 4
        y = sum(point.y for point in quad.points) / 4
        x, y = view.coordinate_mapper(page_index).canonical_to_viewport((x, y))
        return QPoint(round(x), round(y))

    def select(view, page_index: int, first=0, last=8) -> None:
        start, end = char_point(view, page_index, first), char_point(view, page_index, last)
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(view.viewport(), end, delay=5)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        wait(lambda: view.text_layer.confirmed is not None)

    long_path, second_path = directory / "long-editorial.pdf", directory / "second.pdf"
    create_in_subprocess(long_path, pages=300, dense=True)
    create_in_subprocess(second_path)
    last_beat = perf_counter()
    timer.start(5)
    long_session, long_view, first_raster_ms = open_pdf(long_path)
    frames = []
    text_pages: set[int] = set()
    chunk_costs: dict[int, int] = {}
    # Cross the 16 MiB GUI text-cache budget deterministically with distinct
    # dense pages before returning to page zero.
    for page_index in (0, 20, 40, 60, 80, 100, 120, 150, 180,
                       200, 220, 250, 299, 0):
        page = long_view.page_layout.pages[page_index]
        long_view.verticalScrollBar().setValue(round(page.y))
        wait(lambda: long_view.scheduler.settled)
        visible = long_view.scheduler.visible
        wait(lambda: all(long_view.text_layer.page_text(index, record=False) is not None
                         and bool(long_view.text_layer.loaded_chunks(index))
                         for index in visible))
        text_pages.update(visible)
        chunk_costs.update({index: sum(text_object_cost(chunk) for chunk in
                                       long_view.text_layer.loaded_chunks(index))
                            for index in visible})
    select(long_view, 0)
    selected_text = long_view.text_layer.selected_text()
    long_view.setFocus()
    QTest.keyClick(long_view, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert app.clipboard().text() == selected_text
    for zoom in (1.0, 1.5, 4.0, 8.0):
        selected_quad = long_view.text_layer.confirmed.quads[0]
        anchor = long_view.coordinate_mapper(0).canonical_to_viewport(
            (selected_quad.upper_left.x, selected_quad.upper_left.y))
        long_view.set_zoom(zoom, QPointF(*anchor))
        wait(lambda: long_view.scheduler.settled)
        image_path = directory / f"m6-selection-{round(zoom * 100):03d}.png"
        assert window.grab().save(str(image_path))
        frames.append(str(image_path))
    long_view.set_zoom(1.0)
    long_view.set_layout(LayoutMode.DOUBLE)
    wait(lambda: long_view.scheduler.settled)
    frames.append(str(directory / "m6-double.png"))
    assert window.grab().save(frames[-1])
    renders_before = long_session.worker.submitted_counts[Operation.RENDER]
    window.highlight_action.trigger()
    wait(lambda: long_session.document_revision == 1)
    assert long_session.worker.submitted_counts[Operation.RENDER] == renders_before
    frames.append(str(directory / "m6-highlight.png"))
    assert window.grab().save(frames[-1])
    long_worker_text = {
        "cache_bytes": long_view.text_layer.worker_cache_bytes,
        "extractions": long_view.text_layer.worker_extractions,
        "cache_hits": long_view.text_layer.worker_cache_hits,
    }

    second_session, second_view, second_first_raster = open_pdf(second_path)
    wait(lambda: window.manager.active_id == second_session.document_id)
    select(second_view, 0)
    assert second_view.text_layer.confirmed is not None
    assert long_session.document_revision == 1 and second_session.document_revision == 0
    assert not set(long_session.annotations) & set(second_session.annotations)
    worker_ram = {session.document_id: working_set(session.worker.pid)
                  for session in (long_session, second_session)}
    timer.stop()
    cache = window.manager.text_cache
    report = {
        "platform": app.platformName(),
        "first_raster_ms": first_raster_ms,
        "second_first_raster_ms": second_first_raster,
        "event_loop_p95_ms": percentile(heartbeat, .95),
        "event_loop_max_ms": max(heartbeat, default=0),
        "gui_working_set": working_set(os.getpid()),
        "worker_working_sets": worker_ram,
        "text_cache": {"budget": cache.max_bytes, "bytes": cache.bytes,
                       "peak": cache.peak_bytes, "entries": cache.entry_count,
                       "hits": cache.hits, "misses": cache.misses,
                       "evictions": cache.evictions},
        "worker_text_before_inactive": long_worker_text,
        "worker_text_after_inactive_bytes": long_view.text_layer.worker_cache_bytes,
        "chunk_costs": {"simple": chunk_costs[0],
                           "dense_min": min(value for page, value in chunk_costs.items()
                                            if page >= 3),
                           "dense_max": max(value for page, value in chunk_costs.items()
                                            if page >= 3)},
        "text_pages_seen": sorted(text_pages),
        "long_text_requests": (
            long_session.worker.submitted_counts[Operation.TEXT_PAGE_INDEX]
            + long_session.worker.submitted_counts[Operation.TEXT_CHUNK]),
        "long_render_requests": long_session.worker.submitted_counts[Operation.RENDER],
        "raster_cache": {"bytes": window.manager.resident_budget.bytes,
                         "peak": window.manager.resident_budget.peak_bytes,
                         "hits": window.manager.resident_budget.hits,
                         "misses": window.manager.resident_budget.misses,
                         "evictions": window.manager.resident_budget.evictions},
        "render_metrics": {
            "viewport_p50_ms": statistics.median(
                long_view.scheduler.metrics.samples.get("viewport_to_ready_ms", ())),
            "viewport_p95_ms": percentile(list(
                long_view.scheduler.metrics.samples.get("viewport_to_ready_ms", ())), .95),
            "request_p95_ms": percentile(list(
                long_view.scheduler.metrics.samples.get("request_to_visible_ms", ())), .95),
        },
        "frames": frames,
    }
    assert report["long_text_requests"] < 80
    assert cache.bytes <= cache.max_bytes and cache.peak_bytes <= cache.max_bytes
    assert cache.evictions > 0
    workers = [long_session.worker, second_session.worker]
    assert window.manager.request_shutdown(discard_changes=True)
    wait(lambda: window.manager.is_shutdown)
    window.close()
    app.processEvents()
    assert all(worker.stopped.is_set() for worker in workers)
    assert not mp.active_children() and "pymupdf" not in sys.modules
    report["children_after_close"] = 0
    return report


def main() -> None:
    root = Path(".m0-artifacts")
    root.mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="m6-native-", dir=root)).resolve()
    report = native_smoke(directory)
    destination = directory / "m6-report.json"
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("M6_NATIVE_PASS " + str(directory))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
