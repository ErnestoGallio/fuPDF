from pathlib import Path
import os
import signal
import subprocess
import sys

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
import pytest

from pdf_annotator.app.document_session import Lifecycle
from pdf_annotator.app.main_window import MainWindow
from pdf_annotator.domain.annotation_commands import CommandContext
from pdf_annotator.domain.errors import MutationError, MutationErrorCode
from pdf_annotator.domain.results import MutationOutcome, MutationResult
from pdf_annotator.viewer.layout_engine import LayoutMode
from pdf_annotator.worker.protocol import Operation
from pdf_annotator.worker import document_service
from .conftest import close_window_discard, request, wait_until
from .m6_helpers import failing_text_worker, slow_text_worker


@pytest.fixture
def m6_window(tmp_path: Path, app, pdf_fixture_copy):
    first, second = tmp_path / "viewer-a.pdf", tmp_path / "viewer-b.pdf"
    pdf_fixture_copy(first)
    pdf_fixture_copy(second)
    window = MainWindow()
    window.resize(900, 700)
    window.show()
    yield window, first, second
    close_window_discard(window)


def _open(window: MainWindow, path: Path):
    window.open_paths([str(path)])
    wait_until(lambda: any(session.canonical_path == str(path.resolve())
                           and session.lifecycle == Lifecycle.READY
                           for session in window.manager.sessions.values()))
    session = next(session for session in window.manager.sessions.values()
                   if session.canonical_path == str(path.resolve()))
    view = window._widgets[session.document_id]
    wait_until(lambda: view.scheduler.settled)
    wait_until(lambda: bool(view.text_layer.loaded_chunks(0)))
    return session, view


def _viewport_point(view, page_index, char) -> QPoint:
    quad = char.quad
    x = sum(point.x for point in quad.points) / 4
    y = sum(point.y for point in quad.points) / 4
    mapped = view.coordinate_mapper(page_index).canonical_to_viewport((x, y))
    return QPoint(round(mapped[0]), round(mapped[1]))


def _drag(view, start=0, end=10, *, page_index=0):
    snapshot = view.text_layer.loaded_chunks(page_index)[0]
    begin, finish = (_viewport_point(view, page_index, snapshot.characters[index])
                     for index in (start, end))
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=begin)
    QTest.mouseMove(view.viewport(), finish, delay=5)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=finish)
    wait_until(lambda: view.text_layer.confirmed is not None)


def test_selection_copy_highlight_overlay_and_no_rerender(m6_window, app):
    window, path, _ = m6_window
    session, view = _open(window, path)
    _drag(view, 0, 9)
    selected = view.text_layer.selected_text()
    assert selected == "Prima riga"
    view.setFocus()
    QTest.keyClick(view, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert app.clipboard().text() == selected
    rendered = session.worker.submitted_counts[Operation.RENDER]
    assert window.highlight_action.isEnabled()
    window.highlight_action.trigger()
    wait_until(lambda: session.document_revision == 1)
    assert session.dirty and len(session.annotations) == 1
    assert view.text_layer.has_selection is False
    assert session.worker.submitted_counts[Operation.RENDER] == rendered
    frame = view.grab()
    assert not frame.isNull()


@pytest.mark.parametrize("zoom", [1.0, 1.5, 3.0, 4.0, 8.0])
def test_selection_geometry_survives_zoom_without_changing_pdf_target(m6_window, zoom):
    window, path, _ = m6_window
    _, view = _open(window, path)
    _drag(view, 0, 4)
    target = view.text_layer.confirmed
    view.set_zoom(zoom)
    assert view.text_layer.confirmed == target
    first = target.quads[0].upper_left
    viewport = view.coordinate_mapper(0).canonical_to_viewport((first.x, first.y))
    canonical = view.coordinate_mapper(0).viewport_to_canonical(viewport)
    assert canonical == pytest.approx((first.x, first.y))
    view.setFocus()
    QTest.keyClick(view, Qt.Key.Key_Escape)
    assert not view.text_layer.has_selection


@pytest.mark.parametrize("layout", [LayoutMode.SINGLE, LayoutMode.DOUBLE,
                                    LayoutMode.DOUBLE_COVER])
def test_selection_is_layout_invariant(m6_window, layout):
    window, path, _ = m6_window
    _, view = _open(window, path)
    view.set_layout(layout)
    _drag(view, 0, 4)
    assert view.text_layer.selected_text() == "Prima"


def test_double_layout_routes_hit_testing_to_right_page(m6_window):
    window, path, _ = m6_window
    _, view = _open(window, path)
    view.set_zoom(.6)
    view.set_layout(LayoutMode.DOUBLE)
    wait_until(lambda: view.scheduler.settled)
    wait_until(lambda: bool(view.text_layer.loaded_chunks(1)))
    _drag(view, 0, 6, page_index=1)
    assert view.text_layer.confirmed.page_index == 1
    assert view.text_layer.selected_text() == "Colonna"

    view.text_layer.clear()
    first_snapshot = view.text_layer.loaded_chunks(0)[0]
    right_snapshot = view.text_layer.loaded_chunks(1)[0]
    start = _viewport_point(view, 0, first_snapshot.characters[0])
    outside = _viewport_point(view, 1, right_snapshot.characters[0])
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view.viewport(), outside, delay=5)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=outside)
    wait_until(lambda: view.text_layer.confirmed is not None)
    assert view.text_layer.confirmed.page_index == 0


def test_tab_selection_cache_and_highlight_state_are_isolated(m6_window):
    window, first_path, second_path = m6_window
    first, first_view = _open(window, first_path)
    _drag(first_view, 0, 5)
    first_selection = first_view.text_layer.confirmed
    assert first_view.text_layer.worker_cache_bytes > 0
    second, second_view = _open(window, second_path)
    wait_until(lambda: window.manager.active_id == second.document_id)
    wait_until(lambda: bool(second_view.text_layer.loaded_chunks(0)))
    assert second_view.text_layer.confirmed is None
    assert all(key[0] != first.document_id for key in window.manager.text_cache.keys)
    wait_until(lambda: first_view.text_layer.worker_cache_bytes == 0)
    _drag(second_view, 6, 10)
    assert second_view.text_layer.confirmed != first_selection
    window.manager.activate(first.document_id)
    assert first_view.text_layer.confirmed == first_selection
    assert first.document_revision == second.document_revision == 0


def test_click_outside_text_clears_selection(m6_window):
    window, path, _ = m6_window
    _, view = _open(window, path)
    _drag(view)
    rect = view.page_viewport_rect(0)
    point = QPoint(round(rect.right() - 10), round(rect.bottom() - 10))
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert not view.text_layer.has_selection


def test_app_highlight_click_select_delete_and_drag_remains_text_selection(m6_window):
    window, path, _ = m6_window
    session, view = _open(window, path)
    _drag(view, 0, 5)
    window.highlight_action.trigger()
    wait_until(lambda: session.document_revision == 1)
    annotation_id = next(iter(session.annotations))
    snapshot = view.text_layer.loaded_chunks(0)[0]
    point = _viewport_point(view, 0, snapshot.characters[2])

    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert view.selected_text_markup_id == annotation_id and not view.text_layer.has_selection
    assert not view.grab().isNull()  # selected overlay paints a minimal quad outline

    # A miss clears the Highlight selection instead of accepting a permissive
    # annotation bounding rectangle.
    page = view.page_viewport_rect(0)
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                     pos=QPoint(round(page.right() - 8), round(page.bottom() - 8)))
    assert view.selected_text_markup_id is None

    # Starting in a Highlight and dragging still performs the M6 text gesture;
    # it never creates a move mutation for text markup.
    finish = _viewport_point(view, 0, snapshot.characters[7])
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseMove(view.viewport(), finish, delay=5)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=finish)
    wait_until(lambda: view.text_layer.confirmed is not None)
    assert view.selected_text_markup_id is None and session.document_revision == 1

    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert view.selected_text_markup_id == annotation_id
    # Failed/stale delete acknowledgements leave the native overlay selected;
    # no phantom revision is created before the real service command below.
    for outcome, code in ((MutationOutcome.FAILED, MutationErrorCode.WORKER_FAILURE),
                          (MutationOutcome.STALE, MutationErrorCode.STALE_GENERATION)):
        command_id = CommandContext.create(session.document_id, session.document_generation).command_id
        view.pending_delete_id = command_id
        view.handle_annotation_result(MutationResult(
            command_id, session.document_id, session.document_generation, outcome,
            session.document_revision, session.document_revision,
            error=MutationError.create(code)))
        assert view.selected_text_markup_id == annotation_id and annotation_id in session.annotations

    view.setFocus(); QTest.keyClick(view, Qt.Key.Key_Delete)
    wait_until(lambda: session.document_revision == 2 and not session.annotations)
    assert view.selected_text_markup_id is None


def test_evicted_worker_snapshot_cannot_leave_selected_but_highlight_disabled(
        m6_window):
    window, path, _ = m6_window
    session, view = _open(window, path)
    snapshot = view.text_layer.loaded_chunks(0)[0]
    assert snapshot is not None
    trimmed = request(session.worker, Operation.TEXT_TRIM)
    assert trimmed.success

    _drag(view, 0, 9)

    assert view.text_layer.selected_text() == "Prima riga"
    assert view.text_layer.confirmed is not None
    assert view.text_layer.visual_selection is view.text_layer.confirmed
    selected_but_disabled = bool(
        view.text_layer.visual_selection
        and view.text_layer.selected_text()
        and view.text_layer.confirmed.quads
        and not window.highlight_action.isEnabled())
    assert not selected_but_disabled
    assert window.highlight_action.isEnabled()


def test_dense_page_cross_chunk_copy_and_highlight(tmp_path, app):
    path = tmp_path / "dense-30000.pdf"
    subprocess.run([sys.executable, "-m", "tests.m6_fixtures", "limit",
                    str(path), "300", "100"], check=True, timeout=30,
                   capture_output=True)
    window = MainWindow()
    window.resize(900, 700)
    window.show()
    try:
        session, view = _open(window, path)
        wait_until(lambda: len(view.text_layer.loaded_chunks(0)) >= 2)
        chunks = sorted(view.text_layer.loaded_chunks(0), key=lambda item: item.chunk_index)
        first, second = chunks[0], chunks[1]
        begin = _viewport_point(view, 0, first.characters[-10])
        finish = _viewport_point(view, 0, second.characters[10])
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=begin)
        QTest.mouseMove(view.viewport(), finish, delay=5)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=finish)
        wait_until(lambda: view.text_layer.confirmed is not None)
        assert "\n" in view.text_layer.selected_text()
        view.setFocus()
        QTest.keyClick(view, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        assert app.clipboard().text() == view.text_layer.selected_text()
        window.highlight_action.trigger()
        wait_until(lambda: session.document_revision == 1)
        assert len(session.annotations) == 1
    finally:
        close_window_discard(window)


def test_close_with_pending_text_request_is_nonblocking(m6_window, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", slow_text_worker)
    window, path, _ = m6_window
    window.open_paths([str(path)])
    wait_until(lambda: bool(window.manager.sessions))
    session = next(iter(window.manager.sessions.values()))
    wait_until(lambda: session.lifecycle == Lifecycle.READY)
    view = window._widgets[session.document_id]
    wait_until(lambda: session.worker.submitted_counts[Operation.TEXT_PAGE_INDEX] > 0
               and session.worker.active_request_id is not None)
    worker = session.worker
    assert window.manager.request_close(session.document_id)
    wait_until(lambda: session.document_id not in window.manager.sessions)
    assert worker.stopped.is_set() and not worker.forced_termination


def test_worker_failure_during_text_request_does_not_block_gui(m6_window, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", slow_text_worker)
    window, path, _ = m6_window
    window.open_paths([str(path)])
    wait_until(lambda: bool(window.manager.sessions))
    session = next(iter(window.manager.sessions.values()))
    wait_until(lambda: session.lifecycle == Lifecycle.READY)
    wait_until(lambda: session.worker.submitted_counts[Operation.TEXT_PAGE_INDEX] > 0
               and session.worker.active_request_id is not None)
    os.kill(session.worker.pid, signal.SIGTERM)
    wait_until(lambda: session.lifecycle == Lifecycle.FAILED)
    assert window.isEnabled() and session.document_revision == 0


def test_nonrecoverable_page_text_failure_is_negative_cached_without_retry(
        m6_window, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", failing_text_worker)
    window, path, _ = m6_window
    errors = []
    window.open_paths([str(path)])
    wait_until(lambda: bool(window.manager.sessions))
    session = next(iter(window.manager.sessions.values()))
    wait_until(lambda: session.lifecycle == Lifecycle.READY)
    view = window._widgets[session.document_id]
    view.text_error.connect(errors.append)
    wait_until(lambda: session.worker.submitted_counts[Operation.TEXT_PAGE_INDEX] == 1
               and bool(errors))
    QTest.qWait(250)
    assert session.worker.submitted_counts[Operation.TEXT_PAGE_INDEX] == 1
    assert len(errors) == 1
