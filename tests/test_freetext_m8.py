import json
from pathlib import Path
import subprocess
import sys

import pytest

from pdf_annotator.app.document_manager import DocumentManager
from pdf_annotator.app.document_session import Lifecycle
from pdf_annotator.domain.annotation_commands import (CommandContext, CreateFreeText,
                                                       DeleteAnnotation, MoveFreeText,
                                                       ResizeFreeText, TransferFreeText,
                                                       UpdateFreeText)
from pdf_annotator.domain.errors import MutationErrorCode
from pdf_annotator.domain.geometry import PdfRect
from pdf_annotator.domain.results import RequestState
from pdf_annotator.domain.styles import FreeTextStyle, RgbColor
from pdf_annotator.domain.targets import RectTarget
from pdf_annotator.util.paths import identify_file
from pdf_annotator.worker import document_service
from .conftest import wait_until
from .m8_helpers import persistence_worker


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
    result = subprocess.run([sys.executable, "-m", "tests.m8_helpers", str(path)],
                            cwd=Path(__file__).parents[1], capture_output=True,
                            text=True, check=True, timeout=30)
    return json.loads(result.stdout)


def _style() -> FreeTextStyle:
    return FreeTextStyle(RgbColor(.86, .12, .12), RgbColor(.79, .89, 1),
                         RgbColor(.12, .58, .22), .63, 1.25, 12)


def _create(session, text="Testo di revisione"):
    return CreateFreeText(CommandContext.create(session.document_id, session.document_generation),
                          RectTarget(0, PdfRect(30, 40, 270, 140)), text, _style())


def test_create_persists_native_freetext_plain_text_independent_style_and_nm(
        tmp_path, manager, monkeypatch, pdf_fixture_copy):
    source = tmp_path / "m8.pdf"
    pdf_fixture_copy(source)
    before = _inspect(source)
    monkeypatch.setattr(document_service, "document_worker_main", persistence_worker)
    session = _opened(manager, source)
    command = _create(session, "caffè & tè <ok>\nseconda riga")
    result = manager.submit_annotation(command)
    assert result.state == RequestState.PENDING
    wait_until(lambda: session.document_revision == 1)
    report = _inspect(Path(str(source) + ".m8-export.pdf"))
    record = report["records"][0]
    assert record["subtype"] == ["name", "/FreeText"]
    assert command.annotation_id.value in record["nm"][1]
    assert record["text"] == command.text and record["contents"][1] == command.text
    assert "&lt;ok&gt;" in record["rc"][1] and "&amp;" in record["rc"][1]
    assert record["rect"] == pytest.approx([30, 40, 270, 140], abs=2)
    # PyMuPDF exposes rich-text FreeText's fill through the native appearance
    # color channel rather than ``Annot.colors['fill']`` on this pinned build.
    assert ".79" in record["c"][1] and ".89" in record["c"][1]
    assert record["border"]["width"] == pytest.approx(1.25)
    assert record["opacity"] == pytest.approx(.63, abs=.01)
    # Acrobat's rich FreeText profile carries fill in /C, border/fallback in
    # /DA and run-level text color in valid XHTML /RC.  The appearance must
    # agree with all three semantic values rather than borrowing text color
    # for the border.
    assert ".86" in record["da"][1] and ".12" in record["da"][1]
    assert "#1f9438" in record["rc"][1]
    assert 'xmlns="http://www.w3.org/1999/xhtml"' in record["rc"][1]
    assert "1999/xtml" not in record["rc"][1]
    assert ".79 .89 1 rg" in record["appearance"]
    assert ".86 .12 .12 RG" in record["appearance"]
    # The first baseline is a normal small inset from the top; the old
    # malformed rich wrapper emitted a leading blank line and suspended text.
    assert "2.5 13.3 Tm" in record["appearance"]
    assert before["page_text"] in report["page_text"]
    assert report["content_hash"] == before["content_hash"]


def test_empty_freetext_persists_as_empty_native_contents(tmp_path, manager, monkeypatch,
                                                           pdf_fixture_copy):
    source = tmp_path / "m8-empty.pdf"
    pdf_fixture_copy(source)
    monkeypatch.setattr(document_service, "document_worker_main", persistence_worker)
    session = _opened(manager, source)
    command = _create(session, "")
    assert manager.submit_annotation(command).state == RequestState.PENDING
    wait_until(lambda: session.document_revision == 1)
    model = session.annotations[command.annotation_id.value]
    record = _inspect(Path(str(source) + ".m8-export.pdf"))["records"][0]
    assert model.text == model.rich_text.plain == ""
    assert record["contents"] == ["string", ""] and record["text"] == ""


def test_edit_move_resize_delete_and_overflow_are_one_revision_each(
        tmp_path, manager, monkeypatch, pdf_fixture_copy):
    source = tmp_path / "m8-edit.pdf"
    pdf_fixture_copy(source)
    monkeypatch.setattr(document_service, "document_worker_main", persistence_worker)
    session = _opened(manager, source)
    create = _create(session)
    manager.submit_annotation(create); wait_until(lambda: session.document_revision == 1)
    update = UpdateFreeText(CommandContext.create(session.document_id, 1), create.annotation_id, 0,
                            "Testo modificato", _style())
    manager.submit_annotation(update); wait_until(lambda: session.document_revision == 2)
    move = MoveFreeText(CommandContext.create(session.document_id, 1), create.annotation_id, 0, 25, 18)
    manager.submit_annotation(move); wait_until(lambda: session.document_revision == 3)
    resize = ResizeFreeText(CommandContext.create(session.document_id, 1), create.annotation_id,
                            RectTarget(0, PdfRect(55, 58, 330, 185)))
    manager.submit_annotation(resize); wait_until(lambda: session.document_revision == 4)
    report = _inspect(Path(str(source) + ".m8-export.pdf"))
    assert report["records"][0]["text"] == "Testo modificato"
    assert report["records"][0]["rect"] == pytest.approx([55, 58, 330, 185], abs=2)
    overflow = ResizeFreeText(CommandContext.create(session.document_id, 1), create.annotation_id,
                              RectTarget(0, PdfRect(55, 58, 75, 76)))
    results = []
    manager.annotation_result.connect(results.append)
    resized_small = manager.submit_annotation(overflow)
    assert resized_small.state == RequestState.PENDING
    wait_until(lambda: session.document_revision == 5)
    assert results[-1].success
    assert session.annotations[create.annotation_id.value].target.rect == overflow.target.rect
    delete = DeleteAnnotation(CommandContext.create(session.document_id, 1), create.annotation_id, 0)
    manager.submit_annotation(delete); wait_until(lambda: session.document_revision == 6)
    assert _inspect(Path(str(source) + ".m8-export.pdf"))["records"] == []


def test_stale_and_duplicate_freetext_commands_do_not_mutate(
        tmp_path, manager, monkeypatch, pdf_fixture_copy):
    source = tmp_path / "m8-stale.pdf"
    pdf_fixture_copy(source)
    monkeypatch.setattr(document_service, "document_worker_main", persistence_worker)
    session = _opened(manager, source)
    command = _create(session)
    first = manager.submit_annotation(command)
    duplicate = manager.submit_annotation(command)
    assert first.state == RequestState.PENDING and duplicate.result.error.code == MutationErrorCode.DUPLICATE_COMMAND
    wait_until(lambda: session.document_revision == 1)
    stale = CreateFreeText(CommandContext.create(session.document_id, 2),
                            RectTarget(0, PdfRect(10, 10, 220, 120)), "stale", _style())
    stale_result = manager.submit_annotation(stale)
    assert stale_result.result.error.code == MutationErrorCode.STALE_GENERATION
    assert session.document_revision == 1


@pytest.mark.parametrize(("family", "resource", "css_family"),
                         (("Helvetica", "/Helv", "Helvetica"),
                          ("Times", "/TiRo", "Times"),
                          ("Courier", "/Cour", "'Courier Std'")))
def test_font_family_and_size_persist_in_style_rc_and_da(
        tmp_path, manager, monkeypatch, pdf_fixture_copy, family, resource, css_family):
    source = tmp_path / f"m8-font-{family}.pdf"
    pdf_fixture_copy(source)
    monkeypatch.setattr(document_service, "document_worker_main", persistence_worker)
    session = _opened(manager, source)
    style = FreeTextStyle(font_size=17, font_family=family)
    command = CreateFreeText(CommandContext.create(session.document_id, session.document_generation),
                             RectTarget(0, PdfRect(30, 40, 300, 160)), "Font persistente", style)
    manager.submit_annotation(command)
    wait_until(lambda: session.document_revision == 1)
    model = session.annotations[command.annotation_id.value]
    assert model.style == style
    record = _inspect(Path(str(source) + ".m8-export.pdf"))["records"][0]
    assert resource in record["da"][1] and "17" in record["da"][1]
    assert f"font-family:{css_family}" in record["rc"][1]
    assert f"font: {css_family},sans-serif 17.0pt" in record["ds"][1]


def test_transfer_freetext_is_one_revision_and_preserves_identity_style_and_nm(
        tmp_path, manager, monkeypatch, pdf_fixture_copy):
    source = tmp_path / "m8-transfer.pdf"
    pdf_fixture_copy(source, pages=2, geometry=True)
    monkeypatch.setattr(document_service, "document_worker_main", persistence_worker)
    session = _opened(manager, source)
    create = _create(session, "Trasferisci")
    manager.submit_annotation(create)
    wait_until(lambda: session.document_revision == 1)
    target = RectTarget(1, PdfRect(45, 60, 285, 170))
    transfer = TransferFreeText(CommandContext.create(session.document_id, session.document_generation),
                                create.annotation_id, 0, target)
    manager.submit_annotation(transfer)
    wait_until(lambda: session.document_revision == 2)
    model = session.annotations[create.annotation_id.value]
    assert model.annotation_id == create.annotation_id
    assert model.target == target and model.style == create.style and model.text == create.text
    records = _inspect(Path(str(source) + ".m8-export.pdf"))["records"]
    assert len(records) == 1
    assert records[0]["page_index"] == 1
    assert create.annotation_id.value in records[0]["nm"][1]
    assert records[0]["rect"] == pytest.approx([45, 60, 285, 170], abs=2)


def test_transfer_allows_overflow_and_preserves_the_original_content(
        tmp_path, manager, monkeypatch, pdf_fixture_copy):
    source = tmp_path / "m8-transfer-rollback.pdf"
    pdf_fixture_copy(source, pages=2)
    monkeypatch.setattr(document_service, "document_worker_main", persistence_worker)
    session = _opened(manager, source)
    create = _create(session, "Testo che non entra nella destinazione")
    manager.submit_annotation(create)
    wait_until(lambda: session.document_revision == 1)
    original = session.annotations[create.annotation_id.value]
    target = RectTarget(1, PdfRect(20, 20, 55, 50))
    manager.submit_annotation(TransferFreeText(
        CommandContext.create(session.document_id, session.document_generation),
        create.annotation_id, 0, target))
    wait_until(lambda: session.document_revision == 2)
    moved = session.annotations[create.annotation_id.value]
    assert moved.target == target and moved.text == original.text


def test_long_text_and_large_font_save_reopen_without_changing_geometry(
        tmp_path, manager, monkeypatch, pdf_fixture_copy):
    source = tmp_path / "m8-font-overflow.pdf"
    pdf_fixture_copy(source)
    monkeypatch.setattr(document_service, "document_worker_main", persistence_worker)
    session = _opened(manager, source)
    target = RectTarget(0, PdfRect(30, 40, 150, 95))
    text = "contenuto molto lungo " * 24
    style = FreeTextStyle(font_size=48, font_family="Courier")
    command = CreateFreeText(CommandContext.create(session.document_id, session.document_generation),
                             target, text, style)
    manager.submit_annotation(command)
    wait_until(lambda: session.document_revision == 1)
    model = session.annotations[command.annotation_id.value]
    assert model.target == target and model.text == text and model.style == style
    record = _inspect(Path(str(source) + ".m8-export.pdf"))["records"][0]
    assert record["contents"] == ["string", text]
    assert record["rect"] == pytest.approx([30, 40, 150, 95], abs=2)
