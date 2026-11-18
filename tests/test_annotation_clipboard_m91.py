"""M9.1 typed clipboard coverage for app-created FreeText annotations."""

from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from pdf_annotator.app.main_window import MainWindow
from pdf_annotator.domain.annotation_clipboard import (ANNOTATION_CLIPBOARD_MIME,
                                                        AnnotationClipboardPayload)
from pdf_annotator.domain.annotation_commands import CommandContext, CreateCallout, CreateFreeText
from pdf_annotator.domain.geometry import PageBounds, PdfPoint, PdfRect
from pdf_annotator.domain.kinds import AnnotationKind
from pdf_annotator.domain.rich_text import RichText, RichTextRun
from pdf_annotator.domain.errors import MutationError, MutationErrorCode
from pdf_annotator.domain.results import MutationOutcome, MutationResult
from pdf_annotator.domain.styles import FreeTextStyle, RgbColor
from pdf_annotator.domain.targets import CalloutTarget, RectTarget
from .conftest import close_window_discard, wait_until
from .test_freetext_viewer_m8 import _open


@pytest.fixture(autouse=True)
def _release_clipboard_mime_before_qt_finalization(app):
    """Release Qt-owned MIME data while the shared QApplication is still alive.

    ``QClipboard.setMimeData()`` transfers QObject ownership to Qt.  The
    offscreen Windows platform can otherwise retain that object until process
    teardown, after PySide's wrapper cleanup has begun.
    """
    yield
    app.clipboard().clear()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def _style() -> FreeTextStyle:
    return FreeTextStyle(RgbColor(.86, .12, .12), RgbColor(.79, .89, 1),
                         RgbColor(0, 0, 0), .8, 1.5, 12)


def _create(view, target, rich: RichText):
    context = CommandContext.create(view.session.document_id, view.session.document_generation)
    command = (CreateFreeText(context, target, rich.plain, _style(), rich_text=rich)
               if isinstance(target, RectTarget)
               else CreateCallout(context, target, rich.plain, _style(), rich_text=rich))
    view.text_box_requested.emit(command)
    wait_until(lambda: command.annotation_id.value in view.session.annotations)
    return view.session.annotations[command.annotation_id.value]


def _close(window: MainWindow) -> None:
    close_window_discard(window)


def test_payload_is_source_independent_validated_and_scales_callout_geometry():
    rich = RichText((RichTextRun("Alpha ", "Helvetica", 12, RgbColor(0, 0, 0)),
                     RichTextRun("Beta", "Courier", 20, RgbColor(.86, .12, .12), True)))
    target = CalloutTarget(0, PdfRect(40, 60, 180, 120), PdfPoint(20, 30), PdfPoint(30, 90))
    payload = AnnotationClipboardPayload(AnnotationKind.CALLOUT, target, rich.plain, rich,
                                         _style(), PageBounds(400, 300))
    restored = AnnotationClipboardPayload.from_bytes(payload.to_bytes())
    pasted = restored.target_for_destination(1, PageBounds(800, 600), 12, 12)
    assert isinstance(pasted, CalloutTarget)
    assert pasted.text_rect == PdfRect(92, 132, 372, 252)
    assert pasted.anchor == PdfPoint(52, 72) and pasted.knee == PdfPoint(72, 192)
    assert restored.rich_text == rich and restored.style == _style()
    with pytest.raises(ValueError):
        AnnotationClipboardPayload.from_bytes(b'{"version":2}')


def test_copy_paste_textbox_cross_page_and_editor_text_priority(tmp_path, app, pdf_fixture_copy):
    source = tmp_path / "m91-text.pdf"; pdf_fixture_copy(source, pages=2)
    window = MainWindow(); window.resize(1000, 760); window.show()
    try:
        _session, view = _open(window, source)
        rich = RichText((RichTextRun("uno ", "Helvetica", 12, RgbColor(0, 0, 0)),
                         RichTextRun("due", "Courier", 18, RgbColor(.08, .32, .86), True)))
        original = _create(view, RectTarget(0, PdfRect(50, 60, 220, 130)), rich)
        view._set_selected_text_box(original.annotation_id.value)
        view.setFocus(); QTest.keyClick(view, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        mime = QApplication.clipboard().mimeData()
        assert mime is not None and mime.hasFormat(ANNOTATION_CLIPBOARD_MIME)
        # A normal click, rather than source-page metadata, chooses destination.
        view.verticalScrollBar().setValue(round(view.page_layout.pages[1].y))
        QApplication.processEvents()
        destination = view.coordinate_mapper(1).canonical_to_viewport((100, 100))
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                         pos=QtCorePoint(destination))
        QTest.keyClick(view, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: len(view.session.annotations) == 2)
        copy = next(model for model in view.session.annotations.values()
                    if model.annotation_id != original.annotation_id)
        assert copy.kind == AnnotationKind.TEXT_BOX and copy.page_index == 1
        assert copy.annotation_id != original.annotation_id and copy.rich_text == rich
        assert copy.style == original.style and copy.target.rect.x0 > original.target.rect.x0
        assert view.selected_text_box_id == copy.annotation_id.value

        view._begin_text_box_editor(original.target, original.annotation_id.value, original.style,
                                    original.text, original.rich_text)
        editor = view._text_editor; editor.setFocus()
        cursor = editor.textCursor(); cursor.setPosition(0); cursor.setPosition(3, QTextCursor.MoveMode.KeepAnchor)
        editor.setTextCursor(cursor)
        QTest.keyClick(editor, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        assert QApplication.clipboard().text() == "uno"
    finally:
        _close(window)


def QtCorePoint(point) -> "QPoint":
    """Keep coordinate construction local so tests use actual viewer clicks."""
    from PySide6.QtCore import QPoint
    return QPoint(round(point[0]), round(point[1]))


def test_screen_space_offset_cascades_for_textbox_and_callout(tmp_path, app, pdf_fixture_copy):
    source = tmp_path / "m91-offset.pdf"; pdf_fixture_copy(source)
    window = MainWindow(); window.resize(1000, 760); window.show()
    try:
        _session, view = _open(window, source)
        rich = RichText.plain_text("offset", font_family="Helvetica", font_size=12,
                                   color=RgbColor(0, 0, 0))
        original = _create(view, RectTarget(0, PdfRect(80, 90, 260, 160)), rich)
        for zoom in (.5, 1, 1.5, 4, 8):
            view.set_zoom(zoom)
            view._set_selected_text_box(original.annotation_id.value)
            view.setFocus(); QTest.keyClick(view, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
            QTest.keyClick(view, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
            wait_until(lambda: len(view.session.annotations) >= 2)
            copy = next(model for model in view.session.annotations.values()
                        if model.annotation_id != original.annotation_id)
            mapper = view.coordinate_mapper(0)
            source_xy = mapper.canonical_to_viewport((original.target.rect.x0, original.target.rect.y0))
            copy_xy = mapper.canonical_to_viewport((copy.target.rect.x0, copy.target.rect.y0))
            assert copy_xy[0] - source_xy[0] == pytest.approx(24, abs=1.5)
            assert copy_xy[1] - source_xy[1] == pytest.approx(24, abs=1.5)
            view.session.annotations.pop(copy.annotation_id.value)

        callout = _create(view, CalloutTarget(0, PdfRect(280, 180, 440, 250),
                                               PdfPoint(90, 110), PdfPoint(190, 210)), rich)
        view._set_selected_text_box(None)
        view._set_selected_callout(callout.annotation_id.value)
        view.setFocus(); QTest.keyClick(view, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(view, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: sum(model.kind == AnnotationKind.CALLOUT
                               for model in view.session.annotations.values()) >= 2)
        QTest.keyClick(view, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: sum(model.kind == AnnotationKind.CALLOUT
                               for model in view.session.annotations.values()) >= 3)
        copies = [m for m in view.session.annotations.values()
                  if m.annotation_id != callout.annotation_id and m.kind == AnnotationKind.CALLOUT]
        assert copies[-1].target.text_rect.x0 > copies[0].target.text_rect.x0
        assert copies[-1].target.anchor.x > copies[0].target.anchor.x
    finally:
        _close(window)


def test_cut_keeps_prepared_payload_and_paste_generates_new_callout(tmp_path, app, pdf_fixture_copy):
    source = tmp_path / "m91-callout.pdf"; pdf_fixture_copy(source)
    window = MainWindow(); window.resize(1000, 760); window.show()
    try:
        _session, view = _open(window, source)
        rich = RichText.plain_text("commento", font_family="Helvetica", font_size=12,
                                   color=RgbColor(0, 0, 0))
        original = _create(view, CalloutTarget(0, PdfRect(180, 120, 360, 200),
                                               PdfPoint(40, 60), PdfPoint(90, 160)), rich)
        view._set_selected_callout(original.annotation_id.value)
        view.setFocus(); QTest.keyClick(view, Qt.Key.Key_X, Qt.KeyboardModifier.ControlModifier)
        mime = QApplication.clipboard().mimeData()
        assert mime is not None and mime.hasFormat(ANNOTATION_CLIPBOARD_MIME)
        wait_until(lambda: not view.session.annotations)
        QTest.keyClick(view, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: len(view.session.annotations) == 1)
        pasted = next(iter(view.session.annotations.values()))
        assert pasted.kind == AnnotationKind.CALLOUT and pasted.annotation_id != original.annotation_id
        assert pasted.rich_text == rich and pasted.style == original.style
    finally:
        _close(window)


def test_cross_document_paste_and_invalid_mime_are_safe(tmp_path, app, pdf_fixture_copy):
    source_a = tmp_path / "m91-a.pdf"; pdf_fixture_copy(source_a)
    source_b = tmp_path / "m91-b.pdf"; pdf_fixture_copy(source_b)
    window = MainWindow(); window.resize(1000, 760); window.show()
    try:
        _session_a, view_a = _open(window, source_a)
        rich = RichText.plain_text("tra documenti", font_family="Courier", font_size=16,
                                   color=RgbColor(.08, .32, .86), bold=True)
        original = _create(view_a, RectTarget(0, PdfRect(80, 90, 270, 170)), rich)
        view_a._set_selected_text_box(original.annotation_id.value)
        view_a.setFocus(); QTest.keyClick(view_a, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)

        _session_b, view_b = _open(window, source_b)
        view_b.setFocus(); QTest.keyClick(view_b, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: len(view_b.session.annotations) == 1)
        pasted = next(iter(view_b.session.annotations.values()))
        assert pasted.annotation_id != original.annotation_id and pasted.rich_text == rich

        bad = QMimeData(); bad.setData(ANNOTATION_CLIPBOARD_MIME, b"not JSON")
        QApplication.clipboard().setMimeData(bad)
        before = view_b.session.document_revision
        QTest.keyClick(view_b, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        assert view_b.session.document_revision == before and len(view_b.session.annotations) == 1
    finally:
        _close(window)


def test_paste_pending_overlay_is_removed_on_failure(tmp_path, app, pdf_fixture_copy):
    source = tmp_path / "m91-pending.pdf"; pdf_fixture_copy(source)
    window = MainWindow(); window.resize(1000, 760); window.show()
    try:
        _session, view = _open(window, source)
        rich = RichText.plain_text("pending", font_family="Helvetica", font_size=12,
                                   color=RgbColor(0, 0, 0))
        original = _create(view, RectTarget(0, PdfRect(60, 70, 200, 130)), rich)
        view._set_selected_text_box(original.annotation_id.value)
        assert view._copy_selected_annotation()
        view.text_box_requested.disconnect(window.manager.submit_annotation)
        try:
            assert view._paste_annotation()
            assert len(view.pending_text_boxes) == 1
            command_id = next(iter(view.pending_text_boxes))
            assert command_id in view._pending_paste_ids
            view.handle_annotation_result(MutationResult(
                command_id, view.session.document_id, view.session.document_generation,
                MutationOutcome.FAILED, view.session.document_revision,
                view.session.document_revision,
                error=MutationError.create(MutationErrorCode.WORKER_FAILURE)))
            assert not view.pending_text_boxes and not view._pending_paste_ids
            assert list(view.session.annotations) == [original.annotation_id.value]
        finally:
            view.text_box_requested.connect(window.manager.submit_annotation)
    finally:
        _close(window)


def test_copy_paste_preserves_overflowing_geometry_and_rich_text(tmp_path, app, pdf_fixture_copy):
    source = tmp_path / "m91-overflow.pdf"; pdf_fixture_copy(source)
    window = MainWindow(); window.resize(1000, 760); window.show()
    try:
        _session, view = _open(window, source)
        text = "testo lungo nel clipboard " * 18
        rich = RichText((RichTextRun(text, "Courier", 28, RgbColor(.86, .12, .12), True),))
        original = _create(view, RectTarget(0, PdfRect(60, 70, 130, 122)), rich)
        view._set_selected_text_box(original.annotation_id.value)
        view.setFocus(); QTest.keyClick(view, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(view, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: len(view.session.annotations) == 2)
        pasted = next(model for model in view.session.annotations.values()
                      if model.annotation_id != original.annotation_id)
        assert pasted.rich_text == rich
        assert pasted.target.rect.x1 - pasted.target.rect.x0 == pytest.approx(
            original.target.rect.x1 - original.target.rect.x0)
        assert pasted.target.rect.y1 - pasted.target.rect.y0 == pytest.approx(
            original.target.rect.y1 - original.target.rect.y0)

        callout = _create(view, CalloutTarget(0, PdfRect(200, 160, 270, 212),
                                               PdfPoint(70, 90), PdfPoint(150, 160)), rich)
        view._set_selected_text_box(None); view._set_selected_callout(callout.annotation_id.value)
        view.setFocus(); QTest.keyClick(view, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(view, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: sum(model.kind == AnnotationKind.CALLOUT
                               for model in view.session.annotations.values()) == 2)
        pasted_callout = next(model for model in view.session.annotations.values()
                              if model.kind == AnnotationKind.CALLOUT
                              and model.annotation_id != callout.annotation_id)
        assert pasted_callout.rich_text == rich
        assert pasted_callout.target.text_rect.x1 - pasted_callout.target.text_rect.x0 == pytest.approx(
            callout.target.text_rect.x1 - callout.target.text_rect.x0)
        assert pasted_callout.target.text_rect.y1 - pasted_callout.target.text_rect.y0 == pytest.approx(
            callout.target.text_rect.y1 - callout.target.text_rect.y0)
    finally:
        _close(window)
