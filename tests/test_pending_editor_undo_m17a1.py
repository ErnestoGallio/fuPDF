"""Regression coverage for empty unconfirmed FreeText/Callout drafts."""

from html import escape
from pathlib import Path

import pytest
from PySide6.QtCore import QMimeData, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox

from pdf_annotator.app.main_window import MainWindow
from tests.conftest import close_window_discard, wait_until
from tests.test_freetext_viewer_m8 import _open, _point


@pytest.fixture
def editor_window(tmp_path: Path, app, pdf_fixture_copy):
    first, second = tmp_path / "pending-a.pdf", tmp_path / "pending-b.pdf"
    pdf_fixture_copy(first)
    pdf_fixture_copy(second)
    window = MainWindow()
    window.resize(1050, 780)
    window.show()
    yield window, first, second
    close_window_discard(window)


def _start_text_box(view):
    view.set_text_box_enabled(True)
    start, end = _point(view, 0, (55, 90)), _point(view, 0, (280, 180))
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view.viewport(), end, delay=1)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    assert view._text_editor is not None
    return view._text_editor


def _start_callout(view):
    view.set_callout_enabled(True)
    anchor, start, end = (_point(view, 0, point)
                          for point in ((55, 80), (220, 120), (400, 220)))
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=anchor)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view.viewport(), end, delay=1)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    assert view._text_editor is not None
    return view._text_editor


def _paste_styled(editor, text: str) -> None:
    mime = QMimeData()
    mime.setText(text)
    mime.setHtml(f'<span style="font-family: Arial">{escape(text)}</span>')
    editor.insertFromMimeData(mime)


@pytest.mark.parametrize("starter, enabled", [(_start_text_box, "text_box_enabled"),
                                                (_start_callout, "callout_enabled")])
def test_empty_pending_editor_ctrl_z_cancels_only_the_draft(editor_window, starter, enabled):
    window, first, _second = editor_window
    session, view = _open(window, first)
    editor = starter(view)
    # Ctrl+Z must not reach the global history action even where a tab has a
    # prior history step. The draft has no command or optimistic mutation.
    QTest.keyClick(editor, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert view._text_editor is None and view._editing_target is None
    assert not view.pending_text_boxes and not session.annotations
    assert session.document_revision == 0 and session.history_position == 0
    assert getattr(view, enabled)
    assert all(item is None for item in (view.selected_text_box_id, view.selected_callout_id,
                                         view.selected_inventory_id))


@pytest.mark.parametrize("starter, enabled", [(_start_text_box, "text_box_enabled"),
                                                (_start_callout, "callout_enabled")])
def test_empty_pending_editor_escape_cancels_and_keeps_tool_active(editor_window, starter, enabled):
    _window, first, _second = editor_window
    session, view = _open(_window, first)
    editor = starter(view)
    QTest.keyClick(editor, Qt.Key.Key_Escape)
    assert view._text_editor is None and view._editing_target is None
    assert not view.pending_text_boxes and not session.annotations
    assert session.document_revision == 0 and getattr(view, enabled)


@pytest.mark.parametrize("starter, enabled", [(_start_text_box, "text_box_enabled"),
                                                (_start_callout, "callout_enabled")])
def test_empty_pending_editor_ctrl_enter_confirms_real_annotation(editor_window, starter, enabled):
    _window, first, _second = editor_window
    session, view = _open(_window, first)
    editor = starter(view)
    QTest.keyClick(editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    wait_until(lambda: session.document_revision == 1)
    assert view._text_editor is None and len(session.annotations) == 1
    assert next(iter(session.annotations.values())).text == ""
    assert not getattr(view, enabled)
    assert not view.pending_text_boxes
    # A successful one-shot creation returns the ordinary click path to neutral.
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=_point(view, 0, (500, 350)))
    assert session.document_revision == 1 and len(session.annotations) == 1


@pytest.mark.parametrize("starter, enabled", [(_start_text_box, "text_box_enabled"),
                                                (_start_callout, "callout_enabled")])
def test_empty_pending_editor_focus_loss_confirms_real_annotation(editor_window, starter, enabled):
    _window, first, _second = editor_window
    session, view = _open(_window, first)
    editor = starter(view)
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                     pos=_point(view, 0, (500, 350)))
    wait_until(lambda: session.document_revision == 1)
    assert view._text_editor is None and len(session.annotations) == 1
    assert next(iter(session.annotations.values())).text == ""
    assert not getattr(view, enabled)
    assert not view.pending_text_boxes
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=_point(view, 0, (600, 430)))
    assert session.document_revision == 1 and len(session.annotations) == 1


def test_nonempty_pending_text_box_undo_stays_local_then_empty_undo_cancels(editor_window):
    _window, first, _second = editor_window
    session, view = _open(_window, first)
    editor = _start_text_box(view)
    QTest.keyClicks(editor, "bozza")
    QTest.keyClick(editor, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert view._text_editor is editor and editor.toPlainText() == ""
    assert session.document_revision == 0 and not session.annotations
    QTest.keyClick(editor, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert view._text_editor is None and view.text_box_enabled


def test_confirmed_draft_uses_existing_document_undo_redo(editor_window):
    window, first, _second = editor_window
    session, view = _open(window, first)
    editor = _start_text_box(view)
    editor.setPlainText("confermato")
    QTest.keyClick(editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    wait_until(lambda: session.document_revision == 1)
    assert len(session.annotations) == 1
    window.undo_action.trigger()
    wait_until(lambda: session.document_revision == 2)
    assert not session.annotations
    window.redo_action.trigger()
    wait_until(lambda: session.document_revision == 3)
    assert len(session.annotations) == 1


@pytest.mark.parametrize("starter", [_start_text_box, _start_callout])
def test_confirmed_empty_draft_uses_existing_document_undo_redo(editor_window, starter):
    window, first, _second = editor_window
    session, view = _open(window, first)
    QTest.keyClick(starter(view), Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    wait_until(lambda: session.document_revision == 1)
    annotation = next(iter(session.annotations.values()))
    assert annotation.text == ""
    window.undo_action.trigger()
    wait_until(lambda: session.document_revision == 2)
    assert not session.annotations
    window.redo_action.trigger()
    wait_until(lambda: session.document_revision == 3)
    assert session.annotations[annotation.annotation_id.value].text == ""


def test_empty_draft_undo_does_not_undo_a_previously_confirmed_annotation(editor_window):
    _window, first, _second = editor_window
    session, view = _open(_window, first)
    editor = _start_text_box(view)
    editor.setPlainText("esistente")
    QTest.keyClick(editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    wait_until(lambda: session.document_revision == 1)
    assert len(session.annotations) == 1 and session.can_undo

    draft = _start_text_box(view)
    QTest.keyClick(draft, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert view._text_editor is None and len(session.annotations) == 1
    assert session.document_revision == 1 and session.can_undo


def test_cancellation_allows_recreation_in_current_and_second_document(editor_window):
    window, first, second = editor_window
    first_session, first_view = _open(window, first)
    QTest.keyClick(_start_text_box(first_view), Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert first_view._text_editor is None
    first_editor = _start_text_box(first_view)
    first_editor.setPlainText("nuovo")
    QTest.keyClick(first_editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    wait_until(lambda: first_session.document_revision == 1)

    second_session, second_view = _open(window, second)
    QTest.keyClick(_start_text_box(second_view), Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert second_view._text_editor is None and second_view.text_box_enabled
    second_editor = _start_text_box(second_view)
    second_editor.setPlainText("secondo")
    QTest.keyClick(second_editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    wait_until(lambda: second_session.document_revision == 1)
    assert len(first_session.annotations) == len(second_session.annotations) == 1


def test_plain_text_paste_survives_text_box_callout_click_zoom_and_undo(editor_window):
    window, first, _second = editor_window
    session, view = _open(window, first)
    text_box_text = "M17 Text Box — caffè & tè <ok>"
    editor = _start_text_box(view)
    assert not editor.acceptRichText()
    _paste_styled(editor, text_box_text)
    assert editor.toPlainText() == text_box_text
    assert view._editor_rich_text().plain == text_box_text

    # TextBox -> Callout confirms exactly once and does not import the hidden
    # clipboard font family into the strict domain model.
    assert view.set_callout_enabled(True)
    wait_until(lambda: session.document_revision == 1)
    assert view._text_editor is None and view.callout_enabled and not view.text_box_enabled
    assert next(iter(session.annotations.values())).text == text_box_text

    callout_text = "M17 Callout — città & <ok>"
    editor = _start_callout(view)
    _paste_styled(editor, callout_text)
    assert editor.toPlainText() == callout_text
    assert view.set_text_box_enabled(True)
    wait_until(lambda: session.document_revision == 2)
    assert view._text_editor is None and view.text_box_enabled and not view.callout_enabled
    assert {model.text for model in session.annotations.values()} == {text_box_text, callout_text}

    # A direct rich-document insertion exercises the defensive extraction
    # fallback independently of the plain-text clipboard policy.
    click_text = "Click — caffè & tè <ok>"
    editor = _start_text_box(view)
    editor.insertHtml(f'<span style="font-family: Arial">{escape(click_text)}</span>')
    assert view._editor_rich_text().plain == click_text
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                     pos=_point(view, 0, (500, 350)))
    wait_until(lambda: session.document_revision == 3)
    assert view._text_editor is None

    editor = _start_callout(view)
    editor.clear()
    assert view._editor_rich_text().plain == ""
    old_zoom = session.ui.zoom
    view.set_zoom(old_zoom * 1.1)
    wait_until(lambda: session.document_revision == 4)
    assert view._text_editor is None and session.ui.zoom == pytest.approx(old_zoom * 1.1)
    assert sum(model.text == "" for model in session.annotations.values()) == 1

    window.undo_action.trigger()
    wait_until(lambda: session.document_revision == 5)
    assert view._text_editor is None and len(session.annotations) == 3


def test_styled_editor_content_does_not_block_tab_or_window_close(
        tmp_path, app, pdf_fixture_copy, monkeypatch):
    first, second = tmp_path / "close-a.pdf", tmp_path / "close-b.pdf"
    pdf_fixture_copy(first)
    pdf_fixture_copy(second)
    window = MainWindow()
    window.resize(1050, 780)
    window.show()
    monkeypatch.setattr(window, "_close_choice",
                        lambda _session: QMessageBox.StandardButton.Discard)
    try:
        first_session, first_view = _open(window, first)
        editor = _start_text_box(first_view)
        editor.insertHtml('<span style="font-family: Arial">tab close</span>')
        window._tab_closed(window.tabs.indexOf(first_view))
        wait_until(lambda: first_session.document_id not in window.manager.sessions)
        assert first_view._text_editor is None

        second_session, second_view = _open(window, second)
        editor = _start_callout(second_view)
        editor.insertHtml('<span style="font-family: Arial">window close</span>')
        window.close()
        wait_until(lambda: window.manager.is_shutdown)
        assert second_view._text_editor is None and not window.isVisible()
    finally:
        if not window.manager.is_shutdown:
            close_window_discard(window)
