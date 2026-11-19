# Personal PDF Annotator — Architecture

**Status:** normative architecture for v1  
**Version:** 0.5\
**Date:** 2026-09-01

## 1. Architectural objective

Keep the GUI responsive while each open PDF is isolated in its own dedicated PyMuPDF worker process.

The architecture deliberately avoids threads for PyMuPDF because the current PyMuPDF documentation states that multithreaded use is unsupported and may cause incorrect behavior or crashes.

The GUI owns multiple independent document sessions. Each session has one persistent worker process for the lifetime of that open document.

```text
┌──────────────────────────── GUI PROCESS ────────────────────────────┐
│ PySide6                                                             │
│                                                                    │
│ MainWindow / document tabs                                         │
│   ├─ DocumentSession A ─ viewer/search/sidebar/undo state           │
│   ├─ DocumentSession B ─ viewer/search/sidebar/undo state           │
│   └─ DocumentSession C ─ viewer/search/sidebar/undo state           │
│                                                                    │
│ DocumentManager                                                    │
│ Global bounded raster-cache controller                             │
│ Per-document IPC clients                                           │
└───────────────┬────────────────┬────────────────┬───────────────────┘
                │                │                │ explicit messages
                ▼                ▼                ▼
        ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
        │ PDF WORKER A │  │ PDF WORKER B │  │ PDF WORKER C │
        │ PyMuPDF Doc A│  │ PyMuPDF Doc B│  │ PyMuPDF Doc C│
        │ sole owner   │  │ sole owner   │  │ sole owner   │
        │ bounded      │  │ bounded      │  │ bounded      │
        │ internal     │  │ internal     │  │ internal     │
        │ caches       │  │ caches       │  │ caches       │
        └──────────────┘  └──────────────┘  └──────────────┘
```

Each worker is the sole owner of its document's `Document`, `Page`, `Annot`, `DisplayList` and `TextPage` objects and performs open/close, metadata, rendering, text/search, annotation CRUD and save operations for that document only.

No PyMuPDF object crosses a process boundary.

---

## 2. Why a process, not QThread/QThreadPool

Do **not** implement rendering by putting PyMuPDF calls into `QThread`, `QRunnable`, `QThreadPool`, Python `threading`, `concurrent.futures.ThreadPoolExecutor`, or equivalent.

PyMuPDF's current documentation explicitly states that it does not support multithreaded use. The correct concurrency boundary is process isolation.

This rule is architectural and must not be relaxed as a local optimization.

Official references:

- https://pymupdf.readthedocs.io/en/latest/recipes-multiprocessing.html
- https://pymupdf.readthedocs.io/en/latest/faq/index.html

---

## 3. Proposed repository structure

```text
pdf-annotator/
│
├─ AGENTS.md
├─ PROJECT_SPEC.md
├─ ARCHITECTURE.md
├─ ACCEPTANCE_TESTS.md
├─ ROADMAP.md
├─ pyproject.toml
├─ README.md
│
├─ src/pdf_annotator/
│  ├─ __init__.py
│  ├─ main.py
│  │
│  ├─ app/
│  │  ├─ main_window.py
│  │  ├─ actions.py
│  │  ├─ app_state.py
│  │  ├─ document_manager.py
│  │  └─ document_session.py
│  │
│  ├─ ui/
│  │  ├─ annotation_toolbar.py
│  │  ├─ comments_sidebar.py
│  │  ├─ search_bar.py
│  │  ├─ status_bar.py
│  │  └─ color_palette.py
│  │
│  ├─ viewer/
│  │  ├─ pdf_view.py
│  │  ├─ page_item.py
│  │  ├─ layout_engine.py
│  │  ├─ viewport_tracker.py
│  │  ├─ coordinate_mapper.py
│  │  ├─ render_scheduler.py
│  │  └─ raster_cache.py
│  │
│  ├─ annotations/
│  │  ├─ models.py
│  │  ├─ overlay_layer.py
│  │  ├─ selection_controller.py
│  │  ├─ pen_tool.py
│  │  ├─ highlight_tool.py
│  │  ├─ freetext_tool.py
│  │  └─ callout_tool.py
│  │
│  ├─ search/
│  │  ├─ models.py
│  │  └─ controller.py
│  │
│  ├─ commands/
│  │  ├─ base.py
│  │  └─ annotation_commands.py
│  │
│  ├─ worker/
│  │  ├─ process.py
│  │  ├─ protocol.py
│  │  ├─ document_service.py
│  │  ├─ render_service.py
│  │  ├─ text_service.py
│  │  ├─ annotation_service.py
│  │  └─ save_service.py
│  │
│  └─ util/
│     ├─ logging.py
│     └─ paths.py
│
└─ tests/
   ├─ unit/
   ├─ integration/
   ├─ fixtures/
   └─ manual/
```

This structure is a target, not permission to create every file immediately. Milestones should add modules only as they become necessary.

---

## 4. Core design rules

### A-01 — One owner per open document

Every user-open PDF has exactly one persistent dedicated worker process, and only that worker may own the live session's writable `pymupdf.Document`.

A one-shot validator process is the sole exception: during full-save fallback it may open the **temporary candidate file** read-only for validation, then it terminates. It is not a second live session owner and must never mutate the session document.

The GUI owns a `DocumentSession` per tab and stores only serializable/session models:

- stable document/session id;
- canonical path and lightweight file signature/identity;
- document generation;
- confirmed/saved revision and pending mutation state;
- page count and canonical page geometry;
- viewer state (scroll position, zoom, layout, sidebar state);
- dirty state and undo/redo state;
- search state;
- annotation DTOs;
- search result DTOs;
- rendered image buffers converted to Qt images.

Closing a tab closes only its worker and releases only that session's resources.

### A-02 — Explicit IPC protocol

GUI ↔ worker communication must use typed/validated message structures, not arbitrary dictionaries scattered throughout the code.

Each request includes at least:

- request id;
- document id/generation;
- operation type;
- operation payload;
- mutation sequence/revision metadata when applicable.

Each response includes at least:

- request id;
- success/error;
- response payload;
- document id/generation;
- confirmed/saved revision where relevant.

Avoid sending live Python exceptions across IPC. Serialize an error code + concise technical message.

**No IPC operation may block the Qt GUI thread.** GUI-side send/receive/join handling must be event-driven, polled by bounded timers, or delegated to non-PyMuPDF transport helpers. Queues/channels and individual raster payloads require explicit size/count limits. A saturated channel must apply backpressure/drop stale render work rather than freeze the event loop.

### A-03 — Document identity and generations

Every open tab has a stable `document_id` / session id. Requests and responses are routed by this id.

At open, `DocumentManager` canonicalizes the path (absolute/resolved + case-normalized on Windows) and records a lightweight file signature such as size + high-resolution mtime + available file identity metadata. Only one writable session may target the same canonical/identified file.

Within a document session, reopening/reloading/replacing the underlying file increments `document_generation`. Zoom/search/render operations may use narrower generation counters where useful.

Every confirmed annotation mutation increments a monotonic `document_revision`; `saved_revision` records the revision durably saved. Pending mutations are tracked separately.

Any response for a closed document id, stale generation, stale render/search generation, or superseded request is ignored.

### A-04 — No heavy GUI work

The GUI process may perform lightweight geometry/layout calculations and Qt painting.

It must not:

- parse complete PDFs;
- render PDF pages;
- run full-document search;
- synchronously rewrite large PDF files.

### A-05 — Bounded resources

All caches, queues and raster transfers must have explicit bounds or eviction/backpressure rules.

The global raster-cache budget is distinct from total working-set memory. Account separately for resident scene/cache rasters, in-flight raster bytes and worker-side caches. Define a maximum single-raster allocation. If the ideal full-page raster does not fit, use bounded regional rendering at target density; degrade only if an individual minimum region still cannot fit its own hard limit.

### A-06 — Shared annotation/correction command and service boundary

Every tool and every permitted mutation follows this logical path:

```text
UI
  → structured annotation/edit command
  → annotation service
  → document worker
  → PyMuPDF/PDF
```

The command domain has no dependency on Qt widgets, input events or the source that submitted it. The annotation service is one logical application service: its submission boundary validates/dispatches DTOs over the existing nonblocking IPC, and its authoritative PDF implementation runs inside the owning document worker. It is not another process and does not move PyMuPDF into the GUI. Worker-side validation, mutation ordering and revision acknowledgement remain authoritative.

Conceptual creation commands are `CreateInk`, `CreateHighlight`, `CreateTextBox`, `CreateCallout`, `CreateDeleteText`, `CreateInsertText` and `CreateReplaceText`. Concrete class names are implementation details. Permitted update/delete and later undo/redo operations use the same command/service boundary. No tool may write PDF dictionaries, call a PDF API or bypass the service from a GUI event handler.

Commands carry document id/generation, request/mutation identity, tool-specific plain data and the revision/selection preconditions needed to reject stale work. Use existing IPC, canonical geometry and per-document serialization; do not introduce a second mutation queue or dirty-state model. The UI collects intent and previews confirmed geometry; the service/worker maps that intent to the native PDF representation and returns confirmed DTOs/revisions.

For text operations, a target is selection-derived data, not just a selected string:

- page index and document/generation identity;
- for Highlight/Delete/Replace, the confirmed text range and ordered quads in canonical unrotated page coordinates;
- for Insert, the confirmed text insertion boundary plus its canonical geometry, sufficient to distinguish before/after adjacent characters;
- selection/request identity and, where useful, original text as a consistency check.

The exact range/caret DTO representation is chosen with the selection implementation; it must reject an ambiguous or stale target rather than search for an arbitrary equal string elsewhere. This does not require full-document semantic extraction or an AI document model. Insert/replacement payloads remain Unicode plain text, independent of search normalization.

The same domain could later receive commands from UI interaction, local automation or an AI agent. v1 implements only the requested UI and test callers: no automation API, LLM integration, agent, semantic model or InDesign scripting. Test injection of a DTO is not permission to expose an automation feature.

This boundary is introduced in the future command/service milestone before interactive annotation tools. Completed M0 disposable probes and the M1/M2 lifecycle/viewer remain historical foundations; this extension does not require retroactive changes to their scope or code.

## 5. Worker execution model

Use **one persistent worker process per open PDF document** in v1.

Reasons:

- PyMuPDF correctness and isolation without multithreading;
- each writable document has one authoritative owner;
- independent PDFs cannot corrupt or leak state into one another;
- a slow operation in one document does not execute inside another document's worker;
- switching tabs does not require reopening the PDF.

The GUI `DocumentManager` creates a worker when a document is opened and terminates that worker when its tab is closed. A worker owns exactly one live session document. For offline review preparation (M21.2), the same worker thread may capture that state into a private immutable temporary file. A bounded extract operation may open one disposable PDF instance backed by the snapshot file, change its in-memory page tree and write to a separate extract file. The disposable instance closes before the operation returns. The source and snapshot files and the live session are preserved. No PDF objects or bytes cross GUI IPC, and no additional PDF process is started.

M21.3 adds the post-v1 analysis service. One `ReviewScheduler` per application
owns the background job loop and two staging slots. A slot materializes at most
one extract (20 MiB PDF), verifies its hash, closes the reader and releases the
file. Request serialization and the single HTTPS POST run outside Qt and the
PDF process. Responses remain tied to the snapshot and attempt that sent them.
Save pauses review IPC admission and lets the targeted review consumer claim
pending file handles before the barrier. Subsequent operations use the current
worker generation with the historical snapshot ID.

The ledger in `%LOCALAPPDATA%\fuPDF\review-jobs` stores the manifest, batch
lineage, individual attempts and validated results in atomic JSON files. The
published result for a batch is its coverage commit. The ledger can rebuild
the M20 aggregate and its separate provenance report after an interrupted
publication. Recovery by resending is explicit and uses the retained snapshot.

M21.4 starts application after closing the analysis cycle and publishing its
report. The PDF worker localizes on a disposable snapshot instance and returns
only a sealed target proof. It compares the live page's content and geometry,
reconfirms the text range or caret, and authorizes one exact command for the
current generation. The Qt bridge submits that command through
`DocumentManager` and `AnnotationService`. A subsequent Save As invalidates
the old authorization and the bridge requests a fresh confirmation after Save.

The application ledger writes mutation intent before submission and records
the adopted result separately from analysis. Proposal identity derives from
job, batch and original proposal index and is carried as the native `/NM`.
Reconciliation requires a verified native group in the same session. An absent
or unverifiable group leaves an uncertain intent and prevents automatic replay.

Each worker executes short jobs from its own priority-aware queue.

### M24.1b linear text extraction

The document worker extracts one page at a time with content-only base_textpage,
locally binds its TextPage to the page for get_text("text", sort=False), and
replaces line breaks with spaces without compressing other spacing. No RAWDICT,
paragraph reconstruction or geometric ordering is used by M24. Export only
pages with physical page/text; retain empty pages. Identity/revision checks,
private-file size/hash verification, bounded IPC and atomic asynchronous
streaming remain internal. Limits fail the export instead of dropping text.
The obsolete paragraph DTOs, extraction IDs and text batch subsystem are
removed. Future M24.2 can consume PageText through StructuredTextWorker.page;
no new batching interface is needed. Existing multimodal localization,
snapshot/live reconfirmation and A-06 remain unchanged.

M24.1c uses domain.structured_text.normalize_page_text only from the M24
worker adapter: alphabetic line-end hyphens are joined before existing line
flattening. Native flags, base_textpage and multimodal matching stay unchanged.

### 5.1 Intra-document priority classes

1. `VISIBLE_RENDER` — current visible page(s)
2. `INTERACTION` — annotation CRUD, text selection needed for active gesture
3. `PREFETCH_RENDER` — nearby pages
4. `SEARCH_SLICE` — one/few pages of an active search
5. `MAINTENANCE` — cache cleanup, non-urgent metadata

A long operation must be decomposed when possible. In particular, full-document search must run page-by-page or in small slices so visible rendering can interleave.

Do not attempt to interrupt an individual MuPDF page render halfway through. Instead prevent obsolete queued work from starting and discard stale completed results.

### 5.2 Active vs inactive document policy

The active document tab receives system-level priority.

For the active document:

- visible rendering is scheduled immediately;
- limited nearby prefetch is allowed;
- active search slices may proceed when they do not delay visible rendering.

For inactive documents:

- do not schedule speculative page prefetch;
- do not keep high-resolution raster pages merely for convenience;
- retain only the minimum state/caches needed for fast restoration;
- background work must not consume enough CPU or memory to degrade the active tab.

The worker process itself remains alive while its tab remains open. Do not close/reopen inactive documents merely to reduce memory.

### 5.3 Cross-document scheduling

Because workers are independent processes, priority must also be enforced in the GUI-side scheduler/document manager. Do not flood every worker with background work at once.

The design must remain correct when several documents are open and requests/responses arrive interleaved.

---

## 6. Rendering pipeline

### 6.1 Page geometry first

On open, worker returns lightweight page metadata sufficient for layout using the canonical coordinate contract:

```python
PageGeometry(
    page_index: int,
    width_pt: float,      # canonical unrotated PyMuPDF page-space width
    height_pt: float,     # canonical unrotated PyMuPDF page-space height
    rotation: int,        # 0/90/180/270 display rotation
    cropbox: PdfRect,
    mediabox: PdfRect,
)
```

The GUI immediately creates virtual page placeholders without rasterizing every page.

`width_pt`/`height_pt` and all DTO annotation geometry use the **unrotated PyMuPDF page coordinate space**. Display rotation, scene placement, zoom and device-pixel ratio are separate transforms owned by `coordinate_mapper.py`.

### 6.2 Virtualized scene

The viewer should use Qt graphics/view primitives (e.g. `QGraphicsView`/`QGraphicsScene` or an equivalent custom virtualized widget) so only nearby pages have raster items.

The layout engine knows the rectangle of every page at the current zoom/layout, but raster images exist only near the viewport.

### 6.3 Render request

A render key should contain at least:

```text
(document_id, document_generation, page_index, render_scale_bucket)
```

The scheduler also associates a request/generation id so obsolete responses are rejectable.

At ordinary zoom, a key without a region identifies a full-page raster. When the estimated ideal full-page RGB raster would exceed the single-raster limit, the scheduler uses deterministic page-local regions at the ideal density. A regional key additionally identifies its stable region. Layout is not part of either key because the same page raster is reusable across layout modes.

Regional rectangles are transient display-space derivatives of canonical unrotated page geometry and page rotation. They are never persisted as PDF geometry. The worker receives a bounded clip in the coordinate space required by the pinned `Page.get_pixmap()` API and returns the exact rendered coverage; annotation/text geometry continues to use canonical unrotated coordinates independently.

### 6.3.1 Full-page versus regional rendering

The mode decision is demand based:

1. compute ideal density from geometric zoom, DPR and the active supersampling profile;
2. estimate the full-page opaque RGB allocation at that density;
3. use a full-page raster when it fits the tested single-raster limit;
4. otherwise render only deterministic regions needed by the visible viewport and limited adjacent prefetch.

The decision is deterministic for page geometry and density, so it does not oscillate with viewport motion. Definitive regional rasters are never intentionally upscaled beyond their target density. During debounce or a missing-region transition, a compatible older full-page raster may be painted temporarily as a preview.

### 6.4 Rendering annotations

Default page rendering should use `annots=False` where practical so page content and interactive annotation overlays are conceptually separate.

PyMuPDF supports suppressing annotations during `Page.get_pixmap(..., annots=False)`.

The application owns visual overlays for the seven annotation/correction tools while they are interactive. A correction preview follows its confirmed text target and is not its saved PDF encoding. Existing unsupported annotations may use a worker-produced annotation preview or another documented fallback; they must never be accidentally deleted or flattened.

Do not solve annotation interactivity by baking annotations into the page raster.

Official reference:

- https://pymupdf.readthedocs.io/en/latest/recipes-images.html#how-to-create-or-suppress-annotation-images

### 6.5 Pixmap format

Render ordinary opaque pages as RGB with alpha disabled.

PyMuPDF documentation notes that omitting alpha reduces RGB pixmap memory by about 25% and can reduce processing work.

### 6.6 GUI image transport

Start with the simplest correct bounded IPC transport for rendered pixel data.

Requirements:

- no temporary image files;
- data is transferred in memory;
- GUI safely owns/copies/adopts the final buffer for the full `QImage` lifetime;
- message includes width, height, stride/format and render key;
- each raster payload is checked against a maximum single-message/single-raster size;
- total raster bytes queued/in transit are bounded;
- obsolete render payloads may be discarded before GUI adoption;
- send/receive and shutdown never block the Qt event loop.

Do **not** prematurely implement shared-memory complexity. If profiling later proves copies are a material bottleneck, introduce a transport abstraction and optimize separately.

### 6.7 DisplayList cache

Inside the worker, `Page.get_displaylist()` / `DisplayList.get_pixmap()` may be used as a bounded parse/render cache **only after profiling shows value**.

If DisplayList is used for the base page raster, its construction/render path must explicitly exclude annotations using the verified pinned PyMuPDF API so it remains consistent with §6.4 (`annots=False` semantics). If that cannot be guaranteed cleanly, do not use DisplayList for the base raster.

DisplayLists must not be shared across processes or threads and must have an independent bounded LRU/equivalent policy.

Official reference:

- https://pymupdf.readthedocs.io/en/latest/displaylist.html

---

## 7. Raster cache and prefetch

### 7.1 GUI raster cache

Cache QImages/QPixmaps under a **global application resident-raster budget**, currently 128 MiB across all open documents.

Every cache key includes `document_id` so raster data can never collide across tabs.

Cache/resource cost is byte based, approximately:

```text
bytes = bytes_per_line * height
```

not number of pages.

The budget accounting must include app-controlled raster images still referenced by the scene/view even if they have been evicted from the lookup cache. Track in-flight raster bytes separately and cap them.

Define `MAX_SINGLE_RASTER_BYTES` (16 MiB after M3 measurement). If the ideal full-page render would exceed it, retain the requested geometric zoom and density but switch to deterministic bounded regions. Full-page and regional entries compete in this same cache and the same in-flight budget. Never allocate an oversized full-page or regional raster; resolution degradation is only the final fallback if an individual minimum region cannot fit.

On tab deactivation, aggressively release that document's high-resolution rasters while retaining at most a small restoration set according to profiling.

### 7.2 Prefetch policy

Render first:

- all pages intersecting viewport;
- then one row/spread immediately ahead in scroll direction;
- then one row/spread behind.

For a page in regional mode this means all regions intersecting its visible clip first, followed by a small deterministic adjacent band in the scroll direction. Do not prefetch the rest of a highly zoomed page.

The exact window may be tuned, but it must remain small.

### 7.3 Fast scrolling

When scroll velocity is high:

- stop expanding prefetch aggressively;
- cancel/skip queued pages no longer near viewport;
- prioritize the eventual destination.

### 7.4 Zoom

Zoom has two layers:

1. **geometric zoom** — immediate Qt transform/UI percentage, allowed across the product zoom range;
2. **raster render scale** — bounded by memory/single-raster policy and quantized/debounced for definitive rerenders.

During input, scale the current raster immediately. After debounce, request the ideal full-page raster when it fits or the required deterministic regions at target density. High-DPI device pixel ratio affects raster density, not canonical PDF geometry. Do not multiply rotation/CropBox, regional boundaries or DPR into persistent annotation coordinates. The future text layer remains independent of both full-page and regional rasterization.

## 8. Layout engine

`layout_engine.py` is pure geometry and should be heavily unit-tested.

Inputs:

- page dimensions;
- zoom;
- viewport width if needed for centering;
- mode: `SINGLE`, `DOUBLE`, `DOUBLE_COVER`;
- gaps/margins.

Outputs:

- scene rectangle for each page;
- row/spread membership;
- total scene bounds;
- page anchor utilities.

No PDF calls inside the layout engine.

### Double layout

```text
row 0: page 0, page 1
row 1: page 2, page 3
```

### Double with cover

```text
row 0: page 0 centered/single
row 1: page 1, page 2
row 2: page 3, page 4
```

Use zero-based indexing internally; UI displays page numbers as one-based.

---

## 9. Coordinate system

This is a high-risk architectural contract and must be centralized.

### 9.1 Canonical PDF page space

All cross-process annotation/text-selection DTO geometry uses **PyMuPDF's unrotated page coordinate space**:

- unit: point (1/72 inch);
- origin: top-left;
- +X right;
- +Y down;
- independent of viewer zoom and device-pixel ratio;
- page API coordinates are unrotated unless an explicitly documented PyMuPDF API says otherwise;
- CropBox/MediaBox values are retained explicitly; visible bounds and offsets are verified against fixtures in M0.

### 9.2 Transform chain

`coordinate_mapper.py` owns the only supported transforms among:

```text
Qt viewport logical coordinates
      ↕
scene coordinates
      ↕
displayed page-local coordinates (rotation applied)
      ↕
canonical unrotated PyMuPDF page coordinates
```

It must account for:

- scene placement/margins;
- geometric zoom;
- page rotation 0/90/180/270;
- CropBox origin/offset and page rect semantics;
- mixed page sizes;
- Qt device-pixel ratio only when converting to/from raster pixels.

Annotation tools, hit testing, search overlays and sidebar navigation must call this mapper rather than reproducing transform math.

Tests must include 0/90/180/270 rotation, non-trivial CropBox offsets and combined rotation+CropBox cases with independent expected coordinates.

## 10. Annotation model

Use serializable data-transfer models independent of PyMuPDF objects. Distinguish annotations/reports (Ink, Highlight, Text Box, Callout) from applicable editorial corrections (Delete Text / Strikethrough, Insert Text, Replace Text). Domain kind expresses the intended tool semantics; it must not be inferred from one PDF subtype alone.

Conceptual model:

```python
AnnotationModel(
    app_id: str,           # stable for app-created annotations
    pdf_nm: str | None,    # PDF /NM when unique/available
    xref: int | None,      # session locator only; never permanent identity
    page_index: int,
    kind: AnnotationKind,
    rect: PdfRect,
    text: str | None,
    stroke_color: Color | None,
    fill_color: Color | None,
    opacity: float,
    geometry: ...,
    origin: AnnotationOrigin,   # APP / EXTERNAL
    readonly: bool = False,
)
```

For supported annotation types store the geometry needed for Qt overlay painting:

- Ink: list of point sequences;
- Existing Line (M23.1): two ordered endpoints in inventory geometry, with
  native line endings and border/color properties in the inventory style;
- Highlight: ordered quads;
- FreeText: rectangle plus fixed Base-14 font family and PDF-point size;
- Callout: text rectangle + callout points;
- Delete Text: confirmed original range and ordered text quads;
- Insert Text: confirmed text insertion boundary/canonical geometry and inserted plain text;
- Replace Text: confirmed original range/quads and replacement plain text.

Text targets follow A-06. Exact PDF locators and any associated-object relationships for corrections are selected by the encoding spike, not prescribed by these DTO examples. If a validated encoding uses several objects, expose one logical correction with stable identity for selection, sidebar, deletion, persistence and undo/redo; never leave independent fragments or identify it only by a transient xref.

App-created annotations receive a unique stable PDF annotation name (`/NM`) when supported. External annotations with missing/duplicate `/NM` receive a session identity; `xref` may assist mapping inside one open generation but is not a durable identity.

Untouched external annotations are preserved in place and must not be reconstructed merely to normalize them. External supported annotations are editable only when all materially relevant properties can be represented without loss; otherwise DTO marks them read-only.

## 11. Annotation implementation

All implementations below are adapters behind A-06. UI previews and gestures produce structured commands; only the worker's annotation service implements the PDF-specific behavior.

### Ink

Use PyMuPDF `Page.add_ink_annot()`.

GUI previews the mouse path as a Qt overlay. The final path is converted once to canonical coordinates and submitted as `CreateInk` through the annotation service to the worker.

App-created Ink may be moved only through an explicit `MoveInk` command carrying a canonical translation delta. During drag, the GUI paints a translated overlay only and sends no IPC. On release, the worker applies one serialized mutation to the native `/Ink` geometry. The translation is clamped as one vector so the complete path remains on its originating page; no point-wise clipping, cross-page transfer or shape deformation is allowed. PyMuPDF 1.28 exposes Ink vertices as read-only and does not translate an Ink rectangle, so the app-owned adapter may replace the underlying `/Ink` object while preserving the stable app `annotation_id` and `/NM`, complete path topology and Ink style. `xref` remains a generation-local locator, not identity.

### Existing native Line (M23.1)

Reuse `EditExternalAnnotation(MOVE)` and `DeleteExternalAnnotation`, the
capability-driven native gesture, bounded native appearance cache and exact
native history snapshots. Hit-test the canonical endpoints in viewport space
with a screen-pixel tolerance. Mouse movement paints a rigid translated native
appearance locally; release submits one mutation through A-06.

The worker translates `/L` and `/Rect` in place, updating only `/M` otherwise.
It never calls `Annot.update()` or replaces the Line/AP, so authored line
endings, captions, resources, colors and metadata survive. The existing
transaction restores the native dictionary on adapter or post-write adoption
failure. Existing generation/revision/save barriers apply. The pinned
`Annot.get_pixmap()` includes page rotation: Line appearance coverage is
derotated to canonical coordinates while its pixels remain display-oriented.

M23.1 explicitly authorizes this preserving translation independently of the
historical M15.1 release registry; it does not add a fabricated Acrobat gate
entry. Manual Acrobat Line interoperability remains a separate unrun gate.

### Highlight

Do not implement as a rectangle.

The worker must derive actual text geometry and create `Page.add_highlight_annot()` from quads.

For text marker annotations, PyMuPDF documentation recommends quadrilateral geometry, including for rotated/non-horizontal text.

Official references:

- https://pymupdf.readthedocs.io/en/latest/page.html
- https://pymupdf.readthedocs.io/en/latest/faq/index.html

### FreeText box

Use the verified PyMuPDF 1.28.x FreeText API path that can persist independent fill and border colors.

The **user model remains plain text**. If PyMuPDF requires `richtext=True` to achieve the appearance, worker code must safely HTML/XML-escape user text and generate a fixed internal style wrapper. No arbitrary rich-text input or formatting controls are exposed. The only exposed font values are Helvetica, Times and Courier at 6–72 PDF points; map them consistently through the fixed `/DA` and `/RC` appearance path without system-font discovery or arbitrary embedding.

Before commit/update, verify that the requested fixed font/style fits inside the rectangle. If the saved appearance would clip text, reject confirmation/update with an overflow result; do not silently lose visible text.

Normal UI focus/tool/tab changes request the same confirmation path as `Ctrl+Enter`; only `Esc` discards editor state. An unsuccessful confirmation restores or retains its editable state and a completed request cannot be submitted twice by repeated focus events.

Move/resize keeps the Qt preview authoritative until its request resolves: while pending, suppress the old confirmed overlay and paint the proposed geometry; success replaces it with the returned model and failure/staleness restores the prior model. A typed `TransferFreeText` moves an app-created FreeText between pages in the same document as one worker mutation: add the destination annotation with the preserved app identity/`/NM`, then remove the source, rolling back the destination on failure. The target rectangle is fully valid for its destination page; no cross-document transfer is permitted.

### FreeText callout

Use the verified PyMuPDF 1.28.x FreeText callout path with real callout geometry and independent fill/border-line color.

The user model remains plain text; the same fixed escaped internal rich-text serialization rule as FreeText box applies when technically required.

The callout must remain one FreeText PDF annotation with native callout geometry/intent, not a separate line plus text box baked into page content.

Validate text fit before commit/update and reject silent overflow.

### Editorial corrections: required meaning, encoding to validate

- **Delete Text / Strikethrough:** a native StrikeOut comment requesting removal of the selected range, with ordered text quads. It must be recognized as Strikethrough Text by Acrobat and by InDesign with working `Accept`.
- **Insert Text:** a native comment requesting insertion of its Unicode plain text at the confirmed text boundary. Its concrete PDF representation remains undecided until the spike.
- **Replace Text:** a native comment requesting replacement of the confirmed range with its Unicode plain text. Neither its dictionary layout nor a combination of PDF objects is assumed to provide this meaning until verified.

These operations never edit original PDF page content. A Text Box, Callout, painted line or an unverified set of independent annotations is not a semantic substitute. No flattening is allowed.

The future encoding spike must produce disposable samples with the pinned PyMuPDF writer, inspect/reopen them, compare with Acrobat-authored reference comments where useful, and run the Acrobat and InDesign type/`Accept` gates. An Acrobat reference is evidence to investigate, not an encoding specification or proof that InDesign will accept our output.

Prefer the verified public API. Any necessary low-level dictionary manipulation stays in a small documented worker adapter with focused persistence, grouping, geometry, text and preservation tests. Do not add a PDF library without a demonstrated need and an explicit dependency decision. If native interoperable encoding cannot be established, stop the affected tools before production implementation and report the blocker; do not silently downgrade their semantics.

If the verified encoding spans associated objects, creation/deletion and undo/redo must treat the whole correction as one logical mutation. Confirmation occurs only for a coherent complete result; a partial failure must not leave an orphaned fragment reported as a successful correction or clean save.

## 12. Text extraction and selection targets

The worker maintains a small bounded `TextPage`/text-geometry cache for visible/recent pages.

Selection contract:

- a gesture is confined to the page where it starts; no multi-page highlight gesture in v1;
- GUI converts drag endpoints to canonical unrotated page coordinates;
- worker resolves endpoints to actual character/caret positions using text geometry, not a rectangular marquee alone;
- reverse drag produces the same logical character range with reversed endpoints;
- worker returns exact selected text plus **ordered quads**;
- GUI previews those exact quads;
- commit creates the Highlight annotation from the same confirmed quads/request identity;
- stale delayed selection results are discarded by gesture/request generation.

For `two_columns.pdf`, tests define exact endpoint/range expectations. A gesture whose endpoints are in the same column must not select intervening text from another column merely because bounding rectangles overlap.

If the pointer leaves the starting page, clamp/continue selection only within that starting page until release. Do not silently create a cross-page selection.

Exact extraction implementation may use words/spans/chars, but selection semantics are acceptance-test driven.

Delete Text and Replace Text reuse this single-page confirmed range/quads contract; they must not invent a second rectangular selection model. Insert Text resolves a boundary between text characters and returns an unambiguous target with canonical geometry. Its precise placement interaction is finalized in the interactive correction milestone using fixture expectations for before/after positions, repeated text and line boundaries. Image-only pages offer no fabricated insertion/range targets and no OCR fallback.

The confirmed target and its request/generation identity pass through A-06 for preview and commit. Do not recompute a different range from the plain-text payload at commit time.

## 13. Search architecture

Search is on-demand and progressive.

### 13.1 Query lifecycle

Every new query gets a `search_generation`.

Results from earlier generations are discarded.

### 13.2 Worker slicing

Do not run one blocking loop across hundreds of pages that prevents visible render jobs from running.

Search one page or a small batch, enqueue continuation, and allow higher-priority render/interaction jobs between slices.

### 13.3 Unicode matching

For v1, normalize both query and extracted searchable text with Unicode **NFKC**, then compare using Unicode `casefold()`.

Do not strip diacritics. Preserve a mapping from normalized-match indices back to original text character geometry so result highlights target the original PDF text.

Results are emitted in document/page text order. While search is incomplete, total count is provisional. Next/previous may traverse known results, but global wrap is enabled only after all pages for the current query have completed.

### 13.4 Search overlays

Search highlights are Qt-only ephemeral overlays.

They are not PDF annotations and never make the document dirty.

---

## 14. Sidebar architecture

The sidebar consumes the same annotation models used by overlays.

It includes all seven domain kinds, representing an editorial correction as one logical entry even when its verified native encoding has associated objects. Insert/replacement text and target excerpts come from the same DTO; unsupported or unsafe-to-map external corrections remain read-only.

It must not independently rescan the PDF.

Worker emits/returns updated annotation lists after:

- open;
- create;
- update;
- delete;
- save/reload if ids/xrefs change.

Sidebar click calls a navigation service using page index + annotation geometry.

---

## 15. Undo/redo

Undo/redo is semantic-command based and isolated per `DocumentSession`.

### 15.1 Mutation ordering

Each mutation request has a per-document sequence id. The worker processes mutation commands serially and acknowledges each with a new monotonic `document_revision` plus resulting annotation DTO(s).

GUI distinguishes:

- `pending_mutations`;
- `confirmed_revision`;
- `saved_revision`.

A command is added to durable undo history only after confirmation. Undo/redo themselves are serialized mutations and increment revision.

### 15.2 Save barrier

A Save request is enqueued as a barrier after all earlier mutation requests for that document. It identifies the revision being saved. A Save result reports `saved_revision`.

If a new mutation was submitted/confirmed after the save barrier, the tab remains dirty even when that Save succeeds.

### 15.3 Command scope

Support inverse semantic state for allowed app-created operations through A-06: create/delete, Ink move, TextBox move/resize/text/color, Callout box/anchor move/resize/text/color. Highlight and the three editorial correction kinds are create/delete only in v1. Undo/redo of a correction preserves its complete native meaning, original target and inserted/replacement text; it never modifies base PDF text or another document's history.

Text-editor-local Ctrl+Z/Delete takes precedence while editing text and must not accidentally dispatch document annotation commands.

## 16. Save strategy

Worker is the live document save authority. Save operations are revision barriers and must first perform the same-file/external-change checks described below.

### 16.1 File identity / external modification

At open and after each successful save, record canonical path plus a lightweight file signature (at least size and high-resolution mtime; include available file identity metadata where practical).

Before Save/Save As overwrite, compare the on-disk target with the expected identity/signature. If it changed externally, abort without overwriting and report a controlled conflict. Continuous file watching or merge is not required.

Only one writable app session may target a canonical/identified source. Reopening an already-open source activates its existing tab. Save As to another open writable document is rejected.

### 16.2 Save in place — incremental path

1. Save is ordered after all earlier mutation commands.
2. Capture the confirmed `target_revision`.
3. Verify source file identity/signature has not unexpectedly changed.
4. If `Document.can_save_incrementally()` is true, perform incremental save using the verified supported API.
5. Refresh file signature and report `saved_revision=target_revision` only after success.

Incremental save is not promised to be crash-atomic; do not claim otherwise.

### 16.3 Save in place — safe full-save fallback

When incremental save is unavailable:

1. create a unique temporary sibling candidate in the same directory;
2. while the live `Document` is still open, save the full confirmed `target_revision` to the candidate;
3. invoke a short-lived validation process that opens **only the candidate**, checks successful parse, expected page count and required smoke invariants, then exits;
4. if writing/validation fails, keep the live document untouched and dirty; delete invalid temporary output when safe;
5. once a candidate is validated and replacement is ready, close the live PyMuPDF `Document` to release Windows file handles;
6. atomically replace the target with `os.replace()` where supported by the filesystem;
7. reopen the final target in the same persistent worker, rebuild session locators/annotation mappings as necessary, increment `document_generation`, refresh file signature, and report `saved_revision`;
8. if replacement fails after the live document was closed, **retain the validated candidate as recovery data**, reopen a recoverable backing document if possible, keep the logical session dirty, and report the recovery path/state. Never silently delete the only copy containing the confirmed changes.

M0 must spike-test the Windows handle/replacement/reopen sequence before production save code is built.

### 16.4 Save As

1. reject destinations already open in another writable tab;
2. if destination exists, require explicit overwrite confirmation from the UI before worker write;
3. write to a unique temporary sibling candidate near the destination;
4. validate candidate in the short-lived validation process;
5. replace/create the destination only after validation;
6. only then transition the live session to the new canonical path, reopen/alignment state as required, refresh identity/signature and generation, and preserve semantic undo/history/view state;
7. subsequent Ctrl+S targets the new path.

Do not casually enable cleanup/garbage/linearization options that could alter unrelated structure.

## 17. Worker failure handling

The GUI must monitor worker liveness and must also support bounded graceful shutdown.

Normal close sequence:

1. stop scheduling new work for the document;
2. cancel/mark obsolete queued render/search jobs;
3. resolve or reject pending mutation/save operations according to close policy;
4. request worker shutdown asynchronously;
5. wait with a bounded timeout **without blocking the Qt event loop**;
6. only force termination as a last resort, after transport channels are no longer being actively written where possible;
7. release pending/in-flight raster buffers and IPC resources.

Tests must cover shutdown while a worker is busy and while raster payloads are pending/saturated.

If one document worker exits unexpectedly:

- mark only that document session as failed;
- stop accepting annotation commands for that document;
- invalidate that document's pending requests;
- show a concise document-specific error;
- keep other open document tabs/workers usable;
- allow close/reopen where safe.

Do not silently restart a worker against a dirty unsaved document unless a future recovery design explicitly guarantees correctness.

## 18. Instrumentation

Use lightweight structured logging in development builds for:

- render request latency;
- render execution latency;
- stale render discard count;
- raster cache hits/misses/bytes;
- worker queue depth by priority and document id;
- active/inactive document transitions;
- per-document and global raster-cache bytes;
- search pages/sec;
- save duration;
- worker errors with document id.

No telemetry. Logs remain local.

Performance instrumentation should be easy to disable for normal use.

---

## 19. Testing architecture

### Unit tests

Pure logic:

- layout engine;
- coordinate transforms;
- cache eviction;
- render scheduling/stale result rejection;
- annotation DTO serialization;
- command/service validation independent of GUI events, including stale/ambiguous text-target rejection;
- search normalization/matching;
- command undo/redo state.

### Integration tests

Use generated PDF fixtures with PyMuPDF:

- add/save/reopen each supported annotation;
- verify annotation type and geometry;
- rotated/CropBox pages;
- incremental save behavior where available;
- worker IPC request/response;
- multiple simultaneous document sessions with independent workers;
- interleaved responses without cross-document state leakage;
- worker failure isolation/error reporting where feasible;
- editorial command persistence, native target/payload preservation and whole-correction lifecycle after save/reopen, including associated objects if required by the verified encoding.

### Manual compatibility tests

Adobe Acrobat compatibility cannot be assumed from internal tests. Use the checklist in `ACCEPTANCE_TESTS.md` on Windows with a current Acrobat Desktop installation. The three editorial correction types additionally require current InDesign Import PDF Comments / `Accept` gates using an unchanged source InDesign document and its exported PDF. Reopening with PyMuPDF or success in Acrobat alone never establishes InDesign compatibility.

The encoding-spike gate precedes the interactive correction implementation; gates must then be repeated with the interactive app and the final production save path, including app → Acrobat → save → InDesign and app → Acrobat → app where applicable. Test-only export of disposable fixtures before production Save exists does not qualify production Save or reopen completed M0–M2 milestones.

---

## 20. Architecture invariants

These are release-blocking invariants:

1. One persistent PyMuPDF worker owns each user-open writable PDF session.
2. The only extra PyMuPDF process allowed is a one-shot read-only validator for a temporary save candidate.
3. No PyMuPDF object crosses process boundaries or is used from multiple threads.
4. GUI thread never blocks on heavy PDF work, IPC send/receive, or worker `join()`.
5. Every async result is routed by document id + generation/request identity; stale work is discarded.
6. All persistent annotation/text/search geometry uses canonical unrotated PyMuPDF page coordinates; DPR/raster scale is never stored as PDF geometry.
7. Raster/cache/in-flight resources are explicitly bounded; one oversized page cannot bypass limits.
8. User text is plain text; fixed escaped rich-text serialization is only an internal FreeText appearance mechanism when required.
9. Native PDF annotation objects are preserved; normal save never flattens them.
10. Untouched external annotations are not reconstructed/normalized.
11. Annotation mutations are serialized and revisioned; dirty state is tied to saved vs confirmed/pending revision.
12. Only one writable app session targets a given source/destination file identity.
13. Encrypted/protected PDFs are rejected in v1.
14. Full-save fallback validates a temporary candidate before replacing the target and retains recoverable data on late failure.
15. All seven tools use the shared source-independent command/annotation-service boundary; no GUI tool bypasses it.
16. Editorial corrections preserve native comment semantics and original PDF content; Insert/Replace are never substituted with Text Box/Callout or flattened graphics.
17. Exact correction encodings require the technical spike and separate Acrobat/InDesign manual gates; success in one reader is not proof of `Accept` interoperability.
18. Future UI/automation/AI sources share one command domain, but automation, AI/LLM integration and semantic document models are outside v1.
