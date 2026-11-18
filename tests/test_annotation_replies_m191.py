"""M19.1 focused worker/GUI/save regressions; no full-suite invocation."""

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QKeySequence, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from pdf_annotator.app.main_window import MainWindow
from pdf_annotator.domain.annotation_commands import CommandContext, DeleteExternalAnnotation, Undo, Redo
from pdf_annotator.domain.annotation_inventory import external_command_annotation_id, is_attached_reply
from pdf_annotator.domain.external_edits import EditExternalAnnotation, ExternalEditKind
from pdf_annotator.domain.results import HistoryDelta
from pdf_annotator.domain.serialization import history_delta_from_payload, history_delta_to_payload
from pdf_annotator.worker.annotation_inventory import summary_from_payload, summary_to_payload
from pdf_annotator.worker.protocol import Operation, ErrorInfo
from pdf_annotator.viewer.sticky_notes import note_icon_rect
from tests.conftest import close_window_discard, wait_until
from tests.m191_fixtures import LONG_REASON, PLAIN_NOTE
from tests.test_annotation_inventory_m12 import _client, _open, _request
from tests.test_save_m15 import ink, save, submit


def fixture_command(operation, path, source=None):
    result = subprocess.run([sys.executable, "-m", "tests.m191_fixtures", operation, str(path), *([str(source)] if source is not None else [])],
                            check=True, capture_output=True, timeout=40)
    return json.loads(result.stdout) if result.stdout else None


@pytest.fixture(scope="module")
def enriched(tmp_path_factory, m19_samples):
    path = tmp_path_factory.mktemp("m191-fixture") / "enriched.pdf"
    fixture_command("create", path, m19_samples / "m19_motivated_corrections.pdf")
    return path


@pytest.fixture
def opened(app, tmp_path, enriched, request, m19_samples):
    path = tmp_path / "copy.pdf"
    if getattr(request, "param", None) == "clean":
        fixture_command("clean", path, m19_samples / "m19_motivated_corrections.pdf")
    else:
        path.write_bytes(enriched.read_bytes())
    window = MainWindow()
    window.resize(1050, 800)
    window.show()
    window.open_paths([str(path)])
    wait_until(lambda: bool(window.manager.sessions))
    session = next(iter(window.manager.sessions.values()))
    wait_until(lambda: session.inventory_complete)
    yield window, session, window._widgets[session.document_id], path
    close_window_discard(window)


def named(session, name):
    return next(item for item in session.annotation_inventory.values() if item.native_name == name)


def root(session, kind="replace_text"):
    return next(item for item in session.annotation_inventory.values() if item.kind == kind)


def read_group(window, session, summary):
    window._widgets[session.document_id].select_inventory_annotation(summary.logical_id, reveal=True)
    ids = (summary.logical_id, *summary.reply_ids)
    wait_until(lambda: all(value in session.annotation_details for value in ids))
    return window.annotation_sidebar.detail


@pytest.mark.parametrize("filename", ["m19_motivated_corrections.pdf", "m19_pymupdf_resaved.pdf"])
def test_original_m19_three_native_corrections_and_relations(filename, m19_samples, app):
    source = m19_samples / filename
    original = source.read_bytes()
    client = _client(source)
    try:
        _open(client)
        records = _request(client, Operation.ANNOTATION_SUMMARIES, {"cursor": 0})["summaries"]
        summaries = [summary_from_payload(value) for value in records]
        assert len(summaries) == 6  # Three roots and three replies remain in inventory.
        by_id = {item.logical_id: item for item in summaries}
        for kind in ("replace_text", "delete_text", "insert_text"):
            parent = next(item for item in summaries if item.kind == kind)
            assert len(parent.reply_ids) == 1
            reply = by_id[parent.reply_ids[0]]
            assert is_attached_reply(reply, by_id)
            assert reply.reply.irt_xref == int(parent.logical_id.rsplit(":", 1)[1])
            assert reply.reply.reply_type == "/R" and reply.author == "Revisione IA"
            assert summary_from_payload(summary_to_payload(reply)) == reply
            delta = HistoryDelta(inventory_updated=(parent, reply))
            assert history_delta_from_payload(history_delta_to_payload(delta)) == delta
            assert not parent.capabilities.deletable  # Existing policy protects incoming replies.
        replacement = next(item for item in summaries if item.kind == "replace_text")
        assert replacement.geometry.quads and replacement.geometry.caret
        assert len(replacement.component_xrefs) == 4  # Replies are not correction components.
    finally:
        client.close_async()
        wait_until(lambda: client.stopped.is_set())
    assert source.read_bytes() == original


def test_external_authors_multiple_default_nested_and_member_replies(opened):
    window, session, _view, _path = opened
    parent = root(session)
    assert len(parent.reply_ids) == 5
    assert len(set(parent.reply_ids)) == 5
    for name in ("long-reply", "default-r", "member-reply", "nested-reply"):
        reply = named(session, name)
        assert reply.reply.parent_logical_id == parent.logical_id
        assert is_attached_reply(reply, session.annotation_inventory)
        assert reply.author == "Autrice esterna"
    assert named(session, "default-r").reply.reply_type is None
    sidebar_ids = [window.annotation_sidebar.list.item(i).data(Qt.ItemDataRole.UserRole)
                   for i in range(window.annotation_sidebar.list.count())]
    assert parent.logical_id in sidebar_ids
    assert not set(parent.reply_ids) & set(sidebar_ids)
    assert named(session, "ordinary").logical_id in sidebar_ids


@pytest.mark.parametrize("name,status", [
    ("missing", "missing_parent"), ("self", "cycle"), ("cycle-a", "cycle"),
    ("cycle-b", "cycle"), ("group-note", "unsupported_reply_type"),
    ("unknown-rt", "unsupported_reply_type"), ("invalid-irt", "invalid_irt"),
    ("popup-parent", "unreadable_parent"), ("cross-page", "cross_page")])
def test_anomalies_remain_accessible(opened, name, status):
    window, session, _view, _path = opened
    reply = named(session, name)
    assert reply.reply.status == status
    assert not is_attached_reply(reply, session.annotation_inventory)
    detail = read_group(window, session, reply)
    assert "Risposta non associata: " in detail.toPlainText()
    assert "Motivazione esterna" in detail.toPlainText()


def test_long_plain_readonly_copy_metadata_and_no_document_mutation(opened):
    window, session, _view, path = opened
    original = path.read_bytes()
    before = session.document_revision, session.dirty, session.history_length
    commands = session.worker.submitted_counts[Operation.ANNOTATION_COMMAND]
    detail = read_group(window, session, root(session))
    text = detail.toPlainText()
    assert text.startswith("Contenuto editoriale\nè\n\nMotivazioni / risposte")
    assert LONG_REASON in text and "Autrice esterna" in text
    assert text.count(LONG_REASON) == 1
    assert detail.isReadOnly() and detail.verticalScrollBar().maximum() > 0
    detail.selectAll()
    QTest.keySequence(detail, QKeySequence.StandardKey.Copy)
    assert QApplication.clipboard().text() == text
    QTest.keyClicks(detail, "must not edit")
    assert detail.toPlainText() == text
    reply = named(session, "long-reply")
    assert session.annotation_details[reply.logical_id].subject == "Oggetto esterno"
    assert reply.created == "D:20260102030405Z"
    assert (session.document_revision, session.dirty, session.history_length) == before
    assert session.worker.submitted_counts[Operation.ANNOTATION_COMMAND] == commands
    assert path.read_bytes() == original


def test_no_reply_icon_or_hit_target_and_ordinary_note_unchanged(opened):
    window, session, view, _path = opened
    reply = named(session, "long-reply")
    mapper = view.coordinate_mapper(0)
    image = QImage(900, 900, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("transparent"))
    painter = QPainter(image)
    view._paint_inventory_annotation(painter, mapper, reply, selected=True)
    painter.end()
    assert not any(image.pixelColor(x, y).alpha() for x in range(0, 900, 10) for y in range(0, 900, 10))
    assert view._hit_test_inventory(note_icon_rect(mapper, reply.geometry.bbox).center(), 0) != reply.logical_id
    ordinary = named(session, "ordinary")
    view.select_inventory_annotation(ordinary.logical_id, reveal=True)
    # Fixture notes overlap deliberately; directly select the ordinary note to
    # open its existing reader without assigning an independent reply target.
    view.sticky_note_reader.open_note(ordinary)
    wait_until(lambda: view.sticky_note_reader.text.toPlainText() == PLAIN_NOTE)
    assert view.sticky_note_reader.isVisible()
    assert not is_attached_reply(ordinary, session.annotation_inventory)


def test_rapid_selection_retires_late_multichunk_reply(opened, monkeypatch):
    window, session, view, _path = opened
    poll = window.manager._poll_inventory
    first = root(session)
    read_group(window, session, first)
    reply = named(session, "long-reply")
    session.annotation_details.pop(reply.logical_id)
    monkeypatch.setattr(window.manager, "_poll_inventory", lambda _: None)
    window.annotation_sidebar.select_annotation(session, first.logical_id)
    assert session.inventory_detail_id == reply.logical_id
    second = root(session, "insert_text")
    view.select_inventory_annotation(second.logical_id, reveal=True)
    assert session.inventory_detail_pending_id == second.logical_id
    monkeypatch.setattr(window.manager, "_poll_inventory", poll)
    read_group(window, session, second)
    text = window.annotation_sidebar.detail.toPlainText()
    assert text.startswith("Contenuto editoriale\nè \n") and LONG_REASON not in text
    window._annotation_detail_changed(session.document_id, reply.logical_id)
    window._annotation_detail_changed("other-document", reply.logical_id)
    assert window.annotation_sidebar.detail.toPlainText() == text


@pytest.mark.parametrize("mode", ["failure", "no_progress", "stale_generation"])
def test_bad_detail_terminates_without_infinite_loading(opened, monkeypatch, mode):
    window, session, _view, _path = opened
    target = named(session, "long-reply")
    poll = session.worker.poll

    def intercept(request_id=None):
        delivery = poll(request_id)
        if delivery and delivery.response.operation == Operation.ANNOTATION_DETAIL and delivery.response.payload.get("logical_id") == target.logical_id:
            response = delivery.response
            if mode == "failure":
                delivery.response = replace(response, success=False, payload={}, error=ErrorInfo("TEST", "failed"))
            elif mode == "no_progress":
                delivery.response = replace(response, payload={**response.payload, "contents": "", "next_offset": 0, "complete": False})
            else:
                delivery.response = replace(response, document_generation=response.document_generation + 1)
        return delivery

    monkeypatch.setattr(session.worker, "poll", intercept)
    parent = root(session)
    window._widgets[session.document_id].select_inventory_annotation(parent.logical_id, reveal=True)
    wait_until(lambda: target.logical_id in session.annotation_detail_errors)
    wait_until(lambda: session.inventory_request_id is None)
    text = window.annotation_sidebar.detail.toPlainText()
    assert "Impossibile caricare" in text and "Caricamento" not in text
    assert "Motivazione esterna" in text  # Other replies remain readable.


@pytest.mark.parametrize("opened", ["clean"], indirect=True)
def test_mutation_history_save_as_save_reopen_preserves_relations(opened, tmp_path):
    window, session, _view, source = opened
    baseline = fixture_command("inspect", source)
    parent = root(session)
    expected_order = parent.reply_ids
    submit(window.manager, ink(session))
    read_group(window, session, parent)
    for command in (Undo, Redo):
        submit(window.manager, command(CommandContext.create(session.document_id, session.document_generation)))
        read_group(window, session, parent)
        assert root(session).reply_ids == expected_order
    target = tmp_path / "saved-as.pdf"
    save(window.manager, session, target)
    wait_until(lambda: session.inventory_complete)
    read_group(window, session, root(session))
    save(window.manager, session)
    wait_until(lambda: session.inventory_complete)
    after = fixture_command("inspect", target)
    assert after["base"] == baseline["base"]
    # Added Ink is unrelated. Native reply graphs and metadata remain exact.
    original_xrefs = {item["xref"] for item in baseline["annotations"]}
    assert [item for item in after["annotations"] if item["xref"] in original_xrefs] == baseline["annotations"]
    assert window.manager.request_close(session.document_id, discard_changes=True)
    wait_until(lambda: session.document_id not in window.manager.sessions)
    window.open_paths([str(target)])
    wait_until(lambda: bool(window.manager.sessions))
    reopened = next(iter(window.manager.sessions.values()))
    wait_until(lambda: reopened.inventory_complete)
    assert len(root(reopened).reply_ids) == 5
    read_group(window, reopened, root(reopened))


def test_existing_deletion_policy_and_orphan_fallback(opened, tmp_path, app, m19_samples):
    window, session, view, _source = opened
    parent = root(session)
    result = window.manager.submit_annotation(DeleteExternalAnnotation(
        CommandContext.create(session.document_id, session.document_generation),
        external_command_annotation_id(parent.logical_id), parent.logical_id, parent.page_index))
    assert result.state.value == "failed"
    assert parent.logical_id in session.annotation_inventory
    reply = session.annotation_inventory[parent.reply_ids[0]]
    session.annotation_inventory.pop(parent.logical_id)
    window.annotation_sidebar.apply_history_delta(session, HistoryDelta(inventory_removed_ids=(parent.logical_id,)))
    assert not is_attached_reply(reply, session.annotation_inventory)
    assert reply.logical_id in [window.annotation_sidebar.list.item(i).data(Qt.ItemDataRole.UserRole)
                               for i in range(window.annotation_sidebar.list.count())]
    session.annotation_inventory[parent.logical_id] = parent
    orphan = tmp_path / "external-orphan.pdf"
    fixture_command("orphan", orphan, m19_samples / "m19_motivated_corrections.pdf")
    client = _client(orphan)
    try:
        _open(client)
        values = _request(client, Operation.ANNOTATION_SUMMARIES, {"cursor": 0})["summaries"]
        orphans = [summary_from_payload(item) for item in values if (item.get("reply") or {}).get("status") == "missing_parent"]
        assert len(orphans) == 1 and orphans[0].page_index == 1
    finally:
        client.close_async()
        wait_until(lambda: client.stopped.is_set())


@pytest.mark.parametrize("opened", ["clean"], indirect=True)
@pytest.mark.parametrize("invalidate_first", [False, True])
def test_related_native_edit_and_history_keep_reply_links_incrementally(opened, invalidate_first):
    window, session, _view, _path = opened
    parent = named(session, "editable-parent")
    assert parent.capabilities.movable
    expected = parent.reply_ids
    if invalidate_first:
        submit(window.manager, ink(session))
    command = EditExternalAnnotation(
        CommandContext.create(session.document_id, session.document_generation),
        external_command_annotation_id(parent.logical_id), parent.logical_id, 0,
        ExternalEditKind.MOVE, delta=(8, 0))
    result = submit(window.manager, command)
    assert not result.refresh_inventory
    wait_until(lambda: session.inventory_complete)
    for command_type in (Undo, Redo):
        current = named(session, "editable-parent")
        assert current.reply_ids == expected
        assert "Risposta al riquadro" in read_group(window, session, current).toPlainText()
        result = submit(window.manager, command_type(CommandContext.create(session.document_id, session.document_generation)))
        assert not result.refresh_inventory
        wait_until(lambda: session.inventory_complete)
    assert named(session, "editable-parent").reply_ids == expected


def test_attached_selection_resolves_to_parent_and_never_opens_note_reader(opened):
    window, session, view, _path = opened
    parent = root(session)
    child = session.annotation_inventory[parent.reply_ids[0]]
    view.select_inventory_annotation(child.logical_id, reveal=True)
    assert session.selected_annotation_id == parent.logical_id
    view.sticky_note_reader.open_note(child)
    assert not view.sticky_note_reader.isVisible()
    assert LONG_REASON in read_group(window, session, parent).toPlainText()
