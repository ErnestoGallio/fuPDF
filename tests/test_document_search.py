"""M13 search: worker normalization and per-tab UI state."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from pdf_annotator.app.document_session import Lifecycle
from pdf_annotator.app.main_window import MainWindow
from pdf_annotator.util.paths import identify_file
from pdf_annotator.viewer.layout_engine import LayoutMode
from pdf_annotator.worker.client import RasterBudget, WorkerClient
from pdf_annotator.worker.document_service import DocumentSource
from pdf_annotator.worker.protocol import Operation
from .conftest import close_window_discard, request, wait_until
from .m13_fixtures import (UNICODE_MAPPING_TEXT, create_dense_search_pdf,
                            create_unicode_mapping_pdf)


def _slice(client, query: str, page: int, offset: int = 0):
    response = request(client, Operation.SEARCH_SLICE, {
        "query": query, "page_index": page, "normalized_offset": offset,
        "search_generation": 1})
    assert response.success, response.error
    return response.payload


def _m6_characters(client) -> list[list]:
    index = request(client, Operation.TEXT_PAGE_INDEX, {"page_index": 0})
    page_text = index.payload["page_text"]
    records: list[list] = []
    for descriptor in page_text["chunks"]:
        chunk = request(client, Operation.TEXT_CHUNK, {
            "page_index": 0, "page_text_id": page_text["page_text_id"],
            "chunk_index": descriptor[0]})
        records.extend(chunk.payload["chunk"]["characters"])
    return records


def test_search_normalized_offsets_map_to_original_m6_ranges_and_quads(tmp_path: Path, app):
    """Expanding ß/ﬁ must not turn normalized positions into source indices."""
    path = tmp_path / "unicode-offsets.pdf"
    source = UNICODE_MAPPING_TEXT
    create_unicode_mapping_pdf(path)
    client = WorkerClient("unicode-offsets", DocumentSource(identify_file(str(path))), RasterBudget())
    try:
        assert request(client, Operation.OPEN_DOCUMENT).success
        characters = _m6_characters(client)
        assert "".join(item[1] for item in characters) == source

        def assert_exact(query: str, original: str) -> None:
            match = next(item for item in _slice(client, query, 0)["matches"]
                         if item["preview"] == original)
            start = source.index(original)
            end = start + len(original) - 1
            assert (match["start_char_index"], match["end_char_index"]) == (start, end)
            assert match["quads"] == [item[2] for item in characters[start:end + 1]]

        # ß expands to "ss" under casefold, but owns exactly one source index/quad.
        assert_exact("STRASSE", "Straße")
        assert_exact("strasse", "Straße")
        # The compatibility ligature NFKC-normalizes to two code points.
        assert_exact("fi", "ﬁ")
        # Source-side characters immediately before and after expansion remain exact.
        assert_exact("A", "A")
        assert_exact(" B", " B")
        # A longer match crosses both length-changing source characters.
        assert_exact("A STRASSE B FI C", "A Straße B ﬁ C")
        assert_exact("CAFFÈ", "caffè")
        assert _slice(client, "caffe", 0)["matches"] == []
    finally:
        client.close_async()
        wait_until(client.stopped.is_set)


def test_worker_search_nfkc_casefold_and_accent_distinction(tmp_path: Path, app,
                                                            pdf_fixture_copy):
    path = pdf_fixture_copy(tmp_path / "search.pdf")
    client = WorkerClient("search-worker", DocumentSource(identify_file(str(path))), RasterBudget())
    try:
        assert request(client, Operation.OPEN_DOCUMENT).success
        accented = _slice(client, "CAFFÈ", 0)
        assert accented["matches"] and accented["matches"][0]["preview"] == "caffè"
        assert accented["matches"][0]["quads"]
        assert not _slice(client, "caffe", 0)["matches"]
        ligature = _slice(client, "fi", 0)
        assert any(item["preview"] == "ﬁ" for item in ligature["matches"])
        assert _slice(client, "non presente", 2)["matches"] == []
    finally:
        client.close_async()
        wait_until(client.stopped.is_set)


def test_dense_page_search_is_chunked_and_reply_bounded(tmp_path: Path, app):
    path = tmp_path / "dense-search.pdf"
    create_dense_search_pdf(path, lines=300, characters_per_line=100)
    client = WorkerClient("dense-search", DocumentSource(identify_file(str(path))), RasterBudget())
    try:
        assert request(client, Operation.OPEN_DOCUMENT).success
        index = request(client, Operation.TEXT_PAGE_INDEX, {"page_index": 0})
        assert len(index.payload["page_text"]["chunks"]) > 1
        reply = _slice(client, "xxx", 0)
        assert len(reply["matches"]) == 64
        assert reply["next_page"] == 0 and reply["normalized_offset"] > 0
        assert client.submitted_counts[Operation.RENDER] == 0
    finally:
        client.close_async()
        wait_until(client.stopped.is_set)


def _open(window: MainWindow, path: Path):
    window.open_paths([str(path)])
    wait_until(lambda: any(item.canonical_path == str(path.resolve())
                           and item.lifecycle == Lifecycle.READY
                           for item in window.manager.sessions.values()))
    session = next(item for item in window.manager.sessions.values()
                   if item.canonical_path == str(path.resolve()))
    view = window._widgets[session.document_id]
    wait_until(lambda: view.scheduler.settled)
    return session, view


def test_search_is_progressive_stale_safe_and_tab_isolated(tmp_path: Path, app,
                                                           pdf_fixture_copy):
    first_path = pdf_fixture_copy(tmp_path / "first.pdf", pages=9, dense=True)
    second_path = pdf_fixture_copy(tmp_path / "second.pdf", pages=9, dense=True)
    window = MainWindow()
    window.resize(900, 700)
    window.show()
    try:
        first, first_view = _open(window, first_path)
        rendered = first.worker.submitted_counts[Operation.RENDER]
        first_view.set_search_query("testo")
        # Replacing an in-flight query must invalidate its generation, not append it.
        first_view.set_search_query("Prima")
        wait_until(lambda: first.ui.search.complete)
        assert first.ui.search.results
        assert all("Prima" in match.preview for match in first.ui.search.results)
        assert first.worker.submitted_counts[Operation.RENDER] == rendered
        first_view.set_layout(LayoutMode.DOUBLE)
        first_view.set_zoom(1.5)
        first_view.navigate_search(True)
        assert first.ui.search.current_result == 0  # completed single match wraps

        second, second_view = _open(window, second_path)
        second_view.set_search_query("Colonna")
        wait_until(lambda: second.ui.search.complete and bool(second.ui.search.results))
        assert first.ui.search.query == "Prima"
        assert second.ui.search.query == "Colonna"
        window.manager.activate(first.document_id)
        assert first_view.search_layer.active and not second_view.search_layer.active
        assert first.ui.search.results != second.ui.search.results
        last = len(second.ui.search.results) - 1
        for _ in range(last):
            second_view.navigate_search(True)
        assert second.ui.search.current_result == last
        second_view.navigate_search(True)
        assert second.ui.search.current_result == 0
        second_view.navigate_search(False)
        assert second.ui.search.current_result == last
        window.manager.activate(second.document_id)
        window.find_action.trigger()
        assert window.find_bar.isVisible() and window.search_field.hasFocus()
        QTest.keyClick(window.search_field, Qt.Key.Key_Escape)
        assert not window.find_bar.isVisible() and not second.ui.search.query
        window.manager.activate(first.document_id)

        first_view.set_search_query("testo")
        wait_until(lambda: first_view.search_layer.pending is not None)
        worker = first.worker
        assert window.manager.request_close(first.document_id)
        wait_until(lambda: first.document_id not in window.manager.sessions)
        assert worker.stopped.is_set()
    finally:
        close_window_discard(window)
