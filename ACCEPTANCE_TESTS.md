# Personal PDF Annotator — Acceptance Tests

**Status:** release gate for v1  
**Version:** 0.5\
**Date:** 2026-09-01

## 1. Test philosophy

A milestone is not complete because the UI "looks right". It is complete when its relevant acceptance tests pass.

Automate what can be automated. Keep Adobe Acrobat interoperability and InDesign Import PDF Comments / Accept as separate explicit manual gates. No new test in this revision is implicitly passed by completed M0–M2 evidence; their historical scope and criteria are unchanged.

Record for manual performance runs:

- application build/commit;
- Windows version;
- CPU/RAM;
- PDF filename or fixture id;
- page count;
- file size;
- whether PDF is image-heavy or text-heavy.

---

## 2. Required fixtures

Create deterministic local test PDFs under `tests/fixtures/` or generate them during tests.

Minimum fixture set:

- `simple_text.pdf` — 5 pages, normal horizontal text;
- `simple_text_alt.pdf` — distinct 5-page text PDF used for multi-document isolation tests;
- `multiline_text.pdf` — paragraphs crossing multiple lines;
- `mixed_page_sizes.pdf` — A4 + landscape + differently sized pages;
- `rotated_pages.pdf` — pages rotated 90/180/270 degrees;
- `cropbox_offset.pdf` — CropBox not identical to MediaBox / non-trivial origin where feasible;
- `two_columns.pdf` — two-column text for selection/search behavior;
- `image_only.pdf` — scanned/image-only page with no text layer;
- `long_text_300p.pdf` — generated 300-page text PDF for virtualization/search/performance;
- `existing_annots.pdf` — includes app-like supported annotations, externally styled FreeText/callout variants, duplicate/missing `/NM` cases, and at least one unsupported annotation type;
- `incremental_save_ok.pdf` — normal PDF capable of incremental save;
- `damaged_or_repaired.pdf` — internal save-service fixture whose state prevents incremental save if reliably producible; it need not be accepted by normal UI open policy.
- `encrypted.pdf` — encrypted/protected fixture that v1 must reject.
- `large_page.pdf` — page large enough that a high zoom request would exceed the configured single-raster limit.
- `editorial_targets.pdf` — deterministic selectable text, repeated strings, multiline/two-column ranges and exact caret/range expectations; include rotated/CropBox variants for automated correction geometry checks and Unicode/plain-text payloads such as `caffè & tè <ok>`.
- `editorial_source.indd` + its exported `editorial_export.pdf` — manually prepared InDesign fixture pair, with recorded fonts/export settings, exact source text and expected edits. The source remains unchanged after export until the tested Accept action. A PyMuPDF-generated PDF cannot substitute for this pair in the InDesign gate.
- Acrobat-authored correction reference PDFs derived from the same export may be retained for encoding comparisons and a control import; these are distinct from app-generated samples and do not establish that our writer passes.

---

## 3. Document tests

### AT-DOC-01 — Open normal PDF

**Covers:** DOC-01

1. Launch app.
2. Open `simple_text.pdf`.
3. Verify page count.
4. Verify first page appears.

**Pass:** correct page count; no error; no full-document rasterization.

### AT-DOC-02 — Open long PDF lazily

**Covers:** DOC-01, PERF-01

1. Instrument render requests.
2. Open `long_text_300p.pdf`.

**Pass:** initial opening does not request/render all 300 page pixmaps. Only visible/nearby pages are requested.

### AT-DOC-03 — Unsupported/encrypted/corrupt file handling

**Covers:** DOC-01, error handling

Open representative unreadable/invalid/corrupt input and `encrypted.pdf`.

**Pass:** controlled concise error; application remains usable; no crash/hang.

### AT-DOC-04 — Dirty document-tab close prompt

**Covers:** DOC-04

1. Open two PDFs.
2. Create one annotation in the first document.
3. Close only the dirty document tab.

**Pass:** Save / Discard / Cancel prompt applies only to that document; the second tab remains open and unaffected; each branch behaves correctly.

### AT-DOC-05 — Multiple PDFs remain open

**Covers:** DOC-05

1. Open `simple_text.pdf`.
2. Open `simple_text_alt.pdf` without closing the first.
3. Switch repeatedly between both tabs.

**Pass:** both documents remain open; switching does not reopen/restart either document; each tab shows the correct PDF and page count.

### AT-DOC-06 — Independent per-document state

**Covers:** DOC-04, DOC-05

1. Open two PDFs.
2. In PDF A set a distinct page/zoom/layout, run a search and create an unsaved annotation.
3. In PDF B set different page/zoom/layout/search state and create a different unsaved annotation.
4. Switch between tabs.

**Pass:** each document restores its own reading position, zoom, layout, search/sidebar state, dirty state and annotation state; no state leaks between tabs.

### AT-DOC-07 — Independent undo/redo histories

**Covers:** DOC-05, ANN-06

1. Create annotation A in PDF A.
2. Create annotation B in PDF B.
3. Undo while PDF B is active.
4. Switch to PDF A.

**Pass:** only annotation B is undone; PDF A's history and annotation A are unchanged.

### AT-DOC-08 — Close one document releases only its resources

**Covers:** DOC-05

1. Open at least three PDFs.
2. Record their worker/session identifiers.
3. Close the middle tab.

**Pass:** only the closed document's worker terminates and its raster entries are released; remaining tabs/workers continue normally.

### AT-DOC-09 — Application close with multiple dirty documents

**Covers:** DOC-04, DOC-05

1. Open two PDFs and modify both.
2. Close the application.

**Pass:** the application provides a controlled Save / Discard / Cancel resolution for each dirty document and never silently discards either document's changes.

---

### AT-DOC-10 — Duplicate source activates existing tab

**Covers:** DOC-05

1. Open `simple_text.pdf`.
2. Attempt to open the same file again using the same and an equivalent/case-varied path where feasible on Windows.

**Pass:** no second writable session/worker is created; the existing tab is activated.

### AT-DOC-11 — Save As cannot target another open document

**Covers:** DOC-03, DOC-05

Open PDF A and PDF B, then attempt Save As from A to B's canonical path.

**Pass:** operation is rejected before writing; both sessions/files remain unchanged.

### AT-DOC-12 — External modification conflict

**Covers:** DOC-02

Open a fixture, then modify/replace its on-disk file externally in a controlled test before Save.

**Pass:** Save detects the signature/identity change and aborts without overwriting the external version; session remains dirty.

## 4. Annotation tests

### AT-ANN-01 — Ink creation and persistence

**Covers:** ANN-01, PDF-01

1. Open `simple_text.pdf`.
2. Select red pen.
3. Draw a curved stroke.
4. Save As.
5. Reopen saved PDF using PyMuPDF test code.

**Automated pass:** an Ink annotation exists on expected page with non-empty vertices and red-equivalent stroke color.

### AT-ANN-02 — Ink position and movement across transforms

**Covers:** ANN-01, ANN-05, PDF-01, PDF-03

1. Draw stroke at 100%.
2. With Pen off, select it and drag it to a new position; verify that mouse moves send no annotation IPC and mouse release sends exactly one typed mutation.
3. Repeat at 100%, 150%, 400% and 800%, rotations 0/90/180/270, non-trivial CropBox offsets, all three layouts and a right-hand double-layout page.
4. Drag beyond each page edge.
5. Export/reopen and compare the native annotation.

**Pass:** stroke remains anchored to identical page content with no zoom/layout/rotation/CropBox/DPR drift. Its path is translated by the canonical delta only; point count/topology, color, width, opacity, `annotation_id` and `/NM` remain unchanged. The path clamps as a whole at a page edge and remains on its original page. The real `/Ink` path is updated after reopen; base page content is unchanged, there is no flattening, and only successful release increments revision once. Esc, tab/close, stale/duplicate and worker-failure cases restore the prior confirmed geometry without a phantom revision.

### AT-ANN-03 — Text box creation

**Covers:** ANN-02

1. Drag a text box.
2. Enter `Testo di revisione`.
3. Choose pale-blue fill and red border.
4. Verify the editor immediately shows chosen fill, border, text color, Base-14 family and PDF-point size; confirm with `Ctrl+Enter`, then repeat by click/tool/tab auto-confirm.
5. Save/reopen.

**Automated pass:** FreeText annotation exists; plain user text, rectangle, independent fill and border colors, fixed font family and PDF-point size persist within tolerance through `/DA` and `/RC`. Auto-confirm emits one mutation only; `Esc` alone cancels; a rejected/overflow confirmation retains editable text and geometry. The implementation may use fixed internal escaped rich-text serialization, but no user rich-text semantics are introduced.

### AT-ANN-04 — Text box edit/move

**Covers:** ANN-02, ANN-05

1. Create text box.
2. Edit text.
3. Move and resize box; hold the new Qt geometry pending worker acknowledgement and verify no old-geometry flash.
4. Move the app-created box to a second page in every layout and rotation/CropBox fixture.
5. Save/reopen.

**Pass:** edited text and final geometry persist; sidebar updates. Same-document page transfer preserves `annotation_id`, `/NM`, text, style and one-revision lifecycle, leaving exactly one native FreeText on the destination page. Failure/stale transfer restores the original page/geometry without a phantom revision.

Overflow subcase: shrink the box until the fixed saved appearance cannot display all text.

**Pass:** confirmation/save of that edit is blocked with a concise overflow indication; no silently invisible text is committed.

### AT-ANN-05 — Single-line highlight

**Covers:** ANN-03

1. Highlight a known exact character range in one line, once left→right and once right→left.
2. Save/reopen.

**Automated pass:** Highlight annotation exists and uses quads corresponding to the same expected selected text range in both drag directions.

### AT-ANN-06 — Multi-line highlight

**Covers:** ANN-03

1. Highlight a known range crossing at least three lines in `multiline_text.pdf`.
2. In `two_columns.pdf`, select a known range whose endpoints both lie in the same column.
3. Attempt to drag outside the starting page before release.

**Pass:** line quads match the expected text; no large rectangular block spans margins; same-column selection does not pull text from the other column; selection remains confined to the starting page.

### AT-ANN-07 — Image-only page

**Covers:** ANN-03

Attempt highlight on `image_only.pdf`.

**Pass:** no fake text selection, no OCR, no crash; UI indicates/behaves as no selectable text.

### AT-ANN-08 — Callout creation

**Covers:** ANN-04

1. Place callout anchor near text.
2. Position text box away from anchor.
3. Enter `Verificare questo dato`.
4. Choose pale-yellow fill and blue border.
5. Save/reopen.

**Automated pass:** one FreeText annotation exists with native callout geometry/intent; plain user text and independent colors persist.

Overflow subcase: reduce the callout box until text cannot fit.

**Pass:** commit is blocked rather than silently clipping saved text.

### AT-ANN-09 — Callout edit/move anchor

**Covers:** ANN-04, ANN-05

Edit text, move box and move anchor.

**Pass:** final geometry persists after save/reopen and sidebar navigation targets it.

### AT-ANN-10 — Delete

**Covers:** ANN-05, ANN-07

For app-created Ink, Highlight, TextBox and Callout: select and delete.

**Pass:** removed visually, removed from sidebar, absent after save/reopen.

For an external annotation marked read-only: attempt Delete/move/edit.

**Pass:** destructive/edit operations are unavailable or rejected; annotation remains unchanged.

### AT-ANN-11 — Undo/redo

**Covers:** ANN-06, DOC-04

For each operation allowed by ANN-05 (create/delete; Ink move; TextBox move/resize/text/color; Callout box/anchor move/resize/text/color):

1. perform action and wait for worker confirmation;
2. undo and verify previous semantic state;
3. redo and verify action state;
4. save an intermediate revision;
5. perform a new mutation while/during a controlled delayed Save response.

**Pass:** commands remain per-document and ordered; saved revision is tracked correctly; an older Save response never clears dirty state for a newer mutation; view/search state is unchanged.

### AT-ANN-12 — Existing annotation preservation

**Covers:** ANN-07, PDF-04

1. Open `existing_annots.pdf` containing externally styled supported and unsupported annotations.
2. Record type, text, geometry, `/NM` where present, colors/appearance-relevant fields and a rendered/semantic preservation baseline for untouched annotations.
3. Add one new app annotation.
4. Save As.
5. Reopen and compare.

**Pass:** untouched external annotations remain materially equivalent and are not normalized merely by opening/saving. External annotations that cannot be represented losslessly are read-only; missing/duplicate `/NM` does not cause identity collisions.

### AT-LINE-01 — Existing native Line recognition and selection (M23.1)

Use synthetic horizontal, vertical, diagonal and reversed `/Line` annotations,
with native endpoints, thin/dashed strokes, line endings, metadata and optional
popups; include missing AP, rotations 0/90/180/270 and nonzero CropBox offsets.
Verify separate Line inventory/sidebar entries and read-only properties, exact
native appearance coverage, and segment hit testing with screen-pixel tolerance.
Clicks inside an empty diagonal bounding area must not select the Line. Ink
and page-content lines remain distinct. No creation/endpoint/resize tools.

### AT-LINE-02 — Native delete/history/persistence (M23.1)

Delete through the ordinary selected-annotation command. Verify removal from
viewer/inventory/sidebar and PDF, including an owned Popup. Undo/redo must
restore/remove the exact graph, including after Save As followed by Save.
Unsafe incoming relationships remain protected by the existing graph policy.

### AT-LINE-03 — Rigid drag/history/preservation (M23.1)

At 100/150/400/800%, all rotations and all layouts, drag a selected Line. Mouse
moves send no mutation; release sends one external MOVE command/revision.
Both endpoints receive the same canonical delta: length and inclination are
unchanged. Save/Save As/reopen preserves native Line type, endpoints, style,
endings, author, metadata and original AP streams/resources. Only `/L`, `/Rect`
and `/M` may change. Undo/redo restores the exact native before/after state.
Esc, a return to the press position, stale/duplicate commands and injected
post-write failure leave no partial geometry or phantom successful history.

### AT-LINE-04 — Manual Acrobat round trip (M23.1)

NOT RUN until recorded with current Adobe Acrobat: author several Lines there
(thin, dashed, decorated, with comments), open/select/move/delete in fuPDF,
undo/redo, Save and Save As, reopen in both applications. Check endpoint
positions, line endings, comments/author, native editability and an untouched
Ink/editorial control. Automated PyMuPDF reopen is insufficient for this gate.

### AT-CMD-01 — Shared source-independent command/service path

**Covers:** ANN-11, architecture A-06

Verify the domain equivalents of CreateInk, CreateHighlight, CreateTextBox, CreateCallout, CreateDeleteText, CreateInsertText and CreateReplaceText, plus the permitted update/delete and undo/redo commands.

**Pass:** GUI events produce structured DTOs submitted through the same annotation service contract used by direct test callers. Commands depend on no QWidget/input event; UI code contains no PDF encoding logic. Worker routing, generation checks and per-document revision acknowledgement remain authoritative. Wrong-document/stale commands fail without mutations; a rejected command does not create successful history or falsely clear dirty state.

At the command foundation milestone, validate DTOs and dispatch with test doubles; exercise real persistence incrementally as each adapter is implemented. Direct test callers do not imply a production automation/AI API. The full v1 test covers all seven tools and must expose no bypass path.

### AT-EDIT-01 — Delete Text / Strikethrough persistence

**Covers:** ANN-08, ANN-11, PDF-01/03/04/05

1. Select a known exact character range in `editorial_targets.pdf`, in both drag directions.
2. Include multiline/same-column ranges, a repeated string at a different location, and rotated/CropBox cases with independent expected coordinates.
3. Commit through CreateDeleteText; compare confirmed preview quads and persisted target.
4. Save/reopen using the encoding established by the spike; compare original base text/content before and after.

**Automated pass:** a native StrikeOut comment targets exactly the expected ordered quads/range, not a drawn line or page-content edit. Native type, target geometry and deletion intent survive reopening; original PDF text is unchanged. This is necessary but insufficient for interoperability: AT-ACROBAT-06 and AT-INDESIGN-01 must also pass.

### AT-EDIT-02 — Insert Text persistence

**Covers:** ANN-09, ANN-11, PDF-03/04/05

1. Resolve known insertion boundaries between characters, including before/after repeated words and line boundaries; record the exact expected position.
2. Enter Unicode plain text including accents, spaces and literal `&`, `<`, `>` characters.
3. Commit through CreateInsertText, then save/reopen and inspect using the verified encoding.

**Automated pass:** insertion position and full plain-text payload survive without normalization loss or markup interpretation; original PDF text remains unchanged. The saved native comment carries the insertion semantics proven by the spike, not a Text Box/Callout. Require AT-ACROBAT-07 and AT-INDESIGN-02 independently.

### AT-EDIT-03 — Replace Text persistence

**Covers:** ANN-10, ANN-11, PDF-03/04/05

1. Select a known range, including reverse/multiline/same-column cases and a repeated occurrence elsewhere that must remain untargeted.
2. Enter replacement text such as `revisione più chiara` and a Unicode/literal-markup-character case.
3. Commit through CreateReplaceText; save/reopen and inspect the complete logical correction using the verified encoding.

**Automated pass:** original range/quads and replacement plain text are preserved exactly; original PDF page content remains unchanged. The saved comment retains replacement semantics rather than a generic note or unrelated deletion/insertion fragments. Require AT-ACROBAT-08 and AT-INDESIGN-03 independently.

### AT-EDIT-04 — Correction targeting and complete logical lifecycle

**Covers:** ANN-05/06/07/08/09/10/11, DOC-04/05, PDF-04

For each editorial correction type:

- reject a stale selection/document generation, an ambiguous target and an image-only page without creating a comment, guessing another text occurrence or invoking OCR;
- keep gestures/ranges on their starting page; confirm that preview and commit use the same target;
- create/select/delete the complete logical correction; after undo/redo is available, undo/redo creation and deletion while another PDF has its own independent history;
- save/reopen after creation, deletion and undo/redo; if the verified encoding has associated objects, verify whole-comment identity and absence of orphaned fragments after failure/deletion;
- preserve untouched external correction semantics and text, marking unsafe mappings read-only instead of reconstructing them.

**Pass:** one confirmed logical operation has one coherent history/revision outcome; no partial or cross-document mutation is reported as success. Existing dirty/save-barrier rules apply. The sidebar later lists one logical correction and its target/payload, and deletion never deletes base PDF text. Each milestone runs the applicable subcases; full coverage is required before v1 release.

## 5. Layout/view tests

### AT-VIEW-01 — Single continuous

**Covers:** VIEW-01, VIEW-02

Open 5+ page fixture and scroll from page 1 to last page.

**Pass:** one page per row; continuous vertical scroll; no page snapping/navigation mode.

### AT-VIEW-02 — Double continuous

**Covers:** VIEW-03

Expected grouping for 6 pages:

```text
1 2
3 4
5 6
```

**Pass:** exact grouping and continuous vertical scroll.

### AT-VIEW-03 — Double with cover

**Covers:** VIEW-04

Expected grouping for 6 pages:

```text
  1
2 3
4 5
6
```

**Pass:** first page isolated; subsequent spreads correct.

### AT-VIEW-04 — Mixed page sizes

**Covers:** VIEW-02/03/04

Use `mixed_page_sizes.pdf` in each mode.

**Pass:** pages do not overlap; rows remain centered/aligned according to layout rules; scrolling bounds correct.

### AT-VIEW-05 — Layout switch anchor

**Covers:** VIEW-05

1. Scroll so page 120 is primary visible page.
2. Switch Single → Double → Double Cover → Single.

**Pass:** user remains approximately at page 120; no jump to document start.

### AT-VIEW-06 — Zoom interaction

**Covers:** VIEW-06, PERF-07

1. Rapidly zoom with Ctrl+wheel.
2. Observe immediate temporary scaling.
3. Stop input.

**Pass:** UI reacts immediately; definitive raster replaces scaled preview after debounce; no visible old-scale late render replaces newer one.

### AT-VIEW-07 — Zoom geometry integrity

**Covers:** PDF-03

With an annotation visible, zoom through several levels.

**Pass:** annotation remains aligned with same page content.

### AT-VIEW-08 — Current page indicator

**Covers:** VIEW-07

Scroll slowly through page boundaries.

**Pass:** indicator changes to page that is materially in reading focus; never shows 0-based numbering.

---

## 6. Sidebar tests

### AT-SIDE-01 — Collapse/restore

**Covers:** SIDE-01

**Pass:** sidebar collapses and restores without changing document position.

### AT-SIDE-02 — Inventory/order

**Covers:** SIDE-02, SIDE-03

Create annotations on multiple pages and positions.

**Pass:** entries show correct type/page/text/excerpt and sort in document order.

### AT-SIDE-03 — Click navigation

**Covers:** SIDE-04

From far away in document, click annotation on another page.

**Pass:** viewer scrolls to/reveals correct annotation in all three layouts.

### AT-SIDE-04 — Live update

**Covers:** SIDE-05

Create/edit/delete annotations.

**Pass:** no reopen/rescan required; sidebar changes immediately.

---

## 7. Search tests

### AT-SEARCH-01 — Open/close find bar

**Covers:** SEARCH-01

- Ctrl+F opens and focuses field.
- Escape closes it.

**Pass:** correct behavior; document unchanged.

### AT-SEARCH-02 — Basic search

**Covers:** SEARCH-02

Search a word with known occurrences.

**Pass:** visible matches get temporary overlays; result counter is correct after search completion; no PDF annotation is created.

### AT-SEARCH-03 — Italian case-insensitive Unicode

**Covers:** SEARCH-02

Fixture contains known variants including `È`, `è`, composed/decomposed equivalent forms, ligature/compatibility forms, and plain `e`.

**Pass:** NFKC + Unicode casefold behavior matches the documented expected set; canonically/compatibility equivalent forms match as intended; accents are not stripped (`e` does not automatically match `è`); result geometry maps back to the original PDF characters.

### AT-SEARCH-04 — Progressive/cancellable search

**Covers:** SEARCH-03

1. Search a common term in `long_text_300p.pdf`.
2. Before completion, replace query with a different term.

**Pass:** old results stop updating UI; stale search generation ignored; new query proceeds.

### AT-SEARCH-05 — Render priority during search

**Covers:** SEARCH-03, PERF-03

1. Start long search.
2. Rapidly scroll to a distant page.

**Pass:** visible page rendering is serviced without waiting for search to finish.

### AT-SEARCH-06 — Next/previous navigation

**Covers:** SEARCH-04

Run a deliberately slowed progressive search with results on early and late pages.

**Pass:** next/previous traverses known results while incomplete; total indicator is visibly provisional; global wrap is disabled until search completion; after completion navigation wraps correctly in document order.

### AT-SEARCH-07 — Image-only PDF

**Covers:** SEARCH-02

Search in `image_only.pdf`.

**Pass:** zero results; no OCR; no crash.

---

## 8. Rendering/performance tests

### AT-PERF-01 — No eager rasterization

**Covers:** PERF-01

Automated/instrumented.

Open `long_text_300p.pdf` and count rendered page keys after initial idle.

**Pass:** count is a small viewport-related subset, not proportional to all 300 pages.

### AT-PERF-02 — Bounded raster cache

**Covers:** PERF-05

1. Set deterministic small resident-cache, in-flight-byte and single-raster limits.
2. Scroll through many pages.
3. Hold scene references long enough to exercise eviction accounting.

**Pass:** app-controlled resident raster bytes obey the documented global policy; evicted-but-still-referenced images remain accounted until released; in-flight bytes remain bounded; old entries evict.

### AT-PERF-03 — Cache reuse

**Covers:** PERF-05

1. Visit pages 1–5.
2. Move forward a short distance.
3. Return before eviction.

**Pass:** expected recent pages produce cache hits rather than redundant renders.

### AT-PERF-04 — Stale and cross-document render rejection

**Covers:** PERF-04, DOC-05

Simulate/delay a render response, then change zoom/document generation and interleave responses from another open document.

**Pass:** stale responses are discarded; every valid response is routed only to its matching `document_id`; a raster from PDF A is never painted in PDF B.

### AT-PERF-05 — Long-document memory behavior

**Covers:** PERF-01, PERF-05, PERF-09

Manual/instrumented:

1. Open `long_text_300p.pdf`.
2. Scroll first→last.
3. Record resident raster bytes, in-flight raster bytes, worker cache counters and process working sets periodically.

**Pass:** app-controlled raster resources plateau according to policy and do not grow roughly linearly with pages visited. Worker/non-raster memory may vary, but any sustained unbounded growth must be investigated.

### AT-PERF-06 — Fast-scroll responsiveness

**Covers:** PERF-08

Rapidly drag scrollbar/scroll wheel through long document.

**Pass:** main window remains interactive; placeholders/lower-res pages are acceptable; no requirement to render every traversed page.

### AT-PERF-07 — Worker crash containment

**Covers:** error handling, architecture

Terminate the only worker in a single-document controlled development test.

**Pass:** GUI detects failure and shows controlled error instead of hanging indefinitely or crashing without explanation.

### AT-PERF-08 — Global cache bound across multiple documents

**Covers:** PERF-05, DOC-05

1. Set a deterministic small global raster-cache budget.
2. Open three PDFs.
3. Visit enough pages in each to exceed the budget if caches were independent/unbounded.
4. Switch repeatedly between tabs.

**Pass:** total raster-cache bytes across all documents remain within the configured global policy bound; opening more tabs does not multiply the cache budget.

### AT-PERF-09 — Inactive documents stop speculative prefetch

**Covers:** DOC-05, PERF-01, PERF-05

1. Open two long PDFs.
2. Make PDF A active and let its visible/nearby rendering settle.
3. Switch to PDF B.
4. Observe scheduling for PDF A.

**Pass:** PDF A does not continue speculative prefetch while inactive; its high-resolution raster cache is aggressively reduced according to policy; PDF B receives active rendering priority.

### AT-PERF-10 — Multi-document responsiveness

**Covers:** DOC-05, PERF-08, PERF-09

1. Open at least three representative PDFs, including `long_text_300p.pdf`.
2. Search/scroll in one document, switch to another and rapidly zoom/scroll.
3. Keep the third document inactive.

**Pass:** active-tab interaction remains responsive; background workers do not cause sustained CPU/memory behavior that materially degrades the active viewer.

### AT-PERF-11 — Worker failure isolation

**Covers:** DOC-05, error handling, architecture

1. Open two PDFs.
2. Terminate the worker belonging to PDF A in a controlled development test.

**Pass:** PDF A enters a controlled failed state; PDF B remains fully usable and its worker/session is unaffected.

---

### AT-PERF-12 — Oversized raster remains bounded

**Covers:** VIEW-06, PERF-05

On `large_page.pdf`, request a zoom/DPR combination whose ideal full-page RGB raster would exceed `MAX_SINGLE_RASTER_BYTES`.

**Pass:** worker does not allocate/send an oversized raster. The viewer selects bounded regional rendering at target density when a region can fit; geometric zoom remains correct and the GUI stays responsive. Resolution is degraded only if an individual minimum region cannot fit its hard limit.

### AT-PERF-13 — Saturated IPC and busy shutdown

**Covers:** PERF-02, architecture A-02, worker shutdown

1. In a controlled test, delay GUI raster adoption and saturate the configured raster channel/backpressure limit.
2. Request document/tab close while worker is rendering or payloads are pending.

**Pass:** Qt event loop remains responsive; no unbounded payload growth; graceful shutdown uses bounded waiting; other document tabs remain usable; forced termination, if required, is controlled.

### AT-PERF-14 — Demand-based regional selection and quality

1. At 100% and 150%, verify that an ordinary page whose ideal raster fits uses one full-page key.
2. At 300%, 400%, 600% and 800%, verify mode selection against the estimated ideal full-page byte requirement.
3. Compare the same small serif/sans text and thin lines against the former capped full-page baseline.

**Pass:** no arbitrary zoom-only threshold controls the decision; a page switches to regional mode exactly when its ideal full-page allocation cannot fit. Definitive visible regions use target density without full-page upscaling and are visibly sharper at 400–800%.

### AT-PERF-15 — Regional determinism, reuse and composition

Exercise slow/fast forward and reverse scroll, a return to a resident region, zoom 400→800→400%, layout switches, DPR 1.25/1.5/2, partial edge regions, mixed sizes, CropBox and rotations 0/90/180/270.

**Pass:** region keys and boundaries are deterministic; a resident return is a cache hit with no new render; stale results are rejected; visible composition has no gaps, white seams, incorrect overlap or coordinate drift. Layout is absent from reusable raster keys.

### AT-PERF-16 — Regional memory, tabs and cleanup

On a long document at 400–800%, perform two equivalent traversals, then repeat with two open documents and a double layout. Switch tabs and close one while regional work is pending.

**Pass:** full-page and regional rasters share the documented global 128 MiB budget and bounded transit slots; the second traversal does not cause sustained growth; inactive-tab regional prefetch stops and its rasters are aggressively trimmed; close/failure releases cache entries and transport credits without affecting the other document.

## 9. Coordinate integrity tests

### AT-COORD-01 — Rotated page annotation

**Covers:** PDF-03

Create deterministic point/rect/quad annotations on pages rotated 0°, 90°, 180° and 270°. Tests must compare canonical unrotated expected coordinates independently of the production mapper.

**Pass:** after save/reopen and display transform, annotations appear at intended content locations with no double rotation.

### AT-COORD-02 — CropBox annotation

**Covers:** PDF-03

1. Create annotations near visible edges on `cropbox_offset.pdf`.
2. Include at least one page combining non-trivial CropBox offset with non-zero rotation.
3. Compare worker DTO geometry to independently calculated expected canonical coordinates.

**Pass:** no systematic offset/double transform after save/reopen; combined rotation+CropBox remains correct.

### AT-COORD-03 — Sidebar target after transform changes

**Covers:** PDF-03, SIDE-04

Create annotation, switch layout, zoom, scroll elsewhere, click sidebar entry.

**Pass:** exact annotation is revealed.

---

## 10. Save tests

### AT-SAVE-01 — Incremental save

**Covers:** DOC-02

On `incremental_save_ok.pdf`:

1. add and confirm an annotation mutation;
2. verify incremental path is selected;
3. enqueue Save as a revision barrier;
4. submit a newer controlled mutation around a delayed Save response;
5. reopen.

**Pass:** saved revision persists; newer mutation is not falsely marked saved; file remains valid; dirty state reflects confirmed/pending revision correctly.

### AT-SAVE-02 — Save As

**Covers:** DOC-03

1. Save to a new path and continue editing.
2. Exercise existing-destination overwrite confirmation.
3. Attempt Save As to a path already open in another tab.

**Pass:** validated new path becomes active only after success; subsequent Ctrl+S targets it; existing destination is never overwritten without confirmation; another open document path is rejected before write; viewer/undo semantic state remains coherent.

### AT-SAVE-03 — Save failure

**Covers:** DOC-02, DOC-03, error handling

Exercise unavailable/read-only destination, externally modified source and controlled replacement failure.

**Pass:** controlled error; original is not silently overwritten; dirty state remains; if a validated temporary candidate is the only copy containing confirmed changes, it is retained/reported as recovery data.

### AT-SAVE-04 — Existing content preservation smoke test

**Covers:** PDF-04

Compare before/after:

- page count and canonical page sizes;
- extracted base text;
- targeted page-content rendering/hash checks for deterministic fixtures;
- untouched external annotation geometry/text/colors/appearance-relevant properties.

**Pass:** materially unchanged aside from intended annotation structures and unavoidable save-container metadata/structure changes.

### AT-SAVE-05 — Safe full-save fallback

**Covers:** DOC-02

Use an internal fixture/state for which incremental save is unavailable.

Test phases separately:

1. candidate write succeeds/fails;
2. validator process opens candidate, checks expected page count/annotation state, then exits;
3. live source handle is closed only after candidate validation;
4. replacement succeeds/fails;
5. final reopen succeeds/fails in controlled injection.

**Pass — success:** final target is replaced only after candidate validation, worker reopens final path, generation/signature refresh, and `saved_revision` is correct.

**Pass — early failure:** original remains untouched; live session remains dirty and usable when possible.

**Pass — late replacement/reopen failure:** validated candidate is retained as recovery data and is not silently deleted; application reports a controlled recoverable state rather than claiming success.

## 11. Adobe Acrobat interoperability gate

Run on Windows with current Adobe Acrobat Desktop.

For each supported type create a clean sample in the application, save, then open in Acrobat.

### AT-ACROBAT-01 — Ink

**Pass if:**

- visible at correct position;
- appears as annotation/comment rather than baked page artwork;
- can be selected/deleted in Acrobat.

### AT-ACROBAT-02 — Highlight

**Pass if:**

- correct text is highlighted;
- multi-line geometry is correct;
- appears in Acrobat comment/annotation system;
- can be selected/deleted in Acrobat.

### AT-ACROBAT-03 — FreeText box

**Pass if:**

- text correct;
- fill/border colors correct within normal viewer tolerance;
- rectangle correct;
- Acrobat recognizes it as editable annotation;
- editing text in Acrobat is possible.

### AT-ACROBAT-04 — FreeText callout

**Pass if:**

- text correct;
- box, line and anchor correct;
- colors correct within normal viewer tolerance;
- Acrobat recognizes it as an editable callout/FreeText annotation rather than unrelated page graphics.

### AT-ACROBAT-05 — Round trip

1. Create Ink, Highlight, Text Box and Callout (the annotations/reports group) in this app and save. Editorial correction round trips are covered separately by AT-ACROBAT-09 and AT-INDESIGN-04.
2. Open in current Acrobat Desktop.
3. Modify a TextBox and Callout in Acrobat; optionally modify another supported annotation if Acrobat permits.
4. Save in Acrobat and reopen in this app.
5. Without changing the untouched external annotations, add another app annotation and Save As.
6. Reopen again in Acrobat.

**Pass:** PDF opens throughout; external changes are preserved/listed; annotations that can be represented losslessly may remain editable, while incompatible external styling becomes read-only rather than being normalized; untouched Acrobat annotations remain materially unchanged after the second app save.

### AT-ACROBAT-06 — Delete Text / Strikethrough recognition

Open an app-created AT-EDIT-01 sample in current Acrobat Desktop.

**Pass:** Acrobat identifies Strikethrough Text with the intended text range; it is a native selectable/deletable comment, not page artwork or a generic note. Its meaning and target survive Acrobat save/reopen. Do not repair/recreate the comment in Acrobat to make the app's sample pass.

### AT-ACROBAT-07 — Insert Text recognition

Open an app-created AT-EDIT-02 sample in current Acrobat Desktop.

**Pass:** Acrobat recognizes the intended Insert Text semantics at the correct character boundary and preserves the complete Unicode plain-text payload after save/reopen. A Text Box/Callout or generic comment is failure.

### AT-ACROBAT-08 — Replace Text recognition

Open an app-created AT-EDIT-03 sample in current Acrobat Desktop.

**Pass:** Acrobat recognizes Replace Text with the correct original range and replacement text, including accents and literal special characters; semantics and payload survive save/reopen. An unrelated visual strike and note are insufficient.

### AT-ACROBAT-09 — Editorial correction round trip back to the app

For all three types, run app → Acrobat → save → app. Check kind, target and plain-text payload; save again from the app without editing the correction. When Acrobat permits it, also modify an Insert/Replace payload there and verify the changed plain text on return.

**Pass:** no correction becomes a generic annotation, loses its target/text or is normalized on app save. An external variant that cannot be mapped losslessly is preserved read-only. The resulting PDF must also retain applicability under AT-INDESIGN-04; passing this test alone does not establish InDesign compatibility.

## 12. Adobe InDesign interoperability gate

This is mandatory for the three editorial corrections. Run on Windows with recorded current Acrobat Desktop and InDesign versions/builds, app build/commit, pinned PyMuPDF version, fixture identity, fonts and PDF export settings. Preserve source/export and result evidence, with exact before/after text and observed comment type/Accept availability. Missing Adobe applications or an unexecuted manual run is NOT RUN/BLOCKED, never PASS.

Adobe documents both the supported Accept operations and the need for the source to remain unchanged after export: [Import PDF Comments](https://helpx.adobe.com/indesign/desktop/collaborate-and-review/share-and-collaborate/import-pdf-comments.html). Those documented capabilities do not validate any particular PyMuPDF encoding.

### Controlled workflow for every case

1. Prepare `editorial_source.indd` with selectable text and recorded exact expected targets.
2. Export `editorial_export.pdf` from that document; preserve an immutable baseline pair.
3. Make no subsequent edits to the InDesign source before import. Use a fresh working copy of that same source/export pair for each case, so previous Accept actions cannot change another case's reference text/layout.
4. Create the correction in this app on the exported PDF and save a distinct annotated PDF. For the encoding spike only, use the app's noninteractive command/service test harness and disposable export path; later repeat with the interactive UI and final production Save/Save As.
5. In the matching unchanged InDesign working copy, choose `File > Import PDF Comments` and import the app-written PDF, without recreating or repairing its comments in Acrobat first.
6. Verify the imported comment's actual type and target, not just its visible appearance.
7. Manually choose `Accept` for the correction.
8. Compare the resulting InDesign text with the exact expected edit and verify that surrounding text/content is intact.

**Failure in any case:** wrong comment type, generic-comment-only import, missing `Accept`, an unmapped/wrong target, incorrect changed range/position, lost characters or content. Recognition in Acrobat or successful PyMuPDF reopening cannot substitute for this gate. A control sample authored in Acrobat may diagnose a fixture problem but cannot count as an app sample passing.

Minimal deterministic source paragraph for the cases below: `È una prova. È una prova.`. Each case starts from its own unchanged baseline. Add Unicode/literal-character and selection-boundary variants from AT-EDIT-01/02/03 with recorded exact expectations; never compare only by visual resemblance.

### AT-INDESIGN-01 — Accept Strikethrough Text

**Covers:** ANN-08, PDF-05/06

In the controlled workflow, mark the first `una ` (including its trailing space) for deletion.

**Pass:** imported type is Strikethrough Text, `Accept` is available, and the resulting paragraph is exactly `È prova. È una prova.`. The second occurrence and all other text remain unchanged.

### AT-INDESIGN-02 — Accept Insert Text

**Covers:** ANN-09, PDF-05/06

In the controlled workflow, insert plain text `buona ` immediately before the second `prova`.

**Pass:** imported type is Insert Text, `Accept` is available, and the result is exactly `È una prova. È una buona prova.`. Repeat with accented Unicode and literal `&`, `<`, `>` payload characters; no character may be dropped or treated as user markup.

### AT-INDESIGN-03 — Accept Replace Text

**Covers:** ANN-10, PDF-05/06

In the controlled workflow, replace the second `prova` with `revisione più chiara`.

**Pass:** imported type is Replace Text, `Accept` is available, and the result is exactly `È una prova. È una revisione più chiara.`. Correct deletion without insertion of the complete replacement is failure. Include the Unicode/plain-text variants of AT-EDIT-03.

### AT-INDESIGN-04 — Editorial round trips retain Accept semantics

**Covers:** ANN-08/09/10, PDF-02/04/05/06

For each of the three correction types, repeat its controlled source/export case through:

- app → Acrobat → save → InDesign;
- app → Acrobat → save → app → save → InDesign, exercising AT-ACROBAT-09 preservation and a changed Insert/Replace payload where supported.

**Pass:** the imported type, available `Accept`, target and exact resulting text remain correct for the recorded payload. No generic-comment downgrade, normalization, lost associated object or lost character is allowed. Start each route from a fresh baseline pair; Acrobat must not silently be used to repair an invalid app encoding. Direct app → InDesign success is required separately by AT-INDESIGN-01/02/03.

The encoding spike must establish native encoding plus AT-ACROBAT-06/07/08/09 and AT-INDESIGN-01/02/03/04 on disposable samples before final interactive correction implementation. Repeat the gates with the interactive tools and again with the production save path before v1 release. No spike is executed by this document update.

## 13. v1 release checklist

Required before v1 completion:

- [ ] All automated required tests green
- [ ] AT-ACROBAT-01 pass
- [ ] AT-ACROBAT-02 pass
- [ ] AT-ACROBAT-03 pass
- [ ] AT-ACROBAT-04 pass
- [ ] No PyMuPDF multithreading present
- [ ] No eager full-document page rasterization
- [ ] Raster cache bounded
- [ ] Long-document scroll tested
- [ ] Search tested during scrolling
- [ ] Rotated/CropBox fixtures tested
- [ ] Dirty close behavior tested
- [ ] Multiple-document tab state isolation tested
- [ ] One independent worker per open PDF verified
- [ ] Global raster-cache bound verified with multiple PDFs open
- [ ] Inactive documents perform no speculative prefetch
- [ ] Single-worker failure leaves other open documents usable
- [ ] Duplicate/open-file writer protection tested
- [ ] External file modification conflict tested
- [ ] Oversized raster degradation and in-flight limits tested
- [ ] Busy/saturated IPC shutdown tested
- [ ] FreeText/Callout overflow prevention tested
- [ ] Annotation revision/save-barrier dirty-state tests green
- [ ] Full-save failure/recovery transaction tested
- [ ] AT-ACROBAT-05 round trip pass
- [ ] AT-CMD-01 covers all seven tools without a UI/PDF bypass
- [ ] AT-EDIT-01/02/03/04 persistence, targeting and logical lifecycle pass
- [ ] AT-ACROBAT-06/07/08 editorial recognition pass
- [ ] AT-ACROBAT-09 editorial return-to-app round trip pass
- [ ] AT-INDESIGN-01/02/03 type and exact Accept results pass
- [ ] AT-INDESIGN-04 round trips pass on unchanged source/export fixtures
- [ ] Final editorial interoperability gates repeated on interactive tools and production Save/Save As
- [ ] No out-of-scope feature introduced


## M24.1b — Native linear text extraction

AT-TEXT-01: match native sort=False text per physical page; replace line breaks
with spaces, preserve internal spacing, Unicode and hyphens, exclude comments.
AT-TEXT-02: reuse existing multimodal localization; ambiguous text is rejected.
AT-TEXT-03: export exactly pages/page/text, with no extra fields or CR/LF.
AT-TEXT-04: retain empty pages and their physical numbering; no batch duplicates.
AT-TEXT-05: bounded IPC/private files, stale identity/revision rejection,
background atomic export, tab binding, Save As invalidation and close cleanup.
AT-TEXT-06: targeted shared multimodal/selection regressions. When available,
check all 44 pages of the owner's proof, including order on page 25 and
expressive characters/spaces on pages 21–24. Manual Adobe gates remain NOT RUN.

### AT-TEXT-07 — M24 deterministic line-end dehyphenation

Test letters around an ASCII hyphen/line break with optional adjacent spaces
or tabs, Unicode letters, CRLF and successive joins. Preserve inline compounds,
numeric/operator/symbol boundaries and other spacing. Characterize genuine
compounds split at a line end as inherently ambiguous under this rule.
On the local school proof audit all 55 changes and the C++ listings; localize
analizzare/riguardano/chiamante with the existing multimodal matcher. Compare
all 44 novel pages with M24.1b, including expressive pages 21–24 and page 25.

## M23.2 — Highlight colors

### AT-HLC-01 — Creation palette

Select PDF text using the current interaction. Choose each of the five presets
beside Highlight, create, repeat, and switch document tabs. The chosen preset
is visibly checked, persists for the document session, and leaves the confirmed
text target unchanged. The native annotation is Highlight with the chosen RGB.

### AT-HLC-02 — Native recoloring and history

Select an app-created and an imported Highlight. Recolor with the same palette;
the overlay updates immediately. Undo/redo restores the exact native states.
Preserve QuadPoints, Rect, author, Unicode content, opacity, dates, popup/reply
relationships and unknown native keys. Change only /C and the new /AP; never
mutate shared original appearance streams. Other subtypes and locked/unsafe
profiles reject the color command in both service and worker.

### AT-HLC-03 — Persistence and preservation

Exercise production Save and Save As, reopen and compare native color,
geometry, metadata and unrelated Ink/Line/editorial annotations. History must
remain correct across the Save As generation barrier. Cover rotated CropBox
pages, missing/shared appearances and original custom RGB colors. Selecting a
custom color displays its swatch without selecting or applying a preset.

### AT-HLC-04 — Manual Acrobat round-trip

In Acrobat, create colored Highlights with opacity, author and comment text;
include a custom color and a multiline range. Open in fuPDF, recolor, undo/redo,
Save and Save As. Reopen both results in Acrobat: verify editable native
Highlight, exact quads, opacity, author, content and other comments. Save from
Acrobat and reopen in fuPDF. This is a separate manual gate: synthetic fixtures
and PyMuPDF reopening do not establish Acrobat interoperability. NOT RUN until
manual evidence is recorded.
