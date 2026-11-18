"""Spawn-safe disposable export and PDF inspection for M8 integration tests."""

import hashlib
import json
from pathlib import Path
import sys

from pdf_annotator.worker.annotation_adapter import ProductAnnotationAdapter
from pdf_annotator.worker.document_service import run_document_worker


def persistence_worker(conn, document_id, source) -> None:
    output = source.identity.canonical_path + ".m8-export.pdf"

    def factory(text_service):
        adapter = ProductAnnotationAdapter(text_service)

        class ExportingAdapter:
            def __call__(self, document, command):
                return adapter(document, command)

            def history_before(self, document, command):
                return adapter.history_before(document, command)

            def history_after(self, document, before, applied):
                entry = adapter.history_after(document, before, applied)
                self._export(document)
                return entry

            def undo_history(self, document, entry):
                restored = adapter.undo_history(document, entry)
                self._export(document)
                return restored

            def redo_history(self, document, entry):
                restored = adapter.redo_history(document, entry)
                self._export(document)
                return restored

            def history_delta(self, entry, *, undo):
                return adapter.history_delta(entry, undo=undo)

            @staticmethod
            def _export(document):
                Path(output).unlink(missing_ok=True)
                # Keep worker xrefs stable: M14 history snapshots intentionally
                # reference the live native objects rather than a lossy export.
                document.save(output, garbage=0, deflate=True)

        return ExportingAdapter()

    run_document_worker(conn, document_id, source, mutation_adapter_factory=factory)


def inspect(path: Path) -> dict:
    import pymupdf

    with pymupdf.open(path) as document:
        records = []
        for page_index, page in enumerate(document):
            for annotation in page.annots() or ():
                records.append({
                    "page_index": page_index,
                    "subtype": list(document.xref_get_key(annotation.xref, "Subtype")),
                    "nm": list(document.xref_get_key(annotation.xref, "NM")),
                    "contents": list(document.xref_get_key(annotation.xref, "Contents")),
                    "rc": list(document.xref_get_key(annotation.xref, "RC")),
                    "da": list(document.xref_get_key(annotation.xref, "DA")),
                    "ds": list(document.xref_get_key(annotation.xref, "DS")),
                    "rd": list(document.xref_get_key(annotation.xref, "RD")),
                    "q": list(document.xref_get_key(annotation.xref, "Q")),
                    "it": list(document.xref_get_key(annotation.xref, "IT")),
                    "cl": list(document.xref_get_key(annotation.xref, "CL")),
                    "le": list(document.xref_get_key(annotation.xref, "LE")),
                    "rect": list(annotation.rect),
                    "colors": annotation.colors,
                    "c": list(document.xref_get_key(annotation.xref, "C")),
                    "border": annotation.border,
                    "opacity": annotation.opacity,
                    "text": annotation.info.get("content"),
                    "appearance": document.xref_stream(int(
                        document.xref_get_key(annotation.xref, "AP/N")[1].split()[0]
                    )).decode("latin1"),
                })
        contents = b"".join(document.xref_stream(xref) for item in document for xref in item.get_contents())
        return {"records": records, "page_text": document[0].get_text(),
                "content_hash": hashlib.sha256(contents).hexdigest()}


if __name__ == "__main__":
    print(json.dumps(inspect(Path(sys.argv[1])), ensure_ascii=True))
