# AGENTS.md — Instructions for Codex and coding agents

## 1. Read first

Before changing code, read these files in this order:

1. `PROJECT_SPEC.md`
2. `ARCHITECTURE.md`
3. `ACCEPTANCE_TESTS.md`
4. `ROADMAP.md`
5. this `AGENTS.md`

They are normative.

Priority when documents appear to conflict:

1. the user's latest explicit instruction;
2. `PROJECT_SPEC.md`;
3. `ARCHITECTURE.md`;
4. `ACCEPTANCE_TESTS.md`;
5. `ROADMAP.md` for milestone sequencing only;
6. existing implementation.

If you detect a material contradiction, do not silently choose a new product behavior. Report it.

---

## 2. Scope discipline

Implement only the requested milestone/task.

The post-v1 review analysis specified in `PROJECT_SPEC.md` is a scoped
exception for explicitly requested milestones and explicitly started reviews.

Do not add "helpful" features, abstractions, dependencies, settings, menu items or workflows that are not required by the current milestone or the project specification.

In particular, do not add:

- OCR;
- cloud/network features;
- accounts/telemetry;
- database;
- Electron/browser/web server;
- page editing/management;
- redaction/signatures/forms;
- AI/LLM agents or APIs, semantic document extraction/models, or automation interfaces;
- direct InDesign integration, InDesign scripting or automatic acceptance of comments;
- Underline, Sticky Note, Arrow, Rectangle, Cloud, Stamp or any creation tools beyond the seven specified for v1;
- arbitrary formatting panels.

If something outside scope seems necessary for correctness, explain why before broadening the implementation.

---

## 3. Architecture invariants

These rules are mandatory:

1. **Never use PyMuPDF from multiple threads.**
2. Every user-open PDF owns exactly one persistent dedicated PDF worker process; a worker owns exactly one live writable document. M21.2 allows that thread to capture an immutable temporary snapshot and open one disposable instance backed by it for extraction. Change only the disposable instance and write only to a separate extract file. M21.4 also uses that disposable instance for read-only localization. It has no writable user session and closes before the operation returns.
3. The only allowed extra PyMuPDF process is a short-lived read-only validator for a temporary full-save candidate.
4. All PyMuPDF `Document`, `Page`, `Annot`, `DisplayList` and `TextPage` objects remain inside the process that owns them.
5. Do not pass PyMuPDF objects through IPC.
6. Multiple document tabs have independent viewer, search, annotation, dirty, revision and undo/redo state.
7. Route every asynchronous result by document id plus generation/request identity; never allow cross-document leakage.
8. Annotation mutations are serialized per document and revisioned; Save is a revision barrier.
9. Heavy PDF rendering/search/file work and IPC send/receive/join must not block the Qt GUI event loop.
10. Never eagerly rasterize every page.
11. Raster cache, in-flight raster payloads and single-raster allocations are bounded; high zoom degrades raster resolution rather than exceeding limits.
12. All persistent geometry uses the canonical **unrotated PyMuPDF page coordinate system** defined in `ARCHITECTURE.md`; DPR/raster scale is not PDF geometry.
13. Normal save preserves native PDF annotations and never flattens them.
14. Untouched external annotations are preserved without reconstruction; unsafe-to-map external annotations are read-only.
15. Search result highlighting is UI-only and never modifies the PDF.
16. Only one writable app session may target a given source/destination file identity.
17. Encrypted/protected PDFs are rejected in v1.
18. No annotation/correction tool may bypass the common command/service layer (A-06): UI → structured annotation/edit command → annotation service → document worker → PyMuPDF/PDF. PDF encoding never belongs in UI event handlers.
19. UI and any future local automation/AI caller must share the same source-independent command domain. AI/LLM, semantic document models and automation are not v1 features and require an explicit scope change before implementation.
20. Preserve native editorial-comment semantics without changing original PDF text or flattening corrections. Insert Text and Replace Text must not be replaced by ordinary Text Boxes, Callouts or unverified generic comments.
21. Do not claim Acrobat or InDesign compatibility without passing their respective manual gates. Neither PyMuPDF reopening nor Acrobat recognition proves InDesign import/`Accept` behavior.

Do not "temporarily" violate these rules to make a milestone easier.

## 4. Dependency policy

Keep dependencies minimal.

Default allowed runtime dependencies:

- PySide6
- PyMuPDF

Development/test dependencies may include pytest and small focused testing/linting tools if justified in `pyproject.toml`.

Before adding any other runtime dependency:

1. verify the standard library / PySide6 / PyMuPDF cannot reasonably solve the need;
2. explain the dependency's purpose in the task report;
3. keep it out if it only saves a small amount of code.

Do not replace the chosen stack without an explicit project decision.

---

## 5. Coding style

- Python 3.11+.
- Use type hints for public interfaces and DTOs.
- Prefer dataclasses/enums for IPC and domain models.
- Keep UI code, PDF engine code and pure geometry logic separate.
- Prefer small modules with one clear responsibility.
- Avoid giant controller classes.
- Avoid premature framework-style abstraction.
- No global mutable document state.
- No silent exception swallowing.
- User-facing errors must be concise; technical details go to local logs/tests.
- Comments should explain non-obvious invariants, not restate code.

---

## 6. UI rules

The UI is intentionally minimal.

- Do not introduce ribbon interfaces.
- Do not add permanent panels for settings that can be contextual.
- The creation surface has exactly seven tools: Pen, Text Box, Highlight, Callout, Delete Text / Strikethrough Text, Insert Text and Replace Text.
- Distinguish annotations/reporting (Pen, Highlight, Text Box, Callout) from applicable text corrections (Delete/Strikethrough, Insert, Replace) in the domain. This does not require two toolbars.
- A neutral pointer/selection mode is permitted.
- Sidebar is on the right and collapsible.
- Multiple open PDFs are represented as simple document tabs; only one document is visually active at a time.
- Switching tabs preserves each document's independent UI/session state.
- Search bar appears on demand.
- Scrolling is always continuous vertically.
- Maintain single, double and double-with-cover layouts.

When adding UI for a milestone, prefer the smallest interaction surface that satisfies the spec.

---

## 7. Rendering/performance rules

Performance work is part of correctness.

For any change touching rendering, scrolling, zoom, search, caches or IPC:

- consider stale/cancelled work;
- keep visible-page work highest priority;
- do not make cache unbounded;
- bound in-flight raster bytes and individual raster payload size, not only lookup-cache entries;
- enforce the raster-cache limit globally across documents rather than allocating the full budget per tab;
- stop speculative prefetch for inactive documents and aggressively trim their raster entries;
- avoid unnecessary alpha channel in ordinary page rasterization;
- avoid rerendering pages already cached at an acceptable key;
- do not schedule every intermediate zoom tick for definitive rendering;
- do not perform full-document text extraction during open.

If you add a cache, add a test for eviction/bounds.

If you add asynchronous/worker requests, add a test for stale response rejection where practical.

Profile before adding complex optimization such as shared memory, worker pools, or extra render processes beyond the required one-worker-per-open-document model.

---

## 8. PDF correctness rules

Supported annotation/reporting mapping:

- Pen → PDF Ink
- Highlight → PDF Highlight using ordered text quads
- Text Box → PDF FreeText
- Callout → PDF FreeText with native callout geometry

Required editorial semantics:

- Delete Text / Strikethrough Text → native Strikethrough comment requesting deletion of a confirmed range; validate the concrete StrikeOut encoding in the future interoperability spike.
- Insert Text → native comment requesting plain-text insertion at a confirmed text boundary.
- Replace Text → native comment requesting replacement of a confirmed range with plain text.

Do not prescribe an unverified PDF subtype/dictionary combination for Insert/Replace. The future encoding spike must prove Acrobat recognition and exact InDesign `Accept` behavior for all three corrections before their definitive interactive implementation. PyMuPDF remains the writer. Any necessary low-level dictionary work must be small, isolated inside the worker adapter, documented and tested; do not add another PDF library without demonstrated need and an explicit dependency decision.

Do not emulate annotations or corrections by drawing into page content and never flatten them. Original PDF text remains unchanged; accepting an editorial correction is a manual InDesign workflow outside this app.

All future tool implementations, including their test/spike callers, use the A-06 command/service domain: conceptually `CreateInk`, `CreateHighlight`, `CreateTextBox`, `CreateCallout`, `CreateDeleteText`, `CreateInsertText`, `CreateReplaceText`, plus permitted update/delete/undo operations. Completed M0 disposable probes are historical evidence, not an alternative product mutation path to extend or a requirement for retroactive refactoring.

Text commands must identify document/generation/page and a confirmed selection-derived range with ordered quads, or an insertion boundary with canonical geometry. A selected string alone is not a target. Preserve Insert/Replace payloads as Unicode plain text; do not apply search normalization to user correction text. Preview and commit must use the same confirmed target. Treat any multi-object encoding as one logical correction for sidebar, dirty state, history and persistence.

For Text Box/Callout, the **user-facing model is plain text**. A fixed safely escaped rich-text serialization may be used internally only when the pinned PyMuPDF API requires it to obtain independent fill/border appearance. Never expose arbitrary rich-text authoring.

Do not commit Text Box/Callout state that would silently clip user text in the saved PDF appearance.

Do not implement highlight as a rectangular saved overlay. Preview and commit use the same confirmed ordered quads.

Use the centralized canonical unrotated PyMuPDF coordinate contract so annotations survive zoom/layout/rotation/CropBox differences.

Untouched external annotations must not be `update()`d/rebuilt merely to normalize them. If an external annotation cannot be represented losslessly by the app model, keep it read-only.

When changing annotation code, add/maintain save-reopen integration tests for type, geometry, text/colors and preservation properties relevant to that milestone.

## 9. Tests are part of the task

Every behavioral implementation must add or update tests relevant to the milestone.

Before declaring a task complete:

1. run the targeted new tests;
2. run the full automated test suite unless there is a concrete reason not to;
3. report exact commands and outcomes;
4. report any manual test still required.

Never claim Adobe Acrobat compatibility solely because PyMuPDF can reopen the file, or InDesign compatibility because Acrobat recognizes the comment. The separate manual gates are defined in `ACCEPTANCE_TESTS.md`; new editorial coverage includes AT-EDIT-01..04, AT-CMD-01, AT-ACROBAT-06..09 and AT-INDESIGN-01..04. InDesign tests require the unchanged source/export pair, correct imported type, available `Accept`, and an exact resulting-text check. Missing Adobe applications or unperformed manual checks must be reported as NOT RUN/BLOCKED. Repeat the editorial gates after interactive implementation and again through production Save/Save As as sequenced in `ROADMAP.md`.

Do not weaken/remove a test just to make implementation pass unless the test contradicts the current spec; if so, explain the contradiction.

---

## 10. Working method for each milestone

Before editing:

1. read the current milestone definition in `ROADMAP.md`;
2. state which requirement/test IDs the milestone targets;
3. inspect only the relevant existing modules first;
4. identify architectural risks;
5. make the smallest coherent implementation plan.

During editing:

- keep diffs focused;
- avoid unrelated formatting/refactors;
- preserve public behavior outside the milestone;
- add instrumentation only when it supports debugging/performance acceptance.

After editing, report:

```text
Implemented
- ...

Files changed
- path: purpose

Tests
- command -> result

Acceptance status
- AT-... PASS/NOT RUN/BLOCKED

Known limitations / next milestone dependencies
- ...
```

Do not write a long narrative if this structured report is sufficient.

---

## 11. Git rules

- Do not commit unless explicitly asked.
- Do not push unless explicitly asked.
- Do not rewrite history.
- Do not delete user files or unrelated work.
- Do not mass-format the repository unless the task is specifically formatting.

If the working tree contains pre-existing changes, preserve them and distinguish your changes in the final report.

---

## 12. Security/privacy

This is a local personal tool.

- No telemetry.
- v1 makes no external API calls and uploads no documents.
- Post-v1 review may send only the bounded extracts and editorial inputs
  authorized by the explicit review start.
- No automatic network checks.
- Do not log document text/content by default.
- Development logs may record page numbers, timings, request ids and error types, but should avoid sensitive PDF content.

---

## 13. Source verification

For PyMuPDF or PySide6 API behavior that may vary by version, consult current official documentation before relying on a non-trivial API detail.

Important current references:

- PyMuPDF page/annotation API: https://pymupdf.readthedocs.io/en/latest/page.html
- PyMuPDF annotation recipes: https://pymupdf.readthedocs.io/en/latest/recipes-annotations.html
- PyMuPDF multiprocessing/threading: https://pymupdf.readthedocs.io/en/latest/recipes-multiprocessing.html
- PyMuPDF document/save API: https://pymupdf.readthedocs.io/en/latest/document.html
- PyMuPDF DisplayList: https://pymupdf.readthedocs.io/en/latest/displaylist.html
- Qt for Python: https://doc.qt.io/qtforpython-6/
- Adobe InDesign Import PDF Comments: https://helpx.adobe.com/indesign/desktop/collaborate-and-review/share-and-collaborate/import-pdf-comments.html

Do not copy example code blindly; adapt it to the architecture and tests.

---

## 14. Stop conditions

Stop and report rather than improvising if a task would require:

- violating the persistent one-worker-per-open-document PyMuPDF rule or the validator-only exception;
- blocking the Qt event loop on heavy PDF work or IPC;
- flattening annotations to obtain compatibility;
- bypassing the common annotation command/service layer or implementing PDF encoding in GUI handlers;
- substituting generic comments, Text Boxes or Callouts for native Insert/Replace semantics;
- beginning definitive interactive editorial tools before the encoding/interoperability gate passes, or claiming Adobe compatibility without its required manual evidence;
- adding AI/LLM, semantic document models, automation or InDesign integration without an explicit scope change;
- replacing PySide6/PyMuPDF;
- adding a major runtime dependency;
- changing the canonical coordinate contract;
- allowing multiple writable sessions to the same target file;
- changing a user-visible interaction defined in `PROJECT_SPEC.md`;
- broadening scope materially;
- risking destructive modification of source PDFs or deleting the only validated recovery candidate;
- proceeding into the next milestone without an explicit new task/approval.

A small incomplete milestone with correct architecture is preferable to a large implementation that violates project invariants.
