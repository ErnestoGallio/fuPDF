from pathlib import Path

import pytest

from pdf_annotator.domain.annotation_commands import (CommandContext, CommandSource,
                                                       CreateInk, DeleteAnnotation, MoveInk)
from pdf_annotator.domain.geometry import PdfPoint
from pdf_annotator.domain.results import MutationOutcome
from pdf_annotator.domain.serialization import command_to_payload, result_from_payload
from pdf_annotator.domain.styles import InkStyle, RgbColor
from pdf_annotator.domain.targets import InkTarget
from pdf_annotator.util.paths import identify_file
from pdf_annotator.worker.client import RasterBudget, WorkerClient
from pdf_annotator.worker.document_service import DocumentSource
from pdf_annotator.worker.protocol import Operation
from .conftest import request, wait_until


@pytest.fixture
def ink_client(tmp_path: Path, app, pdf_fixture_copy):
    path = tmp_path / "m7-ink.pdf"
    pdf_fixture_copy(path)
    client = WorkerClient("ink-doc", DocumentSource(identify_file(str(path))),
                          RasterBudget())
    assert request(client, Operation.OPEN_DOCUMENT).success
    yield client
    client.close_async()
    wait_until(client.stopped.is_set)


def _command(client, *, command_id=None, page=0, points=None,
             style=InkStyle(RgbColor(.86, .12, .12), 2.25, .63)):
    context = (CommandContext(command_id, client.document_id, client.generation,
                              CommandSource.UI)
               if command_id else CommandContext.create(client.document_id, client.generation))
    return CreateInk(context, InkTarget(page, (points or (
        PdfPoint(10, 20), PdfPoint(30, 40), PdfPoint(55, 22)),)), style)


def test_native_ink_returns_authoritative_model_style_identity_and_revision(ink_client):
    command = _command(ink_client)
    response = request(ink_client, Operation.ANNOTATION_COMMAND,
                       command_to_payload(command))
    result = result_from_payload(response.payload)
    assert result.success and result.outcome == MutationOutcome.SUCCESS
    assert (result.revision_before, result.document_revision) == (0, 1)
    assert result.annotation.target == command.target
    assert result.annotation.style == command.style
    assert result.annotation.backing.pdf_nm == command.annotation_id.value
    assert result.annotation.backing.xref > 0


def test_two_inks_increment_and_duplicate_or_invalid_do_not(ink_client):
    first = _command(ink_client)
    second = _command(ink_client, points=(PdfPoint(100, 100), PdfPoint(120, 130)))
    one = result_from_payload(request(ink_client, Operation.ANNOTATION_COMMAND,
                                      command_to_payload(first)).payload)
    two = result_from_payload(request(ink_client, Operation.ANNOTATION_COMMAND,
                                      command_to_payload(second)).payload)
    duplicate = result_from_payload(request(ink_client, Operation.ANNOTATION_COMMAND,
                                            command_to_payload(second)).payload)
    assert (one.document_revision, two.document_revision) == (1, 2)
    assert not duplicate.success and duplicate.error.code.value == "duplicate_command"
    assert duplicate.document_revision == 2

    invalid = _command(ink_client, page=999)
    rejected = result_from_payload(request(ink_client, Operation.ANNOTATION_COMMAND,
                                           command_to_payload(invalid)).payload)
    assert not rejected.success and rejected.error.code.value == "page_out_of_range"
    assert rejected.document_revision == 2


def test_worker_deletes_only_ink_created_in_same_document_session(ink_client):
    create = _command(ink_client)
    created = result_from_payload(request(
        ink_client, Operation.ANNOTATION_COMMAND, command_to_payload(create)).payload)
    delete = DeleteAnnotation(CommandContext.create(ink_client.document_id,
                                                     ink_client.generation),
                              create.annotation_id, 0)
    deleted = result_from_payload(request(
        ink_client, Operation.ANNOTATION_COMMAND, command_to_payload(delete)).payload)
    duplicate = DeleteAnnotation(CommandContext.create(ink_client.document_id,
                                                        ink_client.generation),
                                 create.annotation_id, 0)
    rejected = result_from_payload(request(
        ink_client, Operation.ANNOTATION_COMMAND, command_to_payload(duplicate)).payload)
    assert created.document_revision == 1
    assert deleted.success and deleted.annotation is None and deleted.document_revision == 2
    assert not rejected.success and rejected.error.code.value == "read_only_annotation"
    assert rejected.document_revision == 2


def test_app_created_ink_move_preserves_identity_style_and_path_shape(ink_client):
    create = _command(ink_client)
    created = result_from_payload(request(
        ink_client, Operation.ANNOTATION_COMMAND, command_to_payload(create)).payload)
    move = MoveInk(CommandContext.create(ink_client.document_id, ink_client.generation),
                   create.annotation_id, 0, 42.5, -8.0)
    moved = result_from_payload(request(
        ink_client, Operation.ANNOTATION_COMMAND, command_to_payload(move)).payload)
    assert created.success and moved.success
    assert (moved.revision_before, moved.document_revision) == (1, 2)
    assert moved.annotation.annotation_id == create.annotation_id
    assert moved.annotation.backing.pdf_nm == create.annotation_id.value
    assert moved.annotation.style == create.style
    assert moved.annotation.target.paths == ((PdfPoint(52.5, 12.0),
                                               PdfPoint(72.5, 32.0),
                                               PdfPoint(97.5, 14.0)),)

    duplicate = result_from_payload(request(
        ink_client, Operation.ANNOTATION_COMMAND, command_to_payload(move)).payload)
    assert not duplicate.success and duplicate.error.code.value == "duplicate_command"
    assert duplicate.document_revision == 2
