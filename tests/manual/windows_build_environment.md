# M17B-B — Resolved native collection contamination

**Resolved and target-validated. M17B-B COMPLETE.** Current final results and
commands are in [the packaging record](windows_packaging.md).

## Root cause

Inherited PATH placed these development-runtime directories ahead of System32:

```text
%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\libheif\libheif\bin
%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\jxrlib\jxrlib\bin
%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\poppler\Library\bin
```

PyInstaller's native resolver falls back to PATH. It collected Poppler ICU 78.3.0.0
with suffixed exports, incompatible with Qt's 20 unsuffixed ICU imports, plus
libheif ucrtbase and 42 API-set DLLs. Native Qt imports on Windows 10 passed while
both default/filtered frozen probes failed: application filtering was not the cause.
PYTHONPATH/PYTHONHOME/QT_PLUGIN_PATH/QML2_IMPORT_PATH were absent in that environment.
PySide6 itself added only its wheel directory to PATH and sibling shiboken6 through
os.add_dll_directory. Codex CLI/node/tectonic variables were recorded locally;
there was no evidence they directly affected DLL resolution beyond inherited PATH.

## Deterministic build boundary

`build.ps1` derives a fresh PATH using isolated stdlib startup (`python -I -S`):
repository `.venv/Scripts`, base Python and its DLLs directory, Windows System32,
and Windows. No arbitrary caller tools are retained. Subsequent Python build
commands use `-I`. PYTHONPATH, PYTHONHOME, QT_PLUGIN_PATH, QML2_IMPORT_PATH,
QML_IMPORT_PATH and QT_QPA_PLATFORM_PLUGIN_PATH are cleared in the script only.
All affected caller environment values are restored in `finally`; no permanent
user/system environment modification occurs.

Preflight checks PATH/sys.path and observed Qt search paths using normalized and
resolved paths (including junction targets). Final COLLECT provenance rejects
Codex runtime cache / Poppler / libheif / jxrlib sources, checks source/bundle
SHA-256 equality, and rejects DLL/PYD files without provenance. Legitimate Python
and system runtime sources remain allowed. This is not a generic dependency scanner.

Sanitized stock-hook probe and both fuPDF variants pass provenance. No bundled
ICU, ucrtbase or API-set contamination remains; Qt resolves ICU through Windows.
The build host's system ICU shim and its forwarded exports satisfy all 20 names.
User-confirmed Windows 10 Pro 19045 probe/Console/Windowed execution establishes
the target result beyond static inspection. No dependency downgrade or manual
replacement of dist DLLs was needed.

The separate File/Open theme regression was an explicit DontUseNativeDialog flag,
removed while keeping asynchronous open(). Save As already used the native path.
Both pickers now pass target validation; application graphite QSS remains intact.

Complete raw environment inventories, original failed bundles and PE diagnostics
are local ignored build/dist evidence, not tracked packaging inputs or releases.
No developer-specific absolute checkout path is required by the build scripts.
