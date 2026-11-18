# M17B-B — First portable Windows build: COMPLETE

Authoritative closeout: user-confirmed manual validation on **Windows 10 Pro
build 19045**. This checkpoint closes M17B-B, not all of M17. **M17B-C NOT STARTED**.

## Implementation

- PyInstaller one-folder selected; one shared `fupdf.spec` for Console/Windowed.
  Build pins: PyInstaller 6.22.3, hooks-contrib 2026.8 (complete list in
  `packaging/windows/requirements-build.txt`). Python 3.11.9 x64, PySide6/Shiboken
  6.11.2 and PyMuPDF 1.28.2 remain unchanged.
- `launcher.py` dispatches frozen multiprocessing children before GUI import.
  Existing one-worker-per-document and nested save-validator architecture retained.
- `build.ps1` sanitizes the process environment and restores caller variables;
  `build_environment.py` checks search paths and final native provenance/hashes.
- fuPDF application identity, supplied PNG, seven-size ICO and version metadata
  from pyproject.toml (0.0.0); eight physical canonical fonts and both licenses.
- PyMuPDF `_extra.pyd`, `_mupdf.pyd`, `mupdfcpp64.dll` verified. Qt filtering keeps
  Core/Gui/Widgets/Network and required platform/style/image/network/TLS plugins,
  excluding unused QtPdf/QML/Quick/virtual-keyboard branches.
- Windowed per-PID rotating logs in `%LOCALAPPDATA%\fuPDF\logs` (128 KiB plus one
  backup; temporary-directory fallback). Missing standard streams are handled.
- Open/Save As retain asynchronous QFileDialog instances and existing services.
  Removed Open's explicit DontUseNativeDialog flag; native Windows controls own
  their colors. Graphite QSS, cancellation, filters and save semantics unchanged.

## Final validation

| Check | Result |
| --- | --- |
| QtProbeSanitized | PASS |
| ConsoleSanitized one-folder | PASS |
| Windowed one-folder, no console, one GUI, branding | PASS |
| Native readable Windows Open/Save dialogs | PASS |
| Multiple documents, rendering, search, layouts, zoom | PASS |
| Annotation flow | PASS |
| Save As, nested frozen validator, saved PDF reopening | PASS |
| LocalAppData logging | PASS |
| Normal shutdown, zero fuPDF processes/orphans | PASS |
| Complete pytest suite, manually run and reported by user | PASS; only known intentional historical custom-Callout AP spike skipped |

These are packaged smoke results, including the relevant DOC/open/multi-document,
SAVE/validator and lifecycle behavior. They do not substitute for all broader
stress/performance, Windows 11/DPI or Adobe release gates.

Static validation/provenance PASS: 51 x64 native DLL/PYD files in fuPDF, identical
between ConsoleSanitized and Windowed. No Codex/Poppler/libheif/jxrlib source,
incompatible bundled ICU, foreign UCRT or API-set DLLs. Resources match source;
metadata is fuPDF / 0.0.0. Windowed PE subsystem 2; Console subsystem 3.
Final Windowed EXE SHA-256:
`062c4bfb4e8c672dc5d0beb8ccf8a2761683b0ac083cc6d88fd8b5df490e2ebb`.

Earlier local execution was blocked by Smart App Control; security settings were
not changed. Successful target validation above supersedes the historical blocker
and pending smoke records. Contaminated artifacts are not distribution candidates;
see [resolved environment diagnosis](windows_build_environment.md).

## Reproduce and transfer

From the repository root with its pinned `.venv`:

```powershell
.venv/Scripts/python.exe -m pip install -r packaging/windows/requirements-build.txt
powershell.exe -NoProfile -ExecutionPolicy Bypass -File packaging/windows/build.ps1 -Probe
powershell.exe -NoProfile -ExecutionPolicy Bypass -File packaging/windows/build.ps1 -Mode Console
powershell.exe -NoProfile -ExecutionPolicy Bypass -File packaging/windows/build.ps1 -Mode Windowed
```

Outputs (transfer entire application folder including `_internal`):

- `dist/windows/Diagnostics/QtProbeSanitized/QtProbeSanitized.exe`
- `dist/windows/ConsoleSanitized/fuPDF/fuPDF.exe`
- `dist/windows/Windowed/fuPDF/fuPDF.exe`

Build-generated `environment.json` and `native-inventory.json` record discovery
and provenance; additional local static reports remain ignored with build/dist.
`build/`, `dist/`, logs, temporary diagnostics and target artifacts are not tracked.
Version resource and ICO are intentional reproducible packaging inputs.

Focused development checks passed: 20 packaging/bootstrap/resource/environment
tests and 13 directly affected dialog/save/chrome tests. The final complete suite
PASS is user-reported; an exact count was not supplied. No pytest rerun at closeout.
Closeout commands:

```powershell
.venv/Scripts/python.exe -m compileall -q src
.venv/Scripts/python.exe -m pip check
git diff --check
```

All PASS (diff check may show line-ending conversion notices).

## Remaining qualification and subsequent phases

M17B-C has not started. Pending: Windows 11 clean-machine qualification, DPI
100/125/150/200% and mixed-monitor behavior, and remaining M17 release acceptance
including relevant performance/stress and separate Adobe gates. Do not infer these
from the Windows 10 smoke. No all-M17 completion claim is made.

MSIX/Microsoft Store packaging is a subsequent phase. Microsoft developer account
exists; no Store implementation or submission has started. No release or tag is
created for this checkpoint.
