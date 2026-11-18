"""M9 native FreeTextCallout persistence through the common mutation path."""

import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

from pdf_annotator.app.document_manager import DocumentManager
from pdf_annotator.app.document_session import Lifecycle
from pdf_annotator.domain.annotation_commands import (CommandContext, CreateCallout,
                                                       MoveCalloutAnchor, Redo, ResizeCallout,
                                                       Undo)
from pdf_annotator.domain.geometry import PdfPoint, PdfRect
from pdf_annotator.domain.kinds import AnnotationKind
from pdf_annotator.domain.errors import MutationError, MutationErrorCode
from pdf_annotator.domain.rich_text import RichText, RichTextRun
from pdf_annotator.domain.results import MutationOutcome, MutationResult
from pdf_annotator.domain.styles import FreeTextStyle, RgbColor
from pdf_annotator.domain.targets import CalloutTarget
from pdf_annotator.domain.callout_geometry import (CALLOUT_LEADER_OFFSET, callout_line,
                                                    move_callout_box)
from pdf_annotator.app.main_window import MainWindow
from pdf_annotator.viewer.layout_engine import LayoutMode
from pdf_annotator.viewer.pdf_view import (CALLOUT_INITIAL_WIDTH,
                                           CALLOUT_INITIAL_WIDTH_SCALE,
                                           TEXT_BOX_MIN_SIZE, PdfView,
                                           _PendingFreeText, _final_text_box_rect)
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QTextCursor
from PySide6.QtTest import QTest
from pdf_annotator.worker import document_service
from .conftest import close_window_discard, wait_until
from .m8_helpers import persistence_worker
from .test_freetext_viewer_m8 import _open


def _point(view, page_index: int, point: tuple[float, float]) -> QPoint:
    mapped = view.coordinate_mapper(page_index).canonical_to_viewport(point)
    return QPoint(round(mapped[0]), round(mapped[1]))


def _style() -> FreeTextStyle:
    return FreeTextStyle(RgbColor(.08, .32, .86), RgbColor(1, .96, .70),
                         RgbColor(0, 0, 0), .8, 1.5, 12)


def test_callout_box_crossing_anchor_reselects_nearest_attachment_side():
    """A box move keeps the tip fixed but must not retain its old box side."""
    box = PdfRect(220, 140, 460, 260)
    anchor = PdfPoint(80, 190)
    moved, line = move_callout_box(box, callout_line(box, anchor), -600, 0)
    assert line[0] == anchor
    assert line[1] == PdfPoint(moved.x1 + CALLOUT_LEADER_OFFSET, 190)
    assert line[2] == PdfPoint(moved.x1, 190)


@pytest.mark.parametrize("anchor,side", [
    (PdfPoint(450, 100), "top"),
    (PdfPoint(150, 100), "top"),
    (PdfPoint(450, 400), "bottom"),
    (PdfPoint(150, 400), "bottom"),
    (PdfPoint(60, 245), "left"),
    (PdfPoint(300, 50), "top"),
])
def test_acrobat_like_callout_geometry_uses_orthogonal_stub_and_free_long_segment(anchor, side):
    box = PdfRect(200, 200, 400, 300)
    actual_anchor, knee, attachment = callout_line(box, anchor)
    assert actual_anchor == anchor
    if side in ("left", "right"):
        assert attachment.x == (box.x0 if side == "left" else box.x1)
        assert knee.y == attachment.y
        assert abs(knee.x - attachment.x) == CALLOUT_LEADER_OFFSET
    else:
        assert attachment.y == (box.y0 if side == "top" else box.y1)
        assert knee.x == attachment.x
        assert abs(knee.y - attachment.y) == CALLOUT_LEADER_OFFSET
    if anchor.x not in (knee.x, attachment.x) and anchor.y not in (knee.y, attachment.y):
        assert anchor.x != knee.x and anchor.y != knee.y


def test_anchor_move_is_not_quantized_and_box_move_and_resize_recompute_leader():
    box = PdfRect(200, 200, 400, 300)
    first = callout_line(box, PdfPoint(450, 100))
    moved_anchor = callout_line(box, PdfPoint(520, 140))
    assert moved_anchor[0] == PdfPoint(520, 140)
    assert moved_anchor[0].x != moved_anchor[1].x
    assert moved_anchor[0].y != moved_anchor[1].y
    moved_box, moved_line = move_callout_box(box, first, 80, 35)
    assert moved_line[0] == first[0]
    assert moved_line == callout_line(moved_box, first[0])
    resized = PdfRect(moved_box.x0 - 20, moved_box.y0 - 25,
                      moved_box.x1 + 75, moved_box.y1 + 40)
    resized_line = callout_line(resized, first[0])
    assert resized_line != moved_line
    assert resized_line[2].y == resized.y0


def test_short_stub_does_not_extend_past_a_nearby_anchor():
    line = callout_line(PdfRect(5, 100, 205, 200), PdfPoint(0, 150), side="left")
    assert line == (PdfPoint(0, 150), PdfPoint(0, 150), PdfPoint(5, 150))


@pytest.mark.parametrize("points", [
    [QPointF(10, 0)], [QPointF(-10, 0)], [QPointF(0, -10)], [QPointF(0, 10)],
    [QPointF(0, 0), QPointF(-10, 0)],  # coincident knee, box left of anchor
])
def test_open_arrow_wings_point_toward_first_noncoincident_callout_segment(points):
    """The OpenArrow tip stays at CL[0], including the left-box zero-knee case."""
    tip = QPointF(0, 0)
    toward = PdfView._open_arrow_toward(tip, points)
    for wing in PdfView._open_arrow_wings(tip, toward):
        assert ((wing.x() - tip.x()) * (toward.x() - tip.x())
                + (wing.y() - tip.y()) * (toward.y() - tip.y())) > 0


@pytest.fixture
def manager(app):
    instance = DocumentManager()
    yield instance
    instance.request_shutdown(discard_changes=True); wait_until(lambda: instance.is_shutdown)


def _inspect(path: Path) -> dict:
    result = subprocess.run([sys.executable, "-m", "tests.m8_helpers", str(path)],
                            cwd=Path(__file__).parents[1], capture_output=True, text=True,
                            check=True, timeout=30)
    return json.loads(result.stdout)


def _canonical_callout(record: dict, page_height: float) -> tuple[PdfPoint, ...]:
    numbers = [float(value) for value in re.findall(r"-?\d+(?:\.\d+)?", record["cl"][1])]
    return tuple(PdfPoint(numbers[index], page_height - numbers[index + 1])
                 for index in range(0, len(numbers), 2))


def test_callout_native_rich_create_resize_anchor_persists(tmp_path, manager, monkeypatch, pdf_fixture_copy):
    source = tmp_path / "m9-callout.pdf"; pdf_fixture_copy(source)
    monkeypatch.setattr(document_service, "document_worker_main", persistence_worker)
    manager.open_path(str(source)); wait_until(lambda: any(s.lifecycle == Lifecycle.READY for s in manager.sessions.values()))
    session = next(iter(manager.sessions.values()))
    style = FreeTextStyle(RgbColor(.08,.32,.86), RgbColor(1,.96,.70), RgbColor(0,0,0), .8, 1.5, 12)
    rich = RichText((RichTextRun("AAA ", "Helvetica", 12, RgbColor(0,0,0)),
                     RichTextRun("BBB", "Courier", 20, RgbColor(.86,.12,.12), True, True)))
    target = CalloutTarget(0, PdfRect(220, 140, 460, 260), PdfPoint(60, 80), PdfPoint(150, 150))
    create = CreateCallout(CommandContext.create(session.document_id, session.document_generation), target,
                           rich.plain, style, rich_text=rich)
    manager.submit_annotation(create); wait_until(lambda: session.document_revision == 1)
    moved_anchor = CalloutTarget(0, target.text_rect, PdfPoint(80, 95), PdfPoint(155, 150))
    manager.submit_annotation(MoveCalloutAnchor(CommandContext.create(session.document_id, session.document_generation), create.annotation_id, moved_anchor))
    wait_until(lambda: session.document_revision == 2)
    resized = CalloutTarget(0, PdfRect(220, 140, 480, 280), moved_anchor.anchor, moved_anchor.knee)
    manager.submit_annotation(ResizeCallout(CommandContext.create(session.document_id, session.document_generation), create.annotation_id, resized))
    wait_until(lambda: session.document_revision == 3)
    model = session.annotations[create.annotation_id.value]
    expected = CalloutTarget(0, resized.text_rect, moved_anchor.anchor,
                             callout_line(resized.text_rect, moved_anchor.anchor)[1])
    assert model.target == expected and model.rich_text == rich
    record = _inspect(Path(str(source) + ".m8-export.pdf"))["records"][0]
    assert record["subtype"] == ["name", "/FreeText"]
    assert record["contents"] == ["string", "AAA BBB"]
    assert "FreeTextCallout" in record["it"][1] and record["cl"][0] == "array"
    assert "OpenArrow" in record["le"][1]
    callout_numbers = [float(value) for value in re.findall(r"-?\d+(?:\.\d+)?", record["cl"][1])]
    assert callout_numbers[:2] == pytest.approx([
        moved_anchor.anchor.x, session.pages[0].height_pt - moved_anchor.anchor.y])
    assert "font-family:'Courier Std'" in record["rc"][1] and "font-weight:bold" in record["rc"][1]


def test_empty_callout_persists_as_empty_native_contents(tmp_path, manager, monkeypatch,
                                                          pdf_fixture_copy):
    source = tmp_path / "m9-empty-callout.pdf"
    pdf_fixture_copy(source)
    monkeypatch.setattr(document_service, "document_worker_main", persistence_worker)
    manager.open_path(str(source))
    wait_until(lambda: any(session.lifecycle == Lifecycle.READY
                           for session in manager.sessions.values()))
    session = next(iter(manager.sessions.values()))
    target = CalloutTarget(0, PdfRect(220, 140, 460, 260), PdfPoint(60, 80))
    command = CreateCallout(CommandContext.create(session.document_id,
                                                    session.document_generation), target, "", _style())
    manager.submit_annotation(command)
    wait_until(lambda: session.document_revision == 1)
    model = session.annotations[command.annotation_id.value]
    record = _inspect(Path(str(source) + ".m8-export.pdf"))["records"][0]
    assert model.text == model.rich_text.plain == ""
    assert record["contents"] == ["string", ""] and "FreeTextCallout" in record["it"][1]


def test_long_callout_text_persists_in_a_small_box(tmp_path, manager, monkeypatch, pdf_fixture_copy):
    source = tmp_path / "m9-overflow-persist.pdf"; pdf_fixture_copy(source)
    monkeypatch.setattr(document_service, "document_worker_main", persistence_worker)
    manager.open_path(str(source)); wait_until(lambda: any(s.lifecycle == Lifecycle.READY for s in manager.sessions.values()))
    session = next(iter(manager.sessions.values()))
    text = "commento molto lungo da conservare integralmente " * 16
    rich = RichText((RichTextRun(text, "Courier", 28, RgbColor(.86, .12, .12), True, True),))
    target = CalloutTarget(0, PdfRect(220, 140, 290, 192), PdfPoint(60, 80), PdfPoint(150, 140))
    command = CreateCallout(CommandContext.create(session.document_id, session.document_generation),
                            target, text, _style(), rich_text=rich)
    manager.submit_annotation(command); wait_until(lambda: session.document_revision == 1)
    model = session.annotations[command.annotation_id.value]
    expected_target = CalloutTarget(0, target.text_rect, target.anchor,
                                    callout_line(target.text_rect, target.anchor)[1])
    assert model.target == expected_target and model.rich_text == rich
    record = _inspect(Path(str(source) + ".m8-export.pdf"))["records"][0]
    assert record["contents"] == ["string", text]
    assert "font-family:'Courier Std'" in record["rc"][1] and "font-size:28pt" in record["rc"][1]


def test_callout_two_stage_gesture_editor_and_escape(tmp_path, app, monkeypatch, pdf_fixture_copy):
    source = tmp_path / "m9-viewer-create.pdf"; pdf_fixture_copy(source)
    second_source = tmp_path / "m9-viewer-second.pdf"; pdf_fixture_copy(second_source)
    monkeypatch.setattr(document_service, "document_worker_main", persistence_worker)
    window = MainWindow(); window.resize(1100, 800); window.show()
    try:
        session, view = _open(window, source)
        view.set_callout_enabled(True)
        view.set_text_box_fill("pale-yellow"); view.set_text_box_border("blue")
        anchor, start, end = (_point(view, 0, value) for value in ((55, 80), (220, 120), (450, 250)))
        before = session.worker.submitted_counts
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=anchor)
        assert view._callout_anchor is not None and session.worker.submitted_counts == before
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(view.viewport(), end, delay=1)
        assert view._callout_drag is not None
        _page, staged_anchor, staged_position = view._callout_drag
        expected_rect = view._initial_callout_rect(0, staged_position, staged_anchor)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        assert view._text_editor is not None and session.worker.submitted_counts == before
        assert isinstance(view._editing_target, CalloutTarget)
        assert view._editing_target.text_rect == expected_rect
        assert expected_rect.x1 - expected_rect.x0 == pytest.approx(
            CALLOUT_INITIAL_WIDTH * CALLOUT_INITIAL_WIDTH_SCALE)
        one_line = RichText.plain_text("M", font_family=view.text_box_style.font_family,
                                       font_size=view.text_box_style.font_size,
                                       color=view.text_box_style.text)
        previous_default_height = min(max(
            TEXT_BOX_MIN_SIZE,
            view._required_text_height(0, CALLOUT_INITIAL_WIDTH,
                                       view.text_box_style, one_line)),
            session.pages[0].height_pt)
        assert expected_rect.y1 - expected_rect.y0 == pytest.approx(previous_default_height)
        editor = view._text_editor
        editor.setPlainText("Annulla")
        QTest.keyClick(editor, Qt.Key.Key_Escape)
        assert view._text_editor is None and not session.annotations

        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=anchor)
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(view.viewport(), end, delay=1)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        assert view._text_editor is not None
        view._text_editor.setPlainText("Verificare questo dato")
        preview_target = view._editing_target
        QTest.keyClick(view._text_editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: session.document_revision == 1)
        model = next(iter(session.annotations.values()))
        assert model.kind == AnnotationKind.CALLOUT and isinstance(model.target, CalloutTarget)
        assert model.target == preview_target
        record = _inspect(Path(str(source) + ".m8-export.pdf"))["records"][0]
        assert _canonical_callout(record, session.pages[0].height_pt) == callout_line(
            model.target.text_rect, model.target.anchor)
        assert not view.callout_enabled and view._text_editor is None
        # A normal click now selects the completed annotation rather than
        # starting another Callout draft.
        center = _point(view, 0, ((model.target.text_rect.x0 + model.target.text_rect.x1) / 2,
                                  (model.target.text_rect.y0 + model.target.text_rect.y1) / 2))
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=center)
        assert view.selected_callout_id == model.annotation_id.value and view._text_editor is None
        second_session, second_view = _open(window, second_source)
        assert second_view is not view and not second_session.annotations
        assert session.annotations[model.annotation_id.value].target == model.target
    finally:
        close_window_discard(window)


def test_wider_callout_default_does_not_change_text_box_drag_geometry():
    start, end = PdfPoint(40, 55), PdfPoint(190, 125)
    assert _final_text_box_rect(start, end) == PdfRect(40, 55, 190, 125)


def test_callout_undo_redo_restores_exact_native_leader(tmp_path, manager, monkeypatch,
                                                        pdf_fixture_copy):
    source = tmp_path / "m17-callout-history.pdf"
    pdf_fixture_copy(source)
    monkeypatch.setattr(document_service, "document_worker_main", persistence_worker)
    manager.open_path(str(source))
    wait_until(lambda: any(session.lifecycle == Lifecycle.READY
                           for session in manager.sessions.values()))
    session = next(iter(manager.sessions.values()))
    original = CalloutTarget(0, PdfRect(220, 180, 460, 290), PdfPoint(520, 90))
    create = CreateCallout(CommandContext.create(session.document_id, session.document_generation),
                           original, "leader", _style())
    manager.submit_annotation(create)
    wait_until(lambda: session.document_revision == 1)
    created = session.annotations[create.annotation_id.value].target
    created_line = callout_line(created.text_rect, created.anchor)
    changed = CalloutTarget(0, created.text_rect, PdfPoint(95, 355))
    manager.submit_annotation(MoveCalloutAnchor(CommandContext.create(
        session.document_id, session.document_generation), create.annotation_id, changed))
    wait_until(lambda: session.document_revision == 2)
    changed_model = session.annotations[create.annotation_id.value].target
    changed_line = callout_line(changed_model.text_rect, changed_model.anchor)
    assert changed_line != created_line
    manager.submit_annotation(Undo(CommandContext.create(
        session.document_id, session.document_generation)))
    wait_until(lambda: session.document_revision == 3)
    record = _inspect(Path(str(source) + ".m8-export.pdf"))["records"][0]
    assert session.annotations[create.annotation_id.value].target == created
    assert _canonical_callout(record, session.pages[0].height_pt) == created_line
    manager.submit_annotation(Redo(CommandContext.create(
        session.document_id, session.document_generation)))
    wait_until(lambda: session.document_revision == 4)
    record = _inspect(Path(str(source) + ".m8-export.pdf"))["records"][0]
    assert session.annotations[create.annotation_id.value].target == changed_model
    assert _canonical_callout(record, session.pages[0].height_pt) == changed_line


def test_callout_second_click_near_anchor_repositions_compact_box(tmp_path, app, pdf_fixture_copy):
    source = tmp_path / "m9-near-anchor.pdf"; pdf_fixture_copy(source)
    window = MainWindow(); window.resize(1100, 800); window.show()
    try:
        _session, view = _open(window, source)
        view.set_callout_enabled(True)
        anchor = _point(view, 0, (180, 180))
        near_anchor = _point(view, 0, (184, 184))
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=anchor)
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=near_anchor)
        assert isinstance(view._editing_target, CalloutTarget)
        target = view._editing_target
        assert target.anchor == PdfPoint(180, 180)
        rect = target.text_rect
        assert not (rect.x0 - 24 < target.anchor.x < rect.x1 + 24
                    and rect.y0 - 24 < target.anchor.y < rect.y1 + 24)
        assert target.knee == view._callout_target(0, rect, target.anchor).knee
        QTest.keyClick(view._text_editor, Qt.Key.Key_Escape)
    finally:
        close_window_discard(window)


def test_callout_empty_creation_preview_paints_open_arrow_without_domain_text(
        tmp_path, app, pdf_fixture_copy):
    source = tmp_path / "m9-preview.pdf"; pdf_fixture_copy(source)
    window = MainWindow(); window.resize(1100, 800); window.show()
    try:
        _session, view = _open(window, source)
        target = view._callout_target(0, PdfRect(240, 150, 460, 260), PdfPoint(55, 80))
        image = QImage(view.viewport().size(), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(0, 0, 0, 0))
        painter = QPainter(image)
        try:
            # This is the exact pre-editor gesture path: no RichText exists yet.
            view._paint_callout_preview(painter, view.coordinate_mapper(0), target, _style())
        finally:
            painter.end()
        assert any(image.pixelColor(x, y).alpha() for x in range(image.width())
                   for y in range(image.height()))
    finally:
        close_window_discard(window)


def test_callout_editor_auto_grows_vertically_and_preserves_anchor(tmp_path, app, pdf_fixture_copy):
    source = tmp_path / "m9-autogrow.pdf"; pdf_fixture_copy(source)
    window = MainWindow(); window.resize(1100, 800); window.show()
    try:
        session, view = _open(window, source)
        anchor = PdfPoint(55, 80)
        target = view._callout_target(0, PdfRect(220, 130, 280, 170), anchor)
        view._begin_text_box_editor(target, None, _style())
        view._text_editor.setPlainText("a " * 24)
        assert isinstance(view._editing_target, CalloutTarget)
        grown = view._editing_target
        assert grown.text_rect.x0 == target.text_rect.x0 and grown.text_rect.x1 == target.text_rect.x1
        assert grown.text_rect.y1 > target.text_rect.y1
        assert grown.anchor == target.anchor
        assert grown.knee == view._callout_target(0, grown.text_rect, target.anchor).knee
        cursor = view._text_editor.textCursor(); cursor.setPosition(0)
        cursor.setPosition(1, QTextCursor.MoveMode.KeepAnchor); view._text_editor.setTextCursor(cursor)
        view.set_text_box_font_family("Courier"); view.set_text_box_font_size(72)
        view.set_text_box_bold(True); view.set_text_box_italic(True)
        rich_grown = view._editing_target
        assert rich_grown.text_rect.x1 - rich_grown.text_rect.x0 == pytest.approx(
            target.text_rect.x1 - target.text_rect.x0)
        assert rich_grown.text_rect.y1 >= grown.text_rect.y1
        assert rich_grown.anchor == target.anchor
        view._text_editor.selectAll()
        view.set_text_box_font_family("Helvetica"); view.set_text_box_font_size(12)
        view.set_text_box_bold(False); view.set_text_box_italic(False)
        final_target = view._editing_target
        assert final_target.text_rect.y1 >= rich_grown.text_rect.y1
        assert final_target.anchor == target.anchor
        assert view._confirm_text_box_editor(); wait_until(lambda: session.document_revision == 1)
        model = next(iter(session.annotations.values()))
        assert model.target == final_target and model.rich_text.plain == "a " * 24
    finally:
        close_window_discard(window)


def test_callout_rich_edit_move_resize_anchor_transfer_and_delete(tmp_path, app, pdf_fixture_copy):
    source = tmp_path / "m9-viewer-edit.pdf"; pdf_fixture_copy(source, pages=2, geometry=True)
    window = MainWindow(); window.resize(1300, 900); window.show()
    try:
        session, view = _open(window, source)
        target = CalloutTarget(0, PdfRect(220, 120, 460, 250), PdfPoint(60, 80), PdfPoint(60, 185))
        rich = RichText.plain_text("alpha beta", font_family="Helvetica", font_size=12,
                                   color=RgbColor(0, 0, 0))
        view._begin_text_box_editor(target, None, _style(), rich.plain, rich)
        assert view._confirm_text_box_editor(); wait_until(lambda: session.document_revision == 1)
        model = next(iter(session.annotations.values()))
        view.set_callout_enabled(False)

        # Rich-text deltas are the identical M8.2 editor path, including partial selection.
        QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=_point(view, 0, (280, 155)))
        editor = view._text_editor; cursor = editor.textCursor(); cursor.setPosition(6)
        cursor.setPosition(10, QTextCursor.MoveMode.KeepAnchor); editor.setTextCursor(cursor)
        view.set_text_box_bold(True); view.set_text_box_font_family("Courier")
        assert view._editor_rich_text().runs[-1].bold
        assert view._confirm_text_box_editor(); wait_until(lambda: session.document_revision == 2)

        current = session.annotations[model.annotation_id.value]
        for zoom in (.5, 1, 1.5, 4, 8):
            view.session.ui.zoom = zoom
            display = view._display_rect(0, current.target.text_rect)
            assert view._resize_corner(QPoint(display.bottomRight().x() + 12,
                                              display.bottomRight().y()), 0,
                                       current.target.text_rect) == "br"
            assert view._resize_corner(QPoint(display.bottomRight().x() + 17,
                                              display.bottomRight().y()), 0,
                                       current.target.text_rect) is None
        view.session.ui.zoom = 1
        display = view._display_rect(0, current.target.text_rect)
        view._update_freetext_hover_cursor(QPoint(display.bottomRight().x() + 12,
                                                  display.bottomRight().y()))
        assert view.viewport().cursor().shape() == Qt.CursorShape.SizeFDiagCursor
        box = _point(view, 0, (300, 190)); moved = _point(view, 0, (340, 215))
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=box)
        QTest.mouseMove(view.viewport(), moved, delay=1)
        assert session.annotations[model.annotation_id.value] == current
        assert view._callout_move is not None
        assert view._callout_move.anchor == current.target.anchor
        assert view._callout_move.rect is not None
        preview = view._callout_target(0, view._callout_move.rect, view._callout_move.anchor)
        assert preview.knee != current.target.knee
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=moved)
        wait_until(lambda: session.document_revision == 3)
        moved_model = session.annotations[model.annotation_id.value]
        assert moved_model.target.text_rect.x0 > current.target.text_rect.x0
        assert moved_model.target.anchor == current.target.anchor
        assert moved_model.target.knee == preview.knee

        corner = _point(view, 0, (moved_model.target.text_rect.x1, moved_model.target.text_rect.y1))
        wider = _point(view, 0, (moved_model.target.text_rect.x1 + 35, moved_model.target.text_rect.y1 + 20))
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=corner)
        QTest.mouseMove(view.viewport(), wider, delay=1); QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=wider)
        wait_until(lambda: session.document_revision == 4)
        resized = session.annotations[model.annotation_id.value]
        assert resized.target.text_rect.y1 > moved_model.target.text_rect.y1
        assert resized.target.anchor == moved_model.target.anchor

        anchor = _point(view, 0, (resized.target.anchor.x, resized.target.anchor.y))
        replacement_anchor = _point(view, 0, (95, 105))
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=anchor)
        QTest.mouseMove(view.viewport(), replacement_anchor, delay=1); QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=replacement_anchor)
        wait_until(lambda: session.document_revision == 5)
        anchored = session.annotations[model.annotation_id.value]
        assert (anchored.target.anchor != resized.target.anchor
                and anchored.target.text_rect == resized.target.text_rect
                and anchored.target.knee is not None)

        view.set_layout(LayoutMode.DOUBLE)
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=_point(view, 0, (330, 190)))
        QTest.mouseMove(view.viewport(), _point(view, 1, (330, 220)), delay=1)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=_point(view, 1, (330, 220)))
        wait_until(lambda: session.document_revision == 6)
        transferred = session.annotations[model.annotation_id.value]
        assert transferred.page_index == 1 and transferred.annotation_id == model.annotation_id
        view.setFocus(); QTest.keyClick(view, Qt.Key.Key_Delete)
        wait_until(lambda: session.document_revision == 7)
        assert not session.annotations
    finally:
        close_window_discard(window)


def test_callout_shared_overflow_pending_rollback_and_screen_space_anchor_hit(
        tmp_path, app, pdf_fixture_copy):
    source = tmp_path / "m9-geometry.pdf"; pdf_fixture_copy(source, pages=6, geometry=True)
    window = MainWindow(); window.resize(1300, 900); window.show()
    try:
        session, view = _open(window, source)
        rich = RichText.plain_text("testo lungo " * 8, font_family="Courier", font_size=28,
                                   color=RgbColor(0, 0, 0))
        original = PdfRect(40, 80, 480, 390)
        requested = PdfRect(40, 80, 70, 105)
        clamped = view._clamp_resize_rect(original, requested, rich, _style())
        assert clamped == requested

        target = CalloutTarget(len(session.pages) - 1, PdfRect(160, 160, 390, 280),
                               PdfPoint(70, 90), PdfPoint(70, 200))
        for mode in (LayoutMode.SINGLE, LayoutMode.DOUBLE, LayoutMode.DOUBLE_COVER):
            view.set_layout(mode)
            for zoom in (.5, 1, 4, 8):
                view.session.ui.zoom = zoom
                mapped = view.coordinate_mapper(target.page_index).canonical_to_viewport(
                    (target.anchor.x, target.anchor.y))
                assert view._callout_anchor_hit(QPoint(*map(int, mapped)), target.page_index, target)

        # A failed/stale optimistic Callout creation restores the identical editor
        # state and never changes the confirmed document model.
        view.set_callout_enabled(True)
        for outcome, code in ((MutationOutcome.FAILED, MutationErrorCode.WORKER_FAILURE),
                              (MutationOutcome.STALE, MutationErrorCode.STALE_GENERATION)):
            command_id = CommandContext.create(session.document_id, session.document_generation).command_id
            view.pending_text_boxes[command_id] = _PendingFreeText(
                target, rich.plain, rich, _style(), None, 0.0, True)
            view.handle_annotation_result(MutationResult(
                command_id, session.document_id, session.document_generation, outcome,
                session.document_revision, session.document_revision, error=MutationError.create(code)))
            assert view._text_editor is not None and not session.annotations
            assert isinstance(view._editing_target, CalloutTarget)
            assert view._editing_target.anchor == target.anchor
            assert view._editing_target.text_rect.x0 == target.text_rect.x0
            assert view._editing_target.text_rect.x1 == target.text_rect.x1
            assert view._editing_target.text_rect.y1 >= target.text_rect.y1
            assert view.callout_enabled
            QTest.keyClick(view._text_editor, Qt.Key.Key_Escape)
    finally:
        close_window_discard(window)
