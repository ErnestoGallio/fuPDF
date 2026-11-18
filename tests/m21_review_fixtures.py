"""Reproducible PDFs and read-only inspection in a separate test process."""

from base64 import b64decode
import json
from pathlib import Path
import random
import subprocess
import sys

from pdf_annotator.worker.review_snapshot import write_snapshot


def run_fixture(*args: str | int | Path) -> bytes:
    result = subprocess.run([sys.executable, "-m", "tests.m21_review_fixtures", *map(str, args)],
                            capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return result.stdout


def make_pdf(path: Path, pages: int, image_side: int = 0, *, annotated: bool = False) -> None:
    import pymupdf

    rng = random.Random(212)
    with pymupdf.open() as doc:
        for index in range(pages):
            page = doc.new_page(width=420, height=560)
            page.insert_text((40, 85), f"Pagina fisica {index:04d} — fixture M21.2")
            if image_side:
                samples = rng.randbytes(image_side * image_side * 3)
                pix = pymupdf.Pixmap(pymupdf.csRGB, image_side, image_side, samples, False)
                page.insert_image(pymupdf.Rect(40, 130, 380, 470), pixmap=pix)
            if annotated and index == 0:
                root = page.add_text_annot((100, 100), "Nota originaria")
                root.set_popup(pymupdf.Rect(180, 100, 350, 185))
                reply = page.add_text_annot((135, 120), "Risposta originaria")
                reply.set_irt_xref(root.xref)
                doc.xref_set_key(reply.xref, "RT", "/R")
                page.set_cropbox(pymupdf.Rect(15, 35, 395, 520))
                page.set_rotation(90)
            if annotated and index == 2:
                page.add_text_annot((100, 100), "Nota da cancellare")
        if annotated:
            doc.embfile_add("fixture.txt", b"snapshot-only attachment")
            doc.xref_set_key(doc.pdf_catalog(), "PageMode", "/UseOutlines")
            write_snapshot(doc, path)
        else:
            doc.save(path)


def pdf_state(path: Path) -> dict:
    import pymupdf

    with pymupdf.open(path) as doc:
        pages = []
        for page in doc:
            records = []
            for xref, _kind, _name in page.annot_xrefs():
                keys = {key: doc.xref_get_key(xref, key)[1] for key in
                        ("Subtype", "Contents", "NM", "IRT", "RT", "Popup", "Rect")}
                records.append({"xref": xref, **keys})
            pages.append({"text": page.get_text(), "cropbox": list(page.cropbox),
                          "rotation": page.rotation, "annotations": records})
        return {"pages": pages, "mode": doc.xref_get_key(doc.pdf_catalog(), "PageMode")[1],
                "attachments": doc.embfile_names()}


def proposal(text: str | None = None, *, operation: str = "replace", page: int = 0,
             side: str | None = None) -> dict:
    return {
        "operation": operation, "original_text": text if operation != "insert" else None,
        "context_before": None, "context_after": None, "page_index": page,
        "insert_anchor": {"text": text, "side": side} if operation == "insert" else None,
        "replacement_text": "è " if operation in ("replace", "insert") else None,
        "reason": "Motivazione della fixture.", "category": "grammar",
    }


def make_application_pdf(path: Path, *, changed: bool = False) -> None:
    import pymupdf

    with pymupdf.open() as pdf:
        page = pdf.new_page(width=360, height=430)
        page.insert_text((40, 85), "Frase da\nmodificare adesso.")
        page.insert_text((40, 150), "Elimina questa parola." if not changed else "Tieni questo periodo.")
        page.insert_text((40, 180), "Conserva Ambigua Ambigua.")
        page.insert_text((40, 205), "    return 0;", fontname="cour")
        page.add_text_annot((40, 310), "Nota estranea")
        page.add_freetext_annot(pymupdf.Rect(40, 340, 180, 370), "Frase da modificare")
        page = pdf.new_page(width=320, height=440)
        page.insert_text((185, 130), "Annota qui.")
        page.set_cropbox(pymupdf.Rect(20, 40, 300, 410))
        page.set_rotation(90)
        for index in range(2, 6):
            page = pdf.new_page(width=360, height=430)
            page.insert_text((40, 100), f"Correzione per pagina {index}.")
        pdf.save(path)


def application_pdf_state(path: Path) -> dict:
    import pymupdf

    with pymupdf.open(path) as doc:
        content, annotations, ai_reply_targets = [], [], []
        keys = ("Subtype", "T", "Contents", "IRT", "RT", "IT", "QuadPoints", "Popup")
        for page in doc:
            content.append(page.read_contents().hex())
            refs = page.annot_xrefs()
            records = {xref: {key: doc.xref_get_key(xref, key)[1] for key in keys}
                       for xref, _, _ in refs}
            roots = {f"{xref} 0 R": name for xref, _, name in refs if name}
            annotations.append({name: records[xref] for xref, _, name in refs if name})
            ai_reply_targets.append([roots[record["IRT"]] for record in records.values()
                                     if record["RT"] == "/R" and record["T"] == "Revisione IA"
                                     and record["IRT"] in roots])
        return {"content": content, "annotations": annotations, "ai_reply_targets": ai_reply_targets}


def verify_application_evidence(workspace: Path) -> None:
    from uuid import uuid4

    import pymupdf
    import pytest

    from pdf_annotator.util.paths import identify_file
    from pdf_annotator.worker.review_application import ReviewApplicationWorker
    from pdf_annotator.worker.review_snapshot import ReviewError, ReviewSnapshotStore
    from pdf_annotator.worker.text_service import WorkerTextService

    source, target = workspace / "original.pdf", workspace / "changed.pdf"
    make_application_pdf(source)
    make_application_pdf(target, changed=True)
    with pymupdf.open(source) as doc, pymupdf.open(target) as live:
        snapshots = ReviewSnapshotStore(workspace)
        snapshots.capture(doc, "first", 1, 0, identify_file(str(source)))
        engine = ReviewApplicationWorker(snapshots)
        data = {"snapshot_id": snapshots.snapshot.snapshot_id, "job_id": uuid4().hex,
                "annotation_id": uuid4().hex, "proposal": proposal("parola", operation="delete")}
        proof = engine.localize(data)
        confirmation = {**data, **proof, "command_id": uuid4().hex}
        with pytest.raises(ReviewError, match="cambiato"):
            engine.confirm(confirmation, WorkerTextService("other", 1, live), identify_file(str(target)))
        result = engine.confirm(confirmation, WorkerTextService("first", 2, live), identify_file(str(target)))
        assert result["status"] == "target_changed"
        confirmation["location"]["localization"]["quads"] = []
        with pytest.raises(ReviewError, match="non valida"):
            engine.confirm(confirmation, WorkerTextService("first", 2, live), identify_file(str(target)))


def verify_m20_input(path: Path, first: int, last: int) -> None:
    from experiments.m20.review import PageInterval, compose_prompt, request_body

    pdf = path.read_bytes()
    target = PageInterval(first, last)
    pages = PageInterval(first - 1, last + 1)
    prompt = compose_prompt("Fixture M21.2", pages, {})
    request = json.loads(request_body(pdf, target, "offline-test", "medium", 1000, prompt))
    inline = request["input"][0]["content"][-1]
    assert inline["type"] == "input_file"
    prefix, data = inline["file_data"].split(",", 1)
    assert prefix == "data:application/pdf;base64"
    assert b64decode(data, validate=True) == pdf


if __name__ == "__main__":
    path = Path(sys.argv[2])
    if sys.argv[1] == "make":
        make_pdf(path, int(sys.argv[3]), int(sys.argv[4]) if len(sys.argv) > 4 else 0, annotated=True)
    elif sys.argv[1] == "verify-m20":
        verify_m20_input(path, int(sys.argv[3]), int(sys.argv[4]))
    elif sys.argv[1] == "make-application":
        make_application_pdf(path)
    elif sys.argv[1] == "inspect-application":
        print(json.dumps(application_pdf_state(path), ensure_ascii=True))
    elif sys.argv[1] == "verify-application-evidence":
        verify_application_evidence(path)
    else:
        print(json.dumps(pdf_state(path), ensure_ascii=True))
