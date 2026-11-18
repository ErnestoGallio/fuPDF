"""Narrow Windows packaging environment and native-source safety gates."""

import argparse
import ast
import hashlib
import json
import ntpath
import os
from pathlib import Path
import sys

CLEAR = ("PYTHONPATH", "PYTHONHOME", "QT_PLUGIN_PATH", "QML2_IMPORT_PATH",
         "QML_IMPORT_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH")


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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("environment", "preflight", "provenance"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--toc", type=Path)
    parser.add_argument("--bundle", type=Path)
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
    else:
        data = provenance(args.toc, args.bundle)
    args.output.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"{args.action}: PASS ({args.output})")


if __name__ == "__main__":
    main()
