"""Focused M14 worker-history coverage; all PDF access stays in the worker."""

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from PySide6.QtGui import QImage

from pdf_annotator.domain.annotation_commands import (CommandContext, CreateInk,
                                                       CreateCallout, CreateFreeText,
                                                       CreateDeleteText, CreateReplaceText,
                                                       DeleteAnnotation, MoveInk,
                                                       MoveCalloutAnchor, ResizeFreeText,
                                                       TransferFreeText, UpdateFreeText,
                                                       Redo, Undo)
from pdf_annotator.domain.annotation_inventory import external_command_annotation_id
from pdf_annotator.domain.geometry import PdfPoint, PdfRect
from pdf_annotator.domain.serialization import command_to_payload, result_from_payload
from pdf_annotator.domain.styles import FreeTextStyle, InkStyle, RgbColor
from pdf_annotator.domain.targets import CalloutTarget, InkTarget, RectTarget
from pdf_annotator.domain.geometry import PageBounds
from pdf_annotator.util.paths import identify_file
from pdf_annotator.app.document_session import DocumentSession
from pdf_annotator.app.document_manager import DocumentManager
from pdf_annotator.app.annotation_sidebar import AnnotationSidebar
from pdf_annotator.viewer.pdf_view import PdfView
from pdf_annotator.viewer.raster_cache import RasterCache, RenderKey
from pdf_annotator.app.document_session import Lifecycle
from pdf_annotator.domain.annotation_commands import UpdateCallout
from pdf_annotator.domain.rich_text import RichText, RichTextRun
from pdf_annotator.worker import document_service
from tests.m8_helpers import persistence_worker
from tests.test_m6_service_persistence import _target as confirmed_target
from pdf_annotator.worker.annotation_dispatch import (AnnotationCommandDispatcher,
                                                       AppliedMutation)
from pdf_annotator.worker import annotation_dispatch
from pdf_annotator.worker.client import RasterBudget, WorkerClient
from pdf_annotator.worker.document_service import DocumentSource
from pdf_annotator.worker.protocol import Operation
from tests.conftest import request, wait_until
from pdf_annotator.domain.annotation_commands import DeleteExternalAnnotation


def inspect_native(path: Path) -> dict:
    """Keep native PDF inspection out of the pytest GUI process.

    The metadata/viewer isolation tests deliberately assert that PyMuPDF has
    never been imported by pytest. M8's helper has a subprocess entry point
    precisely for this inspection boundary.
    """
    completed = subprocess.run(
        [sys.executable, "-m", "tests.m8_helpers", str(path)],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
    )
    return json.loads(completed.stdout)


def make_native_fixture(path: Path) -> None:
    """Use M12's isolated fixture entry point; it imports PyMuPDF itself."""
    subprocess.run(
        [sys.executable, "-m", "tests.m12_fixtures", "native", str(path)],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        timeout=30,
    )


@pytest.fixture
def history_client(tmp_path: Path, app, pdf_fixture_copy):
    path = tmp_path / "history.pdf"
    pdf_fixture_copy(path)
    client = WorkerClient("history-doc", DocumentSource(identify_file(str(path))), RasterBudget())
    assert request(client, Operation.OPEN_DOCUMENT).success
    yield client
    client.close_async()
    wait_until(client.stopped.is_set)


def _run(client: WorkerClient, command):
    response = request(client, Operation.ANNOTATION_COMMAND, command_to_payload(command))
    assert response.success, response.error
    return result_from_payload(response.payload)


def _ink(client: WorkerClient) -> CreateInk:
    return CreateInk(CommandContext.create(client.document_id, client.generation),
                     InkTarget(0, ((PdfPoint(10, 20), PdfPoint(30, 40), PdfPoint(55, 22)),)),
                     InkStyle(RgbColor(.86, .12, .12), 2.25, .63))


def _undo(client: WorkerClient):
    return _run(client, Undo(CommandContext.create(client.document_id, client.generation)))


def _redo(client: WorkerClient):
    return _run(client, Redo(CommandContext.create(client.document_id, client.generation)))


def test_create_move_delete_undo_redo_and_new_branch(history_client):
    create = _ink(history_client)
    made = _run(history_client, create)
    assert made.history_position == made.history_length == 1
    moved = _run(history_client, MoveInk(CommandContext.create(history_client.document_id,
                                                                 history_client.generation),
                                        create.annotation_id, 0, 12, -4))
    assert moved.annotation.target.paths[0][0] == PdfPoint(22, 16)
    undone_move = _undo(history_client)
    assert undone_move.annotation.target == create.target
    redone_move = _redo(history_client)
    assert redone_move.annotation.target == moved.annotation.target
    deleted = _run(history_client, DeleteAnnotation(CommandContext.create(
        history_client.document_id, history_client.generation), create.annotation_id, 0))
    assert deleted.annotation is None
    restored = _undo(history_client)
    assert restored.annotation.target == moved.annotation.target
    assert _redo(history_client).annotation is None
    restored = _undo(history_client)
    assert restored.annotation is not None
    second = _run(history_client, _ink(history_client))
    assert second.history_position == second.history_length
    no_redo = _redo(history_client)
    assert not no_redo.success


def test_freetext_and_callout_restore_semantic_before_after_states(history_client):
    style = FreeTextStyle(RgbColor(.1, .2, .8), RgbColor(.95, .95, .7), RgbColor(0, 0, 0), .8, 1.25, 12)
    create = CreateFreeText(CommandContext.create(history_client.document_id, history_client.generation),
                            RectTarget(0, PdfRect(40, 40, 280, 140)), "prima", style)
    assert _run(history_client, create).success
    update = UpdateFreeText(CommandContext.create(history_client.document_id, history_client.generation),
                            create.annotation_id, 0, "dopo", style)
    changed = _run(history_client, update)
    assert changed.annotation.text == "dopo"
    assert _undo(history_client).annotation.text == "prima"
    assert _redo(history_client).annotation.text == "dopo"
    transferred = _run(history_client, TransferFreeText(
        CommandContext.create(history_client.document_id, history_client.generation), create.annotation_id, 0,
        RectTarget(1, PdfRect(50, 50, 290, 150))))
    assert transferred.annotation.page_index == 1
    assert _undo(history_client).annotation.page_index == 0
    assert _redo(history_client).annotation.page_index == 1
    callout = CreateCallout(CommandContext.create(history_client.document_id, history_client.generation),
                            CalloutTarget(0, PdfRect(240, 160, 440, 260), PdfPoint(55, 80), PdfPoint(55, 160)),
                            "nota", style)
    assert _run(history_client, callout).success
    changed_anchor = CalloutTarget(0, callout.target.text_rect, PdfPoint(75, 95), PdfPoint(75, 160))
    assert _run(history_client, MoveCalloutAnchor(CommandContext.create(
        history_client.document_id, history_client.generation), callout.annotation_id, changed_anchor)).success
    assert _undo(history_client).annotation.target.anchor == callout.target.anchor
    assert _redo(history_client).annotation.target.anchor == changed_anchor.anchor


def test_external_delete_restores_the_original_native_graph(tmp_path: Path, app):
    path = tmp_path / "external-history.pdf"
    make_native_fixture(path)
    client = WorkerClient("external-history", DocumentSource(identify_file(str(path))), RasterBudget())
    try:
        assert request(client, Operation.OPEN_DOCUMENT).success
        inventory = request(client, Operation.ANNOTATION_SUMMARIES, {"cursor": 0}).payload["summaries"]
        summary = next(item for item in inventory if item["kind"] == "callout")
        command = DeleteExternalAnnotation(CommandContext.create(client.document_id, client.generation),
                                           external_command_annotation_id(summary["logical_id"]),
                                           summary["logical_id"], summary["page_index"])
        deleted = _run(client, command)
        assert deleted.success
        assert summary["logical_id"] not in {item["logical_id"] for item in
                                              request(client, Operation.ANNOTATION_SUMMARIES,
                                                      {"cursor": 0}).payload["summaries"]}
        restored = _undo(client)
        assert restored.success and not restored.refresh_inventory
        assert restored.history_delta is not None
        assert restored.history_delta.inventory_added[0].logical_id == summary["logical_id"]
        after = request(client, Operation.ANNOTATION_SUMMARIES, {"cursor": 0}).payload["summaries"]
        assert next(item for item in after if item["logical_id"] == summary["logical_id"]) == summary
        redone = _redo(client)
        assert redone.success and redone.history_delta is not None
        assert redone.history_delta.inventory_removed_ids == (summary["logical_id"],)
    finally:
        client.close_async()
        wait_until(client.stopped.is_set)


def test_dispatcher_history_budget_evicts_old_steps_without_rejecting_mutations(monkeypatch):
    class Entry:
        byte_size = 900

    class Adapter:
        def __call__(self, _document, _command):
            return AppliedMutation()

        def history_before(self, _document, _command):
            return Entry()

        def history_after(self, entry, _applied):
            return entry

        def undo_history(self, _document, _entry):
            return None

    monkeypatch.setattr(annotation_dispatch, "HISTORY_BYTE_BUDGET", 1000)
    dispatcher = AnnotationCommandDispatcher("bounded", 1,
                                             lambda _page: PageBounds(300, 300), Adapter())
    first = CreateInk(CommandContext.create("bounded", 1),
                      InkTarget(0, ((PdfPoint(10, 10), PdfPoint(20, 20)),)), InkStyle())
    second = CreateInk(CommandContext.create("bounded", 1),
                       InkTarget(0, ((PdfPoint(30, 30), PdfPoint(40, 40)),)), InkStyle())
    assert dispatcher.dispatch(None, command_to_payload(first)).success
    result = dispatcher.dispatch(None, command_to_payload(second))
    assert result.success and (result.history_position, result.history_length) == (1, 1)
    # The retained base is the state after the evicted first entry, so a future
    # save checkpoint can distinguish it from the original state identity.
    undone = dispatcher.dispatch(None, command_to_payload(Undo(CommandContext.create("bounded", 1))))
    assert undone.success and undone.history_state_id != 0
    # An adapter with no trustworthy delta gets the sole documented full M12 fallback.
    assert undone.history_delta is None and undone.refresh_inventory


def test_history_delta_invalidates_only_the_affected_appearance_cache_entry():
    cache = RasterCache(max_bytes=64 * 1024)
    affected = RenderKey("history-cache", 1, 0, 1.0, 1.0, annotation_id="affected")
    untouched = RenderKey("history-cache", 1, 0, 1.0, 1.0, annotation_id="untouched")
    image = lambda: QImage(20, 20, QImage.Format.Format_RGB32)
    assert cache.put(affected, 1_600, 1.0, image)
    assert cache.put(untouched, 1_600, 1.0, image)
    view = SimpleNamespace(
        session=SimpleNamespace(document_id="history-cache", document_generation=1),
        scheduler=SimpleNamespace(budget=cache), _appearance_pending=None,
        _appearance_errors=set(), _history_appearance_invalidations=set(),
        viewport=lambda: SimpleNamespace(update=lambda: None),
    )
    PdfView.invalidate_annotation_appearances(view, ("affected",))
    assert cache.get(affected, record=False) is None
    assert cache.get(untouched, record=False) is not None


@pytest.fixture
def history_manager(app):
    manager = DocumentManager()
    yield manager
    manager.request_shutdown(discard_changes=True)
    wait_until(lambda: manager.is_shutdown)


def _open_history_manager(manager: DocumentManager, path: Path):
    manager.open_path(str(path))
    wait_until(lambda: any(session.lifecycle == Lifecycle.READY for session in manager.sessions.values()))
    return next(iter(manager.sessions.values()))


def test_confirmed_mutation_undo_to_initial_checkpoint_is_clean(
        tmp_path: Path, history_manager, pdf_fixture_copy):
    source = tmp_path / "history-dirty.pdf"
    pdf_fixture_copy(source)
    session = _open_history_manager(history_manager, source)
    create = CreateInk(
        CommandContext.create(session.document_id, session.document_generation),
        InkTarget(0, ((PdfPoint(10, 20), PdfPoint(40, 30)),)),
        InkStyle(),
    )
    assert history_manager.submit_annotation(create).request_id is not None
    wait_until(lambda: session.pending_mutations == 0 and session.document_revision == 1)
    assert session.history_state_id != session.saved_history_state_id
    assert session.dirty

    assert history_manager.submit_annotation(Undo(CommandContext.create(
        session.document_id, session.document_generation))).request_id is not None
    wait_until(lambda: session.pending_mutations == 0 and session.document_revision == 2)
    # Revision is deliberately monotonic; checkpoint state is authoritative.
    assert session.history_state_id == session.saved_history_state_id == 0
    assert not session.dirty


def _semantic(record: dict) -> dict:
    return {key: record[key] for key in ("nm", "contents", "rc", "da", "c", "border", "opacity",
                                         "rect", "cl", "it")}


def test_freetext_native_style_and_rich_text_survive_create_undo_redo(
        tmp_path, history_manager, monkeypatch, pdf_fixture_copy):
    source = tmp_path / "history-rich.pdf"
    pdf_fixture_copy(source)
    monkeypatch.setattr(document_service, "document_worker_main", persistence_worker)
    session = _open_history_manager(history_manager, source)
    deltas = []
    history_manager.history_delta_changed.connect(lambda _document_id, delta: deltas.append(delta))
    style = FreeTextStyle(RgbColor(.15, .35, .85), RgbColor(.94, .89, .50), RgbColor(.8, .1, .2), .73, 2.0, 15)
    rich = RichText((RichTextRun("rosso ", "Helvetica", 15, RgbColor(.8, .1, .2), True, False),
                     RichTextRun("corsivo", "Courier", 19, RgbColor(.1, .5, .2), False, True)))
    create = CreateFreeText(CommandContext.create(session.document_id, session.document_generation),
                            RectTarget(0, PdfRect(30, 40, 330, 160)), rich.plain, style, rich_text=rich)
    history_manager.submit_annotation(create); wait_until(lambda: session.document_revision == 1)
    before = _semantic(inspect_native(Path(str(source) + ".m8-export.pdf"))["records"][0])
    history_manager.submit_annotation(Undo(CommandContext.create(session.document_id, session.document_generation)))
    wait_until(lambda: session.document_revision == 2)
    assert inspect_native(Path(str(source) + ".m8-export.pdf"))["records"] == []
    assert deltas[-1].removed_ids == (create.annotation_id.value,)
    history_manager.submit_annotation(Redo(CommandContext.create(session.document_id, session.document_generation)))
    wait_until(lambda: session.document_revision == 3)
    assert _semantic(inspect_native(Path(str(source) + ".m8-export.pdf"))["records"][0]) == before
    assert deltas[-1].added[0].annotation_id == create.annotation_id
    assert session.annotations[create.annotation_id.value].rich_text == rich
    changed_style = FreeTextStyle(RgbColor(.7, .1, .1), RgbColor(.8, .95, .9), RgbColor(.0, .2, .7), .61, 3.0, 17)
    changed_rich = RichText((RichTextRun("nuovo", "Times", 17, RgbColor(.0, .2, .7), True, True),))
    edit = UpdateFreeText(CommandContext.create(session.document_id, session.document_generation),
                          create.annotation_id, 0, changed_rich.plain, changed_style, changed_rich)
    history_manager.submit_annotation(edit); wait_until(lambda: session.document_revision == 4)
    after_edit = _semantic(inspect_native(Path(str(source) + ".m8-export.pdf"))["records"][0])
    history_manager.submit_annotation(Undo(CommandContext.create(session.document_id, session.document_generation)))
    wait_until(lambda: session.document_revision == 5)
    assert _semantic(inspect_native(Path(str(source) + ".m8-export.pdf"))["records"][0]) == before
    assert deltas[-1].updated[0].annotation_id == create.annotation_id
    history_manager.submit_annotation(Redo(CommandContext.create(session.document_id, session.document_generation)))
    wait_until(lambda: session.document_revision == 6)
    assert _semantic(inspect_native(Path(str(source) + ".m8-export.pdf"))["records"][0]) == after_edit
    assert deltas[-1].updated[0].annotation_id == create.annotation_id


def test_callout_and_unrelated_app_models_survive_editorial_history_transition(
        tmp_path, history_manager, monkeypatch, pdf_fixture_copy):
    source = tmp_path / "history-callout.pdf"
    pdf_fixture_copy(source)
    monkeypatch.setattr(document_service, "document_worker_main", persistence_worker)
    session = _open_history_manager(history_manager, source)
    style = FreeTextStyle(RgbColor(.1, .2, .8), RgbColor(.95, .9, .65), RgbColor(.1, .1, .1), .8, 1.7, 13)
    box = CreateFreeText(CommandContext.create(session.document_id, session.document_generation),
                         RectTarget(0, PdfRect(30, 40, 280, 140)), "box", style)
    callout = CreateCallout(CommandContext.create(session.document_id, session.document_generation),
                            CalloutTarget(0, PdfRect(240, 160, 460, 280), PdfPoint(55, 80), PdfPoint(55, 160)),
                            "callout", style)
    history_manager.submit_annotation(box); wait_until(lambda: session.document_revision == 1)
    history_manager.submit_annotation(callout); wait_until(lambda: session.document_revision == 2)
    history_manager.start_annotation_inventory(session.document_id)
    wait_until(lambda: session.inventory_complete)
    inventory_requests = session.worker.submitted_counts[Operation.ANNOTATION_SUMMARIES]
    box_model = session.annotations[box.annotation_id.value]
    box_detail = session.annotation_details[box.annotation_id.value]
    sidebar = AnnotationSidebar()
    sidebar.set_session(session)
    box_item = next(sidebar.list.item(index) for index in range(sidebar.list.count())
                    if sidebar.list.item(index).data(256) == box.annotation_id.value)
    deltas = []
    history_manager.history_delta_changed.connect(lambda _document_id, delta: deltas.append(delta))
    records = inspect_native(Path(str(source) + ".m8-export.pdf"))["records"]
    before = {record["nm"][1]: _semantic(record) for record in records}
    # Undoing the latest Callout leaves the preceding Text Box byte/semantic-equivalent.
    history_manager.submit_annotation(Undo(CommandContext.create(session.document_id, session.document_generation)))
    wait_until(lambda: session.document_revision == 3)
    after_undo = inspect_native(Path(str(source) + ".m8-export.pdf"))["records"]
    assert _semantic(after_undo[0]) == before[after_undo[0]["nm"][1]]
    assert session.annotations[box.annotation_id.value] is box_model
    assert session.annotation_details[box.annotation_id.value] is box_detail
    assert session.worker.submitted_counts[Operation.ANNOTATION_SUMMARIES] == inventory_requests
    assert deltas[-1].removed_ids == (callout.annotation_id.value,)
    sidebar.apply_history_delta(session, deltas[-1])
    assert next(sidebar.list.item(index) for index in range(sidebar.list.count())
                if sidebar.list.item(index).data(256) == box.annotation_id.value) is box_item
    assert session.annotations[box.annotation_id.value].editable
    history_manager.submit_annotation(Redo(CommandContext.create(session.document_id, session.document_generation)))
    wait_until(lambda: session.document_revision == 4)
    after_redo = {record["nm"][1]: _semantic(record) for record in
                  inspect_native(Path(str(source) + ".m8-export.pdf"))["records"]}
    assert after_redo == before
    edited_style = FreeTextStyle(RgbColor(.8, .1, .2), RgbColor(.85, .95, .8), RgbColor(.2, .2, .7), .67, 2.5, 16)
    edited_target = CalloutTarget(0, callout.target.text_rect, PdfPoint(80, 98), PdfPoint(80, 160))
    update = UpdateCallout(CommandContext.create(session.document_id, session.document_generation),
                           callout.annotation_id, edited_target, "cambiato", edited_style)
    history_manager.submit_annotation(update); wait_until(lambda: session.document_revision == 5)
    edited = {record["nm"][1]: _semantic(record) for record in
              inspect_native(Path(str(source) + ".m8-export.pdf"))["records"]}
    history_manager.submit_annotation(Undo(CommandContext.create(session.document_id, session.document_generation)))
    wait_until(lambda: session.document_revision == 6)
    assert {record["nm"][1]: _semantic(record) for record in
            inspect_native(Path(str(source) + ".m8-export.pdf"))["records"]} == before
    history_manager.submit_annotation(Redo(CommandContext.create(session.document_id, session.document_generation)))
    wait_until(lambda: session.document_revision == 7)
    assert {record["nm"][1]: _semantic(record) for record in
            inspect_native(Path(str(source) + ".m8-export.pdf"))["records"]} == edited
    wait_until(lambda: session.inventory_complete)
    summary = session.annotation_inventory[box.annotation_id.value]
    assert summary.provenance == "app" and summary.capabilities.editable_text
    backing = session.annotations[box.annotation_id.value].backing
    assert f"native:0:{backing.xref}" not in session.annotation_inventory
    sidebar.deleteLater()


def test_undo_redo_editorial_delete_does_not_touch_preceding_freetext_or_callout(
        tmp_path, history_manager, monkeypatch, pdf_fixture_copy):
    source = tmp_path / "history-editorial-isolation.pdf"
    pdf_fixture_copy(source)
    monkeypatch.setattr(document_service, "document_worker_main", persistence_worker)
    session = _open_history_manager(history_manager, source)
    style = FreeTextStyle(RgbColor(.2, .3, .8), RgbColor(.95, .9, .6), RgbColor(.1, .1, .1), .76, 1.8, 14)
    box = CreateFreeText(CommandContext.create(session.document_id, session.document_generation),
                         RectTarget(0, PdfRect(25, 35, 270, 135)), "box", style)
    callout = CreateCallout(CommandContext.create(session.document_id, session.document_generation),
                            CalloutTarget(0, PdfRect(230, 155, 450, 275), PdfPoint(50, 75), PdfPoint(50, 155)),
                            "callout", style)
    history_manager.submit_annotation(box); wait_until(lambda: session.document_revision == 1)
    history_manager.submit_annotation(callout); wait_until(lambda: session.document_revision == 2)
    stable = {record["nm"][1]: _semantic(record) for record in
              inspect_native(Path(str(source) + ".m8-export.pdf"))["records"]}
    delete = CreateDeleteText(CommandContext.create(session.document_id, session.document_generation),
                              confirmed_target(session, 0, 4))
    history_manager.submit_annotation(delete); wait_until(lambda: session.document_revision == 3)
    history_manager.submit_annotation(Undo(CommandContext.create(session.document_id, session.document_generation)))
    wait_until(lambda: session.document_revision == 4)
    after_undo = {record["nm"][1]: _semantic(record) for record in
                  inspect_native(Path(str(source) + ".m8-export.pdf"))["records"]}
    assert after_undo == stable
    history_manager.submit_annotation(Redo(CommandContext.create(session.document_id, session.document_generation)))
    wait_until(lambda: session.document_revision == 5)
    after_redo = {record["nm"][1]: _semantic(record) for record in
                  inspect_native(Path(str(source) + ".m8-export.pdf"))["records"]}
    assert {box.annotation_id.value, callout.annotation_id.value} <= set(after_redo)
    assert {key: after_redo[key] for key in stable} == stable
    replace = CreateReplaceText(CommandContext.create(session.document_id, session.document_generation),
                                confirmed_target(session, 0, 4), "sostituito")
    history_manager.submit_annotation(replace); wait_until(lambda: session.document_revision == 6)
    assert replace.annotation_id.value in session.annotations
    history_manager.submit_annotation(Undo(CommandContext.create(session.document_id, session.document_generation)))
    wait_until(lambda: session.document_revision == 7)
    assert replace.annotation_id.value not in session.annotations
    history_manager.submit_annotation(Redo(CommandContext.create(session.document_id, session.document_generation)))
    wait_until(lambda: session.document_revision == 8)
    # Replace has native components but remains one app/sidebar logical item.
    assert list(item for item in session.annotation_inventory if item == replace.annotation_id.value) == [
        replace.annotation_id.value]
