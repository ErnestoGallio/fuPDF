from dataclasses import replace
from math import floor
import os
import signal
import sys
from time import monotonic

from PySide6.QtCore import QPointF, QRectF, QTimer, Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
import pytest

from pdf_annotator.app.document_session import Lifecycle
from pdf_annotator.viewer.coordinate_mapper import PageGeometry
from pdf_annotator.viewer.layout_engine import (LayoutMode, PAGE_COLUMN_GAP, PAGE_GAP, PAGE_MARGIN,
                                                PAGE_ROW_GAP, PageLayout, SingleLayout)
from pdf_annotator.viewer.pdf_view import PdfView
from pdf_annotator.viewer.render_scheduler import ResidentBudget
from pdf_annotator.worker import document_service
from pdf_annotator.worker.client import WorkerClient
from pdf_annotator.worker.protocol import (Operation, Response, VIEWER_SINGLE_RASTER_BYTES,
                                          MAX_SINGLE_RASTER_BYTES, decode, encode)
from .conftest import receive, wait_until
from .m2_helpers import slow_render_worker
from .test_document_lifecycle import manager, opened, pdf_files, window


def settled(view):
    # M3 may show a scaled preview while the final key is still pending.
    wait_until(lambda: view.scheduler.settled)


def goto(view, page):
    view.verticalScrollBar().setValue(int(view.page_layout.pages[page].y))


def observe_renders(monkeypatch):
    calls = []
    original = WorkerClient.submit

    def submit(client, operation, payload=None):
        request_id = original(client, operation, payload)
        if operation == Operation.RENDER and request_id is not None:
            calls.append((client.document_id, request_id, payload.copy()))
        return request_id

    monkeypatch.setattr(WorkerClient, "submit", submit)
    return calls


def test_single_layout_mixed_rotation_and_strict_viewport_intersection():
    pages = [PageGeometry(0, 600, 800, 0, (0, 0, 600, 800), (0, 0, 600, 800)),
             PageGeometry(1, 500, 660, 90, (50, 70, 550, 730), (0, 0, 600, 800)),
             PageGeometry(2, 320, 400, 0, (0, 0, 320, 400), (0, 0, 320, 400))]
    layout = SingleLayout(pages)
    assert [(p.width, p.height) for p in layout.pages] == [(600, 800), (660, 500), (320, 400)]
    assert [p.y for p in layout.pages] == [PAGE_MARGIN,
                                           PAGE_MARGIN + 800 + PAGE_ROW_GAP,
                                           PAGE_MARGIN + 800 + PAGE_ROW_GAP + 500 + PAGE_ROW_GAP]
    assert layout.width == 700 and layout.height == layout.pages[-1].y + layout.pages[-1].height + PAGE_MARGIN
    assert all(p.x + p.width / 2 == 350 for p in layout.pages)
    assert layout.visible(0, 822, 700, 4) == ()  # just the reduced gap, not either page
    assert layout.nearby((), 822, 4) == (1, 0)
    assert layout.visible(0, 810, 700, 50) == (0, 1)
    assert layout.nearby((0, 1), 810, 50) == (2,)
    assert layout.visible(700, 0, 100, 1772) == ()


def test_unrepresentable_scroll_geometry_has_controlled_cleanup(pdf_files, window, monkeypatch):
    from pdf_annotator.viewer import pdf_view

    def oversized(pages, *args, **kwargs):
        return SingleLayout([replace(pages[0], height_pt=2**31)])

    monkeypatch.setattr(pdf_view, "PageLayout", oversized)
    window.open_paths([str(pdf_files / "normal.pdf")])
    wait_until(lambda: "limiti del viewer" in window.statusBar().currentMessage()
               and not window.manager.sessions)
    assert window.tabs.count() == 0
    assert window.manager.workers_started == 1 and window.manager.budget.bytes == 0


def test_long_metadata_open_zero_rasters_until_viewer_activation(pdf_files, manager):
    session = opened(manager, pdf_files / "long.pdf")
    assert session.worker.submitted_counts[Operation.RENDER] == 0
    view = PdfView(session, manager.resident_budget)
    view.resize(760, 600)
    view.show()
    manager.session_tick.connect(lambda doc: view.tick() if doc == session.document_id else None)
    try:
        QTest.qWait(50)
        assert not view.scheduler.rasters
        assert session.worker.submitted_counts[Operation.RENDER] == 0
        assert len(view.page_layout.pages) == 300
        assert len(view.findChildren(PdfView)) == 0
        initial_layout = view.page_layout.pages
        height, scroll_max = view.page_layout.height, view.verticalScrollBar().maximum()
        view.set_active(True)
        settled(view)
        assert set(view.scheduler.rasters) == set(view.scheduler.targets) == {0, 1}
        assert session.worker.submitted_counts[Operation.RENDER] == 2
        image = view.scheduler.rasters[0].image
        assert image.format() == image.Format.Format_RGB888
        scale = view.scheduler.rasters[0].raster_scale
        color = image.pixelColor(round(110 * scale), round(160 * scale))
        assert (color.red(), color.green(), color.blue()) == pytest.approx((42, 51, 76), abs=2)
        del image
        assert view.page_layout.pages == initial_layout
        assert view.page_layout.height == height and view.verticalScrollBar().maximum() == scroll_max
        assert view.verticalScrollBar().value() == 0
        assert "pymupdf" not in sys.modules
    finally:
        view.set_active(False)
        view.close()
        view.deleteLater()


def test_rapid_traversal_and_distant_jump_do_not_queue_the_path(pdf_files, window, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", slow_render_worker)
    calls = observe_renders(monkeypatch)
    session = opened(window.manager, pdf_files / "long.pdf")
    view = window._widgets[session.document_id]
    wait_until(lambda: view.scheduler.pending is not None and session.worker.active_request_id is not None)
    initial_adoptions = view.scheduler.adopted_count
    beats = []
    timer = QTimer()
    timer.timeout.connect(lambda: beats.append(monotonic()))
    timer.start(5)
    for index in range(1, 250):
        goto(view, index)
    position = view.verticalScrollBar().value()
    bounds = view.page_layout.height, view.verticalScrollBar().maximum()
    settled(view)
    timer.stop()
    assert len(beats) >= 5
    assert session.worker.stale_discarded > 0
    assert set(view.scheduler.rasters) == {248, 249, 250}
    assert view.scheduler.adopted_count - initial_adoptions == 3
    assert len(calls) <= 4
    assert calls[-3][2]["page_index"] == 249  # visible before nearby
    assert bounds == (view.page_layout.height, view.verticalScrollBar().maximum())
    assert view.verticalScrollBar().value() == position
    assert view.session.ui.reading_anchor.page_index == 249
    assert session.worker.pending_count == 0


def test_slow_scroll_keyboard_and_all_the_way_to_last_page(pdf_files, window):
    session = opened(window.manager, pdf_files / "long.pdf")
    view = window._widgets[session.document_id]
    settled(view)
    view.setFocus()
    QTest.keyClick(view, Qt.Key.Key_PageDown)
    assert 0 < view.verticalScrollBar().value() < 800
    QTest.keyClick(view, Qt.Key.Key_PageUp)
    assert view.verticalScrollBar().value() == 0
    peaks = []
    for index in (1, 2, 10, 30, 100, 200, 299):
        goto(view, index)
        settled(view)
        assert set(view.scheduler.rasters) == set(view.scheduler.targets)
        assert len(view.scheduler.rasters) <= 3
        peaks.append(window.manager.resident_budget.bytes)
    assert set(view.scheduler.rasters) == {298, 299}
    # M3 retains recently visited pages in its bounded global cache.
    assert max(peaks) <= window.manager.resident_budget.max_bytes
    assert window.manager.budget.bytes == 0
    assert window.manager.resident_budget.peak_bytes <= window.manager.resident_budget.max_bytes


def test_rotated_cropbox_pages_match_placeholders_and_content(pdf_files, window):
    session = opened(window.manager, pdf_files / "geometry.pdf")
    view = window._widgets[session.document_id]
    # Independent expectations for the marker at (110,160), crop-local (60,90).
    expected = [(110, 160), (640, 110), (490, 640), (160, 490),
                (60, 90), (570, 60), (440, 570), (90, 440)]
    for index, point in enumerate(expected):
        goto(view, index)
        settled(view)
        raster = view.scheduler.rasters[index]
        width, height = ((600, 800) if index < 4 else (500, 660))
        if index % 2:
            width, height = height, width
        assert view.page_layout.pages[index].width == width
        assert view.page_layout.pages[index].height == height
        assert raster.image.width() == pytest.approx(width * raster.raster_scale, abs=1)
        color = raster.image.pixelColor(int(point[0] * raster.raster_scale), int(point[1] * raster.raster_scale))
        assert color.red() == pytest.approx(int((index % 5 + 1) / 6 * 255), abs=2)
        assert color.green() == pytest.approx(51, abs=2)
        del raster


def test_tab_switch_preserves_viewer_scroll_worker_and_content(pdf_files, window):
    a = opened(window.manager, pdf_files / "normal.pdf")
    av = window._widgets[a.document_id]
    goto(av, 2)
    settled(av)
    position = av.verticalScrollBar().value()
    a_workers = window.manager.workers_started
    b = opened(window.manager, pdf_files / "alternative.pdf")
    bv = window._widgets[b.document_id]
    settled(bv)
    old_count = a.worker.submitted_counts[Operation.RENDER]
    assert not av.scheduler.rasters and not av.scheduler.active
    raster = bv.scheduler.rasters[0]
    assert raster.image.pixelColor(round(110 * raster.raster_scale),
                                   round(160 * raster.raster_scale)).green() == pytest.approx(178, abs=2)
    QTest.qWait(80)
    assert a.worker.submitted_counts[Operation.RENDER] == old_count
    workers = a.worker, b.worker
    window.manager.activate(a.document_id)
    settled(av)
    assert window._widgets[a.document_id] is av and av.verticalScrollBar().value() == position
    raster = av.scheduler.rasters[2]
    assert raster.image.pixelColor(round(110 * raster.raster_scale),
                                   round(160 * raster.raster_scale)).green() == pytest.approx(51, abs=2)
    assert (a.worker, b.worker) == workers and window.manager.workers_started == a_workers + 1
    assert not bv.scheduler.rasters
    assert all(r.key.document_id == a.document_id for r in av.scheduler.rasters.values())


def test_tab_switch_while_render_pending_discards_late_pixels(pdf_files, window, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", slow_render_worker)
    b = opened(window.manager, pdf_files / "alternative.pdf")
    bv = window._widgets[b.document_id]
    settled(bv)
    a = opened(window.manager, pdf_files / "normal.pdf")
    av = window._widgets[a.document_id]
    wait_until(lambda: av.scheduler.pending is not None and a.worker.active_request_id is not None)
    count = a.worker.submitted_counts[Operation.RENDER]
    window.manager.activate(b.document_id)
    goto(bv, 3)
    settled(bv)
    wait_until(lambda: a.worker.pending_count == 0)
    assert a.worker.stale_discarded > 0
    assert not av.scheduler.rasters and av.scheduler.adopted_count == 0
    assert a.worker.submitted_counts[Operation.RENDER] == count
    assert window.manager.budget.bytes == 0


def test_stale_identity_generation_and_raster_parameters(pdf_files, window, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", slow_render_worker)
    session = opened(window.manager, pdf_files / "normal.pdf")
    view = window._widgets[session.document_id]
    scheduler = view.scheduler
    wait_until(lambda: scheduler.pending is not None)
    ticket = scheduler.pending
    response = Response(session.document_id, 1, ticket.request_id, Operation.RENDER, True,
                        {"page_index": ticket.key.page_index, "geometric_zoom": 1.0,
                         "dpr": ticket.key.dpr, "requested_scale": ticket.key.scale})
    assert scheduler.accepts(response)
    for stale in (replace(response, document_id="other"), replace(response, document_generation=0),
                  replace(response, request_id=response.request_id + 1),
                  replace(response, payload={**response.payload, "page_index": 4}),
                  replace(response, payload={**response.payload, "dpr": 3.5}),
                  replace(response, payload={**response.payload, "requested_scale": 7.0})):
        assert not scheduler.accepts(stale)
    session.document_generation = 2  # model a reload invalidation while the old process reply is pending
    wait_until(lambda: session.worker.stale_discarded > 0)
    assert scheduler.adopted_count == 0 and not scheduler.rasters
    assert not scheduler.accepts(response)
    session.document_generation = 1  # no production reload operation exists in M2
    settled(view)
    scheduler.set_active(False)
    assert not scheduler.accepts(response) and not scheduler.rasters


def test_global_backpressure_retries_latest_viewport_without_queue(pdf_files, window):
    budget = window.manager.budget
    assert budget.reserve() and budget.reserve()
    try:
        session = opened(window.manager, pdf_files / "long.pdf")
        view = window._widgets[session.document_id]
        wait_until(lambda: view.scheduler.backpressure_count > 0)
        for page in range(200):
            goto(view, page)
        QTest.qWait(40)
        assert view.scheduler.pending is None and not view.scheduler.rasters
        assert session.worker.submitted_counts[Operation.RENDER] == 0
        assert session.worker.pending_count == 0
    finally:
        budget.release()
        budget.release()
    settled(view)
    assert set(view.scheduler.rasters) == {198, 199, 200}
    assert session.worker.submitted_counts[Operation.RENDER] == 3
    assert budget.peak_bytes <= budget.max_bytes


def test_small_resident_limit_degrades_and_does_not_multiply_per_tab(pdf_files, window):
    window.manager.resident_budget = ResidentBudget(256 * 1024)
    for name in ("normal", "alternative", "third"):
        session = opened(window.manager, pdf_files / f"{name}.pdf")
        view = window._widgets[session.document_id]
        goto(view, 1)
        settled(view)
        assert all(r.raster_scale < 1 for r in view.scheduler.rasters.values())
        assert sum(r.cost for v in window._widgets.values() for r in v.scheduler.rasters.values()) == window.manager.resident_budget.bytes
        assert window.manager.resident_budget.bytes <= 256 * 1024
    assert window.manager.resident_budget.peak_bytes <= 256 * 1024


def test_oversized_product_raster_exceeds_spike_limit_but_stays_bounded(pdf_files, window):
    session = opened(window.manager, pdf_files / "large.pdf")
    view = window._widgets[session.document_id]
    layout = view.page_layout.pages
    settled(view)
    raster = view.scheduler.rasters[0]
    assert raster.key.region is not None
    assert raster.cost <= VIEWER_SINGLE_RASTER_BYTES
    assert raster.raster_scale == 1.5
    assert view.page_layout.pages == layout and layout[0].width == 10000
    assert window.manager.budget.peak_bytes <= window.manager.budget.max_bytes


def test_base_scale_high_dpi_uses_product_quality_not_m0_cap(pdf_files, manager):
    session = opened(manager, pdf_files / "normal.pdf")
    session.worker.submit(Operation.RENDER, {"page_index": 0, "geometric_zoom": 1, "dpr": 2,
                                            "max_bytes": VIEWER_SINGLE_RASTER_BYTES})
    with receive(session.worker) as delivery:
        response = delivery.response
        assert response.success and not response.payload["degraded"]
        assert response.payload["width"] == 1200 and response.payload["height"] == 1600
        assert MAX_SINGLE_RASTER_BYTES < len(response.pixels) <= VIEWER_SINGLE_RASTER_BYTES


def test_supersampling_changes_density_not_geometry_and_is_adaptive(pdf_files, window, monkeypatch):
    from pdf_annotator.viewer.render_scheduler import requested_raster_scale, supersampling_for_zoom

    assert [(zoom, supersampling_for_zoom(zoom), requested_raster_scale(zoom, 1.25))
            for zoom in (.75, 1, 1.25, 1.5, 2, 4, 8)] == [
                (.75, 1.5, 1.5), (1, 1.5, 1.875), (1.25, 1.5, 2.375),
                (1.5, 1.5, 2.875), (2, 1.25, 3.125),
                (4, 1., 5.), (8, 1., 10.)]
    factors = [supersampling_for_zoom(zoom) for zoom in (1.5, 1.75, 2, 2.25, 2.5)]
    assert factors == [1.5, 1.375, 1.25, 1.125, 1.0]
    with pytest.raises(ValueError):
        supersampling_for_zoom(0)
    with pytest.raises(ValueError):
        requested_raster_scale(1, float("nan"))
    session = opened(window.manager, pdf_files / "geometry.pdf")
    view = window._widgets[session.document_id]
    monkeypatch.setattr(view.viewport(), "devicePixelRatioF", lambda: 1.25)
    logical = view.page_layout.pages[0].width, view.page_layout.pages[0].height
    settled(view)
    raster = view.scheduler.rasters[0]
    assert raster.key.scale == raster.raster_scale == 1.875
    assert (raster.image.width(), raster.image.height()) == (1125, 1500)
    assert logical == (600, 800) and session.ui.zoom == 1
    assert view.coordinate_mapper(0).canonical_to_viewport((100, 200))[0] - view.page_viewport_rect(0).x() == 100


def test_product_protocol_limit_preserves_m0_profile():
    from pdf_annotator.worker.render import bounded_scale

    assert 0 < bounded_scale(600, 800, 1, 16) < 1
    with pytest.raises(ValueError):
        bounded_scale(600, 800, 1, 15)
    size = MAX_SINGLE_RASTER_BYTES + 2
    size -= size % 3
    response = Response("doc", 1, 1, Operation.RENDER, True,
                        {"width": size // 3, "height": 1, "stride": size, "format": "RGB888"},
                        pixels=b"x" * size)
    with pytest.raises(ValueError):
        encode(response)
    frame = encode(response, VIEWER_SINGLE_RASTER_BYTES)
    with pytest.raises(ValueError):
        decode(frame)
    assert decode(frame, VIEWER_SINGLE_RASTER_BYTES) == response


def test_close_during_render_releases_every_lease_and_ignores_late_result(pdf_files, window, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", slow_render_worker)
    session = opened(window.manager, pdf_files / "normal.pdf")
    view = window._widgets[session.document_id]
    scheduler = view.scheduler
    worker = session.worker
    wait_until(lambda: scheduler.pending is not None and worker.active_request_id is not None)
    window.manager.request_close(session.document_id)
    wait_until(lambda: session.lifecycle == Lifecycle.CLOSED)
    assert worker.stopped.is_set() and worker.pending_count == 0
    assert not scheduler.active and not scheduler.rasters and scheduler.adopted_count == 0
    assert window.manager.resident_budget.bytes == window.manager.budget.bytes == 0


def test_worker_dies_during_render_other_viewer_remains_usable(pdf_files, window, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", slow_render_worker)
    b = opened(window.manager, pdf_files / "alternative.pdf")
    bv = window._widgets[b.document_id]
    settled(bv)
    a = opened(window.manager, pdf_files / "normal.pdf")
    av = window._widgets[a.document_id]
    worker = a.worker
    wait_until(lambda: av.scheduler.pending is not None and worker.active_request_id is not None)
    os.kill(worker.pid, signal.SIGTERM)
    wait_until(lambda: a.lifecycle == Lifecycle.FAILED)
    assert "interrotto" in av.error_message
    assert not av.scheduler.rasters and not av.scheduler.active
    window.manager.activate(b.document_id)
    goto(bv, 3)
    settled(bv)
    assert b.lifecycle == Lifecycle.READY and not b.worker.stopped.is_set()
    assert 3 in bv.scheduler.rasters and worker.pending_count == 0
    window.manager.request_close(a.document_id)
    wait_until(lambda: a.lifecycle == Lifecycle.CLOSED)


def test_zoom_immediate_debounced_anchor_and_no_eager_render(pdf_files, window):
    from time import perf_counter
    from PySide6.QtCore import QPointF
    from pdf_annotator.viewer.render_scheduler import ZOOM_DEBOUNCE_MS

    session = opened(window.manager, pdf_files / "long.pdf")
    view = window._widgets[session.document_id]
    goto(view, 100)
    settled(view)
    anchor = QPointF(view.viewport().width() / 2, view.viewport().height() / 2)
    before = view.coordinate_mapper(100).viewport_to_canonical((anchor.x(), anchor.y()))
    raster = view.scheduler.rasters[100]
    requests = session.worker.submitted_counts[Operation.RENDER]
    started = perf_counter()
    for zoom in (1.1, 1.2, 1.3, 1.4):
        view.set_zoom(zoom, anchor)
        view.tick()
        assert view.page_layout.pages[100].width == pytest.approx(600 * zoom)
        assert view.scheduler.rasters[100] is raster
    assert session.worker.submitted_counts[Operation.RENDER] == requests
    assert view.coordinate_mapper(100).viewport_to_canonical((anchor.x(), anchor.y())) == pytest.approx(before, abs=1.5)
    settled(view)
    assert perf_counter() - started >= ZOOM_DEBOUNCE_MS / 1000
    assert session.worker.submitted_counts[Operation.RENDER] - requests <= 3
    assert view.scheduler.rasters[100].raster_scale >= 1.4
    assert view.session.ui.zoom == 1.4 and window.zoom_label.text() == "140%"
    assert view.coordinate_mapper(100).viewport_to_canonical((anchor.x(), anchor.y())) == pytest.approx(before, abs=1.5)


def test_zoom_controls_wheel_limits_and_tab_state(pdf_files, window):
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication

    session = opened(window.manager, pdf_files / "normal.pdf")
    view = window._widgets[session.document_id]
    settled(view)
    window.zoom_in_action.trigger()
    assert session.ui.zoom == pytest.approx(1.1)
    window.zoom_out_action.trigger()
    assert session.ui.zoom == pytest.approx(1)
    event = QWheelEvent(QPointF(150, 200), QPointF(150, 200), QPoint(), QPoint(0, 120),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier,
                        Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(view.viewport(), event)
    assert session.ui.zoom == pytest.approx(1.1)
    view.set_zoom(100)
    assert session.ui.zoom == 8 and not window.zoom_in_action.isEnabled()
    view.set_zoom(.01)
    assert session.ui.zoom == .25 and not window.zoom_out_action.isEnabled()
    view.set_zoom(1.75)
    goto(view, 2)
    settled(view)
    position = view.verticalScrollBar().value()
    other = opened(window.manager, pdf_files / "alternative.pdf")
    assert not view.scheduler.rasters and other.ui.zoom == 1
    assert all(k.document_id != session.document_id for k in window.manager.resident_budget.keys)
    window.manager.activate(session.document_id)
    settled(view)
    assert session.ui.zoom == 1.75 and view.verticalScrollBar().value() == position
    assert window.zoom_label.text() == "175%"


def test_zoom_cancels_in_flight_render_and_failure_isolated(pdf_files, window, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", slow_render_worker)
    session = opened(window.manager, pdf_files / "normal.pdf")
    view = window._widgets[session.document_id]
    wait_until(lambda: view.scheduler.pending is not None and session.worker.active_request_id is not None)
    view.set_zoom(2)
    goto(view, 2)
    settled(view)
    assert session.worker.stale_discarded > 0
    assert all(r.key.scale >= 2 for r in view.scheduler.rasters.values())
    view.set_zoom(3)
    wait_until(lambda: view.scheduler.pending is not None and session.worker.active_request_id is not None)
    worker = session.worker
    os.kill(worker.pid, signal.SIGTERM)
    wait_until(lambda: session.lifecycle == Lifecycle.FAILED)
    assert not view.scheduler.active and not view.scheduler.rasters
    assert window.manager.resident_budget.bytes == 0


def test_quantized_zoom_reuses_acceptable_key_and_recent_pages(pdf_files, window):
    session = opened(window.manager, pdf_files / "normal.pdf")
    view = window._widgets[session.document_id]
    view.set_zoom(1.01)
    settled(view)
    count = view.scheduler.requested_count
    keys = window.manager.resident_budget.keys
    view.set_zoom(1.02)
    settled(view)
    assert view.scheduler.requested_count == count
    assert window.manager.resident_budget.keys == keys
    goto(view, 2)
    settled(view)
    count = view.scheduler.requested_count
    goto(view, 0)
    settled(view)
    assert view.scheduler.requested_count == count
    assert window.manager.resident_budget.hits > 0


@pytest.mark.parametrize("budget_mib", (256, 128, 96))
def test_dpr_two_zoom_rotation_crop_geometry_and_capped_raster(pdf_files, window, monkeypatch, budget_mib):
    window.manager.resident_budget = ResidentBudget(budget_mib * 1024 * 1024)
    session = opened(window.manager, pdf_files / "geometry.pdf")
    view = window._widgets[session.document_id]
    monkeypatch.setattr(view.viewport(), "devicePixelRatioF", lambda: 2.0)
    view.set_zoom(1.5)
    goto(view, 5)
    settled(view)
    raster = view.scheduler.rasters[5]
    assert raster.key.dpr == 2 and raster.raster_scale == 4.5
    assert raster.key.region is not None
    assert raster.cost <= VIEWER_SINGLE_RASTER_BYTES
    assert raster.image.width() <= 769
    assert view.page_layout.pages[5].width == 660 * 1.5
    assert raster.image.devicePixelRatio() == 1  # explicit target QRect, no second DPR transform
    mapper = view.coordinate_mapper(5)
    assert mapper.viewport_to_canonical(mapper.canonical_to_viewport((60, 90))) == pytest.approx((60, 90))
    view.set_zoom(8)
    settled(view)
    assert all(r.cost <= VIEWER_SINGLE_RASTER_BYTES for r in view.scheduler.rasters.values())
    assert view.scheduler.rasters[5].raster_scale == 16
    assert view.scheduler.rasters[5].key.region is not None
    assert view.page_layout.pages[5].width == 660 * 8


def test_prefetch_direction_and_no_speculation_until_scroll_settles(pdf_files, window, monkeypatch):
    calls = observe_renders(monkeypatch)
    session = opened(window.manager, pdf_files / "long.pdf")
    view = window._widgets[session.document_id]
    settled(view)
    goto(view, 150)
    goto(view, 100)
    assert view.scheduler.nearby == (99, 101)
    settled(view)
    assert [c[2]["page_index"] for c in calls[-3:]] == [100, 99, 101]
    assert view.scheduler.pending is None and session.worker.pending_count == 0


@pytest.mark.parametrize("mode", tuple(LayoutMode))
def test_all_visible_pages_exceeding_normal_quality_share_degrade_safely(pdf_files, window, mode):
    window.manager.resident_budget = ResidentBudget(64 * 1024)
    window.resize(850, 1400)
    session = opened(window.manager, pdf_files / "normal.pdf")
    view = window._widgets[session.document_id]
    view.set_layout(mode)
    view.set_zoom(.25)
    settled(view)
    assert len(view.scheduler.visible) >= 4
    assert all(r.cost <= view.scheduler.raster_limit for r in view.scheduler.rasters.values())
    assert all(r.raster_scale < .25 for r in view.scheduler.rasters.values())
    assert window.manager.resident_budget.bytes <= 64 * 1024
    assert window.manager.resident_budget.peak_bytes <= 64 * 1024


@pytest.mark.parametrize("mode,expected", [
    ("single", (((0,),), ((0,), (1,)), ((0,), (1,), (2,)),
                ((0,), (1,), (2,), (3,)), ((0,), (1,), (2,), (3,), (4,)))),
    ("double", (((0,),), ((0, 1),), ((0, 1), (2,)),
                ((0, 1), (2, 3)), ((0, 1), (2, 3), (4,)))),
    ("double_cover", (((0,),), ((0,), (1,)), ((0,), (1, 2)),
                      ((0,), (1, 2), (3,)), ((0,), (1, 2), (3, 4)))),
])
@pytest.mark.parametrize("count", range(1, 6))
def test_layout_exact_pairing(mode, expected, count):
    pages = [PageGeometry(i, 600, 800, 0, (0, 0, 600, 800), (0, 0, 600, 800))
             for i in range(count)]
    layout = PageLayout(pages, mode=mode)
    assert tuple(row.page_indices for row in layout.rows) == expected[count - 1]
    assert len(layout.pages) == count
    if mode == "double_cover":
        assert layout.pages[0].x + 300 == layout.width / 2
    if mode != "single" and len(layout.rows[-1].page_indices) == 1:
        assert layout.pages[-1].x == 20


@pytest.mark.parametrize("zoom,expected", [
    (.5, ((20, 20, 300, 400), (344, 20, 330, 250), (20, 428, 160, 200), 694, 648)),
    (1, ((20, 20, 600, 800), (644, 20, 660, 500), (20, 828, 320, 400), 1324, 1248)),
    (1.5, ((20, 20, 900, 1200), (944, 20, 990, 750), (20, 1228, 480, 600), 1954, 1848)),
    (3, ((20, 20, 1800, 2400), (1844, 20, 1980, 1500), (20, 2428, 960, 1200), 3844, 3648)),
])
def test_spread_numeric_mixed_geometry_and_fixed_gap(zoom, expected):
    pages = [PageGeometry(0, 600, 800, 0, (0, 0, 600, 800), (0, 0, 600, 800)),
             PageGeometry(1, 500, 660, 90, (50, 70, 550, 730), (0, 0, 600, 800)),
             PageGeometry(2, 320, 400, 180, (0, 0, 320, 400), (0, 0, 320, 400))]
    layout = PageLayout(pages, zoom, "double", column_gap=24)
    assert [(p.x, p.y, p.width, p.height) for p in layout.pages] == list(expected[:3])
    assert (layout.width, layout.height) == expected[3:]
    assert [row.height for row in layout.rows] == [800 * zoom, 400 * zoom]
    cover = PageLayout(pages, zoom, "double_cover", column_gap=24)
    assert cover.pages[0].x + 300 * zoom == cover.width / 2
    assert cover.pages[1].y == cover.pages[2].y == 28 + 800 * zoom
    assert cover.pages[2].x - cover.pages[1].x - cover.pages[1].width == 24
    if zoom == 1:
        assert [(p.x, p.y, p.width, p.height) for p in cover.pages] == [
            (222, 20, 600, 800), (20, 828, 660, 500), (704, 828, 320, 400)]
        assert (cover.width, cover.height) == (1044, 1348)
    with pytest.raises(ValueError):
        PageLayout(pages, column_gap=-1)


def test_spread_visible_uses_page_rect_not_row_and_nearby_is_directional():
    pages = [PageGeometry(i, 600, 800 if i % 2 == 0 else 400, 0,
                          (0, 0, 600, 800 if i % 2 == 0 else 400), (0, 0, 600, 800))
             for i in range(10)]
    layout = PageLayout(pages, mode="double")
    assert layout.visible(0, 500, 1300, 20) == (0,)  # below the short right page
    assert layout.visible(619, 20, 2, 100) == (0, 1)  # both sides of the seam
    assert layout.visible(620, 20, 600, 400) == (1,)
    assert layout.nearby((1,), 20, 400) == (0, 2, 3)
    assert layout.nearby((4, 5), 1636, 300) == (6, 7, 2, 3)
    assert layout.nearby((4, 5), 1636, 300, direction=-1) == (2, 3, 6, 7)
    assert layout.nearby((4,), 1652, 300, count=0) == ()
    assert layout.nearby((), 824, 4) == (2, 3, 0, 1)
    assert layout.page_at(1700, 900) == 5


@pytest.mark.parametrize("mode", tuple(LayoutMode))
def test_default_page_spacing_joins_spreads_and_preserves_vertical_spacing(mode):
    pages = [PageGeometry(index, 600, 800, 0, (0, 0, 600, 800), (0, 0, 600, 800))
             for index in range(3)]
    layout = PageLayout(pages, mode=mode)
    assert PAGE_COLUMN_GAP == 0
    assert PAGE_ROW_GAP == pytest.approx(PAGE_GAP / 2)
    if mode == LayoutMode.SINGLE:
        assert layout.rows[1].y - (layout.rows[0].y + layout.rows[0].height) == PAGE_ROW_GAP
    else:
        spread = next(row for row in layout.rows if len(row.page_indices) == 2)
        left, right = (layout.pages[index] for index in spread.page_indices)
        seam = left.x + left.width
        assert right.x == seam
        assert left.x == PAGE_MARGIN and layout.width - (right.x + right.width) == PAGE_MARGIN
        assert layout.page_at(left.y + 10, seam - .01) == spread.page_indices[0]
        assert layout.page_at(left.y + 10, seam) == spread.page_indices[0]  # existing edge tie-break
        assert layout.page_at(right.y + 10, seam + .01) == spread.page_indices[1]
        if len(layout.rows) > 1:
            assert layout.rows[1].y - (layout.rows[0].y + layout.rows[0].height) == PAGE_ROW_GAP


def center_page(view, index):
    page = view.page_layout.pages[index]
    view.horizontalScrollBar().setValue(round(page.x + page.width / 2 - view.viewport().width() / 2))
    view.verticalScrollBar().setValue(round(page.y + page.height / 2 - view.viewport().height() / 2))


@pytest.mark.parametrize("mode", ("double", "double_cover"))
def test_spread_seam_is_one_physical_pixel_at_fractional_zoom(pdf_files, window, mode):
    window.resize(1200, 850)
    session = opened(window.manager, pdf_files / "normal.pdf")
    view = window._widgets[session.document_id]
    view.set_layout(mode)
    view.set_zoom(.737)
    row = next(row for row in view.page_layout.rows if len(row.page_indices) == 2)
    view.verticalScrollBar().setValue(round(row.y) - 20)
    settled(view)
    left, right = (view.page_viewport_rect(index) for index in row.page_indices)
    assert right.left() == pytest.approx(left.right())
    assert view._page_at_viewport(QPointF(left.right() - .1, left.top() + 10)) == row.page_indices[0]
    assert view._page_at_viewport(QPointF(right.left() + .1, right.top() + 10)) == row.page_indices[1]
    snapshot = view.viewport().grab().toImage()
    dpr = snapshot.devicePixelRatio()
    seam = floor(right.left() * dpr)
    assert 2 <= seam < snapshot.width() - 2
    y = floor((right.top() + 10) * dpr)
    background = QColor("#595c61")
    assert sum(snapshot.pixelColor(seam + dx, y) == background for dx in range(-2, 3)) == 1


@pytest.mark.parametrize("zoom", (.5, 1, 1.5, 3))
def test_layout_changes_anchor_first_last_left_right_and_repeated(pdf_files, window, zoom):
    session = opened(window.manager, pdf_files / "long.pdf")
    view = window._widgets[session.document_id]
    view.set_zoom(zoom)
    QTest.qWait(30)  # settle scrollbar visibility before capturing the anchor
    worker = session.worker
    for page in (0, 126, 127, 299):
        view.set_layout("single")
        center_page(view, page)
        for mode in ("double", "single", "double_cover", "double", "double_cover", "single"):
            index, canonical, point = view._reading_anchor()
            assert index == page
            view.set_layout(mode)
            QTest.qWait(1)
            assert page in view.scheduler.visible
            restored = view.coordinate_mapper(index).canonical_to_viewport(canonical)
            assert QRectF(view.viewport().rect()).adjusted(-1, -1, 1, 1).contains(QPointF(*restored))
            # Vertical placement can be clamped at the first/last spread only.
            if 0 < view.verticalScrollBar().value() < view.verticalScrollBar().maximum():
                assert restored[1] == pytest.approx(point.y(), abs=2)
            assert session.ui.zoom == zoom and session.ui.layout == mode
    assert session.worker is worker and window.manager.workers_started == 1
    settled(view)
    assert session.worker.submitted_counts[Operation.RENDER] < 40


@pytest.mark.parametrize("mode", ("double", "double_cover"))
def test_spread_zoom_right_page_crop_rotation_and_dpr(pdf_files, window, monkeypatch, mode):
    session = opened(window.manager, pdf_files / "geometry.pdf")
    view = window._widgets[session.document_id]
    monkeypatch.setattr(view.viewport(), "devicePixelRatioF", lambda: 2.)
    view.set_layout(mode)
    view.set_zoom(1.5)
    # Every rotated/cropped landmark keeps its independently known displayed position.
    points = ((110, 160), (640, 110), (490, 640), (160, 490),
              (60, 90), (570, 60), (440, 570), (90, 440))
    for index, display_point in enumerate(points):
        canonical = (110, 160) if index < 4 else (60, 90)
        page = view.page_layout.pages[index]
        mapped = view.coordinate_mapper(index).canonical_to_viewport(canonical)
        origin = view._origin()
        assert mapped == pytest.approx((page.x + display_point[0] * 1.5 - origin[0],
                                       page.y + display_point[1] * 1.5 - origin[1]))
    right = 5 if mode == "double" else 6
    center_page(view, right)
    settled(view)
    focus = view.page_viewport_rect(right).intersected(QRectF(view.viewport().rect())).center()
    canonical = view.coordinate_mapper(right).viewport_to_canonical((focus.x(), focus.y()))
    assert view._reading_anchor(focus)[0] == right
    view.set_zoom(3, focus)
    assert view.coordinate_mapper(right).viewport_to_canonical((focus.x(), focus.y())) == pytest.approx(canonical, abs=1)
    settled(view)
    raster = view.scheduler.rasters[right]
    assert raster.key.dpr == 2 and raster.image.devicePixelRatio() == 1
    assert raster.cost <= VIEWER_SINGLE_RASTER_BYTES
    assert view.page_layout.pages[right].width == session.pages[right].displayed_size[0] * 3


def test_layout_reuses_identical_cached_rasters_and_virtualizes_rows(pdf_files, window, monkeypatch):
    window.resize(1400, 900)
    calls = observe_renders(monkeypatch)
    session = opened(window.manager, pdf_files / "long.pdf")
    view = window._widgets[session.document_id]
    settled(view)
    cache = window.manager.resident_budget
    old = dict(view.scheduler.rasters)
    initial_targets = view.scheduler.targets
    evictions = cache.evictions
    view.set_layout("double")
    settled(view)
    assert all(view.scheduler.rasters[i] is raster for i, raster in old.items())
    # The compact rows expose pages 0..3 at this viewport, with 4..5 as the
    # single nearby row. Cached 0..2 are retained; exactly the newly exposed
    # targets are rendered, never a cache reset or off-target speculation.
    assert cache.evictions == evictions
    assert [call[2]["page_index"] for call in calls] == list(dict.fromkeys(
        (*initial_targets, *view.scheduler.targets)))
    assert len(view.page_layout.rows) == 150 and len(view.page_layout.pages) == 300
    assert not view.findChildren(PdfView)
    for page in (250, 254, 258, 254):
        goto(view, page)
        visible = view.scheduler.visible
        missing_visible = [i for i in visible if cache.get(view.scheduler._key(i), record=False) is None]
        before = len(calls)
        settled(view)
        requested = [call[2]["page_index"] for call in calls[before:]]
        assert set(requested).issubset(view.scheduler.targets)
        # `nearby(..., count=1)` permits one adjacent double-page row on each
        # side; compact vertical spacing can make three rows visible here.
        assert len(view.scheduler.nearby) <= 4
        assert len(view.scheduler.targets) == (len(view.scheduler.visible)
                                               + len(view.scheduler.nearby))
        assert len(requested) <= len(view.scheduler.targets)
        if missing_visible:
            assert requested[0] == missing_visible[0]
    assert len(calls) < 30
    assert cache.peak_bytes <= 128 * 1024 * 1024
    assert window.manager.budget.peak_bytes <= 32 * 1024 * 1024
    count = len(calls)
    for mode in ("double_cover", "double", "single", "double"):
        view.set_layout(mode)
        settled(view)
    assert len(calls) - count <= 2  # only a newly exposed adjacent row, never a cache reset


def test_layout_selector_independent_sessions_and_scrollbar_ranges(pdf_files, window):
    assert not window.layout_button.isEnabled()
    a = opened(window.manager, pdf_files / "normal.pdf")
    av = window._widgets[a.document_id]
    window.layout_actions["double_cover"].trigger()
    av.set_zoom(1.25)
    center_page(av, 3)
    settled(av)
    a_position = av.horizontalScrollBar().value(), av.verticalScrollBar().value()
    b = opened(window.manager, pdf_files / "alternative.pdf")
    bv = window._widgets[b.document_id]
    bv.set_zoom(.9)
    center_page(bv, 1)
    settled(bv)
    b_position = bv.horizontalScrollBar().value(), bv.verticalScrollBar().value()
    for _ in range(3):
        for session, view, mode, zoom, position in ((a, av, "double_cover", 1.25, a_position),
                                                  (b, bv, "single", .9, b_position)):
            window.manager.activate(session.document_id)
            settled(view)
            assert session.ui.layout == mode and window.layout_actions[mode].isChecked()
            assert window.layout_button.text() == window.layout_actions[mode].text()
            assert session.ui.zoom == zoom
            assert (view.horizontalScrollBar().value(), view.verticalScrollBar().value()) == position
            assert all(k.document_id == session.document_id for k in window.manager.resident_budget.keys)
    assert window.manager.workers_started == 2
    for mode in LayoutMode:
        bv.set_layout(mode)
        bv.set_zoom(.5)
        QTest.qWait(20)
        assert bv.horizontalScrollBar().maximum() == 0
        bv.set_zoom(3)
        QTest.qWait(20)
        assert bv.horizontalScrollBar().maximum() > 0
        from math import ceil
        assert bv.verticalScrollBar().maximum() == ceil(bv.page_layout.height) - bv.viewport().height()


def test_layout_change_with_pending_render_rejects_obsolete_result(pdf_files, window, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", slow_render_worker)
    session = opened(window.manager, pdf_files / "long.pdf")
    view = window._widgets[session.document_id]
    wait_until(lambda: view.scheduler.pending is not None and session.worker.active_request_id is not None)
    view.set_layout("double_cover")
    center_page(view, 250)  # right page of the cover spread, not its left companion
    view.set_layout("double")
    settled(view)
    assert session.worker.stale_discarded > 0
    assert 250 in view.scheduler.visible
    assert set(view.scheduler.rasters).issubset(view.scheduler.targets)
    assert session.worker.submitted_counts[Operation.RENDER] <= 7
    assert window.manager.budget.bytes == 0
