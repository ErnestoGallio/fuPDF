from PySide6.QtTest import QTest
import pytest

from pdf_annotator.worker import document_service
from pdf_annotator.viewer.regional_render import (TileRegion, adjacent_regions,
                                                   full_page_fits, regional_raster_bytes,
                                                   regions_for_rect, tile_rect,
                                                   tile_size_for_limit)
from pdf_annotator.viewer.render_scheduler import ResidentBudget
from pdf_annotator.viewer.render_scheduler import requested_raster_scale
from pdf_annotator.worker.protocol import Operation

from .conftest import receive, wait_until
from .m2_helpers import slow_render_worker
from .test_document_lifecycle import manager, opened, pdf_files, window


def settle(view):
    wait_until(lambda: view.scheduler.settled)
    QTest.qWait(20)


def test_tile_grid_is_deterministic_contiguous_and_has_partial_edges():
    size, scale = (595.0, 842.0), 10.0
    clip = (50.0, 60.0, 170.0, 145.0)
    first = regions_for_rect(size, scale, clip, 768)
    assert first == regions_for_rect(size, scale, clip, 768)
    assert [(region.row, region.column) for region in first] == [
        (0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2),
    ]
    left, right = TileRegion(0, 0), TileRegion(1, 0)
    assert tile_rect(size, scale, left)[2] == tile_rect(size, scale, right)[0]
    edge = TileRegion(7, 10)
    assert tile_rect(size, scale, edge)[2:] == size
    assert adjacent_regions(size, scale, first, 1)
    assert adjacent_regions(size, scale, first, -1) == ()
    assert tile_size_for_limit(16 * 1024 * 1024) == 768
    assert tile_size_for_limit(256 * 1024) is None


def test_mode_selection_is_derived_from_ideal_full_page_bytes():
    size = (595.0, 842.0)
    limit = 16 * 1024 * 1024
    scale_150 = requested_raster_scale(1.5, 1.25)
    scale_300 = requested_raster_scale(3.0, 1.25)
    assert scale_150 == 2.875 and full_page_fits(size, scale_150, limit)
    assert scale_300 == 3.75 and not full_page_fits(size, scale_300, limit)
    assert full_page_fits((300.0, 400.0), scale_300, limit)


def test_worker_regional_clip_rotation_crop_and_bounds(pdf_files, manager):
    session = opened(manager, pdf_files / "geometry.pdf")
    request_id = session.worker.submit(Operation.RENDER, {
        "page_index": 5, "geometric_zoom": 8, "dpr": 1.25,
        "render_scale": 10.0, "max_bytes": 2 * 1024 * 1024,
        "clip": [0.0, 0.0, 76.8, 76.8], "tile": [0, 0, 768],
    })
    assert request_id is not None
    with receive(session.worker) as delivery:
        response = delivery.response
        assert response.success and response.payload["raster_scale"] == 10
        assert response.payload["tile"] == [0, 0, 768]
        assert response.payload["width"] == response.payload["height"] == 768
        assert response.payload["coverage"] == pytest.approx([0, 0, 76.8, 76.8])
        assert len(response.pixels) <= 2 * 1024 * 1024
    # Both float32 clip edges can expand by one transformed pixel. The
    # reservation must cover that case without weakening the worker limit.
    clip = tile_rect((600.0, 800.0), 5.0, TileRegion(0, 3))
    limit = regional_raster_bytes(clip, 5.0)
    request_id = session.worker.submit(Operation.RENDER, {
        "page_index": 0, "geometric_zoom": 4, "dpr": 1.25,
        "render_scale": 5.0, "max_bytes": limit,
        "clip": list(clip), "tile": [0, 3, 768],
    })
    assert request_id is not None
    with receive(session.worker) as delivery:
        assert delivery.response.success
        assert len(delivery.response.pixels) <= limit


@pytest.mark.parametrize("page_index,dpr", [(4, 1.25), (5, 1.5), (6, 2.0), (7, 1.25)])
def test_regional_crop_rotation_dpr_preserves_target_density_and_coordinates(
        pdf_files, window, monkeypatch, page_index, dpr):
    session = opened(window.manager, pdf_files / "geometry.pdf")
    view = window._widgets[session.document_id]
    monkeypatch.setattr(view.viewport(), "devicePixelRatioF", lambda: dpr)
    view.set_zoom(4)
    view.verticalScrollBar().setValue(round(view.page_layout.pages[page_index].y))
    settle(view)
    keys = view.scheduler.visible_region_keys(page_index)
    assert keys and all(key.scale == requested_raster_scale(4, dpr) for key in keys)
    assert all(window.manager.resident_budget.get(key, record=False) is not None for key in keys)
    mapper = view.coordinate_mapper(page_index)
    canonical = (60.0, 90.0)
    assert mapper.viewport_to_canonical(mapper.canonical_to_viewport(canonical)) == pytest.approx(canonical)


def test_high_zoom_tiles_reuse_on_scroll_return_and_layout_change(pdf_files, window):
    session = opened(window.manager, pdf_files / "geometry.pdf")
    view = window._widgets[session.document_id]
    view.set_zoom(8)
    settle(view)
    assert view.scheduler.requires_regional(0)
    initial = tuple(view.scheduler.visible_region_keys(0))
    assert initial and all(key.scale == requested_raster_scale(8, view.scheduler.dpr)
                           for key in initial)
    initial_requests = view.scheduler.requested_count
    view.verticalScrollBar().setValue(min(view.verticalScrollBar().maximum(), 700))
    settle(view)
    after_scroll = view.scheduler.requested_count
    assert after_scroll > initial_requests
    view.verticalScrollBar().setValue(0)
    settle(view)
    assert view.scheduler.requested_count == after_scroll
    keys_before_layout = set(window.manager.resident_budget.keys)
    view.set_layout("double")
    settle(view)
    assert keys_before_layout & set(window.manager.resident_budget.keys)
    assert all(not hasattr(key, "layout") for key in window.manager.resident_budget.keys)


def test_regional_tab_inactive_trim_and_document_isolation(pdf_files, window):
    first = opened(window.manager, pdf_files / "geometry.pdf")
    first_view = window._widgets[first.document_id]
    first_view.set_zoom(8)
    settle(first_view)
    assert any(key.region is not None and key.document_id == first.document_id
               for key in window.manager.resident_budget.keys)
    second = opened(window.manager, pdf_files / "alternative.pdf")
    second_view = window._widgets[second.document_id]
    second_view.set_zoom(4)
    second_view.set_layout("double")
    settle(second_view)
    assert not any(key.document_id == first.document_id
                   for key in window.manager.resident_budget.keys)
    before = first.worker.submitted_counts[Operation.RENDER]
    QTest.qWait(100)
    assert first.worker.submitted_counts[Operation.RENDER] == before
    assert all(key.document_id == second.document_id
               for key in window.manager.resident_budget.keys)
    second_worker = second.worker
    window.manager.request_close(second.document_id)
    wait_until(lambda: second_worker.stopped.is_set())
    assert not any(key.document_id == second.document_id
                   for key in window.manager.resident_budget.keys)
    first_worker = first.worker
    window.manager.request_close(first.document_id)
    wait_until(lambda: first_worker.stopped.is_set())
    assert window.manager.resident_budget.bytes == window.manager.budget.bytes == 0


def test_regional_rapid_scroll_rejects_stale_tile(pdf_files, window, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", slow_render_worker)
    session = opened(window.manager, pdf_files / "geometry.pdf")
    view = window._widgets[session.document_id]
    view.set_zoom(8)
    wait_until(lambda: view.scheduler.pending is not None
               and view.scheduler.pending.key.region is not None)
    stale_key = view.scheduler.pending.key
    view.verticalScrollBar().setValue(min(view.verticalScrollBar().maximum(), 1800))
    settle(view)
    assert stale_key not in view.scheduler.visible_region_keys(stale_key.page_index)
    assert window.manager.resident_budget.get(stale_key, record=False) is None
    assert session.worker.stale_discarded > 0 or view.scheduler.discarded_count > 0


def test_regional_eviction_remains_globally_bounded(pdf_files, window):
    window.manager.resident_budget = ResidentBudget(20 * 1024 * 1024)
    session = opened(window.manager, pdf_files / "geometry.pdf")
    view = window._widgets[session.document_id]
    view.set_zoom(8)
    maximum = view.verticalScrollBar().maximum()
    for position in range(0, maximum + 1, max(1, maximum // 8)):
        view.verticalScrollBar().setValue(position)
        settle(view)
    cache = window.manager.resident_budget
    assert cache.bytes <= cache.max_bytes
    assert cache.peak_bytes <= cache.max_bytes
    assert cache.evictions > 0
