# One configuration for both diagnostic console and windowed portable builds.
import os
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parents[1]
mode = os.environ.get("FUPDF_BUILD_MODE", "Windowed")
if mode not in ("Console", "Windowed"):
    raise ValueError("FUPDF_BUILD_MODE must be Console or Windowed")
assets = ROOT / "src/pdf_annotator/assets"

a = Analysis(
    [str(ROOT / "packaging/windows/launcher.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[(str(assets / "fonts"), "pdf_annotator/assets/fonts"),
           (str(assets / "branding/fupdf.png"), "pdf_annotator/assets/branding"),
           (str(assets / "branding/fupdf.ico"), "pdf_annotator/assets/branding"),
           (str(ROOT / "src/pdf_annotator/services/review_contract/prompt.txt"),
            "pdf_annotator/services/review_contract")],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[str(ROOT / "packaging/windows/windowed_logging.py")],
    excludes=["tests", "pdf_annotator.spikes", "pdf_annotator.worker.process"],
    noarchive=False,
)
# The first normal-hook build pulled QtPdf via qpdf and QML/Quick via the
# virtual-keyboard plugin. This Widgets app uses none of these. Keep the
# Windows platform/style and ICO reader, plus QtNetwork's normal hook closure.
# PE imports of the retained GUI/Widgets/platform/style binaries were checked.
unused_qt = {"qt6pdf.dll", "qt6quick.dll", "qt6virtualkeyboard.dll",
             "qt6svg.dll", "qt6opengl.dll"}
kept_gui_plugins = {"platforms/qwindows.dll", "styles/qmodernwindowsstyle.dll",
                    "imageformats/qico.dll"}

def needed_binary(entry):
    path = entry[0].replace("\\", "/").lower()
    filename = path.rsplit("/", 1)[-1]
    if filename in unused_qt or filename.startswith("qt6qml"):
        return False
    prefix = "pyside6/plugins/"
    if path.startswith(prefix):
        plugin = path[len(prefix):]
        return plugin in kept_gui_plugins or plugin.startswith(("tls/", "networkinformation/"))
    return True

a.binaries = [entry for entry in a.binaries if needed_binary(entry)]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="fuPDF",
    debug=False,
    strip=False,
    upx=False,
    console=mode == "Console",
    icon=str(assets / "branding/fupdf.ico"),
    version=str(ROOT / "packaging/windows/version_info.txt"),
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="fuPDF")
