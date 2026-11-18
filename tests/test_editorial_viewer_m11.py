from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QToolTip

from pdf_annotator.app.document_session import Lifecycle
from pdf_annotator.app.main_window import MainWindow
from pdf_annotator.domain.errors import MutationError, MutationErrorCode
from pdf_annotator.domain.kinds import AnnotationKind
from pdf_annotator.domain.results import MutationOutcome, MutationResult
from pdf_annotator.domain.targets import TextInsertionTarget, TextRangeTarget
from pdf_annotator.viewer.pdf_view import PdfView, _PendingEditorial
from .conftest import close_window_discard, wait_until


def _open(window: MainWindow, path: Path):
    window.open_paths([str(path)])
    wait_until(lambda: any(session.canonical_path == str(path.resolve())
                           and session.lifecycle == Lifecycle.READY
                           for session in window.manager.sessions.values()))
    session = next(session for session in window.manager.sessions.values()
                   if session.canonical_path == str(path.resolve()))
    view = window._widgets[session.document_id]
    wait_until(lambda: view.scheduler.settled and bool(view.text_layer.loaded_chunks(0)))
    return session, view


def _character_point(view, index: int) -> QPoint:
    characters = tuple(character for chunk in view.text_layer.loaded_chunks(0)
                       for character in chunk.characters)
    quad = characters[index].quad
    center = (sum(point.x for point in quad.points) / 4,
              sum(point.y for point in quad.points) / 4)
    mapped = view.coordinate_mapper(0).canonical_to_viewport(center)
    return QPoint(round(mapped[0]), round(mapped[1]))


def _character(view, index: int):
    return tuple(character for chunk in view.text_layer.loaded_chunks(0)
                 for character in chunk.characters)[index]


def _quad_point(view, point) -> QPoint:
    mapped = view.coordinate_mapper(0).canonical_to_viewport((point.x, point.y))
    return QPoint(round(mapped[0]), round(mapped[1]))


def _quad_center(view, quad) -> QPoint:
    return _quad_point(view, type(quad.upper_left)(
        sum(point.x for point in quad.points) / 4,
        sum(point.y for point in quad.points) / 4))


def _select(view, start: QPoint, end: QPoint) -> TextRangeTarget:
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view.viewport(), end, delay=1)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    wait_until(lambda: view.text_layer.confirmed is not None)
    return view.text_layer.confirmed


def test_m11_delete_insert_replace_are_native_logical_non_movable_viewer_operations(
        tmp_path, app, pdf_fixture_copy):
    source = tmp_path / "m11-viewer.pdf"
    pdf_fixture_copy(source)
    window = MainWindow(); window.resize(1100, 800); window.show()
    try:
        session, view = _open(window, source)

        view.set_delete_text_enabled(True)
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=_character_point(view, 0))
        QTest.mouseMove(view.viewport(), _character_point(view, 4), delay=1)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=_character_point(view, 4))
        wait_until(lambda: session.document_revision == 1)
        delete_model = next(model for model in session.annotations.values()
                            if model.kind == AnnotationKind.DELETE_TEXT)
        assert isinstance(delete_model.target, TextRangeTarget)
        assert view.selected_text_markup_id == delete_model.annotation_id.value
        assert not view.delete_text_enabled
        # The one-shot deletion leaves the next interactions neutral.
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=_character_point(view, 10))
        wait_until(lambda: view.text_layer.insertion is not None)
        assert view.text_layer.confirmed is None
        # A drag starts normal text selection; it never creates a second delete.
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=_character_point(view, 2))
        QTest.mouseMove(view.viewport(), _character_point(view, 5), delay=1)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=_character_point(view, 5))
        assert session.document_revision == 1
        view.set_delete_text_enabled(False)
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=_character_point(view, 2))
        assert view.selected_text_markup_id == delete_model.annotation_id.value
        view.setFocus(); QTest.keyClick(view, Qt.Key.Key_Delete)
        wait_until(lambda: session.document_revision == 2)
        assert not session.annotations

        view.set_insert_text_enabled(True)
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=_character_point(view, 2))
        wait_until(lambda: view._editorial_editor is not None)
        view._editorial_editor.setPlainText(" città ")
        QTest.keyClick(view._editorial_editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: session.document_revision == 3)
        insert_model = next(iter(session.annotations.values()))
        assert insert_model.kind == AnnotationKind.INSERT_TEXT
        assert isinstance(insert_model.target, TextInsertionTarget)
        assert insert_model.text == " città "
        assert view.selected_text_markup_id == insert_model.annotation_id.value
        assert not view.insert_text_enabled and view._editorial_editor is None
        view.setFocus(); QTest.keyClick(view, Qt.Key.Key_Delete)
        wait_until(lambda: session.document_revision == 4)
        assert not session.annotations

        view.set_insert_text_enabled(False)
        view.set_replace_text_enabled(True)
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=_character_point(view, 7))
        QTest.mouseMove(view.viewport(), _character_point(view, 11), delay=1)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=_character_point(view, 11))
        wait_until(lambda: view._editorial_editor is not None)
        # Empty payload cancels rather than creating a Delete correction.
        QTest.keyClick(view._editorial_editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        assert view._editorial_editor is None and session.document_revision == 4
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=_character_point(view, 7))
        QTest.mouseMove(view.viewport(), _character_point(view, 11), delay=1)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=_character_point(view, 11))
        wait_until(lambda: view._editorial_editor is not None)
        view._editorial_editor.setPlainText("nuovo\nUnicode 日本語")
        QTest.keyClick(view._editorial_editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: session.document_revision == 5)
        replace_model = next(iter(session.annotations.values()))
        assert replace_model.kind == AnnotationKind.REPLACE_TEXT
        assert isinstance(replace_model.target, TextRangeTarget)
        assert replace_model.text == "nuovo\nUnicode 日本語"
        # The two PDF components remain one model and one atomic UI delete.
        assert len(session.annotations) == 1
        assert not view.replace_text_enabled and view._editorial_editor is None
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=_character_point(view, 2))
        wait_until(lambda: view.text_layer.insertion is not None)
        assert view.text_layer.confirmed is None
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=_character_point(view, 8))
        assert view.selected_text_markup_id == replace_model.annotation_id.value
        view.setFocus(); QTest.keyClick(view, Qt.Key.Key_Delete)
        wait_until(lambda: session.document_revision == 6)
        assert not session.annotations
    finally:
        close_window_discard(window)


def test_m11_insert_uses_confirmed_before_after_boundaries_and_cleans_failed_pending(
        tmp_path, app, pdf_fixture_copy):
    source = tmp_path / "m11-boundaries.pdf"
    pdf_fixture_copy(source)
    window = MainWindow(); window.resize(1000, 760); window.show()
    try:
        session, view = _open(window, source)
        view.set_insert_text_enabled(True)
        character = _character(view, 3)
        # The two halves of one glyph resolve to its two distinct M6 boundaries.
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                         pos=_quad_point(view, character.quad.upper_left))
        wait_until(lambda: view._editorial_editor is not None)
        assert isinstance(view._editing_editorial_target, TextInsertionTarget)
        assert view._editing_editorial_target.affinity.value == "before"
        QTest.keyClick(view._editorial_editor, Qt.Key.Key_Escape)
        assert session.document_revision == 0 and view.text_layer.insertion is None

        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                         pos=_quad_point(view, character.quad.upper_right))
        wait_until(lambda: view._editorial_editor is not None)
        target = view._editing_editorial_target
        assert isinstance(target, TextInsertionTarget) and target.affinity.value == "after"
        QTest.keyClick(view._editorial_editor, Qt.Key.Key_Escape)

        # A failed/stale response removes the optimistic overlay, keeps the
        # document clean and restores the plain-text editor content.
        command_id = uuid4().hex
        view.pending_editorials[command_id] = _PendingEditorial(
            AnnotationKind.INSERT_TEXT, target, 0.0, "testo recuperabile")
        view.handle_annotation_result(MutationResult(
            command_id, session.document_id, session.document_generation,
            MutationOutcome.FAILED, 0, 0,
            error=MutationError.create(MutationErrorCode.WORKER_FAILURE)))
        assert not view.pending_editorials and not session.annotations
        assert session.document_revision == session.saved_revision == 0
        assert view._editorial_editor is not None
        assert view._editorial_editor.toPlainText() == "testo recuperabile"
        QTest.keyClick(view._editorial_editor, Qt.Key.Key_Escape)
    finally:
        close_window_discard(window)


def test_m11_delete_and_replace_keep_m6_multiline_quads(tmp_path, app, pdf_fixture_copy):
    source = tmp_path / "m11-multiline.pdf"
    pdf_fixture_copy(source)
    window = MainWindow(); window.resize(1000, 760); window.show()
    try:
        session, view = _open(window, source)
        characters = tuple(character for chunk in view.text_layer.loaded_chunks(0)
                           for character in chunk.characters)
        first = characters[0]
        second_line = next(character for character in characters
                           if character.line_index != first.line_index)
        start, end = _quad_point(view, first.quad.upper_left), _quad_point(view, second_line.quad.upper_right)

        view.set_delete_text_enabled(True)
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(view.viewport(), end, delay=1)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        wait_until(lambda: session.document_revision == 1)
        deleted = next(iter(session.annotations.values()))
        assert deleted.kind == AnnotationKind.DELETE_TEXT
        assert isinstance(deleted.target, TextRangeTarget) and len(deleted.target.quads) >= 2

        view.set_replace_text_enabled(True)
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(view.viewport(), end, delay=1)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        wait_until(lambda: view._editorial_editor is not None)
        view._editorial_editor.setPlainText("sostituzione multilinea")
        QTest.keyClick(view._editorial_editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: session.document_revision == 2)
        replaced = next(model for model in session.annotations.values()
                        if model.kind == AnnotationKind.REPLACE_TEXT)
        assert isinstance(replaced.target, TextRangeTarget) and len(replaced.target.quads) >= 2
        view.set_replace_text_enabled(False)
        # Every text quad, not just the visual strike or caret, selects the
        # one logical Replace correction and exposes its confirmed payload.
        for quad in replaced.target.quads:
            point = _quad_center(view, quad)
            assert view._hit_test_non_movable_text_markup(QPointF(point), 0) == replaced.annotation_id.value
            QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
            assert view.selected_text_markup_id == replaced.annotation_id.value
            assert view._hover_editorial_id == replaced.annotation_id.value
            assert PdfView.editorial_tooltip_text(replaced) == "sostituzione multilinea"
    finally:
        close_window_discard(window)


def test_m11_toolbar_consumes_m6_selection_before_tool_activation(tmp_path, app, pdf_fixture_copy):
    source = tmp_path / "m11-selection-first.pdf"
    pdf_fixture_copy(source)
    window = MainWindow(); window.resize(1000, 760); window.show()
    try:
        session, view = _open(window, source)
        selected = _select(view, _character_point(view, 1), _character_point(view, 5))
        window.delete_text_action.trigger()
        wait_until(lambda: session.document_revision == 1)
        deleted = next(iter(session.annotations.values()))
        assert deleted.kind == AnnotationKind.DELETE_TEXT and deleted.target == selected

        # Successful Delete is one-shot: the next drag makes a normal selection,
        # not a second deletion request.
        assert not view.delete_text_enabled and view.text_layer.confirmed is None
        next_selected = _select(view, _character_point(view, 7), _character_point(view, 10))
        assert session.document_revision == 1 and view.text_layer.confirmed == next_selected

        selected = _select(view, _character_point(view, 12), _character_point(view, 16))
        window.replace_text_action.trigger()
        wait_until(lambda: view._editorial_editor is not None)
        assert view._editing_editorial_target == selected
        view._editorial_editor.setPlainText(" sostituzione ")
        QTest.keyClick(view._editorial_editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: session.document_revision == 2)
        replaced = next(model for model in session.annotations.values()
                        if model.kind == AnnotationKind.REPLACE_TEXT)
        assert replaced.target == selected and replaced.text == " sostituzione "
    finally:
        close_window_discard(window)


def test_m11_insert_stays_point_based_and_editorial_tooltips_preserve_payload(
        tmp_path, app, pdf_fixture_copy):
    source = tmp_path / "m11-tooltip.pdf"
    pdf_fixture_copy(source)
    window = MainWindow(); window.resize(1000, 760); window.show()
    try:
        session, view = _open(window, source)
        _select(view, _character_point(view, 1), _character_point(view, 5))
        window.insert_text_action.trigger()
        assert session.document_revision == 0 and view._editorial_editor is None
        assert view.text_layer.confirmed is None  # Insert never consumes a range.
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=_character_point(view, 2))
        wait_until(lambda: view._editorial_editor is not None)
        insert_payload = " città  日本語 "
        view._editorial_editor.setPlainText(insert_payload)
        QTest.keyClick(view._editorial_editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: session.document_revision == 1)
        inserted = next(iter(session.annotations.values()))
        assert not view.insert_text_enabled
        hover = _quad_point(view, inserted.target.anchor)
        QTest.mouseMove(view.viewport(), hover)
        view._update_editorial_tooltip(QPointF(hover))
        assert view._hover_editorial_id == inserted.annotation_id.value
        assert PdfView.editorial_tooltip_text(inserted) == insert_payload
        assert insert_payload.strip() in QToolTip.text()

        selected = _select(view, _character_point(view, 8), _character_point(view, 11))
        window.replace_text_action.trigger()
        wait_until(lambda: view._editorial_editor is not None)
        replace_payload = "  revisione\npiù chiara  "
        view._editorial_editor.setPlainText(replace_payload)
        QTest.keyClick(view._editorial_editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: session.document_revision == 2)
        replaced = next(model for model in session.annotations.values()
                        if model.kind == AnnotationKind.REPLACE_TEXT)
        assert not view.replace_text_enabled
        hover = _character_point(view, 9)
        QTest.mouseMove(view.viewport(), hover)
        view._update_editorial_tooltip(QPointF(hover))
        assert view._hover_editorial_id == replaced.annotation_id.value
        assert PdfView.editorial_tooltip_text(replaced) == replace_payload
        assert replace_payload.strip().splitlines()[0] in QToolTip.text()

        # Highlight retains its established selection-first command path.
        _select(view, _character_point(view, 15), _character_point(view, 18))
        window.highlight_action.trigger()
        wait_until(lambda: session.document_revision == 3)
        assert any(model.kind == AnnotationKind.HIGHLIGHT for model in session.annotations.values())
    finally:
        QToolTip.hideText()
        close_window_discard(window)


def test_m11_insert_caret_hit_area_is_screen_space_and_click_shows_payload(
        tmp_path, app, pdf_fixture_copy):
    source = tmp_path / "m11-insert-hit.pdf"
    pdf_fixture_copy(source)
    window = MainWindow(); window.resize(1000, 760); window.show()
    try:
        session, view = _open(window, source)
        view.set_insert_text_enabled(True)
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=_character_point(view, 2))
        wait_until(lambda: view._editorial_editor is not None)
        payload = "  inserimento 日本語  "
        view._editorial_editor.setPlainText(payload)
        QTest.keyClick(view._editorial_editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: session.document_revision == 1)
        model = next(iter(session.annotations.values()))
        view.set_insert_text_enabled(False)
        before = session.worker.submitted_counts
        for zoom in (.5, 1.0, 4.0):
            view.set_zoom(zoom)
            top = view.coordinate_mapper(0).canonical_to_viewport(
                (model.target.boundary_top.x, model.target.boundary_top.y))
            bottom = view.coordinate_mapper(0).canonical_to_viewport(
                (model.target.anchor.x, model.target.anchor.y))
            midpoint = QPointF((top[0] + bottom[0]) / 2, (top[1] + bottom[1]) / 2)
            inside = QPoint(round(midpoint.x() + 13), round(midpoint.y()))
            outside = QPoint(round(midpoint.x() + 16), round(midpoint.y()))
            assert view._hit_test_editorial_insert(QPointF(inside), 0) == model.annotation_id.value
            assert view._hit_test_editorial_insert(QPointF(outside), 0) is None
            QTest.mouseMove(view.viewport(), inside)
            # QTest's synthetic viewport move does not enter the scroll-area
            # override. Resolve the UI-only hover at the delivered position.
            assert not view.insert_text_enabled
            view._update_editorial_tooltip(QPointF(inside))
            assert view._hover_editorial_id == model.annotation_id.value
            assert payload.strip() in QToolTip.text()
        assert session.worker.submitted_counts == before  # hover is UI-only.
        point = _quad_point(view, model.target.anchor)
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
        assert view.selected_text_markup_id == model.annotation_id.value
        assert view._hover_editorial_id == model.annotation_id.value
        assert PdfView.editorial_tooltip_text(model) == payload
    finally:
        QToolTip.hideText()
        close_window_discard(window)
