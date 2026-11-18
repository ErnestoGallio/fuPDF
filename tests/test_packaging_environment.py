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


@pytest.mark.parametrize("name", [
    "environment.json", "native-inventory.json", ".env", "debug.log",
    "_internal/editorial.pdf", "_internal/unexpected.dll",
])
def test_portable_contents_rejects_unlisted_files(tmp_path, name):
    unexpected = tmp_path / name
    unexpected.parent.mkdir(parents=True, exist_ok=True)
    unexpected.write_bytes(b"fixture")
    with pytest.raises(RuntimeError, match="Portable contents mismatch"):
        HELPER["check_bundle_contents"](tmp_path, set(), ())


@pytest.mark.parametrize("payload", ["local-path", "private-key-header"])
def test_portable_contents_rejects_sensitive_bytes(tmp_path, payload):
    path = Path("THIRD_PARTY_NOTICES.md")
    home = Path(r"D:\Users\Example")
    data = (str(home).encode("utf-16-le") if payload == "local-path"
            else b"-----BEGIN PRIVATE KEY-----\nQUJDREVGR0hJSktMTU5PUA==\n-----END PRIVATE KEY-----")
    (tmp_path / path).write_bytes(data)
    with pytest.raises(RuntimeError, match="Private material"):
        HELPER["check_bundle_contents"](tmp_path, {path}, (home,))


def test_portable_checks_members_of_stdlib_zip(tmp_path):
    from zipfile import ZipFile

    path = Path("_internal/base_library.zip")
    (tmp_path / path).parent.mkdir()
    with ZipFile(tmp_path / path, "w") as archive:
        archive.writestr("pkg/allowed.pyc", b"sample")
    HELPER["check_bundle_contents"](tmp_path, {path}, ())
    with ZipFile(tmp_path / path, "a") as archive:
        archive.writestr("pkg/private.pdf", b"sample")
    with pytest.raises(RuntimeError, match="Unexpected stdlib ZIP member"):
        HELPER["check_bundle_contents"](tmp_path, {path}, ())


def test_portable_distinguishes_symbols_from_provider_tokens(tmp_path):
    path = Path("resource.bin")
    resource = tmp_path / path
    prefix = b"gh" + b"u_"
    resource.write_bytes(b"symbol_" + prefix + b"a" * 792)
    HELPER["check_bundle_contents"](tmp_path, {path}, ())
    resource.write_bytes(b"\x00" + prefix + b"A0" * 18 + b"\x00")
    with pytest.raises(RuntimeError, match="Private material"):
        HELPER["check_bundle_contents"](tmp_path, {path}, ())
