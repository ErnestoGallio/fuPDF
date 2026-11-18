import os
from pathlib import Path
import signal

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
import pytest

from pdf_annotator.app.document_session import Lifecycle
from pdf_annotator.app.main_window import MainWindow
from pdf_annotator.domain.geometry import PdfPoint
from pdf_annotator.domain.kinds import AnnotationKind
from pdf_annotator.viewer.layout_engine import LayoutMode
from pdf_annotator.worker import document_service
from pdf_annotator.worker.protocol import Operation
from .conftest import close_window_discard, wait_until
from .m7_helpers import slow_ink_worker


@pytest.fixture
def m7_window(tmp_path: Path, app, pdf_fixture_copy):
    first, second = tmp_path / "pen-a.pdf", tmp_path / "pen-b.pdf"
    pdf_fixture_copy(first)
    pdf_fixture_copy(second)
    window = MainWindow()
    window.resize(1000, 760)
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
    return session, view


def _point(view, page_index, canonical) -> QPoint:
    value = view.coordinate_mapper(page_index).canonical_to_viewport(canonical)
    return QPoint(round(value[0]), round(value[1]))


def _draw(view, page_index=0, points=((30, 35), (55, 65), (90, 42))):
    mapped = [_point(view, page_index, point) for point in points]
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=mapped[0])
    for point in mapped[1:]:
        QTest.mouseMove(view.viewport(), point, delay=2)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=mapped[-1])


def _move_ink(view, page_index=0, start=(55, 65), end=(85, 85)):
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton,
                     pos=_point(view, page_index, start))
    QTest.mouseMove(view.viewport(), _point(view, page_index, end), delay=2)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton,
                      pos=_point(view, page_index, end))


def _char_point(view, page_index, char_index):
    wait_until(lambda: bool(view.text_layer.loaded_chunks(page_index)))
    chars = tuple(char for chunk in view.text_layer.loaded_chunks(page_index)
                  for char in chunk.characters)
    quad = chars[char_index].quad
    center = (sum(point.x for point in quad.points) / 4,
              sum(point.y for point in quad.points) / 4)
    return _point(view, page_index, center)


def _select(view, start=0, end=8, page_index=0):
    first, last = (_char_point(view, page_index, index) for index in (start, end))
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=first)
    QTest.mouseMove(view.viewport(), last, delay=3)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=last)
    wait_until(lambda: view.text_layer.confirmed is not None)


def test_explicit_pen_mode_uses_no_move_ipc_and_preserves_m6_selection(m7_window):
    window, path, _ = m7_window
    session, view = _open(window, path)
    _select(view)
    selection = view.text_layer.confirmed
    renders = session.worker.submitted_counts[Operation.RENDER]
    annotation_requests = session.worker.submitted_counts[Operation.ANNOTATION_COMMAND]
    window.pen_action.trigger()
    assert view.pen_enabled and window.context_pen_color_button.isVisible()
    _draw(view)
    assert session.worker.submitted_counts[Operation.ANNOTATION_COMMAND] == annotation_requests + 1
    wait_until(lambda: session.document_revision == 1)
    assert session.worker.submitted_counts[Operation.RENDER] == renders
    assert view.text_layer.confirmed == selection
    assert view.pen_enabled  # Pen deliberately remains the persistent tool.
    assert not view.pending_inks
    model = next(iter(session.annotations.values()))
    assert model.kind == AnnotationKind.INK
    assert view.last_pen_metrics.raw_events >= 3
    assert 2 <= view.last_pen_metrics.final_points <= view.last_pen_metrics.raw_events

    window.pen_action.trigger()
    assert not view.pen_enabled
    _select(view, 10, 15)
    assert view.text_layer.confirmed != selection


def test_color_pending_confirmed_overlay_and_fixed_pdf_width(m7_window):
    window, path, _ = m7_window
    session, view = _open(window, path)
    view.set_pen_enabled(True)
    view.set_pen_color("red")
    _draw(view)
    assert view.pending_inks or session.document_revision == 1
    assert not view.grab().isNull()
    wait_until(lambda: session.document_revision == 1)
    model = next(iter(session.annotations.values()))
    assert model.style == view.pen_style
    assert model.style.width == 1.5
    assert model.style.color.red == pytest.approx(.86)
    assert not view.pending_inks and not view.grab().isNull()


def test_new_pen_preset_starts_red_and_persists_after_strokes(m7_window):
    window, path, _ = m7_window
    session, view = _open(window, path)
    assert session.ui.pen_color == "red" and view.pen_style.color.red == pytest.approx(.86)
    view.set_pen_enabled(True)
    _draw(view)
    wait_until(lambda: session.document_revision == 1)
    assert view.pen_enabled and session.ui.pen_color == "red"
    view.set_pen_color("purple")
    _draw(view, points=((120, 40), (145, 65), (175, 45)))
    wait_until(lambda: session.document_revision == 2)
    assert view.pen_enabled and session.ui.pen_color == "purple"


def test_app_created_ink_can_be_selected_and_deleted_through_service(m7_window):
    window, path, _ = m7_window
    session, view = _open(window, path)
    view.set_pen_enabled(True)
    _draw(view)
    wait_until(lambda: session.document_revision == 1)
    annotation_id = next(iter(session.annotations))

    view.set_pen_enabled(False)
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                     pos=_point(view, 0, (55, 65)))
    assert view.selected_ink_id == annotation_id
    QTest.keyClick(view, Qt.Key.Key_Delete)
    wait_until(lambda: session.document_revision == 2)
    assert not session.annotations
    assert view.selected_ink_id is None and view.pending_delete_id is None


@pytest.mark.parametrize("zoom", [1.0, 1.5, 4.0, 8.0])
def test_app_ink_drag_preview_uses_one_release_mutation_at_all_required_zooms(
        m7_window, zoom):
    window, path, _ = m7_window
    session, view = _open(window, path)
    view.set_pen_enabled(True)
    _draw(view)
    wait_until(lambda: session.document_revision == 1)
    original = next(iter(session.annotations.values())).target
    view.set_pen_enabled(False)
    view.set_zoom(zoom)
    view.horizontalScrollBar().setValue(0)
    view.verticalScrollBar().setValue(0)
    request_count = session.worker.submitted_counts[Operation.ANNOTATION_COMMAND]
    start, end = (55, 65), (85, 85)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=_point(view, 0, start))
    QTest.mouseMove(view.viewport(), _point(view, 0, end), delay=2)
    assert session.worker.submitted_counts[Operation.ANNOTATION_COMMAND] == request_count
    assert view._ink_move is not None
    preview_point = view._ink_move.preview_target().paths[0][1]
    assert (preview_point.x, preview_point.y) == pytest.approx(end, abs=1.1 / zoom)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=_point(view, 0, end))
    wait_until(lambda: session.document_revision == 2)
    moved = next(iter(session.annotations.values()))
    delta = (moved.target.paths[0][1].x - original.paths[0][1].x,
             moved.target.paths[0][1].y - original.paths[0][1].y)
    assert delta == pytest.approx((30, 20), abs=1.1 / zoom)
    for point, before in zip(moved.target.paths[0], original.paths[0], strict=True):
        assert (point.x - before.x, point.y - before.y) == pytest.approx(delta)
    assert session.worker.submitted_counts[Operation.ANNOTATION_COMMAND] == request_count + 1
    assert view.selected_ink_id == moved.annotation_id.value and not view.pending_ink_moves


@pytest.mark.parametrize("layout", [LayoutMode.SINGLE, LayoutMode.DOUBLE,
                                    LayoutMode.DOUBLE_COVER])
def test_app_ink_drag_stays_in_original_page_and_clamps_whole_path_at_edge(m7_window, layout):
    window, path, _ = m7_window
    session, view = _open(window, path)
    view.set_zoom(.6)
    view.set_layout(layout)
    view.set_pen_enabled(True)
    _draw(view, 0, ((5, 8), (20, 25), (35, 12)))
    wait_until(lambda: session.document_revision == 1)
    view.set_pen_enabled(False)
    start = _point(view, 0, (20, 25))
    rect = view.page_viewport_rect(0)
    outside = QPoint(round(rect.left() - 80), round(rect.top() - 80))
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view.viewport(), outside, delay=2)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=outside)
    wait_until(lambda: session.document_revision == 2)
    model = next(iter(session.annotations.values()))
    assert model.page_index == 0
    assert min(point.x for point in model.target.paths[0]) == pytest.approx(0)
    assert min(point.y for point in model.target.paths[0]) == pytest.approx(0)


def test_app_ink_drag_on_rotated_cropbox_pages_and_double_right_page(tmp_path, app,
                                                                       pdf_fixture_copy):
    path = tmp_path / "m71-drag-rotated-cropbox.pdf"
    pdf_fixture_copy(path, geometry=True)
    window = MainWindow()
    window.resize(1000, 760)
    window.show()
    try:
        session, view = _open(window, path)
        view.set_zoom(.6)
        view.set_layout(LayoutMode.DOUBLE)
        source = ((25, 28), (48, 62), (82, 34))
        for page_index in range(4):
            page = view.page_layout.pages[page_index]
            view.verticalScrollBar().setValue(round(page.y))
            wait_until(lambda: view.scheduler.settled)
            view.set_pen_enabled(True)
            _draw(view, page_index, source)
            wait_until(lambda: session.document_revision == page_index * 2 + 1)
            original = list(session.annotations.values())[-1]
            view.set_pen_enabled(False)
            _move_ink(view, page_index, (48, 62), (68, 72))
            wait_until(lambda: session.document_revision == page_index * 2 + 2)
            moved = session.annotations[original.annotation_id.value]
            assert moved.page_index == page_index
            for point, before in zip(moved.target.paths[0], original.target.paths[0], strict=True):
                assert (point.x - before.x, point.y - before.y) == pytest.approx((20, 10), abs=1e-6)
            assert moved.annotation_id == original.annotation_id
        # Page 2 (index 1) is the right-hand page in the current double spread.
        assert view.page_layout.pages[1].x > view.page_layout.pages[0].x
    finally:
        close_window_discard(window)


def test_click_escape_and_tab_change_cancel_without_revision(m7_window):
    window, first_path, second_path = m7_window
    first, view = _open(window, first_path)
    view.set_pen_enabled(True)
    point = _point(view, 0, (30, 30))
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert first.document_revision == 0 and not view.pending_inks

    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseMove(view.viewport(), _point(view, 0, (60, 60)))
    QTest.keyClick(view, Qt.Key.Key_Escape)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton,
                       pos=_point(view, 0, (60, 60)))
    assert first.document_revision == 0 and not view.pen_gesture.active

    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseMove(view.viewport(), _point(view, 0, (65, 65)))
    second, _ = _open(window, second_path)
    assert not view.pen_gesture.active
    assert first.document_revision == second.document_revision == 0


def test_ink_move_escape_and_tab_change_restore_original_without_mutation(m7_window):
    window, first_path, second_path = m7_window
    first, view = _open(window, first_path)
    view.set_pen_enabled(True)
    _draw(view)
    wait_until(lambda: first.document_revision == 1)
    original = next(iter(first.annotations.values())).target
    view.set_pen_enabled(False)

    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=_point(view, 0, (55, 65)))
    QTest.mouseMove(view.viewport(), _point(view, 0, (85, 85)), delay=2)
    assert view._ink_move is not None and view._ink_move.dragging
    QTest.keyClick(view, Qt.Key.Key_Escape)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=_point(view, 0, (85, 85)))
    assert first.document_revision == 1 and next(iter(first.annotations.values())).target == original

    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=_point(view, 0, (55, 65)))
    QTest.mouseMove(view.viewport(), _point(view, 0, (85, 85)), delay=2)
    second, _second_view = _open(window, second_path)
    assert view._ink_move is None and first.document_revision == 1
    assert second.document_revision == 0 and not second.annotations


@pytest.mark.parametrize("zoom", [.5, 1.0, 1.5, 4.0, 8.0])
def test_pen_command_geometry_is_zoom_invariant(m7_window, zoom):
    window, path, _ = m7_window
    session, view = _open(window, path)
    view.set_zoom(zoom)
    view.set_pen_enabled(True)
    expected = ((25, 28), (42, 51), (64, 35))
    _draw(view, points=expected)
    wait_until(lambda: session.document_revision == 1)
    actual = next(iter(session.annotations.values())).target.paths[0]
    assert [value for point in actual for value in (point.x, point.y)] == pytest.approx(
        [value for point in expected for value in point], abs=1.1 / zoom)


@pytest.mark.parametrize("layout", [LayoutMode.SINGLE, LayoutMode.DOUBLE,
                                    LayoutMode.DOUBLE_COVER])
def test_pen_works_in_every_layout(m7_window, layout):
    window, path, _ = m7_window
    session, view = _open(window, path)
    view.set_zoom(.7)
    view.set_layout(layout)
    view.set_pen_enabled(True)
    _draw(view)
    wait_until(lambda: session.document_revision == 1)
    assert next(iter(session.annotations.values())).page_index == 0


def test_cross_page_drag_remains_owned_and_clipped_to_start_page(m7_window):
    window, path, _ = m7_window
    session, view = _open(window, path)
    view.set_zoom(.6)
    view.set_layout(LayoutMode.DOUBLE)
    view.set_pen_enabled(True)
    start = _point(view, 0, (50, 50))
    outside = _point(view, 1, (50, 50))
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view.viewport(), outside, delay=3)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=outside)
    wait_until(lambda: session.document_revision == 1)
    model = next(iter(session.annotations.values()))
    assert model.page_index == 0
    bounds = session.pages[0]
    assert all(0 <= point.x <= bounds.width_pt and 0 <= point.y <= bounds.height_pt
               for point in model.target.paths[0])


def test_rotated_cropbox_preview_and_confirmed_overlay_use_canonical_geometry(
        tmp_path, app, pdf_fixture_copy):
    path = tmp_path / "m7-viewer-rotated-cropbox.pdf"
    pdf_fixture_copy(path, geometry=True)
    window = MainWindow()
    window.resize(1000, 760)
    window.show()
    try:
        session, view = _open(window, path)
        assert [page.rotation for page in session.pages] == [0, 90, 180, 270]
        assert any(page.cropbox != page.mediabox for page in session.pages)
        view.set_zoom(.7)
        view.set_pen_enabled(True)
        expected = ((25, 28), (48, 62), (82, 34))
        for page_index in range(4):
            page = view.page_layout.pages[page_index]
            view.horizontalScrollBar().setValue(0)
            view.verticalScrollBar().setValue(round(page.y))
            wait_until(lambda: view.scheduler.settled)
            _draw(view, page_index, expected)
            wait_until(lambda: session.document_revision == page_index + 1)
            model = list(session.annotations.values())[-1]
            assert model.page_index == page_index
            actual = model.target.paths[0]
            assert [value for point in actual for value in (point.x, point.y)] == pytest.approx(
                [value for point in expected for value in point], abs=1.6 / .7)
            assert not view.grab().isNull()
    finally:
        close_window_discard(window)


def test_worker_failure_removes_pending_without_ghost_or_revision(
        m7_window, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", slow_ink_worker)
    window, path, _ = m7_window
    session, view = _open(window, path)
    view.set_pen_enabled(True)
    _draw(view)
    wait_until(lambda: bool(view.pending_inks) and session.pending_mutations == 1)
    os.kill(session.worker.pid, signal.SIGTERM)
    wait_until(lambda: session.lifecycle == Lifecycle.FAILED)
    assert not view.pending_inks
    assert session.document_revision == 0 and session.pending_mutations == 0


def test_worker_failure_during_ink_move_restores_confirmed_geometry(m7_window, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", slow_ink_worker)
    window, path, _ = m7_window
    session, view = _open(window, path)
    view.set_pen_enabled(True)
    _draw(view)
    wait_until(lambda: session.document_revision == 1)
    original = next(iter(session.annotations.values())).target
    view.set_pen_enabled(False)
    _move_ink(view)
    wait_until(lambda: bool(view.pending_ink_moves) and session.pending_mutations == 1)
    os.kill(session.worker.pid, signal.SIGTERM)
    wait_until(lambda: session.lifecycle == Lifecycle.FAILED)
    assert not view.pending_ink_moves
    assert session.document_revision == 1 and next(iter(session.annotations.values())).target == original


def test_discard_close_with_pending_ink_preserves_source_bytes(
        m7_window, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", slow_ink_worker)
    window, path, _ = m7_window
    original = path.read_bytes()
    session, view = _open(window, path)
    view.set_pen_enabled(True)
    _draw(view)
    wait_until(lambda: session.pending_mutations == 1)
    worker = session.worker
    assert window.manager.request_close(session.document_id, discard_changes=True)
    wait_until(lambda: session.document_id not in window.manager.sessions)
    assert worker.stopped.is_set() and path.read_bytes() == original


def test_pen_mode_color_revision_and_overlays_are_document_isolated(m7_window):
    window, first_path, second_path = m7_window
    first, first_view = _open(window, first_path)
    first_view.set_pen_enabled(True)
    first_view.set_pen_color("purple")
    _draw(first_view)
    wait_until(lambda: first.document_revision == 1)
    second, second_view = _open(window, second_path)
    assert not second_view.pen_enabled and second_view.session.ui.pen_color == "red"
    assert second.document_revision == 0 and not second.annotations
    window.manager.activate(first.document_id)
    assert first_view.pen_enabled and first_view.session.ui.pen_color == "purple"
    assert len(first.annotations) == 1 and not set(first.annotations) & set(second.annotations)
