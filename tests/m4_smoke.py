"""Native Windows layout smoke, bounded diagnostics and inspectable frames.

Run: python -m tests.m4_smoke. Fixture creation is a separate, pre-GUI process;
only the existing document workers use PyMuPDF while the viewer is running.
"""

import argparse
import json
import multiprocessing as mp
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from time import perf_counter

from .m3_benchmark import create_fixtures, working_set


def fixtures(directory: Path) -> None:
    import pymupdf

    create_fixtures(directory)
    for name, sizes in (("odd", [(595, 842, 0)] * 5),
                        ("mixed", [(500, 700, 0), (800, 450, 0), (420, 620, 90),
                                   (700, 520, 270), (600, 800, 0), (600, 800, 90),
                                   (600, 800, 180), (600, 800, 270)])):
        with pymupdf.open() as doc:
            for index, (width, height, rotation) in enumerate(sizes):
                page = doc.new_page(width=width, height=height)
                color = ((index % 3 + 1) / 4, .3, .55)
                page.draw_rect((80, 100, width - 80, height - 100), color=color, width=3)
                page.insert_text((100, 150), f"M4 {name} - PAGE {index + 1}", fontsize=18, color=color)
                page.insert_text((100, 190), f"{width} x {height}; rotation {rotation}", fontsize=12)
                if name == "mixed" and index >= 4:
                    page.set_cropbox(pymupdf.Rect(50, 70, 550, 730))
                page.set_rotation(rotation)
            doc.save(directory / f"{name}.pdf", deflate=True)


def native_smoke(directory: Path) -> dict:
    from PySide6 import __version__ as qt_version
    from PySide6.QtCore import QPoint, QPointF, QRectF, QTimer, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication
    from pdf_annotator.app.document_session import Lifecycle
    from pdf_annotator.app.main_window import MainWindow
    from pdf_annotator.viewer.metrics import ViewerMetrics

    app = QApplication([])
    assert sys.platform == "win32" and app.platformName() == "windows"
    window = MainWindow()
    window.resize(1330, 920)
    window.manager.metrics.enabled = True
    window.show()
    window.raise_()
    window.activateWindow()
    metrics = ViewerMetrics()
    metrics.enabled = True
    frames, memory, views, transitions = [], [], [], []
    state = {"failed": None, "phase": "open", "last": perf_counter(), "active_ticks": 0, "ticks": 0}

    def ready(name):
        return next((v for v in window._widgets.values()
                     if Path(v.session.canonical_path).stem == name and v.session.lifecycle == Lifecycle.READY), None)

    def attach(name):
        view = ready(name)
        view.scheduler.metrics.enabled = True
        views.append(view)
        return view

    def capture(name):
        assert window.windowHandle().isExposed() and len(frames) < 10
        with metrics.span("capture_ms"):
            frames.append((name, window.grab().toImage()))

    def sample(page):
        cache = window.manager.resident_budget
        memory.append({"phase": state["phase"], "page": page, "resident": cache.bytes,
                       "raster_count": cache.count, "transit": window.manager.budget.bytes,
                       "gui_working_set": working_set(os.getpid()),
                       "worker_working_sets": [working_set(v.session.worker.pid) for v in views
                                               if v.session.worker is not None and not v.session.worker.stopped.is_set()],
                       "harness_frame_bytes": sum(frame.sizeInBytes() for _, frame in frames)})

    def focus(view, page):
        rect = view.page_layout.pages[page]
        view.horizontalScrollBar().setValue(round(rect.x + rect.width / 2 - view.viewport().width() / 2))
        view.verticalScrollBar().setValue(round(rect.y + rect.height / 2 - view.viewport().height() / 2))

    def layout(view, mode):
        index, canonical, point = view._reading_anchor()
        cache = window.manager.resident_budget
        before = set(cache.keys)
        requests, evictions = view.scheduler.requested_count, cache.evictions
        view.set_layout(mode)
        restored = QPointF(*view.coordinate_mapper(index).canonical_to_viewport(canonical))
        assert index in view.scheduler.visible
        assert QRectF(view.viewport().rect()).adjusted(-1, -1, 1, 1).contains(restored)
        assert before.issubset(cache.keys) and cache.evictions == evictions
        transitions.append({"page": index, "layout": mode, "zoom": view.session.ui.zoom,
                            "kept_keys": len(before), "immediate_new_requests": view.scheduler.requested_count - requests,
                            "vertical_anchor_shift": restored.y() - point.y()})

    def delay(seconds):
        until = perf_counter() + seconds
        return lambda: perf_counter() >= until

    def workflow():
        window.open_paths([str(directory / "medium.pdf")])
        yield lambda: ready("medium") is not None
        medium = attach("medium")
        yield lambda: medium.scheduler.settled
        capture("01-medium-single")
        # Exercise the actual selector, then the same view operation used by it.
        window.layout_actions["double"].trigger()
        yield lambda: medium.scheduler.settled
        capture("02-medium-double")
        for _ in range(20):
            event = QWheelEvent(QPointF(450, 350), QPointF(450, 350), QPoint(), QPoint(0, -120),
                                Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                                Qt.ScrollPhase.NoScrollPhase, False)
            QApplication.sendEvent(medium.viewport(), event)
            yield delay(.02)
        state["phase"] = "layout-zoom"
        for zoom in (.5, 1, 1.5, 3):
            medium.set_zoom(zoom)
            yield lambda: medium.scheduler.settled
            focus(medium, 27)
            for mode in ("single", "double", "double_cover", "single"):
                layout(medium, mode)
                yield lambda: medium.scheduler.settled
        medium.set_zoom(1)
        layout(medium, "double_cover")
        focus(medium, 0)
        yield lambda: medium.scheduler.settled
        capture("03-medium-cover")
        medium.set_zoom(1.25)
        focus(medium, 27)
        yield lambda: medium.scheduler.settled
        medium_position = medium.horizontalScrollBar().value(), medium.verticalScrollBar().value()

        window.open_paths([str(directory / "long.pdf")])
        yield lambda: ready("long") is not None
        long = attach("long")
        layout(long, "double")
        yield lambda: long.scheduler.settled
        assert long.scheduler.requested_count <= 6 and not medium.scheduler.rasters
        state["phase"] = "long-double-traversal"
        for page in range(0, 300, 2):
            long.verticalScrollBar().setValue(round(long.page_layout.pages[page].y))
            yield lambda: all(long.scheduler.budget.get(long.scheduler._key(i), record=False)
                              for i in long.scheduler.visible)
            assert len(long.scheduler.targets) <= 8
            if page % 20 == 0 or page == 298:
                sample(page)
        yield lambda: long.scheduler.settled
        state["phase"] = "cache-return-layout"
        for page in (290, 280, 290):
            focus(long, page)
            yield lambda: long.scheduler.settled
        before = long.scheduler.requested_count
        for mode in ("single", "double_cover", "double"):
            layout(long, mode)
            yield lambda: long.scheduler.settled
        reuse_renders = long.scheduler.requested_count - before
        assert reuse_renders <= 2
        # A distant jump must not render the skipped path.
        focus(long, 30)
        yield lambda: long.scheduler.settled
        before = long.scheduler.requested_count
        focus(long, 250)
        yield lambda: long.scheduler.settled
        assert long.scheduler.requested_count - before <= 8
        capture("04-long-page-251")
        long.set_zoom(3)
        yield lambda: long.scheduler.settled
        layout(long, "double_cover")
        yield lambda: long.scheduler.settled
        capture("05-long-300-percent")
        long.set_zoom(1)
        focus(long, 299)
        yield lambda: long.scheduler.settled
        capture("06-long-final-orphan")
        long_position = long.horizontalScrollBar().value(), long.verticalScrollBar().value()
        state["phase"] = "tab-switch"
        for _ in range(3):
            for view, position in ((medium, medium_position), (long, long_position)):
                window.manager.activate(view.session.document_id)
                yield lambda: view.scheduler.settled
                assert (view.horizontalScrollBar().value(), view.verticalScrollBar().value()) == position
                assert window.layout_actions[view.session.ui.layout].isChecked()
        assert window.manager.workers_started == 2
        sample(299)
        window.manager.request_close(medium.session.document_id)
        yield lambda: medium.session.lifecycle == Lifecycle.CLOSED
        window.open_paths([str(directory / "odd.pdf")])
        yield lambda: ready("odd") is not None
        odd = attach("odd")
        state["phase"] = "odd"
        layout(odd, "double")
        focus(odd, 4)
        yield lambda: odd.scheduler.settled
        capture("07-odd-final-left")
        layout(odd, "double_cover")
        yield lambda: odd.scheduler.settled
        assert tuple(row.page_indices for row in odd.page_layout.rows) == ((0,), (1, 2), (3, 4))
        window.manager.request_close(odd.session.document_id)
        yield lambda: odd.session.lifecycle == Lifecycle.CLOSED
        window.open_paths([str(directory / "mixed.pdf")])
        yield lambda: ready("mixed") is not None
        mixed = attach("mixed")
        state["phase"] = "mixed"
        for mode in ("single", "double", "double_cover"):
            layout(mixed, mode)
            for page in (0, 2, 4, 6, 7):
                focus(mixed, page)
                yield lambda: mixed.scheduler.settled
        mixed.set_zoom(.75)
        layout(mixed, "double")
        focus(mixed, 0)
        yield lambda: mixed.scheduler.settled
        capture("08-mixed-top-aligned")
        focus(mixed, 4)
        yield lambda: mixed.scheduler.settled
        capture("09-mixed-crop-rotated")
        state["reuse_renders"] = reuse_renders
        state["dpr"] = mixed.viewport().devicePixelRatioF()
        sample(4)
        state["phase"] = "idle"
        yield delay(.5)

    iterator, condition = workflow(), [lambda: True]
    timer, started = QTimer(), perf_counter()
    timer.setTimerType(Qt.TimerType.PreciseTimer)

    def tick():
        now = perf_counter()
        state["ticks"] += 1
        state["active_ticks"] += int(window.isActiveWindow())
        gap = (now - state["last"]) * 1000
        metrics.add("event_loop_ms", gap)
        metrics.add(f"event_loop_{state['phase']}_ms", gap)
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
    assert window.manager.is_shutdown and not mp.active_children()
    assert state["failed"] is None, state
    assert window.manager.resident_budget.bytes == window.manager.budget.bytes == 0
    for name, frame in frames:
        assert frame.save(str(directory / f"{name}.png"))
    cache = window.manager.resident_budget
    return {"platform": app.platformName(), "qt": qt_version, "dpr": state["dpr"],
            "metrics": metrics.report(), "manager": window.manager.metrics.report(),
            "views": [{"name": Path(v.session.canonical_path).stem, "requests": v.scheduler.requested_count,
                       "metrics": v.scheduler.metrics.report()} for v in views],
            "resident_limit": cache.max_bytes, "resident_peak": cache.peak_bytes,
            "transit_peak": window.manager.budget.peak_bytes, "cache_hits": cache.hits,
            "cache_misses": cache.misses, "evictions": cache.evictions,
            "cache_return_layout_renders": state["reuse_renders"], "transitions": transitions,
            "active_ticks": state["active_ticks"], "ticks": state["ticks"],
            "memory": memory, "children_after_close": len(mp.active_children())}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path)
    args = parser.parse_args()
    if args.fixtures:
        fixtures(args.fixtures)
        return
    root = Path(".m0-artifacts")
    root.mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="m4-native-", dir=root)).resolve()
    subprocess.run([sys.executable, "-m", "tests.m4_smoke", "--fixtures", str(directory)], check=True, timeout=60)
    report = native_smoke(directory)
    (directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("M4_NATIVE_PASS " + str(directory))
    print(json.dumps({k: v for k, v in report.items() if k not in ("memory", "transitions")}))


if __name__ == "__main__":
    mp.freeze_support()
    main()
