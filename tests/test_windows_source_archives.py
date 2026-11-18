import hashlib
import json
from pathlib import Path
import runpy

import pytest


STAGE = runpy.run_path(str(Path(__file__).resolve().parents[1] /
                          "packaging/windows/stage_source_archives.py"))["stage_sources"]


def test_stage_source_archives_copies_only_verified_inputs(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    data = b"synthetic source archive"
    name = "sample.tar.xz"
    digest = hashlib.sha256(data).hexdigest()
    (cache / name).write_bytes(data)
    (cache / "private.log").write_bytes(b"fixture outside inventory")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"archives": [{"filename": name, "sha256": digest}]}))
    output = tmp_path / "output"
    STAGE(manifest, [cache], output)
    assert {path.name for path in output.iterdir()} == {name, "source-archives.json", "SHA256SUMS"}
    assert (output / name).read_bytes() == data
    assert (output / "SHA256SUMS").read_text() == f"{digest}  {name}\n"


@pytest.mark.parametrize("problem", ["missing", "changed", "path"])
def test_stage_source_archives_rejects_invalid_inputs_before_creating_output(tmp_path, problem):
    name = "../sample.tar.xz" if problem == "path" else "sample.tar.xz"
    if problem == "changed":
        (tmp_path / name).write_bytes(b"unexpected")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"archives": [{"filename": name, "sha256": "0" * 64}]}))
    output = tmp_path / "output"
    error = FileNotFoundError if problem == "missing" else ValueError
    with pytest.raises(error):
        STAGE(manifest, [tmp_path], output)
    assert not output.exists()


def test_export_release_source_reads_commit_and_links_archive(tmp_path):
    import subprocess
    from zipfile import ZipFile

    repo = Path(__file__).resolve().parents[1]
    if not (repo / ".git").exists():
        pytest.skip("A checkout is required to exercise Git archive")
    git = ["git", "-C", str(repo)]
    commit = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
    export = STAGE.__globals__["export_release_source"]
    export(repo, commit, tmp_path)
    receipt = json.loads((tmp_path / "RELEASE-SOURCE.json").read_bytes())
    assert receipt["commit"] == commit
    assert receipt["tree"] == subprocess.check_output(
        [*git, "rev-parse", "HEAD^{tree}"], text=True).strip()
    archive = tmp_path / receipt["source_archive"]
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == receipt["source_archive_sha256"]
    with ZipFile(archive) as snapshot:
        assert snapshot.read("app/README.md") == subprocess.check_output(
            [*git, "show", "HEAD:README.md"])
    with pytest.raises(FileExistsError):
        export(repo, commit, tmp_path)
