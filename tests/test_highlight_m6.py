from pathlib import Path

import pytest

from pdf_annotator.domain.annotation_commands import (CommandContext, CommandSource,
                                                       CreateHighlight)
from pdf_annotator.domain.results import MutationOutcome
from pdf_annotator.domain.serialization import (command_to_payload, result_from_payload,
                                                target_from_payload)
from pdf_annotator.domain.styles import HighlightStyle, RgbColor
from pdf_annotator.util.paths import identify_file
from pdf_annotator.worker.client import RasterBudget, WorkerClient
from pdf_annotator.worker.document_service import DocumentSource
from pdf_annotator.worker.protocol import Operation
from .conftest import request, wait_until
from .m6_fixtures import quad_center


@pytest.fixture
def highlight_client(tmp_path: Path, app, pdf_fixture_copy):
    path = tmp_path / "m6-highlight.pdf"
    pdf_fixture_copy(path)
    client = WorkerClient("highlight-doc", DocumentSource(identify_file(str(path))),
                          RasterBudget())
    assert request(client, Operation.OPEN_DOCUMENT).success
    yield client
    client.close_async()
    wait_until(client.stopped.is_set)


def _confirmed_target(client, page: int = 0, start: int = 0, end: int = 8):
    page_text, characters, _ = _page_records(client, page)
    reply = request(client, Operation.TEXT_SELECTION, {
        "page_index": page,
        "page_text_id": page_text["page_text_id"],
        "anchor": quad_center(characters[start][2]),
        "focus": quad_center(characters[end][2]),
    })
    assert reply.success, reply.error
    return target_from_payload(reply.payload["target"])


def _page_records(client, page=0):
    page_text = request(client, Operation.TEXT_PAGE_INDEX,
                        {"page_index": page}).payload["page_text"]
    chunks = [request(client, Operation.TEXT_CHUNK, {
        "page_index": page, "page_text_id": page_text["page_text_id"],
        "chunk_index": index}).payload["chunk"]
        for index in range(len(page_text["chunks"]))]
    return (page_text,
            [record for chunk in chunks for record in chunk["characters"]],
            [record for chunk in chunks for record in chunk["lines"]])


def _command(client, target, *, command_id=None, color=RgbColor(.2, .8, .1)):
    context = (CommandContext(command_id, client.document_id, client.generation,
                              CommandSource.UI)
               if command_id else
               CommandContext.create(client.document_id, client.generation,
                                     CommandSource.UI))
    return CreateHighlight(context, target, HighlightStyle(color, .42))


def test_native_highlight_returns_confirmed_model_style_identity_and_revision(highlight_client):
    command = _command(highlight_client, _confirmed_target(highlight_client))
    response = request(highlight_client, Operation.ANNOTATION_COMMAND,
                       command_to_payload(command))
    assert response.success, response.error
    result = result_from_payload(response.payload)
    assert result.success and result.outcome == MutationOutcome.SUCCESS
    assert (result.revision_before, result.document_revision) == (0, 1)
    assert result.annotation_id == command.annotation_id
    assert result.annotation is not None
    assert result.annotation.target == command.target
    assert result.annotation.style == command.style
    assert result.annotation.backing.pdf_nm == command.annotation_id.value
    assert result.annotation.backing.xref > 0


def test_two_highlights_increment_revision_and_duplicate_does_not(highlight_client):
    first = _command(highlight_client, _confirmed_target(highlight_client, start=0, end=4))
    second = _command(highlight_client, _confirmed_target(highlight_client, start=10, end=15))
    one = result_from_payload(request(highlight_client, Operation.ANNOTATION_COMMAND,
                                      command_to_payload(first)).payload)
    two = result_from_payload(request(highlight_client, Operation.ANNOTATION_COMMAND,
                                      command_to_payload(second)).payload)
    duplicate = result_from_payload(request(highlight_client, Operation.ANNOTATION_COMMAND,
                                            command_to_payload(second)).payload)
    assert (one.document_revision, two.document_revision) == (1, 2)
    assert not duplicate.success
    assert duplicate.error.code.value == "duplicate_command"
    assert duplicate.revision_before == duplicate.document_revision == 2


def test_forward_and_reverse_ranges_create_identical_quad_geometry(highlight_client):
    page_text, characters, _ = _page_records(highlight_client)
    first = quad_center(characters[0][2])
    last = quad_center(characters[9][2])

    def resolve(anchor, focus):
        reply = request(highlight_client, Operation.TEXT_SELECTION, {
            "page_index": 0, "page_text_id": page_text["page_text_id"],
            "anchor": anchor, "focus": focus})
        return target_from_payload(reply.payload["target"])

    forward, reverse = resolve(first, last), resolve(last, first)
    assert forward.original_text == reverse.original_text == "Prima riga"
    assert forward.quads == reverse.quads
    first_result = result_from_payload(request(
        highlight_client, Operation.ANNOTATION_COMMAND,
        command_to_payload(_command(highlight_client, forward))).payload)
    second_result = result_from_payload(request(
        highlight_client, Operation.ANNOTATION_COMMAND,
        command_to_payload(_command(highlight_client, reverse))).payload)
    assert first_result.success and second_result.success
    assert first_result.annotation.target.quads == second_result.annotation.target.quads


def test_forged_and_invalid_targets_do_not_mutate_revision(highlight_client):
    target = _confirmed_target(highlight_client)
    forged = type(target).create(target.page_index, target.quads, target.original_text,
                                 snapshot_id=target.snapshot_id,
                                 start_char_index=target.start_char_index,
                                 end_char_index=target.end_char_index)
    result = result_from_payload(request(highlight_client, Operation.ANNOTATION_COMMAND,
                                         command_to_payload(_command(highlight_client, forged))).payload)
    assert not result.success
    assert result.error.code.value == "stale_text_target"
    assert result.document_revision == 0


def test_out_of_range_text_requests_are_typed_and_worker_survives(highlight_client):
    reply = request(highlight_client, Operation.TEXT_PAGE_INDEX, {"page_index": 999})
    assert not reply.success and reply.error.code == "page_out_of_range"
    valid = request(highlight_client, Operation.TEXT_PAGE_INDEX, {"page_index": 0})
    assert valid.success


def test_vertical_text_quad_creates_native_highlight(tmp_path, app, pdf_fixture_copy):
    path = tmp_path / "vertical.pdf"
    pdf_fixture_copy(path, geometry=True)
    client = WorkerClient("vertical-doc", DocumentSource(identify_file(str(path))),
                          RasterBudget())
    try:
        assert request(client, Operation.OPEN_DOCUMENT).success
        page_text, characters, lines = _page_records(client)
        line = next(record for record in lines if record[4][1] < -.99)
        start, end = line[2], line[3] - 1
        reply = request(client, Operation.TEXT_SELECTION, {
            "page_index": 0, "page_text_id": page_text["page_text_id"],
            "anchor": quad_center(characters[start][2]),
            "focus": quad_center(characters[end][2])})
        target = target_from_payload(reply.payload["target"])
        result = result_from_payload(request(
            client, Operation.ANNOTATION_COMMAND,
            command_to_payload(_command(client, target))).payload)
        assert result.success and result.annotation.target.quads == target.quads
    finally:
        client.close_async()
        wait_until(client.stopped.is_set)
