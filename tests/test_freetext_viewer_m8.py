from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt, qInstallMessageHandler
from PySide6.QtTest import QTest

from pdf_annotator.app.document_session import Lifecycle
from pdf_annotator.app.main_window import MainWindow
from pdf_annotator.domain.annotation_commands import CreateFreeText
from pdf_annotator.domain.kinds import AnnotationKind
from pdf_annotator.viewer.layout_engine import LayoutMode
from .conftest import close_window_discard, wait_until


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


def _point(view, page_index: int, point: tuple[float, float]) -> QPoint:
    mapped = view.coordinate_mapper(page_index).canonical_to_viewport(point)
    return QPoint(round(mapped[0]), round(mapped[1]))


def _character_point(view, character_index: int, *, minimum_y: float = 0) -> QPoint:
    wait_until(lambda: bool(view.text_layer.loaded_chunks(0)))
    characters = tuple(
        character for chunk in view.text_layer.loaded_chunks(0)
        for character in chunk.characters
        if sum(point.y for point in character.quad.points) / 4 >= minimum_y)
    quad = characters[character_index].quad
    center = (sum(point.x for point in quad.points) / 4,
              sum(point.y for point in quad.points) / 4)
    return _point(view, 0, center)


def test_text_box_gesture_edit_move_resize_delete_uses_local_previews_only(
        tmp_path, app, pdf_fixture_copy):
    source = tmp_path / "m8-viewer.pdf"
    pdf_fixture_copy(source)
    window = MainWindow(); window.resize(1000, 760); window.show()
    try:
        session, view = _open(window, source)
        view.set_text_box_enabled(True)
        view.set_text_box_fill("pale-blue")
        view.set_text_box_border("red")
        start, end = _point(view, 0, (35, 45)), _point(view, 0, (285, 155))
        before = session.worker.submitted_counts
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(view.viewport(), end, delay=2)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        assert view._text_editor is not None
        assert session.worker.submitted_counts == before
        view._text_editor.setPlainText("Testo di revisione")
        QTest.keyClick(view._text_editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: session.document_revision == 1)
        model = next(iter(session.annotations.values()))
        assert model.kind == AnnotationKind.TEXT_BOX and model.text == "Testo di revisione"
        assert model.style.fill is not None and model.style.border.red == pytest.approx(.86)
        assert not view.text_box_enabled and view._text_editor is None
        # The following drag is neutral object movement, not a second box draw.
        center = _point(view, 0, (160, 100))
        target = _point(view, 0, (190, 120))
        count = session.worker.submitted_counts
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=center)
        QTest.mouseMove(view.viewport(), target, delay=2)
        assert session.worker.submitted_counts == count
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=target)
        wait_until(lambda: session.document_revision == 2)
        moved = session.annotations[model.annotation_id.value]
        assert moved.target.rect.x0 > model.target.rect.x0
        corner = _point(view, 0, (moved.target.rect.x1, moved.target.rect.y1))
        wider = _point(view, 0, (moved.target.rect.x1 + 40, moved.target.rect.y1 + 25))
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=corner)
        QTest.mouseMove(view.viewport(), wider, delay=2)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=wider)
        wait_until(lambda: session.document_revision == 3)
        resized = session.annotations[model.annotation_id.value]
        assert resized.target.rect.x1 > moved.target.rect.x1
        QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton,
                           pos=_point(view, 0, (resized.target.rect.x0 + 40,
                                                 resized.target.rect.y0 + 30)))
        assert view._text_editor is not None
        view._text_editor.setPlainText("Testo modificato")
        assert view._editing_target.rect == resized.target.rect
        QTest.keyClick(view._text_editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: session.document_revision == 4)
        view.setFocus(); QTest.keyClick(view, Qt.Key.Key_Delete)
        wait_until(lambda: session.document_revision == 5)
        assert not session.annotations
    finally:
        close_window_discard(window)


def test_text_box_overflow_preserves_geometry_and_confirms_full_text(
        tmp_path, app, pdf_fixture_copy):
    source = tmp_path / "m8-overflow-viewer.pdf"
    pdf_fixture_copy(source)
    window = MainWindow(); window.resize(900, 700); window.show()
    try:
        session, view = _open(window, source)
        view.set_text_box_enabled(True)
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=_point(view, 0, (30, 40)))
        QTest.mouseMove(view.viewport(), _point(view, 0, (130, 75)), delay=2)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=_point(view, 0, (130, 75)))
        assert view._text_editor is not None
        initial = view._editing_target.rect
        view._text_editor.setPlainText("questa frase non puo entrare in un riquadro cosi piccolo")
        final_rect = view._editing_target.rect
        assert final_rect.x0 == initial.x0 and final_rect.x1 == initial.x1
        assert final_rect.y0 == initial.y0 and final_rect.y1 > initial.y1
        assert view._text_editor.hasFocus()
        QTest.keyClick(view._text_editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: session.document_revision == 1)
        model = next(iter(session.annotations.values()))
        assert model.target.rect == final_rect and model.text == "questa frase non puo entrare in un riquadro cosi piccolo"
        view.set_text_box_enabled(False)
        QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton,
                           pos=_point(view, 0, (final_rect.x0 + 8, final_rect.y0 + 8)))
        assert view._text_editor is not None
        assert view._text_editor.toPlainText() == model.text
        QTest.keyClick(view._text_editor, Qt.Key.Key_Escape)
    finally:
        close_window_discard(window)


def test_text_box_auto_grow_uses_editor_layout_without_shrinking(tmp_path, app, pdf_fixture_copy):
    source = tmp_path / "m8-autogrow.pdf"; pdf_fixture_copy(source)
    window = MainWindow(); window.resize(900, 700); window.show()
    try:
        session, view = _open(window, source)
        view.set_text_box_enabled(True)
        start, end = _point(view, 0, (35, 45)), _point(view, 0, (145, 75))
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(view.viewport(), end, delay=1)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        editor = view._text_editor
        assert editor is not None
        initial = view._editing_target.rect
        editor.setPlainText("una frase lunga che deve andare a capo nel riquadro stretto")
        grown = view._editing_target.rect
        assert grown.x0 == initial.x0 and grown.x1 == initial.x1 and grown.y1 > initial.y1
        assert editor.geometry() == view._display_rect(0, grown).toAlignedRect()
        editor.setPlainText("breve")
        assert view._editing_target.rect == grown
        view.set_text_box_font_size(36)
        assert view._editing_target.rect.y1 >= grown.y1
        QTest.keyClick(editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: session.document_revision == 1)
        model = next(iter(session.annotations.values()))
        assert model.target.rect == grown or model.target.rect.y1 >= grown.y1
    finally:
        close_window_discard(window)


def test_text_box_gesture_preview_stays_transient_and_confirmed_box_survives_ui_changes(
        tmp_path, app, pdf_fixture_copy):
    first_path, second_path = tmp_path / "m8-preview.pdf", tmp_path / "m8-other.pdf"
    pdf_fixture_copy(first_path)
    pdf_fixture_copy(second_path)
    window = MainWindow(); window.resize(1000, 760); window.show()
    messages: list[str] = []
    previous_handler = qInstallMessageHandler(
        lambda _kind, _context, message: messages.append(message))
    try:
        session, view = _open(window, first_path)
        commands = []
        view.text_box_requested.connect(commands.append)
        view.set_text_box_enabled(True)
        start = _point(view, 0, (40, 50))

        # A press and both axis-degenerate moves must remain purely Qt preview state.
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        view.viewport().grab()
        QTest.mouseMove(view.viewport(), _point(view, 0, (40, 100)), delay=1)
        view.viewport().grab()
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton,
                           pos=_point(view, 0, (40, 100)))
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(view.viewport(), _point(view, 0, (120, 50)), delay=1)
        view.viewport().grab()
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton,
                           pos=_point(view, 0, (120, 50)))
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(view.viewport(), _point(view, 0, (48, 58)), delay=1)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton,
                           pos=_point(view, 0, (48, 58)))
        assert view._text_editor is None and not commands
        assert session.document_revision == 0 and not view.pending_text_boxes

        end = _point(view, 0, (260, 150))
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(view.viewport(), end, delay=1)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        assert view._text_editor is not None
        view._text_editor.setPlainText("Text Box confermato")
        QTest.keyClick(view._text_editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: len(commands) == 1)
        assert isinstance(commands[0], CreateFreeText)
        wait_until(lambda: session.document_revision == 1)
        annotation_id = next(iter(session.annotations))
        assert session.annotations[annotation_id].kind == AnnotationKind.TEXT_BOX
        view.viewport().grab()

        # A tool change commits both populated and intentionally empty boxes;
        # geometry and content are independent annotation state.
        view.set_text_box_enabled(False)
        assert annotation_id in session.annotations and view._text_editor is None
        view.set_text_box_enabled(True)
        assert annotation_id in session.annotations
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(view.viewport(), end, delay=1)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        assert view._text_editor is not None
        view.set_text_box_enabled(False)
        wait_until(lambda: len(commands) == 2 and session.document_revision == 2)
        assert view._text_editor is None and annotation_id in session.annotations
        assert sum(model.text == "" for model in session.annotations.values()) == 1

        first, last = (_character_point(view, index, minimum_y=160) for index in (0, 5))
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=first)
        QTest.mouseMove(view.viewport(), last, delay=1)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=last)
        wait_until(lambda: view.text_layer.confirmed is not None)
        assert annotation_id in session.annotations
        view.set_zoom(1.5)
        view.set_layout(LayoutMode.DOUBLE)
        view.viewport().grab()
        assert annotation_id in session.annotations

        _second_session, _second_view = _open(window, second_path)
        window.manager.activate(session.document_id)
        wait_until(lambda: window.manager.active_id == session.document_id)
        assert annotation_id in session.annotations
        QTest.qWait(1)
    finally:
        qInstallMessageHandler(previous_handler)
        close_window_discard(window)
    assert not any("QBackingStore::endPaint() called with active painter" in message
                   for message in messages)


def test_text_box_autoconfirms_for_tool_click_and_tab_change_without_double_commit(
        tmp_path, app, pdf_fixture_copy):
    first_path, second_path = tmp_path / "m8-auto-first.pdf", tmp_path / "m8-auto-second.pdf"
    pdf_fixture_copy(first_path)
    pdf_fixture_copy(second_path)
    window = MainWindow(); window.resize(1000, 760); window.show()
    try:
        session, view = _open(window, first_path)
        commands = []
        view.text_box_requested.connect(commands.append)
        view.set_text_box_enabled(True)
        start, end = _point(view, 0, (35, 45)), _point(view, 0, (270, 145))
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(view.viewport(), end, delay=1)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        view._text_editor.setPlainText("Auto conferma")
        view.set_text_box_enabled(False)
        view.set_pen_enabled(True)  # a consecutive tool event must not submit twice
        wait_until(lambda: session.document_revision == 1)
        assert len(commands) == 1 and view._text_editor is None
        model = next(iter(session.annotations.values()))

        view.set_pen_enabled(False)
        QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton,
                           pos=_point(view, 0, (100, 85)))
        assert view._text_editor is not None
        view._text_editor.setPlainText("Conferma dal click")
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                         pos=_point(view, 0, (400, 300)))
        wait_until(lambda: session.document_revision == 2)
        assert len(commands) == 2 and session.annotations[model.annotation_id.value].text == "Conferma dal click"

        QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton,
                           pos=_point(view, 0, (100, 85)))
        assert view._text_editor is not None
        view._text_editor.setPlainText("Conferma dal cambio tab")
        _open(window, second_path)
        wait_until(lambda: session.document_revision == 3)
        assert len(commands) == 3 and view._text_editor is None
    finally:
        close_window_discard(window)


def test_text_box_escape_is_the_explicit_cancel_and_editor_uses_current_style(
        tmp_path, app, pdf_fixture_copy):
    source = tmp_path / "m8-editor-style.pdf"
    pdf_fixture_copy(source)
    window = MainWindow(); window.resize(1000, 760); window.show()
    try:
        session, view = _open(window, source)
        commands = []
        view.text_box_requested.connect(commands.append)
        view.set_text_box_enabled(True)
        view.set_text_box_fill("pale-blue")
        view.set_text_box_border("red")
        view.set_text_box_text_color("green")
        view.set_text_box_font_family("Courier")
        view.set_text_box_font_size(18)
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=_point(view, 0, (35, 45)))
        QTest.mouseMove(view.viewport(), _point(view, 0, (290, 160)), delay=1)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=_point(view, 0, (290, 160)))
        editor = view._text_editor
        assert editor is not None
        # The semantic/PDF role stays Courier, while Qt renders it with the
        # bundled canonical Courier Prime physical font.
        assert session.ui.text_box_font_family == "Courier"
        assert editor.font().family() == "Courier Prime" and editor.font().pixelSize() == 18
        assert "rgba(201, 227, 255, 1)" in editor.styleSheet()
        assert "rgb(31, 148, 56)" in editor.styleSheet()
        assert "rgb(219, 31, 31)" in editor.styleSheet()
        editor.setPlainText("Annulla solo con Esc")
        QTest.keyClick(editor, Qt.Key.Key_Escape)
        assert view._text_editor is None and not commands
        assert session.document_revision == 0 and not session.annotations
    finally:
        close_window_discard(window)


def test_text_box_cross_page_drag_keeps_the_new_preview_pending_then_transfers_once(
        tmp_path, app, pdf_fixture_copy):
    source = tmp_path / "m8-cross-page-viewer.pdf"
    pdf_fixture_copy(source, pages=2, geometry=True)
    window = MainWindow(); window.resize(1300, 900); window.show()
    try:
        session, view = _open(window, source)
        view.set_text_box_enabled(True)
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=_point(view, 0, (100, 160)))
        QTest.mouseMove(view.viewport(), _point(view, 0, (300, 260)), delay=1)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=_point(view, 0, (300, 260)))
        view._text_editor.setPlainText("Da pagina a pagina")
        QTest.keyClick(view._text_editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: session.document_revision == 1)
        model = next(iter(session.annotations.values()))
        view.set_text_box_enabled(False)
        view.set_layout(LayoutMode.DOUBLE)
        source_point = _point(view, 0, (200, 210))
        destination_point = _point(view, 1, (300, 260))
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=source_point)
        QTest.mouseMove(view.viewport(), destination_point, delay=1)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=destination_point)
        # The source overlay is suppressed while the new-page preview is pending.
        assert model.annotation_id.value in view._moving_text_box_ids()
        wait_until(lambda: session.document_revision == 2)
        transferred = session.annotations[model.annotation_id.value]
        assert transferred.page_index == 1
        assert transferred.annotation_id == model.annotation_id and transferred.style == model.style
    finally:
        close_window_discard(window)
