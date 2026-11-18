"""M20.3 worker, native persistence, failure and history coverage."""

from copy import deepcopy
from dataclasses import asdict, replace
from hashlib import sha256
import json

import pymupdf
import pytest

from pdf_annotator.domain.annotation_commands import CommandContext, Redo, Undo
from pdf_annotator.domain.serialization import command_to_payload, result_from_payload
from pdf_annotator.util.paths import identify_file, identify_save_target
from pdf_annotator.worker.annotation_adapter import ProductAnnotationAdapter
from pdf_annotator.worker.annotation_dispatch import AnnotationCommandDispatcher
from pdf_annotator.worker.document_service import DocumentSource
from pdf_annotator.worker.protocol import Operation
from pdf_annotator.worker.text_service import WorkerTextService

from experiments.m20.application_verify import annotation_state
from experiments.m20 import application_worker
from experiments.m20.application_worker import WorkerPlan
from experiments.m20.apply import BatchFailure, read_plan, run_application, worker_session
from experiments.m20.localization_text import page_bounds
from experiments.m20.localize import localize_files
from experiments.m20.test_localize import proposal


@pytest.fixture
def inputs(tmp_path):
    pdf, corrections, manifest = (tmp_path / filename for filename in
                                   ("original.pdf", "corrections.json", "localization.json"))
    with pymupdf.open() as document:
        page = document.new_page(width=350, height=400)
        page.insert_text((40, 85), "Frase da\nmodificare adesso.")
        page.insert_text((40, 150), "Elimina questa parola.")
        page.insert_text((40, 180), "Conserva Ambigua Ambigua.")
        page.insert_text((40, 205), "    return 0;", fontname="cour")
        page.add_text_annot((40, 310), "Annotazione preesistente")
        page = document.new_page(width=320, height=440)
        page.insert_text((185, 130), "Annota qui.")
        page.set_cropbox(pymupdf.Rect(20, 40, 300, 410))
        page.set_rotation(90)
        document.save(pdf)
    items = [proposal("Frase da modificare"), proposal("parola", operation="delete"),
             proposal("return 0;", operation="insert", side="after"),
             proposal("Annota", operation="comment", page=1), proposal(operation="comment", page=1),
             proposal("Ambigua")]
    items[2]["replacement_text"] = "\n} — è ☃"
    corrections.write_text(json.dumps({"corrections": items}, ensure_ascii=False), encoding="utf-8")
    result = localize_files(pdf, corrections)
    manifest.write_text(json.dumps(result), encoding="utf-8")
    assert [row["status"] for row in result["results"]] == [
        "text_normalized", "text_exact", "text_exact", "text_exact", "page_fallback", "ambiguous"]
    return pdf, corrections, manifest


def test_batch_through_worker_save_as_and_reopen(inputs, tmp_path):
    pdf, corrections, manifest = inputs
    input_hashes = [sha256(path.read_bytes()).hexdigest() for path in inputs]
    output = tmp_path / "output"
    output.mkdir()
    report = run_application(pdf, read_plan(corrections, manifest), output)
    assert report["error"] is None, report
    assert report["summary"] == {"applied": 5, "not_applied": 1, "error": 0}
    assert report["results"][-1]["reason"] == "multiple_text_matches"
    verification = report["verification"]
    assert verification["native_objects"] == {"/Caret": 2, "/StrikeOut": 2, "/Text": 5, "/Popup": 9}
    assert verification["existing_annotations_unchanged"] and verification["base_page_text_and_streams_unchanged"]
    assert [sha256(path.read_bytes()).hexdigest() for path in inputs] == input_hashes


def test_tampered_range_caret_note_and_ambiguous_target_do_not_apply(inputs, tmp_path):
    pdf, corrections, manifest = inputs
    plan = read_plan(corrections, manifest)
    changed = deepcopy(plan.localizations)
    changed[0]["quads"][0][0][0] += 1
    changed[2]["insertion"]["point"][0] += 1
    changed[3]["comment_position"] = [999, 999]
    changed[5]["applicable"] = True
    report = run_application(pdf, replace(plan, localizations=changed), tmp_path)
    assert report["error"] is None
    assert report["summary"] == {"applied": 2, "not_applied": 4, "error": 0}
    assert [row["proposal_index"] for row in report["results"] if row["status"] == "applied"] == [1, 4]
    assert {row["reason"] for row in report["results"] if row["status"] == "not_applied"} == {"stale_text_target"}


def test_proposal_and_pdf_hash_must_match(inputs, tmp_path):
    pdf, corrections, manifest = inputs
    plan = read_plan(corrections, manifest)
    corrections.write_bytes(corrections.read_bytes() + b"\n")
    with pytest.raises(BatchFailure, match="localization_manifest_mismatch"):
        read_plan(corrections, manifest)
    pdf.write_bytes(pdf.read_bytes() + b"\n")
    report = run_application(pdf, plan, tmp_path)
    assert report["error"] == "source_hash_mismatch" and report["output_pdf"] is None
    assert report["summary"]["not_applied"] == len(plan.proposals)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_note_and_insertion_geometry_on_cropped_rotated_page(tmp_path, rotation):
    pdf = tmp_path / "cropped.pdf"
    proposals = tmp_path / "corrections.json"
    manifest = tmp_path / "localization.json"
    with pymupdf.open() as document:
        page = document.new_page(width=320, height=440)
        page.insert_text((185, 130), "Apponi un segno.")
        page.set_cropbox(pymupdf.Rect(20, 40, 300, 410))
        page.set_rotation(rotation)
        document.save(pdf)
    items = [proposal("segno", operation="insert", side="before"), proposal(operation="comment")]
    proposals.write_text(json.dumps({"corrections": items}), encoding="utf-8")
    manifest.write_text(json.dumps(localize_files(pdf, proposals)), encoding="utf-8")
    report = run_application(pdf, read_plan(proposals, manifest), tmp_path)
    assert report["error"] is None, report
    assert report["summary"]["applied"] == 2
    assert report["verification"]["native_structure_verified"]


def test_undo_redo_duplicate_and_generation_barrier(inputs, tmp_path):
    pdf, corrections, manifest = inputs
    source = DocumentSource(identify_file(str(pdf)))
    with worker_session(source, read_plan(corrections, manifest)) as worker:
        created = []
        for index in (0, 1, 2, 3, 4):
            response = worker.request(Operation.ANNOTATION_COMMAND, {"proposal_index": index})
            result = result_from_payload(response.payload)
            assert result.success and result.history_length == index + 1
            created.append(result.annotation_id)
        duplicate = worker.request(Operation.ANNOTATION_COMMAND, {"proposal_index": 0})
        assert not duplicate.success and duplicate.error.code == "duplicate_command"

        def history(command_type, expected_id):
            command = command_type(CommandContext.create(worker.document_id, worker.generation))
            response = worker.request(Operation.ANNOTATION_COMMAND, command_to_payload(command))
            result = result_from_payload(response.payload)
            assert result.success and result.annotation_id == expected_id
            return result.document_revision

        for name in reversed(created):
            history(Undo, name)
        inventory = worker.request(Operation.ANNOTATION_SUMMARIES).payload["summaries"]
        assert [row["kind"] for row in inventory] == ["sticky_note"]  # preexisting
        for name in created:
            revision = history(Redo, name)
        destination = identify_save_target(str(tmp_path / "history.pdf"))
        saved = worker.request(Operation.SAVE,
                               {"target": asdict(destination), "revision": revision, "overwrite": False})
        assert saved.success and worker.generation == 2
        stale = worker.request(Operation.ANNOTATION_COMMAND, {"proposal_index": 5})
        assert not stale.success and stale.error.code == "stale_generation"


def test_page_extraction_failure_isolated_from_other_proposals(inputs, monkeypatch):
    pdf, corrections, manifest = inputs
    identity = identify_file(str(pdf))
    original = application_worker.localize_document

    def extract(document, proposals):
        if proposals[0]["page_index"] == 0:
            raise RuntimeError("Injected extraction error")
        return original(document, proposals)

    monkeypatch.setattr(application_worker, "localize_document", extract)
    with pymupdf.open(pdf) as document:
        service = WorkerTextService("doc", 1, document)
        preparation = WorkerPlan("doc", identity, read_plan(corrections, manifest))
        with pytest.raises(RuntimeError, match="Injected extraction error"):
            preparation.prepare(service, {"proposal_index": 0})
        payload = preparation.prepare(service, {"proposal_index": 3})
        dispatcher = AnnotationCommandDispatcher(
            "doc", 1, lambda number: page_bounds(document[number]), ProductAnnotationAdapter(service))
        assert dispatcher.dispatch(document, payload).success


@pytest.mark.parametrize("index", [0, 1, 2, 3, 4])
def test_atomic_rollback_if_finalization_fails(inputs, index):
    pdf, corrections, manifest = inputs
    identity = identify_file(str(pdf))
    plan = read_plan(corrections, manifest)
    with pymupdf.open(pdf) as document:
        service = WorkerTextService("doc", 1, document)
        preparation = WorkerPlan("doc", identity, plan)
        payload = preparation.prepare(service, {"proposal_index": index})
        baseline = annotation_state(document)
        dispatcher = AnnotationCommandDispatcher(
            "doc", 1, lambda number: page_bounds(document[number]), ProductAnnotationAdapter(service))

        def fail(_result):
            raise RuntimeError("Injected finalization failure")

        result = dispatcher.dispatch(document, payload, finalize=fail)
        assert not result.success and result.error.code.value == "internal_pdf_mutation"
        assert dispatcher.document_revision == result.history_length == 0
        assert annotation_state(document) == baseline
