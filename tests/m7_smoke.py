"""Native Windows M7 workflow, gesture timing, overlay and memory diagnostics."""

import json
import math
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
    from PySide6.QtCore import QPoint, QTimer, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from pdf_annotator.app.document_session import Lifecycle
    from pdf_annotator.app.main_window import MainWindow
    from pdf_annotator.viewer.layout_engine import LayoutMode
    from pdf_annotator.worker.protocol import Operation

    assert sys.platform == "win32"
    app = QApplication([])
    assert app.platformName() == "windows" and "pymupdf" not in sys.modules
    first_path, second_path = directory / "m7-dense.pdf", directory / "m7-second.pdf"
    create_in_subprocess(first_path, pages=8, dense=True)
    create_in_subprocess(second_path, pages=5)
    source_bytes = {first_path: first_path.read_bytes(), second_path: second_path.read_bytes()}
    window = MainWindow()
    window.resize(1280, 900)
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
        assert predicate(), "M7 native smoke timed out"

    def open_pdf(path: Path):
        window.open_paths([str(path)])
        wait(lambda: any(session.canonical_path == str(path.resolve())
                         and session.lifecycle == Lifecycle.READY
                         for session in window.manager.sessions.values()))
        session = next(session for session in window.manager.sessions.values()
                       if session.canonical_path == str(path.resolve()))
        view = window._widgets[session.document_id]
        wait(lambda: view.scheduler.settled)
        return session, view

    def point(view, page_index: int, canonical) -> QPoint:
        mapped = view.coordinate_mapper(page_index).canonical_to_viewport(canonical)
        return QPoint(round(mapped[0]), round(mapped[1]))

    def draw(view, page_index: int, points, revision: int, *, delay=0) -> None:
        mapped = [point(view, page_index, value) for value in points]
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=mapped[0])
        for value in mapped[1:]:
            QTest.mouseMove(view.viewport(), value, delay=delay)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=mapped[-1])
        wait(lambda: view.session.document_revision == revision)

    def move_ink(view, page_index: int, start, end, revision: int) -> None:
        before = view.session.worker.submitted_counts[Operation.ANNOTATION_COMMAND]
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=point(view, page_index, start))
        for step in range(1, 25):
            ratio = step / 24
            current = (start[0] + (end[0] - start[0]) * ratio,
                       start[1] + (end[1] - start[1]) * ratio)
            QTest.mouseMove(view.viewport(), point(view, page_index, current), delay=1)
        assert view.session.worker.submitted_counts[Operation.ANNOTATION_COMMAND] == before
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=point(view, page_index, end))
        wait(lambda: view.session.document_revision == revision)

    def char_point(view, page_index: int, char_index: int) -> QPoint:
        wait(lambda: bool(view.text_layer.loaded_chunks(page_index)))
        chars = tuple(char for chunk in view.text_layer.loaded_chunks(page_index)
                      for char in chunk.characters)
        quad = chars[char_index].quad
        center = (sum(value.x for value in quad.points) / 4,
                  sum(value.y for value in quad.points) / 4)
        return point(view, page_index, center)

    def select(view, page_index=0, first=0, last=8) -> None:
        begin, end = char_point(view, page_index, first), char_point(view, page_index, last)
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=begin)
        QTest.mouseMove(view.viewport(), end, delay=3)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        wait(lambda: view.text_layer.confirmed is not None)

    last_beat = perf_counter()
    timer.start(5)
    first, view = open_pdf(first_path)
    view.scheduler.metrics.enabled = True
    select(view)
    window.highlight_action.trigger()
    wait(lambda: first.document_revision == 1)
    renders_before_pen = first.worker.submitted_counts[Operation.RENDER]
    view.set_pen_enabled(True)
    view.set_pen_color("red")
    raw_final: list[tuple[int, int]] = []
    frames = []

    slow = [(30 + index * 3, 55 + math.sin(index / 4) * 22) for index in range(40)]
    draw(view, 0, slow, 2, delay=2)
    raw_final.append((view.last_pen_metrics.raw_events, view.last_pen_metrics.final_points))
    fast = [(45, 115), (210, 145)]
    draw(view, 0, fast, 3)
    raw_final.append((view.last_pen_metrics.raw_events, view.last_pen_metrics.final_points))
    curve = [(165 + math.cos(index / 40 * math.tau) * 48,
              230 + math.sin(index / 40 * math.tau) * 48) for index in range(41)]
    draw(view, 0, curve, 4)
    raw_final.append((view.last_pen_metrics.raw_events, view.last_pen_metrics.final_points))

    first_target = next(model.target for model in first.annotations.values()
                        if model.kind.value == "ink")
    for zoom in (1.0, 1.5, 4.0, 8.0):
        view.set_zoom(zoom)
        view.horizontalScrollBar().setValue(0)
        view.verticalScrollBar().setValue(0)
        wait(lambda: view.scheduler.settled)
        draw(view, 0, [(25, 25 + zoom), (45, 48 + zoom), (70, 30 + zoom)],
             first.document_revision + 1)
        frame = directory / f"m7-pen-{round(zoom * 100):03d}.png"
        assert window.grab().save(str(frame))
        frames.append(str(frame))
    view.set_zoom(1.0)
    assert first_target == next(model.target for model in first.annotations.values()
                                if model.kind.value == "ink")
    view.set_pen_enabled(False)
    move_ink(view, 0, (30, 55), (50, 70), first.document_revision + 1)
    view.set_pen_enabled(True)

    view.set_zoom(.6)
    view.set_layout(LayoutMode.DOUBLE)
    wait(lambda: view.scheduler.settled)
    draw(view, 0, [(35, 300), (80, 330)], first.document_revision + 1)
    draw(view, 1, [(35, 300), (80, 330)], first.document_revision + 1)
    frame = directory / "m7-double.png"
    assert window.grab().save(str(frame))
    frames.append(str(frame))

    # M6 remains intact after explicit Pen OFF, including a dense >8k page.
    view.set_pen_enabled(False)
    view.set_layout(LayoutMode.SINGLE)
    view.set_zoom(1.0)
    page = view.page_layout.pages[3]
    view.verticalScrollBar().setValue(round(page.y))
    wait(lambda: view.scheduler.settled and bool(view.text_layer.loaded_chunks(3)))
    select(view, 3, 0, 20)
    selected = view.text_layer.selected_text()
    view.setFocus()
    QTest.keyClick(view, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert app.clipboard().text() == selected

    # Overlay/payload stress: 100 short Inks and three near-limit paths.
    view.verticalScrollBar().setValue(0)
    view.set_pen_enabled(True)
    for index in range(100):
        x = 20 + index % 20 * 7
        y = 360 + index // 20 * 7
        draw(view, 0, [(x, y), (x + 4, y + 3)], first.document_revision + 1)
    for row in range(3):
        long_path = [(20 + index * .18, 430 + row * 28 + math.sin(index / 14) * 9)
                     for index in range(900)]
        draw(view, 0, long_path, first.document_revision + 1)
        raw_final.append((view.last_pen_metrics.raw_events,
                          view.last_pen_metrics.final_points))

    second, second_view = open_pdf(second_path)
    assert not second_view.pen_enabled
    second_view.set_pen_enabled(True)
    second_view.set_pen_color("blue")
    draw(second_view, 0, [(30, 40), (75, 80), (105, 55)], 1)
    assert first.document_revision > 1 and second.document_revision == 1
    assert not set(first.annotations) & set(second.annotations)
    worker_ram = {session.document_id: working_set(session.worker.pid)
                  for session in (first, second)}
    timer.stop()
    report = {
        "platform": app.platformName(),
        "event_loop_p95_ms": percentile(heartbeat, .95),
        "event_loop_max_ms": max(heartbeat, default=0),
        "gui_working_set": working_set(os.getpid()),
        "worker_working_sets": worker_ram,
        "ink_counts": {first.document_id: sum(model.kind.value == "ink"
                                                for model in first.annotations.values()),
                        second.document_id: sum(model.kind.value == "ink"
                                                 for model in second.annotations.values())},
        "raw_final_points": raw_final,
        "mouse_move_p50_ms": statistics.median(view.pen_move_ms),
        "mouse_move_p95_ms": percentile(list(view.pen_move_ms), .95),
        "release_enqueue_p50_ms": statistics.median(view.pen_release_enqueue_ms),
        "release_enqueue_p95_ms": percentile(list(view.pen_release_enqueue_ms), .95),
        "enqueue_confirm_p50_ms": statistics.median(view.pen_roundtrip_ms),
        "enqueue_confirm_p95_ms": percentile(list(view.pen_roundtrip_ms), .95),
        "ink_move_mouse_p95_ms": percentile(list(view.ink_move_ms), .95),
        "ink_move_release_enqueue_p95_ms": percentile(
            list(view.ink_move_release_enqueue_ms), .95),
        "ink_move_enqueue_confirm_p95_ms": percentile(list(view.ink_move_roundtrip_ms), .95),
        "renders_before_pen": renders_before_pen,
        "renders_after_pen": first.worker.submitted_counts[Operation.RENDER],
        "raster_cache": {"bytes": window.manager.resident_budget.bytes,
                         "peak": window.manager.resident_budget.peak_bytes},
        "text_cache": {"bytes": window.manager.text_cache.bytes,
                       "peak": window.manager.text_cache.peak_bytes},
        "frames": frames,
    }
    assert max(final for _raw, final in raw_final) <= 1024
    assert report["mouse_move_p95_ms"] < 5
    assert report["ink_move_mouse_p95_ms"] < 5
    workers = [first.worker, second.worker]
    for session in (first, second):
        assert window.manager.request_close(session.document_id, discard_changes=True)
    wait(lambda: not window.manager.sessions)
    assert all(path.read_bytes() == source_bytes[path] for path in source_bytes)
    window.close()
    wait(lambda: window.manager.is_shutdown)
    assert all(worker.stopped.is_set() for worker in workers)
    assert not mp.active_children() and "pymupdf" not in sys.modules
    report["children_after_close"] = 0
    return report


def main() -> None:
    root = Path(".m0-artifacts")
    root.mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="m7-native-", dir=root)).resolve()
    report = native_smoke(directory)
    (directory / "m7-report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")
    print("M7_NATIVE_PASS " + str(directory))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
