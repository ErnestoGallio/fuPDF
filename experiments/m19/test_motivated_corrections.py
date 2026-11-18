"""M19-only checks: no application GUI and no full-suite invocation."""

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from shutil import copyfile
import subprocess
import sys

import pymupdf
import pytest

from experiments.m19 import motivated_corrections as probe


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    output = tmp_path_factory.mktemp("m19-parent") / "probe"
    result = subprocess.run([
        sys.executable, "-m", "experiments.m19.motivated_corrections",
        "--output-dir", str(output)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    return output, manifest


def test_native_graph_payloads_popups_and_two_reopens(generated):
    directory, manifest = generated
    assert manifest["automatic_checks"] == "PASS"
    assert manifest["acrobat"] == manifest["indesign"] == "NOT RUN"
    assert [case["revision"] for case in manifest["cases"]] == [1, 2, 3]
    assert sha256((directory / "m19_source.pdf").read_bytes()).hexdigest() == manifest["source_sha256"]
    for filename in ("m19_motivated_corrections.pdf", "m19_pymupdf_resaved.pdf"):
        with pymupdf.open(directory / filename) as document:
            report = probe.validate_document(document, manifest)
            assert len(report["annotation_objects"]) == 14
            assert all(check["status"] == "PASS" for check in report["checks"])


def test_delete_confirms_second_occurrence_and_insert_boundary(generated):
    directory, manifest = generated
    with pymupdf.open(directory / "m19_source.pdf") as document:
        service = probe.WorkerTextService(probe.DOCUMENT_ID, 1, document)
        second = probe.confirmed_range(service, 1, probe.CASES[1])
        first = probe.confirmed_range(service, 1, replace(probe.CASES[1], occurrence=0))
        assert second.start_char_index == first.end_char_index + 1
        assert second.original_text == "molto "
        insertion = manifest["cases"][2]["command"]["target"]
        selected = probe.confirmed_range(service, 2, probe.CASES[2])
        assert insertion["char_index"] == selected.start_char_index
        assert insertion["affinity"] == "before"


@pytest.mark.parametrize("corruption", ["group_parent", "popup_parent", "reply_type", "payload", "page_content", "appearance"])
def test_validation_rejects_corruptions(generated, tmp_path, corruption):
    directory, manifest = generated
    copy = tmp_path / "private.pdf"
    copyfile(directory / "m19_motivated_corrections.pdf", copy)
    with pymupdf.open(copy) as document:
        records = probe.annotation_records(document)
        by_name = {r["keys"].get("NM"): r for r in records}
        case = manifest["cases"][0]
        root = by_name[case["root_name"]]["xref"]
        strike = by_name[case["root_name"] + ":strike"]["xref"]
        reply = by_name[case["root_name"] + ":reason"]["xref"]
        if corruption == "group_parent":
            document.xref_set_key(reply, "IRT", f"{strike} 0 R")
        elif corruption == "popup_parent":
            popup = int(probe.key(document, root, "Popup").split()[0])
            document.xref_set_key(reply, "IRT", f"{popup} 0 R")
        elif corruption == "reply_type":
            document.xref_set_key(reply, "RT", "/Group")
        elif corruption == "payload":
            document.xref_set_key(root, "Contents", pymupdf.get_pdf_str(case["reason"]))
        elif corruption == "appearance":
            appearance = int(probe.key(document, root, "AP/N").split()[0])
            document.update_stream(appearance, b"q Q")
        else:
            document[0].insert_text((48, 480), "Unwanted page edit")
        with pytest.raises(ValueError):
            probe.validate_document(document, manifest)


def test_reply_creator_refuses_group_member(generated, tmp_path):
    directory, manifest = generated
    copy = tmp_path / "private.pdf"
    copyfile(directory / "m19_motivated_corrections.pdf", copy)
    with pymupdf.open(copy) as document:
        before = probe.annotation_records(document)
        with pytest.raises(ValueError, match="not a primary correction"):
            probe.add_reply(document, 0, manifest["cases"][0]["root_name"] + ":strike", "reason")
        assert probe.annotation_records(document) == before


def test_unicode_plain_payload_and_reason_stay_separate(tmp_path, monkeypatch):
    payload = "caffè & tè <ok> e\u0301 ①\nseconda riga"
    reason = "Motivazione distinta: perché & < > e\u0301 ①\nAltra riga."
    monkeypatch.setattr(probe, "CASES", tuple(
        replace(case, payload=None if case.kind == "delete" else payload, reason=reason)
        for case in probe.CASES))
    directory = tmp_path / "unicode"
    manifest = probe.run_probe(directory)
    with pymupdf.open(directory / "m19_motivated_corrections.pdf") as document:
        report = probe.validate_document(document, manifest)
        assert len(report["checks"]) == 3


def test_existing_output_is_never_overwritten(tmp_path):
    marker = tmp_path / "keep.txt"
    marker.write_text("existing evidence", encoding="utf-8")
    with pytest.raises(FileExistsError):
        probe.run_probe(tmp_path)
    assert marker.read_text(encoding="utf-8") == "existing evidence"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["keep.txt"]
