"""M16-A: prove the read-only diagnostics harness reflects existing state."""

import json
from pathlib import Path

from pdf_annotator.app.document_session import Lifecycle
from pdf_annotator.app.main_window import MainWindow
from pdf_annotator.worker import document_service

from .conftest import close_window_discard, wait_until
from .m16_diagnostics import snapshot
from .m2_helpers import slow_render_worker


def _open(window: MainWindow, paths: list[Path]) -> list[object]:
    window.open_paths([str(path) for path in paths])
    canonical = {str(path.resolve()) for path in paths}
    wait_until(lambda: {session.canonical_path for session in window.manager.sessions.values()
                        if session.lifecycle == Lifecycle.READY} >= canonical)
    return [next(session for session in window.manager.sessions.values()
                 if session.canonical_path == str(path.resolve())) for path in paths]


def _by_id(state: dict[str, object]) -> dict[str, dict[str, object]]:
    return {item["document_id"]: item for item in state["documents"]}  # type: ignore[index]


def test_snapshot_reports_simultaneous_documents_and_active_inactive_state(
        tmp_path: Path, app, pdf_fixture_copy):
    paths = [pdf_fixture_copy(tmp_path / f"document-{index}.pdf", pages=8)
             for index in range(3)]
    window = MainWindow()
    window.resize(900, 700)
    window.show()
    try:
        sessions = _open(window, paths)
        window.manager.activate(sessions[1].document_id)
        state = snapshot(window.manager, window._widgets)
        documents = _by_id(state)

        assert json.loads(json.dumps(state)) == state
        assert state["global"]["session_count"] == 3  # type: ignore[index]
        assert state["global"]["active_document_id"] == sessions[1].document_id  # type: ignore[index]
        assert set(documents) == {session.document_id for session in sessions}
        assert [documents[session.document_id]["active"] for session in sessions] == [False, True, False]
        assert documents[sessions[1].document_id]["viewer"]["active"]  # type: ignore[index]
        assert not documents[sessions[0].document_id]["viewer"]["active"]  # type: ignore[index]
        assert all(documents[session.document_id]["generation"] == 1 for session in sessions)
    finally:
        close_window_discard(window)


def test_snapshot_reconciles_global_raster_and_in_flight_accounting(
        tmp_path: Path, app, pdf_fixture_copy, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", slow_render_worker)
    paths = [pdf_fixture_copy(tmp_path / f"render-{index}.pdf", pages=16)
             for index in range(2)]
    window = MainWindow()
    window.resize(900, 700)
    window.show()
    try:
        sessions = _open(window, paths)
        wait_until(lambda: window.manager.budget.count > 0)
        state = snapshot(window.manager, window._widgets)
        global_state = state["global"]  # type: ignore[index]
        documents = _by_id(state)

        assert global_state["in_flight_raster"]["bytes"] == window.manager.budget.bytes  # type: ignore[index]
        assert global_state["in_flight_raster"]["count"] == window.manager.budget.count  # type: ignore[index]
        assert global_state["in_flight_raster"]["document_reserved_count"] == window.manager.budget.count  # type: ignore[index]
        assert sum(item["raster_cache"]["bytes"] for item in documents.values()) == (  # type: ignore[index]
            global_state["raster_cache"]["bytes"])
        assert sum(item["raster_cache"]["entries"] for item in documents.values()) == (  # type: ignore[index]
            global_state["raster_cache"]["entries"])
        assert all(item["worker"] is not None for item in documents.values())
        assert all(item["worker"]["pending_requests"] >= item["worker"]["reserved_rasters"]  # type: ignore[index]
                   for item in documents.values())
        assert {item["document_id"] for item in state["documents"]} == {  # type: ignore[index]
            session.document_id for session in sessions}
    finally:
        close_window_discard(window)


def test_snapshot_removes_closed_document_resources_without_mutating_state(
        tmp_path: Path, app, pdf_fixture_copy):
    paths = [pdf_fixture_copy(tmp_path / f"close-{index}.pdf", pages=8)
             for index in range(2)]
    window = MainWindow()
    window.resize(900, 700)
    window.show()
    try:
        first, second = _open(window, paths)
        wait_until(lambda: window._widgets[second.document_id].scheduler.settled)
        before = snapshot(window.manager, window._widgets)
        assert before == snapshot(window.manager, window._widgets)
        assert window.manager.request_close(second.document_id)
        wait_until(lambda: second.document_id not in window.manager.sessions)

        after = snapshot(window.manager, window._widgets)
        documents = _by_id(after)
        assert second.document_id not in documents
        assert second.document_id not in window._widgets
        assert after["global"]["session_count"] == 1  # type: ignore[index]
        assert set(documents) == {first.document_id}
        assert documents[first.document_id]["worker"]["stopped"] is False  # type: ignore[index]
        assert documents[first.document_id]["raster_cache"]["bytes"] == (  # type: ignore[index]
            after["global"]["raster_cache"]["bytes"])
        assert documents[first.document_id]["text_cache"]["bytes"] == (  # type: ignore[index]
            after["global"]["text_cache"]["bytes"])
    finally:
        close_window_discard(window)
