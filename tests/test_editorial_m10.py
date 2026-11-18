"""M10 inspector and authoritative rejection of unconfirmed editorial targets."""

import base64
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

import pytest

from pdf_annotator.app.document_manager import DocumentManager
from pdf_annotator.domain.annotation_commands import (
    CommandContext, CommandSource, CreateDeleteText, CreateInsertText, CreateReplaceText,
)
from pdf_annotator.domain.errors import MutationErrorCode
from pdf_annotator.domain.geometry import PdfPoint
from pdf_annotator.domain.results import RequestState
from pdf_annotator.domain.serialization import command_from_payload, command_to_payload
from pdf_annotator.domain.targets import BoundaryAffinity, TextInsertionTarget
from pdf_annotator.spikes.editorial_inspector import EDITORIAL_KEYS, indirect_references
from pdf_annotator.worker import document_service
from .conftest import wait_until
from .m10_helpers import rejection_worker
from .test_m6_service_persistence import _opened, _target


def _run(*args):
    return subprocess.run([sys.executable, *map(str, args)], capture_output=True,
                          text=True, encoding="utf-8", timeout=30)


@pytest.fixture
def graph_fixture(tmp_path):
    path = tmp_path / "synthetic-inspector-only.pdf"
    result = _run("-m", "tests.m10_helpers", path)
    assert result.returncode == 0, result.stderr
    return path, json.loads(result.stdout)


@pytest.mark.parametrize("source, expected", [
    ("<< /IRT 8 0 R /Nested [9 2 R << /P 8 0 R >>] >>", ((8, 0), (9, 2))),
    (r"<< /Contents (999 0 R (888 0 R) \(777 0 R\)) /NM /123 /IRT 4 0 R >>", ((4, 0),)),
    ("[<393920302052> /R /123 0 R 9 % 40 0 R\n 0 R]", ((9, 0),)),
    ("[12 (0) R 13 <30> R 14 /0 R 15 0 /R]", ()),
])
def test_reference_lexer_ignores_strings_names_and_comments(source, expected):
    assert indirect_references(source) == expected


def test_inspector_follows_popup_reply_private_array_string_and_appearance(graph_fixture):
    path, ids = graph_fixture
    before = path.read_bytes()
    result = _run("-m", "pdf_annotator.spikes.editorial_inspector", path, "--include-streams")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["source_unchanged"] and path.read_bytes() == before
    assert report["sha256"] == sha256(before).hexdigest()
    assert "unclassified" in report["semantics"]
    records = {item["xref"]: item for item in report["annotations"]}
    assert {ids["root"], ids["reply"], ids["popup"]} <= records.keys()
    root = records[ids["root"]]
    assert root["api"]["info"]["content"] == ids["payload"]
    assert set(root["editorial_keys"]) == set(EDITORIAL_KEYS)
    assert root["editorial_keys"]["IT"] == {"type": "null", "value": "null"}
    assert records[ids["reply"]]["editorial_keys"]["IRT"]["value"] == f'{ids["root"]} 0 R'
    assert records[ids["popup"]]["editorial_keys"]["Parent"]["value"] == f'{ids["root"]} 0 R'
    assert root["api_nm"] == records[ids["reply"]]["api_nm"]
    objects = {item["xref"]: item for item in report["objects"]}
    assert {ids[key] for key in ("root", "reply", "popup", "private", "array", "text_object")} <= objects.keys()
    assert 999999 not in objects
    assert "FEFF" in objects[ids["text_object"]]["serialized_object"].upper()
    assert report["pages"][0]["rotation"] == 90
    assert report["pages"][0]["cropbox"] == [20, 30, 480, 380]
    assert objects[report["pages"][0]["xref"]]["traversal_boundary"] == "document_structure"
    streams = [item["stream"] for item in objects.values() if "stream" in item]
    assert streams
    for stream in streams:
        decoded = base64.b64decode(stream["decoded_base64"])
        assert sha256(decoded).hexdigest() == stream["decoded_sha256"]
        assert len(decoded) == stream["decoded_bytes"]


def test_inspector_utf8_output_is_exclusive_and_source_cannot_be_overwritten(graph_fixture, tmp_path):
    path, ids = graph_fixture
    output = tmp_path / "inspection.json"
    result = _run("-m", "pdf_annotator.spikes.editorial_inspector", path, "--output", output)
    assert result.returncode == 0, result.stderr
    text = output.read_text(encoding="utf-8")
    assert ids["payload"] in text
    report = json.loads(text)
    assert all("decoded_base64" not in obj.get("stream", {}) for obj in report["objects"])
    before = path.read_bytes()
    for destination in (output, path):
        result = _run("-m", "pdf_annotator.spikes.editorial_inspector", path, "--output", destination)
        assert result.returncode == 1 and "FileExistsError" in result.stderr
    assert path.read_bytes() == before and output.read_text(encoding="utf-8") == text


def test_inspector_missing_input_fails_without_output(tmp_path):
    output = tmp_path / "missing.json"
    result = _run("-m", "pdf_annotator.spikes.editorial_inspector", tmp_path / "missing.pdf",
                  "--output", output)
    assert result.returncode == 1 and not output.exists()


def test_editorial_dtos_service_and_real_worker_remain_fail_closed(
        app, graph_fixture, monkeypatch):
    source, _ = graph_fixture
    before = sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(document_service, "document_worker_main", rejection_worker)
    manager = DocumentManager()
    worker = None
    results = []
    manager.annotation_result.connect(results.append)
    try:
        session = _opened(manager, source)
        worker = session.worker
        target = replace(_target(session), target_id=CommandContext.create("target", 1).command_id)
        assert target.snapshot_id and target.start_char_index is not None
        payload = "  città, perché, Über, 日本語 & <ok> e\u0301 ①\n"
        def context():
            return CommandContext.create(session.document_id, session.document_generation,
                                         CommandSource.INTERNAL)
        # The insertion DTO is a placeholder, not a worker-confirmed caret.
        insertion = TextInsertionTarget.create(
            0, PdfPoint(target.quads[0].upper_left.x, target.quads[0].upper_left.y),
            BoundaryAffinity.BEFORE, context_after=target.original_text)
        commands = (CreateDeleteText(context(), target),
                    CreateInsertText(context(), insertion, payload),
                    CreateReplaceText(context(), target, payload))
        assert len({command.kind for command in commands}) == 3
        for index, command in enumerate(commands, 1):
            wire = json.loads(json.dumps(command_to_payload(command), ensure_ascii=True))
            assert command_from_payload(wire) == command
            assert manager.submit_annotation(command).state == RequestState.PENDING
            wait_until(lambda: len(results) == index)
            result = results[-1]
            assert result.error.code == MutationErrorCode.STALE_TEXT_TARGET
            assert result.revision_before == result.document_revision == 0
            assert result.annotation is None and result.annotation_id is None
            assert session.pending_mutations == 0 and not session.dirty
            assert not session.annotations
        stale = replace(commands[0], context=CommandContext.create(session.document_id, 2))
        assert manager.submit_annotation(stale).result.error.code == MutationErrorCode.STALE_GENERATION
        wrong = replace(commands[0], context=CommandContext.create("another-document", 1))
        assert manager.submit_annotation(wrong).result.error.code == MutationErrorCode.DOCUMENT_NOT_READY
        assert sha256(source.read_bytes()).hexdigest() == before
    finally:
        manager.request_shutdown()
        wait_until(lambda: manager.is_shutdown)
    assert worker is not None and worker.stopped.is_set()
