"""Select from the latest viewport, with one ticket and a global raster cache."""

from dataclasses import dataclass
from math import ceil, isfinite
from time import perf_counter
from typing import Callable

from ..app.document_session import DocumentSession, Lifecycle
from ..worker.protocol import MIN_RASTER_BYTES, Operation, Response
from ..worker.render import bounded_scale
from .metrics import ViewerMetrics
from .raster_cache import RasterCache, RenderKey, ResidentRaster
from .raster_image import adopt_rgb
from .regional_render import (Rect, adjacent_regions, full_page_fits,
                              regional_raster_bytes, regions_for_rect, tile_rect,
                              tile_size_for_limit)

# Preserve the M2 constructor/import boundary.
ResidentBudget = RasterCache
ZOOM_DEBOUNCE_MS = 120
RASTER_SCALE_STEPS = 8  # eighths of a physical pixel/point, rounded up for text clarity
QUALITY_SUPERSAMPLING = 1.5
QUALITY_SUPERSAMPLING_FULL_THROUGH = 1.5
QUALITY_SUPERSAMPLING_MID = 1.25
QUALITY_SUPERSAMPLING_MID_ZOOM = 2.0
QUALITY_SUPERSAMPLING_END_ZOOM = 2.5


def supersampling_for_zoom(zoom: float) -> float:
    """Extra raster density only; PDF geometry and DPR remain independent."""
    if not isfinite(zoom) or zoom <= 0:
        raise ValueError("Invalid zoom")
    if zoom <= QUALITY_SUPERSAMPLING_FULL_THROUGH:
        return QUALITY_SUPERSAMPLING
    if zoom <= QUALITY_SUPERSAMPLING_MID_ZOOM:
        progress = ((zoom - QUALITY_SUPERSAMPLING_FULL_THROUGH) /
                    (QUALITY_SUPERSAMPLING_MID_ZOOM - QUALITY_SUPERSAMPLING_FULL_THROUGH))
        return QUALITY_SUPERSAMPLING + (QUALITY_SUPERSAMPLING_MID - QUALITY_SUPERSAMPLING) * progress
    if zoom >= QUALITY_SUPERSAMPLING_END_ZOOM:
        return 1.0
    progress = ((zoom - QUALITY_SUPERSAMPLING_MID_ZOOM) /
                (QUALITY_SUPERSAMPLING_END_ZOOM - QUALITY_SUPERSAMPLING_MID_ZOOM))
    return QUALITY_SUPERSAMPLING_MID + (1.0 - QUALITY_SUPERSAMPLING_MID) * progress


def requested_raster_scale(zoom: float, dpr: float) -> float:
    if not isfinite(dpr) or dpr <= 0:
        raise ValueError("Invalid DPR")
    requested = zoom * dpr * supersampling_for_zoom(zoom)
    return ceil(requested * RASTER_SCALE_STEPS) / RASTER_SCALE_STEPS


@dataclass(frozen=True)
class RenderTicket:
    key: RenderKey
    request_id: int
    epoch: int
    max_bytes: int
    zoom: float
    started: float


class RenderScheduler:
    def __init__(self, session: DocumentSession, budget: RasterCache,
                 changed: Callable[[], None]) -> None:
        self.session, self.budget, self.changed = session, budget, changed
        self.active = False
        self.epoch = 0
        self.visible: tuple[int, ...] = ()
        self.nearby: tuple[int, ...] = ()
        self.dpr = 1.0
        self.zoom = session.ui.zoom
        self.generation = session.document_generation
        self.pending: RenderTicket | None = None
        self.errors: set[int] = set()
        self.adopted_count = self.discarded_count = self.backpressure_count = 0
        self.requested_count = self.completed_count = 0
        self.regional_requested_count = 0
        self.metrics = ViewerMetrics()
        self._lookups: set[RenderKey] = set()
        self._debounce_until = 0.0
        self._prefetch_until = 0.0
        self._viewport_since = perf_counter()
        self._viewport_reported = False
        self._visible_clips: tuple[tuple[int, Rect], ...] = ()
        self._direction = 1
        self._visible_keys: tuple[RenderKey, ...] = ()
        self._prefetch_keys: tuple[RenderKey, ...] = ()
        self._error_keys: set[RenderKey] = set()
        self.interaction_pending = False

    @property
    def targets(self) -> tuple[int, ...]:
        return self.visible + self.nearby

    @property
    def raster_limit(self) -> int:
        # Leave one share for replacing a preview. Oversized all-visible sets
        # converge at lower density rather than allowing a memory exception.
        return min(self.session.worker.budget.single_bytes,
                   self.budget.max_bytes // (max(1, len(self.targets)) + 1))

    def _key(self, index: int) -> RenderKey:
        """Full-page key retained as the preview/ordinary-render compatibility boundary."""
        width, height = self.session.pages[index].displayed_size
        requested = requested_raster_scale(self.zoom, self.dpr)
        scale = bounded_scale(width, height, requested, max(MIN_RASTER_BYTES, self.raster_limit))
        return RenderKey(self.session.document_id, self.session.document_generation, index, scale, self.dpr)

    def requires_regional(self, index: int) -> bool:
        size = self.session.pages[index].displayed_size
        requested = requested_raster_scale(self.zoom, self.dpr)
        limit = max(MIN_RASTER_BYTES, self.raster_limit)
        return (not full_page_fits(size, requested, limit)
                and tile_size_for_limit(limit) is not None)

    def _region_key(self, index: int, region) -> RenderKey:
        return RenderKey(self.session.document_id, self.session.document_generation, index,
                         requested_raster_scale(self.zoom, self.dpr), self.dpr, region)

    def _build_target_keys(self) -> tuple[tuple[RenderKey, ...], tuple[RenderKey, ...]]:
        clips = dict(self._visible_clips)
        visible_keys: list[RenderKey] = []
        prefetch_keys: list[RenderKey] = []
        for index in self.visible:
            if not self.requires_regional(index):
                visible_keys.append(self._key(index))
                continue
            scale = requested_raster_scale(self.zoom, self.dpr)
            tile_px = tile_size_for_limit(max(MIN_RASTER_BYTES, self.raster_limit))
            clip = clips.get(index)
            if tile_px is None or clip is None:
                visible_keys.append(self._key(index))
                continue
            regions = regions_for_rect(self.session.pages[index].displayed_size, scale,
                                       clip, tile_px)
            visible_keys.extend(self._region_key(index, region) for region in regions)
            adjacent = adjacent_regions(self.session.pages[index].displayed_size, scale,
                                        regions, self._direction)
            prefetch_keys.extend(self._region_key(index, region) for region in adjacent
                                 if region not in regions)
        for index in self.nearby:
            # Never prefetch an entire highly zoomed nearby page.
            if not self.requires_regional(index):
                prefetch_keys.append(self._key(index))
        return tuple(dict.fromkeys(visible_keys)), tuple(dict.fromkeys(prefetch_keys))

    @property
    def target_keys(self) -> tuple[RenderKey, ...]:
        return self._visible_keys + self._prefetch_keys

    def visible_region_keys(self, index: int) -> tuple[RenderKey, ...]:
        return tuple(key for key in self._visible_keys
                     if key.page_index == index and key.region is not None)

    @property
    def visible_ready(self) -> bool:
        return bool(self._visible_keys) and all(
            self.budget.get(key, record=False) is not None for key in self._visible_keys)

    @property
    def rasters(self) -> dict[int, ResidentRaster]:
        """Borrowed view of targets, not additional owners; painting uses leases."""
        if not self.active or self.session.worker is None:
            return {}
        result = {}
        for index in self.targets:
            key = next((candidate for candidate in self._visible_keys
                        if candidate.page_index == index), self._key(index))
            raster = self.budget.get(key, record=False)
            if raster is None and key.region is None:
                raster = self.budget.preview(key)
            if raster is not None:
                result[index] = raster
        return result

    @property
    def settled(self) -> bool:
        return (self.active and self.pending is None and perf_counter() >= self._debounce_until
                and perf_counter() >= self._prefetch_until
                and all(self.budget.get(key, record=False) is not None for key in self.target_keys))

    def _cancel(self) -> None:
        self.epoch += 1
        if self.pending is not None and self.session.worker is not None:
            self.session.worker.cancel_renders()

    def clear(self) -> None:
        self.budget.remove_document(self.session.document_id)

    def set_active(self, active: bool) -> None:
        if active == self.active:
            return
        self.active = active
        self._cancel()
        self.errors.clear()
        self._error_keys.clear()
        self._lookups.clear()
        self._viewport_reported = False
        self._viewport_since = perf_counter()
        if not active:
            self.clear()
        else:
            self.budget.set_relevance(self.session.document_id, self.visible, self.nearby,
                                      self._visible_keys, self._prefetch_keys)
        self.changed()

    def defer_zoom(self) -> None:
        self._debounce_until = perf_counter() + ZOOM_DEBOUNCE_MS / 1000

    def defer_prefetch(self) -> None:
        self._prefetch_until = perf_counter() + 0.10

    def set_viewport(self, visible: tuple[int, ...], nearby: tuple[int, ...], dpr: float,
                     visible_clips: tuple[tuple[int, Rect], ...] = (), direction: int = 1) -> None:
        if not isfinite(dpr) or dpr <= 0:
            raise ValueError("Invalid DPR")
        if direction not in (-1, 1):
            raise ValueError("Invalid scroll direction")
        old_state = (self.visible, self.nearby, self.dpr, self.generation, self.zoom,
                     self._visible_keys, self._prefetch_keys)
        old_generation = self.generation
        self.visible, self.nearby = visible, nearby
        self.dpr, self.generation, self.zoom = dpr, self.session.document_generation, self.session.ui.zoom
        self._visible_clips, self._direction = visible_clips, direction
        self._visible_keys, self._prefetch_keys = self._build_target_keys()
        state = (self.visible, self.nearby, self.dpr, self.generation, self.zoom,
                 self._visible_keys, self._prefetch_keys)
        if state == old_state:
            return
        self._cancel()
        self.errors.clear()
        self._error_keys.clear()
        self._lookups.clear()
        self._viewport_reported = False
        self._viewport_since = perf_counter()
        if old_generation != self.generation:
            self.clear()
        if self.active:
            self.budget.set_relevance(self.session.document_id, visible, nearby,
                                      self._visible_keys, self._prefetch_keys)
        self.changed()

    def accepts(self, response: Response) -> bool:
        ticket = self.pending
        return bool(ticket and self.active and self.session.lifecycle == Lifecycle.READY
                    and response.operation == Operation.RENDER
                    and response.document_id == self.session.document_id == ticket.key.document_id
                    and response.document_generation == self.session.document_generation == ticket.key.generation
                    and response.request_id == ticket.request_id and ticket.epoch == self.epoch
                    and response.payload.get("page_index") == ticket.key.page_index
                    and ticket.key in self.target_keys
                    and response.payload.get("geometric_zoom") == ticket.zoom
                    and response.payload.get("dpr") == ticket.key.dpr
                    and response.payload.get("requested_scale") == ticket.key.scale
                    and ((ticket.key.region is None and response.payload.get("clip") is None)
                         or (ticket.key.region is not None
                             and response.payload.get("tile") == [ticket.key.region.column,
                                                                  ticket.key.region.row,
                                                                  ticket.key.region.tile_px]
                             and response.payload.get("clip") == list(tile_rect(
                                 self.session.pages[ticket.key.page_index].displayed_size,
                                 ticket.key.scale, ticket.key.region)))))

    def tick(self) -> None:
        with self.metrics.span("scheduler_ms"):
            self._tick()

    def _tick(self) -> None:
        worker = self.session.worker
        if worker is None or self.session.lifecycle != Lifecycle.READY:
            self.set_active(False)
            return
        if self.generation != self.session.document_generation:
            self.set_viewport(self.visible, self.nearby, self.dpr,
                              self._visible_clips, self._direction)
        if self.pending is not None:
            ticket = self.pending
            delivery = worker.poll(ticket.request_id)
            if delivery is not None:
                self.completed_count += 1
                with delivery:
                    response = delivery.response
                    if not response.success:
                        if ticket.epoch == self.epoch:
                            self.errors.add(ticket.key.page_index)
                            self._error_keys.add(ticket.key)
                    elif not self.accepts(response):
                        self.discarded_count += 1
                    else:
                        p = response.payload
                        cost = ((p["width"] * 3 + 3) // 4 * 4) * p["height"]
                        if cost > ticket.max_bytes or not response.pixels:
                            self.errors.add(ticket.key.page_index)
                            self._error_keys.add(ticket.key)
                        else:
                            try:
                                with self.metrics.span("adoption_ms"):
                                    coverage = (tuple(p["coverage"])
                                                if p.get("coverage") is not None else None)
                                    stored = self.budget.put(ticket.key, cost, p["raster_scale"],
                                                             lambda: adopt_rgb(delivery), coverage)
                                if stored:
                                    self.adopted_count += 1
                                    if ticket.key.page_index in self.visible:
                                        self.metrics.add("request_to_visible_ms", (perf_counter() - ticket.started) * 1000)
                                else:
                                    self.backpressure_count += 1
                            except (ValueError, MemoryError):
                                self.errors.add(ticket.key.page_index)
                                self._error_keys.add(ticket.key)
                self.changed()
            if not worker.is_pending(ticket.request_id):
                self.pending = None
        if self.pending is not None or not self.active or worker.failure:
            return
        if perf_counter() < self._debounce_until:
            return
        for key in self.target_keys:
            index = key.page_index
            raster = self.budget.get(key, record=key not in self._lookups)
            self._lookups.add(key)
            if raster is not None:
                continue
            if key in self._error_keys:
                continue
            if key in self._prefetch_keys and self.interaction_pending:
                break
            if key in self._prefetch_keys and perf_counter() < self._prefetch_until:
                break
            clip = (tile_rect(self.session.pages[index].displayed_size, key.scale, key.region)
                    if key.region is not None else None)
            limit = (regional_raster_bytes(clip, key.scale)
                     if clip is not None else self.raster_limit)
            if limit < MIN_RASTER_BYTES:
                self.errors.add(index)
                self._error_keys.add(key)
                continue
            if not self.budget.can_admit(key, limit):
                self.backpressure_count += 1
                break
            payload = {"page_index": index, "geometric_zoom": self.zoom, "dpr": self.dpr,
                       "render_scale": key.scale, "max_bytes": limit}
            if clip is not None:
                payload.update({"clip": list(clip),
                                "tile": [key.region.column, key.region.row, key.region.tile_px]})
            request_id = worker.submit(Operation.RENDER, payload)
            if request_id is None:
                self.backpressure_count += 1
            else:
                self.requested_count += 1
                self.regional_requested_count += int(key.region is not None)
                self.pending = RenderTicket(key, request_id, self.epoch, limit, self.zoom, perf_counter())
            break
        if (not self._viewport_reported and self._visible_keys
                and all(self.budget.get(key, record=False) is not None for key in self._visible_keys)):
            self.metrics.add("viewport_to_ready_ms", (perf_counter() - self._viewport_since) * 1000)
            self._viewport_reported = True
