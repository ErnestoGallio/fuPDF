"""Production save transactions; PDF operations never enter the Qt process."""

from functools import partial
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest
from PySide6.QtCore import QTimer

from pdf_annotator.app.document_manager import DocumentManager
from pdf_annotator.app.document_session import Lifecycle
from pdf_annotator.domain.annotation_commands import (CommandContext, CreateInk, Undo, Redo,
                                                       DeleteAnnotation, CreateHighlight, CreateFreeText,
                                                       CreateCallout, CreateDeleteText, CreateInsertText,
                                                       CreateReplaceText, DeleteExternalAnnotation,
                                                       MoveCallout, MoveFreeText, MoveInk)
from pdf_annotator.domain.geometry import PdfPoint, PdfRect
from pdf_annotator.domain.callout_geometry import callout_line
from pdf_annotator.domain.targets import InkTarget, RectTarget, CalloutTarget
from pdf_annotator.domain.styles import InkStyle, RgbColor, FreeTextStyle, HighlightStyle
from pdf_annotator.domain.rich_text import RichText, RichTextRun
from pdf_annotator.domain.annotation_inventory import external_command_annotation_id
from pdf_annotator.domain.external_edits import CAPABILITY_FOR_EDIT
from pdf_annotator.domain.results import RequestState
from pdf_annotator.worker import document_service
from pdf_annotator.worker.protocol import Operation, Response
from tests.conftest import wait_until
from tests.m15_helpers import failure_worker
from tests.test_m6_service_persistence import _opened
from tests.m10_probe import range_target, insertion_target


def inspect(path):
    result = subprocess.run([sys.executable, "-m", "tests.m15_helpers", "inspect", str(path)],
                            capture_output=True, check=True, timeout=120)
    return json.loads(result.stdout)


@pytest.fixture
def manager(app):
    manager = DocumentManager()
    yield manager
    wait_until(lambda: not any(s.saving for s in manager.sessions.values()), timeout=180)
    workers = [s.worker for s in manager.sessions.values()]
    assert manager.request_shutdown(discard_changes=True)
    wait_until(lambda: manager.is_shutdown)
    assert all(w.stopped.is_set() and not w.forced_termination for w in workers)
    assert manager.budget.bytes == 0


def ink(session):
    return CreateInk(CommandContext.create(session.document_id, session.document_generation),
                     InkTarget(0, ((PdfPoint(20, 30), PdfPoint(40, 50)),)),
                     InkStyle(RgbColor(1, 0, 0), 2, .8))


def submit(manager, command):
    results = []
    manager.annotation_result.connect(results.append)
    try:
        result = manager.submit_annotation(command)
        assert result.state == RequestState.PENDING
        wait_until(lambda: any(r.command_id == command.context.command_id for r in results))
        assert results[-1].success, results[-1].error
        return results[-1]
    finally:
        manager.annotation_result.disconnect(results.append)


def save(manager, session, path=None, *, overwrite=False, success=True):
    finished = []
    manager.save_finished.connect(lambda key, ok: finished.append((key, ok)))
    assert manager.saves.request(session.document_id, str(path) if path is not None else None, overwrite=overwrite)
    wait_until(lambda: bool(finished), timeout=180)
    assert finished[-1] == (session.document_id, success)
    assert not session.saving
    return finished[-1]


def test_save_checkpoint_generation_and_undo_redo(tmp_path, manager, pdf_fixture_copy):
    path = pdf_fixture_copy(tmp_path / "source.pdf")
    session = _opened(manager, path)
    before = inspect(path)
    command = ink(session)
    submit(manager, command)
    pid = session.worker.pid
    save(manager, session)
    assert not session.dirty and session.document_generation == 2
    assert session.history_position == session.history_length == 1
    assert session.worker.pid == pid and manager.workers_started == 1
    assert inspect(path)["text"] == before["text"]
    assert inspect(path)["content"] == before["content"]
    submit(manager, Undo(CommandContext.create(session.document_id, 2)))
    assert session.dirty
    submit(manager, Redo(CommandContext.create(session.document_id, 2)))
    assert not session.dirty
    submit(manager, DeleteAnnotation(CommandContext.create(session.document_id, 2), command.annotation_id, 0))
    save(manager, session)
    assert not session.annotations and not session.dirty
    # An annotation deleted before save still restores its native AP/resources.
    submit(manager, Undo(CommandContext.create(session.document_id, 3)))
    assert command.annotation_id.value in session.annotations and session.dirty
    save(manager, session)
    assert not list(tmp_path.glob(".pdf-annotator-*.pdf"))
    assert manager.request_close(session.document_id)
    wait_until(lambda: session.document_id not in manager.sessions)
    reopened = _opened(manager, path)
    manager.start_annotation_inventory(reopened.document_id)
    wait_until(lambda: reopened.inventory_complete)
    assert any(item.kind == "ink" for item in reopened.annotation_inventory.values())


def test_save_as_source_unchanged_and_following_save(tmp_path, manager, pdf_fixture_copy):
    source = pdf_fixture_copy(tmp_path / "source.pdf")
    original = source.read_bytes()
    session = _opened(manager, source)
    submit(manager, ink(session))
    target = tmp_path / "destination.pdf"
    save(manager, session, target)
    assert session.canonical_path == str(target.resolve()) and source.read_bytes() == original
    assert not session.dirty and session.history_position == 1
    submit(manager, ink(session))
    save(manager, session)
    assert len(inspect(target)["annots"][0]) == 2
    assert source.read_bytes() == original
    assert manager._by_path[session.identity.path_key] == session.document_id
    old = _opened(manager, source)
    assert old.document_id != session.document_id


def test_callout_save_as_reopen_preserves_exact_oblique_leader(tmp_path, manager,
                                                               pdf_fixture_copy):
    source = pdf_fixture_copy(tmp_path / "callout-source.pdf")
    session = _opened(manager, source)
    requested = CalloutTarget(0, PdfRect(220, 180, 460, 290), PdfPoint(525, 92))
    command = CreateCallout(CommandContext.create(session.document_id,
                                                   session.document_generation),
                            requested, "leader obliqua", FreeTextStyle())
    submit(manager, command)
    model = session.annotations[command.annotation_id.value]
    expected = callout_line(model.target.text_rect, model.target.anchor)
    destination = tmp_path / "callout-saved.pdf"
    save(manager, session, destination)
    completed = subprocess.run(
        [sys.executable, "-m", "tests.m8_helpers", str(destination)],
        cwd=Path(__file__).resolve().parents[1], check=True, capture_output=True,
        text=True, timeout=30)
    record = json.loads(completed.stdout)["records"][0]
    numbers = [float(value) for value in re.findall(r"-?\d+(?:\.\d+)?", record["cl"][1])]
    reopened = tuple(PdfPoint(numbers[index], session.pages[0].height_pt - numbers[index + 1])
                     for index in range(0, len(numbers), 2))
    assert reopened == expected
    assert reopened[0].x != reopened[1].x and reopened[0].y != reopened[1].y


@pytest.mark.parametrize("phase", ["write", "validator", "content", "appearance", "validator_crash", "replace", "reopen"])
def test_failure_original_intact_dirty_usable_no_temps(tmp_path, manager, monkeypatch, pdf_fixture_copy, phase):
    monkeypatch.setattr(document_service, "document_worker_main", partial(failure_worker, phase=phase))
    source = pdf_fixture_copy(tmp_path / "source.pdf")
    original = source.read_bytes()
    session = _opened(manager, source)
    submit(manager, ink(session))
    save(manager, session, success=False)
    assert source.read_bytes() == original and session.dirty
    assert session.lifecycle == Lifecycle.READY
    assert not list(tmp_path.glob(".pdf-annotator-*.pdf"))
    submit(manager, ink(session))
    assert len(session.annotations) == 2
    submit(manager, Undo(CommandContext.create(session.document_id, session.document_generation)))
    assert len(session.annotations) == 1
    save(manager, session)
    assert not session.dirty
    assert len(inspect(source)["annots"][0]) == 1


def test_save_as_collision_cancel_overwrite_and_external_conflict(tmp_path, manager, pdf_fixture_copy):
    source = pdf_fixture_copy(tmp_path / "source.pdf")
    other = pdf_fixture_copy(tmp_path / "other.pdf")
    session = _opened(manager, source)
    other_session = _opened(manager, other)
    other_bytes = other.read_bytes()
    save(manager, session, other, overwrite=True, success=False)
    alias = tmp_path / "other-hardlink.pdf"
    os.link(other, alias)
    save(manager, session, str(alias).upper(), overwrite=True, success=False)
    assert other.read_bytes() == other_bytes and other_session.document_generation == 1
    target = pdf_fixture_copy(tmp_path / "target.pdf")
    confirmations = []
    manager.save_overwrite_required.connect(lambda key, path: confirmations.append(key))
    assert manager.saves.request(session.document_id, str(target))
    wait_until(lambda: bool(confirmations))
    manager.saves.confirm_overwrite(session.document_id, False)
    assert session.document_generation == 1 and session.canonical_path == str(source)
    assert target.read_bytes() == other_bytes
    submit(manager, ink(session))
    save(manager, session, target, overwrite=True)
    assert session.document_generation == 2
    target.write_bytes(target.read_bytes() + b"\n% external change\n")
    changed = target.read_bytes()
    submit(manager, ink(session))
    save(manager, session, success=False)
    assert target.read_bytes() == changed and session.dirty


def test_save_barrier_blocks_mutations_and_close_without_blocking_qt(tmp_path, manager, pdf_fixture_copy):
    session = _opened(manager, pdf_fixture_copy(tmp_path / "source.pdf"))
    assert manager.submit_annotation(ink(session)).state == RequestState.PENDING
    ticks = []
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start()
    assert manager.saves.request(session.document_id)
    assert manager.submit_annotation(ink(session)).state != RequestState.PENDING
    assert not session.can_undo and not session.can_redo
    assert not manager.request_close(session.document_id, discard_changes=True)
    assert not manager.request_shutdown(discard_changes=True)
    assert not manager.saves.request(session.document_id)
    wait_until(lambda: not session.saving, timeout=180)
    timer.stop()
    assert len(ticks) > 5 and not session.dirty
    assert session.saved_revision == session.document_revision == 1


def test_save_reports_owner_validator_and_barrier_profile(tmp_path, manager, pdf_fixture_copy):
    session = _opened(manager, pdf_fixture_copy(tmp_path / "profile.pdf"))
    submit(manager, ink(session))
    save(manager, session)
    timings = session.last_save_timing_ms
    assert timings["mutation_barrier_wait"] >= 0
    assert timings["candidate_write"] >= 0
    assert timings["candidate_fsync"] >= 0
    assert timings["validator_spawn"] >= 0
    assert timings["validator_startup"] >= 0
    assert timings["validator_semantic_scan"] >= 0
    assert timings["owner_close"] >= 0
    assert timings["os_replace"] >= 0
    assert timings["owner_reopen"] >= 0
    assert timings["post_reopen_rebind_invalidation"] >= 0
    assert timings["cleanup"] >= 0
    assert timings["gui_save_round_trip"] >= timings["worker_total"]


def test_production_save_preserves_acrobat_freetext_semantic_profile(
        tmp_path, manager, pdf_fixture_copy):
    """The validator compares live pre-save state to each candidate; this
    checks the same Acrobat-facing profile after both production replaces."""
    source = pdf_fixture_copy(tmp_path / "freetext-profile.pdf")
    session = _opened(manager, source)
    red, blue = RgbColor(.86, .12, .12), RgbColor(.08, .32, .86)
    pale_yellow, pale_blue = RgbColor(1, .96, .70), RgbColor(.79, .89, 1)
    rich = RichText((
        RichTextRun("grassetto città ", "Helvetica", 12, blue, bold=True),
        RichTextRun("corsivo 日本語 😀", "Times", 12, red, italic=True),
    ))
    context = lambda: CommandContext.create(session.document_id, session.document_generation)
    submit(manager, CreateFreeText(
        context(), RectTarget(0, PdfRect(35, 80, 270, 145)), rich.plain,
        FreeTextStyle(red, pale_blue, RgbColor(0, 0, 0)), rich_text=rich))
    submit(manager, CreateCallout(
        context(), CalloutTarget(0, PdfRect(320, 150, 560, 240), PdfPoint(285, 115)), rich.plain,
        FreeTextStyle(blue, pale_yellow, RgbColor(0, 0, 0)), rich_text=rich))
    target = tmp_path / "freetext-profile-save-as.pdf"
    save(manager, session, target)

    def profile(path):
        result = subprocess.run([sys.executable, "-m", "tests.m8_helpers", str(path)],
                                capture_output=True, text=True, check=True, timeout=30)
        records = json.loads(result.stdout)["records"]
        return [{key: record[key] for key in
                 ("contents", "rc", "ds", "da", "it", "cl", "c", "q", "appearance")}
                for record in records]

    before_normal_save = profile(target)
    assert len(before_normal_save) == 2
    text_box, callout = before_normal_save
    assert text_box["contents"][1] == rich.plain and callout["contents"][1] == rich.plain
    assert all('xmlns="http://www.w3.org/1999/xhtml"' in record["rc"][1]
               and "1999/xtml" not in record["rc"][1] for record in before_normal_save)
    for record in before_normal_save:
        rc = record["rc"][1]
        assert "font-family:Helvetica;font-size:12pt;color:#1452db;font-weight:bold" in rc
        assert "font-family:Times;font-size:12pt;color:#db1f1f;font-weight:normal;font-style:italic" in rc
    assert "0.86 0.12 0.12 rg" in text_box["da"][1]
    assert ".79 .89 1" in text_box["c"][1]
    assert callout["it"][1] == "/FreeTextCallout" and callout["cl"][0] == "array"
    assert "0.08 0.32 0.86 rg" in callout["da"][1]
    assert "1 .96 .7" in callout["c"][1]
    save(manager, session)
    assert profile(target) == before_normal_save


def test_gui_never_imports_pymupdf():
    assert "pymupdf" not in sys.modules and "fitz" not in sys.modules


def test_semantic_validation_ignores_object_numbers_but_checks_appearance(tmp_path):
    result = subprocess.run([sys.executable, "-m", "tests.m15_helpers", "validation-contract",
                             str(tmp_path / "renumbered.pdf")], capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode(errors="replace")


@pytest.mark.parametrize("phase", ["replace", "reopen"])
@pytest.mark.parametrize("existing", [False, True])
def test_save_as_failure_preserves_both_source_and_destination(tmp_path, manager, monkeypatch, pdf_fixture_copy, phase, existing):
    monkeypatch.setattr(document_service, "document_worker_main", partial(failure_worker, phase=phase))
    source = pdf_fixture_copy(tmp_path / "source.pdf")
    source_bytes = source.read_bytes()
    target = tmp_path / "destination.pdf"
    if existing:
        pdf_fixture_copy(target, pages=2)
    target_bytes = target.read_bytes() if existing else None
    session = _opened(manager, source)
    submit(manager, ink(session))
    save(manager, session, target, overwrite=existing, success=False)
    assert source.read_bytes() == source_bytes
    assert (target.read_bytes() if target.exists() else None) == target_bytes
    assert session.canonical_path == str(source) and session.dirty
    save(manager, session, target, overwrite=existing)
    assert session.canonical_path == str(target) and not session.dirty


def test_target_changed_after_overwrite_question_is_not_overwritten(tmp_path, manager, pdf_fixture_copy):
    session = _opened(manager, pdf_fixture_copy(tmp_path / "source.pdf"))
    target = pdf_fixture_copy(tmp_path / "target.pdf")
    questions = []
    manager.save_overwrite_required.connect(lambda key, path: questions.append(key))
    assert manager.saves.request(session.document_id, str(target))
    wait_until(lambda: bool(questions))
    target.write_bytes(target.read_bytes() + b"\n% changed after question\n")
    changed = target.read_bytes()
    manager.saves.confirm_overwrite(session.document_id, True)
    wait_until(lambda: not session.saving, timeout=180)
    assert session.document_generation == 1 and target.read_bytes() == changed


@pytest.mark.parametrize("fixture", ["native", "unknown"])
def test_external_graph_appearance_unicode_unknown_preserved(tmp_path, manager, fixture):
    source = tmp_path / "external.pdf"
    module = "tests.m15_helpers" if fixture == "unknown" else "tests.m12_fixtures"
    subprocess.run([sys.executable, "-m", module, fixture, str(source)], check=True, timeout=30)
    assert_external_graph_preserved(manager, source, tmp_path / "roundtrip.pdf")


def test_external_editorial_graph_appearance_preserved(tmp_path, manager, external_editorials):
    source = tmp_path / "external.pdf"
    source.write_bytes(external_editorials.read_bytes())
    assert_external_graph_preserved(manager, source, tmp_path / "roundtrip.pdf")


def assert_external_graph_preserved(manager, source, target):
    before = inspect(source)
    session = _opened(manager, source)
    save(manager, session, target)
    after = inspect(target)
    assert after == before
    wait_until(lambda: session.inventory_complete)
    from pdf_annotator.worker.external_profiles import VERIFIED_EXTERNAL_EDITS
    for item in session.annotation_inventory.values():
        profile = item.style.get("external_profile")
        expected = {field for operation, field in CAPABILITY_FOR_EDIT.items()
                    if (profile, operation.value) in VERIFIED_EXTERNAL_EDITS}
        actual = {field for field in CAPABILITY_FOR_EDIT.values()
                  if getattr(item.capabilities, field)}
        assert actual == expected
    save(manager, session)
    assert inspect(target) == before


def test_seven_tools_rich_unicode_replace_history_roundtrip(tmp_path, manager, monkeypatch, external_editorials):
    author = "Giulia (prova) 日本語 😀"
    monkeypatch.setattr(manager, "_manual_author", author)
    source = tmp_path / "all-tools.pdf"
    source.write_bytes(external_editorials.read_bytes())
    before = inspect(source)
    session = _opened(manager, source)
    context = lambda: CommandContext.create(session.document_id, session.document_generation)
    rich = RichText((RichTextRun("città ", "Helvetica", 12, RgbColor(0, 0, 0), bold=True),
                     RichTextRun("日本語 & < >", "Helvetica", 12, RgbColor(0, 0, 0), italic=True)))
    style = FreeTextStyle()
    commands = [ink(session),
                CreateHighlight(context(), range_target(session, "brown"), HighlightStyle()),
                CreateFreeText(context(), RectTarget(0, PdfRect(40, 280, 300, 370)), rich.plain, style, rich_text=rich),
                CreateCallout(context(), CalloutTarget(0, PdfRect(310, 280, 560, 390), PdfPoint(280, 240)),
                              rich.plain, style, rich_text=rich),
                CreateDeleteText(context(), range_target(session, "brown")),
                CreateInsertText(context(), insertion_target(session), " più 日本語 😀"),
                CreateReplaceText(context(), range_target(session, "lazy"), "città 日本語 😀")]
    for command in commands:
        submit(manager, command)
        assert session.annotation_inventory[command.annotation_id.value].author == author
    save(manager, session)
    after = inspect(source)
    assert after["text"] == before["text"] and after["content"] == before["content"]
    by_name = {record["nm"]: record for record in after["annots"][0]}
    for command in commands:
        record = by_name[command.annotation_id.value]
        assert record["author"] == ["string", author]
        if hasattr(command, "text"):
            assert record["text"] == command.text
    for original in before["annots"][0]:
        if original["subtype"] != "/Popup":
            assert by_name[original["nm"]]["author"] == original["author"]
    assert len(session.annotations) == 7
    # Save at an undone position, then redo restores the complete Replace graph.
    submit(manager, Undo(context()))
    target = tmp_path / "all-tools-as.pdf"
    save(manager, session, target)
    assert not session.dirty
    submit(manager, Redo(context()))
    assert session.dirty and len(session.annotations) == 7
    save(manager, session)
    assert inspect(target)["annots"] == after["annots"]
    submit(manager, Undo(context()))
    assert session.dirty
    submit(manager, Redo(context()))
    assert not session.dirty
    replacements = (MoveInk(context(), commands[0].annotation_id, 0, 5, 5),
                    MoveFreeText(context(), commands[2].annotation_id, 0, 5, 5),
                    MoveCallout(context(), commands[3].annotation_id, 0, 5, 5))
    for command in replacements:
        submit(manager, command)
        assert session.annotation_inventory[command.annotation_id.value].author == author
    save(manager, session)
    moved = {record["nm"]: record["author"] for record in inspect(target)["annots"][0]}
    assert all(moved[command.annotation_id.value] == ["string", author] for command in replacements)
    assert manager.request_close(session.document_id)
    wait_until(lambda: session.document_id not in manager.sessions)
    reopened = _opened(manager, target)
    manager.start_annotation_inventory(reopened.document_id)
    wait_until(lambda: reopened.inventory_complete)
    authors = {summary.native_name: summary.author for summary in reopened.annotation_inventory.values()}
    assert all(authors[command.annotation_id.value] == author for command in commands)


def test_external_deleted_before_save_restores_complete_graph(tmp_path, manager):
    source = tmp_path / "external-history.pdf"
    subprocess.run([sys.executable, "-m", "tests.m12_fixtures", "native", str(source)], check=True)
    before = inspect(source)
    session = _opened(manager, source)
    manager.start_annotation_inventory(session.document_id)
    wait_until(lambda: session.inventory_complete)
    summary = next(item for item in session.annotation_inventory.values() if item.kind == "callout")
    submit(manager, DeleteExternalAnnotation(CommandContext.create(session.document_id, 1),
            external_command_annotation_id(summary.logical_id), summary.logical_id, summary.page_index))
    save(manager, session)
    submit(manager, Undo(CommandContext.create(session.document_id, 2)))
    save(manager, session)
    assert inspect(source)["checks"]["semantic_digest"] == before["checks"]["semantic_digest"]


def test_save_as_reserves_target_against_other_saves_and_open(tmp_path, manager, pdf_fixture_copy):
    a = _opened(manager, pdf_fixture_copy(tmp_path / "a.pdf"))
    b = _opened(manager, pdf_fixture_copy(tmp_path / "b.pdf"))
    target = pdf_fixture_copy(tmp_path / "reserved.pdf")
    confirmations = []
    manager.save_overwrite_required.connect(lambda key, path: confirmations.append(key))
    assert manager.saves.request(a.document_id, str(target))
    wait_until(lambda: bool(confirmations))
    manager.open_path(str(target).upper())
    wait_until(lambda: manager.pending_open_count == 0)
    assert len(manager.sessions) == 2 and manager.active_id == a.document_id
    save(manager, b, target, overwrite=True, success=False)
    assert a.saving and not b.saving
    manager.saves.confirm_overwrite(a.document_id, False)
    assert not a.saving


def test_stale_deferred_response_rejected_after_save(tmp_path, manager, pdf_fixture_copy):
    session = _opened(manager, pdf_fixture_copy(tmp_path / "source.pdf"))
    save(manager, session)
    client = session.worker
    stale_id = 987654
    with client._lock:
        client._records[stale_id] = False
        client._deferred[stale_id] = Response(session.document_id, 1, stale_id, Operation.ANNOTATION_SUMMARIES,
                                            True, {"summaries": []})
    assert client.poll(stale_id) is None and not client.is_pending(stale_id)


def test_annotated_multipage_roundtrip(tmp_path, manager):
    source = tmp_path / "imported.pdf"
    subprocess.run([sys.executable, "-m", "tests.m151_fixtures", str(source)],
                   check=True, timeout=30)
    original = sha256(source.read_bytes()).hexdigest()
    session = _opened(manager, source)
    target = tmp_path / "imported-save-as.pdf"
    save(manager, session, target)
    assert inspect(source) == inspect(target)
    assert sha256(source.read_bytes()).hexdigest() == original
