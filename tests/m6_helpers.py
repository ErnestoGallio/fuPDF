"""Spawn-safe M6 persistence harness; not reachable from the product UI."""

import json
from pathlib import Path
import sys
from time import sleep
from types import SimpleNamespace

from pdf_annotator.worker.document_service import run_document_worker
from pdf_annotator.worker.annotation_adapter import ProductAnnotationAdapter


def persistence_worker(conn, document_id, source) -> None:
    output = source.identity.canonical_path + ".m6-export.pdf"

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


def slow_text_worker(conn, document_id, source) -> None:
    from pdf_annotator.worker.text_service import WorkerTextService

    original = WorkerTextService._extract

    def delayed(self, page_index, **kwargs):
        sleep(.35)
        return original(self, page_index, **kwargs)

    WorkerTextService._extract = delayed
    run_document_worker(conn, document_id, source)


def failing_text_worker(conn, document_id, source) -> None:
    from pdf_annotator.domain.errors import DomainValidationError, MutationErrorCode
    from pdf_annotator.worker.text_service import WorkerTextService

    def fail(self, page_index, **_kwargs):
        raise DomainValidationError(
            MutationErrorCode.TEXT_PAGE_TOO_COMPLEX,
            validator="test", field="page_text", reason="pathological_fixture",
            value_category="bounded_failure", page_index=page_index)

    WorkerTextService._extract = fail
    run_document_worker(conn, document_id, source)


def validate_export(path: Path) -> dict:
    import pymupdf

    with pymupdf.open(path) as document:
        page = document[0]
        annotations = list(page.annots() or ())
        annotation = annotations[0]
        subtype = document.xref_get_key(annotation.xref, "Subtype")
        quadpoints = document.xref_get_key(annotation.xref, "QuadPoints")
        nm = document.xref_get_key(annotation.xref, "NM")
        pages = []
        for current in document:
            current_annotations = list(current.annots() or ())
            pages.append([{
                "subtype": list(document.xref_get_key(item.xref, "Subtype")),
                "quadpoints": list(document.xref_get_key(item.xref, "QuadPoints")),
                "nm": list(document.xref_get_key(item.xref, "NM")),
            } for item in current_annotations])
        return {
            "count": len(annotations),
            "subtype": list(subtype),
            "quadpoints": list(quadpoints),
            "nm": list(nm),
            "colors": annotation.colors,
            "opacity": annotation.opacity,
            "text": page.get_text(),
            "xref": annotation.xref,
            "drawings": len(page.get_drawings()),
            "pages": pages,
        }


def degenerate_snapshot_probe() -> dict:
    import pymupdf

    from pdf_annotator.worker.text_service import WorkerTextService

    records = (
        {"c": "X", "bbox": (10, 10, 10, 20)},
        {"c": "A", "bbox": (12, 10, 20, 20)},
        {"c": "B", "bbox": (20, 10, 28, 20)},
    )
    raw = {"blocks": [{"type": 0, "lines": [{
        "dir": (1.0, 0.0), "spans": [{"chars": records}],
    }]}]}

    class FakeTextPage:
        def extractText(self, *, sort=False):
            return "XAB"

        def extractRAWDICT(self, *, sort=False):
            assert sort is False
            return raw

    class FakePage:
        rect = SimpleNamespace(width=100.0, height=100.0)
        rotation = 0

        def get_textpage(self, **kwargs):
            assert kwargs["flags"] == (pymupdf.TEXTFLAGS_RAWDICT
                                        & ~pymupdf.TEXT_PRESERVE_IMAGES)
            return FakeTextPage()

    document = SimpleNamespace(page_count=1,
                               load_page=lambda page_index: FakePage())

    def recover(_direction, _span, record):
        x0, y0, x1, y1 = record["bbox"]
        return SimpleNamespace(ul=SimpleNamespace(x=x0, y=y0),
                               ur=SimpleNamespace(x=x1, y=y0),
                               ll=SimpleNamespace(x=x0, y=y1),
                               lr=SimpleNamespace(x=x1, y=y1))

    original = pymupdf.recover_char_quad
    pymupdf.recover_char_quad = recover
    try:
        service = WorkerTextService("degenerate", 1, document)
        index = service.page_index(0)
        chunk = service.chunk(0, index.page_text_id, 0)
    finally:
        pymupdf.recover_char_quad = original
    return {"text": "".join(char.text for char in chunk.characters),
            "lines": index.line_count}


def coalesced_snapshot_probe() -> dict:
    import pymupdf

    from pdf_annotator.worker.text_service import WorkerTextService

    records = (
        {"c": "A", "bbox": (10, 10, 18, 20)},
        {"c": "X", "bbox": (18, 10, 18, 20)},
        {"c": "B", "bbox": (18, 10, 26, 20)},
    )
    raw = {"blocks": [{"type": 0, "lines": [{
        "dir": (1.0, 0.0), "spans": [{"chars": records}],
    }]}]}

    class FakeTextPage:
        def extractText(self, *, sort=False):
            return "AXB"

        def extractRAWDICT(self, *, sort=False):
            return raw

    class FakePage:
        rect = SimpleNamespace(width=100.0, height=100.0)
        rotation = 0

        def get_textpage(self, **_kwargs):
            return FakeTextPage()

    document = SimpleNamespace(page_count=1, load_page=lambda _index: FakePage())

    def recover(_direction, _span, record):
        x0, y0, x1, y1 = record["bbox"]
        return SimpleNamespace(ul=SimpleNamespace(x=x0, y=y0),
                               ur=SimpleNamespace(x=x1, y=y0),
                               ll=SimpleNamespace(x=x0, y=y1),
                               lr=SimpleNamespace(x=x1, y=y1))

    original = pymupdf.recover_char_quad
    pymupdf.recover_char_quad = recover
    try:
        service = WorkerTextService("coalesced", 1, document)
        index = service.page_index(0)
        chunk = service.chunk(0, index.page_text_id, 0)
    finally:
        pymupdf.recover_char_quad = original
    return {"text": "".join(char.text for char in chunk.characters),
            "lines": index.line_count}


def pathological_snapshot_probe() -> dict:
    import pymupdf

    from pdf_annotator.domain.errors import DomainValidationError
    from pdf_annotator.worker import text_service as module

    records = tuple({"c": "x", "bbox": (10 + index % 50, 10,
                                           11 + index % 50, 20)}
                    for index in range(101))
    raw = {"blocks": [{"type": 0, "lines": [{
        "dir": (1.0, 0.0), "spans": [{"chars": records}],
    }]}]}

    class FakeTextPage:
        def extractText(self, *, sort=False):
            return "x" * 101

        def extractRAWDICT(self, *, sort=False):
            return raw

    class FakePage:
        rect = SimpleNamespace(width=100.0, height=100.0)
        rotation = 0

        def get_textpage(self, **_kwargs):
            return FakeTextPage()

    document = SimpleNamespace(page_count=1, load_page=lambda _index: FakePage())

    def recover(_direction, _span, record):
        x0, y0, x1, y1 = record["bbox"]
        return SimpleNamespace(ul=SimpleNamespace(x=x0, y=y0),
                               ur=SimpleNamespace(x=x1, y=y0),
                               ll=SimpleNamespace(x=x0, y=y1),
                               lr=SimpleNamespace(x=x1, y=y1))

    original_recover = pymupdf.recover_char_quad
    original_limit = module.MAX_PATHOLOGICAL_PAGE_CHARACTERS
    pymupdf.recover_char_quad = recover
    module.MAX_PATHOLOGICAL_PAGE_CHARACTERS = 100
    service = module.WorkerTextService("pathological", 1, document)
    try:
        service.page_index(0)
    except DomainValidationError as exc:
        result = {"code": exc.code.value, "reason": exc.reason,
                  "cache_bytes": service.cache.bytes}
    else:
        raise AssertionError("Pathological page unexpectedly succeeded")
    finally:
        module.MAX_PATHOLOGICAL_PAGE_CHARACTERS = original_limit
        pymupdf.recover_char_quad = original_recover
    return result


if __name__ == "__main__":
    report = (degenerate_snapshot_probe() if sys.argv[1] == "--degenerate-snapshot"
              else coalesced_snapshot_probe() if sys.argv[1] == "--coalesced-snapshot"
              else pathological_snapshot_probe() if sys.argv[1] == "--pathological-snapshot"
              else validate_export(Path(sys.argv[1])))
    print(json.dumps(report, ensure_ascii=True))
