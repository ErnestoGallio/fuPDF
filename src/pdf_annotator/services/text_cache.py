"""Byte-bounded LRU for page-text indexes and independently reloadable chunks."""

from collections import OrderedDict
from dataclasses import fields, is_dataclass
import sys
from typing import Any

from ..domain.text import TextChunk, TextPageIndex

GUI_TEXT_CACHE_BYTES = 16 * 1024 * 1024
WORKER_TEXT_CACHE_BYTES = 16 * 1024 * 1024

TextCacheValue = TextPageIndex | TextChunk
TextCacheKey = tuple[str, int, int, str, int]


def text_object_cost(value: TextCacheValue) -> int:
    """Conservative recursive Python-object size, counting shared objects once."""
    seen: set[int] = set()

    def size(item: Any) -> int:
        identity = id(item)
        if identity in seen:
            return 0
        seen.add(identity)
        total = sys.getsizeof(item)
        if is_dataclass(item) and not isinstance(item, type):
            return total + sum(size(getattr(item, field.name)) for field in fields(item))
        if isinstance(item, dict):
            return total + sum(size(key) + size(element) for key, element in item.items())
        if isinstance(item, (tuple, list, set, frozenset)):
            return total + sum(size(element) for element in item)
        return total

    return size(value)


class TextSnapshotCache:
    """Compatibility name for the M6 global chunk/index geometry cache."""

    def __init__(self, max_bytes: int = GUI_TEXT_CACHE_BYTES) -> None:
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("Invalid text cache budget")
        self.max_bytes = max_bytes
        self.bytes = self.peak_bytes = 0
        self.hits = self.misses = self.evictions = 0
        self._entries: OrderedDict[TextCacheKey, tuple[TextCacheValue, int]] = OrderedDict()

    @staticmethod
    def key(value: TextCacheValue) -> TextCacheKey:
        if isinstance(value, TextPageIndex):
            return (value.document_id, value.document_generation, value.page_index,
                    "index", -1)
        if isinstance(value, TextChunk):
            return (value.document_id, value.document_generation, value.page_index,
                    value.page_text_id, value.chunk_index)
        raise ValueError("Invalid text cache value")

    def get(self, key: TextCacheKey, *, record: bool = True) -> TextCacheValue | None:
        entry = self._entries.get(key)
        if entry is None:
            if record:
                self.misses += 1
            return None
        self._entries.move_to_end(key)
        if record:
            self.hits += 1
        return entry[0]

    def index(self, document_id: str, generation: int, page_index: int,
              *, record: bool = True) -> TextPageIndex | None:
        value = self.get((document_id, generation, page_index, "index", -1), record=record)
        return value if isinstance(value, TextPageIndex) else None

    def chunk(self, document_id: str, generation: int, page_index: int,
              page_text_id: str, chunk_index: int, *, record: bool = True) -> TextChunk | None:
        value = self.get((document_id, generation, page_index, page_text_id, chunk_index),
                         record=record)
        return value if isinstance(value, TextChunk) else None

    def put(self, value: TextCacheValue) -> bool:
        key = self.key(value)
        cost = text_object_cost(value)
        old = self._entries.pop(key, None)
        if old is not None:
            self.bytes -= old[1]
        if cost > self.max_bytes:
            return False
        while self._entries and self.bytes + cost > self.max_bytes:
            _, (_, evicted_cost) = self._entries.popitem(last=False)
            self.bytes -= evicted_cost
            self.evictions += 1
        self._entries[key] = value, cost
        self.bytes += cost
        self.peak_bytes = max(self.peak_bytes, self.bytes)
        return True

    def remove_document(self, document_id: str) -> None:
        for key in tuple(self._entries):
            if key[0] == document_id:
                _, cost = self._entries.pop(key)
                self.bytes -= cost

    def remove_page(self, document_id: str, generation: int, page_index: int) -> None:
        for key in tuple(self._entries):
            if key[:3] == (document_id, generation, page_index):
                _, cost = self._entries.pop(key)
                self.bytes -= cost

    def clear(self) -> None:
        self._entries.clear()
        self.bytes = 0

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    @property
    def keys(self) -> tuple[TextCacheKey, ...]:
        return tuple(self._entries)
