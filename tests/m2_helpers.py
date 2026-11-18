"""Test-only delay injection and native viewer smoke; no runtime test operations."""

import time


def slow_render_worker(conn, document_id, source):
    import pymupdf
    from pdf_annotator.worker.document_service import document_worker_main

    original = pymupdf.Page.get_pixmap

    def delayed(page, *args, **kwargs):
        time.sleep(0.25)
        return original(page, *args, **kwargs)

    pymupdf.Page.get_pixmap = delayed
    document_worker_main(conn, document_id, source)


def native_smoke() -> None:
    """Run the actual entry point on Windows and save inspectable native frames."""
    from collections import deque
    import json
    import multiprocessing as mp
    import os
    from pathlib import Path
    import platform
    import subprocess
    import sys
    import tempfile

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from pdf_annotator.app.document_session import Lifecycle
    from pdf_annotator.app.main_window import MainWindow
    from pdf_annotator.app.document_manager import DocumentManager
    from pdf_annotator.viewer.pdf_view import PdfView
    from pdf_annotator.viewer.render_scheduler import RenderScheduler
    from pdf_annotator.main import main
    from pdf_annotator.worker import document_service
    from pdf_annotator.worker.protocol import Operation

    assert sys.platform == "win32" and os.environ.get("QT_QPA_PLATFORM") == "windows"
    root = Path(".m0-artifacts")
    root.mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="m2-native-", dir=root)).resolve()
    subprocess.run([sys.executable, "-m", "tests.m1_fixtures", str(directory)], check=True, timeout=30)
    # Delay is entirely in the test worker, not in the GUI event loop.
    document_service.document_worker_main = slow_render_worker
    app = QApplication([])
    assert app.platformName() == "windows"
    timer = QTimer()
    beats = deque(maxlen=8192)
    slow_ticks = []
    frames = []
    samples = []
    state = {"phase": "open", "window": None, "failed": None, "workers": [],
             "a": None, "position": 0, "slow_y": 0, "last_step": 0.0}
    started = time.monotonic()
    timings = {}
    spans = {}

    def measured(name, function):
        def call(*args, **kwargs):
            begin = time.monotonic()
            try:
                return function(*args, **kwargs)
            finally:
                spans[name] = max(spans.get(name, 0), (time.monotonic() - begin) * 1000)
        return call

    # Diagnostic spans distinguish viewer work from native screenshot overhead.
    DocumentManager._poll = measured("manager_poll_ms", DocumentManager._poll)
    RenderScheduler.tick = measured("scheduler_tick_ms", RenderScheduler.tick)
    PdfView.paintEvent = measured("paint_ms", PdfView.paintEvent)

    def ready(view):
        return set(view.scheduler.targets) <= view.scheduler.rasters.keys() and view.scheduler.pending is None

    def capture(name):
        window = state["window"]
        assert window.windowHandle().isExposed()
        # Keep the fixed seven test snapshots until shutdown. PNG encoding and
        # disk writes must not contaminate viewer heartbeat measurements.
        frames.append((name, measured("native_capture_ms", lambda: window.grab().toImage())()))
        samples.append({"phase": name, "resident_bytes": window.manager.resident_budget.bytes,
                        "in_flight_bytes": window.manager.budget.bytes})

    def tick():
        now = time.monotonic()
        if beats and now - beats[-1] > 0.04:
            slow_ticks.append({"phase": state["phase"], "gap_ms": (now - beats[-1]) * 1000})
        beats.append(now)
        try:
            if now - started > 40:
                raise TimeoutError(f"Native smoke timeout in {state['phase']}")
            if state["window"] is None:
                window = next(w for w in app.topLevelWidgets() if isinstance(w, MainWindow))
                state["window"] = window
                window.resize(980, 1040)
                window.open_paths([str(directory / "long.pdf")])
            window = state["window"]
            sessions = list(window.manager.sessions.values())
            phase = state["phase"]
            if phase == "open" and sessions and sessions[0].lifecycle == Lifecycle.READY:
                view = window._widgets[sessions[0].document_id]
                assert not view.scheduler.rasters
                state["a"] = view
                state["workers"].append(sessions[0].worker)
                capture("01-placeholder")
                state["phase"] = "initial"
            elif phase == "initial" and ready(state["a"]):
                view = state["a"]
                assert 0 in view.scheduler.rasters and 1 in view.scheduler.visible
                timings["initial_ready_s"] = now - started
                capture("02-first-pages")
                state["phase"] = "slow"
            elif phase == "slow" and now - state["last_step"] > 0.06:
                state["slow_y"] += 40
                state["a"].verticalScrollBar().setValue(state["slow_y"])
                state["last_step"] = now
                if state["slow_y"] >= 900:
                    state["phase"] = "slow_ready"
            elif phase == "slow_ready" and ready(state["a"]):
                capture("03-continuous-scroll")
                view = state["a"]
                for page in range(10, 251):
                    view.verticalScrollBar().setValue(int(view.page_layout.pages[page].y))
                assert not view.scheduler.rasters
                state["position"] = view.verticalScrollBar().value()
                timings["jump_started_s"] = now - started
                capture("04-distant-placeholder")
                state["phase"] = "far"
            elif phase == "far" and ready(state["a"]):
                assert 250 in state["a"].scheduler.rasters
                timings["jump_ready_s"] = now - started - timings["jump_started_s"]
                capture("05-distant-raster")
                window.open_paths([str(directory / "alternative.pdf")])
                state["phase"] = "other"
            elif phase == "other" and len(sessions) == 2 and sessions[1].lifecycle == Lifecycle.READY:
                view = window._widgets[sessions[1].document_id]
                if ready(view):
                    assert not state["a"].scheduler.rasters
                    state["workers"].append(sessions[1].worker)
                    capture("06-other-tab")
                    window.manager.activate(state["a"].session.document_id)
                    state["phase"] = "back"
            elif phase == "back" and ready(state["a"]):
                assert state["a"].verticalScrollBar().value() == state["position"]
                assert window.manager.workers_started == 2
                assert state["a"].session.worker is state["workers"][0]
                capture("07-restored-tab")
                timer.stop()
                window.close()
        except Exception as exc:
            state["failed"] = repr(exc)
            timer.stop()
            if state["window"] is not None:
                state["window"].close()
            else:
                app.quit()

    timer.timeout.connect(tick)
    timer.start(10)
    result = main([])
    assert result == 0 and state["failed"] is None, state["failed"]
    assert state["window"].manager.is_shutdown and not mp.active_children()
    assert all(w.stopped.is_set() and not w.forced_termination for w in state["workers"])
    for name, frame in frames:
        assert frame.save(str(directory / f"{name}.png"))
    intervals = sorted((b - a) * 1000 for a, b in zip(beats, list(beats)[1:]))
    report = {"platform": app.platformName(), "os": platform.platform(),
              "python": platform.python_version(), "processor": os.environ.get("PROCESSOR_IDENTIFIER"),
              "fixtures": ["long.pdf: 300 text pages", "alternative.pdf: 5 text pages"],
              "timings": timings, "heartbeat_max_ms": max(intervals),
              "heartbeat_p95_ms": intervals[int(len(intervals) * 0.95)],
              "slow_ticks": slow_ticks,
              "max_spans_ms": spans,
              "samples": samples, "raster_requests": [w.submitted_counts[Operation.RENDER] for w in state["workers"]],
              "resident_peak": state["window"].manager.resident_budget.peak_bytes,
              "in_flight_peak": state["window"].manager.budget.peak_bytes,
              "children_after_close": len(mp.active_children())}
    (directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("M2_NATIVE_PASS " + str(directory))
    print(json.dumps(report))


if __name__ == "__main__":
    native_smoke()
