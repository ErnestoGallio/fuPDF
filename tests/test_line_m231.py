"""M23.1 uses the production external command/history/save path, offline."""

from dataclasses import replace
import json
import math
import shutil
import subprocess
import sys

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtTest import QTest

from pdf_annotator.app.document_manager import DocumentManager
from pdf_annotator.app.main_window import MainWindow
from pdf_annotator.domain.annotation_commands import (CommandContext, DeleteExternalAnnotation, Undo, Redo)
from pdf_annotator.domain.annotation_inventory import external_command_annotation_id
from pdf_annotator.domain.external_edits import EditExternalAnnotation, ExternalEditKind
from pdf_annotator.domain.serialization import command_to_payload, result_from_payload
from pdf_annotator.viewer.layout_engine import LayoutMode
from pdf_annotator.worker.annotation_inventory import summary_from_payload, summary_to_payload
from pdf_annotator.worker.protocol import Operation
from tests.conftest import close_window_discard, request, wait_until
from tests.test_freetext_viewer_m8 import _open, _point
from tests.test_m6_service_persistence import _opened
from tests.test_save_m15 import save, submit


def helper(operation, path):
    result = subprocess.run([sys.executable, "-m", "tests.m231_helpers", operation, str(path)],
                            capture_output=True, check=True, timeout=40)
    return json.loads(result.stdout) if result.stdout else None


@pytest.fixture(scope="module")
def line_source(tmp_path_factory):
    path = tmp_path_factory.mktemp("m231") / "native-lines.pdf"
    helper("fixture", path)
    return path


@pytest.fixture
def line_session(app, tmp_path, line_source):
    path = tmp_path / "lines.pdf"
    shutil.copyfile(line_source, path)
    manager = DocumentManager()
    session = _opened(manager, path)
    manager.start_annotation_inventory(session.document_id)
    wait_until(lambda: session.inventory_complete)
    try:
        yield manager, session, path
    finally:
        wait_until(lambda: not session.saving, timeout=60)
        manager.request_shutdown(discard_changes=True)
        wait_until(lambda: manager.is_shutdown)


def summary_for(session, name="diagonal", page=0):
    return next(row for row in session.annotation_inventory.values()
                if row.page_index == page and row.native_name == name)


def row_for(snapshot, name="diagonal", page=0):
    return next(row for row in snapshot["rows"] if row["page"] == page and row["name"] == name)


def move(session, summary, delta=(12, 8)):
    return EditExternalAnnotation(CommandContext.create(session.document_id, session.document_generation),
        external_command_annotation_id(summary.logical_id), summary.logical_id, summary.page_index,
        ExternalEditKind.MOVE, delta=delta)


def test_recognition_properties_and_serialization(line_session):
    _manager, session, _path = line_session
    assert len(session.annotation_inventory) == 28  # 6 Lines + Ink on each page; no content line.
    for page in range(4):
        summary = summary_for(session, page=page)
        assert summary.kind == "line" and summary.subtype == "Line"
        assert [(p.x, p.y) for p in summary.geometry.line] == [(70, 110), (250, 200)]
        assert not summary.geometry.paths and not summary.geometry.callout_line
        assert summary.style["stroke"] == pytest.approx([.1, .3, .8])
        assert summary.style["border"]["width"] == 2
        assert summary.style["line_ends"] == [1, 5]
        assert summary.author == "Autore esterno" and "è intatta" in summary.preview
        assert summary.created == "D:20200101000000Z"
        assert summary.capabilities.movable and summary.capabilities.deletable
        assert not summary.capabilities.resizable and not summary.capabilities.movable_anchor
        assert not summary.capabilities.editable_style and not summary.capabilities.editable_text
        assert summary_from_payload(summary_to_payload(summary)) == summary
        assert not summary_for(session, "locked", page).capabilities.movable
        assert summary_for(session, "ink", page).kind == "ink"


@pytest.mark.parametrize("page", range(4))
@pytest.mark.parametrize("name", ["diagonal", "horizontal", "vertical", "reverse", "no-ap"])
def test_move_history_save_reopen_preserves_native_line(line_session, tmp_path, page, name):
    manager, session, path = line_session
    before = helper("inspect", path)
    initial = summary_for(session, name, page)
    original = row_for(before, name, page)
    command = move(session, initial)
    assert submit(manager, command).document_revision == 1
    changed = summary_for(session, name, page)
    for a, b in zip(initial.geometry.line, changed.geometry.line, strict=True):
        assert (b.x - a.x, b.y - a.y) == pytest.approx((12, 8), abs=.001)
    a, b = initial.geometry.line
    c, d = changed.geometry.line
    assert (d.x-c.x, d.y-c.y) == pytest.approx((b.x-a.x, b.y-a.y), abs=.001)
    assert math.dist((c.x, c.y), (d.x, d.y)) == pytest.approx(math.dist((a.x, a.y), (b.x, b.y)))
    assert changed.style == initial.style and changed.author == initial.author
    assert submit(manager, Undo(CommandContext.create(session.document_id, session.document_generation))).success
    assert summary_for(session, name, page) == initial
    assert submit(manager, Redo(CommandContext.create(session.document_id, session.document_generation))).success
    destination = tmp_path / "moved.pdf"
    save(manager, session, destination)
    after = helper("inspect", destination)
    persisted = row_for(after, name, page)
    changed_keys = {key for key in original["keys"].keys() | persisted["keys"].keys()
                    if original["keys"].get(key) != persisted["keys"].get(key)}
    assert changed_keys == {"L", "Rect", "M"}
    assert persisted["subtype"] == "Line" and persisted["ap"] == original["ap"]
    for actual, expected in zip(persisted["vertices"], original["vertices"], strict=True):
        assert actual == pytest.approx([expected[0]+12, expected[1]+8], abs=.001)
    assert after["text"] == before["text"]
    # Every pre-existing stream, including arbitrary native AP and page content,
    # is unchanged. All other annotations, Ink included, retain their dictionary.
    for xref, data in before["objects"].items():
        assert after["objects"][xref]["stream"] == data["stream"]
    for row in before["rows"]:
        if row["xref"] != original["xref"]:
            assert row_for(after, row["name"], row["page"]) == row
    # History survives the Save As generation barrier, then incremental Save.
    submit(manager, Undo(CommandContext.create(session.document_id, session.document_generation)))
    save(manager, session)
    restored = row_for(helper("inspect", destination), name, page)
    assert restored["keys"] == original["keys"]


def test_delete_undo_redo_save_and_save_as(line_session, tmp_path):
    manager, session, path = line_session
    before = helper("inspect", path)
    summary = summary_for(session)
    delete = DeleteExternalAnnotation(CommandContext.create(session.document_id, session.document_generation),
        external_command_annotation_id(summary.logical_id), summary.logical_id, summary.page_index)
    submit(manager, delete)
    assert summary.logical_id not in session.annotation_inventory
    submit(manager, Undo(CommandContext.create(session.document_id, session.document_generation)))
    assert summary_for(session) == summary
    submit(manager, Redo(CommandContext.create(session.document_id, session.document_generation)))
    destination = tmp_path / "deleted.pdf"
    save(manager, session, destination)
    after = helper("inspect", destination)
    assert not any(row["page"] == 0 and row["name"] == "diagonal" for row in after["rows"])
    # Popup belongs to the deleted graph; Undo restores both exactly.
    submit(manager, Undo(CommandContext.create(session.document_id, session.document_generation)))
    save(manager, session)
    assert row_for(helper("inspect", destination))["keys"] == row_for(before)["keys"]
    submit(manager, Redo(CommandContext.create(session.document_id, session.document_generation)))
    save(manager, session)
    assert not any(row["page"] == 0 and row["name"] == "diagonal"
                   for row in helper("inspect", destination)["rows"])


def test_native_appearance_translation_and_missing_ap_are_read_only(line_source):
    results = helper("appearance", line_source)
    for result in results:
        assert result["visible"] and result["read_only_render"]
        # Integral displacement at scale 2 retains the complete native raster.
        assert result["same_pixels"]
        assert result["before"]["byte_count"] <= 16 * 1024 * 1024
        assert result["bounded_bytes"] <= 64 * 1024
        baseline = next(row for row in results if row["page"] == 0 and row["name"] == result["name"])
        assert result["before"]["coverage"] == pytest.approx(baseline["before"]["coverage"])
        assert result["after"]["coverage"] == pytest.approx(
            [value + (12 if index % 2 == 0 else 8)
             for index, value in enumerate(result["before"]["coverage"])])


@pytest.mark.parametrize("page,zoom,layout", [
    (0, 1.0, LayoutMode.SINGLE), (1, 1.5, LayoutMode.DOUBLE),
    (2, 4.0, LayoutMode.DOUBLE_COVER), (3, 8.0, LayoutMode.SINGLE)])
def test_selection_drag_cancel_and_delete_ui(app, line_source, tmp_path, page, zoom, layout):
    path = tmp_path / "ui-lines.pdf"
    shutil.copyfile(line_source, path)
    window = MainWindow()
    window.resize(1100, 900)
    window.show()
    try:
        session, view = _open(window, path)
        wait_until(lambda: session.inventory_complete)
        view.set_layout(layout)
        view.set_zoom(zoom)
        summary = summary_for(session, "horizontal", page)
        view.select_inventory_annotation(summary.logical_id, reveal=True)
        wait_until(lambda: view.scheduler.settled)
        wait_until(lambda: view._has_current_native_appearance(summary))
        item = next(window.annotation_sidebar.list.item(index)
                    for index in range(window.annotation_sidebar.list.count())
                    if window.annotation_sidebar.list.item(index).data(Qt.ItemDataRole.UserRole) == summary.logical_id)
        assert item.text() == f"Linea · p. {page+1}"
        window.annotation_sidebar.list.setCurrentItem(item)
        wait_until(lambda: summary.logical_id in session.annotation_details)
        assert "Estremi (pt): (70, 280) → (250, 280)" in window.annotation_sidebar.detail.toPlainText()
        assert "Terminazioni: Quadrato → Freccia chiusa" in window.annotation_sidebar.detail.toPlainText()

        def paint_frame():
            image = QImage(view.viewport().size(), QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            try:
                view.external_interaction.paint(painter, view.coordinate_mapper(page), summary, True)
            finally:
                painter.end()
            return view._last_native_appearance_paint

        idle_frame = paint_frame()
        assert idle_frame.annotation_id == summary.logical_id
        middle = (160, 280)
        start = _point(view, page, middle)
        near = _point(view, page, (middle[0], middle[1]+4/zoom))
        far = _point(view, page, (middle[0], middle[1]+15/zoom))
        assert view._hit_test_inventory(QPointF(near), page) == summary.logical_id
        assert view._hit_test_inventory(QPointF(far), page) != summary.logical_id
        diagonal = summary_for(session, page=page)
        away = _point(view, page, (90, 190))  # inside diagonal Rect, away from segment
        assert view._hit_test_inventory(QPointF(away), page) != diagonal.logical_id
        end = _point(view, page, (168, 286))
        count = session.worker.submitted_counts[Operation.ANNOTATION_COMMAND]
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(view.viewport(), end, delay=2)
        assert view.selected_inventory_id == summary.logical_id
        assert view.external_interaction.gesture is not None
        assert session.worker.submitted_counts[Operation.ANNOTATION_COMMAND] == count
        dragged_frame = paint_frame()
        dx, dy = view.external_interaction.gesture.command.delta
        mapper = view.coordinate_mapper(page)
        origin, shifted = mapper.canonical_to_viewport((0, 0)), mapper.canonical_to_viewport((dx, dy))
        assert dragged_frame.pixel_size == idle_frame.pixel_size
        assert dragged_frame.source_key == idle_frame.source_key
        assert dragged_frame.destination == pytest.approx((
            idle_frame.destination[0] + shifted[0] - origin[0],
            idle_frame.destination[1] + shifted[1] - origin[1], *idle_frame.destination[2:]))
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        wait_until(lambda: session.document_revision == 1)
        assert session.worker.submitted_counts[Operation.ANNOTATION_COMMAND] == count + 1
        moved = summary_for(session, "horizontal", page)
        for a, b in zip(summary.geometry.line, moved.geometry.line, strict=True):
            assert (b.x-a.x, b.y-a.y) == pytest.approx((8, 6), abs=1/zoom)
        assert not moved.capabilities.resizable and not moved.capabilities.movable_anchor
        # Returning to the press position is a no-op, not a phantom history step.
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        QTest.mouseMove(view.viewport(), start, delay=2)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        assert session.worker.submitted_counts[Operation.ANNOTATION_COMMAND] == count + 1
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        QTest.mouseMove(view.viewport(), start, delay=2)
        QTest.keyClick(view, Qt.Key.Key_Escape)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        assert view.external_interaction.gesture is None
        assert session.worker.submitted_counts[Operation.ANNOTATION_COMMAND] == count + 1
        QTest.keyClick(view, Qt.Key.Key_Delete)
        wait_until(lambda: summary.logical_id not in session.annotation_inventory)
        assert session.document_revision == 2
        assert window.annotation_sidebar.list.count() == 27
    finally:
        close_window_discard(window)


def test_stale_duplicate_locked_and_post_write_failure_are_atomic(app, line_source, tmp_path, monkeypatch):
    from pdf_annotator.worker import document_service
    from tests.m231_helpers import failing_line_worker

    monkeypatch.setattr(document_service, "document_worker_main", failing_line_worker)
    path = tmp_path / "rollback.pdf"
    shutil.copyfile(line_source, path)
    manager = DocumentManager()
    try:
        session = _opened(manager, path)
        manager.start_annotation_inventory(session.document_id)
        wait_until(lambda: session.inventory_complete)
        initial = summary_for(session)
        command = move(session, initial)
        results = []
        manager.annotation_result.connect(results.append)
        manager.submit_annotation(command)
        wait_until(lambda: bool(results))
        assert not results[-1].success and session.document_revision == 0
        assert summary_for(session) == initial
        save(manager, session)
        assert row_for(helper("inspect", path))["vertices"] == [[70, 110], [250, 200]]
        wait_until(lambda: session.inventory_complete)
        tiny = move(session, initial, (1e-8, 0))
        manager.submit_annotation(tiny)
        wait_until(lambda: any(result.command_id == tiny.context.command_id for result in results))
        assert not results[-1].success and session.document_revision == 0
        assert summary_for(session) == initial
        command = move(session, initial)
        submit(manager, command)
        revision = session.document_revision
        for rejected in (command, replace(command, context=replace(command.context,
                document_generation=session.document_generation+1))):
            # Direct IPC exercises the worker's authoritative duplicate/stale guards.
            result = result_from_payload(request(session.worker, Operation.ANNOTATION_COMMAND,
                                                   command_to_payload(rejected)).payload)
            assert not result.success and result.document_revision == revision
        locked = move(session, summary_for(session, "locked"))
        assert manager.submit_annotation(locked).result.error.code.value == "read_only_annotation"
        assert session.document_revision == revision
    finally:
        manager.request_shutdown(discard_changes=True)
        wait_until(lambda: manager.is_shutdown)
