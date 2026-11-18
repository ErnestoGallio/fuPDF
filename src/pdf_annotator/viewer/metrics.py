"""Bounded local diagnostics. No PDF text, file paths or telemetry."""

from collections import deque
from contextlib import contextmanager
from time import perf_counter
from typing import Iterator


class ViewerMetrics:
    def __init__(self) -> None:
        self.enabled = False
        self.samples: dict[str, deque[float]] = {}
        self.totals: dict[str, int] = {}
        self.maxima: dict[str, float] = {}
        self.events: deque[tuple[float, str, float]] = deque(maxlen=1024)

    def add(self, name: str, value: float) -> None:
        if not self.enabled:
            return
        self.samples.setdefault(name, deque(maxlen=4096)).append(value)
        self.totals[name] = self.totals.get(name, 0) + 1
        self.maxima[name] = max(self.maxima.get(name, 0), value)
        self.events.append((perf_counter(), name, value))

    @contextmanager
    def span(self, name: str) -> Iterator[None]:
        start = perf_counter() if self.enabled else 0
        try:
            yield
        finally:
            if self.enabled:
                self.add(name, (perf_counter() - start) * 1000)

    def report(self) -> dict:
        result = {}
        for name, values in self.samples.items():
            ordered = sorted(values)
            result[name] = {"count": self.totals[name], "max": self.maxima[name],
                            "p50": ordered[len(ordered) // 2],
                            "p95": ordered[min(len(ordered) - 1, int(len(ordered) * .95))]}
        return result
