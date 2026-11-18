"""Isolated M15 fixture, inspection and owner-only failure seams."""

import json
from pathlib import Path
import sys


def failure_worker(conn, document_id, source, *, phase):
    from pdf_annotator.worker import save_service
    from pdf_annotator.worker.document_service import run_document_worker
    from pdf_annotator.worker.save_validation import validate_candidate
    import pymupdf

    def fail(*args, **kwargs):
        raise OSError("Injected save failure")

    def corrupt(candidate, expected):
        Path(candidate).write_bytes(b"invalid candidate")
        return validate_candidate(candidate, expected)

    def corrupt_appearance(candidate, expected):
        with pymupdf.open(candidate) as doc:
            xref = doc[0].annot_xrefs()[0][0]
            if phase == "appearance":
                ap = int(doc.xref_get_key(xref, "AP/N")[1].split()[0])
                doc.update_stream(ap, b"q Q")
            else:
                doc.xref_set_key(xref, "Contents", pymupdf.get_pdf_str("lost content"))
            doc.saveIncr()
        return validate_candidate(candidate, expected)

    def crash_validator(candidate, expected):
        from pdf_annotator.worker import save_validation
        original = save_validation.validator_main
        save_validation.validator_main = exit_validator
        try:
            return validate_candidate(candidate, expected, timeout=3)
        finally:
            save_validation.validator_main = original

    seam = ({"write": fail} if phase == "write" else
            {"validate": corrupt} if phase == "validator" else
            {"validate": corrupt_appearance} if phase in ("content", "appearance") else
            {"validate": crash_validator} if phase == "validator_crash" else
            {"replace": fail} if phase == "replace" else
            {"reopen": fail})
    original_save = save_service.safe_save

    def fail_once(*args, **kwargs):
        save_service.safe_save = original_save
        return original_save(*args, **kwargs, **seam)

    save_service.safe_save = fail_once
    run_document_worker(conn, document_id, source)


def exit_validator(conn, candidate, expected):
    import os
    os._exit(73)


def inspect(path: Path) -> dict:
    import pymupdf
    from pdf_annotator.worker.save_validation import base_page_text, inspect_document
    from hashlib import sha256

    with pymupdf.open(path) as doc:
        return {"checks": inspect_document(doc),
                "text": [base_page_text(page) for page in doc],
                "content": [[sha256(doc.xref_stream(xref)).hexdigest()
                             for xref in page.get_contents()] for page in doc],
                "annots": [[dict(nm=doc.xref_get_key(xref, "NM")[1],
                                  subtype=doc.xref_get_key(xref, "Subtype")[1],
                                  author=doc.xref_get_key(xref, "T"),
                                  text=doc.xref_get_key(xref, "Contents")[1],
                                  ap=doc.xref_get_key(xref, "AP"),
                                  intent=doc.xref_get_key(xref, "IT")[1])
                             for xref, _, _ in page.annot_xrefs()] for page in doc]}


def inspect_freetext(path: Path) -> list[dict[str, str]]:
    """Inspect saved FreeText data only in the ``python -m`` helper process."""
    import pymupdf

    with pymupdf.open(path) as doc:
        records = []
        for page in doc:
            annot = page.first_annot
            while annot is not None:
                records.append({"type": annot.type[1], "content": annot.info["content"]})
                annot = annot.next
        return records


def unknown_fixture(path: Path):
    import pymupdf
    from tests.m12_fixtures import appearance

    appearance(path)
    with pymupdf.open(path) as doc:
        page = doc[0]
        page.insert_text((20, 360), "alpha beta gamma")
        note = page.add_text_annot((20, 30), "città 日本語 😀 (12 0 R)")
        doc.xref_set_key(note.xref, "Subtype", "/FutureUnknown")
        doc.xref_set_key(note.xref, "PrivateData", "<< /Label (123 0 R) /Values [3 2 1] >>")
        doc.saveIncr()


def validation_contract(path: Path):
    import pymupdf
    from pdf_annotator.worker.save_validation import semantic_digest

    with pymupdf.open() as doc:
        page = doc.new_page()
        dead = doc.get_new_xref()
        doc.update_object(dead, "<< /Unused (unused) >>")
        page.insert_text((30, 60), "base")
        page.add_text_annot((30, 100), "Unicode città 日本語 (123 0 R)")
        before = semantic_digest(doc)
        doc.save(path, garbage=2)
    with pymupdf.open(path) as doc:
        assert semantic_digest(doc) == before, "Semantic check depended on xref numbers"
        annotation = doc[0].annot_xrefs()[0][0]
        doc.xref_set_key(annotation, "AP", "null")
        assert semantic_digest(doc) != before, "Missing appearance was not detected"


def gate_source(path: Path) -> None:
    """Create the blank disposable source for the manual Acrobat gate only."""
    import pymupdf

    with pymupdf.open() as doc:
        page = doc.new_page(width=595, height=842)
        page.insert_text((36, 36), "M15 Acrobat FreeText / Callout gate", fontsize=10)
        doc.save(path)


if __name__ == "__main__":
    if sys.argv[1] == "inspect":
        print(json.dumps(inspect(Path(sys.argv[2])), ensure_ascii=True))
    elif sys.argv[1] == "inspect-freetext":
        print(json.dumps(inspect_freetext(Path(sys.argv[2])), ensure_ascii=True))
    elif sys.argv[1] == "unknown":
        unknown_fixture(Path(sys.argv[2]))
    elif sys.argv[1] == "validation-contract":
        validation_contract(Path(sys.argv[2]))
    elif sys.argv[1] == "gate-source":
        gate_source(Path(sys.argv[2]))
