"""M3 budget comparison, native Windows only. No product knobs or IPC changes.

python -m tests.m3_budget_benchmark --compare
Runs 256/128/96 then 96/128/256 MiB in fresh processes on identical local fixtures.
Screenshot capture/encoding occurs only after all measured navigation and samples.
"""

import argparse
from collections import Counter
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
from time import perf_counter
import weakref

from .m3_benchmark import create_fixtures, working_set


def distribution(values: list[float]) -> dict:
    ordered = sorted(values)
    return ({"count": len(ordered), "p50": ordered[len(ordered) // 2],
             "p95": ordered[min(len(ordered) - 1, int(len(ordered) * .95))],
             "max": ordered[-1]} if ordered else {})


def run(directory: Path, budget_mib: int, output: Path, passes: int = 1) -> dict:
    from PySide6 import __version__ as qt_version
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtWidgets import QApplication
    from pdf_annotator.app.document_session import Lifecycle
    from pdf_annotator.app.main_window import MainWindow
    from pdf_annotator.viewer.raster_cache import RasterCache
    from pdf_annotator.worker.protocol import Operation

    app = QApplication([])
    assert sys.platform == "win32" and app.platformName() == "windows"
    window = MainWindow()
    cache = RasterCache(budget_mib * 1024 * 1024)
    window.manager.resident_budget = cache
    window.resize(1080, 920)
    window.show()
    visits, memory, views, workers, image_refs = [], [], [], [], []
    adoptions, rerenders = Counter(), Counter()
    seen_keys = set()
    loop_intervals = []
    state = {"phase": "startup", "failed": None, "last": perf_counter()}
    original_put = cache.put

    def observed_put(key, cost, scale, make_image):
        stored = original_put(key, cost, scale, make_image)
        if stored:
            identity = (key.document_id, key.generation, key.page_index, key.scale, key.dpr)
            adoptions[state["phase"]] += 1
            rerenders[state["phase"]] += identity in seen_keys
            seen_keys.add(identity)
            # Weak references never keep Qt images/rasters alive for diagnostics.
            image_refs.append(weakref.ref(cache.preview(key).image))
        return stored

    cache.put = observed_put

    def ready(name):
        return next((v for v in window._widgets.values()
                     if Path(v.session.canonical_path).stem == name
                     and v.session.lifecycle == Lifecycle.READY), None)

    def attach(view):
        views.append(view)
        workers.append(view.session.worker)

    def delay(seconds):
        until = perf_counter() + seconds
        return lambda: perf_counter() >= until

    def sample():
        active_workers = [w for w in workers if not w.stopped.is_set()]
        memory.append({"phase": state["phase"], "resident": cache.bytes, "rasters": cache.count,
                       "live_qimages": sum(ref() is not None for ref in image_refs),
                       "retired": len(cache._retired), "transit": window.manager.budget.bytes,
                       "gui_ws": working_set(os.getpid()),
                       "worker_ws": [working_set(w.pid) for w in active_workers]})
        assert cache.bytes <= cache.max_bytes
        assert sum(ref() is not None for ref in image_refs) == cache.count
        assert not cache._retired  # sample runs outside paint/borrow scopes

    def visit(view, page, phase, *, zoom=None, activate=False, dwell=.04):
        state["phase"] = phase
        started = perf_counter()
        if activate:
            window.manager.activate(view.session.document_id)
        if zoom is not None:
            view.set_zoom(zoom)
        view.verticalScrollBar().setValue(round(view.page_layout.pages[page].y))
        scheduler = view.scheduler
        keys = tuple(scheduler._key(i) for i in scheduler.visible)
        assert keys
        blank = any(cache.preview(k) is None for k in keys)

        def available():
            # Inspect without mutating LRU/hit counters. Require definitive keys.
            return all((r := cache.preview(k)) is not None and r.key == k for k in keys)

        hit = available()
        if not hit:
            yield available
        elapsed = (perf_counter() - started) * 1000
        visits.append({"phase": phase, "page": page, "ms": elapsed, "hit": hit,
                       "placeholder": blank, "zoom": view.session.ui.zoom})
        yield delay(dwell)  # same reading pace at every budget, allow prefetch

    def workflow():
        window.open_paths([str(directory / "medium.pdf")])
        yield lambda: ready("medium") is not None
        medium = ready("medium")
        attach(medium)
        for page in range(60):
            yield from visit(medium, page, "medium_single", dwell=.05)
            if page % 10 == 0:
                sample()
        for page in (56, 55, 57, 59):
            yield from visit(medium, page, "medium_recent")
        window.manager.request_close(medium.session.document_id)
        yield lambda: medium.session.lifecycle == Lifecycle.CLOSED
        assert cache.bytes == 0

        window.open_paths([str(directory / "long.pdf")])
        yield lambda: ready("long") is not None
        long = ready("long")
        attach(long)
        for lap in range(passes):
            for page in range(400):
                yield from visit(long, page, "long_single" if lap == 0 else "long_revisit", dwell=.025)
                if page % 20 == 0 or page == 399:
                    sample()
        for page in (395, 390, 392, 399, 393, 395, 399):
            yield from visit(long, page, "long_recent")
        for page in (10, 350, 20, 399):
            yield from visit(long, page, "distant_jump")

        # A 24-page moderate-zoom reading window plus short and extended returns.
        # Its working set is near 128 MiB at native DPR 1.25, beyond 96 MiB.
        yield from visit(long, 200, "zoom", zoom=1.5, dwell=.15)
        for page in range(200, 224):
            yield from visit(long, page, "moderate_read", dwell=.15)
        sample()
        for page in (220, 218, 219, 223):
            yield from visit(long, page, "moderate_recent", dwell=.15)
        for page in range(223, 199, -1):
            yield from visit(long, page, "moderate_return", dwell=.15)
        sample()
        yield from visit(long, 350, "zoom", zoom=1, dwell=.15)

        window.open_paths([str(directory / "medium.pdf")])
        yield lambda: ready("medium") is not None
        medium = ready("medium")
        attach(medium)
        assert len(window.manager.sessions) == 2 and not long.scheduler.rasters
        for page in range(30):
            yield from visit(medium, page, "two_documents", dwell=.05)
        yield from visit(medium, 12, "zoom", zoom=1.5, dwell=.15)
        sample()
        for _ in range(3):
            yield from visit(long, 350, "tab_restore", activate=True, dwell=.15)
            yield from visit(medium, 12, "tab_restore", activate=True, dwell=.15)
            sample()
        state["phase"] = "idle"
        yield delay(.5)
        sample()
        state["dpr"] = medium.viewport().devicePixelRatioF()
        # Measurements end before captures: no screenshot storage in working sets.
        state["phase"] = "capture"
        assert window.windowHandle().isExposed()
        window.grab().save(str(output / "medium-150.png"))
        window.manager.activate(long.session.document_id)
        yield lambda: long.scheduler.settled
        window.grab().save(str(output / "long-100.png"))

    iterator = workflow()
    condition = [lambda: True]
    started = perf_counter()
    timer = QTimer()
    timer.setTimerType(Qt.TimerType.PreciseTimer)

    def tick():
        now = perf_counter()
        if state["phase"] != "capture":
            loop_intervals.append((now - state["last"]) * 1000)
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
    timer.start(5)
    app.exec()
    assert state["failed"] is None, state["failed"]
    assert window.manager.is_shutdown and not mp.active_children()
    assert cache.bytes == cache.count == window.manager.budget.bytes == 0
    assert not any(ref() is not None for ref in image_refs)
    phases = {}
    for phase in dict.fromkeys(row["phase"] for row in visits):
        rows = [row for row in visits if row["phase"] == phase]
        phases[phase] = {"latency_ms": distribution([r["ms"] for r in rows]),
                         "visible_hits": sum(r["hit"] for r in rows),
                         "placeholders": sum(r["placeholder"] for r in rows),
                         "blank_over_50ms": sum(r["placeholder"] and r["ms"] > 50 for r in rows),
                         "adopted": adoptions[phase], "rerenders": rerenders[phase]}
    return {"budget_mib": budget_mib, "passes": passes, "platform": app.platformName(), "qt": qt_version,
            "python": platform.python_version(), "os": platform.platform(), "dpr": state["dpr"],
            "elapsed_s": perf_counter() - started, "phases": phases,
            "viewport_ms": distribution([r["ms"] for r in visits]),
            "event_loop_ms": distribution(loop_intervals),
            "cache_hits": cache.hits, "cache_misses": cache.misses, "evictions": cache.evictions,
            "resident_peak": cache.peak_bytes,
            "gui_ws_peak": max(row["gui_ws"] for row in memory),
            "worker_ws_peak": max(max(row["worker_ws"], default=0) for row in memory),
            "requests": sum(w.submitted_counts[Operation.RENDER] for w in workers),
            "completed": sum(w.completed_counts[Operation.RENDER] for w in workers),
            "rerenders": sum(rerenders.values()), "discarded": sum(w.stale_discarded for w in workers),
            "transit_peak": window.manager.budget.peak_bytes,
            "live_images_after_close": sum(ref() is not None for ref in image_refs),
            "children_after_close": len(mp.active_children()), "memory": memory, "visits": visits}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compare", action="store_true")
    parser.add_argument("--prepare", type=Path)
    parser.add_argument("--fixtures", type=Path)
    parser.add_argument("--budget", type=int, choices=(256, 128, 96))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--passes", type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    if args.prepare:
        create_fixtures(args.prepare, image_heavy=True)
        return
    if args.compare:
        Path(".m0-artifacts").mkdir(exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix="m3-budgets-", dir=".m0-artifacts")).resolve()
        subprocess.run([sys.executable, "-m", __package__ + ".m3_budget_benchmark",
                        "--prepare", str(root)], check=True, timeout=120)
        manifest = {}
        for name in ("medium.pdf", "long.pdf"):
            with (root / name).open("rb") as stream:
                manifest[name] = {"bytes": (root / name).stat().st_size,
                                  "sha256": hashlib.file_digest(stream, "sha256").hexdigest()}
        (root / "fixtures.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print("BUDGET_COMPARISON " + str(root), flush=True)
        for number, budget in enumerate((256, 128, 96, 96, 128, 256), 1):
            output = root / f"run-{number}-{budget}"
            output.mkdir()
            subprocess.run([sys.executable, "-m", __package__ + ".m3_budget_benchmark",
                            "--fixtures", str(root), "--budget", str(budget), "--output", str(output),
                            "--passes", str(args.passes)],
                           check=True, timeout=190)
        return
    if not (args.fixtures and args.output and args.budget):
        parser.error("Use --compare, --prepare DIR or --fixtures DIR --budget N --output DIR")
    args.output.mkdir(parents=True, exist_ok=True)
    report = run(args.fixtures.resolve(), args.budget, args.output.resolve(), args.passes)
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("BUDGET_PASS " + json.dumps({k: v for k, v in report.items() if k not in ("memory", "visits", "phases")}), flush=True)


if __name__ == "__main__":
    main()
