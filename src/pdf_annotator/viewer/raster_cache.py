"""GUI-owned byte-bounded LRU. Evicted but borrowed images remain charged."""

from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator

from PySide6.QtGui import QImage

from ..worker.protocol import MIN_RASTER_BYTES
from .regional_render import Rect, TileRegion

# M3 native 400-page comparison: 128 MiB keeps moderate-zoom returns cached;
# 96 MiB introduces extra visible misses. Transit/single-raster caps are separate.
RESIDENT_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True)
class RenderKey:
    document_id: str
    generation: int
    page_index: int
    scale: float  # physical pixels / PDF point, including DPR and cap
    dpr: float
    region: TileRegion | None = None
    annotation_id: str | None = None


@dataclass
class ResidentRaster:
    key: RenderKey
    image: QImage
    raster_scale: float
    cost: int
    coverage: Rect | None = None
    borrowers: int = 0
    retired: bool = False


class RasterCache:
    def __init__(self, max_bytes: int = RESIDENT_BYTES) -> None:
        if type(max_bytes) is not int or max_bytes < MIN_RASTER_BYTES:
            raise ValueError("Resident budget too small")
        self.max_bytes = max_bytes
        self.bytes = self.peak_bytes = 0
        self.hits = self.misses = self.evictions = 0
        self._entries: OrderedDict[RenderKey, ResidentRaster] = OrderedDict()
        self._retired: list[ResidentRaster] = []
        self._relevance: dict[str, tuple[tuple[int, ...], tuple[int, ...],
                                         frozenset[RenderKey], frozenset[RenderKey]]] = {}

    @property
    def count(self) -> int:
        return len(self._entries) + len(self._retired)

    @property
    def keys(self) -> tuple[RenderKey, ...]:
        return tuple(self._entries)

    def set_relevance(self, document_id: str, visible: tuple[int, ...], nearby: tuple[int, ...],
                      visible_keys: tuple[RenderKey, ...] = (),
                      prefetch_keys: tuple[RenderKey, ...] = ()) -> None:
        self._relevance[document_id] = (visible, nearby, frozenset(visible_keys),
                                        frozenset(prefetch_keys))

    def _priority(self, key: RenderKey) -> int:
        visible, nearby, visible_keys, prefetch_keys = self._relevance.get(
            key.document_id, ((), (), frozenset(), frozenset()))
        if key in visible_keys:
            return 2
        if key in prefetch_keys:
            return 1
        return 2 if key.page_index in visible else 1 if key.page_index in nearby else 0

    def get(self, key: RenderKey, *, record: bool = True) -> ResidentRaster | None:
        entry = self._entries.get(key)
        if record:
            if entry is None:
                self.misses += 1
            else:
                self.hits += 1
        if entry is not None:
            self._entries.move_to_end(key)
        return entry

    def preview(self, key: RenderKey) -> ResidentRaster | None:
        """A fallback may be scaled during debounce, never across pages/generations."""
        exact = self._entries.get(key)
        if exact is not None:
            return exact
        candidates = (entry for other, entry in self._entries.items()
                      if (other.document_id, other.generation, other.page_index)
                      == (key.document_id, key.generation, key.page_index)
                      and other.region == key.region and other.annotation_id == key.annotation_id)
        return min(candidates, key=lambda entry: abs(entry.key.scale - key.scale), default=None)

    def page_entries(self, document_id: str, generation: int,
                     page_index: int) -> tuple[ResidentRaster, ...]:
        return tuple(entry for key, entry in self._entries.items()
                     if (key.document_id, key.generation, key.page_index)
                     == (document_id, generation, page_index) and key.annotation_id is None)

    @contextmanager
    def borrow(self, key: RenderKey) -> Iterator[ResidentRaster | None]:
        entry = self._entries.get(key)
        if entry is not None:
            entry.borrowers += 1
        try:
            yield entry
        finally:
            if entry is not None:
                entry.borrowers -= 1
                if entry.retired and not entry.borrowers:
                    self._retired.remove(entry)
                    self._free(entry)

    def _free(self, entry: ResidentRaster) -> None:
        entry.image = QImage()  # release owned storage before its accounting credit
        self.bytes -= entry.cost
        assert self.bytes >= 0

    def _drop(self, key: RenderKey) -> None:
        entry = self._entries.pop(key)
        entry.retired = True
        if entry.borrowers:
            self._retired.append(entry)
        else:
            self._free(entry)

    def remove_document(self, document_id: str) -> None:
        for key in tuple(self._entries):
            if key.document_id == document_id:
                self._drop(key)
        self._relevance.pop(document_id, None)

    def remove(self, key: RenderKey) -> None:
        """Retire one cached raster while leaving unrelated annotations hot."""
        if key in self._entries:
            self._drop(key)

    def can_admit(self, key: RenderKey, cost: int) -> bool:
        if not 0 < cost <= self.max_bytes:
            return False
        reclaimable = sum(entry.cost for other, entry in self._entries.items()
                          if not entry.borrowers and self._priority(other) <= self._priority(key))
        return self.bytes + cost - reclaimable <= self.max_bytes

    def put(self, key: RenderKey, cost: int, raster_scale: float,
            make_image: Callable[[], QImage], coverage: Rect | None = None) -> bool:
        """Reserve before adoption; lowest relevance, then oldest entry first."""
        if not self.can_admit(key, cost):
            return False
        if key in self._entries:
            self._drop(key)
        for victim in sorted(self._entries, key=self._priority):
            if self.bytes + cost <= self.max_bytes:
                break
            if not self._entries[victim].borrowers:
                self._drop(victim)
                self.evictions += 1
        if self.bytes + cost > self.max_bytes:
            return False
        self.bytes += cost
        self.peak_bytes = max(self.peak_bytes, self.bytes)
        try:
            image = make_image()
            if image.isNull() or image.sizeInBytes() != cost:
                raise ValueError("Unexpected Qt image storage size")
            self._entries[key] = ResidentRaster(key, image, raster_scale, cost, coverage)
        except Exception:
            self.bytes -= cost
            raise
        return True
