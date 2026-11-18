"""Spawn-safe M7 persistence/failure harnesses; never product save paths."""

import hashlib
import json
from pathlib import Path
import sys
from time import sleep

from pdf_annotator.domain.annotation_commands import CreateInk, MoveInk
from pdf_annotator.worker.annotation_adapter import ProductAnnotationAdapter
from pdf_annotator.worker.document_service import run_document_worker


def persistence_worker(conn, document_id, source) -> None:
    output = source.identity.canonical_path + ".m7-export.pdf"

    def factory(text_service):
        adapter = ProductAnnotationAdapter(text_service)

        def apply_and_export(document, command):
            applied = adapter(document, command)
            Path(output).unlink(missing_ok=True)
            document.save(output, garbage=3, deflate=True)
            return applied
        return apply_and_export

    run_document_worker(conn, document_id, source,
                        mutation_adapter_factory=factory)


def slow_ink_worker(conn, document_id, source) -> None:
    def factory(text_service):
        adapter = ProductAnnotationAdapter(text_service)

        def delayed(document, command):
            if isinstance(command, (CreateInk, MoveInk)):
                sleep(.4)
            return adapter(document, command)
        return delayed

    run_document_worker(conn, document_id, source,
                        mutation_adapter_factory=factory)


def _content_hash(document) -> str:
    return hashlib.sha256(b"".join(
        document.xref_stream(xref)
        for page in document
        for xref in page.get_contents()
    )).hexdigest()


def inspect(path: Path) -> dict:
    import pymupdf

    with pymupdf.open(path) as document:
        pages = []
        for page in document:
            records = []
            for annotation in page.annots() or ():
                vertices = annotation.vertices or []
                records.append({
                    "subtype": list(document.xref_get_key(annotation.xref, "Subtype")),
                    "ink_list": list(document.xref_get_key(annotation.xref, "InkList")),
                    "nm": list(document.xref_get_key(annotation.xref, "NM")),
                    "vertices": [[[point[0], point[1]] for point in path]
                                 for path in vertices],
                    "colors": annotation.colors,
                    "opacity": annotation.opacity,
                    "border": annotation.border,
                    "xref": annotation.xref,
                })
            pages.append(records)
        return {
            "page_count": document.page_count,
            "pages": pages,
            "content_hash": _content_hash(document),
            "text": "".join(page.get_text() for page in document),
        }


if __name__ == "__main__":
    print(json.dumps(inspect(Path(sys.argv[1])), ensure_ascii=True))
