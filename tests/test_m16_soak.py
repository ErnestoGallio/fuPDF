"""M16-C repeated lifecycle soak using the M16-B six-document workload."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from time import perf_counter

import pytest

from pdf_annotator.app.document_session import Lifecycle
from pdf_annotator.app.main_window import MainWindow
from pdf_annotator.domain.annotation_commands import CommandContext, Redo, Undo
from pdf_annotator.domain.external_edits import ExternalEditKind
from pdf_annotator.domain.geometry import PdfPoint
from pdf_annotator.worker import document_service
from pdf_annotator.worker.protocol import Operation

from .conftest import close_window_discard, wait_until
from .m16_diagnostics import snapshot
from .m151_helpers import candidate_worker
from .test_external_edits_m151 import command_for, find_native, helper
from .test_save_m15 import save

CYCLE_COUNT = int(os.environ.get("M16_SOAK_CYCLES", "2"))


@pytest.fixture(scope="module")
def m16_soak_fixtures(tmp_path_factory) -> Path:
    directory = tmp_path_factory.mktemp("m16-soak-pdfs")
    subprocess.run([sys.executable, "-m", "tests.m1_fixtures", str(directory)],
                   cwd=Path(__file__).resolve().parents[1], check=True,
                   capture_output=True, timeout=60)
    return directory


def _open(window: MainWindow, paths: list[Path]) -> list[object]:
    window.open_paths([str(path) for path in paths])
    canonical = {str(path.resolve()) for path in paths}
    wait_until(lambda: {session.canonical_path for session in window.manager.sessions.values()
                        if session.lifecycle == Lifecycle.READY} >= canonical, timeout=45)
    return [next(session for session in window.manager.sessions.values()
                 if session.canonical_path == str(path.resolve())) for path in paths]


def _documents(state: dict[str, object]) -> dict[str, dict[str, object]]:
    return {item["document_id"]: item for item in state["documents"]}  # type: ignore[index]


def _assert_reconciled(window: MainWindow) -> dict[str, object]:
    state = snapshot(window.manager, window._widgets)
    global_state = state["global"]  # type: ignore[index]
    documents = _documents(state)
    resident = global_state["raster_cache"]
    transit = global_state["in_flight_raster"]
    text = global_state["text_cache"]
    assert global_state["session_count"] == len(documents)
    assert sum(item["raster_cache"]["bytes"] for item in documents.values()) == resident["bytes"]  # type: ignore[index]
    assert sum(item["raster_cache"]["entries"] for item in documents.values()) == resident["entries"]  # type: ignore[index]
    assert sum(item["text_cache"]["bytes"] for item in documents.values()) == text["bytes"]  # type: ignore[index]
    assert transit["document_reserved_count"] == transit["count"]  # type: ignore[index]
    assert 0 <= resident["bytes"] <= resident["max_bytes"]  # type: ignore[index]
    assert 0 <= transit["bytes"] <= transit["max_bytes"]  # type: ignore[index]
    assert 0 <= transit["count"] <= transit["max_count"]  # type: ignore[index]
    return state


def _assert_closed_baseline(window: MainWindow) -> None:
    state = _assert_reconciled(window)
    global_state = state["global"]  # type: ignore[index]
    assert state["documents"] == []  # type: ignore[index]
    assert global_state["active_document_id"] is None
    assert global_state["raster_cache"]["bytes"] == 0  # type: ignore[index]
    assert global_state["raster_cache"]["entries"] == 0  # type: ignore[index]
    assert global_state["in_flight_raster"]["bytes"] == 0  # type: ignore[index]
    assert global_state["in_flight_raster"]["count"] == 0  # type: ignore[index]
    assert global_state["text_cache"]["bytes"] == 0  # type: ignore[index]
    assert global_state["text_cache"]["entries"] == 0  # type: ignore[index]
    assert not window._widgets


def _native_fixture(path: Path) -> None:
    subprocess.run([sys.executable, "-m", "tests.m151_helpers", "fixture", str(path)],
                   cwd=Path(__file__).resolve().parents[1], check=True,
                   capture_output=True, timeout=60)


def _rss_bytes(pid: int) -> int | None:
    """Best-effort Windows working-set observation; never a test threshold."""
    if sys.platform != "win32":
        return None
    try:
        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("page_fault_count", wintypes.DWORD),
                       ("peak_working_set_size", ctypes.c_size_t),
                       ("working_set_size", ctypes.c_size_t),
                       ("quota_peak_paged_pool_usage", ctypes.c_size_t),
                       ("quota_paged_pool_usage", ctypes.c_size_t),
                       ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
                       ("quota_non_paged_pool_usage", ctypes.c_size_t),
                       ("pagefile_usage", ctypes.c_size_t),
                       ("peak_pagefile_usage", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE,
                                                ctypes.POINTER(ProcessMemoryCounters),
                                                wintypes.DWORD)
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        handle = kernel32.GetCurrentProcess() if pid == os.getpid() else kernel32.OpenProcess(
            0x1000 | 0x0400, False, pid)
        if not handle:
            return None
        try:
            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return None
            return int(counters.working_set_size)
        finally:
            if pid != os.getpid():
                kernel32.CloseHandle(handle)
    except (AttributeError, OSError):
        return None


def _rss_sample(window: MainWindow | None = None) -> dict[str, object]:
    pids = [os.getpid()]
    if window is not None:
        pids.extend(session.worker.pid for session in window.manager.sessions.values()
                    if session.worker is not None and session.worker.pid is not None)
    measurements = {str(pid): _rss_bytes(pid) for pid in pids}
    values = [value for value in measurements.values() if value is not None]
    return {"available": len(values) == len(measurements), "bytes_by_pid": measurements,
            "total_bytes": sum(values) if len(values) == len(measurements) else None}


def _cycle_paths(cycle_dir: Path, pdf_fixture_copy, m16_soak_fixtures: Path) -> list[Path]:
    native = cycle_dir / "native-source.pdf"
    _native_fixture(native)
    free_text, callout = cycle_dir / "external-freetext.pdf", cycle_dir / "external-callout.pdf"
    shutil.copyfile(native, free_text)
    shutil.copyfile(native, callout)
    dense = pdf_fixture_copy(cycle_dir / "dense.pdf", pages=20, dense=True)
    regular = pdf_fixture_copy(cycle_dir / "regular.pdf", pages=12)
    geometry = pdf_fixture_copy(cycle_dir / "geometry.pdf", geometry=True, pages=8)
    large = cycle_dir / "large.pdf"
    shutil.copyfile(m16_soak_fixtures / "large.pdf", large)
    return [free_text, callout, dense, large, regular, geometry]


def _run_cycle(cycle: int, tmp_path: Path, app, pdf_fixture_copy,
               m16_soak_fixtures: Path) -> dict[str, object]:
    cycle_dir = tmp_path / f"cycle-{cycle}"
    cycle_dir.mkdir()
    paths = _cycle_paths(cycle_dir, pdf_fixture_copy, m16_soak_fixtures)
    window = MainWindow()
    window.resize(900, 700)
    window.show()
    opened_at = perf_counter()
    try:
        sessions = _open(window, paths)
        open_ms = (perf_counter() - opened_at) * 1000
        text_session, callout_session, search_session, regional_session, regular_session, geometry_session = sessions
        text_view = window._widgets[text_session.document_id]
        callout_view = window._widgets[callout_session.document_id]
        search_view = window._widgets[search_session.document_id]
        regional_view = window._widgets[regional_session.document_id]
        for view in window._widgets.values():
            view.scheduler.metrics.enabled = True
        assert len({session.worker.pid for session in sessions}) == 6
        wait_until(lambda: text_session.inventory_complete and callout_session.inventory_complete)
        window.manager.activate(regular_session.document_id)
        regular_view = window._widgets[regular_session.document_id]
        wait_until(lambda: regular_view.scheduler.settled)
        assert regular_session.worker.submitted_counts[Operation.RENDER] > 0

        text_row = find_native(helper("inspect", paths[0]), "plain")
        callout_row = find_native(helper("inspect", paths[1]), "callout")
        text_command = command_for(text_session, text_row, ExternalEditKind.MOVE, delta=(8, 6))
        callout_command = command_for(callout_session, callout_row, ExternalEditKind.ANCHOR,
                                      anchor=PdfPoint(230, 475))
        assert window.manager.submit_annotation(text_command).request_id is not None
        assert window.manager.submit_annotation(callout_command).request_id is not None

        window.manager.activate(search_session.document_id)
        wait_until(lambda: search_view.scheduler.settled)
        search_view.set_search_query("Prima")
        wait_until(lambda: search_view.search_layer.pending is not None)
        window.manager.activate(regional_session.document_id)
        regional_view.set_zoom(8.0)
        wait_until(lambda: regional_view.scheduler.regional_requested_count > 0)
        wait_until(lambda: text_session.pending_mutations == callout_session.pending_mutations == 0)
        window.manager.activate(search_session.document_id)
        wait_until(lambda: search_view.scheduler.settled and search_session.ui.search.complete)
        assert search_session.ui.search.results and not regional_session.ui.search.query

        window.manager.activate(text_session.document_id)
        appearance_before = text_session.worker.submitted_counts[Operation.ANNOTATION_APPEARANCE]
        wait_until(lambda: text_view.scheduler.settled)
        wait_until(lambda: text_session.worker.submitted_counts[Operation.ANNOTATION_APPEARANCE] > appearance_before)
        wait_until(lambda: _documents(_assert_reconciled(window))[text_session.document_id]
                   ["raster_cache"]["annotation_appearance_entries"] > 0)
        window.manager.activate(callout_session.document_id)
        callout_appearance_before = callout_session.worker.submitted_counts[Operation.ANNOTATION_APPEARANCE]
        wait_until(lambda: callout_view.scheduler.settled)
        wait_until(lambda: callout_session.worker.submitted_counts[Operation.ANNOTATION_APPEARANCE]
                   > callout_appearance_before)
        wait_until(lambda: _documents(_assert_reconciled(window))[callout_session.document_id]
                   ["raster_cache"]["annotation_appearance_entries"] > 0)
        assert _documents(_assert_reconciled(window))[text_session.document_id]["raster_cache"]["entries"] == 0  # type: ignore[index]

        window.manager.activate(text_session.document_id)
        assert window.manager.undo_active().request_id is not None
        wait_until(lambda: text_session.pending_mutations == 0 and text_session.history_position == 0)
        assert callout_session.history_position == 1
        assert window.manager.redo_active().request_id is not None
        wait_until(lambda: text_session.pending_mutations == 0 and text_session.history_position == 1)
        save(window.manager, text_session, cycle_dir / "save-as.pdf")
        assert text_session.document_generation == 2 and not text_session.dirty
        assert callout_session.document_generation == 1 and callout_session.dirty
        before_close = _assert_reconciled(window)
        rss_peak = _rss_sample(window)
        performance = {
            "ordinary_render_requests": regular_session.worker.submitted_counts[Operation.RENDER],
            "regional_render_requests": regional_view.scheduler.regional_requested_count,
            "search": {"requested": search_view.search_layer.requested,
                       "adopted": search_view.search_layer.adopted,
                       "stale_discarded": search_view.search_layer.stale_discarded},
            "annotation_appearance_requests": {
                "freetext": text_session.worker.submitted_counts[Operation.ANNOTATION_APPEARANCE],
                "callout": callout_session.worker.submitted_counts[Operation.ANNOTATION_APPEARANCE],
            },
            "stale_discarded": sum(session.worker.stale_discarded for session in sessions),
            "backpressure": sum(view.scheduler.backpressure_count for view in window._widgets.values()),
            "resident_peak_bytes": before_close["global"]["raster_cache"]["peak_bytes"],  # type: ignore[index]
            "in_flight_peak_bytes": before_close["global"]["in_flight_raster"]["peak_bytes"],  # type: ignore[index]
        }

        closing_at = perf_counter()
        assert window.manager.request_shutdown(discard_changes=True)
        wait_until(lambda: window.manager.is_shutdown, timeout=45)
        close_ms = (perf_counter() - closing_at) * 1000
        _assert_closed_baseline(window)
        return {
            "cycle": cycle,
            "open_ms": round(open_ms, 1),
            "close_ms": round(close_ms, 1),
            "rss_peak": rss_peak,
            **performance,
        }
    finally:
        close_window_discard(window)


def test_repeated_six_document_lifecycle_soak(tmp_path: Path, app, pdf_fixture_copy,
                                               m16_soak_fixtures, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", candidate_worker)
    baseline = _rss_sample()
    cycles = [_run_cycle(index, tmp_path, app, pdf_fixture_copy, m16_soak_fixtures)
              for index in range(1, CYCLE_COUNT + 1)]
    final = _rss_sample()
    totals = [sample["rss_peak"]["total_bytes"] for sample in cycles]
    report = {"cycles": CYCLE_COUNT, "rss": {"baseline": baseline, "cycle_peaks": totals,
              "final": final}, "performance": cycles}
    print("M16_C_SOAK " + json.dumps(report, sort_keys=True))
    assert all(sample["regional_render_requests"] > 0 for sample in cycles)
    assert all(sample["search"]["adopted"] > 0 for sample in cycles)
    assert all(sample["annotation_appearance_requests"]["freetext"] > 0 for sample in cycles)
    assert all(sample["annotation_appearance_requests"]["callout"] > 0 for sample in cycles)
