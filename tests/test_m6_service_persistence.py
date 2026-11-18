import json
from pathlib import Path
import subprocess
import sys

import pytest

from pdf_annotator.app.document_manager import DocumentManager
from pdf_annotator.app.document_session import Lifecycle
from pdf_annotator.domain.annotation_commands import (CommandContext, CommandSource,
                                                       CreateHighlight, DeleteAnnotation)
from pdf_annotator.domain.results import RequestState
from pdf_annotator.domain.serialization import target_from_payload
from pdf_annotator.domain.serialization import command_to_payload, result_from_payload
from pdf_annotator.domain.styles import HighlightStyle, RgbColor
from pdf_annotator.util.paths import identify_file
from pdf_annotator.worker import document_service
from pdf_annotator.worker.protocol import Operation
from .conftest import request, wait_until
from .m6_fixtures import quad_center
from .m6_helpers import persistence_worker


@pytest.fixture
def manager(app):
    instance = DocumentManager()
    yield instance
    instance.request_shutdown(discard_changes=True)
    wait_until(lambda: instance.is_shutdown)


def _opened(manager: DocumentManager, path: Path):
    manager.open_path(str(path))
    identity = identify_file(str(path))
    wait_until(lambda: any(session.identity.file_key == identity.file_key
                           and session.lifecycle == Lifecycle.READY
                           for session in manager.sessions.values()))
    return next(session for session in manager.sessions.values()
                if session.identity.file_key == identity.file_key)


def _target(session, start=0, end=8):
    page_text, characters = _page_records(session, 0)
    selected = request(session.worker, Operation.TEXT_SELECTION, {
        "page_index": 0,
        "page_text_id": page_text["page_text_id"],
        "anchor": quad_center(characters[start][2]),
        "focus": quad_center(characters[end][2]),
    })
    return target_from_payload(selected.payload["target"])


def _page_records(session, page_index):
    page_text = request(session.worker, Operation.TEXT_PAGE_INDEX,
                        {"page_index": page_index}).payload["page_text"]
    chunks = [request(session.worker, Operation.TEXT_CHUNK, {
        "page_index": page_index, "page_text_id": page_text["page_text_id"],
        "chunk_index": index}).payload["chunk"]
        for index in range(len(page_text["chunks"]))]
    return page_text, [record for chunk in chunks for record in chunk["characters"]]


def test_create_highlight_through_service_worker_and_separate_reopen(
        tmp_path, manager, monkeypatch, pdf_fixture_copy):
    source = tmp_path / "service-source.pdf"
    pdf_fixture_copy(source)
    original_text = "Prima riga di testo selezionabile\nSeconda riga con spazi e punteggiatura.\n"
    monkeypatch.setattr(document_service, "document_worker_main", persistence_worker)
    session = _opened(manager, source)
    target = _target(session)
    style = HighlightStyle(RgbColor(.1, .7, .3), .46)
    command = CreateHighlight(
        CommandContext.create(session.document_id, 1, CommandSource.UI), target, style)
    results = []
    manager.annotation_result.connect(results.append)
    submission = manager.submit_annotation(command)
    assert submission.state == RequestState.PENDING
    wait_until(lambda: len(results) == 1)
    result = results[0]
    assert result.success and session.document_revision == 1 and session.dirty
    assert session.annotations[command.annotation_id.value] == result.annotation
    duplicate = result_from_payload(request(
        session.worker, Operation.ANNOTATION_COMMAND,
        command_to_payload(command)).payload)
    assert not duplicate.success and duplicate.error.code.value == "duplicate_command"
    assert duplicate.document_revision == 1
    export = Path(str(source) + ".m6-export.pdf")
    assert export.is_file()
    completed = subprocess.run(
        [sys.executable, "-m", "tests.m6_helpers", str(export)],
        cwd=Path(__file__).parents[1], check=True, capture_output=True,
        text=True, timeout=30)
    report = json.loads(completed.stdout)
    assert report["count"] == 1
    assert report["subtype"] == ["name", "/Highlight"]
    assert report["quadpoints"][0] == "array"
    assert command.annotation_id.value in report["nm"][1]
    assert report["colors"]["stroke"] == pytest.approx([0.1, 0.7, 0.3])
    assert abs(report["opacity"] - .46) < .01
    assert original_text in report["text"]


def test_two_document_service_state_and_revisions_remain_isolated(
        tmp_path, manager, pdf_fixture_copy):
    first_path, second_path = tmp_path / "first.pdf", tmp_path / "second.pdf"
    pdf_fixture_copy(first_path)
    pdf_fixture_copy(second_path)
    first, second = _opened(manager, first_path), _opened(manager, second_path)
    first_target, second_target = _target(first), _target(second)
    results = []
    manager.annotation_result.connect(results.append)
    first_command = CreateHighlight(CommandContext.create(first.document_id, 1),
                                    first_target, HighlightStyle())
    second_command = CreateHighlight(CommandContext.create(second.document_id, 1),
                                     second_target, HighlightStyle())
    manager.submit_annotation(first_command)
    manager.submit_annotation(second_command)
    wait_until(lambda: len(results) == 2)
    assert first.document_revision == second.document_revision == 1
    assert set(first.annotations) == {first_command.annotation_id.value}
    assert set(second.annotations) == {second_command.annotation_id.value}
    assert not set(first.annotations) & set(second.annotations)


def test_app_highlight_delete_is_one_service_revision_and_removes_native_pdf(
        tmp_path, manager, monkeypatch, pdf_fixture_copy):
    source = tmp_path / "service-delete-highlight.pdf"
    pdf_fixture_copy(source)
    monkeypatch.setattr(document_service, "document_worker_main", persistence_worker)
    session = _opened(manager, source)
    create = CreateHighlight(CommandContext.create(session.document_id, session.document_generation),
                             _target(session), HighlightStyle())
    manager.submit_annotation(create)
    wait_until(lambda: session.document_revision == 1)
    delete = DeleteAnnotation(CommandContext.create(session.document_id, session.document_generation),
                              create.annotation_id, 0)
    manager.submit_annotation(delete)
    wait_until(lambda: session.document_revision == 2 and not session.annotations)
    export = Path(str(source) + ".m6-export.pdf")
    # Keep PyMuPDF outside the GUI test process: product workers own it.
    inspected = subprocess.run(
        [sys.executable, "-c",
         "import pymupdf,sys; d=pymupdf.open(sys.argv[1]); print(len(list(d[0].annots() or ()))); d.close()",
         str(export)], check=True, capture_output=True, text=True, timeout=30)
    assert inspected.stdout.strip() == "0"


def test_rotated_cropbox_highlights_persist_on_all_pages(
        tmp_path, manager, monkeypatch, pdf_fixture_copy):
    source = tmp_path / "rotated-cropbox.pdf"
    pdf_fixture_copy(source, geometry=True)
    monkeypatch.setattr(document_service, "document_worker_main", persistence_worker)
    session = _opened(manager, source)
    results = []
    manager.annotation_result.connect(results.append)
    commands = []
    for page_index in range(4):
        page_text, characters = _page_records(session, page_index)
        selected = request(session.worker, Operation.TEXT_SELECTION, {
            "page_index": page_index,
            "page_text_id": page_text["page_text_id"],
            "anchor": quad_center(characters[0][2]),
            "focus": quad_center(characters[8][2]),
        })
        command = CreateHighlight(
            CommandContext.create(session.document_id, 1, CommandSource.UI),
            target_from_payload(selected.payload["target"]), HighlightStyle())
        commands.append(command)
        manager.submit_annotation(command)
        wait_until(lambda: len(results) == page_index + 1)
        assert results[-1].success
    assert session.document_revision == 4
    export = Path(str(source) + ".m6-export.pdf")
    completed = subprocess.run(
        [sys.executable, "-m", "tests.m6_helpers", str(export)],
        cwd=Path(__file__).parents[1], check=True, capture_output=True,
        text=True, timeout=30)
    report = json.loads(completed.stdout)
    assert [len(page) for page in report["pages"]] == [1, 1, 1, 1]
    for page, command in zip(report["pages"], commands, strict=True):
        assert page[0]["subtype"] == ["name", "/Highlight"]
        assert page[0]["quadpoints"][0] == "array"
        assert command.annotation_id.value in page[0]["nm"][1]
