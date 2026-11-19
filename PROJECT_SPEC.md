# fuPDF — Project Specification

**Status:** source of truth for product behavior  
**Version:** 0.5\
**Date:** 2026-09-01
**Target platform:** Windows 11  
**Distribution:** planned public source release under GNU AGPLv3 (`AGPL-3.0-only`) and portable Windows x64 package. The public repository will have a separate history.

## 1. Purpose

Build a very small, fast desktop application for reviewing PDF proofs.

The application is an **annotator**, not a general-purpose PDF editor. It must open ordinary PDFs, let the user add a deliberately small set of standard PDF annotations and editorial correction comments, navigate and search the document, and save a PDF whose comments preserve their native semantics in Adobe Acrobat. Delete Text, Insert Text and Replace Text must also support the controlled Adobe InDesign Import PDF Comments / Accept workflow defined below. The application never applies these corrections to the original PDF page content.

The priorities, in order, are:

1. **Rendering fluidity and responsiveness.**
2. **Correct native comment semantics, Acrobat compatibility and InDesign applicability for the three editorial corrections.**
3. **Minimal, uncluttered UI.**
4. **Low runtime memory/CPU usage.**
5. Maintainable, small codebase.

If a feature conflicts with these priorities, the feature loses.

---

## 2. Non-goals

The application MUST NOT grow into an Acrobat replacement.

Explicitly out of scope for v1:

- editing or replacing original PDF page content;
- OCR;
- redaction;
- Underline and Squiggly tools;
- Sticky Note, Arrow, Rectangle, Cloud, Stamp and other annotation tools beyond the seven listed in §4.2;
- image editing;
- page insertion, deletion, extraction, rotation or reordering;
- forms;
- signatures;
- bookmarks management;
- cloud storage, accounts, sync or collaboration;
- telemetry or analytics;
- AI/LLM features, agents, semantic document extraction or an AI document semantic model;
- local automation interfaces, direct InDesign integration, InDesign scripting or automatic acceptance of comments;
- conversion to/from Word, images or other formats;
- printing workflows beyond the operating system's normal facilities;
- arbitrary plug-ins or scripting;
- browser/Electron/web-server architecture.

Do not add any of these unless `PROJECT_SPEC.md` is explicitly changed first.

---

## 3. Technology constraints

Preferred stack:

- Python 3.11
- PySide6 / Qt 6 for the desktop UI
- PyMuPDF 1.28.x for PDF rendering, text extraction/search and annotations
- pytest for automated tests

The exact PySide6 and PyMuPDF patch versions must be pinned in `pyproject.toml` after the M0 technical-spike checks pass. Do not silently float to a new major/minor API series.

Runtime requirements:

- local PDF editing, with optional network access for explicitly started post-v1 review;
- no network connection required for reading, manual annotation or saving;
- no web server;
- no database;
- no Electron;
- no browser engine;
- no background services installed in Windows.

### Post-v1 review analysis (M21.3)

An explicitly started review may submit bounded PDF extracts with editorial
instructions to the OpenAI Responses API. The review includes the requested
physical pages and up to one adjacent context page on each side of each batch.
Only target pages count toward coverage. Analysis and selective retry use the
same immutable snapshot while its session remains available.

One application scheduler allows up to two concurrent attempts, subject to
request and token budgets. It keeps valid batch results locally, reports all
requested pages and exports an aggregate using the M20 correction contract.
The editor can still be used offline. M21.3 exposes a service API for analysis.
M21.4 applies the usable proposals after the analysis cycle reaches a complete
or partial terminal outcome. Cancellation or a systemic suspension defers
application. Each applied proposal is one native undoable annotation command.
The user controls Save and Save As. M21.5 opens a non-modal review window from
File, bound to the selected document. The user can choose a registered
editorial profile, physical pages and optional editorial context. Windows
Credential Manager stores the API key for the current user.

### 3.1 PyMuPDF concurrency rule

PyMuPDF must **never be used concurrently from multiple threads within the same worker/document context**. PyMuPDF objects must not be shared across threads or processes.

The application supports multiple PDF documents open at the same time. Each open document owns an **independent dedicated PDF worker process**. That worker owns the document's PyMuPDF `Document` and all related `Page`, `Annot`, `DisplayList` and `TextPage` objects.

The GUI process never shares PyMuPDF objects with workers. Communication occurs only through explicit serializable messages/results. See `ARCHITECTURE.md`.

M21.2 adds offline review preparation only. The existing owner may serialize an immutable temporary snapshot and open one disposable PDF instance backed by that snapshot on the same worker thread. Page selection changes only the disposable instance and writes to a separate extract file. The instance has no writable user session and is closed before the request completes. The source, snapshot and live session are preserved. This exception introduces neither API calls nor a GUI review workflow.

M21.4 uses the same disposable snapshot instance, in read-only use, to localize
one proposal in the owning worker. Live targets are reconfirmed on the current
generation and applied by the existing annotation command path.

A short-lived **validation process** is allowed only for validating a temporary PDF produced during a safe full-save fallback. It must not mutate the live session document, must open only the temporary candidate file, and must terminate immediately after validation. This is not a render pool and does not relax the one persistent worker per open document rule.

## 4. Main window

The UI must remain visually sparse.

Conceptual structure:

```text
┌──────────────────────────────────────────────────────────────┐
│ Open  Save  Save As     [1] [2] [2+cover]      − 100% +    │
│ [proof_01.pdf] [proof_02.pdf] [chapter_03.pdf]               │
│ Find bar appears here only while Ctrl+F is active            │
├────────┬─────────────────────────────────────┬───────────────┤
│        │                                     │ Comments      │
│  Pen   │                                     │               │
│        │                                     │ p. 12         │
│  Text  │               PDF                   │ Highlight ... │
│        │                                     │               │
│  Mark  │                                     │ p. 15         │
│        │                                     │ Callout ...   │
│ Callout│                                     │               │
│ Delete │                                     │               │
│ Insert │                                     │               │
│ Replace│                                     │               │
├────────┴─────────────────────────────────────┴───────────────┤
│ page n / total                                               │
└──────────────────────────────────────────────────────────────┘
```

The exact visual styling may evolve, but the information architecture must not become more complex than this without an explicit spec change.

### 4.1 Document tabs

- The application may keep multiple PDF documents open simultaneously.
- Each open PDF is represented by one document tab.
- Switching tabs must be immediate from the GUI perspective and must preserve each document's independent state, including reading position, zoom, layout mode, sidebar state, search state, selection and unsaved annotation state.
- Closing one document tab must not affect the other open documents.
- A document with unsaved changes must be visibly distinguishable in its tab using a minimal dirty-state indicator.
- The active tab receives rendering/search priority. Inactive tabs must reduce raster-cache usage aggressively and perform no speculative prefetch.


### 4.2 Annotation and correction tools

v1 has exactly seven creation tools, in two semantic groups:

| Group | Mandatory tools |
| --- | --- |
| Annotations / reports (annotazioni/segnalazioni) | Pen, Highlight, Text Box, Callout |
| Applicable text corrections (correzioni testuali applicabili) | Delete Text / Strikethrough Text, Insert Text, Replace Text |

This is a distinction in meaning and the domain model, not a requirement for two separate toolbars. Keep a compact tool surface; do not add other creation tools. A neutral selection/pointer state is allowed for selecting and manipulating comments according to ANN-05; it is not an additional creation tool.

### 4.3 Context controls

Tool-specific controls appear only when relevant. Do not permanently occupy the window with formatting panels.

Allowed controls:

- pen color;
- Highlight color (M23.2), using the compact contextual palette for creation and selection;
- text-box fill color;
- text-box border color;
- text-box font family and size from a compact fixed control;
- callout fill color;
- callout border color;
- plain-text entry for Insert Text and Replace Text.

Text Box font size and family are the sole v1 exceptions: use 6–72 PDF points and only Helvetica, Times and Courier. Do not scan system fonts or embed arbitrary fonts. Border width, pen width and other formatting remain fixed sensible defaults unless a later spec explicitly exposes them.

### 4.4 Color palette

Use a compact preset palette, not an always-visible arbitrary color picker.

Minimum pen/border palette:

- black;
- red;
- orange;
- yellow/gold;
- green;
- blue;
- purple;
- gray.

Minimum fill palette:

- transparent;
- white;
- pale yellow;
- pale red/pink;
- pale green;
- pale blue.

Exact RGB values are implementation details but must be stable once chosen and must map correctly to PDF color values.

Highlight defaults to the existing yellow. M23.2 adds green, light blue, pink
and purple presets, retained per document for the session. The contextual palette
appears beside the existing Highlight action for a confirmed text selection,
or beside a selected Highlight for recoloring. Custom native colors remain
visible without preset substitution. Recoloring changes only native Highlight
color and appearance, preserves geometry, opacity and unrelated metadata, and
uses the existing command/history and Save/Save As paths. Locked or unsafe
native profiles remain read-only; no other annotation type gains color editing.

---

## 5. Functional requirements

Requirement IDs are stable and are referenced by `ACCEPTANCE_TESTS.md`.

### DOC-01 — Open PDF

- Open a local `.pdf` file.
- Display page count and page dimensions quickly without eagerly rasterizing the full document.
- Unsupported or corrupt PDFs must fail cleanly with a concise user-facing error.
- **All encrypted/protected PDFs are rejected in v1**, including files that might technically open without an interactive password prompt. Password/encryption handling is out of scope.

### DOC-02 — Save

- `Ctrl+S` saves changes for the active document.
- Annotation mutations are serialized per document and acknowledged by the worker with a monotonically increasing `document_revision`.
- Save acts as a barrier for a specific confirmed revision. A successful response reports `saved_revision`; the document is clean only if no newer confirmed or pending mutation exists.
- Before writing, perform a best-effort external-change check against the file identity/signature captured at open or last successful save. If the source changed externally, stop and ask the user to resolve it; never overwrite silently.
- Prefer incremental save when PyMuPDF reports that it is safe/available.
- If in-place incremental save is unavailable, perform the safe full-save transaction defined in `ARCHITECTURE.md`: write a temporary sibling candidate, validate it in a short-lived validation process, close the live file handle only when replacement is ready, replace the target, and reopen the final file.
- If replacement fails after the live handle has been closed, the validated temporary candidate must be retained as a recovery copy and the session must remain dirty/recoverable; do not silently delete the only copy containing the user's changes.
- Saving must preserve annotations as native PDF annotations. Never flatten them.

### DOC-03 — Save As

- Save the current confirmed document revision to another PDF path using the same temporary-write + validation discipline as full Save.
- If the destination exists, require explicit overwrite confirmation.
- Save As to a path that is already open in another writable document tab must be rejected.
- The resulting PDF becomes the active document path only after successful validation and final replacement/creation.
- After success, subsequent `Ctrl+S` targets the new path and the worker backing state is reopened/aligned to that path without losing viewer state or undo/redo semantics.

### DOC-04 — Dirty state

- Track unsaved annotation changes independently for every open document.
- Dirty state is revision-based: `dirty = pending mutations exist OR confirmed_revision != saved_revision`.
- A Save response for an older revision must not mark a document clean if a newer mutation was issued or confirmed meanwhile.
- Closing a document tab with unsaved or pending changes prompts: Save / Discard / Cancel for that document only.
- Closing the application with multiple dirty documents must provide a controlled way to resolve each unsaved document without silently discarding changes.
- No autosave in v1.

### DOC-05 — Multiple open documents

- The application supports multiple local PDF documents open simultaneously in tabs.
- Opening another PDF must not require closing the current one.
- Each document has independent viewer state, annotation state, undo/redo history, search state and dirty state.
- Each document owns one independent dedicated PDF worker process.
- A physical/source PDF may have only **one writable session** in the application. Opening an already-open canonical path activates the existing tab instead of creating a second writer.
- Equivalent paths must be canonicalized for this check; where available, store a lightweight file signature/identity to help detect aliases and external replacement.
- Save As to a file already open in another tab is rejected.
- Switching the active tab must not restart or reopen the document unless recovery from a worker failure requires it.
- Closing a document cleanly terminates its worker and releases its caches/resources.
- v1 does not require split-screen viewing of two documents at once; only one document tab is visually active at a time.

### ANN-01 — Freehand pen

- Mouse drag creates a freehand stroke.
- Stroke is previewed immediately while drawing.
- On completion it becomes a standard PDF Ink annotation.
- User can choose the stroke color before drawing.
- Default stroke width is fixed and unobtrusive.
- Multiple disconnected strokes produced by separate gestures are separate undoable actions.

### ANN-02 — Text box

- Drag on the page to define a rectangle.
- Enter **plain user text** directly in the box; v1 exposes no rich-text editor or arbitrary formatting.
- Supports a fill color and an independent border color from the preset palettes.
- Saves as a standard PDF FreeText annotation.
- Internally, a fixed escaped rich-text representation may be generated solely when required by the verified PyMuPDF API to produce the requested border/fill appearance. This is an implementation detail and must not expose HTML/rich-text authoring to the user.
- Default text color is chosen to remain legible against the allowed fill palette.
- User can reopen and edit app-created text boxes before/after saving.
- `Ctrl+Enter` confirms explicitly and `Esc` cancels explicitly. A normal click elsewhere, tool/tab change or comparable UI action attempts confirmation; if it cannot commit, the same editor state remains recoverable rather than being discarded.
- User can move and resize the box, including moving an app-created box to another page of the same document. The final native annotation belongs to exactly one page; transfer preserves annotation identity and is one serialized mutation/revision.
- Font family and size are selectable only from the fixed v1 Base-14 subset in §4.3 and must persist through editor, PDF appearance and overlay rendering.
- Confirmation is blocked while any text would be silently clipped/invisible in the saved PDF appearance; the UI must indicate overflow concisely.

### ANN-03 — Text highlight

- Activate highlight tool and drag across selectable PDF text.
- A highlight gesture is confined to the page on which it starts; v1 does not support one gesture spanning multiple pages.
- The selection preview must follow actual text/character geometry, not merely draw a translucent rectangle.
- Resolve start/end text positions and generate ordered quads from text geometry. Dragging in reverse must select the same logical range in reverse direction.
- Multi-column behavior must be covered by deterministic fixture expectations; a selection whose endpoints are in the same column must not accidentally include text from another column.
- On completion create a standard PDF Highlight annotation using exactly the confirmed quads shown in preview.
- Multi-line selection produces the correct set of quads.
- Highlight must not modify or duplicate page text.
- Scanned/image-only pages simply have no selectable text; no OCR fallback.

### ANN-04 — Text callout

- Create a callout by selecting/placing an anchor point and a text box position.
- Callout consists of a FreeText annotation with a real PDF callout line, not a flattened drawing.
- Supports fill color and independent border/line color from preset palettes.
- User input is plain text only; the same fixed escaped internal rich-text representation allowed for ANN-02 may be used solely to obtain the required PDF appearance.
- User can edit app-created callout text, move/resize the text box and reposition the callout anchor.
- Confirmation is blocked while text would be silently clipped/invisible in the saved appearance.
- Save in a representation that Adobe Acrobat recognizes as an editable FreeText callout.

### ANN-05 — Select/edit/delete

The allowed operations are deliberately type-specific:

| Annotation type | Select | Delete | Move | Resize | Edit text/colors/anchor |
| --- | --- | --- | --- | --- | --- |
| App-created Ink | yes | yes | yes (drag translation only) | no | no (color chosen at creation) |
| App-created Highlight | yes | yes | no | no | color only (M23.2) |
| App-created Text box | yes | yes | yes, including same-document page transfer | yes | text + fill/border colors + font family/size |
| App-created Callout | yes | yes | box + anchor | box | text + fill/border colors + anchor |
| App-created Delete Text / Strikethrough | yes | yes | no | no | no |
| App-created Insert Text | yes | yes | no | no | no (plain text entered at creation) |
| App-created Replace Text | yes | yes | no | no | no (plain text entered at creation) |
| Existing native Highlight (M23.2) | yes | safe native graph only | no | no | color only for safe unlocked profiles |
| Existing native Line (M23.1) | yes | safe native graph only | rigid drag translation | no | read-only properties |
| External annotation not safely representable | yes/read-only | no | no | no | no |

Additional rules:

- selected annotation has a subtle selection indication in the UI;
- annotation geometry changes remain in canonical page coordinates and survive zoom/layout changes;
- moving app-created Ink is a pure translation of its complete path on its original page: it preserves path topology, color, width, opacity, app identity and PDF `/NM`; dragging beyond a page edge clamps the whole path at that edge without distortion or cross-page movement;
- editing controls must not steal global Delete/Undo shortcuts while a text editor has focus: text-local editing takes precedence.
- deleting a correction removes the whole logical comment, not the original PDF text; creation/deletion of a correction is one semantic operation even if its validated encoding needs several associated PDF objects;
- v1 does not add post-creation retargeting or formatting for corrections; a different correction can be created after deleting the old comment.

### ANN-06 — Undo/redo

- `Ctrl+Z` / `Ctrl+Y` operate on **confirmed app mutation commands** in the active document only.
- At minimum cover create, delete, move/resize, text edit and color change where those operations are allowed by ANN-05.
- Mutation commands are serialized per document. Pending commands are not silently reordered around undo/redo or save barriers.
- Undo/redo updates `document_revision`; a saved revision remains the clean point, so undoing back exactly to the saved semantic state may make the document clean again.
- Undo/redo must not affect zoom, scrolling, search state or sidebar collapsed state.

### ANN-07 — Existing annotations

- Existing annotations in an opened PDF must remain preserved on save unless the user explicitly modifies a supported, safely representable annotation.
- The sidebar enumerates annotations that PyMuPDF can identify.
- **App-created annotations and corrections** support exactly the operations listed in ANN-05.
- Existing/external comments corresponding to the seven tools are editable only when the worker can map their properties and any associated objects to the app model without losing material appearance or correction semantics. Otherwise they are listed and shown as read-only.
- Unsupported annotation types are read-only.
- M23.1 recognizes existing native `/Line` separately from Ink and page graphics.
  Its endpoints, color, width, line endings and metadata are available in the
  inventory and existing sidebar detail. Safe native graphs can be deleted;
  valid unlocked standalone Lines can be translated on their original page
  through the existing external command path, with one undoable drag mutation.
  Both endpoints move by the same canonical vector. Authored appearances,
  style and metadata are preserved, apart from modification time. No Line
  creation, resize, rotation or independent endpoint edit is exposed.
  Malformed geometry and dependent/locked profiles retain conservative edit
  restrictions. Acrobat interoperability still requires a manual round trip.
- Never reconstruct or `update()` an untouched external annotation merely to normalize it.
- App-created annotations receive an application-stable identity (e.g. a unique PDF annotation name `/NM`). External annotations with missing/duplicate names receive session identities only unless safely modified.

### ANN-08 — Delete Text / Strikethrough Text

- Select a text range using the confirmed text geometry, including ordered quads; the single-page, reverse-drag and multi-column selection rules of ANN-03 also apply.
- Create a native PDF Strikethrough Text comment representing a request to delete exactly that range. It is not a freehand line or a graphic drawn over text.
- The original PDF text remains unchanged, including after save/reopen.
- Acrobat must recognize the comment as Strikethrough Text; InDesign must import that type and remove exactly the intended source text when the user chooses `Accept` in the controlled PDF-06 workflow.
- Native StrikeOut encoding and importer behavior must pass the future technical spike and manual gates; the appearance alone is not proof of deletion semantics.

### ANN-09 — Insert Text

- Indicate a position between characters in selectable text and enter plain text.
- The command targets a confirmed insertion position in the page text, not an arbitrary page point or a search for a matching string alone. The precise placement interaction is defined in the interactive correction milestone.
- Create a native PDF comment semantically equivalent to Insert Text; do not modify original PDF text or substitute a Text Box/Callout.
- Preserve Unicode characters, accents and plain-text content through save/reopen. Search normalization must not rewrite the inserted text.
- Acrobat must recognize the intended insertion semantics. InDesign must import it as Insert Text and insert the supplied text at exactly that position after `Accept`.
- The concrete PDF encoding is a technical-spike decision, not prescribed here.

### ANN-10 — Replace Text

- Select an original text range and enter replacement plain text.
- Reuse the single-page, reverse-drag, multi-column and confirmed-range rules of ANN-03; preserve both the target and replacement payload.
- Create a native PDF comment semantically equivalent to Replace Text. The PDF's original text remains unchanged; replacement is a requested editorial action, not page-content editing.
- Unicode and accented characters must survive save/reopen without loss or conversion into user-facing rich text.
- Acrobat must recognize Replace Text. InDesign must import it as Replace Text and, after `Accept`, replace exactly the selected source range with the supplied text.
- Do not emulate replacement using a Text Box, Callout or an unverified combination of independent comments. Its concrete encoding must be validated in the technical spike.

### ANN-11 — Common command/service contract

- Every create, permitted update, delete and undo/redo mutation for all seven tools goes through the shared structured command / annotation service contract in `ARCHITECTURE.md` A-06. UI handlers never implement tool-specific PDF writing.
- Commands are independent of their source. A future UI, local automation or AI agent must use the same command domain; this requirement adds no automation, LLM API, agent or semantic document model to v1.
- Text targets include document/generation identity, page index and selection-derived range/quads or insertion-position geometry in canonical coordinates. Selected text alone is insufficient; original text may be included as a consistency check.
- Reject stale or ambiguous targets rather than guessing another occurrence. On pages with no selectable text, do not fabricate corrections or invoke OCR.
- A correction shares the existing per-document revision, dirty, save-barrier and undo/redo rules; it is not a parallel editing subsystem.

## 6. Viewer requirements

### VIEW-01 — Continuous vertical scrolling

- Scrolling is always continuous vertically.
- There is no page-by-page navigation mode.
- Changing layout must not change this behavior.

### VIEW-02 — Single-page layout

Pages are stacked vertically, one page per row.

```text
1
2
3
4
```

### VIEW-03 — Two-page layout

Pages are arranged as spreads, two per row.

```text
1  2
3  4
5  6
```

Odd final pages are left/right aligned consistently according to the layout engine's documented rule.

### VIEW-04 — Two-page layout with cover

The first page is displayed alone as a cover; subsequent pages form spreads.

```text
   1
2  3
4  5
6  7
```

### VIEW-05 — Layout switching

Switching among the three layouts must:

- preserve zoom;
- preserve an approximate reading position using the currently visible page as an anchor;
- invalidate only geometry-dependent UI state, not PDF content state.

### VIEW-06 — Zoom

- Plus/minus controls.
- `Ctrl` + mouse wheel.
- A compact percentage display.
- Sensible geometric zoom limits (recommended initial range: 25%–800%).
- Zoom interaction must feel immediate: scale the current raster instantly and request a definitive rerender only after a short debounce.
- Definitive raster resolution is separately capped by the raster-memory policy; very high geometric zoom must not force a single enormous raster allocation.
- When the cap is reached, Qt may continue geometric scaling until a more detailed bounded render can be produced.
- Zoom never blocks the GUI waiting for a new PDF render.

### VIEW-07 — Current page indicator

Display current page / total pages based on the page with the largest visible area near the reading focus.

---

## 7. Comments sidebar

### SIDE-01 — Collapsible

- Right-side panel.
- Can be collapsed to maximize document space and restored with one action.

### SIDE-02 — Contents

Each annotation entry shows, at minimum:

- annotation type/icon;
- 1-based page number;
- text for FreeText/callout when present;
- short text excerpt for highlights when extraction is available;
- correction kind, target excerpt when available, and inserted/replacement plain text for editorial corrections; one entry represents one logical correction;
- generic label for Ink and unsupported annotations.

### SIDE-03 — Ordering

Default order is document order:

1. page number;
2. vertical position on page;
3. horizontal position as tie-breaker.

### SIDE-04 — Navigation

Clicking an entry:

- scrolls to the page;
- centers or reveals the annotation;
- briefly indicates the target visually.

Navigation must work in all three page layouts and all zoom levels.

### SIDE-05 — Live updates

Create/edit/delete operations update the sidebar without reopening the PDF.

---

## 8. Text search

### SEARCH-01 — UI

- `Ctrl+F` opens a compact find bar.
- Escape closes it.
- Search field, previous result, next result, result counter, close button.
- Enter = next; Shift+Enter = previous.

### SEARCH-02 — Behavior

- Search textual PDF content only; no OCR.
- Unicode matching policy for v1 is **NFKC normalization + Python-style Unicode `casefold()`**, while preserving the original text/geometry mapping used for result coordinates.
- Accents are not stripped: `e` and `è` are distinct unless the source text itself normalizes equivalently.
- Search must not create PDF annotations.
- Results appear as temporary UI overlays.
- Current result is visually distinguishable from other visible results.

### SEARCH-03 — Progressive execution

- Do not extract/index the complete document at open time solely for search.
- Search begins on demand and runs page-by-page or in small slices.
- Results may arrive progressively in document order.
- While incomplete, the result counter must indicate that the total is provisional.
- Long searches must be cancellable/replaced when the query changes.
- Visible-page rendering retains priority over background search work.

### SEARCH-04 — Navigation

- Results are ordered by page, then text order/geometry on the page.
- Next/previous navigates among results already known while search is in progress.
- Global wrap from last→first or first→last is enabled only after the whole-document search for the current query has completed.
- Navigating to a result scrolls it into view and works across all layouts.

## 9. Rendering and performance requirements

Performance is a product feature, not a later optimization pass.

### PERF-01 — Lazy page rasterization

Never rasterize all pages on document open.

At any moment, rasterize only:

- pages currently visible;
- a small prefetch window around them.

Off-screen pages retain geometry placeholders so scroll metrics remain correct.

### PERF-02 — GUI thread rule

The GUI thread performs no heavy PDF rendering, full-document text extraction or blocking file rewrite.

The event loop must remain responsive during:

- scrolling;
- zooming;
- search;
- page render requests;
- save operations where technically possible.

### PERF-03 — One PDF worker per open document

Each open PDF owns exactly one persistent dedicated PDF worker process. That worker owns the live session's PyMuPDF `Document` and all related objects.

The only permitted extra PyMuPDF process is a **short-lived validator** that may open a temporary full-save candidate for validation; it never mutates the live document and exits immediately.

Do not add a multi-process render pool for a single document unless profiling demonstrates a real need and the architecture document is updated first.

For the active document, its worker must support task prioritization/cooperative chunking so that:

1. visible-page render work has highest priority;
2. active interaction/mutation work is not starved;
3. near-page prefetch is lower priority;
4. long search/index operations yield regularly.

Inactive documents must:

- perform no speculative page prefetch;
- release most or all raster pixmaps beyond a very small retained cache;
- avoid background search/index work unless needed to continue an explicit user search;
- keep only lightweight state needed for fast tab restoration.

The number of simultaneously open documents must not cause unbounded raster-memory growth.

### PERF-04 — Render generations and cancellation

Every render request carries enough identity to reject obsolete results, including at least:

- document generation;
- page number;
- target zoom/render scale;
- render generation/request id.

When zoom/layout changes, stale results must be discarded rather than displayed late.

Queued obsolete render work should be cancellable or skippable before expensive execution.

### PERF-05 — Cache

Use bounded caches, never unbounded dictionaries of pixmaps.

At minimum distinguish:

- GUI resident page raster cache;
- raster payloads currently in transit/awaiting adoption by Qt;
- optional DisplayList cache inside each worker;
- text/search cache.

Initial target for the **GUI-resident raster cache**: **256 MiB globally across all open documents**, configurable as an internal constant. This is not a cap on the entire application working set.

Resource accounting must include all app-controlled resident raster images, including scene-held images that remain live after cache eviction. In-flight raster payloads have a separate explicit byte/count bound so IPC cannot queue an unbounded amount of image data.

Define a maximum single-raster allocation. If the requested render scale would exceed it, reduce the raster scale while preserving the requested geometric zoom in Qt. A single page must never be allowed to bypass the resource policy merely because it cannot fit in the normal cache.

DisplayList/text caches inside workers must also be bounded independently.

### PERF-06 — Raster format

For normal opaque PDF page rendering:

- RGB is sufficient;
- alpha channel should be disabled unless a concrete case requires it.

### PERF-07 — Zoom debounce

During active zoom gestures:

- visually scale the current page image immediately in Qt;
- wait approximately 100–150 ms after the last zoom input before requesting the final raster at the new scale;
- replace the temporary scaled raster when the definitive render arrives.

The exact debounce value may be tuned from profiling.

### PERF-08 — Scroll behavior

Fast scrolling must never be blocked waiting for a render.

Allowed transient states:

- cached lower-resolution page;
- neutral page placeholder.

Not allowed:

- freezing the main window until every traversed page has rendered.

### PERF-09 — Initial performance targets

These are engineering targets, not promises independent of hardware/PDF complexity. Benchmarks must record document characteristics and machine information.

For a representative text-heavy 200–400 page PDF:

- document metadata/layout should appear without whole-document rasterization;
- steady scrolling after initial cache warm-up should not visibly stall the UI event loop;
- working-set memory must remain bounded as the user scrolls through the entire document;
- returning to recently viewed pages should usually hit cache;
- a 400-page document must not leave 400 full-resolution page pixmaps resident.

Where useful, automated instrumentation should measure render latency, cache hit rate, cache memory, queued/stale jobs and UI-frame stalls.

### PERF-10 — Multiple-document resource policy

- Only the active document receives normal visible-page rendering and prefetch priority.
- Inactive document raster caches are aggressively trimmed when a tab loses focus.
- Switching back to an inactive document may reuse a small retained raster if available, otherwise it rerenders the visible pages on demand.
- Worker processes for inactive documents remain idle unless explicitly needed; they must not consume sustained CPU merely because their tabs are open.
- Aggregate cache usage across all open documents must be bounded by an application-level memory policy, not merely by a separate unlimited allowance per document.

---

## 10. PDF interoperability requirements

### PDF-01 — Native annotations

Use standard PDF annotation objects:

- freehand → Ink;
- highlight → Highlight with quadrilateral geometry;
- text box → FreeText;
- callout → FreeText with callout geometry/intent supported by PyMuPDF.

Delete Text, Insert Text and Replace Text are native editorial comments subject to PDF-05; their product meanings are mandatory even though their exact encoding requires validation. Never flatten annotations or corrections during normal save.

### PDF-02 — Acrobat compatibility

A PDF saved by the application must open in current Adobe Acrobat Desktop with:

- annotations visible in the correct location;
- text preserved;
- colors preserved within normal PDF viewer tolerances;
- supported annotations shown as comments/annotations rather than page artwork;
- FreeText and callout remaining editable as annotations;
- highlight remaining associated with its text geometry;
- Ink remaining an annotation;
- Delete Text, Insert Text and Replace Text retaining their respective editorial comment semantics, target and plain-text payload where applicable.

Compatibility is an explicit acceptance test, not an assumption.

### PDF-03 — Coordinate integrity

The canonical DTO/page geometry space is **PyMuPDF's unrotated page coordinate system**:

- units: PDF points (1/72 inch);
- origin: top-left of the PyMuPDF page coordinate space;
- +X to the right;
- +Y downward;
- geometry stored independent of current viewer zoom;
- API operations use unrotated page coordinates as required by PyMuPDF;
- visible page bounds are derived from the page's CropBox/rect semantics as verified in M0, with MediaBox retained as metadata where needed.

Annotation geometry must survive:

- non-zero CropBox offsets;
- rotated pages (0/90/180/270 degrees);
- zoom changes and high-DPI display scaling;
- all three viewer layouts.

Keep one documented transformation path between:

1. Qt viewport logical coordinates;
2. scene/displayed-page coordinates;
3. canonical unrotated PyMuPDF page coordinates.

Device-pixel ratio/raster density is not part of canonical geometry. Rotation/CropBox transforms must not be applied twice. Do not scatter ad-hoc conversions through UI classes.

### PDF-04 — Preservation

Normal save must not intentionally alter unrelated page content, fonts, images, metadata or existing annotations.

Untouched external annotations must be preserved without normalization/reconstruction. Full-save fallback may necessarily rewrite PDF structure, but semantic page content and untouched annotation appearance/properties must remain materially equivalent within the explicit acceptance checks.

### PDF-05 — Editorial semantics versus PDF encoding

ANN-08/09/10 define mandatory product semantics. The exact native StrikeOut encoding and the representations for Insert Text and Replace Text must be demonstrated through PyMuPDF-written fixtures, Acrobat recognition and InDesign `Accept` before their production implementation is finalized. No PDF dictionary, subtype/intent combination or grouping for Insert/Replace is assumed to work in advance.

PyMuPDF remains the PDF writer. Any necessary low-level dictionary manipulation must be narrowly contained in the worker's annotation service, documented with its compatibility evidence and covered by persistence/preservation tests. Do not add another PDF library without demonstrated necessity and an explicit dependency decision. A comment that merely looks correct or reopens in PyMuPDF does not satisfy this requirement.

### PDF-06 — InDesign Import PDF Comments / Accept

The three editorial correction types are mandatory v1 interoperability targets. The controlled workflow is: create a test InDesign document → export its PDF → leave the source InDesign document unchanged → annotate the PDF in this app → import through `File > Import PDF Comments` → verify type → manually choose `Accept` → compare resulting source text with the exact expected edit.

Adobe documents `Accept` for Strikethrough Text, Insert Text and Replace Text, and requires the source document to remain unchanged after export for accurate placement. This does not establish compatibility of our writer's output; `ACCEPTANCE_TESTS.md` defines the required manual gates. See [Adobe InDesign: Import PDF Comments](https://helpx.adobe.com/indesign/desktop/collaborate-and-review/share-and-collaborate/import-pdf-comments.html).

Generic-comment import, a wrong type, unavailable `Accept`, a wrong target or lost text is failure. Also verify app → Acrobat → save → InDesign and, where applicable, app → Acrobat → app, without loss of correction semantics. Acrobat success alone never establishes InDesign compatibility. This workflow adds no direct InDesign integration, scripting or auto-accept capability to the application.

## 11. Error handling

Errors must be concise and actionable.

Required cases:

- file not found/unreadable;
- invalid/corrupt PDF;
- encrypted/protected PDF not supported by v1;
- failed save/write permission;
- incremental save unavailable;
- worker process failure/crash;
- out-of-memory/render failure.

A worker crash must not crash the Qt GUI process without a controlled message. Recovery may require reopening the document in v1.

---

## 12. Keyboard shortcuts

Required:

- `Ctrl+O` — Open
- `Ctrl+S` — Save
- `Ctrl+Shift+S` — Save As
- `Ctrl+F` — Find
- `Ctrl+Z` — Undo
- `Ctrl+Y` — Redo
- `Delete` — Delete selected annotation
- `Esc` — cancel current drawing/edit action or close Find when appropriate
- `Ctrl+mouse wheel` — zoom

Additional shortcuts require explicit justification and must not clutter the UI.

---

## 13. Definition of v1 complete

v1 is complete only when:

1. all required acceptance tests pass;
2. comments from all seven tools preserve their native semantics through the required Acrobat round trips, and the three editorial corrections pass InDesign type/`Accept` and round-trip gates;
3. the three continuous-scroll layouts work correctly;
4. search works on normal textual PDFs;
5. sidebar navigation works;
6. no whole-document eager rasterization exists;
7. app-controlled raster/cache/in-flight memory remains bounded during long-document scrolling, with no unexplained sustained unbounded process growth;
8. zoom/scroll remain responsive under normal use;
9. multiple PDFs can remain open simultaneously in independent tabs without cross-document state leakage or unbounded raster-memory growth;
10. no out-of-scope feature has been added.
