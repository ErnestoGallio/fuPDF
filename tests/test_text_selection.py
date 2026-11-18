from pathlib import Path
import json
import subprocess
import sys
from uuid import uuid4

import pytest

from pdf_annotator.domain.geometry import PdfPoint, PdfQuad, PdfRect
from pdf_annotator.domain.serialization import (
    text_chunk_from_payload, text_chunk_to_payload,
    text_page_index_from_payload, text_page_index_to_payload)
from pdf_annotator.domain.text import (
    MAX_PATHOLOGICAL_PAGE_CHARACTERS, MAX_TEXT_CHUNK_WIRE_BYTES,
    TextCharacter, TextChunk, TextChunkDescriptor, TextLine, TextPageIndex,
    hit_test, resolve_chunk_indices)
from pdf_annotator.services.text_cache import TextSnapshotCache, text_object_cost
from pdf_annotator.util.paths import identify_file
from pdf_annotator.worker.client import RasterBudget, WorkerClient
from pdf_annotator.worker.document_service import DocumentSource
from pdf_annotator.worker.protocol import Operation
from .conftest import request, wait_until
from .m6_fixtures import quad_center


def _quad(x: float, y: float, width: float = 8, height: float = 10) -> PdfQuad:
    return PdfQuad(PdfPoint(x, y), PdfPoint(x + width, y),
                   PdfPoint(x, y + height), PdfPoint(x + width, y + height))


def _chunk(document_id: str = "doc", page: int = 0, chunk_index: int = 0,
           start: int = 0, text: str = "abc def") -> TextChunk:
    characters = tuple(TextCharacter(start + index, value, _quad(10 + index * 9, 10),
                                     0, 0) for index, value in enumerate(text))
    return TextChunk(document_id, 1, page, uuid4().hex, chunk_index,
                     uuid4().hex, characters,
                     (TextLine(0, 0, start, start + len(characters), (1, 0)),))


def _index(chunk: TextChunk) -> TextPageIndex:
    descriptor = TextChunkDescriptor(
        chunk.chunk_index, chunk.chunk_id, chunk.start_char_index,
        chunk.end_char_index, chunk.lines[0].line_index, chunk.lines[-1].line_index,
        PdfRect(10, 10, 10 + len(chunk.characters) * 9, 20), 1024)
    return TextPageIndex(chunk.document_id, chunk.document_generation, chunk.page_index,
                         chunk.page_text_id, len(chunk.characters), 1, (descriptor,))


def _page(client, page_index=0):
    reply = request(client, Operation.TEXT_PAGE_INDEX, {"page_index": page_index})
    assert reply.success, reply.error
    return reply, reply.payload["page_text"]


def _chunk_payload(client, page_text, chunk_index):
    reply = request(client, Operation.TEXT_CHUNK, {
        "page_index": page_text["page_index"],
        "page_text_id": page_text["page_text_id"],
        "chunk_index": chunk_index})
    assert reply.success, reply.error
    return reply, reply.payload["chunk"]


def _all_records(client, page_text):
    chunks = [_chunk_payload(client, page_text, index)[1]
              for index in range(len(page_text["chunks"]))]
    return chunks, [record for chunk in chunks for record in chunk["characters"]]


def test_chunk_and_page_index_wire_roundtrip_and_reverse_selection():
    chunk = _chunk()
    index = _index(chunk)
    assert text_chunk_from_payload(text_chunk_to_payload(chunk)) == chunk
    assert text_page_index_from_payload(text_page_index_to_payload(index)) == index
    selection = resolve_chunk_indices(chunk, 6, 0)
    assert selection.text == "abc def"
    assert selection.anchor_index == 6 and selection.focus_index == 0
    assert hit_test(chunk, PdfPoint(10, 10)) == 0
    assert hit_test(chunk, PdfPoint(1, 1)) is None


def test_text_cache_is_byte_bounded_lru_and_document_isolated():
    first, second = _chunk("first"), _chunk("second")
    cost = text_object_cost(first)
    cache = TextSnapshotCache(cost + 64)
    assert cache.put(first)
    assert cache.put(second)
    assert cache.bytes <= cache.max_bytes
    assert cache.keys == (("second", 1, 0, second.page_text_id, 0),)
    assert cache.evictions == 1
    cache.remove_document("second")
    assert cache.entry_count == 0 and cache.bytes == 0


@pytest.fixture
def text_client(tmp_path: Path, app, pdf_fixture_copy):
    path = tmp_path / "m6-text.pdf"
    pdf_fixture_copy(path)
    client = WorkerClient("m6-doc", DocumentSource(identify_file(str(path))), RasterBudget())
    yield client
    client.close_async()
    wait_until(client.stopped.is_set)


def test_worker_extracts_unicode_lazily_and_image_page_is_empty(text_client):
    assert request(text_client, Operation.OPEN_DOCUMENT).success
    assert text_client.submitted_counts[Operation.TEXT_PAGE_INDEX] == 0
    first, page = _page(text_client, 0)
    chunks, records = _all_records(text_client, page)
    extracted = "".join(record[1] for record in records)
    assert "caffè l’apostrofo “virgolette” € Ω" in extracted
    assert "ﬁ 漢字" in extracted
    assert first.payload["metrics"]["extractions"] == 1
    cached, same_page = _page(text_client, 0)
    assert same_page["page_text_id"] == page["page_text_id"]
    assert cached.payload["metrics"]["cache_hits"] >= 1
    _, image_page = _page(text_client, 2)
    assert image_page["character_count"] == 0 and image_page["chunks"] == []
    assert chunks


def test_worker_confirms_forward_reverse_multiline_and_unicode(text_client):
    assert request(text_client, Operation.OPEN_DOCUMENT).success
    _, page = _page(text_client, 0)
    _, records = _all_records(text_client, page)
    flattened = "".join(record[1] for record in records)
    offsets = [index for index, record in enumerate(records) for _ in record[1]]
    for expected in ("Prima", " riga ", "punteggiatura.", "caffè", "ﬁ"):
        start_offset = flattened.index(expected)
        start, end = offsets[start_offset], offsets[start_offset + len(expected) - 1]
        payload = {"page_index": 0, "page_text_id": page["page_text_id"],
                   "anchor": quad_center(records[start][2]),
                   "focus": quad_center(records[end][2])}
        forward = request(text_client, Operation.TEXT_SELECTION, payload)
        reverse = request(text_client, Operation.TEXT_SELECTION, {
            **payload, "anchor": payload["focus"], "focus": payload["anchor"]})
        assert forward.success and reverse.success
        assert forward.payload["target"]["original_text"] == expected
        assert reverse.payload["target"]["original_text"] == expected
    multiline = request(text_client, Operation.TEXT_SELECTION, {
        "page_index": 0, "page_text_id": page["page_text_id"],
        "anchor": quad_center(records[0][2]), "focus": quad_center(records[50][2])})
    assert multiline.success and len(multiline.payload["target"]["quads"]) == 2


def test_release_with_no_character_hit_and_stale_identity_are_controlled(text_client):
    assert request(text_client, Operation.OPEN_DOCUMENT).success
    _, page = _page(text_client, 0)
    no_selection = request(text_client, Operation.TEXT_SELECTION, {
        "page_index": 0, "page_text_id": page["page_text_id"],
        "anchor": [590.0, 790.0], "focus": [590.0, 790.0]})
    assert no_selection.success
    assert no_selection.payload == {"status": "no_selection", "target": None}
    stale = request(text_client, Operation.TEXT_SELECTION, {
        "page_index": 0, "page_text_id": "stale-page-text",
        "anchor": [10.0, 10.0], "focus": [10.0, 10.0]})
    assert stale.success and stale.payload == {"status": "stale", "target": None}


@pytest.mark.parametrize("characters", [8_000, 10_000, 16_000, 30_000])
def test_dense_valid_pages_are_chunked_selectable_and_bounded(
        tmp_path, app, characters):
    path = tmp_path / f"dense-{characters}.pdf"
    lines, per_line = characters // 100, 100
    subprocess.run([sys.executable, "-m", "tests.m6_fixtures", "limit",
                    str(path), str(lines), str(per_line)], check=True, timeout=30,
                   capture_output=True)
    client = WorkerClient(f"dense-{characters}",
                          DocumentSource(identify_file(str(path))), RasterBudget())
    try:
        assert request(client, Operation.OPEN_DOCUMENT).success
        page_reply, page = _page(client)
        assert page["character_count"] == characters
        assert len(page["chunks"]) >= 2
        assert max(record[7] for record in page["chunks"]) <= MAX_TEXT_CHUNK_WIRE_BYTES
        first_chunk = _chunk_payload(client, page, 0)[1]
        second_chunk = _chunk_payload(client, page, 1)[1]
        selected = request(client, Operation.TEXT_SELECTION, {
            "page_index": 0, "page_text_id": page["page_text_id"],
            "anchor": quad_center(first_chunk["characters"][-10][2]),
            "focus": quad_center(second_chunk["characters"][10][2])})
        assert selected.success and selected.payload["status"] == "confirmed"
        assert "\n" in selected.payload["target"]["original_text"]
        assert page_reply.payload["metrics"]["cache_bytes"] <= 16 * 1024 * 1024
        assert _chunk_payload(client, page, 0)[0].payload["metrics"]["cache_bytes"] <= 16 * 1024 * 1024
    finally:
        client.close_async()
        wait_until(client.stopped.is_set)


def test_cache_eviction_does_not_stale_same_generation_page_identity(tmp_path, app):
    path = tmp_path / "dense-reload.pdf"
    subprocess.run([sys.executable, "-m", "tests.m6_fixtures", "limit",
                    str(path), "160", "100"], check=True, timeout=30,
                   capture_output=True)
    client = WorkerClient("dense-reload", DocumentSource(identify_file(str(path))),
                          RasterBudget())
    try:
        assert request(client, Operation.OPEN_DOCUMENT).success
        _, page = _page(client)
        _, first = _chunk_payload(client, page, 0)
        _, second = _chunk_payload(client, page, 1)
        assert request(client, Operation.TEXT_TRIM).success
        selected = request(client, Operation.TEXT_SELECTION, {
            "page_index": 0, "page_text_id": page["page_text_id"],
            "anchor": quad_center(first["characters"][-5][2]),
            "focus": quad_center(second["characters"][5][2])})
        assert selected.success and selected.payload["status"] == "confirmed"
        assert selected.payload["target"]["snapshot_id"] == page["page_text_id"]
    finally:
        client.close_async()
        wait_until(client.stopped.is_set)


def test_two_column_range_confined_to_left_block_does_not_jump_right(text_client):
    assert request(text_client, Operation.OPEN_DOCUMENT).success
    _, page = _page(text_client, 1)
    _, records = _all_records(text_client, page)
    left = [record for record in records if record[3] == 0]
    selected = request(text_client, Operation.TEXT_SELECTION, {
        "page_index": 1, "page_text_id": page["page_text_id"],
        "anchor": quad_center(left[0][2]), "focus": quad_center(left[-1][2])})
    assert selected.success
    text = selected.payload["target"]["original_text"]
    assert "Colonna sinistra" in text and "destra" not in text


def test_rotations_and_cropbox_keep_canonical_character_quads(tmp_path, app,
                                                               pdf_fixture_copy):
    path = tmp_path / "geometry-text.pdf"
    pdf_fixture_copy(path, geometry=True)
    client = WorkerClient("geometry-text", DocumentSource(identify_file(str(path))),
                          RasterBudget())
    try:
        assert request(client, Operation.OPEN_DOCUMENT).success
        geometries = request(client, Operation.PAGE_GEOMETRY, {"start": 0}).payload["pages"]
        for page_index, geometry in enumerate(geometries):
            _, page = _page(client, page_index)
            _, records = _all_records(client, page)
            first_quad = records[0][2]
            xs, ys = ([point[0] for point in first_quad],
                      [point[1] for point in first_quad])
            assert 45 <= min(xs) <= 55 and 60 <= min(ys) <= 85
            assert max(xs) <= geometry["width_pt"] and max(ys) <= geometry["height_pt"]
            selected = request(client, Operation.TEXT_SELECTION, {
                "page_index": page_index, "page_text_id": page["page_text_id"],
                "anchor": quad_center(first_quad), "focus": quad_center(first_quad)})
            assert selected.success
    finally:
        client.close_async()
        wait_until(client.stopped.is_set)


def test_pathological_safety_cap_is_far_above_dense_product_cases():
    assert MAX_PATHOLOGICAL_PAGE_CHARACTERS >= 8 * 30_000
    completed = subprocess.run(
        [sys.executable, "-m", "tests.m6_helpers", "--pathological-snapshot"],
        cwd=Path(__file__).parents[1], check=True, capture_output=True,
        text=True, timeout=30)
    assert json.loads(completed.stdout) == {
        "code": "text_page_too_complex",
        "reason": "pathological_character_safety_limit",
        "cache_bytes": 0,
    }


def test_one_degenerate_raw_character_does_not_invalidate_page_chunks():
    completed = subprocess.run(
        [sys.executable, "-m", "tests.m6_helpers", "--degenerate-snapshot"],
        cwd=Path(__file__).parents[1], check=True, capture_output=True,
        text=True, timeout=30)
    assert json.loads(completed.stdout) == {"text": "AB", "lines": 1}
    assert "filtered_significant_chars" in completed.stderr


def test_successful_zero_width_coalescing_is_not_logged_as_an_error():
    completed = subprocess.run(
        [sys.executable, "-m", "tests.m6_helpers", "--coalesced-snapshot"],
        cwd=Path(__file__).parents[1], check=True, capture_output=True,
        text=True, timeout=30)
    assert json.loads(completed.stdout) == {"text": "AXB", "lines": 1}
    assert "filtered_significant_chars" not in completed.stderr
