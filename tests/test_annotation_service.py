from hashlib import sha256
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from pdf_annotator.app.document_manager import DocumentManager
from pdf_annotator.app.document_session import Lifecycle
from pdf_annotator.domain.annotation_commands import (CommandContext, CommandSource,
                                                       CreateFreeText, CreateHighlight,
                                                       CreateInk)
from pdf_annotator.domain.errors import MutationErrorCode
from pdf_annotator.domain.geometry import PdfPoint, PdfQuad, PdfRect
from pdf_annotator.domain.results import MutationOutcome, RequestState
from pdf_annotator.domain.serialization import command_to_payload, result_from_payload
from pdf_annotator.domain.styles import FreeTextStyle, HighlightStyle, InkStyle
from pdf_annotator.domain.targets import InkTarget, RectTarget, TextRangeTarget
from pdf_annotator.util.paths import identify_file
from pdf_annotator.worker import document_service
from pdf_annotator.worker.protocol import Operation
from .conftest import request, wait_until
from .m5_helpers import (delayed_synthetic_worker, failing_synthetic_worker,
                         synthetic_worker)


@pytest.fixture(scope="module")
def m5_pdf_files(tmp_path_factory):
    directory = tmp_path_factory.mktemp("m5-pdfs")
    subprocess.run([sys.executable, "-m", "tests.m1_fixtures", str(directory)],
                   check=True, timeout=30, capture_output=True)
    return directory


@pytest.fixture
def manager(app):
    instance = DocumentManager()
    yield instance
    instance.request_shutdown(discard_changes=True)
    wait_until(lambda: instance.is_shutdown)


def opened(manager, path: Path):
    assert manager.open_path(str(path)) is not None
    identity = identify_file(str(path))
    wait_until(lambda: any(s.identity.file_key == identity.file_key
                           and s.lifecycle == Lifecycle.READY
                           for s in manager.sessions.values()))
    return next(s for s in manager.sessions.values()
                if s.identity.file_key == identity.file_key)


def highlight(session, *, page=0, command_id=None):
    context = (CommandContext(command_id, session.document_id,
                              session.document_generation, source=CommandSource.UI)
               if command_id is not None else
               CommandContext.create(session.document_id, session.document_generation))
    quad = PdfQuad(PdfPoint(10, 10), PdfPoint(80, 10),
                   PdfPoint(10, 24), PdfPoint(80, 24))
    return CreateHighlight(context, TextRangeTarget.create(page, (quad,), "selected"),
                           HighlightStyle())


def collect_results(manager):
    results = []
    manager.annotation_result.connect(results.append)
    return results


def ink(session):
    context = CommandContext.create(session.document_id, session.document_generation)
    return CreateInk(context, InkTarget(0, ((PdfPoint(10, 10), PdfPoint(20, 20)),)),
                     InkStyle())


def test_annotation_service_is_not_polled_when_idle(m5_pdf_files, manager, monkeypatch):
    session = opened(manager, m5_pdf_files / "normal.pdf")
    calls = []
    monkeypatch.setattr(manager.annotation_service, "poll_document", calls.append)
    manager._poll_once()
    assert not calls and not manager.annotation_service.has_pending(session.document_id)


def test_freetext_product_command_mutates_worker_only_and_updates_revision(m5_pdf_files, manager):
    source = m5_pdf_files / "normal.pdf"
    before = sha256(source.read_bytes()).digest()
    session = opened(manager, source)
    results = collect_results(manager)
    command = CreateFreeText(
        CommandContext.create(session.document_id, session.document_generation),
        RectTarget(0, PdfRect(10, 10, 100, 50)), "M8 fuori scope", FreeTextStyle())
    submission = manager.submit_annotation(command)
    assert submission.state == RequestState.PENDING and session.pending_mutations == 1
    wait_until(lambda: len(results) == 1)
    assert results[0].success
    assert results[0].document_revision == session.document_revision == 1
    assert results[0].annotation.text == "M8 fuori scope"
    assert session.pending_mutations == 0 and session.dirty
    assert sha256(source.read_bytes()).digest() == before


def test_end_to_end_service_worker_success_updates_revision_and_dirty(
        m5_pdf_files, manager, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", synthetic_worker)
    session = opened(manager, m5_pdf_files / "normal.pdf")
    command = highlight(session)
    results = collect_results(manager)
    submission = manager.submit_annotation(command)
    assert submission.state == RequestState.PENDING
    assert session.pending_mutations == 1 and session.dirty
    wait_until(lambda: results)
    result = results[0]
    assert result.outcome == MutationOutcome.SUCCESS
    assert (result.revision_before, result.document_revision) == (0, 1)
    assert result.annotation_id == command.annotation_id
    assert session.document_revision == 1 and session.saved_revision == 0
    assert session.pending_mutations == 0 and session.dirty


def test_ordered_mutations_increment_exactly_once(m5_pdf_files, manager, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", synthetic_worker)
    session = opened(manager, m5_pdf_files / "normal.pdf")
    results = collect_results(manager)
    commands = [highlight(session) for _ in range(3)]
    assert all(manager.submit_annotation(command).state == RequestState.PENDING
               for command in commands)
    wait_until(lambda: len(results) == 3)
    assert [result.command_id for result in results] == [c.context.command_id for c in commands]
    assert [(r.revision_before, r.document_revision) for r in results] == [(0, 1), (1, 2), (2, 3)]
    assert session.document_revision == 3 and session.pending_mutations == 0


def test_duplicate_rejected_by_service_and_authoritatively_by_worker(
        m5_pdf_files, manager, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", synthetic_worker)
    session = opened(manager, m5_pdf_files / "normal.pdf")
    command = highlight(session)
    first = manager.submit_annotation(command)
    duplicate = manager.submit_annotation(command)
    assert first.state == RequestState.PENDING
    assert duplicate.state == RequestState.FAILED
    assert duplicate.result.error.code == MutationErrorCode.DUPLICATE_COMMAND
    wait_until(lambda: session.document_revision == 1 and session.pending_mutations == 0)

    raw = highlight(session)
    payload = command_to_payload(raw)
    first_response = request(session.worker, Operation.ANNOTATION_COMMAND, payload)
    second_response = request(session.worker, Operation.ANNOTATION_COMMAND, payload)
    first_result = result_from_payload(first_response.payload)
    second_result = result_from_payload(second_response.payload)
    assert first_result.success and first_result.document_revision == 2
    assert second_result.error.code == MutationErrorCode.DUPLICATE_COMMAND
    assert second_result.document_revision == 2


def test_multi_document_routing_revision_and_worker_failure_are_isolated(
        m5_pdf_files, manager, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", synthetic_worker)
    first = opened(manager, m5_pdf_files / "normal.pdf")
    second = opened(manager, m5_pdf_files / "alternative.pdf")
    results = collect_results(manager)
    first_command, second_command = highlight(first), highlight(second)
    manager.submit_annotation(first_command)
    manager.submit_annotation(second_command)
    wait_until(lambda: len(results) == 2)
    assert {r.document_id for r in results} == {first.document_id, second.document_id}
    assert first.document_revision == second.document_revision == 1
    assert first.worker.pid != second.worker.pid

    os.kill(first.worker.pid, signal.SIGTERM)
    wait_until(lambda: first.lifecycle == Lifecycle.FAILED)
    third = highlight(second)
    manager.submit_annotation(third)
    wait_until(lambda: second.document_revision == 2)
    assert second.lifecycle == Lifecycle.READY and second.worker.failure is None


def test_invalid_page_and_stale_generation_never_reach_worker(m5_pdf_files, manager):
    session = opened(manager, m5_pdf_files / "normal.pdf")
    submitted = session.worker.submitted_counts[Operation.ANNOTATION_COMMAND]
    invalid = manager.submit_annotation(highlight(session, page=session.page_count))
    assert invalid.state == RequestState.FAILED
    assert invalid.result.error.code == MutationErrorCode.PAGE_OUT_OF_RANGE
    stale_command = highlight(session)
    session.document_generation += 1
    stale = manager.submit_annotation(stale_command)
    assert stale.state == RequestState.STALE
    assert stale.result.error.code == MutationErrorCode.STALE_GENERATION
    assert session.worker.submitted_counts[Operation.ANNOTATION_COMMAND] == submitted
    assert session.document_revision == 0 and not session.dirty


def test_pending_stale_command_does_not_delay_other_document(
        m5_pdf_files, manager, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", delayed_synthetic_worker)
    first = opened(manager, m5_pdf_files / "normal.pdf")
    second = opened(manager, m5_pdf_files / "alternative.pdf")
    results = collect_results(manager)
    manager.submit_annotation(highlight(first))
    manager.submit_annotation(highlight(second))
    first.document_generation += 1
    wait_until(lambda: len(results) == 2)
    by_document = {result.document_id: result for result in results}
    assert by_document[first.document_id].outcome == MutationOutcome.STALE
    assert by_document[first.document_id].error.code == MutationErrorCode.STALE_GENERATION
    assert by_document[second.document_id].outcome == MutationOutcome.SUCCESS
    assert first.document_revision == 0 and second.document_revision == 1


def test_close_while_pending_cancels_without_ghost_revision(
        m5_pdf_files, manager, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", delayed_synthetic_worker)
    session = opened(manager, m5_pdf_files / "normal.pdf")
    source_digest = sha256(Path(session.canonical_path).read_bytes()).digest()
    worker = session.worker
    results = collect_results(manager)
    manager.submit_annotation(highlight(session))
    assert session.pending_mutations == 1
    assert not manager.request_close(session.document_id)
    assert manager.request_close(session.document_id, discard_changes=True)
    assert results[-1].outcome == MutationOutcome.CANCELLED
    assert results[-1].error.code == MutationErrorCode.CANCELLED
    assert session.pending_mutations == 0 and session.document_revision == session.saved_revision
    wait_until(lambda: session.document_id not in manager.sessions)
    assert worker.stopped.is_set()
    assert sha256(Path(session.canonical_path).read_bytes()).digest() == source_digest
    assert session.annotations == {} and session.pages == []
    assert session.document_id not in manager.annotation_service._pending
    assert session.document_id not in manager.annotation_service._completed


def test_worker_death_resolves_pending_failure_without_revision(
        m5_pdf_files, manager, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", delayed_synthetic_worker)
    session = opened(manager, m5_pdf_files / "normal.pdf")
    results = collect_results(manager)
    manager.submit_annotation(highlight(session))
    wait_until(lambda: session.worker.active_request_id is not None)
    os.kill(session.worker.pid, signal.SIGTERM)
    wait_until(lambda: session.lifecycle == Lifecycle.FAILED and results)
    assert results[-1].error.code == MutationErrorCode.WORKER_FAILURE
    assert session.document_revision == 0 and session.pending_mutations == 0


def test_adapter_exception_is_typed_and_does_not_increment_revision(
        m5_pdf_files, manager, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", failing_synthetic_worker)
    session = opened(manager, m5_pdf_files / "normal.pdf")
    results = collect_results(manager)
    manager.submit_annotation(highlight(session))
    wait_until(lambda: results)
    assert results[0].error.code == MutationErrorCode.INTERNAL_PDF_MUTATION
    assert results[0].revision_before == results[0].document_revision == 0
    assert session.document_revision == 0 and not session.dirty


def test_worker_rejects_unknown_and_malformed_payload_without_state_change(
        m5_pdf_files, manager, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", synthetic_worker)
    session = opened(manager, m5_pdf_files / "normal.pdf")
    valid = command_to_payload(highlight(session))
    unknown = dict(valid, kind="execute_anything")
    response = request(session.worker, Operation.ANNOTATION_COMMAND, unknown)
    result = result_from_payload(response.payload)
    assert result.error.code == MutationErrorCode.UNSUPPORTED_COMMAND
    malformed = {"kind": "create_highlight"}
    response = request(session.worker, Operation.ANNOTATION_COMMAND, malformed)
    assert not response.success and response.error.code == MutationErrorCode.INVALID_COMMAND.value
    assert session.document_revision == 0


def test_worker_validation_rejects_stale_geometry_text_and_nan_without_crash(
        m5_pdf_files, manager, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", synthetic_worker)
    session = opened(manager, m5_pdf_files / "normal.pdf")

    stale = command_to_payload(highlight(session))
    stale["document_generation"] = 2
    response = request(session.worker, Operation.ANNOTATION_COMMAND, stale)
    result = result_from_payload(response.payload)
    assert result.outcome == MutationOutcome.STALE
    assert result.error.code == MutationErrorCode.STALE_GENERATION

    outside = command_to_payload(highlight(session))
    outside["target"]["quads"][0][1] = [10_000, 10]
    result = result_from_payload(request(
        session.worker, Operation.ANNOTATION_COMMAND, outside).payload)
    assert result.error.code == MutationErrorCode.INVALID_GEOMETRY

    invalid_text = command_to_payload(highlight(session))
    invalid_text["target"]["original_text"] = "bad\x00text"
    result = result_from_payload(request(
        session.worker, Operation.ANNOTATION_COMMAND, invalid_text).payload)
    assert result.error.code == MutationErrorCode.INVALID_COMMAND

    nonfinite = command_to_payload(highlight(session))
    nonfinite["target"]["quads"][0][0][0] = float("nan")
    before = session.worker.pending_count
    with pytest.raises(ValueError):
        session.worker.submit(Operation.ANNOTATION_COMMAND, nonfinite)
    assert session.worker.pending_count == before

    valid = command_to_payload(highlight(session))
    result = result_from_payload(request(
        session.worker, Operation.ANNOTATION_COMMAND, valid).payload)
    assert result.success and result.document_revision == 1
