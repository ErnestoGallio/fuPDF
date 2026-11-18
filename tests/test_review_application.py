import json
import asyncio
from dataclasses import replace
from functools import partial
from pathlib import Path
import sys
from threading import Event

import pytest

from pdf_annotator.domain.annotation_commands import CommandContext, CommandSource, CreateFreeText, DeleteAnnotation, Redo, Undo
from pdf_annotator.domain.geometry import PdfRect
from pdf_annotator.domain.kinds import AnnotationKind
from pdf_annotator.domain.styles import FreeTextStyle
from pdf_annotator.domain.targets import RectTarget
from pdf_annotator.services.review_application import MutationDelivery
from pdf_annotator.services import review_application
from pdf_annotator.services.review_jobs import ReviewScheduler
from pdf_annotator.services.review_transport import classify_response
from pdf_annotator.services.review_worker import ReviewWorker
from pdf_annotator.worker import document_service
from pdf_annotator.worker.protocol import Operation
from .conftest import wait_until
from .m21_review_fixtures import proposal, run_fixture
from .test_review_jobs import FakeClock, MockTransport, success
from .test_save_m15 import _opened, ink, manager, save, submit
from .m192_helpers import fault_worker


@pytest.fixture
def scheduler(tmp_path, manager):
    clock = FakeClock()
    scheduler = ReviewScheduler(tmp_path / "jobs", clock=clock, sleep=clock.sleep, jitter=lambda: 0)
    yield scheduler
    closed = scheduler.shutdown()
    wait_until(closed.done, timeout=30)
    closed.result()


def _make_pdf(path: Path) -> Path:
    run_fixture("make-application", path)
    return path


def _run(job):
    wait_until(job.future.done, timeout=45)
    return job.future.result()


def _outcomes(job):
    report = json.loads((job.store.directory / "application_report.json").read_text(encoding="utf-8"))
    return report["outcomes"]


def _native_state(path: Path) -> dict:
    return json.loads(run_fixture("inspect-application", path))


def test_offline_pipeline_worker_undo_redo_then_user_save(tmp_path, manager, scheduler):
    path = _make_pdf(tmp_path / "source.pdf")
    before = path.read_bytes()
    baseline = _native_state(path)
    session = _opened(manager, path)
    items = [proposal("Frase da modificare"), proposal("parola", operation="delete"),
             proposal("return 0;", operation="insert", side="after"),
             proposal("Annota", operation="comment", page=1), proposal(operation="comment", page=1),
             proposal("Ambigua"), proposal("Assente"), proposal("qui", operation="insert", side="after", page=1)]
    items[2]["replacement_text"] = "\n} — è ☃"
    transport = MockTransport(lambda *_: success(items))
    job = manager.review_application.start(scheduler, session.document_id, transport, project="test", last=1)
    report = _run(job)
    rows = _outcomes(job)
    assert report["status"] == "completed" and report["application"]["applied"] == 6, rows
    assert report["application"]["pages_analyzed"] == 2 and report["application"]["discarded"] == 2
    assert {item["status"] for item in rows} == {"applied", "not_localizable", "ambiguous"}
    assert rows[0]["localization"]["status"] == "text_normalized"
    assert len(rows[0]["localization"]["quads"]) == 2
    assert [item["live_status"] for item in rows if item["operation"] == "comment"] == ["text_exact", "page_fallback"]
    assert {model.kind for model in session.annotations.values()} == {
        AnnotationKind.REPLACE_TEXT, AnnotationKind.DELETE_TEXT, AnnotationKind.INSERT_TEXT, AnnotationKind.STICKY_NOTE}
    assert all(model.source == CommandSource.AI for model in session.annotations.values())
    assert all(model.metadata.reason and model.metadata.author == "Revisione IA"
               for model in session.annotations.values() if model.kind != AnnotationKind.STICKY_NOTE)
    assert session.dirty and session.document_revision == session.history_length == 6
    assert session.saved_revision == 0 and path.read_bytes() == before
    assert job.snapshot is None and job.phase == "finished"
    for _ in range(6):
        submit(manager, Undo(CommandContext.create(session.document_id, session.document_generation)))
    assert not session.annotations and not session.dirty
    for _ in range(6):
        submit(manager, Redo(CommandContext.create(session.document_id, session.document_generation)))
    assert session.dirty and set(session.annotations) == {row["annotation_id"] for row in rows if row["status"] == "applied"}
    saved = tmp_path / "saved.pdf"
    save(manager, session, saved)
    state = _native_state(saved)
    annotations = state["annotations"]
    assert state["content"] == baseline["content"] and path.read_bytes() == before
    for expected, actual in zip(baseline["annotations"], annotations, strict=True):
        assert expected.items() <= actual.items()
    roots = [annotations[row["page_index"]][row["annotation_id"]] for row in rows if row["status"] == "applied"]
    assert {root["Subtype"] for root in roots} == {"/Caret", "/StrikeOut", "/Text"}
    assert [root["Subtype"] for root in roots[:3]] == ["/Caret", "/StrikeOut", "/Caret"]
    assert roots[0]["IT"] == "/Replace"
    assert all(annotations[0][row["annotation_id"]]["Popup"] != "null" for row in rows[:3])
    for row in rows:
        if row["status"] != "applied" or row["operation"] == "comment":
            continue
        assert row["annotation_id"] in state["ai_reply_targets"][row["page_index"]]
    assert "pymupdf" not in sys.modules and "fitz" not in sys.modules


@pytest.mark.parametrize("mode", ["partial", "suspended", "cancelled"])
def test_barrier_outcomes_and_selective_recovery(tmp_path, manager, scheduler, mode):
    session = _opened(manager, _make_pdf(tmp_path / "source.pdf"))
    wait_for_last, release = Event(), Event()
    first = proposal("parola", operation="delete")
    second = proposal("Correzione", operation="delete", page=4)

    def responses(start, _last):
        if start == 0:
            return success([first])
        wait_for_last.set()
        assert release.wait(20)
        if mode == "suspended":
            return classify_response(429, {"error": {"code": "insufficient_quota"}}, {})
        return success([second]) if mode == "cancelled" else classify_response(413, {}, {})

    job = manager.review_application.start(scheduler, session.document_id, MockTransport(responses), project="test")
    try:
        wait_until(wait_for_last.is_set, timeout=20)
        assert session.document_revision == 0 and not session.dirty and job.phase == "analysis"
        if mode == "cancelled":
            job.cancel()
        elif mode == "partial":
            submit(manager, CreateFreeText(CommandContext.create(session.document_id, session.document_generation),
                                          RectTarget(0, PdfRect(215, 260, 325, 300)), "parola", FreeTextStyle()))
        release.set()
        report = _run(job)
    finally:
        release.set()
    expected = {"partial": "partial", "suspended": "suspended", "cancelled": "interrupted"}
    assert report["status"] == expected[mode]
    if mode != "partial":
        assert session.document_revision == 0 and report["application"]["status"] == "deferred"
        assert report["application"]["applied"] == 0
        return
    assert report["application"]["applied"] == 1 and report["application"]["pages_not_analyzed"] == 2
    previous = next(model for model in session.annotations.values() if model.source == CommandSource.AI)
    submit(manager, DeleteAnnotation(CommandContext.create(session.document_id, session.document_generation),
                                     previous.annotation_id, previous.page_index))
    failed = next(batch["batch_id"] for batch in report["batches"] if batch["status"] == "failed")
    scheduler.retry(job, (failed,), transport=MockTransport(lambda *_: success([second])), project="test")
    final = _run(job)
    assert final["application"]["applied"] == final["application"]["already_applied"] == 1
    assert previous.annotation_id.value not in session.annotations and len(session.annotations) == 2
    scheduler.resume_application(job)
    assert _run(job)["application"]["already_applied"] == 2
    assert len(session.annotations) == 2


def test_switch_tab_save_as_and_reconfirmation_before_submit(tmp_path, manager, scheduler, monkeypatch):
    first = _opened(manager, _make_pdf(tmp_path / "first.pdf"))
    second = _opened(manager, _make_pdf(tmp_path / "second.pdf"))
    held, release = Event(), Event()
    original_confirm = ReviewWorker.confirm
    generation = first.document_generation
    confirmations = []

    async def pause_confirm(worker, data, stopping):
        result = await original_confirm(worker, data, stopping)
        if result["status"] == "confirmed":
            confirmations.append(result["command"]["document_generation"])
            if len(confirmations) == 1:
                held.set()
                while not release.is_set():
                    await asyncio.sleep(0.01)
        return result

    monkeypatch.setattr(ReviewWorker, "confirm", pause_confirm)
    job = manager.review_application.start(scheduler, first.document_id,
        MockTransport(lambda *_: success([proposal("parola", operation="delete")])), project="p", last=0)
    try:
        wait_until(held.is_set, timeout=30)
        assert job.phase == "application" and not first.dirty
        submit(manager, ink(first))
        save(manager, first, tmp_path / "saved-during-application.pdf")
        manager.activate(second.document_id)
    finally:
        release.set()
    report = _run(job)
    assert report["application"]["applied"] == 1, _outcomes(job)
    assert confirmations == [generation, generation + 1]
    assert first.dirty and first.document_revision == 2 and first.saved_revision == 1
    assert len(first.annotations) == 2 and not second.annotations and not second.dirty
    assert manager.active_id == second.document_id


def test_uncertain_acknowledgement_reconciles_native_name_without_replay(tmp_path, manager, scheduler, monkeypatch):
    session = _opened(manager, _make_pdf(tmp_path / "source.pdf"))
    app = manager.review_application._application
    admit = app._submit

    async def miss(client, command, stopping):
        delivered = await admit(client, command, stopping)
        assert delivered.result.success
        return MutationDelivery("accepted")

    monkeypatch.setattr(app, "_submit", miss)
    job = manager.review_application.start(scheduler, session.document_id,
        MockTransport(lambda *_: success([proposal("parola", operation="delete")])), project="p", last=0)
    failed = _run(job)
    assert failed["application"]["uncertain"] == 1 and failed["application"]["applied"] == 0
    assert session.document_revision == 1 and job.snapshot is not None
    scheduler.resume_application(job)
    resolved = _run(job)
    assert resolved["application"]["applied"] == 1 and resolved["application"]["uncertain"] == 0
    assert _outcomes(job)[0]["reconciled"]
    assert session.document_revision == 1 and job.snapshot is None


def test_commit_ledger_failure_after_creation_preserves_intent(tmp_path, manager, scheduler, monkeypatch):
    source = _make_pdf(tmp_path / "source.pdf")
    session = _opened(manager, source)
    write = review_application.write_json
    fail_once = True

    def commit(path, data):
        nonlocal fail_once
        if fail_once and path.parent.name == "application" and data["status"] == "applied":
            fail_once = False
            raise OSError("simulated archive write failure")
        write(path, data)

    monkeypatch.setattr(review_application, "write_json", commit)
    items = [proposal("parola", operation="delete"), proposal("Annota", operation="comment", page=1)]
    job = manager.review_application.start(scheduler, session.document_id,
        MockTransport(lambda *_: success(items)), project="p", last=1)
    report = _run(job)
    assert report["application"]["error"] == "persistence_failed"
    assert report["application"]["uncertain"] == 1 and session.document_revision == 1
    own, = session.annotations.values()
    path = job.store.directory / "application" / f"{own.annotation_id.value}.json"
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "submitting"
    scheduler.resume_application(job)
    assert _run(job)["application"]["applied"] == 2
    assert session.document_revision == 2
    assert _outcomes(job)[0]["reconciled"]


def test_unknown_native_outcome_does_not_replay_after_deletion(tmp_path, manager, scheduler, monkeypatch):
    session = _opened(manager, _make_pdf(tmp_path / "source.pdf"))
    application = manager.review_application._application
    admit = application._submit

    async def miss(client, command, stopping):
        await admit(client, command, stopping)
        return MutationDelivery("accepted")

    monkeypatch.setattr(application, "_submit", miss)
    job = manager.review_application.start(scheduler, session.document_id,
        MockTransport(lambda *_: success([proposal("parola", operation="delete")])), project="p", last=0)
    assert _run(job)["application"]["uncertain"] == 1
    model, = session.annotations.values()
    submit(manager, DeleteAnnotation(CommandContext.create(session.document_id, session.document_generation),
                                     model.annotation_id, model.page_index))
    scheduler.resume_application(job)
    state = _run(job)["application"]
    assert state["applied"] == 0 and state["uncertain"] == 1
    assert not session.annotations and session.document_revision == 2


def test_transaction_failure_rolls_back_and_allows_later_proposal(tmp_path, manager, scheduler, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", partial(fault_worker, stage="reason"))
    path = _make_pdf(tmp_path / "source.pdf")
    before = _native_state(path)
    session = _opened(manager, path)
    items = [proposal("Frase da modificare"), proposal("parola", operation="delete")]
    job = manager.review_application.start(scheduler, session.document_id,
        MockTransport(lambda *_: success(items)), project="p", last=0)
    report = _run(job)
    rows = _outcomes(job)
    assert report["application"]["applied"] == report["application"]["errors"] == 1
    assert rows[0]["status"] == "command_error" and rows[0]["reason"] == "internal_pdf_mutation"
    assert rows[1]["status"] == "applied"
    assert session.document_revision == session.history_length == 1 and session.dirty
    model, = session.annotations.values()
    assert model.kind == AnnotationKind.DELETE_TEXT
    saved = tmp_path / "proof.pdf"
    save(manager, session, saved)
    state = _native_state(saved)
    annotations = state["annotations"]
    assert state["content"] == before["content"] and before["annotations"][0].items() <= annotations[0].items()
    assert rows[0]["annotation_id"] not in annotations[0]
    assert len(annotations[0]) == len(before["annotations"][0]) + 2  # root and reason


def test_cancel_after_one_ack_keeps_history_and_does_not_admit_the_next(tmp_path, manager, scheduler, monkeypatch):
    session = _opened(manager, _make_pdf(tmp_path / "source.pdf"))
    application = manager.review_application._application
    submit_application = application._submit

    async def stop_after_first(client, command, stopping):
        result = await submit_application(client, command, stopping)
        job.cancel()
        return result

    monkeypatch.setattr(application, "_submit", stop_after_first)
    items = [proposal("parola", operation="delete"), proposal("Annota", operation="comment", page=1)]
    job = manager.review_application.start(scheduler, session.document_id,
        MockTransport(lambda *_: success(items)), project="p", last=1)
    result = _run(job)
    app = result["application"]
    assert result["status"] == "completed" and app["status"] == "interrupted"
    assert app["applied"] == app["not_attempted"] == 1
    assert session.document_revision == 1 and session.dirty
    path = job.snapshot.path
    closed = manager.review_application.close(session.document_id)
    wait_until(closed.done)
    closed.result()
    assert not Path(path).exists()


def test_conflicts_and_duplicate_text_operations_are_reported(tmp_path, manager, scheduler):
    session = _opened(manager, _make_pdf(tmp_path / "source.pdf"))
    items = [proposal("Elimina questa"), proposal("questa parola", operation="delete"),
             proposal("Correzione", operation="delete", page=2), proposal("Correzione", operation="delete", page=2)]
    job = manager.review_application.start(scheduler, session.document_id,
        MockTransport(lambda *_: success(items)), project="p", last=2)
    report = _run(job)
    assert report["application"]["applied"] == 1 and report["application"]["discarded"] == 3
    assert [row["reason"] for row in _outcomes(job)] == [
        "overlapping_proposals", "overlapping_proposals", None, "duplicate_proposal"]


def test_lost_ipc_acknowledgement_retires_mutation_path(tmp_path, manager, scheduler, monkeypatch):
    session = _opened(manager, _make_pdf(tmp_path / "source.pdf"))
    worker = session.worker
    poll = worker.poll

    def invalid_ack(request_id=None, *, include_review=True):
        delivery = poll(request_id, include_review=include_review)
        if delivery and delivery.response.operation == Operation.ANNOTATION_COMMAND:
            delivery.response = replace(delivery.response, payload={})
        return delivery

    monkeypatch.setattr(worker, "poll", invalid_ack)
    items = [proposal("parola", operation="delete"), proposal("Annota", operation="comment", page=1)]
    job = manager.review_application.start(scheduler, session.document_id,
        MockTransport(lambda *_: success(items)), project="p", last=1)
    report = _run(job)
    assert report["application"]["uncertain"] == 1 and report["application"]["applied"] == 0
    assert session.lifecycle.value == "failed"
    assert worker.submitted_counts[Operation.ANNOTATION_COMMAND] == 1
    assert _outcomes(job)[0]["reason"] == "protocol_error"


def test_tab_close_between_confirmation_and_admission_releases_resources(tmp_path, manager, scheduler, monkeypatch):
    path = _make_pdf(tmp_path / "source.pdf")
    before = path.read_bytes()
    session = _opened(manager, path)
    client = session.worker
    waiting, release = Event(), Event()
    confirm = ReviewWorker.confirm

    async def hold(worker, data, stopping):
        result = await confirm(worker, data, stopping)
        waiting.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return result

    monkeypatch.setattr(ReviewWorker, "confirm", hold)
    job = manager.review_application.start(scheduler, session.document_id,
        MockTransport(lambda *_: success([proposal("parola", operation="delete")])), project="p", last=0)
    try:
        wait_until(waiting.is_set, timeout=30)
        workspace = Path(job.snapshot.path).parent
        assert manager.request_close(session.document_id)
    finally:
        release.set()
    assert _run(job)["application"]["not_attempted"] == 1
    wait_until(lambda: session.document_id not in manager.sessions)
    assert not workspace.exists() and path.read_bytes() == before
    assert client.submitted_counts[Operation.ANNOTATION_COMMAND] == 0


def test_worker_rejects_changed_page_and_tampered_snapshot_evidence(tmp_path):
    run_fixture("verify-application-evidence", tmp_path)
    assert "pymupdf" not in sys.modules and "fitz" not in sys.modules
