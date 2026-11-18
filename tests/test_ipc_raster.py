import gc
from multiprocessing.connection import _ConnectionBase
from multiprocessing.process import BaseProcess
import threading
import sys

from PySide6.QtCore import QTimer
import pytest

from pdf_annotator.viewer.raster_image import adopt_rgb
from pdf_annotator.worker.protocol import (MAX_PENDING_REQUESTS, MAX_SINGLE_RASTER_BYTES,
                                           Operation)
from pdf_annotator.worker.render import bounded_scale
from .conftest import receive, request, wait_until


def test_two_workers_interleaved_and_independent_close(clients):
    a, b = clients("A"), clients("B")
    a_id, b_id = a.submit(Operation.INFO), b.submit(Operation.INFO)
    with receive(b) as bd, receive(a) as ad:
        ar, br = ad.response, bd.response
        assert ar.success and br.success
        assert (ar.document_id, ar.request_id, ar.payload["marker"]) == ("A", a_id, "A")
        assert (br.document_id, br.request_id, br.payload["marker"]) == ("B", b_id, "B")
        assert ar.payload["path"] == a.fixture.path
        assert br.payload["path"] == b.fixture.path
        assert ar.payload["pid"] != br.payload["pid"]
    a.close_async()
    wait_until(a.stopped.is_set)
    assert not a.forced_termination and a.failure is None
    assert request(b, Operation.INFO).payload["marker"] == "B"
    assert "pymupdf" not in sys.modules  # only the spawned owners import the engine


def test_crash_isolated_and_transport_requests_released(clients):
    a, b = clients("A"), clients("B")
    assert request(a, Operation.INFO).success
    assert request(b, Operation.INFO).success
    assert a.submit(Operation.CRASH)
    a.submit(Operation.RENDER)
    wait_until(a.stopped.is_set)
    assert a.failure is not None
    assert a.pending_count == 0
    assert a.budget.bytes == 0
    assert request(b, Operation.INFO).success


def test_stale_completed_raster_and_document_generation(clients):
    a = clients()
    assert request(a, Operation.INFO).success
    old = a.submit(Operation.RENDER)
    wait_until(lambda: a._responses.qsize() == 1)
    new = a.submit(Operation.RENDER, {"geometric_zoom": 1.25})
    assert new > old
    with receive(a) as delivery:
        assert delivery.response.request_id == new
        assert delivery.response.payload["geometric_zoom"] == 1.25
    assert a.stale_discarded == 1
    assert a.budget.bytes == 0
    a.submit(Operation.RENDER)
    wait_until(lambda: a._responses.qsize() == 1)
    a.generation += 1  # simulate closing/replacing a generation before adoption
    assert a.poll() is None
    assert a.stale_discarded == 2
    assert a.budget.bytes == 0


def test_global_backpressure_qimage_lifetime_and_single_raster_cap(clients):
    a, b = clients("A"), clients("B", large=True)
    assert request(a, Operation.INFO).success
    assert request(b, Operation.INFO).success
    assert a.submit(Operation.RENDER)
    assert b.submit(Operation.RENDER, {"geometric_zoom": 8, "dpr": 2})
    wait_until(lambda: a._responses.qsize() == b._responses.qsize() == 1)
    assert a.budget.count == 2
    assert a.budget.bytes == 2 * MAX_SINGLE_RASTER_BYTES
    assert a.submit(Operation.RENDER) is None
    ad = receive(a)
    assert ad.response.payload["alpha"] is False
    assert len(ad.response.pixels) == ad.response.payload["stride"] * ad.response.payload["height"]
    image = adopt_rgb(ad)
    assert ad.response is None
    assert a.budget.bytes == MAX_SINGLE_RASTER_BYTES
    gc.collect()
    assert image.pixelColor(105, 155).red() == 255
    assert image.pixelColor(105, 155).green() == 0
    assert image.sizeInBytes() == image.bytesPerLine() * image.height()
    bd = receive(b)
    p = bd.response.payload
    assert p["degraded"] is True and p["raster_scale"] < 16
    assert p["geometric_zoom"] == 8 and p["dpr"] == 2
    assert len(bd.response.pixels) <= MAX_SINGLE_RASTER_BYTES
    second = adopt_rgb(bd)
    assert not second.isNull()
    a.close_async()
    b.close_async()
    wait_until(lambda: a.stopped.is_set() and b.stopped.is_set())
    # Detached Qt storage survives reply release AND worker/process teardown.
    assert image.pixelColor(105, 155).green() == 0
    assert not second.isNull()
    assert a.budget.bytes == 0
    del image, second
    gc.collect()


def test_control_channel_saturation_has_explicit_refusal(clients):
    a = clients()
    assert request(a, Operation.INFO).success
    for _ in range(MAX_PENDING_REQUESTS):
        assert a.submit(Operation.INFO) is not None
    assert a.submit(Operation.INFO) is None
    assert a.pending_count == MAX_PENDING_REQUESTS
    for _ in range(MAX_PENDING_REQUESTS):
        with receive(a) as delivery:
            assert delivery.response.success
    assert a.pending_count == 0


def test_delivered_buffer_keeps_credit_until_released_after_worker_close(clients):
    a = clients()
    assert request(a, Operation.INFO).success
    payload = {"geometric_zoom": 1.0}
    assert a.submit(Operation.RENDER, payload)
    payload["geometric_zoom"] = 99.0  # submitting snapshots mutable caller data
    delivery = receive(a)
    assert delivery.response.payload["geometric_zoom"] == 1.0
    a.close_async()
    wait_until(a.stopped.is_set)
    assert a.budget.bytes == MAX_SINGLE_RASTER_BYTES
    assert a.pending_count == 1
    delivery.release()
    delivery.release()  # explicit ownership release is idempotent
    assert a.budget.bytes == 0 and a.pending_count == 0


@pytest.mark.parametrize("forced", [False, True])
def test_busy_shutdown_and_pending_payloads_keep_qt_responsive(clients, forced):
    a, b = clients("A", shutdown_timeout=0.15 if forced else 2.0), clients("B")
    assert request(a, Operation.INFO).success
    assert request(b, Operation.INFO).success
    # Hold one unadopted raster, then close while another operation is running.
    assert a.submit(Operation.RENDER)
    wait_until(lambda: a._responses.qsize() == 1)
    hold = a.submit(Operation.HOLD, {"seconds": 2 if forced else 0.2})
    wait_until(lambda: a.active_request_id == hold)
    beats = []
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: beats.append(1))
    timer.start()
    a.close_async()
    wait_until(a.stopped.is_set)
    timer.stop()
    assert len(beats) >= 3
    assert a.forced_termination is forced
    assert a.pending_count == 0 and a.budget.bytes == 0
    assert request(b, Operation.INFO).success


def test_send_recv_and_join_never_execute_on_gui_thread(clients, monkeypatch):
    observed = []
    for owner, name in [(_ConnectionBase, "send_bytes"), (_ConnectionBase, "recv_bytes"), (BaseProcess, "join")]:
        original = getattr(owner, name)

        def traced(self, *args, _name=name, _original=original, **kwargs):
            observed.append((_name, threading.current_thread() is threading.main_thread()))
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(owner, name, traced)
    a = clients()
    assert request(a, Operation.INFO).success
    assert a.submit(Operation.RENDER)
    with receive(a) as delivery:
        assert delivery.response.success
    a.close_async()
    wait_until(a.stopped.is_set)
    assert {name for name, _ in observed} == {"send_bytes", "recv_bytes", "join"}
    assert all(not is_gui for _, is_gui in observed)


def test_single_raster_estimate_handles_rounding_and_rejects_nonfinite():
    import math

    for w, h in [(600, 800), (10000, 12000), (1, 500000)]:
        scale = bounded_scale(w, h, 16)
        assert (math.ceil(w * scale) + 1) * (math.ceil(h * scale) + 1) * 3 <= MAX_SINGLE_RASTER_BYTES
    with pytest.raises(ValueError):
        bounded_scale(600, 800, float("inf"))
