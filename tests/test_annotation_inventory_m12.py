"""Worker-side M12 inventory tests using real native PDF annotation objects."""

from pathlib import Path
import subprocess
import sys

import pytest

from PySide6.QtCore import Qt
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QTest
from pdf_annotator.util.paths import identify_file
from pdf_annotator.app.main_window import MainWindow
from pdf_annotator.domain.annotation_commands import (CommandContext,
                                                       DeleteExternalAnnotation)
from pdf_annotator.domain.annotation_inventory import external_command_annotation_id
from pdf_annotator.domain.results import MutationOutcome
from pdf_annotator.domain.serialization import command_to_payload, result_from_payload
from pdf_annotator.worker.client import RasterBudget, WorkerClient
from pdf_annotator.worker.document_service import DocumentSource
from pdf_annotator.worker.protocol import Operation
from tests.conftest import receive, wait_until


def _request(client, operation, payload):
    request_id = client.submit(operation, payload)
    assert request_id is not None
    with receive(client) as delivery:
        assert delivery.response is not None
        assert delivery.response.request_id == request_id
        assert delivery.response.success, delivery.response.error
        return delivery.response.payload


def _fixture(operation: str, path: Path) -> None:
    """Keep native-PDF setup outside the process that represents the GUI."""
    subprocess.run([sys.executable, "-m", "tests.m12_fixtures", operation, str(path)],
                   check=True, timeout=30, capture_output=True)


def _native_fixture(path: Path) -> None:
    _fixture("native", path)


def _appearance_fixture(path: Path) -> None:
    _fixture("appearance", path)


def _client(path: Path) -> WorkerClient:
    return WorkerClient("m12-test", DocumentSource(identify_file(str(path))), RasterBudget())


def _open(client: WorkerClient) -> None:
    _request(client, Operation.OPEN_DOCUMENT, {})


def _delete_external(client: WorkerClient, summary: dict) -> object:
    command = DeleteExternalAnnotation(
        CommandContext.create(client.document_id, client.generation),
        external_command_annotation_id(summary["logical_id"]), summary["logical_id"],
        summary["page_index"])
    return result_from_payload(_request(client, Operation.ANNOTATION_COMMAND,
                                        command_to_payload(command)))


def test_native_inventory_is_paginated_lossless_and_safely_deletable(tmp_path, app):
    source = tmp_path / "native-inventory.pdf"
    _native_fixture(source)
    client = _client(source)
    try:
        _open(client)
        first = _request(client, Operation.ANNOTATION_SUMMARIES, {"cursor": 0})
        summaries = first["summaries"]
        kinds = {item["kind"] for item in summaries}
        assert {"highlight", "ink", "text_box", "callout", "sticky_note", "underline"} <= kinds
        assert first["complete"] is True
        assert all(item["capabilities"]["selectable"] for item in summaries)
        assert all(item["capabilities"]["deletable"] for item in summaries)
        assert len(next(item for item in summaries if item["kind"] == "ink")["geometry"]["paths"]) == 2
        assert next(item for item in summaries if item["kind"] == "highlight")["geometry"]["quads"]
        assert all(next(item for item in summaries if item["kind"] == kind)["style"]["opacity"] == 1.0
                   for kind in ("highlight", "ink", "text_box", "callout"))
        textbox = next(item for item in summaries if item["kind"] == "text_box")
        parts, offset, chunks = [], 0, 0
        while True:
            response = _request(client, Operation.ANNOTATION_DETAIL,
                                {"logical_id": textbox["logical_id"], "offset": offset})
            parts.append(response["contents"])
            chunks += 1
            offset = response["next_offset"]
            if response["complete"]:
                break
        assert "città 日本語" in "".join(parts)
        assert len("".join(parts)) > len(textbox["preview"])
        assert chunks > 1
    finally:
        client.close_async()
        wait_until(lambda: client.stopped.is_set())


def test_external_supported_items_delete_via_worker_and_update_inventory(tmp_path, app):
    source = tmp_path / "native-delete.pdf"
    _native_fixture(source)
    client = _client(source)
    try:
        _open(client)
        payload = _request(client, Operation.ANNOTATION_SUMMARIES, {"cursor": 0})
        for kind in ("text_box", "callout", "ink", "highlight", "sticky_note", "underline"):
            summary = next(item for item in payload["summaries"] if item["kind"] == kind)
            result = _delete_external(client, summary)
            assert result.success and result.outcome == MutationOutcome.SUCCESS
            refreshed = _request(client, Operation.ANNOTATION_SUMMARIES, {"cursor": 0})
            assert summary["logical_id"] not in {item["logical_id"] for item in refreshed["summaries"]}
            payload = refreshed
    finally:
        client.close_async()
        wait_until(lambda: client.stopped.is_set())


def test_external_freetext_appearance_is_bounded_rgba_and_rd_keeps_the_true_text_box(tmp_path, app):
    source = tmp_path / "external-appearance.pdf"
    _appearance_fixture(source)
    client = _client(source)
    try:
        _open(client)
        summaries = _request(client, Operation.ANNOTATION_SUMMARIES, {"cursor": 0})["summaries"]
        callout = next(item for item in summaries if item["kind"] == "callout")
        assert callout["appearance_available"] is True
        # In canonical coordinates: left/right come directly from /RD, while
        # PDF bottom/top map to canonical top/bottom in reverse order.
        bbox = callout["geometry"]["bbox"]
        assert callout["geometry"]["text_rect"] == pytest.approx(
            [bbox[0] + 2, bbox[1] + 4, bbox[2] - 3, bbox[3] - 80])
        request_id = client.submit(Operation.ANNOTATION_APPEARANCE, {
            "logical_id": callout["logical_id"], "page_index": callout["page_index"],
            "render_scale": 1.0, "max_bytes": 1024 * 1024})
        assert request_id is not None
        with receive(client) as delivery:
            response = delivery.response
            assert response is not None and response.success, response.error if response else None
            assert response.operation == Operation.ANNOTATION_APPEARANCE
            assert response.payload["format"] == "RGBA8888" and response.payload["alpha"] is True
            assert response.payload["byte_count"] == len(response.pixels) <= 1024 * 1024
            assert response.payload["logical_id"] == callout["logical_id"]
    finally:
        client.close_async()
        wait_until(lambda: client.stopped.is_set())


def test_external_replace_is_one_logical_record(app, external_editorials):
    from pdf_annotator.app.annotation_sidebar import AnnotationSidebar, _AUTHOR
    from pdf_annotator.worker.annotation_inventory import summary_from_payload

    client = _client(external_editorials)
    try:
        _open(client)
        payload = _request(client, Operation.ANNOTATION_SUMMARIES, {"cursor": 0})
        replacements = [item for item in payload["summaries"] if item["kind"] == "replace_text"]
        assert {"delete_text", "insert_text", "replace_text"} <= {
            item["kind"] for item in payload["summaries"]}
        assert len(replacements) == 1
        summary = summary_from_payload(replacements[0])
        assert summary.author
        assert AnnotationSidebar._make_item(summary).data(_AUTHOR) == summary.author
        assert replacements[0]["capabilities"]["logical_group"] is True
        assert len(replacements[0]["component_xrefs"]) >= 2
        detail = _request(client, Operation.ANNOTATION_DETAIL,
                          {"logical_id": replacements[0]["logical_id"], "offset": 0})
        assert detail["contents"] == "sleepy"
    finally:
        client.close_async()
        wait_until(lambda: client.stopped.is_set())


def test_external_replace_delete_is_atomic_and_keeps_no_child_record(app, external_editorials):
    client = _client(external_editorials)
    try:
        _open(client)
        before = _request(client, Operation.ANNOTATION_SUMMARIES, {"cursor": 0})
        replacement = next(item for item in before["summaries"] if item["kind"] == "replace_text")
        assert replacement["capabilities"]["deletable"] is True
        assert _delete_external(client, replacement).success
        after = _request(client, Operation.ANNOTATION_SUMMARIES, {"cursor": 0})
        assert replacement["logical_id"] not in {item["logical_id"] for item in after["summaries"]}
        assert not any(item["kind"] == "replace_text" for item in after["summaries"])
    finally:
        client.close_async()
        wait_until(lambda: client.stopped.is_set())


def test_external_delete_failure_rolls_back_the_complete_annotation_graph(tmp_path):
    """A worker-side write error cannot leave a half-deleted external item."""
    source = tmp_path / "rollback.pdf"
    _fixture("rollback", source)


def test_many_native_annotations_are_split_across_summary_frames(tmp_path, app):
    source = tmp_path / "many-native.pdf"
    _fixture("many", source)
    client = _client(source)
    try:
        _open(client)
        first = _request(client, Operation.ANNOTATION_SUMMARIES, {"cursor": 0})
        assert len(first["summaries"]) == 96 and first["complete"] is False
        second = _request(client, Operation.ANNOTATION_SUMMARIES,
                          {"cursor": first["next_cursor"]})
        assert len(second["summaries"]) == 14 and second["complete"] is True
    finally:
        client.close_async()
        wait_until(lambda: client.stopped.is_set())


def test_multi_page_native_annotations_do_not_orphan_the_inventory(tmp_path, app):
    """Sanitized regression for an Annot becoming orphaned after page switches."""
    source = tmp_path / "cross-page-native.pdf"
    _fixture("cross-page", source)
    client = _client(source)
    try:
        _open(client)
        response = _request(client, Operation.ANNOTATION_SUMMARIES, {"cursor": 0})
        assert len(response["summaries"]) == 6
        assert {item["page_index"] for item in response["summaries"]} == {0, 1, 2}
    finally:
        client.close_async()
        wait_until(lambda: client.stopped.is_set())


def test_sidebar_and_viewer_share_external_selection(tmp_path, app):
    source = tmp_path / "native-sidebar.pdf"
    _native_fixture(source)
    window = MainWindow()
    window.resize(900, 700)
    window.show()
    try:
        window.open_paths([str(source)])
        wait_until(lambda: bool(window.manager.sessions))
        session = next(iter(window.manager.sessions.values()))
        wait_until(lambda: session.inventory_complete)
        view = window._widgets[session.document_id]
        assert window.annotation_sidebar.list.count() == len(session.annotation_inventory)
        item = window.annotation_sidebar.list.item(0)
        logical_id = item.data(256)  # Qt.UserRole
        window.annotation_sidebar.list.setCurrentItem(item)
        wait_until(lambda: view.selected_inventory_id == logical_id)
        wait_until(lambda: logical_id in session.annotation_details)
        assert window.annotation_sidebar.detail.toPlainText()
        window.annotation_sidebar.show()
        QTest.keyClick(window.annotation_sidebar.list, Qt.Key.Key_Return)
        assert window.annotation_sidebar.detail.isVisible()
        commands = session.worker.submitted_counts[Operation.ANNOTATION_COMMAND]
        view.setFocus()
        QTest.keyClick(view, Qt.Key.Key_Delete)
        wait_until(lambda: logical_id not in session.annotation_inventory)
        assert session.worker.submitted_counts[Operation.ANNOTATION_COMMAND] == commands + 1
        assert session.dirty and session.selected_annotation_id is None
        assert not window.annotation_sidebar.detail.isVisible()
    finally:
        window.manager.request_shutdown(discard_changes=True)
        window.close()
        wait_until(lambda: window.manager.is_shutdown)


def _has_overlay_pixels(view, summary, *, selected: bool) -> bool:
    image = QImage(view.viewport().size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    try:
        view._paint_inventory_annotation(painter, view.coordinate_mapper(summary.page_index),
                                         summary, selected)
    finally:
        painter.end()
    return any(image.pixelColor(x, y).alpha() for y in range(image.height())
               for x in range(image.width()))


def test_external_supported_overlays_are_visible_before_selection(tmp_path, app):
    source = tmp_path / "native-overlay.pdf"
    _native_fixture(source)
    window = MainWindow(); window.resize(900, 700); window.show()
    try:
        window.open_paths([str(source)])
        wait_until(lambda: bool(window.manager.sessions))
        session = next(iter(window.manager.sessions.values()))
        wait_until(lambda: session.inventory_complete)
        view = window._widgets[session.document_id]
        for kind in ("text_box", "callout", "ink", "highlight", "underline"):
            summary = next(item for item in session.annotation_inventory.values() if item.kind == kind)
            if kind in ("text_box", "callout"):
                # External FreeText/Callout uses its native AP only. The
                # inventory arrives before the asynchronous AP raster.
                wait_until(lambda: view._has_current_native_appearance(summary))
            assert _has_overlay_pixels(view, summary, selected=False), kind
            assert _has_overlay_pixels(view, summary, selected=True), kind
    finally:
        window.close()
        wait_until(lambda: window.manager.is_shutdown)


def test_external_freetext_and_callout_use_the_async_native_appearance_cache(tmp_path, app):
    source = tmp_path / "native-appearance-viewer.pdf"
    _appearance_fixture(source)
    window = MainWindow(); window.resize(900, 700); window.show()
    try:
        window.open_paths([str(source)])
        wait_until(lambda: bool(window.manager.sessions))
        session = next(iter(window.manager.sessions.values()))
        wait_until(lambda: session.inventory_complete)
        view = window._widgets[session.document_id]
        for kind in ("text_box", "callout"):
            summary = next(item for item in session.annotation_inventory.values() if item.kind == kind)
            wait_until(lambda: view.scheduler.budget.get(view._appearance_key(summary), record=False) is not None)
            assert _has_overlay_pixels(view, summary, selected=False)
            assert _has_overlay_pixels(view, summary, selected=True)
    finally:
        window.close()
        wait_until(lambda: window.manager.is_shutdown)


def test_reopened_editorials_keep_quads_caret_hit_testing_and_payload(app, tmp_path):
    output = tmp_path / "probe"
    subprocess.run([sys.executable, "-m", "tests.m10_probe", str(output)], check=True, timeout=60)
    source = output / "m10_editorial_comments_probe.pdf"
    window = MainWindow(); window.resize(900, 700); window.show()
    try:
        window.open_paths([str(source)])
        wait_until(lambda: bool(window.manager.sessions))
        session = next(iter(window.manager.sessions.values()))
        wait_until(lambda: session.inventory_complete)
        view = window._widgets[session.document_id]
        summaries = session.annotation_inventory.values()
        delete = next(item for item in summaries if item.kind == "delete_text")
        insert = next(item for item in summaries if item.kind == "insert_text")
        replace = next(item for item in summaries if item.kind == "replace_text")
        assert delete.geometry.quads and replace.geometry.quads
        assert insert.geometry.caret is not None and replace.geometry.caret is not None
        for summary in (delete, insert, replace):
            assert _has_overlay_pixels(view, summary, selected=False), summary.kind
            assert _has_overlay_pixels(view, summary, selected=True), summary.kind
        top, bottom = insert.geometry.caret
        midpoint = ((top.x + bottom.x) / 2, (top.y + bottom.y) / 2)
        screen = view.coordinate_mapper(insert.page_index).canonical_to_viewport(midpoint)
        hit = view._hit_test_inventory_editorial(QPointF(*screen), insert.page_index)
        assert hit is not None and hit.logical_id == insert.logical_id
        assert view._inventory_editorial_tooltip_text(hit) == insert.preview
        replace_hit = view._hit_test_inventory_editorial(
            QPointF(*view.coordinate_mapper(replace.page_index).canonical_to_viewport(
                ((replace.geometry.quads[0].upper_left.x + replace.geometry.quads[0].upper_right.x) / 2,
                 (replace.geometry.quads[0].upper_left.y + replace.geometry.quads[0].lower_left.y) / 2))),
            replace.page_index)
        assert replace_hit is not None and replace_hit.logical_id == replace.logical_id
    finally:
        window.close(); wait_until(lambda: window.manager.is_shutdown)
