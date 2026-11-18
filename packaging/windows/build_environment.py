"""Narrow Windows packaging environment and native-source safety gates."""

import argparse
import ast
import hashlib
from importlib.metadata import distribution
import json
import ntpath
import os
from pathlib import Path
import re
import sys
import zipfile

CLEAR = ("PYTHONPATH", "PYTHONHOME", "QT_PLUGIN_PATH", "QML2_IMPORT_PATH",
         "QML_IMPORT_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH")


SYSTEM_VC_RUNTIME_DLLS = frozenset({
    "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
    "vcruntime140.dll", "vcruntime140_1.dll",
})


def is_vc_runtime_path(path: str | Path) -> bool:
    return ntpath.basename(str(path)).casefold() in SYSTEM_VC_RUNTIME_DLLS


def check_no_app_local_vc_runtime(bundle: Path) -> None:
    from pefile import PE

    for relative in bundle_files(bundle):
        if is_vc_runtime_path(relative):
            raise RuntimeError(f"App-local VC++ runtime in portable folder: {relative}")
        if relative.suffix.lower() not in {".dll", ".pyd", ".exe"}:
            continue
        # Version resources detect a renamed copy of one of the excluded DLLs.
        with PE(str(bundle / relative), fast_load=True) as binary:
            binary.parse_data_directories(directories=[2])
            for group in getattr(binary, "FileInfo", []):
                for info in group:
                    for table in getattr(info, "StringTable", []):
                        name = table.entries.get(b"OriginalFilename", b"").decode("ascii", "replace")
                        if is_vc_runtime_path(name):
                            raise RuntimeError(f"Renamed app-local VC++ runtime: {relative}")


def forbidden(path: str) -> bool:
    """Check both spelling and resolved junction/symlink target, case-insensitively."""
    for candidate in (path, str(Path(path).resolve())):
        parts = ntpath.normpath(candidate).replace("/", "\\").casefold().split("\\")
        if any(part in {"poppler", "libheif", "jxrlib"} for part in parts):
            return True
        if any(a == ".cache" and b == "codex-runtimes"
               for a, b in zip(parts, parts[1:])):
            return True
    return False


def allowed_path(scripts: Path, base: Path, windows: Path) -> str:
    paths = [scripts, base, base / "DLLs", windows / "System32", windows]
    resolved = [str(path.resolve()) for path in paths]
    if any(forbidden(path) for path in resolved):
        raise RuntimeError("Forbidden directory in required build paths")
    return os.pathsep.join(dict.fromkeys(resolved))


def preflight(environment: dict[str, str], search_paths: list[str], expected: str) -> None:
    if environment.get("PATH") != expected:
        raise RuntimeError("Build PATH differs from explicit allowlist")
    if any(environment.get(name) for name in CLEAR):
        raise RuntimeError("Python/Qt discovery override still set")
    for path in environment["PATH"].split(os.pathsep) + search_paths:
        if path and forbidden(path):
            raise RuntimeError(f"Forbidden build search path: {path}")


def native_entries(toc: object):
    if isinstance(toc, (tuple, list)):
        if len(toc) == 3 and all(isinstance(item, str) for item in toc):
            if toc[2] in {"BINARY", "EXTENSION"}:
                yield toc
        else:
            for item in toc:
                yield from native_entries(item)


def provenance(toc: Path, bundle: Path) -> list[dict[str, str]]:
    records = []
    for destination, source, kind in native_entries(ast.literal_eval(toc.read_text(encoding="utf-8"))):
        if forbidden(source):
            raise RuntimeError(f"Forbidden native source: {source}")
        collected = bundle / "_internal" / destination
        source_path = Path(source).resolve()
        source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        if hashlib.sha256(collected.read_bytes()).hexdigest() != source_hash:
            raise RuntimeError(f"Native source/bundle mismatch: {destination}")
        records.append(dict(destination=destination, source=str(source_path),
                            kind=kind, sha256=source_hash))
    known = {ntpath.normcase(record["destination"]) for record in records}
    for file in (bundle / "_internal").rglob("*"):
        if file.suffix.lower() in {".dll", ".pyd"}:
            if ntpath.normcase(str(file.relative_to(bundle / "_internal"))) not in known:
                raise RuntimeError(f"Native file without provenance: {file}")
    if not records:
        raise RuntimeError("No native provenance entries found")
    return records


def bundle_files(bundle: Path) -> set[Path]:
    paths = set()
    for path in bundle.rglob("*"):
        if path.is_symlink() or path.is_junction():
            raise RuntimeError(f"Link in portable folder: {path.relative_to(bundle)}")
        if path.is_file():
            paths.add(path.relative_to(bundle))
    return paths


def check_bundle_contents(bundle: Path, expected: set[Path], private_paths: tuple[Path, ...]) -> None:
    actual = bundle_files(bundle)
    if actual != expected:
        unexpected = sorted(path.as_posix() for path in actual - expected)
        missing = sorted(path.as_posix() for path in expected - actual)
        raise RuntimeError(f"Portable contents mismatch: unexpected={unexpected}, missing={missing}")
    locations = [
        spelling.lower().encode(encoding)
        for path in private_paths
        for spelling in (str(path), path.as_posix())
        for encoding in ("utf-8", "utf-16-le")
    ]
    secret = re.compile(
        rb"-----BEGIN (?P<key>(?:[A-Z0-9]+ )?PRIVATE KEY)-----\r?\n"
        rb"(?:[A-Za-z0-9+/=]{16,128}\r?\n)+-----END (?P=key)-----"
        rb"|(?<![a-zA-Z0-9_-])(?:gh[pousr]_|github_pat_|sk-(?:proj|svcacct|admin)-)"
        rb"[a-zA-Z0-9_-]{24,200}(?![a-zA-Z0-9_-])"
    )
    def check(data: bytes, name: str) -> None:
        lowered = data.lower()
        if any(location in lowered for location in locations) or secret.search(data):
            raise RuntimeError(f"Private material in portable file: {name}")

    for path in actual:
        filename = bundle / path
        check(filename.read_bytes(), path.as_posix())
        if path == Path("_internal/base_library.zip"):
            with zipfile.ZipFile(filename) as modules:
                for name in modules.namelist():
                    if not re.fullmatch(r"(?:[A-Za-z_]\w*/)*[A-Za-z_]\w*\.pyc", name, re.ASCII):
                        raise RuntimeError(f"Unexpected stdlib ZIP member: {name}")
                    check(modules.read(name), name)
        if path == Path("fuPDF.exe"):
            from PyInstaller.archive.readers import CArchiveReader

            executable = CArchiveReader(str(filename))
            for name, entry in executable.toc.items():
                if entry[-1] not in {"s", "m", "M", "z"}:
                    raise RuntimeError(f"Unexpected embedded EXE item: {name}")
                if entry[-1] != "z":
                    check(executable.extract(name), name)
            modules = executable.open_embedded_archive("PYZ.pyz")
            for name in modules.toc:
                data = modules.extract(name, raw=True)
                if data is not None:
                    check(data, name)


def prepare_portable(bundle: Path, records: list[dict[str, str]], root: Path,
                     *, system_vc_runtime: bool = False) -> None:
    site = Path(sys.prefix) / "Lib/site-packages"
    base = Path(sys.base_prefix)
    assets = Path("pdf_annotator/assets")
    expected = {Path("fuPDF.exe"), Path("_internal/base_library.zip")}

    def verify_asset(relative: Path, source: Path) -> None:
        destination = Path("_internal") / relative
        if not (bundle / destination).is_file() or (
            (bundle / destination).read_bytes() != source.read_bytes()
        ):
            raise RuntimeError(f"Portable resource mismatch: {relative.as_posix()}")
        expected.add(destination)

    for entry in records:
        path = Path(entry["destination"])
        if (path.anchor or ".." in path.parts or path.suffix.lower() not in {".dll", ".pyd"}):
            raise RuntimeError(f"Unexpected native file in portable folder: {path}")
        source = Path(entry["source"]).resolve()
        if len(path.parts) == 1:
            allowed = {base / path.name, base / "DLLs" / path.name}
        elif path.parts[0] in {"PySide6", "shiboken6", "pymupdf"}:
            allowed = {site / path}
        else:
            allowed = set()
        if source not in {file.resolve() for file in allowed}:
            raise RuntimeError(f"Unapproved native source for {path.as_posix()}")
        expected.add(Path("_internal") / path)

    for family in ("CourierPrime", "SourceSans3"):
        for face in ("Regular", "Bold", "Italic", "BoldItalic"):
            relative = assets / "fonts" / f"{family}-{face}.ttf"
            verify_asset(relative, root / "src" / relative)
    for relative in (
        assets / "fonts/COURIER_PRIME_OFL.txt",
        assets / "fonts/SOURCE_SANS_3_LICENSE.md",
        assets / "branding/fupdf.ico",
        assets / "branding/fupdf.png",
        Path("pdf_annotator/services/review_contract/prompt.txt"),
    ):
        verify_asset(relative, root / "src" / relative)
    translation_dir = site / "PySide6/translations"
    for catalog in translation_dir.glob("*.qm"):
        if re.fullmatch(r"(?:qt_help|qtbase|qt)_[A-Za-z0-9_]+\.qm", catalog.name):
            relative = Path("PySide6/translations") / catalog.name
            verify_asset(relative, site / relative)

    private_paths = (root, Path(os.environ["USERPROFILE"]))
    check_bundle_contents(bundle, expected, private_paths)
    if system_vc_runtime:
        check_no_app_local_vc_runtime(bundle)
    licenses = (
        "GPL-3.0-only.txt", "LGPL-3.0-only.txt", "libffi-3.4.4.txt", "XZ-5.2.5.txt",
        "Mesa-11.2.2-core.txt", "Mesa-11.2.2-llvmpipe-WGL.txt", "LLVM-3.6.2.txt", "OpenSSL-3.5.9.txt", "zlib-ng-2.2.4.txt",
        "zstd-1.5.7.txt", "Expat-2.9.0.txt", "mpdecimal-2.5.1.txt", "MuPDF-1.28.2.txt",
        "MuPDF-1.28.2-vendored-notices.txt", "QtBase-6.11.2-vendored-notices.txt",
        "MuPDF-1.28.2-font-notices.txt",
    )
    notices = {
        Path("LICENSE"): root / "LICENSE",
        Path("THIRD_PARTY_NOTICES.md"): root / "packaging/windows/THIRD_PARTY_NOTICES.md",
        Path("LICENSES/CPython-3.14.10.txt"): base / "LICENSE.txt",
        Path("LICENSES/PyMuPDF-1.28.2.txt"): Path(distribution("PyMuPDF").locate_file(
            "pymupdf-1.28.2.dist-info/COPYING")),
        Path("LICENSES/PyInstaller-6.22.3.txt"): root / "LICENSES/PyInstaller-6.22.3.txt",
        **{Path("LICENSES") / name: root / "LICENSES" / name for name in licenses},
    }
    if system_vc_runtime:
        notices[Path("WINDOWS-PORTABLE.md")] = root / "packaging/windows/WINDOWS-PORTABLE.md"
    for path, source in notices.items():
        data = source.read_bytes()
        if not data.strip():
            raise RuntimeError(f"Empty portable notice: {path}")
        destination = bundle / path
        destination.parent.mkdir(exist_ok=True)
        destination.write_bytes(data)
    check_bundle_contents(bundle, expected | notices.keys(), private_paths)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("environment", "preflight", "provenance", "portable"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--toc", type=Path)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--system-vc-runtime", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    expected = allowed_path(root / ".venv/Scripts", Path(sys.base_prefix), Path(os.environ["SystemRoot"]))
    if args.action == "environment":
        print(json.dumps({"PATH": expected, **{key: None for key in CLEAR}}))
        return
    if args.action == "preflight":
        preflight(dict(os.environ), sys.path, expected)
        dll_directories = []
        sys.addaudithook(lambda event, values: dll_directories.append(str(values[0]))
                        if event == "os.add_dll_directory" else None)
        import PySide6
        from PySide6 import QtCore, QtGui, QtWidgets
        for path in dll_directories + os.environ["PATH"].split(os.pathsep):
            if forbidden(path):
                raise RuntimeError(f"Forbidden Qt DLL search path: {path}")
        data = dict(path=expected.split(os.pathsep), sys_path=sys.path,
                    cleared=list(CLEAR), add_dll_directory=dll_directories,
                    qt_path=os.environ["PATH"].split(os.pathsep),
                    python=sys.version, pyside=PySide6.__version__, qt=QtCore.qVersion())
    elif args.action == "portable":
        records = json.loads(args.inventory.read_text(encoding="utf-8"))
        prepare_portable(args.bundle, records, root, system_vc_runtime=args.system_vc_runtime)
        print("portable: PASS")
        return
    else:
        data = provenance(args.toc, args.bundle)
    args.output.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"{args.action}: PASS ({args.output})")


if __name__ == "__main__":
    main()
