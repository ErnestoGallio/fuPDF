"""Run with `python -m tests.m10_probe output/m10/probe` (development harness, no UI).

Produces disposable source/export/manifest. Never takes a user PDF as writable
input; the actual corrections go through AnnotationService and the product worker.
"""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
from shutil import copyfile
import subprocess
import sys

from PySide6.QtCore import QCoreApplication

from pdf_annotator.app.document_manager import DocumentManager
from pdf_annotator.domain.annotation_commands import (
    CommandContext, CommandSource, CreateDeleteText, CreateInsertText, CreateReplaceText,
)
from pdf_annotator.domain.results import RequestState
from pdf_annotator.domain.serialization import command_to_payload, target_from_payload
from pdf_annotator.worker import document_service
from pdf_annotator.worker.protocol import Operation
from .conftest import request, wait_until
from .m10_helpers import persistence_worker
from .m6_fixtures import quad_center
from .test_m6_service_persistence import _opened, _page_records


def context(session):
    return CommandContext.create(session.document_id, session.document_generation, CommandSource.INTERNAL)


def range_target(session, text: str, *, reverse: bool = False, occurrence: int = 0, page_index: int = 0):
    """Locate a deterministic fixture occurrence, then confirm through M6 geometry."""
    index, records = _page_records(session, page_index)
    joined = "".join(record[1] for record in records)
    start = -1
    for _ in range(occurrence + 1):
        start = joined.find(text, start + 1)
        if start < 0:
            raise ValueError(f"Fixture target not found: {text!r}")
    offsets = []
    for record in records:
        offsets.extend([record] * len(record[1]))
    first, last = offsets[start], offsets[start + len(text) - 1]
    if reverse:
        first, last = last, first
    response = request(session.worker, Operation.TEXT_SELECTION, {
        "page_index": page_index, "page_text_id": index["page_text_id"],
        "anchor": quad_center(first[2]), "focus": quad_center(last[2]),
    })
    assert response.success, response.error
    assert response.payload["status"] == "confirmed"
    return target_from_payload(response.payload["target"])


def insertion_target(session, text: str = "The", *, affinity: str = "after"):
    ranged = range_target(session, text)
    response = request(session.worker, Operation.TEXT_INSERTION, {
        "page_index": 0, "page_text_id": ranged.snapshot_id,
        "char_index": ranged.end_char_index if affinity == "after" else ranged.start_char_index,
        "affinity": affinity,
    })
    assert response.success and response.payload["status"] == "confirmed", response.error
    return target_from_payload(response.payload["target"])


def submit(manager, command):
    results = []
    manager.annotation_result.connect(results.append)
    try:
        submission = manager.submit_annotation(command)
        assert submission.state == RequestState.PENDING, submission
        wait_until(lambda: results)
        result = results[-1]
        assert result.success, result
        return result
    finally:
        manager.annotation_result.disconnect(results.append)


def inspect(path: Path):
    result = subprocess.run([sys.executable, "-m", "tests.m10_helpers", "--inspect", str(path)],
                            check=True, capture_output=True, text=True, encoding="utf-8", timeout=40)
    return json.loads(result.stdout)


def signature(report, root):
    """Describe the observed graph, independent of xref/NM values and timestamps."""
    def key(annotation, name):
        return annotation["editorial_keys"][name]["value"]
    members = [a for a in report["annotations"] if key(a, "IRT") == f'{root["xref"]} 0 R']
    return {
        "root": {k: key(root, k) for k in ("Subtype", "IT", "RT", "IRT", "Contents")},
        "members": [{k: key(a, k) for k in ("Subtype", "IT", "RT", "Contents")} for a in members],
        "popup_count": sum(key(a, "Popup") != "null" for a in (root, *members)),
        "quad_counts": [len(key(a, "QuadPoints").strip("[]").split()) // 8
                        for a in (root, *members) if key(a, "QuadPoints") != "null"],
    }


def compare_reference(reference, probe, manifest):
    compared = []
    for case in manifest[:3]:
        root = next(a for a in probe["annotations"] if a["api_nm"] == case["command"]["annotation_id"])
        original = next(a for a in reference["annotations"]
                        if a["editorial_keys"]["Subtype"] == root["editorial_keys"]["Subtype"]
                        and a["editorial_keys"]["IT"] == root["editorial_keys"]["IT"]
                        and a["editorial_keys"]["IRT"]["value"] == "null")
        expected, observed = signature(reference, original), signature(probe, root)
        compared.append({"case": case["case"], "reference": expected, "probe": observed,
                         "semantic_structure_equal": expected == observed})
    return {"reference_sha256": reference["sha256"], "probe_sha256": probe["sha256"],
            "comparisons": compared, "acrobat": "NOT RUN", "indesign": "NOT RUN"}


def main(directory: Path, *, acrobat_reference: Path | None = None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "m10_editorial_source.pdf"
    if source.exists():
        raise FileExistsError("Use a new disposable directory for the M10 probe")
    subprocess.run([sys.executable, "-m", "tests.m10_helpers", "--source", str(source)],
                   check=True, timeout=30)
    baseline = inspect(source)
    application = QCoreApplication.instance() or QCoreApplication([])
    previous = document_service.document_worker_main
    document_service.document_worker_main = persistence_worker
    manager = DocumentManager()
    manifest = []
    try:
        session = _opened(manager, source)
        cases = (
            ("delete_brown", lambda: CreateDeleteText(context(session), range_target(session, "brown"))),
            ("insert_very", lambda: CreateInsertText(context(session), insertion_target(session), " very")),
            ("replace_lazy", lambda: CreateReplaceText(context(session), range_target(session, "lazy"), "sleepy")),
            ("unicode_replace", lambda: CreateReplaceText(context(session), range_target(session, "editable"),
                                                        "città, perché, Über, 日本語 & <ok> e\u0301 ①")),
            ("multiline_delete", lambda: CreateDeleteText(context(session), range_target(
                session, "begins here.and finishes", reverse=True))),
        )
        for label, build in cases:
            command = build()
            result = submit(manager, command)
            manifest.append({"case": label, "command": command_to_payload(command),
                             "revision": result.document_revision,
                             "backing": asdict(result.annotation.backing)})
    finally:
        for session in tuple(manager.sessions.values()):
            manager.request_close(session.document_id, discard_changes=True)
        manager.request_shutdown()
        wait_until(lambda: manager.is_shutdown)
        document_service.document_worker_main = previous
    output = directory / "m10_editorial_comments_probe.pdf"
    copyfile(str(source) + ".m10-export.pdf", output)
    observed = inspect(output)
    assert baseline["content_streams"] == observed["content_streams"]
    assert baseline["base_rasters"] == observed["base_rasters"]
    assert baseline["page_text"] == observed["page_text"]
    (directory / "m10_probe_inspection.json").write_text(
        json.dumps(observed, ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / "m10_probe_manifest.json").write_text(
        json.dumps({"cases": manifest, "base_content_unchanged": True,
                    "acrobat": "NOT RUN", "indesign": "NOT RUN"}, ensure_ascii=False, indent=2), encoding="utf-8")
    reference_path = acrobat_reference
    if reference_path is None:
        reference_path = directory / "synthetic-editorial-reference.pdf"
        subprocess.run([sys.executable, "-m", "tests.m10_helpers", "--external-editorials",
                        str(reference_path)], check=True, timeout=30)
    reference = inspect(reference_path)
    comparison = compare_reference(reference, observed, manifest)
    comparison["reference_origin"] = "acrobat-user-supplied" if acrobat_reference else "synthetic"
    assert all(item["semantic_structure_equal"] for item in comparison["comparisons"])
    (directory / "m10_structure_comparison.json").write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(output.resolve()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--acrobat-reference", type=Path,
                        help="Genuine Acrobat fixture supplied for a local comparison")
    args = parser.parse_args()
    main(args.directory, acrobat_reference=args.acrobat_reference)
