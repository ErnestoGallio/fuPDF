"""M10 native persistence/geometry/atomicity. Adobe gates remain manual."""

from dataclasses import replace
from functools import partial
from hashlib import sha256
import json
from pathlib import Path
from shutil import copyfile
import subprocess
import sys
from xml.etree import ElementTree

import pytest

from pdf_annotator.app.document_manager import DocumentManager
from pdf_annotator.domain.annotation_commands import CreateDeleteText, CreateInsertText, CreateReplaceText, DeleteAnnotation
from pdf_annotator.domain.errors import MutationErrorCode
from pdf_annotator.domain.serialization import command_from_payload, command_to_payload
from pdf_annotator.worker import document_service
from pdf_annotator.worker.protocol import Operation
from .conftest import request, wait_until
from .m10_helpers import failure_worker, persistence_worker
from .m10_probe import context, inspect, insertion_target, range_target, signature, submit
from .m6_fixtures import quad_center
from .test_m6_service_persistence import _opened, _page_records

@pytest.fixture
def manager(app, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", persistence_worker)
    instance = DocumentManager()
    yield instance
    workers = [session.worker for session in instance.sessions.values()]
    for session in tuple(instance.sessions.values()):
        instance.request_close(session.document_id, discard_changes=True)
    instance.request_shutdown()
    wait_until(lambda: instance.is_shutdown)
    assert all(worker.stopped.is_set() for worker in workers)


def source_file(tmp_path, rotation=0, crop=False):
    source = tmp_path / "source.pdf"
    subprocess.run([sys.executable, "-m", "tests.m10_helpers", "--source", str(source),
                    str(rotation), "crop" if crop else "full"], check=True, timeout=30)
    return source


def by_name(report, name):
    matches = [a for a in report["annotations"] if a["api_nm"] == name]
    assert len(matches) == 1
    return matches[0]


def key(annotation, name):
    return annotation["editorial_keys"][name]["value"]


def numbers(value):
    return [float(item) for item in value.strip("[]").split()]


def assert_strikeout_quad(worker, reference, *, acrobat):
    assert len(worker) == len(reference) == 8
    assert worker[::2] == pytest.approx(reference[::2], abs=.002, rel=0)
    # Reader and MuPDF use different font bounds vertically for this text.
    # The observed maximum edge difference is .630 pt. Horizontal bounds
    # still identify the exact first and last character of the shared word.
    assert worker[1::2] == pytest.approx(reference[1::2], abs=.75 if acrobat else .002, rel=0)


@pytest.mark.parametrize("dx,dy", [(.1, 0), (0, 1)])
def test_acrobat_quad_comparison_rejects_shift(dx, dy):
    reference = [86.64, 564.370, 127.26, 564.370, 86.64, 545.575, 127.26, 545.575]
    shifted = [value + (dy if index % 2 else dx) for index, value in enumerate(reference)]
    with pytest.raises(AssertionError):
        assert_strikeout_quad(shifted, reference, acrobat=True)


def text_at_quad(annotation, characters):
    xs, ys = zip(*annotation["api"]["vertices"], strict=True)
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    centers = ((record[1], quad_center(record[2])) for record in characters if record[2])
    return "".join(text for text, (x, y) in centers if x0 <= x <= x1 and y0 <= y <= y1)


def assert_preserved(before, after):
    for field in ("page_text", "content_streams", "base_rasters"):
        assert before[field] == after[field]
    objects = {item["xref"]: item for item in after["objects"]}
    for original in before["objects"]:
        if original.get("traversal_boundary") or original.get("role") == "page_annotation_array":
            continue  # The page's /Annots container is the intentional change.
        current = objects[original["xref"]]
        # MuPDF can reorder dictionary keys on write. Compare every key/value
        # and stream byte, not the whitespace/order of xref_object's rendering.
        def stream_keys(record):
            keys = dict(record["keys"])
            # Equivalent PDF spelling of a one-stage stream filter. MuPDF's
            # save can unwrap it without changing any encoded stream bytes.
            if keys.get("Filter") == {"type": "array", "value": "[/FlateDecode]"}:
                keys["Filter"] = {"type": "name", "value": "/FlateDecode"}
            return keys
        assert stream_keys(current) == stream_keys(original)
        assert sorted((r["xref"], r["generation"]) for r in current["references"]) == sorted(
            (r["xref"], r["generation"]) for r in original["references"])
        assert current.get("stream") == original.get("stream")


def test_reference_and_worker_have_same_semantic_graph(tmp_path, manager, external_editorials, acrobat_reference):
    reference_hash = sha256(external_editorials.read_bytes()).hexdigest()
    reference = inspect(external_editorials)
    original_roots = [a for a in reference["annotations"] if key(a, "Subtype") in ("/Caret", "/StrikeOut")
                      and key(a, "IRT") == "null"]
    assert len(original_roots) == 3
    # Work on a copy and preserve every baseline object while adding our counterparts.
    source = tmp_path / "reference-copy.pdf"
    copyfile(external_editorials, source)
    session = _opened(manager, source)
    commands = (CreateDeleteText(context(session), range_target(session, "brown")),
                CreateInsertText(context(session), insertion_target(session), " very"),
                CreateReplaceText(context(session), range_target(session, "lazy"), "sleepy"))
    for command in commands:
        submit(manager, command)
    exported = inspect(Path(str(source) + ".m10-export.pdf"))
    assert_preserved(reference, exported)
    for command in commands:
        ours = by_name(exported, command.annotation_id.value)
        original = next(a for a in original_roots if key(a, "Subtype") == key(ours, "Subtype")
                        and key(a, "IT") == key(ours, "IT"))
        assert ours["page_index"] == original["page_index"] == command.target.page_index
        assert signature(exported, ours) == signature(reference, original)
        if key(ours, "Subtype") == "/StrikeOut":
            assert_strikeout_quad(numbers(key(ours, "QuadPoints")), numbers(key(original, "QuadPoints")),
                                  acrobat=external_editorials == acrobat_reference)
            expected_vertices = [coordinate for quad in command.target.quads
                                 for point in quad.points for coordinate in (point.x, point.y)]
            actual_vertices = [coordinate for point in ours["api"]["vertices"] for coordinate in point]
            assert actual_vertices == pytest.approx(expected_vertices, abs=.002, rel=0)
            _, characters = _page_records(session, command.target.page_index)
            assert text_at_quad(ours, characters) == text_at_quad(original, characters) == "brown"
    assert sha256(external_editorials.read_bytes()).hexdigest() == reference_hash


@pytest.mark.parametrize("rotation,crop", [(0, False), (0, True), (90, True), (180, True), (270, True)])
def test_native_geometry_unicode_reopen_and_whole_correction_delete(tmp_path, manager, rotation, crop):
    source = source_file(tmp_path, rotation, crop)
    baseline = inspect(source)
    source_hash = sha256(source.read_bytes()).hexdigest()
    session = _opened(manager, source)
    selected = range_target(session, "brown", reverse=True)
    forward = range_target(session, "brown")
    assert (selected.quads, selected.original_text) == (forward.quads, "brown")
    inserted = insertion_target(session)
    unicode_text = " città, perché, Über, 日本語 & <ok> e\u0301 ①\n"
    commands = (CreateDeleteText(context(session), selected),
                CreateInsertText(context(session), inserted, unicode_text),
                CreateReplaceText(context(session), range_target(session, "lazy"), unicode_text))
    for revision, command in enumerate(commands, 1):
        assert command_from_payload(json.loads(json.dumps(command_to_payload(command)))) == command
        result = submit(manager, command)
        assert result.document_revision == revision
        assert result.annotation.annotation_id == command.annotation_id
    exported = Path(str(source) + ".m10-export.pdf")
    report = inspect(exported)
    assert_preserved(baseline, report)
    roots = [by_name(report, command.annotation_id.value) for command in commands]
    strike = by_name(report, commands[-1].annotation_id.value + ":strike")
    assert key(strike, "IRT") == f'{roots[-1]["xref"]} 0 R'
    assert key(strike, "RT") == "/Group" and key(strike, "IT") == "/StrikeOutTextEdit"
    for command, annotation in ((commands[0], roots[0]), (commands[2], strike)):
        # Independent native coordinate expectation for the generated 600x500
        # fixture: crop-local x+20, PDF y=500-(canonical y+30), ignoring /Rotate.
        expected = [value for quad in command.target.quads for p in quad.points
                    for value in (p.x + (20 if crop else 0), 500 - p.y - (30 if crop else 0))]
        assert numbers(key(annotation, "QuadPoints")) == pytest.approx(expected, abs=.002)
        expected_vertices = [(p.x, p.y) for q in command.target.quads for p in q.points]
        assert len(annotation["api"]["vertices"]) == len(expected_vertices)
        for actual, expected_vertex in zip(annotation["api"]["vertices"], expected_vertices):
            assert actual == pytest.approx(expected_vertex, abs=.002)
    for annotation in roots[1:]:
        assert key(annotation, "Contents") == unicode_text
        assert "".join(ElementTree.fromstring(key(annotation, "RC")).itertext()) == unicode_text
    # The inner bottom-centre of the Caret is exactly the confirmed boundary.
    rect = roots[1]["api"]["rect"]
    obj = next(item for item in report["objects"] if item["xref"] == roots[1]["xref"])
    rd = numbers(obj["keys"]["RD"]["value"])
    assert (rect[0] + rect[2]) / 2 == pytest.approx(inserted.anchor.x, abs=.002)
    assert rect[3] - rd[1] == pytest.approx(inserted.anchor.y, abs=.002)
    assert len(session.annotations) == 3 and session.dirty
    for command in reversed(commands):
        submit(manager, DeleteAnnotation(context(session), command.annotation_id, 0))
    final = inspect(exported)
    assert_preserved(baseline, final)
    assert final["annotations"] == baseline["annotations"]
    assert session.document_revision == 6 and not session.annotations
    assert sha256(source.read_bytes()).hexdigest() == source_hash


@pytest.mark.parametrize("stage", ["strike", "caret", "links", "delete"])
def test_partial_failure_restores_the_entire_correction_and_revision(tmp_path, manager, monkeypatch, stage):
    monkeypatch.setattr(document_service, "document_worker_main", partial(failure_worker, stage=stage))
    source = source_file(tmp_path)
    before = inspect(source)
    session = _opened(manager, source)
    command = CreateReplaceText(context(session), range_target(session, "lazy"), "sleepy")
    if stage == "delete":
        submit(manager, command)
        before = inspect(Path(str(source) + ".m10-export.pdf"))
        failing = DeleteAnnotation(context(session), command.annotation_id, 0)
    else:
        failing = command
    results = []
    manager.annotation_result.connect(results.append)
    manager.submit_annotation(failing)
    wait_until(lambda: results)
    assert results[-1].error.code == MutationErrorCode.INTERNAL_PDF_MUTATION
    assert session.document_revision == (1 if stage == "delete" else 0)
    assert session.pending_mutations == 0
    after = inspect(Path(str(source) + ".m10-export.pdf"))
    assert_preserved(before, after)
    assert after["annotations"] == before["annotations"]
    # A fresh command can use the unchanged confirmed target after rollback.
    retry = replace(failing, context=context(session))
    submit(manager, retry)
    after_retry = inspect(Path(str(source) + ".m10-export.pdf"))
    assert len(after_retry["annotations"]) == (2 if stage == "delete" else 6)


def test_insertion_boundaries_stale_and_mismatch_rejected(tmp_path, manager):
    source = source_file(tmp_path)
    session = _opened(manager, source)
    first = insertion_target(session, "The", affinity="before")
    last = insertion_target(session, "dog.", affinity="after")
    assert first.char_index < last.char_index
    assert first.anchor.x == 60 and first.affinity.value == "before"
    page_index, _ = _page_records(session, 0)
    response = request(session.worker, Operation.TEXT_INSERTION, {
        "page_index": 0, "page_text_id": "0" * 32, "char_index": 0, "affinity": "before"})
    assert response.success and response.payload == {"status": "stale", "target": None}
    response = request(session.worker, Operation.TEXT_INSERTION, {
        "page_index": 0, "page_text_id": page_index["page_text_id"],
        "char_index": page_index["character_count"], "affinity": "after"})
    assert not response.success
    results = []
    manager.annotation_result.connect(results.append)
    command = CreateInsertText(context(session), replace(first, char_index=last.char_index), "very")
    manager.submit_annotation(command)
    wait_until(lambda: results)
    assert results[-1].error.code == MutationErrorCode.STALE_TEXT_TARGET
    assert not session.dirty and session.document_revision == 0


def test_multiline_reverse_selection_persists_ordered_quads(tmp_path, manager):
    source = source_file(tmp_path)
    session = _opened(manager, source)
    target = range_target(session, "begins here.and finishes", reverse=True)
    assert len(target.quads) == 2 and target.original_text == "begins here.\nand finishes"
    command = CreateReplaceText(context(session), target, "one line")
    submit(manager, command)
    report = inspect(Path(str(source) + ".m10-export.pdf"))
    strike = by_name(report, command.annotation_id.value + ":strike")
    assert len(numbers(key(strike, "QuadPoints"))) == 16
    assert key(by_name(report, command.annotation_id.value), "Contents") == "one line"


def test_m6_columns_and_no_text_insertion(tmp_path, manager, pdf_fixture_copy):
    source = pdf_fixture_copy(tmp_path / "m6-editorial.pdf")
    session = _opened(manager, source)
    target = range_target(session, "Colonna sinistra unoColonna sinistra due", page_index=1, reverse=True)
    assert target.original_text == "Colonna sinistra uno\nColonna sinistra due"
    assert all(p.x < 260 for q in target.quads for p in q.points)
    command = CreateDeleteText(context(session), target)
    submit(manager, command)
    page_index = request(session.worker, Operation.TEXT_PAGE_INDEX, {"page_index": 2}).payload["page_text"]
    response = request(session.worker, Operation.TEXT_INSERTION, {
        "page_index": 2, "page_text_id": page_index["page_text_id"], "char_index": 0, "affinity": "before"})
    assert response.success and response.payload == {"status": "no_selection", "target": None}
    report = inspect(Path(str(source) + ".m10-export.pdf"))
    assert by_name(report, command.annotation_id.value)["page_index"] == 1
    assert session.document_revision == 1


def test_targets_are_document_local_and_confirmation_registry_is_bounded(tmp_path, manager):
    source = source_file(tmp_path)
    alternate = tmp_path / "alternate.pdf"
    copyfile(source, alternate)
    first, second = _opened(manager, source), _opened(manager, alternate)
    target = insertion_target(first)
    results = []
    manager.annotation_result.connect(results.append)
    manager.submit_annotation(CreateInsertText(context(second), target, "very"))
    wait_until(lambda: results)
    assert results[-1].error.code == MutationErrorCode.STALE_TEXT_TARGET
    for _ in range(64):
        response = request(first.worker, Operation.TEXT_INSERTION, {
            "page_index": 0, "page_text_id": target.snapshot_id,
            "char_index": target.char_index, "affinity": target.affinity.value})
        assert response.success
    manager.submit_annotation(CreateInsertText(context(first), target, "very"))
    wait_until(lambda: len(results) == 2)
    assert results[-1].error.code == MutationErrorCode.STALE_TEXT_TARGET
    assert not first.dirty and not second.dirty
    refreshed = insertion_target(first)
    created = CreateInsertText(context(first), refreshed, "very")
    submit(manager, created)
    # Different command identity may not reuse an existing /NM.
    manager.submit_annotation(replace(created, context=context(first)))
    wait_until(lambda: len(results) == 4)
    assert results[-1].error.code == MutationErrorCode.INVALID_COMMAND
    assert first.document_revision == 1 and second.document_revision == 0
