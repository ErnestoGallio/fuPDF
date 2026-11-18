"""Focused M17A Ctrl+left-drag viewport pan regressions."""

from pathlib import Path

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

from pdf_annotator.app.main_window import MainWindow
from tests.conftest import close_window_discard, wait_until
from tests.test_m7_viewer import _char_point, _draw, _open, _point


def _ctrl_drag(view, start: QPoint, end: QPoint) -> None:
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.ControlModifier, start)
    assert view.viewport().cursor().shape() == Qt.CursorShape.OpenHandCursor
    QTest.mouseMove(view.viewport(), end, delay=2)
    assert view._pan_active
    assert view.viewport().cursor().shape() == Qt.CursorShape.ClosedHandCursor
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton,
                      Qt.KeyboardModifier.ControlModifier, end)


def test_ctrl_left_drag_pans_without_changing_pdf_interaction(tmp_path: Path, app, pdf_fixture_copy):
    source = tmp_path / "pan.pdf"; pdf_fixture_copy(source)
    window = MainWindow(); window.resize(900, 700); window.show()
    try:
        session, view = _open(window, source)
        view.set_zoom(2.0)
        view.verticalScrollBar().setValue(120)
        vertical_before = view.verticalScrollBar().value()
        _ctrl_drag(view, QPoint(400, 360), QPoint(400, 220))
        assert view.verticalScrollBar().value() > vertical_before
        assert view.viewport().cursor().shape() != Qt.CursorShape.ClosedHandCursor

        view.horizontalScrollBar().setValue(160)
        horizontal_before = view.horizontalScrollBar().value()
        _ctrl_drag(view, QPoint(420, 300), QPoint(260, 300))
        assert view.horizontalScrollBar().value() > horizontal_before
        assert session.document_revision == 0 and not session.annotations

        # A Ctrl click on selectable text is deliberately inert: no caret,
        # range, annotation or history action is created before the threshold.
        view.verticalScrollBar().setValue(0); view.horizontalScrollBar().setValue(0)
        view.text_layer.clear()
        char = _char_point(view, 0, 0)
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.ControlModifier, char)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton,
                          Qt.KeyboardModifier.ControlModifier, char)
        assert not view.text_layer.has_target and view._text_editor is None
        assert session.document_revision == 0

        view.set_pen_enabled(True)
        _ctrl_drag(view, QPoint(400, 360), QPoint(400, 240))
        assert view.pen_enabled and session.document_revision == 0
        view.verticalScrollBar().setValue(0); view.horizontalScrollBar().setValue(0)
        _draw(view)
        wait_until(lambda: session.document_revision == 1)
        view.set_pen_enabled(False)
        revision = session.document_revision
        _ctrl_drag(view, _point(view, 0, (55, 65)), _point(view, 0, (150, 65)))
        assert session.document_revision == revision

        view.set_text_box_enabled(True)
        _ctrl_drag(view, QPoint(400, 360), QPoint(400, 240))
        assert view.text_box_enabled and view._text_box_drag is None
        view.set_callout_enabled(True)
        _ctrl_drag(view, QPoint(400, 360), QPoint(400, 240))
        assert view.callout_enabled and view._callout_anchor is None and view._callout_drag is None
    finally:
        close_window_discard(window)
