import struct

import pytest

from pdf_annotator.worker.protocol import (MAX_CONTROL_BYTES, MAX_SINGLE_RASTER_BYTES,
                                           Operation, Request, Response, decode, encode)


def test_typed_roundtrip_and_identity():
    req = Request("A", 3, 8, Operation.RENDER, {"geometric_zoom": 2.0})
    assert decode(encode(req)) == req
    reply = Response("A", 3, 8, Operation.RENDER, True,
                     {"width": 2, "height": 1, "stride": 6, "format": "RGB888"},
                     pixels=b"\xff\x00\x00\x00\xff\x00")
    assert decode(encode(reply)) == reply


@pytest.mark.parametrize("payload", [{"object": object()}, {"zoom": float("nan")}])
def test_non_serializable_and_nonfinite_rejected(payload):
    with pytest.raises((TypeError, ValueError)):
        encode(Request("A", 1, 1, Operation.INFO, payload))


def test_size_and_malformed_frame_limits():
    with pytest.raises(ValueError):
        encode(Request("A", 1, 1, Operation.INFO, {"x": "x" * MAX_CONTROL_BYTES}))
    with pytest.raises(ValueError):
        encode(Response("A", 1, 1, Operation.RENDER, True,
                        pixels=b"x" * (MAX_SINGLE_RASTER_BYTES + 1)))
    with pytest.raises(ValueError):
        decode(struct.pack("!I", MAX_CONTROL_BYTES + 1))
    with pytest.raises(ValueError):
        decode(encode(Response("A", 1, 1, Operation.RENDER, True,
                               {"width": 2, "height": 1, "stride": 6, "format": "RGB888"},
                               pixels=b"123")))
