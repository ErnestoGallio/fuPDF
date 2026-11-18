"""M16-B deterministic multi-document stress and failure-injection coverage."""

import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

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


def _open(window: MainWindow, paths: list[Path]) -> list[object]:
    window.open_paths([str(path) for path in paths])
    canonical = {str(path.resolve()) for path in paths}
    wait_until(lambda: {session.canonical_path for session in window.manager.sessions.values()
                        if session.lifecycle == Lifecycle.READY} >= canonical, timeout=45)
    return [next(session for session in window.manager.sessions.values()
                 if session.canonical_path == str(path.resolve())) for path in paths]


def _documents(state: dict[str, object]) -> dict[str, dict[str, object]]:
    return {item["document_id"]: item for item in state["documents"]}  # type: ignore[index]


def _assert_accounting(window: MainWindow) -> dict[str, object]:
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


def _native_fixture(path: Path) -> None:
    subprocess.run([sys.executable, "-m", "tests.m151_helpers", "fixture", str(path)],
                   cwd=Path(__file__).resolve().parents[1], check=True,
                   capture_output=True, timeout=60)


@pytest.fixture(scope="module")
def m16_pdf_files(tmp_path_factory) -> Path:
    directory = tmp_path_factory.mktemp("m16-pdfs")
    subprocess.run([sys.executable, "-m", "tests.m1_fixtures", str(directory)],
                   cwd=Path(__file__).resolve().parents[1], check=True,
                   capture_output=True, timeout=60)
    return directory


def _slow_candidate_worker(conn, document_id, source) -> None:
    """Delay only test-worker pixmaps so close exercises pending raster credits."""
    import pymupdf

    page_pixmap = pymupdf.Page.get_pixmap
    annot_pixmap = pymupdf.Annot.get_pixmap

    def delayed_page(page, *args, **kwargs):
        time.sleep(.20)
        return page_pixmap(page, *args, **kwargs)

    def delayed_annotation(annotation, *args, **kwargs):
        time.sleep(.20)
        return annot_pixmap(annotation, *args, **kwargs)

    pymupdf.Page.get_pixmap = delayed_page
    pymupdf.Annot.get_pixmap = delayed_annotation
    candidate_worker(conn, document_id, source)


def test_six_document_interleaved_external_edits_search_render_history_and_save(
        tmp_path: Path, app, pdf_fixture_copy, m16_pdf_files, monkeypatch):
    """Real workers complete independently; identity boundaries must remain intact."""
    monkeypatch.setattr(document_service, "document_worker_main", candidate_worker)
    native = tmp_path / "native-source.pdf"
    _native_fixture(native)
    free_text, callout = tmp_path / "external-freetext.pdf", tmp_path / "external-callout.pdf"
    shutil.copyfile(native, free_text)
    shutil.copyfile(native, callout)
    dense = pdf_fixture_copy(tmp_path / "dense.pdf", pages=20, dense=True)
    regular = pdf_fixture_copy(tmp_path / "regular.pdf", pages=12)
    geometry = pdf_fixture_copy(tmp_path / "geometry.pdf", geometry=True, pages=8)
    large = tmp_path / "large.pdf"
    shutil.copyfile(m16_pdf_files / "large.pdf", large)
    paths = [free_text, callout, dense, large, regular, geometry]
    window = MainWindow()
    window.resize(900, 700)
    window.show()
    try:
        sessions = _open(window, paths)
        text_session, callout_session, search_session, regional_session, regular_session, geometry_session = sessions
        text_view = window._widgets[text_session.document_id]
        callout_view = window._widgets[callout_session.document_id]
        search_view = window._widgets[search_session.document_id]
        regional_view = window._widgets[regional_session.document_id]
        assert len({session.worker.pid for session in sessions}) == 6
        assert _assert_accounting(window)["global"]["workers_started"] == 6  # type: ignore[index]

        wait_until(lambda: text_session.inventory_complete and callout_session.inventory_complete)
        text_row = find_native(helper("inspect", free_text), "plain")
        callout_row = find_native(helper("inspect", callout), "callout")
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
        assert not search_view.search_layer.active
        assert not search_session.ui.search.complete
        window.manager.activate(search_session.document_id)
        wait_until(lambda: search_view.scheduler.settled)
        wait_until(lambda: search_session.ui.search.complete)

        assert all("Prima" in match.preview for match in search_session.ui.search.results)
        assert not regional_session.ui.search.query
        assert text_session.history_length == callout_session.history_length == 1
        assert text_command.logical_id in text_session.annotation_inventory
        assert callout_command.logical_id in callout_session.annotation_inventory
        assert regular_session.document_revision == geometry_session.document_revision == 0
        state = _assert_accounting(window)
        documents = _documents(state)
        assert documents[regional_session.document_id]["viewer"]["regional_requested"] > 0  # type: ignore[index]
        assert documents[search_session.document_id]["viewer"]["search_adopted"] > 0  # type: ignore[index]

        window.manager.activate(text_session.document_id)
        appearance_before = text_session.worker.submitted_counts[Operation.ANNOTATION_APPEARANCE]
        wait_until(lambda: text_view.scheduler.settled)
        wait_until(lambda: text_session.worker.submitted_counts[Operation.ANNOTATION_APPEARANCE] > appearance_before)
        wait_until(lambda: _documents(_assert_accounting(window))[text_session.document_id]
                   ["raster_cache"]["annotation_appearance_entries"] > 0)
        window.manager.activate(callout_session.document_id)
        callout_appearance_before = callout_session.worker.submitted_counts[Operation.ANNOTATION_APPEARANCE]
        wait_until(lambda: callout_view.scheduler.settled)
        wait_until(lambda: callout_session.worker.submitted_counts[Operation.ANNOTATION_APPEARANCE]
                   > callout_appearance_before)
        wait_until(lambda: _documents(_assert_accounting(window))[callout_session.document_id]
                   ["raster_cache"]["annotation_appearance_entries"] > 0)
        inactive = _documents(_assert_accounting(window))[text_session.document_id]
        assert inactive["viewer"]["active"] is False  # type: ignore[index]
        assert inactive["raster_cache"]["entries"] == 0  # type: ignore[index]

        window.manager.activate(text_session.document_id)
        assert window.manager.undo_active().request_id is not None
        wait_until(lambda: text_session.pending_mutations == 0 and text_session.history_position == 0)
        assert callout_session.history_position == 1
        assert window.manager.redo_active().request_id is not None
        wait_until(lambda: text_session.pending_mutations == 0 and text_session.history_position == 1)
        saved_path = tmp_path / "interleaved-save-as.pdf"
        save(window.manager, text_session, saved_path)
        assert text_session.canonical_path == str(saved_path.resolve())
        assert text_session.document_generation == 2 and not text_session.dirty
        assert callout_session.document_generation == 1 and callout_session.dirty
        assert _assert_accounting(window)["global"]["session_count"] == 6  # type: ignore[index]
    finally:
        close_window_discard(window)


def test_close_during_pending_regional_and_annotation_appearance_work(
        tmp_path: Path, app, m16_pdf_files, pdf_fixture_copy, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", _slow_candidate_worker)
    native = tmp_path / "native.pdf"
    _native_fixture(native)
    external = tmp_path / "external.pdf"
    large = tmp_path / "large.pdf"
    survivor = pdf_fixture_copy(tmp_path / "survivor.pdf", pages=10)
    shutil.copyfile(native, external)
    shutil.copyfile(m16_pdf_files / "large.pdf", large)
    window = MainWindow()
    window.resize(900, 700)
    window.show()
    try:
        external_session, large_session, survivor_session = _open(window, [external, large, survivor])
        large_view = window._widgets[large_session.document_id]
        window.manager.activate(large_session.document_id)
        large_view.set_zoom(8.0)
        wait_until(lambda: large_view.scheduler.pending is not None
                   and large_view.scheduler.pending.key.region is not None)
        large_worker = large_session.worker
        assert window.manager.request_close(large_session.document_id)
        wait_until(lambda: large_session.document_id not in window.manager.sessions)
        assert large_worker.stopped.is_set()
        after_regional_close = _assert_accounting(window)
        assert large_session.document_id not in _documents(after_regional_close)

        external_view = window._widgets[external_session.document_id]
        wait_until(lambda: external_session.inventory_complete)
        window.manager.activate(external_session.document_id)
        wait_until(lambda: external_view.scheduler.settled)
        wait_until(lambda: external_view._appearance_pending is not None)
        external_worker = external_session.worker
        assert window.manager.request_close(external_session.document_id)
        wait_until(lambda: external_session.document_id not in window.manager.sessions)
        assert external_worker.stopped.is_set()
        wait_until(lambda: window._widgets[survivor_session.document_id].scheduler.settled)
        wait_until(lambda: window.manager.budget.count == 0)
        final = _assert_accounting(window)
        documents = _documents(final)
        assert set(documents) == {survivor_session.document_id}
        assert final["global"]["in_flight_raster"]["count"] == 0  # type: ignore[index]
        assert documents[survivor_session.document_id]["worker"]["stopped"] is False  # type: ignore[index]
    finally:
        close_window_discard(window)


def test_worker_failure_isolated_and_failed_document_resources_retire(
        tmp_path: Path, app, pdf_fixture_copy):
    paths = [pdf_fixture_copy(tmp_path / f"failure-{index}.pdf", pages=10, dense=True)
             for index in range(3)]
    window = MainWindow()
    window.resize(900, 700)
    window.show()
    try:
        first, failed, survivor = _open(window, paths)
        window.manager.activate(failed.document_id)
        wait_until(lambda: window._widgets[failed.document_id].scheduler.pending is not None)
        failed_worker = failed.worker
        survivor_worker = survivor.worker
        os.kill(failed_worker.pid, signal.SIGTERM)
        wait_until(lambda: failed.lifecycle == Lifecycle.FAILED)
        state = _assert_accounting(window)
        failed_state = _documents(state)[failed.document_id]
        assert failed_state["worker"]["failure_code"] is not None  # type: ignore[index]
        assert survivor.lifecycle == Lifecycle.READY and not survivor_worker.stopped.is_set()
        window.manager.activate(survivor.document_id)
        wait_until(lambda: window._widgets[survivor.document_id].scheduler.settled)
        assert window.manager.request_close(failed.document_id, discard_changes=True)
        wait_until(lambda: failed.document_id not in window.manager.sessions)
        after = _assert_accounting(window)
        documents = _documents(after)
        assert set(documents) == {first.document_id, survivor.document_id}
        assert all(item["worker"]["stopped"] is False for item in documents.values())  # type: ignore[index]
        assert after["global"]["in_flight_raster"]["count"] == 0  # type: ignore[index]
    finally:
        close_window_discard(window)
