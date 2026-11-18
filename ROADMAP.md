# Personal PDF Annotator — Implementation Roadmap

**Status:** normative milestone sequence  
**Version:** 0.3\
**Date:** 2026-09-01

## 1. Purpose

This file defines **implementation order and stop gates**, not product behavior. Product behavior remains governed by `PROJECT_SPEC.md`, architecture by `ARCHITECTURE.md`, and release criteria by `ACCEPTANCE_TESTS.md`.

Codex may work autonomously **inside the currently assigned milestone**, including iterating on code and tests, but must stop at the milestone exit gate and report. It must not begin the next milestone without a new explicit instruction.

M0, M1 and M2 are completed milestones. Their definitions below remain unchanged as the historical scope and criteria for that work. The expanded v1 tool set and command/service requirements apply to future milestones only; this documentation update does not start M3 or reopen M0–M2.

Every milestone must end with:

- files changed;
- tests/commands run and results;
- acceptance-test IDs PASS / NOT RUN / BLOCKED;
- unresolved risks/limitations;
- no commit unless explicitly requested.

---

## M0 — Foundations and technical-risk spikes

### Goal
Validate the architectural assumptions that would be expensive to change later. This is not a product-feature milestone.

### In scope
- `pyproject.toml`, src/test package skeleton, pytest, minimal local logging.
- Pin Python 3.11-compatible PySide6 and PyMuPDF 1.28.x patch versions after verification.
- Minimal PySide6 window/entry point.
- Windows `multiprocessing`/`spawn` bootstrap.
- Typed IPC prototype with `document_id`, generations/request ids, bounded non-blocking GUI-side transport, graceful shutdown and worker-failure detection.
- Two simultaneous test workers opening two disposable PDFs to prove isolation.
- Canonical-coordinate contract implementation skeleton + tests for rotation/CropBox assumptions.
- Raster transport spike: RGB payload, ownership/lifetime, in-flight bound and oversized-raster policy.
- **Non-interactive disposable spikes** proving:
  - FreeText with independent fill/border while user text remains plain;
  - native FreeText callout with independent colors;
  - save→temporary candidate→one-shot validator→Windows replace/reopen feasibility.
- No writes to user production PDFs; spikes use fixtures/copies only.

### Out of scope
Viewer product UI, toolbar, sidebar, search, interactive annotations, layouts, full undo/redo, production Save/Save As.

### Exit gate
All M0 technical assumptions either pass with automated/reproducible evidence or are reported as blocking before M1. No unresolved coordinate/FreeText/IPC/save-feasibility ambiguity may be silently carried forward.

---

## M1 — Document lifecycle and multi-document sessions

### Goal
Open/close multiple PDFs safely with one persistent worker per document and independent tab/session state.

### In scope
- `DocumentManager` / `DocumentSession`.
- Open PDF metadata/page geometry without eager rasterization.
- Reject encrypted/protected/corrupt unsupported input cleanly.
- Canonical path + file identity/signature tracking.
- Duplicate-open activates existing tab.
- Basic document tabs, dirty indicator plumbing (no annotation mutations yet).
- Clean worker shutdown and failure isolation.

### Primary acceptance
AT-DOC-01/02/03/05/06/08/10, relevant worker-failure tests.

---

## M2 — Virtualized continuous viewer

### Goal
A correct lazy single-page continuous viewer before optimization complexity.

### In scope
- virtual page placeholders;
- active viewport tracking;
- visible-page rendering through worker;
- RGB page raster transport/adoption;
- stale render rejection;
- continuous vertical scrolling;
- mixed page sizes.

### Primary acceptance
AT-VIEW-01/04, AT-PERF-01/04/06.

### Stop gate
Do not start cache/zoom tuning until basic virtualization is correct and GUI never waits synchronously for rendering.

---

## M3 — Rendering performance, cache and zoom

### Goal
Make the viewer genuinely light and fluid.

### In scope
- global resident raster budget;
- in-flight raster budget/backpressure;
- maximum single-raster policy;
- bounded worker caches/optional DisplayList only if measured useful;
- nearby prefetch for active tab;
- inactive-tab raster trimming/no speculative prefetch;
- zoom immediate geometric scaling + debounced definitive render;
- instrumentation for render latency/cache hits/stale jobs/memory counters.

### Primary acceptance
AT-VIEW-06/07, AT-PERF-02/03/05/08/09/10/12/13.

### Stop gate
Record performance on representative 200–400 page PDF before proceeding. Do not introduce shared memory or render pools without measurements and spec change.

---

## M4 — Page layouts

### Goal
Add the remaining display modes without changing continuous-scroll semantics.

### In scope
- single;
- two-page spreads;
- two-page with isolated cover;
- layout switching preserving zoom/reading anchor.

### Primary acceptance
AT-VIEW-02/03/05 plus coordinate regression tests.

---

## M4.1 — Regional high-zoom rendering

### Goal
Preserve target raster density when a full-page raster would exceed the bounded single-raster policy.

### In scope
- keep full-page rendering at ordinary zoom when the ideal raster fits;
- select regional rendering from the estimated ideal full-page byte requirement, not an arbitrary zoom threshold;
- deterministic bounded regions with limited visible-first overscan/prefetch;
- full-page and regional entries in the same global multi-document raster cache;
- seam-free composition across CropBox, rotation, DPR and all M4 layouts;
- stale-response rejection, inactive-tab trimming and bounded cleanup for regional work;
- native Windows quality/performance evidence at 300–800%.

### Out of scope
Text selection, annotations, search, saving, a second cache budget, a render pool or changes to canonical PDF geometry.

### Primary acceptance
AT-PERF-02/03/04/05/06/08/09/12/13/14/15/16 and AT-VIEW-06/07.

### Stop gate
Stop before M5 if correct regional rendering would require a different worker model, a replacement viewer, a new dependency or a change to canonical coordinates.

---

## M5 — Annotation command/service foundation

### Goal
Establish the shared, source-independent command path before implementing interactive annotation tools.

### In scope
- Structured domain commands conceptually equivalent to `CreateInk`, `CreateHighlight`, `CreateTextBox`, `CreateCallout`, `CreateDeleteText`, `CreateInsertText` and `CreateReplaceText`.
- The A-06 path: UI → structured annotation/edit command → annotation service → document worker → PyMuPDF/PDF.
- Service submission/validation and authoritative worker-side handling through the existing isolated IPC architecture; no PDF encoding in UI handlers.
- Document/generation/revision identity and target preconditions; text targets carry page, canonical geometry and selection-derived range/boundary data, not only a string.
- Common routing for permitted later update/delete/undo operations; reuse serialized mutation and dirty/revision rules.
- Contract/dispatch tests with test doubles where concrete tool adapters are not yet implemented. Real PDF adapters are added in their respective milestones.

### Out of scope
Interactive annotation creation, unverified editorial encoding, automation interfaces, AI/LLM agents/APIs and semantic document models. Completed M0 probes are not retroactively refactored into product tools.

### Primary acceptance
ANN-11, A-06 and the foundation portion of AT-CMD-01; subsequent tool milestones complete its real-adapter coverage.

### Stop gate
Demonstrate one shared command domain and service boundary independent of GUI events, with no cross-document or stale-target mutation. Do not implement a second PDF mutation path for a tool or future caller.

---

## M6 — Text selection and Highlight

### Goal
Implement reliable char-aware single-page text selection and native PDF Highlight.

### In scope
- text geometry cache for visible/recent pages;
- endpoint→character-range resolution;
- reverse drag;
- multiline and two-column deterministic behavior;
- exact quad preview/commit identity;
- image-only no-op behavior;
- native Highlight persistence through the M5 `CreateHighlight` service path;
- selection-derived target data reusable by later Delete/Replace commands; character-boundary data for later Insert targeting, without introducing editorial tool UI or encoding yet.

### Primary acceptance
AT-ANN-05/06/07, AT-COORD-01/02, AT-ACROBAT-02 when manual test is available.

### Stop gate
Verify one saved Highlight manually in Acrobat before relying on the annotation layer design for later tools.

---

## M7 — Freehand Ink

### Goal
Implement immediate freehand preview and native PDF Ink persistence.

### In scope
- pen gesture/preview;
- preset stroke color;
- canonical point conversion;
- create/select/delete app-created Ink;
- `CreateInk` and permitted mutations through the M5 service;
- sidebar/event plumbing may remain minimal until M12.

### Primary acceptance
AT-ANN-01/02/10, coordinate tests, AT-ACROBAT-01.

---

## M7.1 — Move app-created Ink

### Goal
Add natural drag movement for app-created Ink without broadening the annotation surface or beginning M8.

### In scope
- Pen-off hit testing, selection and Qt-only translated drag preview for app-created Ink.
- One explicit typed `MoveInk` command through A-06 on release only; no IPC or PyMuPDF work during mouse move.
- Canonical unrotated translation deltas across all layouts, zooms, rotations and CropBox offsets.
- Deterministic whole-path boundary clamp on the original page; no cross-page movement or point-wise distortion.
- Worker adapter update of native `/Ink` geometry while preserving app `annotation_id`, `/NM`, style and path topology.
- Revision/dirty/stale/failure/cancel routing through the existing serialized service contract, plus targeted drag timing instrumentation and regression tests.

### Out of scope
- Highlight movement; external annotation movement; Text Box/Callout movement or resize; undo/redo implementation; M8 and later milestones.

### Primary acceptance
AT-ANN-01/02/10, AT-CMD-01, AT-COORD-01/02 and M7/M6/M4.1 regression smoke.

### Exit gate
Pass only when an app-created Ink drag is previewed locally, commits exactly one successful native `/Ink` mutation on release, preserves identity/style/topology except for translation, and does not regress Pen, M6 selection, Highlight, delete, dirty-close, multi-document isolation or canonical-coordinate behavior. Stop before M8.

---

## M8 — Text Box / FreeText

### Goal
Implement app-created plain-text boxes with independent fill/border and reliable saved appearance.

### In scope
- drag rectangle;
- plain-text editing;
- internal fixed escaped rich-text serialization only if required by pinned API;
- move/resize;
- same-document page transfer through one typed FreeText mutation;
- fill/border presets;
- Helvetica, Times and Courier with 6–72 PDF-point sizes;
- auto-confirm that retains failed editor state, and pending optimistic overlay replacement/rollback;
- overflow prevention;
- native FreeText persistence through `CreateTextBox` and the common service for permitted edits.

### Primary acceptance
AT-ANN-03/04/10, AT-ACROBAT-03.

---

## M9 — Callout / FreeTextCallout

### Goal
Implement native editable callouts compatible with Acrobat.

### In scope
- anchor + text-box gesture;
- native callout geometry/intent;
- move box/anchor, resize box;
- plain text + fixed internal styling path;
- fill/border-line colors;
- overflow prevention;
- `CreateCallout` and permitted mutations through the common service.

### Primary acceptance
AT-ANN-08/09/10, AT-ACROBAT-04.

### Stop gate
Complete AT-ACROBAT-01..04 for the Pen, Highlight, Text Box and Callout annotation/reporting group. The separate editorial-correction gates follow in M10/M11; these checks alone do not establish InDesign compatibility.

---

## M10 — Editorial comment encoding and interoperability spike

**2026-09-08 status: spike implemented; Acrobat interoperability and save/reopen
round trip PASS, confirmed by the user.** The probe opens correctly; Delete,
Insert and Replace are recognized and displayed with correct ranges, payloads
and Replace grouping. The full suite was run manually by the user: PASS, exit
code 0. The service/worker probe, reference comparison and evidence are in
[M10_REPORT.md](M10_REPORT.md). InDesign Import PDF Comments / Accept remains
NOT RUN because InDesign is unavailable. M10 is not a final PASS pending that
gate; M11 has not started. This status update changes documentation only and
does not rerun tests.

### Goal
Validate native Delete/Strikethrough, Insert and Replace comment encodings and their actual Acrobat/InDesign behavior before definitive interactive implementation.

### In scope
- Disposable fixtures from an unchanged InDesign source and its PDF export, with exact expected text and recorded application versions.
- Exercise the M5 command/service domain and M6 selection targets through a non-interactive test caller; PyMuPDF remains the writer inside the owning worker.
- Validate StrikeOut encoding and discover/verify Insert Text and Replace Text encoding without assuming a PDF subtype/dictionary combination is sufficient.
- Prefer the pinned public API; isolate, document and test any strictly necessary low-level dictionary manipulation. No additional PDF library without demonstrated need and an explicit dependency decision.
- Verify native Acrobat recognition, persistence, and app → Acrobat → save → app preservation.
- Verify InDesign Import PDF Comments type, availability of `Accept`, and its exact deletion/insertion/replacement effect, including Unicode payloads and repeated target text.
- Test app → Acrobat → save → InDesign as well as direct app → InDesign import; use fresh copies of the unchanged source for each case.
- A disposable save/export test harness sufficient for these checks, not production Save/Save As UI or recovery implementation.

### Primary acceptance
PDF-05/06; service/spike portions of AT-EDIT-01..03; AT-ACROBAT-06..09 and AT-INDESIGN-01..04.

### Stop gate
All three encodings must have reproducible automated persistence evidence and passing manual Acrobat/InDesign gates before M11. Wrong/generic comment type, unavailable `Accept`, incorrect target or lost payload fails the gate. Missing Adobe applications means NOT RUN/BLOCKED, never inferred compatibility. Report an unresolved encoding as blocking rather than substituting a Text Box/Callout or graphic mark. This is a future spike, not a reopening of M0.

---

## M11 — Interactive editorial corrections

### Goal
Implement the three required editorial tools using the encoding proven in M10 and the common command/service path.

### In scope
- Delete Text / Strikethrough from a confirmed text range and ordered quads.
- Insert Text at a confirmed character boundary with plain Unicode text; define the minimal insertion-point interaction in this milestone.
- Replace Text for a confirmed range with plain Unicode replacement text.
- Preview and commit use the same confirmed target; original PDF text remains unchanged.
- Create/select/delete each correction as one logical item, including all native objects required by its validated encoding.
- Apply document/generation/revision checks, dirty state and external-annotation preservation through the common service.
- Repeat the Acrobat/InDesign gates through actual interactive creation and disposable test export; production Save/Save As is still M15.

### Primary acceptance
ANN-08..11, PDF-05/06, AT-EDIT-01..04 (sidebar/undo portions completed in M12/M14), AT-CMD-01 real-adapter coverage, AT-ACROBAT-06..09 and AT-INDESIGN-01..04.

### Stop gate
All three interactive tools must preserve the M10 semantics and pass the manual import/`Accept` cases. Do not add formatting, retargeting workflows or extra annotation tools; do not proceed with an unverified substitute encoding.

---

## M12 — Annotation inventory and sidebar

### Goal
Provide the collapsible comments/interventions sidebar and navigation.

### In scope
- enumerate app/external annotations;
- app stable `/NM` identity; safe session identity for external missing/duplicate names;
- read-only policy for external annotations not losslessly representable;
- document-order sorting;
- click→scroll/reveal/flash;
- live create/edit/delete updates;
- all seven domain kinds, with each editorial correction represented as one logical entry showing its target/payload as appropriate, even if its encoding uses multiple PDF objects.

### Primary acceptance
AT-SIDE-01..04, AT-ANN-12, AT-COORD-03 and sidebar/preservation portions of AT-EDIT-04.

---

## M13 — Text search

### Goal
Implement progressive lightweight Ctrl+F search without degrading rendering.

### In scope
- find bar;
- NFKC + casefold with original-geometry mapping;
- page-sliced progressive search;
- provisional result count;
- cancellation/query generations;
- next/previous and post-completion wrap;
- temporary UI-only result overlays.

### Primary acceptance
AT-SEARCH-01..07, AT-PERF-05/10 regressions.

---

## M14 — Undo/redo and revision hardening

### Goal
Complete semantic undo/redo and harden the command/service model established in M5 before production saving; do not introduce a separate late command layer.

### In scope
- harden the existing serialized per-document mutation queue and service boundary;
- pending/confirmed/saved revision state;
- semantic command history, treating an editorial correction and its native components as one coherent operation;
- undo/redo for operations allowed by ANN-05 across all seven domain kinds;
- text-editor-local shortcut precedence;
- dirty-state correctness under delayed responses;
- multi-document history isolation.

### Primary acceptance
AT-ANN-10/11, AT-DOC-04/07/09, AT-CMD-01, remaining undo/revision portions of AT-EDIT-04 and revision-specific tests.

---

## M15 — Production Save, Save As and Acrobat/InDesign round trips

### Goal
Implement safe persistence without silent data loss.

### In scope
- revision save barrier;
- external file signature conflict check;
- incremental Save;
- full-save temporary candidate + validator + replace/reopen/recovery state;
- Save As confirmation/collision rules;
- preservation checks;
- preservation of editorial target/payload/relationships as one semantic correction;
- final Acrobat and InDesign round trips through production Save and Save As, repeating the earlier disposable-export gates.

### Primary acceptance
AT-DOC-11/12, AT-SAVE-01..05, AT-ANN-12, AT-EDIT-01..04, AT-ACROBAT-01..09 and AT-INDESIGN-01..04.

### Stop gate
This is a high-risk gate. Do not proceed on the basis of “PyMuPDF can reopen it” or Acrobat recognition alone; manual current-Acrobat and InDesign import/`Accept` gates plus failure-injection save tests must pass using the production persistence path.

---

## M15.1 — External annotation editing & Acrobat round-trip

### Goal
Enable interoperable editing of imported/external annotations only when their
round-trip is demonstrably lossless.

### In scope
- assess external FreeText, FreeTextCallout, Ink, Highlight/common markup and
  Sticky Note profiles;
- assess Delete Text, Insert Text and Replace Text editorial corrections where
  their external representation can be preserved safely;
- evaluate per-type/profile capability for move, resize, text edit, style edit,
  color/opacity/border changes and Callout anchor/leader changes;
- enable editing only when the complete relevant PDF representation can be
  preserved without loss: structure, `/AP`, `/RC`, `/DA`, `/RD`, `/CL`, `/IT`,
  Unicode, metadata and relevant relationships;
- prove the Acrobat → app → modify → Save → Acrobat round-trip for every
  enabled profile, covering visual fidelity, content, geometry, style,
  metadata/relationships and absence of information loss.

### Read-only policy
Recognized subtype alone never authorizes external editing. An annotation or
profile that is not proven lossless remains read-only, while remaining deletable
when M12 can prove that its logical deletion is safe.

### Scope boundary
Do not anticipate this work in M12–M15. M15 remains the production Save/Save As
and base round-trip milestone; M15.1 extends that work specifically to editing
external annotations.

### Primary acceptance
New focused external-editing round-trip cases, plus the applicable preservation,
Acrobat and Save/Save As acceptance gates from M15.

### Stop gate
Do not enable a capability on an external profile until the corresponding
Acrobat round-trip proves lossless preservation. PyMuPDF reopening alone is not
evidence of interoperability.

---

## M16 — Multi-document stress and resource hardening

### Goal
Prove the architecture remains light and isolated under realistic simultaneous use.

### In scope
- several open PDFs;
- interleaved render/search/mutation responses, including editorial commands and their logical component state;
- active/inactive priority behavior;
- worker crash/close during load;
- global raster/in-flight limits across tabs;
- duplicate-path and Save As collision regression;
- leak/resource cleanup investigation.

### Primary acceptance
AT-DOC-05..12, AT-PERF-07..13, AT-EDIT-04 and relevant save/dirty regressions.

---

## M17 — Minimal UI polish and Windows packaging

### Goal
Deliver the intended sparse personal desktop application without adding scope.

### In scope
- final spacing/icons/keyboard focus/accessibility basics;
- compact contextual color controls and the minimal seven-tool creation surface;
- robust error messages;
- Windows executable/package strategy;
- clean startup/shutdown;
- final full test/release checklist.

### Out of scope
Any new product feature, settings system, telemetry, updater, cloud integration or “nice to have” annotation tools.

### Exit gate
`ACCEPTANCE_TESTS.md` v1 checklist complete, including separate manual Acrobat and InDesign gates and a recorded representative performance run.

### M17A — UI/UX consolidation: COMPLETE

- Floating Pen / TextBox / Callout palette with graphite contextual property capsules,
  icon-only annotation controls and role-specific colour controls.
- Empty TextBox/Callout content, one-shot TextBox/Callout and native PDF
  Highlight/Delete/Replace actions, plus the context-menu Insert Text flow.
- Neutral single-click caret, canonical drag selection and double-click word
  selection; reduced resize hit zones and TextBox/Callout vertical auto-grow.
- Persistent red-default Pen, Ctrl+left-drag transient pan, compact page spacing,
  and consolidated File / Modifica / Visualizza chrome with real document tabs.
  Layout and zoom live under Visualizza; the former permanent toolbar is removed.
- Manual UX QA and the final full suite passed. The only intentional skip is the
  historical rejected custom-Callout AP spike. No worker, save or PDF architecture
  regression was introduced.


### M17B-B — First portable Windows build: COMPLETE

- PyInstaller one-folder; build-only PyInstaller 6.22.3 / hooks-contrib 2026.8 pins.
- Early frozen multiprocessing launcher, sanitized PATH/environment and native
  provenance gate; one shared Console/Windowed spec with bounded Qt plugin collection.
- fuPDF branding/icon/version metadata, physical fonts/licenses and verified
  PyMuPDF native binaries; LocalAppData windowed logs and native Windows pickers.
- User-confirmed Windows 10 Pro build 19045: sanitized probe, Console and Windowed
  PASS; multi-document/render/search/layout/zoom/annotation flow PASS; Save As,
  nested frozen validator and reopening PASS; zero processes/orphans after shutdown.
- Full pytest suite PASS as reported by the user; only the known intentional
  historical custom-Callout AP spike skipped. Not rerun during closeout.
- Evidence and reproducible commands: `tests/manual/windows_packaging.md`.

### M17B-C — COMPLETE WITH RECORDED LIMITATIONS

- Clean Hyper-V Windows 11 26H2 x64 build 26300.9457: portable Windowed build,
  no installed Python/PySide6/PyMuPDF/project fonts/developer environment,
  offline execution, spaces/Unicode/non-default path and arbitrary working
  directory PASS.
- Full 100% workflow PASS: launch/branding/native dialogs, multiple documents,
  render/scroll/search/zoom/layouts, all seven tools, dirty state, Save As,
  frozen nested validator, reopen, logging, source preservation and zero orphan
  processes.
- 125% and 150% DPI visual checks plus representative Open → annotate → Save As
  → close → reopen PASS. 200% and mixed-monitor DPI are explicitly NOT RUN.
- Formal three-run packaged/development comparison NOT RUN. A slower Save As on
  a different substantially longer PDF at 150% completed successfully and is
  retained only as an informal observation, not a DPI failure or comparison.
- Windows 10 Pro 22H2/build 19045 remains compatibility-tested/known working
  through M17B-B; Windows 11 remains the normative target.
- Evidence: `tests/manual/windows_release_qualification.md`.

At M17 closeout, AT-ACROBAT-01..09 and AT-INDESIGN-01..04 remained pending.
See the later interoperability reports for subsequent evidence. The public
distribution plan is tracked in M22.

## M21.2 — Offline review snapshot and batch preparation

Capture the current revision in its existing PDF worker. Prepare bounded
extracts from an immutable snapshot and keep the physical target/context map.
The exit checks are snapshot fidelity, complete target coverage and measured
same-worker delays (see AT-DOC-04, AT-SAVE-02 and AT-PERF-05/10). API transport,
concurrent scheduling, review application and GUI are later milestones.

## M21.2a — Explicit review extract lifetime

Keep independently releasable extracts available from the same snapshot. Check
two overlapping readers, cleanup on release or close, and rollback of failed
materialization. Keep the M21.2 worker ordering and extraction policy.

## M21.3 — Review scheduling, transport and results

Run explicitly started post-v1 analysis in one application scheduler with at
most two HTTPS attempts, RPM/TPM admission, bounded retries and atomic batch
results. Preserve M20's aggregate and report complete page coverage and
selective retry on the same snapshot. The exit checks use an injected transport,
controlled time, simulated persistence failures and the Save As barrier.
Application of results and the interactive workflow follow separately.

## M21.4 — Deferred application in the open document

After the analysis barrier, localize on the retained snapshot, reconfirm on
the live document and submit one native correction at a time through A-06.
Record per-proposal outcomes and reconcile uncertain intents by `/NM`. Check
AT-CMD-01 and the automated targeting, persistence and history portions of
AT-EDIT-01..04 with simulated analysis and a real PDF worker. The interactive
review controls and the manual Adobe gates are separate.

## M21.5 — Review window and Windows credentials

Open a non-modal review window from File with the current document bound at
opening. Expose profile, physical page range, context, Windows credential
management, measured progress and selective retry. Use the M21.4 application
path and confirm interruption before closing an active document. Test the
Qt lifecycle offline with a simulated transport and credential storage.
The manual development and Adobe procedure is in `M21_5_COLLAUDO.md`.

## M21.5a — Review monitor controls and Sticky Note display

Allow the monitor to minimize independently, keep the physical page range
ordered, and show newly created native Sticky Notes through the existing M18
overlay and reader. Exercise live application, margin hit testing, rotation,
Save As and reopening. Reuse the existing test PDF for the manual check.

## M21.5b — Model selection

Offer GPT-6.1 Sol and GPT-6.1 Terra with medium reasoning in the review
window. Default to Terra and retain the selected model for every
attempt and selective retry of the job. Verify pricing and model-specific
admission with simulated Responses calls.

## M22 — Open-source publication preparation

M22.1 audited the source and Windows bundle. M22.2 replaced private test
material with synthetic fixtures and documented the remaining Adobe checks.
M22.3 prepares the license, third-party notices and public documentation.
M22.3a adds the public Acrobat Reader reference. In M22.3b the project owner
approves GNU AGPLv3 and authorizes distribution of the branding. A later
milestone will create the separate public GitHub repository and qualify the
portable ZIP, including corresponding source, required notices and private
vulnerability reporting.
