import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from pdf_annotator.app.document_manager import DocumentManager
from pdf_annotator.app.document_session import Lifecycle
from pdf_annotator.domain.annotation_commands import (CommandContext, CreateInk,
                                                       DeleteAnnotation, MoveInk)
from pdf_annotator.domain.geometry import PdfPoint
from pdf_annotator.domain.results import RequestState
from pdf_annotator.domain.styles import InkStyle, RgbColor
from pdf_annotator.domain.targets import InkTarget
from pdf_annotator.util.paths import identify_file
from pdf_annotator.worker import document_service
from .conftest import wait_until
from .m7_helpers import persistence_worker


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


def _inspect(path: Path) -> dict:
    completed = subprocess.run(
        [sys.executable, "-m", "tests.m7_helpers", str(path)],
        cwd=Path(__file__).parents[1], check=True, capture_output=True,
        text=True, timeout=30)
    return json.loads(completed.stdout)


def test_create_ink_service_export_and_separate_reopen_preserve_native_pdf(
        tmp_path, manager, monkeypatch, pdf_fixture_copy):
    source = tmp_path / "m7-service.pdf"
    pdf_fixture_copy(source)
    source_bytes = source.read_bytes()
    source_report = _inspect(source)
    monkeypatch.setattr(document_service, "document_worker_main", persistence_worker)
    session = _opened(manager, source)
    style = InkStyle(RgbColor(.08, .32, .86), 2.75, .57)
    target = InkTarget(0, ((PdfPoint(22, 32), PdfPoint(50, 65),
                            PdfPoint(90, 38)),))
    command = CreateInk(CommandContext.create(session.document_id, 1), target, style)
    results = []
    manager.annotation_result.connect(results.append)
    submission = manager.submit_annotation(command)
    assert submission.state == RequestState.PENDING
    wait_until(lambda: len(results) == 1)
    assert results[0].success and session.document_revision == 1 and session.dirty
    assert session.annotations[command.annotation_id.value] == results[0].annotation

    export = Path(str(source) + ".m7-export.pdf")
    report = _inspect(export)
    record = report["pages"][0][0]
    assert record["subtype"] == ["name", "/Ink"]
    assert record["ink_list"][0] == "array"
    assert command.annotation_id.value in record["nm"][1]
    assert [value for point in record["vertices"][0] for value in point] == pytest.approx(
        [value for point in target.paths[0] for value in (point.x, point.y)])
    assert record["colors"]["stroke"] == pytest.approx([.08, .32, .86])
    assert record["border"]["width"] == pytest.approx(2.75)
    assert record["opacity"] == pytest.approx(.57, abs=.01)
    assert report["content_hash"] == source_report["content_hash"]
    assert report["text"] == source_report["text"]
    assert source.read_bytes() == source_bytes
    assert hashlib.sha256(source.read_bytes()).digest() == hashlib.sha256(source_bytes).digest()


def test_rotated_cropbox_ink_geometry_persists_on_own_pages(
        tmp_path, manager, monkeypatch, pdf_fixture_copy):
    source = tmp_path / "m7-rotated-cropbox.pdf"
    pdf_fixture_copy(source, geometry=True)
    monkeypatch.setattr(document_service, "document_worker_main", persistence_worker)
    session = _opened(manager, source)
    results = []
    manager.annotation_result.connect(results.append)
    commands = []
    for page_index in range(4):
        target = InkTarget(page_index, ((PdfPoint(18, 25), PdfPoint(45, 55),
                                         PdfPoint(80, 30)),))
        command = CreateInk(CommandContext.create(session.document_id, 1), target,
                            InkStyle(RgbColor(.12, .58, .22), 1.5, 1.0))
        commands.append(command)
        manager.submit_annotation(command)
        wait_until(lambda: len(results) == page_index + 1)
        assert results[-1].success
    report = _inspect(Path(str(source) + ".m7-export.pdf"))
    assert [len(records) for records in report["pages"]] == [1, 1, 1, 1]
    for records, command in zip(report["pages"], commands, strict=True):
        record = records[0]
        assert record["subtype"] == ["name", "/Ink"]
        assert [value for point in record["vertices"][0] for value in point] == pytest.approx(
            [value for point in command.target.paths[0]
             for value in (point.x, point.y)])
        assert command.annotation_id.value in record["nm"][1]


def test_delete_app_ink_removes_native_annotation_after_export(
        tmp_path, manager, monkeypatch, pdf_fixture_copy):
    source = tmp_path / "m7-delete.pdf"
    pdf_fixture_copy(source)
    monkeypatch.setattr(document_service, "document_worker_main", persistence_worker)
    session = _opened(manager, source)
    results = []
    manager.annotation_result.connect(results.append)
    create = CreateInk(
        CommandContext.create(session.document_id, 1),
        InkTarget(0, ((PdfPoint(20, 30), PdfPoint(50, 60), PdfPoint(85, 35)),)),
        InkStyle(RgbColor(.8, .1, .1), 1.5, 1.0))
    manager.submit_annotation(create)
    wait_until(lambda: session.document_revision == 1)
    assert len(_inspect(Path(str(source) + ".m7-export.pdf"))["pages"][0]) == 1

    delete = DeleteAnnotation(CommandContext.create(session.document_id, 1),
                              create.annotation_id, 0)
    manager.submit_annotation(delete)
    wait_until(lambda: session.document_revision == 2)
    assert results[-1].success and not session.annotations
    assert _inspect(Path(str(source) + ".m7-export.pdf"))["pages"][0] == []


def test_move_ink_service_export_preserves_nm_style_and_translates_only_path(
        tmp_path, manager, monkeypatch, pdf_fixture_copy):
    source = tmp_path / "m71-move.pdf"
    pdf_fixture_copy(source)
    source_report = _inspect(source)
    monkeypatch.setattr(document_service, "document_worker_main", persistence_worker)
    session = _opened(manager, source)
    style = InkStyle(RgbColor(.21, .44, .77), 2.75, .57)
    target = InkTarget(0, ((PdfPoint(22, 32), PdfPoint(50, 65), PdfPoint(90, 38)),))
    create = CreateInk(CommandContext.create(session.document_id, 1), target, style)
    manager.submit_annotation(create)
    wait_until(lambda: session.document_revision == 1)
    move = MoveInk(CommandContext.create(session.document_id, 1), create.annotation_id,
                   0, 31, 17)
    manager.submit_annotation(move)
    wait_until(lambda: session.document_revision == 2)

    record = _inspect(Path(str(source) + ".m7-export.pdf"))["pages"][0][0]
    assert record["subtype"] == ["name", "/Ink"]
    assert create.annotation_id.value in record["nm"][1]
    assert record["colors"]["stroke"] == pytest.approx([.21, .44, .77])
    assert record["border"]["width"] == pytest.approx(2.75)
    assert record["opacity"] == pytest.approx(.57, abs=.01)
    assert [value for point in record["vertices"][0] for value in point] == pytest.approx(
        [value for point in ((53, 49), (81, 82), (121, 55)) for value in point])
    assert session.annotations[create.annotation_id.value].annotation_id == create.annotation_id
    assert session.annotations[create.annotation_id.value].backing.pdf_nm == create.annotation_id.value
    report = _inspect(Path(str(source) + ".m7-export.pdf"))
    assert report["content_hash"] == source_report["content_hash"]
    assert report["text"] == source_report["text"]
