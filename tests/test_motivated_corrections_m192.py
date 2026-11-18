"""M19.2 production command/worker/history/inventory/save regressions."""

from dataclasses import replace
from functools import partial
import json
from pathlib import Path
from shutil import copyfile
import subprocess
import sys

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from pdf_annotator.app.document_manager import DocumentManager
from pdf_annotator.app.main_window import MainWindow
from pdf_annotator.domain.annotation_commands import (
    CommandContext, CommandSource, CreateDeleteText, CreateInsertText, CreateReplaceText,
    DeleteAnnotation, DeleteExternalAnnotation, Undo, Redo,
)
from pdf_annotator.domain.annotation_inventory import is_attached_reply, external_command_annotation_id
from pdf_annotator.domain.editorial_metadata import EditorialMetadata
from pdf_annotator.domain.serialization import command_to_payload, command_from_payload, target_from_payload
from pdf_annotator.domain.results import RequestState
from pdf_annotator.worker import document_service
from pdf_annotator.worker.protocol import Operation
from tests.conftest import close_window_discard, request, wait_until
from tests.m6_fixtures import quad_center
from tests.m192_helpers import fault_worker
from tests.test_m6_service_persistence import _opened
from tests.test_save_m15 import save, submit, ink

META = EditorialMetadata("Perché: città 日本語 😀 & <ok>\nSeconda riga e\u0301.", "Revisione IA città 日本語")


def targeted_request(client, operation, payload):
    request_id = client.submit(operation, payload)
    assert request_id is not None
    found = []
    def poll():
        delivery = client.poll(request_id)
        if delivery is not None:
            with delivery:
                found.append(delivery.response)
        return bool(found)
    wait_until(poll)
    assert found[0].success, found[0].error
    return found[0]


def range_target(session, text, *, page_index, occurrence=0):
    index = targeted_request(session.worker, Operation.TEXT_PAGE_INDEX, {"page_index": page_index}).payload["page_text"]
    records = []
    for chunk_index in range(len(index["chunks"])):
        records.extend(targeted_request(session.worker, Operation.TEXT_CHUNK, {
            "page_index": page_index, "page_text_id": index["page_text_id"],
            "chunk_index": chunk_index}).payload["chunk"]["characters"])
    joined = "".join(record[1] for record in records)
    offset = -1
    for _ in range(occurrence + 1):
        offset = joined.index(text, offset + 1)
    characters = [record for record in records for _ in record[1]]
    response = targeted_request(session.worker, Operation.TEXT_SELECTION, {
        "page_index": page_index, "page_text_id": index["page_text_id"],
        "anchor": quad_center(characters[offset][2]),
        "focus": quad_center(characters[offset + len(text) - 1][2])})
    assert response.payload["status"] == "confirmed"
    return target_from_payload(response.payload["target"])


def inspect(path):
    result = subprocess.run([sys.executable, "-m", "tests.m192_helpers", "inspect", str(path)],
                            capture_output=True, check=True, timeout=40)
    return json.loads(result.stdout)


def mutate(path, output, mode):
    subprocess.run([sys.executable, "-m", "tests.m192_helpers", mode, str(path), str(output)],
                   capture_output=True, check=True, timeout=40)


def command(session, kind="replace", metadata=META):
    context = CommandContext.create(session.document_id, session.document_generation, CommandSource.INTERNAL)
    page, word = {"replace": (0, "sono"), "delete": (1, "molto "), "insert": (2, "chiara")}[kind]
    target = range_target(session, word, page_index=page, occurrence=1 if kind == "delete" else 0)
    if kind == "replace":
        return CreateReplaceText(context, target, "è", metadata=metadata)
    if kind == "delete":
        return CreateDeleteText(context, target, metadata=metadata)
    response = targeted_request(session.worker, Operation.TEXT_INSERTION, {
        "page_index": page, "page_text_id": target.snapshot_id,
        "char_index": target.start_char_index, "affinity": "before"})
    assert response.success and response.payload["status"] == "confirmed"
    return CreateInsertText(context, target_from_payload(response.payload["target"]), "è ", metadata=metadata)


def opened(manager, path):
    session = _opened(manager, path)
    manager.start_annotation_inventory(session.document_id)
    wait_until(lambda: session.inventory_complete)
    return session


@pytest.fixture
def manager(app):
    instance = DocumentManager()
    yield instance
    assert instance.request_shutdown(discard_changes=True)
    wait_until(lambda: instance.is_shutdown)


@pytest.fixture
def source(tmp_path, m19_samples):
    path = tmp_path / "source.pdf"
    copyfile(m19_samples / "m19_source.pdf", path)
    return path


def identities(session):
    return set(session.annotation_inventory)


def fail_submit(manager, cmd):
    results = []
    manager.annotation_result.connect(results.append)
    try:
        assert manager.submit_annotation(cmd).state == RequestState.PENDING
        wait_until(lambda: results)
        assert not results[-1].success
        return results[-1]
    finally:
        manager.annotation_result.disconnect(results.append)


@pytest.mark.parametrize("kind", ["replace", "delete", "insert"])
@pytest.mark.parametrize("metadata", [None, EditorialMetadata(author="Autore città"), META,
    EditorialMetadata(reason="Motivazione senza autore")], ids=["legacy", "author", "motivated", "default-author"])
def test_native_profiles_optional_metadata_and_single_history(manager, source, tmp_path, kind, metadata):
    baseline = inspect(source)
    session = opened(manager, source)
    cmd = command(session, kind, metadata)
    assert command_from_payload(command_to_payload(cmd)) == cmd
    if metadata is None:
        assert "metadata" not in command_to_payload(cmd)
    result = submit(manager, cmd)
    assert (session.document_revision, session.history_length, session.dirty) == (1, 1, True)
    expected_rows = 2 if metadata is not None and metadata.reason is not None else 1
    assert len(session.annotation_inventory) == expected_rows
    if expected_rows == 2:
        parent = session.annotation_inventory[cmd.annotation_id.value]
        assert len(parent.reply_ids) == 1 and parent.author == (metadata.author or "PDF Annotator")
        assert parent.capabilities.logical_group
        reply = session.annotation_inventory[parent.reply_ids[0]]
        assert is_attached_reply(reply, session.annotation_inventory)
        assert reply.reply.parent_logical_id == parent.logical_id
        assert reply.author == (metadata.author or "PDF Annotator")
    output = tmp_path / "saved.pdf"
    save(manager, session, output)
    report = inspect(output)
    assert report["base"] == baseline["base"]
    roots = [r for r in report["annotations"] if r["keys"].get("NM") == cmd.annotation_id.value]
    assert len(roots) == 1
    root = roots[0]
    assert root["keys"]["Subtype"] == ("/StrikeOut" if kind == "delete" else "/Caret")
    assert root["keys"].get("Contents") == (None if kind == "delete" else "è" if kind == "replace" else "è ")
    assert root["keys"]["T"] == (metadata.author if metadata and metadata.author else "PDF Annotator")
    if expected_rows == 2:
        marker = json.loads(root["keys"]["fuPDFCorrection"])
        assert marker["v"] == 1 and marker["group"] == marker["root"] == cmd.annotation_id.value
        assert marker["kind"] == kind + "_text"
        replies = [r for r in report["annotations"] if r["keys"].get("RT") == "/R"]
        assert len(replies) == 1 and replies[0]["keys"]["IRT"] == f'{root["xref"]} 0 R'
        assert replies[0]["keys"]["Contents"] == metadata.reason
        assert len(report["annotations"]) == (6 if kind == "replace" else 4)
        assert report["groups"][0]["components"]
    else:
        assert "fuPDFCorrection" not in root["keys"] and not report["groups"]
        assert len(report["annotations"]) == (4 if kind == "replace" else 2)
    if kind == "replace":
        strike = next(r for r in report["annotations"] if r["keys"].get("RT") == "/Group")
        assert strike["keys"]["IRT"] == f'{root["xref"]} 0 R'
        assert strike["keys"]["IT"] == "/StrikeOutTextEdit" and root["keys"]["IT"] == "/Replace"


@pytest.mark.parametrize("stage", ["primary", "reason", "irt", "rt", "manifest", "history_prepare",
                                  "history_domain", "history_register", "inventory", "serialization", "result"])
def test_failure_restores_pdf_history_inventory_and_retry(app, monkeypatch, source, tmp_path, stage):
    baseline = inspect(source)
    monkeypatch.setattr(document_service, "document_worker_main", partial(fault_worker, stage=stage))
    manager = DocumentManager()
    try:
        session = opened(manager, source)
        before_ids = identities(session)
        result = fail_submit(manager, command(session))
        assert result.document_revision == result.history_length == result.history_position == 0
        assert not session.dirty and session.pending_mutations == 0 and identities(session) == before_ids
        state = inspect(Path(str(source) + ".m192-state.pdf"))
        assert state["annotations"] == baseline["annotations"] == []
        assert state["base"] == baseline["base"]
        # No successful history, dirty or inventory change was published.
        reply = request(session.worker, Operation.ANNOTATION_SUMMARIES, {"cursor": 0})
        assert reply.success and reply.payload["summaries"] == []
        submit(manager, command(session))
        assert session.history_length == session.document_revision == 1
        assert len(session.annotation_inventory) == 2
    finally:
        manager.request_shutdown(discard_changes=True)
        wait_until(lambda: manager.is_shutdown)


@pytest.mark.parametrize("stage", ["history_prepare", "history_register", "serialization", "result"])
def test_original_unmotivated_failure_is_atomic(app, monkeypatch, source, stage):
    monkeypatch.setattr(document_service, "document_worker_main", partial(fault_worker, stage=stage))
    manager = DocumentManager()
    try:
        session = opened(manager, source)
        fail_submit(manager, command(session, metadata=None))
        assert not session.dirty and session.document_revision == session.history_length == 0
        assert inspect(Path(str(source) + ".m192-state.pdf"))["annotations"] == []
    finally:
        manager.request_shutdown(discard_changes=True)
        wait_until(lambda: manager.is_shutdown)


@pytest.mark.parametrize("kind", ["replace", "delete", "insert"])
def test_repeated_history_delete_and_save_while_undone(manager, source, tmp_path, kind):
    session = opened(manager, source)
    cmd = command(session, kind)
    submit(manager, cmd)
    expected_ids = identities(session)
    output = tmp_path / "with-group.pdf"
    save(manager, session, output)
    original = inspect(output)
    for iteration in range(3):
        submit(manager, Undo(CommandContext.create(session.document_id, session.document_generation)))
        assert not session.annotation_inventory
        if iteration == 0:
            save(manager, session)
        submit(manager, Redo(CommandContext.create(session.document_id, session.document_generation)))
        assert identities(session) == expected_ids
    save(manager, session)
    assert inspect(output)["annotations"] == original["annotations"]
    submit(manager, DeleteAnnotation(CommandContext.create(session.document_id, session.document_generation),
                                     cmd.annotation_id, cmd.target.page_index))
    assert not session.annotation_inventory and session.history_length == 2
    submit(manager, Undo(CommandContext.create(session.document_id, session.document_generation)))
    assert identities(session) == expected_ids
    submit(manager, Redo(CommandContext.create(session.document_id, session.document_generation)))
    assert not session.annotation_inventory
    save(manager, session)
    assert inspect(output)["annotations"] == []


@pytest.fixture
def saved_group(manager, source, tmp_path):
    session = opened(manager, source)
    submit(manager, command(session))
    path = tmp_path / "managed.pdf"
    save(manager, session, path)
    assert manager.request_close(session.document_id)
    wait_until(lambda: session.document_id not in manager.sessions)
    return path


@pytest.mark.parametrize("mode", ["normal", "multiple", "renumber"])
def test_delete_after_reopen_restores_complete_group(manager, saved_group, tmp_path, mode):
    source = saved_group
    if mode != "normal":
        source = tmp_path / (mode + ".pdf")
        mutate(saved_group, source, mode)
    before = inspect(source)
    session = opened(manager, source)
    parent = next(s for s in session.annotation_inventory.values() if s.kind == "replace_text")
    assert parent.capabilities.deletable and len(parent.reply_ids) == (2 if mode == "multiple" else 1)
    cmd = DeleteExternalAnnotation(CommandContext.create(session.document_id, session.document_generation),
        external_command_annotation_id(parent.logical_id), parent.logical_id, 0)
    submit(manager, cmd)
    assert not session.annotation_inventory
    submit(manager, Undo(CommandContext.create(session.document_id, session.document_generation)))
    assert len(session.annotation_inventory) == (3 if mode == "multiple" else 2)
    output = tmp_path / "restored.pdf"
    save(manager, session, output)
    assert inspect(output)["annotations"] == before["annotations"]
    submit(manager, Redo(CommandContext.create(session.document_id, session.document_generation)))
    save(manager, session)
    assert inspect(output)["annotations"] == []


@pytest.mark.parametrize("mode", ["foreign", "nested", "cross_page", "missing", "version", "broken_irt", "duplicate",
                                  "unknown_component", "locked", "bad_parent", "string_irt", "missing_popup"])
def test_unverified_mixed_graph_never_deletes(manager, saved_group, tmp_path, mode):
    path = tmp_path / "external.pdf"
    mutate(saved_group, path, mode)
    before = inspect(path)
    session = opened(manager, path)
    parent = next(s for s in session.annotation_inventory.values() if s.kind == "replace_text")
    assert not parent.capabilities.deletable
    cmd = DeleteExternalAnnotation(CommandContext.create(session.document_id, session.document_generation),
        external_command_annotation_id(parent.logical_id), parent.logical_id, 0)
    assert manager.submit_annotation(cmd).state == RequestState.FAILED
    assert not session.dirty and session.document_revision == 0
    # Also bypass the GUI/service capability to test worker authority.
    response = request(session.worker, Operation.ANNOTATION_COMMAND, command_to_payload(cmd))
    assert response.success and response.payload["outcome"] != "success"
    output = tmp_path / "preserved.pdf"
    save(manager, session, output)
    assert inspect(output)["annotations"] == before["annotations"]


def test_sidebar_immediate_copy_and_redo_no_native_alias_leak(app, source):
    window = MainWindow()
    window.resize(1050, 800)
    window.show()
    try:
        window.open_paths([str(source)])
        wait_until(lambda: bool(window.manager.sessions))
        session = next(iter(window.manager.sessions.values()))
        wait_until(lambda: session.inventory_complete)
        metadata = EditorialMetadata("😀" * 15000 + "\nFine", META.author)
        cmd = command(session, metadata=metadata)
        submit(window.manager, cmd)
        view = window._widgets[session.document_id]
        assert window.annotation_sidebar.list.count() == 1
        parent = session.annotation_inventory[cmd.annotation_id.value]
        assert len(parent.reply_ids) == 1 and session.annotation_inventory[parent.reply_ids[0]].reply.parent_logical_id == parent.logical_id
        for _ in range(2):
            view.select_inventory_annotation(parent.logical_id, reveal=True)
            wait_until(lambda: metadata.reason in window.annotation_sidebar.detail.toPlainText())
            sidebar = window.annotation_sidebar
            sidebar.show()
            QTest.keyClick(sidebar.list, Qt.Key.Key_Return)
            detail = sidebar.detail
            assert detail.isVisible() and detail.parentWidget() is sidebar.list.itemWidget(sidebar.list.currentItem())
            assert parent.author == META.author
            assert detail.parentWidget().findChild(QLabel, "annotation_author").text() == META.author
            assert detail.toPlainText().startswith("Contenuto editoriale\nè\n")
            assert detail.isReadOnly() and META.author in detail.toPlainText()
            detail.selectAll()
            QTest.keySequence(detail, QKeySequence.StandardKey.Copy)
            assert metadata.reason in QApplication.clipboard().text()
            submit(window.manager, Undo(CommandContext.create(session.document_id, session.document_generation)))
            assert window.annotation_sidebar.list.count() == 0
            submit(window.manager, Redo(CommandContext.create(session.document_id, session.document_generation)))
            assert window.annotation_sidebar.list.count() == 1
    finally:
        close_window_discard(window)


@pytest.mark.parametrize("stage", ["undo", "redo", "delete"])
def test_history_or_delete_failure_rolls_back_entire_group(app, monkeypatch, source, stage):
    monkeypatch.setattr(document_service, "document_worker_main", partial(fault_worker, stage=stage))
    manager = DocumentManager()
    try:
        session = opened(manager, source)
        cmd = command(session)
        submit(manager, cmd)
        original_ids = identities(session)
        if stage == "redo":
            submit(manager, Undo(CommandContext.create(session.document_id, 1)))
        before = session.document_revision, session.history_position, session.history_length, identities(session)
        attempted = (DeleteAnnotation(CommandContext.create(session.document_id, 1), cmd.annotation_id, 0)
                     if stage == "delete" else
                     (Redo if stage == "redo" else Undo)(CommandContext.create(session.document_id, 1)))
        fail_submit(manager, attempted)
        assert (session.document_revision, session.history_position, session.history_length, identities(session)) == before
        report = inspect(Path(str(source) + ".m192-state.pdf"))
        assert len(report["annotations"]) == (0 if stage == "redo" else 6)
        submit(manager, replace(attempted, context=CommandContext.create(session.document_id, 1)))
        assert identities(session) == (original_ids if stage == "redo" else set())
    finally:
        manager.request_shutdown(discard_changes=True)
        wait_until(lambda: manager.is_shutdown)


def test_rollback_failure_retires_only_its_worker(app, monkeypatch, source, tmp_path):
    monkeypatch.setattr(document_service, "document_worker_main", partial(fault_worker, stage="rollback"))
    manager = DocumentManager()
    try:
        session = opened(manager, source)
        other_path = tmp_path / "other.pdf"
        copyfile(source, other_path)
        other = opened(manager, other_path)
        manager.submit_annotation(command(session))
        wait_until(lambda: session.lifecycle.value == "failed")
        assert other.lifecycle.value == "ready" and other.worker.failure is None
        assert not session.saving
    finally:
        manager.request_shutdown(discard_changes=True)
        wait_until(lambda: manager.is_shutdown)


def test_failed_registration_preserves_external_annotations_and_redo_branch(app, monkeypatch, source, m19_samples):
    external_source = m19_samples / "m19_motivated_corrections.pdf"
    copyfile(external_source, source)
    baseline = inspect(source)
    monkeypatch.setattr(document_service, "document_worker_main", partial(fault_worker, stage="history_register"))
    manager = DocumentManager()
    try:
        session = opened(manager, source)
        created = ink(session)
        submit(manager, created)
        submit(manager, Undo(CommandContext.create(session.document_id, 1)))
        before = session.document_revision, session.history_position, session.history_length, session.history_state_id, identities(session)
        fail_submit(manager, command(session))
        assert (session.document_revision, session.history_position, session.history_length, session.history_state_id, identities(session)) == before
        report = inspect(Path(str(source) + ".m192-state.pdf"))
        assert report["annotations"] == baseline["annotations"]
        assert report["base"] == baseline["base"]
        assert not session.dirty
        submit(manager, Redo(CommandContext.create(session.document_id, 1)))
        assert created.annotation_id.value in session.annotations
    finally:
        manager.request_shutdown(discard_changes=True)
        wait_until(lambda: manager.is_shutdown)


@pytest.mark.parametrize("kwargs", [{"reason": ""}, {"reason": "x" * 16385}, {"author": ""},
                                    {"author": "x" * 257}, {"reason": "\ud800"}, {"author": "\x00"}])
def test_metadata_rejected_before_any_pdf_mutation(kwargs):
    with pytest.raises(ValueError):
        EditorialMetadata(**kwargs)
