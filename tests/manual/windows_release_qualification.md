# M17B-C — Windows 11 release qualification

**Status:** COMPLETE WITH RECORDED LIMITATIONS

**Formal release target:** Windows 11 x64

**Compatibility evidence:** Windows 10 22H2 / build 19045, carried forward from
M17B-B; this is not a change to the normative platform requirement

**Qualified artifact:** `dist/windows/Windowed/fuPDF` from commit
`badebdf568ea4bd33879b9bbaf05364e52399659`

**Executable SHA-256:**
`062C4BFB4E8C672DC5D0BEB8CCF8A2761683B0AC083CC6D88FD8B5DF490E2EBB`

This document qualifies the existing unsigned one-folder build. It does not
rebuild the application, change packaging architecture, start MSIX/Store work,
or weaken Windows security policy.

## Authoritative manual closeout

Recorded on 2026-10-01 from the user-confirmed Windows 11 qualification run.

| Field | Result |
| --- | --- |
| Environment | Hyper-V clean VM; checkpoint `Win11 clean updated` |
| Windows | Windows 11 26H2, build 26300.9457, x64 |
| Clean-machine state | PASS — no functional Python, py launcher, PySide6, PyMuPDF, Source Sans 3, Courier Prime, Visual Studio or developer environment required; only the standard WindowsApps Store alias was present |
| Artifact | PASS — authoritative Windowed SHA-256 matched exactly |
| Application path | PASS — `C:\QA\fuPDF prova qualità\fuPDF\` |
| Working directory | PASS — launched from an unrelated working directory |
| Offline execution | PASS — Hyper-V network adapter disconnected before the functional run |
| Windows 11 / 100% | PASS — full core workflow |
| Windows 11 / 125% | PASS — DPI checklist and representative Open → annotate → Save As → close → reopen |
| Windows 11 / 150% | PASS — DPI checklist and representative workflow |
| Windows 11 / 200% | NOT RUN — user decision |
| Mixed-monitor 100%↔200% | NOT RUN |
| Formal 3× packaged/development comparison | NOT RUN |
| Source preservation | PASS — both original PDF hashes unchanged |
| M17B-C conclusion | COMPLETE WITH RECORDED LIMITATIONS |

The 100% run passed launch, absence of a console, branding/icon, graphite UI,
native Open/Save dialogs, independent two-document tabs, rendering, scrolling,
search, zoom, all three layouts, all seven annotation/correction tools, dirty
state, Save As, the frozen nested validator, saved-file reopening, spaces and
Unicode paths, arbitrary working directory, LocalAppData logging, clean
shutdown and zero remaining fuPDF processes.

Original source PDF SHA-256 values before and after the run were unchanged:

- `E876F865AE549949D403EA050997179AC30D01134FDF939D9499938641022C48`
- `90A80D4BC6E81D598A26DD57F928BE3858B6648CA58D26E73EC001A130670F91`

At 150%, Save As for a substantially longer PDF was noticeably slower but
completed successfully, after which normal closing became available. Because
the workload differed and was not measured at the other scale settings, this is
an informal performance observation only, not a DPI failure or evidence of a
DPI-related performance regression. No threshold or comparative conclusion is
inferred.

## Gate matrix

| Gate | Classification | Environment / evidence |
| --- | --- | --- |
| Artifact identity, resources, native inventory and provenance | automated/static | Existing Windowed artifact on the build host |
| Focused packaging/bootstrap tests | automated/static | Existing development environment |
| Windows 10 portable compatibility | local manual, carried forward | M17B-B Windows 10 Pro 19045 record |
| Windows 11 launch and full core workflow at 100% | clean-machine manual | PASS — clean Hyper-V Windows 11 VM |
| Spaces, Unicode, non-default paths, arbitrary working directory | clean-machine manual | PASS |
| No Python/PySide6/PyMuPDF/project fonts/developer tools; offline execution | clean-machine manual | PASS |
| DPI at 100/125/150/200% | clean-machine manual | 100/125/150% PASS; 200% NOT RUN |
| Mixed-monitor 100%↔200% transition | local manual | NOT RUN |
| Same-machine packaged/development startup and first-open comparison | local manual | NOT RUN |
| Final portable-output Acrobat checks | external compatibility gate | PENDING — AT-ACROBAT-01..09 |
| Editorial import and exact `Accept` results | external compatibility gate | PENDING — AT-INDESIGN-01..04 |

The absence of immediate InDesign access does not block completion of the
M17B-C Windows qualification. It remains an explicit pending gate for the final
M17 exit required by `ROADMAP.md`.

## Static qualification record

Run on the authoritative checkpoint without rebuilding:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest `
  tests/test_packaging_resources.py `
  tests/test_packaging_environment.py `
  tests/test_packaging_bootstrap.py -q

git diff --check

Get-FileHash -Algorithm SHA256 `
  'dist\windows\Windowed\fuPDF\fuPDF.exe'
```

Prepared result:

| Check | Result |
| --- | --- |
| Focused packaging/bootstrap tests | PASS — 20 passed in 0.25 s |
| Native inventory | PASS — 51 inventory entries and 51 bundled DLL/PYD files |
| Inventory target presence and SHA-256 | PASS — zero missing or mismatched files |
| Untracked native files | PASS — zero |
| Architecture | PASS — all inventory entries are x64 (`0x8664`) |
| Provenance | PASS — all sources under the repository `.venv` or `.runtime`; no Codex runtime, Poppler, libheif or jxrlib source |
| Fonts/licenses/branding | PASS — 12 source/bundle files present and byte-identical |
| Required Qt plugins | PASS — seven present, including `qwindows.dll` |
| PyMuPDF native files | PASS — `_extra.pyd`, `_mupdf.pyd`, `mupdfcpp64.dll` present and inventoried |
| `git diff --check` | PASS — exit 0 |
| Windowed executable SHA-256 | PASS — exact match with the value in the header |

The twelve resource files are eight physical TTF files, their two licenses,
`fupdf.png`, and the seven-frame `fupdf.ico`. The required plugin set is
`platforms/qwindows.dll`, `styles/qmodernwindowsstyle.dll`,
`imageformats/qico.dll`, `networkinformation/qnetworklistmanager.dll`, and the
three TLS backends retained by the shared PyInstaller specification.

## Windows 11 clean-machine procedure

### 1. Prepare and identify the environment

Use a clean Windows 11 x64 VM or separate machine. Use a normal unelevated user
for fuPDF. Do not install Python, PySide6, PyMuPDF, Source Sans 3, Courier Prime,
Visual Studio, Build Tools, or other developer tooling.

Record the following before copying fuPDF:

```powershell
$record = 'C:\QA\M17B-C'
New-Item -ItemType Directory -Path $record -Force

Get-ItemProperty `
  'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' |
  Select-Object ProductName, DisplayVersion, CurrentBuildNumber, UBR |
  Format-List | Tee-Object "$record\windows-version.txt"

Get-CimInstance Win32_ComputerSystem |
  Select-Object Manufacturer, Model, TotalPhysicalMemory |
  Format-List | Tee-Object "$record\machine.txt"

Get-CimInstance Win32_VideoController |
  Select-Object Name, DriverVersion, CurrentHorizontalResolution,
    CurrentVerticalResolution |
  Format-List | Tee-Object "$record\display.txt"

Get-Command python, py, pip -ErrorAction SilentlyContinue |
  Select-Object Name, Source |
  Format-List | Tee-Object "$record\python-command-check.txt"

Get-ItemProperty `
  'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*', `
  'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*', `
  'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*' `
  -ErrorAction SilentlyContinue |
  Where-Object DisplayName -Match 'Python|PySide|PyMuPDF|Visual Studio|Build Tools' |
  Select-Object DisplayName, DisplayVersion, Publisher |
  Format-Table -AutoSize | Out-String |
  Set-Content "$record\developer-software-check.txt"

Get-ItemProperty `
  'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts', `
  'HKCU:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts' `
  -ErrorAction SilentlyContinue |
  Out-String | Select-String 'Source Sans 3|Courier Prime' |
  Set-Content "$record\project-font-check.txt"
```

An empty command/software/font result is expected. A Windows Store execution
alias may appear for `python.exe`; record it, but do not install or invoke a
Python runtime. Also record VM product, CPU, assigned RAM, display resolution,
Windows edition/build, and whether the test is through a VM console or remote
session.

### 2. Smart App Control and unsigned-build policy

Record the visible state in **Windows Security → App & browser control → Smart
App Control**. Do not disable it or weaken any security setting.

If Smart App Control blocks `fuPDF.exe`, record:

- Windows edition/build and Smart App Control state;
- the exact Windows notification/error text;
- artifact SHA-256;
- result: `BLOCKED — unsigned portable distribution/signing constraint`.

This is not a fuPDF functional failure. Continue functional testing in a
separate isolated Windows 11 environment whose existing policy permits internal
unsigned executables. Trusted public distribution is deferred to later
MSIX/Microsoft Store work.

### 3. Transfer the complete one-folder artifact

Transfer the entire `fuPDF` directory, including `_internal`, without running
any installer. Place it in a non-default path that exercises spaces and Unicode,
for example:

```powershell
$qaRoot = 'C:\QA\fuPDF prova qualità'
$bundle = Join-Path $qaRoot 'fuPDF'
$exe = Join-Path $bundle 'fuPDF.exe'

Get-FileHash -Algorithm SHA256 -LiteralPath $exe |
  Format-List | Tee-Object 'C:\QA\M17B-C\artifact-hash.txt'

Get-ChildItem -LiteralPath $bundle -Recurse -File |
  Measure-Object Length -Sum |
  Format-List Count, Sum | Tee-Object 'C:\QA\M17B-C\bundle-size.txt'
```

The executable hash must be
`062C4BFB4E8C672DC5D0BEB8CCF8A2761683B0AC083CC6D88FD8B5DF490E2EBB`.
Do not copy only the executable.

Prepare disposable PDF copies with spaces and Unicode in their paths. Preserve
source hashes so the originals can be shown unchanged:

```powershell
$inputs = 'C:\QA\Documenti prova'
$outputs = 'C:\QA\Uscite qualità'
New-Item -ItemType Directory -Path $inputs, $outputs -Force

# Copy representative PDFs from the transfer medium to these disposable names.
$normalPdf = Join-Path $inputs 'prova normale è.pdf'
$longPdf = Join-Path $inputs 'prova lunga qualità.pdf'
Get-FileHash -Algorithm SHA256 -LiteralPath $normalPdf, $longPdf |
  Format-Table -AutoSize | Out-String |
  Set-Content 'C:\QA\M17B-C\input-hashes-before.txt'
```

### 4. Prove offline operation and arbitrary working directory

Disconnect the VM's virtual network adapter in the hypervisor, or physically
isolate the separate test machine. Do not change the development host. Record:

```powershell
Get-NetAdapter |
  Select-Object Name, InterfaceDescription, Status, LinkSpeed |
  Format-Table -AutoSize | Out-String |
  Set-Content 'C:\QA\M17B-C\network-state.txt'

$workingDirectory = Join-Path $env:TEMP 'fuPDF unrelated working directory'
New-Item -ItemType Directory -Path $workingDirectory -Force
$process = Start-Process -FilePath $exe `
  -WorkingDirectory $workingDirectory -PassThru
$process.Id | Set-Content 'C:\QA\M17B-C\initial-process-id.txt'
```

The application must launch without a console, network, installed developer
runtime, project fonts, elevation, or dependency prompt.

### 5. Full Windows 11 workflow at 100%

Set Windows display scaling to 100%, then restart fuPDF. Record scale,
resolution, and whether Windows required sign-out. Execute once:

1. Verify fuPDF name/icon, sparse chrome, document tabs and no console window.
2. Open the normal and long PDFs through the native file dialog. Confirm both
   tabs remain open and preserve independent page, zoom, layout, search,
   selection/sidebar and dirty state.
3. Scroll through representative early, middle and late pages; search for a
   visibly present term while scrolling; move between results.
4. Exercise single, double and double-with-cover layouts, then zoom in/out and
   confirm continuous scroll, stable geometry and responsive definitive render.
5. Create representative Pen, Highlight, Text Box, Callout, Delete Text, Insert
   Text and Replace Text comments through their normal UI flows. Confirm sidebar,
   selection, dirty state and independent tabs.
6. Save As to
   `C:\QA\Uscite qualità\salvato – prova completa.pdf`. Confirm the operation
   completes without blocking the UI, then close and reopen that output.
7. Verify comments, original PDF text, geometry, search, layouts and zoom after
   reopen. Confirm the source PDF hash is unchanged.
8. Check `%LOCALAPPDATA%\fuPDF\logs`: files exist, remain bounded/rotated as
   designed, and normal records do not contain document text.
9. Close all tabs and the application normally. Confirm no worker or validator
   remains:

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath $normalPdf, $longPdf |
  Format-Table -AutoSize | Out-String |
  Set-Content 'C:\QA\M17B-C\input-hashes-after.txt'

Get-ChildItem -LiteralPath "$env:LOCALAPPDATA\fuPDF\logs" -File `
  -ErrorAction SilentlyContinue |
  Select-Object Name, Length, LastWriteTime |
  Format-Table -AutoSize | Out-String |
  Set-Content 'C:\QA\M17B-C\logs.txt'

Get-Process fuPDF -ErrorAction SilentlyContinue |
  Select-Object Id, ProcessName, StartTime |
  Format-Table -AutoSize | Out-String |
  Set-Content 'C:\QA\M17B-C\processes-after-close.txt'
```

The final process listing must be empty. Successful Save As plus reopening is
the packaged nested-validator gate; validator internals are not inferred from
a merely present output file if the application reports failure.

### 6. DPI matrix

At each scale, start a fresh fuPDF process after applying the setting. At 100%
use the full workflow above. At 125%, 150% and 200%, perform the visual checklist
plus one representative **Open → annotate → Save As → close → reopen** flow,
using a distinct output filename for each scale.

For every scale record: Windows scale, resolution, display/driver, VM console or
remote session, maximized and minimum-practical-window screenshots, and PASS or
the exact defect for each observation:

- top chrome, menus and tabs: correct geometry, alignment and click targets;
- floating annotation palette: visible, anchored, non-overlapping and usable;
- context menus: correct cursor placement, label/icon/check alignment;
- native Open/Save dialogs: readable controls, filters and buttons, no clipping;
- Text Box, Callout, Insert and Replace editors: readable cursor/text/controls,
  working confirmation/cancel behavior;
- resize handles and hit targets: visible, reachable and geometrically accurate;
- page layouts and continuous scrolling: centered pages, stable gaps/current
  page, no drift across all three layouts;
- zoom: stable anchor and no inaccessible content;
- icons: present and acceptably sharp at the selected scale;
- labels and controls: no clipping, overlap or inaccessible control.

If suitable hardware or VM multi-display support exists, move a running window
between 100% and 200% monitors and repeat palette/menu/editor/hit-target checks.
Otherwise record `NOT RUN — suitable mixed-DPI display unavailable`; do not infer
a result from single-monitor scaling.

Only an observed reproducible defect authorizes a code change.

### 7. Same-machine performance observations

Use the development Windows machine, the same representative, authorized PDF for both builds, the same display settings and the same warm/cold
state policy. Manually record elapsed observations for:

- process launch → usable main window;
- Open command → first usable page.

Run three packaged observations and three development observations, alternating
variants where practical. Record all six raw pairs; do not average them into a
new release requirement and do not invent a numeric threshold. Investigate only
a clearly repeatable pathological packaged regression. Record CPU, RAM, storage,
Windows build, scale, artifact hash and development commit with the observations.

## Carried-forward Windows 10 evidence

M17B-B already established the following on Windows 10 Pro 22H2/build 19045 for
the same Windowed artifact and commit; do not repeat it from scratch:

- sanitized Qt probe, Console build and Windowed build launch;
- one GUI, no console and correct fuPDF branding;
- readable native Open and Save dialogs;
- multiple documents, rendering, search, all layouts and zoom;
- annotation flow;
- Save As, frozen nested validator and saved-PDF reopen;
- `%LOCALAPPDATA%` logging;
- normal shutdown with zero fuPDF orphan processes;
- full pytest suite PASS with only the intentional historical custom-Callout AP
  spike skipped.

This is compatibility-tested/known-working evidence only. Windows 11 remains
the formal target, and the evidence does not satisfy Windows 11 clean-machine,
offline, path or DPI qualification.

## Result record

| Field | Result |
| --- | --- |
| Result recorded | 2026-10-01 |
| Windows edition/display version/build | Windows 11 26H2 / 26300.9457 / x64 |
| VM or physical machine | Hyper-V clean VM; checkpoint `Win11 clean updated` |
| CPU/RAM/GPU/display resolution | Not supplied in the manual result |
| Scale | 100/125/150% PASS; 200% NOT RUN |
| Commit and executable SHA-256 | `badebdf568ea4bd33879b9bbaf05364e52399659`; authoritative SHA-256 matched |
| Smart App Control state/block result | State not supplied; artifact executed in the chosen isolated environment |
| Python/developer software/project font checks | PASS; WindowsApps Store alias only |
| Offline/network state | PASS; Hyper-V adapter disconnected |
| Launch/branding | PASS |
| Core workflow | PASS at 100% |
| Higher-DPI representative flow | PASS at 125/150%; 200% NOT RUN |
| Paths/arbitrary working directory | PASS |
| DPI checklist and screenshot locations | Visual checks PASS at 100/125/150%; locations not supplied |
| Logs | PASS |
| Zero processes after shutdown | PASS |
| Defects/blockers | None reported; three explicit omissions recorded below |

## Remaining external M17 gates

- **AT-ACROBAT-01..09:** create/save the final samples through this portable
  build, open them in current Acrobat Desktop, and perform the defined native
  annotation/editorial recognition and round-trip checks.
- **AT-INDESIGN-01..04:** use fresh copies of the unchanged InDesign source/PDF
  export pair for every case; record imported type, available `Accept`, exact
  resulting text, application versions and route. Until run, status is
  `NOT RUN/PENDING`, never inferred from Acrobat or PyMuPDF reopening.
- **Mixed-monitor 100%↔200%:** optional/pending when appropriate display support
  is unavailable; record the actual availability.
- **200% DPI:** NOT RUN by user decision.
- **Formal 3× packaged/development comparison:** NOT RUN. The longer 150% Save As
  is retained only as the informal observation recorded above.

M17B-C Windows qualification is complete with these recorded limitations,
independently of unavailable InDesign access. It does not close the overall M17
exit gate or create a release/tag. AT-ACROBAT-01..09 and AT-INDESIGN-01..04
remain explicit external M17 gates until actually executed.
