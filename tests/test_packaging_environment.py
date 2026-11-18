import os
from pathlib import Path
import runpy

import pytest

HELPER = runpy.run_path(str(Path(__file__).resolve().parents[1] /
                            "packaging/windows/build_environment.py"))


def test_allowlist_does_not_inherit_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", r"D:\tools\poppler\bin;D:\compiler\bin")
    result = HELPER["allowed_path"](tmp_path / "venv", tmp_path / "python", tmp_path / "Windows")
    assert result.split(os.pathsep) == [str(tmp_path / part) for part in
        ("venv", "python", "python/DLLs", "Windows/System32", "Windows")]


@pytest.mark.parametrize("path", [r"D:\OtherUser\.CACHE\codex-runtimes\native\x.dll",
    "D:/tools/POppler/Library/bin", r"D:\tools\libheif\bin", r"D:\tools\jxrlib\bin",
    r"D:\tools\other\..\poppler\bin"])
def test_forbidden_paths_are_structural(path):
    assert HELPER["forbidden"](path)


def test_normal_runtime_paths_are_allowed():
    for path in (r"C:\Windows\System32", r"D:\Python311", r"D:\my-poppler-report"):
        assert not HELPER["forbidden"](path)


def test_resolved_target_is_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "resolve", lambda self: Path(r"D:\tools\libheif\bin"))
    assert HELPER["forbidden"](str(tmp_path / "alias"))


def test_preflight_rejects_overrides_and_site_path():
    check = HELPER["preflight"]
    check({"PATH": "clean"}, ["ordinary"], "clean")
    for environment, paths in [({"PATH": "dirty"}, []),
                               ({"PATH": "clean", "PYTHONHOME": "other"}, []),
                               ({"PATH": "clean"}, [r"D:\poppler\bin"])]:
        with pytest.raises(RuntimeError):
            check(environment, paths, "clean")


def test_provenance_requires_matching_known_native_sources(tmp_path):
    source = tmp_path / "Python.dll"
    source.write_bytes(b"native fixture")
    bundle = tmp_path / "bundle"
    internal = bundle / "_internal"
    internal.mkdir(parents=True)
    collected = internal / source.name
    collected.write_bytes(source.read_bytes())
    toc = tmp_path / "COLLECT-00.toc"
    toc.write_text(repr(([(source.name, str(source), "BINARY")],)), encoding="utf-8")
    assert len(HELPER["provenance"](toc, bundle)) == 1
    collected.write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="mismatch"):
        HELPER["provenance"](toc, bundle)
    collected.write_bytes(source.read_bytes())
    (internal / "unknown.dll").write_bytes(b"unexpected")
    with pytest.raises(RuntimeError, match="without provenance"):
        HELPER["provenance"](toc, bundle)
    toc.write_text(repr(([(source.name, r"D:\poppler\Python.dll", "BINARY")],)), encoding="utf-8")
    with pytest.raises(RuntimeError, match="Forbidden native source"):
        HELPER["provenance"](toc, bundle)
