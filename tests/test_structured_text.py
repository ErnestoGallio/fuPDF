import asyncio
from dataclasses import replace
from pathlib import Path
import json
import re
import subprocess
import sys

import pytest

from pdf_annotator.domain.structured_text import ExtractionIdentity, PageText, normalize_page_text
from pdf_annotator.services.review_worker import ReviewWorkerError
from pdf_annotator.services.structured_text_export import StructuredTextWorker, export_document
from pdf_annotator.util.paths import identify_file
from pdf_annotator.worker.client import RasterBudget, WorkerClient
from pdf_annotator.worker.document_service import DocumentSource
from pdf_annotator.worker.protocol import Operation
from .conftest import request, wait_until

@pytest.fixture(scope="module")
def source(tmp_path_factory):
    path = tmp_path_factory.mktemp("m24") / "synthetic.pdf"
    subprocess.run([sys.executable, "-m", "tests.m24_text_fixtures", str(path)],
                   check=True, capture_output=True, timeout=30)
    return path


@pytest.fixture
def client(source, app):
    instance = WorkerClient("m24-document", DocumentSource(identify_file(str(source))), RasterBudget())
    assert request(instance, Operation.OPEN_DOCUMENT).success
    yield instance
    instance.close_async()
    wait_until(instance.stopped.is_set)


def page(client, index=0):
    return asyncio.run(StructuredTextWorker(client).page(ExtractionIdentity(client.document_id, client.generation, 0), index))




def native(path):
    result = subprocess.run([sys.executable, "-m", "tests.m24_text_fixtures", "--native", str(path)],
                            check=True, capture_output=True, timeout=30, encoding="utf-8")
    return json.loads(result.stdout)


def test_native_json_uses_utf8_despite_inherited_windows_encoding(source, monkeypatch):
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    monkeypatch.setenv("PYTHONUTF8", "0")
    text = native(source)[0]
    assert "caffè Ω ﬁ 漢字" in text
    assert "Spazi   interni     conservati" in text


def flattened(text):
    return normalize_page_text(text)


def legacy_flattened(text):
    text = re.sub(r"\r\n|[\r\n\v\f\x85\u2028\u2029]", " ", text)
    return text if text.strip() else ""


def test_native_order_spaces_unicode_empty_and_annotations(client, source):
    originals = native(source)
    for index, original in enumerate(originals):
        actual = page(client, index)
        assert actual == PageText(index + 1, flattened(original))
        assert "\n" not in actual.text and "\r" not in actual.text
        assert "ANNOTATION_ONLY_SENTINEL" not in actual.text
    assert page(client, 2) == PageText(3, "")
    assert "Spazi   interni     conservati" in page(client).text
    assert "sillabazione" in page(client).text
    assert "caffè Ω ﬁ 漢字" in page(client).text
    assert page(client, 1).text.index("destra") < page(client, 1).text.index("sinistra")
    assert "pymupdf" not in sys.modules


def test_exact_export_schema_and_source_unchanged(client, source, tmp_path):
    before = source.read_bytes()
    output = tmp_path / "native.json"
    export_document(client, 0, 8, str(output), protected_paths=(str(source),))
    data = json.loads(output.read_text(encoding="utf-8"))
    assert set(data) == {"pages"}
    assert data["pages"] == [{"page": i + 1, "text": flattened(t)} for i, t in enumerate(native(source))]
    assert all(set(p) == {"page", "text"} for p in data["pages"])
    assert source.read_bytes() == before


@pytest.mark.parametrize("payload", [{"page":1,"text":"a\nb"}, {"page":1,"text":"a\rb"},
    {"page":True,"text":"ok"}, {"page":1,"text":"  "}, {"page":1,"text":"ok","hash":"extra"}])
def test_page_validator(payload):
    with pytest.raises(ValueError):
        PageText.from_payload(payload)


def test_stale_revision_and_generation(client):
    response = request(client, Operation.STRUCTURED_TEXT_PAGE, {"page_index":0,"revision":1})
    assert not response.success and response.error.code == "TEXT_STALE"
    client.generation = 2
    try:
        with pytest.raises(ReviewWorkerError, match="TEXT_STALE"):
            asyncio.run(StructuredTextWorker(client).page(ExtractionIdentity(client.document_id, 1, 0), 0))
    finally:
        client.generation = 1


def test_private_file_integrity_and_candidate_rollback(client, tmp_path, monkeypatch):
    original = StructuredTextWorker._request
    async def corrupt(self, *args, **kwargs):
        response = await original(self, *args, **kwargs)
        return replace(response, payload={**response.payload, "sha256":"0"*64})
    monkeypatch.setattr(StructuredTextWorker, "_request", corrupt)
    output = tmp_path / "preserved.json"
    output.write_text("prior output", encoding="utf-8")
    with pytest.raises(ReviewWorkerError):
        export_document(client, 0, 8, str(output))
    assert output.read_text() == "prior output" and not list(tmp_path.glob(".fupdf-text-*"))


@pytest.mark.parametrize("field,value", [("size_bytes", 64 * 1024 * 1024 + 1),
                                           ("identity", {"document_id":"other", "document_generation":1,"document_revision":0})])
def test_transport_bounds_and_response_binding(client, monkeypatch, field, value):
    original = StructuredTextWorker._request
    async def corrupt(self, *args, **kwargs):
        response = await original(self, *args, **kwargs)
        return replace(response, payload={**response.payload, field:value})
    monkeypatch.setattr(StructuredTextWorker, "_request", corrupt)
    with pytest.raises(ReviewWorkerError, match="TEXT_INVALID_PAGE"):
        page(client)


def test_crlf_and_whitespace_only_page():
    code = """
from pdf_annotator.worker import structured_text as module
class Page:
    def get_text(self, kind, **kwargs):
        assert kind == 'text' and kwargs['sort'] is False
        return self.text
class Document:
    page_count = 1
    def load_page(self, _): return page
page = Page()
module.base_textpage = lambda *args: type("TextPage", (), {})()
page.text = 'a   b\\r\\nc\\rd\\ne'
assert module.extract_page(Document(), 0).text == 'a   b c d e'
page.text = ' \\r\\n   '
assert module.extract_page(Document(), 0).text == ''
module.MAX_PATHOLOGICAL_PAGE_CHARACTERS = 2
page.text = 'too long'
try: module.extract_page(Document(), 0)
except module.ReviewError as exc: assert exc.code == 'TEXT_PAGE_TOO_LARGE'
else: raise AssertionError('limit bypassed')
"""
    subprocess.run([sys.executable, "-c", code], check=True, timeout=20)


def test_real_44_page_document(tmp_path, app):
    source = Path(r"C:\Users\matte\Desktop\MATTEO\Documenti definitivi\Due Ragni in un Boccale.pdf")
    if not source.exists():
        pytest.skip("Owner's local 44-page proof unavailable")
    originals = native(source)
    assert len(originals) == 44
    instance = WorkerClient("m24-real", DocumentSource(identify_file(str(source))), RasterBudget())
    try:
        assert request(instance, Operation.OPEN_DOCUMENT).success
        output = tmp_path / "due-ragni-m24-1b.json"
        export_document(instance, 0, 44, str(output), protected_paths=(str(source),))
        pages = json.loads(output.read_text(encoding="utf-8"))["pages"]
        assert pages == [{"page":i+1,"text":flattened(t)} for i,t in enumerate(originals)]
        expected = "queste astrazioni e di come in loro abbiamo riposto speranze deformi di onnicomprensività"
        assert expected in " ".join(pages[24]["text"].split())
        for index in range(20,24):
            assert ''.join(pages[index]['text'].split()) == ''.join(originals[index].split())
        print(f"REAL_EXPORT_BYTES={output.stat().st_size}; PATH={output}")
    finally:
        instance.close_async()
        wait_until(instance.stopped.is_set)


def test_existing_localizer_needs_no_paragraph_id_and_rejects_ambiguity(source):
    code = """
import sys
import pymupdf
from pdf_annotator.worker.review_localization import localize_document
item = dict(operation='delete', page_index=1, original_text='Testo ripetuto', context_before=None, context_after=None, insert_anchor=None)
with pymupdf.open(sys.argv[1]) as doc:
    result, = localize_document(doc, [item], base_only=True)
    assert result.status == 'ambiguous' and not result.applicable
    item['context_after'] = 'Colonna destra continua'
    result, = localize_document(doc, [item], base_only=True)
    assert result.applicable and result.quads[0][0][0] > 300
"""
    subprocess.run([sys.executable, "-c", code, str(source)], check=True, timeout=20)
