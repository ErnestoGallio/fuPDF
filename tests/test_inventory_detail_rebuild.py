"""Regressions for native detail reads after lazy inventory invalidation."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from pdf_annotator.domain.annotation_commands import (CommandContext, CreateInk,
    DeleteAnnotation, DeleteExternalAnnotation, Undo, Redo)
from pdf_annotator.domain.annotation_inventory import AnnotationDetail, external_command_annotation_id
from pdf_annotator.domain.geometry import PdfPoint
from pdf_annotator.domain.targets import InkTarget
from pdf_annotator.domain.styles import InkStyle
from pdf_annotator.domain.serialization import command_to_payload
from pdf_annotator.worker.annotation_inventory import NativeAnnotationInventory
from pdf_annotator.worker.protocol import Operation, ErrorInfo
from tests.test_sticky_note_reader import (note_pdf, opened_notes, note, click_note, PLAIN, LONG)
from tests.test_annotation_inventory_m12 import _client, _open, _request
from tests.test_save_m15 import ink, submit
from tests.conftest import receive, wait_until


def test_lazy_detail_scans_once_for_all_chunks_and_unknown_ids(monkeypatch):
    from pdf_annotator.worker import external_profiles
    document = SimpleNamespace(load_page=lambda _: SimpleNamespace(load_annot=lambda _: None))
    monkeypatch.setattr(external_profiles, 'inspect_profile', lambda *_:
                        SimpleNamespace(capabilities=lambda: SimpleNamespace(editable_text=False)))
    inventory = NativeAnnotationInventory(document)
    scans = []
    def scan():
        scans.append(True)
        inventory._summaries = ()
        inventory._details['native:0:5'] = AnnotationDetail('native:0:5', LONG)
    monkeypatch.setattr(inventory, '_scan', scan)
    first = inventory.detail('native:0:5', 0)
    inventory.detail('native:0:5', 0)
    assert first['next_offset'] > 0 and not first['complete']
    parts, result = [first['contents']], first
    while not result['complete']:
        result = inventory.detail('native:0:5', result['next_offset'])
        parts.append(result['contents'])
    assert ''.join(parts) == LONG
    with pytest.raises(ValueError, match='Unknown annotation'):
        inventory.detail('native:0:999', 0)
    assert len(scans) == 1


@pytest.mark.parametrize('text', [PLAIN, LONG], ids=['short', 'multichunk'])
def test_gui_ink_then_uncached_note(opened_notes, text):
    window, session, view, _path = opened_notes
    target = note(session, text)
    assert target.logical_id not in session.annotation_details
    submit(window.manager, ink(session))
    reader = click_note(view, target)
    wait_until(lambda: reader.text.toPlainText() == text)
    assert window.annotation_sidebar.detail.toPlainText() == text


def test_real_ipc_interleaved_chunks_delete_and_history(note_pdf, app):
    client = _client(note_pdf)
    try:
        _open(client)
        records = _request(client, Operation.ANNOTATION_SUMMARIES, {'cursor': 0})['summaries']
        target = next(s for s in records if s['page_index'] == 0 and s['preview'] == LONG[:240] + '…')
        other = next(s for s in records if s['page_index'] == 0 and s['preview'] == PLAIN)
        context = lambda: CommandContext.create(client.document_id, client.generation)
        def mutate(command):
            payload = _request(client, Operation.ANNOTATION_COMMAND, command_to_payload(command))
            assert payload['outcome'] == 'success'
        def detail(offset=0):
            return _request(client, Operation.ANNOTATION_DETAIL,
                            {'logical_id': target['logical_id'], 'offset': offset})
        first = detail()
        created = CreateInk(context(), InkTarget(0, ((PdfPoint(20,30), PdfPoint(40,50)),)), InkStyle())
        mutate(created)
        parts, offset = [first['contents']], first['next_offset']
        while True:
            result = detail(offset)
            parts.append(result['contents'])
            offset = result['next_offset']
            if result['complete']:
                break
        assert ''.join(parts) == LONG
        mutate(DeleteAnnotation(context(), created.annotation_id, 0))
        assert detail()['contents'] == first['contents']
        mutate(Undo(context()))
        assert detail()['contents'] == first['contents']
        mutate(Redo(context()))
        assert detail()['contents'] == first['contents']
        mutate(DeleteExternalAnnotation(context(), external_command_annotation_id(other['logical_id']), other['logical_id'], 0))
        assert detail()['contents'] == first['contents']
        mutate(Undo(context()))
        assert detail()['contents'] == first['contents']
        mutate(Redo(context()))
        assert detail()['contents'] == first['contents']
        mutate(DeleteExternalAnnotation(context(), external_command_annotation_id(target['logical_id']), target['logical_id'], 0))
        request = client.submit(Operation.ANNOTATION_DETAIL, {'logical_id': target['logical_id'], 'offset': 0})
        with receive(client) as delivery:
            assert delivery.response.request_id == request
            assert not delivery.response.success
            assert not delivery.response.payload
    finally:
        client.close_async()
        wait_until(lambda: client.stopped.is_set())


def test_sidebar_failure_and_late_other_target(opened_notes, monkeypatch):
    window, session, view, _path = opened_notes
    poll = session.worker.poll
    def fail_detail(request_id=None):
        delivery = poll(request_id)
        if delivery and delivery.response.operation == Operation.ANNOTATION_DETAIL:
            delivery.response = replace(delivery.response, success=False, payload={}, error=ErrorInfo('TEST', 'failure'))
        return delivery
    monkeypatch.setattr(session.worker, 'poll', fail_detail)
    target = note(session)
    click_note(view, target)
    wait_until(lambda: window.annotation_sidebar.detail.toPlainText().startswith('Impossibile'))
    count = session.worker.submitted_counts[Operation.ANNOTATION_DETAIL]
    view.sticky_note_reader.tick()
    assert session.worker.submitted_counts[Operation.ANNOTATION_DETAIL] == count
    monkeypatch.setattr(session.worker, 'poll', poll)
    click_note(view, note(session, LONG))
    wait_until(lambda: window.annotation_sidebar.detail.toPlainText() == LONG)
    window._annotation_detail_failed(session.document_id, target.logical_id)
    window._annotation_detail_failed('other-document', note(session, LONG).logical_id)
    assert window.annotation_sidebar.detail.toPlainText() == LONG
