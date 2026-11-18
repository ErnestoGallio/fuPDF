"""M3 cache accounting, relevance and explicit borrowed-image lifetimes."""

from dataclasses import replace
import weakref

from PySide6.QtGui import QImage
import pytest

from pdf_annotator.viewer.raster_cache import RasterCache, RenderKey


def key(page=0, document="A", scale=1.0, dpr=1.0):
    return RenderKey(document, 1, page, scale, dpr)


def insert(cache, target):
    return cache.put(target, 400, target.scale,
                     lambda: QImage(100, 1, QImage.Format.Format_RGB32))


def test_insert_hit_miss_bytes_and_deterministic_lru():
    cache = RasterCache(800)
    assert insert(cache, key(0)) and insert(cache, key(1))
    assert cache.get(key(9)) is None
    assert cache.get(key(0)).cost == 400
    assert insert(cache, key(2))
    assert cache.keys == (key(0), key(2))
    assert cache.hits == cache.misses == cache.evictions == 1
    assert cache.bytes == cache.peak_bytes == 800
    cache.remove_document("A")
    assert cache.bytes == cache.count == 0


def test_visible_protected_before_nearby_and_inactive_document():
    cache = RasterCache(1200)
    cache.set_relevance("A", (0,), (1,))
    for target in (key(0), key(1), key(0, "B")):
        assert insert(cache, target)
    cache.get(key(0, "B"))  # inactive is newest but still the first victim
    assert insert(cache, key(2))
    assert key(0, "B") not in cache.keys
    assert insert(cache, key(3))
    assert key(0) in cache.keys and key(1) in cache.keys
    cache.remove_document("A")
    assert cache.bytes == 0


def test_evicted_borrowed_image_remains_accounted_until_release():
    cache = RasterCache(800)
    assert insert(cache, key())
    with cache.borrow(key()) as held:
        image_reference = weakref.ref(held.image)
        cache.remove_document("A")
        assert cache.keys == () and cache.count == 1 and cache.bytes == 400
        assert not held.image.isNull()
        assert insert(cache, key(0, "B"))
        assert not cache.put(key(1, "B"), 800, 1,
                             lambda: QImage(200, 1, QImage.Format.Format_RGB32))
        assert cache.bytes == 800
    assert held.image.isNull() and cache.bytes == 400 and cache.count == 1
    assert image_reference() is None  # no hidden cache/lease reference to old Qt storage
    cache.remove_document("B")
    assert cache.bytes == 0


def test_failed_adoption_does_not_leak_and_oversized_does_not_allocate():
    cache = RasterCache(400)
    called = []
    assert not cache.put(key(), 401, 1, lambda: called.append(True))
    assert not called and cache.bytes == 0
    with pytest.raises(ValueError):
        cache.put(key(), 400, 1, QImage)
    assert cache.bytes == 0 and cache.count == 0


def test_scale_dpr_generation_and_document_keys_never_collide():
    cache = RasterCache(4000)
    target = key()
    variants = (target, replace(target, dpr=2), replace(target, scale=2),
                replace(target, document_id="B"), replace(target, generation=2))
    for variant in variants:
        assert insert(cache, variant)
    assert cache.count == 5 and cache.bytes == 2000
    assert cache.preview(replace(target, scale=1.1)).key == target
    assert cache.preview(replace(target, generation=3)) is None
    cache.remove_document("A")
    assert cache.keys == (replace(target, document_id="B"),)


def test_cache_plateaus_across_hundreds_of_pages():
    cache = RasterCache(4000)
    for page in range(600):
        cache.set_relevance("A", (page,), ())
        assert insert(cache, key(page))
        assert cache.bytes <= 4000 and cache.count <= 10
    assert cache.bytes == cache.peak_bytes == 4000 and cache.evictions == 590


def test_diagnostics_are_disabled_by_default_and_samples_are_bounded():
    from pdf_annotator.viewer.metrics import ViewerMetrics

    metrics = ViewerMetrics()
    metrics.add("paint_ms", 10)
    assert metrics.report() == {}
    metrics.enabled = True
    for index in range(6000):
        metrics.add("paint_ms", index / 1000)
    assert len(metrics.samples["paint_ms"]) == 4096
    assert metrics.report()["paint_ms"]["count"] == 6000
