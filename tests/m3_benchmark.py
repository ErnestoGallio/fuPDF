"""Reproducible M3 diagnostics. Fixture/DisplayList work runs outside the Qt process.

python -m tests.m3_benchmark --display-list
QT_QPA_PLATFORM=windows python -m tests.m3_benchmark --native
No user PDF is modified. Every run gets a fresh ignored artifact directory.
"""

import argparse
from collections import deque
import ctypes
import gc
import json
import multiprocessing as mp
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
from threading import Event, Thread
from time import get_clock_info, perf_counter, thread_time


def create_fixtures(directory: Path, *, image_heavy: bool = False) -> None:
    import pymupdf
    from random import Random

    pixels = bytes(value for y in range(180) for x in range(300)
                   for value in (int(x / 300 * 255), int(y / 180 * 255), (x + y) % 256))
    image = pymupdf.Pixmap(pymupdf.csRGB, 300, 180, pixels, False)
    random = Random(31028)
    for name, count in (("medium", 60), ("long", 400 if image_heavy else 300)):
        with pymupdf.open() as doc:
            for index in range(count):
                page = doc.new_page(width=595, height=842)
                page.insert_text((42, 42), f"M3 {name} proof / page {index + 1}", fontsize=16)
                text = "\n".join(
                    f"{line + 1:02d}  Testo di revisione: caffè, qualità, linee sottili e piccoli caratteri."
                    for line in range(52))
                page.insert_textbox((42, 65, 553, 630), text, fontsize=9, lineheight=1.15)
                page.insert_image((42, 655, 252, 781), pixmap=image)
                if image_heavy:
                    # A different incompressible RGB image per page: ~60 MB/400p.
                    # This stresses decoded image memory without private user PDFs.
                    noisy = pymupdf.Pixmap(pymupdf.csRGB, 240, 208,
                                           random.randbytes(240 * 208 * 3), False)
                    page.insert_image((420, 40, 550, 153), pixmap=noisy)
                for line in range(12):
                    page.draw_line((285, 660 + line * 10), (548, 660 + line * 10),
                                   color=(.2, .3, .5), width=.2 + line * .08)
                page.insert_text((285, 805), "6pt: qualità  8pt: dettagli", fontsize=6)
            doc.save(directory / f"{name}.pdf", deflate=True)


def display_list_benchmark(directory: Path) -> dict:
    import pymupdf

    rows = []
    for name in ("medium", "long"):
        with pymupdf.open(directory / f"{name}.pdf") as doc:
            for index in (0, 29, 59):
                page = doc[index]
                start = perf_counter()
                display = page.get_displaylist(annots=False)
                build_ms = (perf_counter() - start) * 1000
                for scale in (1.25, 2.0, 3.0):
                    direct, cached = [], []
                    matrix = pymupdf.Matrix(scale, scale)
                    for run in range(9):
                        # Alternate order to avoid always favoring a warmed second path.
                        methods = ("direct", "cached") if run % 2 else ("cached", "direct")
                        for method in methods:
                            start = perf_counter()
                            pix = (page.get_pixmap(matrix=matrix, colorspace=pymupdf.csRGB,
                                                   alpha=False, annots=False) if method == "direct"
                                   else display.get_pixmap(matrix=matrix, colorspace=pymupdf.csRGB, alpha=False))
                            elapsed = (perf_counter() - start) * 1000
                            if run:
                                (direct if method == "direct" else cached).append(elapsed)
                            del pix
                    rows.append({"document": name, "page": index, "scale": scale,
                                 "build_ms": build_ms, "direct_ms": statistics.median(direct),
                                 "display_list_ms": statistics.median(cached)})
                del display, page
    return {"pymupdf": pymupdf.VersionBind, "samples": rows,
            "median_saved_ms": statistics.median(r["direct_ms"] - r["display_list_ms"] for r in rows),
            "median_saved_percent": statistics.median(100 * (1 - r["display_list_ms"] / r["direct_ms"]) for r in rows)}


def working_set(pid: int) -> int:
    """Windows working set, not the app-controlled raster budget."""
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ("peak", "working", "pool1", "pool2", "pool3", "pool4", "pagefile", "peakfile", "private")]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.working
    finally:
        kernel.CloseHandle(handle)


def native_benchmark(directory: Path, *, foreground: bool = False,
                     supersampling: float | None = None) -> dict:
    from PySide6 import __version__ as qt_version
    from PySide6.QtCore import QPoint, QPointF, QTimer, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication
    from pdf_annotator.app.document_session import Lifecycle
    from pdf_annotator.app.main_window import MainWindow
    from pdf_annotator.viewer.metrics import ViewerMetrics
    from pdf_annotator.viewer import render_scheduler
    from pdf_annotator.worker.protocol import Operation

    if supersampling is not None:
        if supersampling not in (1.0, 1.25, 1.5, 2.0):
            raise ValueError("Unsupported benchmark supersampling factor")
        render_scheduler.QUALITY_SUPERSAMPLING = supersampling
    app = QApplication([])
    assert sys.platform == "win32" and app.platformName() == "windows"
    window = MainWindow()
    window.resize(1080, 920)
    window.manager.metrics.enabled = True
    window.show()
    if foreground:
        window.raise_()
        window.activateWindow()
    metrics = ViewerMetrics()
    metrics.enabled = True
    frames, memory, views, workers = [], [], [], []
    gaps = []
    state = {"phase": "startup", "last": perf_counter(), "last_cpu": thread_time(), "failed": None,
             "active_ticks": 0, "ticks": 0}
    stop = Event()
    background = deque(maxlen=8192)

    def heartbeat():
        last = perf_counter()
        while not stop.wait(.005):
            now = perf_counter()
            background.append((now - last) * 1000)
            last = now

    thread = Thread(target=heartbeat, daemon=True)
    thread.start()
    gc_start = [0.0]

    def gc_callback(phase, info):
        if phase == "start":
            gc_start[0] = perf_counter()
        else:
            metrics.add("gc_ms", (perf_counter() - gc_start[0]) * 1000)

    gc.callbacks.append(gc_callback)

    def ready_view(name):
        return next((v for v in window._widgets.values()
                     if Path(v.session.canonical_path).stem == name and v.session.lifecycle == Lifecycle.READY), None)

    def attach(view):
        view.scheduler.metrics.enabled = True
        views.append(view)
        workers.append(view.session.worker)

    def capture(name):
        assert window.windowHandle().isExposed()
        with metrics.span("capture_ms"):
            frames.append((name, window.grab().toImage()))

    def sample(page):
        with metrics.span("memory_sample_ms"):
            cache = window.manager.resident_budget
            memory.append({"page": page, "resident": cache.bytes, "count": cache.count,
                           "transit": window.manager.budget.bytes,
                           "gui_working_set": working_set(os.getpid()),
                           "worker_working_sets": [working_set(w.pid) for w in workers if not w.stopped.is_set()]})

    def delay(seconds):
        until = perf_counter() + seconds
        return lambda: perf_counter() >= until

    def goto(view, page):
        view.verticalScrollBar().setValue(round(view.page_layout.pages[page].y))

    def wheel(view, delta, control=False):
        event = QWheelEvent(QPointF(400, 300), QPointF(400, 300), QPoint(), QPoint(0, delta),
                            Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.ControlModifier if control else Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(view.viewport(), event)

    def workflow():
        window.open_paths([str(directory / "medium.pdf")])
        yield lambda: ready_view("medium") is not None
        medium = ready_view("medium")
        attach(medium)
        yield lambda: medium.scheduler.settled
        capture("01-medium-100")
        state["phase"] = "rapid-100-150"
        for _ in range(6):
            medium.set_zoom(1.5)
            yield delay(.015)
            medium.set_zoom(1.0)
            yield delay(.015)
        yield lambda: medium.scheduler.settled
        state["phase"] = "zoom-preview"
        medium.set_zoom(1.37)
        capture("02-scaled-preview")
        yield lambda: medium.scheduler.settled
        capture("03-sharp-137")
        state["phase"] = "wheel-zoom-scroll"
        for index in range(12):
            wheel(medium, 120 if index < 6 else -120, control=True)
            medium.verticalScrollBar().setValue(medium.verticalScrollBar().value() + 60)
            yield delay(.02)
        yield lambda: medium.scheduler.settled
        medium.set_zoom(8)
        state["phase"] = "high-zoom"
        yield lambda: medium.scheduler.settled
        capture("04-capped-800")
        medium.set_zoom(1)
        goto(medium, 12)
        yield lambda: medium.scheduler.settled
        medium_position = medium.verticalScrollBar().value()
        state["phase"] = "open-long"
        window.open_paths([str(directory / "long.pdf")])
        yield lambda: ready_view("long") is not None
        long = ready_view("long")
        attach(long)
        yield lambda: long.scheduler.settled
        capture("05-long-initial")
        assert not medium.scheduler.rasters
        state["phase"] = "continuous-scroll"
        for step in range(30):
            wheel(long, -60)
            yield delay(.02)
        yield lambda: long.scheduler.settled
        state["phase"] = "m2-tight-jump-loop"
        with metrics.span("tight_jump_loop_ms"):
            for page in range(10, 251):
                goto(long, page)
        yield lambda: long.scheduler.settled
        capture("06-distant-page")
        state["phase"] = "long-traversal"
        for page in range(300):
            goto(long, page)
            yield lambda: all(long.scheduler.budget.get(long.scheduler._key(i), record=False)
                              for i in long.scheduler.visible)
            if page % 10 == 0 or page == 299:
                sample(page)
        yield lambda: long.scheduler.settled
        state["phase"] = "recent-cache-return"
        for page in (290, 280, 290, 299):
            goto(long, page)
            yield lambda: long.scheduler.settled
        long_position = long.verticalScrollBar().value()
        state["phase"] = "tab-restore"
        window.manager.activate(medium.session.document_id)
        yield lambda: medium.scheduler.settled
        assert medium.verticalScrollBar().value() == medium_position
        capture("07-medium-restored")
        window.manager.activate(long.session.document_id)
        yield lambda: long.scheduler.settled
        assert long.verticalScrollBar().value() == long_position
        assert window.manager.workers_started == 2
        capture("08-long-restored")
        sample(299)
        state["phase"] = "idle"
        yield delay(1)

    iterator = workflow()
    condition = [lambda: True]
    started = perf_counter()
    timer = QTimer()
    timer.setTimerType(Qt.TimerType.PreciseTimer)

    def tick():
        now = perf_counter()
        state["ticks"] += 1
        state["active_ticks"] += int(window.isActiveWindow())
        gap = (now - state["last"]) * 1000
        metrics.add("event_loop_ms", gap)
        if gap > 40:
            recent = [event for diagnostic in [metrics, window.manager.metrics,
                                               *(v.scheduler.metrics for v in views)]
                      for event in diagnostic.events if event[0] >= state["last"]]
            gaps.append({"phase": state["phase"], "ms": gap,
                         "gui_cpu_ms": (thread_time() - state["last_cpu"]) * 1000,
                         "recent_spans": recent})
        state["last"] = now
        state["last_cpu"] = thread_time()
        try:
            if now - started > 180:
                raise TimeoutError(f"Benchmark timeout: {state['phase']}")
            with metrics.span("harness_tick_ms"):
                if condition[0]():
                    condition[0] = next(iterator)
        except StopIteration:
            state["dpr"] = views[0].viewport().devicePixelRatioF()
            timer.stop()
            window.close()
        except Exception as exc:
            state["failed"] = repr(exc)
            timer.stop()
            window.close()

    timer.timeout.connect(tick)
    timer.start(10)
    app.exec()
    stop.set()
    thread.join(timeout=1)
    gc.callbacks.remove(gc_callback)
    assert state["failed"] is None, state["failed"]
    assert window.manager.is_shutdown and not mp.active_children()
    assert window.manager.resident_budget.bytes == window.manager.budget.bytes == 0
    for name, frame in frames:
        assert frame.save(str(directory / f"{name}.png"))
    cache = window.manager.resident_budget
    return {"platform": app.platformName(), "qt": qt_version, "python": platform.python_version(),
            "os": platform.platform(), "processor": os.environ.get("PROCESSOR_IDENTIFIER"),
            "dpr": state["dpr"], "elapsed_s": perf_counter() - started,
            "clocks": {name: vars(get_clock_info(name)) for name in ("monotonic", "perf_counter")},
            "metrics": metrics.report(), "manager": window.manager.metrics.report(),
            "views": [{"metrics": v.scheduler.metrics.report(), "requests": v.scheduler.requested_count,
                       "completed": w.completed_counts[Operation.RENDER], "adopted": v.scheduler.adopted_count,
                       "cancelled_queued": w.cancelled_queued,
                       "discarded": v.scheduler.discarded_count + w.stale_discarded}
                      for v, w in zip(views, workers)],
            "cache_hits": cache.hits, "cache_misses": cache.misses, "evictions": cache.evictions,
            "resident_limit": cache.max_bytes, "resident_peak": cache.peak_bytes,
            "transit_limit": window.manager.budget.max_bytes, "transit_peak": window.manager.budget.peak_bytes,
            "single_raster": window.manager.budget.single_bytes, "memory": memory,
            "foreground_requested": foreground, "active_ticks": state["active_ticks"], "ticks": state["ticks"],
            "quality_supersampling": render_scheduler.QUALITY_SUPERSAMPLING,
            "background_heartbeat_max_ms": max(background), "slow_ticks": gaps,
            "children_after_close": len(mp.active_children())}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path)
    parser.add_argument("--display-list", action="store_true")
    parser.add_argument("--native", action="store_true")
    parser.add_argument("--foreground", action="store_true", help="Activate the native window for comparable interactive timings")
    parser.add_argument("--supersampling", type=float, choices=(1.0, 1.25, 1.5, 2.0),
                        help="Override the normal/medium zoom quality factor for comparison")
    args = parser.parse_args()
    if args.fixtures:
        create_fixtures(args.fixtures)
        return
    root = Path(".m0-artifacts")
    root.mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="m3-", dir=root)).resolve()
    subprocess.run([sys.executable, "-m", "tests.m3_benchmark", "--fixtures", str(directory)], check=True, timeout=60)
    report = (display_list_benchmark(directory) if args.display_list else
              native_benchmark(directory, foreground=args.foreground,
                               supersampling=args.supersampling))
    (directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("M3_BENCHMARK_PASS " + str(directory))
    print(json.dumps({key: value for key, value in report.items() if key not in ("memory", "samples")}))


if __name__ == "__main__":
    main()
