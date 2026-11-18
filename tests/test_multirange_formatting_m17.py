"""M17 editor-only disjoint formatting and existing persistence-path regressions."""

from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QMimeData, Qt
from PySide6.QtGui import QFont, QInputMethodEvent, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QTextEdit

from pdf_annotator.app.main_window import MainWindow
from pdf_annotator.domain.geometry import PdfPoint, PdfRect
from pdf_annotator.domain.rich_text import RichText, RichTextRun
from pdf_annotator.domain.styles import RgbColor
from pdf_annotator.domain.targets import RectTarget
from pdf_annotator.viewer.layout_engine import LayoutMode
from pdf_annotator.viewer.pdf_view import _TextBoxEditor
from tests.conftest import close_window_discard, wait_until
from tests.test_freetext_rich_m82 import _open, _style
from tests.test_save_m15 import save


TEXT = "AAA gap BBB gap CCC"
RANGES = ((0, 3), (8, 11), (16, 19))


def _position(editor, position):
    cursor = QTextCursor(editor.document())
    cursor.setPosition(position)
    return editor.cursorRect(cursor).center()


def _drag(editor, start, end, *, ctrl=False):
    modifiers = (Qt.KeyboardModifier.ControlModifier if ctrl
                 else Qt.KeyboardModifier.NoModifier)
    QTest.mousePress(editor.viewport(), Qt.MouseButton.LeftButton, modifiers,
                     _position(editor, start))
    QTest.mouseMove(editor.viewport(), _position(editor, end), delay=1)
    QTest.mouseRelease(editor.viewport(), Qt.MouseButton.LeftButton, modifiers,
                       _position(editor, end))


def _three_ranges(editor):
    _drag(editor, 0, 3)
    _drag(editor, 8, 11, ctrl=True)
    _drag(editor, 19, 16, ctrl=True)  # Reverse drag has the same range semantics.
    assert editor.formatting_ranges == RANGES


def _double_click(editor, position, *, ctrl=False):
    modifiers = (Qt.KeyboardModifier.ControlModifier if ctrl
                 else Qt.KeyboardModifier.NoModifier)
    point = _position(editor, position)
    # QTest.mouseDClick sends only the double-click event. Include the first
    # click and final release to exercise Qt's actual mouse-event sequence.
    QTest.mouseClick(editor.viewport(), Qt.MouseButton.LeftButton, modifiers, point)
    QTest.mouseDClick(editor.viewport(), Qt.MouseButton.LeftButton, modifiers, point)
    QTest.mouseRelease(editor.viewport(), Qt.MouseButton.LeftButton, modifiers, point)


def _three_words(editor):
    _double_click(editor, 1)
    _double_click(editor, 9, ctrl=True)
    _double_click(editor, 17, ctrl=True)
    assert editor.formatting_ranges == _overlays(editor) == RANGES


def _overlays(editor):
    return tuple((item.cursor.selectionStart(), item.cursor.selectionEnd())
                 for item in editor.extraSelections())


@pytest.fixture
def editor(app):
    instance = _TextBoxEditor()
    instance.resize(700, 200)
    font = QFont("Courier")
    font.setPixelSize(20)
    instance.setFont(font)
    instance.setPlainText(TEXT)
    instance.show()
    instance.setFocus()
    yield instance
    instance.blockSignals(True)
    instance.close()
    instance.deleteLater()


def test_ctrl_drag_adds_visible_ranges_and_ignores_empty_click(editor):
    _drag(editor, 0, 3)
    assert editor.formatting_ranges == ((0, 3),)
    _drag(editor, 8, 11, ctrl=True)
    assert editor.formatting_ranges == ((0, 3), (8, 11))
    assert _overlays(editor) == editor.formatting_ranges
    QTest.keyPress(editor, Qt.Key.Key_Control)
    QTest.keyRelease(editor, Qt.Key.Key_Control)
    _drag(editor, 19, 16, ctrl=True)
    assert editor.formatting_ranges == _overlays(editor) == RANGES
    _drag(editor, 6, 6, ctrl=True)
    assert editor.formatting_ranges == _overlays(editor) == RANGES
    assert not editor.textCursor().hasSelection()


def test_overlap_adjacency_and_normal_selection_replace_the_set(editor):
    _three_ranges(editor)
    _drag(editor, 2, 9, ctrl=True)
    assert editor.formatting_ranges == ((0, 11), (16, 19))
    _drag(editor, 11, 16, ctrl=True)
    assert editor.formatting_ranges == ((0, 19),)
    assert _overlays(editor) == ((0, 19),)
    _drag(editor, 5, 7)
    assert editor.formatting_ranges == _overlays(editor) == ((5, 7),)
    _three_ranges(editor)
    QTest.mouseClick(editor.viewport(), Qt.MouseButton.LeftButton, pos=_position(editor, 5))
    assert not editor.formatting_ranges and not editor.extraSelections()


@pytest.mark.parametrize("action, expected", [
    ("typing", "AAA gap BBB gap Z"),
    ("backspace", "AAA gap BBB gap "),
    ("delete", "AAA gap BBB gap "),
    ("paste", "AAA gap BBB gap città & <ok>"),
    ("cut", "AAA gap BBB gap "),
    ("context_cut", "AAA gap BBB gap "),
    ("mime", "AAA gap BBB gap città & <ok>"),
    ("ime", "AAA gap BBB gap città & <ok>"),
])
def test_text_mutations_use_only_the_native_last_range(editor, action, expected):
    _three_ranges(editor)
    if action == "typing":
        QTest.keyClicks(editor, "Z")
    elif action in ("backspace", "delete"):
        QTest.keyClick(editor, Qt.Key.Key_Backspace if action == "backspace" else Qt.Key.Key_Delete)
    elif action == "paste":
        QApplication.clipboard().setText("città & <ok>")
        QTest.keyClick(editor, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    elif action == "cut":
        QTest.keyClick(editor, Qt.Key.Key_X, Qt.KeyboardModifier.ControlModifier)
    elif action == "context_cut":
        menu = editor.createStandardContextMenu()
        cut = next(item for item in menu.actions()
                   if item.text().endswith("\tCtrl+X"))
        cut.trigger()
        menu.deleteLater()
    elif action == "mime":
        mime = QMimeData()
        mime.setText("città & <ok>")
        mime.setHtml("<b>città &amp; &lt;ok&gt;</b>")
        editor.insertFromMimeData(mime)
    else:
        event = QInputMethodEvent()
        event.setCommitString("città & <ok>")
        QApplication.sendEvent(editor, event)
    assert editor.toPlainText() == expected
    assert not editor.formatting_ranges and not editor.extraSelections()


def test_programmatic_text_change_drops_stored_positions(editor):
    _three_ranges(editor)
    cursor = QTextCursor(editor.document())
    cursor.insertText("prefix ")
    assert editor.toPlainText() == "prefix " + TEXT
    # Qt tracks its one native selection through external cursor edits; the
    # earlier disjoint ranges must be gone rather than retain stale positions.
    assert editor.formatting_ranges == ((23, 26),)
    assert _overlays(editor) == ((23, 26),)


def test_ctrl_drag_starting_inside_native_selection_never_drags_text(editor):
    _drag(editor, 0, 11)
    _drag(editor, 2, 7, ctrl=True)
    assert editor.toPlainText() == TEXT
    assert editor.formatting_ranges == ((0, 11),)
    assert (editor.textCursor().selectionStart(), editor.textCursor().selectionEnd()) == (2, 7)


def test_normal_double_click_selects_native_word_and_replaces_old_ranges(editor):
    _three_ranges(editor)
    _double_click(editor, 9)
    assert editor.textCursor().selectedText() == "BBB"
    assert editor.formatting_ranges == _overlays(editor) == ((8, 11),)


def test_ctrl_double_click_adds_second_and_third_words_without_toggling(editor):
    _drag(editor, 0, 3)
    _double_click(editor, 9, ctrl=True)
    assert editor.textCursor().selectedText() == "BBB"
    assert editor.formatting_ranges == _overlays(editor) == ((0, 3), (8, 11))
    _double_click(editor, 17, ctrl=True)
    assert editor.textCursor().selectedText() == "CCC"
    assert editor.formatting_ranges == _overlays(editor) == RANGES
    for position in (9, 17, 1):
        _double_click(editor, position, ctrl=True)
        assert editor.formatting_ranges == _overlays(editor) == RANGES
    assert editor.toPlainText() == TEXT


@pytest.mark.parametrize("initial, expected", [((1, 2), ((0, 3),)),
                                               ((3, 7), ((0, 7),))])
def test_ctrl_double_click_reuses_overlap_and_adjacency_normalization(editor, initial, expected):
    _drag(editor, *initial)
    _double_click(editor, 1, ctrl=True)
    assert editor.textCursor().selectedText() == "AAA"
    assert editor.formatting_ranges == _overlays(editor) == expected


@pytest.mark.parametrize("text, position", [
    ("first alpha,beta tail", 8),
    ("first alpha,beta tail", 11),  # Punctuation uses the native Qt selection.
    ("first alpha,beta tail", 13),
    ("first l'acqua tail", 9),
    ("first città tail", 8),
    ("first caffè tail", 9),
    ("first e\u0300cole tail", 9),
    ("first 😀 città tail", 12),  # Positions stay in Qt UTF-16 units.
    ("first   tail", 6),
    ("first\n", 6),
    ("", 0),
])
def test_ctrl_double_click_word_boundaries_match_unmodified_qtextedit(editor, text, position):
    editor.setPlainText(text)
    native = QTextEdit()
    native.setFont(editor.font())
    native.resize(editor.size())
    native.setPlainText(text)
    native.show()
    try:
        _double_click(native, position)
        selected = native.textCursor()
        initial = ((0, 5),) if text else ()
        if initial:
            _drag(editor, 0, 5)
        _double_click(editor, position, ctrl=True)
        assert editor.textCursor().selectedText() == selected.selectedText()
        native_range = (selected.selectionStart(), selected.selectionEnd())
        expected_positions = {i for start, end in initial for i in range(start, end)}
        if selected.hasSelection():
            expected_positions.update(range(*native_range))
        assert editor.formatting_ranges == _overlays(editor)
        assert {i for start, end in editor.formatting_ranges
                for i in range(start, end)} == expected_positions
        assert all(start < end for start, end in editor.formatting_ranges)
        assert editor.toPlainText() == text
    finally:
        native.close()
        native.deleteLater()


@pytest.mark.parametrize("reset", ["click", "drag"])
def test_normal_mouse_action_clears_ctrl_double_click_ranges(editor, reset):
    _three_words(editor)
    if reset == "click":
        QTest.mouseClick(editor.viewport(), Qt.MouseButton.LeftButton, pos=_position(editor, 6))
        assert not editor.formatting_ranges and not editor.extraSelections()
    else:
        _drag(editor, 4, 7)
        assert editor.formatting_ranges == _overlays(editor) == ((4, 7),)


@pytest.fixture(params=["textbox", "callout"])
def active_editor(request, tmp_path, app, pdf_fixture_copy):
    source = tmp_path / "multi-range.pdf"
    pdf_fixture_copy(source)
    window = MainWindow()
    window.resize(1100, 800)
    window.show()
    session, view = _open(window, source)
    rect = PdfRect(40, 70, 480, 230)
    target = (RectTarget(0, rect) if request.param == "textbox"
              else view._callout_target(0, rect, PdfPoint(20, 40)))
    rich = RichText((
        RichTextRun("AAA", "Helvetica", 12, RgbColor(0, 0, 0)),
        RichTextRun(" gap ", "Courier", 15, RgbColor(.12, .58, .22), italic=True),
        RichTextRun("BBB", "Helvetica", 12, RgbColor(0, 0, 0)),
        RichTextRun(" gap ", "Courier", 15, RgbColor(.08, .32, .86), bold=True),
        RichTextRun("CCC", "Helvetica", 12, RgbColor(0, 0, 0)),
    ))
    view._begin_text_box_editor(target, None, _style(), rich.plain, rich)
    yield window, session, view, request.param
    close_window_discard(window)


def _characters(rich):
    return [replace(run, text=character) for run in rich.runs for character in run.text]


@pytest.mark.parametrize("select_ranges", [_three_ranges, _three_words], ids=["drag", "double_click"])
def test_all_existing_controls_format_every_range_preserving_other_attributes(active_editor, select_ranges):
    window, session, view, _kind = active_editor
    editor = view._text_editor
    before = _characters(view._editor_rich_text())
    select_ranges(editor)
    operations = [
        (lambda: QTest.mouseClick(window.context_bold_button, Qt.MouseButton.LeftButton),
         {"bold": True}),
        (lambda: QTest.mouseClick(window.context_italic_button, Qt.MouseButton.LeftButton),
         {"italic": True}),
        (lambda: window._text_color_selected(window.text_color_actions["red"]),
         {"color": RgbColor(.86, .12, .12)}),
        (lambda: window._text_font_selected(window.text_font_actions["Courier"]),
         {"font_family": "Courier"}),
        (lambda: window.context_text_size_spin.setValue(24), {"font_size": 24}),
    ]
    for invoke, changes in operations:
        view.preserve_text_box_formatting_selection()
        editor.clearFocus()
        invoke()
        assert view._text_editor is editor and editor.hasFocus()
        before = [replace(run, **changes) if any(start <= i < end for start, end in RANGES)
                  else run for i, run in enumerate(before)]
        assert _characters(view._editor_rich_text()) == before
        assert editor.formatting_ranges == _overlays(editor) == RANGES
        assert session.document_revision == 0


@pytest.mark.parametrize("select_ranges", [_three_ranges, _three_words], ids=["drag", "double_click"])
def test_one_local_undo_redo_covers_the_whole_formatting_action(active_editor, select_ranges):
    _window, session, view, _kind = active_editor
    target = view._editing_target
    assert view._confirm_text_box_editor()
    wait_until(lambda: session.document_revision == 1)
    model = next(iter(session.annotations.values()))
    assert session.can_undo
    view._begin_text_box_editor(target, model.annotation_id.value, model.style,
                                model.text, model.rich_text)
    editor = view._text_editor
    original = view._editor_rich_text()
    select_ranges(editor)
    view.set_text_box_bold(True)
    bold = view._editor_rich_text()
    view.set_text_box_italic(True)
    both = view._editor_rich_text()
    QTest.keyClick(editor, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert view._editor_rich_text() == bold
    QTest.keyClick(editor, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert view._editor_rich_text() == original
    QTest.keyClick(editor, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
    assert view._editor_rich_text() == bold
    QTest.keyClick(editor, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
    assert view._editor_rich_text() == both
    assert not editor.extraSelections()  # Undo/redo returns to normal single-cursor UI.
    assert session.document_revision == 1 and len(session.annotations) == 1
    assert session.can_undo  # Local undo never consumes annotation history.


def test_empty_ctrl_click_keeps_ranges_for_formatting_but_typing_uses_its_caret(active_editor):
    _window, _session, view, _kind = active_editor
    editor = view._text_editor
    _three_ranges(editor)
    _drag(editor, 6, 6, ctrl=True)
    view.set_text_box_bold(True)
    assert editor.formatting_ranges == RANGES
    assert [run.bold for run in _characters(view._editor_rich_text())[:3]] == [True] * 3
    QTest.keyClicks(editor, "Z")
    assert editor.toPlainText() == "AAA gaZp BBB gap CCC"
    assert not editor.formatting_ranges and not editor.extraSelections()


def test_unicode_ranges_use_qt_positions_without_changing_text(active_editor):
    _window, _session, view, _kind = active_editor
    target = view._editing_target
    text = "È 😀 caffè & < > fine\n città e\u0300 fine"
    view._begin_text_box_editor(target, None, _style(), text)
    editor = view._text_editor
    ranges = []
    for token in ("😀", "< >", "città"):
        cursor = editor.document().find(token)
        ranges.append((cursor.selectionStart(), cursor.selectionEnd()))
        _drag(editor, *ranges[-1], ctrl=len(ranges) > 1)
    assert editor.formatting_ranges == _overlays(editor) == tuple(ranges)
    assert ranges[0][1] - ranges[0][0] == 2  # One astral character, two QChars.
    original = _characters(view._editor_rich_text())
    view.set_text_box_bold(True)
    actual = _characters(view._editor_rich_text())
    # Check by tokens instead of Python indices into the UTF-16 range model.
    selected = {i for token in ("😀", "< >", "città")
                for i in range(text.index(token), text.index(token) + len(token))}
    expected = [replace(run, bold=True) if i in selected else run
                for i, run in enumerate(original)]
    assert actual == expected
    assert editor.toPlainText() == view._editor_rich_text().plain == text


def test_confirm_save_reopen_preserves_disjoint_formatted_runs(active_editor, tmp_path):
    window, session, view, kind = active_editor
    source = session.canonical_path
    source_bytes = Path(source).read_bytes()
    text = "caffè gap & < > gap città"
    view._begin_text_box_editor(view._editing_target, None, _style(), text)
    editor = view._text_editor
    for index, token in enumerate(("caffè", "& < >", "città")):
        cursor = editor.document().find(token)
        _drag(editor, cursor.selectionStart(), cursor.selectionEnd(), ctrl=index > 0)
    view.set_text_box_bold(True)
    view.set_text_box_italic(True)
    view.set_text_box_text_color("red")
    view.set_text_box_font_family("Courier")
    view.set_text_box_font_size(18)
    expected = view._editor_rich_text()
    commands = []
    view.text_box_requested.connect(commands.append)
    assert view._confirm_text_box_editor()
    assert not editor.formatting_ranges and not editor.extraSelections()
    assert commands[-1].rich_text == expected
    wait_until(lambda: session.document_revision == 1)
    assert next(iter(session.annotations.values())).rich_text == expected
    saved = tmp_path / "disjoint-saved.pdf"
    save(window.manager, session, saved)
    assert Path(source).read_bytes() == source_bytes
    assert window.manager.request_close(session.document_id)
    wait_until(lambda: session.document_id not in window.manager.sessions)
    reopened, _view = _open(window, saved)
    wait_until(lambda: reopened.inventory_complete)
    assert len(reopened.annotation_inventory) == 1
    summary = next(iter(reopened.annotation_inventory.values()))
    assert summary.kind == ("text_box" if kind == "textbox" else "callout")
    window.manager.request_annotation_detail(reopened.document_id, summary.logical_id)
    wait_until(lambda: summary.logical_id in reopened.annotation_details)
    detail = reopened.annotation_details[summary.logical_id]
    assert detail.contents == text
    actual = detail.edit_rich_text
    assert actual is not None and actual.plain == text
    assert len(actual.runs) == len(expected.runs)
    for saved_run, expected_run in zip(actual.runs, expected.runs):
        assert replace(saved_run, color=expected_run.color) == expected_run
        assert saved_run.color.red == pytest.approx(expected_run.color.red, abs=1 / 255)
        assert saved_run.color.green == pytest.approx(expected_run.color.green, abs=1 / 255)
        assert saved_run.color.blue == pytest.approx(expected_run.color.blue, abs=1 / 255)


@pytest.mark.parametrize("transition", ["switch_tool", "click_outside", "zoom", "layout",
                                        "tab", "tab_close", "application", "cancel"])
@pytest.mark.parametrize("select_ranges", [_three_ranges, _three_words], ids=["drag", "double_click"])
def test_lifecycle_clears_temporary_ranges(active_editor, transition, monkeypatch, tmp_path,
                                          pdf_fixture_copy, select_ranges):
    window, session, view, kind = active_editor
    editor = view._text_editor
    select_ranges(editor)
    view.set_text_box_bold(True)
    expected = view._editor_rich_text()
    commands = []
    view.text_box_requested.connect(commands.append)
    if transition == "switch_tool":
        assert (view.set_callout_enabled(True) if kind == "textbox"
                else view.set_text_box_enabled(True))
        # A fresh editor never inherits selections from the other tool.
        rect = PdfRect(40, 70, 480, 230)
        target = (view._callout_target(0, rect, PdfPoint(20, 40)) if kind == "textbox"
                  else RectTarget(0, rect))
        view._begin_text_box_editor(target, None, _style())
        assert not view._text_editor.formatting_ranges and not view._text_editor.extraSelections()
        view.cancel_text_box_edit()
    elif transition == "click_outside":
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                         pos=view._display_rect(0, PdfRect(500, 300, 510, 310)).center().toPoint())
    elif transition == "zoom":
        view.set_zoom(1.5)
    elif transition == "layout":
        view.set_layout(LayoutMode.DOUBLE)
    elif transition == "tab":
        other = pdf_fixture_copy(tmp_path / "other.pdf")
        _other_session, other_view = _open(window, other)
        assert view._text_editor is None and other_view._text_editor is None
    elif transition in ("tab_close", "application"):
        def discard_after_confirmation(_session):
            # Like the real modal close prompt, allow IPC acknowledgements to
            # complete while the user decides what to do with the draft.
            wait_until(lambda: session.document_revision == 1)
            return QMessageBox.StandardButton.Discard

        monkeypatch.setattr(window, "_close_choice", discard_after_confirmation)
        if transition == "application":
            window.close()
            wait_until(lambda: window.manager.is_shutdown)
        else:
            window._tab_closed(window.tabs.indexOf(view))
            wait_until(lambda: session.document_id not in window.manager.sessions)
    else:
        QTest.keyClick(editor, Qt.Key.Key_Escape)
    # Cancellation happens before deferred QObject destruction, so stale
    # positions cannot survive in a retained reference during a transition.
    # The tab/application paths may already have deleted the Qt object.
    assert not editor.formatting_ranges
    if transition != "cancel":
        assert len(commands) == 1 and commands[0].rich_text == expected
    else:
        assert not commands and session.document_revision == 0
    if transition not in ("application", "tab_close", "cancel"):
        wait_until(lambda: session.document_revision == 1)


def test_empty_editor_ctrl_click_keeps_insertion_and_confirmation_behavior(active_editor):
    _window, session, view, _kind = active_editor
    view._begin_text_box_editor(view._editing_target, None, _style())
    editor = view._text_editor
    _drag(editor, 0, 0, ctrl=True)
    assert not editor.formatting_ranges and not editor.extraSelections()
    view.set_text_box_bold(True)
    assert editor.currentCharFormat().fontWeight() >= QFont.Weight.Bold
    QTest.keyClick(editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    wait_until(lambda: session.document_revision == 1)
    assert next(iter(session.annotations.values())).text == ""
