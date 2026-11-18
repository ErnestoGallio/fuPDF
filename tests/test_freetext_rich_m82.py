"""M8.2 rich FreeText persistence and editor-formatting regressions."""

import json
from pathlib import Path
import subprocess
import sys

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from pdf_annotator.app.document_manager import DocumentManager
from pdf_annotator.app.document_session import Lifecycle
from pdf_annotator.app.main_window import MainWindow
from pdf_annotator.domain.annotation_commands import CommandContext, CreateFreeText
from pdf_annotator.domain.geometry import PdfRect
from pdf_annotator.domain.rich_text import RichText, RichTextRun
from pdf_annotator.domain.serialization import (command_from_payload, command_to_payload,
                                                rich_text_from_payload, rich_text_to_payload)
from pdf_annotator.domain.styles import FreeTextStyle, RgbColor
from pdf_annotator.domain.targets import RectTarget
from pdf_annotator.worker import document_service
from .conftest import close_window_discard, wait_until
from .m8_helpers import persistence_worker


@pytest.fixture
def manager(app):
    instance = DocumentManager()
    yield instance
    instance.request_shutdown(discard_changes=True)
    wait_until(lambda: instance.is_shutdown)


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


def _inspect(path: Path) -> dict:
    result = subprocess.run([sys.executable, "-m", "tests.m8_helpers", str(path)],
                            cwd=Path(__file__).parents[1], capture_output=True,
                            text=True, check=True, timeout=30)
    return json.loads(result.stdout)


def _style() -> FreeTextStyle:
    return FreeTextStyle(RgbColor(.86, .12, .12), RgbColor(.79, .89, 1), RgbColor(0, 0, 0),
                         opacity=.8, border_width=1.5, font_size=12, font_family="Helvetica")


def _point(view, page_index: int, point: tuple[float, float]):
    mapped = view.coordinate_mapper(page_index).canonical_to_viewport(point)
    return QPointF(*mapped).toPoint()


def _rich() -> RichText:
    return RichText((
        RichTextRun("regular ", "Helvetica", 12, RgbColor(0, 0, 0)),
        RichTextRun("bold", "Helvetica", 12, RgbColor(.86, .12, .12), bold=True),
        RichTextRun(" italic", "Courier", 18, RgbColor(.08, .32, .86), italic=True),
        RichTextRun(" both", "Courier", 18, RgbColor(.08, .32, .86), bold=True, italic=True),
        RichTextRun("\nÈ & < >", "Helvetica", 9, RgbColor(.12, .58, .22)),
    ))


def test_rich_text_is_typed_canonical_and_serializable():
    rich = _rich()
    assert rich.plain == "regular bold italic both\nÈ & < >"
    assert rich_text_from_payload(rich_text_to_payload(rich)) == rich
    command = CreateFreeText(CommandContext.create("rich-doc", 1),
                             RectTarget(0, PdfRect(20, 20, 360, 180)), rich.plain, _style(),
                             rich_text=rich)
    restored = command_from_payload(command_to_payload(command))
    assert restored.rich_text == rich and restored.text == rich.plain


def test_rich_runs_persist_as_contents_and_escaped_rc(tmp_path, manager, monkeypatch, pdf_fixture_copy):
    source = tmp_path / "m82-rich.pdf"
    pdf_fixture_copy(source)
    monkeypatch.setattr(document_service, "document_worker_main", persistence_worker)
    manager.open_path(str(source))
    wait_until(lambda: any(session.lifecycle == Lifecycle.READY for session in manager.sessions.values()))
    session = next(iter(manager.sessions.values()))
    rich = _rich()
    command = CreateFreeText(CommandContext.create(session.document_id, session.document_generation),
                             RectTarget(0, PdfRect(30, 40, 420, 220)), rich.plain, _style(),
                             rich_text=rich)
    manager.submit_annotation(command)
    wait_until(lambda: session.document_revision == 1)
    assert session.annotations[command.annotation_id.value].rich_text == rich
    record = _inspect(Path(str(source) + ".m8-export.pdf"))["records"][0]
    assert record["contents"] == ["string", rich.plain]
    rc = record["rc"][1]
    assert "font-family:Helvetica" in rc and "font-family:'Courier Std'" in rc
    assert "font-weight:bold" in rc and "font-style:italic" in rc
    assert "#db1f1f" in rc and "#1452db" in rc
    assert "&amp;" in rc and "&lt;" in rc and "&gt;" in rc
    assert "/Helv 12" in record["da"][1] and "0.86 0.12 0.12 rg" in record["da"][1]
    assert 'xmlns="http://www.w3.org/1999/xhtml"' in rc


def test_selected_run_persists_without_changing_its_neighbours(
        tmp_path, manager, monkeypatch, pdf_fixture_copy):
    source = tmp_path / "m82-selected-run.pdf"
    pdf_fixture_copy(source)
    monkeypatch.setattr(document_service, "document_worker_main", persistence_worker)
    manager.open_path(str(source))
    wait_until(lambda: any(session.lifecycle == Lifecycle.READY for session in manager.sessions.values()))
    session = next(iter(manager.sessions.values()))
    rich = RichText((
        RichTextRun("AAA", "Helvetica", 12, RgbColor(0, 0, 0)),
        RichTextRun("BBB", "Courier", 20, RgbColor(.86, .12, .12), True, True),
        RichTextRun("CCC", "Helvetica", 12, RgbColor(0, 0, 0)),
    ))
    command = CreateFreeText(CommandContext.create(session.document_id, session.document_generation),
                             RectTarget(0, PdfRect(30, 40, 420, 190)), rich.plain, _style(),
                             rich_text=rich)
    manager.submit_annotation(command)
    wait_until(lambda: session.document_revision == 1)
    assert session.annotations[command.annotation_id.value].rich_text == rich
    record = _inspect(Path(str(source) + ".m8-export.pdf"))["records"][0]
    assert record["contents"] == ["string", "AAABBBCCC"]
    rc = record["rc"][1]
    # One body default plus the two Helvetica runs are preserved.
    assert rc.count("font-family:Helvetica") == 3
    assert ("font-family:'Courier Std';font-size:20pt;color:#db1f1f;"
            "font-weight:bold;font-style:italic") in rc
    assert "/Helv 12" in record["da"][1]


def test_rich_overflow_preserves_emphasis_and_run_font_size(
        tmp_path, manager, monkeypatch, pdf_fixture_copy):
    source = tmp_path / "m82-overflow.pdf"
    pdf_fixture_copy(source)
    monkeypatch.setattr(document_service, "document_worker_main", persistence_worker)
    manager.open_path(str(source))
    wait_until(lambda: any(session.lifecycle == Lifecycle.READY for session in manager.sessions.values()))
    session = next(iter(manager.sessions.values()))
    target = RectTarget(0, PdfRect(30, 40, 180, 88))
    regular = RichText.plain_text("una frase breve", font_family="Helvetica", font_size=6,
                                  color=RgbColor(0, 0, 0))
    manager.submit_annotation(CreateFreeText(CommandContext.create(session.document_id, 1), target,
                                             regular.plain, _style(), rich_text=regular))
    wait_until(lambda: session.document_revision == 1)
    overflowing = RichText((RichTextRun("una frase breve", "Courier", 42,
                                        RgbColor(0, 0, 0), bold=True),))
    manager.submit_annotation(CreateFreeText(CommandContext.create(session.document_id, 1), target,
                                             overflowing.plain, _style(), rich_text=overflowing))
    wait_until(lambda: session.document_revision == 2)
    model = list(session.annotations.values())[-1]
    assert model.target == target and model.rich_text == overflowing


def test_editor_formats_only_the_selected_runs_and_corner_hit_area(tmp_path, app, pdf_fixture_copy):
    source = tmp_path / "m82-editor.pdf"
    pdf_fixture_copy(source)
    window = MainWindow(); window.resize(1000, 760); window.show()
    try:
        _session, view = _open(window, source)
        target = RectTarget(0, PdfRect(40, 60, 300, 180))
        view._begin_text_box_editor(target, None, _style(), "alpha beta")
        editor = view._text_editor
        cursor = editor.textCursor(); cursor.setPosition(6); cursor.setPosition(10, QTextCursor.MoveMode.KeepAnchor)
        editor.setTextCursor(cursor)
        view.set_text_box_bold(True)
        view.set_text_box_font_family("Courier")
        view.set_text_box_font_size(18)
        view.set_text_box_text_color("blue")
        rich = view._editor_rich_text()
        assert rich.plain == "alpha beta"
        assert rich.runs[0].text == "alpha " and not rich.runs[0].bold
        selected = rich.runs[-1]
        assert selected.text == "beta" and selected.bold
        assert selected.font_family == "Courier" and selected.font_size == 18
        assert selected.color == RgbColor(.08, .32, .86)
        for zoom in (.5, 1.0, 1.5, 4.0, 8.0):
            # Hit testing is a pure widget-coordinate calculation. Updating
            # the session scale directly keeps this focused regression from
            # scheduling unrelated raster work at each test scale.
            view.session.ui.zoom = zoom
            display = view._display_rect(0, target.rect)
            corners = {"tl": display.topLeft(), "tr": display.topRight(),
                       "bl": display.bottomLeft(), "br": display.bottomRight()}
            for name, corner in corners.items():
                # The compact 16-DIP target remains usable at normal DPI,
                # while the former oversized 17-DIP outer zone is now move.
                assert view._resize_corner(QPointF(corner.x() + 12, corner.y()), 0, target.rect) == name
                assert view._resize_corner(QPointF(corner.x() + 17, corner.y()),
                                           0, target.rect) is None
                assert view._resize_corner(QPointF(corner.x() + 26, corner.y() + 26),
                                           0, target.rect) is None
        QTest.keyClick(editor, Qt.Key.Key_Escape)
    finally:
        close_window_discard(window)


def test_toolbar_focus_preserves_real_selected_range_and_insertion_format(
        tmp_path, app, pdf_fixture_copy):
    source = tmp_path / "m82-toolbar-selection.pdf"
    pdf_fixture_copy(source)
    window = MainWindow(); window.resize(1000, 760); window.show()
    try:
        _session, view = _open(window, source)
        target = RectTarget(0, PdfRect(40, 60, 360, 180))
        view._begin_text_box_editor(target, None, _style(), "AAABBBCCC")
        editor = view._text_editor

        # Mimic a menu click: focus leaves first, then the menu action runs.
        cursor = editor.textCursor(); cursor.setPosition(3); cursor.setPosition(6, QTextCursor.MoveMode.KeepAnchor)
        editor.setTextCursor(cursor)
        view.preserve_text_box_formatting_selection()
        editor.clearFocus()
        window._text_color_selected(window.text_color_actions["red"])
        assert view._text_editor is editor
        window._text_font_selected(window.text_font_actions["Courier"])
        window._text_size_selected(20)
        window._text_bold_toggled(True)
        window._text_italic_toggled(True)
        rich = view._editor_rich_text()
        assert [(run.text, run.font_family, run.font_size, run.color, run.bold, run.italic)
                for run in rich.runs] == [
                    ("AAA", "Helvetica", 12, RgbColor(0, 0, 0), False, False),
                    ("BBB", "Courier", 20, RgbColor(.86, .12, .12), True, True),
                    ("CCC", "Helvetica", 12, RgbColor(0, 0, 0), False, False),
                ]

        # A partial run keeps its untouched neighbours, then a cross-run
        # selection changes only the requested attribute on every covered run.
        cursor = editor.textCursor(); cursor.setPosition(4); cursor.setPosition(6, QTextCursor.MoveMode.KeepAnchor)
        editor.setTextCursor(cursor)
        view.set_text_box_text_color("blue")
        by_character = [(character, run.font_family, run.font_size, run.color, run.bold, run.italic)
                        for run in view._editor_rich_text().runs for character in run.text]
        assert [item[3] for item in by_character[3:9]] == [
            RgbColor(.86, .12, .12), RgbColor(.08, .32, .86), RgbColor(.08, .32, .86),
            RgbColor(0, 0, 0), RgbColor(0, 0, 0), RgbColor(0, 0, 0)]
        cursor = editor.textCursor(); cursor.setPosition(2); cursor.setPosition(7, QTextCursor.MoveMode.KeepAnchor)
        editor.setTextCursor(cursor)
        view.set_text_box_italic(False)
        by_character = [(character, run.font_family, run.font_size, run.color, run.bold, run.italic)
                        for run in view._editor_rich_text().runs for character in run.text]
        assert "".join(item[0] for item in by_character) == "AAABBBCCC"
        assert all(not item[5] for item in by_character[2:7])
        assert all(item[1] == "Courier" and item[2] == 20 and item[4]
                   for item in by_character[3:6])

        cursor = editor.textCursor(); cursor.setPosition(9); editor.setTextCursor(cursor)
        view.set_text_box_bold(True)
        editor.insertPlainText("D")
        rich = view._editor_rich_text()
        assert rich.plain == "AAABBBCCCD"
        assert rich.runs[-1].text == "D" and rich.runs[-1].bold
        assert rich.runs[-2].text == "CCC" and not rich.runs[-2].bold
        QTest.keyClick(editor, Qt.Key.Key_Escape)
    finally:
        close_window_discard(window)


def test_size_spinbox_changes_only_selected_range_and_future_insertion(
        tmp_path, app, pdf_fixture_copy):
    source = tmp_path / "m82-size-selection.pdf"
    pdf_fixture_copy(source)
    window = MainWindow(); window.resize(1000, 760); window.show()
    try:
        _session, view = _open(window, source)
        target = RectTarget(0, PdfRect(40, 60, 360, 180))
        view._begin_text_box_editor(target, None, _style(), "AAA BBB CCC")
        editor = view._text_editor
        cursor = editor.textCursor(); cursor.setPosition(4); cursor.setPosition(7, QTextCursor.MoveMode.KeepAnchor)
        editor.setTextCursor(cursor)
        # The spinbox path is the focus-stealing control reported manually.
        view.preserve_text_box_formatting_selection()
        editor.clearFocus()
        window._text_size_selected(24)
        assert view._text_editor is editor
        runs = view._editor_rich_text().runs
        assert [(run.text, run.font_size) for run in runs] == [
            ("AAA ", 12), ("BBB", 24), (" CCC", 12)]

        cursor = editor.textCursor(); cursor.setPosition(len("AAA BBB CCC")); editor.setTextCursor(cursor)
        view.set_text_box_font_size(18)
        editor.insertPlainText("D")
        runs = view._editor_rich_text().runs
        assert runs[-1].text == "D" and runs[-1].font_size == 18
        assert all(run.font_size != 18 for run in runs[:-1])
        QTest.keyClick(editor, Qt.Key.Key_Escape)
    finally:
        close_window_discard(window)


def test_global_format_delta_preserves_per_run_attributes_and_pdf_rc(
        tmp_path, app, pdf_fixture_copy):
    source = tmp_path / "m82-global-delta.pdf"
    pdf_fixture_copy(source)
    window = MainWindow(); window.resize(1000, 760); window.show()
    try:
        session, view = _open(window, source)
        rich = RichText((
            RichTextRun("AAA", "Helvetica", 12, RgbColor(0, 0, 0)),
            RichTextRun("BBB", "Courier", 12, RgbColor(.86, .12, .12), bold=True),
            RichTextRun("CCC", "Helvetica", 12, RgbColor(.08, .32, .86), italic=True),
        ))
        target = RectTarget(0, PdfRect(35, 45, 420, 190))
        view._begin_text_box_editor(target, None, _style(), rich.plain, rich)
        assert view._confirm_text_box_editor()
        wait_until(lambda: session.document_revision == 1)
        annotation_id = next(iter(session.annotations))
        view._set_selected_text_box(annotation_id)

        view.set_text_box_font_size(20)
        wait_until(lambda: session.document_revision == 2)
        updated = session.annotations[annotation_id].rich_text
        assert [(run.font_family, run.font_size, run.color, run.bold, run.italic)
                for run in updated.runs] == [
                    ("Helvetica", 20, RgbColor(0, 0, 0), False, False),
                    ("Courier", 20, RgbColor(.86, .12, .12), True, False),
                    ("Helvetica", 20, RgbColor(.08, .32, .86), False, True),
                ]
        view.set_text_box_font_family("Courier")
        wait_until(lambda: session.document_revision == 3)
        view.set_text_box_text_color("green")
        wait_until(lambda: session.document_revision == 4)
        updated = session.annotations[annotation_id].rich_text
        assert [(run.font_family, run.font_size, run.color, run.bold, run.italic)
                for run in updated.runs] == [
                    ("Courier", 20, RgbColor(.12, .58, .22), False, False),
                    ("Courier", 20, RgbColor(.12, .58, .22), True, False),
                    ("Courier", 20, RgbColor(.12, .58, .22), False, True),
                ]
    finally:
        close_window_discard(window)


def test_editor_auto_grows_on_paste_and_rich_format_without_shrinking(tmp_path, app, pdf_fixture_copy):
    source = tmp_path / "m82-autogrow.pdf"; pdf_fixture_copy(source)
    window = MainWindow(); window.resize(1100, 800); window.show()
    try:
        session, view = _open(window, source)
        target = RectTarget(0, PdfRect(40, 60, 100, 95))
        view._begin_text_box_editor(target, None, _style())
        editor = view._text_editor
        QApplication.clipboard().setText("a " * 20)
        editor.paste()
        after_paste = view._editing_target.rect
        assert after_paste.x0 == target.rect.x0 and after_paste.x1 == target.rect.x1
        assert after_paste.y1 > target.rect.y1

        # A single partial rich-text edit needs a wider box; the untouched
        # text remains in its original runs and the same editor retains focus.
        cursor = editor.textCursor(); cursor.setPosition(0); cursor.setPosition(1, QTextCursor.MoveMode.KeepAnchor)
        editor.setTextCursor(cursor)
        view.set_text_box_font_size(72); view.set_text_box_bold(True); view.set_text_box_italic(True)
        after_format = view._editing_target.rect
        assert after_format.y1 >= after_paste.y1 and editor.hasFocus()
        assert view._editor_rich_text().runs[0].font_size == 72

        editor.selectAll()
        view.set_text_box_font_size(12); view.set_text_box_bold(False); view.set_text_box_italic(False)
        assert view._editing_target.rect.y1 >= after_format.y1
        editor.setPlainText("a " * 20)
        assert view._editing_target.rect.y1 >= after_format.y1
        committed_rect = view._editing_target.rect
        assert view._confirm_text_box_editor(); wait_until(lambda: session.document_revision == 1)
        model = next(iter(session.annotations.values()))
        assert model.target.rect == committed_rect and model.text == "a " * 20
    finally:
        close_window_discard(window)


def test_resize_clamps_each_corner_and_commits_the_preview_once(tmp_path, app, pdf_fixture_copy):
    source = tmp_path / "m82-resize-clamp.pdf"
    pdf_fixture_copy(source)
    window = MainWindow(); window.resize(1100, 800); window.show()
    try:
        session, view = _open(window, source)
        rich = RichText((
            RichTextRun("wrapping text ", "Helvetica", 16, RgbColor(0, 0, 0)),
            RichTextRun("COURIER", "Courier", 28, RgbColor(.86, .12, .12), True, True),
            RichTextRun("\nmultiline text", "Helvetica", 12, RgbColor(0, 0, 0)),
        ))
        target = RectTarget(0, PdfRect(35, 45, 470, 300))
        view._begin_text_box_editor(target, None, _style(), rich.plain, rich)
        assert view._confirm_text_box_editor()
        wait_until(lambda: session.document_revision == 1)
        annotation_id = next(iter(session.annotations))
        before = session.annotations[annotation_id].target.rect
        for corner in ("tl", "tr", "bl", "br"):
            requested = PdfRect(
                before.x1 - 12 if "l" in corner else before.x0,
                before.y1 - 12 if "t" in corner else before.y0,
                before.x1 if "l" in corner else before.x0 + 12,
                before.y1 if "t" in corner else before.y0 + 12)
            clamped = view._clamp_resize_rect(before, requested, rich, _style())
            assert clamped == requested
            if "l" in corner:
                assert clamped.x1 == pytest.approx(before.x1)
            else:
                assert clamped.x0 == pytest.approx(before.x0)
            if "t" in corner:
                assert clamped.y1 == pytest.approx(before.y1)
            else:
                assert clamped.y0 == pytest.approx(before.y0)

        # The live bottom-right gesture previews and commits the exact geometry.
        display = view._display_rect(0, before)
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=display.bottomRight().toPoint())
        requested_point = _point(view, 0, (before.x0 + 2, before.y0 + 2))
        QTest.mouseMove(view.viewport(), requested_point, delay=2)
        move = view._text_box_move
        assert move is not None and move.rect is not None and move.rect != before
        assert session.annotations[annotation_id].target.rect == before  # local preview, no flash/commit
        preview = move.rect
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=requested_point)
        wait_until(lambda: session.document_revision == 2)
        assert session.annotations[annotation_id].target.rect == preview

    finally:
        close_window_discard(window)
