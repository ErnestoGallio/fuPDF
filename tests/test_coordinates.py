import pytest

from pdf_annotator.viewer.coordinate_mapper import CoordinateMapper, PageGeometry
from pdf_annotator.worker.protocol import Operation
from .conftest import request


def test_rotations_cropbox_and_independent_landmarks(clients):
    client = clients(geometry=True)
    reply = request(client, Operation.INFO)
    assert reply.success, reply.error
    pages = reply.payload["pages"]
    assert len(pages) == 8
    # Independent literal expectations for point (10, 20), not mapper roundtrip only.
    expected_full = [(10, 20), (780, 10), (590, 780), (20, 590)]
    expected_crop = [(10, 20), (640, 10), (490, 640), (20, 490)]
    for i, record in enumerate(pages):
        cropped = i >= 4
        assert record["width_pt"] == (500 if cropped else 600)
        assert record["height_pt"] == (660 if cropped else 800)
        assert record["mediabox"] == [0, 0, 600, 800]
        assert record["cropbox"] == ([50, 70, 550, 730] if cropped else [0, 0, 600, 800])
        # Red rectangle was placed before cropping: canonical coordinates become
        # crop-local exactly once, regardless of /Rotate.
        assert record["landmark"] == pytest.approx([50, 80, 60, 90] if cropped else [100, 150, 110, 160])
        geometry = PageGeometry(**{k: v for k, v in record.items() if k != "landmark"})
        mapper = CoordinateMapper(geometry, zoom=2.5, scene_origin=(30, 40),
                                  viewport_scene_origin=(4, 7))
        expected = (expected_crop if cropped else expected_full)[i % 4]
        assert mapper.canonical_to_display((10, 20)) == pytest.approx(expected)
        viewport = (expected[0] * 2.5 + 26, expected[1] * 2.5 + 33)
        assert mapper.canonical_to_viewport((10, 20)) == pytest.approx(viewport)
        assert mapper.viewport_to_canonical(viewport) == pytest.approx((10, 20))
        assert mapper.canonical_to_raster((10, 20), 5) == pytest.approx((expected[0] * 5, expected[1] * 5))
        assert mapper.canonical_to_viewport((10, 20)) == pytest.approx(viewport)


def test_invalid_mapper_geometry():
    with pytest.raises(ValueError):
        PageGeometry(0, 100, 100, 45, (0, 0, 100, 100), (0, 0, 100, 100))
