# M16 - Multi-document stress and resource hardening

## Scope

M16 is a hardening-only milestone. Its test and benchmark work adds no product
feature, worker architecture, persistence change, or coordinate change. M16-C
also makes a small GUI-side native-appearance cache identity/invalidation fix
in `pdf_view.py` and `external_interaction.py`; it changes no PDF encoding,
worker mutation, save semantics, or canonical coordinates. The work targets
`AT-DOC-05..12`, `AT-PERF-07..13`, `AT-EDIT-04`, and related save/dirty
regressions.

## Instrumentation already present

- `DocumentManager`: open-session registry, active document id, pending-open
  count, worker-start count, lifecycle state, per-document generation,
  revision/saved-revision/history/dirty state, annotation inventory, and close
  retirement.
- `WorkerClient`: one persistent worker per document, process id, stop/failure
  state, active/pending request state, submitted/completed operation counters,
  stale-discard and queued-cancellation counters. `RasterBudget` is the shared
  in-flight raster reservation pool with global bytes/count/peak limits.
- `RasterCache`: global resident bytes/count/peak, hits/misses/evictions,
  document-keyed entries, relevance-based eviction, regional keys, annotation
  appearance keys, and retired-but-borrowed accounting.
- `RenderScheduler`: active flag, viewport/nearby targets, one pending ticket,
  request/completion/adoption/discard/backpressure counts, regional-request
  count, and bounded optional timing metrics.
- `TextSnapshotCache` and `SearchState`: global text-cache bytes/entries and
  per-document progressive-search generation/completion/result state.
- Existing lifecycle/viewer tests already cover targeted areas such as worker
  isolation/close, stale renders, cache limits, inactive raster clearing,
  regional rendering, duplicate paths, and Save As collisions.

## Instrumentation added

`tests/m16_diagnostics.py` exposes `snapshot(manager, views)` for tests and
benchmarks. It is deliberately outside product packages and only reads existing
state. The JSON-serializable snapshot includes:

- global sessions/active id, worker starts, resident raster and in-flight
  budgets, and text-cache accounting;
- per-document lifecycle/generation, revision/dirty/history state, progressive
  search state without query or document text, app/inventory annotation counts,
  worker transport counters, cache ownership, and optional view scheduler state;
- cache counts split into page, regional-tile, annotation-appearance, and
  retired entries; and
- worker reserved-raster counts reconciled to the existing global reservation
  pool.

M16-B also records the existing per-view search controller's pending/requested/
adopted/stale-discard counters. This distinguishes a paused inactive search
from a dropped or misrouted search result without adding a new scheduler.

No metric collection is enabled by the snapshot, no request is submitted, and
no cache lookup is recorded. It is not telemetry and is not reachable from the
application UI.

## Reliable automated measurements

- logical open-session, active/inactive-view, lifecycle, worker, request,
  stale-drop, generation, search-generation, revision/history/dirty, and
  annotation-inventory state;
- global and per-document resident raster/text-cache logical bytes and entries;
- global in-flight raster reservation bytes/count, per-worker reserved count,
  pending request count, and configured limits;
- page versus regional-tile versus native-annotation-appearance cache entries;
- close-time disappearance of a document session, view, cache entries, and
  text-cache entries when no borrowed raster lease remains; and
- process id/stop/failure state as lifecycle evidence, not process-memory
  accounting.

## Unsuitable for strict assertions

- process RSS/working set: measured through the test-only Windows benchmark as
  a report-only metric. Platform/runtime allocator behavior and delayed
  reclamation make exact return-to-baseline assertions invalid; RSS is not a
  strict acceptance assertion.
- wall-clock timing, scheduler ordering beyond existing deterministic priority
  invariants, OS process-exit timing, and Qt deferred-deletion timing.
- exact cache contents after concurrent viewport changes; assert bounds,
  ownership, priority effects, and eventual cleanup instead.
- native worker-internal PyMuPDF object count or canonical child-Form count:
  those objects must remain in the worker and no safe product counter exists.

## Proposed representative M16 workload

1. Open three independent fixtures (short, long/dense, and large/rotated),
   wait only for metadata, then activate/render them in a rotating order.
2. Interleave active-tab scrolling/zoom with progressive searches on different
   documents and serialized mutations/history operations, including one
   editorial logical correction.
3. Include verified external FreeText and FreeTextCallout editing with native
   AP appearance raster requests and canonical child text Form resources; assert
   logical annotation/inventory state and appearance-cache resources remain
   document-scoped through mutation, history, save barriers, and close.
4. Force stale viewport/search responses and controlled worker crash/close while
   raster work is reserved; assert isolation, global bounds, and cleanup.
5. Re-run duplicate-path and Save As-open-target collision checks amid active
   tabs, then close sessions in mixed clean/dirty-discard paths.

## M16-B scenarios implemented

- Six simultaneous real document sessions: two independently copied verified
  M15.1 native-profile PDFs, dense search content, a high-zoom large page, a
  normal document, and rotated/CropBox geometry. Every session has its own
  worker process.
- Interleaved external FreeText move and external Callout anchor mutation,
  progressive search, regional rendering, active/inactive transitions,
  native annotation-appearance rastering, undo/redo, and Save As. The test
  verifies document id, generation, revision/history, dirty, inventory, search
  and cache state remain isolated.
- Active/inactive policy: a search started on the active tab pauses after a
  tab change, then resumes and completes only after reactivation. Inactive
  native-appearance rasters are removed while the active tab owns the current
  resource set.
- Close while a regional raster is reserved, then while an external native AP
  raster is reserved. Late work is harmless; global credits eventually return
  to zero after the surviving tab settles.
- Controlled termination of one of three workers: only that session enters the
  defined failed state, its resources retire on close, and the surviving tabs
  continue rendering normally.
- Direct regression execution for duplicate-path identity, Save As collision
  and external-conflict handling, stale search/raster response rejection,
  global backpressure, and render close cleanup.

## M16-B focused results

- `python -m pytest tests/test_m16_stress.py -q`: `3 passed`.
- `python -m pytest tests/test_m16_diagnostics.py` plus the eight directly
  affected pre-existing regressions: `10 passed`.
- `python -m pytest` for the three directly affected annotation-service
  routing/stale-generation regressions: `3 passed`.
- `git diff --check`: passed.

M16-B found no production bug. Two initial test assumptions were corrected to
match established asynchronous behavior: inactive search is intentionally
paused, and closing an active tab can briefly begin bounded rendering for the
newly activated survivor. Both paths now assert their eventual documented
state, rather than timing-sensitive instantaneous values. M16-C later fixed
the separate GUI-side native AP cache over-invalidation described below.

## M16-C soak and observations

`tests/test_m16_soak.py` is a benchmark-only harness. Its default is two full
cycles; each cycle uses the M16-B six-document workload, including external
FreeText and Callout edit, native AP adoption, Undo/Redo, search, high-zoom
regional render, inactive trimming, Save As, and complete shutdown. It uses
event-loop predicates only, not random sleeps.

Two equivalent isolated cycles were run with the same logical cleanup gate.
After each shutdown the snapshot had zero sessions, no active document, zero
resident raster entries/bytes, zero regional or annotation-appearance ownership,
zero in-flight raster credits, zero text-cache entries/bytes, and no retained
document history/revision/dirty/search state. Worker/session ownership and all
global totals reconciled before and after shutdown.

Validation also ran the default two-cycle harness as one focused test:
`python -m pytest tests/test_m16_soak.py -q` -> `1 passed in 33.27s`.
The two separately reported measurement cycles below use the same harness with
its cycle override set to one so each RSS sample remains visible to the runner.

| Observation | Cycle 1 | Cycle 2 |
| --- | ---: | ---: |
| Six-document open | 3104.2 ms | 2954.5 ms |
| Full close/cleanup | 188.3 ms | 218.1 ms |
| Peak resident raster accounting | 6,833,312 B | 6,833,312 B |
| Peak in-flight raster reservation | 33,554,432 B | 33,554,432 B |
| Ordinary render requests | 2 | 2 |
| Regional render requests | 2 | 2 |
| Search slices adopted | 20 | 20 |
| AP requests, FreeText / Callout | 3 / 3 | 2 / 3 |
| Scheduler backpressure | 0 | 0 |
| Worker stale-discard count | 3 | 3 |

The stale-discard count is expected superseded work from active/inactive and
appearance transitions; there was no cross-document adoption or cleanup leak.
Times are observations only, not acceptance thresholds.

### RSS observation

Windows working-set measurement uses test-only `ctypes` bindings to `psapi`.
It sampled the GUI pytest process and the six live worker processes. RSS is
report-only: exact return to the initial baseline is not asserted.

| RSS sample | Cycle 1 | Cycle 2 |
| --- | ---: | ---: |
| GUI baseline | 66,981,888 B | 66,826,240 B |
| Live GUI + six-worker peak | 443,273,216 B | 443,043,840 B |
| Post-close GUI | 84,287,488 B | 83,292,160 B |

The two equivalent peak samples differ by 229,376 B and post-close samples by
995,328 B. The GUI process retained allocator/runtime memory above its initial
baseline, but the repeated samples do not show sustained cumulative growth.

### Native AP / child-Form lifecycle

M16-B and the soak both exercise verified external FreeText and Callout edit,
native AP adoption, Undo/Redo, inactive trimming, and close. The observable
annotation-appearance cache, inventory/history state, worker lifecycle, and
global resource accounting remain document-scoped and return to the closed
baseline. PyMuPDF child Forms remain worker-private and are neither exposed nor
counted.

### M16-C annotation-appearance invalidation follow-up

Manual Windows stress found a reproducible native-appearance flicker: after
editing, moving, resizing, or styling one external FreeText/Callout, other
visible external FreeText/Callout annotations on the same page briefly
disappeared and were rastered again.

The worker and history layers were already annotation-local. Each
`EditExternalAnnotation` operation (move, resize, text, style, or Callout
anchor) emits `appearance_invalidated_ids` containing only the edited logical
id; external delete/undo/redo likewise identify only their one logical
annotation. The over-invalidation was in the view cache: its native-appearance
key included the document-wide revision. A mutation of A increments that
revision, causing cache lookups for unaffected B/C/D to miss despite their
entries still being resident, which queued new AP requests and left their
authoritative overlay absent until adoption.

The production fix makes the native-appearance key stable per logical native
annotation and retires only the IDs named by the mutation delta. A pending AP
for that same edited ID is invalidated by its existing epoch/cancellation
guard, so a pre-mutation A raster cannot adopt after the change. A pending
unrelated B/C/D request is not cancelled. The committed annotation itself has
no retained old AP: it waits for a fresh worker raster, while unrelated
resident APs remain visible. App-created TextBox/Callout overlays do not use
this external native-AP cache path.

`tests/test_m16_annotation_appearance_invalidation.py` uses one page with
multiple simultaneous visible external APs and verifies move, resize, text,
style, and Callout-anchor edits. For each mutation it freezes the asynchronous
scheduler at the mutation boundary, proves B/C/D cache entries remain adopted
and present in the overlay target set, then records that no B/C/D AP request
is submitted while A receives exactly one fresh request. Existing focused
Callout tests verify that move/anchor now retire A's old AP and that undo/redo
still re-raster only the restored authoritative native state.

### Manual Windows workload

1. Open six PDFs: an ordinary proof, a long/dense text PDF, a large page, a
   rotated/CropBox PDF, and PDFs containing verified external FreeText and
   Callout comments.
2. Scroll the ordinary and dense PDFs, switching tabs frequently; confirm the
   active tab renders first and inactive tabs do not retain high-resolution
   rasters indefinitely.
3. On the large page, zoom until regional tiles are used, then scroll and
   switch tabs while rendering continues.
4. Run a progressive search in the dense PDF, switch away mid-search, then
   return and confirm it resumes with only that document's results.
5. Place at least four verified external TextBox/Callout annotations on one
   visible page. Edit the text, move, resize, and style one TextBox; then move
   a Callout anchor. During each replacement AP request, confirm the other
   annotations remain visibly adopted without disappearing or flashing. Then
   Undo and Redo each operation; the edited annotation may wait for a fresh
   AP, but unrelated annotations must remain visible.
6. In a different tab edit external FreeText, wait for its native appearance,
   then Undo and Redo. Confirm each document's dirty/history/sidebar state
   remains independent.
7. Save As one edited document, close a mix of clean and dirty tabs using the
   normal Save/Discard/Cancel decisions, and reopen the saved output.

Observe responsive interaction, correct active-tab priority, no stale visual or
cross-document state adoption, correct save/close behavior, and no obvious
runaway process or memory growth.

### Adobe status

- Acrobat production round-trip: inherited PASS from M15/M15.1. The M16-C fix
  changes only GUI-side AP cache identity/invalidation; it does not alter PDF
  encoding, mutation, or save behavior. Acrobat re-gating is not required for
  this fix.
- InDesign: NOT RUN.

## Risks and gaps

- Snapshotting per-worker reserved raster credits and per-document retired cache
  entries intentionally uses the existing owner-private collections under their
  owner lock where no public read accessor exists. It duplicates no accounting,
  but a future owner refactor must keep this test harness aligned.
- The current logical resource surface does not expose worker-private PyMuPDF
  object or canonical child-Form counts. M16 exercises their observable
  lifecycle through annotation appearance requests, persistence, worker exit,
  and absence of leaked logical cache/session state rather than exporting PDF
  engine objects or adding telemetry.
- RSS was measured as an optional benchmark/report metric only; it is never a
  strict baseline or acceptance assertion.

## Final M16 status

The M16-C visual/resource bug is fixed and its focused logical cache/request
regressions pass. M16 changed no PDF encoding, worker mutation, save semantics,
or canonical coordinates. Acrobat remains inherited PASS from M15/M15.1, and
InDesign remains NOT RUN.

Final full-suite result from `tmp/m16-full-suite-final2.log`:
`476 passed, 1 skipped in 624.10s (0:10:24)`. The available log does not record
an explicit exit code, so no exit-code value is asserted here.

The automated M16 gate is PASS. The available project evidence does not record
a post-M16-C manual Windows/AP visual retest confirming that unrelated visible
TextBox/Callout appearances remain stable during A's replacement AP request.
Accordingly, final M16 status is **PENDING MANUAL WINDOWS/AP VISUAL GATE**, not
overall PASS. Record that confirmation to close M16 as PASS; do not infer
InDesign status from this result.

Focused validation completed:

- `python -m pytest tests/test_m16_diagnostics.py tests/test_m16_stress.py -q`
  -> `6 passed in 22.71s`.
- `python -m pytest tests/test_m16_soak.py -q` -> `1 passed in 33.27s`.
- `python -m pytest tests/test_m16_annotation_appearance_invalidation.py`
  plus directly affected AP/cache, history/save, app-overlay, worker-isolation,
  and close-cleanup tests -> `35 passed in 60.16s`.
- Manual full suite, recorded in `tmp/m16-full-suite-final2.log`:
  `476 passed, 1 skipped in 624.10s (0:10:24)`.
- `git diff --check` -> passed.
